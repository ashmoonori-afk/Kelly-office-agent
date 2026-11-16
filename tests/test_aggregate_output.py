"""Aggregate-to-document checks and one real CSV/XLSX generation regression."""

import csv
import io
from copy import deepcopy

import pytest

from kelly.tools import _content_from_input

FORMATS = ["text", "md", "csv", "xlsx", "docx"]


def aggregate(grouped):
    return {
        "columns": {"영업이익": {"sum": "10.00"}, "매출": {"sum": "30.00"}},
        "groups": [
            {"key": "A", "sums": {"영업이익": "4.00", "매출": "12.00"}},
            {"key": "B", "sums": {"영업이익": "6.00", "매출": "18.00"}},
        ]
        if grouped
        else [],
        "unit": "원",
        "row_counts": {"included": 3},
    }


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("grouped", [False, True])
def test_no_ratio_document_content_preserves_data_and_declared_unit(fmt, grouped):
    value = aggregate(grouped)
    expected = {
        "title": "" if fmt in {"csv", "xlsx"} else "집계 결과",
        "paragraphs": [] if fmt in {"csv", "xlsx"} else ["단위: 원", "포함 행: 3"],
        "bullets": [],
        "tables": [
            {
                "headers": ["그룹", "영업이익 (원)", "매출 (원)"]
                if fmt in {"csv", "xlsx"}
                else ["그룹", "영업이익", "매출"],
                "rows": [["A", "4.00", "12.00"], ["B", "6.00", "18.00"]]
                if grouped
                else [["전체", "10.00", "30.00"]],
            }
        ],
    }
    assert _content_from_input(value, fmt).model_dump() == expected
    value["ratio"] = None
    assert _content_from_input(value, fmt).model_dump() == expected


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize(
    "ratio,expected",
    [
        ({"value": "0.333333", "reason": None}, "0.333333"),
        ({"value": "0.00", "reason": None}, "0.00"),
        ({"value": None, "reason": "zero_denominator"}, "미정 (분모가 0)"),
    ],
)
def test_global_ratio_is_explicit_and_only_shown_once(fmt, grouped, ratio, expected):
    value = aggregate(grouped)
    baseline = _content_from_input(value, fmt)
    value.update(
        ratio=ratio,
        policy={"ratio": {"numerator": "영업이익", "denominator": "매출"}},
    )
    original = deepcopy(value)
    content = _content_from_input(value, fmt)
    assert value == original
    assert len(content.tables) == 1
    table = content.tables[0]
    assert table.headers == [*baseline.tables[0].headers, "전체 비율 (영업이익/매출)"]
    assert [row[:-1] for row in table.rows] == baseline.tables[0].rows
    assert [row[-1] for row in table.rows] == [expected, *([""] if grouped else [])]
    assert content.title == baseline.title
    assert content.paragraphs == baseline.paragraphs


@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
def test_unspecified_unit_is_not_invented(fmt):
    value = aggregate(False)
    value["unit"] = "unspecified"
    assert _content_from_input(value, fmt).tables[0].headers == ["그룹", "영업이익", "매출"]


def test_declared_units_survive_actual_csv_and_xlsx_creation_and_reopening(tmp_path):
    from openpyxl import load_workbook

    from kelly.artifacts import open_artifact
    from kelly.contracts import Actor, RunRequest
    from kelly.db import create_run, init_db
    from kelly.office import create_document

    actor = Actor(id="owner", project_ids=["sales"])
    home = tmp_path / "files"
    conn = init_db(tmp_path / "state.db")
    value = aggregate(True)
    value.update(
        ratio={"value": "0.333333", "reason": None},
        policy={"ratio": {"numerator": "영업이익", "denominator": "매출"}},
    )
    expected = [
        ["그룹", "영업이익 (원)", "매출 (원)", "전체 비율 (영업이익/매출)"],
        ["A", "4.00", "12.00", "0.333333"],
        ["B", "6.00", "18.00", ""],
    ]
    try:
        for fmt in ("csv", "xlsx"):
            run = create_run(
                conn,
                actor,
                "ui",
                fmt,
                RunRequest(
                    request="단위와 전체 비율을 포함한 집계표",
                    project_id="sales",
                    output_format=fmt,
                ),
            )
            draft = create_document(
                "table-v1",
                _content_from_input(value, fmt),
                [],
                {"home": home, "conn": conn, "actor": actor, "run": run},
            )
            with open_artifact(home, conn, draft["id"], actor, "sales") as handle:
                if fmt == "csv":
                    rows = list(csv.reader(io.StringIO(handle.read().decode("utf-8-sig"))))
                else:
                    book = load_workbook(handle, read_only=True, data_only=False)
                    try:
                        assert book.sheetnames == ["Result"]
                        rows = [
                            ["" if cell is None else cell for cell in row]
                            for row in book.active.values
                        ]
                    finally:
                        book.close()
            assert rows == expected
    finally:
        conn.close()
