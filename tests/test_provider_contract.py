import asyncio

import httpx
import pytest

from birkin_lite import usage
from birkin_lite.context import build_model_request
from birkin_lite.contracts import (
    Actor,
    AppError,
    ModelBinding,
    ModelResult,
    PlanV1,
    Requirement,
    RunRequest,
    TodoV1,
    Usage,
)
from birkin_lite.db import create_run, init_db
from birkin_lite.engine import install_plan
from birkin_lite.providers import TransportError, metered_complete


class SequenceTransport:
    """A fake provider exists only in tests, never in the shipped module."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def __call__(self, request, binding):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome(request, binding) if callable(outcome) else outcome


def prepared(tmp_path):
    conn = init_db(tmp_path / "state.db")
    actor = Actor(id="owner", project_ids=["sales"])
    run = create_run(
        conn,
        actor,
        "ui",
        "first",
        RunRequest(request="PRIVATE_PROMPT 할 일 정리", project_id="sales"),
    )
    todo = TodoV1(
        id="t1",
        title="할 일 정리",
        requirement_ids=["r1"],
        kind="simple",
        reason="명시된 할 일을 추출한다",
        signals=["extraction"],
        clarity="clear",
        usefulness="required",
        executor={"type": "model", "role": "L", "task": "meeting_actions"},
        output_schema="MeetingActionsV1",
        done_when=["근거 있는 할 일만 포함"],
    )
    run = install_plan(
        conn,
        run["id"],
        PlanV1(requirements=[Requirement(id="r1", text="할 일 정리")], todos=[todo]),
        actor,
        tools={},
    )
    context = {"conn": conn, "run": run, "actor": actor, "todo": todo, "purpose": "generate"}
    binding = ModelBinding(
        provider="test",
        model_id="test-model",
        roles=["L"],
        role="L",
        enabled=True,
        available=True,
        allowed_projects=["sales"],
        capabilities=["structured_output", "meeting_actions"],
        context_window=32768,
        max_output=1024,
        quality_evidence_id="fixture",
    )
    return conn, context, binding, build_model_request(todo, {}, context, binding=binding)


def result(**changes):
    values = {
        "parsed_output": {"actions": []},
        "normalized_usage": Usage(
            input_tokens_total=10,
            output_tokens_total=3,
            cached_input_tokens=2,
            reasoning_tokens=1,
            usage_basis="provider",
        ),
        "raw_usage": {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
        "request_id": "response-1",
        "finish_reason": "stop",
        "latency_ms": 3,
    }
    return ModelResult(**(values | changes))


def rows(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM calls ORDER BY transport_attempt_no")]


def test_one_attempt_is_metered_and_prompt_response_secrets_are_not_persisted(tmp_path):
    conn, context, binding, request = prepared(tmp_path)
    transport = SequenceTransport(result())
    returned = metered_complete(request, binding, context, transport=transport)
    recorded = rows(conn)
    assert returned.parsed_output == {"actions": []} and transport.calls == 1
    assert recorded[0]["state"] == "settled"
    assert recorded[0]["input_tokens_total"] == 10 and recorded[0]["output_tokens_total"] == 3
    assert recorded[0]["provider_request_id"] == "response-1"
    assert recorded[0]["actual_micro_usd"] is None
    assert "PRIVATE_PROMPT" not in str(recorded)
    conn.close()


def test_http_retry_has_one_logical_number_and_separate_attempt_rows(tmp_path):
    conn, context, binding, request = prepared(tmp_path)
    transport = SequenceTransport(
        TransportError("PROVIDER_RATE_LIMITED", retryable=True, status=429), result()
    )
    waits = []
    metered_complete(request, binding, context, transport=transport, wait=waits.append)
    recorded = rows(conn)
    assert transport.calls == 2 and len(waits) == 1
    assert [row["logical_call_no"] for row in recorded] == [1, 1]
    assert [row["transport_attempt_no"] for row in recorded] == [1, 2]
    assert recorded[0]["state"] == "unknown" and recorded[1]["state"] == "settled"
    conn.close()


@pytest.mark.parametrize(
    "outcome,code",
    [
        (result(parsed_output={"unexpected": "PRIVATE_OUTPUT"}), "INVALID_OUTPUT"),
        (result(finish_reason="length"), "OUTPUT_TRUNCATED"),
        (
            TransportError(
                "INVALID_OUTPUT",
                usage=Usage(input_tokens_total=10, output_tokens_total=3, usage_basis="provider"),
                raw_usage={"prompt_tokens": 10, "completion_tokens": 3},
                request_id="parse-error",
            ),
            "INVALID_OUTPUT",
        ),
    ],
)
def test_output_failures_preserve_usage_before_raising(tmp_path, outcome, code):
    conn, context, binding, request = prepared(tmp_path)
    with pytest.raises(AppError) as error:
        metered_complete(request, binding, context, transport=SequenceTransport(outcome))
    assert error.value.code == code
    assert rows(conn)[0]["state"] == "settled" and rows(conn)[0]["output_tokens_total"] == 3
    assert "PRIVATE_OUTPUT" not in str(rows(conn))
    conn.close()


def test_no_usage_and_read_timeout_remain_unknown(tmp_path):
    for index, outcome in enumerate(
        [result(normalized_usage=Usage(), raw_usage=None), httpx.ReadTimeout("PRIVATE_API_KEY")]
    ):
        conn, context, binding, request = prepared(tmp_path / str(index))
        transport = SequenceTransport(outcome)
        if isinstance(outcome, BaseException):
            with pytest.raises(AppError) as error:
                metered_complete(request, binding, context, transport=transport)
            assert error.value.code == "PROVIDER_TIMEOUT"
        else:
            metered_complete(request, binding, context, transport=transport)
        recorded = rows(conn)[0]
        assert recorded["state"] == "unknown" and recorded["input_tokens_total"] is None
        assert recorded["actual_micro_usd"] is None and transport.calls == 1
        assert "PRIVATE_API_KEY" not in str(recorded)
        conn.close()


def test_cancellation_before_and_after_transport_preserves_accounting(tmp_path):
    for after in (False, True):
        conn, context, binding, request = prepared(tmp_path / str(after))
        cancelled = [not after]

        def complete_and_cancel(*_, flag=cancelled):
            flag[0] = True
            return result()

        transport = SequenceTransport(complete_and_cancel)
        with pytest.raises(AppError) as error:
            metered_complete(
                request,
                binding,
                context,
                transport=transport,
                cancelled=lambda flag=cancelled: flag[0],
            )
        assert error.value.code == "CANCELLED"
        assert transport.calls == int(after)
        assert rows(conn)[0]["state"] == ("settled" if after else "released")
        assert rows(conn)[0]["output_tokens_total"] == (3 if after else None)
        conn.close()


def test_retry_bound_long_retry_after_and_async_cancellation(tmp_path):
    cases = [
        (
            [TransportError("PROVIDER_UNAVAILABLE", retryable=True, status=503)] * 2,
            "PROVIDER_UNAVAILABLE",
            2,
        ),
        (
            [
                TransportError(
                    "PROVIDER_RATE_LIMITED", retryable=True, status=429, retry_after_s=120
                )
            ],
            "PROVIDER_RETRY_LATER",
            1,
        ),
        ([asyncio.CancelledError()], "CANCELLED", 1),
    ]
    for index, (outcomes, code, count) in enumerate(cases):
        conn, context, binding, request = prepared(tmp_path / str(index))
        transport = SequenceTransport(*outcomes)
        with pytest.raises(AppError) as error:
            metered_complete(request, binding, context, transport=transport, wait=lambda _: None)
        assert error.value.code == code and transport.calls == count
        assert all(row["state"] == "unknown" for row in rows(conn))
        conn.close()


@pytest.mark.parametrize("status", [None, 503, 401])
def test_native_http_errors_retry_only_transient_failures_and_discard_secrets(tmp_path, status):
    conn, context, binding, request = prepared(tmp_path)
    if status is None:
        outcome = httpx.ConnectError("PRIVATE_CONNECTION_SECRET")
    else:
        http_request = httpx.Request("POST", "https://provider.invalid/PRIVATE_URL_SECRET")
        outcome = httpx.HTTPStatusError(
            "PRIVATE_PROVIDER_SECRET",
            request=http_request,
            response=httpx.Response(status, request=http_request),
        )
    transport = SequenceTransport(outcome, result())
    if status == 401:
        with pytest.raises(AppError) as error:
            metered_complete(request, binding, context, transport=transport, wait=lambda _: None)
        assert error.value.code == "PROVIDER_AUTH_ERROR"
        assert rows(conn)[0]["finish_reason"] == "authentication_failed"
        assert transport.calls == 1
    else:
        metered_complete(request, binding, context, transport=transport, wait=lambda _: None)
        assert transport.calls == 2
    assert "PRIVATE_" not in str(rows(conn))
    conn.close()


@pytest.mark.parametrize("invalid_metadata", [False, True])
def test_outer_result_failure_preserves_observed_usage_before_content_validation(
    tmp_path, invalid_metadata
):
    conn, context, binding, request = prepared(tmp_path)
    payload = result().model_dump()
    payload["parsed_output"] = ["PRIVATE_INVALID_OUTPUT"]
    payload["provider_debug"] = "PRIVATE_DEBUG_SECRET"
    if invalid_metadata:
        payload["latency_ms"] = -1
    transport = SequenceTransport(payload)
    with pytest.raises(AppError) as error:
        metered_complete(request, binding, context, transport=transport)
    recorded = rows(conn)
    assert error.value.code == "INVALID_OUTPUT" and transport.calls == 1
    assert len(recorded) == 1 and recorded[0]["state"] == "settled"
    assert recorded[0]["input_tokens_total"] == 10
    assert recorded[0]["output_tokens_total"] == 3
    if not invalid_metadata:
        assert recorded[0]["provider_request_id"] == "response-1"
        assert recorded[0]["finish_reason"] == "stop"
    assert "PRIVATE_" not in str(recorded)
    conn.close()


@pytest.mark.parametrize("changed_at", ["before_wrapper", "before_begin", "before_start"])
def test_stale_request_revision_cannot_become_a_fresh_http_attempt(
    tmp_path, monkeypatch, changed_at
):
    conn, context, binding, request = prepared(tmp_path)
    revision = context["run"]["revision"]

    def change_revision():
        conn.execute("UPDATE runs SET revision=revision+1 WHERE id=?", (context["run"]["id"],))
        conn.commit()

    if changed_at == "before_wrapper":
        change_revision()
    else:
        original = usage.begin_call if changed_at == "before_begin" else usage.start_call

        def changed(*args, **kwargs):
            change_revision()
            return original(*args, **kwargs)

        monkeypatch.setattr(
            usage, "begin_call" if changed_at == "before_begin" else "start_call", changed
        )
    transport = SequenceTransport(result())
    with pytest.raises(AppError) as error:
        metered_complete(request, binding, context, transport=transport)
    assert error.value.code == "CONFLICT" and transport.calls == 0
    assert context["run"]["revision"] == revision
    if changed_at == "before_start":
        assert len(rows(conn)) == 1 and rows(conn)[0]["state"] == "released"
    else:
        assert rows(conn) == []
    conn.close()
