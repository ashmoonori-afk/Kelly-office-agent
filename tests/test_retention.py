"""Actual SQLite and files; retention does not need an Office parser to test deletion guards."""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from birkin_lite import db, retention, usage
from birkin_lite.contracts import (
    Actor,
    AppError,
    ModelBinding,
    ModelRequest,
    RunRequest,
    canonical_json,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)
OWNER = Actor(id="owner", project_ids=["sales"])
REQUEST = RunRequest(request="합성 업무", project_id="sales")


@pytest.fixture
def home(tmp_path):
    conn = db.init_db(tmp_path / "state.db")
    run = db.create_run(conn, OWNER, "ui", "same", REQUEST)
    old = (NOW - timedelta(days=31)).isoformat()
    conn.execute(
        "UPDATE runs SET state='succeeded',updated_at=?,result_json=? WHERE id=?",
        (old, canonical_json({"outputs": {"t1": {"artifact_id": "result1"}}}), run["id"]),
    )
    for aid, kind, directory, text, run_id in (
        ("source1", "source", "sources", "원본 자료", None),
        ("result1", "result", "exports", "합계 3.00", run["id"]),
    ):
        path = tmp_path / directory / (aid + ".txt")
        path.parent.mkdir()
        data = text.encode()
        path.write_bytes(data)
        conn.execute(
            "INSERT INTO artifacts (id,actor_id,project_id,run_id,kind,relative_path,sha256,"
            "media_type,byte_size,provenance_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                aid,
                OWNER.id,
                "sales",
                run_id,
                kind,
                f"{directory}/{aid}.txt",
                hashlib.sha256(data).hexdigest(),
                "text/plain",
                len(data),
                canonical_json({"filename": aid + ".txt"}),
                old,
            ),
        )
    conn.execute(
        "INSERT INTO steps (id,run_id,ordinal,kind,effect,state,spec_json) "
        "VALUES ('step1',?,0,'tool','draft','succeeded','{}')",
        (run["id"],),
    )
    try:
        yield tmp_path, conn, run["id"]
    finally:
        conn.close()


def expire(home):
    path, conn, _ = home
    return retention.expire_completed(conn, path, OWNER, now=NOW)


def plan_snapshot(home):
    _, conn, run_id = home
    bot = db.create_automation(
        conn,
        OWNER,
        {
            "name": "완료된 예약 업무",
            "project_id": "sales",
            "recipe": "summarize",
            "instruction": "합성 업무",
            "trigger": {"kind": "once", "at": (NOW - timedelta(days=31)).isoformat()},
        },
        now=NOW - timedelta(days=32),
    )
    conn.execute(
        "UPDATE runs SET automation_id=?,automation_revision=? WHERE id=?",
        (bot["id"], bot["revision"], run_id),
    )
    key = "automation_plan:" + bot["id"]
    snapshot = {"scope": "a" * 64, "sources": "b" * 64, "run_id": run_id}
    db.set_value(conn, key, snapshot)
    return key, snapshot


@pytest.mark.parametrize("newer_pointer", [False, True])
def test_expiry_clears_only_its_derived_plan_pointer_and_preserves_originals(home, newer_pointer):
    path, conn, run_id = home
    key, snapshot = plan_snapshot(home)
    if newer_pointer:
        next_run = db.create_run(conn, OWNER, "ui", "newer", REQUEST)
        snapshot["run_id"] = next_run["id"]
        db.set_value(conn, key, snapshot)
    original = (path / "sources/source1.txt").read_bytes()
    assert expire(home) == {"archived_runs": 1, "removed_files": 1, "purge_pending": 0}
    assert db.get_value(conn, key) == (snapshot if newer_pointer else None)
    assert db.get_run(conn, run_id, OWNER)["result_json"]["archived"] is True
    assert not (path / "exports/result1.txt").exists()
    assert (path / "sources/source1.txt").read_bytes() == original


@pytest.mark.parametrize("change", ["other_bot", "extra_reference", "non_digest"])
def test_plan_snapshot_exception_does_not_discard_other_references(home, change):
    path, conn, _ = home
    key, snapshot = plan_snapshot(home)
    if change == "other_bot":
        key = "automation_plan:another-bot"
    elif change == "extra_reference":
        snapshot["artifact_id"] = "result1"
    else:
        snapshot["sources"] = "result1"
    db.set_value(conn, key, snapshot)
    assert expire(home)["archived_runs"] == 0
    assert db.get_value(conn, key) == snapshot
    assert (path / "exports/result1.txt").exists()


def test_expiry_removes_only_unpinned_results_and_preserves_idempotency(home):
    path, conn, run_id = home
    original = (path / "sources/source1.txt").read_bytes()
    assert expire(home) == {"archived_runs": 1, "removed_files": 1, "purge_pending": 0}
    assert not (path / "exports/result1.txt").exists()
    assert (path / "sources/source1.txt").read_bytes() == original
    repeated = db.create_run(conn, OWNER, "ui", "same", REQUEST)
    assert repeated["id"] == run_id and repeated["result_json"]["archived"] is True
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert expire(home)["archived_runs"] == 0


@pytest.mark.parametrize(
    "protection", ["pin", "dependent", "memory", "automation", "approval", "outbox"]
)
def test_retention_protects_explicit_pins_references_and_unresolved_authority(home, protection):
    path, conn, run_id = home
    key, snapshot = plan_snapshot(home)
    if protection == "pin":
        retention.pin_artifact(conn, "result1", OWNER, "sales", True)
    elif protection == "dependent":
        db.create_run(
            conn,
            OWNER,
            "ui",
            "next",
            RunRequest(request="결과를 참고", project_id="sales", artifact_ids=["result1"]),
        )
    elif protection in {"memory", "automation"}:
        if protection == "memory":
            conn.execute(
                "UPDATE artifacts SET kind='memory',provenance_json=? WHERE id='source1'",
                (canonical_json({"evidence_refs": [{"artifact_id": "result1"}]}),),
            )
        else:
            db.set_value(conn, "automation_state:future", {"source_ids": ["result1"]})
    elif protection == "approval":
        conn.execute(
            "INSERT INTO approvals (id,run_id,step_id,actor_id,proposal_hash,proposal_json,expires_at,created_at) "
            "VALUES ('approval1',?,'step1','owner',?,'{}',?,?)",
            (run_id, "a" * 64, NOW.isoformat(), NOW.isoformat()),
        )
    else:
        conn.execute(
            "INSERT INTO outbox (id,run_id,delivery_kind,recipient_ref,content_hash,payload_json,authority_json,state,created_at) "
            "VALUES ('outbox1',?,'telegram','owner',?,'{}','{}','unknown',?)",
            (run_id, "a" * 64, NOW.isoformat()),
        )
    assert expire(home)["archived_runs"] == 0
    assert db.get_value(conn, key) == snapshot
    assert (path / "exports/result1.txt").exists()
    if protection == "pin":
        retention.pin_artifact(conn, "result1", OWNER, "sales", False)
        assert expire(home)["archived_runs"] == 1


def test_archive_sql_failure_never_deletes_file_or_loses_payload(home):
    path, conn, run_id = home
    key, snapshot = plan_snapshot(home)
    conn.execute(
        "CREATE TRIGGER retention_fault BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT,'synthetic retention failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        expire(home)
    assert (path / "exports/result1.txt").exists()
    assert conn.execute("SELECT deleted_at FROM artifacts WHERE id='result1'").fetchone()[0] is None
    assert db.get_run(conn, run_id, OWNER)["request_json"] == REQUEST.model_dump()
    assert db.get_value(conn, key) == snapshot


def test_changed_file_is_not_deleted_and_missing_file_crash_window_recovers(home):
    path, _, _ = home
    target = path / "exports/result1.txt"
    target.write_bytes(b"changed after import")
    assert expire(home) == {"archived_runs": 1, "removed_files": 0, "purge_pending": 1}
    assert target.read_bytes() == b"changed after import"
    target.unlink()  # Simulate a prior unlink whose DB acknowledgement did not commit.
    assert expire(home) == {"archived_runs": 0, "removed_files": 1, "purge_pending": 0}


def test_retention_never_follows_corrupted_relative_path_outside_home(home):
    path, conn, _ = home
    external = path.parent / (path.name + "-outside.txt")
    external.write_bytes(b"keep")
    conn.execute(
        "UPDATE artifacts SET relative_path=? WHERE id='result1'", ("../" + external.name,)
    )
    with pytest.raises(AppError) as error:
        expire(home)
    assert error.value.code == "POLICY_DENIED"
    assert external.read_bytes() == b"keep"


def test_recent_and_inaccessible_project_runs_are_not_archived(home):
    path, conn, run_id = home
    conn.execute(
        "UPDATE runs SET updated_at=? WHERE id=?", ((NOW - timedelta(days=29)).isoformat(), run_id)
    )
    assert expire(home)["archived_runs"] == 0
    conn.execute(
        "UPDATE runs SET project_id='other',updated_at=? WHERE id=?",
        ((NOW - timedelta(days=31)).isoformat(), run_id),
    )
    assert expire(home)["archived_runs"] == 0
    assert (path / "exports/result1.txt").exists()


def test_unknown_usage_protects_completed_payload_until_reconciliation(home):
    path, conn, run_id = home
    key, snapshot = plan_snapshot(home)
    conn.execute("UPDATE runs SET state='queued' WHERE id=?", (run_id,))
    binding = ModelBinding(
        provider="xai",
        model_id="fixture",
        roles=["M"],
        role="M",
        enabled=True,
        available=True,
        allowed_projects=["sales"],
        capabilities=["structured_output", "planning"],
        context_window=32768,
        max_output=1000,
        metering_mode="api",
        quality_evidence_id="fixture",
    )
    request = ModelRequest(
        model_id="fixture",
        prompt_version="v1",
        context="합성",
        output_schema={"type": "object"},
        max_output=100,
    )
    record = usage.begin_call(
        conn, run_id, None, binding, request, actor=OWNER, purpose="plan", now=NOW
    )
    usage.start_call(conn, record["id"], OWNER)
    usage.mark_usage_unknown(conn, record["id"], "PROVIDER_TIMEOUT", actor=OWNER, now=NOW)
    conn.execute("UPDATE runs SET state='succeeded' WHERE id=?", (run_id,))
    assert expire(home)["archived_runs"] == 0
    assert (path / "exports/result1.txt").exists()
    assert usage.summarize_usage(conn, run_id, OWNER)["usage_state"] == "unknown"
    assert db.get_value(conn, key) == snapshot


def test_protected_batch_does_not_starve_later_runs_and_is_revisited(home):
    path, conn, _ = home
    protected = []
    for index in range(100):
        run = db.create_run(conn, OWNER, "ui", str(index), REQUEST)
        protected.append(run["id"])
        conn.execute(
            "UPDATE runs SET state='succeeded',updated_at=? WHERE id=?",
            ((NOW - timedelta(days=32)).isoformat(), run["id"]),
        )
        conn.execute(
            "INSERT INTO outbox (id,run_id,delivery_kind,recipient_ref,content_hash,payload_json,"
            "authority_json,state,created_at) VALUES (?,?,'telegram','owner',?,'{}','{}','unknown',?)",
            (str(index), run["id"], "a" * 64, NOW.isoformat()),
        )
    assert expire(home)["archived_runs"] == 0
    assert expire(home)["archived_runs"] == 1
    assert not (path / "exports/result1.txt").exists()
    conn.execute("UPDATE outbox SET state='sent'")
    assert expire(home)["archived_runs"] == 100
    assert all(db.get_run(conn, rid, OWNER)["result_json"]["archived"] for rid in protected)


def test_purge_batch_advances_past_inaccessible_rows(home):
    path, conn, _ = home
    for index in range(500):
        conn.execute(
            "INSERT INTO artifacts (id,actor_id,project_id,kind,relative_path,sha256,media_type,"
            "byte_size,provenance_json,created_at,deleted_at) "
            "VALUES (?,'owner','other','result',?,?,'text/plain',0,?, ?, ?)",
            (
                f"a{index:03}",
                f"exports/a{index:03}.txt",
                "a" * 64,
                canonical_json({"retention_archived": True}),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    assert expire(home)["removed_files"] == 0
    assert (path / "exports/result1.txt").exists()
    assert expire(home)["removed_files"] == 1
    assert not (path / "exports/result1.txt").exists()
    assert (
        conn.execute(
            "SELECT count(*) FROM artifacts WHERE project_id='other' "
            "AND json_extract(provenance_json,'$.purged')=1"
        ).fetchone()[0]
        == 0
    )
