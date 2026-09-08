import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError
from test_routing import todo

from birkin_lite.config import (
    AppConfig,
    ModelProfile,
    QualificationReport,
    create_config,
    home_path,
    load_config,
    probe_binding,
    read_telegram_token,
    read_xai_key,
    resolve_catalog,
    store_secret,
    stored_secret,
)
from birkin_lite.contracts import AppError, ModelBinding, canonical_json
from birkin_lite.routing import choose_model


def profile(**changes):
    return ModelProfile(
        binding=ModelBinding.model_validate(
            {
                "provider": "xai",
                "model_id": "synthetic-account-model",
                "roles": ["L"],
                "enabled": True,
                "available": False,
                "allowed_projects": ["sales"],
                "capabilities": ["structured_output", "analyze"],
                "context_window": 16000,
                "max_output": 1000,
                "metering_mode": "api",
                **changes,
            }
        )
    )


def config(**changes):
    return AppConfig(project_ids=["sales"], models=[profile()], **changes)


def test_telegram_is_optional_and_cannot_grant_an_unconfigured_project(monkeypatch):
    assert config().telegram.enabled is False
    with pytest.raises(ValidationError):
        config(telegram={"enabled": True, "project_id": "other"})
    enabled = config(telegram={"enabled": True, "project_id": "sales"})
    assert enabled.telegram.connection_id == "telegram"
    with pytest.raises(ValidationError):
        config(telegram={"enabled": True, "project_id": "sales", "token": "secret"})
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert read_telegram_token() is None
    token = "123456:" + "a" * 32
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    secret = read_telegram_token()
    assert secret.get_secret_value() == token and token not in repr(secret)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token + "/sendMessage")
    with pytest.raises(AppError) as caught:
        read_telegram_token()
    assert token not in str(caught.value)


def evidence(tmp_path, selected, **changes):
    report = QualificationReport.model_validate(
        {
            "evidence_id": "eval1",
            "provider": "xai",
            "model_id": selected.binding.model_id,
            "mode": "live",
            "completed_at": "2026-09-08T00:00:00Z",
            "cases": [
                {
                    "case_id": f"case{i}",
                    "role": "L",
                    "task": "analyze",
                    "request_id": f"request{i}",
                    "usage": {
                        "input_tokens_total": 10,
                        "output_tokens_total": 5,
                        "usage_basis": "provider",
                    },
                    "fixture_sha256": hashlib.sha256(f"fixture{i}".encode()).hexdigest(),
                    "output_sha256": "2" * 64,
                    "schema_pass": True,
                    "numeric_pass": True,
                    "source_pass": True,
                    "policy_pass": True,
                    "accepted": True,
                    "reference_accepted": True,
                }
                for i in range(20)
            ],
            **changes,
        }
    )
    folder = tmp_path / "evaluations"
    folder.mkdir(exist_ok=True)
    data = canonical_json(report).encode()
    (folder / "eval1.json").write_bytes(data)
    selected.binding.quality_evidence_id = "eval1"
    selected.quality_evidence_sha256 = hashlib.sha256(data).hexdigest()
    return report


def resolve(value, path, ids=None, key=True):
    return resolve_catalog(
        value,
        path,
        lookup_models=lambda: ids if ids is not None else ["synthetic-account-model"],
        api_key_present=key,
    )


def test_example_is_disabled_private_unpriced_and_cannot_call_network(tmp_path):
    example = load_config(Path(__file__).parents[1] / "config.example.json")
    calls = []
    result = resolve_catalog(
        example,
        tmp_path / "config.json",
        lookup_models=lambda: calls.append(1),
        api_key_present=True,
    )
    assert calls == [] and result.statuses[0].state == "disabled"
    assert not result.bindings[0].enabled and not result.bindings[0].available
    assert example.privacy_for("default").allow_cloud is False
    assert example.models[0].price_snapshot.rates.input is None
    assert example.models[0].binding.quality_evidence_id is None


def test_create_is_atomic_no_replace_and_roundtrips_without_secret(tmp_path):
    path = tmp_path / "app" / "config.json"
    value = config()
    digest = create_config(path, value)
    before = path.read_bytes()
    assert hashlib.sha256(before).hexdigest() == digest
    assert load_config(path) == value
    assert home_path(value, path) == path.parent
    assert value.actor().project_ids == ["sales"]
    with pytest.raises(AppError):
        create_config(path, value.model_copy(update={"port": 8766}))
    assert path.read_bytes() == before
    assert list(path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "extra", [{"api_key": "NEVER_PRINT_SECRET"}, {"host": "0.0.0.0"}, {"spend_guard_enabled": True}]
)
def test_unsafe_or_secret_config_fields_are_rejected_without_echo(tmp_path, extra):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**config().model_dump(), **extra}), encoding="utf-8")
    with pytest.raises(AppError) as error:
        load_config(path)
    assert error.value.code == "INVALID_INPUT"
    assert "NEVER_PRINT_SECRET" not in str(error.value)


def test_local_home_boundary_and_provider_claims_are_validated(tmp_path):
    for unsafe in ["../outside", "\\\\server\\share", "folder:stream"]:
        with pytest.raises(AppError):
            home_path(config(home=unsafe), tmp_path / "config.json")
    for changed in [
        {"provider": "other"},
        {"metering_mode": "local"},
        {"available": True},
        {"role": "L"},
    ]:
        with pytest.raises(ValidationError):
            profile(**changed)
    with pytest.raises(ValidationError):
        AppConfig(project_ids=["sales"], models=[profile(allowed_projects=["foreign"])])


def test_only_explicit_xai_environment_key_is_read_and_repr_is_masked(monkeypatch):
    class Environment:
        def __init__(self):
            self.reads = []

        def get(self, key):
            self.reads.append(key)
            return "XAI_SYNTHETIC_SECRET"

    environment = Environment()
    monkeypatch.setattr("birkin_lite.config.os", SimpleNamespace(environ=environment))
    key = read_xai_key()
    assert environment.reads == ["XAI_API_KEY"]
    assert key.get_secret_value() == "XAI_SYNTHETIC_SECRET"
    assert "XAI_SYNTHETIC_SECRET" not in repr(key)


def test_account_discovery_is_required_and_never_proves_model_quality(tmp_path):
    value = config()
    path = tmp_path / "config.json"
    assert resolve(value, path, key=False).statuses[0].state == "key_missing"
    assert (
        resolve_catalog(value, path, api_key_present=True).statuses[0].state
        == "availability_unchecked"
    )
    assert resolve(value, path, ids=[]).statuses[0].state == "unavailable"
    result = resolve(value, path)
    assert result.bindings[0].available and result.statuses[0].state == "unqualified"
    assert result.bindings[0].quality_evidence_id is None
    with pytest.raises(AppError) as error:
        choose_model(todo(), result.bindings, value.privacy_for("sales"))
    assert error.value.code == "NO_CAPABLE_MODEL"
    assert value.models[0].binding.available is False


def test_arbitrary_quality_id_missing_hash_or_missing_file_stays_unqualified(tmp_path):
    value = config()
    selected = value.models[0]
    selected.binding.quality_evidence_id = "invented"
    assert resolve(value, tmp_path / "config.json").statuses[0].qualification == "unverified_hash"
    selected.quality_evidence_sha256 = "a" * 64
    assert resolve(value, tmp_path / "config.json").statuses[0].qualification == "missing_file"


@pytest.mark.parametrize(
    "failure",
    ["offline", "wrong_model", "hash", "provider_usage", "hard_check", "coverage", "acceptance"],
)
def test_qualification_requires_matching_live_evidence_and_measured_checks(tmp_path, failure):
    value = config()
    selected = value.models[0]
    report = evidence(tmp_path, selected)
    data = report.model_dump()
    if failure == "offline":
        data["mode"] = "offline"
    elif failure == "wrong_model":
        data["model_id"] = "different"
    elif failure == "provider_usage":
        data["cases"][0]["usage"]["usage_basis"] = "estimated"
    elif failure == "hard_check":
        data["cases"][0]["policy_pass"] = False
    elif failure == "coverage":
        data["cases"].pop()
    elif failure == "acceptance":
        data["cases"][0]["accepted"] = False
    encoded = canonical_json(data).encode()
    (tmp_path / "evaluations/eval1.json").write_bytes(encoded)
    if failure != "hash":
        selected.quality_evidence_sha256 = hashlib.sha256(encoded).hexdigest()
    else:
        selected.quality_evidence_sha256 = "f" * 64
    result = resolve(value, tmp_path / "config.json")
    assert result.statuses[0].state == "unqualified"
    assert result.bindings[0].quality_evidence_id is None


def test_valid_report_qualifies_binding_but_privacy_still_blocks_cloud(tmp_path):
    value = config()
    evidence(tmp_path, value.models[0])
    result = resolve(value, tmp_path / "config.json")
    assert result.statuses[0].state == "ready"
    assert result.statuses[0].qualification == "verified"
    with pytest.raises(AppError) as error:
        choose_model(todo(), result.bindings, value.privacy_for("sales"))
    assert error.value.code == "POLICY_DENIED"


@pytest.mark.parametrize(
    "change,expected",
    [
        (None, "verified"),
        ("missing-planning", "insufficient_coverage"),
        ("missing-review", "insufficient_coverage"),
        ("missing-content", "insufficient_coverage"),
        ("nineteen", "insufficient_coverage"),
        ("L-planning", "invalid_report"),
        ("M-review", "invalid_report"),
    ],
)
def test_qualification_covers_only_executable_role_task_pairs(tmp_path, change, expected):
    content = [
        "summarize",
        "report_prose",
        "meeting_actions",
        "draft_reply",
        "search_answer",
        "analyze",
    ]
    selected = profile(
        roles=["L", "M", "H"],
        capabilities=["structured_output", "planning", "semantic_review", *content],
    )
    value = AppConfig(project_ids=["sales"], models=[selected])
    report = evidence(tmp_path, selected).model_dump()
    template, cases = report["cases"][0], []
    for role in ("L", "M", "H"):
        tasks = content + (["planning"] if role != "L" else [])
        tasks += ["semantic_review"] if role == "H" else []
        for index in range(20):
            cases.append(
                {
                    **template,
                    "case_id": f"{role}-{index}",
                    "request_id": f"request-{role}-{index}",
                    "fixture_sha256": hashlib.sha256(
                        f"fixture-{role}-{index}".encode()
                    ).hexdigest(),
                    "role": role,
                    "task": tasks[index % len(tasks)],
                }
            )
    if change in {"missing-planning", "missing-review", "missing-content"}:
        role, task = {
            "missing-planning": ("M", "planning"),
            "missing-review": ("H", "semantic_review"),
            "missing-content": ("L", "analyze"),
        }[change]
        for case in cases:
            if (case["role"], case["task"]) == (role, task):
                case["task"] = "summarize"
    elif change == "nineteen":
        cases.pop(0)
    elif change == "L-planning":
        cases[0]["task"] = "planning"
    elif change == "M-review":
        cases[20]["task"] = "semantic_review"
    report["cases"] = cases
    data = canonical_json(report).encode()
    (tmp_path / "evaluations" / "eval1.json").write_bytes(data)
    selected.quality_evidence_sha256 = hashlib.sha256(data).hexdigest()
    assert resolve(value, tmp_path / "config.json").statuses[0].qualification == expected


def test_discovery_failure_never_exposes_credentials(tmp_path):
    def unavailable():
        raise OSError("SYNTHETIC_SECRET_REQUEST_AUTHORIZATION")

    result = resolve_catalog(
        config(), tmp_path / "config.json", lookup_models=unavailable, api_key_present=True
    )
    assert result.statuses[0].state == "discovery_failed"
    assert "SYNTHETIC_SECRET" not in canonical_json(result)


def test_explicit_probe_can_precede_qualification_without_enabling_normal_routing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XAI_API_KEY", "SYNTHETIC_TEST_KEY")
    value = config()
    lookup = lambda: ["synthetic-account-model"]
    with pytest.raises(AppError):
        probe_binding(
            value, tmp_path / "config.json", "synthetic-account-model", lookup_models=lookup
        )
    selected = probe_binding(
        value,
        tmp_path / "config.json",
        "synthetic-account-model",
        lookup_models=lookup,
        explicit=True,
    )
    assert selected.available and selected.quality_evidence_id is None
    assert value.models[0].binding.available is False


@pytest.mark.skipif(os.name != "nt", reason="Actual current-user Windows DPAPI")
@pytest.mark.parametrize("provider", ["xai", "telegram", "bootstrap"])
def test_dpapi_roundtrip_replace_delete_never_writes_plaintext(
    tmp_path, monkeypatch, caplog, provider
):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    first = "123456:" + "synthetic-first-key-" * 2
    second = "123456:" + "synthetic-second-key-" * 2
    assert stored_secret(tmp_path, provider) is None
    store_secret(tmp_path, provider, SecretStr(first))
    path = tmp_path / "auth" / (provider + ".dpapi")
    encrypted = path.read_bytes()
    actual = stored_secret(tmp_path, provider)
    assert isinstance(actual, SecretStr) and actual.get_secret_value() == first
    assert first not in repr(actual) and first not in caplog.text
    assert first.encode() not in encrypted and first.encode("utf-16-le") not in encrypted
    store_secret(tmp_path, provider, SecretStr(second))
    assert stored_secret(tmp_path, provider).get_secret_value() == second
    assert path.read_bytes() != encrypted
    for saved in (tmp_path / "auth").iterdir():
        assert saved == path
        data = saved.read_bytes()
        for plain in (first, second):
            assert plain.encode() not in data and plain.encode("utf-16-le") not in data
    store_secret(tmp_path, provider, SecretStr(""))
    assert stored_secret(tmp_path, provider) is None and not path.exists()
    store_secret(tmp_path, provider, SecretStr(""))
    assert list((tmp_path / "auth").iterdir()) == []
    assert second not in caplog.text


@pytest.mark.skipif(os.name != "nt", reason="Actual current-user Windows DPAPI")
def test_dpapi_tampered_ciphertext_is_rejected_without_secret_echo(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    plain = "SYNTHETIC_TAMPER_TEST_KEY_NO_REMOTE_USE"
    store_secret(tmp_path, "xai", SecretStr(plain))
    path = tmp_path / "auth" / "xai.dpapi"
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(data)
    with pytest.raises(AppError) as rejected:
        read_xai_key(tmp_path)
    assert rejected.value.code == "SECRET_STORE_ERROR"
    assert plain not in str(rejected.value) and str(path) not in str(rejected.value)


@pytest.mark.skipif(os.name != "nt", reason="Actual current-user Windows DPAPI")
@pytest.mark.parametrize("target", ["telegram", "bootstrap"])
def test_dpapi_ciphertext_cannot_be_copied_to_another_secret_slot(tmp_path, target):
    plain = "123456:" + "synthetic-cross-slot-key-" * 2
    store_secret(tmp_path, "xai", SecretStr(plain))
    original = (tmp_path / "auth" / "xai.dpapi").read_bytes()
    (tmp_path / "auth" / (target + ".dpapi")).write_bytes(original)
    with pytest.raises(AppError) as rejected:
        stored_secret(tmp_path, target)
    assert rejected.value.code == "SECRET_STORE_ERROR"
    assert plain not in str(rejected.value)
    assert stored_secret(tmp_path, "xai").get_secret_value() == plain


@pytest.mark.skipif(os.name != "nt", reason="Actual current-user Windows DPAPI")
@pytest.mark.parametrize(
    ("provider", "environment", "reader"),
    [("xai", "XAI_API_KEY", read_xai_key), ("telegram", "TELEGRAM_BOT_TOKEN", read_telegram_token)],
)
def test_dpapi_env_priority_and_empty_env_fallback(
    tmp_path, monkeypatch, provider, environment, reader
):
    monkeypatch.delenv(environment, raising=False)
    saved = "123456:" + "synthetic-saved-key-" * 2
    override = "123456:" + "synthetic-env-key-" * 2
    store_secret(tmp_path, provider, SecretStr(saved))
    assert reader(tmp_path).get_secret_value() == saved
    monkeypatch.setenv(environment, override)
    assert reader(tmp_path).get_secret_value() == override
    assert stored_secret(tmp_path, provider).get_secret_value() == saved
    monkeypatch.setenv(environment, "  ")
    assert reader(tmp_path).get_secret_value() == saved
    monkeypatch.setenv(environment, "INVALID SYNTHETIC KEY")
    with pytest.raises(AppError) as rejected:
        reader(tmp_path)
    assert rejected.value.code == "INVALID_INPUT"
    assert "INVALID SYNTHETIC KEY" not in str(rejected.value)
    monkeypatch.delenv(environment)
    store_secret(tmp_path, provider, SecretStr(""))
    assert reader(tmp_path) is None
