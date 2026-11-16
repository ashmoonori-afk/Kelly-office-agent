from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from kelly import usage as usage_module
from kelly.contracts import (
    Actor,
    AppError,
    ModelBinding,
    ModelRequest,
    PlanV1,
    PriceSnapshot,
    RunRequest,
    TokenRates,
    Usage,
    canonical_json,
)
from kelly.db import create_run, get_run, get_value, init_db
from kelly.engine import install_plan
from kelly.usage import (
    begin_call,
    mark_usage_unknown,
    release_call,
    settle_call,
    start_call,
    summarize_usage,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)
OWNER = Actor(id="owner", project_ids=["sales"])
MEASURED = Usage(
    input_tokens_total=100,
    cached_input_tokens=20,
    cache_write_tokens=5,
    output_tokens_total=40,
    reasoning_tokens=10,
    usage_basis="provider",
)


def binding(**changes):
    return ModelBinding.model_validate(
        {
            "provider": "provider1",
            "model_id": "model-v1",
            "roles": ["L", "M", "H"],
            "role": "L",
            "enabled": True,
            "available": True,
            "allowed_projects": ["sales"],
            "capabilities": ["structured_output", "summarize", "planning"],
            "context_window": 32000,
            "max_output": 1000,
            "metering_mode": "api",
            "quality_evidence_id": "evaluation1",
            **changes,
        }
    )


def request(text="근거를 요약하세요"):
    return ModelRequest(
        model_id="model-v1",
        prompt_version="v1",
        context=text,
        output_schema={"type": "object"},
        max_output=100,
    )


def price(**changes):
    return PriceSnapshot(
        rates=TokenRates(input=1000000, cached_input=500000, cache_write=2000000, output=3000000),
        checked_at=NOW.isoformat(),
        source_url="https://example.com/pricing",
        **{"cache_write_billing": "separate", **changes},
    )


def setup(tmp_path):
    path = tmp_path / "state.db"
    conn = init_db(path)
    run = create_run(conn, OWNER, "ui", "one", RunRequest(request="자료 요약", project_id="sales"))
    plan = PlanV1.model_validate(
        {
            "requirements": [{"id": "r1", "text": "요약"}],
            "todos": [
                {
                    "id": f"t{i}",
                    "title": "요약",
                    "requirement_ids": ["r1"],
                    "kind": "simple",
                    "reason": "명시된 내용 요약",
                    "signals": ["summarization"],
                    "clarity": "clear",
                    "usefulness": "required",
                    "executor": {"type": "model", "role": "L", "task": "summarize"},
                    "output_schema": "ReportSectionsV1",
                    "done_when": ["요약 완료"],
                }
                for i in range(8)
            ],
        }
    )
    install_plan(conn, run["id"], plan, OWNER, tools={})
    return conn, run["id"], path


def reserve(conn, rid, tid="t0", purpose="generate", **kwargs):
    return begin_call(
        conn,
        rid,
        tid,
        binding(role="M" if tid is None else "L"),
        request(),
        actor=OWNER,
        purpose=purpose,
        now=NOW,
        **kwargs,
    )


def test_first_replan_after_zero_call_recipe_keeps_two_planning_call_limit(tmp_path):
    conn, rid, _ = setup(tmp_path)
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    first = reserve(conn, rid, None, "replan")
    settle(conn, first)
    settle(conn, reserve(conn, rid, None, "replan", route={"repair_errors": ["PLAN_INVALID"]}))
    with pytest.raises(AppError) as error:
        reserve(conn, rid, None, "replan")
    assert error.value.code == "CALL_LIMIT"
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 2
    conn.close()


def test_cross_run_totals_keep_each_runs_logical_call_number(tmp_path):
    rows = []
    for directory in (tmp_path / "one", tmp_path / "two"):
        conn, run_id, _ = setup(directory)
        try:
            rows.append(settle(conn, reserve(conn, run_id)))
        finally:
            conn.close()
    summary = usage_module._summarize(rows)
    assert summary["logical_calls"] == 2
    assert summary["transport_attempts"] == 2
    assert summary["total_tokens"] == 280


def settle(conn, record, usage=MEASURED, raw=None, **kwargs):
    if record["state"] == "reserved":
        start_call(conn, record["id"], OWNER)
    return settle_call(
        conn,
        record["id"],
        usage,
        raw,
        actor=OWNER,
        now=NOW,
        request_id="response1",
        finish_reason="stop",
        latency_ms=10,
        **kwargs,
    )


def test_exact_settlement_is_idempotent_conflict_is_recorded_without_overwriting(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        initial_revision = get_run(conn, rid, OWNER)["revision"]
        record = reserve(conn, rid, price_snapshot=price())
        settled = settle(conn, record, raw={"prompt_tokens": 100})
        assert settled["actual_micro_usd"] == 220
        duplicate = settle(conn, settled, raw={"prompt_tokens": 100})
        assert duplicate == settled
        with pytest.raises(AppError) as error:
            settle(
                conn,
                settled,
                MEASURED.model_copy(update={"input_tokens_total": 101}),
                raw={"prompt_tokens": 101},
            )
        assert error.value.code == "USAGE_CONFLICT"
        assert get_value(conn, f"usage_conflict:{record['id']}")["count"] == 1
        assert dict(
            conn.execute("SELECT input_tokens_total,actual_micro_usd FROM calls").fetchone()
        ) == {"input_tokens_total": 100, "actual_micro_usd": 220}
        assert get_run(conn, rid, OWNER)["revision"] == initial_revision
    finally:
        conn.close()


def test_unknown_is_not_zero_and_late_usage_survives_cancelled_run(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        record = reserve(conn, rid)
        start_call(conn, record["id"], OWNER)
        unknown = mark_usage_unknown(conn, record["id"], "TIMEOUT", actor=OWNER, now=NOW)
        assert unknown["state"] == "unknown" and unknown["input_tokens_total"] is None
        summary = summarize_usage(conn, rid, OWNER)
        assert summary["total_tokens"] is None and summary["actual_micro_usd"] is None
        conn.execute("UPDATE runs SET state='cancelled',revision=revision+1 WHERE id=?", (rid,))
        settled = settle(conn, unknown)
        assert settled["state"] == "settled"
        assert settled["actual_micro_usd"] is None
        summary = summarize_usage(conn, rid, OWNER)
        assert summary["total_tokens"] == 140  # Reasoning/cache are subsets, not added again.
        assert summary["reasoning_tokens"] == 10 and summary["cached_input_tokens"] == 20
    finally:
        conn.close()


def test_partial_usage_merges_without_erasing_previously_known_measurements(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        record = reserve(conn, rid)
        partial = settle(conn, record, Usage(input_tokens_total=100, usage_basis="provider"))
        assert partial["state"] == "unknown" and partial["provider_request_id"] == "response1"
        with pytest.raises(AppError) as error:
            settle(
                conn,
                partial,
                Usage(input_tokens_total=99, output_tokens_total=40, usage_basis="provider"),
            )
        assert error.value.code == "USAGE_CONFLICT"
        completed = settle(conn, partial, Usage(output_tokens_total=40, usage_basis="provider"))
        assert completed["input_tokens_total"] == 100 and completed["state"] == "settled"
        assert completed["cached_input_tokens"] is None and completed["actual_micro_usd"] is None
    finally:
        conn.close()


def test_missing_usage_response_metadata_is_retained(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        record = settle(conn, reserve(conn, rid), Usage())
        assert record["state"] == "unknown" and record["finish_reason"] == "stop"
        assert record["provider_request_id"] == "response1" and record["latency_ms"] == 10
    finally:
        conn.close()


def test_unknown_marker_never_downgrades_known_provider_measurements(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        partial = settle(
            conn, reserve(conn, rid), Usage(input_tokens_total=10, usage_basis="provider")
        )
        partial = mark_usage_unknown(conn, partial["id"], "TIMEOUT", actor=OWNER, now=NOW)
        assert partial["usage_basis"] == "provider"
        with pytest.raises(AppError) as error:
            settle(
                conn,
                partial,
                Usage(input_tokens_total=11, output_tokens_total=5, usage_basis="provider"),
            )
        assert error.value.code == "USAGE_CONFLICT"
        assert conn.execute("SELECT input_tokens_total FROM calls").fetchone()[0] == 10
        assert summarize_usage(conn, rid, OWNER)["known_tokens"]["input_tokens_total"] == 10
    finally:
        conn.close()


def test_unknown_basis_is_not_relabelled_estimated(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        settle(
            conn,
            reserve(conn, rid),
            Usage(input_tokens_total=10, output_tokens_total=5, usage_basis="unknown"),
        )
        summary = summarize_usage(conn, rid, OWNER)
        assert summary["usage_state"] == "unknown" and summary["total_tokens"] == 15
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["local", "subscription", "unknown"])
def test_non_api_metering_never_claims_a_zero_bill(tmp_path, mode):
    conn, rid, _ = setup(tmp_path)
    try:
        record = begin_call(
            conn,
            rid,
            "t0",
            binding(metering_mode=mode),
            request(),
            actor=OWNER,
            purpose="generate",
            now=NOW,
            price_snapshot=price(),
        )
        assert settle(conn, record)["actual_micro_usd"] is None
    finally:
        conn.close()


@pytest.mark.parametrize(
    "snapshot,usage,expected",
    [
        (
            price(cache_write_billing="included"),
            MEASURED.model_copy(update={"cache_write_tokens": None}),
            210,
        ),
        (price(cache_write_billing="unknown"), MEASURED, None),
        (price(), MEASURED.model_copy(update={"cache_write_tokens": None}), None),
        (
            PriceSnapshot(cache_write_billing="included"),
            Usage(
                input_tokens_total=0,
                cached_input_tokens=0,
                output_tokens_total=0,
                usage_basis="provider",
            ),
            0,
        ),
        (
            PriceSnapshot(
                rates=TokenRates(input=1, cached_input=1, output=1), cache_write_billing="included"
            ),
            MEASURED,
            3,
        ),
    ],
)
def test_component_billing_uses_integer_ceil_and_preserves_unknowns(
    tmp_path, snapshot, usage, expected
):
    conn, rid, _ = setup(tmp_path)
    try:
        assert (
            settle(conn, reserve(conn, rid, price_snapshot=snapshot), usage)["actual_micro_usd"]
            == expected
        )
    finally:
        conn.close()


def test_stale_price_is_a_label_and_does_not_block_same_role(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        stale = price().model_copy(update={"checked_at": (NOW - timedelta(days=31)).isoformat()})
        record = reserve(conn, rid, price_snapshot=stale)
        assert (
            record["route_json"]["role"] == "L" and record["price_snapshot_json"]["stale"] is True
        )
        settle(conn, record)
        assert summarize_usage(conn, rid, OWNER)["price_stale"] is True
    finally:
        conn.close()


def test_http_retry_shares_logical_call_and_counts_failed_usage(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        first = reserve(conn, rid)
        with pytest.raises(AppError):
            reserve(conn, rid, retry_of=first["id"])
        first = settle(conn, first)
        second = reserve(conn, rid, retry_of=first["id"])
        assert first["logical_call_no"] == second["logical_call_no"] == 1
        assert second["transport_attempt_no"] == 2
        settle(conn, second)
        assert summarize_usage(conn, rid, OWNER)["total_tokens"] == 280
        with pytest.raises(AppError) as error:
            reserve(conn, rid, retry_of=first["id"])
        assert error.value.code == "CALL_LIMIT"
    finally:
        conn.close()


def test_retry_cannot_change_request_step_model_or_purpose(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        first = settle(conn, reserve(conn, rid))
        with pytest.raises(AppError):
            begin_call(
                conn,
                rid,
                "t0",
                binding(),
                request("다른 요청"),
                actor=OWNER,
                purpose="generate",
                now=NOW,
                retry_of=first["id"],
            )
        with pytest.raises(AppError):
            reserve(conn, rid, "t1", retry_of=first["id"])
        with pytest.raises(AppError):
            reserve(conn, rid, purpose="repair", retry_of=first["id"])
        assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1
    finally:
        conn.close()


def test_logical_twelve_and_transport_sixteen_limits_include_planning(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        sequence = [(None, "plan"), (None, "replan")] + [
            (f"t{i}", purpose) for i in range(5) for purpose in ["generate", "repair"]
        ]
        for i, (tid, purpose) in enumerate(sequence):
            record = settle(conn, reserve(conn, rid, tid, purpose))
            if i < 4:
                settle(conn, reserve(conn, rid, tid, purpose, retry_of=record["id"]))
        summary = summarize_usage(conn, rid, OWNER)
        assert summary["logical_calls"] == 12 and summary["transport_attempts"] == 16
        assert summary["by_step"]["planning"]["logical_calls"] == 2
        with pytest.raises(AppError) as error:
            reserve(conn, rid, "t5")
        assert error.value.code == "CALL_LIMIT"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_per_todo_and_planning_limits_do_not_reset_on_unknown(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        for purpose in ["generate", "repair"]:
            record = reserve(conn, rid, purpose=purpose)
            start_call(conn, record["id"], OWNER)
            mark_usage_unknown(conn, record["id"], "TIMEOUT", actor=OWNER, now=NOW)
        with pytest.raises(AppError) as error:
            reserve(conn, rid, purpose="repair")
        assert error.value.code == "CALL_LIMIT"
        settle(conn, reserve(conn, rid, None, "plan"))
        settle(conn, reserve(conn, rid, None, "replan"))
        with pytest.raises(AppError) as error:
            reserve(conn, rid, None, "replan")
        assert error.value.code == "CALL_LIMIT"
    finally:
        conn.close()


@pytest.mark.parametrize("limit", ["logical", "transport"])
def test_total_limits_are_independent(tmp_path, limit):
    conn, rid, _ = setup(tmp_path)
    try:
        if limit == "logical":
            sequence = [(None, "plan"), (None, "replan")] + [
                (f"t{i}", purpose) for i in range(5) for purpose in ["generate", "repair"]
            ]
            for tid, purpose in sequence:
                settle(conn, reserve(conn, rid, tid, purpose))
            assert summarize_usage(conn, rid, OWNER)["transport_attempts"] == 12
            with pytest.raises(AppError) as error:
                reserve(conn, rid, "t5")
        else:
            for i in range(8):
                first = settle(conn, reserve(conn, rid, f"t{i}"))
                settle(conn, reserve(conn, rid, f"t{i}", retry_of=first["id"]))
            assert summarize_usage(conn, rid, OWNER)["logical_calls"] == 8
            with pytest.raises(AppError) as error:
                reserve(conn, rid, None, "plan")
        assert error.value.code == "CALL_LIMIT"
    finally:
        conn.close()


def test_planning_schema_repair_can_keep_purpose_plan_but_needs_errors(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        settle(conn, reserve(conn, rid, None, "plan"))
        with pytest.raises(AppError) as error:
            reserve(conn, rid, None, "plan")
        assert error.value.code == "PLAN_INVALID"
        repaired = reserve(conn, rid, None, "plan", route={"repair_errors": ["PLAN_INVALID"]})
        assert repaired["logical_call_no"] == 2 and repaired["purpose"] == "plan"
        settle(conn, repaired)
        with pytest.raises(AppError) as error:
            reserve(conn, rid, None, "plan", route={"repair_errors": ["PLAN_INVALID"]})
        assert error.value.code == "CALL_LIMIT"
    finally:
        conn.close()


def test_concurrent_reservations_cannot_create_duplicate_logical_calls(tmp_path):
    conn, rid, path = setup(tmp_path)
    conn.close()

    def reserve_in_thread(_):
        connection = init_db(path)
        try:
            return reserve(connection, rid)["id"]
        except AppError as error:
            return error.code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(reserve_in_thread, range(4)))
    conn = init_db(path)
    try:
        assert outcomes.count("CONFLICT") == 3
        assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1
    finally:
        conn.close()


def test_cancel_before_start_releases_and_start_is_once(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        record = reserve(conn, rid)
        conn.execute("UPDATE runs SET state='cancelled',revision=revision+1 WHERE id=?", (rid,))
        with pytest.raises(AppError) as error:
            start_call(conn, record["id"], OWNER)
        assert error.value.code == "CANCELLED"
        assert conn.execute("SELECT state FROM calls").fetchone()[0] == "released"
        assert summarize_usage(conn, rid, OWNER)["usage_state"] == "not_used"
        assert release_call(conn, record["id"], actor=OWNER, now=NOW)["state"] == "released"
        with pytest.raises(AppError):
            settle(conn, record)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "now,hours",
    [(datetime(2026, 3, 8, 12, tzinfo=UTC), 23), (datetime(2026, 11, 1, 12, tzinfo=UTC), 25)],
)
def test_timezone_calendar_boundaries_handle_dst(tmp_path, now, hours):
    conn, rid, _ = setup(tmp_path)
    try:
        record = begin_call(
            conn,
            rid,
            "t0",
            binding(),
            request(),
            actor=OWNER,
            purpose="generate",
            now=now,
            timezone_name="America/New_York",
        )
        assert datetime.fromisoformat(record["day_end_utc"]) - datetime.fromisoformat(
            record["day_start_utc"]
        ) == timedelta(hours=hours)
        assert record["month_start_utc"].endswith("+00:00") and record["month_end_utc"].endswith(
            "+00:00"
        )
    finally:
        conn.close()


def test_korean_byte_estimate_and_no_prompt_response_or_secret_in_ledger(tmp_path):
    conn, rid, _ = setup(tmp_path)
    secret = "UNIQUE_SYNTHETIC_SECRET_123"
    try:
        req = request("한글 " * 50 + secret)
        record = begin_call(
            conn, rid, "t0", binding(), req, actor=OWNER, purpose="generate", now=NOW
        )
        assert record["reserved_tokens"] >= len(req.context.encode("utf-8")) + req.max_output
        result = settle(
            conn,
            record,
            raw={
                "prompt_tokens": 100,
                "api_key": secret,
                "response": secret,
                "prompt_tokens_details": {"cached_tokens": 20, "secret": secret},
            },
        )
        assert result["raw_usage_json"] == {
            "prompt_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 20},
        }
        assert secret not in canonical_json(result)
        with pytest.raises(AppError):
            reserve(conn, rid, "t1", route={"api_key": secret})
        with pytest.raises(AppError):
            summarize_usage(conn, rid, Actor(id="other", project_ids=["sales"]))
    finally:
        conn.close()


def test_repeated_midnight_uses_first_calendar_boundary(tmp_path):
    conn, rid, _ = setup(tmp_path)
    try:
        record = begin_call(
            conn,
            rid,
            "t0",
            binding(),
            request(),
            actor=OWNER,
            purpose="generate",
            now=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            timezone_name="America/Havana",
        )
        assert record["day_start_utc"] == "2026-11-01T04:00:00+00:00"
        assert record["day_end_utc"] == "2026-11-02T05:00:00+00:00"
    finally:
        conn.close()


@pytest.mark.parametrize("todo_id,purpose", [("t0", "generate"), (None, "plan")])
def test_admission_rejects_request_built_before_input_revision_changed(tmp_path, todo_id, purpose):
    conn, rid, _ = setup(tmp_path)
    try:
        expected = get_run(conn, rid, OWNER)["revision"]
        conn.execute("UPDATE runs SET revision=revision+1 WHERE id=?", (rid,))
        with pytest.raises(AppError) as error:
            reserve(conn, rid, todo_id, purpose, expected_revision=expected)
        assert error.value.code == "CONFLICT"
        assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
        current = get_run(conn, rid, OWNER)["revision"]
        record = reserve(conn, rid, todo_id, purpose, expected_revision=current)
        assert record["route_json"]["run_revision"] == current
    finally:
        conn.close()


def test_admission_revision_check_is_inside_reservation_transaction(tmp_path, monkeypatch):
    conn, rid, _ = setup(tmp_path)
    original = usage_module.transaction
    expected = get_run(conn, rid, OWNER)["revision"]

    @contextmanager
    def inputs_changed_before_begin(connection):
        connection.execute("UPDATE runs SET revision=revision+1 WHERE id=?", (rid,))
        with original(connection):
            yield connection

    try:
        monkeypatch.setattr(usage_module, "transaction", inputs_changed_before_begin)
        with pytest.raises(AppError) as error:
            reserve(conn, rid, expected_revision=expected)
        assert error.value.code == "CONFLICT"
        assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
        assert get_run(conn, rid, OWNER)["revision"] == expected + 1
    finally:
        conn.close()
