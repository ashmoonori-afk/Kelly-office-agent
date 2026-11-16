import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from kelly.config import AppConfig, ModelProfile, create_config, load_config, store_secret
from kelly.contracts import AppError, ModelBinding, PlanningResultV1
from kelly.db import get_value, init_db, set_value
from kelly.live_smoke import main, run_probe


def configured(tmp_path, monkeypatch, **binding_changes):
    monkeypatch.setenv("XAI_API_KEY", "SYNTHETIC_KEY_NEVER_PERSIST")
    config = AppConfig(
        project_ids=["sales"],
        models=[
            ModelProfile(
                binding=ModelBinding.model_validate(
                    {
                        "provider": "xai",
                        "model_id": "probe-fixture-model",
                        "roles": ["M"],
                        "enabled": True,
                        "allowed_projects": ["sales"],
                        "capabilities": ["structured_output", "planning"],
                        "context_window": 32768,
                        "max_output": 2048,
                        "metering_mode": "api",
                        **binding_changes,
                    }
                )
            )
        ],
    )
    path = tmp_path / "config.json"
    create_config(path, config)
    return path


def plan(title="LIVE_MODEL_OK", count=1):
    return PlanningResultV1.model_validate(
        {
            "task_spec": {"intent": "합성 연결 검사", "acceptance": ["TODO 하나 반환"]},
            "plan": {
                "requirements": [{"id": "r1", "text": "TODO 하나 반환"}],
                "todos": [
                    {
                        "id": f"t{index}",
                        "title": title,
                        "requirement_ids": ["r1"],
                        "kind": "simple",
                        "reason": "합성 텍스트를 확인한다",
                        "signals": ["extraction"],
                        "clarity": "clear",
                        "usefulness": "required",
                        "executor": {"type": "model", "role": "L", "task": "meeting_actions"},
                        "output_schema": "MeetingActionsV1",
                        "done_when": ["합성 제목 확인"],
                    }
                    for index in range(count)
                ],
            },
        }
    ).model_dump(mode="json")


def response(content=None, *, include_usage=True):
    return {
        "id": "synthetic-request-1",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(plan()) if content is None else content,
                },
            }
        ],
        "usage": {
            "prompt_tokens": 32,
            "completion_tokens": 9,
            "total_tokens": 45,
            "prompt_tokens_details": {"cached_tokens": 6},
            "completion_tokens_details": {"reasoning_tokens": 4},
            "private_debug": "PRIVATE_USAGE_NEVER_PERSIST",
        }
        if include_usage
        else None,
    }


def mock_service(outcome):
    seen = []

    def serve(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "probe-fixture-model"}]})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return httpx.MockTransport(serve), seen


@pytest.mark.parametrize("allow", [False, None, 1])
def test_explicit_flag_required_before_config_key_or_network(tmp_path, monkeypatch, allow):
    def forbidden():
        pytest.fail("A denied probe cannot read a credential")

    monkeypatch.setattr("kelly.live_smoke.read_xai_key", forbidden)
    transport, seen = mock_service(httpx.Response(200, json=response()))
    with pytest.raises(AppError) as error:
        run_probe(tmp_path / "missing.json", "any", allow_live=allow, http_transport=transport)
    assert error.value.code == "POLICY_DENIED"
    assert seen == [] and list(tmp_path.iterdir()) == []


def test_missing_key_is_rejected_before_model_lookup(tmp_path, monkeypatch):
    path = configured(tmp_path, monkeypatch)
    monkeypatch.delenv("XAI_API_KEY")
    transport, seen = mock_service(httpx.Response(200, json=response()))
    with pytest.raises(AppError) as error:
        run_probe(path, "probe-fixture-model", allow_live=True, http_transport=transport)
    assert error.value.code == "NO_CAPABLE_MODEL"
    assert seen == [] and not (tmp_path / "evidence").exists()


def test_probe_uses_the_same_protected_key_store_as_settings(tmp_path, monkeypatch):
    path = configured(tmp_path, monkeypatch)
    monkeypatch.delenv("XAI_API_KEY")
    store_secret(tmp_path, "xai", SecretStr("SYNTHETIC_PROTECTED_KEY"))
    transport, seen = mock_service(httpx.Response(200, json=response()))
    result = run_probe(path, "probe-fixture-model", allow_live=True, http_transport=transport)
    assert result["accepted"] and not result["live"]
    assert [request.method for request in seen] == ["GET", "POST"]


@pytest.mark.parametrize("enabled", [True, False])
def test_probe_uses_current_ui_model_settings_without_modifying_them(
    tmp_path, monkeypatch, enabled
):
    path = configured(
        tmp_path,
        monkeypatch,
        enabled=not enabled,
        roles=["L"] if enabled else ["M"],
        capabilities=["structured_output", "analyze"]
        if enabled
        else ["structured_output", "planning"],
    )
    original = path.read_bytes()
    selected = load_config(path).models[0].model_dump()
    selected["binding"].update(
        enabled=enabled, roles=["M"], capabilities=["structured_output", "planning"]
    )
    settings = {"models": [selected]}
    conn = init_db(tmp_path / "state.db")
    try:
        set_value(conn, "settings", settings)
        transport, seen = mock_service(httpx.Response(200, json=response()))
        if enabled:
            result = run_probe(
                path, "probe-fixture-model", allow_live=True, http_transport=transport
            )
            report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
            assert result["accepted"] and not report["live"] and not report["qualified_for_office"]
            assert [request.method for request in seen] == ["GET", "POST"]
        else:
            with pytest.raises(AppError) as error:
                run_probe(path, "probe-fixture-model", allow_live=True, http_transport=transport)
            assert error.value.code == "NO_CAPABLE_MODEL"
            assert seen == []
        assert get_value(conn, "settings") == settings
        assert path.read_bytes() == original
    finally:
        conn.close()


def test_mock_probe_is_audited_but_never_live_or_office_qualification(tmp_path, monkeypatch):
    path = configured(tmp_path, monkeypatch)
    transport, seen = mock_service(httpx.Response(200, json=response()))
    result = run_probe(path, "probe-fixture-model", allow_live=True, http_transport=transport)
    report_path = Path(result["report"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result["accepted"] is True and result["live"] is False
    assert report["live"] is False and report["qualified_for_office"] is False
    assert report["error_code"] is None and report_path.name.startswith("provider-mock-")
    assert [request.method for request in seen] == ["GET", "POST"]
    assert [request.url.path for request in seen] == ["/v1/models", "/v1/chat/completions"]
    sent = json.loads(seen[1].content)
    assert sent["max_completion_tokens"] == 1024 and "tools" not in sent
    assert "LIVE_MODEL_OK" in sent["messages"][0]["content"]
    assert "SYNTHETIC_KEY" not in str(report) and "PRIVATE_USAGE" not in str(report)
    assert len(report["calls"]) == 1
    call = report["calls"][0]
    assert call["state"] == "settled" and call["provider_request_id"] == "synthetic-request-1"
    assert call["input_tokens_total"] == 32 and call["output_tokens_total"] == 13
    assert call["cached_input_tokens"] == 6 and call["reasoning_tokens"] == 4
    assert call["usage_basis"] == "provider" and call["cache_write_tokens"] is None
    with sqlite3.connect(tmp_path / "evidence" / "provider-probe.db") as conn:
        assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM steps").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    assert not list((tmp_path / "evidence").glob("*.part"))


@pytest.mark.parametrize(
    "title,count", [("live_model_ok", 1), ("LIVE_MODEL_OK ", 1), ("LIVE_MODEL_OK", 2)]
)
def test_only_exact_single_todo_passes(tmp_path, monkeypatch, title, count):
    path = configured(tmp_path, monkeypatch)
    transport, seen = mock_service(
        httpx.Response(200, json=response(json.dumps(plan(title, count))))
    )
    result = run_probe(path, "probe-fixture-model", allow_live=True, http_transport=transport)
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    assert result["accepted"] is False and result["error_code"] == "INVALID_OUTPUT"
    assert len(seen) == 2 and len(report["calls"]) == 1
    assert report["calls"][0]["output_tokens_total"] == 13


@pytest.mark.parametrize(
    "outcome,code,known",
    [
        (httpx.Response(200, json=response("{PRIVATE_INVALID")), "INVALID_OUTPUT", True),
        (httpx.Response(429, json=response()), "PROVIDER_RATE_LIMITED", True),
        (httpx.Response(503, json=response()), "PROVIDER_UNAVAILABLE", True),
        (httpx.ReadTimeout("PRIVATE_TIMEOUT"), "PROVIDER_TIMEOUT", False),
        (httpx.ConnectError("PRIVATE_CONNECT"), "PROVIDER_UNAVAILABLE", False),
    ],
    ids=["malformed", "rate-limit", "server-error", "read-timeout", "connect-error"],
)
def test_failures_keep_usage_and_never_retry_generation(
    tmp_path, monkeypatch, outcome, code, known
):
    path = configured(tmp_path, monkeypatch)
    transport, seen = mock_service(outcome)
    result = run_probe(path, "probe-fixture-model", allow_live=True, http_transport=transport)
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    assert result["accepted"] is False and result["error_code"] == code
    assert [request.method for request in seen] == ["GET", "POST"]
    assert len(report["calls"]) == 1 and "PRIVATE_" not in str(report)
    call = report["calls"][0]
    assert call["state"] == ("settled" if known else "unknown")
    assert call["output_tokens_total"] == (13 if known else None)
    assert call["input_tokens_total"] == (32 if known else None)


@pytest.mark.parametrize(
    "changes", [{"roles": ["L"]}, {"capabilities": ["structured_output", "analyze"]}]
)
def test_probe_requires_configured_planning_role_before_generation(tmp_path, monkeypatch, changes):
    path = configured(tmp_path, monkeypatch, **changes)
    transport, seen = mock_service(httpx.Response(200, json=response()))
    with pytest.raises(AppError) as error:
        run_probe(path, "probe-fixture-model", allow_live=True, http_transport=transport)
    assert error.value.code == "NO_CAPABLE_MODEL"
    assert [request.method for request in seen] == ["GET"]


def test_cli_requires_explicit_live_switch_without_reading_files(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "absent.json"), "--model", "any"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["error_code"] == "POLICY_DENIED"


def test_cli_passes_explicit_options_and_reports_exit_status(tmp_path, monkeypatch, capsys):
    path = configured(tmp_path, monkeypatch)
    transport, seen = mock_service(httpx.Response(200, json=response()))

    def mocked(config_path, model_id, *, allow_live):
        return run_probe(config_path, model_id, allow_live=allow_live, http_transport=transport)

    monkeypatch.setattr("kelly.live_smoke.run_probe", mocked)
    assert main(["--config", str(path), "--model", "probe-fixture-model", "--allow-live"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["accepted"] is True and output["live"] is False
    assert len(seen) == 2
