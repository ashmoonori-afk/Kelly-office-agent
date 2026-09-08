"""Local configuration and evidence-backed model eligibility; never store API keys."""

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationError, model_validator

from .contracts import (
    Actor,
    AppError,
    ContentTask,
    Id,
    IsoDateTime,
    ModelBinding,
    PriceSnapshot,
    PrivacyPolicy,
    ReasoningEffort,
    Role,
    Sha256,
    StrictModel,
    Usage,
    canonical_json,
)
from .safe_io import ensure_directory, guard_directory, open_regular, rename_new, validate_root

MAX_CONFIG_BYTES = 65536
MAX_EVALUATION_BYTES = 2 * 1024 * 1024


class ModelProfile(StrictModel):
    binding: ModelBinding
    price_snapshot: PriceSnapshot = Field(default_factory=PriceSnapshot)
    quality_evidence_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def supported_provider(self):
        if self.binding.provider != "xai" or self.binding.metering_mode != "api":
            raise ValueError("V1 supports the xAI cloud API only")
        if self.binding.available or self.binding.role is not None:
            raise ValueError("Availability and selected roles are runtime observations")
        if "minimal" in self.binding.reasoning_efforts:
            raise ValueError("xAI does not support the minimal reasoning option")
        return self


class TelegramConfig(StrictModel):
    enabled: bool = False
    connection_id: Id = "telegram"
    project_id: Id = "default"


class AppConfig(StrictModel):
    schema_version: Literal[1] = 1
    home: Annotated[str, Field(min_length=1, max_length=4096)] = "."
    actor_id: Id = "owner"
    project_ids: list[Id] = Field(default_factory=lambda: ["default"], min_length=1, max_length=100)
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8765
    privacy: list[PrivacyPolicy] = Field(default_factory=list, max_length=100)
    models: list[ModelProfile] = Field(default_factory=list, max_length=20)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    spend_guard_enabled: Literal[False] = False

    @model_validator(mode="after")
    def valid_scopes(self):
        projects = set(self.project_ids)
        if len(projects) != len(self.project_ids):
            raise ValueError("Duplicate project")
        if self.telegram.enabled and self.telegram.project_id not in projects:
            raise ValueError("Telegram requires a configured project")
        policies = [policy.project_id for policy in self.privacy]
        if len(set(policies)) != len(policies) or not set(policies) <= projects:
            raise ValueError("Privacy policies must have unique configured projects")
        models = [(profile.binding.provider, profile.binding.model_id) for profile in self.models]
        if len(set(models)) != len(models):
            raise ValueError("Combine roles for the same model in one profile")
        for profile in self.models:
            if not set(profile.binding.allowed_projects) <= projects:
                raise ValueError("A model cannot grant an unconfigured project")
        for policy in self.privacy:
            if not set(policy.allowed_providers) <= {"xai"}:
                raise ValueError("Provider is not implemented")
        return self

    def actor(self):
        return Actor(id=self.actor_id, project_ids=self.project_ids)

    def privacy_for(self, project_id):
        if project_id not in self.project_ids:
            raise AppError("NOT_FOUND", status=404)
        return next(
            (p for p in self.privacy if p.project_id == project_id),
            PrivacyPolicy(project_id=project_id),
        )


class QualificationCase(StrictModel):
    case_id: Id
    role: Role
    task: ContentTask | Literal["planning"]
    request_id: Annotated[str, Field(min_length=1, max_length=256)]
    usage: Usage
    fixture_sha256: Sha256
    output_sha256: Sha256
    schema_pass: bool
    numeric_pass: bool
    source_pass: bool
    policy_pass: bool
    accepted: bool
    reference_accepted: bool
    reasoning_effort: ReasoningEffort | None = None


class QualificationReport(StrictModel):
    """P12 writes this only after live, labelled evaluation; a connectivity probe is insufficient."""

    schema_version: Literal[1] = 1
    evidence_id: Id
    provider: Literal["xai"]
    model_id: Annotated[str, Field(min_length=1, max_length=200)]
    mode: Literal["live", "offline"]
    completed_at: IsoDateTime
    cases: list[QualificationCase] = Field(min_length=1, max_length=1000)


class ModelStatus(StrictModel):
    model_id: str
    state: Literal[
        "disabled",
        "key_missing",
        "availability_unchecked",
        "discovery_failed",
        "unavailable",
        "unqualified",
        "ready",
    ]
    qualification: Literal[
        "missing",
        "unverified_hash",
        "missing_file",
        "invalid_report",
        "failed_checks",
        "insufficient_coverage",
        "verified",
    ]


class CatalogResult(StrictModel):
    bindings: list[ModelBinding]
    statuses: list[ModelStatus]
    key_present: bool
    availability_checked: bool


def home_path(config: AppConfig, config_path: Path) -> Path:
    path = Path(config.home)
    if ".." in path.parts:
        raise AppError("POLICY_DENIED")
    path = path if path.is_absolute() else Path(config_path).absolute().parent / path
    return validate_root(path.absolute())


def _read_json(path, root, limit):
    with open_regular(path, root) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise AppError("LIMIT_EXCEEDED")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate configuration key")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_keys)
    except (ValueError, UnicodeError):
        raise AppError("INVALID_INPUT") from None
    return value, hashlib.sha256(data).hexdigest()


def load_config(path) -> AppConfig:
    path = Path(path).absolute()
    value, _ = _read_json(path, path.parent, MAX_CONFIG_BYTES)
    try:
        config = AppConfig.model_validate(value)
    except ValidationError:
        raise AppError("INVALID_INPUT") from None
    home_path(config, path)
    return config


def create_config(path, config: AppConfig) -> str:
    """Create once using held paths and no-replace publication. Existing configuration is preserved."""
    path = Path(path).absolute()
    config = AppConfig.model_validate(config.model_dump())
    home_path(config, path)
    data = (canonical_json(config) + "\n").encode("utf-8")
    if len(data) > MAX_CONFIG_BYTES:
        raise AppError("LIMIT_EXCEEDED")
    sha256 = hashlib.sha256(data).hexdigest()
    ensure_directory(path.parent)
    temp = path.with_name(f".config-{uuid.uuid4().hex}.tmp")
    with guard_directory(path.parent):
        try:
            with temp.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            rename_new(temp, path, expected_sha256=sha256)
        finally:
            if temp.exists():
                temp.unlink()
    return sha256


def _crypt_secret(data, *, protect):
    """Windows current-user DPAPI; never machine-wide and never a UI prompt."""
    import ctypes
    from ctypes import wintypes

    if os.name != "nt":
        raise AppError("UNSUPPORTED_CAPABILITY")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise AppError("SECRET_STORE_ERROR")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        if target.size > 65536:
            raise AppError("SECRET_STORE_ERROR")
        return ctypes.string_at(target.data, target.size)
    finally:
        if not protect:
            ctypes.memset(target.data, 0, target.size)
        kernel.LocalFree(target.data)


def stored_secret(home, provider):
    if provider not in {"xai", "telegram", "bootstrap"}:
        raise AppError("INVALID_INPUT")
    path = Path(home).absolute() / "auth" / (provider + ".dpapi")
    if not path.exists():
        return None
    with open_regular(path, Path(home).absolute()) as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise AppError("SECRET_STORE_ERROR")
    try:
        value = json.loads(_crypt_secret(data, protect=False))
        if set(value) != {"provider", "value"} or value["provider"] != provider:
            raise ValueError("Wrong secret slot")
        if not isinstance(value["value"], str) or not 1 <= len(value["value"]) <= 8192:
            raise ValueError("Invalid secret")
        return SecretStr(value["value"])
    except (ValueError, TypeError, UnicodeError):
        raise AppError("SECRET_STORE_ERROR") from None


def store_secret(home, provider, value: SecretStr):
    """Explicit local-user save; plaintext never reaches a file, DB, config or log."""
    if provider not in {"xai", "telegram", "bootstrap"} or not isinstance(value, SecretStr):
        raise AppError("INVALID_INPUT")
    plain = value.get_secret_value().strip()
    if plain:
        _validate_secret(plain, provider)
    root = Path(home).absolute()
    folder = ensure_directory(root / "auth")
    path = folder / (provider + ".dpapi")
    with guard_directory(folder):
        if path.exists():
            with open_regular(path, root):
                pass
        if not plain:
            path.unlink(missing_ok=True)
            return
        encrypted = _crypt_secret(
            canonical_json({"provider": provider, "value": plain}).encode(), protect=True
        )
        temp = folder / (uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("xb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            # This fixed private credential slot is explicitly replaceable, unlike Office outputs.
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _validate_secret(value, provider):
    if len(value) > 8192 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise AppError("INVALID_INPUT")
    if provider == "telegram" and not re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,128}", value):
        raise AppError("INVALID_INPUT")


def _secret_value(home, provider, env_name):
    value = os.environ.get(env_name)
    if value and value.strip():
        return value.strip()
    saved = stored_secret(home, provider) if home is not None else None
    return saved.get_secret_value() if saved is not None else None


def read_xai_key(home=None) -> SecretStr | None:
    """Read the provider credential without exposing its value in status or repr."""
    value = _secret_value(home, "xai", "XAI_API_KEY")
    if value is None or not value.strip():
        return None
    value = value.strip()
    _validate_secret(value, "xai")
    return SecretStr(value)


def read_telegram_token(home=None) -> SecretStr | None:
    value = _secret_value(home, "telegram", "TELEGRAM_BOT_TOKEN")
    if value is None or not value.strip():
        return None
    value = value.strip()
    _validate_secret(value, "telegram")
    return SecretStr(value)


def _qualification(profile, home):
    binding = profile.binding
    if binding.quality_evidence_id is None:
        return "missing"
    if profile.quality_evidence_sha256 is None:
        return "unverified_hash"
    path = home / "evaluations" / f"{binding.quality_evidence_id}.json"
    if not path.exists():
        return "missing_file"
    try:
        data, actual_hash = _read_json(path, home, MAX_EVALUATION_BYTES)
        report = QualificationReport.model_validate(data)
    except (AppError, ValidationError, OSError):
        return "invalid_report"
    if (
        actual_hash != profile.quality_evidence_sha256
        or report.evidence_id != binding.quality_evidence_id
        or report.provider != binding.provider
        or report.model_id != binding.model_id
        or report.mode != "live"
    ):
        return "invalid_report"
    ids = [case.case_id for case in report.cases]
    request_ids = [case.request_id for case in report.cases]
    if len(set(ids)) != len(ids) or len(set(request_ids)) != len(request_ids):
        return "invalid_report"
    if any(
        not all((case.schema_pass, case.numeric_pass, case.source_pass, case.policy_pass))
        or case.usage.usage_basis != "provider"
        or case.usage.input_tokens_total is None
        or case.usage.output_tokens_total is None
        for case in report.cases
    ):
        return "failed_checks"
    excluded = {
        "L": {"planning", "semantic_review"},
        "M": {"semantic_review"},
        "H": set(),
    }
    if any(case.task in excluded[case.role] for case in report.cases):
        return "invalid_report"
    tasks = set(binding.capabilities) - {"structured_output"}
    for role in binding.roles:
        cases = [case for case in report.cases if case.role == role]
        required = tasks - excluded[role]
        if (
            not required
            or len({case.fixture_sha256 for case in cases}) < 20
            or not required <= {case.task for case in cases}
        ):
            return "insufficient_coverage"
        accepted = sum(case.accepted for case in cases)
        reference = sum(case.reference_accepted for case in cases)
        if not accepted or accepted * 100 < reference * 100 - len(cases) * 2:
            return "failed_checks"
    if not set(binding.reasoning_efforts) <= {case.reasoning_effort for case in report.cases}:
        return "insufficient_coverage"
    return "verified"


def resolve_catalog(
    config: AppConfig, config_path, *, lookup_models=None, api_key_present=None
) -> CatalogResult:
    """Availability is an account observation; model metadata alone never proves capability."""
    config = AppConfig.model_validate(config.model_dump())
    home = home_path(config, Path(config_path))
    key_present = read_xai_key(home) is not None if api_key_present is None else api_key_present
    if type(key_present) is not bool:
        raise AppError("INVALID_INPUT")
    ids, checked, failed = set(), False, False
    if key_present and lookup_models is not None and any(p.binding.enabled for p in config.models):
        try:
            found = lookup_models()
            if (
                not isinstance(found, list)
                or len(found) > 10000
                or any(not isinstance(value, str) or not 1 <= len(value) <= 200 for value in found)
            ):
                raise AppError("INVALID_OUTPUT")
            ids, checked = set(found), True
        except (AppError, OSError, ValueError, TypeError):
            # Account/HTTP exception text can contain credentials; status is the only public output.
            failed = True
    bindings, statuses = [], []
    for profile in config.models:
        binding = profile.binding
        qualification = _qualification(profile, home)
        available = key_present and checked and binding.model_id in ids
        state = (
            "disabled"
            if not binding.enabled
            else "key_missing"
            if not key_present
            else "discovery_failed"
            if failed
            else "availability_unchecked"
            if not checked
            else "unavailable"
            if not available
            else "unqualified"
            if qualification != "verified"
            else "ready"
        )
        bindings.append(
            binding.model_copy(
                update={
                    "available": available,
                    "quality_evidence_id": binding.quality_evidence_id
                    if qualification == "verified"
                    else None,
                }
            )
        )
        statuses.append(
            ModelStatus(model_id=binding.model_id, state=state, qualification=qualification)
        )
    return CatalogResult(
        bindings=bindings, statuses=statuses, key_present=key_present, availability_checked=checked
    )


def probe_binding(
    config: AppConfig, config_path, model_id, *, lookup_models, explicit=False
) -> ModelBinding:
    """An explicit synthetic connectivity test may precede qualification; ordinary routing cannot."""
    if explicit is not True:
        raise AppError("POLICY_DENIED")
    catalog = resolve_catalog(config, config_path, lookup_models=lookup_models)
    binding = next((b for b in catalog.bindings if b.model_id == model_id), None)
    if binding is None or not binding.enabled or not binding.available:
        raise AppError("NO_CAPABLE_MODEL")
    return binding


def register_folder(conn, home, actor, project_id, path, *, explicit_confirmed=False):
    """Register an explicit local-user selection. Models never receive this path or a setter."""
    from .db import require_project, set_value, transaction

    if explicit_confirmed is not True:
        raise AppError("POLICY_DENIED")
    require_project(actor, project_id)
    path, home = Path(path), Path(home).absolute()
    # A grant is immutable: a changed location receives a new ID and a new bot confirmation.
    with guard_directory(path):
        if path == home or path.is_relative_to(home):
            raise AppError("POLICY_DENIED")
        folder_id = uuid.uuid4().hex
        record = {
            "id": folder_id,
            "actor_id": actor.id,
            "project_id": project_id,
            "path": str(path),
        }
        with transaction(conn):
            set_value(conn, "folder:" + folder_id, record)
    return {key: value for key, value in record.items() if key != "path"}


def get_registered_folder(conn, folder_id, actor, project_id):
    from .db import get_value, require_project

    require_project(actor, project_id)
    record = get_value(conn, "folder:" + folder_id)
    if not isinstance(record, dict) or (record.get("actor_id"), record.get("project_id")) != (
        actor.id,
        project_id,
    ):
        raise AppError("NOT_FOUND", status=404)
    return Path(record["path"])


def runtime_config(base: AppConfig, conn) -> AppConfig:
    """Only the authenticated settings UI writes these bounded, non-secret overrides."""
    from .db import get_value

    overrides = get_value(conn, "settings", {})
    if not isinstance(overrides, dict) or set(overrides) - {"models", "privacy", "telegram"}:
        raise AppError("INVALID_INPUT")
    try:
        return AppConfig.model_validate({**base.model_dump(), **overrides})
    except ValidationError:
        raise AppError("INVALID_INPUT") from None
