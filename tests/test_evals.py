"""Synthetic records test scoring mechanics, never model quality or live delivery."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "birkin_eval_runner", Path(__file__).parents[1] / "evals" / "run.py"
)
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


@pytest.fixture(scope="module")
def cases():
    return evaluation.load_cases()


def observation(case, *, identifier="o1", arm="C", execution="offline_fixture", calls=()):
    text = "\n".join(case.gold.allowed_facts)
    return {
        "id": identifier,
        "case_id": case.id,
        "arm": arm,
        "cache": "cold",
        "execution": execution,
        "condition_hash": evaluation.condition_hash(case),
        "environment_hash": "a" * 64,
        "evidence_ref": "tests-only-not-live",
        "workload_complete": True,
        "output_text": text,
        "output_sha256": evaluation.sha(text),
        "numbers": case.gold.numbers,
        "units": case.gold.units,
        "numeric_locators": case.gold.numeric_locators,
        "facts": case.gold.allowed_facts,
        "coverage": {item.id: item.locators for item in case.gold.required_assertions},
        "effects": [],
        "missing_input": case.gold.expected_missing_input,
        "outcome": case.gold.expected_outcome,
        "scenario_result": case.scenario.get("expected", {}),
        "todos": [
            {
                "id": "t1",
                "title": "합성 평가의 필수 업무",
                "requirement_ids": [item.id for item in case.gold.required_assertions],
                "kind": case.gold.expected_kind,
                "usefulness": "required",
                "executed": True,
            }
        ],
        "calls": list(calls),
        "latency_ms": 100,
        "cache_lookups": 2,
        "cache_hits": 0,
        "repair_scope": [],
        "numeric_verified": True,
        "source_verified": True,
        "effect_verified": True,
        "output_verified": True,
    }


def call(identifier="c1", *, tokens=(10, 4), **changes):
    return {
        "id": identifier,
        "purpose": "generate",
        "role": "L",
        "model_id": "synthetic-model",
        "state": "settled",
        "usage": {
            "input_tokens_total": tokens[0],
            "output_tokens_total": tokens[1],
            "cached_input_tokens": 2 if tokens[0] is not None else None,
            "reasoning_tokens": 1 if tokens[1] is not None else None,
            "usage_basis": "provider",
        },
        "actual_micro_usd": 3,
        "price_stale": False,
        **changes,
    }


def label(record, **changes):
    return {
        "blind_id": evaluation.blind_id(record),
        "output_sha256": record.output_sha256,
        "reviewer_id": "human-fixture",
        "blinded": True,
        "adopted": True,
        "coverage_correct": True,
        "facts_grounded": True,
        "usable": True,
        "todo_decomposition_appropriate": True,
        "rationale": "집계 코드 검사에만 쓰는 합성 라벨",
        **changes,
    }


def test_frozen_corpus_has_twenty_diverse_cases_per_category_and_resolving_quotes(cases):
    assert len(cases) == 120
    assert {c.category for c in cases} == set(evaluation.CATEGORIES)
    assert len({c.request for c in cases}) == 120
    assert sum(c.gold.expected_missing_input for c in cases) >= 12
    assert sum(bool(c.gold.numbers) for c in cases) >= 25
    assert all(c.scenario.get("events") for c in cases if c.category == "automation")
    assert len({s.text for c in cases for s in c.sources}) >= 140
    assert evaluation.PROTOCOL["live_runner_available"] is False


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "locator", "nan", "unit", "extra"])
def test_corpus_validator_rejects_corrupted_gold(tmp_path, mutation):
    raw = json.loads((Path(__file__).parents[1] / "evals" / "cases.json").read_text("utf-8"))
    if mutation == "duplicate":
        raw["cases"][1]["id"] = raw["cases"][0]["id"]
    elif mutation == "missing":
        raw["cases"].pop()
    elif mutation == "locator":
        raw["cases"][0]["gold"]["required_assertions"][0]["locators"] = ["missing:L1"]
    elif mutation == "nan":
        raw["cases"][0]["gold"]["numbers"]["total"] = "NaN"
    elif mutation == "unit":
        raw["cases"][0]["gold"]["units"] = {}
    else:
        raw["cases"][0]["budget"] = 50
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        evaluation.load_cases(path)


def test_offline_fixture_scores_never_become_live_quality_or_savings(cases):
    case = cases[40]
    records = evaluation.validate_observations([observation(case, calls=[call()])], cases)
    labels = evaluation.import_labels([label(records[0])], records)
    report = evaluation.score(cases, records, labels)
    group = report["groups"]["offline_fixture/C/cold/simple"]
    assert group["structured_contract_passes"] == 1
    assert group["live_quality_verified"] is False
    assert group["quality_pass_rate"] is None
    assert group["tokens_per_accepted_task"] is None
    assert all(item["token_reduction"] is None for item in report["comparisons"])
    assert report["matrix_complete"] is False and report["expected_matrix_tasks"] == 720


def test_live_import_without_required_human_labels_stays_unverified(cases):
    records = evaluation.validate_observations(
        [observation(cases[40], execution="live", calls=[call()])], cases
    )
    group = evaluation.summarize(records, cases, [])
    assert group["quality_pass_rate"] is None
    assert group["accepted_tasks"] is None
    assert group["total_tokens"] == 14


def test_failed_tasks_and_failed_repair_calls_remain_in_accepted_denominator_numerator(cases):
    first = observation(
        cases[40],
        execution="live",
        calls=[
            call("plan", purpose="plan", role="M"),
            call("repair", purpose="repair", escalated=True),
        ],
    )
    second = observation(cases[41], identifier="o2", execution="live", calls=[call("failed")])
    second["workload_complete"] = False
    records = evaluation.validate_observations([first, second], cases)
    labels = evaluation.import_labels(
        [label(records[0]), label(records[1], adopted=False)], records
    )
    result = evaluation.summarize(records, cases, labels)
    assert result["accepted_tasks"] == 1
    assert result["quality_pass_rate"] == 0.5
    assert result["total_tokens"] == result["tokens_per_accepted_task"] == 42
    assert result["actual_micro_usd"] == result["cost_per_accepted_task_micro_usd"] == 9
    assert result["planning_tokens"] == 14
    assert result["repair_rate"] == result["escalation_rate"] == 0.5
    assert result["cached_input_tokens"] == 6
    assert result["reasoning_tokens_subset"] == 3


def test_unknown_usage_and_stale_price_never_become_zero(cases):
    raw = observation(
        cases[40],
        execution="live",
        calls=[
            call(),
            call("unknown", tokens=(None, None), state="unknown", actual_micro_usd=None),
        ],
    )
    records = evaluation.validate_observations([raw], cases)
    labels = evaluation.import_labels([label(records[0])], records)
    result = evaluation.summarize(records, cases, labels)
    assert result["total_tokens"] is None and result["tokens_per_accepted_task"] is None
    assert result["known_tokens_lower_bound"] == 14
    assert result["actual_micro_usd"] is None
    assert result["cost_per_accepted_task_micro_usd"] is None
    assert result["unknown_usage_rate"] == 0.5
    raw["calls"] = [call(price_stale=True)]
    record = evaluation.validate_observations([raw], cases)[0]
    assert evaluation.summarize([record], cases, [])["actual_micro_usd"] is None


@pytest.mark.parametrize(
    "mutation", ["source", "environment", "duplicate", "output", "extra", "subset"]
)
def test_observation_identity_scope_and_usage_contract(cases, mutation):
    raw = observation(cases[40])
    records = [raw]
    if mutation == "source":
        raw["condition_hash"] = "b" * 64
    elif mutation == "environment":
        other = deepcopy(raw)
        other.update(id="o2", arm="A", environment_hash="b" * 64)
        records.append(other)
    elif mutation == "duplicate":
        records.append(deepcopy(raw))
    elif mutation == "output":
        raw["output_text"] += "변조"
    elif mutation == "extra":
        raw["secret"] = "not-allowed"
    else:
        raw["calls"] = [call(tokens=(1, 4))]
    with pytest.raises(ValueError):
        evaluation.validate_observations(records, cases)


def test_blind_packet_hides_identity_and_import_binds_exact_output(cases):
    records = evaluation.validate_observations([observation(cases[40], calls=[call()])], cases)
    packet = evaluation.blind_packet(records, cases)
    assert not {"arm", "cache", "model_id", "calls", "case_id"} & set(packet[0])
    assert "synthetic-model" not in json.dumps(packet)
    valid = label(records[0])
    assert len(evaluation.import_labels([valid], records)) == 1
    for change in (
        {"output_sha256": "f" * 64},
        {"blind_id": "not-known"},
        {"blinded": False},
        {"model_id": "leaked"},
        {"facts_grounded": False},
    ):
        with pytest.raises(ValueError):
            evaluation.import_labels([valid | change], records)
    with pytest.raises(ValueError):
        evaluation.import_labels([valid, valid], records)


def test_number_units_locator_required_coverage_and_effect_must_all_pass(cases):
    raw = observation(cases[0])
    good = evaluation.validate_observations([raw], cases)[0]
    assert evaluation.checks(cases[0], good)["passed"]
    for change, expected in (
        ({"numbers": {"total": "35001"}}, "numbers"),
        ({"units": {"total": "USD"}}, "numbers"),
        ({"numeric_locators": {"total": ["s1:L1"]}}, "numbers"),
        ({"coverage": {}}, "coverage"),
        ({"effects": ["external_write"]}, "effect"),
        ({"missing_input": True}, "missing_input"),
        ({"calls": [call()]}, "mechanical_model_calls"),
    ):
        record = evaluation.validate_observations([raw | change], cases)[0]
        result = evaluation.checks(cases[0], record)
        assert result["passed"] is False and result["checks"][expected] is False


def test_classification_optional_todo_and_completed_repair_are_visible(cases):
    case = cases[80]
    raw = observation(case)
    raw["todos"][0].update(kind="simple", completed_before_repair=True, reexecuted=True)
    raw["todos"].append(
        {
            "id": "extra",
            "title": "요청하지 않은 장식",
            "requirement_ids": [],
            "kind": "complex",
            "usefulness": "optional",
            "executed": True,
        }
    )
    record = evaluation.validate_observations([raw], cases)[0]
    result = evaluation.checks(case, record)
    assert result["classification_under"] == len(case.gold.required_assertions)
    assert result["unnecessary_todos"] == 1
    assert result["checks"]["bounded_repair"] is False
    raw["todos"][0].update(purpose="support", kind="mechanical")
    record = evaluation.validate_observations([raw], cases)[0]
    assert evaluation.checks(case, record)["classification_under"] == 0


def test_wilson_and_interpolated_latency_have_defined_small_sample_behavior():
    assert evaluation.wilson(0, 0) is None
    low, high = evaluation.wilson(5, 10)
    assert low == pytest.approx(0.2365930905)
    assert high == pytest.approx(0.7634069095)
    assert evaluation.wilson(0, 10)[1] > 0
    assert evaluation.wilson(10, 10)[0] < 1
    assert evaluation.percentile([], 0.95) is None
    assert evaluation.percentile([100, 300, 200], 0.5) == 200
    assert evaluation.percentile([100, 300, 200], 0.95) == 290


def test_matched_live_imports_allow_only_descriptive_comparison(cases):
    case = cases[40]
    raw = [
        observation(
            case, identifier="a", arm="A", execution="live", calls=[call(tokens=(100, 20))]
        ),
        observation(case, identifier="c", arm="C", execution="live", calls=[call(tokens=(40, 20))]),
    ]
    records = evaluation.validate_observations(raw, cases)
    labels = evaluation.import_labels([label(record) for record in records], records)
    report = evaluation.score(cases, records, labels)
    compared = next(
        row
        for row in report["comparisons"]
        if row["challenger"] == "C" and row["cache"] == "cold" and row["cohort"] == "simple"
    )
    assert compared["token_reduction"] == 0.5
    assert compared["quality_delta"] == 0
    assert compared["release_target_proven"] is None
    raw[1]["calls"][0]["usage"]["usage_basis"] = "estimated"
    records = evaluation.validate_observations(raw, cases)
    report = evaluation.score(cases, records, labels)
    assert all(row["token_reduction"] is None for row in report["comparisons"])


def test_real_metered_boundary_with_fake_transport_accounts_failed_attempt():
    result = evaluation.boundary_smoke()
    assert result["live"] is False and result["network_calls"] == 0
    assert result["transport_fixture_attempts"] == 2
    assert result["fixture_tokens_including_failed"] == 23
    assert result["actual_micro_usd"] is None
    assert result["model_quality_pass_rate"] is None
    assert result["workload_executed"] is False


def test_automation_requires_measured_scenario_state_not_just_a_success_label(cases):
    case = cases[100]
    raw = observation(case)
    record = evaluation.validate_observations([raw], cases)[0]
    assert evaluation.checks(case, record)["passed"]
    raw["scenario_result"] = {"runs": 2, "completed": True}
    record = evaluation.validate_observations([raw], cases)[0]
    assert evaluation.checks(case, record)["checks"]["scenario"] is False


def test_json_reader_rejects_duplicate_keys_and_nonfinite_numbers(tmp_path):
    path = tmp_path / "labels.json"
    for raw in ('{"blinded": false, "blinded": true}', '{"latency_ms": NaN}'):
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(ValueError):
            evaluation.read_json(path)
