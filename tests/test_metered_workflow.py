from datetime import UTC, datetime
from functools import partial

import pytest
from test_prepare_run import OWNER, POLICY, binding, planned, setup

from birkin_lite.contracts import (
    AppError,
    ModelResult,
    PlanV1,
    PriceSnapshot,
    RunRequest,
    TokenRates,
    Usage,
    ValidationResult,
    digest,
    evidence_id,
)
from birkin_lite.db import create_run, get_value, row_dict
from birkin_lite.engine import complete_model_todo, execute_next, install_plan
from birkin_lite.planning import prepare_run
from birkin_lite.providers import metered_complete

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def returned(output):
    return ModelResult(
        parsed_output=output,
        normalized_usage=Usage(
            input_tokens_total=10,
            cached_input_tokens=0,
            output_tokens_total=5,
            reasoning_tokens=1,
            usage_basis="provider",
        ),
        raw_usage={
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
        request_id="test-request",
        finish_reason="stop",
        latency_ms=3,
    )


def prices():
    return PriceSnapshot(
        rates=TokenRates(input=1000000, cached_input=0, output=2000000),
        checked_at=NOW.isoformat(),
        source_url="https://example.com/test-only-rates",
        cache_write_billing="included",
    )


def test_engine_format_repair_records_both_paid_generations_without_escalation(tmp_path):
    conn, run, tools = setup(tmp_path)
    plan = PlanV1.model_validate(planned()["plan"])
    plan.todos[0].signals = ["extraction"]
    install_plan(conn, run["id"], plan, OWNER, tools=tools)
    seen = []

    def transport(request, selected):
        seen.append(selected.role)
        return returned(
            {"sections": "wrong shape"}
            if len(seen) == 1
            else {
                "sections": {
                    "content": {"paragraphs": ["검토 결과"]},
                    "claims": [],
                    "numeric_claims": [],
                    "uncertainties": [],
                }
            }
        )

    completed = execute_next(
        conn,
        run["id"],
        actor=OWNER,
        tools=tools,
        now=NOW,
        complete=partial(
            complete_model_todo,
            capability_catalog=[binding().model_copy(update={"metering_mode": "api"})],
            privacy_policy=POLICY,
            transport=transport,
            price_snapshot=prices(),
        ),
    )
    assert completed["state"] == "succeeded" and seen == ["L", "L"]
    calls = [
        row_dict(row) for row in conn.execute("SELECT * FROM calls ORDER BY transport_attempt_no")
    ]
    assert [row["purpose"] for row in calls] == ["generate", "repair"]
    assert [row["logical_call_no"] for row in calls] == [1, 2]
    assert all(row["state"] == "settled" for row in calls)
    assert sum(row["input_tokens_total"] + row["output_tokens_total"] for row in calls) == 30
    assert sum(row["actual_micro_usd"] for row in calls) == 40
    assert conn.execute("SELECT attempt_count FROM steps").fetchone()[0] == 1


def test_planning_schema_repair_is_metered_and_keeps_planning_purpose(tmp_path):
    conn, run, tools = setup(tmp_path)
    seen = []

    def transport(request, selected):
        seen.append(selected.role)
        return returned({"not_a_plan": True} if len(seen) == 1 else planned())

    installed = prepare_run(
        conn,
        tmp_path,
        run["id"],
        OWNER,
        tools=tools,
        capability_catalog=[binding()],
        privacy_policy=POLICY,
        complete=partial(metered_complete, transport=transport),
    )
    assert installed["state"] == "running" and seen == ["M", "M"]
    calls = [
        row_dict(row) for row in conn.execute("SELECT * FROM calls ORDER BY transport_attempt_no")
    ]
    assert [row["purpose"] for row in calls] == ["plan", "plan"]
    assert all(row["step_id"] is None and row["state"] == "settled" for row in calls)
    assert calls[1]["route_json"]["repair_errors"]
    assert all(row["actual_micro_usd"] is None for row in calls)
    assert get_value(conn, "route_bindings:" + run["id"])["t1"]["role"] == "H"


def test_failed_later_todo_returns_previously_completed_content(tmp_path):
    conn, run, tools = setup(tmp_path)
    plan = PlanV1.model_validate(planned()["plan"])
    plan.todos[0].signals = ["extraction"]
    plan.todos.append(plan.todos[0].model_copy(update={"id": "t2", "depends_on": ["t1"]}))
    install_plan(conn, run["id"], plan, OWNER, tools=tools)
    received = []

    def transport(*_):
        received.append(1)
        return returned(
            {"sections": {"content": {"paragraphs": ["완료한 부분"]}}}
            if len(received) == 1
            else {"sections": []}
        )

    complete = partial(
        complete_model_todo,
        capability_catalog=[binding()],
        privacy_policy=POLICY,
        transport=transport,
    )
    execute_next(conn, run["id"], actor=OWNER, tools=tools, complete=complete, now=NOW)
    failed = execute_next(conn, run["id"], actor=OWNER, tools=tools, complete=complete, now=NOW)
    assert failed["state"] == "failed" and len(received) == 3
    assert failed["result_json"]["partial"] is True
    assert set(failed["result_json"]["outputs"]) == {"t1"}
    assert failed["result_json"]["incomplete"][0]["todo_id"] == "t2"


def test_empty_model_verifiers_cannot_bypass_numeric_check_and_repair_is_bounded(tmp_path):
    conn, run, tools = setup(tmp_path)
    plan = PlanV1.model_validate(planned()["plan"])
    plan.todos[0].signals = ["extraction"]
    plan.todos[0].verify_with = []
    install_plan(conn, run["id"], plan, OWNER, tools=tools)
    seen = []

    def transport(request, selected):
        seen.append(selected.role)
        if len(seen) == 2:
            assert "NUMERIC_MISMATCH" in request.context
        return returned({"sections": {"content": {"paragraphs": ["근거 없는 숫자 999"]}}})

    complete = partial(
        complete_model_todo,
        capability_catalog=[binding()],
        privacy_policy=POLICY,
        transport=transport,
    )
    first = execute_next(conn, run["id"], actor=OWNER, tools=tools, complete=complete, now=NOW)
    assert first["state"] == "running"
    assert conn.execute("SELECT state FROM steps").fetchone()[0] == "pending"
    failed = execute_next(conn, run["id"], actor=OWNER, tools=tools, complete=complete, now=NOW)
    assert failed["state"] == "failed" and seen == ["L", "L"]
    assert failed["result_json"]["outputs"] == {}
    execute_next(conn, run["id"], actor=OWNER, tools=tools, complete=complete, now=NOW)
    assert len(seen) == 2
    conn.close()


def test_new_conflict_reclassifies_only_unfinished_steps_and_preserves_paid_output(tmp_path):
    conn, run, tools = setup(tmp_path)
    plan = PlanV1.model_validate(planned()["plan"])
    first = plan.todos[0]
    first.signals = ["extraction"]
    plan.todos.extend(
        [
            first.model_copy(
                update={"id": "t2", "depends_on": ["t1"], "verify_with": ["conflict_check"]}
            ),
            first.model_copy(update={"id": "t3", "depends_on": ["t2"]}),
        ]
    )
    install_plan(conn, run["id"], plan, OWNER, tools=tools)
    seen = []

    def transport(_request, selected):
        seen.append(selected.role)
        return returned({"sections": {"content": {"paragraphs": ["검토 내용"]}}})

    def conflict_check(_output, context):
        attempts = conn.execute(
            "SELECT attempt_count FROM steps WHERE run_id=? AND ordinal=1", (context["run"]["id"],)
        ).fetchone()[0]
        return ValidationResult(
            status="fail" if attempts == 1 else "pass",
            errors=["CONFLICTING_SOURCES"] if attempts == 1 else [],
            checked_layers=["semantic_review"],
        )

    complete = partial(
        complete_model_todo,
        capability_catalog=[binding()],
        privacy_policy=POLICY,
        transport=transport,
    )
    options = {
        "actor": OWNER,
        "tools": tools,
        "complete": complete,
        "now": NOW,
        "verifiers": {"conflict_check": conflict_check},
    }
    execute_next(conn, run["id"], **options)
    frozen = tuple(conn.execute("SELECT * FROM steps WHERE ordinal=0").fetchone())
    execute_next(conn, run["id"], **options)
    execute_next(conn, run["id"], **options)
    completed = execute_next(conn, run["id"], **options)
    # The failed L generation plus H regeneration exhaust t2's two-call allowance.
    # Newly complex content still requires review; it cannot silently skip it or call a third time.
    assert completed["state"] == "failed" and completed["error_code"] == "CALL_LIMIT"
    assert seen == ["L", "L", "H"]
    assert completed["result_json"]["partial"] is True
    assert set(completed["result_json"]["outputs"]) == {"t1"}
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 3
    assert tuple(conn.execute("SELECT * FROM steps WHERE ordinal=0").fetchone()) == frozen
    assert conn.execute("SELECT count(*) FROM calls WHERE purpose='repair'").fetchone()[0] == 1
    assert completed["plan_json"]["current"]["todos"][0]["executor"]["role"] == "L"
    assert [
        todo["executor"]["role"] for todo in completed["plan_json"]["current"]["todos"][1:]
    ] == ["H", "H"]
    conn.close()


def test_verified_response_cache_reuses_output_without_a_new_provider_call(tmp_path):
    conn, run, tools = setup(tmp_path)
    plan = PlanV1.model_validate(planned()["plan"])
    plan.todos[0].signals = ["extraction"]
    seen = []

    def transport(*_):
        seen.append(1)
        return returned({"sections": {"content": {"paragraphs": ["검토 내용"]}}})

    complete = partial(
        complete_model_todo,
        capability_catalog=[binding()],
        privacy_policy=POLICY,
        transport=transport,
    )
    for current in [
        run,
        create_run(conn, OWNER, "ui", "second", RunRequest.model_validate(run["request_json"])),
    ]:
        install_plan(conn, current["id"], plan, OWNER, tools=tools)
        finished = execute_next(
            conn, current["id"], actor=OWNER, tools=tools, complete=complete, now=NOW, home=tmp_path
        )
        assert finished["state"] == "succeeded"
        assert get_value(conn, "cache_observation:" + current["id"]) == {
            "lookups": 1,
            "hits": int(current["id"] != run["id"]),
        }
    assert len(seen) == 1 and conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 1
    assert get_value(conn, "cache_hit:" + current["id"] + ":t1")
    conn.close()


@pytest.mark.parametrize("accepted", [True, False])
def test_conditional_semantic_review_uses_qualified_h_and_shares_todo_call_limit(
    tmp_path, accepted
):
    conn, run, tools = setup(tmp_path)
    plan = PlanV1.model_validate(planned()["plan"])
    todo = plan.todos[0]
    todo.kind, todo.signals = "simple", ["extraction"]
    todo.executor.task, todo.output_schema = "meeting_actions", "MeetingActionsV1"
    installed = install_plan(conn, run["id"], plan, OWNER, tools=tools)
    evidence = {
        "artifact_id": "fixture",
        "sha256": "a" * 64,
        "locator": "text:line:1",
        "quote": "민지가 제안서 초안을 작성한다.",
        "extraction_version": "office-extract-v1",
        "observed_at": NOW.isoformat(),
    }
    ref = evidence_id(evidence)
    candidate = {
        "actions": [
            {
                "action": "제안서 초안 작성",
                "owner": "민지",
                "due_date": None,
                "due_text": None,
                "evidence_ids": [ref],
            }
        ]
    }
    inputs = {"source": {"items": [evidence]}}
    context = {"conn": conn, "actor": OWNER, "run": installed, "todo": todo, "inputs": inputs}
    seen = []

    def transport(request, selected):
        seen.append(selected.role)
        if len(seen) == 1:
            return returned(candidate)
        assert "candidate_output" in request.context and evidence["quote"] in request.context
        return returned(
            {
                "validation": {
                    "status": "pass" if accepted else "fail",
                    "checked_layers": ["semantic_review"],
                    "evidence_refs": [ref],
                }
            }
        )

    catalog = [
        binding().model_copy(
            update={"capabilities": ["structured_output", "meeting_actions", "semantic_review"]}
        )
    ]
    options = {"capability_catalog": catalog, "privacy_policy": POLICY, "transport": transport}
    if accepted:
        assert complete_model_todo(todo, inputs, context, **options) == candidate
        assert context["semantic_review"][0] == digest(candidate)
    else:
        with pytest.raises(AppError) as error:
            complete_model_todo(todo, inputs, context, **options)
        assert error.value.code == "SEMANTIC_REVIEW_FAILED"
    assert seen == ["L", "H"]
    calls = [row_dict(row) for row in conn.execute("SELECT * FROM calls ORDER BY logical_call_no")]
    assert [call["purpose"] for call in calls] == ["generate", "review"]
    assert calls[-1]["route_json"]["review_output_hash"] == digest(candidate)
    assert calls[-1]["route_json"]["role"] == "H"
    assert (
        conn.execute("SELECT state FROM steps").fetchone()[0] == "pending"
    )  # Dispatch contract only; engine has not verified source files.
    with pytest.raises(AppError) as error:
        complete_model_todo(todo, inputs, context, **options)
    assert error.value.code == "CALL_LIMIT" and seen == ["L", "H"]
    conn.close()
