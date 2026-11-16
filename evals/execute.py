"""Execute frozen C cases through local product boundaries, with no model or network client."""

import argparse
import hashlib
import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from kelly import artifacts, db, engine, planning, tools, verification
from kelly.contracts import Actor, AppError, Receipt, RunRequest, TaskSpec
from kelly.contracts import PrivacyPolicy as Privacy

from .run import Case, Observation, condition_hash, fingerprint, load_cases, read_json, sha

ACTOR = Actor(id="eval-owner", project_ids=["eval"])
POLICY = Privacy(project_id="eval", allowed_providers=[], allow_cloud=False, local_only=True)
ADAPTER_VERSION = "offline-workload-v2"
STOP_STATES = {
    "succeeded",
    "failed",
    "cancelled",
    "outcome_unknown",
    "awaiting_input",
    "awaiting_approval",
    "awaiting_budget",
    "retry_wait",
}


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", "utf-8"
    )


def source_bytes(source):
    """Materialize input fixtures only. Gold answers never enter file or model inputs."""
    suffix = Path(source.filename).suffix.lower()
    if Path(source.filename).name != source.filename:
        raise ValueError("source filename must be a basename")
    if suffix in {".txt", ".md", ".csv"} and not source.fixture:
        return source.text.encode("utf-8")
    if suffix != ".xlsx":
        raise AppError("UNSUPPORTED_CAPABILITY")
    from openpyxl import Workbook

    fixture = source.fixture
    if set(fixture) - {"sheets", "hidden_rows", "formula_cache_verified"} or not fixture.get(
        "sheets"
    ):
        raise AppError("INVALID_INPUT")
    if fixture.get("formula_cache_verified"):
        raise AppError("UNSUPPORTED_CAPABILITY")
    book = Workbook()
    book.remove(book.active)
    try:
        for title, rows in fixture["sheets"].items():
            sheet = book.create_sheet(title)
            for row in rows:
                sheet.append(row)
            for number in fixture.get("hidden_rows", {}).get(title, []):
                sheet.row_dimensions[number].hidden = True
        stream = io.BytesIO()
        book.save(stream)
        return stream.getvalue()
    finally:
        book.close()


def task_spec(case, source_ids):
    """Preserve the original request and explicit fixture conditions; never pass gold to planning."""
    intent = (
        case.inputs.get("intent")
        or {
            "table": "aggregate",
            "extraction": "meeting_actions",
            "summary": "summarize",
            "multi_document": "weekly_report",
            "conflict_missing": "analyze",
        }[case.category]
    )
    if case.category == "conflict_missing" and not source_ids and "요약" in case.request:
        intent = "summarize"
    inputs = {key: value for key, value in case.inputs.items() if key != "intent"}
    if case.category == "table" and len(source_ids) == 1:
        inputs["artifact_id"] = source_ids[0]
    return TaskSpec(
        intent=intent,
        source_ids=source_ids,
        inputs=inputs,
        acceptance=[case.request],
        ambiguity="clear",
    )


def environment_hash():
    directory = Path(engine.__file__).parent
    modules = (
        "artifacts.py",
        "context.py",
        "contracts.py",
        "db.py",
        "engine.py",
        "office.py",
        "parser_worker.py",
        "planning.py",
        "routing.py",
        "sandbox.py",
        "tools.py",
        "verification.py",
    )
    return fingerprint(
        {
            "adapter": ADAPTER_VERSION,
            "adapter_code": {
                name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                for name in ("execute.py", "run.py")
            },
            "code": {
                name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                for name in modules
            },
            "models": [],
            "privacy": POLICY.model_dump(),
            "arm": "C",
            "max_steps": 9,
        }
    )


def record_failure(conn, run, code):
    """Persist a prepare/import error with the same allowed state transitions as the local worker."""
    target = (
        "awaiting_input"
        if code in {"MISSING_INPUT", "CONTEXT_LIMIT", "FORMULA_UNVERIFIED"}
        else "failed"
    )
    with db.transaction(conn):
        current = db.get_run(conn, run["id"], ACTOR)
        if current["state"] == "queued" and target == "awaiting_input":
            current = engine.transition_run(conn, run["id"], "queued", "preparing")
        if current["state"] not in engine.TERMINAL and current["state"] != target:
            engine.transition_run(conn, run["id"], current["state"], target)
        conn.execute("UPDATE runs SET error_code=? WHERE id=?", (code, run["id"]))


def rows(conn, table, run_id):
    if table not in {"steps", "calls", "artifacts"}:
        raise ValueError("unsupported ledger table")
    return [
        db.row_dict(row)
        for row in conn.execute(f"SELECT * FROM {table} WHERE run_id=? ORDER BY rowid", (run_id,))
    ]


def reopened_output(home, conn, run, artifact_rows, step_rows):
    """Read only published results with a matching actual engine receipt."""
    receipts = []
    for step in step_rows:
        raw = (step["output_json"] or {}).get("receipt")
        if raw is not None:
            receipts.append(Receipt.model_validate(raw))
    rendered, proofs = [], []
    for record in artifact_rows:
        receipt = next(
            (
                item
                for item in receipts
                if item.artifact_id == record["id"]
                and item.artifact_hash == record["sha256"]
                and item.run_id == run["id"]
                and item.validation.status == "pass"
            ),
            None,
        )
        if record["kind"] != "result" or receipt is None:
            continue
        with artifacts.open_artifact(home, conn, record["id"], ACTOR, "eval") as handle:
            raw = handle.read()
        output_format = Path(record["relative_path"]).suffix.lower().lstrip(".")
        if output_format == "xlsx":
            from openpyxl import load_workbook

            book = load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
            try:
                text = "\n".join(
                    "\t".join("" if value is None else str(value) for value in row)
                    for sheet in book
                    for row in sheet.iter_rows(values_only=True)
                )
            finally:
                book.close()
        elif output_format == "docx":
            from docx import Document

            document = Document(io.BytesIO(raw))
            text = "\n".join(
                [paragraph.text for paragraph in document.paragraphs]
                + [
                    "\t".join(cell.text for cell in row.cells)
                    for table in document.tables
                    for row in table.rows
                ]
            )
        elif output_format in {"text", "txt", "md", "csv"}:
            text = raw.decode("utf-8-sig")
        else:
            raise AppError("UNSUPPORTED_CAPABILITY")
        rendered.append(text)
        proofs.append(
            {
                "artifact_id": record["id"],
                "sha256": record["sha256"],
                "reopened": True,
                "receipt": receipt.model_dump(),
            }
        )
    return "\n".join(rendered), proofs


def numeric_observation(case, source_map, steps):
    """Normalize actual aggregate fields; retain actual locators in the ledger, never copy gold."""
    import csv
    from decimal import Decimal, InvalidOperation
    from urllib.parse import unquote

    def column_index(letters):
        number = 0
        for letter in letters:
            number = number * 26 + ord(letter) - ord("A") + 1
        return number - 1

    def locations_for(column, group, source, aggregate, rows, row_locations):
        selection = aggregate["selection"]
        columns = aggregate["columns"]
        sheet = selection.get("sheet")
        bounds = re.fullmatch(r"([A-Z]+)([0-9]+):([A-Z]+)([0-9]+)", selection["cell_range"])
        span = re.fullmatch(
            r"(?:csv:range:|xlsx:([^!]+)!)([A-Z]+)([0-9]+):([A-Z]+)([0-9]+)",
            columns[column].get("locator", ""),
        )
        if not bounds or not span or span[2] != span[4]:
            return []
        if (unquote(span[1]) if span[1] else None) != sheet:
            return []
        header, last = int(bounds[2]), int(bounds[4])
        index = column_index(span[2])
        if (
            int(span[3]) != header + 1
            or int(span[5]) != last
            or not 1 <= header <= last <= len(rows)
            or not column_index(bounds[1]) <= index <= column_index(bounds[3])
            or index >= len(rows[header - 1])
            or rows[header - 1][index] != column
        ):
            return []
        hidden = source.fixture.get("hidden_rows", {}).get(sheet, [])
        contributing = []
        for number in range(header + 1, last + 1):
            if number in hidden and not selection["include_hidden_rows"]:
                continue
            value = rows[number - 1][index] if index < len(rows[number - 1]) else None
            if value is None or isinstance(value, bool):
                continue
            text = str(value).strip()
            if aggregate["policy"].get("thousands_separator") == ",":
                text = text.replace(",", "")
            try:
                valid = Decimal(text).is_finite()
            except InvalidOperation:
                valid = False
            if valid:
                contributing.append(number)
        if len(contributing) != columns[column]["count"]:
            return []
        if group is not None:
            filters = group.get("filter", {})
            headers = rows[header - 1]
            if (
                filters.get("column") != selection.get("group_by")
                or filters.get("equals") != group["key"]
                or headers.count(filters.get("column")) != 1
            ):
                return []
            group_index = headers.index(filters["column"])
            if not column_index(bounds[1]) <= group_index <= column_index(bounds[3]):
                return []
            contributing = [
                number
                for number in contributing
                if group_index < len(rows[number - 1])
                and str(
                    rows[number - 1][group_index]
                    if rows[number - 1][group_index] is not None
                    else ""
                )
                == filters["equals"]
            ]
        selected_rows = [header, *contributing]
        if not all(number in row_locations for number in selected_rows):
            return []
        return [row_locations[number] for number in selected_rows]

    numbers, units, locators = {}, {}, {}
    for step in steps:
        aggregate = (step["output_json"] or {}).get("aggregate")
        if step["state"] != "succeeded" or not aggregate:
            continue
        artifact_id = step["spec_json"]["todo"]["executor"]["args"]["artifact_id"]
        source = next(
            source for source in case.sources if source_map[source.id]["id"] == artifact_id
        )
        selection = aggregate["selection"]
        columns = aggregate["columns"]
        groups = aggregate["groups"]
        sheets = source.fixture.get("sheets", {})
        sheet = selection.get("sheet")
        rows = sheets.get(sheet, [])
        row_locations = {}
        if aggregate["format"] == "csv":
            rows = []
            reader = csv.reader(io.StringIO(source.text), delimiter=selection["delimiter"])
            raw_lines = source.text.splitlines()
            for row in reader:
                rows.append(row)
                # A multiline CSV record cannot be inferred from a physical-line locator.
                key = f"{source.id}:L{reader.line_num}"
                if (
                    reader.line_num == len(rows)
                    and source.locators.get(key) == raw_lines[reader.line_num - 1]
                ):
                    row_locations[len(rows)] = key
        elif aggregate["format"] == "xlsx":
            for number, row in enumerate(rows, 1):
                rendered = io.StringIO()
                csv.writer(rendered, lineterminator="").writerow(row)
                quotes = {f"{sheet} 시트: {rendered.getvalue()}"}
                if len(sheets) == 1:
                    quotes.add(rendered.getvalue())
                matches = [key for key, quote in source.locators.items() if quote in quotes]
                # Duplicate descriptive rows have no unambiguous coordinate mapping.
                if len(matches) == 1 and rows.count(row) == 1:
                    row_locations[number] = matches[0]

        if groups:
            values = {
                str(group["key"]) if len(columns) == 1 else str(group["key"]) + "/" + column: (
                    value,
                    locations_for(column, group, source, aggregate, rows, row_locations),
                )
                for group in groups
                for column, value in group["sums"].items()
            }
        else:
            values = {
                "total" if len(columns) == 1 else column: (
                    data["sum"],
                    locations_for(column, None, source, aggregate, rows, row_locations),
                )
                for column, data in columns.items()
            }
        for key, (value, locations) in values.items():
            numbers[key], units[key], locators[key] = value, aggregate["unit"], locations
        ratio = aggregate.get("ratio")
        if ratio and ratio["value"] is not None:
            ratio_columns = aggregate["policy"]["ratio"].values()
            ratio_locations = [
                locations_for(column, None, source, aggregate, rows, row_locations)
                for column in ratio_columns
            ]
            locations = (
                list(dict.fromkeys(key for part in ratio_locations for key in part))
                if all(ratio_locations)
                else []
            )
            numbers["비율"], units["비율"], locators["비율"] = ratio["value"], "비율", locations
    return numbers, units, locators


def observe(case, conn, home, run, source_map, evidence_path, cache, elapsed):
    steps, calls = rows(conn, "steps", run["id"]), rows(conn, "calls", run["id"])
    if calls:
        raise AssertionError("offline executor must not admit a model call")
    source_proofs = []
    for source in case.sources:
        record = source_map.get(source.id)
        if record is None:
            continue
        with artifacts.open_artifact(home, conn, record["id"], ACTOR, "eval") as handle:
            actual_hash = hashlib.sha256(handle.read()).hexdigest()
        original_hash = hashlib.sha256(
            (home.parent / "originals" / source.filename).read_bytes()
        ).hexdigest()
        source_proofs.append(
            {
                "source_id": source.id,
                "artifact_id": record["id"],
                "sha256": actual_hash,
                "original_sha256": original_hash,
                "unchanged": actual_hash == record["sha256"] == original_hash,
            }
        )
    output, result_proofs = reopened_output(
        home, conn, run, rows(conn, "artifacts", run["id"]), steps
    )
    # Partial drafts stay in the ledger; a blocked workload does not present them as an answer.
    if run["state"] != "succeeded":
        output = ""
    numbers, units, locations = numeric_observation(case, source_map, steps)
    if run["state"] != "succeeded":
        numbers, units, locations = {}, {}, {}
    checked = [step for step in steps if step["state"] == "succeeded" and step["validation_json"]]
    executed = [step for step in steps if step["claimed_at"] is not None]
    cache_observation = db.get_value(
        conn, "cache_observation:" + run["id"], {"lookups": 0, "hits": 0}
    )
    adapter = Observation(
        id=case.id + "-C-" + cache,
        case_id=case.id,
        arm="C",
        cache=cache,
        execution="offline_workload",
        condition_hash=condition_hash(case),
        environment_hash=environment_hash(),
        evidence_ref=str(evidence_path),
        workload_complete=run["state"] == "succeeded",
        output_text=output,
        output_sha256=sha(output),
        numbers=numbers,
        units=units,
        numeric_locators=locations,
        # Product requirement IDs and gold assertion IDs are different namespaces.
        # Keep semantic coverage unannotated until independently checked against actual output.
        facts=[],
        coverage={},
        effects=sorted({step["effect"] for step in executed}),
        missing_input=run["state"] == "awaiting_input",
        outcome=run["state"]
        if run["state"] == "awaiting_input"
        else run["error_code"] or run["state"],
        todos=[
            {
                "id": step["spec_json"]["todo"]["id"],
                "title": step["spec_json"]["todo"]["title"],
                "requirement_ids": [],
                "kind": step["spec_json"]["todo"]["kind"],
                "usefulness": step["spec_json"]["todo"]["usefulness"],
                "executed": step["claimed_at"] is not None,
                "purpose": "content"
                if step["spec_json"]["todo"]["executor"].get("tool_name") == "table.aggregate"
                or step["kind"] == "model"
                else "support",
            }
            for step in steps
        ],
        calls=[],
        latency_ms=elapsed,
        cache_lookups=cache_observation["lookups"],
        cache_hits=cache_observation["hits"],
        repair_scope=[],
        numeric_verified=bool(numbers)
        and all(locations.values())
        and any(
            "numeric_grounding" in step["spec_json"]["todo"]["verify_with"] for step in checked
        ),
        source_verified=len(source_proofs) == len(case.sources)
        and all(proof["unchanged"] for proof in source_proofs),
        effect_verified=all(step["effect"] in {"read", "draft"} for step in executed),
        output_verified=run["state"] == "succeeded" and bool(result_proofs),
        published_output_count=len(result_proofs),
    )
    write_json(
        evidence_path,
        {
            "evidence_layer": "offline_workload",
            "adapter": ADAPTER_VERSION,
            "case_id": case.id,
            "condition_hash": condition_hash(case),
            "original_request": case.request,
            "run": run,
            "steps": steps,
            "calls": calls,
            "sources": source_proofs,
            "results": result_proofs,
            "model_calls": 0,
            "network_clients_created": 0,
            "semantic_gold_annotations": "unverified",
            "numeric_locator_observation": {
                "complete": bool(locations) and all(locations.values()),
                "unresolved_keys": [key for key, value in locations.items() if not value],
            },
            "cache_instrumentation": "completed checks and reused values observed by the product",
            "cache_observation": cache_observation,
        },
    )
    return adapter


def execute_case(case, directory, *, arm="C", cache="cold"):
    """One durable case. Cold refuses reuse; warm requires the same completed cold setup."""
    case = Case.model_validate(case.model_dump())
    if arm != "C":
        raise ValueError("A/B execution is unsupported; production selection remains unchanged")
    if case.category == "automation":
        raise ValueError("automation scenario execution is unsupported in this runner")
    if cache not in {"cold", "warm"} or not re.fullmatch(r"[a-z0-9_-]{1,96}", case.id):
        raise ValueError("invalid case or cache mode")
    directory = Path(directory).absolute()
    evidence_path = directory / (cache + "-ledger.json")
    if evidence_path.exists():
        raise ValueError(
            "case already executed; use a new workspace rather than duplicate execution"
        )
    if cache == "cold":
        directory.mkdir(parents=True, exist_ok=False)
    elif not (directory / "cold-observation.json").is_file():
        raise ValueError("warm requires the same case's cold observation")
    elif read_json(directory / "cold-observation.json")["environment_hash"] != environment_hash():
        raise ValueError("warm requires unchanged product code and evaluation environment")
    home, original_dir = directory / "files", directory / "originals"
    original_dir.mkdir(exist_ok=cache == "warm")
    started = perf_counter()
    conn = db.init_db(directory / "state.db")
    source_map, error = {}, None
    try:
        if cache == "warm":
            saved = db.get_value(conn, "eval_case")
            if saved is None or saved["condition_hash"] != condition_hash(case):
                raise ValueError("warm case, source or scope changed")
            source_map = saved["sources"]
            if set(source_map) != {source.id for source in case.sources}:
                raise ValueError("warm requires every source to have imported successfully")
            for source in case.sources:
                original = original_dir / source.filename
                if (
                    hashlib.sha256(original.read_bytes()).hexdigest()
                    != source_map[source.id]["sha256"]
                ):
                    raise ValueError("warm original bytes changed")
        else:
            for source in case.sources:
                try:
                    raw = source_bytes(source)
                    original = original_dir / source.filename
                    original.write_bytes(raw)
                    with original.open("rb") as handle:
                        source_map[source.id] = artifacts.import_source(
                            home, conn, handle, {"filename": source.filename}, ACTOR, "eval"
                        )
                except AppError as exc:
                    error = exc.code
                    break
            db.set_value(
                conn, "eval_case", {"condition_hash": condition_hash(case), "sources": source_map}
            )
        request = RunRequest(
            request=case.request,
            project_id="eval",
            artifact_ids=[record["id"] for record in source_map.values()],
            output_format=case.output_format,
        )
        run = db.create_run(conn, ACTOR, "ui", case.id + "-" + cache, request)
        registered = tools.registry(home)
        if error:
            record_failure(conn, run, error)
        else:
            try:
                run = db.supply_inputs(
                    conn, run["id"], ACTOR, task_spec(case, request.artifact_ids), run["revision"]
                )
                run = planning.prepare_run(
                    conn,
                    home,
                    run["id"],
                    ACTOR,
                    tools=registered,
                    capability_catalog=[],
                    privacy_policy=POLICY,
                    complete=None,
                )
                for _ in range(9):
                    if run["state"] in STOP_STATES:
                        break
                    before = [(row["id"], row["state"]) for row in rows(conn, "steps", run["id"])]
                    run = engine.execute_next(
                        conn,
                        run["id"],
                        actor=ACTOR,
                        tools=registered,
                        complete=None,
                        now=datetime.now(UTC),
                        verifiers=verification.registry(home),
                        check_sources=tools.check_sources,
                        home=home,
                    )
                    after = [(row["id"], row["state"]) for row in rows(conn, "steps", run["id"])]
                    if before == after and run["state"] not in STOP_STATES:
                        raise AppError("EXECUTION_FAILED")
            except AppError as exc:
                record_failure(conn, run, exc.code)
        run = db.get_run(conn, run["id"], ACTOR)
        result = observe(
            case,
            conn,
            home,
            run,
            source_map,
            evidence_path,
            cache,
            int((perf_counter() - started) * 1000),
        )
        write_json(directory / (cache + "-observation.json"), result.model_dump())
        return result
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", action="append", required=True, help="Frozen case ID; repeat or use all-office"
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--arm", choices=["A", "B", "C"], default="C")
    parser.add_argument("--cache", choices=["cold", "warm"], default="cold")
    args = parser.parse_args()
    if args.arm != "C":
        parser.error("A/B execution is unsupported; no model selection override is installed")
    cases = load_cases()
    chosen = (
        [case for case in cases if case.category != "automation"]
        if args.case == ["all-office"]
        else [case for case in cases if case.id in args.case]
    )
    if not chosen or args.case != ["all-office"] and len(chosen) != len(set(args.case)):
        parser.error("unknown case ID")
    if any(case.category == "automation" for case in chosen):
        parser.error("automation20 execution is unsupported")
    results = []
    for case in chosen:
        result = execute_case(case, args.workspace / case.id, arm=args.arm, cache=args.cache)
        results.append(result.model_dump())
        print(
            json.dumps(
                {
                    "case_id": case.id,
                    "state": result.outcome,
                    "workload_complete": result.workload_complete,
                },
                ensure_ascii=False,
            )
        )
        write_json(args.workspace / ("observations-" + args.cache + ".json"), results)
    print(
        json.dumps(
            {
                "executed_cases": len(results),
                "live": False,
                "model_calls": 0,
                "automation_supported": False,
                "model_quality": None,
                "token_reduction": None,
            }
        )
    )


if __name__ == "__main__":
    main()
