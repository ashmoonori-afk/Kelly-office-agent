import json
import sqlite3

import pytest

from kelly.contracts import Actor, AppError, RunRequest
from kelly.db import apply_revision, create_run, get_run, init_db, transaction


def test_durable_idempotency_and_access(tmp_path):
    path = tmp_path / "state.db"
    conn = init_db(path)
    actor = Actor(id="owner", project_ids=["sales"])
    request = RunRequest(request="매출 합계", project_id="sales")
    first = create_run(conn, actor, "ui", "submit-1", request)
    assert create_run(conn, actor, "ui", "submit-1", request)["id"] == first["id"]
    with pytest.raises(AppError) as conflict:
        create_run(
            conn, actor, "ui", "submit-1", request.model_copy(update={"request": "다른 요청"})
        )
    assert conflict.value.code == "CONFLICT"
    assert first["task_cap_micro_usd"] is None
    with pytest.raises(AppError):
        get_run(conn, first["id"], Actor(id="other", project_ids=["sales"]))
    with pytest.raises(AppError):
        create_run(conn, Actor(id="owner", project_ids=[]), "ui", "2", request)
    conn.close()
    conn = init_db(path)
    assert get_run(conn, first["id"], actor)["request_json"] == request.model_dump()
    conn.close()


def test_constraints_revisions_and_rollback(tmp_path):
    conn = init_db(tmp_path / "state.db")
    actor = Actor(id="owner", project_ids=["sales"])
    run = create_run(conn, actor, "ui", "1", RunRequest(request="읽기", project_id="sales"))
    updated = apply_revision(conn, "runs", run["id"], 1, {"error_code": "MISSING_INPUT"})
    assert updated["revision"] == 2
    with pytest.raises(AppError):
        apply_revision(conn, "runs", run["id"], 1, {"error_code": None})
    with pytest.raises(AppError):
        apply_revision(conn, "runs; DROP TABLE runs", run["id"], 2, {"error_code": None})
    with pytest.raises(AppError):
        apply_revision(conn, "runs", run["id"], 2, {"actor_id": "other"})
    with pytest.raises(AppError):
        apply_revision(conn, "runs", run["id"], 2, {"request_json": {"request": "changed"}})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE runs SET state='invented' WHERE id=?", (run["id"],))
    with pytest.raises(RuntimeError), transaction(conn):
        conn.execute("INSERT INTO kv VALUES ('temp','{}',1,'now')")
        raise RuntimeError("crash")
    assert conn.execute("SELECT * FROM kv WHERE key='temp'").fetchone() is None
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert len(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()) == 12
    assert (
        json.loads(
            conn.execute("SELECT value_json FROM kv WHERE key='schema_version'").fetchone()[0]
        )
        == 2
    )
    conn.close()


def test_deferred_foreign_key_commit_failure_rolls_back_and_releases_transaction(tmp_path):
    conn = init_db(tmp_path / "state.db")
    with pytest.raises(sqlite3.IntegrityError), transaction(conn):
        conn.execute("PRAGMA defer_foreign_keys=ON")
        conn.execute(
            "INSERT INTO steps (id,run_id,ordinal,kind,effect,spec_json) VALUES ('orphan','missing',0,'tool','read','{}')"
        )
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM steps").fetchone()[0] == 0
    with transaction(conn):
        conn.execute("INSERT INTO kv VALUES ('after_failure','true',1,'now')")
    assert (
        conn.execute("SELECT value_json FROM kv WHERE key='after_failure'").fetchone()[0] == "true"
    )
    conn.close()
