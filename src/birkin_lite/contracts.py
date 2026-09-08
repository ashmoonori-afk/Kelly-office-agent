"""Bounded data contracts. These describe work; none confer execution authority."""

import hashlib
import json
import re
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, model_validator


def canonical_json(value) -> str:
    """Stable approval/idempotency bytes: no floats, implicit casts or non-JSON keys."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")

    def check(item, depth=0):
        if depth > 32:
            raise ValueError("JSON nesting exceeds 32 levels")
        if item is None or type(item) in (str, int, bool):
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("JSON keys must be strings")
                check(child, depth + 1)
            return
        raise ValueError("Use JSON primitives; decimal amounts must be strings")

    check(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Blank text is not allowed")
    return value


def _iso_datetime(value: str) -> str:
    pattern = (
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
    )
    if not re.fullmatch(pattern, value):
        raise ValueError("An ISO datetime with timezone is required")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("A timezone is required")
    return value


Id = Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")]
Text = Annotated[str, Field(min_length=1, max_length=8000), AfterValidator(_nonblank)]
ShortText = Annotated[str, Field(min_length=1, max_length=500), AfterValidator(_nonblank)]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
IsoDateTime = Annotated[str, Field(max_length=40), AfterValidator(_iso_datetime)]
NonnegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(ge=1)]
Role = Literal["L", "M", "H"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]
Kind = Literal["mechanical", "simple", "standard", "complex"]
Clarity = Literal["clear", "uncertain", "missing_input"]
Signal = Literal[
    "fixed_transform",
    "extraction",
    "summarization",
    "synthesis",
    "conflicting_sources",
    "coupled_constraints",
    "causal_judgment",
    "alternative_evaluation",
]
ToolName = Literal[
    "source.inspect",
    "source.extract",
    "source.search",
    "table.aggregate",
    "document.create",
    "artifact.validate",
    "artifact.publish",
    "memory.search",
    "memory.save",
    "automation.propose",
]
ContentTask = Literal[
    "summarize",
    "report_prose",
    "meeting_actions",
    "draft_reply",
    "search_answer",
    "analyze",
    "semantic_review",
]
OutputSchema = Literal[
    "SourceInfoV1",
    "EvidenceListV1",
    "AggregateV1",
    "ReportSectionsV1",
    "MeetingActionsV1",
    "DraftReplyV1",
    "SearchAnswerV1",
    "ArtifactV1",
    "ValidationResultV1",
    "ReceiptV1",
    "MemoryListV1",
    "AutomationProposalV1",
]
OUTPUT_SCHEMA_KEYS = {
    "SourceInfoV1": "source",
    "EvidenceListV1": "evidence",
    "AggregateV1": "aggregate",
    "ReportSectionsV1": "sections",
    "MeetingActionsV1": "actions",
    "DraftReplyV1": "draft",
    "SearchAnswerV1": "answer",
    "ArtifactV1": "artifact",
    "ValidationResultV1": "validation",
    "ReceiptV1": "receipt",
    "MemoryListV1": "memories",
    "AutomationProposalV1": "proposal",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class Actor(StrictModel):
    """Internal authenticated identity. Never accepted in an external request body."""

    id: Id
    project_ids: list[Id] = Field(max_length=100)


class RunRequest(StrictModel):
    request: Text
    project_id: Id
    artifact_ids: list[Id] = Field(default_factory=list, max_length=20)
    quality_mode: Literal["balanced", "high"] = "balanced"
    output_format: Literal["text", "md", "csv", "xlsx", "docx"] = "text"


class Requirement(StrictModel):
    id: Id
    text: ShortText
    required: bool = True


class TaskSpec(StrictModel):
    intent: ShortText
    source_ids: list[Id] = Field(default_factory=list, max_length=20)
    inputs: dict[str, JsonValue] = Field(default_factory=dict, max_length=64)
    required_capabilities: list[Id] = Field(default_factory=list, max_length=20)
    acceptance: list[ShortText] = Field(min_length=1, max_length=20)
    ambiguity: Clarity = "clear"

    @model_validator(mode="after")
    def json_inputs(self):
        if len(canonical_json(self.inputs).encode("utf-8")) > 65536:
            raise ValueError("Task inputs exceed 64 KiB")
        return self


class StepInput(StrictModel):
    step_id: Id
    output_key: Id


class ArtifactInput(StrictModel):
    artifact_id: Id


class ToolExecutor(StrictModel):
    type: Literal["tool"] = "tool"
    tool_name: ToolName
    args: dict[str, JsonValue] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def json_args(self):
        if len(canonical_json(self.args).encode("utf-8")) > 65536:
            raise ValueError("Tool arguments exceed 64 KiB")
        return self


class ModelExecutor(StrictModel):
    type: Literal["model"] = "model"
    role: Role
    task: ContentTask


Executor = Annotated[ToolExecutor | ModelExecutor, Field(discriminator="type")]


class TodoV1(StrictModel):
    id: Id
    title: Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_nonblank)]
    requirement_ids: list[Id] = Field(min_length=1, max_length=20)
    depends_on: list[Id] = Field(default_factory=list, max_length=7)
    inputs: list[StepInput | ArtifactInput] = Field(default_factory=list, max_length=20)
    kind: Kind
    reason: Annotated[str, Field(min_length=1, max_length=120), AfterValidator(_nonblank)]
    signals: list[Signal] = Field(max_length=8)
    clarity: Clarity
    usefulness: Literal["required", "useful", "optional"]
    executor: Executor
    output_schema: OutputSchema
    done_when: list[ShortText] = Field(min_length=1, max_length=20)
    verify_with: list[Id] = Field(default_factory=list, max_length=20)
    status: Literal[
        "pending",
        "running",
        "checking",
        "awaiting_input",
        "awaiting_approval",
        "succeeded",
        "failed",
        "cancelled",
        "skipped",
        "outcome_unknown",
    ] = "pending"


class PlanV1(StrictModel):
    version: Annotated[int, Field(ge=1, le=1)] = 1
    requirements: list[Requirement] = Field(min_length=1, max_length=20)
    todos: list[TodoV1] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def valid_plan(self):
        requirements = {item.id for item in self.requirements}
        if len(requirements) != len(self.requirements):
            raise ValueError("Duplicate requirement id")
        earlier = {}
        covered = set()
        for todo in self.todos:
            if todo.id in earlier:
                raise ValueError("Duplicate TODO id")
            if len(set(todo.depends_on)) != len(todo.depends_on):
                raise ValueError("Duplicate dependency")
            if not set(todo.depends_on) <= earlier.keys():
                raise ValueError("Dependencies must reference earlier TODOs")
            if todo.usefulness != "optional" and any(
                earlier[dep].usefulness == "optional" for dep in todo.depends_on
            ):
                raise ValueError("An executing TODO cannot depend on an optional skipped TODO")
            if not set(todo.requirement_ids) <= requirements:
                raise ValueError("Unknown requirement id")
            for ref in todo.inputs:
                if isinstance(ref, StepInput):
                    if ref.step_id not in todo.depends_on:
                        raise ValueError("Input step must be a declared earlier dependency")
                    if ref.output_key != OUTPUT_SCHEMA_KEYS[earlier[ref.step_id].output_schema]:
                        raise ValueError("Input output key does not exist in referenced schema")
            if todo.usefulness == "required":
                covered.update(todo.requirement_ids)
            earlier[todo.id] = todo
        if not {r.id for r in self.requirements if r.required} <= covered:
            raise ValueError("A required requirement is missing a required TODO")
        return self


class Evidence(StrictModel):
    artifact_id: Id
    sha256: Sha256
    locator: Annotated[str, Field(min_length=1, max_length=512)]
    quote: Annotated[str, Field(max_length=16000)]
    extraction_version: Annotated[str, Field(min_length=1, max_length=64)]
    observed_at: IsoDateTime


class ValidationResult(StrictModel):
    status: Literal["pass", "fail", "needs_input"]
    errors: list[ShortText] = Field(default_factory=list, max_length=64)
    evidence_refs: list[Id] = Field(default_factory=list, max_length=100)
    checked_layers: list[Id] = Field(default_factory=list, max_length=20)


class Usage(StrictModel):
    input_tokens_total: NonnegativeInt | None = None
    cached_input_tokens: NonnegativeInt | None = None
    cache_write_tokens: NonnegativeInt | None = None
    output_tokens_total: NonnegativeInt | None = None
    reasoning_tokens: NonnegativeInt | None = None
    usage_basis: Literal["estimated", "provider", "unknown"] = "unknown"

    @model_validator(mode="after")
    def valid_subsets(self):
        for subset, total in (
            (self.cached_input_tokens, self.input_tokens_total),
            (self.reasoning_tokens, self.output_tokens_total),
        ):
            if subset is not None and (total is None or subset > total):
                raise ValueError("Usage subset requires a matching total and cannot exceed it")
        return self


class ModelRequest(StrictModel):
    model_id: Annotated[str, Field(min_length=1, max_length=200)]
    prompt_version: Id
    output_schema: dict[str, JsonValue] = Field(max_length=100)
    context: Annotated[str, Field(min_length=1, max_length=131072)]
    max_output: Annotated[int, Field(ge=1, le=131072)]
    timeout_s: Annotated[int, Field(ge=1, le=300)] = 60
    reasoning_effort: ReasoningEffort | None = None

    @model_validator(mode="after")
    def json_schema(self):
        if len(canonical_json(self.output_schema).encode("utf-8")) > 65536:
            raise ValueError("Output schema exceeds 64 KiB")
        return self


class ModelResult(StrictModel):
    parsed_output: dict[str, JsonValue]
    normalized_usage: Usage = Field(default_factory=Usage)
    request_id: Annotated[str, Field(max_length=256)] | None = None
    finish_reason: Annotated[str, Field(max_length=64)]
    latency_ms: NonnegativeInt
    raw_usage: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def json_output(self):
        if len(canonical_json(self.parsed_output).encode("utf-8")) > 1024 * 1024:
            raise ValueError("Model result exceeds 1 MiB")
        if len(canonical_json(self.raw_usage).encode("utf-8")) > 65536:
            raise ValueError("Usage metadata exceeds 64 KiB")
        return self


class ActionProposal(StrictModel):
    schema_version: Annotated[int, Field(ge=1, le=1)] = 1
    actor_id: Id
    project_id: Id
    run_id: Id
    step_id: Id
    tool: ToolName
    args: dict[str, JsonValue] = Field(max_length=64)
    source_hashes: dict[Id, Sha256] = Field(max_length=20)
    draft_hash: Sha256
    destination: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    recipient_ref: Id | None = None
    scope_revision: PositiveInt
    expires_at: IsoDateTime
    impact: ShortText

    @model_validator(mode="after")
    def json_args(self):
        if len(canonical_json(self.args).encode("utf-8")) > 65536:
            raise ValueError("Action arguments exceed 64 KiB")
        return self


class Receipt(StrictModel):
    run_id: Id
    step_id: Id
    artifact_id: Id | None = None
    artifact_hash: Sha256 | None = None
    remote_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    validation: ValidationResult
    usage_state: Literal["not_used", "provider", "estimated", "unknown"] = "not_used"
    source_versions: dict[Id, Sha256] = Field(default_factory=dict, max_length=20)
    parser_version: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    template_version: Annotated[str, Field(min_length=1, max_length=64)] | None = None

    @model_validator(mode="after")
    def has_result(self):
        if self.artifact_hash is None and self.remote_id is None:
            raise ValueError("A receipt requires an artifact hash or remote id")
        return self


class TaskFacts(StrictModel):
    supported: bool = True
    required_inputs_present: bool = True
    certified_deterministic: bool = False
    privacy_allows_candidate: bool = True
    capable_model_available: bool = True
    conflicting_sources: bool = False
    causal_judgment: bool = False
    alternative_evaluation: bool = False
    coupled_constraints: bool = False
    requested_or_supporting: bool = True


class PlanningResultV1(StrictModel):
    task_spec: TaskSpec
    plan: PlanV1


class ModelBinding(StrictModel):
    provider: Id
    model_id: Annotated[str, Field(min_length=1, max_length=200)]
    roles: list[Role] = Field(min_length=1, max_length=3)
    role: Role | None = None
    enabled: bool = False
    available: bool = False
    allowed_projects: list[Id] = Field(default_factory=list, max_length=100)
    capabilities: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(max_length=100)
    context_window: Annotated[int, Field(ge=1024, le=2000000)]
    max_output: Annotated[int, Field(ge=1, le=131072)]
    request_timeout_s: Annotated[int, Field(ge=1, le=300)] = 60
    reasoning_efforts: list[ReasoningEffort] = Field(default_factory=list, max_length=6)
    reasoning_effort: ReasoningEffort | None = None
    quality_evidence_id: Id | None = None
    metering_mode: Literal["api", "subscription", "unknown", "local"] = "unknown"

    @model_validator(mode="after")
    def selected_options(self):
        if len(set(self.roles)) != len(self.roles) or (
            self.role is not None and self.role not in self.roles
        ):
            raise ValueError("Binding role must be eligible")
        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in self.reasoning_efforts
        ):
            raise ValueError("Unsupported reasoning option")
        if self.max_output >= self.context_window:
            raise ValueError("Output must leave room for input")
        return self


class PrivacyPolicy(StrictModel):
    project_id: Id
    allowed_providers: list[Id] = Field(default_factory=list, max_length=20)
    allow_cloud: bool = False
    local_only: bool = False
    revision: PositiveInt = 1


class EvaluationRoute(StrictModel):
    """Explicit Python-only experiment selection; never a request/config field."""

    arm: Literal["A", "B"]
    actor_id: Id
    project_id: Id
    run_id: Id
    run_revision: PositiveInt
    todo_id: Id
    todo_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    binding_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    attempt: Annotated[int, Field(ge=1, le=2)]
    privacy: PrivacyPolicy

    def check(self, todo, binding, run, actor, *, snapshot):
        EvaluationRoute.model_validate(self.model_dump())
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("actor_id") != actor.id
            or snapshot.get("project_id") != run["project_id"]
            or snapshot.get("arm") != self.arm
            or snapshot.get("privacy") != self.privacy.model_dump()
        ):
            raise AppError("POLICY_DENIED")
        role = "H" if self.arm == "A" else "L" if self.attempt == 1 else "M"
        if (
            self.actor_id != actor.id
            or self.project_id not in actor.project_ids
            or self.project_id != run["project_id"]
            or self.run_id != run["id"]
            or self.run_revision != run["revision"]
            or self.todo_id != todo.id
            or self.todo_digest != digest(todo)
            or self.binding_digest != digest(binding)
            or not isinstance(todo.executor, ModelExecutor)
            or todo.executor.task == "semantic_review"
            or {"simple": "L", "standard": "M", "complex": "H"}.get(todo.kind) != todo.executor.role
            or todo.usefulness == "optional"
            or todo.status == "skipped"
            or todo.clarity == "missing_input"
        ):
            raise AppError("PLAN_INVALID")
        if (
            binding.role != role
            or role not in binding.roles
            or not binding.enabled
            or not binding.available
            or binding.quality_evidence_id is None
            or self.project_id not in binding.allowed_projects
            or not {"structured_output", todo.executor.task} <= set(binding.capabilities)
        ):
            raise AppError("NO_CAPABLE_MODEL")
        if (
            self.privacy.project_id != self.project_id
            or binding.provider not in self.privacy.allowed_providers
            or binding.metering_mode != "local"
            and (self.privacy.local_only or not self.privacy.allow_cloud)
        ):
            raise AppError("POLICY_DENIED")
        # The isolated evaluation run seals the same catalog and privacy for every arm.
        from .routing import _choose_binding

        qualified = _choose_binding(
            role, todo.executor.task, snapshot.get("catalog", []), self.privacy
        )
        if binding != qualified:
            raise AppError("NO_CAPABLE_MODEL")


class TokenRates(StrictModel):
    """Integer micro USD per million tokens, never a routing input."""

    input: NonnegativeInt | None = None
    cached_input: NonnegativeInt | None = None
    cache_write: NonnegativeInt | None = None
    output: NonnegativeInt | None = None


class PriceSnapshot(StrictModel):
    rates: TokenRates = Field(default_factory=TokenRates)
    checked_at: IsoDateTime | None = None
    source_url: (
        Annotated[str, Field(min_length=1, max_length=2048, pattern=r"^https://[^\s]+$")] | None
    ) = None
    cache_write_billing: Literal["unknown", "included", "separate"] = "unknown"


class DocumentTable(StrictModel):
    headers: list[str] = Field(min_length=1, max_length=100)
    rows: list[list[str | int | bool | None]] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def rectangle(self):
        if any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("Table row widths must match headers")
        return self


class DocumentContent(StrictModel):
    title: str = Field(default="", max_length=1000)
    paragraphs: list[str] = Field(default_factory=list, max_length=1000)
    bullets: list[str] = Field(default_factory=list, max_length=1000)
    tables: list[DocumentTable] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def bounded(self):
        if len(canonical_json(self.model_dump()).encode("utf-8")) > 65536:
            raise ValueError("Document content exceeds 64 KiB")
        strings = [self.title, *self.paragraphs, *self.bullets]
        for table in self.tables:
            strings.extend(table.headers)
            strings.extend(value for row in table.rows for value in row if isinstance(value, str))
        if any(
            (ord(char) < 32 and char not in "\t\r\n") or 0xD800 <= ord(char) <= 0xDFFF
            for text in strings
            for char in text
        ):
            raise ValueError("Document text contains an unsupported control character")
        return self


class Claim(StrictModel):
    id: Id
    text: Text
    kind: Literal["fact", "inference"] = "fact"
    evidence_ids: list[Id] = Field(default_factory=list, max_length=6)


class NumericClaim(StrictModel):
    id: Id
    value: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[+-]?\d+(?:\.\d+)?$")]
    aggregate_step: Id
    column: Annotated[str, Field(min_length=1, max_length=200)]
    group: Annotated[str, Field(max_length=500)] | None = None
    operation: Literal["sum", "average", "ratio"] = "sum"
    unit: Annotated[str, Field(min_length=1, max_length=64)]
    denominator: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class Uncertainty(StrictModel):
    id: Id
    text: ShortText
    evidence_ids: list[Id] = Field(default_factory=list, max_length=6)


class ClaimPart(StrictModel):
    claim_id: Id


class NumericPart(StrictModel):
    numeric_id: Id


class UncertaintyPart(StrictModel):
    uncertainty_id: Id


TextParts = Annotated[list[ClaimPart | NumericPart | UncertaintyPart], Field(max_length=100)]


class ReferenceTable(StrictModel):
    headers: list[TextParts] = Field(min_length=1, max_length=100)
    rows: list[list[TextParts]] = Field(default_factory=list, max_length=2000)


class ReferenceContent(StrictModel):
    """Model layout contains references only; DocumentContent remains the tool contract."""

    title: TextParts = Field(default_factory=list)
    paragraphs: list[TextParts] = Field(default_factory=list, max_length=1000)
    bullets: list[TextParts] = Field(default_factory=list, max_length=1000)
    tables: list[ReferenceTable] = Field(default_factory=list, max_length=20)


class Narrative(StrictModel):
    content: ReferenceContent
    claims: list[Claim] = Field(default_factory=list, max_length=100)
    numeric_claims: list[NumericClaim] = Field(default_factory=list, max_length=100)
    uncertainties: list[Uncertainty] = Field(default_factory=list, max_length=20)


class ReportSectionsV1(StrictModel):
    sections: Narrative


class MeetingAction(StrictModel):
    action: ShortText
    owner: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    due_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None
    due_text: ShortText | None = None
    evidence_ids: list[Id] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def calendar_date(self):
        if self.due_date is not None:
            date.fromisoformat(self.due_date)
        return self


class MeetingActionsV1(StrictModel):
    actions: list[MeetingAction] = Field(max_length=100)


class DraftReply(StrictModel):
    subject: TextParts = Field(min_length=1)
    body: TextParts = Field(min_length=1)
    recipient: TextParts | None = Field(default=None, min_length=1)
    claims: list[Claim] = Field(default_factory=list, max_length=100)
    numeric_claims: list[NumericClaim] = Field(default_factory=list, max_length=100)
    uncertainties: list[Uncertainty] = Field(default_factory=list, max_length=20)


class DraftReplyV1(StrictModel):
    draft: DraftReply


class SearchAnswer(StrictModel):
    found: bool
    text: TextParts = Field(min_length=1)
    claims: list[Claim] = Field(default_factory=list, max_length=100)
    numeric_claims: list[NumericClaim] = Field(default_factory=list, max_length=100)
    uncertainties: list[Uncertainty] = Field(default_factory=list, max_length=20)


class SearchAnswerV1(StrictModel):
    answer: SearchAnswer


class ValidationResultV1(StrictModel):
    validation: ValidationResult


MODEL_OUTPUT_TYPES = {
    "summarize": ReportSectionsV1,
    "report_prose": ReportSectionsV1,
    "analyze": ReportSectionsV1,
    "meeting_actions": MeetingActionsV1,
    "draft_reply": DraftReplyV1,
    "search_answer": SearchAnswerV1,
    "semantic_review": ValidationResultV1,
}


def evidence_id(evidence: Evidence | dict) -> str:
    value = evidence.model_dump() if isinstance(evidence, Evidence) else dict(evidence)
    value.pop("observed_at", None)
    return digest(value)[:24]


SAFE_MESSAGES = {
    "MISSING_INPUT": "필요한 항목을 알려 주세요.",
    "BUDGET_EXCEEDED": "이번 작업 예산이 부족합니다.",
    "SOURCE_CHANGED": "자료가 바뀌어 다시 검토해야 합니다.",
    "POLICY_DENIED": "이 작업은 현재 허용 범위에 없습니다.",
    "UNSUPPORTED_CAPABILITY": "지원하지 않는 작업입니다.",
    "USAGE_UNKNOWN": "사용량을 확인 중입니다.",
    "OUTCOME_UNKNOWN": "처리 여부 확인이 필요합니다.",
    "PLAN_INVALID": "작업 계획을 확인해야 합니다.",
    "NO_CAPABLE_MODEL": "이 업무를 수행할 수 있는 모델 설정이 없습니다.",
    "PROVIDER_RESPONSE_TOO_LARGE": "모델 응답이 크기 제한을 넘었습니다.",
    "PROVIDER_REFUSAL": "모델이 이 요청에 대한 답변을 제공하지 않았습니다.",
    "PROVIDER_AUTH_ERROR": "모델 연결 키와 계정 권한을 확인해 주세요.",
    "PROVIDER_RATE_LIMITED": "모델 호출이 일시적으로 제한되었습니다.",
    "PROVIDER_UNAVAILABLE": "모델 서비스에 연결하지 못했습니다.",
    "PROVIDER_TIMEOUT": "모델 응답 시간이 제한을 넘었습니다. 사용량 확인이 필요할 수 있습니다.",
    "PROVIDER_RETRY_LATER": "모델 서비스가 나중에 다시 요청하도록 안내했습니다.",
    "TELEGRAM_AUTH_ERROR": "Telegram 봇 토큰을 확인해 주세요.",
    "TELEGRAM_WEBHOOK_ACTIVE": "이 봇은 webhook에 연결돼 있습니다. 기존 연결을 해제한 뒤 다시 연결해 주세요.",
    "TELEGRAM_INVALID_RESPONSE": "Telegram 응답을 확인하지 못했습니다. 전달 상태를 확인해 주세요.",
    "TELEGRAM_NETWORK_ERROR": "Telegram 연결이 끊겼습니다. 전달 여부를 확인해 주세요.",
    "TELEGRAM_RATE_LIMIT": "Telegram 요청이 일시적으로 제한돼 지정된 시간까지 기다립니다.",
    "TELEGRAM_RESPONSE_LIMIT": "Telegram 응답이 크기 제한을 넘었습니다.",
    "TELEGRAM_SEND_FAILED": "Telegram 전달을 확인하지 못했습니다. 전달 상태와 연결을 확인해 주세요.",
    "APPROVAL_EXPIRED": "승인 시간이 만료됐습니다. 최신 내용을 다시 확인해 주세요.",
    "SECRET_STORE_ERROR": "현재 Windows 계정의 보호된 키 저장소를 확인해 주세요.",
    "SECRET_ENV_ACTIVE": "환경변수의 키가 우선 적용됩니다. 환경변수를 해제한 뒤 변경해 주세요.",
    "AUTH_REQUIRED": "로컬 연결 코드로 로그인해 주세요.",
    "RESTORE_REVIEW_REQUIRED": "백업을 복구했습니다. 업무 범위와 연결 권한을 다시 확인해 주세요.",
    "ALREADY_RUNNING": "이 데이터 폴더의 서버가 이미 실행 중입니다.",
    "QUEUE_FULL": "대기 업무가 가득 찼습니다. 잠시 뒤 다시 실행해 주세요.",
    "OUTPUT_TRUNCATED": "모델 출력이 길이 제한에 도달했습니다.",
    "CANCELLED": "작업을 취소했습니다.",
    "CONFLICT": "내용이 변경되었습니다. 새로 확인해 주세요.",
    "NOT_FOUND": "요청한 항목을 찾을 수 없습니다.",
    "INVALID_INPUT": "입력 내용을 확인해 주세요.",
    "LIMIT_EXCEEDED": "처리 범위를 줄여 주세요. 파일 또는 항목 수가 제한을 넘었습니다.",
    "PARSER_TIMEOUT": "자료를 읽는 시간이 제한을 넘었습니다. 범위를 줄여 주세요.",
    "PARSER_FAILED": "자료 형식을 읽지 못했습니다. 파일을 확인해 주세요.",
    "PACKAGE_INVALID": "문서 파일 구조를 확인하지 못했습니다.",
    "FORMAT_MISMATCH": "파일 내용과 확장자가 맞지 않습니다.",
    "OUTPUT_EXISTS": "결과 파일이 이미 있어 덮어쓰지 않았습니다.",
    "CALL_LIMIT": "이번 작업의 호출 횟수 제한에 도달했습니다.",
    "INVALID_OUTPUT": "결과가 필요한 형식을 충족하지 못했습니다.",
    "FORMULA_UNVERIFIED": "수식의 계산값을 확인할 수 없습니다. 값이 확정된 자료를 넣어 주세요.",
    "CONTEXT_LIMIT": "자료 범위를 나누거나 줄여 주세요. 근거를 생략하지 않고 확인하겠습니다.",
    "OCR_UNAVAILABLE": "이 PDF에서 읽을 수 있는 텍스트가 없습니다. 텍스트 자료가 필요합니다.",
    "NUMERIC_MISMATCH": "결과 숫자가 원본 계산과 일치하지 않습니다.",
    "NUMERIC_REFERENCE_INVALID": "숫자의 집계 범위·열·단위를 확인하지 못했습니다.",
    "MISSING_EVIDENCE": "결과를 뒷받침할 근거가 부족합니다. 자료와 인용을 확인해 주세요.",
    "OWNER_UNGROUNDED": "담당자를 근거에서 확인하지 못했습니다. 미정으로 두거나 근거를 보충해 주세요.",
    "DUE_UNGROUNDED": "기한을 근거에서 확인하지 못했습니다. 미정으로 두거나 원문을 확인해 주세요.",
    "SEMANTIC_REVIEW_REQUIRED": "문장에 적힌 업무 배정의 의미를 추가로 검토해야 합니다.",
    "SEMANTIC_REVIEW_FAILED": "의미 검토에서 결과와 근거가 일치하는지 확인하지 못했습니다.",
    "EVIDENCE_MISMATCH": "인용한 내용이나 위치가 원본과 일치하지 않습니다.",
    "STRUCTURE_MISMATCH": "생성한 문서의 내용 또는 구조가 예상 결과와 다릅니다.",
    "RENDER_UNAVAILABLE": "파일 구조를 확인했습니다. 화면 모양은 아직 검증하지 않았습니다.",
    "INTERNAL_ERROR": "요청을 처리하지 못했습니다.",
}


class AppError(Exception):
    def __init__(self, code: str, message_ko: str | None = None, status: int = 400):
        self.code = code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else "INTERNAL_ERROR"
        # Only registered public wording may cross the HTTP/Telegram error boundary.
        self.message_ko = (
            message_ko
            if message_ko in SAFE_MESSAGES.values()
            else SAFE_MESSAGES.get(self.code, SAFE_MESSAGES["INTERNAL_ERROR"])
        )
        self.status = status
        super().__init__(self.message_ko)
