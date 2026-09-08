import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from birkin_lite import engine
from birkin_lite.contracts import (
    Actor,
    AppError,
    PlanV1,
    RunRequest,
    TaskSpec,
    ToolExecutor,
    canonical_json,
)
from birkin_lite.db import create_run, get_run, get_value, init_db
from birkin_lite.engine import install_plan, step_key

OWNER = Actor(id="owner", project_ids=["sales"])


def plan(count=8):
    return PlanV1.model_validate(
        {
            "requirements": [{"id": "r1", "text": "모든 결과 작성"}],
            "todos": [
                {
                    "id": f"t{i}",
                    "title": f"근거 {i} 요약",
                    "requirement_ids": ["r1"],
                    "kind": "simple",
                    "reason": "명시된 사실 추출",
                    "signals": ["extraction"],
                    "clarity": "clear",
                    "usefulness": "required",
                    "executor": {"type": "model", "role": "L", "task": "summarize"},
                    "output_schema": "ReportSectionsV1",
                    "done_when": ["근거와 결과 일치"],
                }
                for i in range(count)
            ],
        }
    )


def setup(tmp_path, count=8):
    conn = init_db(tmp_path / "state.db")
    run = create_run(conn, OWNER, "ui", "one", RunRequest(request="요약", project_id="sales"))
    value = plan(count)
    install_plan(conn, run["id"], value, OWNER, tools={})
    return conn, run["id"], value


def call(conn, rid, tid="t0", state="settled"):
    data = {
        "id": "call1",
        "run_id": rid,
        "step_id": step_key(rid, tid),
        "logical_call_no": 1,
        "transport_attempt_no": 1,
        "provider": "provider1",
        "model_id": "model",
        "purpose": "generate",
        "request_hash": "h",
        "prompt_version": "v1",
        "policy_version": "v1",
        "route_json": "{}",
        "price_snapshot_json": "{}",
        "state": state,
        "reserved_tokens": 0,
        "usage_basis": "unknown",
        "timezone_name": "UTC",
        "day_start_utc": "2026-09-08",
        "day_end_utc": "2026-09-09",
        "month_start_utc": "2026-09-01",
        "month_end_utc": "2026-10-01",
        "created_at": "2026-09-08T00:00:00Z",
        "settled_at": "2026-09-08T00:00:01Z" if state == "settled" else None,
    }
    conn.execute(
        "INSERT INTO calls (" + ",".join(data) + ") VALUES (" + ",".join("?" for _ in data) + ")",
        tuple(data.values()),
    )


def approval(conn, rid, tid="t0", state="approved"):
    conn.execute(
        "INSERT INTO approvals(id,run_id,step_id,actor_id,proposal_hash,proposal_json,state,"
        "expires_at,created_at,consumed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "approval1",
            rid,
            step_key(rid, tid),
            OWNER.id,
            "hash",
            "{}",
            state,
            "2026-09-09T00:00:00Z",
            "2026-09-08T00:00:00Z",
            "2026-09-08T00:01:00Z" if state == "consumed" else None,
        ),
    )


def snapshot(conn):
    return {
        name: [dict(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY 1")]
        for name in ["steps", "calls", "approvals", "runs", "kv"]
    }


def test_full_eight_step_reorder_keeps_calls_attempts_and_foreign_keys(tmp_path):
    conn, rid, value = setup(tmp_path)
    try:
        call(conn, rid)
        conn.execute(
            "UPDATE steps SET attempt_count=1,claimed_by='old',claimed_at='before',"
            "output_json='{}',validation_json='{}' WHERE id=?",
            (step_key(rid, "t0"),),
        )
        before = dict(
            conn.execute("SELECT * FROM steps WHERE id=?", (step_key(rid, "t0"),)).fetchone()
        )
        old_call = dict(conn.execute("SELECT * FROM calls").fetchone())
        value.todos.reverse()
        target = value.todos[-1]
        target.kind, target.executor.role = "complex", "H"
        install_plan(conn, rid, value, OWNER, tools={})
        after = dict(
            conn.execute("SELECT * FROM steps WHERE id=?", (step_key(rid, "t0"),)).fetchone()
        )
        assert after["ordinal"] == 7 and after["attempt_count"] == 1
        assert all(
            after[key] == val for key, val in before.items() if key not in {"ordinal", "spec_json"}
        )
        assert dict(conn.execute("SELECT * FROM calls").fetchone()) == old_call
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "state", ["succeeded", "running", "checking", "awaiting_approval", "outcome_unknown"]
)
def test_completed_or_active_step_stays_identical_and_fixed_in_place(tmp_path, state):
    conn, rid, value = setup(tmp_path)
    try:
        conn.execute(
            "UPDATE steps SET state=?,attempt_count=1,output_json='{}' WHERE ordinal=0", (state,)
        )
        before = dict(conn.execute("SELECT * FROM steps WHERE ordinal=0").fetchone())
        value.todos[1:] = reversed(value.todos[1:])
        install_plan(conn, rid, value, OWNER, tools={})
        assert dict(conn.execute("SELECT * FROM steps WHERE ordinal=0").fetchone()) == before
        value.todos[0].title = "완료 내용을 바꾸려는 요청"
        snapshot_before = snapshot(conn)
        with pytest.raises(AppError) as error:
            install_plan(conn, rid, value, OWNER, tools={})
        assert error.value.code == "CONFLICT"
        assert snapshot(conn) == snapshot_before
    finally:
        conn.close()


@pytest.mark.parametrize("state", ["reserved", "in_flight"])
def test_outstanding_call_prevents_step_rewrite(tmp_path, state):
    conn, rid, value = setup(tmp_path)
    try:
        call(conn, rid, state=state)
        value.todos[0].title = "다른 작업"
        before = snapshot(conn)
        with pytest.raises(AppError):
            install_plan(conn, rid, value, OWNER, tools={})
        assert snapshot(conn) == before
    finally:
        conn.close()


def test_unknown_model_usage_keeps_history_but_external_unknown_stays_frozen(tmp_path):
    conn, rid, value = setup(tmp_path / "model", 1)
    try:
        call(conn, rid, state="unknown")
        conn.execute("UPDATE steps SET attempt_count=1,output_json='{}',validation_json='{}'")
        before = dict(conn.execute("SELECT * FROM steps").fetchone())
        previous_call = dict(conn.execute("SELECT * FROM calls").fetchone())
        value.todos[0].kind, value.todos[0].executor.role = "complex", "H"
        install_plan(conn, rid, value, OWNER, tools={})
        after = dict(conn.execute("SELECT * FROM steps").fetchone())
        assert all(after[key] == val for key, val in before.items() if key != "spec_json")
        assert dict(conn.execute("SELECT * FROM calls").fetchone()) == previous_call
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()

    conn, rid, value = setup(tmp_path / "external", 1)
    try:
        value.todos[0].executor = ToolExecutor(tool_name="artifact.publish")
        value.todos[0].kind = "mechanical"
        value.todos[0].output_schema = "ReceiptV1"
        registry = {"artifact.publish": engine.Tool("external_write", lambda *_: None)}
        install_plan(conn, rid, value, OWNER, tools=registry)
        call(conn, rid, state="unknown")
        value.todos[0].title = "외부 작업 재실행 금지"
        before = snapshot(conn)
        with pytest.raises(AppError) as error:
            install_plan(conn, rid, value, OWNER, tools=registry)
        assert error.value.code == "CONFLICT"
        assert snapshot(conn) == before
    finally:
        conn.close()


def test_pending_approval_history_is_revoked_but_consumed_approval_is_immutable(tmp_path):
    conn, rid, value = setup(tmp_path)
    try:
        approval(conn, rid)
        value.todos[0].title = "수정된 초안"
        install_plan(conn, rid, value, OWNER, tools={})
        assert conn.execute("SELECT state FROM approvals").fetchone()[0] == "cancelled"
        conn.execute("UPDATE approvals SET state='consumed',consumed_at='now'")
        value.todos[0].title = "다시 실행하면 안 되는 초안"
        before = snapshot(conn)
        with pytest.raises(AppError):
            install_plan(conn, rid, value, OWNER, tools={})
        assert snapshot(conn) == before
    finally:
        conn.close()


@pytest.mark.parametrize("has_record", [False, True])
def test_only_an_unexecuted_pending_step_can_be_removed(tmp_path, has_record):
    conn, rid, value = setup(tmp_path)
    try:
        if has_record:
            call(conn, rid, "t7")
        value.todos.pop()
        if has_record:
            with pytest.raises(AppError):
                install_plan(conn, rid, value, OWNER, tools={})
            assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 8
        else:
            install_plan(conn, rid, value, OWNER, tools={})
            assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 7
    finally:
        conn.close()


def test_retry_wait_preserves_attempt_count_and_optional_skip(tmp_path):
    conn, rid, value = setup(tmp_path)
    try:
        conn.execute("UPDATE runs SET state='retry_wait',revision=revision+1 WHERE id=?", (rid,))
        conn.execute("UPDATE steps SET attempt_count=1 WHERE ordinal=0")
        value.todos[-1].usefulness, value.todos[-1].status = "optional", "skipped"
        install_plan(conn, rid, value, OWNER, tools={})
        assert get_run(conn, rid, OWNER)["state"] == "running"
        assert conn.execute("SELECT attempt_count FROM steps WHERE ordinal=0").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM steps WHERE ordinal=7").fetchone()[0] == "skipped"
    finally:
        conn.close()


def test_history_capacity_is_distinct_from_model_call_limits(tmp_path):
    conn, rid, value = setup(tmp_path, 1)
    try:
        for i in range(32):
            value.todos[0].reason = f"필수 입력 확인 {i}"
            install_plan(conn, rid, value, OWNER, tools={})
        assert len(get_run(conn, rid, OWNER)["plan_json"]["history"]) == 32
        before = snapshot(conn)
        with pytest.raises(AppError) as error:
            install_plan(conn, rid, value, OWNER, tools={})
        assert error.value.code == "LIMIT_EXCEEDED"
        assert snapshot(conn) == before
    finally:
        conn.close()


def test_stale_expected_revision_cannot_overwrite_changed_inputs(tmp_path):
    conn, rid, value = setup(tmp_path)
    try:
        expected = get_run(conn, rid, OWNER)["revision"]
        conn.execute("UPDATE runs SET revision=revision+1 WHERE id=?", (rid,))
        before = snapshot(conn)
        with pytest.raises(AppError) as error:
            install_plan(conn, rid, value, OWNER, tools={}, expected_revision=expected)
        assert error.value.code == "CONFLICT"
        assert snapshot(conn) == before
    finally:
        conn.close()


def test_run_is_reloaded_inside_transaction_before_mutation(tmp_path, monkeypatch):
    conn, rid, value = setup(tmp_path)
    original = engine.transaction

    @contextmanager
    def changed_before_begin(connection):
        connection.execute(
            "UPDATE runs SET state='cancel_requested',revision=revision+1 WHERE id=?", (rid,)
        )
        with original(connection):
            yield connection

    try:
        before = [dict(row) for row in conn.execute("SELECT * FROM steps ORDER BY ordinal")]
        monkeypatch.setattr(engine, "transaction", changed_before_begin)
        with pytest.raises(AppError):
            install_plan(conn, rid, value, OWNER, tools={})
        assert [dict(row) for row in conn.execute("SELECT * FROM steps ORDER BY ordinal")] == before
        assert get_run(conn, rid, OWNER)["state"] == "cancel_requested"
    finally:
        conn.close()


def test_reinsert_failure_rolls_back_rows_references_and_planning_metadata(tmp_path):
    conn, rid, value = setup(tmp_path)
    try:
        call(conn, rid)
        approval(conn, rid)
        before = snapshot(conn)
        conn.execute(
            "CREATE TRIGGER fail_reinsert BEFORE INSERT ON steps WHEN NEW.ordinal=4 "
            "BEGIN SELECT RAISE(ABORT,'injected write failure'); END"
        )
        value.todos.reverse()
        with pytest.raises(sqlite3.IntegrityError):
            install_plan(
                conn,
                rid,
                value,
                OWNER,
                tools={},
                route_bindings={},
                task_spec=TaskSpec(intent="summarize", acceptance=["모든 결과 작성"]),
            )
        assert snapshot(conn) == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 0
        assert not conn.in_transaction
    finally:
        conn.close()


def test_task_spec_and_bindings_saved_atomically_without_confirming_sources(tmp_path):
    conn, rid, value = setup(tmp_path, 1)
    try:
        spec = TaskSpec(intent="summarize", acceptance=["모든 결과 작성"])
        original_request = canonical_json(get_run(conn, rid, OWNER)["request_json"])
        install_plan(conn, rid, value, OWNER, tools={}, task_spec=spec, route_bindings={})
        assert get_value(conn, f"task_spec:{rid}") == spec.model_dump()
        assert get_value(conn, f"route_bindings:{rid}") == {}
        assert get_value(conn, f"confirmed_sources:{rid}") is None
        assert canonical_json(get_run(conn, rid, OWNER)["request_json"]) == original_request
        before = snapshot(conn)
        spec.source_ids = ["foreign"]
        with pytest.raises(AppError) as error:
            install_plan(conn, rid, value, OWNER, tools={}, task_spec=spec)
        assert error.value.code == "POLICY_DENIED"
        assert snapshot(conn) == before
    finally:
        conn.close()


def test_step_key_is_bounded_stable_and_unambiguous():
    assert len(step_key("r" * 96, "t" * 96)) <= 96
    assert step_key("r", "t") == step_key("r", "t")
    assert step_key("a_b", "c") != step_key("a", "b_c")


def test_replanning_before_claim_cannot_execute_the_stale_todo(tmp_path):
    conn, rid, value = setup(tmp_path, 1)
    calls = []
    try:

        def update_plan(_context):
            value.todos[0].title = "사용자가 바꾼 새 요구"
            install_plan(conn, rid, value, OWNER, tools={})

        engine.execute_next(
            conn,
            rid,
            actor=OWNER,
            tools={},
            complete=lambda *args: calls.append(args),
            now=datetime.now(UTC),
            check_sources=update_plan,
        )
        assert calls == []
        assert conn.execute("SELECT attempt_count FROM steps").fetchone()[0] == 0
        assert (
            get_run(conn, rid, OWNER)["plan_json"]["current"]["todos"][0]["title"]
            == "사용자가 바꾼 새 요구"
        )
    finally:
        conn.close()
