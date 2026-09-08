"""One durable, bounded TODO executor. Models propose; this module owns state."""

import os
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .contracts import (
    OUTPUT_SCHEMA_KEYS,
    ActionProposal,
    Actor,
    AppError,
    ArtifactInput,
    ModelBinding,
    ModelExecutor,
    PlanV1,
    Requirement,
    StepInput,
    TaskSpec,
    TodoV1,
    ToolExecutor,
    ValidationResult,
    canonical_json,
    digest,
)
from .db import allowed_sources, get_run, get_value, row_dict, set_value, transaction, utc_now

TERMINAL = {"succeeded", "failed", "cancelled", "rejected", "expired", "outcome_unknown"}
TRANSITIONS = {
    "queued": {"preparing", "cancel_requested", "failed"},
    "preparing": {"running", "awaiting_input", "cancel_requested", "failed"},
    "running": {
        "checking",
        "retry_wait",
        "awaiting_input",
        "awaiting_budget",
        "cancel_requested",
        "failed",
        "outcome_unknown",
    },
    "checking": {
        "ready",
        "running",
        "awaiting_approval",
        "awaiting_input",
        "awaiting_budget",
        "retry_wait",
        "cancel_requested",
        "failed",
        "outcome_unknown",
    },
    "ready": {"running", "succeeded", "cancel_requested", "failed", "outcome_unknown"},
    "awaiting_input": {"preparing", "cancel_requested", "failed"},
    "retry_wait": {"running", "cancel_requested", "failed"},
    "awaiting_budget": {"running", "cancel_requested", "failed"},
    "awaiting_approval": {
        "ready",
        "rejected",
        "expired",
        "cancel_requested",
        "failed",
        "outcome_unknown",
    },
    "cancel_requested": {"cancelled", "outcome_unknown"},
}


@dataclass(frozen=True)
class Tool:
    effect: str
    execute: Callable
    args_schema: type[BaseModel] | None = None

    def __post_init__(self):
        if self.effect not in {"read", "draft", "external_write", "destructive"}:
            raise ValueError("Unknown registered tool effect")


class DaemonLock:
    """OS lock lifetime is process lifetime; no stale PID/lease guessing."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if os.fstat(self.handle.fileno()).st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise AppError("ALREADY_RUNNING", status=409) from exc
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def transition_run(conn, run_id: str, expected_state: str, next_state: str) -> dict:
    if next_state not in TRANSITIONS.get(expected_state, set()):
        raise AppError("INVALID_TRANSITION", status=409)
    cursor = conn.execute(
        "UPDATE runs SET state=?,revision=revision+1,updated_at=? WHERE id=? AND state=?",
        (next_state, utc_now(), run_id, expected_state),
    )
    if cursor.rowcount != 1:
        raise AppError("CONFLICT", status=409)
    return row_dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())


def validate_plan(plan: PlanV1, requirements: list[Requirement]) -> ValidationResult:
    try:
        PlanV1.model_validate(plan.model_dump())
    except ValidationError:
        return ValidationResult(status="fail", errors=["PLAN_INVALID"], checked_layers=["plan"])
    declared = {r.id: r for r in plan.requirements}
    covered = {rid for t in plan.todos if t.usefulness == "required" for rid in t.requirement_ids}
    for required in requirements:
        if required.required and (
            declared.get(required.id) != required or required.id not in covered
        ):
            return ValidationResult(
                status="fail", errors=["REQUIREMENT_MISSING"], checked_layers=["plan"]
            )
    return ValidationResult(status="pass", checked_layers=["plan"])


def step_key(run_id: str, todo_id: str) -> str:
    return "s_" + digest([run_id, todo_id])


def install_plan(
    conn,
    run_id: str,
    plan: PlanV1,
    actor: Actor,
    *,
    tools: dict[str, Tool],
    requirements: list[Requirement] | None = None,
    proposed_plan: PlanV1 | None = None,
    expected_revision: int | None = None,
    task_spec: TaskSpec | None = None,
    route_bindings: dict | None = None,
):
    # A model can be in flight while another request cancels or supplies new inputs.
    expected = (
        get_run(conn, run_id, actor)["revision"] if expected_revision is None else expected_revision
    )
    if type(expected) is not int:
        raise AppError("INVALID_INPUT")
    plan = PlanV1.model_validate(plan.model_dump())
    proposed = {t.id: t for t in (proposed_plan or plan).todos}
    if set(proposed) != {t.id for t in plan.todos}:
        raise AppError("PLAN_INVALID")
    with transaction(conn):
        run = get_run(conn, run_id, actor)
        if run["revision"] != expected or run["state"] in TERMINAL | {"cancel_requested"}:
            raise AppError("CONFLICT", status=409)
        previous = run["plan_json"]
        original = (
            [Requirement.model_validate(r) for r in previous["current"]["requirements"]]
            if previous
            else requirements or plan.requirements
        )
        if validate_plan(plan, original).status != "pass":
            raise AppError("PLAN_INVALID")
        permitted = set(allowed_sources(conn, run, actor))
        old_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM steps WHERE run_id=? ORDER BY ordinal", (run_id,)
            )
        ]
        old = {row_dict(row)["spec_json"]["todo"]["id"]: row for row in old_rows}
        calls = {}
        approvals = {}
        for row in conn.execute("SELECT * FROM calls WHERE run_id=?", (run_id,)):
            calls.setdefault(row["step_id"], []).append(dict(row))
        for row in conn.execute("SELECT * FROM approvals WHERE run_id=?", (run_id,)):
            approvals.setdefault(row["step_id"], []).append(dict(row))
        frozen = {
            key
            for key, row in old.items()
            if row["state"] != "pending"
            or (row["effect"] in {"external_write", "destructive"} and row["attempt_count"])
            or any(a["state"] == "consumed" for a in approvals.get(row["id"], []))
            or any(
                c["state"] in {"reserved", "in_flight"}
                or (c["state"] == "unknown" and row["effect"] in {"external_write", "destructive"})
                for c in calls.get(row["id"], [])
            )
        }
        revision = (previous or {}).get("revision", 0) + 1
        replacements = []
        for ordinal, todo in enumerate(plan.todos):
            if any(
                isinstance(ref, ArtifactInput) and ref.artifact_id not in permitted
                for ref in todo.inputs
            ):
                raise AppError("POLICY_DENIED")
            tool = (
                tools.get(todo.executor.tool_name)
                if isinstance(todo.executor, ToolExecutor)
                else None
            )
            if isinstance(todo.executor, ToolExecutor):
                if tool is None or tool.effect == "destructive":
                    raise AppError("UNSUPPORTED_CAPABILITY")
                if tool.args_schema is not None:
                    try:
                        tool.args_schema.model_validate(todo.executor.args)
                    except ValidationError:
                        raise AppError("INVALID_INPUT") from None
            prior = old.get(todo.id)
            old_spec = row_dict(prior)["spec_json"] if prior else {}
            kind, effect = ("tool", tool.effect) if tool else ("model", "draft")
            if todo.id in frozen:
                if (
                    prior["ordinal"] != ordinal
                    or old_spec["todo"] != todo.model_dump()
                    or prior["kind"] != kind
                    or prior["effect"] != effect
                ):
                    raise AppError("CONFLICT", status=409)
                continue
            if todo.status != "pending" and not (
                todo.status == "skipped" and todo.usefulness == "optional"
            ):
                raise AppError("PLAN_INVALID")
            if (
                prior
                and prior["attempt_count"]
                and (prior["kind"] != kind or prior["effect"] != effect)
            ):
                raise AppError("CONFLICT", status=409)
            fields = {"kind", "signals", "clarity", "usefulness", "executor", "reason"}
            final = todo.model_dump(include=fields)
            initial = old_spec.get(
                "original_classification", proposed[todo.id].model_dump(include=fields)
            )
            spec = {
                **old_spec,
                "todo": todo.model_dump(),
                "plan_revision": revision,
                "original_classification": initial,
                "final_classification": final,
                "correction_reason": todo.reason if initial != final else None,
            }
            record = (
                dict(prior)
                if prior
                else {
                    "id": step_key(run_id, todo.id),
                    "run_id": run_id,
                    "output_json": None,
                    "validation_json": None,
                    "attempt_count": 0,
                    "claimed_by": None,
                    "claimed_at": None,
                    "completed_at": None,
                }
            )
            record.update(
                ordinal=ordinal,
                kind=kind,
                effect=effect,
                state="skipped" if todo.usefulness == "optional" else "pending",
                spec_json=canonical_json(spec),
            )
            replacements.append(record)
            if prior and old_spec["todo"] != todo.model_dump():
                # Retain approval history but revoke any authority bound to the previous payload.
                conn.execute(
                    "UPDATE approvals SET state='cancelled',revision=revision+1,resolved_at=? "
                    "WHERE step_id=? AND state IN ('pending','approved')",
                    (utc_now(), prior["id"]),
                )
        new_ids = {todo.id for todo in plan.todos}
        for key, row in old.items():
            if key in new_ids:
                continue
            if (
                key in frozen
                or row["attempt_count"]
                or calls.get(row["id"])
                or approvals.get(row["id"])
                or any(
                    row[field] is not None
                    for field in (
                        "output_json",
                        "validation_json",
                        "claimed_by",
                        "claimed_at",
                        "completed_at",
                    )
                )
            ):
                raise AppError("CONFLICT", status=409)
        history = list((previous or {}).get("history", []))
        if previous:
            history.append({"revision": previous["revision"], "plan": previous["current"]})
        if len(history) > 32:
            raise AppError("LIMIT_EXCEEDED")
        if task_spec is not None:
            task_spec = TaskSpec.model_validate(task_spec.model_dump())
            if not set(task_spec.source_ids) <= permitted:
                raise AppError("POLICY_DENIED")
            set_value(conn, f"task_spec:{run_id}", task_spec.model_dump())
        if route_bindings is not None:
            if not set(route_bindings) <= new_ids:
                raise AppError("PLAN_INVALID")
            normalized_bindings = {}
            for todo_id, value in route_bindings.items():
                binding = ModelBinding.model_validate(
                    value.model_dump() if isinstance(value, ModelBinding) else value
                )
                todo = next(t for t in plan.todos if t.id == todo_id)
                if isinstance(todo.executor, ToolExecutor) or binding.role != todo.executor.role:
                    raise AppError("PLAN_INVALID")
                normalized_bindings[todo_id] = binding.model_dump()
            set_value(conn, f"route_bindings:{run_id}", normalized_bindings)
        # All eight ordinals may be occupied. Deferral permits a same-ID row replacement
        # while references in calls/approvals remain intact, without disabling FK checks.
        conn.execute("PRAGMA defer_foreign_keys=ON")
        for key, row in old.items():
            if key not in frozen:
                conn.execute("DELETE FROM steps WHERE id=?", (row["id"],))
        for record in replacements:
            columns = tuple(record)
            conn.execute(
                "INSERT INTO steps ("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(record[column] for column in columns),
            )
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AppError("CONFLICT", status=409)
        stored = {"current": plan.model_dump(), "history": history, "revision": revision}
        changed = conn.execute(
            "UPDATE runs SET plan_json=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
            (canonical_json(stored), utc_now(), run_id, expected),
        )
        if changed.rowcount != 1:
            raise AppError("CONFLICT", status=409)
        if run["state"] in {"queued", "awaiting_input"}:
            transition_run(conn, run_id, run["state"], "preparing")
            transition_run(conn, run_id, "preparing", "running")
        elif run["state"] == "retry_wait":
            transition_run(conn, run_id, "retry_wait", "running")
    return get_run(conn, run_id, actor)


def request_cancel(conn, run_id: str, actor: Actor):
    with transaction(conn):
        run = get_run(conn, run_id, actor)
        if run["state"] in TERMINAL or run["state"] == "cancel_requested":
            return run
        transition_run(conn, run_id, run["state"], "cancel_requested")
        conn.execute(
            "UPDATE outbox SET state='cancelled' WHERE run_id=? AND state IN ('pending','retry_wait')",
            (run_id,),
        )
        conn.execute(
            "UPDATE approvals SET state='cancelled',revision=revision+1 WHERE run_id=? AND state IN ('pending','approved')",
            (run_id,),
        )
    return get_run(conn, run_id, actor)


def _source_versions(conn, run, actor):
    versions = {}
    for artifact_id in allowed_sources(conn, run, actor):
        row = conn.execute(
            "SELECT sha256 FROM artifacts WHERE id=? AND actor_id=? AND project_id=? AND deleted_at IS NULL",
            (artifact_id, actor.id, run["project_id"]),
        ).fetchone()
        if row is None:
            raise AppError("SOURCE_CHANGED")
        versions[artifact_id] = row[0]
    return versions


def _proposal(conn, run, row, todo, inputs, actor, *, expires_at):
    args, policy = todo.executor.args, run["policy_json"]
    return ActionProposal(
        actor_id=actor.id,
        project_id=run["project_id"],
        run_id=run["id"],
        step_id=row["id"],
        tool=todo.executor.tool_name,
        args=args,
        source_hashes=_source_versions(conn, run, actor),
        draft_hash=digest(inputs),
        destination=args.get("destination", policy.get("destination")),
        recipient_ref=args.get("recipient_ref", policy.get("recipient_ref")),
        scope_revision=policy.get("scope_revision", 1),
        expires_at=expires_at,
        impact="선택한 결과를 표시된 대상에 전달합니다.",
    ).model_dump()


def resolve_approval(conn, approval_id, actor, decision, proposal_hash, expected_revision, *, now):
    if decision not in {"approve", "reject"} or type(expected_revision) is not int:
        raise AppError("INVALID_INPUT")
    expired = False
    # A channel may atomically include its durable inbox/nonce/cursor in this decision.
    with nullcontext(conn) if conn.in_transaction else transaction(conn):
        row = row_dict(
            conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        )
        if row is None or row["actor_id"] != actor.id:
            raise AppError("NOT_FOUND", status=404)
        run = get_run(conn, row["run_id"], actor)
        if (
            row["state"] != "pending"
            or row["revision"] != expected_revision
            or row["proposal_hash"] != proposal_hash
        ):
            raise AppError("CONFLICT", status=409)
        if run["state"] != "awaiting_approval":
            raise AppError("CONFLICT", status=409)
        expired = datetime.fromisoformat(row["expires_at"]) <= now
        state = "expired" if expired else "approved" if decision == "approve" else "rejected"
        conn.execute(
            "UPDATE approvals SET state=?,resolved_at=?,revision=revision+1 WHERE id=?",
            (state, now.isoformat(), approval_id),
        )
        if state in {"expired", "rejected"}:
            transition_run(conn, run["id"], "awaiting_approval", state)
    if expired:
        raise AppError("APPROVAL_EXPIRED", status=410)
    return {"id": approval_id, "state": state}


def _claim(conn, run, row, todo, inputs, actor, now):
    current = get_run(conn, run["id"], actor)
    if (
        current["state"] not in {"running", "awaiting_approval"}
        or current["revision"] != run["revision"]
    ):
        return False
    if row["effect"] == "external_write":
        approval = row_dict(
            conn.execute(
                "SELECT * FROM approvals WHERE run_id=? AND step_id=? ORDER BY created_at DESC LIMIT 1",
                (run["id"], row["id"]),
            ).fetchone()
        )
        expiry = approval["expires_at"] if approval else (now + timedelta(minutes=30)).isoformat()
        proposal = _proposal(conn, run, row, todo, inputs, actor, expires_at=expiry)
        proposal_hash = digest(proposal)
        if approval is None:
            conn.execute(
                "INSERT INTO approvals (id,run_id,step_id,actor_id,proposal_hash,proposal_json,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    run["id"],
                    row["id"],
                    actor.id,
                    proposal_hash,
                    canonical_json(proposal),
                    expiry,
                    now.isoformat(),
                ),
            )
            transition_run(conn, run["id"], "running", "checking")
            transition_run(conn, run["id"], "checking", "awaiting_approval")
            conn.execute("UPDATE steps SET state='awaiting_approval' WHERE id=?", (row["id"],))
            return False
        if approval["proposal_hash"] != proposal_hash:
            raise AppError("SOURCE_CHANGED")
        if datetime.fromisoformat(expiry) <= now:
            conn.execute(
                "UPDATE approvals SET state='expired',revision=revision+1 WHERE id=?",
                (approval["id"],),
            )
            transition_run(conn, run["id"], "awaiting_approval", "expired")
            return False
        if approval["state"] == "pending":
            return False
        if approval["state"] != "approved":
            raise AppError("CONFLICT", status=409)
        consumed = conn.execute(
            "UPDATE approvals SET state='consumed',consumed_at=?,revision=revision+1 WHERE id=? AND state='approved'",
            (now.isoformat(), approval["id"]),
        )
        if consumed.rowcount != 1:
            raise AppError("CONFLICT", status=409)
        transition_run(conn, run["id"], "awaiting_approval", "ready")
        transition_run(conn, run["id"], "ready", "running")
    if row["attempt_count"] >= 2:
        raise AppError("CALL_LIMIT")
    cursor = conn.execute(
        "UPDATE steps SET state='running',attempt_count=attempt_count+1,claimed_by=?,claimed_at=? WHERE id=? AND state IN ('pending','awaiting_approval')",
        (str(os.getpid()), now.isoformat(), row["id"]),
    )
    if cursor.rowcount != 1:
        raise AppError("CONFLICT", status=409)
    return True


def _refresh_retry_plan(conn, run, row, actor, tools):
    from .routing import reclassify_pending

    if (row["validation_json"] or {}).get("status") != "fail":
        return run
    plan = PlanV1.model_validate(run["plan_json"]["current"])
    completed = [
        row_dict(saved)["spec_json"]["todo"]["id"]
        for saved in conn.execute(
            "SELECT spec_json FROM steps WHERE run_id=? AND state IN ('succeeded','skipped')",
            (run["id"],),
        )
    ]
    todo_id = row["spec_json"]["todo"]["id"]
    revised = reclassify_pending(
        plan,
        {},
        {todo_id: row["validation_json"].get("errors", [])},
        completed_ids=completed,
    )
    if revised == plan:
        return run
    changed = {old.id for old, new in zip(plan.todos, revised.todos, strict=True) if old != new}
    bindings = {
        key: value
        for key, value in get_value(conn, "route_bindings:" + run["id"], {}).items()
        if key not in changed
    }
    return install_plan(
        conn,
        run["id"],
        revised,
        actor,
        tools=tools,
        expected_revision=run["revision"],
        route_bindings=bindings,
    )


def execute_next(
    conn,
    run_id,
    *,
    actor,
    tools,
    complete,
    now,
    verifiers: dict[str, Callable] | None = None,
    check_sources: Callable | None = None,
    home: Path | None = None,
):
    run = get_run(conn, run_id, actor)
    if run["state"] in TERMINAL or run["state"] in {
        "awaiting_input",
        "awaiting_budget",
        "queued",
        "preparing",
        "retry_wait",
    }:
        return run
    if run["state"] == "cancel_requested":
        with transaction(conn):
            uncertain = conn.execute(
                "SELECT 1 FROM steps WHERE run_id=? AND effect='external_write' AND state='running'",
                (run_id,),
            ).fetchone()
            if uncertain:
                return run
            conn.execute(
                "UPDATE steps SET state='cancelled' WHERE run_id=? AND state IN ('pending','awaiting_approval')",
                (run_id,),
            )
            transition_run(conn, run_id, "cancel_requested", "cancelled")
        return _finish(conn, run_id, actor)
    rows = [
        row_dict(r)
        for r in conn.execute("SELECT * FROM steps WHERE run_id=? ORDER BY ordinal", (run_id,))
    ]
    row = next((r for r in rows if r["state"] not in {"succeeded", "skipped"}), None)
    if row is None:
        return _finish(conn, run_id, actor)
    if row["state"] not in {"pending", "awaiting_approval"}:
        return run
    if row["state"] == "pending" and row["kind"] in {"model", "verify"}:
        run = _refresh_retry_plan(conn, run, row, actor, tools)
        row = row_dict(conn.execute("SELECT * FROM steps WHERE id=?", (row["id"],)).fetchone())
    todo = TodoV1.model_validate(row["spec_json"]["todo"])
    if todo.clarity == "missing_input":
        transition_run(conn, run_id, "running", "awaiting_input")
        return _finish(conn, run_id, actor)
    done = {r["spec_json"]["todo"]["id"]: r for r in rows if r["state"] == "succeeded"}
    if not set(todo.depends_on) <= done.keys():
        raise AppError("PLAN_INVALID")
    inputs = {}
    for ref in todo.inputs:
        if isinstance(ref, StepInput):
            inputs[ref.step_id] = done[ref.step_id]["output_json"][ref.output_key]
        else:
            inputs[ref.artifact_id] = {"artifact_id": ref.artifact_id}
    context = {"conn": conn, "actor": actor, "run": run, "todo": todo, "inputs": inputs, "now": now}
    if home is not None:
        context["home"] = Path(home).absolute()
    try:
        _source_versions(conn, run, actor)
        if check_sources:
            check_sources(context)
        with transaction(conn):
            if not _claim(conn, run, row, todo, inputs, actor, now):
                return get_run(conn, run_id, actor)
        if isinstance(todo.executor, ToolExecutor):
            binding = tools.get(todo.executor.tool_name)
            if binding is None or binding.effect != row["effect"]:
                raise AppError("POLICY_DENIED")
            output = binding.execute(todo.executor.args, context)
        else:
            if complete is None:
                raise AppError("NO_CAPABLE_MODEL")
            output = complete(todo, inputs, context)
        canonical_json(output)
        if not isinstance(output, dict) or set(output) != {OUTPUT_SCHEMA_KEYS[todo.output_schema]}:
            raise AppError("INVALID_OUTPUT")
        validation = ValidationResult(status="pass", checked_layers=["output_shape"])
        if todo.output_schema == "ValidationResultV1":
            validation = ValidationResult.model_validate(output["validation"])
        from .verification import model_grounding, required_verifiers
        from .verification import registry as verifier_registry

        available_checks = {"model_grounding": model_grounding}
        if home is not None:
            available_checks.update(verifier_registry(home))
        available_checks.update(verifiers or {})
        # Untyped fake tools are confined to low-level executor tests. Production tools are typed.
        checks = (
            required_verifiers(todo)
            if (isinstance(todo.executor, ModelExecutor) or binding.args_schema is not None)
            else todo.verify_with
        )
        for verifier_id in checks:
            if verifier_id not in available_checks:
                raise AppError("UNSUPPORTED_CAPABILITY")
            if verifier_id == "model_grounding" and context.get("semantic_review") is not None:
                checked = model_grounding(
                    output, context, semantic_review=context["semantic_review"]
                )
            else:
                checked = available_checks[verifier_id](output, context)
            validation.checked_layers.extend(checked.checked_layers)
            validation.errors.extend(checked.errors)
            validation.evidence_refs.extend(checked.evidence_refs)
            if checked.status == "fail" or (
                checked.status != "pass" and validation.status != "fail"
            ):
                validation.status = checked.status
        with transaction(conn):
            current = get_run(conn, run_id, actor)
            conn.execute(
                "UPDATE steps SET output_json=?,validation_json=?,completed_at=? WHERE id=?",
                (canonical_json(output), canonical_json(validation), now.isoformat(), row["id"]),
            )
            if current["state"] == "cancel_requested":
                conn.execute(
                    "UPDATE steps SET state=? WHERE id=?",
                    ("succeeded" if validation.status == "pass" else "cancelled", row["id"]),
                )
                conn.execute(
                    "UPDATE steps SET state='cancelled' WHERE run_id=? AND state IN ('pending','awaiting_approval')",
                    (run_id,),
                )
                transition_run(conn, run_id, "cancel_requested", "cancelled")
            elif validation.status == "pass":
                candidate = context.get("response_cache_write")
                if candidate is not None and not context.get("semantic_review"):
                    from .context import cache_put

                    cache_put(
                        conn,
                        context["home"],
                        actor,
                        run["project_id"],
                        candidate["key"],
                        output,
                        source_versions=candidate["versions"],
                        expires_at=(now + timedelta(days=1)).isoformat(),
                        now=now.isoformat(),
                        effect="draft",
                    )
                conn.execute("UPDATE steps SET state='succeeded' WHERE id=?", (row["id"],))
            else:
                calls = conn.execute(
                    "SELECT count(DISTINCT logical_call_no) FROM calls WHERE step_id=?",
                    (row["id"],),
                ).fetchone()[0]
                repair = (
                    isinstance(todo.executor, ModelExecutor)
                    and row["attempt_count"] == 0
                    and calls < 2
                    and validation.status == "fail"
                    and bool(
                        set(validation.errors)
                        & {
                            "NUMERIC_MISMATCH",
                            "NUMERIC_REFERENCE_INVALID",
                            "EVIDENCE_MISMATCH",
                            "MISSING_EVIDENCE",
                            "OWNER_UNGROUNDED",
                            "DUE_UNGROUNDED",
                            "CONFLICTING_SOURCES",
                            "CAUSAL_JUDGMENT",
                            "COUPLED_CONSTRAINTS",
                            "ALTERNATIVE_EVALUATION",
                        }
                    )
                )
                conn.execute(
                    "UPDATE steps SET state=? WHERE id=?",
                    (
                        "pending" if validation.status == "needs_input" or repair else "failed",
                        row["id"],
                    ),
                )
                if not repair:
                    transition_run(
                        conn,
                        run_id,
                        current["state"],
                        "awaiting_input" if validation.status == "needs_input" else "failed",
                    )
    except (AppError, ValidationError, ValueError, OSError) as exc:
        code = exc.code if isinstance(exc, AppError) else "EXECUTION_FAILED"
        with transaction(conn):
            current = get_run(conn, run_id, actor)
            current_step = conn.execute(
                "SELECT state FROM steps WHERE id=?", (row["id"],)
            ).fetchone()[0]
            uncertain = row["effect"] == "external_write" and current_step == "running"
            state = (
                "outcome_unknown"
                if uncertain
                else "cancelled"
                if current["state"] == "cancel_requested"
                else "awaiting_input"
                if current["state"] in {"running", "checking", "preparing"}
                and code in {"MISSING_INPUT", "CONTEXT_LIMIT", "FORMULA_UNVERIFIED"}
                else "failed"
            )
            conn.execute(
                "UPDATE steps SET state=? WHERE id=?",
                ("pending" if state == "awaiting_input" else state, row["id"]),
            )
            if state == "cancelled":
                conn.execute(
                    "UPDATE steps SET state='cancelled' WHERE run_id=? AND state IN ('pending','awaiting_approval')",
                    (run_id,),
                )
            if current["state"] not in TERMINAL:
                transition_run(conn, run_id, current["state"], state)
            conn.execute("UPDATE runs SET error_code=? WHERE id=?", (code, run_id))
        return _finish(conn, run_id, actor)
    return _finish(conn, run_id, actor)


def _finish(conn, run_id, actor):
    with transaction(conn):
        run = get_run(conn, run_id, actor)
        if run["state"] not in {"running", "checking", "ready"}:
            if run["state"] in {"failed", "cancelled", "outcome_unknown", "awaiting_input"}:
                rows = [
                    row_dict(row)
                    for row in conn.execute(
                        "SELECT * FROM steps WHERE run_id=? ORDER BY ordinal", (run_id,)
                    )
                ]
                partial = {
                    "partial": True,
                    "outputs": {
                        row["spec_json"]["todo"]["id"]: row["output_json"]
                        for row in rows
                        if row["state"] == "succeeded"
                    },
                    "incomplete": [
                        {
                            "todo_id": row["spec_json"]["todo"]["id"],
                            "state": row["state"],
                            "validation": row["validation_json"],
                        }
                        for row in rows
                        if row["state"] not in {"succeeded", "skipped"}
                    ],
                    "error_code": run["error_code"],
                }
                conn.execute(
                    "UPDATE runs SET result_json=? WHERE id=?", (canonical_json(partial), run_id)
                )
                return get_run(conn, run_id, actor)
            return run
        if conn.execute(
            "SELECT 1 FROM steps WHERE run_id=? AND state NOT IN ('succeeded','skipped')", (run_id,)
        ).fetchone():
            return run
        outputs = {
            row_dict(r)["spec_json"]["todo"]["id"]: row_dict(r)["output_json"]
            for r in conn.execute(
                "SELECT * FROM steps WHERE run_id=? AND state='succeeded' ORDER BY ordinal",
                (run_id,),
            )
        }
        conn.execute(
            "UPDATE runs SET result_json=? WHERE id=?",
            (canonical_json({"outputs": outputs}), run_id),
        )
        if run["state"] == "running":
            transition_run(conn, run_id, "running", "checking")
        if run["state"] != "ready":
            transition_run(conn, run_id, "checking", "ready")
        transition_run(conn, run_id, "ready", "succeeded")
    return get_run(conn, run_id, actor)


def complete_model_todo(
    todo,
    inputs,
    context,
    *,
    capability_catalog,
    privacy_policy,
    transport,
    price_snapshot=None,
    cancelled=None,
    evaluation_arm=None,
):
    """Keep generation's role; its one remaining call is a repair or required H review."""
    # These imports close the existing engine/tools/routing cycle at execution time.
    from .context import (
        _safe,
        build_model_request,
        cache_get,
        record_cache_lookup,
        response_cache_key,
    )
    from .db import get_task_spec
    from .providers import metered_complete
    from .routing import choose_evaluation_model, choose_model
    from .verification import needs_semantic_review, semantic_review_requested

    conn, actor = context["conn"], context["actor"]
    run = get_run(conn, context["run"]["id"], actor)
    if privacy_policy.project_id != run["project_id"]:
        raise AppError("POLICY_DENIED")
    if evaluation_arm not in {None, "A", "B"}:
        raise AppError("INVALID_INPUT")
    evaluation_snapshot = get_value(conn, "evaluation:" + run["id"])
    if evaluation_snapshot is not None and evaluation_snapshot.get("arm") != evaluation_arm:
        raise AppError("POLICY_DENIED")
    selected = choose_model(todo, capability_catalog, privacy_policy)
    if selected is None:
        raise AppError("MISSING_INPUT")
    model_context = {
        **context,
        "run": run,
        "task_spec": get_task_spec(conn, run["id"], actor),
    }
    previous_calls = conn.execute(
        "SELECT count(DISTINCT c.logical_call_no) FROM calls c JOIN steps s ON c.step_id=s.id "
        "WHERE c.run_id=? AND json_extract(s.spec_json,'$.todo.id')=?",
        (run["id"], todo.id),
    ).fetchone()[0]
    if previous_calls >= 2:
        raise AppError("CALL_LIMIT")
    saved = row_dict(
        conn.execute(
            "SELECT * FROM steps WHERE run_id=? AND json_extract(spec_json,'$.todo.id')=?",
            (run["id"], todo.id),
        ).fetchone()
    )
    errors = (saved["validation_json"] or {}).get("errors") if saved is not None else None
    bindings = get_value(conn, "route_bindings:" + run["id"], {})
    set_value(conn, "route_bindings:" + run["id"], {**bindings, todo.id: selected.model_dump()})
    for attempt in range(2 - previous_calls):
        evaluation = None
        if evaluation_arm is not None and todo.executor.task != "semantic_review":
            selected, evaluation = choose_evaluation_model(
                todo,
                capability_catalog,
                privacy_policy,
                arm=evaluation_arm,
                attempt=previous_calls + attempt + 1,
                run=run,
                actor=actor,
                snapshot=evaluation_snapshot,
            )
        request = build_model_request(
            todo, inputs, model_context, binding=selected, errors=errors, evaluation=evaluation
        )
        cache = None
        if (
            "home" in context
            and not errors
            and not attempt
            and not previous_calls
            and todo.executor.task != "semantic_review"
            and not semantic_review_requested(model_context)
        ):
            from .verification import _evidence_items

            versions = _source_versions(conn, run, actor)
            versions.update({ref.artifact_id: ref.sha256 for ref in _evidence_items(inputs)})
            key = response_cache_key(
                actor,
                run["project_id"],
                versions,
                request,
                selected,
                policy={
                    "privacy": privacy_policy.model_dump(),
                    "verifiers": todo.verify_with,
                    "verification_version": 1,
                    **({"evaluation_arm": evaluation_arm} if evaluation is not None else {}),
                },
                effect="draft",
                scope_revision=run["policy_json"].get("scope_revision", 1),
            )
            cache = {"key": key, "versions": versions}
            cached = cache_get(
                conn,
                context["home"],
                actor,
                run["project_id"],
                key,
                source_versions=versions,
                now=context.get("now", datetime.now(UTC)).isoformat(),
            )
            reused = cached is not None and not needs_semantic_review(cached, model_context)
            record_cache_lookup(conn, run["id"], hit=reused)
            if reused:
                set_value(conn, "cache_hit:" + run["id"] + ":" + todo.id, {"key": key})
                return cached
        purpose = (
            "repair"
            if attempt or previous_calls
            else "review"
            if todo.executor.task == "semantic_review"
            else "generate"
        )
        review_started = False
        try:
            result = metered_complete(
                request,
                selected,
                {**model_context, "purpose": purpose, "repair_errors": errors or []},
                transport=transport,
                price_snapshot=price_snapshot(selected)
                if callable(price_snapshot)
                else price_snapshot,
                cancelled=cancelled,
                evaluation=evaluation,
            )
            if (
                cache is not None
                and len(canonical_json(result.parsed_output).encode("utf-8")) <= 65536
            ):
                try:
                    _safe(result.parsed_output)
                except AppError:
                    pass  # A non-cacheable string does not discard an otherwise valid result.
                else:
                    context["response_cache_write"] = cache
            if needs_semantic_review(result.parsed_output, model_context):
                if previous_calls + attempt + 1 >= 2:
                    raise AppError("CALL_LIMIT")
                reviewer_todo = todo.model_copy(
                    update={
                        "kind": "complex",
                        "reason": "후보 내용의 요청 충족과 근거 해석을 검토한다",
                        "executor": ModelExecutor(role="H", task="semantic_review"),
                        "output_schema": "ValidationResultV1",
                        "done_when": [
                            (
                                "Review candidate_output for review_task and the user's request, using only the "
                                "supplied original inputs. Check meaning, omissions, conflicting evidence, "
                                "unsupported conclusions, uncertainty, and any assigned owners or due dates. "
                                "Do not repair the candidate or invent missing facts. Return pass only when "
                                "the candidate meets its requirements and its interpretations are supported; "
                                "otherwise fail or needs_input. This is semantic review, not numeric or visual proof. "
                                "Use checked_layers=['semantic_review'] and cite the supporting evidence_ids."
                            ),
                        ],
                    }
                )
                reviewer = choose_model(reviewer_todo, capability_catalog, privacy_policy)
                if reviewer is None:
                    raise AppError("NO_CAPABLE_MODEL")
                review_request = build_model_request(
                    reviewer_todo,
                    {
                        **inputs,
                        "candidate_output": result.parsed_output,
                        "review_task": {"task": todo.executor.task, "done_when": todo.done_when},
                    },
                    model_context,
                    binding=reviewer,
                )
                review_started = True
                reviewed = metered_complete(
                    review_request,
                    reviewer,
                    {
                        **model_context,
                        "todo": reviewer_todo,
                        "purpose": "review",
                        "review_of": digest(result.parsed_output),
                    },
                    transport=transport,
                    cancelled=cancelled,
                    price_snapshot=price_snapshot(reviewer)
                    if callable(price_snapshot)
                    else price_snapshot
                    if (reviewer.provider, reviewer.model_id)
                    == (selected.provider, selected.model_id)
                    else None,
                )
                verdict = ValidationResult.model_validate(reviewed.parsed_output["validation"])
                if verdict.status != "pass" or verdict.errors:
                    raise AppError(
                        "MISSING_INPUT"
                        if verdict.status == "needs_input"
                        else "SEMANTIC_REVIEW_FAILED"
                    )
                context["semantic_review"] = (digest(result.parsed_output), verdict)
            return result.parsed_output
        except AppError as exc:
            if (
                review_started
                or exc.code not in {"INVALID_OUTPUT", "OUTPUT_TRUNCATED"}
                or attempt
                or previous_calls
            ):
                raise
            errors = [exc.code]
            if evaluation is not None:
                # Persist the actual failed check before the baseline's second logical call.
                conn.execute(
                    "UPDATE steps SET validation_json=? WHERE id=?",
                    (canonical_json(ValidationResult(status="fail", errors=errors)), saved["id"]),
                )
    raise AppError("INVALID_OUTPUT")


def resume_after_restart(conn, *, previous_process_is_dead: bool):
    if not previous_process_is_dead:
        raise AppError("RECOVERY_REQUIRES_EXCLUSIVE_LOCK")
    recovered = []
    with transaction(conn):
        for raw in conn.execute(
            "SELECT * FROM steps WHERE state IN ('running','checking')"
        ).fetchall():
            row = row_dict(raw)
            run = row_dict(
                conn.execute("SELECT * FROM runs WHERE id=?", (row["run_id"],)).fetchone()
            )
            if run["state"] in TERMINAL:
                continue
            external = row["effect"] in {"external_write", "destructive"}
            conn.execute(
                "UPDATE steps SET state=? WHERE id=?",
                ("outcome_unknown" if external else "pending", row["id"]),
            )
            if external:
                transition_run(conn, run["id"], run["state"], "outcome_unknown")
            elif run["state"] in {"checking", "ready"}:
                transition_run(conn, run["id"], run["state"], "running")
            recovered.append(
                {
                    "run_id": run["id"],
                    "step_id": row["id"],
                    "action": "inspect_remote" if external else "resume",
                }
            )
        conn.execute(
            "UPDATE calls SET state='unknown',usage_basis='unknown' WHERE state IN ('reserved','in_flight')"
        )
        conn.execute("UPDATE outbox SET state='unknown' WHERE state='sending'")
    return recovered
