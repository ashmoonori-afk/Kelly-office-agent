"""Mock HTTP only: manifest approval, real core routing, and evidence separation."""

import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from test_config import evidence, profile
from test_eval_comparison import CONTENT, case_and_plan

from evals import live
from evals.run import checks, fingerprint, read_json, sha
from kelly import config, db, office, parser_worker
from kelly.contracts import AppError, PrivacyPolicy


def configured(tmp_path):
    selected = profile(
        model_id="synthetic-model",
        allowed_projects=["eval"],
        roles=["L", "M", "H"],
        capabilities=["structured_output", "planning", "analyze", "summarize", "semantic_review"],
        context_window=32768,
        max_output=2048,
    )
    cases = []
    for role in ("L", "M", "H"):
        tasks = {
            "L": ["analyze", "summarize"],
            "M": ["analyze", "summarize", "planning"],
            "H": ["analyze", "summarize", "planning", "semantic_review"],
        }[role]
        for index in range(20):
            cases.append(
                {
                    "case_id": role + str(index),
                    "role": role,
                    "task": tasks[index % len(tasks)],
                    "request_id": "synthetic-" + role + str(index),
                    "usage": {
                        "input_tokens_total": 10,
                        "output_tokens_total": 5,
                        "usage_basis": "provider",
                    },
                    "fixture_sha256": sha(role + str(index)),
                    "output_sha256": "a" * 64,
                    "schema_pass": True,
                    "numeric_pass": True,
                    "source_pass": True,
                    "policy_pass": True,
                    "accepted": True,
                    "reference_accepted": True,
                }
            )
    evidence(tmp_path, selected, cases=cases)
    current = config.AppConfig(
        project_ids=["eval"],
        models=[selected],
        privacy=[PrivacyPolicy(project_id="eval", allowed_providers=["xai"], allow_cloud=True)],
    )
    path = tmp_path / "config.json"
    config.create_config(path, current)
    return path


def manifest_file(tmp_path, config_path, **changes):
    manifest = live.prepare_manifest(
        config_path,
        ["summary-01"],
        tmp_path / "batch",
        model_ids=["synthetic-model"],
        **changes,
    )
    path = tmp_path / "reviewed.json"
    path.write_text(json.dumps(manifest), "utf-8")
    return path, manifest


def forbidden(*_args, **_kwargs):
    pytest.fail("preflight must not read a key, create a client, or execute work")


@pytest.mark.parametrize("allow", [False, None, 1])
def test_denial_precedes_even_manifest_read(tmp_path, monkeypatch, allow):
    monkeypatch.setattr(config, "read_xai_key", forbidden)
    monkeypatch.setattr(live, "XAIClient", forbidden)
    with pytest.raises(AppError) as caught:
        live.execute_manifest(
            tmp_path / "none", tmp_path / "none", manifest_sha256="a" * 64, allow_live=allow
        )
    assert caught.value.code == "POLICY_DENIED" and not list(tmp_path.iterdir())


def test_prepare_reads_ui_settings_without_key_client_or_workspace(tmp_path, monkeypatch):
    path = configured(tmp_path)
    conn = db.init_db(tmp_path / "state.db")
    override = config.load_config(path).privacy[0].model_copy(update={"revision": 2})
    db.set_value(conn, "settings", {"privacy": [override.model_dump()]})
    conn.close()
    before = {str(item): item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()}
    monkeypatch.setattr(config, "read_xai_key", forbidden)
    monkeypatch.setattr(live, "XAIClient", forbidden)
    result = live.prepare_manifest(
        path, ["summary-01"], tmp_path / "batch", model_ids=["synthetic-model"]
    )
    assert result["privacy"]["revision"] == 2 and result["limits"]["total_http_attempts"] == 96
    assert all(Path(name).read_bytes() == contents for name, contents in before.items())
    assert not (tmp_path / "batch").exists()


@pytest.mark.parametrize("change", ["hash", "source", "code", "privacy", "qualification", "limit"])
def test_changed_review_or_qualification_fails_before_key_and_output(tmp_path, monkeypatch, change):
    path = configured(tmp_path)
    reviewed, manifest = manifest_file(tmp_path, path)
    approved = fingerprint(manifest)
    if change == "hash":
        manifest["arms"] = ["C"]
    elif change == "source":
        record = next(iter(manifest["cases"]["summary-01"]["files"].values()))
        raw = b"private-unapproved-document"
        record.update(base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
        approved = fingerprint(manifest)
    elif change == "code":
        monkeypatch.setattr(live, "_code_hash", lambda: "b" * 64)
    elif change == "privacy":
        conn = db.init_db(tmp_path / "state.db")
        db.set_value(conn, "settings", {"privacy": [{"project_id": "eval"}]})
        conn.close()
    elif change == "qualification":
        (tmp_path / "evaluations" / "eval1.json").unlink()
    else:
        manifest["limits"]["logical_calls_per_todo"] = 3
        approved = fingerprint(manifest)
    reviewed.write_text(json.dumps(manifest), "utf-8")
    monkeypatch.setattr(config, "read_xai_key", forbidden)
    monkeypatch.setattr(live, "XAIClient", forbidden)
    with pytest.raises((ValueError, AppError)):
        live.execute_manifest(path, reviewed, manifest_sha256=approved, allow_live=True)
    assert not (tmp_path / "batch").exists()


def test_fixed_corpus_selection_and_cache_order_are_required(tmp_path):
    path = configured(tmp_path)
    for ids, arms, caches in [
        (["invented"], ["A"], ["cold"]),
        (["automation-01"], ["A"], ["cold"]),
        (["summary-01"], ["D"], ["cold"]),
        (["summary-01"], ["A"], ["warm"]),
    ]:
        with pytest.raises(ValueError):
            live.prepare_manifest(
                path,
                ids,
                tmp_path / "batch",
                model_ids=["synthetic-model"],
                arms=arms,
                caches=caches,
            )


def service(replies):
    seen = []

    def serve(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "synthetic-model"}]})
        return httpx.Response(
            200,
            json={
                "id": "mock-request-" + str(len(seen)),
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(next(replies))},
                    }
                ],
                "usage": {
                    "prompt_tokens": 32,
                    "completion_tokens": 9,
                    "prompt_tokens_details": {"cached_tokens": 4},
                },
            },
        )

    return httpx.MockTransport(serve), seen


def test_mock_runs_use_real_core_arms_preserve_repair_caps_and_never_emit_live_quality(
    tmp_path, monkeypatch
):
    path = configured(tmp_path)
    case, plan = case_and_plan()
    # This test replaces only the corpus loader; the production loader is fixed and accepts no inputs.
    monkeypatch.setattr(live, "load_cases", lambda: [case])
    manifest = live.prepare_manifest(
        path, [case.id], tmp_path / "batch", model_ids=["synthetic-model"], caches=("cold",)
    )
    reviewed = tmp_path / "reviewed.json"
    reviewed.write_text(json.dumps(manifest), "utf-8")
    monkeypatch.setenv("XAI_API_KEY", "SYNTHETIC_TEST_KEY")
    transport, seen = service(iter([plan, CONTENT, plan, {"sections": []}, CONTENT, plan, CONTENT]))
    result = live.execute_manifest(
        path,
        reviewed,
        manifest_sha256=fingerprint(manifest),
        allow_live=True,
        http_transport=transport,
    )
    assert result["generation_http_attempts"] == 7 and len(seen) == 8
    assert result["live"] is False and result["quality_observations"] == 0
    assert result["token_reduction"] is None and result["human_adoption_labels"] == "required"
    ledgers = [read_json(item["path"]) for item in result["ledgers"]]
    assert [[call["route_json"]["role"] for call in ledger["calls"]] for ledger in ledgers] == [
        ["M", "H"],
        ["M", "L", "M"],
        ["M", "M"],
    ]
    for ledger in ledgers:
        assert ledger["run"]["state"] == "succeeded"
        assert (
            ledger["evidence_layer"] == "mock_provider" and ledger["actual_provider_tokens"] is None
        )
        assert ledger["cache_observation"]["hits"] <= ledger["cache_observation"]["lookups"]
        assert all(call["usage_basis"] == "provider" for call in ledger["calls"])
        assert ledger["usage"]["input_tokens_total"] == 32 * len(ledger["calls"])
    notes = {
        "reviewer_id": "human",
        "independent": True,
        "output_sha256": sha(""),
        "facts": [],
        "coverage": {},
        "todo_requirements": {"t1": []},
    }
    with pytest.raises(ValueError, match="exact live output"):
        live.annotate_ledger(case.id, result["ledgers"][0]["path"], notes)
    # Synthetic stored evidence exercises annotation mechanics; never exported as live evidence.
    ledger_path = Path(result["ledgers"][1]["path"])
    synthetic = read_json(ledger_path)
    synthetic.update(live=True, evidence_layer="live")
    ledger_path.write_text(json.dumps(synthetic), "utf-8")
    conn = db.init_db(ledger_path.parent / "state.db")
    db.set_value(conn, "comparison_ledger:" + synthetic["run"]["id"], fingerprint(synthetic))
    conn.close()
    observation = live.annotate_ledger(case.id, ledger_path, notes)
    assert observation.facts == [] and observation.coverage == {}
    assert len(observation.calls) == 3 and observation.calls[-1].role == "M"
    assert observation.calls[-1].escalated
    assert observation.cache_lookups == synthetic["cache_observation"]["lookups"]
    assert observation.output_verified is False
    assert observation.numbers == {} and observation.numeric_verified
    assert checks(case, observation)["checks"]["numbers"] is True
    assert "numeric_references" in synthetic["steps"][0]["validation_json"]["checked_layers"]
    with pytest.raises(ValueError, match="exact live output"):
        live.annotate_ledger(case.id, ledger_path, {**notes, "output_sha256": "a" * 64})
    with pytest.raises(ValueError, match="unknown TODOs"):
        live.annotate_ledger(case.id, ledger_path, {**notes, "todo_requirements": {"invented": []}})
    synthetic["published_text"] = "tampered"
    ledger_path.write_text(json.dumps(synthetic), "utf-8")
    with pytest.raises(ValueError, match="recorded ledger changed"):
        live.annotate_ledger(case.id, ledger_path, {**notes, "output_sha256": sha("tampered")})
    monkeypatch.setattr(config, "read_xai_key", forbidden)
    with pytest.raises(ValueError, match="fresh absolute"):
        live.execute_manifest(
            path,
            reviewed,
            manifest_sha256=fingerprint(manifest),
            allow_live=True,
            http_transport=transport,
        )
    assert len(seen) == 8


def test_batch_cap_prevents_extra_generation_even_if_core_requests_more(tmp_path, monkeypatch):
    path = configured(tmp_path)
    case, plan = case_and_plan()
    monkeypatch.setattr(live, "load_cases", lambda: [case])
    manifest = live.prepare_manifest(
        path,
        [case.id],
        tmp_path / "batch",
        model_ids=["synthetic-model"],
        arms=["C"],
        caches=("cold",),
        max_http_attempts=1,
    )
    reviewed = tmp_path / "reviewed.json"
    reviewed.write_text(json.dumps(manifest), "utf-8")
    monkeypatch.setenv("XAI_API_KEY", "SYNTHETIC_TEST_KEY")
    transport, seen = service(iter([plan]))
    result = live.execute_manifest(
        path,
        reviewed,
        manifest_sha256=fingerprint(manifest),
        allow_live=True,
        http_transport=transport,
    )
    assert result["generation_http_attempts"] == 1 and len(seen) == 2
    ledger = read_json(result["ledgers"][0]["path"])
    assert ledger["run"]["error_code"] == "CALL_LIMIT"
    assert ledger["usage"]["usage_state"] == "unknown" and ledger["actual_provider_tokens"] is None


def test_xlsx_freezing_ignores_only_container_timestamps():
    import io
    import zipfile

    source = next(s for c in live.load_cases() for s in c.sources if s.filename.endswith(".xlsx"))
    raw = live.source_bytes(source)
    if not raw.startswith(b"PK"):
        pytest.fail("choose the fixed XLSX source")
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as original, zipfile.ZipFile(stream, "w") as rewritten:
        for name in original.namelist():
            rewritten.writestr(zipfile.ZipInfo(name, (2001, 1, 1, 0, 0, 0)), original.read(name))
    assert raw != stream.getvalue()
    assert live._fixture_hash(raw, "source.xlsx") == live._fixture_hash(
        stream.getvalue(), "source.xlsx"
    )


def test_model_source_numbers_reach_scoring_only_through_independent_exact_evidence(
    tmp_path, monkeypatch
):
    path = configured(tmp_path)
    case = next(case for case in live.load_cases() if case.id == "summary-07").model_copy(deep=True)
    case.output_format, case.request = "md", "이 자료 요약해줘"
    monkeypatch.setattr(live, "load_cases", lambda: [case])

    def inline_text_worker(path, filename, *, operation="preflight", args=None, **_kwargs):
        # Only the sandbox subprocess boundary is replaced; use the real TXT/MD operations.
        assert Path(filename).suffix in {".txt", ".md"}
        metadata = parser_worker.preflight(path, filename, args or {})
        if operation == "preflight":
            return metadata
        return office.worker_operation(
            operation, path, {**(args or {}), "format": metadata["format"]}
        )

    for module in ("artifacts", "office", "verification"):
        monkeypatch.setattr("kelly." + module + ".parse_file", inline_text_worker)
    facts = [
        "기존 고객 40명이 응답했다.",
        "32명이 새 안내문이 이해하기 쉽다고 답했다.",
        "신규 고객에 대한 결과는 없다.",
    ]
    seen = []

    def serve(request):
        seen.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "synthetic-model"}]})
        payload = json.loads(
            json.loads(request.content)["messages"][-1]["content"].split("\nDATA\n", 1)[1]
        )
        references = []

        def collect(value):
            if isinstance(value, dict):
                if "evidence_id" in value:
                    references.append(value["evidence_id"])
                else:
                    for child in value.values():
                        collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(payload["inputs"])
        assert len(references) == 3
        output = {
            "sections": {
                "content": {
                    "paragraphs": [[{"claim_id": "c" + str(index)}] for index in range(len(facts))]
                },
                "claims": [
                    {"id": "c" + str(index), "text": fact, "evidence_ids": references}
                    for index, fact in enumerate(facts)
                ],
            }
        }
        return httpx.Response(
            200,
            json={
                "id": "mock-source-number",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(output)},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 40},
            },
        )

    manifest = live.prepare_manifest(
        path,
        [case.id],
        tmp_path / "batch",
        model_ids=["synthetic-model"],
        arms=["C"],
        caches=("cold",),
    )
    reviewed = tmp_path / "reviewed.json"
    reviewed.write_text(json.dumps(manifest), "utf-8")
    monkeypatch.setenv("XAI_API_KEY", "SYNTHETIC_TEST_KEY")
    result = live.execute_manifest(
        path,
        reviewed,
        manifest_sha256=fingerprint(manifest),
        allow_live=True,
        http_transport=httpx.MockTransport(serve),
    )
    assert seen == ["GET", "POST"] and result["quality_observations"] == 0 and not result["live"]
    ledger_path = Path(result["ledgers"][0]["path"])
    ledger = read_json(ledger_path)
    assert ledger["run"]["state"] == "succeeded" and ledger["published_receipts"]
    assert ledger["numeric_observation"]["numbers"] == {}
    # Only synthetic test evidence is relabelled to exercise the strictly live annotation entrypoint.
    ledger.update(live=True, evidence_layer="live")
    ledger_path.write_text(json.dumps(ledger), "utf-8")
    conn = db.init_db(ledger_path.parent / "state.db")
    db.set_value(conn, "comparison_ledger:" + ledger["run"]["id"], fingerprint(ledger))
    source_map = db.get_value(conn, "comparison_sources")
    conn.close()
    locations = ["s1:L1", "s1:L2", "s1:L3"]
    notes = {
        "reviewer_id": "independent-test-reviewer",
        "independent": True,
        "output_sha256": sha(ledger["published_text"]),
        "facts": facts,
        "coverage": {key: locations for key in ("r1", "r2", "r3")},
        "todo_requirements": {
            step["spec_json"]["todo"]["id"]: ["r1", "r2", "r3"] if step["kind"] == "model" else []
            for step in ledger["steps"]
        },
        "numbers": {"응답": "40", "이해쉬움": "32"},
        "units": {"응답": "명", "이해쉬움": "명"},
        "numeric_locators": {"응답": locations, "이해쉬움": locations},
    }
    observation = live.annotate_ledger(case.id, ledger_path, notes)
    assert observation.numeric_verified and observation.numbers == {"응답": "40", "이해쉬움": "32"}
    assert checks(case, observation)["passed"], checks(case, observation)
    assert ledger["numeric_observation"]["numbers"] == {}
    for fields in (
        {
            "numbers": {"응답": "72", "이해쉬움": "32"}
        },  # invented arithmetic, even though inputs can sum
        {"units": {"응답": "원", "이해쉬움": "명"}},
        {"numeric_locators": {"응답": ["s1:L3"], "이해쉬움": locations}},
    ):
        with pytest.raises(ValueError, match="verified source quote"):
            live._annotated_numbers(
                case,
                ledger,
                live.IndependentAnnotation.model_validate({**notes, **fields}),
                source_map,
            )
    ledger["numeric_observation"] = {
        "numbers": {"응답": "40"},
        "units": {"응답": "명"},
        "numeric_locators": {"응답": locations},
    }
    with pytest.raises(ValueError, match="cannot replace deterministic"):
        live._annotated_numbers(
            case, ledger, live.IndependentAnnotation.model_validate(notes), source_map
        )
    ledger["numeric_observation"] = {"numbers": {}, "units": {}, "numeric_locators": {}}
    model_step = next(step for step in ledger["steps"] if step["kind"] == "model")
    model_step["validation_json"]["checked_layers"].remove("numeric_references")
    with pytest.raises(ValueError, match="numeric checks did not pass"):
        live._annotated_numbers(
            case, ledger, live.IndependentAnnotation.model_validate(notes), source_map
        )
