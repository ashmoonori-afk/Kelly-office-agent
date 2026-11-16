"""Bounded automation decisions, registered-folder observations, and one durable scheduler tick."""

import copy
import hashlib
import os
import sqlite3
import stat
from contextlib import ExitStack
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, TypeAdapter, ValidationError, field_validator, model_validator

from .contracts import AppError, Id, IsoDateTime, Sha256, StrictModel, Text, digest

Weekday = Annotated[int, Field(ge=0, le=6)]
LocalTime = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]


class TriggerBase(StrictModel):
    timezone: str = Field(default="Asia/Seoul", min_length=1, max_length=100)
    missed_policy: Literal["latest_once", "skip"] = "latest_once"

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Unknown timezone") from None
        return value


class OnceTrigger(TriggerBase):
    kind: Literal["once"] = "once"
    at: IsoDateTime


class DailyTrigger(TriggerBase):
    kind: Literal["daily"] = "daily"
    local_time: LocalTime
    weekdays: list[Weekday] = Field(
        default_factory=lambda: list(range(7)), min_length=1, max_length=7
    )

    @field_validator("weekdays")
    @classmethod
    def unique_weekdays(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Duplicate weekday")
        return sorted(value)


class WeeklyTrigger(DailyTrigger):
    kind: Literal["weekly"] = "weekly"
    weekdays: list[Weekday] = Field(min_length=1, max_length=7)


class IntervalTrigger(TriggerBase):
    kind: Literal["interval"] = "interval"
    seconds: Annotated[int, Field(ge=300)]
    anchor: IsoDateTime


class FileChangedTrigger(TriggerBase):
    kind: Literal["file_changed"] = "file_changed"
    poll_seconds: Annotated[int, Field(ge=60)] = 60
    run_initial: bool = False


Trigger = Annotated[
    OnceTrigger | DailyTrigger | WeeklyTrigger | IntervalTrigger | FileChangedTrigger,
    Field(discriminator="kind"),
]
TRIGGER = TypeAdapter(Trigger)


class SourceScope(StrictModel):
    artifact_ids: list[Id] = Field(default_factory=list, max_length=20)
    folder_id: Id | None = None
    max_files: Annotated[int, Field(ge=1, le=20)] = 10

    @model_validator(mode="after")
    def one_scope(self):
        if self.artifact_ids and self.folder_id is not None:
            raise ValueError("Choose artifacts or a folder")
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("Duplicate source")
        return self


class Delivery(StrictModel):
    channel: Literal["local", "telegram"] = "local"
    recipient_ref: Literal["owner"] = "owner"


class Authority(StrictModel):
    mode: Literal["read_and_draft"] = "read_and_draft"
    send_result_to_owner: bool = False


class Limits(StrictModel):
    max_runs_per_day: Annotated[int, Field(ge=1, le=100)] = 10
    max_todos: Literal[8] = 8


class AutomationConfig(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    project_id: Id
    recipe: Literal[
        "aggregate", "summarize", "weekly_report", "meeting_actions", "draft_reply", "search_answer"
    ]
    instruction: Text
    trigger: Trigger
    source_scope: SourceScope = Field(default_factory=SourceScope)
    quality_mode: Literal["balanced"] = "balanced"
    review_mode: Literal["normal"] = "normal"
    delivery: Delivery = Field(default_factory=Delivery)
    authority: Authority = Field(default_factory=Authority)
    limits: Limits = Field(default_factory=Limits)
    output_format: Literal["text", "md", "csv", "xlsx", "docx"] = "docx"

    @model_validator(mode="after")
    def bounded_authority(self):
        if not self.name.strip():
            raise ValueError("Blank name")
        if self.trigger.kind == "file_changed" and self.source_scope.folder_id is None:
            raise ValueError("File watching requires a folder")
        if self.delivery.channel == "local" and self.authority.send_result_to_owner:
            raise ValueError("Local delivery cannot grant Telegram sending")
        return self


class Event(StrictModel):
    automation_id: Id
    automation_revision: Annotated[int, Field(ge=1)]
    actor_id: Id
    project_id: Id
    scheduled_utc: IsoDateTime | None = None
    artifact_hash: Sha256 | None = None
    source_ids: list[Id] = Field(default_factory=list, max_length=20)

    @field_validator("scheduled_utc")
    @classmethod
    def canonical_time(cls, value):
        return datetime.fromisoformat(value).astimezone(UTC).isoformat() if value else None

    @model_validator(mode="after")
    def exactly_one_identity(self):
        if (self.scheduled_utc is None) == (self.artifact_hash is None):
            raise ValueError("Choose an occurrence or a snapshot hash")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Duplicate source")
        return self

    @property
    def event_key(self):
        return digest(
            {
                "automation_id": self.automation_id,
                "revision": self.automation_revision,
                "scheduled_utc": self.scheduled_utc,
                "artifact_hash": self.artifact_hash,
            }
        )


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise AppError("INVALID_INPUT")
    return value.astimezone(UTC)


def _trigger(value):
    return TRIGGER.validate_python(value.model_dump() if isinstance(value, TriggerBase) else value)


def _calendar(trigger, instant, direction):
    zone = ZoneInfo(trigger.timezone)
    local = instant.astimezone(zone)
    hour, minute = map(int, trigger.local_time.split(":"))
    for offset in range(15):
        day = local.date() + timedelta(days=offset * direction)
        if day.weekday() not in trigger.weekdays:
            continue
        wall = datetime.combine(day, time(hour, minute))
        candidate = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=None) != wall:
            continue  # Nonexistent spring-forward wall time.
        if (direction > 0 and candidate > instant) or (direction < 0 and candidate <= instant):
            return candidate
    return None


def next_occurrence(trigger, after: datetime) -> datetime | None:
    """Strictly later instant; two weeks cover a skipped weekly DST occurrence."""
    trigger, after = _trigger(trigger), _utc(after)
    if trigger.kind == "file_changed":
        return None
    if trigger.kind == "once":
        instant = _utc(trigger.at)
        return instant if instant > after else None
    if trigger.kind == "interval":
        anchor = _utc(trigger.anchor)
        step = timedelta(seconds=trigger.seconds)
        count = max(0, (after - anchor) // step + 1)
        return anchor + count * step
    return _calendar(trigger, after, 1)


def latest_occurrence(trigger, at: datetime) -> datetime | None:
    """Latest instant at or before at, within the bounded calendar window."""
    trigger, at = _trigger(trigger), _utc(at)
    if trigger.kind == "file_changed":
        return None
    if trigger.kind == "once":
        instant = _utc(trigger.at)
        return instant if instant <= at else None
    if trigger.kind == "interval":
        anchor = _utc(trigger.anchor)
        return (
            None
            if at < anchor
            else anchor
            + ((at - anchor) // timedelta(seconds=trigger.seconds))
            * timedelta(seconds=trigger.seconds)
        )
    return _calendar(trigger, at, -1)


def detect_events(automation, observed_state, now) -> list[Event]:
    """Compute tentative state on a caller-owned copy, never a live persisted state object.

    Persist this copy and enqueue returned events in ONE transaction. On failure discard
    the copy, including next_due/completed/baseline changes. No side effect happens here.
    current is supplied by a trusted scanner; stable means two identical stat polls and
    a bounded hash read that did not change. The scanner excludes generated exports.
    """
    now = _utc(now)
    if type(automation.get("enabled")) not in {bool, int} or automation["enabled"] not in {0, 1}:
        raise AppError("INVALID_INPUT")
    if not automation["enabled"]:
        return []
    config = AutomationConfig.model_validate(automation["config_json"])
    if config.project_id != automation["project_id"]:
        raise AppError("POLICY_DENIED")
    trigger = config.trigger
    current = observed_state.get("current") or {}
    sources = current.get("source_ids", config.source_scope.artifact_ids)

    def event(**identity):
        return Event(
            automation_id=automation["id"],
            automation_revision=automation["revision"],
            actor_id=automation["actor_id"],
            project_id=automation["project_id"],
            source_ids=sources,
            **identity,
        )

    if trigger.kind == "file_changed":
        last = observed_state.get("last_poll")
        if last is not None and now - _utc(last) < timedelta(seconds=trigger.poll_seconds):
            return []
        observed_state["last_poll"] = now.isoformat()
        if current.get("stable") is not True:
            return []
        candidate = event(artifact_hash=current.get("snapshot_hash"))
        if len(candidate.source_ids) > config.source_scope.max_files:
            raise AppError("MISSING_INPUT")
        previous = observed_state.get("baseline")
        observed_state["baseline"] = candidate.artifact_hash
        observed_state["baseline_signature"] = current.get("signature")
        if previous == candidate.artifact_hash or (previous is None and not trigger.run_initial):
            return []
        return [candidate]

    if observed_state.get("completed") or observed_state.get("skipped"):
        return []
    due = observed_state.get("next_due", automation.get("next_due_utc"))
    if due is None:
        due = (
            _utc(trigger.at)
            if trigger.kind == "once"
            else next_occurrence(
                trigger, _utc(automation.get("created_at", now)) - timedelta(microseconds=1)
            )
        )
    else:
        due = _utc(due)
    if due is None or due > now:
        observed_state["next_due"] = due.isoformat() if due else None
        return []
    latest = latest_occurrence(trigger, now)
    upcoming = next_occurrence(trigger, now)
    observed_state["next_due"] = upcoming.isoformat() if upcoming else None
    allowed_age = timedelta(seconds=15) if trigger.missed_policy == "skip" else timedelta(hours=24)
    ready = latest is not None and latest >= due and now - latest <= allowed_age
    if trigger.kind == "once":
        observed_state["completed" if ready else "skipped"] = True
    return [event(scheduled_utc=latest.isoformat())] if ready else []


def _folder_entries(folder, max_files):
    entries, total = [], 0
    with os.scandir(folder) as scan:
        for count, entry in enumerate(scan, 1):
            if count > 1000:
                raise AppError("LIMIT_EXCEEDED")
            # Windows DirEntry caches omit file/device IDs; fresh stat matches held handles.
            info = Path(entry.path).stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise AppError("POLICY_DENIED")
            if not stat.S_ISREG(info.st_mode) or Path(entry.name).suffix.lower() not in {
                ".txt",
                ".md",
                ".csv",
                ".xlsx",
                ".docx",
                ".pdf",
            }:
                continue
            total += info.st_size
            if info.st_size > 25 * 1024 * 1024 or total > 100 * 1024 * 1024:
                raise AppError("LIMIT_EXCEEDED")
            entries.append({"name": entry.name, **_stat_fields(info)})
            if len(entries) > max_files:
                raise AppError("LIMIT_EXCEEDED")
    return sorted(entries, key=lambda item: (item["name"].casefold(), item["name"]))


def _stat_fields(info):
    return {
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        # Windows path stat and fstat expose different ctime semantics. open_regular
        # compares handle ctime before/after; each stable poll also hashes the bytes.
        "inode": info.st_ino,
        "device": info.st_dev,
    }


def snapshot_folder(home, conn, actor, folder_id, max_files, previous, currentclock):
    """Observe a registered folder once; two stat polls >=60s apart precede any import.

    Returns only signatures, managed source IDs, stability, and observation time.
    Every stable poll hashes bytes again, even when size/mtime/inode are unchanged.
    The caller runs this outside its enqueue transaction and persists the returned
    observation only after checking the current automation authority revision.
    """
    from .artifacts import import_source, open_artifact
    from .config import get_registered_folder
    from .contracts import canonical_json
    from .db import get_value, row_dict
    from .safe_io import guard_directory, open_regular

    if type(max_files) is not int or not 1 <= max_files <= 20 or conn.in_transaction:
        raise AppError("INVALID_INPUT")
    now, home = _utc(currentclock), Path(home).absolute()
    grant = get_value(conn, "folder:" + folder_id)
    if not isinstance(grant, dict) or grant.get("actor_id") != actor.id:
        raise AppError("NOT_FOUND", status=404)
    project_id = grant["project_id"]
    folder = get_registered_folder(conn, folder_id, actor, project_id)
    if folder == home or folder.is_relative_to(home):
        raise AppError("POLICY_DENIED")
    observation = {
        "signature": None,
        "snapshot_hash": None,
        "source_ids": [],
        "stable": False,
        "observed_at": now.isoformat(),
    }
    try:
        with guard_directory(folder):
            entries = _folder_entries(folder, max_files)
            observation["signature"] = digest(entries)
            if (
                not previous
                or previous.get("signature") != observation["signature"]
                or (now - _utc(previous.get("observed_at")) < timedelta(seconds=60))
            ):
                return observation
            with ExitStack() as held:
                streams, manifest = [], []
                for entry in entries:
                    stream = held.enter_context(open_regular(folder / entry["name"], folder))
                    expected = {key: value for key, value in entry.items() if key != "name"}
                    if _stat_fields(os.fstat(stream.fileno())) != expected:
                        raise AppError("SOURCE_CHANGED")
                    hasher, size = hashlib.sha256(), 0
                    while chunk := stream.read(64 * 1024):
                        size += len(chunk)
                        if size > 25 * 1024 * 1024:
                            raise AppError("LIMIT_EXCEEDED")
                        hasher.update(chunk)
                    if size != entry["size"] or _stat_fields(os.fstat(stream.fileno())) != expected:
                        raise AppError("SOURCE_CHANGED")
                    streams.append(stream)
                    manifest.append({"name": entry["name"], "sha256": hasher.hexdigest()})
                for item, stream in zip(manifest, streams, strict=True):
                    existing = row_dict(
                        conn.execute(
                            "SELECT * FROM artifacts WHERE actor_id=? AND project_id=? AND kind='source' "
                            "AND deleted_at IS NULL AND sha256=? AND json_extract(provenance_json,'$.folder_id')=? "
                            "AND json_extract(provenance_json,'$.source_name')=? ORDER BY created_at DESC,id DESC LIMIT 1",
                            (actor.id, project_id, item["sha256"], folder_id, item["name"]),
                        ).fetchone()
                    )
                    if existing:
                        try:
                            with open_artifact(home, conn, existing["id"], actor, project_id):
                                pass
                        except (AppError, OSError):
                            existing = None
                    if existing is None:
                        stream.seek(0)
                        existing = import_source(
                            home, conn, stream, {"filename": item["name"]}, actor, project_id
                        )
                        if existing["sha256"] != item["sha256"]:
                            raise AppError("SOURCE_CHANGED")
                        provenance = {
                            **existing["provenance_json"],
                            "folder_id": folder_id,
                            "source_name": item["name"],
                        }
                        conn.execute(
                            "UPDATE artifacts SET provenance_json=? WHERE id=? AND actor_id=? AND project_id=?",
                            (canonical_json(provenance), existing["id"], actor.id, project_id),
                        )
                    observation["source_ids"].append(existing["id"])
                if _folder_entries(folder, max_files) != entries:
                    raise AppError("SOURCE_CHANGED")
                observation.update(stable=True, snapshot_hash=digest(manifest))
        return observation
    except (FileNotFoundError, AppError) as error:
        if isinstance(error, AppError) and error.code != "SOURCE_CHANGED":
            raise
        observation.update(stable=False, source_ids=[], snapshot_hash=None)
        return observation


def record_daemon_observation(conn, actor, session_id, kind, now):
    """Persist sampled process observations; gaps never imply a PC power state."""
    from .db import get_value, set_value

    now = _utc(now).isoformat()
    key = "daemon_session:" + session_id
    previous = get_value(conn, key)
    if kind == "start":
        if previous is not None:
            raise AppError("CONFLICT", status=409)
        record = {
            "session_id": session_id,
            "actor_id": actor.id,
            "started_at": now,
            "previous_tick_at": None,
            "last_tick_at": None,
            "stopped_at": None,
            "clock_rollback": False,
        }
    else:
        if kind not in {"tick", "stop"} or not previous or previous["actor_id"] != actor.id:
            raise AppError("POLICY_DENIED")
        if previous["stopped_at"] is not None:
            raise AppError("CONFLICT", status=409)
        record = dict(previous)
        record["clock_rollback"] |= _utc(now) < _utc(
            previous["last_tick_at"] or previous["started_at"]
        )
        if kind == "tick":
            record["previous_tick_at"] = previous["last_tick_at"]
            record["last_tick_at"] = now
        else:
            record["stopped_at"] = now
    set_value(conn, key, record, now=now)
    return record


def _event_observation(event, inbox, observation, now):
    snapshot = observation or {}
    samples = [snapshot.get(key) for key in ("started_at", "previous_tick_at", "last_tick_at")]
    rollback = bool(snapshot.get("clock_rollback")) or any(
        _utc(now) < _utc(value) for value in samples if value is not None
    )
    return {
        "schema_version": 1,
        "actor_id": event.actor_id,
        "project_id": event.project_id,
        "automation_id": event.automation_id,
        "event_key": event.event_key,
        "daemon_session_id": snapshot.get("session_id"),
        "scheduled_utc": event.scheduled_utc,
        "event_received_at": inbox["received_at"],
        "started_at": snapshot.get("started_at"),
        "previous_tick_at": snapshot.get("previous_tick_at"),
        "tick_at": snapshot.get("last_tick_at"),
        "observed_at": now.isoformat(),
        "clock_rollback": rollback,
        "scheduled_daemon_state": "observed"
        if observation
        and event.scheduled_utc is not None
        and not rollback
        and event.scheduled_utc in [*samples, now.isoformat()]
        else "unknown",
        "pc_power_state": "unknown",
    }


def tick_automations(conn, home, actor, now, *, daemon_session_id=None):
    """Run one scheduler tick and return newly/enqueued run rows (never execute them).

    File polling happens at most once per configured interval, before any filesystem IO.
    At most one newest event per bot is enqueued/drained. Event insertion, observed state,
    once completion, and next_due bookkeeping commit atomically after authority recheck.
    A bot error records only its safe code; other bots continue.
    """
    from .config import get_registered_folder
    from .db import enqueue_event_once, get_automation, get_value, row_dict, set_value, transaction

    if conn.in_transaction:
        raise AppError("CONFLICT", status=409)
    supplied_now = now
    clock = now if callable(now) else lambda: supplied_now
    now, runs = _utc(clock()), []
    observation = (
        record_daemon_observation(conn, actor, daemon_session_id, "tick", now)
        if daemon_session_id is not None
        else None
    )
    rows = [
        row_dict(row)
        for row in conn.execute(
            "SELECT * FROM automations WHERE actor_id=? AND enabled=1 ORDER BY created_at,id",
            (actor.id,),
        )
    ]
    for row in rows:
        now = _utc(clock())
        state_key = "automation_state:" + row["id"]
        attempted_poll = False
        try:
            row = get_automation(conn, row["id"], actor)
            if not row["enabled"]:
                continue
            config = AutomationConfig.model_validate(row["config_json"])
            state = copy.deepcopy(get_value(conn, state_key, {}))
            if state.get("revision", row["revision"]) != row["revision"]:
                state = {}
            state["revision"] = row["revision"]
            events, can_detect = [], True
            if config.source_scope.folder_id is not None:
                poll_seconds = (
                    config.trigger.poll_seconds if config.trigger.kind == "file_changed" else 60
                )
                last_poll = state.get("last_poll")
                poll_due = last_poll is None or now - _utc(last_poll) >= timedelta(
                    seconds=poll_seconds
                )
                if poll_due:
                    attempted_poll = True
                    get_registered_folder(
                        conn, config.source_scope.folder_id, actor, row["project_id"]
                    )
                    state["current"] = snapshot_folder(
                        home,
                        conn,
                        actor,
                        config.source_scope.folder_id,
                        config.source_scope.max_files,
                        state.get("current"),
                        now,
                    )
                    if config.trigger.kind != "file_changed":
                        state["last_poll"] = now.isoformat()
                if config.trigger.kind == "file_changed":
                    can_detect = poll_due
                else:
                    can_detect = state.get("current", {}).get("stable") is True
            now = _utc(clock())
            if can_detect:
                events = detect_events(row, state, now)
            with transaction(conn):
                fresh = get_automation(conn, row["id"], actor)
                if not fresh["enabled"] or fresh["revision"] != row["revision"]:
                    continue
                event = events[-1] if events else None
                if event is not None:
                    conn.execute(
                        "UPDATE inbox_events SET state='coalesced' WHERE source='automation' "
                        "AND connection_id=? AND state='received' AND event_key!=?",
                        (row["id"], event.event_key),
                    )
                else:
                    pending = row_dict(
                        conn.execute(
                            "SELECT * FROM inbox_events WHERE source='automation' AND connection_id=? "
                            "AND state='received' ORDER BY id DESC LIMIT 1",
                            (row["id"],),
                        ).fetchone()
                    )
                    if pending is not None:
                        event = Event.model_validate(pending["payload_json"])
                now = _utc(clock())
                result = enqueue_event_once(event, conn, actor=actor, now=now) if event else None
                if event is not None:
                    observation_key = "automation_observation:" + row["id"] + ":" + event.event_key
                    inbox = conn.execute(
                        "SELECT received_at FROM inbox_events WHERE source='automation' "
                        "AND connection_id=? AND event_key=?",
                        (row["id"], event.event_key),
                    ).fetchone()
                    if inbox is not None and get_value(conn, observation_key) is None:
                        set_value(
                            conn,
                            observation_key,
                            _event_observation(event, inbox, observation, now),
                            now=now.isoformat(),
                        )
                state.pop("last_error", None)
                set_value(conn, state_key, state, now=now.isoformat())
                if "next_due" in state:
                    conn.execute(
                        "UPDATE automations SET next_due_utc=?,updated_at=? WHERE id=? AND revision=?",
                        (state["next_due"], now.isoformat(), row["id"], row["revision"]),
                    )
                if result is not None and all(saved["id"] != result["id"] for saved in runs):
                    runs.append(result)
        except (
            AppError,
            ValidationError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
        ) as error:
            code = error.code if isinstance(error, AppError) else "INVALID_INPUT"
            try:
                with transaction(conn):
                    fresh = get_automation(conn, row["id"], actor)
                    if fresh["enabled"] and fresh["revision"] == row["revision"]:
                        failed_state = copy.deepcopy(get_value(conn, state_key, {}))
                        if not isinstance(failed_state, dict):
                            failed_state = {}
                        failed_state["last_error"] = code
                        if attempted_poll:
                            failed_state["last_poll"] = now.isoformat()
                        set_value(conn, state_key, failed_state, now=now.isoformat())
            except (AppError, sqlite3.Error):
                pass
    return runs
