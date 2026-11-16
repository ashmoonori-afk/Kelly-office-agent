"""Explicit, single-use evaluation manifests. Preparing one never authorizes execution.

Prepare: python -m evals.live prepare --config config.json --case summary-01
  --model MODEL --output .birkin-lite/evaluation --manifest reviewed.json
Run only after separate batch approval: python -m evals.live run --config config.json
  --manifest reviewed.json --manifest-sha256 REVIEWED_SHA256 --allow-live

No semantic Observation is issued until an independent reviewer annotates the exact
published output. Human adoption labels remain a separate scorer input.
"""

import argparse
import base64
import hashlib
import io
import json
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Literal

from pydantic import Field

from kelly import config, db, usage, verification
from kelly.contracts import AppError, StrictModel, Usage, evidence_id
from kelly.operations import _database
from kelly.providers import TransportError, XAIClient

from .comparison import ARMS, _environment, _run
from .execute import ACTOR, reopened_output, rows, source_bytes, write_json
from .run import (
    PROTOCOL,
    Observation,
    condition_hash,
    fingerprint,
    load_cases,
    number,
    read_json,
    sha,
)

MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def _configuration(path):
    path = Path(path).absolute()
    current = config.load_config(path)
    home = config.home_path(current, path)
    if (home / "state.db").exists():
        try:
            with _database(home) as stored:
                current = config.runtime_config(current, stored)
        except sqlite3.Error:
            raise AppError("INVALID_INPUT") from None
    return current, home


def _selection(current, path, model_ids, lookup_models=None):
    if not model_ids or len(set(model_ids)) != len(model_ids):
        raise ValueError("select distinct configured model IDs")
    policy = current.privacy_for("eval")
    if policy.local_only or not policy.allow_cloud or policy.allowed_providers != ["xai"]:
        raise AppError("POLICY_DENIED")
    catalog = config.resolve_catalog(
        current, path, api_key_present=lookup_models is not None, lookup_models=lookup_models
    )
    statuses = {value.model_id: value for value in catalog.statuses}
    bindings = {value.model_id: value for value in catalog.bindings}
    selected = []
    for model_id in model_ids:
        value = bindings.get(model_id)
        if (
            value is None
            or not value.enabled
            or "eval" not in value.allowed_projects
            or statuses[model_id].qualification != "verified"
            or lookup_models is not None
            and not value.available
        ):
            raise AppError("NO_CAPABLE_MODEL")
        selected.append(value.model_copy(update={"available": True}))
    if not {"L", "M", "H"} <= {role for value in selected for role in value.roles}:
        raise AppError("NO_CAPABLE_MODEL")
    return selected, policy


def _code_hash():
    return fingerprint(
        {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(__file__).parent.glob("*.py"))
        }
    )


def _fixture_hash(raw, filename):
    if not filename.endswith(".xlsx"):
        return hashlib.sha256(raw).hexdigest()
    # XLSX ZIP and core timestamps vary during generation; all other content must match.
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = {}
        for name in archive.namelist():
            value = archive.read(name)
            if name == "docProps/core.xml":
                root = ET.fromstring(value)
                for item in root:
                    if item.tag in {
                        "{http://purl.org/dc/terms/}created",
                        "{http://purl.org/dc/terms/}modified",
                    }:
                        item.text = "2000-01-01T00:00:00Z"
                value = ET.tostring(root)
            members[name] = hashlib.sha256(value).hexdigest()
        return fingerprint(members)


def prepare_manifest(
    config_path,
    case_ids,
    output_dir,
    *,
    model_ids,
    arms=ARMS,
    caches=("cold", "warm"),
    max_http_attempts=None,
):
    """Read configuration/SQLite and return a synthetic manifest; no key, client or workspace."""
    cases = {case.id: case for case in load_cases()}
    if (
        not case_ids
        or len(set(case_ids)) != len(case_ids)
        or any(key not in cases or cases[key].category == "automation" for key in case_ids)
        or not arms
        or len(set(arms)) != len(arms)
        or not set(arms) <= set(ARMS)
        or tuple(caches) not in {("cold",), ("cold", "warm")}
    ):
        raise ValueError("select fixed Office cases, distinct A/B/C arms, and cold before warm")
    current, _home = _configuration(config_path)
    catalog, privacy = _selection(current, config_path, model_ids)
    count = len(case_ids) * len(arms) * len(caches)
    maximum = count * 16 if max_http_attempts is None else max_http_attempts
    if type(maximum) is not int or not 1 <= maximum <= count * 16:
        raise ValueError("HTTP ceiling must be between one and sixteen per selected run")
    frozen = {}
    for case_id in case_ids:
        case = cases[case_id]
        files = {}
        for source in case.sources:
            if (
                not source.filename
                or source.filename in files
                or Path(source.filename).name != source.filename
                or "/" in source.filename
                or "\\" in source.filename
            ):
                raise ValueError("fixed source filenames must be unique basenames")
            raw = source_bytes(source)
            files[source.filename] = {
                "source_id": source.id,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "base64": base64.b64encode(raw).decode("ascii"),
            }
        inputs = {record["source_id"]: record["sha256"] for record in files.values()}
        frozen[case_id] = {
            "condition_hash": condition_hash(case),
            "files": files,
            "environment_hash": _environment(case, catalog, privacy, inputs),
        }
    manifest = {
        "version": 1,
        "purpose": "synthetic_office_batch_evaluation",
        "corpus_version": PROTOCOL["version"],
        "code_hash": _code_hash(),
        "config_hash": fingerprint(current.model_dump()),
        "output_dir": str(Path(output_dir).absolute()),
        "model_ids": list(model_ids),
        "models": [value.model_dump() for value in catalog],
        "privacy": privacy.model_dump(),
        "case_ids": list(case_ids),
        "arms": list(arms),
        "caches": list(caches),
        "cases": frozen,
        "limits": {
            "logical_calls_per_todo": 2,
            "logical_calls_per_run": 12,
            "http_attempts_per_run": 16,
            "total_http_attempts": maximum,
        },
        "approval_note": "A one-request connectivity approval does not authorize this batch.",
    }
    if len(json.dumps(manifest).encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest too large; choose a smaller fixed subset")
    return manifest


def _validate_manifest(manifest, expected_hash, current, catalog, privacy):
    if fingerprint(manifest) != expected_hash:
        raise AppError("POLICY_DENIED")
    if (
        manifest["version"] != 1
        or manifest["purpose"] != "synthetic_office_batch_evaluation"
        or manifest["corpus_version"] != PROTOCOL["version"]
        or manifest["code_hash"] != _code_hash()
        or manifest["config_hash"] != fingerprint(current.model_dump())
        or manifest["models"] != [value.model_dump() for value in catalog]
        or manifest["privacy"] != privacy.model_dump()
    ):
        raise ValueError("reviewed code, configuration, models or privacy changed")
    fixed = {case.id: case for case in load_cases()}
    ids, arms, caches = manifest["case_ids"], manifest["arms"], manifest["caches"]
    if (
        not ids
        or len(set(ids)) != len(ids)
        or set(ids) != set(manifest["cases"])
        or not arms
        or len(set(arms)) != len(arms)
        or not set(arms) <= set(ARMS)
        or tuple(caches) not in {("cold",), ("cold", "warm")}
    ):
        raise ValueError("invalid fixed selection")
    limits = manifest["limits"]
    maximum = limits["total_http_attempts"]
    if (
        limits
        != {
            "logical_calls_per_todo": 2,
            "logical_calls_per_run": 12,
            "http_attempts_per_run": 16,
            "total_http_attempts": maximum,
        }
        or type(maximum) is not int
        or not 1 <= maximum <= len(ids) * len(arms) * len(caches) * 16
    ):
        raise ValueError("invalid call ceilings")
    for case_id in ids:
        case = fixed.get(case_id)
        if case is None or case.category == "automation":
            raise ValueError("only fixed synthetic Office cases may be sent")
        frozen = manifest["cases"][case_id]
        if frozen["condition_hash"] != condition_hash(case):
            raise ValueError("fixed corpus changed")
        if set(frozen["files"]) != {source.filename for source in case.sources}:
            raise ValueError("fixed source set changed")
        inputs = {}
        for source in case.sources:
            record = frozen["files"][source.filename]
            raw = base64.b64decode(record["base64"], validate=True)
            if (
                record["source_id"] != source.id
                or hashlib.sha256(raw).hexdigest() != record["sha256"]
                or _fixture_hash(raw, source.filename)
                != _fixture_hash(source_bytes(source), source.filename)
            ):
                raise ValueError("frozen source bytes changed")
            inputs[source.id] = record["sha256"]
        if frozen["environment_hash"] != _environment(case, catalog, privacy, inputs):
            raise ValueError("environment changed")
    return fixed


def execute_manifest(
    config_path,
    manifest_path,
    *,
    manifest_sha256,
    allow_live=False,
    http_transport=None,
):
    """Consume exactly one reviewed manifest; never resume or retry an interrupted batch."""
    if allow_live is not True:
        raise AppError("POLICY_DENIED")
    path = Path(manifest_path)
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("manifest too large")
    manifest = read_json(path)
    # Approval/hash and every local preflight precede credentials, clients and output creation.
    if fingerprint(manifest) != manifest_sha256:
        raise AppError("POLICY_DENIED")
    current, home = _configuration(config_path)
    catalog, privacy = _selection(current, config_path, manifest["model_ids"])
    cases = _validate_manifest(manifest, manifest_sha256, current, catalog, privacy)
    directory = Path(manifest["output_dir"])
    if not directory.is_absolute() or directory.exists():
        raise ValueError("evaluation workspace must be a fresh absolute directory")
    key = config.read_xai_key(home)
    if key is None:
        raise AppError("NO_CAPABLE_MODEL")
    with XAIClient(key.get_secret_value(), http_transport=http_transport) as client:
        actual, _ = _selection(current, config_path, manifest["model_ids"], client.list_models)
        if actual != catalog:
            raise AppError("NO_CAPABLE_MODEL")
        directory.mkdir(parents=True, exist_ok=False)
        # Exclusive root creation consumes the attempt before any generation. Failed attempts stay.
        write_json(directory / "manifest.json", manifest)
        count = 0

        def bounded(request, binding):
            nonlocal count
            if count >= manifest["limits"]["total_http_attempts"]:
                raise TransportError("CALL_LIMIT")
            count += 1
            return client.transport(request, binding)

        prices = {profile.binding.model_id: profile.price_snapshot for profile in current.models}
        ledgers = []
        for case_id in manifest["case_ids"]:
            frozen = manifest["cases"][case_id]
            case_dir = directory / case_id
            originals = case_dir / "originals"
            originals.mkdir(parents=True)
            for name, record in frozen["files"].items():
                (originals / name).write_bytes(base64.b64decode(record["base64"], validate=True))
            for cache in manifest["caches"]:
                for arm in manifest["arms"]:
                    inputs = {
                        value["source_id"]: value["sha256"] for value in frozen["files"].values()
                    }
                    if frozen["environment_hash"] != _environment(
                        cases[case_id], catalog, privacy, inputs
                    ):
                        raise ValueError("code changed during comparison; warm replay refused")
                    ledger = _run(
                        cases[case_id],
                        case_dir / arm,
                        arm,
                        bounded,
                        catalog,
                        privacy,
                        originals,
                        frozen["environment_hash"],
                        cache,
                        evidence_layer="live" if http_transport is None else "mock_provider",
                        prices=lambda binding: prices[binding.model_id],
                    )
                    ledgers.append(
                        {
                            "case_id": case_id,
                            "arm": arm,
                            "cache": cache,
                            "path": str(case_dir / arm / (cache + "-ledger.json")),
                            "state": ledger["run"]["state"],
                        }
                    )
        result = {
            "manifest_sha256": manifest_sha256,
            "live": http_transport is None,
            "generation_http_attempts": count,
            "ledgers": ledgers,
            "quality_observations": 0,
            "semantic_annotations": "required",
            "human_adoption_labels": "required",
            "token_reduction": None,
        }
        write_json(directory / "result.json", result)
        return result


class IndependentAnnotation(StrictModel):
    reviewer_id: str = Field(min_length=1, max_length=80)
    independent: Literal[True]
    output_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    facts: list[str]
    coverage: dict[str, list[str]]
    todo_requirements: dict[str, list[str]]
    numbers: dict[str, str] = Field(default_factory=dict)
    units: dict[str, str] = Field(default_factory=dict)
    numeric_locators: dict[str, list[str]] = Field(default_factory=dict)


def _numeric_verified(steps, numeric, complete):
    relevant = [
        step
        for step in steps
        if step["claimed_at"] is not None
        and (
            step["kind"] == "model"
            and step["spec_json"]["todo"]["executor"].get("task") != "semantic_review"
            or step["spec_json"]["todo"]["executor"].get("tool_name") == "table.aggregate"
        )
    ]
    if not complete or not relevant:
        return False
    for step in relevant:
        validation = step["validation_json"] or {}
        required = (
            {"evidence_references", "numeric_references"}
            if step["kind"] == "model"
            else {"numeric", "source_version"}
        )
        if (
            step["state"] != "succeeded"
            or validation.get("status") != "pass"
            or validation.get("errors")
            or not required <= set(validation.get("checked_layers", []))
        ):
            return False
        if step["kind"] != "model" and (
            not numeric["numbers"]
            or set(numeric["numbers"]) != set(numeric["numeric_locators"])
            or not all(numeric["numeric_locators"].values())
        ):
            return False
    return True


def _annotated_numbers(case, ledger, notes, source_map):
    """Copy only independently named source-written values; arithmetic still requires D proof."""
    original = ledger["numeric_observation"]
    if set(notes.numbers) != set(notes.units) or set(notes.numbers) != set(notes.numeric_locators):
        raise ValueError("each independent number needs a unit and source locations")
    if set(notes.numbers) & set(original["numbers"]):
        raise ValueError("independent annotations cannot replace deterministic calculations")
    if not notes.numbers:
        return original
    steps = ledger["steps"]
    if not _numeric_verified(steps, original, ledger["run"]["state"] == "succeeded"):
        raise ValueError("model numeric checks did not pass")
    source_by_artifact = {source_map[source.id]["id"]: source for source in case.sources}
    by_todo = {step["spec_json"]["todo"]["id"]: step for step in steps}
    quoted = {}
    for step in steps:
        validation = step["validation_json"] or {}
        if (
            step["kind"] != "model"
            or step["state"] != "succeeded"
            or validation.get("status") != "pass"
            or validation.get("errors")
            or not {"numeric_references", "source_version", "locator_quote"}
            <= set(validation.get("checked_layers", []))
        ):
            continue
        inputs = [
            by_todo[ref["step_id"]]["output_json"][ref["output_key"]]
            for ref in step["spec_json"]["todo"]["inputs"]
            if "step_id" in ref
        ]
        for evidence in verification._evidence_items(inputs):
            if evidence_id(evidence) not in validation.get("evidence_refs", []):
                continue
            source = source_by_artifact.get(evidence.artifact_id)
            if source is None or source_map[source.id]["sha256"] != evidence.sha256:
                continue
            matches = [
                location for location, quote in source.locators.items() if quote == evidence.quote
            ]
            # Duplicate/partial quotations do not establish one exact evaluation location.
            if len(matches) == 1:
                quoted[matches[0]] = evidence.quote
    for key, value in notes.numbers.items():
        parsed, unit, locations = number(value), notes.units[key], notes.numeric_locators[key]
        if (
            not unit
            or not locations
            or len(set(locations)) != len(locations)
            or not set(locations) <= quoted.keys()
            or not any(
                unit in line and parsed in verification._numbers(line)
                for line in ledger["published_text"].splitlines()
            )
            or not any(
                unit in quoted[location] and parsed in verification._numbers(quoted[location])
                for location in locations
            )
        ):
            raise ValueError(
                "independent number must occur in the published text and verified source quote"
            )
    return {
        "numbers": {**original["numbers"], **notes.numbers},
        "units": {**original["units"], **notes.units},
        "numeric_locators": {**original["numeric_locators"], **notes.numeric_locators},
    }


def annotate_ledger(case_id, ledger_path, annotation):
    """Return an Observation from a live ledger plus hash-bound independent semantics.

    This does not assign adoption or qualify a provider. Pass the returned record and
    separate blinded human labels to evals.run; no gold answers are copied here.
    """
    case = next((case for case in load_cases() if case.id == case_id), None)
    ledger = read_json(ledger_path)
    notes = IndependentAnnotation.model_validate(annotation)
    if (
        case is None
        or ledger["case_id"] != case_id
        or ledger["live"] is not True
        or ledger["evidence_layer"] != "live"
        or ledger["condition_hash"] != condition_hash(case)
        or notes.output_sha256 != sha(ledger["published_text"])
    ):
        raise ValueError("independent annotations require this exact live output")
    directory = Path(ledger_path).absolute().parent
    with _database(directory) as stored:
        if db.get_value(stored, "comparison_ledger:" + ledger["run"]["id"]) != fingerprint(ledger):
            raise ValueError("recorded ledger changed")
        actual_run = db.get_run(stored, ledger["run"]["id"], ACTOR)
        output, proofs = reopened_output(
            directory / "files",
            stored,
            actual_run,
            rows(stored, "artifacts", actual_run["id"]),
            rows(stored, "steps", actual_run["id"]),
        )
        if output != ledger["published_text"] or proofs != ledger["published_receipts"]:
            raise ValueError("published output or receipt changed since annotation")
        source_map = db.get_value(stored, "comparison_sources", {})
    steps, run = ledger["steps"], ledger["run"]
    requirements = {value.id for value in case.gold.required_assertions}
    locations = {key for source in case.sources for key in source.locators}
    if (
        set(notes.todo_requirements) != {step["spec_json"]["todo"]["id"] for step in steps}
        or not set(notes.coverage) <= requirements
        or any(not set(keys) <= requirements for keys in notes.todo_requirements.values())
        or any(not set(keys) <= locations for keys in notes.coverage.values())
    ):
        raise ValueError("annotations reference unknown TODOs, assertions or source locators")
    complete = run["state"] == "succeeded"
    executed = [step for step in steps if step["claimed_at"] is not None]
    numeric = (
        _annotated_numbers(case, ledger, notes, source_map)
        if complete
        else {
            "numbers": {},
            "units": {},
            "numeric_locators": {},
        }
    )
    annotation_path = (
        Path(ledger_path).absolute().with_name(Path(ledger_path).stem + "-annotation.json")
    )
    observation = Observation(
        id=case_id + "-" + ledger["arm"] + "-" + ledger["cache"],
        case_id=case_id,
        arm=ledger["arm"],
        cache=ledger["cache"],
        execution="live",
        condition_hash=ledger["condition_hash"],
        environment_hash=ledger["environment_hash"],
        evidence_ref=str(annotation_path),
        workload_complete=complete,
        output_text=ledger["published_text"] if complete else "",
        output_sha256=sha(ledger["published_text"] if complete else ""),
        **numeric,
        facts=notes.facts if complete else [],
        coverage=notes.coverage if complete else {},
        effects=sorted({step["effect"] for step in executed}),
        missing_input=run["state"] == "awaiting_input",
        outcome=run["state"]
        if run["state"] == "awaiting_input"
        else run["error_code"] or run["state"],
        todos=[
            {
                "id": step["spec_json"]["todo"]["id"],
                "title": step["spec_json"]["todo"]["title"],
                "requirement_ids": notes.todo_requirements[step["spec_json"]["todo"]["id"]],
                "kind": step["spec_json"]["todo"]["kind"],
                "usefulness": step["spec_json"]["todo"]["usefulness"],
                "executed": step["claimed_at"] is not None,
                "purpose": "content"
                if step["kind"] == "model"
                or step["spec_json"]["todo"]["executor"].get("tool_name") == "table.aggregate"
                else "support",
                "reexecuted": step["attempt_count"] > 1,
            }
            for step in steps
        ],
        calls=[
            {
                "id": call["id"],
                "purpose": call["purpose"],
                "role": call["route_json"]["role"],
                "model_id": call["model_id"],
                "state": call["state"],
                "usage": Usage(**{key: call[key] for key in (*usage.TOKEN_FIELDS, "usage_basis")}),
                "actual_micro_usd": call["actual_micro_usd"],
                "price_stale": call["price_snapshot_json"].get("stale", True),
                "escalated": call["route_json"].get("evaluation_attempt", 1) > 1,
            }
            for call in ledger["calls"]
        ],
        latency_ms=ledger["latency_ms"],
        cache_lookups=ledger["cache_observation"]["lookups"],
        cache_hits=ledger["cache_observation"]["hits"],
        repair_scope=[
            step["spec_json"]["todo"]["id"] for step in steps if step["attempt_count"] > 1
        ],
        numeric_verified=_numeric_verified(steps, numeric, complete),
        source_verified=len(ledger["sources"]) == len(case.sources)
        and all(proof["unchanged"] for proof in ledger["sources"]),
        effect_verified=all(step["effect"] in {"read", "draft"} for step in executed),
        output_verified=complete
        and bool(ledger["published_receipts"])
        and all(proof["reopened"] for proof in ledger["published_receipts"]),
        published_output_count=len(ledger["published_receipts"]),
    )
    with annotation_path.open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "ledger": str(Path(ledger_path).absolute()),
                "ledger_sha256": fingerprint(ledger),
                "annotation": notes.model_dump(),
                "observation": observation.model_dump(),
            },
            stream,
            ensure_ascii=False,
            indent=2,
        )
    return observation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--case", action="append", required=True)
    prepare.add_argument("--model", action="append", required=True)
    prepare.add_argument("--arm", action="append", choices=ARMS)
    prepare.add_argument("--cold-only", action="store_true")
    prepare.add_argument("--max-http-attempts", type=int)
    run = sub.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--manifest-sha256", required=True)
    run.add_argument("--allow-live", action="store_true")
    annotate = sub.add_parser("annotate")
    annotate.add_argument("--case", required=True)
    annotate.add_argument("--ledger", type=Path, required=True)
    annotate.add_argument("--annotation", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        value = prepare_manifest(
            args.config,
            args.case,
            args.output,
            model_ids=args.model,
            arms=args.arm or ARMS,
            caches=("cold",) if args.cold_only else ("cold", "warm"),
            max_http_attempts=args.max_http_attempts,
        )
        with args.manifest.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        print(json.dumps({"manifest": str(args.manifest), "sha256": fingerprint(value)}))
    elif args.command == "run":
        result = execute_manifest(
            args.config,
            args.manifest,
            manifest_sha256=args.manifest_sha256,
            allow_live=args.allow_live,
        )
        print(json.dumps(result, ensure_ascii=False))
    else:
        observation = annotate_ledger(args.case, args.ledger, read_json(args.annotation))
        print(observation.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
