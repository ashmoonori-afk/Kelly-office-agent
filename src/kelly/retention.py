"""Thirty-day completed payload retention; preserve originals, pins and unresolved work."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from . import artifacts, db
from .contracts import AppError, canonical_json
from .memory import _references, retry_requested_purges
from .safe_io import guard_directory, open_regular, unlink_file

FINAL = {"succeeded", "failed", "cancelled", "rejected", "expired"}


def pin_artifact(conn, artifact_id, actor, project_id, pinned):
    if type(pinned) is not bool:
        raise AppError("INVALID_INPUT")
    with db.transaction(conn):
        row = artifacts.metadata(conn, artifact_id, actor, project_id)
        if row["kind"] not in {"draft", "result"}:
            raise AppError("INVALID_INPUT")
        value = {
            "actor_id": actor.id,
            "project_id": project_id,
            "artifact_id": artifact_id,
            "sha256": row["sha256"],
            "pinned": pinned,
        }
        db.set_value(conn, "artifact_pin:" + artifact_id, value)
    return value


def _own_key(key, value, run, identifiers):
    run_id = run["id"]
    if (
        run["automation_id"] is not None
        and key == "automation_plan:" + run["automation_id"]
        and isinstance(value, dict)
        and set(value) == {"scope", "sources", "run_id"}
        and value["run_id"] == run_id
        and all(
            isinstance(value[name], str) and re.fullmatch(r"[a-f0-9]{64}", value[name])
            for name in ("scope", "sources")
        )
    ):
        # This derived pointer cannot pin the completed plan and result payload forever.
        return True
    return key in {
        "task_spec:" + run_id,
        "confirmed_sources:" + run_id,
        "prepare_pending:" + run_id,
    } or (
        key.startswith(("telegram:callback:", "telegram:approval_notice:"))
        and (_references(value, identifiers) or key.rsplit(":", 1)[-1] in identifiers)
    )


def _protected(conn, run, identifiers):
    for table, states in (
        ("approvals", "'pending','approved'"),
        ("calls", "'reserved','in_flight','unknown'"),
        ("outbox", "'pending','sending','retry_wait','unknown'"),
    ):
        if conn.execute(
            f"SELECT 1 FROM {table} WHERE run_id=? AND state IN ({states}) LIMIT 1", (run["id"],)
        ).fetchone():
            return True
    # ponytail: bounded batches with conservative reference scans; index references if homes grow.
    for row in conn.execute("SELECT * FROM runs WHERE id<>?", (run["id"],)):
        other = db.row_dict(row)
        if not (other["result_json"] or {}).get("archived") and _references(other, identifiers):
            return True
    for row in conn.execute("SELECT * FROM artifacts WHERE deleted_at IS NULL"):
        item = db.row_dict(row)
        if item["run_id"] != run["id"] and _references(item["provenance_json"], identifiers):
            return True
    for row in conn.execute(
        "SELECT s.* FROM steps s JOIN runs r ON r.id=s.run_id WHERE r.id<>? "
        "AND COALESCE(json_extract(r.result_json,'$.archived'),0)=0",
        (run["id"],),
    ):
        if _references(db.row_dict(row), identifiers):
            return True
    for row in conn.execute("SELECT * FROM inbox_events WHERE state IN ('received','coalesced')"):
        if row["run_id"] != run["id"] and _references(db.row_dict(row), identifiers):
            return True
    for row in conn.execute("SELECT key,value_json FROM kv"):
        key, value = row["key"], json.loads(row["value_json"])
        if key.startswith(("cache:", "retention:")) or _own_key(key, value, run, identifiers):
            continue
        if key.startswith("artifact_pin:") and value.get("pinned") is not True:
            continue
        if _references(value, identifiers):
            return True
    return False


def _archive(conn, run, rows, now):
    identifiers = {run["id"]} | {item["id"] for item in rows} | {item["sha256"] for item in rows}
    identifiers.update(
        row[0] for row in conn.execute("SELECT id FROM approvals WHERE run_id=?", (run["id"],))
    )
    if _protected(conn, run, identifiers):
        return False
    for row in rows:
        if row["kind"] not in {"draft", "result"}:
            continue
        provenance = {
            "filename": row["provenance_json"].get("filename"),
            "retention_archived": True,
        }
        conn.execute(
            "UPDATE artifacts SET deleted_at=?,revision=revision+1,provenance_json=?,validation_json=NULL "
            "WHERE id=?",
            (now.isoformat(), canonical_json(provenance), row["id"]),
        )
    for row in list(conn.execute("SELECT key,value_json FROM kv")):
        value = json.loads(row["value_json"])
        if _own_key(row["key"], value, run, identifiers) or (
            row["key"].startswith("cache:") and _references(value, identifiers)
        ):
            conn.execute("DELETE FROM kv WHERE key=?", (row["key"],))
    # Keep idempotency/remote receipts and accounting shells: retention must not replay effects
    # or reduce this month's usage. Text, plans and document payloads are what expire.
    conn.execute(
        "UPDATE steps SET spec_json='{}',output_json=NULL,validation_json=NULL WHERE run_id=?",
        (run["id"],),
    )
    conn.execute("UPDATE approvals SET proposal_json='{}' WHERE run_id=?", (run["id"],))
    conn.execute(
        "UPDATE outbox SET payload_json='{}',authority_json='{}' WHERE run_id=?", (run["id"],)
    )
    for row in conn.execute("SELECT id,route_json FROM calls WHERE run_id=?", (run["id"],)):
        route = json.loads(row["route_json"])
        conn.execute(
            "UPDATE calls SET route_json=? WHERE id=?",
            (
                canonical_json(
                    {
                        key: route[key]
                        for key in ("role", "kind", "reasoning_effort")
                        if key in route
                    }
                ),
                row["id"],
            ),
        )
    request = {**run["request_json"], "request": "보존 기간이 지난 업무", "artifact_ids": []}
    conn.execute(
        "UPDATE runs SET request_json=?,policy_json='{}',plan_json=NULL,result_json=?,revision=revision+1 "
        "WHERE id=?",
        (canonical_json(request), canonical_json({"archived": True, "outputs": {}}), run["id"]),
    )
    return True


def _purge(conn, home, actor):
    root = Path(home).absolute()
    removed, pending = 0, 0
    cursor_key = "retention:purge:" + actor.id
    after = db.get_value(conn, cursor_key, "")
    candidates = conn.execute(
        "SELECT * FROM artifacts WHERE actor_id=? AND kind IN ('draft','result') "
        "AND deleted_at IS NOT NULL AND json_extract(provenance_json,'$.retention_archived')=1 "
        "AND COALESCE(json_extract(provenance_json,'$.purged'),0)=0 AND id>? ORDER BY id LIMIT 500",
        (actor.id, after),
    ).fetchall()
    for raw in candidates:
        row = db.row_dict(raw)
        if row["project_id"] not in actor.project_ids:
            continue
        relative = PurePosixPath(row["relative_path"])
        if (
            len(relative.parts) != 2
            or relative.parts[0] not in {"drafts", "exports"}
            or "\\" in str(relative)
            or ":" in str(relative)
            or relative.name in {".", ".."}
        ):
            raise AppError("POLICY_DENIED")
        path = root.joinpath(*relative.parts)
        try:
            with guard_directory(path.parent):
                try:
                    path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    with open_regular(path, root) as stream:
                        hashed, size = hashlib.sha256(), 0
                        while chunk := stream.read(65536):
                            size += len(chunk)
                            if size > artifacts.MAX_BYTES or size > row["byte_size"]:
                                raise AppError("SOURCE_CHANGED")
                            hashed.update(chunk)
                        if size != row["byte_size"] or hashed.hexdigest() != row["sha256"]:
                            raise AppError("SOURCE_CHANGED")
                    unlink_file(path)
            with db.transaction(conn):
                conn.execute(
                    "UPDATE artifacts SET provenance_json=? WHERE id=?",
                    (canonical_json({**row["provenance_json"], "purged": True}), row["id"]),
                )
            removed += 1
        except (AppError, OSError):
            pending += 1
    db.set_value(conn, cursor_key, candidates[-1]["id"] if len(candidates) == 500 else "")
    return removed, pending


def expire_completed(conn, home, actor, *, now):
    """Caller owns the local work lane; short DB transactions seal references before unlink."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise AppError("INVALID_INPUT")
    now = now.astimezone(UTC)
    cutoff = now - timedelta(days=30)
    cursor_key = "retention:scan:" + actor.id
    after = db.get_value(conn, cursor_key, {"at": "", "id": ""})
    candidates = conn.execute(
        "SELECT id,project_id,updated_at FROM runs WHERE actor_id=? AND updated_at<? "
        "AND state IN ('succeeded','failed','cancelled','rejected','expired') "
        "AND COALESCE(json_extract(result_json,'$.archived'),0)=0 "
        "AND (updated_at,id)>(?,?) ORDER BY updated_at,id LIMIT 100",
        (actor.id, cutoff.isoformat(), after["at"], after["id"]),
    ).fetchall()
    archived = 0
    for candidate in candidates:
        if candidate["project_id"] not in actor.project_ids:
            continue
        with db.transaction(conn):
            run = db.get_run(conn, candidate[0], actor)
            if run["state"] not in FINAL or datetime.fromisoformat(run["updated_at"]) >= cutoff:
                continue
            rows = [
                db.row_dict(row)
                for row in conn.execute("SELECT * FROM artifacts WHERE run_id=?", (run["id"],))
            ]
            archived += _archive(conn, run, rows, now)
    # Protected/locked rows cannot monopolize the next daily batch. Wrap to revisit them.
    db.set_value(
        conn,
        cursor_key,
        {"at": candidates[-1]["updated_at"], "id": candidates[-1]["id"]}
        if len(candidates) == 100
        else {"at": "", "id": ""},
    )
    removed, pending = _purge(conn, home, actor)
    retry_requested_purges(home, conn, actor)
    result = {"archived_runs": archived, "removed_files": removed, "purge_pending": pending}
    db.set_value(conn, "retention:last", {**result, "at": now.isoformat()})
    return result
