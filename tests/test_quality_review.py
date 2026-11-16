"""SQLite/executor/usage integration; only the external model transport is replaced."""

from functools import partial

import pytest
from test_metered_workflow import NOW, returned
from test_prepare_run import OWNER, POLICY, binding, planned

from kelly.contracts import (
    ModelExecutor,
    PlanV1,
    RunRequest,
    StepInput,
    ToolExecutor,
    ValidationResult,
    digest,
)
from kelly.db import create_run, get_value, init_db, row_dict
from kelly.engine import complete_model_todo, execute_next, install_plan
from kelly.tools import registry
from kelly.verification import (
    model_grounding,
    needs_semantic_review,
    semantic_review_requested,
)

CANDIDATE = {
    "sections": {
        "content": {"paragraphs": [[{"claim_id": "greeting"}]]},
        "claims": [{"id": "greeting", "text": "함께 좋은 하루를 시작해요.", "kind": "inference"}],
    }
}
REVIEW = {"validation": {"status": "pass", "checked_layers": ["semantic_review"]}}


def content_plan(kind="simple", signals=None):
    plan = PlanV1.model_validate(planned()["plan"])
    todo = plan.todos[0]
    todo.kind, todo.signals = kind, signals or ["extraction"]
    todo.executor.role = {"simple": "L", "standard": "M", "complex": "H"}[kind]
    return plan


def setup_run(conn, home, key, plan, *, quality="balanced"):
    run = create_run(
        conn,
        OWNER,
        "ui",
        key,
        RunRequest(
            request="동료에게 건넬 짧은 인사말 작성", project_id="sales", quality_mode=quality
        ),
    )
    return install_plan(conn, run["id"], plan, OWNER, tools=registry(home))


def completion(transport, *, qualified=True):
    catalog = [
        binding().model_copy(
            update={"capabilities": ["structured_output", "analyze", "semantic_review"]}
        )
    ]
    if not qualified:
        catalog[0].roles = ["L", "M"]
    return partial(
        complete_model_todo,
        capability_catalog=catalog,
        privacy_policy=POLICY,
        transport=transport,
    )


def execute(conn, home, run, complete):
    return execute_next(
        conn, run["id"], actor=OWNER, tools=registry(home), complete=complete, now=NOW, home=home
    )


@pytest.mark.parametrize(
    "quality,kind,signals,role",
    [
        ("high", "simple", ["extraction"], "L"),
        ("high", "standard", ["summarization"], "M"),
        ("balanced", "complex", ["causal_judgment"], "H"),
        ("balanced", "standard", ["conflicting_sources"], "M"),
    ],
)
def test_required_review_keeps_generation_classification_and_records_exact_candidate(
    tmp_path, quality, kind, signals, role
):
    conn = init_db(tmp_path / "state.db")
    plan = content_plan(kind, signals)
    before = plan.todos[0].model_dump()
    run = setup_run(conn, tmp_path, "review", plan, quality=quality)
    seen = []

    def transport(request, selected):
        seen.append(selected.role)
        if len(seen) == 1:
            return returned(CANDIDATE)
        assert "candidate_output" in request.context and "review_task" in request.context
        assert "동료에게 건넬" in request.context and "대안을 비교" in request.context
        return returned(REVIEW)

    finished = execute(conn, tmp_path, run, completion(transport))
    assert finished["state"] == "succeeded" and seen == [role, "H"]
    assert finished["plan_json"]["current"]["todos"][0] == before
    calls = [row_dict(row) for row in conn.execute("SELECT * FROM calls ORDER BY logical_call_no")]
    assert [call["purpose"] for call in calls] == ["generate", "review"]
    assert calls[1]["route_json"]["review_output_hash"] == digest(CANDIDATE)
    assert sum(call["input_tokens_total"] for call in calls) == 20
    step = row_dict(conn.execute("SELECT * FROM steps").fetchone())
    assert "semantic_review" in step["validation_json"]["checked_layers"]
    assert not conn.execute("SELECT 1 FROM kv WHERE key LIKE 'cache:response_%'").fetchone()
    conn.close()


def test_high_does_not_reuse_balanced_response_cache(tmp_path):
    conn = init_db(tmp_path / "state.db")
    plan, seen = content_plan(), []

    def transport(request, selected):
        seen.append(selected.role)
        return returned(REVIEW if "candidate_output" in request.context else CANDIDATE)

    complete = completion(transport)
    for key, quality in [("cold", "balanced"), ("review", "high"), ("warm", "balanced")]:
        run = setup_run(conn, tmp_path, key, plan, quality=quality)
        assert execute(conn, tmp_path, run, complete)["state"] == "succeeded"
        hit = get_value(conn, "cache_hit:" + run["id"] + ":t1")
        assert bool(hit) == (key == "warm")
        observed = get_value(conn, "cache_observation:" + run["id"])
        assert observed == (
            None if quality == "high" else {"lookups": 1, "hits": int(key == "warm")}
        )
    assert seen == ["L", "L", "H"]
    assert (
        conn.execute("SELECT count(*) FROM kv WHERE key LIKE 'cache:response_%'").fetchone()[0] == 1
    )
    conn.close()


@pytest.mark.parametrize(
    "verdict,qualified,error,expected_calls",
    [
        (
            {"status": "fail", "checked_layers": ["semantic_review"]},
            True,
            "SEMANTIC_REVIEW_FAILED",
            2,
        ),
        (
            {"status": "needs_input", "checked_layers": ["semantic_review"]},
            True,
            "MISSING_INPUT",
            2,
        ),
        ({"status": "pass", "checked_layers": ["visual_verified"]}, True, "INVALID_OUTPUT", 2),
        (
            {"status": "pass", "checked_layers": ["semantic_review"], "errors": ["unsupported"]},
            True,
            "SEMANTIC_REVIEW_FAILED",
            2,
        ),
        (REVIEW["validation"], False, "NO_CAPABLE_MODEL", 1),
    ],
)
def test_required_review_fails_closed(tmp_path, verdict, qualified, error, expected_calls):
    conn = init_db(tmp_path / "state.db")
    run = setup_run(conn, tmp_path, "review", content_plan(), quality="high")
    seen = []

    def transport(request, selected):
        seen.append(selected.role)
        return returned(CANDIDATE if len(seen) == 1 else {"validation": verdict})

    finished = execute(conn, tmp_path, run, completion(transport, qualified=qualified))
    assert finished["state"] != "succeeded" and len(seen) == expected_calls
    step = row_dict(conn.execute("SELECT * FROM steps").fetchone())
    if error == "INVALID_OUTPUT":
        assert error in step["validation_json"]["errors"]
    else:
        assert finished["error_code"] == error
    assert step["state"] != "succeeded"
    conn.close()


def test_format_repair_does_not_open_a_third_review_call(tmp_path):
    conn = init_db(tmp_path / "state.db")
    run = setup_run(conn, tmp_path, "review", content_plan(), quality="high")
    seen = []

    def transport(request, selected):
        seen.append(selected.role)
        return returned({"sections": "bad"} if len(seen) == 1 else CANDIDATE)

    finished = execute(conn, tmp_path, run, completion(transport))
    assert finished["state"] == "failed" and finished["error_code"] == "CALL_LIMIT"
    assert seen == ["L", "L"]
    conn.close()


@pytest.mark.parametrize(
    "candidate,error",
    [
        (
            {
                "sections": {
                    "content": {"paragraphs": [[{"claim_id": "count"}]]},
                    "claims": [{"id": "count", "text": "999건 완료", "kind": "inference"}],
                }
            },
            "NUMERIC_MISMATCH",
        ),
        (
            {
                "sections": {
                    "content": {"paragraphs": [[{"claim_id": "fact"}]]},
                    "claims": [{"id": "fact", "text": "확정 사실"}],
                }
            },
            "MISSING_EVIDENCE",
        ),
    ],
)
def test_review_cannot_waive_deterministic_failures(tmp_path, candidate, error):
    conn = init_db(tmp_path / "state.db")
    plan = content_plan()
    run = setup_run(conn, tmp_path, "review", plan, quality="high")
    context = {"run": run, "todo": plan.todos[0], "inputs": {}}
    assert not needs_semantic_review(candidate, context)
    proof = (digest(candidate), ValidationResult.model_validate(REVIEW["validation"]))
    assert model_grounding(candidate, context, semantic_review=proof).errors == [error]
    seen = []

    def transport(request, selected):
        seen.append(selected.role)
        return returned(candidate)

    complete = completion(transport)
    execute(conn, tmp_path, run, complete)
    finished = execute(conn, tmp_path, run, complete)
    assert finished["state"] == "failed" and seen == ["L", "L"]
    conn.close()


def test_model_grounding_requires_bound_review_independently_of_dispatch():
    todo = content_plan().todos[0]
    context = {"todo": todo, "inputs": {}, "run": {"request_json": {"quality_mode": "high"}}}
    assert model_grounding(CANDIDATE, context).errors == ["SEMANTIC_REVIEW_REQUIRED"]
    review = ValidationResult.model_validate(REVIEW["validation"])
    assert model_grounding(CANDIDATE, context, semantic_review=("0" * 64, review)).errors == [
        "INVALID_OUTPUT"
    ]
    assert (
        model_grounding(CANDIDATE, context, semantic_review=(digest(CANDIDATE), review)).status
        == "pass"
    )
    context["inputs"]["semantic_review"] = {
        "output_hash": digest(CANDIDATE),
        "validation": review.model_dump(),
    }
    assert model_grounding(CANDIDATE, context).errors == ["SEMANTIC_REVIEW_REQUIRED"]


def test_h_review_cannot_certify_an_unexecuted_numeric_aggregate():
    candidate = {
        "sections": {
            "content": {"paragraphs": [[{"numeric_id": "sum"}]]},
            "numeric_claims": [
                {
                    "id": "sum",
                    "value": "999",
                    "aggregate_step": "forged",
                    "column": "매출",
                    "unit": "KRW",
                }
            ],
        }
    }
    context = {
        "todo": content_plan().todos[0],
        "inputs": {},
        "run": {"request_json": {"quality_mode": "high"}},
    }
    proof = (digest(candidate), ValidationResult.model_validate(REVIEW["validation"]))
    assert model_grounding(candidate, context, semantic_review=proof).errors == [
        "NUMERIC_REFERENCE_INVALID"
    ]


def test_high_deterministic_tool_and_review_task_do_not_request_another_review(tmp_path):
    conn = init_db(tmp_path / "state.db")
    plan = content_plan()
    todo = plan.todos[0]
    todo.kind, todo.signals = "mechanical", ["fixed_transform"]
    todo.executor = ToolExecutor(tool_name="memory.search", args={"query": "없는 기억"})
    todo.output_schema = "MemoryListV1"
    run = setup_run(conn, tmp_path, "search", plan, quality="high")

    def forbidden_transport(*_):
        pytest.fail("A deterministic lookup cannot call a model")

    assert execute(conn, tmp_path, run, completion(forbidden_transport))["state"] == "succeeded"
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    context = {"run": run, "todo": todo, "inputs": {}}
    assert not semantic_review_requested(context)
    todo.kind, todo.signals = "complex", ["conflicting_sources"]
    todo.executor = ModelExecutor(role="H", task="semantic_review")
    todo.output_schema = "ValidationResultV1"
    assert not semantic_review_requested(context) and not needs_semantic_review(REVIEW, context)
    source_todo = PlanV1.model_validate(run["plan_json"]["current"]).todos[0]
    review_todo = todo.model_copy(
        update={
            "id": "review",
            "depends_on": [source_todo.id],
            "inputs": [StepInput(step_id=source_todo.id, output_key="memories")],
        }
    )
    review_plan = PlanV1(requirements=plan.requirements, todos=[source_todo, review_todo])
    review_run = setup_run(conn, tmp_path, "standalone-review", review_plan, quality="high")
    calls = []

    def review_transport(request, selected):
        calls.append(selected.role)
        return returned(REVIEW)

    complete = completion(review_transport)
    execute(conn, tmp_path, review_run, complete)
    assert execute(conn, tmp_path, review_run, complete)["state"] == "succeeded"
    assert calls == ["H"] and conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 1
    conn.close()
