"""Offline eval uses actual files/product execution; no generated model-answer fixture."""

import io
import json
import socket

import pytest
from openpyxl import load_workbook

from evals.execute import execute_case, numeric_observation, source_bytes, task_spec
from evals.run import Case, Source, load_cases, validate_observations


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
    assert (observed.cache_lookups, observed.cache_hits) == (0, 0)
    assert ledger["cache_observation"] == {"lookups": 0, "hits": 0}
    assert ledger["cache_instrumentation"].startswith("completed checks")
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


def numeric_fixture(corpus, source, aggregate):
    values = corpus["table-01"].model_dump()
    values["sources"] = [source.model_dump()]
    # The source selection comes from the actual-shaped aggregate, never these gold locators.
    values["gold"]["numeric_locators"] = {"total": [next(iter(source.locators))]}
    for assertion in values["gold"]["required_assertions"]:
        assertion["locators"] = [next(iter(source.locators))]
    case = Case.model_validate(values)
    step = {
        "state": "succeeded",
        "spec_json": {"todo": {"executor": {"args": {"artifact_id": "actual-source"}}}},
        "output_json": {"aggregate": aggregate},
    }
    return numeric_observation(case, {source.id: {"id": "actual-source"}}, [step])


def test_numeric_locator_uses_selected_header_and_group_contributors(corpus):
    lines = ["범위 밖", "팀,금액", "A,100", "B,200", "A,문자", "A,300", "A,9999"]
    source = Source(
        id="s1",
        filename="source.csv",
        text="\n".join(lines),
        locators={f"s1:L{i}": line for i, line in enumerate(lines, 1)},
    )
    aggregate = {
        "format": "csv",
        "unit": "원",
        "policy": {},
        "selection": {
            "cell_range": "A2:B6",
            "delimiter": ",",
            "sheet": None,
            "include_hidden_rows": False,
            "group_by": "팀",
        },
        "columns": {"금액": {"sum": "600", "count": 3, "locator": "csv:range:B3:B6"}},
        "groups": [
            {"key": "A", "filter": {"column": "팀", "equals": "A"}, "sums": {"금액": "400"}},
            {"key": "B", "filter": {"column": "팀", "equals": "B"}, "sums": {"금액": "200"}},
        ],
    }
    numbers, units, locations = numeric_fixture(corpus, source, aggregate)
    assert numbers == {"A": "400", "B": "200"} and units == {"A": "원", "B": "원"}
    assert locations == {"A": ["s1:L2", "s1:L3", "s1:L6"], "B": ["s1:L2", "s1:L4"]}
    aggregate["groups"][0]["filter"]["equals"] = "B"
    assert numeric_fixture(corpus, source, aggregate)[2]["A"] == []


@pytest.mark.parametrize("include_hidden", [False, True])
def test_numeric_locator_xlsx_uses_exact_sheet_and_hidden_policy(corpus, include_hidden):
    lines = ["확정 시트: 항목,금액", "확정 시트: A,100", "확정 시트: B,900", "예상 시트: A,777"]
    source = Source(
        id="s1",
        filename="source.xlsx",
        text="\n".join(lines),
        locators={f"s1:L{i}": line for i, line in enumerate(lines, 1)},
        fixture={
            "sheets": {
                "확정": [["항목", "금액"], ["A", 100], ["B", 900]],
                "예상": [["항목", "금액"], ["A", 777]],
            },
            "hidden_rows": {"확정": [3]},
        },
    )
    aggregate = {
        "format": "xlsx",
        "unit": "원",
        "policy": {},
        "groups": [],
        "selection": {
            "cell_range": "A1:B3",
            "sheet": "확정",
            "group_by": None,
            "include_hidden_rows": include_hidden,
        },
        "columns": {
            "금액": {
                "sum": "1000" if include_hidden else "100",
                "count": 2 if include_hidden else 1,
                "locator": "xlsx:%ED%99%95%EC%A0%95!B2:B3",
            }
        },
    }
    assert numeric_fixture(corpus, source, aggregate)[2] == {
        "total": ["s1:L1", "s1:L2", "s1:L3"] if include_hidden else ["s1:L1", "s1:L2"]
    }
    aggregate["columns"]["금액"]["locator"] = "xlsx:%EC%98%88%EC%83%81!B2:B3"
    assert numeric_fixture(corpus, source, aggregate)[2] == {"total": []}


def test_numeric_locator_ambiguous_xlsx_description_remains_unannotated(corpus):
    source = Source(
        id="s1",
        filename="source.xlsx",
        text="항목,금액\nA,10\nA,10",
        locators={"s1:L1": "항목,금액", "s1:L2": "A,10", "s1:L3": "A,10"},
        fixture={"sheets": {"자료": [["항목", "금액"], ["A", 10], ["A", 10]]}},
    )
    aggregate = {
        "format": "xlsx",
        "unit": "원",
        "policy": {},
        "groups": [],
        "selection": {
            "cell_range": "A1:B3",
            "sheet": "자료",
            "group_by": None,
            "include_hidden_rows": False,
        },
        "columns": {"금액": {"sum": "20", "count": 2, "locator": "xlsx:%EC%9E%90%EB%A3%8C!B2:B3"}},
    }
    numbers, units, locations = numeric_fixture(corpus, source, aggregate)
    assert numbers == {"total": "20"} and units == {"total": "원"}
    assert locations == {"total": []}


def test_numeric_locator_ratio_uses_both_actual_value_columns(corpus):
    lines = ["항목,완료,전체", "A,1,", "B,,4"]
    source = Source(
        id="s1",
        filename="source.csv",
        text="\n".join(lines),
        locators={f"s1:L{i}": line for i, line in enumerate(lines, 1)},
    )
    aggregate = {
        "format": "csv",
        "unit": "건",
        "groups": [],
        "selection": {
            "cell_range": "A1:C3",
            "sheet": None,
            "delimiter": ",",
            "group_by": None,
            "include_hidden_rows": False,
        },
        "policy": {"ratio": {"numerator": "완료", "denominator": "전체"}},
        "ratio": {"value": "0.25"},
        "columns": {
            "완료": {"sum": "1", "count": 1, "locator": "csv:range:B2:B3"},
            "전체": {"sum": "4", "count": 1, "locator": "csv:range:C2:C3"},
        },
    }
    numbers, _, locations = numeric_fixture(corpus, source, aggregate)
    assert numbers == {"완료": "1", "전체": "4", "비율": "0.25"}
    assert locations == {
        "완료": ["s1:L1", "s1:L2"],
        "전체": ["s1:L1", "s1:L3"],
        "비율": ["s1:L1", "s1:L2", "s1:L3"],
    }
