"""Explicit project memories; managed files are data, never execution authority."""

import io
import json
import re
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from .artifacts import _store, metadata, open_artifact
from .contracts import (
    Actor,
    AppError,
    Evidence,
    Id,
    IsoDateTime,
    StrictModel,
    Text,
    canonical_json,
)
from .db import require_project, row_dict, set_value, transaction, utc_now
from .office import locate
from .safe_io import guard_directory, open_regular, unlink_file


class MemorySaveArgs(StrictModel):
    text: Text
    category: Literal["preference", "decision", "template"]
    evidence_refs: list[Evidence] = Field(default_factory=list, max_length=6)
    expires_at: IsoDateTime | None = None


class MemorySearchArgs(StrictModel):
    query: str = Field(max_length=500)
    limit: int = Field(default=6, ge=1, le=20)


class MemoryScope(StrictModel):
    actor_id: Id
    project_id: Id


class MemoryRecord(MemorySaveArgs):
    id: Id
    scope: MemoryScope
    created_at: IsoDateTime
    updated_at: IsoDateTime
    revision: int = Field(ge=1)

    @model_validator(mode="after")
    def valid_record(self):
        if datetime.fromisoformat(self.created_at) > datetime.fromisoformat(self.updated_at):
            raise ValueError("Memory dates are out of order")
        if self.expires_at and datetime.fromisoformat(self.expires_at) <= datetime.fromisoformat(
            self.updated_at
        ):
            raise ValueError("Memory must expire after its update")
        if len(canonical_json(self).encode("utf-8")) > 65536:
            raise ValueError("Memory exceeds 64 KiB")
        return self


def _validated(schema, value):
    try:
        return schema.model_validate(value.model_dump() if isinstance(value, schema) else value)
    except (ValidationError, ValueError, UnicodeError) as exc:
        raise AppError("INVALID_INPUT") from exc


def _now(value):
    if isinstance(value, datetime):
        value = value.isoformat()
    try:
        return TypeAdapter(IsoDateTime).validate_python(utc_now() if value is None else value)
    except ValidationError as exc:
        raise AppError("INVALID_INPUT") from exc


def _explicit(value):
    # Only authenticated UI/channel code supplies this flag, never a model tool argument.
    if value is not True:
        raise AppError("POLICY_DENIED")


def _normalize(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def _rows(conn, actor, project_id, *, memory_id=None, include_deleted=False):
    require_project(actor, project_id)
    sql = "SELECT * FROM artifacts WHERE actor_id=? AND project_id=? AND kind='memory'"
    args = [actor.id, project_id]
    if memory_id is not None:
        try:
            TypeAdapter(Id).validate_python(memory_id)
        except ValidationError as exc:
            raise AppError("INVALID_INPUT") from exc
        sql += " AND json_extract(provenance_json,'$.memory_id')=?"
        args.append(memory_id)
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    sql += " ORDER BY json_extract(provenance_json,'$.memory_revision') DESC,id"
    return [row_dict(row) for row in conn.execute(sql, args)]


def _read(home, conn, row, actor, project_id):
    with open_artifact(home, conn, row["id"], actor, project_id) as stream:
        try:
            record = MemoryRecord.model_validate_json(stream.read(65537))
        except (ValidationError, ValueError) as exc:
            raise AppError("SOURCE_CHANGED") from exc
    if (
        record.id != row["provenance_json"].get("memory_id")
        or record.revision != row["provenance_json"].get("memory_revision")
        or record.scope != MemoryScope(actor_id=actor.id, project_id=project_id)
    ):
        raise AppError("SOURCE_CHANGED")
    return record


def _view(row, record):
    return {**record.model_dump(mode="json"), "artifact_id": row["id"], "sha256": row["sha256"]}


def _clear_cache(conn, actor, project_id):
    # Small local cache: invalidate the project, including entries with empty source maps.
    conn.execute(
        "DELETE FROM kv WHERE key LIKE 'cache:%' AND json_extract(value_json,'$.actor')=? "
        "AND json_extract(value_json,'$.project')=?",
        (actor.id, project_id),
    )


def _write(home, conn, record, actor, project_id):
    context = {"home": home, "conn": conn, "actor": actor, "run": {"project_id": project_id}}
    for evidence in record.evidence_refs:
        actual = locate(evidence.artifact_id, evidence.locator, context)
        if (actual.sha256, actual.quote, actual.extraction_version) != (
            evidence.sha256,
            evidence.quote,
            evidence.extraction_version,
        ):
            raise AppError("SOURCE_CHANGED")
    row = _store(
        home,
        conn,
        io.BytesIO(canonical_json(record).encode("utf-8")),
        {"filename": f"memory-{record.id}-v{record.revision}.md"},
        actor,
        project_id,
        kind="memory",
        provenance={
            "memory_id": record.id,
            "memory_revision": record.revision,
            "search_text": _normalize(record.text),
            "expires_at": record.expires_at,
            "source_versions": {item.artifact_id: item.sha256 for item in record.evidence_refs},
            "explicit_user_save": True,
        },
    )
    _clear_cache(conn, actor, project_id)
    return _view(row, record)


def save_memory(home, conn, actor: Actor, project_id, args, *, explicit_confirmed=False, now=None):
    _explicit(explicit_confirmed)
    require_project(actor, project_id)
    args = _validated(MemorySaveArgs, args)
    now = _now(now)
    record = _validated(
        MemoryRecord,
        {
            **args.model_dump(),
            "id": uuid.uuid4().hex,
            "scope": {"actor_id": actor.id, "project_id": project_id},
            "created_at": now,
            "updated_at": now,
            "revision": 1,
        },
    )
    with transaction(conn):
        return _write(home, conn, record, actor, project_id)


def get_memory(home, conn, actor: Actor, project_id, memory_id, *, now=None):
    rows = _rows(conn, actor, project_id, memory_id=memory_id)
    if not rows:
        raise AppError("NOT_FOUND", status=404)
    record = _read(home, conn, rows[0], actor, project_id)
    if record.expires_at and datetime.fromisoformat(record.expires_at) <= datetime.fromisoformat(
        _now(now)
    ):
        raise AppError("NOT_FOUND", status=404)
    return _view(rows[0], record)


def evidence_from_memory(record, now=None) -> Evidence:
    """Convert a get/search result; locate_memory rechecks the actual artifact for verification."""
    if not isinstance(record, dict):
        raise AppError("INVALID_INPUT")
    value = dict(record)
    artifact_id, sha256 = value.pop("artifact_id", None), value.pop("sha256", None)
    memory = _validated(MemoryRecord, value)
    now = _now(now)
    if memory.expires_at and datetime.fromisoformat(memory.expires_at) <= datetime.fromisoformat(
        now
    ):
        raise AppError("NOT_FOUND", status=404)
    return _validated(
        Evidence,
        {
            "artifact_id": artifact_id,
            "sha256": sha256,
            "locator": "memory:" + memory.id,
            "quote": memory.text,
            "extraction_version": "memory-v1",
            "observed_at": now,
        },
    )


def locate_memory(artifact_id, locator, context) -> Evidence:
    """Read the exact saved revision under the authenticated actor's current project scope."""
    if not isinstance(locator, str) or not locator.startswith("memory:"):
        raise AppError("INVALID_INPUT")
    home, conn, actor, run = (context[key] for key in ("home", "conn", "actor", "run"))
    row = metadata(conn, artifact_id, actor, run["project_id"])
    if row["kind"] != "memory":
        raise AppError("INVALID_INPUT")
    record = _read(home, conn, row, actor, run["project_id"])
    if locator != "memory:" + record.id:
        raise AppError("SOURCE_CHANGED")
    return evidence_from_memory(_view(row, record), now=context.get("now"))


def search_memory(home, conn, actor: Actor, project_id, args, *, now=None):
    args = _validated(MemorySearchArgs, args)
    now = datetime.fromisoformat(_now(now))
    terms = _normalize(args.query).split()
    found = []
    # ponytail: small project index scan; add FTS only when search evaluations require it.
    for row in _rows(conn, actor, project_id):
        if any(term not in row["provenance_json"].get("search_text", "") for term in terms):
            continue
        record = _read(home, conn, row, actor, project_id)
        if record.expires_at and datetime.fromisoformat(record.expires_at) <= now:
            continue
        if all(term in _normalize(record.text) for term in terms):
            found.append(_view(row, record))
            if len(found) == args.limit:
                break
    return found


def edit_memory(
    home,
    conn,
    actor: Actor,
    project_id,
    memory_id,
    args,
    *,
    expected_revision,
    explicit_confirmed=False,
    now=None,
):
    _explicit(explicit_confirmed)
    args = _validated(MemorySaveArgs, args)
    if type(expected_revision) is not int or expected_revision < 1:
        raise AppError("INVALID_INPUT")
    with transaction(conn):
        rows = _rows(conn, actor, project_id, memory_id=memory_id)
        if not rows:
            raise AppError("NOT_FOUND", status=404)
        old = _read(home, conn, rows[0], actor, project_id)
        if old.revision != expected_revision:
            raise AppError("CONFLICT", status=409)
        now = _now(now)
        if datetime.fromisoformat(now) < datetime.fromisoformat(old.updated_at):
            raise AppError("INVALID_INPUT")
        record = _validated(
            MemoryRecord,
            {
                **old.model_dump(),
                **args.model_dump(),
                "updated_at": now,
                "revision": old.revision + 1,
            },
        )
        result = _write(home, conn, record, actor, project_id)
        conn.execute(
            "UPDATE artifacts SET deleted_at=?,revision=revision+1 WHERE id=?",
            (record.updated_at, rows[0]["id"]),
        )
        return result


def rebuild_memory_index(home, conn, actor: Actor, project_id):
    """Rebuild only derived search text from hash-checked authoritative memory files."""
    with transaction(conn):
        rows = _rows(conn, actor, project_id)
        for row in rows:
            record = _read(home, conn, row, actor, project_id)
            provenance = {
                **row["provenance_json"],
                "search_text": _normalize(record.text),
                "expires_at": record.expires_at,
            }
            conn.execute(
                "UPDATE artifacts SET provenance_json=? WHERE id=?",
                (canonical_json(provenance), row["id"]),
            )
        return len(rows)


def _references(value, identifiers):
    if isinstance(value, str):
        return value in identifiers
    if isinstance(value, dict):
        return any(
            key in identifiers or _references(item, identifiers) for key, item in value.items()
        )
    return isinstance(value, list) and any(_references(item, identifiers) for item in value)


def _protected(conn, actor, project_id, identifiers):
    for row in conn.execute(
        "SELECT * FROM runs WHERE actor_id=? AND project_id=?", (actor.id, project_id)
    ):
        run = row_dict(row)
        candidates = [run]
        for table, columns in (("steps", "spec_json,output_json"), ("outbox", "payload_json")):
            for child in conn.execute(
                f"SELECT {columns} FROM {table} WHERE run_id=?", (run["id"],)
            ):
                candidates.append(row_dict(child))
        for artifact in conn.execute(
            "SELECT provenance_json FROM artifacts WHERE run_id=?", (run["id"],)
        ):
            candidates.append(row_dict(artifact))
        for setting in conn.execute(
            "SELECT value_json FROM kv WHERE key IN (?,?)",
            ("task_spec:" + run["id"], "confirmed_sources:" + run["id"]),
        ):
            candidates.append(json.loads(setting[0]))
        if not _references(candidates, identifiers):
            continue
        if run["state"] not in {"succeeded", "failed", "cancelled", "rejected", "expired"}:
            return True
        for table, states in (
            ("approvals", "'pending','approved'"),
            ("calls", "'reserved','in_flight','unknown'"),
            ("outbox", "'pending','sending','retry_wait','unknown'"),
        ):
            if conn.execute(
                f"SELECT 1 FROM {table} WHERE run_id=? AND state IN ({states}) LIMIT 1",
                (run["id"],),
            ).fetchone():
                return True
    return False


def _delete(conn, actor, project_id, rows, now, purge_original):
    ids = {row["id"] for row in rows}
    identifiers = (
        ids
        | {row["sha256"] for row in rows}
        | {row["provenance_json"]["memory_id"] for row in rows}
    )
    protected = purge_original and _protected(conn, actor, project_id, identifiers)
    conn.executemany(
        "UPDATE artifacts SET deleted_at=?,revision=revision+1 WHERE id=? AND deleted_at IS NULL",
        [(now, artifact_id) for artifact_id in ids],
    )
    if purge_original:
        for memory_id in {row["provenance_json"]["memory_id"] for row in rows}:
            set_value(
                conn,
                "memory_purge:" + memory_id,
                {
                    "actor_id": actor.id,
                    "project_id": project_id,
                    "memory_id": memory_id,
                    "revision": max(
                        row["provenance_json"]["memory_revision"]
                        for row in rows
                        if row["provenance_json"]["memory_id"] == memory_id
                    ),
                },
            )
    _clear_cache(conn, actor, project_id)
    return bool(protected)


def _purge(home, rows, purge_original, protected, *, conn=None):
    # Tombstones commit first. A filesystem failure cannot resurrect deleted search data.
    removed = []
    if purge_original and not protected:
        for row in rows:
            relative = PurePosixPath(row["relative_path"])
            if (
                len(relative.parts) != 2
                or relative.parts[0] != "memories"
                or relative.name != f"{row['id']}.md"
            ):
                raise AppError("POLICY_DENIED")
            path = Path(home).absolute().joinpath(*relative.parts)
            with guard_directory(path.parent):
                try:
                    path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    with open_regular(path, Path(home).absolute()):
                        pass
                    # Parent handles stay held; unlink removes only this managed directory entry.
                    unlink_file(path)
            removed.append(row["id"])
        if conn is not None:
            with transaction(conn):
                conn.executemany(
                    "DELETE FROM kv WHERE key=?",
                    [
                        ("memory_purge:" + memory_id,)
                        for memory_id in {row["provenance_json"]["memory_id"] for row in rows}
                    ],
                )
    return {
        "deleted": True,
        "memory_ids": sorted({row["provenance_json"]["memory_id"] for row in rows}),
        "removed_artifact_ids": sorted(removed),
        "purge_pending": bool(protected),
    }


def delete_memory(
    home,
    conn,
    actor: Actor,
    project_id,
    memory_id,
    *,
    expected_revision,
    explicit_confirmed=False,
    purge_original=False,
    now=None,
):
    _explicit(explicit_confirmed)
    if (
        type(expected_revision) is not int
        or expected_revision < 1
        or type(purge_original) is not bool
    ):
        raise AppError("INVALID_INPUT")
    with transaction(conn):
        rows = _rows(conn, actor, project_id, memory_id=memory_id, include_deleted=True)
        if not rows:
            raise AppError("NOT_FOUND", status=404)
        if rows[0]["provenance_json"]["memory_revision"] != expected_revision:
            raise AppError("CONFLICT", status=409)
        protected = _delete(conn, actor, project_id, rows, _now(now), purge_original)
    return _purge(home, rows, purge_original, protected, conn=conn)


def delete_project_memories(
    home,
    conn,
    actor: Actor,
    project_id,
    *,
    explicit_confirmed=False,
    purge_original=False,
    now=None,
):
    """Invoke before revoking a project; this never alters project access or other files."""
    _explicit(explicit_confirmed)
    if type(purge_original) is not bool:
        raise AppError("INVALID_INPUT")
    with transaction(conn):
        rows = _rows(conn, actor, project_id, include_deleted=True)
        protected = _delete(conn, actor, project_id, rows, _now(now), purge_original)
    return _purge(home, rows, purge_original, protected, conn=conn)


def retry_requested_purges(home, conn, actor):
    """Resume only durable, explicit file-removal requests after their references resolve."""
    requests = conn.execute(
        "SELECT key,value_json FROM kv WHERE key LIKE 'memory_purge:%' "
        "AND json_extract(value_json,'$.actor_id')=? ORDER BY updated_at,key LIMIT 50",
        (actor.id,),
    ).fetchall()
    for row in requests:
        value = json.loads(row["value_json"])
        if value["project_id"] not in actor.project_ids:
            continue
        try:
            delete_memory(
                home,
                conn,
                actor,
                value["project_id"],
                value["memory_id"],
                expected_revision=value["revision"],
                explicit_confirmed=True,
                purge_original=True,
            )
        except (AppError, OSError):
            # Rotate failures so one locked file cannot monopolize the next bounded batch.
            set_value(conn, row["key"], value)
