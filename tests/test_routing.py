import inspect
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from birkin_lite.contracts import (
    AppError,
    ModelBinding,
    ModelExecutor,
    PrivacyPolicy,
    RunRequest,
    TaskFacts,
    TaskSpec,
    TodoV1,
    ToolExecutor,
)
from birkin_lite.routing import choose_model, expand_recipe, normalize_todo


def todo(**changes):
    return TodoV1.model_validate(
        {
            "id": "t1",
            "title": "근거 처리",
            "requirement_ids": ["r1"],
            "kind": "simple",
            "reason": "명시된 사실 추출",
            "signals": ["extraction"],
            "clarity": "clear",
            "usefulness": "required",
            "executor": {"type": "model", "role": "L", "task": "analyze"},
            "output_schema": "ReportSectionsV1",
            "done_when": ["근거와 결과 일치"],
            **changes,
        }
    )


def binding(**changes):
    return ModelBinding.model_validate(
        {
            "provider": "provider1",
            "model_id": "pinned-model",
            "roles": ["L", "M", "H"],
            "enabled": True,
            "available": True,
            "allowed_projects": ["sales"],
            "capabilities": ["structured_output", "analyze"],
            "context_window": 32000,
            "max_output": 2000,
            "quality_evidence_id": "evaluation1",
            "metering_mode": "api",
            "reasoning_efforts": ["minimal", "medium", "high"],
            **changes,
        }
    )


POLICY = PrivacyPolicy(project_id="sales", allowed_providers=["provider1"], allow_cloud=True)
CASES = json.loads((Path(__file__).parent / "fixtures/routing-cases.json").read_text("utf-8"))


@pytest.mark.parametrize(
    "output_format,phrase",
    [
        ("xlsx", "XLSX로 만들어 주세요"),
        ("csv", "csv 파일로 작성해 주세요"),
        ("docx", "DOCX로 만들어 줘"),
        ("md", "MD로 만들어 주세요"),
        ("text", "TXT로 만들어 주세요"),
    ],
)
def test_exact_confirmed_output_phrase_preserves_deterministic_recipe(output_format, phrase):
    request = RunRequest(
        request="금액 열 합계를 " + phrase + ".",
        project_id="sales",
        artifact_ids=["source-1"],
        output_format=output_format,
    )
    plan = expand_recipe(
        request,
        [
            {
                "artifact_id": "source-1",
                "format": "csv",
                "headers_valid": True,
                "headers": ["항목", "금액"],
                "sheet": None,
            }
        ],
    )
    assert plan is not None
    assert all(item.kind == "mechanical" for item in plan.todos)
    assert plan.todos[0].executor.args["value_columns"] == ["금액"]
    assert plan.requirements[0].text == request.request


@pytest.mark.parametrize(
    "request_text",
    [
        "금액 열 합계를 CSV로 만들어 주세요.",
        "금액 열 합계를 XLSX로 만들어 주세요. 환불 행은 제외해 주세요.",
        "금액 열 합계를 XLSX로 만들어 주세요. 이름을 익명화해 주세요.",
        "금액 열 합계를 XLSX로 만들어 주세요. 고객에게 전송해 주세요.",
        "금액 열 합계를 PDF로 만들어 주세요.",
        "금액 열 합계를 CSV와 XLSX로 만들어 주세요.",
    ],
)
def test_format_phrase_does_not_erase_mismatch_or_extra_requirements(request_text):
    assert (
        expand_recipe(
            RunRequest(
                request=request_text,
                project_id="sales",
                artifact_ids=["source-1"],
                output_format="xlsx",
            ),
            [
                {
                    "artifact_id": "source-1",
                    "format": "csv",
                    "headers_valid": True,
                    "headers": ["항목", "금액"],
                    "sheet": None,
                }
            ],
        )
        is None
    )


@pytest.mark.parametrize(
    "text,inputs,format",
    [
        ("판매액 열 합계를 XLSX로 만들어 주세요.", {"value_columns": ["판매액"]}, "csv"),
        ("따옴표로 감싼 천 단위 판매액을 합계 내 주세요.", {"value_columns": ["판매액"]}, "csv"),
        ("판매액 열의 음수를 포함해 합계 내 주세요.", {"value_columns": ["판매액"]}, "csv"),
        (
            "무게 열을 소수 셋째 자리까지 합계 내 주세요.",
            {"value_columns": ["무게"], "policy": {"decimal_places": 3}},
            "csv",
        ),
        (
            "팀별 판매액 합계를 작성해 주세요.",
            {"value_columns": ["판매액"], "group_by": "팀"},
            "csv",
        ),
        (
            "빈 판매액 셀은 제외하고 합계 내 주세요.",
            {"value_columns": ["판매액"], "policy": {"blank_policy": "skip"}},
            "csv",
        ),
        (
            "판매액의 문자 행을 제외하고 합계 내 주세요.",
            {"value_columns": ["판매액"], "policy": {"mixed_policy": "skip"}},
            "csv",
        ),
        (
            "판매액에 문자가 있으면 합계하지 말고 입력 보완을 요청해 주세요.",
            {"value_columns": ["판매액"], "policy": {"mixed_policy": "error"}},
            "csv",
        ),
        (
            "확정 시트의 판매액만 합계 내 주세요.",
            {"value_columns": ["판매액"], "sheet": "확정"},
            "xlsx",
        ),
        (
            "A1:B5 범위의 판매액만 합계 내 주세요.",
            {"value_columns": ["판매액"], "cell_range": "A1:B5"},
            "csv",
        ),
        (
            "숨긴 행도 포함해서 판매액을 합계 내 주세요.",
            {"value_columns": ["판매액"], "policy": {"include_hidden_rows": True}},
            "xlsx",
        ),
        (
            "숨긴 행을 제외해서 판매액을 합계 내 주세요.",
            {"value_columns": ["판매액"], "policy": {"include_hidden_rows": False}},
            "xlsx",
        ),
        (
            "합계 뒤 HALF_EVEN으로 소수 둘째 자리까지 표시해 주세요.",
            {"value_columns": ["판매액"], "policy": {"rounding": "ROUND_HALF_EVEN"}},
            "csv",
        ),
        (
            "완료 수와 전체 수를 합산하고 전체가 0이면 비율을 미정으로 남겨 주세요.",
            {
                "value_columns": ["완료", "전체"],
                "policy": {"ratio": {"numerator": "완료", "denominator": "전체"}},
            },
            "csv",
        ),
        (
            "수식 결과가 확정되지 않으면 합계를 중단해 주세요.",
            {"value_columns": ["판매액"]},
            "xlsx",
        ),
    ],
)
def test_confirmed_aggregate_policies_restate_only_the_actual_tool_contract(text, inputs, format):
    source = {
        "artifact_id": "source-1",
        "format": format,
        "headers_valid": True,
        "headers": ["팀", "판매액", "무게", "완료", "전체"],
        "sheet": inputs.get("sheet"),
    }
    request = RunRequest(
        request=text, project_id="sales", artifact_ids=["source-1"], output_format="xlsx"
    )
    spec = TaskSpec(
        intent="aggregate",
        source_ids=["source-1"],
        inputs={"artifact_id": "source-1", **inputs},
        acceptance=[text],
    )
    before = spec.model_dump()
    plan = expand_recipe(request, [source], spec)
    assert plan is not None
    assert all(item.kind == "mechanical" for item in plan.todos)
    assert plan.todos[0].executor.tool_name == "table.aggregate"
    args = plan.todos[0].executor.args
    assert args["value_columns"] == inputs["value_columns"]
    assert args["sheet"] == inputs.get("sheet")
    assert args["cell_range"] == inputs.get("cell_range")
    assert all(args["policy"][key] == value for key, value in inputs.get("policy", {}).items())
    assert any(requirement.text == text for requirement in plan.requirements)
    assert "r1" in plan.todos[0].requirement_ids
    assert spec.model_dump() == before and request.request == text
    if "천 단위" in text:
        assert args["policy"]["thousands_separator"] == ","


@pytest.mark.parametrize(
    "text,changes,acceptance",
    [
        ("빈 판매액 셀은 제외하고 합계 내 주세요.", {"policy": {"blank_policy": "error"}}, None),
        (
            "판매액의 문자 행을 제외하고 합계 내 주세요.",
            {"policy": {"mixed_policy": "error"}},
            None,
        ),
        (
            "숨긴 행을 제외해서 판매액을 합계 내 주세요.",
            {"policy": {"include_hidden_rows": True}},
            None,
        ),
        ("판매액 열을 소수 셋째 자리까지 합계 내 주세요.", {"policy": {"decimal_places": 2}}, None),
        (
            "합계 뒤 HALF_EVEN으로 소수 둘째 자리까지 표시해 주세요.",
            {"policy": {"rounding": "ROUND_HALF_UP"}},
            None,
        ),
        ("A1:B6 범위의 판매액만 합계 내 주세요.", {"cell_range": "A1:B5"}, None),
        ("확정 시트의 판매액만 합계 내 주세요.", {"sheet": "초안"}, None),
        ("비용 합계를 작성해 주세요.", {}, None),
        ("판매액 합계를 작성해 주세요. 해외 판매 행을 제외해 주세요.", {}, None),
        ("판매액 합계를 작성해 주세요. 고객에게 전송해 주세요.", {}, None),
        ("판매액 합계를 작성해 주세요.", {}, "이름을 익명화해 주세요"),
        (
            "따옴표로 감싼 천 단위 판매액을 합계 내 주세요.",
            {"policy": {"thousands_separator": None}},
            None,
        ),
    ],
)
def test_confirmed_arguments_never_erase_conflicting_or_unsupported_conditions(
    text, changes, acceptance
):
    source = {
        "artifact_id": "source-1",
        "format": "csv",
        "headers_valid": True,
        "headers": ["판매액", "비용"],
        "sheet": changes.get("sheet"),
    }
    spec = TaskSpec(
        intent="aggregate",
        source_ids=["source-1"],
        inputs={"artifact_id": "source-1", "value_columns": ["판매액"], **changes},
        acceptance=[text, *([acceptance] if acceptance else [])],
    )
    assert (
        expand_recipe(
            RunRequest(
                request=text, project_id="sales", artifact_ids=["source-1"], output_format="xlsx"
            ),
            [source],
            spec,
        )
        is None
    )


@pytest.mark.parametrize("currencies", ["원과 달러", "EUR와 JPY", "위안과 파운드"])
def test_requested_cross_currency_total_requires_input_without_any_tool(currencies):
    text = currencies + " 금액을 환산해서 통합 합계 내 주세요."
    request = RunRequest(request=text, project_id="sales", artifact_ids=["source-1"])
    source = {
        "artifact_id": "source-1",
        "format": "csv",
        "headers_valid": True,
        "headers": ["금액", "통화"],
        "sheet": None,
    }
    spec = TaskSpec(
        intent="aggregate",
        source_ids=["source-1"],
        inputs={"artifact_id": "source-1", "value_columns": ["금액"], "policy": {"unit": "원"}},
        acceptance=[text],
    )
    plan = expand_recipe(request, [source], spec)
    assert plan is not None and len(plan.todos) == 1
    assert plan.todos[0].clarity == "missing_input"
    assert plan.todos[0].executor.type == "model"
    assert choose_model(plan.todos[0], [], PrivacyPolicy(project_id="sales")) is None
    assert plan.requirements[0].text == text


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["id"])
def test_spec_routing_cases_use_actual_normalization_and_binding(case):
    data = {**CASES["defaults"], **case["input"]}
    facts = TaskFacts(**{k: v for k, v in data.items() if k in TaskFacts.model_fields})
    candidate = todo(**{k: v for k, v in data.items() if k in TodoV1.model_fields})
    if facts.certified_deterministic:
        candidate.executor = ToolExecutor(tool_name="table.aggregate", args={})
    try:
        normalized = normalize_todo(candidate, facts)
        selected = choose_model(normalized, [binding()], POLICY)
        if normalized.status == "skipped":
            actual = {"action": "skip_optional", "role": None}
        elif normalized.clarity == "missing_input":
            actual = {"action": "awaiting_input", "role": None}
        else:
            actual = {"action": "execute", "role": selected.role if selected else "D"}
    except AppError as error:
        actual = {"action": error.code, "role": None}
    assert actual == case["expected"]


def test_capability_order_and_privacy_have_no_price_or_credit_input():
    normalized = normalize_todo(todo(kind="standard"), TaskFacts())
    candidates = [binding(model_id="preferred"), binding(model_id="backup")]
    snapshots = [
        {"prices": {"preferred": 100000, "backup": 1}, "credit": 0},
        {"prices": {"preferred": 1, "backup": 100000}, "credit": 999999},
    ]
    selected = []
    for accounting in snapshots:
        assert accounting  # Accounting lives outside the routing contract.
        selected.append(choose_model(normalized, candidates, POLICY).model_id)
    assert selected == ["preferred", "preferred"]
    assert list(inspect.signature(choose_model).parameters) == [
        "todo",
        "capability_catalog",
        "privacy_policy",
    ]
    with pytest.raises(ValidationError):
        binding(price=1)
    with pytest.raises(TypeError):
        choose_model(normalized, candidates, POLICY, credit=100)


@pytest.mark.parametrize(
    "invalid",
    [
        {"roles": ["L"]},
        {"enabled": False},
        {"available": False},
        {"allowed_projects": ["other"]},
        {"capabilities": ["structured_output"]},
        {"capabilities": ["analyze"]},
        {"quality_evidence_id": None},
    ],
)
def test_ineligible_model_does_not_silently_lower_role(invalid):
    high = normalize_todo(todo(kind="complex"), TaskFacts())
    with pytest.raises(AppError) as error:
        choose_model(high, [binding(**invalid)], POLICY)
    assert error.value.code == "NO_CAPABLE_MODEL"


def test_same_role_fallback_respects_privacy_and_supported_reasoning():
    high = normalize_todo(todo(kind="complex"), TaskFacts())
    denied = binding(provider="unapproved")
    selected = choose_model(high, [denied, binding(reasoning_efforts=["low"])], POLICY)
    assert selected.role == "H" and selected.reasoning_effort is None
    with pytest.raises(AppError) as error:
        choose_model(high, [denied], POLICY)
    assert error.value.code == "POLICY_DENIED"
    local = binding(metering_mode="local")
    local_policy = POLICY.model_copy(update={"local_only": True, "allow_cloud": False})
    assert choose_model(high, [binding(), local], local_policy).metering_mode == "local"
    with pytest.raises(AppError):
        choose_model(high, [local], PrivacyPolicy(project_id="sales", local_only=True))
    assert (
        choose_model(todo(), [binding(reasoning_efforts=["low"])], POLICY).reasoning_effort == "low"
    )


def test_normalization_preserves_input_and_does_not_grant_a_tool():
    initial = todo(kind="mechanical", signals=["causal_judgment"])
    before = initial.model_dump()
    normalized = normalize_todo(initial, TaskFacts())
    assert initial.model_dump() == before
    assert isinstance(normalized.executor, ModelExecutor)
    assert normalized.executor.role == "H"
    with pytest.raises(AppError) as error:
        normalize_todo(initial, TaskFacts(certified_deterministic=True))
    assert error.value.code == "PLAN_INVALID"
    with pytest.raises(AppError):
        normalize_todo(todo(usefulness="useful"), TaskFacts(requested_or_supporting=False))


def test_acceptance_cannot_invent_a_different_numeric_locale_from_execution():
    request = RunRequest(
        request="금액 합계", project_id="sales", artifact_ids=["source-1"], output_format="xlsx"
    )
    task = TaskSpec(
        intent="aggregate",
        source_ids=["source-1"],
        inputs={"value_columns": ["금액"]},
        acceptance=["따옴표로 감싼 천 단위 금액 합계"],
        ambiguity="clear",
    )
    source = {
        "artifact_id": "source-1",
        "format": "csv",
        "headers": ["금액"],
        "headers_valid": True,
    }
    assert expand_recipe(request, [source], task) is None


def test_missing_input_and_optional_do_not_require_any_provider():
    for candidate in [todo(clarity="missing_input"), todo(usefulness="optional")]:
        normalized = normalize_todo(
            candidate, TaskFacts(privacy_allows_candidate=False, capable_model_available=False)
        )
        assert choose_model(normalized, [], PrivacyPolicy(project_id="sales")) is None
