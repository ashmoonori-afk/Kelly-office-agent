"""Local operation boundaries use real SQLite/files; provider calls are unnecessary."""

import hashlib
import io
import json
import os
import subprocess
import zipfile
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from kelly import artifacts, config, db, engine, operations, planning, telegram, tools
from kelly.__main__ import main
from kelly.automation import AutomationConfig
from kelly.contracts import AppError, PrivacyPolicy, RunRequest

NOW = datetime(2026, 9, 8, tzinfo=UTC)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    path, home = tmp_path / "config.json", tmp_path / "data"
    settings = config.AppConfig(home="data", project_ids=["sales"])
    config.create_config(path, settings)
    with closing(db.init_db(home / "state.db")):
        pass
    return path, home, settings.actor()


def test_doctor_and_export_are_local_allowlists_without_reading_credentials(
    workspace, tmp_path, monkeypatch, capsys
):
    path, home, actor = workspace
    marker = "SYNTHETIC-PRIVATE-REQUEST-SOURCE-SECRET"
    with closing(db.init_db(home / "state.db")) as conn:
        db.create_run(
            conn, actor, "ui", "private-request", RunRequest(request=marker, project_id="sales")
        )
        db.set_value(conn, "private-test", {"prompt": marker, "path": str(home)})
        conn.execute("UPDATE runs SET error_code=?", (marker,))
        db.set_value(
            conn,
            "settings",
            {"telegram": {"enabled": True, "project_id": "sales", "connection_id": "personal"}},
        )
    (home / "auth").mkdir()
    (home / "auth" / "xai.dpapi").write_bytes(marker.encode())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", marker)
    report = operations.doctor(path)
    assert report["ok"] and report["database"] == "ok"
    assert report["credentials_present"] == {"xai": True, "telegram": True}
    assert report["network_calls"] == 0
    assert report["telegram_enabled"]
    output = tmp_path / "safe-log.json"
    log = operations.export_log(path, output)
    assert log["counts"]["runs"] == 1
    assert set(log) == {"format_version", "counts", "states", "safe_errors", "unknown_calls"}
    assert log["safe_errors"] == {"OTHER": 1} and log["states"]["runs"] == {"queued": 1}
    assert log["unknown_calls"] == 0
    assert marker not in output.read_text() and str(home) not in output.read_text()
    assert main(["doctor", "--config", str(path)]) == 0
    rendered = capsys.readouterr().out
    assert marker not in rendered and str(home) not in rendered
    assert main(["export-log", "--config", str(path), "--output", str(output)]) == 1
    assert output.read_text() == db.canonical_json(log)


def test_backup_restore_preserves_sqlite_records_and_retracts_external_authority(
    workspace, tmp_path
):
    path, home, actor = workspace
    with closing(db.init_db(home / "state.db")) as conn:
        normal = db.create_run(
            conn, actor, "ui", "queued", RunRequest(request="합계", project_id="sales")
        )
        external = db.create_run(
            conn, actor, "ui", "external", RunRequest(request="결과 전달", project_id="sales")
        )
        conn.execute("UPDATE runs SET state='running' WHERE id=?", (external["id"],))
        conn.execute(
            "INSERT INTO steps(id,run_id,ordinal,kind,effect,state,spec_json) VALUES('apply',?,0,'apply','external_write','running','{}')",
            (external["id"],),
        )
        conn.execute(
            "INSERT INTO approvals(id,run_id,step_id,actor_id,proposal_hash,proposal_json,state,expires_at,created_at) VALUES('approve',?,'apply','owner',?,'{}','approved',?,?)",
            (external["id"], "a" * 64, (NOW + timedelta(days=1)).isoformat(), NOW.isoformat()),
        )
        for index, state in enumerate(("pending", "retry_wait", "sending", "sent")):
            conn.execute(
                "INSERT INTO outbox(id,run_id,delivery_kind,recipient_ref,content_hash,payload_json,authority_json,state,created_at) VALUES(?,?,'telegram','owner',?,'{}','{}',?,?)",
                (state, external["id"], str(index) * 64, state, NOW.isoformat()),
            )
        for index, state in enumerate(("settled", "in_flight"), 1):
            conn.execute(
                "INSERT INTO calls(id,run_id,logical_call_no,transport_attempt_no,provider,model_id,purpose,request_hash,prompt_version,policy_version,route_json,price_snapshot_json,state,reserved_tokens,input_tokens_total,output_tokens_total,usage_basis,timezone_name,day_start_utc,day_end_utc,month_start_utc,month_end_utc,created_at,settled_at) VALUES(?,?,1,?,'xai','synthetic','plan',?,'v1','v1','{}','{}',?,10,3,2,?,'UTC',?,?,?,?,?,?)",
                (
                    state,
                    normal["id"],
                    index,
                    "b" * 64,
                    state,
                    "provider" if state == "settled" else "estimated",
                    NOW.isoformat(),
                    NOW.isoformat(),
                    NOW.isoformat(),
                    NOW.isoformat(),
                    NOW.isoformat(),
                    NOW.isoformat() if state == "settled" else None,
                ),
            )
        bot = db.create_automation(
            conn,
            actor,
            AutomationConfig(
                name="예약",
                project_id="sales",
                recipe="aggregate",
                instruction="금액 합계",
                trigger={"kind": "once", "at": (NOW + timedelta(days=1)).isoformat()},
            ),
            now=NOW,
        )
        db.set_automation_enabled(
            conn,
            bot["id"],
            bot["revision"],
            True,
            actor,
            now=NOW,
            confirmed_hash=db.automation_proposal_hash(bot),
        )
        watched = tmp_path / "watched"
        watched.mkdir()
        grant = config.register_folder(conn, home, actor, "sales", watched, explicit_confirmed=True)
        telegram.register_connection(conn, actor, "personal", "sales", now=NOW)
        paired = db.get_value(conn, "telegram:connection:personal")
        paired.update(user_id=123, chat_id=123, credential_fingerprint="c" * 64)
        db.set_value(conn, "telegram:connection:personal", paired)
        for key in (
            "telegram:pair:personal",
            "telegram:callback:opaque",
            "telegram:automation:" + bot["id"],
        ):
            db.set_value(conn, key, {"synthetic": True})
        db.set_value(
            conn,
            "settings",
            {
                "privacy": [
                    PrivacyPolicy(
                        project_id="sales", allow_cloud=True, allowed_providers=["xai"]
                    ).model_dump()
                ],
                "telegram": {"enabled": True, "project_id": "sales", "connection_id": "personal"},
            },
        )
        db.set_value(conn, "retained-decision", {"text": "과거 결정"})
    (home / "auth").mkdir()
    (home / "auth" / "xai.dpapi").write_bytes(b"SYNTHETIC-SECRET-DO-NOT-COPY")
    snapshot = tmp_path / "snapshot"
    manifest = operations.backup(path, snapshot)
    assert set(manifest["files"]) == {"state.db", "config.json"}
    assert not (snapshot / "auth").exists()
    restored = tmp_path / "restored"
    assert operations.restore(snapshot, restored)["authority_reset"]
    recovered_config = config.load_config(restored / "config.json")
    assert config.home_path(recovered_config, restored / "config.json") == restored
    assert not (restored / "auth").exists()
    with closing(db.init_db(restored / "state.db")) as conn:
        assert db.get_run(conn, normal["id"], actor)["state"] == "cancelled"
        assert db.get_run(conn, normal["id"], actor)["error_code"] == "RESTORE_REVIEW_REQUIRED"
        assert db.get_run(conn, external["id"], actor)["state"] == "outcome_unknown"
        assert conn.execute("SELECT state FROM approvals").fetchone()[0] == "expired"
        assert dict(conn.execute("SELECT id,state FROM outbox")) == {
            "pending": "cancelled",
            "retry_wait": "cancelled",
            "sending": "unknown",
            "sent": "sent",
        }
        calls = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM calls")}
        assert (
            calls["settled"]["input_tokens_total"] == 3
            and calls["settled"]["usage_basis"] == "provider"
        )
        assert calls["in_flight"]["state"] == "unknown"
        assert db.get_automation(conn, bot["id"], actor)["enabled"] == 0
        assert db.get_value(conn, "folder:" + grant["id"]) is None
        reset = db.get_value(conn, "telegram:connection:personal")
        assert reset["chat_id"] is None and reset["user_id"] is None
        assert reset["revision"] == paired["revision"] + 1
        assert reset["update_epoch"] == paired["update_epoch"] + 1
        assert "credential_fingerprint" not in reset
        assert db.get_value(conn, "telegram:callback:opaque") is None
        effective = config.runtime_config(recovered_config, conn)
        assert not effective.telegram.enabled and not effective.privacy_for("sales").allow_cloud
        assert db.get_value(conn, "retained-decision") == {"text": "과거 결정"}
    with closing(db.init_db(home / "state.db")) as conn:
        assert db.get_run(conn, normal["id"], actor)["state"] == "queued"
        assert db.get_automation(conn, bot["id"], actor)["enabled"] == 1
    diagnosis = operations.export_log(restored / "config.json", tmp_path / "restored-log.json")
    assert diagnosis["unknown_calls"] == 1
    assert diagnosis["states"]["outbox"]["unknown"] == 1
    assert diagnosis["safe_errors"] == {"RESTORE_REVIEW_REQUIRED": 1}
    assert all(
        hashlib.sha256((snapshot / name).read_bytes()).hexdigest() == item["sha256"]
        for name, item in manifest["files"].items()
    )


def test_backup_requires_stopped_daemon_and_never_overwrites(workspace, tmp_path):
    path, home, _ = workspace
    # Windows denies replacement; POSIX detects it before guarded work is accepted.
    if os.name == "nt":
        with operations._identity_guard(home / "state.db"), pytest.raises(OSError):
            (home / "state.db").rename(home / "replacement.db")
    else:
        with pytest.raises(AppError) as error, operations._identity_guard(home / "state.db"):
            (home / "state.db").rename(home / "replacement.db")
        assert error.value.code == "SOURCE_CHANGED"
        (home / "replacement.db").rename(home / "state.db")
    snapshot = tmp_path / "snapshot"
    with engine.DaemonLock(home / "daemon.lock"), pytest.raises(AppError, match="이미 실행"):
        operations.backup(path, snapshot)
    assert not snapshot.exists()
    operations.backup(path, snapshot)
    before = (snapshot / "manifest.json").read_bytes()
    with pytest.raises(AppError):
        operations.backup(path, snapshot)
    assert (snapshot / "manifest.json").read_bytes() == before
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "user-data.txt"
    sentinel.write_bytes(b"preserve")
    with pytest.raises(AppError):
        operations.restore(snapshot, existing)
    assert sentinel.read_bytes() == b"preserve"
    with pytest.raises(AppError):
        operations.backup(path, home / "nested")


def test_only_pinned_evaluation_bytes_are_preserved_without_qualifying_a_model(workspace, tmp_path):
    path, home, _ = workspace
    # Deliberately not live qualification evidence; copying cannot make it valid.
    report = b'{"mode":"offline","note":"synthetic backup fixture"}'
    profile = config.ModelProfile(
        binding={
            "provider": "xai",
            "model_id": "synthetic",
            "roles": ["L"],
            "capabilities": ["analyze"],
            "context_window": 4096,
            "max_output": 512,
            "metering_mode": "api",
            "quality_evidence_id": "fixed-evidence",
        },
        quality_evidence_sha256=hashlib.sha256(report).hexdigest(),
    )
    with closing(db.init_db(home / "state.db")) as conn:
        db.set_value(conn, "settings", {"models": [profile.model_dump()]})
    folder = home / "evaluations"
    folder.mkdir()
    (folder / "fixed-evidence.json").write_bytes(report)
    (folder / "unreferenced.json").write_bytes(b"not included")
    assert operations.doctor(path)["configured_models"] == 1
    snapshot, restored = tmp_path / "snapshot", tmp_path / "restored"
    manifest = operations.backup(path, snapshot)
    assert "evaluations/fixed-evidence.json" in manifest["files"]
    assert "evaluations/unreferenced.json" not in manifest["files"]
    operations.restore(snapshot, restored)
    assert (restored / "evaluations" / "fixed-evidence.json").read_bytes() == report
    assert config._qualification(profile, restored) == "invalid_report"
    (folder / "fixed-evidence.json").unlink()
    with pytest.raises(AppError):
        operations.backup(path, tmp_path / "missing-pinned-report")


def test_deleted_memory_bytes_and_purged_tombstones_are_explicit(workspace, tmp_path):
    path, home, _ = workspace
    retained_path = "memories/" + "a" * 32 + ".md"
    (home / "memories").mkdir()
    retained = b"explicitly deleted memory kept until purge"
    (home / retained_path).write_bytes(retained)
    tombstones = ["memories/" + "b" * 32 + ".md", "exports/" + "c" * 32 + ".txt"]
    with closing(db.init_db(home / "state.db")) as conn:
        for relative, kind, provenance in (
            (retained_path, "memory", {}),
            (tombstones[0], "memory", {}),
            (tombstones[1], "result", {"retention_archived": True, "purged": True}),
        ):
            conn.execute(
                "INSERT INTO artifacts(id,actor_id,project_id,kind,relative_path,sha256,media_type,byte_size,provenance_json,created_at,deleted_at) VALUES(?, 'owner','sales',?,?,?,'text/plain',?,?,?,?)",
                (
                    relative.split("/")[1].split(".")[0],
                    kind,
                    relative,
                    hashlib.sha256(retained).hexdigest(),
                    len(retained),
                    json.dumps(provenance),
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
    snapshot, restored = tmp_path / "snapshot", tmp_path / "restored"
    manifest = operations.backup(path, snapshot)
    assert manifest["omitted_files"] == sorted(tombstones)
    assert (snapshot / retained_path).read_bytes() == retained
    operations.restore(snapshot, restored)
    assert (restored / retained_path).read_bytes() == retained
    with closing(db.init_db(restored / "state.db")) as conn:
        assert (
            conn.execute("SELECT count(*) FROM artifacts WHERE deleted_at IS NOT NULL").fetchone()[
                0
            ]
            == 3
        )
    with closing(db.init_db(home / "state.db")) as conn:
        conn.execute("UPDATE artifacts SET deleted_at=NULL WHERE relative_path=?", (tombstones[0],))
    with pytest.raises(AppError) as error:
        operations.backup(path, tmp_path / "missing-active-source")
    assert error.value.code == "SOURCE_CHANGED"


@pytest.mark.parametrize(
    "attack",
    [
        "traversal",
        "extra",
        "hash",
        "oversize",
        "oversize_document",
        "oversize_evaluation",
        "corrupt_db",
        "junction",
        "zip_bomb",
    ],
)
def test_restore_rejects_unsafe_backups_before_creating_home(workspace, tmp_path, attack):
    path, _, _ = workspace
    snapshot = tmp_path / "snapshot"
    operations.backup(path, snapshot)
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if attack == "traversal":
        manifest["files"]["../escape"] = {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    elif attack == "extra":
        (snapshot / "extra.txt").write_bytes(b"unlisted")
    elif attack == "hash":
        (snapshot / "config.json").write_bytes(b"changed")
    elif attack == "oversize":
        manifest["files"]["state.db"]["bytes"] = operations.MAX_FILE_BYTES + 1
    elif attack in {"oversize_document", "oversize_evaluation"}:
        relative, maximum = (
            ("sources/" + "f" * 32 + ".txt", 25 * 1024 * 1024)
            if attack == "oversize_document"
            else ("evaluations/fixed.json", config.MAX_EVALUATION_BYTES)
        )
        manifest["files"][relative] = {"bytes": maximum + 1, "sha256": "0" * 64}
    elif attack == "corrupt_db":
        damaged = b"not SQLite"
        (snapshot / "state.db").write_bytes(damaged)
        manifest["files"]["state.db"] = {
            "bytes": len(damaged),
            "sha256": hashlib.sha256(damaged).hexdigest(),
        }
    elif attack == "junction":
        linked = tmp_path / "linked"
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked), str(snapshot)],
                capture_output=True,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert result.returncode == 0
        else:
            linked.symlink_to(snapshot, target_is_directory=True)
        snapshot = linked
    elif attack == "zip_bomb":
        archive = tmp_path / "bomb.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            output.writestr("oversize.txt", b"0" * 1024 * 1024)
        snapshot = archive
    manifest_path.write_text(json.dumps(manifest))
    target = tmp_path / "restored"
    with pytest.raises((AppError, db.sqlite3.Error)):
        operations.restore(snapshot, target)
    assert not target.exists()
    assert not (tmp_path / "escape").exists()


def test_actual_office_result_and_source_survive_backup_restore(workspace, tmp_path):
    from openpyxl import load_workbook

    path, home, actor = workspace
    csv = "부서,금액\n영업,1\n지원,2\n".encode()
    with closing(db.init_db(home / "state.db")) as conn:
        source = artifacts.import_source(
            home, conn, io.BytesIO(csv), {"filename": "sales.csv"}, actor, "sales"
        )
        run = db.create_run(
            conn,
            actor,
            "ui",
            "sum",
            RunRequest(
                request="금액 합계 계산해줘",
                project_id="sales",
                artifact_ids=[source["id"]],
                output_format="xlsx",
            ),
        )
        planning.prepare_run(
            conn,
            home,
            run["id"],
            actor,
            tools=tools.registry(home),
            capability_catalog=[],
            privacy_policy=PrivacyPolicy(project_id="sales"),
            complete=None,
        )
        for _ in range(8):
            if db.get_run(conn, run["id"], actor)["state"] == "succeeded":
                break
            engine.execute_next(
                conn,
                run["id"],
                actor=actor,
                tools=tools.registry(home),
                complete=None,
                home=home,
                now=datetime.now(UTC),
            )
        finished = db.get_run(conn, run["id"], actor)
        assert finished["state"] == "succeeded"
        receipt = next(
            item["receipt"]
            for item in finished["result_json"]["outputs"].values()
            if "receipt" in item
        )
        with artifacts.open_artifact(home, conn, receipt["artifact_id"], actor, "sales") as stream:
            original_result = stream.read()
    snapshot, restored = tmp_path / "snapshot", tmp_path / "restored"
    operations.backup(path, snapshot)
    operations.restore(snapshot, restored)
    with closing(db.init_db(restored / "state.db")) as conn:
        assert db.get_run(conn, run["id"], actor)["result_json"] == finished["result_json"]
        for artifact_id, expected in (
            (source["id"], csv),
            (receipt["artifact_id"], original_result),
        ):
            with artifacts.open_artifact(restored, conn, artifact_id, actor, "sales") as stream:
                assert stream.read() == expected
        workbook = load_workbook(io.BytesIO(original_result), data_only=True)
        try:
            assert list(workbook["Result"].values) == [("그룹", "금액"), ("전체", "3.00")]
        finally:
            workbook.close()
        assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    assert (home / source["relative_path"]).read_bytes() == csv
