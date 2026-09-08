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


def test_missing_input_and_optional_do_not_require_any_provider():
    for candidate in [todo(clarity="missing_input"), todo(usefulness="optional")]:
        normalized = normalize_todo(
            candidate, TaskFacts(privacy_allows_candidate=False, capable_model_available=False)
        )
        assert choose_model(normalized, [], PrivacyPolicy(project_id="sales")) is None
