"""One metered callable boundary. Provider HTTP adapters are added independently."""

import asyncio
import json
import math
import re
from datetime import UTC, datetime
from itertools import combinations
from threading import Event
from time import monotonic

import httpx
from pydantic import ValidationError

from . import usage
from .context import compact_schema
from .contracts import (
    MODEL_OUTPUT_TYPES,
    AppError,
    ModelRequest,
    ModelResult,
    PlanningResultV1,
    Usage,
    canonical_json,
    digest,
)
from .db import get_run

MAX_HTTP_ATTEMPTS = 2
MAX_RETRY_WAIT_S = 2
MAX_RESPONSE_BYTES = 1024 * 1024


class TransportError(AppError):
    """Transport status and usage only; never carry a request, response body or secret."""

    def __init__(
        self,
        code,
        *,
        retryable=False,
        status=None,
        retry_after_s=None,
        usage=None,
        raw_usage=None,
        request_id=None,
        finish_reason=None,
        latency_ms=None,
    ):
        super().__init__(code, status=502)
        self.retryable = retryable
        self.http_status = status
        self.retry_after_s = retry_after_s
        self.usage = usage
        self.raw_usage = raw_usage
        self.request_id = request_id
        self.finish_reason = finish_reason
        self.latency_ms = latency_ms


def normalize_xai_usage(raw):
    """Normalize xAI Chat Completions only; completion excludes separately reported reasoning."""
    if not isinstance(raw, dict):
        return Usage()

    def count(value):
        return value if type(value) is int and 0 <= value <= 2**63 - 1 else None

    prompt = count(raw.get("prompt_tokens"))
    visible = count(raw.get("completion_tokens"))
    details = raw.get("completion_tokens_details")
    reasoning = count(details.get("reasoning_tokens")) if isinstance(details, dict) else None
    details = raw.get("prompt_tokens_details")
    cached = count(details.get("cached_tokens")) if isinstance(details, dict) else None
    if prompt is None or cached is not None and cached > prompt:
        cached = None
    output = count(visible + reasoning) if visible is not None and reasoning is not None else None
    total = count(raw.get("total_tokens"))
    if "total_tokens" in raw and (
        total is None or prompt is not None and output is not None and total != prompt + output
    ):
        output = None
    if output is None:
        reasoning = None
    return Usage(
        input_tokens_total=prompt,
        cached_input_tokens=cached,
        output_tokens_total=output,
        reasoning_tokens=reasoning,
        usage_basis="provider" if prompt is not None or output is not None else "unknown",
    )


def xai_schema(schema):
    """Preserve constraints; reject circular refs and ambiguous oneOf semantics before HTTP."""
    schema = json.loads(canonical_json(schema))

    def resolve(node, refs):
        while "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/") or ref in refs:
                raise AppError("UNSUPPORTED_CAPABILITY")
            name = ref[len("#/$defs/") :].replace("~1", "/").replace("~0", "~")
            node = schema.get("$defs", {}).get(name)
            if not isinstance(node, dict):
                raise AppError("UNSUPPORTED_CAPABILITY")
            refs = (*refs, ref)
        return node, refs

    def visit(node, refs=()):
        if not isinstance(node, dict):
            raise AppError("UNSUPPORTED_CAPABILITY")
        resolved, chain = resolve(node, refs)
        if resolved is not node:
            visit(resolved, chain)
        if any(key in node for key in ("minContains", "maxContains")):
            raise AppError("UNSUPPORTED_CAPABILITY")
        for key in ("enum", "anyOf", "oneOf"):
            if key in node and not node[key]:
                raise AppError("UNSUPPORTED_CAPABILITY")
        if "oneOf" in node:
            variants = [resolve(value, refs)[0] for value in node["oneOf"]]
            if any(value.get("type") != "object" for value in variants):
                raise AppError("UNSUPPORTED_CAPABILITY")
            for left, right in combinations(variants, 2):
                exclusive = any(
                    one.get("additionalProperties") is False
                    and set(other.get("required", [])) - set(one.get("properties", {}))
                    for one, other in ((left, right), (right, left))
                )
                if not exclusive:
                    fields = set(left.get("required", [])) | set(right.get("required", []))
                    exclusive = any(
                        all(
                            "const" in value.get("properties", {}).get(field, {})
                            for value in (left, right)
                        )
                        and left["properties"][field]["const"]
                        != right["properties"][field]["const"]
                        for field in fields
                    )
                if not exclusive:
                    raise AppError("UNSUPPORTED_CAPABILITY")
        node.pop("discriminator", None)  # Annotation only; disjoint const constraints remain.
        for key, value in node.items():
            if key in {"properties", "$defs"}:
                for child in value.values():
                    visit(child, refs)
            elif key in {"anyOf", "oneOf", "allOf", "prefixItems"}:
                for child in value:
                    visit(child, refs)
            elif (
                key in {"items", "not", "if", "then", "else"}
                or key == "additionalProperties"
                and isinstance(value, dict)
            ):
                visit(value, refs)

    visit(schema)
    return schema


class XAIClient:
    """One reusable HTTPS client. Every transport invocation performs at most one HTTP attempt."""

    def __init__(self, api_key, *, http_transport=None):
        if not isinstance(api_key, str) or not re.fullmatch(r"[!-~]{1,4096}", api_key):
            raise AppError("PROVIDER_AUTH_ERROR")
        self._client = httpx.Client(
            base_url="https://api.x.ai/v1/",
            headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
            verify=True,
            follow_redirects=False,
            trust_env=False,
            transport=http_transport
            if http_transport is not None
            else httpx.HTTPTransport(verify=True, retries=0, trust_env=False),
        )

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _request(self, method, path, *, timeout_s, payload=None):
        started = monotonic()
        try:
            with self._client.stream(method, path, json=payload, timeout=timeout_s) as response:
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise TransportError("PROVIDER_RESPONSE_TOO_LARGE")
                    content.extend(chunk)
                try:
                    body = json.loads(content)
                except (ValueError, UnicodeError):
                    body = None
                body = body if isinstance(body, dict) else {}
                observed = normalize_xai_usage(body.get("usage"))
                try:
                    raw = usage._raw_metrics(body.get("usage"))
                except AppError:
                    raw = None
                request_id = body.get("id", response.headers.get("x-request-id"))
                request_id = (
                    request_id
                    if isinstance(request_id, str)
                    and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", request_id)
                    else None
                )
                metadata = {
                    "usage": observed,
                    "raw_usage": raw,
                    "request_id": request_id,
                    "latency_ms": max(0, round((monotonic() - started) * 1000)),
                }
                if not 200 <= response.status_code < 300:
                    failure = _failure(
                        httpx.HTTPStatusError("", request=response.request, response=response)
                    )
                    raise TransportError(
                        failure.code,
                        retryable=failure.retryable,
                        status=response.status_code,
                        retry_after_s=failure.retry_after_s,
                        **metadata,
                    )
                if not body:
                    raise TransportError("INVALID_OUTPUT", **metadata)
                return body, metadata
        except httpx.HTTPError as exc:
            raise _failure(exc) from None

    def list_models(self, timeout_s=10):
        if type(timeout_s) is not int or not 1 <= timeout_s <= 60:
            raise AppError("INVALID_INPUT")
        body, metadata = self._request("GET", "models", timeout_s=timeout_s)
        models = body.get("data")
        if (
            not isinstance(models, list)
            or len(models) > 1000
            or any(
                not isinstance(value, dict)
                or not isinstance(value.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", value["id"])
                for value in models
            )
        ):
            raise TransportError("INVALID_OUTPUT", **metadata)
        return sorted({value["id"] for value in models})

    def transport(self, request, binding):
        request = ModelRequest.model_validate(request.model_dump())
        if binding.provider != "xai" or binding.model_id != request.model_id:
            raise AppError("NO_CAPABLE_MODEL")
        output_type = next(
            (
                value
                for value in (PlanningResultV1, *MODEL_OUTPUT_TYPES.values())
                if compact_schema(value.model_json_schema())
                == compact_schema(request.output_schema)
            ),
            None,
        )
        if output_type is None:
            raise AppError("UNSUPPORTED_CAPABILITY")
        payload = {
            "model": request.model_id,
            "messages": [{"role": "user", "content": request.context}],
            "stream": False,
            "n": 1,
            "max_completion_tokens": request.max_output,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "office_result",
                    "strict": True,
                    "schema": xai_schema(request.output_schema),
                },
            },
        }
        if request.reasoning_effort is not None:
            if (
                request.reasoning_effort not in {"none", "low", "medium", "high", "xhigh"}
                or request.reasoning_effort not in binding.reasoning_efforts
            ):
                raise AppError("UNSUPPORTED_CAPABILITY")
            payload["reasoning_effort"] = request.reasoning_effort
        body, metadata = self._request(
            "POST", "chat/completions", timeout_s=request.timeout_s, payload=payload
        )
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise TransportError("INVALID_OUTPUT", **metadata)
        choice = choices[0]
        finish = choice.get("finish_reason")
        metadata["finish_reason"] = finish if finish in usage.FINISH_REASONS else "unknown"
        message = choice.get("message")
        if finish == "length":
            raise TransportError("OUTPUT_TRUNCATED", **metadata)
        if not isinstance(message, dict):
            raise TransportError("INVALID_OUTPUT", **metadata)
        if message.get("refusal"):
            metadata["finish_reason"] = "refusal"
            raise TransportError("PROVIDER_REFUSAL", **metadata)
        if finish != "stop" or message.get("role") != "assistant" or message.get("tool_calls"):
            raise TransportError("INVALID_OUTPUT", **metadata)
        try:
            parsed = json.loads(message["content"])
            canonical_json(parsed)
            output_type.model_validate(parsed)
            return ModelResult(
                parsed_output=parsed,
                normalized_usage=metadata.pop("usage"),
                **metadata,
            )
        except (KeyError, ValueError, TypeError, ValidationError):
            metadata.setdefault("usage", normalize_xai_usage(body.get("usage")))
            raise TransportError("INVALID_OUTPUT", **metadata) from None

    def complete(self, request, binding):
        return self.transport(request, binding)


def _failure(exc):
    if isinstance(exc, TransportError):
        return exc
    if isinstance(exc, asyncio.CancelledError):
        return TransportError("CANCELLED")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        code = (
            "PROVIDER_AUTH_ERROR"
            if status in {401, 403}
            else "PROVIDER_RATE_LIMITED"
            if status == 429
            else "PROVIDER_UNAVAILABLE"
            if 500 <= status <= 599
            else "PROVIDER_ERROR"
        )
        value = exc.response.headers.get("retry-after")
        try:
            delay = float(value) if value is not None else None
        except ValueError:
            # An HTTP date needs an adapter's server-clock interpretation; never retry early.
            delay = MAX_RETRY_WAIT_S + 1
        return TransportError(
            code,
            status=status,
            retryable=status == 429 or 500 <= status <= 599,
            retry_after_s=delay,
        )
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, ConnectionError)):
        return TransportError("PROVIDER_UNAVAILABLE", retryable=True)
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return TransportError("PROVIDER_TIMEOUT")
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return TransportError("INVALID_OUTPUT")
    return TransportError("PROVIDER_ERROR")


def _output_type(request, context, purpose):
    todo = context.get("todo")
    if purpose in {"plan", "replan"}:
        model = PlanningResultV1
    elif todo is not None and todo.executor.type == "model":
        model = MODEL_OUTPUT_TYPES[todo.executor.task]
    else:
        model = next(
            (
                value
                for value in MODEL_OUTPUT_TYPES.values()
                if compact_schema(value.model_json_schema())
                == compact_schema(request.output_schema)
            ),
            None,
        )
    if model is None or compact_schema(model.model_json_schema()) != compact_schema(
        request.output_schema
    ):
        raise AppError("UNSUPPORTED_CAPABILITY")
    return model


def metered_complete(
    request,
    binding,
    context,
    *,
    transport,
    price_snapshot=None,
    cancelled=None,
    wait=None,
    evaluation=None,
) -> ModelResult:
    """At most two HTTP attempts, with each admitted attempt durably accounted for.

    The transport executes once and returns ModelResult, or raises TransportError
    carrying any usage already received. Content repair belongs to the task executor.
    """
    request = ModelRequest.model_validate(request.model_dump())
    conn, actor = context["conn"], context["actor"]
    expected_revision = context["run"]["revision"]
    run = get_run(conn, context["run"]["id"], actor)
    if run["revision"] != expected_revision:
        raise AppError(
            "CANCELLED" if run["state"] in {"cancel_requested", "cancelled"} else "CONFLICT",
            status=409,
        )
    todo = context.get("todo")
    purpose = context.get("purpose", "generate" if todo is not None else "plan")
    output_type = _output_type(request, context, purpose)
    if request.model_id != binding.model_id:
        raise AppError("NO_CAPABLE_MODEL")
    route = {
        "role": binding.role,
        "kind": todo.kind if todo is not None else "complex" if binding.role == "H" else "standard",
        "reason": todo.reason
        if todo is not None
        else "요청을 TODO로 나누면서 난도와 담당 역할을 배정한다",
        "quality_evidence_id": binding.quality_evidence_id,
        "reasoning_effort": binding.reasoning_effort,
        "repair_errors": context.get("repair_errors", []),
        "plan_revision": (run["plan_json"] or {}).get("revision", 0)
        + int(purpose in {"plan", "replan"}),
    }

    def is_cancelled():
        return bool(cancelled and cancelled()) or get_run(conn, run["id"], actor)["state"] in {
            "cancel_requested",
            "cancelled",
        }

    retry_of = None
    for attempt in range(MAX_HTTP_ATTEMPTS):
        call = usage.begin_call(
            conn,
            run["id"],
            todo.id if todo else None,
            binding,
            request,
            actor=actor,
            purpose=purpose,
            now=datetime.now(UTC),
            price_snapshot=price_snapshot,
            policy_version=digest(run["policy_json"]),
            retry_of=retry_of,
            route=route,
            expected_revision=expected_revision,
            review_of=context.get("review_of"),
            evaluation=evaluation,
        )
        if is_cancelled():
            usage.release_call(
                conn, call["id"], actor=actor, now=datetime.now(UTC), reason="CANCELLED"
            )
            raise AppError("CANCELLED")
        usage.start_call(conn, call["id"], actor)
        try:
            received = transport(request, binding)
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
            # Third-party exceptions may contain credentials; retain only typed metadata.
            error = _failure(exc)
            usage.settle_call(
                conn,
                call["id"],
                error.usage or Usage(),
                error.raw_usage,
                actor=actor,
                now=datetime.now(UTC),
                request_id=error.request_id,
                finish_reason=error.finish_reason
                or {
                    "CANCELLED": "cancelled",
                    "INVALID_OUTPUT": "invalid_output",
                    "OUTPUT_TRUNCATED": "output_truncated",
                    "PROVIDER_AUTH_ERROR": "authentication_failed",
                    "PROVIDER_RATE_LIMITED": "rate_limited",
                    "PROVIDER_TIMEOUT": "provider_timeout",
                }.get(error.code, "provider_error"),
                latency_ms=error.latency_ms,
            )
            if error.code == "CANCELLED" or is_cancelled():
                raise AppError("CANCELLED") from None
            if not error.retryable or attempt + 1 >= MAX_HTTP_ATTEMPTS:
                raise error from None
            delay = 0.25 if error.retry_after_s is None else error.retry_after_s
            if (
                type(delay) not in {int, float}
                or not math.isfinite(delay)
                or not 0 <= delay <= MAX_RETRY_WAIT_S
            ):
                raise AppError("PROVIDER_RETRY_LATER") from None
            (wait or Event().wait)(delay)
            if is_cancelled():
                raise AppError("CANCELLED") from None
            retry_of = call["id"]
            continue
        except BaseException:
            usage.mark_usage_unknown(
                conn, call["id"], "INTERRUPTED", actor=actor, now=datetime.now(UTC)
            )
            raise
        values = received.model_dump() if isinstance(received, ModelResult) else received
        metadata = {
            key: values[key]
            for key in (
                "normalized_usage",
                "raw_usage",
                "request_id",
                "finish_reason",
                "latency_ms",
            )
            if isinstance(values, dict) and key in values
        }
        try:
            observed = Usage.model_validate(metadata.get("normalized_usage", {}))
        except (ValidationError, ValueError, TypeError):
            observed = Usage()
        try:
            # Validate only metadata before accounting; malformed content cannot erase usage.
            metadata = ModelResult.model_validate({"parsed_output": {}, **metadata}).model_dump(
                exclude={"parsed_output"}
            )
        except (ValidationError, ValueError, TypeError):
            metadata = {"normalized_usage": observed}
        usage.settle_call(
            conn,
            call["id"],
            metadata["normalized_usage"],
            metadata.get("raw_usage"),
            actor=actor,
            now=datetime.now(UTC),
            request_id=metadata.get("request_id"),
            finish_reason=metadata.get("finish_reason"),
            latency_ms=metadata.get("latency_ms"),
        )
        if is_cancelled():
            raise AppError("CANCELLED")
        try:
            result = ModelResult.model_validate(values)
        except (ValidationError, ValueError, TypeError):
            raise AppError("INVALID_OUTPUT") from None
        if result.finish_reason in {"length", "max_tokens", "max_output_tokens"}:
            raise AppError("OUTPUT_TRUNCATED")
        if result.finish_reason not in {"stop", "completed"}:
            raise AppError("INVALID_OUTPUT")
        try:
            output_type.model_validate(result.parsed_output)
            canonical_json(result.parsed_output)
        except (ValidationError, ValueError, TypeError):
            raise AppError("INVALID_OUTPUT") from None
        return result
    raise AppError("CALL_LIMIT")
