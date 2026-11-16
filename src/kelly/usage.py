"""Durable call accounting. Token measurement never chooses a model or lowers its role."""

import re
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .context import CONTEXT_TARGET, MAX_REQUEST_BYTES, estimate_input_tokens
from .contracts import (
    SAFE_MESSAGES,
    AppError,
    EvaluationRoute,
    ModelBinding,
    ModelRequest,
    PriceSnapshot,
    TodoV1,
    Usage,
    canonical_json,
    digest,
)
from .db import get_run, get_value, row_dict, set_value, transaction

TOKEN_FIELDS = (
    "input_tokens_total",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens_total",
    "reasoning_tokens",
)
STOPPED = {
    "cancel_requested",
    "cancelled",
    "succeeded",
    "failed",
    "rejected",
    "expired",
    "outcome_unknown",
}
ROUTE_FIELDS = {
    "role",
    "kind",
    "reason",
    "quality_evidence_id",
    "reasoning_effort",
    "plan_revision",
    "repair_errors",
}
RAW_FIELDS = {
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "audio_tokens",
    "text_tokens",
    "image_tokens",
    "cached_audio_tokens",
    "accepted_prediction_tokens",
    "rejected_prediction_tokens",
    "prompt_tokens_details",
    "completion_tokens_details",
    "input_tokens_details",
    "output_tokens_details",
    "server_tool_use",
    "web_search_requests",
    "web_fetch_requests",
    "cache_creation",
    "ephemeral_5m_input_tokens",
    "ephemeral_1h_input_tokens",
}
FINISH_REASONS = {
    "stop",
    "length",
    "tool_calls",
    "content_filter",
    "refusal",
    "error",
    "cancelled",
    "timeout",
    "unknown",
    "invalid_output",
    "completed",
    "max_output_tokens",
    "max_tokens",
    "output_truncated",
    "rate_limited",
    "provider_timeout",
    "provider_error",
    "authentication_failed",
} | {code.lower() for code in SAFE_MESSAGES}


def _now(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise AppError("INVALID_INPUT")
    return value.astimezone(UTC)


def _bounds(now, timezone_name):
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise AppError("INVALID_INPUT") from None
    local = now.astimezone(zone)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
    month = day.replace(day=1)
    next_month = month.replace(year=month.year + (month.month == 12), month=month.month % 12 + 1)
    return [
        value.astimezone(UTC).isoformat()
        for value in (day, day + timedelta(days=1), month, next_month)
    ]


def _call(conn, call_id, actor):
    record = row_dict(conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone())
    if record is None:
        raise AppError("NOT_FOUND", status=404)
    get_run(conn, record["run_id"], actor)
    return record


def _route(value, binding, run):
    value = value or {}
    if not isinstance(value, dict) or set(value) - ROUTE_FIELDS:
        raise AppError("INVALID_INPUT")
    for key, item in value.items():
        if key == "repair_errors":
            if (
                not isinstance(item, list)
                or len(item) > 8
                or any(
                    not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code)
                    for code in item
                )
            ):
                raise AppError("INVALID_INPUT")
        elif key == "plan_revision":
            if type(item) is not int or item < 1:
                raise AppError("INVALID_INPUT")
        elif item is not None and (
            not isinstance(item, str) or len(item) > (120 if key == "reason" else 96)
        ):
            raise AppError("INVALID_INPUT")
    return {
        **value,
        "role": binding.role,
        "quality_evidence_id": binding.quality_evidence_id,
        "reasoning_effort": binding.reasoning_effort,
        "run_revision": run["revision"],
    }


def begin_call(
    conn,
    run_id,
    todo_id,
    binding,
    request,
    *,
    actor,
    purpose,
    now,
    price_snapshot=None,
    policy_version="1",
    timezone_name="Asia/Seoul",
    retry_of=None,
    route=None,
    expected_revision: int | None = None,
    review_of: str | None = None,
    evaluation: EvaluationRoute | None = None,
):
    now = _now(now)
    binding = ModelBinding.model_validate(binding.model_dump())
    if evaluation is not None and (
        not isinstance(evaluation, EvaluationRoute)
        or todo_id is None
        or purpose not in {"generate", "repair"}
        or review_of is not None
    ):
        raise AppError("INVALID_INPUT")
    request = ModelRequest.model_validate(request.model_dump())
    price = PriceSnapshot.model_validate(
        price_snapshot.model_dump()
        if isinstance(price_snapshot, PriceSnapshot)
        else price_snapshot or {}
    )
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", policy_version)
        or purpose not in {"plan", "replan", "generate", "repair", "review"}
        or ((todo_id is None) != (purpose in {"plan", "replan"}))
    ):
        raise AppError("INVALID_INPUT")
    if expected_revision is not None and (
        type(expected_revision) is not int or expected_revision < 1
    ):
        raise AppError("INVALID_INPUT")
    if request.model_id != binding.model_id or request.max_output > binding.max_output:
        raise AppError("PLAN_INVALID")
    if review_of is not None:
        from .context import compact_schema
        from .contracts import ValidationResultV1

        if (
            purpose != "review"
            or todo_id is None
            or binding.role != "H"
            or not isinstance(review_of, str)
            or not re.fullmatch(r"[a-f0-9]{64}", review_of)
            or request.output_schema != compact_schema(ValidationResultV1.model_json_schema())
            or not {"structured_output", "semantic_review"} <= set(binding.capabilities)
        ):
            raise AppError("PLAN_INVALID")
    if not binding.enabled or not binding.available or binding.role is None:
        raise AppError("NO_CAPABLE_MODEL")
    input_estimate = estimate_input_tokens(request)
    estimated = input_estimate + request.max_output
    if (
        len(canonical_json(request).encode("utf-8")) > MAX_REQUEST_BYTES
        or input_estimate > CONTEXT_TARGET
        or estimated > binding.context_window
    ):
        raise AppError("CONTEXT_LIMIT")
    bounds = _bounds(now, timezone_name)
    request_hash = digest(request)
    with transaction(conn):
        run = get_run(conn, run_id, actor)
        if run["state"] in STOPPED or run["state"] in {"ready", "awaiting_approval"}:
            raise AppError(
                "CANCELLED" if run["state"] in {"cancel_requested", "cancelled"} else "CONFLICT",
                status=409,
            )
        if expected_revision is not None and run["revision"] != expected_revision:
            raise AppError("CONFLICT", status=409)
        if run["project_id"] not in binding.allowed_projects:
            raise AppError("POLICY_DENIED")
        step_id = None
        if todo_id is not None:
            step = row_dict(
                conn.execute(
                    "SELECT * FROM steps WHERE run_id=? AND json_extract(spec_json,'$.todo.id')=?",
                    (run_id, todo_id),
                ).fetchone()
            )
            if (
                step is None
                or step["kind"] != "model"
                or step["state"] not in {"pending", "running", "checking"}
            ):
                raise AppError("PLAN_INVALID")
            executor = step["spec_json"]["todo"]["executor"]
            if evaluation is not None:
                evaluation.check(
                    TodoV1.model_validate(step["spec_json"]["todo"]),
                    binding,
                    run,
                    actor,
                    snapshot=get_value(conn, "evaluation:" + run_id),
                )
            if review_of is None and (
                evaluation is None
                and executor["role"] != binding.role
                or not {
                    "structured_output",
                    executor["task"],
                }
                <= set(binding.capabilities)
            ):
                raise AppError("NO_CAPABLE_MODEL")
            step_id = step["id"]
        elif binding.role not in {"M", "H"} or not {"structured_output", "planning"} <= set(
            binding.capabilities
        ):
            raise AppError("NO_CAPABLE_MODEL")
        rows = [
            row_dict(row) for row in conn.execute("SELECT * FROM calls WHERE run_id=?", (run_id,))
        ]
        transport_no = max((row["transport_attempt_no"] for row in rows), default=0) + 1
        if transport_no > 16:
            raise AppError("CALL_LIMIT")
        routing = _route(route, binding, run)
        if (
            evaluation is None
            and purpose in {"generate", "repair"}
            and get_value(conn, "evaluation:" + run_id) is not None
        ):
            raise AppError("POLICY_DENIED")
        if evaluation is not None:
            same = [r for r in rows if r["step_id"] == step_id]
            if retry_of is None:
                if evaluation.attempt != len({r["logical_call_no"] for r in same}) + 1:
                    raise AppError("CALL_LIMIT")
                if same and (
                    any(r["route_json"].get("evaluation_arm") != evaluation.arm for r in same)
                    or (step["validation_json"] or {}).get("status") != "fail"
                    or not (step["validation_json"] or {}).get("errors")
                ):
                    raise AppError("PLAN_INVALID")
            else:
                prior_route = _call(conn, retry_of, actor)["route_json"]
                if prior_route.get("evaluation_digest") != digest(evaluation):
                    raise AppError("CONFLICT", status=409)
            routing.update(
                evaluation_arm=evaluation.arm,
                evaluation_attempt=evaluation.attempt,
                evaluation_digest=digest(evaluation),
                classified_role=executor["role"],
                todo_digest=evaluation.todo_digest,
            )
        if review_of is not None:
            if not any(
                r["step_id"] == step_id
                and r["finish_reason"] in {"stop", "completed"}
                and r["purpose"] in {"generate", "repair"}
                for r in rows
            ):
                raise AppError("PLAN_INVALID")
            routing["review_output_hash"] = review_of
        snapshot = {
            **price.model_dump(),
            "metering_mode": binding.metering_mode,
            "stale": price.checked_at is None
            or now - datetime.fromisoformat(price.checked_at) > timedelta(days=30),
        }
        if retry_of is not None:
            prior = _call(conn, retry_of, actor)
            if (
                any(
                    prior[key] != value
                    for key, value in {
                        "run_id": run_id,
                        "step_id": step_id,
                        "provider": binding.provider,
                        "model_id": binding.model_id,
                        "purpose": purpose,
                        "request_hash": request_hash,
                        "policy_version": policy_version,
                    }.items()
                )
                or prior["route_json"].get("run_revision") != run["revision"]
                or prior["route_json"].get("review_output_hash") != review_of
            ):
                raise AppError("CONFLICT", status=409)
            logical_no = prior["logical_call_no"]
            attempts = [r for r in rows if r["logical_call_no"] == logical_no]
            if any(r["state"] in {"reserved", "in_flight"} for r in attempts):
                raise AppError("CONFLICT", status=409)
            if len(attempts) >= 2:
                raise AppError("CALL_LIMIT")
            snapshot = prior["price_snapshot_json"]
        else:
            logical_no = max((row["logical_call_no"] for row in rows), default=0) + 1
            same_step = {r["logical_call_no"] for r in rows if r["step_id"] == step_id}
            if logical_no > 12 or len(same_step) >= 2:
                raise AppError("CALL_LIMIT")
            if todo_id is None and (
                (purpose == "plan" and same_step and not routing.get("repair_errors"))
                or (purpose == "replan" and not same_step and not run["plan_json"])
            ):
                raise AppError("PLAN_INVALID")
            if any(
                r["state"] in {"reserved", "in_flight"} for r in rows if r["step_id"] == step_id
            ):
                raise AppError("CONFLICT", status=409)
        record = {
            "id": uuid.uuid4().hex,
            "run_id": run_id,
            "step_id": step_id,
            "logical_call_no": logical_no,
            "transport_attempt_no": transport_no,
            "provider": binding.provider,
            "model_id": binding.model_id,
            "purpose": purpose,
            "request_hash": request_hash,
            "prompt_version": request.prompt_version,
            "policy_version": policy_version,
            "route_json": canonical_json(routing),
            "price_snapshot_json": canonical_json(snapshot),
            "state": "reserved",
            "reserved_micro_usd": None,
            "reserved_tokens": estimated,
            "usage_basis": "estimated",
            "timezone_name": timezone_name,
            "day_start_utc": bounds[0],
            "day_end_utc": bounds[1],
            "month_start_utc": bounds[2],
            "month_end_utc": bounds[3],
            "created_at": now.isoformat(),
        }
        conn.execute(
            "INSERT INTO calls ("
            + ",".join(record)
            + ") VALUES ("
            + ",".join("?" for _ in record)
            + ")",
            tuple(record.values()),
        )
        return _call(conn, record["id"], actor)


def start_call(conn, call_id, actor):
    denied = None
    with transaction(conn):
        record = _call(conn, call_id, actor)
        run = get_run(conn, record["run_id"], actor)
        if record["state"] != "reserved":
            raise AppError("CONFLICT", status=409)
        if run["state"] in STOPPED or run["revision"] != record["route_json"]["run_revision"]:
            denied = (
                "CANCELLED" if run["state"] in {"cancel_requested", "cancelled"} else "CONFLICT"
            )
            conn.execute(
                "UPDATE calls SET state='released',finish_reason='cancelled' WHERE id=?", (call_id,)
            )
        else:
            conn.execute(
                "UPDATE calls SET state='in_flight' WHERE id=? AND state='reserved'", (call_id,)
            )
        result = _call(conn, call_id, actor)
    if denied:
        raise AppError(denied, status=409)
    return result


def _raw_metrics(value, depth=0):
    """Keep recognized numeric usage structure only; discard unknown fields and all free text."""
    if value is None:
        return None
    if not isinstance(value, dict) or depth > 4:
        raise AppError("INVALID_INPUT")
    result = {}
    for key, item in value.items():
        if key not in RAW_FIELDS:
            continue
        if type(item) is int and item >= 0 or item is None:
            result[key] = item
        elif isinstance(item, dict):
            result[key] = _raw_metrics(item, depth + 1)
    if len(canonical_json(result).encode("utf-8")) > 65536:
        raise AppError("INVALID_INPUT")
    return result


def _cost(usage, snapshot):
    if snapshot["metering_mode"] != "api" or snapshot["cache_write_billing"] == "unknown":
        return None
    if (
        usage.input_tokens_total is None
        or usage.cached_input_tokens is None
        or usage.output_tokens_total is None
    ):
        return None
    components = [
        (usage.input_tokens_total - usage.cached_input_tokens, "input"),
        (usage.cached_input_tokens, "cached_input"),
        (usage.output_tokens_total, "output"),
    ]
    if snapshot["cache_write_billing"] == "separate":
        components.append((usage.cache_write_tokens, "cache_write"))
    total = 0
    for quantity, name in components:
        rate = snapshot["rates"][name]
        if quantity is None or (quantity and rate is None):
            return None
        total += 0 if not quantity else (quantity * rate + 999999) // 1000000
    return total


def settle_call(
    conn,
    call_id,
    normalized_usage,
    raw_usage,
    *,
    actor,
    now,
    request_id=None,
    finish_reason=None,
    latency_ms=None,
):
    now = _now(now)
    usage = Usage.model_validate(
        normalized_usage.model_dump() if isinstance(normalized_usage, Usage) else normalized_usage
    )
    raw = _raw_metrics(raw_usage)
    if latency_ms is not None and (type(latency_ms) is not int or latency_ms < 0):
        raise AppError("INVALID_INPUT")
    request_id = (
        request_id
        if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", request_id)
        else None
    )
    finish_reason = finish_reason if finish_reason in FINISH_REASONS else "unknown"
    fields = {
        **usage.model_dump(),
        "raw_usage_json": canonical_json(raw) if raw is not None else None,
        "provider_request_id": request_id,
        "finish_reason": finish_reason,
        "latency_ms": latency_ms,
    }
    conflict = False
    with transaction(conn):
        record = _call(conn, call_id, actor)
        if record["state"] not in {"in_flight", "unknown", "settled"}:
            raise AppError("CONFLICT", status=409)
        if record["state"] == "unknown":
            for key in TOKEN_FIELDS:
                if fields[key] is None:
                    fields[key] = record[key]
                elif (
                    record[key] is not None
                    and fields[key] != record[key]
                    and record["usage_basis"] == "provider"
                ):
                    conflict = True
            if fields["usage_basis"] == "unknown" and record["usage_basis"] == "provider":
                fields["usage_basis"] = "provider"
            for key in ("provider_request_id", "latency_ms"):
                if fields[key] is None:
                    fields[key] = record[key]
            if fields["finish_reason"] == "unknown":
                fields["finish_reason"] = record["finish_reason"] or "unknown"
            if fields["raw_usage_json"] is None and record["raw_usage_json"] is not None:
                fields["raw_usage_json"] = canonical_json(record["raw_usage_json"])
        if record["state"] == "settled":
            expected = {key: record[key] for key in fields}
            expected["raw_usage_json"] = (
                canonical_json(expected["raw_usage_json"])
                if expected["raw_usage_json"] is not None
                else None
            )
            if expected != fields:
                conflict = True
        if conflict:
            key = f"usage_conflict:{call_id}"
            previous = get_value(conn, key, {"count": 0})
            set_value(
                conn,
                key,
                {
                    "count": previous["count"] + 1,
                    "last_at": now.isoformat(),
                    "incoming_hash": digest(fields),
                },
            )
        elif record["state"] != "settled":
            usage = Usage(**{key: fields[key] for key in (*TOKEN_FIELDS, "usage_basis")})
            complete = (
                usage.input_tokens_total is not None and usage.output_tokens_total is not None
            )
            update = {
                **fields,
                "state": "settled" if complete else "unknown",
                "actual_micro_usd": _cost(usage, record["price_snapshot_json"]),
                "settled_at": now.isoformat(),
            }
            conn.execute(
                "UPDATE calls SET " + ",".join(f"{key}=?" for key in update) + " WHERE id=?",
                (*update.values(), call_id),
            )
        result = _call(conn, call_id, actor)
    if conflict:
        raise AppError("USAGE_CONFLICT", status=409)
    return result


def mark_usage_unknown(conn, call_id, reason, *, actor, now):
    now = _now(now)
    if not isinstance(reason, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason):
        raise AppError("INVALID_INPUT")
    with transaction(conn):
        record = _call(conn, call_id, actor)
        if record["state"] in {"settled", "released"}:
            return record
        route = {**record["route_json"], "usage_unknown_reason": reason}
        basis = (
            record["usage_basis"]
            if any(record[key] is not None for key in TOKEN_FIELDS)
            else "unknown"
        )
        conn.execute(
            "UPDATE calls SET state='unknown',usage_basis=?,route_json=?,settled_at=? WHERE id=?",
            (basis, canonical_json(route), now.isoformat(), call_id),
        )
        return _call(conn, call_id, actor)


def release_call(conn, call_id, *, actor, now, reason="CANCELLED"):
    _now(now)
    if not isinstance(reason, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason):
        raise AppError("INVALID_INPUT")
    with transaction(conn):
        record = _call(conn, call_id, actor)
        if record["state"] == "released":
            return record
        if record["state"] != "reserved":
            raise AppError("CONFLICT", status=409)
        route = {**record["route_json"], "release_reason": reason}
        conn.execute(
            "UPDATE calls SET state='released',route_json=? WHERE id=?",
            (canonical_json(route), call_id),
        )
        return _call(conn, call_id, actor)


def _summarize(rows):
    sent = [row for row in rows if row["state"] != "released"]
    totals = {
        key: sum(row[key] for row in sent) if all(row[key] is not None for row in sent) else None
        for key in TOKEN_FIELDS
    }
    actual = (
        sum(row["actual_micro_usd"] for row in sent)
        if all(row["actual_micro_usd"] is not None for row in sent)
        else None
    )
    state = (
        "not_used"
        if not sent
        else "unknown"
        if any(row["state"] != "settled" or row["usage_basis"] == "unknown" for row in sent)
        else ("estimated" if any(row["usage_basis"] == "estimated" for row in sent) else "provider")
    )
    return {
        **totals,
        "total_tokens": totals["input_tokens_total"] + totals["output_tokens_total"]
        if totals["input_tokens_total"] is not None and totals["output_tokens_total"] is not None
        else None,
        "usage_state": state,
        "actual_micro_usd": actual,
        "known_tokens": {
            key: sum(row[key] for row in sent if row[key] is not None) for key in TOKEN_FIELDS
        },
        "known_actual_micro_usd": sum(
            row["actual_micro_usd"] for row in sent if row["actual_micro_usd"] is not None
        ),
        "price_stale": any(row["price_snapshot_json"].get("stale", True) for row in sent),
        "unknown_calls": sum(row["state"] == "unknown" for row in rows),
        "in_flight_calls": sum(row["state"] in {"reserved", "in_flight"} for row in rows),
        "released_calls": sum(row["state"] == "released" for row in rows),
        "unpriced_calls": sum(row["actual_micro_usd"] is None for row in sent),
        "logical_calls": len({(row["run_id"], row["logical_call_no"]) for row in rows}),
        "transport_attempts": len(rows),
    }


def summarize_usage(conn, run_id, actor):
    get_run(conn, run_id, actor)
    rows = [
        row_dict(row)
        for row in conn.execute(
            "SELECT * FROM calls WHERE run_id=? ORDER BY transport_attempt_no", (run_id,)
        )
    ]
    by_step = {
        step or "planning": _summarize([row for row in rows if row["step_id"] == step])
        for step in dict.fromkeys(row["step_id"] for row in rows)
    }
    return {**_summarize(rows), "by_step": by_step}
