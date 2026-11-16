from datetime import UTC, datetime

import pytest

from kelly.contracts import Actor, AppError, PlanV1, Requirement, RunRequest
from kelly.db import create_run, get_run, init_db, row_dict
from kelly.engine import (
    DaemonLock,
    Tool,
    execute_next,
    install_plan,
    request_cancel,
    resolve_approval,
    resume_after_restart,
    transition_run,
    validate_plan,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)
OWNER = Actor(id="owner", project_ids=["sales"])


def plan():
    todos = []
    for i, (tool, schema) in enumerate(
        [
            ("source.inspect", "SourceInfoV1"),
            ("source.extract", "EvidenceListV1"),
            ("artifact.publish", "ReceiptV1"),
        ]
    ):
        todos.append(
            {
                "id": f"t{i}",
                "title": tool,
                "requirement_ids": ["r1"],
                "depends_on": [f"t{i - 1}"] if i else [],
                "inputs": [],
                "kind": "mechanical",
                "reason": "확정된 도구 작업",
                "signals": ["fixed_transform"],
                "clarity": "clear",
                "usefulness": "required",
                "executor": {"type": "tool", "tool_name": tool, "args": {}},
                "output_schema": schema,
                "done_when": ["결과 확인"],
                "verify_with": [],
            }
        )
    return PlanV1.model_validate(
        {"requirements": [{"id": "r1", "text": "업무 완료"}], "todos": todos}
    )


def setup_run(tmp_path, *, external=False, key="one"):
    conn = init_db(tmp_path / "state.db")
    run = create_run(conn, OWNER, "ui", key, RunRequest(request="업무 완료", project_id="sales"))
    seen = []

    def result(name, output):
        def call(args, context):
            seen.append(name)
            return output

        return call

    tools = {
        "source.inspect": Tool("read", result("inspect", {"source": {"format": "txt"}})),
        "source.extract": Tool("read", result("extract", {"evidence": []})),
        "artifact.publish": Tool(
            "external_write" if external else "draft", result("publish", {"receipt": {"id": "ok"}})
        ),
    }
    install_plan(conn, run["id"], plan(), OWNER, tools=tools)
    return conn, run["id"], tools, seen


def test_three_todos_persist_and_never_repeat(tmp_path):
    conn, rid, tools, seen = setup_run(tmp_path)
    for _ in range(4):
        execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    assert get_run(conn, rid, OWNER)["state"] == "succeeded"
    assert seen == ["inspect", "extract", "publish"]
    conn.close()
    conn = init_db(tmp_path / "state.db")
    assert resume_after_restart(conn, previous_process_is_dead=True) == []
    execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    assert seen == ["inspect", "extract", "publish"]
    with pytest.raises(AppError):
        transition_run(conn, rid, "succeeded", "queued")
    conn.close()


def test_classification_history_keeps_original_and_correction(tmp_path):
    conn, rid, tools, _seen = setup_run(tmp_path)
    revised = plan()
    revised.todos[1].clarity = "uncertain"
    revised.todos[1].reason = "자료 범위가 추가되어 재검토 필요"
    install_plan(conn, rid, revised, OWNER, tools=tools)
    row = row_dict(
        conn.execute("SELECT * FROM steps WHERE run_id=? AND ordinal=1", (rid,)).fetchone()
    )
    spec = row["spec_json"]
    assert spec["original_classification"]["clarity"] == "clear"
    assert spec["final_classification"]["clarity"] == "uncertain"
    assert spec["correction_reason"] == revised.todos[1].reason
    assert spec["plan_revision"] == 2
    conn.close()


@pytest.mark.parametrize("status,expected", [("fail", "failed"), ("needs_input", "awaiting_input")])
def test_validation_tool_result_blocks_following_publication(tmp_path, status, expected):
    conn, rid, tools, seen = setup_run(tmp_path)
    revised = plan()
    revised.todos[1].executor.tool_name = "artifact.validate"
    revised.todos[1].output_schema = "ValidationResultV1"
    tools["artifact.validate"] = Tool(
        "read",
        lambda _args, _ctx: {
            "validation": {
                "status": status,
                "errors": ["FORMULA_UNVERIFIED"],
                "checked_layers": ["numeric"],
            }
        },
    )
    install_plan(conn, rid, revised, OWNER, tools=tools)
    for _ in range(3):
        execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    assert get_run(conn, rid, OWNER)["state"] == expected
    assert seen == ["inspect"]
    conn.close()


def test_exact_approval_actor_once_and_no_effect_before_approval(tmp_path):
    conn, rid, tools, seen = setup_run(tmp_path, external=True)
    for _ in range(3):
        execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    assert get_run(conn, rid, OWNER)["state"] == "awaiting_approval"
    assert seen == ["inspect", "extract"]
    approval = dict(conn.execute("SELECT * FROM approvals").fetchone())
    with pytest.raises(AppError):
        resolve_approval(
            conn,
            approval["id"],
            Actor(id="other", project_ids=["sales"]),
            "approve",
            approval["proposal_hash"],
            1,
            now=NOW,
        )
    with pytest.raises(AppError):
        resolve_approval(conn, approval["id"], OWNER, "approve", "changed", 1, now=NOW)
    resolve_approval(conn, approval["id"], OWNER, "approve", approval["proposal_hash"], 1, now=NOW)
    execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    assert seen.count("publish") == 1
    assert conn.execute("SELECT state FROM approvals").fetchone()[0] == "consumed"
    with pytest.raises(AppError):
        resolve_approval(
            conn, approval["id"], OWNER, "approve", approval["proposal_hash"], 1, now=NOW
        )
    conn.close()


def test_cancel_recovery_and_singleton(tmp_path):
    conn, rid, tools, seen = setup_run(tmp_path)
    request_cancel(conn, rid, OWNER)
    execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    assert not seen
    assert get_run(conn, rid, OWNER)["state"] == "cancelled"
    with (
        DaemonLock(tmp_path / "daemon.lock"),
        pytest.raises(AppError),
        DaemonLock(tmp_path / "daemon.lock"),
    ):
        pytest.fail("second owner acquired lock")
    with DaemonLock(tmp_path / "daemon.lock"):
        pass
    conn.close()


def test_cancel_between_todos_returns_completed_output(tmp_path):
    conn, rid, tools, seen = setup_run(tmp_path)
    execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    request_cancel(conn, rid, OWNER)
    cancelled = execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    assert cancelled["state"] == "cancelled" and seen == ["inspect"]
    assert cancelled["result_json"]["partial"] is True
    assert set(cancelled["result_json"]["outputs"]) == {"t0"}
    conn.close()


@pytest.mark.parametrize(
    "effect,expected", [("read", "running"), ("external_write", "outcome_unknown")]
)
def test_recovery_uses_effect_not_expiring_lease(tmp_path, effect, expected):
    conn, rid, _tools, _ = setup_run(tmp_path)
    conn.execute("UPDATE steps SET state='running',effect=? WHERE ordinal=0", (effect,))
    with pytest.raises(AppError):
        resume_after_restart(conn, previous_process_is_dead=False)
    resume_after_restart(conn, previous_process_is_dead=True)
    assert get_run(conn, rid, OWNER)["state"] == expected
    conn.close()


def test_independent_requirements_cannot_be_dropped(tmp_path):
    assert (
        validate_plan(plan(), [Requirement(id="r2", text="누락되면 안 되는 항목")]).status == "fail"
    )
    conn, rid, tools, _ = setup_run(tmp_path)
    execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    changed = plan().model_dump()
    changed["todos"][0]["title"] = "성공한 작업 변경"
    with pytest.raises(AppError):
        install_plan(conn, rid, PlanV1.model_validate(changed), OWNER, tools=tools)
    conn.close()
