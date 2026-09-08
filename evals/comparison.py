"""Offline A/B/C pipeline fixtures. No API client, live command or quality claim."""

import hashlib
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from birkin_lite import artifacts, db, engine, planning, tools, usage, verification
from birkin_lite.contracts import AppError, ModelBinding, ModelResult, RunRequest
from birkin_lite.providers import TransportError, metered_complete

from .execute import ACTOR, STOP_STATES, reopened_output, rows, source_bytes, task_spec, write_json
from .run import condition_hash, fingerprint, read_json

ARMS = ("A", "B", "C")
LIMIT_NOTE = (
    "B starts at L and may use M after a failed check. The shared two-call TODO ceiling "
    "also includes any required H review; L/M/H three-generation cascades are not allowed."
)


class FixtureTransport:
    """A finite sequence of typed synthetic replies; no callable/network adapter accepted."""

    def __init__(self, replies):
        self.replies = []
        for value in replies:
            if not isinstance(value, ModelResult):
                raise TypeError("offline fixtures require ModelResult values")
            reply = ModelResult.model_validate(value.model_dump())
            if reply.normalized_usage.usage_basis == "provider":
                raise ValueError(
                    "fixture tokens are estimated or unknown, never actual provider usage"
                )
            self.replies.append(reply)
        self.used = 0

    def __call__(self, _request, _binding):
        if self.used >= len(self.replies):
            raise TransportError("PROVIDER_ERROR")
        reply = self.replies[self.used]
        self.used += 1
        return reply.model_copy(deep=True)


def _environment(case, catalog, privacy, inputs):
    source = Path(engine.__file__).parent
    return fingerprint(
        {
            "adapter": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "product": {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(source.glob("*.py"))
            },
            "condition": condition_hash(case),
            "models": [binding.model_dump() for binding in catalog],
            "privacy": privacy.model_dump(),
            "input_hashes": inputs,
            "logical_calls_per_todo": 2,
            "logical_calls_per_run": 12,
            "http_attempts_per_run": 16,
        }
    )


def _run(case, directory, arm, transport, catalog, privacy, frozen, environment, cache):
    if cache == "cold":
        directory.mkdir()
    elif not (directory / "cold-ledger.json").is_file():
        raise ValueError("warm requires a completed cold fixture attempt for every arm")
    destination = directory / (cache + "-ledger.json")
    if destination.exists():
        raise ValueError("fixture attempt already recorded; use a fresh directory")
    home = directory / "files"
    conn = db.init_db(directory / "state.db")
    source_map = {}
    try:
        if cache == "cold":
            for source in case.sources:
                with (frozen / source.filename).open("rb") as stream:
                    source_map[source.id] = artifacts.import_source(
                        home, conn, stream, {"filename": source.filename}, ACTOR, "eval"
                    )
            db.set_value(conn, "comparison_sources", source_map)
        else:
            source_map = db.get_value(conn, "comparison_sources", {})
            if set(source_map) != {source.id for source in case.sources}:
                raise ValueError("warm source set changed or cold import was incomplete")
        request = RunRequest(
            request=case.request,
            project_id="eval",
            artifact_ids=[record["id"] for record in source_map.values()],
            output_format=case.output_format,
        )
        run = db.create_run(conn, ACTOR, "ui", case.id + "-" + cache, request)
        if arm != "C":
            db.set_value(
                conn,
                "evaluation:" + run["id"],
                {
                    "actor_id": ACTOR.id,
                    "project_id": "eval",
                    "arm": arm,
                    "privacy": privacy.model_dump(),
                    "catalog": [binding.model_dump() for binding in catalog],
                },
            )
        registered = tools.registry(home)
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
                capability_catalog=catalog,
                privacy_policy=privacy,
                complete=partial(metered_complete, transport=transport),
            )
            complete = partial(
                engine.complete_model_todo,
                capability_catalog=catalog,
                privacy_policy=privacy,
                transport=transport,
                evaluation_arm=arm if arm != "C" else None,
            )
            # Eight TODOs can each need one verification repair; core call caps still apply.
            for _ in range(17):
                if run["state"] in STOP_STATES:
                    break
                before = [(r["id"], r["state"]) for r in rows(conn, "steps", run["id"])]
                run = engine.execute_next(
                    conn,
                    run["id"],
                    actor=ACTOR,
                    tools=registered,
                    complete=complete,
                    now=datetime.now(UTC),
                    verifiers=verification.registry(home),
                    check_sources=tools.check_sources,
                    home=home,
                )
                after = [(r["id"], r["state"]) for r in rows(conn, "steps", run["id"])]
                if before == after and run["state"] not in STOP_STATES:
                    raise AppError("EXECUTION_FAILED")
        except AppError as exc:
            from .execute import record_failure

            record_failure(conn, run, exc.code)
        run = db.get_run(conn, run["id"], ACTOR)
        steps, calls = rows(conn, "steps", run["id"]), rows(conn, "calls", run["id"])
        output, receipts = reopened_output(
            home, conn, run, rows(conn, "artifacts", run["id"]), steps
        )
        source_proofs = []
        for source in case.sources:
            record = source_map[source.id]
            with artifacts.open_artifact(home, conn, record["id"], ACTOR, "eval") as stream:
                managed_hash = hashlib.sha256(stream.read()).hexdigest()
            original_hash = hashlib.sha256((frozen / source.filename).read_bytes()).hexdigest()
            source_proofs.append(
                {
                    "source_id": source.id,
                    "sha256": original_hash,
                    "unchanged": original_hash == managed_hash == record["sha256"],
                }
            )
        ledger = {
            "evidence_layer": "offline_fixture",
            "case_id": case.id,
            "arm": arm,
            "cache": cache,
            "environment_hash": environment,
            "condition_hash": condition_hash(case),
            "original_request": case.request,
            "run": run,
            "steps": steps,
            "calls": calls,
            "sources": source_proofs,
            "published_text": output,
            "published_receipts": receipts,
            "fixture_usage": usage.summarize_usage(conn, run["id"], ACTOR),
            "fixture_replies_used": transport.used,
            "live": False,
            "actual_provider_tokens": None,
            "model_quality": None,
            "token_reduction": None,
            "comparison_limit": LIMIT_NOTE,
        }
        write_json(destination, ledger)
        return ledger
    finally:
        conn.close()


def compare_fixture(case, directory, *, scripts, catalog, privacy, cache="cold"):
    """Run the same original task/tools/verifiers three times with finite fake replies.

    This is a pipeline-boundary test report, deliberately not a scorer Observation.
    No expected answer is used to build a request, output, verification or observation.
    """
    names = [source.filename for source in case.sources]
    if len(set(names)) != len(names) or any(
        not name or name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name
        for name in names
    ):
        raise ValueError("source filenames must be unique basenames")
    if case.category == "automation":
        raise ValueError("automation scenarios use the separate automation runner")
    if cache not in {"cold", "warm"} or set(scripts) != set(ARMS):
        raise ValueError("exactly A/B/C fixture scripts and cold/warm cache mode are required")
    if privacy.project_id != "eval" or set(privacy.allowed_providers) - {"fixture"}:
        raise ValueError("offline comparison allows only the fixture provider in project eval")
    catalog = [ModelBinding.model_validate(value.model_dump()) for value in catalog]
    if any(value.provider != "fixture" or value.metering_mode != "local" for value in catalog):
        raise ValueError("offline comparison accepts only local fixture bindings")
    transports = {arm: FixtureTransport(scripts[arm]) for arm in ARMS}
    directory = Path(directory).absolute()
    frozen = directory / "originals"
    if cache == "cold":
        directory.mkdir(parents=True, exist_ok=False)
        frozen.mkdir()
        for source in case.sources:
            (frozen / source.filename).write_bytes(source_bytes(source))
    elif not (directory / "manifest.json").is_file():
        raise ValueError("warm requires the cold comparison manifest")
    inputs = {
        source.id: hashlib.sha256((frozen / source.filename).read_bytes()).hexdigest()
        for source in case.sources
    }
    environment = _environment(case, catalog, privacy, inputs)
    manifest = {"environment_hash": environment, "input_hashes": inputs}
    if cache == "cold":
        write_json(directory / "manifest.json", manifest)
    elif read_json(directory / "manifest.json") != manifest:
        raise ValueError("warm requires identical inputs, code, catalog and privacy")
    return [
        _run(
            case,
            directory / arm,
            arm,
            transports[arm],
            catalog,
            privacy,
            frozen,
            environment,
            cache,
        )
        for arm in ARMS
    ]
