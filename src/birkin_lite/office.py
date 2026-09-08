"""Bounded Office reads in the parser worker; deterministic drafts in the parent."""

import csv
import io
import math
import re
import unicodedata
import zipfile
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Literal
from urllib.parse import quote as urlquote

from pydantic import Field, model_validator

from . import artifacts
from .contracts import AppError, DocumentContent, Evidence, StrictModel, digest
from .db import allowed_sources, utc_now
from .sandbox import parse_file

EXTRACTION_VERSION = "office-extract-v1"
FORMULA_PREFIX = "[unverified formula] "


class ExtractProjection(StrictModel):
    sheet: str | None = Field(default=None, max_length=128)
    cell_range: str | None = Field(default=None, max_length=64)
    offset: int = Field(default=0, ge=0, le=2_000_000)
    max_spans: int = Field(default=6, ge=1, le=100)
    page: bool = False


class ExtractLimits(StrictModel):
    max_chars_per_span: int = Field(default=4000, ge=1, le=16000)


class Ratio(StrictModel):
    numerator: str = Field(min_length=1, max_length=128)
    denominator: str = Field(min_length=1, max_length=128)


class AggregatePolicy(StrictModel):
    unit: str = Field(default="unspecified", min_length=1, max_length=64)
    decimal_places: int = Field(default=2, ge=0, le=6)
    rounding: str = "ROUND_HALF_UP"
    blank_policy: str = "skip"
    mixed_policy: str = "error"
    include_hidden_rows: bool = False
    delimiter: str = ","
    thousands_separator: Literal[","] | None = None
    ratio: Ratio | None = None

    @model_validator(mode="after")
    def known_policies(self):
        if (
            self.rounding not in {"ROUND_HALF_UP", "ROUND_HALF_EVEN"}
            or self.blank_policy not in {"skip", "error"}
            or self.mixed_policy not in {"skip", "error"}
            or self.delimiter != ","
        ):
            raise ValueError("Unsupported numeric or CSV dialect policy")
        return self


def _validated(model, value):
    from pydantic import ValidationError

    try:
        return model.model_validate(value)
    except ValidationError:
        raise AppError("INVALID_INPUT") from None


def _display(value):
    from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

    if isinstance(value, ArrayFormula):
        if not isinstance(value.text, str):
            raise AppError("UNSUPPORTED_CAPABILITY")
        return value.text
    if isinstance(value, DataTableFormula):
        raise AppError("UNSUPPORTED_CAPABILITY")
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AppError("INVALID_INPUT")
        return str(value)
    return str(value)


def _range(value):
    if value is None:
        return 1, 1, None, None
    from openpyxl.utils.cell import range_boundaries

    try:
        minimum_column, minimum_row, maximum_column, maximum_row = range_boundaries(value)
        if (
            None in (minimum_column, minimum_row, maximum_column, maximum_row)
            or not 1 <= minimum_column <= maximum_column <= 256
            or not 1 <= minimum_row <= maximum_row <= 1_048_576
            or (maximum_column - minimum_column + 1) * (maximum_row - minimum_row + 1) > 2_000_000
        ):
            raise ValueError()
        return minimum_column, minimum_row, maximum_column, maximum_row
    except (ValueError, TypeError):
        raise AppError("INVALID_INPUT") from None


def _rows(path, fmt, args):
    """Yield selected logical row, (column,value,is_formula) cells, hidden flag."""
    from openpyxl import load_workbook

    minimum_column, minimum_row, maximum_column, maximum_row = _range(args.get("cell_range"))
    count = 0
    if fmt == "csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            csv.field_size_limit(1024 * 1024)
            for number, values in enumerate(csv.reader(stream, strict=True), 1):
                if maximum_row is not None and number > maximum_row:
                    break
                if number < minimum_row:
                    continue
                width = maximum_column if maximum_column is not None else len(values)
                if width > 256 or number > 1_048_576:
                    raise AppError("LIMIT_EXCEEDED")
                cells = [
                    (column, values[column - 1] if column <= len(values) else None, False)
                    for column in range(minimum_column, width + 1)
                ]
                count += len(cells)
                if count > 2_000_000:
                    raise AppError("LIMIT_EXCEEDED")
                yield number, cells, False
        return
    if fmt != "xlsx":
        raise AppError("UNSUPPORTED_CAPABILITY")
    with path.open("rb") as stream:
        workbook = load_workbook(stream, read_only=True, data_only=False, keep_links=False)
        try:
            sheet = args.get("sheet")
            if sheet is None:
                if len(workbook.sheetnames) != 1:
                    raise AppError("MISSING_INPUT")
                sheet = workbook.sheetnames[0]
            if sheet not in workbook.sheetnames:
                raise AppError("INVALID_INPUT")
            worksheet = workbook[sheet]
            worksheet.reset_dimensions()  # Package dimension metadata can lie about populated cells.
            hidden = set()
            from defusedxml.ElementTree import iterparse

            namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            row_path = [namespace + tag for tag in ("worksheet", "sheetData", "row")]
            stack, previous_row = [], 0
            with zipfile.ZipFile(path) as package, package.open(worksheet._worksheet_path) as xml:
                for event, element in iterparse(xml, events=("start", "end"), forbid_dtd=True):
                    if event == "start":
                        stack.append(element.tag)
                        continue
                    if element.tag == namespace + "row":
                        if stack != row_path:
                            raise AppError("INVALID_INPUT")
                        number = int(element.attrib.get("r", previous_row + 1))
                        if not previous_row < number <= 1_048_576:
                            raise AppError("INVALID_INPUT")
                        previous_row = number
                        if element.attrib.get("hidden", "").lower() in {"1", "true"}:
                            hidden.add(number)
                        element.clear()
                    stack.pop()
            for number, values in enumerate(
                worksheet.iter_rows(
                    min_row=minimum_row,
                    max_row=maximum_row,
                    min_col=minimum_column,
                    max_col=maximum_column,
                ),
                minimum_row,
            ):
                cells = [
                    (column, cell.value, cell.data_type == "f")
                    for column, cell in enumerate(values, minimum_column)
                ]
                count += len(cells)
                if len(cells) > 256 or count > 2_000_000:
                    raise AppError("LIMIT_EXCEEDED")
                yield number, cells, number in hidden
        finally:
            workbook.close()


def _sheet_name(path, supplied):
    if supplied is not None:
        return supplied
    names = _sheet_names(path)
    if len(names) != 1:
        raise AppError("MISSING_INPUT")
    return names[0]


def _sheet_names(path):
    from openpyxl import load_workbook

    with path.open("rb") as stream:
        workbook = load_workbook(stream, read_only=True, keep_links=False)
        try:
            if len(workbook.sheetnames) > 100:
                raise AppError("LIMIT_EXCEEDED")
            return workbook.sheetnames
        finally:
            workbook.close()


def _units(path, args):
    fmt = args["format"]
    if fmt in {"txt", "md"}:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for number, line in enumerate(stream, 1):
                yield f"text:line:{number}", line.rstrip("\r\n"), False
    elif fmt in {"csv", "xlsx"}:
        from openpyxl.utils.cell import get_column_letter

        sheets = (
            [None] if fmt == "csv" else [args["sheet"]] if args.get("sheet") else _sheet_names(path)
        )
        count = 0
        for sheet in sheets:
            for number, cells, _ in _rows(path, fmt, {**args, "sheet": sheet}):
                count += len(cells)
                if count > 2_000_000:
                    raise AppError("LIMIT_EXCEEDED")
                for column, value, formula in cells:
                    locator = (
                        f"csv:row:{number}:col:{column}"
                        if fmt == "csv"
                        else f"xlsx:{urlquote(sheet, safe='')}!{get_column_letter(column)}{number}"
                    )
                    text = (FORMULA_PREFIX if formula else "") + _display(value)
                    yield locator, text, formula
    elif fmt == "docx":
        from docx import Document
        from docx.table import Table

        document = Document(path)
        if document.element.xpath(
            ".//w:txbxContent | .//w:ins | .//w:del | .//w:tbl//w:tbl | .//w:sdt"
        ):
            raise AppError("UNSUPPORTED_CAPABILITY")
        with zipfile.ZipFile(path) as package:
            from ._vendor.office.safe_xml import ElementTree

            for name in package.namelist():
                if re.fullmatch(r"word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml", name):
                    xml = ElementTree.fromstring(package.read(name))
                    if any(item.text for item in xml.iter() if item.tag.endswith("}t")):
                        raise AppError("UNSUPPORTED_CAPABILITY")
        paragraph_index = table_index = 0
        for block in document.iter_inner_content():
            if isinstance(block, Table):
                for row_index, row in enumerate(block.rows):
                    for column_index, cell in enumerate(row.cells, row.grid_cols_before):
                        yield (
                            f"docx:t:{table_index}:r:{row_index}:c:{column_index}",
                            cell.text,
                            False,
                        )
                table_index += 1
            else:
                yield f"docx:p:{paragraph_index}", block.text, False
                paragraph_index += 1
    elif fmt == "pdf":
        from pypdf import PdfReader

        with path.open("rb") as stream:
            reader = PdfReader(stream, strict=True)
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text()
                if not text.strip():
                    raise AppError("OCR_UNAVAILABLE")
                yield f"pdf:page:{number}", text, False
    else:
        raise AppError("UNSUPPORTED_CAPABILITY")


def _spans(path, args):
    maximum = args.get("max_chars_per_span", 4000)
    if type(maximum) is not int or not 1 <= maximum <= 16000:
        raise AppError("INVALID_INPUT")
    for locator, text, formula in _units(path, args):
        if len(text) <= maximum:
            yield {"locator": locator, "quote": text, "unverified": formula}
        else:
            for start in range(0, len(text), maximum):
                end = min(start + maximum, len(text))
                yield {
                    "locator": f"{locator}:chars:{start}:{end}",
                    "quote": text[start:end],
                    "unverified": formula,
                }


def _extract_worker(path, args):
    offset, maximum = args.get("offset", 0), args.get("max_spans", 6)
    if type(offset) is not int or offset < 0 or type(maximum) is not int or not 1 <= maximum <= 100:
        raise AppError("INVALID_INPUT")
    selected = []
    for index, span in enumerate(_spans(path, args)):
        if index < offset:
            continue
        selected.append(span)
        if len(selected) > maximum:
            return {"spans": selected[:-1], "has_more": True, "next_offset": offset + maximum}
    return {"spans": selected, "has_more": False, "next_offset": None}


def _locate_worker(path, args):
    locator = args.get("locator")
    if not isinstance(locator, str) or len(locator) > 512:
        raise AppError("INVALID_INPUT")
    match = re.fullmatch(r"(.+):chars:(\d+):(\d+)", locator)
    base = match[1] if match else locator
    for unit_locator, text, formula in _units(path, args):
        if unit_locator == base:
            if match:
                start, end = int(match[2]), int(match[3])
                if not 0 <= start < end <= len(text) or end - start > 16000:
                    raise AppError("INVALID_INPUT")
                text = text[start:end]
            if len(text) > 16000:
                raise AppError("CONTEXT_LIMIT")
            return {"locator": locator, "quote": text, "unverified": formula}
    raise AppError("NOT_FOUND")


def _number(value, *, thousands_separator=None):
    if isinstance(value, (bool, date, datetime)):
        return None
    text = _display(value).strip()
    if len(text) > 128:
        return None
    if thousands_separator == "," and re.fullmatch(
        r"[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]*)?", text
    ):
        text = text.replace(",", "")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() and -28 <= number.adjusted() <= 28 else None


def _aggregate_worker(path, args):
    from openpyxl.utils.cell import get_column_letter

    fmt = args["format"]
    policy = _validated(AggregatePolicy, args.get("policy", {}))
    columns = args.get("value_columns")
    group_by = args.get("group_by")
    if (
        not isinstance(columns, list)
        or not 1 <= len(columns) <= 32
        or any(not isinstance(item, str) or not item for item in columns)
        or len(set(columns)) != len(columns)
        or (group_by is not None and (not isinstance(group_by, str) or not group_by))
    ):
        raise AppError("INVALID_INPUT")
    sheet = _sheet_name(path, args.get("sheet")) if fmt == "xlsx" else None
    rows = iter(_rows(path, fmt, {**args, "sheet": sheet}))
    try:
        header_row, header_cells, _ = next(rows)
    except StopIteration:
        raise AppError("MISSING_INPUT") from None
    headers = [_display(value) for _, value, _ in header_cells]
    if (
        not headers
        or any(not header for header in headers)
        or len(headers) != len(set(headers))
        or any(formula for _, _, formula in header_cells)
        or not set(columns) <= set(headers)
        or (group_by is not None and group_by not in headers)
    ):
        raise AppError("INVALID_INPUT")
    indices = {name: index for index, name in enumerate(headers)}
    if policy.ratio and not {policy.ratio.numerator, policy.ratio.denominator} <= set(columns):
        raise AppError("INVALID_INPUT")
    stats = {
        name: {"sum": Decimal(0), "count": 0, "blank": 0, "invalid": 0, "formula": 0}
        for name in columns
    }
    groups = {}
    group_type = None
    counts = {"selected": 0, "included": 0, "hidden_excluded": 0}
    last_row = header_row
    formula_found = False
    with localcontext() as decimal_context:
        # Input numbers have <=128 characters and exponents are bounded; sums stay exact here.
        decimal_context.prec = 256
        for number, cells, hidden in rows:
            last_row = number
            counts["selected"] += 1
            if hidden and not policy.include_hidden_rows:
                counts["hidden_excluded"] += 1
                continue
            counts["included"] += 1
            if len(cells) < len(headers):
                cells += [
                    (header_cells[index][0], None, False)
                    for index in range(len(cells), len(headers))
                ]
            if len(cells) != len(headers):
                raise AppError("INVALID_INPUT")
            group = None
            if group_by is not None:
                _, group_value, group_formula = cells[indices[group_by]]
                if group_formula:
                    formula_found = True
                    for name in columns:
                        stats[name]["formula"] += 1
                    continue
                if group_value is not None and group_value != "":
                    value_type = (
                        "number"
                        if type(group_value) in {int, float}
                        else type(group_value).__name__
                    )
                    if group_type is not None and value_type != group_type:
                        raise AppError("INVALID_INPUT")
                    group_type = value_type
                key = _display(group_value)
                if key not in groups:
                    if len(groups) >= 1000:
                        raise AppError("LIMIT_EXCEEDED")
                    groups[key] = {
                        "key": key,
                        "row_count": 0,
                        "sums": {name: Decimal(0) for name in columns},
                        "filter": {"column": group_by, "equals": key},
                    }
                group = groups[key]
                group["row_count"] += 1
            for name in columns:
                _, value, formula = cells[indices[name]]
                stat = stats[name]
                if formula:
                    formula_found = True
                    stat["formula"] += 1
                    continue
                if value is None or (isinstance(value, str) and not value.strip()):
                    stat["blank"] += 1
                    if policy.blank_policy == "error":
                        raise AppError("MISSING_INPUT")
                    continue
                numeric = _number(value, thousands_separator=policy.thousands_separator)
                if numeric is None:
                    stat["invalid"] += 1
                    if policy.mixed_policy == "error":
                        raise AppError("MISSING_INPUT")
                    continue
                stat["count"] += 1
                stat["sum"] += numeric
                if group is not None:
                    group["sums"][name] += numeric
        quantum = Decimal(1).scaleb(-policy.decimal_places)
        rounding = ROUND_HALF_UP if policy.rounding == "ROUND_HALF_UP" else ROUND_HALF_EVEN

        def rendered(value):
            return format(value.quantize(quantum, rounding=rounding), "f")

        ratio = None
        if policy.ratio:
            numerator = stats[policy.ratio.numerator]["sum"]
            denominator = stats[policy.ratio.denominator]["sum"]
            ratio = {
                "value": rendered(numerator / denominator) if denominator else None,
                "reason": None if denominator else "zero_denominator",
            }
        for name, stat in stats.items():
            column = header_cells[indices[name]][0]
            letter = get_column_letter(column)
            data_range = f"{letter}{header_row + 1}:{letter}{max(header_row + 1, last_row)}"
            stat["locator"] = (
                f"csv:range:{data_range}"
                if fmt == "csv"
                else f"xlsx:{urlquote(sheet, safe='')}!{data_range}"
            )
            stat["average"] = rendered(stat["sum"] / stat["count"]) if stat["count"] else None
            stat["sum"] = rendered(stat["sum"])
        for group in groups.values():
            group["sums"] = {name: rendered(value) for name, value in group["sums"].items()}
    selected_range = (
        f"{get_column_letter(header_cells[0][0])}{header_row}:"
        f"{get_column_letter(header_cells[-1][0])}{last_row}"
    )
    return {
        "format": fmt,
        "selection": {
            "sheet": sheet,
            "cell_range": selected_range,
            "include_hidden_rows": policy.include_hidden_rows,
            "group_by": group_by,
            "value_columns": columns,
            "delimiter": policy.delimiter,
        },
        "unit": policy.unit,
        "rounding": {"decimal_places": policy.decimal_places, "mode": policy.rounding},
        "row_counts": counts,
        "columns": stats,
        "groups": list(groups.values()),
        "ratio": ratio,
        "verification": {
            "numeric": "needs_input" if formula_found else "pass",
            "formula_cache": "unverified" if formula_found else "not_present",
        },
        "policy": policy.model_dump(),
    }


def worker_operation(operation, path, args):
    if operation == "inspect":
        fmt = args["format"]
        if fmt not in {"csv", "xlsx"}:
            return {}
        sheets = []
        if fmt == "xlsx":
            from openpyxl import load_workbook

            with path.open("rb") as stream:
                workbook = load_workbook(stream, read_only=True, keep_links=False)
                sheets = workbook.sheetnames
                workbook.close()
        selected = args.get("sheet")
        if fmt == "xlsx" and selected is None and len(sheets) == 1:
            selected = sheets[0]
        info = {
            "sheet_names": sheets[:100],
            "has_more_sheets": len(sheets) > 100,
            "sheet": selected,
            "selection_required": fmt == "xlsx" and selected is None,
            "headers": [],
            "header_row": 1,
            "sample_types": {},
            "headers_valid": False,
        }
        if info["selection_required"]:
            return info
        rows = _rows(path, fmt, {**args, "sheet": selected})
        try:
            first = next(rows, None)
            if first is not None:
                info["header_row"] = first[0]
                info["headers"] = [_display(value) for _, value, _ in first[1]]
                info["headers_valid"] = (
                    bool(info["headers"])
                    and all(info["headers"])
                    and len(set(info["headers"])) == len(info["headers"])
                    and not any(formula for _, _, formula in first[1])
                )
                kinds = [set() for _ in first[1]]
                sampled = 0
                for _, cells, _ in rows:
                    sampled += 1
                    for index, (_, value, formula) in enumerate(cells[: len(kinds)]):
                        kind = (
                            "formula"
                            if formula
                            else "blank"
                            if value in {None, ""}
                            else "number"
                            if _number(value) is not None
                            else "text"
                        )
                        kinds[index].add(kind)
                    if sampled == 5:
                        break
                info["sample_types"] = {
                    name: {"kinds": sorted(kinds[index]), "sampled_rows": sampled}
                    for index, name in enumerate(info["headers"])
                }
        finally:
            rows.close()
        return info
    if operation == "structure":
        return _check_structure(
            path.read_bytes(), args["format"], _validated(DocumentContent, args.get("expected"))
        )
    if operation == "extract":
        return _extract_worker(path, args)
    if operation == "locate":
        return _locate_worker(path, args)
    if operation == "aggregate":
        return _aggregate_worker(path, args)
    if operation == "search":
        query, maximum = args.get("query"), args.get("limit", 6)
        if not isinstance(query, str) or not query.strip() or not 1 <= maximum <= 100:
            raise AppError("INVALID_INPUT")
        matches = []
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        for locator, text, formula in _units(path, args):
            match = pattern.search(text)
            if match:
                if len(text) > 4000:
                    start = max(0, match.start() - 1000)
                    end = min(len(text), start + 4000)
                    locator += f":chars:{start}:{end}"
                    text = text[start:end]
                matches.append({"locator": locator, "quote": text, "unverified": formula})
                if len(matches) > maximum:
                    return {"spans": matches[:-1], "has_more": True}
        return {"spans": matches, "has_more": False}
    raise AppError("UNSUPPORTED_CAPABILITY")


def _call(artifact_id, operation, args, context):
    home, conn, actor, run = (context[key] for key in ("home", "conn", "actor", "run"))
    record = artifacts.metadata(conn, artifact_id, actor, run["project_id"])
    # Search is explicitly project-wide; locator verification reads its exact evidence.
    if (
        operation not in {"search", "locate"}
        and artifact_id not in allowed_sources(conn, run, actor)
        and record["run_id"] != run["id"]
    ):
        raise AppError("POLICY_DENIED")
    with artifacts.open_artifact(home, conn, artifact_id, actor, run["project_id"]):
        result = parse_file(
            Path(home).absolute() / record["relative_path"],
            record["provenance_json"]["filename"],
            operation=operation,
            args=args,
        )
    return record, result


def _evidence(record, span):
    text = span["quote"]
    if span.get("unverified") and not text.startswith(FORMULA_PREFIX):
        text = FORMULA_PREFIX + text
    if len(text) > 16000:
        raise AppError("CONTEXT_LIMIT")
    return Evidence(
        artifact_id=record["id"],
        sha256=record["sha256"],
        locator=span["locator"],
        quote=text,
        extraction_version=EXTRACTION_VERSION,
        observed_at=utc_now(),
    )


def extract(artifact_id, projection, limits, context):
    projection = _validated(ExtractProjection, projection or {})
    limits = _validated(ExtractLimits, limits or {})
    record, result = _call(
        artifact_id, "extract", {**projection.model_dump(), **limits.model_dump()}, context
    )
    if result["has_more"] and not projection.page:
        raise AppError("CONTEXT_LIMIT")
    return [_evidence(record, span) for span in result["spans"]]


def locate(artifact_id, locator, context):
    if isinstance(locator, str) and locator.startswith("memory:"):
        from .memory import locate_memory

        return locate_memory(artifact_id, locator, context)
    from urllib.parse import unquote

    args = {"locator": locator, "max_chars_per_span": 16000}
    if isinstance(locator, str) and locator.startswith("xlsx:") and "!" in locator:
        args["sheet"] = unquote(locator[5:].split("!", 1)[0])
    record, result = _call(artifact_id, "locate", args, context)
    return _evidence(record, result)


def aggregate(artifact_id, sheet, cell_range, group_by, value_columns, context, **policy):
    policy = _validated(AggregatePolicy, policy)
    record, result = _call(
        artifact_id,
        "aggregate",
        {
            "sheet": sheet,
            "cell_range": cell_range,
            "group_by": group_by,
            "value_columns": value_columns,
            "policy": policy.model_dump(),
        },
        context,
    )
    result["source"] = {"artifact_id": artifact_id, "sha256": record["sha256"]}
    result.pop("sandbox", None)
    return result


def search(source_ids, query, context, limit=6):
    if (
        not isinstance(source_ids, list)
        or not 1 <= len(source_ids) <= 20
        or len(set(source_ids)) != len(source_ids)
        or type(limit) is not int
        or not 1 <= limit <= 100
        or not isinstance(query, str)
        or not query.strip()
        or len(query) > 500
    ):
        raise AppError("INVALID_INPUT")
    # Validate the complete source scope before returning any partial content.
    for source in source_ids:
        artifacts.metadata(context["conn"], source, context["actor"], context["run"]["project_id"])
    items, has_more = [], False
    for source in source_ids:
        record, result = _call(source, "search", {"query": query, "limit": limit}, context)
        items.extend(_evidence(record, span).model_dump() for span in result["spans"])
        has_more |= result["has_more"] or len(items) > limit
    return {"items": items[:limit], "has_more": has_more}


def _dangerous_cell(text):
    # Retained from Birkin csv_safety._dangerous_trigger, MIT; see THIRD_PARTY.md.
    normalized = unicodedata.normalize("NFKC", text)
    index = 0
    while index < len(normalized) and unicodedata.category(normalized[index])[0] in {"C", "Z"}:
        index += 1
    return index < len(normalized) and normalized[index] in "=+-@"


def _csv_cell(value):
    cell = _display(value)
    return "'" + cell if isinstance(value, str) and _dangerous_cell(cell) else cell


def _plain_text(content):
    lines = [content.title, *content.paragraphs, *("- " + bullet for bullet in content.bullets)]
    for table in content.tables:
        lines.extend(
            "\t".join(_display(cell) for cell in row) for row in [table.headers, *table.rows]
        )
    return "\n\n".join(lines)


def _check_structure(data, fmt, content):
    """Parent uses only its own just-generated bytes; user files call this in the worker."""
    expected_tables = [
        [[_display(value) for value in row] for row in [table.headers, *table.rows]]
        for table in content.tables
    ]
    valid = False
    table_only = not (content.title or content.paragraphs or content.bullets)
    if fmt == "docx":
        from docx import Document

        document = Document(io.BytesIO(data))
        paragraphs = [paragraph.text for paragraph in document.paragraphs]
        expected_paragraphs = (
            ([content.title] if content.title else []) + content.paragraphs + content.bullets
        )
        actual_tables = [
            [[cell.text for cell in row.cells] for row in table.rows] for table in document.tables
        ]
        styles = [paragraph.style.name for paragraph in document.paragraphs]
        expected_styles = (
            (["Title"] if content.title else [])
            + ["Normal"] * len(content.paragraphs)
            + ["List Bullet"] * len(content.bullets)
        )
        valid = (
            paragraphs == expected_paragraphs
            and actual_tables == expected_tables
            and styles == expected_styles
            and all(table.style.name == "Table Grid" for table in document.tables)
        )
    elif fmt == "xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(
            io.BytesIO(data), read_only=True, data_only=False, keep_links=False
        )
        try:
            if workbook.sheetnames == ["Result"]:
                rows = list(workbook.active.rows)
                valid = (
                    table_only
                    and len(expected_tables) == 1
                    and [[_display(cell.value) for cell in row] for row in rows]
                    == expected_tables[0]
                    and not any(cell.data_type == "f" for row in rows for cell in row)
                )
        finally:
            workbook.close()
    elif fmt == "csv":
        actual = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True))
        valid = (
            table_only
            and len(content.tables) == 1
            and actual
            == [
                [_csv_cell(value) for value in row]
                for row in [content.tables[0].headers, *content.tables[0].rows]
            ]
        )
    elif fmt in {"txt", "text", "md"}:
        valid = data.decode("utf-8") == _plain_text(content)
    return {
        "status": "pass" if valid else "fail",
        "errors": [] if valid else ["STRUCTURE_MISMATCH"],
        "checked_layers": ["structure"],
        "visual_verified": False,
    }


def create_document(template_id, content, evidence_refs, context):
    if template_id not in {"office-report-v1", "table-v1"}:
        raise AppError("UNSUPPORTED_CAPABILITY")
    content = _validated(DocumentContent, content)
    run, actor, conn, home = (context[key] for key in ("run", "actor", "conn", "home"))
    source_versions = {}
    for artifact_id in allowed_sources(conn, run, actor):
        record = artifacts.metadata(conn, artifact_id, actor, run["project_id"])
        with artifacts.open_artifact(home, conn, artifact_id, actor, run["project_id"]):
            source_versions[artifact_id] = record["sha256"]
    refs = []
    if not isinstance(evidence_refs, list) or len(evidence_refs) > 100:
        raise AppError("INVALID_INPUT")
    for item in evidence_refs:
        evidence = _validated(Evidence, item.model_dump() if isinstance(item, Evidence) else item)
        actual = locate(evidence.artifact_id, evidence.locator, context)
        if (
            actual.sha256 != evidence.sha256
            or actual.quote != evidence.quote
            or actual.extraction_version != evidence.extraction_version
        ):
            raise AppError("SOURCE_CHANGED")
        source_versions[evidence.artifact_id] = actual.sha256
        if len(source_versions) > 20:
            raise AppError("LIMIT_EXCEEDED")
        refs.append(evidence.model_dump())
    fmt = run["request_json"]["output_format"]
    if template_id == "table-v1" and len(content.tables) != 1:
        raise AppError("INVALID_INPUT")
    if fmt in {"csv", "xlsx"} and (
        len(content.tables) != 1 or content.title or content.paragraphs or content.bullets
    ):
        raise AppError("INVALID_INPUT")
    output = io.BytesIO()
    protected = 0
    if fmt == "docx":
        from docx import Document

        document = Document()
        if content.title:
            document.add_heading(content.title, 0)
        for paragraph in content.paragraphs:
            document.add_paragraph(paragraph)
        for bullet in content.bullets:
            document.add_paragraph(bullet, style="List Bullet")
        for table in content.tables:
            rendered = document.add_table(rows=1, cols=len(table.headers))
            rendered.style = "Table Grid"
            for cell, value in zip(rendered.rows[0].cells, table.headers, strict=True):
                cell.text = value
            for row in table.rows:
                for cell, value in zip(rendered.add_row().cells, row, strict=True):
                    cell.text = _display(value)
        document.save(output)
        reopened = Document(io.BytesIO(output.getvalue()))
        if len(reopened.tables) != len(content.tables):
            raise AppError("INVALID_OUTPUT")
    elif fmt == "xlsx":
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Result"
        table = content.tables[0]
        for row_number, row in enumerate([table.headers, *table.rows], 1):
            for column, value in enumerate(row, 1):
                cell = worksheet.cell(row_number, column, value)
                if isinstance(value, str):
                    cell.data_type = "s"
                    protected += int(_dangerous_cell(value))
        workbook.save(output)
        workbook.close()
    elif fmt == "csv":
        text = io.StringIO(newline="")
        writer = csv.writer(text)
        table = content.tables[0]
        for row in [table.headers, *table.rows]:
            converted = []
            for value in row:
                cell = _display(value)
                if isinstance(value, str) and _dangerous_cell(cell):
                    protected += 1
                    cell = "'" + cell
                converted.append(cell)
            writer.writerow(converted)
        output.write(text.getvalue().encode("utf-8-sig"))
    elif fmt in {"text", "md"}:
        output.write(_plain_text(content).encode("utf-8"))
    else:
        raise AppError("UNSUPPORTED_CAPABILITY")
    if _check_structure(output.getvalue(), fmt, content)["status"] != "pass":
        raise AppError("INVALID_OUTPUT")
    output.seek(0)
    return artifacts.create_draft(
        home,
        conn,
        output,
        "result." + ("txt" if fmt == "text" else fmt),
        actor,
        run["project_id"],
        run["id"],
        provenance={
            "template_id": template_id,
            "template_version": "1",
            "source_versions": source_versions,
            "expected_structure": content.model_dump(),
            "content_hash": digest(content.model_dump()),
            "content_proof": context.get("content_proof"),
            "evidence_refs": refs,
            "structure_verified": True,
            "visual_verified": False,
            "render_status": "RENDER_UNAVAILABLE",
            "protected_formula_strings": protected,
        },
    )
