"""Real SQLite/planning/executor/verifier boundaries with finite offline model replies."""

from datetime import UTC, datetime
from functools import partial

import pytest

from birkin_lite import db, engine, usage
from birkin_lite.context import build_model_request
from birkin_lite.contracts import (
    AppError,
    ModelBinding,
    ModelResult,
    PlanV1,
    PrivacyPolicy,
    RunRequest,
    Usage,
)
from birkin_lite.providers import TransportError
from birkin_lite.routing import choose_evaluation_model, choose_model
from birkin_lite.tools import registry
from evals.comparison import FixtureTransport, compare_fixture
from evals.execute import ACTOR
from evals.run import Case, load_cases

POLICY = PrivacyPolicy(project_id="eval", allowed_providers=["fixture"], local_only=True)
CONTENT = {
    "sections": {
        "content": {"paragraphs": [[{"claim_id": "greeting"}]]},
        "claims": [
            {
                "id": "greeting",
                "text": "함께 좋은 하루를 시작해요.",
                "kind": "inference",
                "evidence_ids": [],
            }
        ],
    }
}
REVIEW = {"validation": {"status": "pass", "checked_layers": ["semantic_review"]}}
NOW = datetime(2026, 9, 8, tzinfo=UTC)


def catalog():
    return [
        ModelBinding(
            provider="fixture",
            model_id="fixture-" + role,
            roles=[role],
            enabled=True,
            available=True,
            allowed_projects=["eval"],
            capabilities=["structured_output", "planning", "analyze", "semantic_review"],
            quality_evidence_id="synthetic-qualification",
            context_window=32768,
            max_output=2048,
            metering_mode="local",
        )
        for role in ("L", "M", "H")
    ]


def case_and_plan(kind="standard"):
    values = load_cases()[40].model_dump()
    values.update(id="comparison-boundary-fixture", request="동료에게 짧은 인사말 작성", sources=[])
    values["gold"].update(
        numbers={},
        units={},
        numeric_locators={},
        required_assertions=[
            {"id": "greeting", "text": "짧은 인사말", "locators": [], "expected_kind": "standard"}
        ],
    )
    case = Case.model_validate(values)
    plan = {
        "task_spec": {"intent": "summarize", "source_ids": [], "acceptance": [case.request]},
        "plan": {
            "requirements": [{"id": "r1", "text": case.request}],
            "todos": [
                {
                    "id": "t1",
                    "title": "인사말 작성",
                    "requirement_ids": ["r1"],
                    "kind": kind,
                    "reason": "요청에 맞는 짧은 문장을 작성한다",
                    "signals": ["summarization"] if kind == "standard" else ["causal_judgment"],
                    "clarity": "clear",
                    "usefulness": "required",
                    "executor": {
                        "type": "model",
                        "role": "M" if kind == "standard" else "H",
                        "task": "analyze",
                    },
                    "output_schema": "ReportSectionsV1",
                    "done_when": [case.request],
                }
            ],
        },
    }
    return case, plan


def reply(output, *, known=True):
    return ModelResult(
        parsed_output=output,
        finish_reason="stop",
        latency_ms=1,
        normalized_usage=Usage(
            input_tokens_total=10, output_tokens_total=5, usage_basis="estimated"
        )
        if known
        else Usage(),
    )


def test_actual_pipeline_keeps_classification_and_records_actual_baseline_roles(tmp_path):
    case, plan = case_and_plan()
    before = plan["plan"]["todos"][0].copy()
    scripts = {
        "A": [reply(plan), reply(CONTENT)],
        "B": [reply(plan), reply({"sections": []}), reply(CONTENT)],
        "C": [reply(plan), reply(CONTENT, known=False)],
    }
    ledgers = compare_fixture(
        case, tmp_path / "comparison", scripts=scripts, catalog=catalog(), privacy=POLICY
    )
    assert [r["run"]["state"] for r in ledgers] == ["succeeded"] * 3
    assert [[r["route_json"]["role"] for r in item["calls"]] for item in ledgers] == [
        ["M", "H"],
        ["M", "L", "M"],
        ["M", "M"],
    ]
    assert len({item["environment_hash"] for item in ledgers}) == 1
    for item in ledgers:
        todo = item["steps"][0]["spec_json"]["todo"]
        assert todo["kind"] == before["kind"] and todo["executor"] == before["executor"]
        assert item["steps"][0]["validation_json"]["status"] == "pass"
        assert (
            item["actual_provider_tokens"]
            is item["model_quality"]
            is item["token_reduction"]
            is None
        )
        assert item["evidence_layer"] == "offline_fixture" and item["live"] is False
    baseline = ledgers[1]["calls"]
    assert [r["route_json"]["evaluation_attempt"] for r in baseline[1:]] == [1, 2]
    assert all(r["route_json"]["classified_role"] == "M" for r in baseline[1:])
    assert ledgers[2]["calls"][-1]["input_tokens_total"] is None
    assert ledgers[2]["calls"][-1]["actual_micro_usd"] is None


@pytest.mark.parametrize("fail_first", [False, True])
def test_required_h_review_shares_the_two_call_ceiling(tmp_path, fail_first):
    case, plan = case_and_plan("complex")
    scripts = {arm: [reply(plan), reply(CONTENT), reply(REVIEW)] for arm in ("A", "B", "C")}
    if fail_first:
        scripts["B"] = [reply(plan), reply({"sections": []}), reply(CONTENT), reply(REVIEW)]
    ledgers = compare_fixture(
        case, tmp_path / "comparison", scripts=scripts, catalog=catalog(), privacy=POLICY
    )
    middle = ledgers[1]
    assert [r["route_json"]["role"] for r in middle["calls"]] == (
        ["M", "L", "M"] if fail_first else ["M", "L", "H"]
    )
    assert middle["run"]["state"] == ("failed" if fail_first else "succeeded")
    if fail_first:
        assert middle["run"]["error_code"] == "CALL_LIMIT"
    else:
        assert middle["calls"][-1]["purpose"] == "review"
        assert "review_output_hash" in middle["calls"][-1]["route_json"]
    assert all(len([r for r in item["calls"] if r["step_id"]]) == 2 for item in ledgers)


def prepared(tmp_path):
    case, planned = case_and_plan()
    conn = db.init_db(tmp_path / "state.db")
    run = db.create_run(
        conn, ACTOR, "ui", "fixture", RunRequest(request=case.request, project_id="eval")
    )
    run = engine.install_plan(
        conn, run["id"], PlanV1.model_validate(planned["plan"]), ACTOR, tools=registry(tmp_path)
    )
    db.set_value(
        conn,
        "evaluation:" + run["id"],
        {
            "actor_id": ACTOR.id,
            "project_id": "eval",
            "arm": "B",
            "privacy": POLICY.model_dump(),
            "catalog": [b.model_dump() for b in catalog()],
        },
    )
    todo = PlanV1.model_validate(run["plan_json"]["current"]).todos[0]
    return conn, run, todo


def selection(conn, run, todo, attempt=1):
    return choose_evaluation_model(
        todo,
        catalog(),
        POLICY,
        arm="B",
        attempt=attempt,
        run=run,
        actor=ACTOR,
        snapshot=db.get_value(conn, "evaluation:" + run["id"]),
    )


def test_default_request_and_usage_reject_a_lower_role_without_explicit_sealed_selection(tmp_path):
    conn, run, todo = prepared(tmp_path)
    try:
        selected, route = selection(conn, run, todo)
        context = {"conn": conn, "run": run, "actor": ACTOR, "todo": todo}
        with pytest.raises(AppError) as caught:
            build_model_request(todo, {}, context, binding=selected)
        assert caught.value.code == "NO_CAPABLE_MODEL"
        request = build_model_request(todo, {}, context, binding=selected, evaluation=route)
        with pytest.raises(AppError) as caught:
            usage.begin_call(
                conn,
                run["id"],
                todo.id,
                selected,
                request,
                actor=ACTOR,
                purpose="generate",
                now=NOW,
            )
        assert caught.value.code == "NO_CAPABLE_MODEL"
        assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
        normal = choose_model(todo, catalog(), POLICY)
        normal_request = build_model_request(todo, {}, context, binding=normal)
        with pytest.raises(AppError) as caught:
            usage.begin_call(
                conn,
                run["id"],
                todo.id,
                normal,
                normal_request,
                actor=ACTOR,
                purpose="generate",
                now=NOW,
            )
        assert caught.value.code == "POLICY_DENIED"
    finally:
        conn.close()


@pytest.mark.parametrize("changed", ["actor", "project", "todo", "privacy", "catalog", "snapshot"])
def test_selection_cannot_widen_or_change_the_sealed_execution_boundary(tmp_path, changed):
    conn, run, todo = prepared(tmp_path)
    try:
        selected, route = selection(conn, run, todo)
        if changed == "actor":
            route = route.model_copy(update={"actor_id": "other"})
        elif changed == "project":
            route = route.model_copy(update={"project_id": "other"})
        elif changed == "todo":
            todo = todo.model_copy(update={"done_when": ["추가 요청"]})
        elif changed == "privacy":
            route = route.model_copy(
                update={"privacy": POLICY.model_copy(update={"allow_cloud": True})}
            )
        elif changed == "catalog":
            snapshot = db.get_value(conn, "evaluation:" + run["id"])
            snapshot["catalog"] = []
            db.set_value(conn, "evaluation:" + run["id"], snapshot)
        else:
            conn.execute("DELETE FROM kv WHERE key=?", ("evaluation:" + run["id"],))
        with pytest.raises(AppError):
            build_model_request(
                todo,
                {},
                {"conn": conn, "run": run, "actor": ACTOR},
                binding=selected,
                evaluation=route,
            )
        assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    finally:
        conn.close()


def test_b_second_generation_without_actual_failed_check_is_denied(tmp_path):
    conn, run, todo = prepared(tmp_path)
    try:
        selected, route = selection(conn, run, todo, 2)
        context = {"conn": conn, "run": run, "actor": ACTOR, "todo": todo}
        request = build_model_request(todo, {}, context, binding=selected, evaluation=route)
        with pytest.raises(AppError) as caught:
            usage.begin_call(
                conn,
                run["id"],
                todo.id,
                selected,
                request,
                actor=ACTOR,
                purpose="repair",
                now=NOW,
                evaluation=route,
            )
        assert caught.value.code == "CALL_LIMIT"
        first_binding, first_route = selection(conn, run, todo)
        first_request = build_model_request(
            todo, {}, context, binding=first_binding, evaluation=first_route
        )
        call = usage.begin_call(
            conn,
            run["id"],
            todo.id,
            first_binding,
            first_request,
            actor=ACTOR,
            purpose="generate",
            now=NOW,
            evaluation=first_route,
        )
        usage.start_call(conn, call["id"], ACTOR)
        usage.settle_call(
            conn, call["id"], Usage(), None, actor=ACTOR, now=NOW, finish_reason="stop"
        )
        with pytest.raises(AppError) as caught:
            usage.begin_call(
                conn,
                run["id"],
                todo.id,
                selected,
                request,
                actor=ACTOR,
                purpose="repair",
                now=NOW,
                evaluation=route,
            )
        assert caught.value.code == "PLAN_INVALID"
    finally:
        conn.close()


def test_warm_replays_use_actual_response_cache_and_keep_cold_usage(tmp_path):
    case, plan = case_and_plan()
    directory = tmp_path / "comparison"
    cold = compare_fixture(
        case,
        directory,
        scripts={a: [reply(plan), reply(CONTENT)] for a in ("A", "B", "C")},
        catalog=catalog(),
        privacy=POLICY,
    )
    warm = compare_fixture(
        case,
        directory,
        cache="warm",
        scripts={a: [reply(plan)] for a in ("A", "B", "C")},
        catalog=catalog(),
        privacy=POLICY,
    )
    assert all(item["run"]["state"] == "succeeded" for item in warm)
    assert all(len(item["calls"]) == 1 and item["calls"][0]["purpose"] == "plan" for item in warm)
    assert all(len(item["calls"]) == 2 for item in cold)
    for arm in ("A", "B", "C"):
        conn = db.init_db(directory / arm / "state.db")
        assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 3
        conn.close()


def test_fixture_transport_rejects_live_usage_and_external_callables():
    with pytest.raises(ValueError, match="actual provider"):
        FixtureTransport(
            [reply(CONTENT).model_copy(update={"normalized_usage": Usage(usage_basis="provider")})]
        )
    with pytest.raises(TypeError, match="ModelResult"):
        FixtureTransport([partial(choose_model)])


@pytest.mark.parametrize("retryable", [False, True])
def test_transport_failure_cannot_advance_b_cascade(tmp_path, retryable):
    conn, run, _todo = prepared(tmp_path)
    seen = []

    def transport(_request, binding):
        seen.append(binding.role)
        if len(seen) == 1:
            raise TransportError(
                "PROVIDER_RATE_LIMITED" if retryable else "PROVIDER_ERROR",
                retryable=retryable,
                status=429 if retryable else 400,
                retry_after_s=0,
            )
        return reply(CONTENT)

    try:
        finished = engine.execute_next(
            conn,
            run["id"],
            actor=ACTOR,
            tools=registry(tmp_path),
            now=NOW,
            home=tmp_path,
            complete=partial(
                engine.complete_model_todo,
                capability_catalog=catalog(),
                privacy_policy=POLICY,
                transport=transport,
                evaluation_arm="B",
            ),
        )
        assert finished["state"] == ("succeeded" if retryable else "failed")
        assert seen == (["L", "L"] if retryable else ["L"])
        calls = [db.row_dict(row) for row in conn.execute("SELECT * FROM calls")]
        assert {row["logical_call_no"] for row in calls} == {1}
        assert all(row["route_json"]["evaluation_attempt"] == 1 for row in calls)
    finally:
        conn.close()


def test_source_escape_is_rejected_before_any_workspace_or_outside_write(tmp_path):
    case = load_cases()[0].model_copy(deep=True)
    outside = tmp_path / "keep.txt"
    outside.write_text("unchanged", "utf-8")
    case.sources[0].filename = str(outside)
    directory = tmp_path / "comparison"
    with pytest.raises(ValueError, match="basenames"):
        compare_fixture(
            case,
            directory,
            scripts={a: [] for a in ("A", "B", "C")},
            catalog=catalog(),
            privacy=POLICY,
        )
    assert outside.read_text("utf-8") == "unchanged" and not directory.exists()


def test_all_three_arms_use_the_same_real_csv_tools_and_reopened_xlsx(tmp_path):
    case = next(case for case in load_cases() if case.id == "table-01")
    ledgers = compare_fixture(
        case,
        tmp_path / "files-comparison",
        scripts={a: [] for a in ("A", "B", "C")},
        catalog=catalog(),
        privacy=POLICY,
    )
    for item in ledgers:
        assert item["run"]["state"] == "succeeded"
        assert item["calls"] == [] and item["fixture_replies_used"] == 0
        assert len(item["steps"]) == 4 and all(step["kind"] == "tool" for step in item["steps"])
        assert "35000" in item["published_text"]
        assert len(item["published_receipts"]) == 1 and item["published_receipts"][0]["reopened"]
        assert all(source["unchanged"] for source in item["sources"])
    assert len({item["sources"][0]["sha256"] for item in ledgers}) == 1
