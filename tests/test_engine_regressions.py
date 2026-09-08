import subprocess
import sys

import pytest
from pydantic import ValidationError
from test_engine import NOW, OWNER, setup_run

from birkin_lite.contracts import AppError, PlanV1, ValidationResult
from birkin_lite.db import get_run, init_db
from birkin_lite.engine import (
    DaemonLock,
    Tool,
    execute_next,
    install_plan,
    request_cancel,
    resume_after_restart,
)


def states(conn):
    return [r[0] for r in conn.execute("SELECT state FROM steps ORDER BY ordinal")]


def test_cancel_between_preflight_and_claim_prevents_tool_execution(tmp_path):
    conn, rid, tools, seen = setup_run(tmp_path)
    execute_next(
        conn,
        rid,
        actor=OWNER,
        tools=tools,
        complete=None,
        now=NOW,
        check_sources=lambda _: request_cancel(conn, rid, OWNER),
    )
    execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    assert seen == []
    assert get_run(conn, rid, OWNER)["state"] == "cancelled"
    assert states(conn) == ["cancelled"] * 3
    conn.close()


def test_verifier_missing_input_can_resume_the_unfinished_step(tmp_path):
    conn, rid, tools, seen = setup_run(tmp_path)
    updated = get_run(conn, rid, OWNER)["plan_json"]["current"]
    updated["todos"][0]["verify_with"] = ["source_input"]
    install_plan(conn, rid, PlanV1.model_validate(updated), OWNER, tools=tools)
    result = execute_next(
        conn,
        rid,
        actor=OWNER,
        tools=tools,
        complete=None,
        now=NOW,
        verifiers={"source_input": lambda *_: ValidationResult(status="needs_input")},
    )
    assert result["state"] == "awaiting_input"
    assert states(conn)[0] == "pending"
    install_plan(conn, rid, PlanV1.model_validate(updated), OWNER, tools=tools)
    for _ in range(3):
        result = execute_next(
            conn,
            rid,
            actor=OWNER,
            tools=tools,
            complete=None,
            now=NOW,
            verifiers={"source_input": lambda *_: ValidationResult(status="pass")},
        )
    assert result["state"] == "succeeded"
    assert seen == ["inspect", "inspect", "extract", "publish"]
    conn.close()


def test_read_failure_during_cancellation_finishes_cancelled(tmp_path):
    conn, rid, tools, seen = setup_run(tmp_path)

    def cancel_and_fail(*_):
        request_cancel(conn, rid, OWNER)
        raise AppError("SOURCE_CHANGED")

    tools["source.inspect"] = Tool("read", cancel_and_fail)
    result = execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    assert result["state"] == "cancelled"
    assert states(conn) == ["cancelled"] * 3
    assert seen == []
    conn.close()


def test_required_step_cannot_depend_on_skipped_optional_output(tmp_path):
    conn, rid, tools, _ = setup_run(tmp_path)
    updated = get_run(conn, rid, OWNER)["plan_json"]["current"]
    updated["todos"][0]["usefulness"] = "optional"
    with pytest.raises((ValidationError, AppError)):
        install_plan(conn, rid, PlanV1.model_validate(updated), OWNER, tools=tools)
    assert states(conn) == ["pending"] * 3
    conn.close()


@pytest.mark.parametrize("status", ["pass", "fail", "needs_input"])
def test_cancel_during_verification_exposes_only_passed_output(tmp_path, status):
    conn, rid, tools, _ = setup_run(tmp_path)
    execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    updated = get_run(conn, rid, OWNER)["plan_json"]["current"]
    updated["todos"][1]["verify_with"] = ["cancel_check"]
    install_plan(conn, rid, PlanV1.model_validate(updated), OWNER, tools=tools)

    def check(*_):
        request_cancel(conn, rid, OWNER)
        return ValidationResult(status=status, checked_layers=["source"])

    result = execute_next(
        conn,
        rid,
        actor=OWNER,
        tools=tools,
        complete=None,
        now=NOW,
        verifiers={"cancel_check": check},
    )
    assert result["state"] == "cancelled"
    assert set(result["result_json"]["outputs"]) == ({"t0", "t1"} if status == "pass" else {"t0"})
    assert states(conn) == [
        "succeeded",
        "succeeded" if status == "pass" else "cancelled",
        "cancelled",
    ]
    conn.close()


def test_read_checker_crash_recovers_run_to_completion(tmp_path):
    conn, rid, tools, seen = setup_run(tmp_path)
    conn.execute("UPDATE runs SET state='checking' WHERE id=?", (rid,))
    conn.execute("UPDATE steps SET state='checking' WHERE run_id=? AND ordinal=0", (rid,))
    conn.close()
    conn = init_db(tmp_path / "state.db")
    actions = resume_after_restart(conn, previous_process_is_dead=True)
    assert [action["action"] for action in actions] == ["resume"]
    for _ in range(4):
        result = execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    assert result["state"] == "succeeded"
    assert states(conn) == ["succeeded"] * 3
    assert seen == ["inspect", "extract", "publish"]
    conn.close()


def test_singleton_lock_is_enforced_across_real_processes(tmp_path):
    code = """
import sys
from pathlib import Path
from birkin_lite.contracts import AppError
from birkin_lite.engine import DaemonLock
try:
    with DaemonLock(Path(sys.argv[1])):
        print('ACQUIRED')
except AppError as exc:
    print(exc.code)
"""
    path = tmp_path / "daemon.lock"

    def child():
        return subprocess.run(
            [sys.executable, "-c", code, str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()

    with DaemonLock(path):
        assert child() == "ALREADY_RUNNING"
    assert child() == "ACQUIRED"
