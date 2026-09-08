"""Prepare one durable plan: known recipes use code; new work uses one M response."""

from pydantic import ValidationError

from .context import build_plan_request
from .contracts import (
    MODEL_OUTPUT_TYPES,
    AppError,
    ArtifactInput,
    ModelResult,
    PlanningResultV1,
    PlanV1,
    PrivacyPolicy,
    RunRequest,
    StepInput,
    TaskFacts,
    ToolExecutor,
    digest,
)
from .db import (
    allowed_sources,
    get_automation,
    get_run,
    get_task_spec,
    get_value,
    row_dict,
    set_value,
)
from .engine import install_plan
from .routing import choose_model, choose_planner, expand_recipe, normalize_todo
from .tools import catalog

TOOL_OUTPUTS = {
    "source.inspect": "SourceInfoV1",
    "source.extract": "EvidenceListV1",
    "source.search": "EvidenceListV1",
    "table.aggregate": "AggregateV1",
    "document.create": "ArtifactV1",
    "artifact.validate": "ValidationResultV1",
    "artifact.publish": "ReceiptV1",
    "memory.search": "MemoryListV1",
    "memory.save": "MemoryListV1",
    "automation.propose": "AutomationProposalV1",
}


def normalize_plan(
    plan, *, tools, capability_catalog, privacy_policy, permitted_sources, completed_ids=()
):
    """Validate executable boundaries before accepting a model's classification labels."""
    from .verification import required_verifiers

    plan = PlanV1.model_validate(plan.model_dump())
    needed = {t.id for t in plan.todos if t.usefulness == "required"}
    for todo in reversed(plan.todos):
        if todo.id in needed:
            needed.update(todo.depends_on)
    normalized, bindings = [], {}
    for todo in plan.todos:
        if todo.id in completed_ids:
            # install_plan compares these exact specs/ordinals against durable completed rows.
            normalized.append(todo)
            continue
        deterministic = isinstance(todo.executor, ToolExecutor)
        if any(
            isinstance(ref, ArtifactInput) and ref.artifact_id not in permitted_sources
            for ref in todo.inputs
        ):
            raise AppError("POLICY_DENIED")
        if deterministic:
            tool = tools.get(todo.executor.tool_name)
            if tool is None or tool.effect == "destructive" or tool.args_schema is None:
                raise AppError("UNSUPPORTED_CAPABILITY")
            try:
                tool.args_schema.model_validate(todo.executor.args)
            except ValidationError:
                raise AppError("INVALID_INPUT") from None
            args = todo.executor.args
            if args.get("artifact_id") is not None and args["artifact_id"] not in permitted_sources:
                raise AppError("POLICY_DENIED")
            if args.get("input_ref") is not None and args["input_ref"] not in {
                ref.step_id for ref in todo.inputs if isinstance(ref, StepInput)
            }:
                raise AppError("PLAN_INVALID")
            expected = TOOL_OUTPUTS[todo.executor.tool_name]
        else:
            if any(isinstance(ref, ArtifactInput) for ref in todo.inputs):
                # Artifact IDs carry no source text; use a bounded extract/aggregate predecessor.
                raise AppError("PLAN_INVALID")
            if any(
                isinstance(ref, StepInput) and ref.output_key == "source" for ref in todo.inputs
            ):
                raise AppError("PLAN_INVALID")
            if permitted_sources and not todo.inputs and todo.clarity != "missing_input":
                raise AppError("PLAN_INVALID")
            expected = MODEL_OUTPUT_TYPES[todo.executor.task].__name__
        if todo.output_schema != expected:
            raise AppError("PLAN_INVALID")
        value = normalize_todo(
            todo,
            TaskFacts(
                certified_deterministic=deterministic,
                requested_or_supporting=todo.id in needed,
                required_inputs_present=todo.clarity != "missing_input",
            ),
        )
        value.verify_with = required_verifiers(value)
        binding = choose_model(value, capability_catalog, privacy_policy)
        if binding is not None:
            bindings[value.id] = binding.model_dump()
        normalized.append(value)
    return PlanV1(requirements=plan.requirements, todos=normalized), bindings


def _validate_requirements(result, confirmed_spec, permitted, completed_ids=()):
    producers = {
        todo.id
        for todo in result.plan.todos
        if todo.output_schema
        in {"AggregateV1", "ReportSectionsV1", "MeetingActionsV1", "DraftReplyV1", "SearchAnswerV1"}
    }
    for todo in result.plan.todos:
        if (
            todo.id not in completed_ids
            and isinstance(todo.executor, ToolExecutor)
            and todo.executor.tool_name == "document.create"
            and todo.executor.args.get("input_ref") not in producers
        ):
            # A planner cannot smuggle authored prose into a certified mechanical file writer.
            # Trusted direct tool callers can still supply literal DocumentContent.
            raise AppError("PLAN_INVALID")
    if not set(result.task_spec.source_ids) <= set(permitted):
        raise AppError("POLICY_DENIED")
    if result.task_spec.ambiguity == "missing_input" and not any(
        todo.clarity == "missing_input" and todo.usefulness == "required"
        for todo in result.plan.todos
    ):
        raise AppError("PLAN_INVALID")
    conditions = list(result.task_spec.acceptance)
    if confirmed_spec is not None:
        if any(
            result.task_spec.inputs.get(key) != value
            for key, value in confirmed_spec.inputs.items()
        ):
            raise AppError("PLAN_INVALID")
        conditions.extend(confirmed_spec.acceptance)
    # Structural coverage is enforceable here. Semantic request coverage is evaluated
    # against independent gold cases; a planner's self-report cannot prove it.
    for condition in conditions:
        required = {r.id for r in result.plan.requirements if r.required and r.text == condition}
        if not required or not any(
            todo.usefulness == "required"
            and required.intersection(todo.requirement_ids)
            and condition in todo.done_when
            for todo in result.plan.todos
        ):
            raise AppError("PLAN_INVALID")


def _automation_scope(conn, run, actor, request, confirmed_spec, privacy_policy):
    if run["automation_id"] is None:
        return None
    bot = get_automation(conn, run["automation_id"], actor)
    if (
        not bot["enabled"]
        or bot["revision"] != run["automation_revision"]
        or digest(bot["config_json"]) != run["policy_json"].get("automation_config_hash")
    ):
        raise AppError("POLICY_DENIED")
    return digest(
        [
            actor.id,
            bot["id"],
            bot["revision"],
            run["project_id"],
            run["policy_json"],
            request.model_dump(),
            confirmed_spec.model_dump() if confirmed_spec is not None else None,
            privacy_policy.model_dump(),
        ]
    )


def _previous_automation_plan(conn, run, actor, scope):
    snapshot = get_value(conn, "automation_plan:" + run["automation_id"], {})
    if snapshot.get("scope") != scope or snapshot.get("run_id") == run["id"]:
        return None, None
    # A retained snapshot may outlive its run. Never search other actors or bots.
    previous = row_dict(
        conn.execute("SELECT * FROM runs WHERE id=?", (snapshot["run_id"],)).fetchone()
    )
    if (
        previous is None
        or any(
            previous[key] != run[key]
            for key in ("actor_id", "project_id", "automation_id", "automation_revision")
        )
        or previous["state"] != "succeeded"
        or not previous["plan_json"]
    ):
        return None, None
    result = PlanningResultV1(
        task_spec=get_task_spec(conn, previous["id"], actor),
        plan=previous["plan_json"]["current"],
    )
    # Only structure crosses runs: execution state, outputs and approvals stay behind.
    for todo in result.plan.todos:
        todo.status = "pending"
    return result, snapshot["sources"]


def prepare_run(
    conn,
    home,
    run_id,
    actor,
    *,
    tools,
    capability_catalog,
    privacy_policy: PrivacyPolicy,
    complete=None,
    errors=None,
):
    """complete(request, binding, context) is the metered external model boundary.

    It returns ModelResult. No classifier call, executor call or raw conversation
    replay happens here. Failed schema repair is a separately metered replan.
    """
    run = get_run(conn, run_id, actor)
    if run["state"] not in {"queued", "awaiting_input"}:
        return run
    if privacy_policy.project_id != run["project_id"]:
        raise AppError("POLICY_DENIED")
    permitted = allowed_sources(conn, run, actor)
    request = RunRequest.model_validate({**run["request_json"], "artifact_ids": permitted})
    confirmed_spec = get_task_spec(conn, run_id, actor)
    automation_scope = _automation_scope(conn, run, actor, request, confirmed_spec, privacy_policy)
    completed_ids = [
        row_dict(row)["spec_json"]["todo"]["id"]
        for row in conn.execute(
            "SELECT spec_json FROM steps WHERE run_id=? AND state IN ('succeeded','skipped')",
            (run_id,),
        )
    ]
    context = {"conn": conn, "home": home, "run": run, "actor": actor, "inputs": {}}
    supported = {name.replace(".", "_") for name in tools} | set(MODEL_OUTPUT_TYPES)
    if confirmed_spec is not None and not set(confirmed_spec.required_capabilities) <= supported:
        raise AppError("UNSUPPORTED_CAPABILITY")
    inspected = []
    for source_id in permitted:
        args = {"artifact_id": source_id}
        if confirmed_spec is not None and confirmed_spec.inputs.get("sheet") is not None:
            args["sheet"] = confirmed_spec.inputs["sheet"]
        inspected.append(tools["source.inspect"].execute(args, context)["source"])
    proposed = expand_recipe(request, inspected, confirmed_spec)
    task_spec = confirmed_spec
    plan, bindings = None, {}
    normalization = {
        "tools": tools,
        "capability_catalog": capability_catalog,
        "privacy_policy": privacy_policy,
        "permitted_sources": permitted,
        "completed_ids": completed_ids,
    }
    previous_plan = run["plan_json"]["current"] if run["plan_json"] else None
    if proposed is None and previous_plan is None and automation_scope is not None:
        prior, source_digest = _previous_automation_plan(conn, run, actor, automation_scope)
        if prior is not None:
            previous_plan = prior.plan
            # Unversioned memory/search and other ambient state require planner re-evaluation.
            versioned = bool(inspected) and all(source.get("sha256") for source in inspected)
            attached_only = all(
                not isinstance(todo.executor, ToolExecutor)
                or todo.executor.tool_name
                in {
                    "source.inspect",
                    "source.extract",
                    "table.aggregate",
                    "document.create",
                    "artifact.validate",
                    "artifact.publish",
                }
                for todo in prior.plan.todos
            )
            if versioned and attached_only and source_digest == digest(inspected):
                _validate_requirements(prior, confirmed_spec, permitted)
                if not set(prior.task_spec.required_capabilities) <= supported:
                    raise AppError("UNSUPPORTED_CAPABILITY")
                plan, bindings = normalize_plan(prior.plan, **normalization)
                proposed, task_spec = prior.plan, prior.task_spec
            elif source_digest != digest(inspected):
                errors = list(dict.fromkeys([*(errors or []), "SOURCE_CHANGED"]))
    if proposed is None:
        binding = choose_planner(
            capability_catalog,
            privacy_policy,
            role="H"
            if run["plan_json"]
            and set(errors or []) & {"COUPLED_CONSTRAINTS", "CONFLICTING_SOURCES"}
            else "M",
        )
        if complete is None:
            raise AppError("NO_CAPABLE_MODEL")
        previous_calls = conn.execute(
            "SELECT count(DISTINCT logical_call_no) FROM calls WHERE run_id=? AND purpose IN ('plan','replan')",
            (run_id,),
        ).fetchone()[0]
        if previous_calls >= 2:
            raise AppError("CALL_LIMIT")
        for attempt in range(2 - previous_calls):
            model_request = build_plan_request(
                request,
                inspected,
                catalog(tools),
                binding=binding,
                task_spec=confirmed_spec,
                errors=errors,
                previous_plan=previous_plan,
                completed_ids=completed_ids,
            )
            try:
                result = ModelResult.model_validate(
                    complete(
                        model_request,
                        binding,
                        {
                            **context,
                            "purpose": "replan" if run["plan_json"] else "plan",
                            "repair_errors": errors or [],
                        },
                    )
                )
                if result.finish_reason not in {"stop", "completed"}:
                    raise AppError("OUTPUT_TRUNCATED")
                generated = PlanningResultV1.model_validate(result.parsed_output)
                _validate_requirements(generated, confirmed_spec, permitted, completed_ids)
                if not set(generated.task_spec.required_capabilities) <= supported:
                    raise AppError("UNSUPPORTED_CAPABILITY")
                plan, bindings = normalize_plan(generated.plan, **normalization)
                proposed, task_spec = generated.plan, generated.task_spec
                break
            except (AppError, ValidationError) as exc:
                code = exc.code if isinstance(exc, AppError) else "PLAN_INVALID"
                if (
                    code
                    not in {"PLAN_INVALID", "INVALID_INPUT", "INVALID_OUTPUT", "OUTPUT_TRUNCATED"}
                    or attempt + previous_calls >= 1
                ):
                    raise AppError(code) from None
                errors = [code]
    if plan is None:
        plan, bindings = normalize_plan(proposed, **normalization)
    bindings = {
        **{
            key: value
            for key, value in get_value(conn, "route_bindings:" + run_id, {}).items()
            if key in completed_ids
        },
        **bindings,
    }
    # A planner may have been in flight while the bot was edited or disabled.
    _automation_scope(conn, run, actor, request, confirmed_spec, privacy_policy)
    installed = install_plan(
        conn,
        run_id,
        plan,
        actor,
        tools=tools,
        requirements=PlanV1.model_validate(previous_plan).requirements if previous_plan else None,
        proposed_plan=proposed,
        expected_revision=run["revision"],
        task_spec=task_spec,
        route_bindings=bindings,
    )
    if automation_scope is not None:
        set_value(
            conn,
            "automation_plan:" + run["automation_id"],
            {"scope": automation_scope, "sources": digest(inspected), "run_id": run_id},
        )
    return installed
