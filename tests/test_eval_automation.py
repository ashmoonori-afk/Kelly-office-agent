"""Stateful eval scenarios call production APIs; only clock and HTTP responses are synthetic."""

import json
import socket

import pytest

from evals.automation import execute_scenario
from evals.run import PROTOCOL, fingerprint, load_cases, validate_observations


@pytest.fixture(scope="module")
def corpus():
    return {case.id: case for case in load_cases()}


@pytest.mark.parametrize("case_id", ["automation-01", "automation-18", "automation-15"])
def test_once_recovery_permission_use_actual_durable_state(corpus, tmp_path, monkeypatch, case_id):
    def no_network(*_args, **_kwargs):
        raise AssertionError("evaluation attempted real network")

    monkeypatch.setattr(socket, "create_connection", no_network)
    case = corpus[case_id]
    directory = tmp_path / case.id
    observed = execute_scenario(case, directory)
    ledger = json.loads((directory / "ledger.json").read_text("utf-8"))
    assert observed.execution == "offline_workload"
    assert observed.workload_complete and observed.scenario_result == case.scenario["expected"]
    assert ledger["scenario_gold_match"] is True
    assert ledger["real_network_calls"] == ledger["model_calls"] == 0
    assert ledger["ledger"]["calls"] == []
    assert ledger["office_payload_executed"] is False
    assert validate_observations([observed.model_dump()], list(corpus.values())) == [observed]
    if case_id == "automation-01":
        assert len(ledger["ledger"]["runs"]) == 1
        assert ledger["ledger"]["runs"][0]["state"] == "queued"
        assert len(ledger["ledger"]["inbox_events"]) == 1
        assert next(iter(ledger["state"].values()))["completed"] is True
    elif case_id == "automation-18":
        assert ledger["connection"]["update_epoch"] == 2
        assert ledger["trace"][0]["previous_cursor"] == 901
        assert len(ledger["ledger"]["runs"]) == 1
        assert ledger["ledger"]["inbox_events"][-1]["event_key"] == "2:1"
    else:
        assert ledger["ledger"]["automations"][0]["enabled"] == 0
        assert ledger["mock_http_methods"] == ["answerCallbackQuery"]
        assert ledger["ledger"]["inbox_events"][-1]["state"] == "rejected"
    with pytest.raises(FileExistsError):
        execute_scenario(case, directory)


def test_unsupported_scenario_is_not_a_successful_test_alias(corpus, tmp_path):
    case = corpus["automation-12"].model_copy(deep=True)
    case.id = "automation-99"
    result = execute_scenario(case, tmp_path / case.id)
    assert result.workload_complete is False
    assert result.outcome == "UNSUPPORTED_EVAL_SCENARIO"
    assert result.scenario_result == {} and result.output_text == ""


def test_altering_gold_does_not_alter_observed_once_result(corpus, tmp_path):
    case = corpus["automation-01"].model_copy(deep=True)
    case.scenario["expected"] = {"runs": 999, "completed": False}
    result = execute_scenario(case, tmp_path / "changed-gold")
    ledger = json.loads((tmp_path / "changed-gold" / "ledger.json").read_text("utf-8"))
    assert result.scenario_result == {"runs": 1, "completed": True}
    assert ledger["scenario_gold_match"] is False


@pytest.mark.parametrize(
    "case_id",
    [
        "automation-02",
        "automation-03",
        "automation-04",
        "automation-05",
        "automation-06",
        "automation-07",
        "automation-08",
        "automation-13",
        "automation-14",
        "automation-19",
    ],
)
def test_other_calendar_and_ingress_cases_record_actual_results(corpus, tmp_path, case_id):
    case = corpus[case_id]
    result = execute_scenario(case, tmp_path / case_id)
    assert result.workload_complete, result.outcome
    assert result.scenario_result == case.scenario["expected"]
    if case_id in {"automation-13", "automation-14"}:
        ledger = json.loads((tmp_path / case_id / "ledger.json").read_text("utf-8"))
        assert ledger["office_payload_executed"] is False
        assert not any(row["state"] == "succeeded" for row in ledger["ledger"]["runs"])
        assert not ledger["ledger"]["calls"] and not ledger["ledger"]["artifacts"]


@pytest.mark.parametrize("case_id", ["automation-16", "automation-17"])
def test_document_authority_and_unknown_delivery_are_real_artifact_boundaries(
    corpus, tmp_path, case_id
):
    case = corpus[case_id]
    result = execute_scenario(case, tmp_path / case_id)
    ledger = json.loads((tmp_path / case_id / "ledger.json").read_text("utf-8"))
    assert result.workload_complete, ledger["trace"]
    assert result.scenario_result == case.scenario["expected"]
    assert ledger["model_calls"] == ledger["real_network_calls"] == 0
    assert ledger["office_payload_executed"] is False
    assert len(ledger["ledger"]["artifacts"]) == 2
    assert ledger["trace"][0]["fixture_only"] is True
    assert ledger["trace"][0]["actual_sha256"] == ledger["ledger"]["artifacts"][-1]["sha256"]
    if case_id == "automation-16":
        assert ledger["mock_http_methods"] == []
        assert ledger["ledger"]["outbox"] == []
        assert ledger["trace"][-1]["error_code"] == "POLICY_DENIED"
    else:
        assert ledger["mock_http_methods"] == ["sendDocument"]
        assert ledger["ledger"]["outbox"][0]["attempt_count"] == 1
        assert ledger["trace"][-1]["artifacts_preserved"] is True
        assert ledger["trace"][-1]["db_reopened"] is True
        assert ledger["trace"][-1]["retried"] is False


@pytest.mark.parametrize(
    "case_id", ["automation-09", "automation-10", "automation-11", "automation-12", "automation-20"]
)
def test_folder_events_use_real_files_and_current_grants(corpus, tmp_path, case_id):
    case = corpus[case_id]
    result = execute_scenario(case, tmp_path / case_id)
    ledger = json.loads((tmp_path / case_id / "ledger.json").read_text("utf-8"))
    assert result.workload_complete, ledger["trace"]
    assert ledger["model_calls"] == ledger["real_network_calls"] == 0
    if case_id == "automation-12":
        assert result.scenario_result == {
            "reads_outside_grant": 0,
            "runs": 0,
            "error": "POLICY_DENIED",
        }
        assert ledger["scenario_gold_match"] is True
        assert ledger["trace"][-1]["outside_source_unchanged"] is True
    else:
        assert result.scenario_result == case.scenario["expected"]
    if case_id == "automation-11":
        writes = [item for item in ledger["trace"] if item["op"] == "fixture_write"]
        assert writes[0]["stat"] == writes[1]["stat"] == writes[2]["stat"]
        assert writes[0]["sha256"] != writes[1]["sha256"]
        assert len(ledger["ledger"]["artifacts"]) == 2
    if case_id == "automation-20":
        assert ledger["trace"][-1]["grant_revision"] == 2
        assert ledger["trace"][-1]["pending_events"] == 1
        assert ledger["trace"][-1]["runs_unchanged"] is True
        assert ledger["trace"][-1]["source_unchanged"] is True
        assert next(iter(ledger["state"].values()))["last_error"] == "NOT_FOUND"


@pytest.mark.parametrize("previous_version", ["birkin-eval-v1", "birkin-eval-v1.1"])
def test_protocol_revision_rejects_previous_observation(corpus, tmp_path, previous_version):
    assert PROTOCOL["version"] == "birkin-eval-v1.2"
    assert corpus["automation-12"].scenario["expected"]["error"] == "POLICY_DENIED"
    case = corpus["automation-01"]
    observed = execute_scenario(case, tmp_path / case.id)
    old = observed.model_dump()
    old["condition_hash"] = fingerprint(
        {
            "protocol": PROTOCOL | {"version": previous_version},
            "case": case.model_dump(),
        }
    )
    assert old["condition_hash"] != observed.condition_hash
    with pytest.raises(ValueError, match="protocol mismatch"):
        validate_observations([old], list(corpus.values()))
