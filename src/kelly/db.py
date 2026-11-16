"""Explicit, short SQLite transactions. Each connection belongs to its thread."""

import json
import sqlite3
import uuid
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .contracts import Actor, AppError, RunRequest, TaskSpec, canonical_json, digest


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    for key, value in result.items():
        if key.endswith("_json") and value is not None:
            result[key] = json.loads(value)
    return result


def init_db(path: Path) -> sqlite3.Connection:
    path = Path(path).absolute()
    if str(path).startswith("\\\\"):
        raise AppError("POLICY_DENIED")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    existing = conn.execute("SELECT 1 FROM sqlite_master WHERE name='kv'").fetchone()
    if not existing:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        # Startup is serialized by the daemon's OS file lock before opening the DB.
        conn.executescript(
            "BEGIN IMMEDIATE;\n" + schema + "\nINSERT INTO kv VALUES ('schema_version','1',1,"
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'));\nCOMMIT;"
        )
    else:
        version = conn.execute("SELECT value_json FROM kv WHERE key='schema_version'").fetchone()
        if version is None or json.loads(version[0]) != 1:
            conn.close()
            raise AppError("SCHEMA_VERSION")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    if conn.in_transaction:
        raise RuntimeError("Nested transaction is not supported")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def require_project(actor: Actor, project_id: str):
    if project_id not in actor.project_ids:
        raise AppError("NOT_FOUND", status=404)


def get_run(conn, run_id: str, actor: Actor) -> dict:
    run = row_dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
    if run is None or run["actor_id"] != actor.id:
        raise AppError("NOT_FOUND", status=404)
    require_project(actor, run["project_id"])
    return run


def _validate_sources(conn, ids, actor, project_id):
    require_project(actor, project_id)
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise AppError("INVALID_INPUT")
    ids = list(dict.fromkeys(ids))
    if len(ids) > 20:
        raise AppError("LIMIT_EXCEEDED", status=413)
    total = 0
    for artifact_id in ids:
        row = conn.execute(
            "SELECT byte_size FROM artifacts WHERE id=? AND actor_id=? AND project_id=? "
            "AND deleted_at IS NULL",
            (artifact_id, actor.id, project_id),
        ).fetchone()
        if row is None:
            raise AppError("NOT_FOUND", status=404)
        total += row["byte_size"]
    if total > 100 * 1024 * 1024:
        raise AppError("LIMIT_EXCEEDED", status=413)
    return ids


def allowed_sources(conn, run: dict, actor: Actor) -> list[str]:
    # The caller's stale or model-modified run dictionary cannot grant file access.
    current = get_run(conn, run["id"], actor)
    confirmed = get_value(conn, "confirmed_sources:" + current["id"], [])
    if not isinstance(confirmed, list):
        raise AppError("INVALID_INPUT")
    return _validate_sources(
        conn,
        current["request_json"]["artifact_ids"] + confirmed,
        actor,
        current["project_id"],
    )


def get_task_spec(conn, run_id: str, actor: Actor) -> TaskSpec | None:
    run = get_run(conn, run_id, actor)
    value = get_value(conn, "task_spec:" + run_id)
    if value is None:
        return None
    spec = TaskSpec.model_validate(value)
    if set(spec.source_ids) - set(allowed_sources(conn, run, actor)):
        raise AppError("POLICY_DENIED")
    return spec


def supply_inputs(
    conn, run_id: str, actor: Actor, task_spec: TaskSpec, expected_revision: int
) -> dict:
    """Store a user's complete input correction; model-generated TaskSpecs never call this."""
    if type(expected_revision) is not int or expected_revision < 1:
        raise AppError("INVALID_INPUT")
    spec = TaskSpec.model_validate(task_spec.model_dump())
    with transaction(conn):
        run = get_run(conn, run_id, actor)
        if run["revision"] != expected_revision or run["state"] not in {"queued", "awaiting_input"}:
            raise AppError("CONFLICT", status=409)
        original = run["request_json"]["artifact_ids"]
        selected = _validate_sources(conn, original + spec.source_ids, actor, run["project_id"])
        policy = dict(run["policy_json"])
        revision = policy.get("scope_revision", 1)
        if type(revision) is not int or revision < 1:
            raise AppError("INVALID_INPUT")
        policy["scope_revision"] = revision + 1
        now = utc_now()
        updated = conn.execute(
            "UPDATE runs SET policy_json=?,revision=revision+1,updated_at=? WHERE id=? AND revision=? "
            "AND state IN ('queued','awaiting_input')",
            (canonical_json(policy), now, run_id, expected_revision),
        )
        if updated.rowcount != 1:
            raise AppError("CONFLICT", status=409)
        set_value(conn, "task_spec:" + run_id, spec.model_dump(mode="json"), now=now)
        set_value(conn, "prepare_pending:" + run_id, {"revision": expected_revision + 1}, now=now)
        set_value(
            conn,
            "confirmed_sources:" + run_id,
            [item for item in selected if item not in original],
            now=now,
        )
        conn.execute(
            "UPDATE approvals SET state='expired',revision=revision+1,resolved_at=? "
            "WHERE run_id=? AND state IN ('pending','approved')",
            (now, run_id),
        )
        return get_run(conn, run_id, actor)


def create_run(
    conn,
    actor: Actor,
    source: str,
    key: str,
    payload: RunRequest,
    policy: dict | None = None,
    *,
    automation_id=None,
    automation_revision=None,
    parent_run_id=None,
    now: str | None = None,
) -> dict:
    require_project(actor, payload.project_id)
    if source not in {"ui", "telegram", "automation"} or not key or len(key) > 256:
        raise AppError("INVALID_INPUT")
    body = payload.model_dump(mode="json")
    request_hash = digest(body)
    # Callers that atomically enqueue inbox+run+cursor already own the transaction.
    if conn.in_transaction:
        return _insert_run(
            conn,
            actor,
            source,
            key,
            body,
            request_hash,
            policy or {},
            automation_id,
            automation_revision,
            parent_run_id,
            now or utc_now(),
        )
    with transaction(conn):
        return _insert_run(
            conn,
            actor,
            source,
            key,
            body,
            request_hash,
            policy or {},
            automation_id,
            automation_revision,
            parent_run_id,
            now or utc_now(),
        )


def _insert_run(
    conn,
    actor,
    source,
    key,
    body,
    request_hash,
    policy,
    automation_id,
    automation_revision,
    parent_run_id,
    now,
):
    old = row_dict(
        conn.execute(
            "SELECT * FROM runs WHERE actor_id=? AND source=? AND idempotency_key=?",
            (actor.id, source, key),
        ).fetchone()
    )
    if old:
        if old["request_hash"] != request_hash:
            raise AppError("CONFLICT", status=409)
        return old
    if conn.execute("SELECT count(*) FROM runs WHERE state='queued'").fetchone()[0] >= 100:
        raise AppError("QUEUE_FULL", status=429)
    _validate_sources(conn, body["artifact_ids"], actor, body["project_id"])
    if parent_run_id:
        get_run(conn, parent_run_id, actor)
    if automation_id:
        automation = conn.execute(
            "SELECT revision FROM automations WHERE id=? AND actor_id=? AND project_id=?",
            (automation_id, actor.id, body["project_id"]),
        ).fetchone()
        if automation is None or automation[0] != automation_revision:
            raise AppError("CONFLICT", status=409)
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO runs (id,actor_id,project_id,source,idempotency_key,request_hash,"
        "request_json,policy_json,automation_id,automation_revision,parent_run_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            actor.id,
            body["project_id"],
            source,
            key,
            request_hash,
            canonical_json(body),
            canonical_json(policy),
            automation_id,
            automation_revision,
            parent_run_id,
            now,
            now,
        ),
    )
    return row_dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())


_REVISION_FIELDS = {
    "runs": {"plan_json", "result_json", "error_code", "updated_at"},
    "automations": {"name", "enabled", "config_json", "next_due_utc", "updated_at"},
    "approvals": {"state", "resolved_at", "consumed_at"},
    "artifacts": {"deleted_at", "provenance_json", "sha256", "byte_size"},
}


def apply_revision(conn, table: str, id: str, expected_revision: int, patch: dict) -> dict:
    allowed = _REVISION_FIELDS.get(table)
    if not allowed or not patch or set(patch) - allowed or type(expected_revision) is not int:
        raise AppError("INVALID_INPUT")
    values = [
        canonical_json(value) if key.endswith("_json") and value is not None else value
        for key, value in patch.items()
    ]
    fields = ",".join(f"{name}=?" for name in patch)
    # Both table and columns were checked against constant allowlists above.
    cursor = conn.execute(
        f"UPDATE {table} SET {fields},revision=revision+1 WHERE id=? AND revision=?",
        (*values, id, expected_revision),
    )
    if cursor.rowcount != 1:
        raise AppError("CONFLICT", status=409)
    return row_dict(conn.execute(f"SELECT * FROM {table} WHERE id=?", (id,)).fetchone())


def get_value(conn, key: str, default=None):
    row = conn.execute("SELECT value_json FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_value(conn, key: str, value, *, now: str | None = None):
    conn.execute(
        "INSERT INTO kv VALUES (?,?,1,?) ON CONFLICT(key) DO UPDATE SET "
        "value_json=excluded.value_json,revision=kv.revision+1,updated_at=excluded.updated_at",
        (key, canonical_json(value), now or utc_now()),
    )


def _automation_now(value):
    value = datetime.now(UTC) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AppError("INVALID_INPUT")
    return value.astimezone(UTC)


def _automation_config(value):
    from pydantic import ValidationError

    from .automation import AutomationConfig

    try:
        return AutomationConfig.model_validate(
            value.model_dump() if hasattr(value, "model_dump") else value
        )
    except ValidationError as error:
        raise AppError("INVALID_INPUT") from error


def get_automation(conn, automation_id, actor):
    row = row_dict(
        conn.execute("SELECT * FROM automations WHERE id=?", (automation_id,)).fetchone()
    )
    if row is None or row["actor_id"] != actor.id:
        raise AppError("NOT_FOUND", status=404)
    require_project(actor, row["project_id"])
    return row


def automation_proposal_hash(row):
    return digest(
        {key: row[key] for key in ("id", "actor_id", "project_id", "revision", "config_json")}
    )


def _validate_automation_scope(conn, actor, config):
    from .config import get_registered_folder

    _validate_sources(conn, config.source_scope.artifact_ids, actor, config.project_id)
    if config.source_scope.folder_id is not None:
        get_registered_folder(conn, config.source_scope.folder_id, actor, config.project_id)
    for source_id in config.source_scope.artifact_ids:
        if (
            conn.execute("SELECT kind FROM artifacts WHERE id=?", (source_id,)).fetchone()[0]
            != "source"
        ):
            raise AppError("POLICY_DENIED")


def create_automation(conn, actor, config, *, now=None):
    config, now = _automation_config(config), _automation_now(now)
    require_project(actor, config.project_id)
    _validate_automation_scope(conn, actor, config)
    automation_id = uuid.uuid4().hex
    with transaction(conn):
        conn.execute(
            "INSERT INTO automations (id,actor_id,project_id,name,config_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                automation_id,
                actor.id,
                config.project_id,
                config.name,
                canonical_json(config),
                now.isoformat(),
                now.isoformat(),
            ),
        )
    return get_automation(conn, automation_id, actor)


def _stop_automation_runs(conn, automation_id, now):
    from .engine import TERMINAL, transition_run

    for raw in conn.execute(
        "SELECT * FROM runs WHERE automation_id=?", (automation_id,)
    ).fetchall():
        run = row_dict(raw)
        if run["state"] not in TERMINAL | {"cancel_requested"}:
            transition_run(conn, run["id"], run["state"], "cancel_requested")
            if run["state"] == "queued":
                transition_run(conn, run["id"], "cancel_requested", "cancelled")
                conn.execute(
                    "UPDATE steps SET state='cancelled' WHERE run_id=? AND state IN ('pending','awaiting_approval')",
                    (run["id"],),
                )
        conn.execute(
            "UPDATE approvals SET state='expired',revision=revision+1,resolved_at=? "
            "WHERE run_id=? AND state IN ('pending','approved')",
            (now.isoformat(), run["id"]),
        )
        conn.execute(
            "UPDATE outbox SET state='cancelled' WHERE run_id=? AND state IN ('pending','retry_wait')",
            (run["id"],),
        )
    conn.execute(
        "UPDATE inbox_events SET state='skipped' WHERE source='automation' AND connection_id=? AND state='received'",
        (automation_id,),
    )


def update_automation(conn, automation_id, actor, config, *, expected_revision, now=None):
    config, now = _automation_config(config), _automation_now(now)
    with transaction(conn):
        old = get_automation(conn, automation_id, actor)
        if type(expected_revision) is not int or old["revision"] != expected_revision:
            raise AppError("CONFLICT", status=409)
        if old["project_id"] != config.project_id:
            raise AppError("POLICY_DENIED")
        _validate_automation_scope(conn, actor, config)
        _stop_automation_runs(conn, automation_id, now)
        apply_revision(
            conn,
            "automations",
            automation_id,
            expected_revision,
            {
                "name": config.name,
                "config_json": config.model_dump(),
                "enabled": 0,
                "next_due_utc": None,
                "updated_at": now.isoformat(),
            },
        )
        conn.execute("DELETE FROM kv WHERE key=?", ("automation_state:" + automation_id,))
    return get_automation(conn, automation_id, actor)


def set_automation_enabled(
    conn, automation_id, expected_revision, enabled, actor, *, now=None, confirmed_hash=None
):
    from .automation import next_occurrence

    now = _automation_now(now)
    if type(enabled) is not bool:
        raise AppError("INVALID_INPUT")
    # Telegram commits the opaque callback, this decision and the cursor together.
    with nullcontext(conn) if conn.in_transaction else transaction(conn):
        row = get_automation(conn, automation_id, actor)
        if type(expected_revision) is not int or row["revision"] != expected_revision:
            raise AppError("CONFLICT", status=409)
        if enabled and confirmed_hash != automation_proposal_hash(row):
            raise AppError("POLICY_DENIED")
        if bool(row["enabled"]) == enabled:
            return row
        config = _automation_config(row["config_json"])
        if enabled:
            _validate_automation_scope(conn, actor, config)
            due = row["next_due_utc"]
            if due is None:
                at = (
                    datetime.fromisoformat(config.trigger.at)
                    if config.trigger.kind == "once"
                    else next_occurrence(config.trigger, now - timedelta(microseconds=1))
                )
                due = at.astimezone(UTC).isoformat() if at else None
        else:
            due = row["next_due_utc"]  # Resume applies missed_policy to the preserved due time.
            _stop_automation_runs(conn, automation_id, now)
        updated = apply_revision(
            conn,
            "automations",
            automation_id,
            expected_revision,
            {
                "enabled": int(enabled),
                "next_due_utc": due,
                "updated_at": now.isoformat(),
            },
        )
        state = get_value(conn, "automation_state:" + automation_id, {})
        # A new authority revision cannot replay an old pending event; keep the observed baseline.
        state.update(revision=updated["revision"])
        set_value(conn, "automation_state:" + automation_id, state, now=now.isoformat())
    return get_automation(conn, automation_id, actor)


def require_automation_run(conn, run_id, actor):
    run = get_run(conn, run_id, actor)
    if run["automation_id"] is None:
        return run
    bot = get_automation(conn, run["automation_id"], actor)
    if not bot["enabled"] or bot["revision"] != run["automation_revision"]:
        raise AppError("POLICY_DENIED")
    return run


def enqueue_event_once(event, conn, *, actor, now=None):
    from zoneinfo import ZoneInfo

    from pydantic import ValidationError

    from .automation import Event
    from .engine import TERMINAL

    try:
        event = Event.model_validate(event.model_dump() if hasattr(event, "model_dump") else event)
    except ValidationError as error:
        raise AppError("INVALID_INPUT") from error
    now = _automation_now(now)
    with nullcontext(conn) if conn.in_transaction else transaction(conn):
        bot = get_automation(conn, event.automation_id, actor)
        if (event.actor_id, event.project_id) != (actor.id, bot["project_id"]):
            raise AppError("POLICY_DENIED")
        old = row_dict(
            conn.execute(
                "SELECT * FROM inbox_events WHERE source='automation' AND connection_id=? AND event_key=?",
                (bot["id"], event.event_key),
            ).fetchone()
        )
        payload = event.model_dump()
        if old:
            if canonical_json(old["payload_json"]) != canonical_json(payload):
                raise AppError("CONFLICT", status=409)
            if old["state"] == "enqueued":
                return get_run(conn, old["run_id"], actor)
            if old["state"] != "received":
                return None
        if not bot["enabled"] or event.automation_revision != bot["revision"]:
            raise AppError("POLICY_DENIED")
        config = _automation_config(bot["config_json"])
        if event.scheduled_utc and datetime.fromisoformat(event.scheduled_utc) > now:
            raise AppError("INVALID_INPUT")
        if bool(event.artifact_hash) != (config.trigger.kind == "file_changed"):
            raise AppError("INVALID_INPUT")
        _validate_sources(conn, event.source_ids, actor, bot["project_id"])
        scope = config.source_scope
        if scope.folder_id is None:
            if set(event.source_ids) != set(scope.artifact_ids):
                raise AppError("POLICY_DENIED")
        elif len(event.source_ids) > scope.max_files:
            raise AppError("LIMIT_EXCEEDED")
        for source_id in event.source_ids:
            source = row_dict(
                conn.execute("SELECT * FROM artifacts WHERE id=?", (source_id,)).fetchone()
            )
            if source["kind"] != "source" or (
                scope.folder_id is not None
                and source["provenance_json"].get("folder_id") != scope.folder_id
            ):
                raise AppError("POLICY_DENIED")
        if old is None:
            conn.execute(
                "INSERT INTO inbox_events (source,connection_id,event_key,actor_id,payload_json,received_at) "
                "VALUES ('automation',?,?,?,?,?)",
                (bot["id"], event.event_key, actor.id, canonical_json(payload), now.isoformat()),
            )

        def mark(state, run_id=None):
            conn.execute(
                "UPDATE inbox_events SET state=?,run_id=? WHERE source='automation' AND connection_id=? AND event_key=?",
                (state, run_id, bot["id"], event.event_key),
            )

        if event.scheduled_utc:
            age = now - datetime.fromisoformat(event.scheduled_utc)
            limit = (
                timedelta(seconds=15)
                if config.trigger.missed_policy == "skip"
                else timedelta(hours=24)
            )
            if age > limit:
                mark("skipped")
                return None

        pending_id = conn.execute(
            "SELECT id FROM inbox_events WHERE source='automation' AND connection_id=? AND event_key=?",
            (bot["id"], event.event_key),
        ).fetchone()[0]
        if conn.execute(
            "SELECT 1 FROM inbox_events WHERE source='automation' AND connection_id=? AND state='received' AND id>?",
            (bot["id"], pending_id),
        ).fetchone():
            mark("coalesced")
            return None
        conn.execute(
            "UPDATE inbox_events SET state='coalesced' WHERE source='automation' AND connection_id=? "
            "AND state='received' AND id<?",
            (bot["id"], pending_id),
        )

        # At most one newest event waits while this bot owns a nonterminal run.
        busy = any(
            row[0] not in TERMINAL
            for row in conn.execute("SELECT state FROM runs WHERE automation_id=?", (bot["id"],))
        )
        if busy:
            return None
        zone = ZoneInfo(config.trigger.timezone)
        local = now.astimezone(zone)
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
        daily = conn.execute(
            "SELECT count(*) FROM runs WHERE automation_id=? AND created_at>=? AND created_at<?",
            (
                bot["id"],
                day_start.astimezone(UTC).isoformat(),
                (day_start + timedelta(days=1)).astimezone(UTC).isoformat(),
            ),
        ).fetchone()[0]
        if daily >= config.limits.max_runs_per_day:
            mark("skipped")
            return None
        if conn.execute("SELECT count(*) FROM runs WHERE state='queued'").fetchone()[0] >= 100:
            return None
        run = create_run(
            conn,
            actor,
            "automation",
            event.event_key,
            RunRequest(
                request=config.instruction,
                project_id=bot["project_id"],
                artifact_ids=event.source_ids,
                quality_mode=config.quality_mode,
                output_format=config.output_format,
            ),
            {
                "scope_revision": bot["revision"],
                "automation_config_hash": digest(bot["config_json"]),
                "authority": config.authority.model_dump(),
                "delivery": config.delivery.model_dump(),
            },
            automation_id=bot["id"],
            automation_revision=bot["revision"],
            now=now.isoformat(),
        )
        set_value(
            conn,
            "task_spec:" + run["id"],
            TaskSpec(
                intent=config.recipe,
                source_ids=event.source_ids,
                acceptance=[
                    config.instruction
                    if len(config.instruction) <= 500
                    else "등록된 업무 지시의 모든 필수 조건을 충족한다"
                ],
            ).model_dump(),
            now=now.isoformat(),
        )
        mark("enqueued", run["id"])
        return run
