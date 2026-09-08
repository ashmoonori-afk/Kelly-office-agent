"""Reference coverage regressions; pure checks do not certify source or semantic truth."""

import copy

import pytest
from test_context import binding
from test_verification import NOW, model_todo, report, source_context

from birkin_lite import office, verification
from birkin_lite.context import (
    PROMPT_VERSION,
    build_model_request,
    build_plan_request,
    response_cache_key,
)
from birkin_lite.contracts import (
    Actor,
    AppError,
    DraftReply,
    Evidence,
    Narrative,
    RunRequest,
    SearchAnswer,
    ValidationResult,
    digest,
    evidence_id,
)
from birkin_lite.tools import _content_from_input, catalog, registry


def numeric(**changes):
    return {
        "id": "n1",
        "value": "300.00",
        "aggregate_step": "t1",
        "column": "매출",
        "unit": "KRW",
        **changes,
    }


def test_raw_body_and_dangling_reference_cannot_hide_an_unreported_sentence():
    context = {"todo": model_todo(), "inputs": {}}
    good = report("확인한 내용을 정리합니다.")
    assert verification.model_grounding(good, context).status == "pass"
    for addition in ["회사가 폐업했다.", [{"claim_id": "undeclared"}]]:
        bad = copy.deepcopy(good)
        bad["sections"]["content"]["paragraphs"].append(addition)
        assert verification.model_grounding(bad, context).errors == ["INVALID_OUTPUT"]
    bad = copy.deepcopy(good)
    bad["sections"]["claims"].append({"id": "hidden", "text": "회사가 폐업했다."})
    bad["sections"]["content"]["paragraphs"].append([{"claim_id": "hidden"}])
    assert verification.model_grounding(bad, context).errors == ["MISSING_EVIDENCE"]


@pytest.mark.parametrize("field", ["title", "paragraphs", "bullets", "headers", "cells"])
def test_every_report_text_location_is_reference_only(field):
    value = report("정리 내용")["sections"]
    layout = value["content"]
    if field == "title":
        layout["title"] = "신고하지 않은 문장"
    elif field in {"paragraphs", "bullets"}:
        layout[field] = ["신고하지 않은 문장"]
    else:
        layout["tables"] = [{"headers": [[{"claim_id": "c1"}]], "rows": [[[{"claim_id": "c1"}]]]}]
        if field == "headers":
            layout["tables"][0]["headers"] = ["신고하지 않은 문장"]
        else:
            layout["tables"][0]["rows"] = [["신고하지 않은 문장"]]
    result = verification.model_grounding({"sections": value}, {"todo": model_todo(), "inputs": {}})
    assert result.errors == ["INVALID_OUTPUT"]


def test_numeric_render_preserves_subject_unit_operation_and_ratio_identity():
    value = Narrative.model_validate(
        {
            "content": {"paragraphs": [[{"numeric_id": "n1"}], [{"numeric_id": "n2"}]]},
            "numeric_claims": [
                numeric(group="영업"),
                numeric(id="n2", operation="ratio", denominator="비용", unit="ratio", value="2"),
            ],
        }
    )
    expected = ["영업 · 매출 합계: 300.00 KRW", "매출/비용 비율: 2 ratio"]
    assert verification.render_content(value).paragraphs == expected
    assert _content_from_input(value.model_dump(), "docx").paragraphs == expected
    # There is no caller-authored numeric label/value slot to swap independently.
    altered = value.model_dump()
    altered["content"]["paragraphs"][0][0]["label"] = "비용 USD"
    assert verification.model_grounding(
        {"sections": altered}, {"todo": model_todo(), "inputs": {}}
    ).errors == ["INVALID_OUTPUT"]


@pytest.mark.parametrize("unused", [True, False])
def test_literal_claim_cannot_borrow_an_aggregate_value_even_if_declared(unused):
    candidate = report("비용 300원", numeric=[numeric()])
    if unused:
        candidate["sections"]["content"]["paragraphs"].pop()
    result = verification.model_grounding(
        candidate,
        {"todo": model_todo(), "inputs": {"t1": {"columns": {"매출": {"sum": "300.00"}}}}},
    )
    assert result.errors == ["INVALID_OUTPUT" if unused else "NUMERIC_MISMATCH"]


def test_literal_numbers_are_scoped_to_each_claim_and_uncertainty():
    evidence = Evidence(
        artifact_id="source",
        sha256="a" * 64,
        locator="text:line:1",
        quote="매출 300원, 작성일 2026-09-08",
        extraction_version="fixture",
        observed_at=NOW.isoformat(),
    )
    ref = evidence_id(evidence)
    context = {"todo": model_todo(), "inputs": {"t1": {"items": [evidence.model_dump()]}}}
    good = report("매출 300원, 작성일 2026-09-08", [ref])
    parsed = verification.MODEL_OUTPUT_TYPES["summarize"].model_validate(good)
    assert verification._model_fields(parsed, {ref: evidence}, context)[2] == {ref}
    for field, key, item in [
        ("claims", "claim_id", {"id": "other", "text": "비용 300원", "kind": "inference"}),
        ("uncertainties", "uncertainty_id", {"id": "other", "text": "비용 300원 여부 미정"}),
    ]:
        bad = copy.deepcopy(good)
        bad["sections"].setdefault(field, []).append(item)
        bad["sections"]["content"]["paragraphs"].append([{key: "other"}])
        parsed = verification.MODEL_OUTPUT_TYPES["summarize"].model_validate(bad)
        with pytest.raises(AppError) as error:
            verification._model_fields(parsed, {ref: evidence}, context)
        assert error.value.code == "NUMERIC_MISMATCH"


def test_duplicate_cross_kind_ids_and_unused_numbers_fail_closed():
    value = report("설명", numeric=[numeric()])["sections"]
    value["numeric_claims"][0]["id"] = "c1"
    with pytest.raises(AppError):
        verification.render_content(Narrative.model_validate(value))
    value = report("설명", numeric=[numeric()])["sections"]
    value["content"]["paragraphs"].pop()
    with pytest.raises(AppError):
        verification.render_content(Narrative.model_validate(value))


def test_adjacent_references_cannot_form_a_new_number():
    candidate = {
        "sections": {
            "content": {"paragraphs": [[{"claim_id": "c1"}, {"claim_id": "c2"}]]},
            "claims": [
                {"id": "c1", "text": "10", "kind": "inference"},
                {"id": "c2", "text": "0", "kind": "inference"},
            ],
        }
    }
    context = {
        "todo": model_todo(),
        "inputs": {},
        "run": {"request_json": {"request": "10 과 0을 정리"}},
    }
    assert verification.model_grounding(candidate, context).status == "pass"
    rendered = verification.render_content(Narrative.model_validate(candidate["sections"]))
    assert rendered.paragraphs[0] == "10 0"
    assert "100" not in "\n".join(rendered.paragraphs)
    assert _content_from_input(candidate["sections"], "docx") == rendered


def test_reply_recipient_and_unknown_answer_keep_coverage_without_forcing_citations():
    draft = DraftReply.model_validate(
        {
            "subject": [{"claim_id": "subject"}],
            "body": [{"claim_id": "body"}],
            "claims": [
                {"id": "subject", "text": "감사 인사", "kind": "inference"},
                {"id": "body", "text": "도와주셔서 감사합니다.", "kind": "inference"},
            ],
        }
    )
    rendered = verification.render_content(draft)
    assert rendered.title == "감사 인사"
    assert rendered.paragraphs[:2] == ["수신인: 미정", "도와주셔서 감사합니다."]
    assert "해석: 도와주셔서 감사합니다." in rendered.paragraphs
    assert (
        verification.model_grounding(
            {"draft": draft.model_dump()},
            {"todo": model_todo("draft_reply", "DraftReplyV1"), "inputs": {}},
        ).status
        == "pass"
    )
    bad = draft.model_dump()
    bad["recipient"] = [{"claim_id": "unknown_recipient"}]
    with pytest.raises(AppError):
        verification.render_content(DraftReply.model_validate(bad))
    answer = SearchAnswer.model_validate(
        {
            "found": False,
            "text": [{"uncertainty_id": "u1"}],
            "uncertainties": [{"id": "u1", "text": "확인할 근거를 찾지 못했습니다."}],
        }
    )
    result = verification.model_grounding(
        {"answer": answer.model_dump()},
        {"todo": model_todo("search_answer", "SearchAnswerV1"), "inputs": {}},
    )
    assert result.status == "pass" and result.evidence_refs == []
    assert verification.render_content(answer).paragraphs == [
        "미확정: 확인할 근거를 찾지 못했습니다."
    ]
    assert verification.model_grounding(
        {"answer": {"found": False, "text": []}},
        {"todo": model_todo("search_answer", "SearchAnswerV1"), "inputs": {}},
    ).errors == ["INVALID_OUTPUT"]


def test_content_prompt_and_cache_version_change_without_planner_overhead(tmp_path):
    selected = binding()
    request = build_model_request(model_todo(), {}, {}, binding=selected)
    assert request.prompt_version == PROMPT_VERSION == "office-task-v2"
    assert "every text leaf" in request.context
    planned = build_plan_request(
        RunRequest(request="내용을 정리해 줘", project_id="sales"),
        [],
        catalog(registry(tmp_path)),
        binding=selected,
    )
    assert "every text leaf" not in planned.context
    actor = Actor(id="owner", project_ids=["sales"])
    old = request.model_copy(update={"prompt_version": "office-task-v1"})
    assert response_cache_key(
        actor, "sales", {}, request, selected, policy={}
    ) != response_cache_key(actor, "sales", {}, old, selected, policy={})


def test_reference_rendering_preserves_reply_text_length_limits():
    candidate = {
        "draft": {
            "subject": [{"claim_id": "c1"}],
            "body": [{"claim_id": "c1"}],
            "claims": [{"id": "c1", "text": "가" * 501, "kind": "inference"}],
        }
    }
    context = {"todo": model_todo("draft_reply", "DraftReplyV1"), "inputs": {}}
    assert verification.model_grounding(candidate, context).errors == ["INVALID_OUTPUT"]


def test_actual_txt_coverage_and_review_cannot_waive_undeclared_prose(tmp_path):
    """Real source reopening, fixture review proof; this is not a live model accuracy score."""
    source, context = source_context(tmp_path, "예산은 100원이다.\n작성일은 2026-09-08이다.")
    try:
        evidence = office.locate(source["id"], "text:line:1", context)
        ref = evidence_id(evidence)
        context["inputs"] = {"t1": {"items": [evidence.model_dump()]}}
        good = report("예산은 100원이다.", [ref])
        assert verification.model_grounding(good, context).status == "pass"
        bad = copy.deepcopy(good)
        bad["sections"]["content"]["paragraphs"].append("회사가 폐업했다.")
        proof = (
            digest(bad),
            ValidationResult(
                status="pass", checked_layers=["semantic_review"], evidence_refs=[ref]
            ),
        )
        assert verification.model_grounding(bad, context, semantic_review=proof).errors == [
            "INVALID_OUTPUT"
        ]
        bad = copy.deepcopy(good)
        bad["sections"]["claims"].append({"id": "extra", "text": "비용 100원", "kind": "inference"})
        bad["sections"]["content"]["paragraphs"].append([{"claim_id": "extra"}])
        proof = (digest(bad), proof[1])
        assert verification.model_grounding(bad, context, semantic_review=proof).errors == [
            "NUMERIC_MISMATCH"
        ]
    finally:
        context["conn"].close()
