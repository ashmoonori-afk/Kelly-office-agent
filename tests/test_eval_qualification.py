"""Qualification boundary fixtures; no actual provider, key access or human judgement."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from birkin_lite import config, db
from birkin_lite.contracts import AppError, ModelBinding, PrivacyPolicy, digest
from evals import qualification as q
from evals.execute import task_spec
from evals.run import blind_id, load_cases


def configured(tmp_path):
    profile = config.ModelProfile(
        binding=ModelBinding(
            provider="xai",
            model_id="synthetic-candidate",
            roles=["M"],
            enabled=True,
            allowed_projects=["eval"],
            capabilities=["structured_output", "planning"],
            context_window=32768,
            max_output=2048,
            metering_mode="api",
        )
    )
    value = config.AppConfig(
        actor_id="eval-owner",
        project_ids=["eval"],
        models=[profile],
        privacy=[PrivacyPolicy(project_id="eval", allowed_providers=["xai"], allow_cloud=True)],
    )
    path = tmp_path / "config.json"
    config.create_config(path, value)
    return path, profile


def manifest(tmp_path):
    path, _ = configured(tmp_path)
    return q.prepare_trials(
        path,
        model_id="synthetic-candidate",
        role="M",
        task="planning",
        case_ids=["conflict_missing-05"],
        workspace=tmp_path / "exam",
    )


def planned():
    case = next(case for case in load_cases() if case.id == "conflict_missing-05")
    return {
        "task_spec": task_spec(case, []).model_dump(),
        "plan": {
            "requirements": [{"id": "r1", "text": case.request}],
            "todos": [
                {
                    "id": "t1",
                    "title": "자료 요청",
                    "requirement_ids": ["r1"],
                    "kind": "simple",
                    "reason": "요청에 필요한 자료를 확인한다",
                    "signals": ["extraction"],
                    "clarity": "missing_input",
                    "usefulness": "required",
                    "executor": {"type": "model", "role": "L", "task": "summarize"},
                    "output_schema": "ReportSectionsV1",
                    "done_when": [case.request],
                }
            ],
        },
    }


def mock_client(seen, status=200):
    def serve(request):
        seen.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "synthetic-candidate"}]})
        return httpx.Response(
            status,
            json={
                "id": "mock-request-1",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(planned())},
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 3,
                    "total_tokens": 8,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    return httpx.MockTransport(serve)


@pytest.mark.parametrize(
    "change", [None, "role", "task", "case", "duplicate", "scope", "capability"]
)
def test_prepare_is_readonly_and_rejects_unreachable_coverage(tmp_path, monkeypatch, change):
    path, _ = configured(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(q.config, "read_xai_key", lambda *_: pytest.fail("prepare read a key"))
    monkeypatch.setattr(
        q, "XAIClient", lambda *_args, **_kwargs: pytest.fail("prepare created a client")
    )
    options = {
        "model_id": "synthetic-candidate",
        "role": "M",
        "task": "planning",
        "case_ids": ["conflict_missing-05"],
        "workspace": tmp_path / "exam",
    }
    if change == "role":
        options["role"] = "L"
    elif change == "task":
        options["task"] = "semantic_review"
    elif change == "case":
        options["case_ids"] = ["automation-01"]
    elif change == "duplicate":
        options["case_ids"] *= 2
    elif change in {"scope", "capability"}:
        value = json.loads(before)
        if change == "scope":
            value["privacy"][0]["allow_cloud"] = False
        else:
            value["models"][0]["binding"]["capabilities"].append("unsupported_exam")
        path.write_text(json.dumps(value), "utf-8")
        before = path.read_bytes()
    if change:
        with pytest.raises(AppError):
            q.prepare_trials(path, **options)
    else:
        prepared = q.prepare_trials(path, **options)
        assert prepared.max_generations == 1
        assert prepared.profile.binding.quality_evidence_id is None
    assert path.read_bytes() == before and not (tmp_path / "exam").exists()


@pytest.mark.parametrize("status", [200, 429])
def test_mock_exam_calls_once_stays_unqualified_and_cannot_be_replayed(
    tmp_path, monkeypatch, status
):
    prepared = manifest(tmp_path)
    accesses, seen = [], []
    monkeypatch.setattr(
        q.config, "read_xai_key", lambda *_: accesses.append(1) or SecretStr("SYNTHETIC")
    )
    result = q.execute_trials(
        prepared,
        manifest_sha256=digest(prepared),
        allow_live=True,
        http_transport=mock_client(seen, status),
    )
    assert seen == ["GET", "POST"] and accesses == [1]
    assert result["live"] is result["qualified"] is False
    home = Path(prepared.workspace)
    complete = json.loads((home / "complete.json").read_text("utf-8"))
    receipt = json.loads((home / (complete["trial_ids"][0] + ".trial.json")).read_text("utf-8"))
    with db.init_db(home / "state.db") as conn:
        call = db.row_dict(conn.execute("SELECT * FROM calls").fetchone())
        assert call["route_json"]["quality_evidence_id"] is None
        assert db.get_value(conn, "generation_attempts") == 1
    assert receipt["live"] is False
    assert receipt["schema_pass"] is (status == 200)
    with pytest.raises(AppError):
        q.execute_trials(
            prepared,
            manifest_sha256=digest(prepared),
            allow_live=True,
            http_transport=mock_client(seen),
        )
    assert seen == ["GET", "POST"] and accesses == [1]
    with pytest.raises(AppError) as caught:
        q.write_qualification([home], [home], [], tmp_path / "published", evidence_id="candidate")
    assert caught.value.code == "POLICY_DENIED" and not (tmp_path / "published").exists()


@pytest.mark.parametrize("change", ["flag", "hash", "count", "profile", "config"])
def test_approval_or_manifest_change_fails_before_key_client_and_workspace(
    tmp_path, monkeypatch, change
):
    prepared = manifest(tmp_path)
    allow = change != "flag"
    if change == "count":
        prepared.max_generations = 2
    elif change == "profile":
        prepared.profile.binding.max_output = 1024
    elif change == "config":
        path = Path(prepared.config_path)
        value = json.loads(path.read_text("utf-8"))
        value["port"] = 8766
        path.write_text(json.dumps(value), "utf-8")
    monkeypatch.setattr(
        q.config, "read_xai_key", lambda *_: pytest.fail("rejected scope read a key")
    )
    monkeypatch.setattr(
        q, "XAIClient", lambda *_args, **_kwargs: pytest.fail("rejected scope created client")
    )
    with pytest.raises(AppError):
        q.execute_trials(
            prepared,
            manifest_sha256="0" * 64 if change == "hash" else digest(prepared),
            allow_live=allow,
        )
    assert not Path(prepared.workspace).exists()


def report_fixtures(profile):
    """Injected reader values test writer logic only; they are not live execution evidence."""
    groups = []
    for prefix, role in (("candidate", "M"), ("reference", "H")):
        records = []
        for number in range(20):
            records.append(
                {
                    "trial_id": prefix + str(number),
                    "case_id": "case" + str(number),
                    "fixture_sha256": digest(["distinct-fixture", number]),
                    "trial_input_sha256": digest(["distinct-input", number]),
                    "provider": "xai",
                    "model_id": profile.binding.model_id,
                    "role": role,
                    "task": "planning",
                    "output_sha256": digest([prefix, number]),
                    "schema_pass": True,
                    "numeric_pass": True,
                    "source_pass": True,
                    "policy_pass": True,
                    "validation": {"status": "pass", "checked_layers": ["schema", "plan"]},
                    "request_id": prefix + "-request-" + str(number),
                    "reasoning_effort": None,
                    "usage": {
                        "usage_basis": "provider",
                        "input_tokens_total": 10,
                        "output_tokens_total": 5,
                    },
                    "proof": {"synthetic_reader_fixture": True, "number": number, "group": prefix},
                }
            )
        groups.append(records)
    labels = [
        {
            "blind_id": blind_id(
                SimpleNamespace(id=r["trial_id"], output_sha256=r["output_sha256"])
            ),
            "output_sha256": r["output_sha256"],
            "reviewer_id": "synthetic-reviewer",
            "blinded": True,
            "adopted": True,
            "coverage_correct": True,
            "facts_grounded": True,
            "usable": True,
            "todo_decomposition_appropriate": True,
            "rationale": "Writer contract fixture, not a human evaluation.",
        }
        for group in groups
        for r in group
    ]
    return *groups, labels


@pytest.mark.parametrize(
    "change",
    [
        "label_missing",
        "label_hash",
        "same_run",
        "same_request",
        "reference_missing",
        "review_input_changed",
        "environment_changed",
        "checks",
        "coverage",
    ],
)
def test_writer_rejects_missing_or_reused_independent_evidence(tmp_path, monkeypatch, change):
    _, profile = configured(tmp_path)
    candidates, references, labels = report_fixtures(profile)
    if change == "label_missing":
        labels.pop()
    elif change == "label_hash":
        labels[0]["output_sha256"] = "0" * 64
    elif change == "same_run":
        references[0]["trial_id"] = candidates[0]["trial_id"]
    elif change == "same_request":
        references[0]["request_id"] = candidates[0]["request_id"]
    elif change == "reference_missing":
        references[0]["fixture_sha256"] = "0" * 64
    elif change == "review_input_changed":
        references[0]["trial_input_sha256"] = "0" * 64
    elif change == "environment_changed":
        references[0]["proof"]["environment_sha256"] = "0" * 64
    elif change == "checks":
        references[0]["source_pass"] = False
    else:
        candidates.pop()
        references.pop()
        labels = labels[:19] + labels[20:39]
    monkeypatch.setattr(
        q,
        "_bundle",
        lambda path: (
            SimpleNamespace(profile=profile, role="M" if path == "candidate" else "H"),
            candidates if path == "candidate" else references,
        ),
    )
    with pytest.raises((AppError, ValueError)):
        q.write_qualification(
            ["candidate"], ["reference"], labels, tmp_path / "output", evidence_id="qualified"
        )
    assert not (tmp_path / "output/evaluations/qualified.json").exists()


def test_writer_contract_with_injected_reader_outputs_sidecar_and_does_not_install(
    tmp_path, monkeypatch
):
    path, profile = configured(tmp_path)
    before = path.read_bytes()
    candidates, references, labels = report_fixtures(profile)
    monkeypatch.setattr(
        q,
        "_bundle",
        lambda name: (
            SimpleNamespace(profile=profile, role="M" if name == "candidate" else "H"),
            candidates if name == "candidate" else references,
        ),
    )
    result = q.write_qualification(
        ["candidate"],
        ["reference"],
        labels,
        tmp_path / "writer-unit-fixture",
        evidence_id="qualified",
    )
    assert result["qualified"] is True and result["installed"] is False
    assert path.read_bytes() == before
    report = config.QualificationReport.model_validate_json(Path(result["report"]).read_bytes())
    assert len(report.cases) == 20 and all(
        case.accepted and case.reference_accepted for case in report.cases
    )
    sidecar = json.loads(
        (tmp_path / "writer-unit-fixture/qualified.provenance.json").read_text("utf-8")
    )
    assert sidecar["report_sha256"] == result["report_sha256"] and len(sidecar["labels"]) == 40
    assert len(sidecar["matches"]) == 20


def test_all_six_content_tasks_have_explicit_frozen_role_exams(tmp_path, monkeypatch):
    path, _ = configured(tmp_path)
    value = json.loads(path.read_text("utf-8"))
    tasks = set(q.INSTRUCTIONS) - {"planning", "semantic_review"}
    value["models"][0]["binding"]["capabilities"] = ["structured_output", *sorted(tasks)]
    path.write_text(json.dumps(value), "utf-8")
    monkeypatch.setattr(q.config, "read_xai_key", lambda *_: pytest.fail("prepare read key"))
    monkeypatch.setattr(q, "XAIClient", lambda *_a, **_k: pytest.fail("prepare created client"))
    case = next(case for case in load_cases() if case.id == "summary-01")
    original = case.model_dump()
    for task in tasks:
        prepared = q.prepare_trials(
            path,
            model_id="synthetic-candidate",
            role="M",
            task=task,
            case_ids=[case.id],
            workspace=tmp_path / task,
        )
        assert prepared.instructions == q.INSTRUCTIONS[task]
        assert case.request in prepared.requests[case.id]
        assert prepared.output_schema == q.MODEL_OUTPUT_TYPES[task].model_json_schema()
        assert prepared.case_hashes[case.id] == q.condition_hash(case)
        assert not Path(prepared.workspace).exists()
    assert next(c for c in load_cases() if c.id == case.id).model_dump() == original


@pytest.mark.parametrize("task", ["search_answer", "semantic_review"])
def test_real_text_source_with_mock_http_records_exam_checks_and_blind_task(
    tmp_path, monkeypatch, task
):
    """Real parser/evidence/usage boundary, synthetic HTTP; never a live quality claim."""
    path, _ = configured(tmp_path)
    value = json.loads(path.read_text("utf-8"))
    value["models"][0]["binding"].update(roles=["H"], capabilities=["structured_output", task])
    path.write_text(json.dumps(value), "utf-8")
    review = {
        "sections": {
            "content": {"paragraphs": [[{"claim_id": "unsupported"}]]},
            "claims": [
                {"id": "unsupported", "text": "자료에 없는 결론입니다.", "evidence_ids": []}
            ],
        }
    }
    prepared = q.prepare_trials(
        path,
        model_id="synthetic-candidate",
        role="H",
        task=task,
        case_ids=["summary-01"],
        workspace=tmp_path / "exam",
        review_outputs={"summary-01": review} if task == "semantic_review" else None,
    )
    monkeypatch.setattr(q.config, "read_xai_key", lambda *_: SecretStr("SYNTHETIC"))
    seen = []

    def serve(request):
        seen.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "synthetic-candidate"}]})
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][0]["content"].split("\nDATA\n", 1)[1])
        evidence = []

        def walk(item):
            if isinstance(item, list):
                for child in item:
                    walk(child)
            elif isinstance(item, dict):
                if "evidence_id" in item:
                    evidence.append(item)
                for child in item.values():
                    walk(child)

        walk(data["inputs"])
        assert evidence and data["task"] == task
        if task == "semantic_review":
            assert data["inputs"]["candidate_output"] == review
            output = {
                "validation": {
                    "status": "fail",
                    "errors": ["UNSUPPORTED_FACT"],
                    "checked_layers": ["semantic_review"],
                    "evidence_refs": [item["evidence_id"] for item in evidence],
                }
            }
        else:
            output = {
                "answer": {
                    "found": True,
                    "text": [{"claim_id": "claim"}],
                    "claims": [
                        {
                            "id": "claim",
                            "text": evidence[0]["quote"],
                            "evidence_ids": [evidence[0]["evidence_id"]],
                        }
                    ],
                }
            }
        return httpx.Response(
            200,
            json={
                "id": "mock-content-request",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(output)},
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 3,
                    "total_tokens": 8,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    result = q.execute_trials(
        prepared,
        manifest_sha256=digest(prepared),
        allow_live=True,
        http_transport=httpx.MockTransport(serve),
    )
    home = Path(prepared.workspace)
    complete = json.loads((home / "complete.json").read_text("utf-8"))
    receipt = json.loads((home / (complete["trial_ids"][0] + ".trial.json")).read_text("utf-8"))
    assert seen == ["GET", "POST"] and result["live"] is False
    assert all(
        receipt[key] for key in ("schema_pass", "numeric_pass", "source_pass", "policy_pass")
    ), receipt
    assert receipt["validation"]["status"] == ("pass" if task == "semantic_review" else "fail")
    assert receipt["trial_input_sha256"] == q._input_hash(prepared, "summary-01")
    packet = json.loads((home / "blind.json").read_text("utf-8"))[0]
    assert packet["task"] == task and packet["output_schema"] == prepared.output_schema
    assert packet["request"] == prepared.requests["summary-01"]
    assert "model" not in packet and "role" not in packet
    with pytest.raises(AppError) as caught:
        q.write_qualification([home], [home], [], tmp_path / "published", evidence_id="candidate")
    assert caught.value.code == "POLICY_DENIED"
    assert not (tmp_path / "published").exists()


def test_explicit_effort_is_manifest_bound_and_exercised_by_mock_http(tmp_path, monkeypatch):
    path, _ = configured(tmp_path)
    value = json.loads(path.read_text("utf-8"))
    value["models"][0]["binding"]["reasoning_efforts"] = ["none", "medium", "xhigh"]
    path.write_text(json.dumps(value), "utf-8")
    prepared = q.prepare_trials(
        path,
        model_id="synthetic-candidate",
        role="M",
        task="planning",
        reasoning_effort="xhigh",
        case_ids=["conflict_missing-05"],
        workspace=tmp_path / "exam",
    )
    assert prepared.reasoning_effort == "xhigh" and prepared.max_http_requests == 2
    monkeypatch.setattr(q.config, "read_xai_key", lambda *_: SecretStr("SYNTHETIC"))
    inner = mock_client([])

    def serve(request):
        if request.method == "POST":
            assert json.loads(request.content)["reasoning_effort"] == "xhigh"
        return inner.handle_request(request)

    q.execute_trials(
        prepared,
        manifest_sha256=digest(prepared),
        allow_live=True,
        http_transport=httpx.MockTransport(serve),
    )
    with db.init_db(Path(prepared.workspace) / "state.db") as conn:
        call = db.row_dict(conn.execute("SELECT * FROM calls").fetchone())
        assert call["route_json"]["reasoning_effort"] == "xhigh"
    with pytest.raises(AppError):
        q.prepare_trials(
            path,
            model_id="synthetic-candidate",
            role="M",
            task="planning",
            reasoning_effort="low",
            case_ids=["conflict_missing-05"],
            workspace=tmp_path / "bad",
        )
