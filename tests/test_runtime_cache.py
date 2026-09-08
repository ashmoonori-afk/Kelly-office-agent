"""Extraction cache integration with real files and a counting, non-stubbing parser wrapper."""

import io
from datetime import UTC, datetime, timedelta

import pytest

from birkin_lite import office
from birkin_lite import tools as tool_module
from birkin_lite.artifacts import import_source
from birkin_lite.context import extraction_cache_key
from birkin_lite.contracts import Actor, AppError, PlanV1, RunRequest, canonical_json
from birkin_lite.db import create_run, get_run, get_value, init_db, row_dict, set_value
from birkin_lite.engine import execute_next, install_plan
from birkin_lite.tools import ExtractArgs, registry

OWNER = Actor(id="owner", project_ids=["sales"])


@pytest.fixture
def prepare(tmp_path, monkeypatch):
    connections = []

    def make(raw=None):
        if raw is None:
            raw = "자료 검토를 진행한다.\n".encode()
        conn, home = init_db(tmp_path / "state.db"), tmp_path / "files"
        connections.append(conn)
        source = import_source(
            home, conn, io.BytesIO(raw), {"filename": "자료.txt"}, OWNER, "sales"
        )
        run = create_run(
            conn,
            OWNER,
            "ui",
            "cache-test",
            RunRequest(request="문서 요약해줘", project_id="sales", artifact_ids=[source["id"]]),
        )
        calls = []
        actual_parser = office.parse_file

        def counted_parser(*args, **kwargs):
            calls.append(kwargs.get("operation"))
            return actual_parser(*args, **kwargs)

        monkeypatch.setattr(office, "parse_file", counted_parser)
        context = {"conn": conn, "home": home, "actor": OWNER, "run": run, "inputs": {}}
        return context, source, registry(home)["source.extract"], calls

    yield make
    for conn in connections:
        conn.close()


def cache_record(context, source, args=None):
    value = ExtractArgs.model_validate({"artifact_id": source["id"], **(args or {})})
    run = get_run(context["conn"], context["run"]["id"], OWNER)
    key = "cache:" + extraction_cache_key(
        OWNER,
        "sales",
        {source["id"]: source["sha256"]},
        parser_version=office.EXTRACTION_VERSION,
        projection=value.projection.model_dump(exclude_none=True),
        limits=value.limits.model_dump(),
        scope_revision=run["policy_json"].get("scope_revision", 1),
    )
    return key, get_value(context["conn"], key)


def test_non_table_inspect_reuses_verified_import_but_checks_bytes_and_parser_version(
    prepare, monkeypatch
):
    raw = "자료 검토를 진행한다.\n".encode()
    context, source, _, _ = prepare(raw)
    inspector = registry(context["home"])["source.inspect"]
    actual_parser, calls = tool_module.parse_file, []

    def counted_parser(*args, **kwargs):
        calls.append(kwargs.get("operation"))
        return actual_parser(*args, **kwargs)

    monkeypatch.setattr(tool_module, "parse_file", counted_parser)
    args = {"artifact_id": source["id"]}
    first = inspector.execute(args, context)
    second = inspector.execute(args, context)
    assert first == second and calls == []
    assert first["source"]["sha256"] == source["sha256"]
    path = context["home"] / source["relative_path"]
    path.write_bytes("변경한 실제 파일".encode())
    with pytest.raises(AppError) as error:
        inspector.execute(args, context)
    assert error.value.code == "SOURCE_CHANGED" and calls == []
    path.write_bytes(raw)
    provenance = source["provenance_json"]
    provenance["inspection"]["parser_version"] = "old-preflight-version"
    context["conn"].execute(
        "UPDATE artifacts SET provenance_json=? WHERE id=?",
        (canonical_json(provenance), source["id"]),
    )
    inspected = inspector.execute(args, context)
    assert calls == ["inspect"] and inspected["source"]["sha256"] == source["sha256"]


def test_repeat_extract_reuses_exact_output_and_has_fixed_day_ttl(prepare):
    context, source, tool, calls = prepare()
    before = datetime.now(UTC)
    first = tool.execute({"artifact_id": source["id"]}, context)
    after = datetime.now(UTC)
    second = tool.execute({"artifact_id": source["id"]}, context)
    assert calls == ["extract"] and second == first
    assert get_value(context["conn"], "cache_observation:" + context["run"]["id"]) == {
        "lookups": 2,
        "hits": 1,
    }
    assert first["evidence"]["items"][0]["quote"] == "자료 검토를 진행한다."
    _, stored = cache_record(context, source)
    expiry = datetime.fromisoformat(stored["expires_at"])
    assert before + timedelta(hours=24) <= expiry <= after + timedelta(hours=24)
    assert stored["source_versions"] == {source["id"]: source["sha256"]}
    assert stored["actor"] == "owner" and stored["project"] == "sales"


@pytest.mark.parametrize("change", ["projection", "limits", "scope_revision", "parser_version"])
def test_changed_extract_contract_or_current_scope_cannot_reuse_previous_result(
    prepare, monkeypatch, change
):
    context, source, tool, calls = prepare()
    args = {"artifact_id": source["id"]}
    tool.execute(args, context)
    if change == "projection":
        args["projection"] = {"page": True}
    elif change == "limits":
        args["limits"] = {"max_chars_per_span": 8000}
    elif change == "scope_revision":
        policy = {**context["run"]["policy_json"], "scope_revision": 2}
        context["conn"].execute(
            "UPDATE runs SET policy_json=? WHERE id=?",
            (canonical_json(policy), context["run"]["id"]),
        )
        assert context["run"]["policy_json"].get("scope_revision", 1) == 1  # Stale caller snapshot.
    else:
        monkeypatch.setattr(office, "EXTRACTION_VERSION", "office-extract-test-v2")
    tool.execute(args, context)
    tool.execute(args, context)
    assert calls == ["extract", "extract"]


@pytest.mark.parametrize("change,code", [("tamper", "SOURCE_CHANGED"), ("delete", "NOT_FOUND")])
def test_cached_extraction_never_returns_changed_or_deleted_actual_source(prepare, change, code):
    context, source, tool, calls = prepare()
    tool.execute({"artifact_id": source["id"]}, context)
    if change == "tamper":
        (context["home"] / source["relative_path"]).write_text("원본 변경", encoding="utf-8")
    else:
        context["conn"].execute(
            "UPDATE artifacts SET deleted_at=? WHERE id=?",
            (datetime.now(UTC).isoformat(), source["id"]),
        )
    with pytest.raises(AppError) as error:
        tool.execute({"artifact_id": source["id"]}, context)
    assert error.value.code == code and calls == ["extract"]


def test_expired_cached_result_is_extracted_again(prepare):
    context, source, tool, calls = prepare()
    tool.execute({"artifact_id": source["id"]}, context)
    key, stored = cache_record(context, source)
    stored["expires_at"] = "2000-01-01T00:00:00Z"
    set_value(context["conn"], key, stored)
    result = tool.execute({"artifact_id": source["id"]}, context)
    assert calls == ["extract", "extract"]
    assert get_value(context["conn"], "cache_observation:" + context["run"]["id"]) == {
        "lookups": 2,
        "hits": 0,
    }
    assert result["evidence"]["items"][0]["quote"] == "자료 검토를 진행한다."
    assert datetime.fromisoformat(get_value(context["conn"], key)["expires_at"]) > datetime.now(UTC)


def test_cached_evidence_still_runs_mandatory_locator_verification(prepare):
    context, source, tool, calls = prepare()
    tool.execute({"artifact_id": source["id"]}, context)
    key, stored = cache_record(context, source)
    stored["value"]["evidence"]["items"][0]["quote"] = "캐시에만 있는 거짓 내용"
    set_value(context["conn"], key, stored)
    plan = PlanV1.model_validate(
        {
            "requirements": [{"id": "r1", "text": "근거 추출"}],
            "todos": [
                {
                    "id": "t1",
                    "title": "근거 추출",
                    "requirement_ids": ["r1"],
                    "inputs": [{"artifact_id": source["id"]}],
                    "kind": "mechanical",
                    "reason": "고정 자료 추출",
                    "signals": ["fixed_transform"],
                    "clarity": "clear",
                    "usefulness": "required",
                    "executor": {
                        "type": "tool",
                        "tool_name": "source.extract",
                        "args": {"artifact_id": source["id"]},
                    },
                    "output_schema": "EvidenceListV1",
                    "done_when": ["실제 자료와 일치"],
                    "verify_with": [],
                }
            ],
        }
    )
    conn, tools = context["conn"], registry(context["home"])
    install_plan(conn, context["run"]["id"], plan, OWNER, tools=tools)
    result = execute_next(
        conn,
        context["run"]["id"],
        actor=OWNER,
        tools=tools,
        complete=None,
        home=context["home"],
        now=datetime.now(UTC),
    )
    step = row_dict(conn.execute("SELECT * FROM steps").fetchone())
    assert calls == ["extract", "locate"]
    assert result["state"] == "failed" and step["validation_json"]["status"] == "fail"
    assert "EVIDENCE_MISMATCH" in step["validation_json"]["errors"]
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    assert not conn.execute("SELECT 1 FROM artifacts WHERE kind='result'").fetchone()


def test_large_valid_extraction_is_returned_in_full_without_cache(prepare):
    raw = (("a" * 12000 + "\n") * 6).encode()
    context, source, tool, calls = prepare(raw)
    args = {"artifact_id": source["id"], "limits": {"max_chars_per_span": 16000}}
    first = tool.execute(args, context)
    second = tool.execute(args, context)
    assert len(canonical_json(first).encode("utf-8")) > 65536
    assert calls == ["extract", "extract"]
    assert [item["quote"] for item in first["evidence"]["items"]] == ["a" * 12000] * 6
    assert [item["quote"] for item in second["evidence"]["items"]] == ["a" * 12000] * 6
    assert cache_record(context, source, {"limits": {"max_chars_per_span": 16000}})[1] is None


def test_cache_text_guard_does_not_change_requested_source_content(prepare):
    content = r"문서 위치: C:\Office\memo.txt"
    context, source, tool, calls = prepare(content.encode())
    output = tool.execute({"artifact_id": source["id"]}, context)
    assert calls == ["extract"] and output["evidence"]["items"][0]["quote"] == content
    assert cache_record(context, source)[1] is None
