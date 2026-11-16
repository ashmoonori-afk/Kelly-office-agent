import json

import pytest
from pydantic import ValidationError

from kelly.contracts import (
    ActionProposal,
    Actor,
    AppError,
    Evidence,
    MeetingAction,
    ModelResult,
    PlanV1,
    RunRequest,
    TaskSpec,
    ToolExecutor,
    Usage,
    canonical_json,
    digest,
)


def test_nested_tool_and_task_input_size_is_bounded():
    oversized = {"nested": {"text": "가" * 22000}}
    with pytest.raises(ValidationError):
        ToolExecutor(tool_name="source.extract", args=oversized)
    with pytest.raises(ValidationError):
        TaskSpec(intent="요약", acceptance=["근거 포함"], inputs=oversized)


def test_model_result_and_usage_metadata_are_bounded():
    with pytest.raises(ValidationError):
        ModelResult(parsed_output={"text": "x" * (1024 * 1024)}, finish_reason="stop", latency_ms=1)
    with pytest.raises(ValidationError):
        ModelResult(
            parsed_output={}, raw_usage={"extra": "x" * 65536}, finish_reason="stop", latency_ms=1
        )


def test_meeting_dates_validate_calendar_and_unknown_values_remain_null():
    action = MeetingAction(action="계약 검토", evidence_ids=["e1"])
    assert action.owner is None and action.due_date is None
    with pytest.raises(ValidationError):
        MeetingAction(action="계약 검토", evidence_ids=["e1"], due_date="2026-02-30")


def todo(id="t1", **patch):
    value = {
        "id": id,
        "title": "자료 집계",
        "requirement_ids": ["r1"],
        "depends_on": [],
        "inputs": [{"artifact_id": "a-source_1"}],
        "kind": "mechanical",
        "reason": "지정한 범위의 합계를 계산한다",
        "signals": ["fixed_transform"],
        "clarity": "clear",
        "usefulness": "required",
        "executor": {"type": "tool", "tool_name": "table.aggregate", "args": {}},
        "output_schema": "AggregateV1",
        "done_when": ["합계와 출처를 제공한다"],
        "verify_with": ["numeric_grounding"],
        "status": "pending",
    }
    return value | patch


def plan(*todos):
    return {
        "version": 1,
        "requirements": [{"id": "r1", "text": "매출 합계 제공", "required": True}],
        "todos": list(todos or [todo()]),
    }


def test_json_roundtrip_and_canonical_hash():
    value = PlanV1.model_validate_json(json.dumps(plan()))
    assert PlanV1.model_validate_json(value.model_dump_json()) == value
    assert canonical_json({"나": 2, "a": True}) == '{"a":true,"나":2}'
    assert digest({"b": 2, "a": 1}) == digest({"a": 1, "b": 2})
    assert digest({"a": "1"}) != digest({"a": 1})


@pytest.mark.parametrize("value", [1.0, float("nan"), {"x": [1.5]}, {1: "x"}, (1, 2)])
def test_canonical_json_rejects_noncanonical_types(value):
    with pytest.raises(ValueError):
        canonical_json(value)


@pytest.mark.parametrize(
    "patch",
    [
        {"actor_id": "forged"},
        {"quality_mode": "cheap"},
        {"request": True},
        {"request": " "},
        {"project_id": "../sales"},
        {"artifact_ids": ["a"] * 21},
    ],
)
def test_untrusted_run_input_is_strict(patch):
    with pytest.raises(ValidationError):
        RunRequest.model_validate({"request": "매출 집계", "project_id": "sales"} | patch)


def test_actor_has_explicit_internal_scope():
    actor = Actor(id="local", project_ids=["sales"])
    assert actor.project_ids == ["sales"]
    with pytest.raises(ValidationError):
        Actor(id="local", project_ids=["sales"], trusted=True)


@pytest.mark.parametrize(
    "patch",
    [
        {"confidence": 0.9},
        {"kind": "cheap"},
        {"signals": ["price_low"]},
        {"reason": "a" * 121},
        {"output_schema": "ExecuteShellV1"},
        {"executor": {"type": "tool", "tool_name": "shell", "args": {}}},
        {"executor": {"type": "model", "role": "L", "task": "eval"}},
        {"executor": {"type": "model", "role": "M", "task": "report_prose", "budget": 1}},
        {"executor": {"type": "tool", "tool_name": "table.aggregate", "args": {"amount": 0.1}}},
        {"done_when": []},
    ],
)
def test_todo_fields_reject_invalid_values(patch):
    with pytest.raises(ValidationError):
        PlanV1.model_validate(plan(todo(**patch)))


@pytest.mark.parametrize(
    "value",
    [
        plan(todo(), todo()),
        plan(todo(depends_on=["t2"]), todo("t2")),
        plan(todo(inputs=[{"step_id": "missing", "output_key": "aggregate"}])),
        plan(todo(), todo("t2", inputs=[{"step_id": "t1", "output_key": "aggregate"}])),
        plan(
            todo(), todo("t2", depends_on=["t1"], inputs=[{"step_id": "t1", "output_key": "shell"}])
        ),
        plan(todo(requirement_ids=["unknown"])),
        plan(todo(usefulness="optional")),
        plan(todo(usefulness="useful")),
        plan(*[todo(f"t{i}") for i in range(9)]),
        plan() | {"version": True},
        plan() | {"requirements": [{"id": "r1", "text": "x", "required": 1}]},
    ],
)
def test_plan_rejects_invalid_dependencies_and_required_coverage(value):
    with pytest.raises(ValidationError):
        PlanV1.model_validate(value)


def test_plan_accepts_declared_backward_dependency():
    value = plan(
        todo(),
        todo("t2", depends_on=["t1"], inputs=[{"step_id": "t1", "output_key": "aggregate"}]),
    )
    assert len(PlanV1.model_validate(value).todos) == 2
    with pytest.raises(ValidationError):
        PlanV1.model_validate_json('{"version": 1, invalid}')


@pytest.mark.parametrize(
    "observed_at",
    [
        "2026-02-30T12:00:00Z",
        "2026-09-06",
        "2026-09-06T12:00:00",
        "2026-09-06T12:00:00+00:99",
        12,
        True,
    ],
)
def test_evidence_requires_real_timezone_date(observed_at):
    with pytest.raises(ValidationError):
        Evidence(
            artifact_id="a1",
            sha256="a" * 64,
            locator="Sheet1!A2",
            quote="100",
            extraction_version="1",
            observed_at=observed_at,
        )


def test_evidence_accepts_z_and_offset_dates():
    for observed_at in ["2026-09-06T12:00:00Z", "2026-09-06T21:00:00+09:00"]:
        assert (
            Evidence(
                artifact_id="a1",
                sha256="a" * 64,
                locator="Sheet1!A2",
                quote="100",
                extraction_version="1",
                observed_at=observed_at,
            ).observed_at
            == observed_at
        )


def test_usage_unknown_is_not_zero_and_subsets_are_validated():
    assert Usage().input_tokens_total is None
    for patch in [
        {"input_tokens_total": True},
        {"input_tokens_total": -1},
        {"input_tokens_total": 10, "cached_input_tokens": 11},
        {"output_tokens_total": 10, "reasoning_tokens": 11},
    ]:
        with pytest.raises(ValidationError):
            Usage(**patch)


def test_action_payload_and_safe_errors():
    value = {
        "actor_id": "local",
        "project_id": "sales",
        "run_id": "run1",
        "step_id": "t1",
        "tool": "artifact.publish",
        "args": {"draft_id": "a1"},
        "source_hashes": {"a1": "a" * 64},
        "draft_hash": "b" * 64,
        "destination": "exports/report.docx",
        "scope_revision": 1,
        "expires_at": "2026-09-06T12:00:00Z",
        "impact": "새 보고서 발행",
    }
    assert ActionProposal(**value).schema_version == 1
    with pytest.raises(ValidationError):
        ActionProposal(**(value | {"scope_revision": True}))
    assert "secret" not in str(AppError("INTERNAL_ERROR", "secret API_KEY"))
    assert AppError("POLICY_DENIED").status == 400
