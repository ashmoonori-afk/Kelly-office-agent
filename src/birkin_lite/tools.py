"""The executable tool catalog. Its typed arguments cannot grant permissions."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from . import office
from .artifacts import metadata, open_artifact, publish_draft
from .context import cache_get, cache_put, extraction_cache_key, record_cache_lookup
from .contracts import (
    AppError,
    DraftReply,
    Evidence,
    Id,
    MeetingActionsV1,
    Narrative,
    Receipt,
    SearchAnswer,
    StrictModel,
    canonical_json,
    digest,
    evidence_id,
)
from .db import allowed_sources, get_run, row_dict
from .engine import Tool
from .parser_worker import PREFLIGHT_VERSION
from .sandbox import parse_file


class ArtifactArgs(StrictModel):
    artifact_id: Id


class InspectArgs(ArtifactArgs):
    sheet: Annotated[str, Field(min_length=1, max_length=100)] | None = None


class ExtractArgs(ArtifactArgs):
    projection: office.ExtractProjection = Field(default_factory=office.ExtractProjection)
    limits: office.ExtractLimits = Field(default_factory=office.ExtractLimits)


class SearchArgs(StrictModel):
    query: Annotated[str, Field(min_length=1, max_length=500)]
    source_ids: list[Id] | None = Field(default=None, max_length=20)
    limit: Annotated[int, Field(ge=1, le=20)] = 6


class AggregateArgs(ArtifactArgs):
    sheet: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    cell_range: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    group_by: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    value_columns: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=20
    )
    policy: office.AggregatePolicy = Field(default_factory=office.AggregatePolicy)


class CreateArgs(StrictModel):
    template_id: Literal["office-report-v1", "table-v1"]
    content: office.DocumentContent | None = None
    input_ref: Id | None = None
    evidence_refs: list[Evidence] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def one_content(self):
        if (self.content is None) == (self.input_ref is None):
            raise ValueError("Provide content or an input reference")
        return self


class ArtifactReference(StrictModel):
    artifact_id: Id | None = None
    input_ref: Id | None = None

    @model_validator(mode="after")
    def one_reference(self):
        if (self.artifact_id is None) == (self.input_ref is None):
            raise ValueError("Provide one artifact reference")
        return self


class ValidateArgs(ArtifactReference):
    checks: list[Literal["preflight", "source_grounding", "report_structure"]] = Field(
        default_factory=lambda: ["preflight"], min_length=1, max_length=3
    )


def resolve_artifact(value, context):
    if value.artifact_id is not None:
        return value.artifact_id
    referenced = context["inputs"].get(value.input_ref)
    if not isinstance(referenced, dict) or not isinstance(referenced.get("artifact_id"), str):
        raise AppError("PLAN_INVALID")
    return referenced["artifact_id"]


def _scope(artifact_id, context, *, source=False):
    run, actor, conn = context["run"], context["actor"], context["conn"]
    record = metadata(conn, artifact_id, actor, run["project_id"])
    if source and record["kind"] != "source":
        raise AppError("POLICY_DENIED")
    if artifact_id not in allowed_sources(conn, run, actor) and record["run_id"] != run["id"]:
        raise AppError("POLICY_DENIED")
    return record


def _inspection(artifact_id, context, *, sheet=None):
    record = _scope(artifact_id, context)
    inspection = record["provenance_json"]["inspection"]
    with open_artifact(
        context["home"],
        context["conn"],
        artifact_id,
        context["actor"],
        context["run"]["project_id"],
    ):
        reusable = (
            record["kind"] == "source"
            and inspection.get("format") in {"txt", "md", "docx", "pdf"}
            and inspection.get("parser_version") == PREFLIGHT_VERSION
            and inspection.get("sha256") == record["sha256"]
        )
        # Non-table inspect adds no fields. The open above still checks current bytes and scope.
        result = (
            {}
            if reusable
            else parse_file(
                context["home"] / record["relative_path"],
                record["provenance_json"]["filename"],
                operation="inspect" if record["kind"] == "source" else "preflight",
                args={"sheet": sheet},
            )
        )
    return {
        **inspection,
        **result,
        "artifact_id": artifact_id,
        "sha256": record["sha256"],
        "filename": record["provenance_json"]["filename"],
    }


def inspect(args, context):
    value = InspectArgs.model_validate(args)
    return {"source": _inspection(value.artifact_id, context, sheet=value.sheet)}


def extract(args, context):
    value = ExtractArgs.model_validate(args)
    record = _scope(value.artifact_id, context, source=True)
    conn, home, actor = context["conn"], context["home"], context["actor"]
    run = get_run(conn, context["run"]["id"], actor)
    projection = value.projection.model_dump(exclude_none=True)
    limits = value.limits.model_dump()
    versions = {value.artifact_id: record["sha256"]}
    key = extraction_cache_key(
        actor,
        run["project_id"],
        versions,
        parser_version=office.EXTRACTION_VERSION,
        projection=projection,
        limits=limits,
        scope_revision=run["policy_json"].get("scope_revision", 1),
    )
    now = datetime.now(UTC)
    cached = cache_get(
        conn,
        home,
        actor,
        run["project_id"],
        key,
        source_versions=versions,
        now=now.isoformat(),
    )
    record_cache_lookup(conn, run["id"], hit=cached is not None)
    if cached is not None:
        return cached
    spans = office.extract(value.artifact_id, projection, limits, context)
    output = {
        "evidence": {
            "items": [span.model_dump() for span in spans],
            "projection": projection,
            "bounded_page": projection.get("page", False),
        }
    }
    if len(canonical_json(output).encode("utf-8")) <= 65536:
        try:
            cache_put(
                conn,
                home,
                actor,
                run["project_id"],
                key,
                output,
                source_versions=versions,
                expires_at=(now + timedelta(hours=24)).isoformat(),
                now=now.isoformat(),
            )
        except AppError as error:
            # Valid extracted text may contain paths or secret-like strings forbidden in caches.
            if error.code != "POLICY_DENIED":
                raise
    return output


def search(args, context):
    value = SearchArgs.model_validate(args)
    run, conn, actor = context["run"], context["conn"], context["actor"]
    # A project search is explicitly scoped to its authorized project, never all home files.
    ids = value.source_ids
    if ids is None:
        ids = allowed_sources(conn, run, actor) or [
            row[0]
            for row in conn.execute(
                "SELECT id FROM artifacts WHERE actor_id=? AND project_id=? AND kind='source' "
                "AND deleted_at IS NULL ORDER BY created_at,id LIMIT 21",
                (actor.id, run["project_id"]),
            )
        ]
        if len(ids) > 20:
            raise AppError("MISSING_INPUT")
    for artifact_id in ids:
        record = metadata(conn, artifact_id, actor, run["project_id"])
        if record["kind"] != "source":
            raise AppError("POLICY_DENIED")
    result = (
        office.search(ids, value.query, context, limit=value.limit)
        if ids
        else {"items": [], "has_more": False}
    )
    if value.source_ids is None and not allowed_sources(conn, run, actor):
        from .memory import evidence_from_memory, search_memory

        memories = search_memory(
            context["home"],
            conn,
            actor,
            run["project_id"],
            {"query": value.query, "limit": value.limit},
            now=context.get("now"),
        )
        combined = [
            *result["items"],
            *[evidence_from_memory(item, context.get("now")).model_dump() for item in memories],
        ]
        result = {
            "items": combined[: value.limit],
            "has_more": result["has_more"] or len(combined) > value.limit,
        }
    return {"evidence": {**result, "query": value.query, "source_ids": ids}}


def aggregate(args, context):
    value = AggregateArgs.model_validate(args)
    _scope(value.artifact_id, context, source=True)
    result = office.aggregate(
        value.artifact_id,
        value.sheet,
        value.cell_range,
        value.group_by,
        value.value_columns,
        context,
        **value.policy.model_dump(),
    )
    return {"aggregate": result}


def _content_from_input(value, output_format):
    if isinstance(value, dict) and "content" in value:
        narrative = Narrative.model_validate(value)
        content = narrative.content.model_dump()
        content["paragraphs"].extend("미확정: " + item for item in narrative.uncertainties)
        content["paragraphs"].extend(
            "해석: " + claim.text for claim in narrative.claims if claim.kind == "inference"
        )
        return office.DocumentContent.model_validate(content)
    if isinstance(value, list):
        actions = MeetingActionsV1(actions=value).actions
        return office.DocumentContent.model_validate(
            {
                "title": "" if output_format in {"csv", "xlsx"} else "회의 할 일",
                "tables": [
                    {
                        "headers": ["할 일", "담당자", "기한"],
                        "rows": [
                            [
                                action.action,
                                action.owner or "미정",
                                action.due_date or action.due_text or "미정",
                            ]
                            for action in actions
                        ],
                    }
                ],
            }
        )
    if isinstance(value, dict) and "subject" in value and "body" in value:
        draft = DraftReply.model_validate(value)
        return office.DocumentContent(
            title=draft.subject,
            paragraphs=["수신인: " + (draft.recipient or "미정"), draft.body],
        )
    if isinstance(value, dict) and "found" in value:
        answer = SearchAnswer.model_validate(value)
        return office.DocumentContent(
            title="검색 결과" if answer.found else "근거를 찾지 못했습니다",
            paragraphs=[answer.text],
        )
    if isinstance(value, dict) and "columns" in value and "groups" in value:
        columns = list(value["columns"])
        groups = value["groups"] or [
            {"key": "전체", "sums": {column: value["columns"][column]["sum"] for column in columns}}
        ]
        unit = (
            f" ({value['unit']})"
            if output_format in {"csv", "xlsx"} and value["unit"] != "unspecified"
            else ""
        )
        headers = ["그룹", *(column + unit for column in columns)]
        rows = [[group["key"], *[group["sums"][c] for c in columns]] for group in groups]
        if value.get("ratio") is not None:
            ratio, policy = value["ratio"], value["policy"]["ratio"]
            headers.append(f"전체 비율 ({policy['numerator']}/{policy['denominator']})")
            rendered = (
                "미정 (분모가 0)" if ratio["reason"] == "zero_denominator" else ratio["value"]
            )
            for index, row in enumerate(rows):
                row.append(rendered if index == 0 else "")
        return office.DocumentContent.model_validate(
            {
                "title": "" if output_format in {"csv", "xlsx"} else "집계 결과",
                "paragraphs": []
                if output_format in {"csv", "xlsx"}
                else [f"단위: {value['unit']}", f"포함 행: {value['row_counts']['included']}"],
                "tables": [{"headers": headers, "rows": rows}],
            }
        )
    return office.DocumentContent.model_validate(value)


def _input_provenance(input_ref, context):
    """Read only completed ancestors; the model cannot supply its own proof metadata."""
    from .verification import _evidence_items

    rows = {
        row["spec_json"]["todo"]["id"]: row
        for raw in context["conn"].execute(
            "SELECT * FROM steps WHERE run_id=? AND state='succeeded'",
            (context["run"]["id"],),
        )
        for row in [row_dict(raw)]
    }
    if input_ref not in rows:
        raise AppError("PLAN_INVALID")
    producer = rows[input_ref]
    if producer["validation_json"].get("status") != "pass":
        raise AppError("INVALID_OUTPUT")
    seen, refs, pending = set(), {}, [input_ref]
    while pending:
        step_id = pending.pop()
        if step_id in seen:
            continue
        seen.add(step_id)
        if step_id not in rows or len(seen) > 8:
            raise AppError("PLAN_INVALID")
        row = rows[step_id]
        for ref in _evidence_items(row["output_json"]):
            refs[evidence_id(ref)] = ref
        pending.extend(row["spec_json"]["todo"]["depends_on"])
    return list(refs.values()), {
        "step_id": input_ref,
        "output_hash": digest(producer["output_json"]),
        "validation": producer["validation_json"],
    }


def create(args, context):
    value = CreateArgs.model_validate(args)
    if (value.content is None) == (value.input_ref is None):
        raise AppError("INVALID_INPUT")
    refs, proof = value.evidence_refs, None
    if value.input_ref is not None:
        if value.input_ref not in context["inputs"]:
            raise AppError("PLAN_INVALID")
        content = _content_from_input(
            context["inputs"][value.input_ref], context["run"]["request_json"]["output_format"]
        )
        inherited, proof = _input_provenance(value.input_ref, context)
        refs = list({evidence_id(ref): ref for ref in [*refs, *inherited]}.values())
    else:
        content = value.content
    if refs and context["run"]["request_json"]["output_format"] not in {"csv", "xlsx"}:
        content = content.model_copy(deep=True)
        content.paragraphs.extend(
            [
                "근거 위치",
                *[
                    f"[{index}] {metadata(context['conn'], ref.artifact_id, context['actor'], context['run']['project_id'])['provenance_json']['filename']} · {ref.locator}"
                    for index, ref in enumerate(refs, 1)
                ],
            ]
        )
    artifact = office.create_document(
        value.template_id, content.model_dump(), refs, {**context, "content_proof": proof}
    )
    return {"artifact": public_artifact(artifact)}


def public_artifact(record):
    return {
        "artifact_id": record["id"],
        "sha256": record["sha256"],
        "media_type": record["media_type"],
        "byte_size": record["byte_size"],
        "filename": record["provenance_json"]["filename"],
        "validation": record["validation_json"],
    }


def memory_search(args, context):
    from .memory import search_memory

    return {
        "memories": search_memory(
            context["home"],
            context["conn"],
            context["actor"],
            context["run"]["project_id"],
            args,
            now=context.get("now"),
        )
    }


def memory_save(args, context):
    from .memory import save_memory

    # The authenticated UI records the exact confirmed content. Model arguments cannot consent.
    confirmed = context["run"]["policy_json"].get("confirmed_memory_saves", {})
    exact = confirmed.get(context["todo"].id) == digest(args)
    saved = save_memory(
        context["home"],
        context["conn"],
        context["actor"],
        context["run"]["project_id"],
        args,
        explicit_confirmed=exact,
        now=context.get("now"),
    )
    return {"memories": [saved]}


def validate(args, context):
    from .verification import report_structure, source_grounding

    value = ValidateArgs.model_validate(args)
    artifact_id = resolve_artifact(value, context)
    _inspection(artifact_id, context)
    result = {"status": "pass", "errors": [], "checked_layers": ["preflight"], "evidence_refs": []}
    for check in value.checks:
        if check == "preflight":
            continue
        verifier = {"source_grounding": source_grounding, "report_structure": report_structure}[
            check
        ]
        checked = verifier({"artifact": {"artifact_id": artifact_id}}, context)
        result["errors"].extend(checked.errors)
        result["checked_layers"].extend(checked.checked_layers)
        if checked.status == "fail" or (checked.status != "pass" and result["status"] != "fail"):
            result["status"] = checked.status
    context["conn"].execute(
        "UPDATE artifacts SET validation_json=? WHERE id=? AND actor_id=? AND project_id=?",
        (
            canonical_json({**result, "visual_verified": False}),
            artifact_id,
            context["actor"].id,
            context["run"]["project_id"],
        ),
    )
    return {"validation": result}


def publish(args, context):
    value = ArtifactReference.model_validate(args)
    artifact_id = resolve_artifact(value, context)
    record = _scope(artifact_id, context)
    if record["validation_json"].get("status") != "pass":
        raise AppError("INVALID_OUTPUT")
    receipt = publish_draft(
        context["home"],
        context["conn"],
        artifact_id,
        context["actor"],
        context["run"]["project_id"],
        run_id=context["run"]["id"],
        step_id=context["todo"].id,
    )
    return {"receipt": Receipt.model_validate(receipt).model_dump()}


def registry(home: Path) -> dict[str, Tool]:
    from .automation import AutomationConfig
    from .memory import MemorySaveArgs, MemorySearchArgs

    home = Path(home).absolute()

    def bind(function, schema, effect):
        def execute(args, context):
            # Refresh owned state and permissions; a saved plan cannot carry a stale actor snapshot.
            current = get_run(context["conn"], context["run"]["id"], context["actor"])
            return function(
                schema.model_validate(args).model_dump(), {**context, "home": home, "run": current}
            )

        return Tool(effect, execute, schema)

    return {
        "source.inspect": bind(inspect, InspectArgs, "read"),
        "source.extract": bind(extract, ExtractArgs, "read"),
        "source.search": bind(search, SearchArgs, "read"),
        "table.aggregate": bind(aggregate, AggregateArgs, "read"),
        "document.create": bind(create, CreateArgs, "draft"),
        "artifact.validate": bind(validate, ValidateArgs, "read"),
        "artifact.publish": bind(publish, ArtifactReference, "draft"),
        "memory.search": bind(memory_search, MemorySearchArgs, "read"),
        "memory.save": bind(memory_save, MemorySaveArgs, "draft"),
        "automation.propose": bind(propose_automation, AutomationConfig, "read"),
    }


def propose_automation(args, context):
    from .automation import AutomationConfig
    from .db import _validate_automation_scope

    config = AutomationConfig.model_validate(args)
    if config.project_id != context["run"]["project_id"]:
        raise AppError("POLICY_DENIED")
    if not set(config.source_scope.artifact_ids) <= set(
        allowed_sources(context["conn"], context["run"], context["actor"])
    ):
        raise AppError("POLICY_DENIED")
    _validate_automation_scope(context["conn"], context["actor"], config)
    return {"proposal": {"config": config.model_dump(), "enabled": False}}


def catalog(tools):
    return [
        {"name": name, "effect": tool.effect, "parameters": tool.args_schema.model_json_schema()}
        for name, tool in tools.items()
        if tool.args_schema is not None
    ]


def check_sources(context):
    for artifact_id in allowed_sources(context["conn"], context["run"], context["actor"]):
        with open_artifact(
            context["home"],
            context["conn"],
            artifact_id,
            context["actor"],
            context["run"]["project_id"],
        ):
            pass
