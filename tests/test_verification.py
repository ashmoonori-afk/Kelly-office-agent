import copy
import io
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from birkin_lite import office
from birkin_lite.artifacts import import_source
from birkin_lite.contracts import (
    Actor,
    AppError,
    Evidence,
    MeetingActionsV1,
    NumericClaim,
    PlanV1,
    RunRequest,
    TodoV1,
    ValidationResult,
    canonical_json,
    digest,
    evidence_id,
)
from birkin_lite.db import create_run, init_db
from birkin_lite.engine import install_plan
from birkin_lite.tools import registry as tool_registry
from birkin_lite.verification import model_grounding, required_verifiers

OWNER = Actor(id="owner", project_ids=["office"])
NOW = datetime(2026, 9, 8, tzinfo=UTC)


def model_todo(task="summarize", schema="ReportSectionsV1", **updates):
    value = {
        "id": "t2",
        "title": "확인한 근거로 작성",
        "requirement_ids": ["r1"],
        "kind": "standard",
        "reason": "제공된 자료 정리",
        "signals": ["summarization"],
        "clarity": "clear",
        "usefulness": "required",
        "executor": {"type": "model", "role": "M", "task": task},
        "output_schema": schema,
        "done_when": ["근거 확인"],
        **updates,
    }
    return TodoV1.model_validate(value)


def source_context(tmp_path, text, *, filename="meeting.txt", quality="balanced"):
    conn, home = init_db(tmp_path / "state.db"), tmp_path / "files"
    source = import_source(
        home, conn, io.BytesIO(text.encode()), {"filename": filename}, OWNER, "office"
    )
    run = create_run(
        conn,
        OWNER,
        "ui",
        "verification",
        RunRequest(
            request="자료를 정리해줘",
            project_id="office",
            artifact_ids=[source["id"]],
            quality_mode=quality,
        ),
    )
    context = {
        "conn": conn,
        "home": home,
        "actor": OWNER,
        "run": run,
        "todo": model_todo(),
        "inputs": {},
    }
    return source, context


def report(text, refs=(), numeric=()):
    return {
        "sections": {
            "content": {
                "paragraphs": [
                    [{"claim_id": "c1"}],
                    *[[{"numeric_id": claim["id"]}] for claim in numeric],
                ]
            },
            "claims": [
                {
                    "id": "c1",
                    "text": text,
                    "kind": "fact" if refs else "inference",
                    "evidence_ids": list(refs),
                }
            ],
            "numeric_claims": list(numeric),
        }
    }


def test_required_verifiers_cannot_be_omitted_or_duplicated():
    todo = model_todo(verify_with=["report_grounding", "source_grounding", "numeric_grounding"])
    assert required_verifiers(todo) == ["model_grounding"]
    todo.verify_with = []
    assert required_verifiers(todo) == ["model_grounding"]
    tool = TodoV1.model_validate(
        {
            **todo.model_dump(),
            "kind": "mechanical",
            "executor": {
                "type": "tool",
                "tool_name": "document.create",
                "args": {"template_id": "office-report-v1", "content": {"title": "자료"}},
            },
            "output_schema": "ArtifactV1",
        }
    )
    assert required_verifiers(tool) == ["source_grounding", "report_structure"]
    todo.verify_with = ["invented_verifier"]
    assert required_verifiers(todo) == ["invented_verifier", "model_grounding"]


def test_no_source_writing_is_schema_checked_without_filesystem_context():
    result = model_grounding(report("감사 인사 초안입니다."), {"todo": model_todo(), "inputs": {}})
    assert result.status == "pass"
    assert "schema" in result.checked_layers
    assert not {"semantic_truth", "visual", "visual_verified"} & set(result.checked_layers)
    invalid = model_grounding({"sections": []}, {"todo": model_todo(), "inputs": {}})
    assert invalid.status == "fail" and invalid.errors == ["INVALID_OUTPUT"]


def test_claim_ids_are_bound_to_supplied_reopened_evidence(tmp_path):
    source, context = source_context(
        tmp_path, "프로젝트 예산은 100원으로 확정했다.\n담당자는 미정이다."
    )
    evidence = office.locate(source["id"], "text:line:1", context)
    ref = evidence_id(evidence)
    context["inputs"] = {"t1": {"items": [evidence.model_dump()]}}
    output = report("프로젝트 예산은 100원이다.", [ref])
    result = model_grounding(output, context)
    assert result.status == "pass" and result.evidence_refs == [ref]
    assert {"source_version", "locator_quote", "evidence_references"} <= set(result.checked_layers)
    assert (
        model_grounding(
            output, {key: value for key, value in context.items() if key != "home"}
        ).status
        == "fail"
    )
    assert model_grounding(
        output, {**context, "actor": Actor(id="other", project_ids=["office"])}
    ).errors == ["NOT_FOUND"]

    for mutation, error in [
        (
            lambda value: value["sections"]["claims"][0].update(evidence_ids=["madeup"]),
            "EVIDENCE_MISMATCH",
        ),
        (lambda value: value["sections"]["claims"][0].update(evidence_ids=[]), "MISSING_EVIDENCE"),
        (
            lambda value: value["sections"]["claims"][0].update(text="프로젝트 예산은 999원이다."),
            "NUMERIC_MISMATCH",
        ),
        (
            lambda value: value["sections"]["claims"].append(
                copy.deepcopy(value["sections"]["claims"][0])
            ),
            "INVALID_OUTPUT",
        ),
    ]:
        candidate = copy.deepcopy(output)
        mutation(candidate)
        result = model_grounding(candidate, context)
        assert result.status == "fail" and result.errors == [error]

    # A valid locator elsewhere in the same project was never sent to this model.
    other = office.locate(source["id"], "text:line:2", context)
    result = model_grounding(report("담당자는 미정이다.", [evidence_id(other)]), context)
    assert result.errors == ["EVIDENCE_MISMATCH"]
    forged = evidence.model_copy(update={"quote": "프로젝트 예산은 999원으로 확정했다."})
    context["inputs"] = {"t1": {"items": [forged.model_dump()]}}
    result = model_grounding(report(forged.quote, [evidence_id(forged)]), context)
    assert result.errors == ["EVIDENCE_MISMATCH"]
    context["inputs"] = {"t1": {"items": [evidence.model_dump()]}}
    (context["home"] / source["relative_path"]).write_text("변경된 자료", encoding="utf-8")
    assert model_grounding(output, context).errors == ["SOURCE_CHANGED"]
    context["conn"].close()


def test_missing_owner_and_calendar_date_are_not_guessed(tmp_path):
    source, context = source_context(
        tmp_path,
        "담당자: 민지; 기한: 2026년 9월 10일; 보고서 작성\n"
        "참석자: 민지; 담당자: 미정; 금요일까지 견적 보내기",
    )
    spans = [office.locate(source["id"], f"text:line:{line}", context) for line in (1, 2)]
    refs = [evidence_id(span) for span in spans]
    context.update(
        todo=model_todo("meeting_actions", "MeetingActionsV1"),
        inputs={"t1": {"items": [span.model_dump() for span in spans]}},
    )
    output = {
        "actions": [
            {
                "action": "보고서 작성",
                "owner": "민지",
                "due_date": "2026-09-10",
                "due_text": "2026년 9월 10일",
                "evidence_ids": [refs[0]],
            },
            {
                "action": "견적 보내기",
                "owner": None,
                "due_date": None,
                "due_text": "금요일",
                "evidence_ids": [refs[1]],
            },
        ]
    }
    assert model_grounding(output, context).status == "pass"
    for index, field, value, error in [
        (1, "owner", "민지", "OWNER_UNGROUNDED"),
        (0, "owner", "철수", "OWNER_UNGROUNDED"),
        (1, "due_date", "2026-09-11", "DUE_UNGROUNDED"),
        (0, "due_text", "2026년 9월 11일", "DUE_UNGROUNDED"),
    ]:
        candidate = copy.deepcopy(output)
        candidate["actions"][index][field] = value
        checked = model_grounding(candidate, context)
        assert checked.status == "fail" and checked.errors == [error]
    context["conn"].close()


def test_search_found_requires_a_factual_cited_claim_and_cannot_invent_proof():
    context = {"todo": model_todo("search_answer", "SearchAnswerV1"), "inputs": {}}
    assert (
        model_grounding(
            {
                "answer": {
                    "found": False,
                    "text": [{"uncertainty_id": "u1"}],
                    "uncertainties": [{"id": "u1", "text": "확인 불가", "evidence_ids": []}],
                }
            },
            context,
        ).status
        == "pass"
    )
    assert model_grounding(
        {
            "answer": {
                "found": True,
                "text": [{"claim_id": "c1"}],
                "claims": [{"id": "c1", "text": "승인했음", "kind": "inference"}],
            }
        },
        context,
    ).errors == ["MISSING_EVIDENCE"]
    context["todo"] = model_todo("semantic_review", "ValidationResultV1")
    context["inputs"] = {"t1": {"content": {"paragraphs": ["검토 대상"]}}}
    assert model_grounding(
        {"validation": {"status": "pass", "checked_layers": ["visual_verified"]}}, context
    ).errors == ["INVALID_OUTPUT"]
    checked = model_grounding(
        {"validation": {"status": "pass", "checked_layers": ["semantic_review"]}}, context
    )
    assert checked.status == "pass" and "semantic_truth" not in checked.checked_layers


def test_high_review_rechecks_real_txt_and_all_claim_references(tmp_path):
    """Real source boundary with a fixture review verdict, not live model qualification."""
    source, context = source_context(
        tmp_path, "일정은 연기되었습니다.\n범위는 유지됩니다.", quality="high"
    )
    try:
        evidence = [office.locate(source["id"], f"text:line:{line}", context) for line in (1, 2)]
        refs = [evidence_id(item) for item in evidence]
        context["inputs"] = {"t1": {"items": [item.model_dump() for item in evidence]}}
        candidate = {
            "sections": {
                "content": {
                    "paragraphs": [[{"claim_id": f"c{index}"}] for index in range(len(evidence))]
                },
                "claims": [
                    {"id": f"c{index}", "text": item.quote, "evidence_ids": [ref]}
                    for index, (item, ref) in enumerate(zip(evidence, refs, strict=True))
                ],
            }
        }
        verdict = ValidationResult(
            status="pass", checked_layers=["semantic_review"], evidence_refs=refs
        )
        assert model_grounding(candidate, context).errors == ["SEMANTIC_REVIEW_REQUIRED"]
        assert model_grounding(
            candidate,
            context,
            semantic_review=(
                digest(candidate),
                verdict.model_copy(update={"evidence_refs": refs[:1]}),
            ),
        ).errors == ["EVIDENCE_MISMATCH"]
        assert model_grounding(candidate, context, semantic_review=("0" * 64, verdict)).errors == [
            "INVALID_OUTPUT"
        ]
        checked = model_grounding(candidate, context, semantic_review=(digest(candidate), verdict))
        assert checked.status == "pass"
        assert {"source_version", "locator_quote", "semantic_review"} <= set(checked.checked_layers)
        path = context["home"] / source["relative_path"]
        path.write_text("변경된 원본", encoding="utf-8")
        assert model_grounding(
            candidate, context, semantic_review=(digest(candidate), verdict)
        ).errors == ["SOURCE_CHANGED"]
    finally:
        context["conn"].close()


def test_field_and_numeric_contracts_without_process_boundary(monkeypatch):
    """Pure contract evidence; the separate file tests prove the actual parser boundary."""
    from birkin_lite import verification

    evidence = Evidence(
        artifact_id="source",
        sha256="a" * 64,
        locator="text:line:1",
        quote="참석자: 민지; 담당자: 미정; 금요일까지 보고서 작성",
        extraction_version="fixture",
        observed_at=NOW.isoformat(),
    )
    ref = evidence_id(evidence)
    action = MeetingActionsV1.model_validate(
        {
            "actions": [
                {
                    "action": "보고서 작성",
                    "due_text": "금요일",
                    "evidence_ids": [ref],
                }
            ]
        }
    ).actions[0]
    assert verification._meeting_refs([action], {ref: evidence}) == {ref}
    for owner in ("민지", "미정"):
        with pytest.raises(AppError) as error:
            verification._meeting_refs(
                [action.model_copy(update={"owner": owner})], {ref: evidence}
            )
        assert error.value.code == "OWNER_UNGROUNDED"
    with pytest.raises(AppError) as error:
        verification._meeting_refs(
            [action.model_copy(update={"due_date": "2026-09-11"})], {ref: evidence}
        )
    assert error.value.code == "DUE_UNGROUNDED"

    monkeypatch.setattr(
        verification,
        "_verified_aggregate",
        lambda *_: {
            "columns": {"매출": {"sum": "60.00", "average": "20.00"}},
            "unit": "KRW",
            "groups": [{"key": "영업", "sums": {"매출": "30.00"}}],
        },
    )
    claim = NumericClaim(id="n1", value="60", aggregate_step="t1", column="매출", unit="KRW")
    assert verification._numeric_refs([claim], {}) == {Decimal(60)}
    for update, code in [
        ({"value": "61"}, "NUMERIC_MISMATCH"),
        ({"unit": "USD"}, "NUMERIC_REFERENCE_INVALID"),
        ({"group": "다른 부서"}, "NUMERIC_REFERENCE_INVALID"),
    ]:
        with pytest.raises(AppError) as error:
            verification._numeric_refs([claim.model_copy(update=update)], {})
        assert error.value.code == code


def test_natural_assignment_needs_separate_exactly_bound_semantic_review(monkeypatch):
    """The file reopen is mocked here; hash/locator enforcement has separate actual-file tests."""
    from birkin_lite import verification

    evidence = Evidence(
        artifact_id="source",
        sha256="a" * 64,
        locator="text:line:1",
        quote="민지가 보고서를 작성한다.",
        extraction_version="fixture",
        observed_at=NOW.isoformat(),
    )
    ref = evidence_id(evidence)
    output = {"actions": [{"action": "보고서 작성", "owner": "민지", "evidence_ids": [ref]}]}
    context = {
        "todo": model_todo("meeting_actions", "MeetingActionsV1"),
        "inputs": {"t1": {"items": [evidence.model_dump()]}},
    }
    checked_sources = []
    monkeypatch.setattr(
        verification, "_check_evidence", lambda *args: checked_sources.append(args[0])
    )
    assert verification.needs_semantic_review(output, context) is True
    paraphrased = copy.deepcopy(output)
    paraphrased["actions"][0]["action"] = "문서 준비"
    assert verification.needs_semantic_review(paraphrased, context) is True
    multi = evidence.model_copy(
        update={"quote": "담당자: 민지; 보고서 작성. 담당자: 철수; 문서 준비."}
    )
    multi_ref = evidence_id(multi)
    swapped = {"actions": [{"action": "문서 준비", "owner": "민지", "evidence_ids": [multi_ref]}]}
    assert (
        verification.needs_semantic_review(
            swapped, {**context, "inputs": {"t1": {"items": [multi.model_dump()]}}}
        )
        is True
    )
    assert checked_sources == []  # The routing hint does not reparse source files.
    assert model_grounding(output, context).errors == ["SEMANTIC_REVIEW_REQUIRED"]
    review = ValidationResult(
        status="pass", checked_layers=["semantic_review"], evidence_refs=[ref]
    )
    # An input/source field named like the internal keyword grants no review authority.
    context["inputs"]["semantic_review"] = {
        "output_hash": digest(output),
        "validation": review.model_dump(),
    }
    assert model_grounding(output, context).errors == ["SEMANTIC_REVIEW_REQUIRED"]
    result = model_grounding(output, context, semantic_review=(digest(output), review))
    assert result.status == "pass" and "semantic_review" in result.checked_layers
    assert model_grounding(output, context, semantic_review=("b" * 64, review)).errors == [
        "INVALID_OUTPUT"
    ]
    assert model_grounding(
        output,
        context,
        semantic_review=(digest(output), review.model_copy(update={"status": "fail"})),
    ).errors == ["INVALID_OUTPUT"]
    altered = copy.deepcopy(output)
    altered["actions"][0]["owner"] = "철수"
    assert verification.needs_semantic_review(altered, context) is False
    assert model_grounding(altered, context, semantic_review=(digest(altered), review)).errors == [
        "OWNER_UNGROUNDED"
    ]
    altered = copy.deepcopy(output)
    altered["actions"][0]["due_date"] = "2026-09-11"
    assert verification.needs_semantic_review(altered, context) is False
    assert len(checked_sources) == 6


def test_natural_assignment_review_rechecks_real_txt_and_exact_candidate(tmp_path):
    """Real managed TXT/locator checks with a fixture H verdict; no live provider is called."""
    source, context = source_context(tmp_path, "민지가 보고서를 작성한다.\n철수는 예산을 확인한다.")
    try:
        evidence = office.locate(source["id"], "text:line:1", context)
        ref = evidence_id(evidence)
        context.update(
            todo=model_todo("meeting_actions", "MeetingActionsV1"),
            inputs={"t1": {"items": [evidence.model_dump()]}},
        )
        candidate = {"actions": [{"action": "보고서 작성", "owner": "민지", "evidence_ids": [ref]}]}
        review = ValidationResult(
            status="pass", checked_layers=["semantic_review"], evidence_refs=[ref]
        )
        proof = (digest(candidate), review)
        assert model_grounding(candidate, context).errors == ["SEMANTIC_REVIEW_REQUIRED"]
        accepted = model_grounding(candidate, context, semantic_review=proof)
        assert accepted.status == "pass" and accepted.evidence_refs == [ref]
        assert {
            "source_version",
            "locator_quote",
            "semantic_review",
            "reviewed_meeting_fields",
        } <= set(accepted.checked_layers)

        altered_candidate = copy.deepcopy(candidate)
        altered_candidate["actions"][0]["action"] = "보고서 폐기"
        assert model_grounding(altered_candidate, context, semantic_review=proof).errors == [
            "INVALID_OUTPUT"
        ]

        # Make every supplied ID/hash agree with the forged locator so only reopening
        # the actual second line can detect that its quote belongs to the first line.
        forged = evidence.model_copy(update={"locator": "text:line:2"})
        forged_ref = evidence_id(forged)
        forged_candidate = copy.deepcopy(candidate)
        forged_candidate["actions"][0]["evidence_ids"] = [forged_ref]
        forged_review = review.model_copy(update={"evidence_refs": [forged_ref]})
        assert model_grounding(
            forged_candidate,
            {**context, "inputs": {"t1": {"items": [forged.model_dump()]}}},
            semantic_review=(digest(forged_candidate), forged_review),
        ).errors == ["EVIDENCE_MISMATCH"]

        # A prior H verdict cannot authorize newly changed physical source bytes.
        (context["home"] / source["relative_path"]).write_text(
            "보고서 담당자는 아직 정하지 않았다.", encoding="utf-8"
        )
        assert model_grounding(candidate, context, semantic_review=proof).errors == [
            "SOURCE_CHANGED"
        ]
    finally:
        context["conn"].close()


def test_numeric_claims_recalculate_actual_source_and_match_exact_reference(tmp_path, monkeypatch):
    source, context = source_context(
        tmp_path, "부서,매출,비용\n영업,10,5\n영업,20,10\n지원,30,15\n", filename="sales.csv"
    )
    aggregate_todo = TodoV1.model_validate(
        {
            **model_todo().model_dump(),
            "id": "t1",
            "kind": "mechanical",
            "executor": {
                "type": "tool",
                "tool_name": "table.aggregate",
                "args": {
                    "artifact_id": source["id"],
                    "group_by": "부서",
                    "value_columns": ["매출", "비용"],
                    "policy": {
                        "unit": "KRW",
                        "ratio": {"numerator": "매출", "denominator": "비용"},
                    },
                },
            },
            "output_schema": "AggregateV1",
        }
    )
    todo = model_todo(depends_on=["t1"], inputs=[{"step_id": "t1", "output_key": "aggregate"}])
    install_plan(
        context["conn"],
        context["run"]["id"],
        PlanV1.model_validate(
            {
                "requirements": [{"id": "r1", "text": "검증 보고서"}],
                "todos": [aggregate_todo.model_dump(), todo.model_dump()],
            }
        ),
        OWNER,
        tools=tool_registry(context["home"]),
    )
    aggregate = tool_registry(context["home"])["table.aggregate"].execute(
        aggregate_todo.executor.args, context
    )["aggregate"]
    context["conn"].execute(
        "UPDATE steps SET state='succeeded',output_json=? WHERE run_id=? AND ordinal=0",
        (canonical_json({"aggregate": aggregate}), context["run"]["id"]),
    )
    context.update(todo=todo, inputs={"t1": aggregate})
    claims = [
        {
            "id": "sum",
            "value": "60.00",
            "aggregate_step": "t1",
            "column": "매출",
            "operation": "sum",
            "unit": "KRW",
        },
        {
            "id": "average",
            "value": "20.00",
            "aggregate_step": "t1",
            "column": "매출",
            "operation": "average",
            "unit": "KRW",
        },
        {
            "id": "group",
            "value": "30.00",
            "aggregate_step": "t1",
            "column": "매출",
            "group": "영업",
            "operation": "sum",
            "unit": "KRW",
        },
        {
            "id": "ratio",
            "value": "2.00",
            "aggregate_step": "t1",
            "column": "매출",
            "operation": "ratio",
            "unit": "ratio",
            "denominator": "비용",
        },
    ]
    calls = []
    real_aggregate = office.aggregate

    def recalculate(*args, **kwargs):
        calls.append(args[0])
        return real_aggregate(*args, **kwargs)

    monkeypatch.setattr(office, "aggregate", recalculate)
    assert (
        model_grounding(report("합계, 평균, 영업 집계와 비율", numeric=claims), context).status
        == "pass"
    )
    assert calls == [source["id"]]
    for update, code in [
        ({"value": "600"}, "NUMERIC_MISMATCH"),
        ({"column": "비용"}, "NUMERIC_MISMATCH"),
        ({"aggregate_step": "other_run"}, "NUMERIC_REFERENCE_INVALID"),
        ({"unit": "USD"}, "NUMERIC_REFERENCE_INVALID"),
        ({"group": "미등록부서"}, "NUMERIC_REFERENCE_INVALID"),
        ({"group": "영업", "operation": "average"}, "NUMERIC_REFERENCE_INVALID"),
    ]:
        claim = {**claims[0], **update}
        assert model_grounding(report("수치", numeric=[claim]), context).errors == [code]
    assert model_grounding(report("합계 999원", numeric=[claims[0]]), context).errors == [
        "NUMERIC_MISMATCH"
    ]
    # A real verified revenue total cannot justify a literal claim about costs, even when the
    # same number is declared in a numeric reference elsewhere in the document.
    assert model_grounding(report("비용 60원", numeric=[claims[0]]), context).errors == [
        "NUMERIC_MISMATCH"
    ]
    assert model_grounding(
        report("비율", numeric=[{**claims[-1], "denominator": "매출"}]), context
    ).errors == ["NUMERIC_REFERENCE_INVALID"]
    # Corrupting both the saved result and the supplied result must not fool a recomputation.
    aggregate["columns"]["매출"]["sum"] = "600.00"
    context["conn"].execute(
        "UPDATE steps SET output_json=? WHERE run_id=? AND ordinal=0",
        (canonical_json({"aggregate": aggregate}), context["run"]["id"]),
    )
    assert model_grounding(
        report("합계", numeric=[{**claims[0], "value": "600"}]), context
    ).errors == ["NUMERIC_MISMATCH"]
    context["conn"].close()
