import io
import zipfile

import pytest
from docx import Document
from openpyxl import Workbook, load_workbook
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from birkin_lite import office
from birkin_lite.artifacts import import_source, open_artifact
from birkin_lite.contracts import Actor, AppError, RunRequest
from birkin_lite.db import create_run, init_db
from birkin_lite.sandbox import parse_file

OWNER = Actor(id="owner", project_ids=["sales"])


def setup_source(tmp_path, data, filename, output_format="docx"):
    home, conn = tmp_path / "files", init_db(tmp_path / "state.db")
    source = import_source(home, conn, io.BytesIO(data), {"filename": filename}, OWNER, "sales")
    run = create_run(
        conn,
        OWNER,
        "ui",
        "request",
        RunRequest(
            request="문서 작업",
            project_id="sales",
            artifact_ids=[source["id"]],
            output_format=output_format,
        ),
    )
    return source, {"home": home, "conn": conn, "actor": OWNER, "run": run}


def book_bytes():
    book = Workbook()
    sheet = book.active
    sheet.title = "매출! Q1"
    sheet.append(["group", "amount", "base"])
    sheet.append(["영업", "0.10", 0])
    sheet.append(["영업", "0.20", 0])
    sheet.row_dimensions[3].hidden = True
    sheet.append(["영업", "=SUM(B2:B3)", 0])
    output = io.BytesIO()
    book.save(output)
    book.close()
    result = io.BytesIO()
    with zipfile.ZipFile(output) as source, zipfile.ZipFile(result, "w") as target:
        for item in source.infolist():
            data = source.read(item)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = data.replace(b'dimension ref="A1:C4"', b'dimension ref="A1:A1"')
                data = data.replace(b"<f>SUM(B2:B3)</f><v></v>", b"<f>SUM(B2:B3)</f><v>999</v>")
            target.writestr(item, data)
    return result.getvalue()


def test_csv_decimal_blank_mixed_counts_ratio_and_rounding(tmp_path):
    source, context = setup_source(
        tmp_path, b"group,amount,base\nA,0.10,0\nA,0.20,0\nB,,0\nB,NaN,0\n", "values.csv"
    )
    result = office.aggregate(
        source["id"],
        None,
        None,
        "group",
        ["amount", "base"],
        context,
        mixed_policy="skip",
        unit="KRW",
        ratio={"numerator": "amount", "denominator": "base"},
    )
    assert result["columns"]["amount"]["sum"] == "0.30"
    assert result["columns"]["amount"]["count"] == 2
    assert result["columns"]["amount"]["blank"] == 1
    assert result["columns"]["amount"]["invalid"] == 1
    assert result["columns"]["amount"]["locator"] == "csv:range:B2:B5"
    assert result["selection"]["cell_range"] == "A1:C5"
    assert result["groups"][0]["filter"] == {"column": "group", "equals": "A"}
    assert result["ratio"] == {"value": None, "reason": "zero_denominator"}
    with pytest.raises(AppError) as mixed:
        office.aggregate(source["id"], None, None, None, ["amount"], context)
    assert mixed.value.code == "MISSING_INPUT"
    with pytest.raises(AppError) as blank:
        office.aggregate(
            source["id"], None, "A1:C4", None, ["amount"], context, blank_policy="error"
        )
    assert blank.value.code == "MISSING_INPUT"
    context["conn"].close()


def test_number_accepts_strict_thousands_groups_without_guessing_decimal_commas():
    from decimal import Decimal

    for text, expected in (("1,200", "1200"), (" -12,345,678.90 ", "-12345678.90")):
        assert office._number(text) is None
        assert office._number(text, thousands_separator=",") == Decimal(expected)
    for text in ("1,2", "12,34", "1234,567", "1,234,56", "1.234,50", "1,200원", "1,,200"):
        assert office._number(text) is None
        assert office._number(text, thousands_separator=",") is None
    assert office._number("0,125") is None
    assert office._number("0,125", thousands_separator=",") == Decimal(125)


def test_long_decimal_near_half_is_not_rounded_before_sum(tmp_path):
    raw = ("amount\n0.004" + "9" * 100 + "\n").encode()
    source, context = setup_source(tmp_path, raw, "precision.csv")
    result = office.aggregate(source["id"], None, None, None, ["amount"], context)
    assert result["columns"]["amount"]["sum"] == "0.00"
    context["conn"].close()


def test_xlsx_hidden_rows_dimensions_formula_cache_and_locator(tmp_path):
    source, context = setup_source(tmp_path, book_bytes(), "sheet.xlsx")
    result = office.aggregate(source["id"], "매출! Q1", "A1:C3", "group", ["amount"], context)
    assert result["columns"]["amount"]["sum"] == "0.10"
    assert result["row_counts"]["hidden_excluded"] == 1
    both = office.aggregate(
        source["id"], "매출! Q1", "A1:C3", "group", ["amount"], context, include_hidden_rows=True
    )
    assert both["columns"]["amount"]["sum"] == "0.30"
    formula = office.aggregate(source["id"], "매출! Q1", None, "group", ["amount"], context)
    assert formula["verification"] == {"numeric": "needs_input", "formula_cache": "unverified"}
    assert formula["columns"]["amount"]["formula"] == 1
    assert formula["columns"]["amount"]["sum"] == "0.10"
    evidence = office.locate(source["id"], "xlsx:%EB%A7%A4%EC%B6%9C%21%20Q1!B4", context)
    assert evidence.quote == "[unverified formula] =SUM(B2:B3)"
    context["conn"].close()


def test_formula_group_is_not_a_certified_group_key(tmp_path):
    book = Workbook()
    book.active.append(["group", "amount"])
    book.active.append(["=1+1", 100])
    data = io.BytesIO()
    book.save(data)
    source, context = setup_source(tmp_path, data.getvalue(), "groups.xlsx")
    result = office.aggregate(source["id"], None, None, "group", ["amount"], context)
    assert result["verification"]["numeric"] == "needs_input"
    assert result["groups"] == []
    context["conn"].close()


def test_exact_text_chunks_csv_multiline_and_search_after_first_hundred(tmp_path):
    text = "가" * 16001
    source, context = setup_source(tmp_path, text.encode(), "large.txt")
    with pytest.raises(AppError) as limit:
        office.extract(source["id"], {"max_spans": 1}, {}, context)
    assert limit.value.code == "CONTEXT_LIMIT"
    spans = office.extract(source["id"], {"max_spans": 10}, {}, context)
    assert "".join(span.quote for span in spans) == text
    assert office.locate(source["id"], spans[-1].locator, context).quote == spans[-1].quote
    late = import_source(
        context["home"],
        context["conn"],
        io.BytesIO(("ordinary\n" * 150 + "needle\nneedle\n").encode()),
        {"filename": "late.txt"},
        OWNER,
        "sales",
    )
    found = office.search([late["id"]], "needle", context, limit=1)
    assert found["items"][0]["locator"] == "text:line:151"
    assert found["has_more"] is True
    table = import_source(
        context["home"],
        context["conn"],
        io.BytesIO('name,amount\r\n"가\n나",0.10\r\nA,0.20\r\n'.encode()),
        {"filename": "multiline.csv"},
        OWNER,
        "sales",
    )
    assert office.locate(table["id"], "csv:row:3:col:2", context).quote == "0.20"
    context["conn"].close()


@pytest.mark.parametrize("fmt", ["docx", "xlsx", "csv", "md", "text"])
def test_fixed_templates_reopen_structure_and_formula_strings(tmp_path, fmt):
    source, context = setup_source(tmp_path, "근거입니다".encode(), "source.txt", fmt)
    content = {
        "title": "한국어 보고서",
        "paragraphs": ["수치는 확인했습니다."],
        "bullets": ["담당자 미정"],
        "tables": [
            {"headers": ["항목", "값"], "rows": [["서울", "0.30"], ["수식 문자열", "=1+1"]]}
        ],
    }
    if fmt in {"xlsx", "csv"}:
        content.update(title="", paragraphs=[], bullets=[])
    evidence = office.locate(source["id"], "text:line:1", context)
    if fmt == "md":
        # Search may discover an authorized source absent from request attachments.
        context["run"]["request_json"]["artifact_ids"] = []
    draft = office.create_document(
        "table-v1" if fmt in {"xlsx", "csv"} else "office-report-v1", content, [evidence], context
    )
    provenance = draft["provenance_json"]
    assert provenance["source_versions"] == {source["id"]: source["sha256"]}
    assert provenance["visual_verified"] is False and provenance["structure_verified"] is True
    result = parse_file(
        context["home"] / draft["relative_path"],
        provenance["filename"],
        operation="structure",
        args={"expected": provenance["expected_structure"]},
    )
    assert result["status"] == "pass"
    wrong = {**provenance["expected_structure"], "tables": [{"headers": ["x"], "rows": []}]}
    failed = parse_file(
        context["home"] / draft["relative_path"],
        provenance["filename"],
        operation="structure",
        args={"expected": wrong},
    )
    assert failed["status"] == "fail"
    with open_artifact(context["home"], context["conn"], draft["id"], OWNER, "sales") as handle:
        if fmt == "xlsx":
            book = load_workbook(handle, data_only=False)
            assert book.active["B3"].data_type == "s"
            assert book.active["B3"].value == "=1+1"
            book.close()
        elif fmt == "csv":
            assert "'=1+1" in handle.read().decode("utf-8-sig")
        elif fmt == "docx":
            document = Document(handle)
            assert document.paragraphs[0].text == "한국어 보고서"
            assert document.tables[0].cell(1, 1).text == "0.30"
    context["conn"].close()


def pdf_bytes(blank=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    if not blank:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 10 10 Td (quarterly report) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def test_pdf_text_is_locatable_empty_text_is_explicitly_unavailable(tmp_path):
    source, context = setup_source(tmp_path, pdf_bytes(), "text.pdf")
    evidence = office.locate(source["id"], "pdf:page:1", context)
    assert "quarterly report" in evidence.quote
    context["conn"].close()
    # Extraction requires this PDF to be explicitly selected by its own run.
    empty, context = setup_source(tmp_path / "empty", pdf_bytes(blank=True), "scan.pdf")
    with pytest.raises(AppError) as error:
        office.extract(empty["id"], {}, {}, context)
    assert error.value.code == "OCR_UNAVAILABLE"
    context["conn"].close()


def test_document_scope_and_wrong_quote_fail_before_draft(tmp_path):
    document = Document()
    document.add_paragraph("본문")
    document.sections[0].header.paragraphs[0].text = "헤더 근거"
    data = io.BytesIO()
    document.save(data)
    source, context = setup_source(tmp_path, data.getvalue(), "header.docx")
    with pytest.raises(AppError) as error:
        office.extract(source["id"], {}, {}, context)
    assert error.value.code == "UNSUPPORTED_CAPABILITY"
    context["conn"].close()


def test_inspect_headers_and_search_match_across_previous_chunk_boundary(tmp_path):
    source, context = setup_source(tmp_path, b"group,amount\nA,0.10\n", "values.csv")
    inspected = parse_file(
        context["home"] / source["relative_path"], "values.csv", operation="inspect"
    )
    assert inspected["headers"] == ["group", "amount"]
    assert inspected["sample_types"]["amount"] == {"kinds": ["number"], "sampled_rows": 1}
    boundary = "x" * 3998 + "Needle across boundary" + "y" * 1000
    other = import_source(
        context["home"],
        context["conn"],
        io.BytesIO(boundary.encode()),
        {"filename": "boundary.txt"},
        OWNER,
        "sales",
    )
    result = office.search([other["id"]], "needle across", context)
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert "Needle across" in item["quote"]
    assert office.locate(other["id"], item["locator"], context).quote == item["quote"]
    context["conn"].close()


def test_search_in_second_worksheet_and_strict_content_controls(tmp_path):
    book = Workbook()
    book.active.title = "first"
    book.active.append(["ordinary"])
    book.create_sheet("second! 시트").append(["needle"])
    data = io.BytesIO()
    book.save(data)
    source, context = setup_source(tmp_path, data.getvalue(), "two.xlsx")
    result = office.search([source["id"]], "needle", context)
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert "second%21%20" in item["locator"]
    assert office.locate(source["id"], item["locator"], context).quote == "needle"
    with pytest.raises(AppError) as invalid:
        office.create_document("office-report-v1", {"title": "bad\x00text"}, [], context)
    assert invalid.value.code == "INVALID_INPUT"
    context["conn"].close()


@pytest.mark.parametrize("nested", [False, True], ids=["foreign-namespace", "nested-row"])
def test_xlsx_hidden_row_spoof_cannot_remove_visible_values(tmp_path, nested):
    book = Workbook()
    book.active.append(["amount"])
    book.active.append([100])
    original = io.BytesIO()
    book.save(original)
    book.close()
    spoof = (
        b'<evil:wrapper xmlns:evil="urn:fixture"><row r="2" hidden="1"/></evil:wrapper>'
        if nested
        else b'<evil:row xmlns:evil="urn:fixture" r="2" hidden="1"/>'
    )
    modified = io.BytesIO()
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(modified, "w") as target:
        for entry in source.infolist():
            data = source.read(entry)
            if entry.filename == "xl/worksheets/sheet1.xml":
                data = data.replace(b"</sheetData>", spoof + b"</sheetData>")
            target.writestr(entry, data)
    source, context = setup_source(tmp_path, modified.getvalue(), "hidden-spoof.xlsx")
    try:
        if nested:
            with pytest.raises(AppError) as error:
                office.aggregate(source["id"], None, None, None, ["amount"], context)
            assert error.value.code == "INVALID_INPUT"
        else:
            result = office.aggregate(source["id"], None, None, None, ["amount"], context)
            assert result["columns"]["amount"]["sum"] == "100.00"
            assert result["row_counts"]["hidden_excluded"] == 0
    finally:
        context["conn"].close()


def test_array_formula_evidence_is_deterministic_and_relocatable(tmp_path):
    from openpyxl.worksheet.formula import ArrayFormula

    book = Workbook()
    book.active["A1"] = ArrayFormula(ref="A1", text="=1+1")
    data = io.BytesIO()
    book.save(data)
    book.close()
    source, context = setup_source(tmp_path, data.getvalue(), "array.xlsx")
    try:
        first = office.extract(source["id"], {}, {}, context)
        second = office.extract(source["id"], {}, {}, context)
        assert len(first) == len(second) == 1
        assert first[0].quote == second[0].quote == "[unverified formula] =1+1"
        actual = office.locate(source["id"], first[0].locator, context)
        assert actual.quote == first[0].quote
        assert actual.sha256 == first[0].sha256
    finally:
        context["conn"].close()


def test_mixed_boolean_and_text_group_labels_are_rejected(tmp_path):
    book = Workbook()
    book.active.append(["group", "amount"])
    book.active.append([True, 10])
    book.active.append(["True", 20])
    data = io.BytesIO()
    book.save(data)
    book.close()
    source, context = setup_source(tmp_path, data.getvalue(), "mixed-groups.xlsx")
    try:
        with pytest.raises(AppError) as error:
            office.aggregate(source["id"], None, None, "group", ["amount"], context)
        assert error.value.code == "INVALID_INPUT"
    finally:
        context["conn"].close()


def test_docx_content_control_paragraph_and_table_are_explicitly_refused(tmp_path):
    from docx.oxml import OxmlElement

    document = Document()
    document.add_paragraph("ordinary")
    paragraph = document.add_paragraph("CONTROL PARAGRAPH")
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "CONTROL TABLE"
    control = OxmlElement("w:sdt")
    content = OxmlElement("w:sdtContent")
    control.append(content)
    for element in (paragraph._p, table._tbl):
        element.getparent().remove(element)
        content.append(element)
    document.element.body.insert(1, control)
    data = io.BytesIO()
    document.save(data)
    source, context = setup_source(tmp_path, data.getvalue(), "control.docx")
    try:
        with pytest.raises(AppError) as error:
            office.extract(source["id"], {}, {}, context)
        assert error.value.code == "UNSUPPORTED_CAPABILITY"
    finally:
        context["conn"].close()


def test_docx_omitted_grid_column_locator_matches_actual_cell(tmp_path):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    document = Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Header A"
    table.cell(0, 1).text = "Header B"
    table.cell(1, 1).text = "VALUE AT B2"
    row = table.rows[1]._tr
    row.remove(row.tc_lst[0])
    before = OxmlElement("w:gridBefore")
    before.set(qn("w:val"), "1")
    row.get_or_add_trPr().append(before)
    data = io.BytesIO()
    document.save(data)
    source, context = setup_source(tmp_path, data.getvalue(), "omitted-grid.docx")
    try:
        evidence = office.extract(source["id"], {}, {}, context)
        match = next(item for item in evidence if item.quote == "VALUE AT B2")
        assert match.locator == "docx:t:0:r:1:c:1"
        assert office.locate(source["id"], match.locator, context).quote == "VALUE AT B2"
    finally:
        context["conn"].close()


@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
@pytest.mark.parametrize("field", ["title", "paragraphs", "bullets"])
def test_table_formats_reject_prose_instead_of_discarding_it(tmp_path, fmt, field):
    _, context = setup_source(tmp_path, b"source", "source.txt", fmt)
    content = {"tables": [{"headers": ["amount"], "rows": [[100]]}]}
    content[field] = "must survive" if field == "title" else ["must survive"]
    try:
        with pytest.raises(AppError) as error:
            office.create_document("table-v1", content, [], context)
        assert error.value.code == "INVALID_INPUT"
        assert (
            context["conn"]
            .execute("SELECT COUNT(*) FROM artifacts WHERE kind='draft'")
            .fetchone()[0]
            == 0
        )
    finally:
        context["conn"].close()
