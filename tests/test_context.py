import io
import json
from pathlib import Path

import pytest

from kelly.artifacts import import_source
from kelly.context import (
    build_model_request,
    build_plan_request,
    cache_get,
    cache_put,
    estimate_input_tokens,
    extraction_cache_key,
    response_cache_key,
)
from kelly.contracts import (
    Actor,
    AppError,
    ModelBinding,
    PlanV1,
    Requirement,
    RunRequest,
    TodoV1,
    canonical_json,
)
from kelly.db import create_run, init_db
from kelly.tools import catalog, registry
from kelly.tools import inspect as inspect_source


def binding():
    return ModelBinding(
        provider="test",
        model_id="test-model",
        roles=["L", "M", "H"],
        role="M",
        enabled=True,
        available=True,
        allowed_projects=["sales"],
        capabilities=["planning", "summarize", "meeting_actions"],
        context_window=32768,
        max_output=2048,
    )


def todo(task="meeting_actions"):
    return TodoV1(
        id="t2",
        title="회의록 정리",
        requirement_ids=["r1"],
        kind="standard",
        reason="확정된 발언을 정리한다",
        signals=["synthesis"],
        clarity="clear",
        usefulness="required",
        executor={"type": "model", "role": "M", "task": task},
        output_schema="MeetingActionsV1" if task == "meeting_actions" else "ReportSectionsV1",
        done_when=["근거가 있는 항목만 포함"],
    )


def evidence(number=1):
    return {
        "artifact_id": "source1",
        "sha256": "a" * 64,
        "locator": f"text:line:{number}",
        "quote": "민수는 내일까지 계약을 검토한다.",
        "extraction_version": "office-extract-v1",
        "observed_at": "2026-09-08T00:00:00Z",
    }


def test_planning_is_bounded_single_envelope_with_catalog_and_no_runtime_state(tmp_path):
    request = RunRequest(request="회의록을 검토하여 실행안을 작성해 줘", project_id="sales")
    first = build_plan_request(request, [], catalog(registry(tmp_path)), binding=binding())
    second = build_plan_request(
        request.model_copy(update={"request": "의견을 비교해 줘"}),
        [],
        catalog(registry(tmp_path)),
        binding=binding(),
    )
    assert first.context.split("\nDATA\n")[0] == second.context.split("\nDATA\n")[0]
    assert set(first.output_schema["properties"]) == {"task_spec", "plan"}
    assert first.output_schema["additionalProperties"] is False
    assert estimate_input_tokens(first) <= 8000
    assert "source.extract" in first.context
    assert "budget" not in first.output_schema["properties"]


def test_only_task_state_and_evidence_sent_and_overhead_is_counted():
    context = {
        "home": Path("C:/private"),
        "secret": "must-not-send",
        "history": ["old-secret"],
        "run": {"request_json": {"request": "회의록의 할 일을 알려 줘"}},
        "task_state": {"confirmed_facts": ["발언은 확정본"], "history": ["old-secret"]},
    }
    result = build_model_request(
        todo(), {"t1": {"items": [evidence()]}}, context, binding=binding()
    )
    assert "must-not-send" not in result.context and "old-secret" not in result.context
    assert "C:/private" not in result.context
    assert "evidence_id" in result.context and "text:line:1" in result.context
    serialized = canonical_json(result).encode("utf-8")
    ascii_bytes = sum(byte < 128 for byte in serialized)
    assert (
        estimate_input_tokens(result)
        == (ascii_bytes + 1) // 2 + len(serialized) - ascii_bytes + 256
    )
    assert set(result.output_schema["properties"]) == {"actions"}
    with pytest.raises(AppError) as error:
        build_model_request(
            todo(), {"t1": {"items": [evidence(i) for i in range(7)]}}, context, binding=binding()
        )
    assert error.value.code == "CONTEXT_LIMIT"
    with pytest.raises(AppError) as error:
        build_model_request(
            todo(),
            {"t1": {"items": [{**evidence(), "quote": "한" * 3000}]}},
            context,
            binding=binding(),
        )
    assert error.value.code == "CONTEXT_LIMIT"


def test_actual_csv_metadata_fits_new_planning_context(tmp_path):
    actor = Actor(id="owner", project_ids=["sales"])
    conn, home = init_db(tmp_path / "state.db"), tmp_path / "files"
    try:
        source = import_source(
            home,
            conn,
            io.BytesIO("부서,매출\n영업,100\n".encode()),
            {"filename": "sales.csv"},
            actor,
            "sales",
        )
        request = RunRequest(
            request="자료를 비교해 제안서를 작성해 줘",
            project_id="sales",
            artifact_ids=[source["id"]],
        )
        run = create_run(conn, actor, "ui", "new-plan", request)
        metadata = inspect_source(
            {"artifact_id": source["id"]}, {"home": home, "conn": conn, "actor": actor, "run": run}
        )["source"]
        result = build_plan_request(request, [metadata], catalog(registry(home)), binding=binding())
        payload = json.loads(result.context.split("\nDATA\n", 1)[1])
        assert payload["sources"][0]["sha256"] == source["sha256"]
        assert payload["sources"][0]["headers"] == ["부서", "매출"]
        assert estimate_input_tokens(result) <= 8000
        assert len(canonical_json(result).encode()) <= 65536
    finally:
        conn.close()


def test_replan_prompt_preserves_exact_prior_objects_and_completed_ordinals(tmp_path):
    prior = PlanV1(
        requirements=[Requirement(id="r1", text="근거 기반 정리")],
        todos=[todo().model_copy(update={"id": "t1"}), todo()],
    )
    request = RunRequest(request="추가 자료도 반영해 줘", project_id="sales")
    result = build_plan_request(
        request,
        [],
        catalog(registry(tmp_path)),
        binding=binding(),
        previous_plan=prior,
        completed_ids=["t1"],
    )
    payload = json.loads(result.context.split("\nDATA\n", 1)[1])
    assert payload["previous_plan"] == prior.model_dump()
    assert payload["completed_ids"] == ["t1"]
    assert "same ordinal" in result.context and "full plan" in result.context
    assert estimate_input_tokens(result) <= 8000
    for previous, completed in [(None, ["t1"]), (prior, ["missing"]), (prior, ["t1", "t1"])]:
        with pytest.raises(AppError) as error:
            build_plan_request(
                request, [], [], binding=binding(), previous_plan=previous, completed_ids=completed
            )
        assert error.value.code == "PLAN_INVALID"


def test_partial_evidence_does_not_become_a_full_document_summary():
    partial = {"t1": {"items": [evidence()], "bounded_page": True}}
    with pytest.raises(AppError) as error:
        build_model_request(todo("summarize"), partial, {}, binding=binding())
    assert error.value.code == "MISSING_INPUT"
    partial["t1"]["bounded_page"] = False
    complete = build_model_request(todo("summarize"), partial, {}, binding=binding())
    assert "근거" in complete.context
    assert "title" in complete.output_schema["$defs"]["ReferenceContent"]["properties"]
    with pytest.raises(AppError) as error:
        build_model_request(
            todo(), {"t1": {"rows": [[1, 2]], "columns": ["a", "b"]}}, {}, binding=binding()
        )
    assert error.value.code == "MISSING_INPUT"


def test_model_window_and_sensitive_payload_and_numeric_evidence():
    selected = binding().model_copy(update={"context_window": 4096, "max_output": 3000})
    with pytest.raises(AppError) as error:
        build_model_request(todo(), {"t1": {"items": [evidence()]}}, {}, binding=selected)
    assert error.value.code == "CONTEXT_LIMIT"
    for unsafe in [{"api_key": "sensitive"}, {"text": "C:\\Users\\owner\\secret.txt"}]:
        with pytest.raises(AppError) as error:
            build_model_request(todo(), unsafe, {}, binding=binding())
        assert error.value.code == "POLICY_DENIED"
    aggregate = {
        "columns": {"매출": {"sum": "12.00", "locator": "csv:range:A2:A3"}},
        "groups": [],
        "row_counts": {"included": 2},
        "unit": "KRW",
    }
    value = build_model_request(
        todo("report_prose"),
        {"t1": aggregate, "t2": {"items": [{**evidence(), "locator": "csv:row:1:col:2"}]}},
        {},
        binding=binding(),
    )
    assert "12.00" in value.context and "csv:row:1:col:2" in value.context


def test_serialized_byte_hardcap_is_independent_of_token_estimate(monkeypatch):
    monkeypatch.setattr("kelly.context.CONTEXT_TARGET", 200000)
    large_window = binding().model_copy(update={"context_window": 200000})
    with pytest.raises(AppError) as error:
        build_model_request(
            todo(),
            {},
            {"task_state": {"confirmed_facts": ["x" * 65536]}},
            binding=large_window,
        )
    assert error.value.code == "CONTEXT_LIMIT"


def test_cache_exact_scope_expiry_versions_and_actual_file_identity(tmp_path):
    actor = Actor(id="owner", project_ids=["sales", "other"])
    conn = init_db(tmp_path / "state.db")
    home = tmp_path / "files"
    source = import_source(
        home, conn, io.BytesIO(b"meeting"), {"filename": "meeting.txt"}, actor, "sales"
    )
    versions = {source["id"]: source["sha256"]}
    options = {"parser_version": "v1", "projection": {"page": False}, "limits": {"spans": 6}}
    key = extraction_cache_key(actor, "sales", versions, **options)
    for changed in [
        {"parser_version": "v2"},
        {"projection": {"page": True}},
        {"limits": {"spans": 2}},
        {"scope_revision": 2},
    ]:
        assert key != extraction_cache_key(actor, "sales", versions, **(options | changed))
    cache_put(
        conn,
        home,
        actor,
        "sales",
        key,
        {"items": []},
        source_versions=versions,
        expires_at="2026-09-09T00:00:00Z",
        now="2026-09-08T00:00:00Z",
    )
    assert cache_get(
        conn, home, actor, "sales", key, source_versions=versions, now="2026-09-08T00:00:00Z"
    ) == {"items": []}
    assert (
        cache_get(
            conn, home, actor, "sales", key, source_versions=versions, now="2026-09-10T00:00:00Z"
        )
        is None
    )
    for denied_actor, project in [(actor, "other"), (Actor(id="owner", project_ids=[]), "sales")]:
        with pytest.raises(AppError):
            cache_get(
                conn,
                home,
                denied_actor,
                project,
                key,
                source_versions=versions,
                now="2026-09-08T00:00:00Z",
            )
    Path(home / source["relative_path"]).write_bytes(b"tampered")
    with pytest.raises(AppError) as error:
        cache_get(
            conn, home, actor, "sales", key, source_versions=versions, now="2026-09-08T00:00:00Z"
        )
    assert error.value.code == "SOURCE_CHANGED"
    conn.execute("UPDATE artifacts SET deleted_at='2026-09-08T00:00:00Z'")
    with pytest.raises(AppError):
        cache_get(
            conn, home, actor, "sales", key, source_versions=versions, now="2026-09-08T00:00:00Z"
        )
    conn.close()


def test_response_cache_versions_policy_binding_and_effect():
    actor = Actor(id="owner", project_ids=["sales"])
    request = build_model_request(todo(), {"t1": {"items": [evidence()]}}, {}, binding=binding())
    args = (actor, "sales", {"source1": "a" * 64}, request, binding())
    key = response_cache_key(*args, policy={"cloud": True})
    assert key != response_cache_key(*args, policy={"cloud": False})
    assert key != response_cache_key(*args, policy={"cloud": True}, scope_revision=2)
    changed = (*args[:-1], binding().model_copy(update={"model_id": "new-model"}))
    assert key != response_cache_key(*changed, policy={"cloud": True})
    with pytest.raises(AppError):
        response_cache_key(*args, policy={}, effect="external_write")
