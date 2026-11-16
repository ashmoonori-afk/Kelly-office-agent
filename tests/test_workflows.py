"""Real managed Office files and execution; only the metered model transport is a fixture."""

import io
import json
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial

import pytest
from docx import Document

from kelly.artifacts import import_source, metadata, open_artifact
from kelly.contracts import (
    Actor,
    Evidence,
    ModelBinding,
    ModelResult,
    PrivacyPolicy,
    RunRequest,
    Usage,
    canonical_json,
    digest,
    evidence_id,
)
from kelly.db import create_run, init_db, row_dict
from kelly.engine import complete_model_todo, execute_next
from kelly.planning import prepare_run
from kelly.tools import registry
from kelly.usage import summarize_usage
from kelly.verification import registry as verifier_registry

OWNER = Actor(id="owner", project_ids=["sales"])
NOW = datetime(2026, 9, 8, tzinfo=UTC)
POLICY = PrivacyPolicy(project_id="sales", allowed_providers=["fixture"], allow_cloud=True)
BINDING = ModelBinding(
    provider="fixture",
    model_id="offline-workflow-fixture",
    roles=["L", "M", "H"],
    enabled=True,
    available=True,
    allowed_projects=["sales"],
    capabilities=[
        "structured_output",
        "planning",
        "summarize",
        "report_prose",
        "meeting_actions",
        "draft_reply",
        "search_answer",
        "analyze",
        "semantic_review",
    ],
    context_window=32768,
    max_output=2048,
    quality_evidence_id="offline-fixture-only",
    metering_mode="api",
)


@pytest.fixture
def workspace(tmp_path):
    conn, home = init_db(tmp_path / "state.db"), tmp_path / "files"
    try:
        yield conn, home
    finally:
        conn.close()


def source_and_run(workspace, filename, raw, request, output_format, *, attach=True):
    conn, home = workspace
    source = import_source(home, conn, io.BytesIO(raw), {"filename": filename}, OWNER, "sales")
    run = create_run(
        conn,
        OWNER,
        "ui",
        "workflow",
        RunRequest(
            request=request,
            project_id="sales",
            artifact_ids=[source["id"]] if attach else [],
            output_format=output_format,
        ),
    )
    return source, run


def sent_evidence(payload):
    result = []

    def visit(value):
        if isinstance(value, dict):
            if {"artifact_id", "sha256", "locator", "quote", "evidence_id"} <= value.keys():
                item = Evidence.model_validate(
                    {key: child for key, child in value.items() if key != "evidence_id"}
                )
                assert value["evidence_id"] == evidence_id(item)
                result.append(value)
            else:
                for child in value.values():
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload["inputs"])
    return result


def fact(item, text, claim_id="fact1"):
    return {"id": claim_id, "text": text, "kind": "fact", "evidence_ids": [item["evidence_id"]]}


def finish(workspace, run, expected_task, expected_role, make_output):
    conn, home = workspace
    tools, seen = registry(home), []

    def no_planner(*_):
        pytest.fail("A fixed workflow must expand without a planning model call")

    prepared = prepare_run(
        conn,
        home,
        run["id"],
        OWNER,
        tools=tools,
        capability_catalog=[BINDING],
        privacy_policy=POLICY,
        complete=no_planner,
    )
    assert prepared["state"] == "running", prepared.get("error_code")
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0

    def transport(request, binding):
        payload = json.loads(request.context.split("\nDATA\n", 1)[1])
        assert payload["task"] == expected_task
        assert binding.role == expected_role and binding.model_id == BINDING.model_id
        seen.append(payload)
        return ModelResult(
            parsed_output=make_output(payload),
            normalized_usage=Usage(
                input_tokens_total=40,
                cached_input_tokens=8,
                output_tokens_total=12,
                reasoning_tokens=2,
                usage_basis="provider",
            ),
            raw_usage={
                "prompt_tokens": 40,
                "completion_tokens": 10,
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
            request_id="workflow-request-1",
            finish_reason="stop",
            latency_ms=1,
        )

    complete = partial(
        complete_model_todo,
        capability_catalog=[BINDING],
        privacy_policy=POLICY,
        transport=transport,
    )
    for _ in range(9):
        result = execute_next(
            conn,
            run["id"],
            actor=OWNER,
            tools=tools,
            complete=complete,
            now=NOW,
            home=home,
            verifiers=verifier_registry(home),
        )
        if result["state"] != "running":
            break
    steps = [row_dict(row) for row in conn.execute("SELECT * FROM steps ORDER BY ordinal")]
    assert result["state"] == "succeeded", (result.get("error_code"), steps)
    assert len(seen) == 1 and len(steps) == 5
    assert all(
        step["state"] == "succeeded" and step["validation_json"]["status"] == "pass"
        for step in steps
    )
    calls = [row_dict(row) for row in conn.execute("SELECT * FROM calls")]
    assert len(calls) == 1 and calls[0]["purpose"] == "generate"
    assert calls[0]["logical_call_no"] == calls[0]["transport_attempt_no"] == 1
    assert calls[0]["route_json"]["role"] == expected_role and calls[0]["state"] == "settled"
    assert calls[0]["provider_request_id"] == "workflow-request-1"
    assert calls[0]["input_tokens_total"] == 40 and calls[0]["output_tokens_total"] == 12
    assert calls[0]["actual_micro_usd"] is None
    assert summarize_usage(conn, run["id"], OWNER)["usage_state"] == "provider"
    assert not conn.execute("SELECT 1 FROM approvals").fetchone()
    assert not conn.execute("SELECT 1 FROM outbox").fetchone()

    outputs = result["result_json"]["outputs"]
    receipt = outputs["t5"]["receipt"]
    assert receipt["usage_state"] == "provider"
    record = metadata(conn, receipt["artifact_id"], OWNER, "sales")
    assert record["kind"] == "result" and receipt["artifact_hash"] == record["sha256"]
    with open_artifact(home, conn, record["id"], OWNER, "sales") as handle:
        raw = handle.read()
    provenance = record["provenance_json"]["sources"]
    proof = provenance["content_proof"]
    assert proof["step_id"] == "t2" and proof["output_hash"] == digest(outputs["t2"])
    assert proof["validation"]["status"] == "pass"
    assert "numeric_references" in proof["validation"]["checked_layers"]
    assert provenance["visual_verified"] is False
    snapshot = canonical_json(steps)
    execute_next(conn, run["id"], actor=OWNER, tools=tools, complete=complete, now=NOW, home=home)
    assert len(seen) == 1
    assert conn.execute("SELECT count(*) FROM artifacts WHERE kind='result'").fetchone()[0] == 1
    assert (
        canonical_json(
            [row_dict(row) for row in conn.execute("SELECT * FROM steps ORDER BY ordinal")]
        )
        == snapshot
    )
    return result, receipt, provenance, raw


def check_source(workspace, source, raw, receipt):
    conn, home = workspace
    assert receipt["source_versions"][source["id"]] == source["sha256"]
    with open_artifact(home, conn, source["id"], OWNER, "sales") as handle:
        assert handle.read() == raw


def test_w02_csv_to_grounded_report_docx_and_receipt(workspace):
    raw = "부서,금액\n영업,100.10\n지원,199.90\n".encode()
    source, run = source_and_run(
        workspace, "매출.csv", raw, "매출표 금액 합계 보고서 만들어줘", "docx"
    )

    def output(payload):
        aggregate = payload["inputs"]["t1"]
        assert Decimal(aggregate["columns"]["금액"]["sum"]) == Decimal("300.00")
        assert aggregate["unit"] == "unspecified"
        return {
            "sections": {
                "content": {
                    "title": [{"claim_id": "title1"}],
                    "paragraphs": [[{"numeric_id": "n1"}]],
                },
                "claims": [
                    {
                        "id": "title1",
                        "text": "매출 보고서",
                        "kind": "inference",
                        "evidence_ids": [],
                    }
                ],
                "numeric_claims": [
                    {
                        "id": "n1",
                        "value": "300.00",
                        "aggregate_step": "t1",
                        "column": "금액",
                        "operation": "sum",
                        "unit": "unspecified",
                    }
                ],
            }
        }

    result, receipt, provenance, created = finish(workspace, run, "report_prose", "M", output)
    document = Document(io.BytesIO(created))
    assert "금액 합계: 300.00 unspecified" in [paragraph.text for paragraph in document.paragraphs]
    assert Decimal(
        result["result_json"]["outputs"]["t1"]["aggregate"]["columns"]["금액"]["sum"]
    ) == Decimal("300.00")
    assert provenance["source_versions"] == {source["id"]: source["sha256"]}
    check_source(workspace, source, raw, receipt)


def test_w03_docx_meeting_actions_keep_unknown_fields_and_source_locations(workspace):
    document = Document()
    for line in ["제품 출시 회의", "자료 검토 | 담당자: 민지 | 기한: 2026-09-10", "견적 확인"]:
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    raw = buffer.getvalue()
    source, run = source_and_run(workspace, "회의록.docx", raw, "회의록에서 할 일 뽑아줘", "docx")

    def output(payload):
        items = sent_evidence(payload)
        known = next(item for item in items if "자료 검토" in item["quote"])
        unknown = next(item for item in items if item["quote"] == "견적 확인")
        return {
            "actions": [
                {
                    "action": "자료 검토",
                    "owner": "민지",
                    "due_date": "2026-09-10",
                    "due_text": "2026-09-10",
                    "evidence_ids": [known["evidence_id"]],
                },
                {
                    "action": "견적 확인",
                    "owner": None,
                    "due_date": None,
                    "due_text": None,
                    "evidence_ids": [unknown["evidence_id"]],
                },
            ]
        }

    result, receipt, provenance, created = finish(workspace, run, "meeting_actions", "L", output)
    reopened = Document(io.BytesIO(created))
    rows = [[cell.text for cell in row.cells] for row in reopened.tables[0].rows]
    assert rows == [
        ["할 일", "담당자", "기한"],
        ["자료 검토", "민지", "2026-09-10"],
        ["견적 확인", "미정", "미정"],
    ]
    actions = result["result_json"]["outputs"]["t2"]["actions"]
    assert actions[1]["owner"] is None and actions[1]["due_date"] is None
    text = "\n".join(paragraph.text for paragraph in reopened.paragraphs)
    assert "회의록.docx · docx:p:1" in text and "회의록.docx · docx:p:2" in text
    assert "explicit_meeting_fields" in provenance["content_proof"]["validation"]["checked_layers"]
    check_source(workspace, source, raw, receipt)


def test_w04_project_search_answers_with_date_and_excludes_foreign_source(workspace):
    raw = "제안서 결정: 기존 제안을 유지한다. 결정일: 2026-09-03.\n".encode()
    source, run = source_and_run(
        workspace, "제안서.md", raw, "지난번 '제안서 결정' 결정 찾아줘", "md", attach=False
    )
    conn, home = workspace
    foreign = Actor(id="other", project_ids=["foreign"])
    import_source(
        home,
        conn,
        io.BytesIO("제안서 결정: 외부 프로젝트만의 내용".encode()),
        {"filename": "타사.md"},
        foreign,
        "foreign",
    )

    def output(payload):
        items = sent_evidence(payload)
        assert len(items) == 1 and items[0]["artifact_id"] == source["id"]
        answer = "2026-09-03에 기존 제안을 유지하기로 결정했습니다."
        return {
            "answer": {
                "found": True,
                "text": [{"claim_id": "fact1"}],
                "claims": [fact(items[0], answer)],
            }
        }

    _, receipt, provenance, created = finish(workspace, run, "search_answer", "L", output)
    text = created.decode("utf-8")
    assert "2026-09-03에 기존 제안을 유지하기로 결정했습니다." in text
    assert "제안서.md · text:line:1" in text and "외부 프로젝트" not in text
    assert provenance["source_versions"] == {source["id"]: source["sha256"]}
    check_source(workspace, source, raw, receipt)


def test_single_source_summary_is_one_light_call_and_reopens_as_markdown(workspace):
    raw = "고객은 납기 확인을 요청했다.\n담당 부서가 일정을 검토한다.\n".encode()
    source, run = source_and_run(workspace, "업무메모.md", raw, "문서 요약해줘", "md")

    def output(payload):
        items = sent_evidence(payload)
        assert len(items) == 2
        return {
            "sections": {
                "content": {
                    "title": [{"claim_id": "title1"}],
                    "paragraphs": [[{"claim_id": "fact1"}]],
                },
                "claims": [
                    {
                        "id": "title1",
                        "text": "업무 요약",
                        "kind": "inference",
                        "evidence_ids": [],
                    },
                    {
                        "id": "fact1",
                        "text": "고객의 납기 확인 요청에 따라 담당 부서가 일정을 검토한다.",
                        "kind": "fact",
                        "evidence_ids": [item["evidence_id"] for item in items],
                    },
                ],
            }
        }

    _, receipt, _, created = finish(workspace, run, "summarize", "L", output)
    text = created.decode("utf-8")
    assert "고객의 납기 확인 요청에 따라 담당 부서가 일정을 검토한다." in text
    assert "업무메모.md · text:line:1" in text and "업무메모.md · text:line:2" in text
    check_source(workspace, source, raw, receipt)


def test_reply_draft_reopens_as_docx_with_unknown_recipient_and_no_send(workspace):
    raw = "안녕하세요. 행사 안내문을 검토해 주세요.\n검토 결과를 회신 부탁드립니다.\n".encode()
    source, run = source_and_run(workspace, "받은메일.txt", raw, "메일 답장 초안 작성해줘", "docx")

    def output(payload):
        item = next(item for item in sent_evidence(payload) if "안내문" in item["quote"])
        return {
            "draft": {
                "subject": [{"claim_id": "subject1"}],
                "recipient": None,
                "body": [{"claim_id": "fact1"}, {"claim_id": "reply1"}],
                "claims": [
                    {
                        "id": "subject1",
                        "text": "행사 안내문 검토 회신 초안",
                        "kind": "inference",
                        "evidence_ids": [],
                    },
                    fact(item, "안녕하세요. 행사 안내문 검토 요청을 확인했습니다. "),
                    {
                        "id": "reply1",
                        "text": "검토 후 회신드리겠습니다.",
                        "kind": "inference",
                        "evidence_ids": [],
                    },
                ],
            }
        }

    _, receipt, _, created = finish(workspace, run, "draft_reply", "M", output)
    text = "\n".join(paragraph.text for paragraph in Document(io.BytesIO(created)).paragraphs)
    assert "수신인: 미정" in text and "행사 안내문 검토 요청을 확인했습니다." in text
    assert "받은메일.txt · text:line:1" in text
    check_source(workspace, source, raw, receipt)


def test_w04_no_search_hit_returns_unconfirmed_without_fabricated_sources(workspace):
    source, run = source_and_run(
        workspace,
        "메모.md",
        "사무용품 재고를 확인한다.\n".encode(),
        "지난번 '제안서 결정' 결정 찾아줘",
        "md",
        attach=False,
    )

    def output(payload):
        assert sent_evidence(payload) == []
        return {
            "answer": {
                "found": False,
                "text": [{"uncertainty_id": "u1"}],
                "claims": [],
                "uncertainties": [{"id": "u1", "text": "근거 없음", "evidence_ids": []}],
            }
        }

    _, receipt, provenance, created = finish(workspace, run, "search_answer", "L", output)
    text = created.decode("utf-8")
    assert "미확정: 근거 없음" in text and "근거를 찾지 못했습니다" in text
    assert receipt["source_versions"] == {} and provenance["evidence_refs"] == []
    assert source["id"] not in canonical_json(provenance)
