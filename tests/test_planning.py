import io
from datetime import UTC, datetime

import pytest
from test_routing import POLICY, binding, todo

from kelly import artifacts, tools, verification
from kelly.contracts import (
    Actor,
    AppError,
    ArtifactInput,
    PlanV1,
    Requirement,
    RunRequest,
    StepInput,
    TaskFacts,
    TaskSpec,
)
from kelly.db import create_run, get_run, init_db
from kelly.engine import execute_next, install_plan
from kelly.planning import normalize_plan
from kelly.routing import expand_recipe, reclassify_pending

SOURCE = {
    "artifact_id": "source1",
    "format": "csv",
    "headers": ["부서", "매출", "수량"],
    "headers_valid": True,
    "selection_required": False,
    "sheet": None,
    "sample_types": {"매출": {"kinds": ["number"], "sampled_rows": 5}},
}


def request(text="부서별 매출 합계 내줘", **changes):
    return RunRequest(request=text, project_id="sales", artifact_ids=["source1"], **changes)


@pytest.mark.parametrize(
    "recipe,roles",
    [
        ("aggregate", []),
        ("weekly_report", ["M"]),
        ("summarize", ["L"]),
        ("meeting_actions", ["L"]),
        ("draft_reply", ["M"]),
        ("search_answer", ["L"]),
    ],
)
def test_six_recipes_expand_without_provider_or_planner(recipe, roles):
    inputs = (
        {"value_columns": ["매출"], "group_by": "부서"}
        if recipe in {"aggregate", "weekly_report"}
        else ({"query": "제안서"} if recipe == "search_answer" else {})
    )
    spec = TaskSpec(intent=recipe, source_ids=["source1"], inputs=inputs, acceptance=["업무 완료"])
    text = {
        "aggregate": "부서별 매출 합계 내줘",
        "weekly_report": "부서별 매출 합계 보고서 만들어줘",
        "summarize": "자료 요약해줘",
        "meeting_actions": "회의록 할 일 추출",
        "draft_reply": "메일 답장 초안 작성해줘",
        "search_answer": "제안서 결정 찾아줘",
    }[recipe]
    plan = expand_recipe(request(text), [SOURCE], spec)
    assert plan is not None and len(plan.todos) <= 8
    assert [t.executor.role for t in plan.todos if t.executor.type == "model"] == roles
    assert all(t.clarity == "clear" for t in plan.todos)
    assert plan.todos[-1].executor.tool_name == "artifact.publish"
    PlanV1.model_validate(plan.model_dump())


@pytest.mark.parametrize(
    "text,expected",
    [
        ("부서별 매출 합계 내줘", "table.aggregate"),
        ("부서별 매출 집계해서 보고서 만들어줘", "table.aggregate"),
        ("이 회의록에서 할 일 뽑아줘", "source.extract"),
        ("이 자료 요약해줘", "source.extract"),
        ("이 메일 답장 초안 작성해줘", "source.extract"),
        ('지난번 "제안서" 결정이 뭐였지?', "source.search"),
    ],
)
def test_known_language_tasks_select_fixed_recipe(text, expected):
    assert expand_recipe(request(text), [SOURCE]).todos[0].executor.tool_name == expected


@pytest.mark.parametrize(
    "text",
    [
        "매출표와 회의록으로 부진 원인을 분석하고 보고서 만들어줘",
        "충돌한 제약을 풀어 대안을 비교해줘",
        "요약하고 답장 초안 작성해줘",
        "평일 9시에 업무 브리핑 보내줘",
        "처음 보는 복합 업무를 실행해줘",
        "내용" * 260 + " 요약해줘",
    ],
)
def test_new_composite_or_long_request_is_not_narrowed_to_a_recipe(text):
    assert expand_recipe(request(text), [SOURCE]) is None


@pytest.mark.parametrize(
    "text",
    [
        "VIP 고객만 대상으로 부서별 매출 합계 내줘",
        "이번 달 부서별 매출 합계 내줘",
        "해외 매출을 제외하고 부서별 매출 합계 내줘",
        "이 자료 요약하고 영어로 번역해줘",
        "이 회의록에서 할 일 뽑고 우선순위 판단해줘",
    ],
)
def test_unparsed_conditions_are_preserved_for_dynamic_planning(text):
    assert expand_recipe(request(text), [SOURCE]) is None


def test_confirmed_acceptance_is_not_dropped_by_recipe():
    spec = TaskSpec(intent="summarize", source_ids=["source1"], acceptance=["세 문장으로 요약"])
    plan = expand_recipe(request("자료 요약해줘"), [SOURCE], spec)
    requirement = next(r for r in plan.requirements if r.text == "세 문장으로 요약")
    producer = next(t for t in plan.todos if t.executor.type == "model")
    assert requirement.id in producer.requirement_ids
    assert requirement.text in producer.done_when


def test_confirmed_recipe_and_corrections_never_remove_composite_requirements():
    spec = TaskSpec(
        intent="weekly_report",
        source_ids=["source1"],
        inputs={"value_columns": ["매출"]},
        acceptance=["매출 하락 원인과 대안 비교"],
    )
    assert (
        expand_recipe(
            request("매출 하락의 원인을 분석하고 대안을 비교해 보고서를 만들어줘"), [SOURCE], spec
        )
        is None
    )
    assert expand_recipe(request("매출 합계 보고서 만들어줘"), [SOURCE], spec) is None
    corrected = TaskSpec(
        intent="aggregate",
        inputs={"value_columns": ["매출"], "user_corrections": "해외 매출 제외"},
        acceptance=["집계"],
    )
    assert expand_recipe(request(), [SOURCE], corrected) is None


def test_type_sample_never_selects_an_unrequested_numeric_column():
    plan = expand_recipe(request("이 매출표를 부서별로 합쳐줘"), [SOURCE])
    assert len(plan.todos) == 1 and plan.todos[0].clarity == "missing_input"
    confirmed = TaskSpec(
        intent="aggregate",
        source_ids=["source1"],
        inputs={"value_columns": ["매출"], "group_by": "부서"},
        acceptance=["집계"],
    )
    repaired = expand_recipe(request("이 표 합쳐줘"), [SOURCE], confirmed)
    assert repaired.todos[0].executor.args["value_columns"] == ["매출"]
    assert repaired.todos[0].executor.args["group_by"] == "부서"


def test_missing_sheet_source_and_search_query_ask_for_input():
    for req, sources in [
        (request(), [{**SOURCE, "selection_required": True}]),
        (request("회의록 할 일 추출"), []),
        (request("지난번 제안서 결정이 뭐였지?"), [SOURCE]),
    ]:
        assert expand_recipe(req, sources).todos[0].clarity == "missing_input"


def test_recipe_cannot_use_an_unattached_source_or_unknown_columns():
    with pytest.raises(AppError) as error:
        expand_recipe(request(), [{**SOURCE, "artifact_id": "foreign"}])
    assert error.value.code == "POLICY_DENIED"
    spec = TaskSpec(intent="aggregate", inputs={"value_columns": ["invented"]}, acceptance=["집계"])
    with pytest.raises(AppError) as error:
        expand_recipe(request(), [SOURCE], spec)
    assert error.value.code == "INVALID_INPUT"
    with pytest.raises(AppError) as error:
        expand_recipe(request("엑셀 수식 재계산해줘"), [SOURCE])
    assert error.value.code == "UNSUPPORTED_CAPABILITY"


def test_reclassification_changes_only_affected_unfinished_suffix():
    first = todo(id="t1", status="succeeded")
    second = todo(
        id="t2", depends_on=["t1"], inputs=[StepInput(step_id="t1", output_key="sections")]
    )
    third = todo(
        id="t3",
        kind="standard",
        depends_on=["t2"],
        inputs=[StepInput(step_id="t2", output_key="sections")],
    )
    independent = todo(id="t4")
    plan = PlanV1(
        requirements=[Requirement(id="r1", text="모든 요청 충족")],
        todos=[first, second, third, independent],
    )
    before = plan.model_dump()
    changed = reclassify_pending(plan, {"t2": TaskFacts(conflicting_sources=True)}, {}, ["t1"])
    assert changed.todos[0].model_dump() == before["todos"][0]
    assert changed.todos[3].model_dump() == before["todos"][3]
    assert changed.todos[1].executor.role == changed.todos[2].executor.role == "H"
    assert plan.model_dump() == before
    assert [t.id for t in changed.todos] == ["t1", "t2", "t3", "t4"]
    assert changed.requirements == plan.requirements


def test_validation_schema_error_does_not_promote_but_missing_input_waits():
    plan = PlanV1(requirements=[Requirement(id="r1", text="결과")], todos=[todo()])
    assert reclassify_pending(plan, {}, {"t1": ["INVALID_OUTPUT"]}).todos[0].executor.role == "L"
    missing = reclassify_pending(plan, {}, {"t1": ["MISSING_INPUT"]})
    assert missing.todos[0].clarity == "missing_input"
    resumed = reclassify_pending(missing, {"t1": TaskFacts()}, {})
    assert resumed.todos[0].clarity == "clear"
    with pytest.raises(AppError):
        reclassify_pending(plan, {"unknown": TaskFacts()}, {})


@pytest.mark.parametrize(
    "input_kind", ["extracted_text", "artifact_id_only", "inspection_only", "missing"]
)
def test_model_source_inputs_require_extracted_content_and_mandatory_proof(tmp_path, input_kind):
    plan = expand_recipe(request("자료 요약해줘"), [SOURCE])
    extract = plan.todos[0]
    model = next(item for item in plan.todos if item.executor.type == "model")
    assert extract.executor.tool_name == "source.extract"
    assert model.inputs == [StepInput(step_id=extract.id, output_key="evidence")]
    model.verify_with = []
    extract.verify_with = []
    if input_kind == "artifact_id_only":
        model.inputs = [ArtifactInput(artifact_id="source1")]
    elif input_kind == "inspection_only":
        extract.executor.tool_name = "source.inspect"
        extract.executor.args = {"artifact_id": "source1"}
        extract.output_schema = "SourceInfoV1"
        model.inputs = [StepInput(step_id=extract.id, output_key="source")]
    elif input_kind == "missing":
        model.inputs = []
    options = {
        "tools": tools.registry(tmp_path),
        "capability_catalog": [binding(capabilities=["structured_output", "summarize"])],
        "privacy_policy": POLICY,
        "permitted_sources": {"source1"},
    }
    if input_kind != "extracted_text":
        with pytest.raises(AppError) as error:
            normalize_plan(plan, **options)
        assert error.value.code == "PLAN_INVALID"
        return
    normalized, selected = normalize_plan(plan, **options)
    actual = next(item for item in normalized.todos if item.id == model.id)
    assert actual.inputs == model.inputs and actual.depends_on == model.depends_on
    assert "model_grounding" in actual.verify_with
    assert "source_grounding" in normalized.todos[0].verify_with
    assert selected[model.id]["role"] == "L"


def test_actual_aggregate_recipe_runs_through_engine_with_zero_model_calls(tmp_path):
    home = tmp_path / "home"
    conn = init_db(home / "state.db")
    actor = Actor(id="owner", project_ids=["sales"])
    try:
        record = artifacts.import_source(
            home,
            conn,
            io.BytesIO("부서,매출\n영업,10.10\n영업,20.20\n".encode()),
            {"filename": "sales.csv"},
            actor,
            "sales",
        )
        req = RunRequest(
            request="부서별 매출 합계 내줘",
            project_id="sales",
            artifact_ids=[record["id"]],
            output_format="xlsx",
        )
        run = create_run(conn, actor, "ui", "recipe-test", req)
        registered = tools.registry(home)
        context = {"home": home, "conn": conn, "actor": actor, "run": run, "inputs": {}}
        inspected = tools.inspect({"artifact_id": record["id"]}, context)["source"]
        plan = expand_recipe(req, [inspected])
        install_plan(conn, run["id"], plan, actor, tools=registered)
        calls = []

        def forbidden(*args):
            calls.append(args)
            raise AssertionError("No model is needed for this recipe")

        for _ in range(len(plan.todos) + 1):
            execute_next(
                conn,
                run["id"],
                actor=actor,
                tools=registered,
                complete=forbidden,
                now=datetime.now(UTC),
                verifiers=verification.registry(home),
            )
        assert get_run(conn, run["id"], actor)["state"] == "succeeded"
        assert calls == []
        assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE kind='result'").fetchone()[0] == 1
    finally:
        conn.close()
