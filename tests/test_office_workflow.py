import copy
import io
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from kelly.artifacts import import_source, metadata, open_artifact
from kelly.contracts import Actor, AppError, PlanV1, RunRequest
from kelly.db import create_run, get_run, init_db
from kelly.engine import execute_next, install_plan
from kelly.tools import registry
from kelly.verification import numeric_grounding, source_grounding
from kelly.verification import registry as verifier_registry

OWNER = Actor(id="owner", project_ids=["sales"])
NOW = datetime(2026, 9, 8, tzinfo=UTC)


def workflow(artifact_id):
    tasks = [
        (
            "table.aggregate",
            "AggregateV1",
            {
                "artifact_id": artifact_id,
                "group_by": "부서",
                "value_columns": ["금액"],
                "policy": {"unit": "KRW"},
            },
            [],
            ["numeric_grounding"],
        ),
        (
            "document.create",
            "ArtifactV1",
            {"template_id": "table-v1", "input_ref": "t1"},
            [{"step_id": "t1", "output_key": "aggregate"}],
            ["report_structure", "source_grounding"],
        ),
        (
            "artifact.validate",
            "ValidationResultV1",
            {"input_ref": "t2", "checks": ["preflight", "report_structure", "source_grounding"]},
            [{"step_id": "t2", "output_key": "artifact"}],
            [],
        ),
        (
            "artifact.publish",
            "ReceiptV1",
            {"input_ref": "t2"},
            [{"step_id": "t2", "output_key": "artifact"}],
            [],
        ),
    ]
    todos = []
    for i, (tool, schema, args, inputs, checks) in enumerate(tasks, 1):
        depends = [ref["step_id"] for ref in inputs]
        if i == 4:
            depends.append("t3")
        todos.append(
            {
                "id": f"t{i}",
                "title": tool,
                "requirement_ids": ["r1"],
                "depends_on": depends,
                "inputs": inputs,
                "kind": "mechanical",
                "reason": "확정된 표의 계산과 파일 생성",
                "signals": ["fixed_transform"],
                "clarity": "clear",
                "usefulness": "required",
                "executor": {"type": "tool", "tool_name": tool, "args": args},
                "output_schema": schema,
                "done_when": ["숫자와 파일 구조 확인"],
                "verify_with": checks,
            }
        )
    return PlanV1.model_validate(
        {"requirements": [{"id": "r1", "text": "부서별 합계표"}], "todos": todos}
    )


def setup(tmp_path, raw, *, output_format="xlsx"):
    conn, home = init_db(tmp_path / "state.db"), tmp_path / "files"
    source = import_source(home, conn, io.BytesIO(raw), {"filename": "매출.csv"}, OWNER, "sales")
    run = create_run(
        conn,
        OWNER,
        "ui",
        "one",
        RunRequest(
            request="부서별로 금액을 합쳐줘",
            project_id="sales",
            artifact_ids=[source["id"]],
            output_format=output_format,
        ),
    )
    return conn, home, source, run


def test_w01_real_100000_rows_to_xlsx_and_receipt_without_models(tmp_path):
    raw = ("부서,금액\n" + "영업,0.10\n지원,0.20\n" * 50000).encode()
    conn, home, source, run = setup(tmp_path, raw)
    tools = registry(home)
    plan = workflow(source["id"])
    install_plan(conn, run["id"], plan, OWNER, tools=tools)
    seen = []

    def no_model(*args):
        seen.append(args)
        raise AssertionError("W01 must not invoke a model")

    for _ in range(5):
        result = execute_next(
            conn,
            run["id"],
            actor=OWNER,
            tools=tools,
            complete=no_model,
            now=NOW,
            verifiers=verifier_registry(home),
        )
    assert result["state"] == "succeeded", result.get("error_code")
    assert seen == [] and conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    outputs = result["result_json"]["outputs"]
    aggregate = outputs["t1"]["aggregate"]
    assert aggregate["row_counts"]["included"] == 100000
    assert Decimal(aggregate["columns"]["금액"]["sum"]) == Decimal("15000.00")
    assert {group["key"]: Decimal(group["sums"]["금액"]) for group in aggregate["groups"]} == {
        "영업": Decimal("5000.00"),
        "지원": Decimal("10000.00"),
    }
    receipt = outputs["t4"]["receipt"]
    record = metadata(conn, receipt["artifact_id"], OWNER, "sales")
    with open_artifact(home, conn, record["id"], OWNER, "sales") as handle:
        book = load_workbook(handle, read_only=True)
        values = list(book.active.values)
        book.close()
    assert values[0] == ("그룹", "금액 (KRW)")
    assert {row[0]: Decimal(str(row[1])) for row in values[1:]} == {
        "영업": Decimal("5000.00"),
        "지원": Decimal("10000.00"),
    }
    with open_artifact(home, conn, source["id"], OWNER, "sales") as handle:
        assert handle.read() == raw
    assert receipt["artifact_hash"] == record["sha256"]
    assert "source_versions" in record["provenance_json"]["sources"]
    execute_next(conn, run["id"], actor=OWNER, tools=tools, complete=no_model, now=NOW)
    assert conn.execute("SELECT count(*) FROM artifacts WHERE kind='result'").fetchone()[0] == 1
    conn.close()


def test_actual_numeric_verifier_rejects_modified_sum(tmp_path):
    conn, home, source, run = setup(tmp_path, "부서,금액\n영업,0.10\n영업,0.20\n".encode())
    plan = workflow(source["id"])
    context = {
        "home": home,
        "conn": conn,
        "actor": OWNER,
        "run": run,
        "todo": plan.todos[0],
        "inputs": {},
    }
    output = registry(home)["table.aggregate"].execute(plan.todos[0].executor.args, context)
    assert numeric_grounding(output, context).status == "pass"
    changed = copy.deepcopy(output)
    changed["aggregate"]["columns"]["금액"]["sum"] = "999.00"
    assert numeric_grounding(changed, context).status == "fail"
    conn.close()


def test_evidence_locator_is_reloaded_from_actual_source(tmp_path):
    from kelly import office

    conn, home, source, run = setup(tmp_path, "부서,금액\n영업,0.10\n".encode())
    context = {"home": home, "conn": conn, "actor": OWNER, "run": run, "inputs": {}}
    inspected = registry(home)["source.inspect"].execute({"artifact_id": source["id"]}, context)
    assert inspected["source"]["headers"] == ["부서", "금액"]
    assert inspected["source"]["sample_types"]["금액"]["kinds"] == ["number"]
    evidence = office.locate(source["id"], "csv:row:2:col:2", context)
    assert evidence.quote == "0.10"
    output = {"evidence": {"items": [evidence.model_dump()]}}
    assert source_grounding(output, context).status == "pass"
    output["evidence"]["items"][0]["quote"] = "12345"
    assert source_grounding(output, context).status == "fail"
    conn.close()


def test_tool_arguments_rejected_before_partial_execution(tmp_path):
    conn, home, source, run = setup(tmp_path, b"group,value\na,1\n")
    plan = workflow(source["id"])
    plan.todos[1].executor.args["actor_id"] = "injected"
    with pytest.raises(AppError) as error:
        install_plan(conn, run["id"], plan, OWNER, tools=registry(home))
    assert error.value.code == "INVALID_INPUT"
    assert conn.execute("SELECT count(*) FROM steps").fetchone()[0] == 0
    assert get_run(conn, run["id"], OWNER)["state"] == "queued"
    conn.close()


@pytest.mark.parametrize("value", ["", "확인 필요"])
def test_missing_required_numeric_value_waits_without_publishing(tmp_path, value):
    conn, home, source, run = setup(tmp_path, f"부서,금액\n영업,{value}\n".encode())
    plan = workflow(source["id"])
    plan.todos[0].executor.args["policy"]["blank_policy"] = "error"
    tools = registry(home)
    install_plan(conn, run["id"], plan, OWNER, tools=tools)
    result = execute_next(
        conn,
        run["id"],
        actor=OWNER,
        tools=tools,
        complete=None,
        now=NOW,
        verifiers=verifier_registry(home),
    )
    assert result["state"] == "awaiting_input"
    assert result["error_code"] == "MISSING_INPUT"
    assert (
        conn.execute("SELECT count(*) FROM artifacts WHERE kind IN ('draft','result')").fetchone()[
            0
        ]
        == 0
    )
    conn.close()
