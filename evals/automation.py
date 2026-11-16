"""Actual local automation scenarios; HTTP is always MockTransport and Office work is not faked."""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from time import perf_counter

import httpx
from pydantic import SecretStr

from kelly import artifacts, db, engine, safe_io, telegram
from kelly import automation as scheduler
from kelly.config import register_folder
from kelly.contracts import Actor, AppError, canonical_json

from .run import Case, Observation, condition_hash, fingerprint, load_cases, sha

ACTOR = Actor(id="eval-owner", project_ids=["eval"])
CONNECTION = "offline-automation"
BASE = datetime(2026, 9, 8, tzinfo=UTC)
SUPPORTED = {
    "automation-01",
    "automation-02",
    "automation-03",
    "automation-04",
    "automation-05",
    "automation-06",
    "automation-07",
    "automation-08",
    "automation-09",
    "automation-10",
    "automation-11",
    "automation-12",
    "automation-13",
    "automation-14",
    "automation-15",
    "automation-16",
    "automation-17",
    "automation-18",
    "automation-19",
    "automation-20",
}
ADAPTER_VERSION = "automation-offline-v1"


def instant(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", "utf-8"
    )


def config(case, trigger, source_scope=None):
    return scheduler.AutomationConfig(
        name=case.id,
        project_id="eval",
        recipe="summarize",
        instruction=case.request,
        trigger=trigger,
        source_scope=source_scope or {},
        output_format="text",
    )


def create_bot(conn, case, trigger, now, *, enabled=True):
    row = db.create_automation(conn, ACTOR, config(case, trigger), now=now)
    if enabled:
        row = db.set_automation_enabled(
            conn,
            row["id"],
            row["revision"],
            True,
            ACTOR,
            now=now,
            confirmed_hash=db.automation_proposal_hash(row),
        )
    return row


def update_message(update_id, text):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 1,
            "from": {"id": 101, "is_bot": False},
            "chat": {"id": 101, "type": "private"},
            "text": text,
        },
    }


def callback_message(update_id, data):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "offline-" + str(update_id),
            "from": {"id": 101, "is_bot": False},
            "message": {"message_id": 50, "chat": {"id": 101, "type": "private"}},
            "data": data,
        },
    }


def pair(conn, client, update_id, *, now=BASE):
    telegram.register_connection(conn, ACTOR, CONNECTION, "eval", now=now, client=client)
    code = telegram.create_pairing(conn, ACTOR, CONNECTION, now=now)
    return telegram.ingest_update(
        conn,
        ACTOR,
        CONNECTION,
        update_message(update_id, "/pair " + code["code"]),
        now=now,
        client=client,
    )


def table_rows(conn, name):
    if name not in {"runs", "automations", "inbox_events", "outbox", "calls", "artifacts"}:
        raise ValueError("unknown ledger table")
    return [db.row_dict(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]


def scheduled_from_inbox(conn):
    return [
        iso(instant(row["payload_json"]["scheduled_utc"]))
        for row in table_rows(conn, "inbox_events")
        if row["source"] == "automation"
        and row["state"] == "enqueued"
        and row["payload_json"].get("scheduled_utc")
    ]


def calendar_case(case, conn, directory, trace):
    initial, events = case.scenario["initial"], case.scenario["events"]
    trigger = initial.get("trigger") or {
        "kind": "once",
        "at": events[0]["at"],
    }
    if events[0]["op"] == "next_occurrence":
        after = instant(events[0]["after"])
        row = create_bot(conn, case, trigger, after, enabled=False)
        parsed = scheduler.AutomationConfig.model_validate(row["config_json"]).trigger
        due = scheduler.next_occurrence(parsed, after)
        result = {"scheduled_utc": iso(due)}
        db.set_value(conn, "eval:calendar-result", result)
        trace.append({"op": "next_occurrence", "after": iso(after), "actual": result})
        return result
    first = instant(events[0]["at"])
    start = (
        instant(initial["last_tick"]) + timedelta(microseconds=1)
        if initial.get("last_tick")
        else first - timedelta(days=1)
    )
    row = create_bot(conn, case, trigger, start)
    if initial.get("enabled") is False:
        row = db.set_automation_enabled(conn, row["id"], row["revision"], False, ACTOR, now=start)
    for event in events:
        now = instant(event["at"])
        admitted = scheduler.tick_automations(conn, directory / "files", ACTOR, now)
        trace.append(
            {
                "op": "tick",
                "at": iso(now),
                "admitted_run_ids": [item["id"] for item in admitted],
                "state": db.get_value(conn, "automation_state:" + row["id"], {}),
            }
        )
    count = len(table_rows(conn, "runs"))
    scheduled = scheduled_from_inbox(conn)
    if case.id == "automation-01":
        state = db.get_value(conn, "automation_state:" + row["id"], {})
        return {"runs": count, "completed": state.get("completed", False)}
    if case.id in {"automation-03", "automation-06"}:
        return {"runs": count, "scheduled_utc": scheduled[-1] if scheduled else None}
    return {"runs": count}


@contextmanager
def observe_read_boundary():
    """Observe real Python calls without replacing any file/DB/engine implementation."""
    previous = sys.getprofile()
    observed = {"open_regular_entries": 0}
    boundary = safe_io.open_regular.__wrapped__.__code__

    def profile(frame, event, arg):
        if event == "call" and frame.f_code is boundary:
            observed["open_regular_entries"] += 1
        if previous is not None:
            previous(frame, event, arg)

    sys.setprofile(profile)
    try:
        yield observed
    finally:
        sys.setprofile(previous)


def folder_case(case, conn, directory, trace):
    initial, events = case.scenario["initial"], case.scenario["events"]
    home, folder = directory / "files", directory / "registered-input"
    folder.mkdir()
    grant = register_folder(conn, home, ACTOR, "eval", folder, explicit_confirmed=True)
    first = instant(events[0]["at"]) if events[0].get("at") else BASE
    start = first - timedelta(minutes=3)
    settings = config(
        case,
        {
            "kind": "file_changed",
            "run_initial": initial.get("run_initial", False),
            "poll_seconds": initial.get("poll_seconds", 60),
        },
        {"folder_id": grant["id"]},
    )
    bot = db.create_automation(conn, ACTOR, settings, now=start)
    bot = db.set_automation_enabled(
        conn,
        bot["id"],
        bot["revision"],
        True,
        ACTOR,
        now=start,
        confirmed_hash=db.automation_proposal_hash(bot),
    )
    key = "automation_state:" + bot["id"]

    def tick(at):
        admitted = scheduler.tick_automations(conn, home, ACTOR, at)
        state = db.get_value(conn, key, {})
        trace.append(
            {
                "op": "tick",
                "at": iso(at),
                "state": state,
                "admitted_run_ids": [row["id"] for row in admitted],
            }
        )
        return state

    if case.id == "automation-12":
        target = directory / "outside-grant"
        target.mkdir()
        source = target / "outside.txt"
        content = case.sources[0].text.encode("utf-8")
        source.write_bytes(content)
        alias = folder / "alias"
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "New-Item -ItemType Junction -Path $env:EVAL_JUNCTION -Target $env:EVAL_TARGET",
            ],
            env=os.environ | {"EVAL_JUNCTION": str(alias), "EVAL_TARGET": str(target)},
            check=True,
            capture_output=True,
            timeout=30,
        )
        try:
            if not alias.is_junction():
                raise ValueError("native junction fixture missing")
            with observe_read_boundary() as read_observation:
                state = tick(first)
            unchanged = source.read_bytes() == content
            trace.append(
                {
                    "op": "junction_guard",
                    **read_observation,
                    "read_observation_basis": "production safe_io.open_regular call entries",
                    "outside_source_unchanged": unchanged,
                }
            )
            if not unchanged:
                raise AppError("SOURCE_CHANGED")
            return {
                "reads_outside_grant": read_observation["open_regular_entries"],
                "runs": len(table_rows(conn, "runs")),
                "error": state.get("last_error"),
            }
        finally:
            if alias.is_junction():
                alias.rmdir()  # Remove only this owned link, never the target directory.

    source = folder / events[0].get("name", "source.txt")

    def materialize(item):
        content = item["text"].encode("utf-8")
        source.write_bytes(content)
        stamp = 1_700_000_000_000_000_000 + item["stat"]["mtime"] * 1_000_000_000
        os.utime(source, ns=(stamp, stamp))
        info = source.stat()
        trace.append(
            {
                "op": "fixture_write",
                "name": source.name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "stat": {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "inode": info.st_ino},
            }
        )

    baseline = None
    if case.id == "automation-11":
        materialize({"text": initial["baseline_text"], "stat": initial["stat"]})
        tick(first - timedelta(minutes=2))
        baseline = tick(first - timedelta(minutes=1)).get("baseline")
        if baseline is None:
            raise ValueError("actual initial baseline was not established")
    for item in events:
        materialize(item)
        state = tick(instant(item["at"]))
        if source.read_bytes() != item["text"].encode("utf-8"):
            raise ValueError("scanner changed original input")
    count = len(table_rows(conn, "runs"))
    if case.id == "automation-09":
        return {"runs": count, "baseline": state.get("baseline") is not None}
    if case.id == "automation-10":
        return {"runs": count, "stable": state.get("current", {}).get("stable", False)}
    return {"runs": count, "hash_changed": baseline != state.get("baseline")}


def revoked_grant_case(case, conn, directory, trace):
    home, folder = directory / "files", directory / "registered-input"
    folder.mkdir()
    source = folder / "source.txt"
    content = case.sources[0].text.encode("utf-8")
    source.write_bytes(content)
    grant = register_folder(conn, home, ACTOR, "eval", folder, explicit_confirmed=True)
    settings = config(case, {"kind": "interval", "seconds": 300, "anchor": iso(BASE)})
    settings.source_scope.folder_id = grant["id"]
    bot = db.create_automation(conn, ACTOR, settings, now=BASE)
    bot = db.set_automation_enabled(
        conn,
        bot["id"],
        bot["revision"],
        True,
        ACTOR,
        now=BASE,
        confirmed_hash=db.automation_proposal_hash(bot),
    )
    first_run = None
    for seconds in (0, 1):
        at = BASE + timedelta(seconds=seconds)
        event = scheduler.Event(
            automation_id=bot["id"],
            automation_revision=bot["revision"],
            actor_id=ACTOR.id,
            project_id="eval",
            scheduled_utc=iso(at),
        )
        added = db.enqueue_event_once(event, conn, actor=ACTOR, now=at)
        first_run = first_run or added
    engine.transition_run(conn, first_run["id"], "queued", "failed")
    before = table_rows(conn, "runs")
    # Folder grants are immutable; a durable tombstone withdraws this server-side grant.
    db.set_value(conn, "folder:" + grant["id"], None)
    revision = conn.execute(
        "SELECT revision FROM kv WHERE key=?", ("folder:" + grant["id"],)
    ).fetchone()[0]
    with observe_read_boundary() as read_observation:
        admitted = scheduler.tick_automations(conn, home, ACTOR, BASE + timedelta(minutes=1))
    unchanged = source.read_bytes() == content
    trace.append(
        {
            "op": "revoke_and_drain",
            "grant_revision": revision,
            "grant_present": db.get_value(conn, "folder:" + grant["id"]) is not None,
            "admitted_run_ids": [row["id"] for row in admitted],
            "runs_unchanged": before == table_rows(conn, "runs"),
            "pending_events": sum(
                row["state"] == "received" for row in table_rows(conn, "inbox_events")
            ),
            "source_unchanged": unchanged,
            "read_observation_basis": "production safe_io.open_regular call entries",
            **read_observation,
        }
    )
    if not unchanged:
        raise AppError("SOURCE_CHANGED")
    return {"source_reads": read_observation["open_regular_entries"], "network_calls": 0}


def queue_case(case, conn, trace):
    """Baseline and finish events are state fixtures, never claimed Office completions."""
    initial, events = case.scenario["initial"], case.scenario["events"]
    first_time = instant(events[0]["at"])
    start = first_time - timedelta(minutes=1)
    settings = config(case, {"kind": "interval", "seconds": 300, "anchor": iso(start)})
    if case.id == "automation-14":
        settings.limits.max_runs_per_day = initial["max_runs_per_day"]
        settings.trigger.timezone = initial["timezone"]
    bot = db.create_automation(conn, ACTOR, settings, now=start)
    bot = db.set_automation_enabled(
        conn,
        bot["id"],
        bot["revision"],
        True,
        ACTOR,
        now=start,
        confirmed_hash=db.automation_proposal_hash(bot),
    )

    def event(at):
        return scheduler.Event(
            automation_id=bot["id"],
            automation_revision=bot["revision"],
            actor_id=ACTOR.id,
            project_id="eval",
            scheduled_utc=iso(at),
        )

    if case.id == "automation-14":
        for index in range(initial["runs_today"]):
            at = start + timedelta(seconds=index)
            run = db.enqueue_event_once(event(at), conn, actor=ACTOR, now=at)
            if run is None:
                raise ValueError("initial admission could not be established")
            # Failed admissions count toward the daily cap; no success/output is fabricated.
            engine.transition_run(conn, run["id"], "queued", "failed")
        baseline = len(table_rows(conn, "runs"))
        added = db.enqueue_event_once(event(first_time), conn, actor=ACTOR, now=first_time)
        trace.append(
            {
                "op": "daily_cap",
                "baseline_admissions": baseline,
                "baseline_payload_executed": False,
                "admitted_run_id": added["id"] if added else None,
            }
        )
        return {"new_runs": len(table_rows(conn, "runs")) - baseline}
    first_event = event(start)
    run = db.enqueue_event_once(first_event, conn, actor=ACTOR, now=start)
    engine.transition_run(conn, run["id"], "queued", "preparing")
    engine.transition_run(conn, run["id"], "preparing", "running")
    names, pending, latest_time = {first_event.event_key: initial["running"]}, None, start
    for item in events:
        if item["op"] == "enqueue":
            latest_time = instant(item["at"])
            pending = event(latest_time)
            names[pending.event_key] = item["event"]
            admitted = db.enqueue_event_once(pending, conn, actor=ACTOR, now=latest_time)
            trace.append(
                {
                    "op": "enqueue",
                    "event": item["event"],
                    "admitted_run_id": admitted["id"] if admitted else None,
                    "first_run_state": db.get_run(conn, run["id"], ACTOR)["state"],
                }
            )
        elif item["op"] == "finish":
            engine.transition_run(conn, run["id"], "running", "failed")
            trace.append({"op": "finish", "state": "failed", "payload_executed": False})
        elif item["op"] == "drain":
            db.enqueue_event_once(pending, conn, actor=ACTOR, now=latest_time)
    inbox = table_rows(conn, "inbox_events")
    admitted = [row for row in inbox if row["state"] == "enqueued"]
    return {
        "next_event": names[admitted[-1]["event_key"]],
        "coalesced": [names[row["event_key"]] for row in inbox if row["state"] == "coalesced"],
    }


def telegram_case(case, conn, clients, trace):
    first, second = clients
    if case.id == "automation-15":
        pair(conn, first, 1)
        bot = create_bot(
            conn,
            case,
            {"kind": "once", "at": iso(BASE + timedelta(days=1))},
            BASE,
            enabled=False,
        )
        data = telegram.create_callback(
            conn,
            ACTOR,
            CONNECTION,
            kind="automation_enable",
            target_id=bot["id"],
            now=BASE,
        )
        changed = scheduler.AutomationConfig.model_validate(bot["config_json"])
        changed.instruction += " 수정된 업무 조건"
        updated = db.update_automation(
            conn,
            bot["id"],
            ACTOR,
            changed,
            expected_revision=bot["revision"],
            now=BASE,
        )
        received = telegram.ingest_update(
            conn,
            ACTOR,
            CONNECTION,
            callback_message(2, data),
            now=BASE,
            client=first,
        )
        actual = db.get_automation(conn, bot["id"], ACTOR)
        trace.append(
            {
                "op": "stale_callback",
                "before_revision": bot["revision"],
                "after_revision": updated["revision"],
                "inbox_state": received["state"],
            }
        )
        return {"enabled": bool(actual["enabled"]), "callback": received["state"]}
    if case.id == "automation-18":
        pair(conn, first, 899)
        old = telegram.ingest_update(
            conn,
            ACTOR,
            CONNECTION,
            update_message(900, "합성 이전 업무 요청"),
            now=BASE,
            client=first,
        )
        previous_run = db.get_run(conn, old["run_id"], ACTOR)
        bot = create_bot(
            conn,
            case,
            {"kind": "once", "at": iso(BASE + timedelta(days=1))},
            BASE,
            enabled=False,
        )
        old_callback = telegram.create_callback(
            conn,
            ACTOR,
            CONNECTION,
            kind="automation_enable",
            target_id=bot["id"],
            now=BASE,
        )
        before = db.get_value(conn, "telegram:cursor:" + CONNECTION)
        rotated = telegram.register_connection(
            conn, ACTOR, CONNECTION, "eval", now=BASE, client=second
        )
        stale = telegram.ingest_update(
            conn,
            ACTOR,
            CONNECTION,
            callback_message(0, old_callback),
            now=BASE,
            client=second,
        )
        pair(conn, second, 1)
        current = db.get_value(conn, "telegram:connection:" + CONNECTION)
        cursor = db.get_value(conn, "telegram:cursor:" + CONNECTION)
        trace.append(
            {
                "op": "rotate_token",
                "previous_cursor": before,
                "unpaired_after_rotation": rotated["user_id"] is None,
                "stale_callback_state": stale["state"],
                "cursor_after_pair": cursor,
            }
        )
        return {
            "update_epoch": current["update_epoch"],
            "cursor": cursor,
            "old_callbacks": stale["state"],
            "old_runs_preserved": db.get_run(conn, previous_run["id"], ACTOR) == previous_run,
        }
    pair(conn, first, 6)
    target = case.scenario["events"][0]["utf8_size"]
    update = update_message(7, "")
    update["message"]["text"] = "x" * (target - len(canonical_json(update).encode("utf-8")))
    oversized = telegram.ingest_update(conn, ACTOR, CONNECTION, update, now=BASE, client=first)
    next_update = telegram.ingest_update(
        conn,
        ACTOR,
        CONNECTION,
        update_message(8, "합성 인사"),
        now=BASE,
        client=first,
    )
    rejected = [
        int(row["event_key"])
        for row in table_rows(conn, "inbox_events")
        if row["state"] == "rejected"
    ]
    trace.append(
        {
            "op": "oversized_then_next",
            "raw_bytes": len(canonical_json(update).encode("utf-8")),
            "oversized_state": oversized["state"],
            "next_state": next_update["state"],
        }
    )
    return {
        "rejected": rejected,
        "cursor": db.get_value(conn, "telegram:cursor:" + CONNECTION),
        "poison_loop": next_update["state"] != "enqueued",
    }


def document_fixture(case, conn, directory, client, trace):
    """Create the declared initial artifact; semantic Office generation is not evaluated."""
    pair(conn, client, 1)
    if case.id == "automation-16":
        bot = create_bot(conn, case, {"kind": "once", "at": iso(BASE)}, BASE - timedelta(seconds=1))
        event = scheduler.Event(
            automation_id=bot["id"],
            automation_revision=bot["revision"],
            actor_id=ACTOR.id,
            project_id="eval",
            scheduled_utc=iso(BASE),
        )
        run_id = db.enqueue_event_once(event, conn, actor=ACTOR, now=BASE)["id"]
    else:
        run_id = telegram.ingest_update(
            conn,
            ACTOR,
            CONNECTION,
            update_message(2, case.request),
            now=BASE,
            client=client,
        )["run_id"]
    content = case.sources[0].text.encode("utf-8")
    home = directory / "files"
    draft = artifacts.create_draft(
        home,
        conn,
        io.BytesIO(content),
        "initial-result.txt",
        ACTOR,
        "eval",
        run_id,
        provenance={"eval_initial_fixture": True},
    )
    receipt = artifacts.publish_draft(
        home,
        conn,
        draft["id"],
        ACTOR,
        "eval",
        run_id=run_id,
        step_id="fixture-publication",
    )
    # These transitions establish scenario.initial only; they are not Office task evidence.
    states = ["queued", "preparing", "running", "checking", "ready", "succeeded"]
    for before, after in pairwise(states):
        engine.transition_run(conn, run_id, before, after)
    conn.execute(
        "UPDATE runs SET result_json=? WHERE id=?",
        (
            canonical_json({"outputs": {"fixture-publication": {"receipt": receipt}}}),
            run_id,
        ),
    )
    with artifacts.open_artifact(home, conn, receipt["artifact_id"], ACTOR, "eval") as saved:
        saved_hash = hashlib.sha256(saved.read()).hexdigest()
    if saved_hash != hashlib.sha256(content).hexdigest():
        raise ValueError("initial fixture content changed")
    trace.append(
        {
            "op": "initial_document_fixture",
            "run_id": run_id,
            "artifact_id": receipt["artifact_id"],
            "actual_sha256": saved_hash,
            "fixture_only": True,
            "semantic_quality": "unverified",
        }
    )
    try:
        delivery = telegram.prepare_delivery(
            conn,
            ACTOR,
            CONNECTION,
            run_id,
            artifact_id=receipt["artifact_id"],
            now=BASE,
        )
    except AppError as error:
        if case.id != "automation-16":
            raise
        trace.append({"op": "prepare_delivery", "error_code": error.code})
        return {"network_calls": 0, "delivery": "denied"}
    if case.id == "automation-16":
        return {"network_calls": 0, "delivery": "allowed"}
    sent = telegram.deliver_once(client, conn, home, ACTOR, now=BASE)
    trace.append({"op": "deliver", "outbox_id": delivery["id"], "state": sent["state"]})
    return {}


def execute_scenario(case, directory):
    """Run one mapped scenario; unsupported cases remain unexecuted, never fabricated passes."""
    case = Case.model_validate(case.model_dump())
    if case.category != "automation" or not re.fullmatch(r"automation-[0-9]{2}", case.id):
        raise ValueError("select a frozen automation case")
    directory = Path(directory).absolute()
    directory.mkdir(parents=True, exist_ok=False)
    started = perf_counter()
    conn = db.init_db(directory / "state.db")
    trace, mock_methods, actual = [], [], {}
    error, completed = None, False

    def remote_boundary(request):
        method = request.url.path.rsplit("/", 1)[-1]
        mock_methods.append(method)
        if case.id == "automation-17" and method == "sendDocument":
            if case.sources[0].text.encode("utf-8") not in request.content:
                raise AssertionError("simulated sendDocument did not carry the saved fixture")
            raise httpx.ReadTimeout("Synthetic response lost after request", request=request)
        if method != "answerCallbackQuery":
            raise AssertionError("this scenario has no authorized simulated message delivery")
        return httpx.Response(200, json={"ok": True, "result": True})

    try:
        if case.id not in SUPPORTED:
            error = "UNSUPPORTED_EVAL_SCENARIO"
        else:
            try:
                if case.id in {
                    "automation-15",
                    "automation-16",
                    "automation-17",
                    "automation-18",
                    "automation-19",
                }:
                    with (
                        telegram.TelegramClient(
                            SecretStr("123:" + "a" * 24),
                            http_transport=httpx.MockTransport(remote_boundary),
                        ) as first,
                        telegram.TelegramClient(
                            SecretStr("456:" + "b" * 24),
                            http_transport=httpx.MockTransport(remote_boundary),
                        ) as second,
                    ):
                        if case.id in {"automation-16", "automation-17"}:
                            actual = document_fixture(case, conn, directory, first, trace)
                            if case.id == "automation-17":
                                before = table_rows(conn, "artifacts")
                                conn.close()
                                conn = db.init_db(directory / "state.db")
                                engine.resume_after_restart(conn, previous_process_is_dead=True)
                                repeated = telegram.deliver_once(
                                    first,
                                    conn,
                                    directory / "files",
                                    ACTOR,
                                    now=BASE + timedelta(hours=1),
                                )
                                final = table_rows(conn, "outbox")
                                fixture = next(
                                    item
                                    for item in trace
                                    if item["op"] == "initial_document_fixture"
                                )
                                with artifacts.open_artifact(
                                    directory / "files", conn, fixture["artifact_id"], ACTOR, "eval"
                                ) as saved:
                                    final_hash = hashlib.sha256(saved.read()).hexdigest()
                                if final_hash != fixture["actual_sha256"]:
                                    raise AppError("SOURCE_CHANGED")
                                trace.append(
                                    {
                                        "op": "restart_and_deliver",
                                        "db_reopened": True,
                                        "process_restart_simulated": True,
                                        "retried": repeated is not None,
                                        "artifacts_preserved": before
                                        == table_rows(conn, "artifacts"),
                                        "actual_artifact_sha256_after": final_hash,
                                    }
                                )
                                actual = {
                                    "outbox": final[0]["state"],
                                    "transport_attempts": len(mock_methods),
                                }
                        else:
                            actual = telegram_case(case, conn, (first, second), trace)
                elif case.id in {"automation-13", "automation-14"}:
                    actual = queue_case(case, conn, trace)
                elif case.id in {
                    "automation-09",
                    "automation-10",
                    "automation-11",
                    "automation-12",
                }:
                    actual = folder_case(case, conn, directory, trace)
                elif case.id == "automation-20":
                    actual = revoked_grant_case(case, conn, directory, trace)
                else:
                    actual = calendar_case(case, conn, directory, trace)
                completed = True
            except (
                AppError,
                ValueError,
                KeyError,
                TypeError,
                OSError,
                subprocess.SubprocessError,
            ) as exc:
                error = exc.code if isinstance(exc, AppError) else "SCENARIO_EXECUTION_ERROR"
                trace.append({"op": "error", "code": error})
        ledger = {
            name: table_rows(conn, name)
            for name in ("runs", "automations", "inbox_events", "outbox", "calls", "artifacts")
        }
        if ledger["calls"]:
            raise AssertionError("automation eval admitted a model call")
        # Do not dump pairing codes, credential fingerprints or opaque callback credentials.
        connection = db.get_value(conn, "telegram:connection:" + CONNECTION, {})
        connection = {
            key: value
            for key, value in connection.items()
            if key
            in {"id", "revision", "update_epoch", "user_id", "chat_id", "actor_id", "project_id"}
        }
        state = {
            row["id"]: db.get_value(conn, "automation_state:" + row["id"], {})
            for row in ledger["automations"]
        }
        module_dir = Path(scheduler.__file__).parent
        env_hash = fingerprint(
            {
                "adapter": ADAPTER_VERSION,
                "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "models": [],
                "network": "MockTransport_only",
                "code": {
                    name: hashlib.sha256((module_dir / name).read_bytes()).hexdigest()
                    for name in (
                        "automation.py",
                        "channels/base.py",
                        "channels/router.py",
                        "channels/telegram.py",
                        "telegram/__init__.py",
                        "db.py",
                        "engine.py",
                        "config.py",
                        "artifacts.py",
                        "safe_io.py",
                        "parser_worker.py",
                        "office.py",
                    )
                },
            }
        )
        output = canonical_json(actual) if completed else ""
        result = Observation(
            id=case.id + "-C-cold",
            case_id=case.id,
            arm="C",
            cache="cold",
            execution="offline_workload",
            condition_hash=condition_hash(case),
            environment_hash=env_hash,
            evidence_ref=str(directory / "ledger.json"),
            workload_complete=completed,
            output_text=output,
            output_sha256=sha(output),
            numbers={},
            units={},
            numeric_locators={},
            facts=[],
            coverage={},
            effects=[],
            missing_input=False,
            outcome="scenario_assertions" if completed else error,
            scenario_result=actual,
            todos=[],
            calls=[],
            latency_ms=int((perf_counter() - started) * 1000),
            cache_lookups=0,
            cache_hits=0,
            repair_scope=[],
            numeric_verified=False,
            source_verified=False,
            effect_verified=not any(
                name in {"sendMessage", "sendDocument"} for name in mock_methods
            ),
            output_verified=completed,
        )
        write_json(
            directory / "ledger.json",
            {
                "evidence_layer": "offline_automation_scenario",
                "execution_scope": "scenario_control_plane",
                "live": False,
                "case_id": case.id,
                "original_request": case.request,
                "fixture_source_hashes": {source.id: sha(source.text) for source in case.sources},
                "scenario_input": {
                    "initial": case.scenario["initial"],
                    "events": case.scenario["events"],
                },
                "trace": trace,
                "observed": actual,
                "state": state,
                "connection": connection,
                "ledger": ledger,
                "error_code": error,
                "scenario_executed": completed,
                "scenario_gold_match": actual == case.scenario["expected"] if completed else None,
                "mock_http_methods": mock_methods,
                "real_network_calls": 0,
                "model_calls": 0,
                "office_payload_executed": False,
                "source_files_materialized": any(
                    row["kind"] == "source" for row in ledger["artifacts"]
                ),
                "initial_artifact_fixture_created": any(
                    item["op"] == "initial_document_fixture" for item in trace
                ),
                "semantic_gold_annotations": "unverified",
            },
        )
        write_json(directory / "observation.json", result.model_dump())
        return result
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", action="append", required=True, help="automation ID; repeat or all"
    )
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    all_cases = [case for case in load_cases() if case.category == "automation"]
    cases = (
        all_cases if args.case == ["all"] else [case for case in all_cases if case.id in args.case]
    )
    if not cases or args.case != ["all"] and len(cases) != len(set(args.case)):
        parser.error("unknown automation case")
    results = []
    for case in cases:
        result = execute_scenario(case, args.workspace / case.id)
        results.append(result.model_dump())
        write_json(args.workspace / "observations.json", results)
        print(
            json.dumps(
                {
                    "case_id": case.id,
                    "executed": result.workload_complete,
                    "outcome": result.outcome,
                    "observed": result.scenario_result,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
