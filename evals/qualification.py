"""Isolated candidate exams and evidence-bound qualification publication. No ordinary bypass."""

import argparse
import hashlib
import io
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from pydantic import Field

from kelly import artifacts, config, context, db, engine, tools, usage, verification
from kelly.contracts import (
    MODEL_OUTPUT_TYPES,
    Actor,
    AppError,
    ContentTask,
    Id,
    ModelExecutor,
    PlanningResultV1,
    PlanV1,
    ReasoningEffort,
    Requirement,
    Role,
    RunRequest,
    Sha256,
    StrictModel,
    TaskSpec,
    TodoV1,
    Usage,
    ValidationResult,
    canonical_json,
    digest,
)
from kelly.providers import TransportError, XAIClient, metered_complete
from kelly.safe_io import (
    ensure_directory,
    guard_directory,
    rename_new,
    validate_root,
)

from . import execute as office_execute
from .execute import source_bytes, task_spec
from .run import blind_id, condition_hash, import_labels, load_cases

ACTOR = Actor(id="eval-owner", project_ids=["eval"])
TASKS = {
    "extraction": "meeting_actions",
    "summary": "summarize",
    "multi_document": "report_prose",
    "conflict_missing": "analyze",
}
INSTRUCTIONS = {
    "summarize": "제공 자료에서 원래 요청과 관련된 핵심 사실을 요약하세요.",
    "report_prose": "제공 자료에서 원래 요청과 관련된 사실을 근거가 있는 보고서 문단으로 작성하세요.",
    "meeting_actions": "제공 자료의 명시적인 후속 업무, 담당자, 기한을 추출하세요. 없는 항목은 만들지 마세요.",
    "draft_reply": "원래 요청에 대한 이메일 답장 초안을 제공 자료에 근거해 작성하세요. 받는 사람이 없으면 null로 두세요. 전송하지 마세요.",
    "search_answer": "제공 자료 안에서 원래 요청의 답을 찾아 근거와 함께 답하세요. 자료에 답이 없으면 found=false로 작성하세요.",
    "analyze": "원래 요청과 관련된 자료의 충돌, 누락, 불확실성을 분석하고 사실과 추론을 구분하세요.",
    "semantic_review": "별도로 제공된 후보 출력이 원래 요청과 자료에 부합하는지 검토하세요. checked_layers는 semantic_review 하나이며 실제 근거 ID를 인용하세요.",
    "planning": "원래 요청을 실행 가능한 TODO와 TaskSpec으로 분해하세요. 제공되지 않은 자료나 권한을 가정하지 마세요.",
}
EXCLUDED = {"L": {"planning", "semantic_review"}, "M": {"semantic_review"}, "H": set()}
LIMIT = config.MAX_EVALUATION_BYTES


class ExamManifest(StrictModel):
    version: Literal[1] = 1
    config_path: str
    config_sha256: Sha256
    environment_sha256: Sha256
    workspace: str
    profile: config.ModelProfile
    role: Role
    task: ContentTask | Literal["planning"]
    reasoning_effort: ReasoningEffort | None = None
    instructions: str
    output_schema: dict
    requests: dict[Id, str] = Field(min_length=1, max_length=120)
    case_hashes: dict[Id, Sha256] = Field(min_length=1, max_length=120)
    source_hashes: dict[Id, dict[Id, Sha256]] = Field(min_length=1, max_length=120)
    review_outputs: dict[Id, dict] = Field(default_factory=dict, max_length=120)
    max_generations: int = Field(ge=1, le=120)
    max_http_requests: int = Field(ge=2, le=121)


def _read(root, name):
    return config._read_json(Path(root) / name, Path(root), LIMIT)


def _write(root, name, value):
    root = ensure_directory(Path(root).absolute())
    if Path(name).name != name:
        raise AppError("POLICY_DENIED")
    data = canonical_json(value).encode("utf-8")
    if len(data) > LIMIT:
        raise AppError("LIMIT_EXCEEDED")
    sha = hashlib.sha256(data).hexdigest()
    target, temporary = root / name, root / (uuid.uuid4().hex + ".part")
    with guard_directory(root):
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            rename_new(temporary, target, expected_sha256=sha)
        finally:
            temporary.unlink(missing_ok=True)
    return sha


def prepare_trials(
    config_path,
    *,
    model_id,
    role,
    task,
    case_ids,
    workspace,
    review_outputs=None,
    reasoning_effort=None,
):
    """Read-only manifest preparation: no credential read, provider client, directory or file write."""
    path = Path(config_path).absolute()
    configured = config.load_config(path)
    profile = next((p for p in configured.models if p.binding.model_id == model_id), None)
    policy = configured.privacy_for("eval")
    supported = {*INSTRUCTIONS, "structured_output"}
    if (
        profile is None
        or not profile.binding.enabled
        or role not in EXCLUDED
        or role not in profile.binding.roles
        or task in EXCLUDED[role]
        or not {"structured_output", task} <= set(profile.binding.capabilities)
        or set(profile.binding.capabilities) - supported
    ):
        raise AppError("NO_CAPABLE_MODEL")
    if (
        configured.actor() != ACTOR
        or "eval" not in profile.binding.allowed_projects
        or not policy.allow_cloud
        or policy.local_only
        or "xai" not in policy.allowed_providers
    ):
        raise AppError("POLICY_DENIED")
    if reasoning_effort is None:
        preferred = {"L": ("none", "low"), "M": ("medium",), "H": ("high",)}[role]
        reasoning_effort = next(
            (value for value in preferred if value in profile.binding.reasoning_efforts), None
        )
    if reasoning_effort is not None and reasoning_effort not in profile.binding.reasoning_efforts:
        raise AppError("UNSUPPORTED_CAPABILITY")
    cases = {case.id: case for case in load_cases()}
    if not case_ids or len(set(case_ids)) != len(case_ids) or not set(case_ids) <= cases.keys():
        raise AppError("INVALID_INPUT")
    reviews = review_outputs or {}
    if (
        task == "semantic_review"
        and set(reviews) != set(case_ids)
        or task != "semantic_review"
        and reviews
    ):
        raise AppError("MISSING_INPUT")
    for identity in case_ids:
        case = cases[identity]
        if case.category == "automation":
            raise AppError("UNSUPPORTED_CAPABILITY")
        names = [source.filename for source in case.sources]
        if len(set(names)) != len(names) or any(
            not name
            or name in {".", ".."}
            or Path(name).name != name
            or "/" in name
            or "\\" in name
            for name in names
        ):
            raise AppError("POLICY_DENIED")
        # Role exams reuse immutable text/CSV fixtures, not the original Office workload's gold.
        # Workbook materialization has ZIP timestamps; it cannot certify identical byte inputs here.
        if any(
            Path(source.filename).suffix.lower() not in {".txt", ".md", ".csv"} or source.fixture
            for source in case.sources
        ):
            raise AppError("UNSUPPORTED_CAPABILITY")
        if task == "semantic_review":
            source_task = TASKS.get(case.category)
            if source_task is None:
                raise AppError("UNSUPPORTED_CAPABILITY")
            MODEL_OUTPUT_TYPES[source_task].model_validate(reviews[identity])
    directory = validate_root(Path(workspace).absolute())
    if directory == config.home_path(configured, path) or directory.exists():
        raise AppError("CONFLICT", status=409)
    manifest = ExamManifest(
        config_path=str(path),
        config_sha256=digest(configured),
        workspace=str(directory),
        environment_sha256=digest(
            {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [
                    *Path(engine.__file__).parent.glob("*.py"),
                    Path(__file__),
                    Path(office_execute.__file__),
                ]
            }
        ),
        profile=profile,
        role=role,
        task=task,
        reasoning_effort=reasoning_effort,
        instructions=INSTRUCTIONS[task],
        output_schema=(
            PlanningResultV1 if task == "planning" else MODEL_OUTPUT_TYPES[task]
        ).model_json_schema(),
        requests={
            identity: cases[identity].request
            if task == "planning"
            else INSTRUCTIONS[task] + "\n원래 고정 요청: " + cases[identity].request
            for identity in case_ids
        },
        case_hashes={identity: condition_hash(cases[identity]) for identity in case_ids},
        source_hashes={
            identity: {
                source.id: hashlib.sha256(source_bytes(source)).hexdigest()
                for source in cases[identity].sources
            }
            for identity in case_ids
        },
        review_outputs=reviews,
        max_generations=len(case_ids),
        max_http_requests=len(case_ids) + 1,
    )
    if len(canonical_json(manifest).encode()) > LIMIT:
        raise AppError("LIMIT_EXCEEDED")
    return manifest


def _input_hash(manifest, case_id):
    return digest(
        [
            manifest.case_hashes[case_id],
            manifest.source_hashes[case_id],
            manifest.task,
            manifest.instructions,
            manifest.output_schema,
            manifest.requests[case_id],
            manifest.review_outputs.get(case_id),
        ]
    )


def _exam_todo(manifest, case):
    # This is an explicit role exam, not a classified Office plan or a corpus TODO observation.
    return TodoV1(
        id="exam",
        title="후보 모델 역할 시험",
        requirement_ids=["exam"],
        kind={"L": "simple", "M": "standard", "H": "complex"}[manifest.role],
        reason="고정 합성 task에서 명시한 후보 역할의 출력을 평가한다",
        signals=[],
        clarity="clear",
        usefulness="required",
        executor=ModelExecutor(role=manifest.role, task=manifest.task),
        output_schema=MODEL_OUTPUT_TYPES[manifest.task].__name__,
        done_when=[manifest.requests[case.id]],
    )


def _checks(task, result, exam_context, spec, registered):
    if task != "planning":
        return verification.model_grounding(result.parsed_output, exam_context)
    from kelly.planning import _validate_requirements

    parsed = PlanningResultV1.model_validate(result.parsed_output)
    try:
        permitted = set(spec.source_ids)
        _validate_requirements(parsed, spec, permitted)
        for todo in parsed.plan.todos:
            executor = todo.executor
            if executor.type == "tool":
                tool = registered[executor.tool_name]
                tool.args_schema.model_validate(executor.args)
                if executor.args.get(
                    "artifact_id", next(iter(permitted), None)
                ) not in permitted | {None}:
                    raise AppError("POLICY_DENIED")
        return ValidationResult(
            status="pass",
            checked_layers=["schema", "plan", "source_scope", "numeric_not_applicable"],
        )
    except (AppError, ValueError, KeyError) as exc:
        return ValidationResult(
            status="fail",
            errors=[exc.code if isinstance(exc, AppError) else "PLAN_INVALID"],
            checked_layers=["schema"],
        )


def _trial(conn, home, manifest, case, binding, transport, live):
    records = []
    for source in case.sources:
        raw = source_bytes(source)
        if hashlib.sha256(raw).hexdigest() != manifest.source_hashes[case.id][source.id]:
            raise AppError("SOURCE_CHANGED")
        record = artifacts._store(
            home,
            conn,
            io.BytesIO(raw),
            {"filename": source.filename},
            ACTOR,
            "eval",
            kind="source",
            provenance={},
            artifact_id=digest([case.id, source.id, condition_hash(case)])[:32],
        )
        records.append(record)
    request = RunRequest(
        request=manifest.requests[case.id],
        project_id="eval",
        artifact_ids=[r["id"] for r in records],
        output_format=case.output_format if manifest.task == "planning" else "text",
    )
    run = db.create_run(conn, ACTOR, "ui", case.id, request)
    spec, registered = task_spec(case, request.artifact_ids), tools.registry(home)
    if manifest.task != "planning":
        spec = TaskSpec(
            intent=manifest.task,
            source_ids=request.artifact_ids,
            inputs={"original_request": case.request},
            acceptance=[request.request],
        )
    exam_context = {
        "conn": conn,
        "home": home,
        "actor": ACTOR,
        "run": run,
        "inputs": {},
        "task_spec": spec,
    }
    output, error = None, None
    checked = ValidationResult(status="fail", errors=["EXECUTION_FAILED"])
    try:
        sources = [
            registered["source.inspect"].execute({"artifact_id": r["id"]}, exam_context)["source"]
            for r in records
        ]
        if manifest.task == "planning":
            model_request = context.build_plan_request(
                request, sources, tools.catalog(registered), binding=binding, task_spec=spec
            )
            exam_context["purpose"] = "plan"
        else:
            todo = _exam_todo(manifest, case)
            plan = PlanV1(requirements=[Requirement(id="exam", text=request.request)], todos=[todo])
            run = engine.install_plan(
                conn, run["id"], plan, ACTOR, tools=registered, task_spec=spec
            )
            inputs = {
                r["id"]: registered["source.extract"].execute(
                    {"artifact_id": r["id"]}, {**exam_context, "run": run}
                )["evidence"]
                for r in records
            }
            if manifest.task == "semantic_review":
                inputs.update(
                    candidate_output=manifest.review_outputs[case.id],
                    review_task={"task": TASKS[case.category], "done_when": [case.request]},
                )
            exam_context.update(
                run=run,
                todo=todo,
                inputs=inputs,
                purpose="review" if manifest.task == "semantic_review" else "generate",
            )
            model_request = context.build_model_request(todo, inputs, exam_context, binding=binding)
        result = metered_complete(model_request, binding, exam_context, transport=transport)
        output = result.parsed_output
        checked = _checks(manifest.task, result, exam_context, spec, registered)
    except AppError as exc:
        error = exc.code
    sources_unchanged = True
    for record in records:
        with artifacts.open_artifact(home, conn, record["id"], ACTOR, "eval") as stream:
            sources_unchanged &= (
                hashlib.file_digest(stream, "sha256").hexdigest() == record["sha256"]
            )
    calls = [
        db.row_dict(row) for row in conn.execute("SELECT * FROM calls WHERE run_id=?", (run["id"],))
    ]
    identity = run["id"]
    output_sha = _write(home, identity + ".output.json", output)
    layers = set(checked.checked_layers)
    receipt = {
        "version": 1,
        "kind": "candidate_exam",
        "live": live,
        "manifest_sha256": digest(manifest),
        "trial_id": identity,
        "case_id": case.id,
        "fixture_sha256": condition_hash(case),
        "trial_input_sha256": _input_hash(manifest, case.id),
        "provider": "xai",
        "model_id": binding.model_id,
        "role": manifest.role,
        "task": manifest.task,
        "reasoning_effort": binding.reasoning_effort,
        "output_sha256": output_sha,
        "source_artifacts": [{"id": r["id"], "sha256": r["sha256"]} for r in records],
        "schema_pass": output is not None,
        "numeric_pass": bool(layers & {"numeric_references", "numeric_not_applicable"}),
        "source_pass": sources_unchanged and bool(layers & {"evidence_references", "source_scope"}),
        "policy_pass": error is None and sources_unchanged and len(calls) == 1,
        "validation": checked.model_dump(),
        "error_code": error,
        "call_ids": [call["id"] for call in calls],
    }
    _write(home, identity + ".trial.json", receipt)
    db.set_value(conn, "qualification_result:" + identity, receipt)
    return receipt


def execute_trials(manifest, *, manifest_sha256, allow_live=False, http_transport=None):
    if allow_live is not True:
        raise AppError("POLICY_DENIED")
    manifest = ExamManifest.model_validate(manifest.model_dump())
    if digest(manifest) != manifest_sha256 or manifest.max_generations != len(manifest.case_hashes):
        raise AppError("INVALID_INPUT")
    current = prepare_trials(
        manifest.config_path,
        model_id=manifest.profile.binding.model_id,
        role=manifest.role,
        task=manifest.task,
        case_ids=list(manifest.case_hashes),
        workspace=manifest.workspace,
        review_outputs=manifest.review_outputs,
        reasoning_effort=manifest.reasoning_effort,
    )
    if current != manifest:
        raise AppError("CONFLICT", status=409)
    configured = config.load_config(manifest.config_path)
    key = config.read_xai_key(config.home_path(configured, Path(manifest.config_path)))
    if key is None:
        raise AppError("NO_CAPABLE_MODEL")
    home = ensure_directory(Path(manifest.workspace))
    live = http_transport is None
    _write(home, "manifest.json", manifest)
    _write(
        home,
        "execution.json",
        {"manifest_sha256": digest(manifest), "live": live, "state": "claimed"},
    )
    conn = db.init_db(home / "state.db")
    db.set_value(
        conn, "qualification_execution", {"manifest_sha256": digest(manifest), "live": live}
    )
    try:
        with XAIClient(key.get_secret_value(), http_transport=http_transport) as client:
            candidate = manifest.profile.binding
            if candidate.model_id not in client.list_models():
                raise AppError("NO_CAPABLE_MODEL")
            binding = candidate.model_copy(
                update={
                    "available": True,
                    "role": manifest.role,
                    "quality_evidence_id": None,
                    "reasoning_effort": manifest.reasoning_effort,
                }
            )

            def once(request, selected):
                with db.transaction(conn):
                    count = db.get_value(conn, "generation_attempts", 0)
                    if count >= manifest.max_generations:
                        raise AppError("CALL_LIMIT")
                    db.set_value(conn, "generation_attempts", count + 1)
                try:
                    return client.transport(request, selected)
                except TransportError as exc:
                    exc.retryable = False
                    raise

            cases = {case.id: case for case in load_cases()}
            receipts = [
                _trial(conn, home, manifest, cases[identity], binding, once, live)
                for identity in manifest.case_hashes
            ]
        packet = []
        for receipt in receipts:
            record = SimpleNamespace(id=receipt["trial_id"], output_sha256=receipt["output_sha256"])
            output, _ = _read(home, receipt["trial_id"] + ".output.json")
            case = cases[receipt["case_id"]]
            packet.append(
                {
                    "blind_id": blind_id(record),
                    "output_sha256": record.output_sha256,
                    "kind": "candidate_role_exam",
                    "task": manifest.task,
                    "instructions": manifest.instructions,
                    "request": manifest.requests[case.id],
                    "original_request": case.request,
                    "output_schema": manifest.output_schema,
                    "review_output": manifest.review_outputs.get(case.id),
                    "review_task": TASKS.get(case.category)
                    if manifest.task == "semantic_review"
                    else None,
                    "sources": [s.model_dump() for s in case.sources],
                    "output": output,
                }
            )
        _write(home, "blind.json", sorted(packet, key=lambda value: value["blind_id"]))
        _write(
            home,
            "complete.json",
            {
                "manifest_sha256": digest(manifest),
                "live": live,
                "trial_ids": [r["trial_id"] for r in receipts],
                "generation_attempts": db.get_value(conn, "generation_attempts", 0),
            },
        )
        return {"workspace": str(home), "live": live, "qualified": False, "trials": len(receipts)}
    finally:
        conn.close()


def _bundle(workspace):
    root = validate_root(Path(workspace).absolute())
    manifest_raw, manifest_sha = _read(root, "manifest.json")
    manifest = ExamManifest.model_validate(manifest_raw)
    complete, _ = _read(root, "complete.json")
    execution, _ = _read(root, "execution.json")
    if (
        str(root) != manifest.workspace
        or digest(manifest) != manifest_sha
        or complete.get("manifest_sha256") != manifest_sha
        or execution.get("manifest_sha256") != manifest_sha
        or complete.get("live") is not True
        or execution.get("live") is not True
    ):
        raise AppError("POLICY_DENIED")
    records = []
    from kelly.operations import _database

    with _database(root) as conn:
        if db.get_value(conn, "qualification_execution") != {
            "manifest_sha256": manifest_sha,
            "live": True,
        }:
            raise AppError("POLICY_DENIED")
        case_ids = set()
        for identity in complete.get("trial_ids", []):
            if not isinstance(identity, str) or not identity.isalnum() or len(identity) > 96:
                raise AppError("INVALID_INPUT")
            receipt, receipt_sha = _read(root, identity + ".trial.json")
            _output, output_sha = _read(root, identity + ".output.json")
            if (
                receipt.get("live") is not True
                or receipt.get("manifest_sha256") != manifest_sha
                or receipt.get("trial_id") != identity
                or receipt.get("output_sha256") != output_sha
                or receipt.get("fixture_sha256") != manifest.case_hashes.get(receipt.get("case_id"))
                or receipt.get("model_id") != manifest.profile.binding.model_id
                or receipt.get("role") != manifest.role
                or receipt.get("task") != manifest.task
                or receipt.get("reasoning_effort") != manifest.reasoning_effort
                or receipt.get("trial_input_sha256")
                != _input_hash(manifest, receipt.get("case_id"))
                or db.get_value(conn, "qualification_result:" + identity) != receipt
            ):
                raise AppError("SOURCE_CHANGED")
            case_ids.add(receipt["case_id"])
            calls = [
                db.row_dict(row)
                for row in conn.execute("SELECT * FROM calls WHERE run_id=?", (identity,))
            ]
            if (
                len(calls) != 1
                or receipt.get("call_ids") != [calls[0]["id"]]
                or calls[0]["state"] != "settled"
                or calls[0]["provider"] != "xai"
                or calls[0]["model_id"] != receipt["model_id"]
                or calls[0]["route_json"]["role"] != receipt["role"]
                or calls[0]["route_json"].get("reasoning_effort") != receipt["reasoning_effort"]
                or calls[0]["route_json"].get("quality_evidence_id") is not None
                or not calls[0]["provider_request_id"]
            ):
                raise AppError("INVALID_OUTPUT")
            observed = Usage(
                **{field: calls[0][field] for field in (*usage.TOKEN_FIELDS, "usage_basis")}
            )
            if (
                observed.usage_basis != "provider"
                or observed.input_tokens_total is None
                or observed.output_tokens_total is None
            ):
                raise AppError("INVALID_OUTPUT")
            for source in receipt["source_artifacts"]:
                with artifacts.open_artifact(root, conn, source["id"], ACTOR, "eval") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != source["sha256"]:
                        raise AppError("SOURCE_CHANGED")
            expected_sources = {
                digest([receipt["case_id"], source_id, receipt["fixture_sha256"]])[:32]: sha
                for source_id, sha in manifest.source_hashes[receipt["case_id"]].items()
            }
            if {s["id"]: s["sha256"] for s in receipt["source_artifacts"]} != expected_sources:
                raise AppError("SOURCE_CHANGED")
            run = db.get_run(conn, identity, ACTOR)
            if (
                run["project_id"] != "eval"
                or run["request_json"]["request"] != manifest.requests[receipt["case_id"]]
                or set(run["request_json"]["artifact_ids"]) != expected_sources.keys()
            ):
                raise AppError("SOURCE_CHANGED")
            record = {
                **receipt,
                "usage": observed.model_dump(),
                "request_id": calls[0]["provider_request_id"],
                "proof": {
                    "workspace": str(root),
                    "manifest_sha256": manifest_sha,
                    "environment_sha256": manifest.environment_sha256,
                    "receipt_sha256": receipt_sha,
                    "output_sha256": output_sha,
                    "call": calls[0],
                },
            }
            records.append(record)
        if (
            case_ids != set(manifest.case_hashes)
            or len(records) != len(case_ids)
            or complete.get("generation_attempts") != len(records)
            or db.get_value(conn, "generation_attempts", 0) != len(records)
        ):
            raise AppError("INVALID_OUTPUT")
    return manifest, records


def write_qualification(
    candidate_workspaces, reference_workspaces, labels, output_dir, *, evidence_id
):
    """Require real ledgers and independently labelled H results; never accept a claimed report."""
    candidates, references, profiles = [], [], []
    for workspace in candidate_workspaces:
        manifest, records = _bundle(workspace)
        candidates.extend(records)
        profiles.append(manifest.profile)
    for workspace in reference_workspaces:
        manifest, records = _bundle(workspace)
        if manifest.role != "H":
            raise AppError("INVALID_INPUT")
        references.extend(records)
    if not profiles or any(profile != profiles[0] for profile in profiles):
        raise AppError("INVALID_INPUT")
    all_records = candidates + references
    if len({r["trial_id"] for r in all_records}) != len(all_records) or len(
        {r["request_id"] for r in all_records}
    ) != len(all_records):
        raise AppError("INVALID_INPUT")
    selected_labels = import_labels(
        labels,
        [SimpleNamespace(id=r["trial_id"], output_sha256=r["output_sha256"]) for r in all_records],
    )
    grouped = {}
    for label in selected_labels:
        grouped.setdefault(label.blind_id, []).append(label)

    def adopted(record):
        identity = blind_id(
            SimpleNamespace(id=record["trial_id"], output_sha256=record["output_sha256"])
        )
        judgments = grouped.get(identity)
        if not judgments:
            raise AppError("MISSING_INPUT")
        return all(label.adopted for label in judgments)

    matched, cases = [], []
    for candidate in candidates:
        reference = [
            r
            for r in references
            if (r["case_id"], r["task"], r["fixture_sha256"], r["trial_input_sha256"])
            == (
                candidate["case_id"],
                candidate["task"],
                candidate["fixture_sha256"],
                candidate["trial_input_sha256"],
            )
        ]
        if len(reference) != 1:
            raise AppError("MISSING_INPUT")
        reference = reference[0]
        if candidate["proof"].get("environment_sha256") != reference["proof"].get(
            "environment_sha256"
        ):
            raise AppError("SOURCE_CHANGED")
        for record in (candidate, reference):
            if not all(
                record.get(key) is True
                for key in ("schema_pass", "numeric_pass", "source_pass", "policy_pass")
            ):
                raise AppError("INVALID_OUTPUT")
            checked = ValidationResult.model_validate(record["validation"])
            if checked.status != "pass" and checked.errors != ["SEMANTIC_REVIEW_REQUIRED"]:
                raise AppError("INVALID_OUTPUT")
        cases.append(
            config.QualificationCase(
                case_id="q-"
                + digest(
                    [
                        candidate["case_id"],
                        candidate["role"],
                        candidate["task"],
                        candidate["reasoning_effort"],
                    ]
                )[:40],
                role=candidate["role"],
                task=candidate["task"],
                request_id=candidate["request_id"],
                usage=Usage.model_validate(candidate["usage"]),
                fixture_sha256=candidate["fixture_sha256"],
                output_sha256=candidate["output_sha256"],
                schema_pass=True,
                numeric_pass=True,
                source_pass=True,
                policy_pass=True,
                accepted=adopted(candidate),
                reference_accepted=adopted(reference),
                reasoning_effort=candidate["reasoning_effort"],
            )
        )
        matched.append(
            {
                "candidate": candidate["proof"],
                "reference": reference["proof"],
                "candidate_trial_id": candidate["trial_id"],
                "reference_trial_id": reference["trial_id"],
            }
        )
    report = config.QualificationReport(
        evidence_id=evidence_id,
        provider="xai",
        model_id=profiles[0].binding.model_id,
        mode="live",
        completed_at=datetime.now(UTC).isoformat(),
        cases=cases,
    )
    # Use the unchanged production gate on private staging bytes before publishing anything reusable.
    root = ensure_directory(validate_root(Path(output_dir).absolute()))
    stage = ensure_directory(root / (".check-" + uuid.uuid4().hex))
    stage_reports = ensure_directory(stage / "evaluations")
    name = report.evidence_id + ".json"
    try:
        report_sha = _write(stage_reports, name, report)
        profile = profiles[0].model_copy(deep=True)
        profile.binding.quality_evidence_id = evidence_id
        profile.quality_evidence_sha256 = report_sha
        if config._qualification(profile, stage) != "verified":
            raise AppError("NO_CAPABLE_MODEL")
        provenance = {
            "schema_version": 1,
            "report_sha256": report_sha,
            "matches": matched,
            "labels": [label.model_dump() for label in selected_labels],
        }
        provenance_sha = _write(root, evidence_id + ".provenance.json", provenance)
        destination = ensure_directory(root / "evaluations") / name
        if _write(destination.parent, name, report) != report_sha:
            raise AppError("SOURCE_CHANGED")
        _write(
            root,
            evidence_id + ".profile.json",
            {"profile": profile.model_dump(), "provenance_sha256": provenance_sha},
        )
        return {
            "report": str(destination),
            "report_sha256": report_sha,
            "provenance_sha256": provenance_sha,
            "qualified": True,
            "installed": False,
        }
    finally:
        (stage_reports / name).unlink(missing_ok=True)
        stage_reports.rmdir()
        stage.rmdir()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--model", required=True)
    prepare.add_argument("--role", choices=["L", "M", "H"], required=True)
    prepare.add_argument("--task", required=True)
    prepare.add_argument("--case", action="append", required=True)
    prepare.add_argument("--workspace", type=Path, required=True)
    prepare.add_argument("--review-inputs", type=Path)
    prepare.add_argument("--reasoning-effort", choices=["none", "low", "medium", "high", "xhigh"])
    execute = commands.add_parser("execute")
    execute.add_argument("--manifest", type=Path, required=True)
    execute.add_argument("--manifest-sha256", required=True)
    execute.add_argument("--allow-live", action="store_true")
    publish = commands.add_parser("write")
    publish.add_argument("--candidate", type=Path, action="append", required=True)
    publish.add_argument("--reference", type=Path, action="append", required=True)
    publish.add_argument("--labels", type=Path, required=True)
    publish.add_argument("--output", type=Path, required=True)
    publish.add_argument("--evidence-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        reviews = None
        if args.review_inputs:
            reviews, _ = _read(args.review_inputs.absolute().parent, args.review_inputs.name)
        result = prepare_trials(
            args.config,
            model_id=args.model,
            role=args.role,
            task=args.task,
            case_ids=args.case,
            workspace=args.workspace,
            review_outputs=reviews,
            reasoning_effort=args.reasoning_effort,
        )
        print(canonical_json({"manifest": result.model_dump(), "manifest_sha256": digest(result)}))
    elif args.command == "execute":
        if not args.allow_live:
            raise AppError("POLICY_DENIED")
        raw, _ = _read(args.manifest.absolute().parent, args.manifest.name)
        manifest = ExamManifest.model_validate(raw.get("manifest", raw))
        print(
            canonical_json(
                execute_trials(manifest, manifest_sha256=args.manifest_sha256, allow_live=True)
            )
        )
    else:
        labels, _ = _read(args.labels.absolute().parent, args.labels.name)
        print(
            canonical_json(
                write_qualification(
                    args.candidate,
                    args.reference,
                    labels,
                    args.output,
                    evidence_id=args.evidence_id,
                )
            )
        )


if __name__ == "__main__":
    main()
