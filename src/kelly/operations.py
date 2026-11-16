"""Offline diagnostics and bounded, uncompressed backups of managed local data."""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, db, engine
from ._vendor.office import path_identity, windows_native
from ._vendor.office.errors import DocumentError
from .contracts import SAFE_MESSAGES, AppError, canonical_json
from .safe_io import guard_directory, open_new, open_regular, validate_root

MAX_FILES = 10000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MANAGED = {"sources", "drafts", "exports", "memories"}


def _relative(value):
    if value in {"state.db", "config.json"}:
        return value
    if isinstance(value, str) and re.fullmatch(
        r"evaluations/[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.json", value
    ):
        return value
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:sources|drafts|exports|memories)/[a-f0-9]{32}\.[a-z0-9]+", value
    ):
        raise AppError("POLICY_DENIED")
    return value


def _read(path, root, maximum=MAX_FILE_BYTES):
    with open_regular(path, root) as stream:
        if os.fstat(stream.fileno()).st_size > maximum:
            raise AppError("LIMIT_EXCEEDED")
        content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise AppError("LIMIT_EXCEEDED")
        return content


def _write(path, content):
    with guard_directory(path.parent), open_new(path) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def _identity_guard(path):
    # A data-read handle enforces delete-sharing denial; metadata-only handles
    # are insufficient on Windows. SQLite may read/write the same pinned file.
    try:
        expected = path_identity.regular_path_identity(path)
        native = windows_native.api()
        handle = windows_native.open_handle(
            path,
            directory=False,
            access=native.GENERIC_READ,
            share=native.FILE_SHARE_READ | native.FILE_SHARE_WRITE,
        )
    except (OSError, DocumentError):
        raise AppError("POLICY_DENIED") from None
    try:
        if windows_native.handle_identity(handle) != expected:
            raise AppError("SOURCE_CHANGED")
        yield
    finally:
        path_identity.close_guard(handle)


@contextmanager
def _database(home):
    with guard_directory(home), ExitStack() as stack:
        for suffix in ("", "-wal", "-shm"):
            path = home / ("state.db" + suffix)
            if not suffix or path.exists():
                stack.enter_context(_identity_guard(path))
        conn = stack.enter_context(
            closing(sqlite3.connect((home / "state.db").as_uri() + "?mode=ro", uri=True))
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA trusted_schema=OFF")
        yield conn


@contextmanager
def _new_database(path):
    _write(path, b"")
    with _identity_guard(path), closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # This unpublished copy can be retried after a crash. Memory journals
        # avoid opening attacker-created journal aliases in its new directory.
        conn.execute("PRAGMA journal_mode=MEMORY")
        yield conn


def _integrity(conn):
    size = (
        conn.execute("PRAGMA page_count").fetchone()[0]
        * conn.execute("PRAGMA page_size").fetchone()[0]
    )
    if size > MAX_FILE_BYTES:
        raise AppError("LIMIT_EXCEEDED")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone():
        raise AppError("SCHEMA_VERSION")
    if conn.execute("PRAGMA quick_check").fetchall()[0][0] != "ok":
        raise AppError("INVALID_INPUT")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise AppError("INVALID_INPUT")
    row = conn.execute("SELECT value_json FROM kv WHERE key='schema_version'").fetchone()
    if row is None or json.loads(row[0]) != 1:
        raise AppError("SCHEMA_VERSION")


def doctor(config_path):
    """Only local presence/health checks: no secret decryption or provider probe."""
    settings = config.load_config(config_path)
    home = config.home_path(settings, config_path)
    validate_root(home)
    existing = home
    while not existing.exists():
        existing = existing.parent
    database = "not_initialized"
    if (home / "state.db").exists():
        try:
            with _database(home) as conn:
                _integrity(conn)
                settings = config.runtime_config(settings, conn)
            database = "ok"
        except (AppError, sqlite3.Error, ValueError):
            database = "invalid"
    return {
        "format_version": 1,
        "ok": database != "invalid" and os.name == "nt",
        "database": database,
        "disk_free_bytes": shutil.disk_usage(existing).free,
        "timezone": ZoneInfo("Asia/Seoul").key,
        "configured_models": len(settings.models),
        "telegram_enabled": settings.telegram.enabled,
        "credentials_present": {
            provider: env in os.environ or (home / "auth" / f"{provider}.dpapi").is_file()
            for provider, env in (("xai", "XAI_API_KEY"), ("telegram", "TELEGRAM_BOT_TOKEN"))
        },
        "local_modules": {
            module: importlib.util.find_spec(module) is not None
            for module in ("docx", "openpyxl", "pypdf")
        },
        "network_calls": 0,
    }


def export_log(config_path, destination):
    """An allowlist of numeric counts, never rows, names, paths or error text."""
    settings = config.load_config(config_path)
    home = config.home_path(settings, config_path)
    report = {"format_version": 1, "counts": {}, "states": {}, "safe_errors": {}}
    with _database(home) as conn:
        _integrity(conn)
        for table in ("runs", "steps", "calls", "approvals", "artifacts", "automations", "outbox"):
            report["counts"][table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        state_sets = {
            "runs": set(engine.TRANSITIONS) | engine.TERMINAL,
            "calls": {"reserved", "in_flight", "settled", "released", "unknown"},
            "outbox": {
                "pending",
                "sending",
                "sent",
                "retry_wait",
                "failed",
                "unknown",
                "cancelled",
            },
        }
        for table, allowed in state_sets.items():
            counts = {}
            for state, count in conn.execute(f"SELECT state,count(*) FROM {table} GROUP BY state"):
                label = state if state in allowed else "OTHER"
                counts[label] = counts.get(label, 0) + count
            report["states"][table] = counts
        for table in ("runs", "outbox"):
            for code, count in conn.execute(
                f"SELECT error_code,count(*) FROM {table} WHERE error_code IS NOT NULL GROUP BY error_code"
            ):
                label = code if code in SAFE_MESSAGES else "OTHER"
                report["safe_errors"][label] = report["safe_errors"].get(label, 0) + count
        report["unknown_calls"] = report["states"]["calls"].get("unknown", 0)
    destination = Path(destination).absolute()
    validate_root(destination.parent)
    _write(destination, canonical_json(report).encode())
    return report


@contextmanager
def _stopped(home):
    with guard_directory(home):
        lock = home / "daemon.lock"
        if not lock.exists():
            _write(lock, b"0")
        with _identity_guard(lock), engine.DaemonLock(lock):
            yield


def _new_directory(path):
    path = Path(path).absolute()
    validate_root(path)
    with guard_directory(path.parent):
        try:
            path.mkdir()
        except FileExistsError:
            raise AppError("OUTPUT_EXISTS", status=409) from None
    return path


def _entry(content):
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def _artifact_files(conn, home):
    result, omitted = {}, []
    for row in conn.execute(
        "SELECT relative_path,sha256,byte_size,deleted_at,kind,provenance_json FROM artifacts"
    ):
        relative = _relative(row[0])
        if (
            relative.split("/")[0] not in MANAGED
            or type(row[2]) is not int
            or not 0 <= row[2] <= 25 * 1024 * 1024
            or not isinstance(row[1], str)
            or not re.fullmatch(r"[a-f0-9]{64}", row[1])
        ):
            raise AppError("POLICY_DENIED")
        provenance = json.loads(row[5])
        if not isinstance(provenance, dict):
            raise AppError("INVALID_INPUT")
        tombstone = row[3] is not None and (
            row[4] == "memory" or provenance.get("retention_archived") is True
        )
        if tombstone and (provenance.get("purged") is True or not (home / relative).exists()):
            omitted.append(relative)
            continue
        if not (home / relative).exists():
            raise AppError("SOURCE_CHANGED")
        result[relative] = {"bytes": row[2], "sha256": row[1]}
    return result, sorted(omitted)


def _evaluation_files(settings, home):
    files = {}
    for profile in settings.models:
        evidence_id, sha = profile.binding.quality_evidence_id, profile.quality_evidence_sha256
        if evidence_id is None or sha is None:
            continue  # An unpinned profile remains unqualified.
        relative = _relative(f"evaluations/{evidence_id}.json")
        content = _read(home / relative, home, config.MAX_EVALUATION_BYTES)
        if hashlib.sha256(content).hexdigest() != sha:
            raise AppError("SOURCE_CHANGED")
        files[relative] = _entry(content)
    return files


def backup(config_path, destination):
    """Requires a stopped daemon. Manifest is written last; incomplete copies cannot restore."""
    settings = config.load_config(config_path)
    home = config.home_path(settings, config_path)
    destination = Path(destination).absolute()
    if destination == home or destination.is_relative_to(home):
        raise AppError("POLICY_DENIED")
    with _stopped(home), _database(home) as source:
        _integrity(source)
        files, omitted = _artifact_files(source, home)
        effective = config.runtime_config(settings, source).model_copy(update={"home": "."})
        files.update(_evaluation_files(effective, home))
        if (
            len(files) + 2 > MAX_FILES
            or sum(item["bytes"] for item in files.values()) > MAX_TOTAL_BYTES
        ):
            raise AppError("LIMIT_EXCEEDED")
        destination = _new_directory(destination)
        with guard_directory(destination):
            with _new_database(destination / "state.db") as snapshot:
                source.backup(snapshot)
                snapshot.execute("PRAGMA journal_mode=DELETE")
                _integrity(snapshot)
            files["state.db"] = _entry(_read(destination / "state.db", destination))
            content = canonical_json(effective).encode()
            _write(destination / "config.json", content)
            files["config.json"] = _entry(content)
            total = files["state.db"]["bytes"] + files["config.json"]["bytes"]
            for relative, expected in files.items():
                if relative in {"state.db", "config.json"}:
                    continue
                content = _read(home / relative, home, expected["bytes"])
                if _entry(content) != expected:
                    raise AppError("SOURCE_CHANGED")
                total += len(content)
                if total > MAX_TOTAL_BYTES:
                    raise AppError("LIMIT_EXCEEDED")
                folder = destination / relative.split("/")[0]
                if not folder.exists():
                    folder.mkdir()
                _write(destination / relative, content)
            manifest = {"format_version": 1, "files": files, "omitted_files": omitted}
            encoded = canonical_json(manifest).encode()
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise AppError("LIMIT_EXCEEDED")
            _write(destination / "manifest.json", encoded)
            return manifest


def _validated_backup(root):
    # No archive extraction exists. ZIPs (including bombs) are refused as files.
    with guard_directory(root):
        try:
            manifest = json.loads(_read(root / "manifest.json", root, MAX_MANIFEST_BYTES))
        except (ValueError, UnicodeError):
            raise AppError("INVALID_INPUT") from None
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"format_version", "files", "omitted_files"}
            or type(manifest["format_version"]) is not int
            or manifest["format_version"] != 1
        ):
            raise AppError("INVALID_INPUT")
        files = manifest["files"]
        if (
            not isinstance(files, dict)
            or not 2 <= len(files) <= MAX_FILES
            or not {"state.db", "config.json"} <= files.keys()
        ):
            raise AppError("INVALID_INPUT")
        total = 0
        for relative, expected in files.items():
            _relative(relative)
            if not isinstance(expected, dict) or set(expected) != {"bytes", "sha256"}:
                raise AppError("INVALID_INPUT")
            size, sha = expected["bytes"], expected["sha256"]
            maximum = (
                config.MAX_EVALUATION_BYTES
                if relative.startswith("evaluations/")
                else 25 * 1024 * 1024
                if "/" in relative
                else config.MAX_CONFIG_BYTES
                if relative == "config.json"
                else MAX_FILE_BYTES
            )
            if (
                type(size) is not int
                or not 0 <= size <= maximum
                or not isinstance(sha, str)
                or not re.fullmatch(r"[a-f0-9]{64}", sha)
            ):
                raise AppError("LIMIT_EXCEEDED")
            total += size
            if total > MAX_TOTAL_BYTES:
                raise AppError("LIMIT_EXCEEDED")
        actual = {"manifest.json"}
        for child in root.iterdir():
            if child.name in MANAGED | {"evaluations"}:
                with guard_directory(child):
                    actual.update(child.name + "/" + item.name for item in child.iterdir())
            else:
                actual.add(child.name)
        if actual != {*files, "manifest.json"}:
            raise AppError("INVALID_INPUT")
        for relative, expected in files.items():
            if _entry(_read(root / relative, root, expected["bytes"])) != expected:
                raise AppError("SOURCE_CHANGED")
        with _database(root) as conn:
            _integrity(conn)
            expected_artifacts, omitted = _artifact_files(conn, root)
            if manifest["omitted_files"] != omitted:
                raise AppError("INVALID_INPUT")
            if expected_artifacts != {
                key: value for key, value in files.items() if key.split("/")[0] in MANAGED
            }:
                raise AppError("INVALID_INPUT")
        settings = config.load_config(root / "config.json")
        if _evaluation_files(settings, root) != {
            key: value for key, value in files.items() if key.startswith("evaluations/")
        }:
            raise AppError("INVALID_INPUT")
        return manifest


def _reset_authority(conn, settings):
    engine.resume_after_restart(conn, previous_process_is_dead=True)
    now = db.utc_now()
    safe = settings.model_copy(
        update={
            "home": ".",
            "telegram": settings.telegram.model_copy(update={"enabled": False}),
            "privacy": [
                item.model_copy(update={"allow_cloud": False, "revision": item.revision + 1})
                for item in settings.privacy
            ],
        }
    )
    with db.transaction(conn):
        conn.execute("UPDATE automations SET enabled=0,revision=revision+1,updated_at=?", (now,))
        conn.execute(
            "UPDATE approvals SET state='expired',revision=revision+1,resolved_at=? WHERE state IN ('pending','approved')",
            (now,),
        )
        conn.execute("UPDATE outbox SET state='cancelled' WHERE state IN ('pending','retry_wait')")
        conn.execute(
            "UPDATE inbox_events SET state='skipped' WHERE source='automation' AND state='received'"
        )
        for row in conn.execute("SELECT id,state FROM runs").fetchall():
            if row["state"] not in engine.TERMINAL:
                if row["state"] != "cancel_requested":
                    engine.transition_run(conn, row["id"], row["state"], "cancel_requested")
                engine.transition_run(conn, row["id"], "cancel_requested", "cancelled")
                conn.execute(
                    "UPDATE runs SET error_code='RESTORE_REVIEW_REQUIRED' WHERE id=?", (row["id"],)
                )
                conn.execute(
                    "UPDATE steps SET state='cancelled' WHERE run_id=? AND state NOT IN ('succeeded','failed','cancelled','skipped','outcome_unknown')",
                    (row["id"],),
                )
        connections = conn.execute(
            "SELECT key,value_json FROM kv WHERE key LIKE 'telegram:connection:%'"
        ).fetchall()
        conn.execute(
            "DELETE FROM kv WHERE key LIKE 'folder:%' OR key LIKE 'telegram:%' OR key LIKE 'prepare_pending:%'"
        )
        for row in connections:
            value = json.loads(row["value_json"])
            value.update(
                user_id=None,
                chat_id=None,
                revision=value["revision"] + 1,
                update_epoch=value.get("update_epoch", 1) + 1,
            )
            value.pop("credential_fingerprint", None)
            db.set_value(conn, row["key"], value)
        db.set_value(
            conn,
            "settings",
            {key: safe.model_dump()[key] for key in ("models", "privacy", "telegram")},
        )
        db.set_value(
            conn, "restore_notice", {"reason": "RESTORE_REVIEW_REQUIRED", "restored_at": now}
        )
    return safe


def restore(backup_directory, new_home):
    root, target = Path(backup_directory).absolute(), Path(new_home).absolute()
    if target == root or target.is_relative_to(root):
        raise AppError("POLICY_DENIED")
    with guard_directory(root):
        manifest = _validated_backup(root)
        checked = {
            name: _read(root / name, root, manifest["files"][name]["bytes"])
            for name in ("state.db", "config.json")
        }
        if any(_entry(content) != manifest["files"][name] for name, content in checked.items()):
            raise AppError("SOURCE_CHANGED")
        settings = config.AppConfig.model_validate_json(checked["config.json"])
        target = _new_directory(target)
        with guard_directory(target), _stopped(target):
            for relative, expected in manifest["files"].items():
                content = _read(root / relative, root, expected["bytes"])
                if _entry(content) != expected:
                    raise AppError("SOURCE_CHANGED")
                if "/" in relative:
                    folder = target / relative.split("/")[0]
                    if not folder.exists():
                        folder.mkdir()
                if relative not in {"config.json", "state.db"}:
                    _write(target / relative, content)
            with (
                closing(sqlite3.connect(":memory:")) as source,
                _new_database(target / "state.db") as copied,
            ):
                # Reuse exactly the verified bytes instead of reopening a pathname.
                source.deserialize(checked["state.db"])
                source.execute("PRAGMA trusted_schema=OFF")
                _integrity(source)
                source.backup(copied)
                safe = _reset_authority(copied, settings)
                _integrity(copied)
                copied.execute("PRAGMA journal_mode=DELETE")
            config.create_config(target / "config.json", safe)
            return {
                "format_version": 1,
                "files_restored": len(manifest["files"]),
                "authority_reset": True,
            }
