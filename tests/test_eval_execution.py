"""Offline eval uses actual files/product execution; no generated model-answer fixture."""

import io
import json
import socket

import pytest
from openpyxl import load_workbook

from evals.execute import execute_case, source_bytes, task_spec
from evals.run import Case, load_cases, validate_observations


@pytest.fixture(scope="module")
def corpus():
    return {case.id: case for case in load_cases()}


def test_task_mapping_keeps_request_and_explicit_inputs_but_does_not_leak_gold(corpus):
    case = corpus["table-01"].model_copy(deep=True)
    case.gold.numbers["total"] = "999999"
    case.gold.required_assertions[0].text = "GOLD_MUST_NOT_ENTER_TASK"
    spec = task_spec(case, ["managed-source"])
    assert spec.intent == "aggregate"
    assert spec.inputs["artifact_id"] == "managed-source"
    assert spec.inputs["value_columns"] == ["금액"]
    assert spec.inputs["policy"]["unit"] == "원"
    assert spec.acceptance == [case.request]
    serialized = spec.model_dump_json()
    assert "999999" not in serialized and "GOLD_MUST_NOT_ENTER_TASK" not in serialized


def test_xlsx_materializer_preserves_sheet_selection_hidden_rows_and_formula(corpus):
    raw = source_bytes(corpus["table-13"].sources[0])
    book = load_workbook(io.BytesIO(raw), data_only=False)
    try:
        assert book["자료"]["B3"].value == 900
        assert book["자료"].row_dimensions[3].hidden is True
    finally:
        book.close()
    raw = source_bytes(corpus["table-15"].sources[0])
    book = load_workbook(io.BytesIO(raw), data_only=False)
    try:
        assert book["자료"]["B2"].value == "=SUM(C1:C2)"
    finally:
        book.close()


@pytest.mark.parametrize("arm", ["A", "B"])
def test_unsupported_arm_never_creates_workspace(corpus, tmp_path, arm):
    directory = tmp_path / "unsupported"
    with pytest.raises(ValueError, match="A/B"):
        execute_case(corpus["table-01"], directory, arm=arm)
    assert not directory.exists()


def test_automation_scenario_cannot_be_reported_as_executed(corpus, tmp_path):
    directory = tmp_path / "unsupported"
    with pytest.raises(ValueError, match="automation"):
        execute_case(corpus["automation-01"], directory)
    assert not directory.exists()


def test_warm_requires_cold_setup_and_unchanged_environment(corpus, tmp_path):
    directory = tmp_path / "warm"
    with pytest.raises(ValueError, match="cold observation"):
        execute_case(corpus["table-01"], directory, cache="warm")
    directory.mkdir()
    (directory / "cold-observation.json").write_text(
        json.dumps({"environment_hash": "0" * 64}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unchanged product code"):
        execute_case(corpus["table-01"], directory, cache="warm")
    assert not (directory / "state.db").exists()


@pytest.mark.parametrize(
    "case_id,expected",
    [
        ("table-01", "succeeded"),
        ("conflict_missing-05", "NO_CAPABLE_MODEL"),
        ("summary-01", "NO_CAPABLE_MODEL"),
    ],
)
def test_three_frozen_representatives_use_real_product_boundaries(
    corpus, tmp_path, monkeypatch, case_id, expected
):
    def no_network(*_args, **_kwargs):
        raise AssertionError("offline eval attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", no_network)
    case = corpus[case_id]
    directory = tmp_path / case_id
    observed = execute_case(case, directory)
    ledger = json.loads((directory / "cold-ledger.json").read_text("utf-8"))
    assert observed.outcome == expected
    assert observed.execution == "offline_workload"
    assert observed.calls == ledger["calls"] == []
    assert ledger["original_request"] == ledger["run"]["request_json"]["request"] == case.request
    assert len(ledger["sources"]) == len(case.sources)
    assert all(source["unchanged"] for source in ledger["sources"])
    assert validate_observations([observed.model_dump()], list(corpus.values())) == [observed]
    if expected == "succeeded":
        assert observed.workload_complete and observed.output_verified
        assert observed.numbers == {"total": "35000.00"}
        assert observed.units == {"total": "원"}
        assert "35000" in observed.output_text
        assert all(step["state"] == "succeeded" for step in ledger["steps"])
        assert len(ledger["results"]) == 1 and ledger["results"][0]["reopened"]
    else:
        assert observed.workload_complete is False
        assert observed.output_text == "" and observed.numbers == {}
        assert observed.facts == [] and observed.coverage == {}
        assert ledger["run"]["state"] == "failed"
    with pytest.raises(ValueError, match="already executed"):
        execute_case(case, directory)


def test_separate_narrow_missing_input_fixture_is_not_a_frozen_case_quality_result(
    corpus, tmp_path
):
    # The original corpus sentence stays unchanged in the test above. This explicitly named
    # narrow fixture exercises the supported missing-input product state without any model.
    values = corpus["conflict_missing-05"].model_dump()
    values.update(id="narrow-missing-fixture", request="자료 요약해줘")
    case = Case.model_validate(values)
    result = execute_case(case, tmp_path / case.id)
    assert result.outcome == "awaiting_input"
    assert result.missing_input and result.workload_complete is False
    assert result.calls == [] and result.output_text == ""
