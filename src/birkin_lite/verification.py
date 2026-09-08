"""Deterministic proof layers; no text/structure check claims semantic or visual truth."""

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from . import office
from .artifacts import metadata, open_artifact
from .contracts import (
    MODEL_OUTPUT_TYPES,
    AppError,
    Evidence,
    ModelExecutor,
    StepInput,
    TodoV1,
    ToolExecutor,
    ValidationResult,
    canonical_json,
    digest,
    evidence_id,
)
from .db import get_run, row_dict
from .sandbox import parse_file


def numeric_grounding(output, context):
    if isinstance(context["todo"].executor, ModelExecutor):
        return model_grounding(output, context)
    value = output.get("aggregate")
    todo = context["todo"]
    if not isinstance(value, dict) or not isinstance(todo.executor, ToolExecutor):
        return ValidationResult(
            status="fail", errors=["INVALID_OUTPUT"], checked_layers=["numeric"]
        )
    if value.get("verification", {}).get("numeric") == "needs_input":
        return ValidationResult(
            status="needs_input", errors=["FORMULA_UNVERIFIED"], checked_layers=["numeric"]
        )
    if todo.executor.tool_name != "table.aggregate":
        return ValidationResult(
            status="fail", errors=["INVALID_OUTPUT"], checked_layers=["numeric"]
        )
    # Re-read the same managed version. Candidate totals are never trusted as calculation inputs.
    from .tools import AggregateArgs

    args = AggregateArgs.model_validate(todo.executor.args)
    expected = office.aggregate(
        args.artifact_id,
        args.sheet,
        args.cell_range,
        args.group_by,
        args.value_columns,
        context,
        **args.policy.model_dump(),
    )
    if expected["verification"]["numeric"] == "needs_input":
        return ValidationResult(
            status="needs_input", errors=["FORMULA_UNVERIFIED"], checked_layers=["numeric"]
        )
    keys = (
        "format",
        "source",
        "selection",
        "unit",
        "rounding",
        "row_counts",
        "columns",
        "groups",
        "ratio",
        "verification",
        "policy",
    )
    valid = all(canonical_json(value.get(key)) == canonical_json(expected.get(key)) for key in keys)
    return ValidationResult(
        status="pass" if valid else "fail",
        errors=[] if valid else ["NUMERIC_MISMATCH"],
        checked_layers=["numeric", "source_version"],
    )


def _evidence_items(value):
    if isinstance(value, dict):
        if {"artifact_id", "sha256", "locator", "quote"} <= value.keys():
            yield Evidence.model_validate(value)
        else:
            for child in value.values():
                yield from _evidence_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from _evidence_items(child)


def _record(artifact_id, context):
    return metadata(context["conn"], artifact_id, context["actor"], context["run"]["project_id"])


def _check_version(artifact_id, sha256, context):
    record = _record(artifact_id, context)
    if record["sha256"] != sha256:
        raise AppError("SOURCE_CHANGED")
    with open_artifact(
        context["home"],
        context["conn"],
        artifact_id,
        context["actor"],
        context["run"]["project_id"],
    ):
        pass


def source_grounding(output, context):
    if isinstance(getattr(context.get("todo"), "executor", None), ModelExecutor):
        return model_grounding(output, context)
    try:
        evidences = list(_evidence_items(output))
        artifact = output.get("artifact")
        if artifact is not None:
            record = _record(artifact["artifact_id"], context)
            provenance = record["provenance_json"]
            versions = provenance.get("source_versions", {})
            if not isinstance(versions, dict):
                raise AppError("INVALID_OUTPUT")
            for artifact_id, sha256 in versions.items():
                _check_version(artifact_id, sha256, context)
            evidences.extend(_evidence_items(provenance.get("evidence_refs", [])))
        elif "evidence" not in output:
            return ValidationResult(
                status="fail", errors=["INVALID_OUTPUT"], checked_layers=["source"]
            )
        if len(evidences) > 100:
            raise AppError("LIMIT_EXCEEDED")
        for evidence in evidences:
            _check_version(evidence.artifact_id, evidence.sha256, context)
            actual = office.locate(evidence.artifact_id, evidence.locator, context)
            if (
                actual.quote != evidence.quote
                or actual.sha256 != evidence.sha256
                or actual.extraction_version != evidence.extraction_version
            ):
                raise AppError("EVIDENCE_MISMATCH")
    except (AppError, ValidationError, KeyError, TypeError) as error:
        code = error.code if isinstance(error, AppError) else "INVALID_OUTPUT"
        return ValidationResult(status="fail", errors=[code], checked_layers=["source"])
    return ValidationResult(status="pass", checked_layers=["source_version", "locator_quote"])


def report_structure(output, context):
    try:
        artifact_id = output["artifact"]["artifact_id"]
        record = _record(artifact_id, context)
        provenance = record["provenance_json"]
        if record["run_id"] != context["run"]["id"] or "expected_structure" not in provenance:
            raise AppError("INVALID_OUTPUT")
        with open_artifact(
            context["home"],
            context["conn"],
            artifact_id,
            context["actor"],
            context["run"]["project_id"],
        ):
            result = parse_file(
                Path(context["home"]) / record["relative_path"],
                provenance["filename"],
                operation="structure",
                args={"expected": provenance["expected_structure"]},
            )
        if result.get("status") != "pass":
            raise AppError("STRUCTURE_MISMATCH")
    except (AppError, KeyError, TypeError) as error:
        code = error.code if isinstance(error, AppError) else "INVALID_OUTPUT"
        return ValidationResult(status="fail", errors=[code], checked_layers=["structure"])
    return ValidationResult(status="pass", checked_layers=["preflight", "structure"])


MODEL_VERIFIER_ALIASES = {
    "report_grounding",
    "meeting_actions_grounding",
    "draft_reply_grounding",
    "search_answer_grounding",
    "model_grounding",
}


def required_verifiers(todo):
    """A model-generated empty list cannot remove a server-required proof layer."""
    checks = list(todo.verify_with)
    if isinstance(todo.executor, ModelExecutor):
        checks = [
            "model_grounding"
            if check in MODEL_VERIFIER_ALIASES | {"numeric_grounding", "source_grounding"}
            else check
            for check in checks
        ]
        checks.append("model_grounding")
    else:
        checks.extend(
            {
                "table.aggregate": ["numeric_grounding"],
                "source.extract": ["source_grounding"],
                "source.search": ["source_grounding"],
                "document.create": ["source_grounding", "report_structure"],
            }.get(todo.executor.tool_name, [])
        )
    return list(dict.fromkeys(checks))


def _supplied_evidence(context):
    evidences = list(_evidence_items(context["inputs"]))
    if len(evidences) > 100:
        raise AppError("LIMIT_EXCEEDED")
    return {evidence_id(item): item for item in evidences}


def _check_evidence(evidences, context):
    for evidence in evidences.values():
        _check_version(evidence.artifact_id, evidence.sha256, context)
        actual = office.locate(evidence.artifact_id, evidence.locator, context)
        if (
            actual.quote != evidence.quote
            or actual.sha256 != evidence.sha256
            or actual.extraction_version != evidence.extraction_version
        ):
            raise AppError("EVIDENCE_MISMATCH")


def _claim_refs(claims, evidences):
    if len({claim.id for claim in claims}) != len(claims):
        raise AppError("INVALID_OUTPUT")
    refs = set()
    for claim in claims:
        if claim.kind == "fact" and not claim.evidence_ids:
            raise AppError("MISSING_EVIDENCE")
        if not set(claim.evidence_ids) <= evidences.keys():
            raise AppError("EVIDENCE_MISMATCH")
        refs.update(claim.evidence_ids)
    return refs


def _absolute_dates(text):
    # Only a full source-written year/month/day can become an ISO date.
    for match in re.finditer(
        r"(?<!\d)(\d{4})(?:\s*[-./]\s*|\s*년\s*)(\d{1,2})"
        r"(?:\s*[-./]\s*|\s*월\s*)(\d{1,2})(?:\s*일)?(?!\d)",
        text,
    ):
        try:
            yield date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            continue


def _review_assignment(value, quotes, refs, reviewed_refs, missing_code):
    # ponytail: literal presence only routes to review; the reviewer must establish attribution.
    if not any(value in quote for quote in quotes):
        raise AppError(missing_code)
    return reviewed_refs is None or not set(refs) <= reviewed_refs


def _meeting_refs(actions, evidences, *, reviewed_refs=None):
    refs = set()
    needs_review = False
    for action in actions:
        if not set(action.evidence_ids) <= evidences.keys():
            raise AppError("EVIDENCE_MISMATCH")
        quotes = [evidences[ref].quote for ref in action.evidence_ids]
        if action.owner is not None and action.owner.casefold().strip() in {
            "미정",
            "미배정",
            "없음",
            "unknown",
            "unassigned",
            "tbd",
        }:
            raise AppError("OWNER_UNGROUNDED")
        if action.owner is not None and not any(
            action.action in quote
            and len(
                re.findall(r"(?:\bowner|\bassignee|담당자|담당)\s*[:=]", quote, flags=re.IGNORECASE)
            )
            == 1
            and re.search(
                r"(?:\bowner|\bassignee|담당자|담당)\s*[:=]\s*"
                + re.escape(action.owner)
                + r"(?![\w])",
                quote,
                flags=re.IGNORECASE,
            )
            for quote in quotes
        ):
            if any(
                re.search(
                    r"(?:담당자|담당|\bowner|\bassignee)\s*[:=]\s*"
                    r"(?:미정|미배정|없음|unknown|unassigned|tbd)(?![\w])",
                    quote,
                    flags=re.IGNORECASE,
                )
                for quote in quotes
            ):
                raise AppError("OWNER_UNGROUNDED")
            needs_review |= _review_assignment(
                action.owner,
                quotes,
                action.evidence_ids,
                reviewed_refs,
                "OWNER_UNGROUNDED",
            )
        if action.due_text is not None and not any(
            action.action in quote
            and len(
                re.findall(
                    r"기한|마감|완료일|\bdue\b|\bdeadline\b|까지", quote, flags=re.IGNORECASE
                )
            )
            == 1
            and re.search(
                r"(?:기한|마감|완료일|\bdue(?:\s+by)?|\bdeadline)\s*[:=]?\s*"
                + re.escape(action.due_text)
                + r"(?![\w])|"
                + re.escape(action.due_text)
                + r"\s*까지",
                quote,
                flags=re.IGNORECASE,
            )
            for quote in quotes
        ):
            needs_review |= _review_assignment(
                action.due_text,
                quotes,
                action.evidence_ids,
                reviewed_refs,
                "DUE_UNGROUNDED",
            )
        if action.due_date is not None and (
            action.due_text is None or action.due_date not in set(_absolute_dates(action.due_text))
        ):
            raise AppError("DUE_UNGROUNDED")
        refs.update(action.evidence_ids)
    if needs_review:
        raise AppError("SEMANTIC_REVIEW_REQUIRED")
    return refs


def semantic_review_requested(context):
    """User precision and complex interpretation require review, without changing generation."""
    from .routing import COMPLEX_SIGNALS

    todo = context.get("todo")
    if not isinstance(getattr(todo, "executor", None), ModelExecutor):
        return False
    return todo.executor.task != "semantic_review" and (
        context.get("run", {}).get("request_json", {}).get("quality_mode") == "high"
        or todo.kind == "complex"
        or bool(COMPLEX_SIGNALS.intersection(todo.signals))
    )


def _model_fields(parsed, evidences, context, *, reviewed_refs=None):
    claims, numeric, rendered, refs, needs_review = [], [], None, set(), False
    if hasattr(parsed, "sections"):
        claims, numeric = parsed.sections.claims, parsed.sections.numeric_claims
        rendered = parsed.sections.content.model_dump()
        if evidences and not claims and not numeric and not parsed.sections.uncertainties:
            raise AppError("MISSING_EVIDENCE")
    elif hasattr(parsed, "actions"):
        try:
            refs = _meeting_refs(parsed.actions, evidences, reviewed_refs=reviewed_refs)
        except AppError as error:
            if error.code != "SEMANTIC_REVIEW_REQUIRED":
                raise
            # All literal fields were checked before this signal; no attribution is certified.
            refs = {ref for action in parsed.actions for ref in action.evidence_ids}
            needs_review = True
        rendered = [action.action for action in parsed.actions]
    elif hasattr(parsed, "draft"):
        claims = parsed.draft.claims
        rendered = {"subject": parsed.draft.subject, "body": parsed.draft.body}
    elif hasattr(parsed, "answer"):
        claims, rendered = parsed.answer.claims, parsed.answer.text
        if parsed.answer.found and not any(claim.kind == "fact" for claim in claims):
            raise AppError("MISSING_EVIDENCE")
        if not parsed.answer.found and claims:
            raise AppError("INVALID_OUTPUT")
    else:
        if not context["inputs"]:
            raise AppError("MISSING_INPUT")
        if parsed.validation.checked_layers != ["semantic_review"]:
            raise AppError("INVALID_OUTPUT")
        if (evidences and not parsed.validation.evidence_refs) or not set(
            parsed.validation.evidence_refs
        ) <= evidences.keys():
            raise AppError("EVIDENCE_MISMATCH")
        refs.update(parsed.validation.evidence_refs)
    refs.update(_claim_refs(claims, evidences))
    return numeric, rendered, refs, needs_review


def _source_numbers(evidences, refs, context):
    values = _numbers([evidences[ref].quote for ref in refs])
    if not context["inputs"]:
        values.update(_numbers(context.get("run", {}).get("request_json", {}).get("request", "")))
    return values


def needs_semantic_review(output, context):
    """Pure routing hint; only model_grounding certifies actual source and numeric inputs."""
    todo = context.get("todo")
    if (
        not isinstance(getattr(todo, "executor", None), ModelExecutor)
        or todo.executor.task == "semantic_review"
    ):
        return False
    try:
        model_type = MODEL_OUTPUT_TYPES[todo.executor.task]
        if model_type.__name__ != todo.output_schema:
            return False
        parsed = model_type.model_validate(output)
        evidences = _supplied_evidence(context)
        numeric, rendered, refs, meeting_review = _model_fields(parsed, evidences, context)
        supported = _source_numbers(evidences, refs, context)
        # This hint may recognize claimed values; the mandatory verifier independently recomputes them.
        supported.update(Decimal(claim.value) for claim in numeric)
        return (meeting_review or semantic_review_requested(context)) and not (
            _numbers(rendered) - supported
        )
    except (AppError, ValidationError, KeyError, TypeError, ValueError):
        pass
    return False


def _verified_aggregate(step_id, context):
    todo = context["todo"]
    if not any(
        isinstance(ref, StepInput) and ref.step_id == step_id and ref.output_key == "aggregate"
        for ref in todo.inputs
    ):
        raise AppError("NUMERIC_REFERENCE_INVALID")
    run = get_run(context["conn"], context["run"]["id"], context["actor"])
    for raw in context["conn"].execute("SELECT * FROM steps WHERE run_id=?", (run["id"],)):
        row = row_dict(raw)
        source_todo = TodoV1.model_validate(row["spec_json"]["todo"])
        if source_todo.id != step_id:
            continue
        if row["state"] != "succeeded" or not isinstance(source_todo.executor, ToolExecutor):
            break
        aggregate = row["output_json"].get("aggregate")
        if aggregate is None or canonical_json(aggregate) != canonical_json(
            context["inputs"].get(step_id)
        ):
            break
        check = numeric_grounding(
            {"aggregate": aggregate}, {**context, "run": run, "todo": source_todo}
        )
        if check.status != "pass":
            raise AppError(check.errors[0] if check.errors else "NUMERIC_MISMATCH")
        return aggregate
    raise AppError("NUMERIC_REFERENCE_INVALID")


def _numeric_refs(claims, context):
    aggregates, values = {}, set()
    for claim in claims:
        if claim.aggregate_step not in aggregates:
            aggregates[claim.aggregate_step] = _verified_aggregate(claim.aggregate_step, context)
        aggregate = aggregates[claim.aggregate_step]
        if claim.column not in aggregate["columns"]:
            raise AppError("NUMERIC_REFERENCE_INVALID")
        if claim.operation == "ratio":
            policy = aggregate["policy"].get("ratio")
            if (
                claim.group is not None
                or not policy
                or claim.column != policy["numerator"]
                or claim.unit != "ratio"
            ):
                raise AppError("NUMERIC_REFERENCE_INVALID")
            expected = aggregate["ratio"]["value"]
        elif claim.group is not None:
            # The aggregate tool exposes grouped sums, not grouped means or ratios.
            if claim.operation != "sum" or claim.unit != aggregate["unit"]:
                raise AppError("NUMERIC_REFERENCE_INVALID")
            group = next((item for item in aggregate["groups"] if item["key"] == claim.group), None)
            if group is None:
                raise AppError("NUMERIC_REFERENCE_INVALID")
            expected = group["sums"][claim.column]
        else:
            if claim.unit != aggregate["unit"]:
                raise AppError("NUMERIC_REFERENCE_INVALID")
            expected = aggregate["columns"][claim.column][claim.operation]
        if expected is None or Decimal(claim.value) != Decimal(expected):
            raise AppError("NUMERIC_MISMATCH")
        values.add(Decimal(claim.value))
    return values


def _numbers(value):
    if isinstance(value, dict):
        return set().union(*(_numbers(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(_numbers(child) for child in value))
    if isinstance(value, str):
        return {
            Decimal(match.replace(",", ""))
            for match in re.findall(
                r"(?<![A-Za-z0-9_])[+-]?\d+(?:,\d{3})*(?:\.\d+)?(?![A-Za-z0-9_])", value
            )
        }
    return {Decimal(value)} if type(value) is int else set()


def model_grounding(output, context, *, semantic_review=None):
    """Check supplied references and values; do not assert paraphrase/causal/visual truth."""
    layers = ["schema"]
    try:
        todo = context["todo"]
        if not isinstance(todo.executor, ModelExecutor):
            raise AppError("INVALID_OUTPUT")
        model_type = MODEL_OUTPUT_TYPES[todo.executor.task]
        if model_type.__name__ != todo.output_schema:
            raise AppError("INVALID_OUTPUT")
        parsed = model_type.model_validate(output)
        evidences = _supplied_evidence(context)
        _check_evidence(evidences, context)
        if evidences:
            layers.extend(["source_version", "locator_quote"])
        reviewed_refs = None
        if semantic_review is not None:
            # An engine-only keyword binds a successful review to these exact candidate bytes.
            # Source text and JSON fields cannot manufacture this separate call argument.
            if (
                not isinstance(semantic_review, tuple)
                or len(semantic_review) != 2
                or semantic_review[0] != digest(output)
                or not isinstance(semantic_review[1], ValidationResult)
                or semantic_review[1].status != "pass"
                or semantic_review[1].errors
                or semantic_review[1].checked_layers != ["semantic_review"]
            ):
                raise AppError("INVALID_OUTPUT")
            reviewed_refs = set(semantic_review[1].evidence_refs)
            if (evidences and not reviewed_refs) or not reviewed_refs <= evidences.keys():
                raise AppError("EVIDENCE_MISMATCH")
            layers.append("semantic_review")
        numeric, rendered, refs, meeting_review = _model_fields(
            parsed, evidences, context, reviewed_refs=reviewed_refs
        )
        if hasattr(parsed, "actions") and not meeting_review:
            layers.append(
                "explicit_meeting_fields" if reviewed_refs is None else "reviewed_meeting_fields"
            )
        layers.append("evidence_references")
        supported_numbers = _numeric_refs(numeric, context)
        supported_numbers.update(_source_numbers(evidences, refs, context))
        if rendered is not None and _numbers(rendered) - supported_numbers:
            raise AppError("NUMERIC_MISMATCH")
        layers.append("numeric_references")
        if meeting_review or semantic_review_requested(context):
            if reviewed_refs is None:
                raise AppError("SEMANTIC_REVIEW_REQUIRED")
            if not refs <= reviewed_refs:
                raise AppError("EVIDENCE_MISMATCH")
    except (AppError, ValidationError, ValueError, KeyError, TypeError) as error:
        code = error.code if isinstance(error, AppError) else "INVALID_OUTPUT"
        return ValidationResult(
            status="needs_input" if code in {"MISSING_INPUT", "FORMULA_UNVERIFIED"} else "fail",
            errors=[code],
            checked_layers=list(dict.fromkeys(layers)),
        )
    return ValidationResult(
        status="pass", evidence_refs=sorted(refs), checked_layers=list(dict.fromkeys(layers))
    )


def registry(home):
    def bind(function):
        return lambda output, context: function(output, {**context, "home": Path(home).absolute()})

    return {
        "numeric_grounding": bind(numeric_grounding),
        "source_grounding": bind(source_grounding),
        "report_structure": bind(report_structure),
        "artifact_structure": bind(report_structure),
        **{name: bind(model_grounding) for name in MODEL_VERIFIER_ALIASES},
    }
