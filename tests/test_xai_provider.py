import json

import httpx
import pytest

from birkin_lite.context import compact_schema
from birkin_lite.contracts import (
    MODEL_OUTPUT_TYPES,
    AppError,
    ModelBinding,
    ModelRequest,
    PlanningResultV1,
)
from birkin_lite.providers import (
    MAX_RESPONSE_BYTES,
    TransportError,
    XAIClient,
    normalize_xai_usage,
    xai_schema,
)


def inputs():
    binding = ModelBinding(
        provider="xai",
        model_id="fixture-model",
        roles=["L"],
        role="L",
        enabled=True,
        available=True,
        allowed_projects=["sales"],
        capabilities=["structured_output", "meeting_actions"],
        context_window=32768,
        max_output=1024,
        reasoning_efforts=["low"],
        reasoning_effort="low",
    )
    request = ModelRequest(
        model_id=binding.model_id,
        prompt_version="test-v1",
        context="PRIVATE_PROMPT",
        max_output=512,
        timeout_s=7,
        reasoning_effort="low",
        output_schema=compact_schema(MODEL_OUTPUT_TYPES["meeting_actions"].model_json_schema()),
    )
    return request, binding


def response_body(**changes):
    # Official Chat Completions example: 32 input + 9 visible + 94 reasoning = 135.
    return {
        "id": "response-1",
        "object": "chat.completion",
        "model": "fixture-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '{"actions":[]}', "refusal": None},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 32,
            "completion_tokens": 9,
            "total_tokens": 135,
            "prompt_tokens_details": {"cached_tokens": 6},
            "completion_tokens_details": {"reasoning_tokens": 94},
        },
        **changes,
    }


def test_one_reused_client_sends_exact_schema_auth_timeout_and_no_tools():
    seen = []

    def serve(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        return httpx.Response(200, json=response_body())

    request, binding = inputs()
    with XAIClient("fixture-secret", http_transport=httpx.MockTransport(serve)) as client:
        assert client.list_models() == ["fixture-model"]
        result = client.transport(request, binding)
        assert not client._client.is_closed
    assert client._client.is_closed and len(seen) == 2
    sent = json.loads(seen[1].content)
    assert str(seen[1].url) == "https://api.x.ai/v1/chat/completions"
    assert seen[1].headers["authorization"] == "Bearer fixture-secret"
    assert sent["response_format"]["json_schema"] == {
        "name": "office_result",
        "strict": True,
        "schema": request.output_schema,
    }
    assert sent["stream"] is False and sent["n"] == 1
    assert "tools" not in sent and "tool_choice" not in sent
    assert sent["max_completion_tokens"] == 512 and sent["reasoning_effort"] == "low"
    assert set(seen[1].extensions["timeout"].values()) == {7}
    assert result.parsed_output == {"actions": []}
    assert result.normalized_usage.input_tokens_total == 32
    assert result.normalized_usage.cached_input_tokens == 6
    assert result.normalized_usage.output_tokens_total == 103
    assert result.normalized_usage.reasoning_tokens == 94
    assert result.normalized_usage.cache_write_tokens is None


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "PROVIDER_AUTH_ERROR", False),
        (429, "PROVIDER_RATE_LIMITED", True),
        (503, "PROVIDER_UNAVAILABLE", True),
        (302, "PROVIDER_ERROR", False),
    ],
)
def test_http_errors_preserve_usage_without_retry_or_redirect(status, code, retryable):
    seen = []

    def serve(request):
        seen.append(request)
        return httpx.Response(
            status,
            headers={"Retry-After": "1", "Location": "https://invalid.example"},
            json=response_body(error="PRIVATE_ERROR_SECRET"),
        )

    with (
        XAIClient("fixture-secret", http_transport=httpx.MockTransport(serve)) as client,
        pytest.raises(TransportError) as error,
    ):
        client.transport(*inputs())
    assert len(seen) == 1 and error.value.code == code
    assert error.value.retryable is retryable
    assert error.value.usage.output_tokens_total == 103
    assert error.value.request_id == "response-1"
    assert "PRIVATE_" not in str(error.value)
    assert not hasattr(error.value, "response")


@pytest.mark.parametrize(
    "exception,code,retryable",
    [
        (httpx.ReadTimeout("PRIVATE_TIMEOUT_SECRET"), "PROVIDER_TIMEOUT", False),
        (httpx.ConnectError("PRIVATE_CONNECT_SECRET"), "PROVIDER_UNAVAILABLE", True),
    ],
)
def test_network_failures_are_safe_and_left_for_metered_retry(exception, code, retryable):
    seen = []

    def serve(request):
        seen.append(request)
        raise exception

    with (
        XAIClient("fixture-secret", http_transport=httpx.MockTransport(serve)) as client,
        pytest.raises(TransportError) as error,
    ):
        client.transport(*inputs())
    assert len(seen) == 1 and error.value.code == code
    assert error.value.retryable is retryable and error.value.usage is None
    assert "PRIVATE_" not in str(error.value)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"content": "{PRIVATE_MALFORMED"}, "INVALID_OUTPUT"),
        ({"content": '["PRIVATE_WRONG_TYPE"]'}, "INVALID_OUTPUT"),
        ({"content": '{"unexpected":"PRIVATE_WRONG_SCHEMA"}'}, "INVALID_OUTPUT"),
        ({"refusal": "PRIVATE_REFUSAL"}, "PROVIDER_REFUSAL"),
        ({"finish_reason": "length", "content": "{"}, "OUTPUT_TRUNCATED"),
        ({"tool_calls": [{"function": {"name": "forbidden"}}]}, "INVALID_OUTPUT"),
    ],
)
def test_content_errors_keep_already_observed_usage(change, code):
    body = response_body()
    change = dict(change)
    body["choices"][0]["finish_reason"] = change.pop("finish_reason", "stop")
    body["choices"][0]["message"].update(change)
    body["usage"]["private_debug"] = "PRIVATE_USAGE_SECRET"
    with (
        XAIClient(
            "fixture-secret",
            http_transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
        ) as client,
        pytest.raises(TransportError) as error,
    ):
        client.transport(*inputs())
    assert error.value.code == code
    assert (
        error.value.usage.input_tokens_total == 32 and error.value.usage.output_tokens_total == 103
    )
    assert error.value.request_id == "response-1"
    assert "PRIVATE_" not in str(error.value.raw_usage)


@pytest.mark.parametrize(
    "body,code",
    [
        (b"not JSON", "INVALID_OUTPUT"),
        (b"x" * (MAX_RESPONSE_BYTES + 1), "PROVIDER_RESPONSE_TOO_LARGE"),
    ],
    ids=["invalid-json", "oversized"],
)
def test_invalid_or_oversized_outer_body_has_unknown_usage(body, code):
    with (
        XAIClient(
            "fixture-secret",
            http_transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)),
        ) as client,
        pytest.raises(TransportError) as error,
    ):
        client.transport(*inputs())
    assert error.value.code == code
    assert error.value.usage is None or error.value.usage.output_tokens_total is None


def test_missing_and_inconsistent_usage_never_become_zero_or_double_counted():
    assert normalize_xai_usage(None).usage_basis == "unknown"
    raw = response_body()["usage"]
    raw["completion_tokens"] = 103  # Already includes reasoning: incompatible endpoint shape.
    value = normalize_xai_usage(raw)
    assert value.input_tokens_total == 32 and value.output_tokens_total is None
    assert value.reasoning_tokens is None
    raw = response_body()["usage"]
    raw.pop("completion_tokens_details")
    assert normalize_xai_usage(raw).output_tokens_total is None
    raw = response_body()["usage"]
    raw["prompt_tokens"] = True
    assert normalize_xai_usage(raw).input_tokens_total is None
    with XAIClient(
        "fixture-secret",
        http_transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response_body(usage=None))
        ),
    ) as client:
        value = client.transport(*inputs())
    assert value.normalized_usage.usage_basis == "unknown"
    assert value.parsed_output == {"actions": []}


def test_schema_preserves_optional_refs_and_rejects_unsupported_semantics():
    schema = {
        "type": "object",
        "properties": {"maybe": {"$ref": "#/$defs/Value"}},
        "$defs": {"Value": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "additionalProperties": False,
    }
    assert xai_schema(schema) == schema
    plan = compact_schema(PlanningResultV1.model_json_schema())
    converted = xai_schema(plan)
    assert "oneOf" in converted["$defs"]["TodoV1"]["properties"]["executor"]
    assert "discriminator" not in converted["$defs"]["TodoV1"]["properties"]["executor"]
    for task in ("summarize", "draft_reply", "search_answer"):
        reference_schema = compact_schema(MODEL_OUTPUT_TYPES[task].model_json_schema())
        assert xai_schema(reference_schema) == reference_schema
    for invalid in [
        {"$defs": {"Cycle": {"$ref": "#/$defs/Cycle"}}, "$ref": "#/$defs/Cycle"},
        {"oneOf": [{"type": "string"}, {"type": "string"}]},
        {"type": "object", "properties": {"bad": True}},
        {"type": "array", "items": [{"type": "string"}]},
    ]:
        with pytest.raises(AppError) as error:
            xai_schema(invalid)
        assert error.value.code == "UNSUPPORTED_CAPABILITY"


def test_unsupported_binding_schema_and_effort_fail_before_http():
    seen = []
    with XAIClient(
        "fixture-secret", http_transport=httpx.MockTransport(lambda request: seen.append(request))
    ) as client:
        request, binding = inputs()
        for candidate in [
            request.model_copy(update={"model_id": "other"}),
            request.model_copy(update={"output_schema": {"type": "object"}}),
            request.model_copy(update={"reasoning_effort": "high"}),
        ]:
            with pytest.raises(AppError):
                client.transport(candidate, binding)
    assert not seen
