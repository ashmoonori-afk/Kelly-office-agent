"""Managed copy-on-write artifacts. Public paths are IDs, never model-chosen paths."""

import hashlib
import io
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .contracts import Actor, AppError, canonical_json, digest
from .db import get_run, require_project, row_dict, utc_now
from .safe_io import ensure_directory, guard_directory, open_regular, rename_new, validate_root
from .sandbox import parse_file

MAX_BYTES = 25 * 1024 * 1024
MEDIA = {
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


def _filename(metadata):
    if set(metadata) - {"filename", "media_type"}:
        raise AppError("INVALID_INPUT")
    name = metadata.get("filename")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 240
        or re.search(r"[\\/:\x00-\x1f]", name)
        or name.endswith((".", " "))
    ):
        raise AppError("INVALID_INPUT")
    suffix = Path(name).suffix.lower().lstrip(".")
    if suffix not in MEDIA:
        raise AppError("UNSUPPORTED_CAPABILITY", status=415)
    media = metadata.get("media_type")
    if media and media not in {MEDIA[suffix], "application/octet-stream"}:
        raise AppError("FORMAT_MISMATCH", status=415)
    return name, suffix


def _directory(home, name):
    return ensure_directory(Path(home).absolute() / name)


def _file_stamp(stream):
    try:
        info = os.fstat(stream.fileno())
    except (AttributeError, io.UnsupportedOperation):
        return None
    if not stat.S_ISREG(info.st_mode):
        raise AppError("POLICY_DENIED")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def metadata(conn, artifact_id, actor: Actor, project_id: str):
    require_project(actor, project_id)
    row = row_dict(
        conn.execute(
            "SELECT * FROM artifacts WHERE id=? AND actor_id=? AND project_id=? AND deleted_at IS NULL",
            (artifact_id, actor.id, project_id),
        ).fetchone()
    )
    if row is None:
        raise AppError("NOT_FOUND", status=404)
    return row


@contextmanager
def open_artifact(home, conn, artifact_id, actor, project_id):
    record = metadata(conn, artifact_id, actor, project_id)
    relative = PurePosixPath(record["relative_path"])
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] not in {"sources", "drafts", "exports", "memories"}
        or any(part in {".", ".."} for part in relative.parts)
        or "\\" in str(relative)
        or ":" in str(relative)
    ):
        raise AppError("POLICY_DENIED")
    root = Path(home).absolute()
    validate_root(root)
    with open_regular(root.joinpath(*relative.parts), root) as handle:
        hashed = hashlib.sha256()
        count = 0
        before = _file_stamp(handle)
        while chunk := handle.read(65536):
            count += len(chunk)
            if count > MAX_BYTES:
                raise AppError("SOURCE_CHANGED")
            hashed.update(chunk)
        if (
            count != record["byte_size"]
            or hashed.hexdigest() != record["sha256"]
            or before != _file_stamp(handle)
        ):
            raise AppError("SOURCE_CHANGED")
        handle.seek(0)
        yield handle
        if before != _file_stamp(handle):
            raise AppError("SOURCE_CHANGED")


def _store(
    home,
    conn,
    stream,
    upload,
    actor,
    project_id,
    *,
    kind,
    provenance,
    run_id=None,
    artifact_id=None,
):
    require_project(actor, project_id)
    if run_id:
        run = get_run(conn, run_id, actor)
        if run["project_id"] != project_id:
            raise AppError("POLICY_DENIED")
    filename, suffix = _filename(upload)
    directory = _directory(
        home,
        {"source": "sources", "draft": "drafts", "result": "exports", "memory": "memories"}[kind],
    )
    aid = artifact_id or uuid.uuid4().hex
    destination = directory / f"{aid}.{suffix}"
    temporary = directory / f"{uuid.uuid4().hex}.tmp"
    source_stamp = _file_stamp(stream)
    hasher = hashlib.sha256()
    size = 0
    with guard_directory(directory):
        try:
            with temporary.open("xb") as output:
                while chunk := stream.read(65536):
                    if not isinstance(chunk, bytes):
                        raise AppError("INVALID_INPUT")
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise AppError("LIMIT_EXCEEDED", status=413)
                    hasher.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if source_stamp != _file_stamp(stream):
                raise AppError("SOURCE_CHANGED")
            inspection = parse_file(temporary, filename, operation="preflight")
            if inspection.get("format") != suffix:
                raise AppError("FORMAT_MISMATCH", status=415)
            sha256 = hasher.hexdigest()
            with open_regular(temporary, Path(home).absolute()) as checked:
                if hashlib.file_digest(checked, "sha256").hexdigest() != sha256:
                    raise AppError("SOURCE_CHANGED")
            existing = conn.execute("SELECT id FROM artifacts WHERE id=?", (aid,)).fetchone()
            if existing:
                record = metadata(conn, aid, actor, project_id)
                if (
                    record["sha256"] != sha256
                    or record["media_type"] != MEDIA[suffix]
                    or record["kind"] != kind
                    or record["run_id"] != run_id
                    or record["provenance_json"].get("publication_key")
                    != provenance.get("publication_key")
                ):
                    raise AppError("CONFLICT", status=409)
                with open_artifact(home, conn, aid, actor, project_id):
                    return record
            if destination.exists():
                # Recover only the exact server-generated publication after a DB-write crash.
                if kind != "result" or not provenance.get("publication_key"):
                    raise AppError("CONFLICT", status=409)
                with open_regular(destination, Path(home).absolute()) as existing_file:
                    if hashlib.file_digest(existing_file, "sha256").hexdigest() != sha256:
                        raise AppError("CONFLICT", status=409)
            else:
                rename_new(temporary, destination, expected_sha256=sha256)
            details = {
                **provenance,
                "filename": filename,
                "parser_version": "1",
                "inspection": inspection,
            }
            validation = {
                "status": "pass",
                "checked_layers": ["preflight"],
                "visual_verified": False,
            }
            conn.execute(
                "INSERT INTO artifacts (id,actor_id,project_id,run_id,kind,relative_path,sha256,media_type,byte_size,provenance_json,validation_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    actor.id,
                    project_id,
                    run_id,
                    kind,
                    f"{directory.name}/{destination.name}",
                    sha256,
                    MEDIA[suffix],
                    size,
                    canonical_json(details),
                    canonical_json(validation),
                    utc_now(),
                ),
            )
            return metadata(conn, aid, actor, project_id)
        finally:
            # Only our unpredictable private temporary name is removed; original files are retained.
            if temporary.exists():
                temporary.unlink()


def import_source(home, conn, stream, upload, actor, project_id):
    return _store(home, conn, stream, upload, actor, project_id, kind="source", provenance={})


def create_draft(home, conn, stream, filename, actor, project_id, run_id, *, provenance):
    return _store(
        home,
        conn,
        stream,
        {"filename": filename},
        actor,
        project_id,
        kind="draft",
        provenance=provenance,
        run_id=run_id,
    )


def publish_draft(home, conn, draft_id, actor, project_id, *, run_id, step_id):
    from .usage import summarize_usage

    draft = metadata(conn, draft_id, actor, project_id)
    if draft["kind"] != "draft" or draft["run_id"] != run_id:
        raise AppError("POLICY_DENIED")
    publication_key = digest({"actor": actor.id, "run": run_id, "step": step_id})
    aid = publication_key[:32]
    with open_artifact(home, conn, draft_id, actor, project_id) as stream:
        result = _store(
            home,
            conn,
            stream,
            {"filename": draft["provenance_json"]["filename"]},
            actor,
            project_id,
            kind="result",
            run_id=run_id,
            artifact_id=aid,
            provenance={
                "draft_id": draft_id,
                "draft_hash": draft["sha256"],
                "publication_key": publication_key,
                "sources": draft["provenance_json"],
            },
        )
    return {
        "run_id": run_id,
        "step_id": step_id,
        "artifact_id": result["id"],
        "artifact_hash": result["sha256"],
        "validation": {"status": "pass", "checked_layers": ["preflight", "content_hash"]},
        "usage_state": summarize_usage(conn, run_id, actor)["usage_state"],
        "source_versions": draft["provenance_json"].get("source_versions", {}),
        "parser_version": result["provenance_json"]["inspection"]["parser_version"],
        "template_version": draft["provenance_json"].get("template_version"),
    }
