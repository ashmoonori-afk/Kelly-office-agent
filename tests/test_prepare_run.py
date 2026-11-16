import io
import json
from datetime import UTC, datetime

import pytest

from kelly.artifacts import import_source
from kelly.contracts import (
    Actor,
    AppError,
    ModelBinding,
    ModelResult,
    PrivacyPolicy,
    RunRequest,
    TaskSpec,
)
from kelly.db import create_run, get_run, get_value, init_db, supply_inputs
from kelly.engine import execute_next, request_cancel
from kelly.planning import prepare_run
from kelly.tools import registry

OWNER = Actor(id="owner", project_ids=["sales"])
POLICY = PrivacyPolicy(project_id="sales", allow_cloud=True, allowed_providers=["test"])


def binding():
    return ModelBinding(
        provider="test",
        model_id="qualified",
        roles=["L", "M", "H"],
        enabled=True,
        available=True,
        allowed_projects=["sales"],
        capabilities=["structured_output", "planning", "analyze"],
        quality_evidence_id="test-fixture",
        context_window=32768,
        max_output=2048,
    )


def planned():
    condition = "두 대안을 비교하고 근거를 표시"
    return {
        "task_spec": {"intent": "compare", "source_ids": [], "acceptance": [condition]},
        "plan": {
            "requirements": [{"id": "r1", "text": condition}],
            "todos": [
                {
                    "id": "t1",
                    "title": "대안 비교",
                    "requirement_ids": ["r1"],
                    "kind": "simple",
                    "reason": "대안 간 조건을 비교한다",
                    "signals": ["alternative_evaluation"],
                    "clarity": "clear",
                    "usefulness": "required",
                    "executor": {"type": "model", "role": "L", "task": "analyze"},
                    "output_schema": "ReportSectionsV1",
                    "done_when": [condition],
                }
            ],
        },
    }


def result(output=None):
    return ModelResult(parsed_output=output or planned(), finish_reason="stop", latency_ms=1)


def setup(tmp_path, request="두 대안을 비교하고 근거를 표시해 줘"):
    conn = init_db(tmp_path / "state.db")
    run = create_run(conn, OWNER, "ui", "test", RunRequest(request=request, project_id="sales"))
    return conn, run, registry(tmp_path)


def prepare(conn, home, run, tools, complete):
    return prepare_run(
        conn,
        home,
        run["id"],
        OWNER,
        tools=tools,
        capability_catalog=[binding()],
        privacy_policy=POLICY,
        complete=complete,
    )


def test_one_planning_response_also_assigns_roles_and_does_not_execute(tmp_path):
    conn, run, tools = setup(tmp_path)
    calls = []

    def complete(request, selected, context):
        calls.append(context["purpose"])
        assert selected.role == "M"
        assert set(request.output_schema["properties"]) == {"plan", "task_spec"}
        return result()

    prepared = prepare(conn, tmp_path, run, tools, complete)
    todo = prepared["plan_json"]["current"]["todos"][0]
    assert calls == ["plan"] and prepared["state"] == "running"
    assert todo["kind"] == "complex" and todo["executor"]["role"] == "H"
    assert get_value(conn, "route_bindings:" + run["id"])["t1"]["role"] == "H"
    assert conn.execute("SELECT attempt_count FROM steps").fetchone()[0] == 0
    assert prepare(conn, tmp_path, prepared, tools, complete) == prepared
    assert calls == ["plan"]


def test_known_recipe_uses_actual_inspection_and_zero_planning_calls(tmp_path):
    conn = init_db(tmp_path / "state.db")
    artifact = import_source(
        tmp_path,
        conn,
        io.BytesIO("부서,매출\n영업,20\n영업,30\n".encode()),
        {"filename": "sales.csv"},
        OWNER,
        "sales",
    )
    run = create_run(
        conn,
        OWNER,
        "ui",
        "aggregate",
        RunRequest(
            request="부서별 매출 합계 내줘",
            project_id="sales",
            artifact_ids=[artifact["id"]],
            output_format="xlsx",
        ),
    )
    prepared = prepare_run(
        conn,
        tmp_path,
        run["id"],
        OWNER,
        tools=registry(tmp_path),
        capability_catalog=[],
        privacy_policy=PrivacyPolicy(project_id="sales"),
        complete=lambda *_: pytest.fail("Known recipe called a planner"),
    )
    assert (
        prepared["plan_json"]["current"]["todos"][0]["executor"]["tool_name"] == "table.aggregate"
    )
    assert get_value(conn, "route_bindings:" + run["id"]) == {}


@pytest.mark.parametrize(
    "failure",
    [
        "foreign_source",
        "missing_acceptance",
        "wrong_output",
        "price",
        "unsupported_capability",
        "missing_data",
    ],
)
def test_malformed_or_scope_expanding_plan_never_installs_steps(tmp_path, failure):
    conn, run, tools = setup(tmp_path)
    output = planned()
    if failure == "foreign_source":
        output["task_spec"]["source_ids"] = ["unapproved"]
    elif failure == "missing_acceptance":
        output["plan"]["todos"][0]["done_when"] = ["대충 완료"]
    elif failure == "wrong_output":
        output["plan"]["todos"][0]["output_schema"] = "ReceiptV1"
    elif failure == "price":
        output["plan"]["todos"][0]["price"] = "cheap"
    elif failure == "unsupported_capability":
        output["task_spec"]["required_capabilities"] = ["ocr"]
    else:
        output["task_spec"]["ambiguity"] = "missing_input"
    with pytest.raises(AppError):
        prepare(conn, tmp_path, run, tools, lambda *_: result(output))
    assert conn.execute("SELECT count(*) FROM steps").fetchone()[0] == 0
    assert get_run(conn, run["id"], OWNER)["plan_json"] is None


def test_cancellation_during_provider_call_prevents_stale_plan_install(tmp_path):
    conn, run, tools = setup(tmp_path)

    def complete(*_):
        request_cancel(conn, run["id"], OWNER)
        return result()

    with pytest.raises(AppError) as error:
        prepare(conn, tmp_path, run, tools, complete)
    assert error.value.code == "CONFLICT"
    assert conn.execute("SELECT count(*) FROM steps").fetchone()[0] == 0
    assert get_run(conn, run["id"], OWNER)["state"] == "cancel_requested"


def test_privacy_blocks_planning_before_transport(tmp_path):
    conn, run, tools = setup(tmp_path)
    with pytest.raises(AppError) as error:
        prepare_run(
            conn,
            tmp_path,
            run["id"],
            OWNER,
            tools=tools,
            capability_catalog=[binding()],
            privacy_policy=PrivacyPolicy(project_id="sales"),
            complete=lambda *_: pytest.fail("Privacy denied before network"),
        )
    assert error.value.code == "POLICY_DENIED"


@pytest.mark.parametrize("use_producer", [False, True])
def test_planner_cannot_hide_complex_prose_inside_a_mechanical_writer(tmp_path, use_producer):
    conn, run, tools = setup(tmp_path)
    output = planned()
    analysis = output["plan"]["todos"][0]
    writer = {
        **analysis,
        "id": "t2",
        "title": "비교 문서 저장",
        "kind": "mechanical",
        "signals": ["fixed_transform"],
        "depends_on": ["t1"] if use_producer else [],
        "inputs": [{"step_id": "t1", "output_key": "sections"}] if use_producer else [],
        "executor": {
            "type": "tool",
            "tool_name": "document.create",
            "args": {
                "template_id": "office-report-v1",
                **(
                    {"input_ref": "t1"}
                    if use_producer
                    else {
                        "content": {"title": "분석", "paragraphs": ["근거 없이 확정한 원인 분석"]}
                    }
                ),
            },
        },
        "output_schema": "ArtifactV1",
    }
    output["plan"]["todos"] = [analysis, writer] if use_producer else [writer]
    if use_producer:
        prepared = prepare(conn, tmp_path, run, tools, lambda *_: result(output))
        todos = prepared["plan_json"]["current"]["todos"]
        assert todos[0]["executor"]["role"] == "H"
        assert "model_grounding" in todos[0]["verify_with"]
        assert todos[1]["executor"]["args"]["input_ref"] == "t1"
    else:
        # Direct authenticated tool use keeps its explicit typed-content contract.
        tools["document.create"].args_schema.model_validate(writer["executor"]["args"])
        with pytest.raises(AppError) as error:
            prepare(conn, tmp_path, run, tools, lambda *_: result(output))
        assert error.value.code == "PLAN_INVALID"
        assert conn.execute("SELECT count(*) FROM steps").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    conn.close()


def test_actual_attachment_and_input_replan_preserve_completed_step_bytes(tmp_path):
    conn = init_db(tmp_path / "state.db")
    artifact = import_source(
        tmp_path,
        conn,
        io.BytesIO(b"team,sales\nA,100\n"),
        {"filename": "sales.csv"},
        OWNER,
        "sales",
    )
    run = create_run(
        conn,
        OWNER,
        "ui",
        "dynamic",
        RunRequest(
            request="두 대안을 비교하고 근거를 표시해 줘",
            project_id="sales",
            artifact_ids=[artifact["id"]],
        ),
    )
    tools = registry(tmp_path)
    output = planned()
    output["task_spec"]["source_ids"] = [artifact["id"]]
    analysis = output["plan"]["todos"][0]
    analysis.update(id="t2", clarity="missing_input", depends_on=["t1"])
    inspect = {
        **analysis,
        "id": "t1",
        "title": "자료 검사",
        "kind": "mechanical",
        "clarity": "clear",
        "signals": ["fixed_transform"],
        "depends_on": [],
        "inputs": [{"artifact_id": artifact["id"]}],
        "executor": {
            "type": "tool",
            "tool_name": "source.inspect",
            "args": {"artifact_id": artifact["id"]},
        },
        "output_schema": "SourceInfoV1",
    }
    output["plan"]["todos"] = [inspect, analysis]
    first = prepare(conn, tmp_path, run, tools, lambda *_: result(output))
    execute_next(conn, run["id"], actor=OWNER, tools=tools, complete=None, now=datetime.now(UTC))
    waiting = execute_next(
        conn, run["id"], actor=OWNER, tools=tools, complete=None, now=datetime.now(UTC)
    )
    assert waiting["state"] == "awaiting_input"
    old = dict(
        conn.execute("SELECT * FROM steps WHERE run_id=? AND ordinal=0", (run["id"],)).fetchone()
    )
    assert old["state"] == "succeeded"
    pending_before = dict(
        conn.execute("SELECT * FROM steps WHERE run_id=? AND ordinal=1", (run["id"],)).fetchone()
    )
    supplied = supply_inputs(
        conn,
        run["id"],
        OWNER,
        TaskSpec(
            intent="compare",
            source_ids=[artifact["id"]],
            inputs={"user_corrections": "계약안 A와 B를 비교"},
            acceptance=output["task_spec"]["acceptance"],
        ),
        waiting["revision"],
    )
    purposes = []

    def replan(request, binding, context):
        payload = json.loads(request.context.split("\nDATA\n", 1)[1])
        assert payload["completed_ids"] == ["t1"]
        assert payload["previous_plan"] == first["plan_json"]["current"]
        replacement = payload["previous_plan"]
        extraction = {
            **inspect,
            "id": "extract1",
            "title": "대안 비교용 원문 추출",
            "reason": "모델에 실제 원문 근거를 전달한다",
            "depends_on": ["t1"],
            "executor": {
                "type": "tool",
                "tool_name": "source.extract",
                "args": {"artifact_id": artifact["id"]},
            },
            "output_schema": "EvidenceListV1",
            "done_when": ["제한된 범위의 원문 근거를 추출"],
        }
        replacement["todos"][1].update(
            clarity="clear",
            depends_on=["t1", "extract1"],
            inputs=[{"step_id": "extract1", "output_key": "evidence"}],
        )
        replacement["todos"].insert(1, extraction)
        purposes.append(context["purpose"])
        return result({"task_spec": payload["task_spec"], "plan": replacement})

    resumed = prepare(conn, tmp_path, supplied, tools, replan)
    assert purposes == ["replan"] and resumed["state"] == "running"
    assert dict(conn.execute("SELECT * FROM steps WHERE id=?", (old["id"],)).fetchone()) == old
    current = resumed["plan_json"]["current"]["todos"]
    assert [item["id"] for item in current] == ["t1", "extract1", "t2"]
    assert current[0] == first["plan_json"]["current"]["todos"][0]
    assert current[1]["executor"]["tool_name"] == "source.extract"
    assert current[2]["depends_on"] == ["t1", "extract1"]
    assert current[2]["inputs"] == [{"step_id": "extract1", "output_key": "evidence"}]
    pending_after = dict(
        conn.execute("SELECT * FROM steps WHERE id=?", (pending_before["id"],)).fetchone()
    )
    assert pending_after["ordinal"] == 2
    assert all(
        pending_after[key] == value
        for key, value in pending_before.items()
        if key not in {"ordinal", "spec_json"}
    )
    assert resumed["request_json"] == run["request_json"]
