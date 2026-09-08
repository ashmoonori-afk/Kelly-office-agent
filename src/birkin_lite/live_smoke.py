"""Explicit, synthetic provider probe. This never qualifies a model for office work."""

import argparse
import hashlib
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .config import home_path, load_config, probe_binding, read_xai_key
from .context import build_plan_request
from .contracts import AppError, PlanningResultV1, RunRequest, canonical_json
from .db import create_run, init_db, row_dict
from .providers import TransportError, XAIClient, metered_complete
from .safe_io import ensure_directory, guard_directory, rename_new


def run_probe(config_path, model_id, *, allow_live=False, http_transport=None):
    """Probe synthetic planning once; injected transports never count as live evidence."""
    if allow_live is not True:
        raise AppError("POLICY_DENIED")
    live = http_transport is None
    config_path = Path(config_path).absolute()
    config = load_config(config_path)
    home = home_path(config, config_path)
    key = read_xai_key(home)
    if key is None:
        raise AppError("NO_CAPABLE_MODEL")
    home = ensure_directory(home)
    with XAIClient(key.get_secret_value(), http_transport=http_transport) as client:
        binding = probe_binding(
            config, config_path, model_id, lookup_models=client.list_models, explicit=True
        )
        if "M" not in binding.roles or "planning" not in binding.capabilities:
            raise AppError("NO_CAPABLE_MODEL")
        binding = binding.model_copy(
            update={
                "role": "M",
                "reasoning_effort": None,
                "max_output": min(binding.max_output, 1024),
            }
        )
        if not binding.allowed_projects:
            raise AppError("POLICY_DENIED")
        payload = RunRequest(
            request="합성 연결 검사입니다. LIVE_MODEL_OK라는 제목의 단순 TODO 하나를 계획하세요. 자료나 도구를 사용하거나 작업을 실행하지 마세요.",
            project_id=binding.allowed_projects[0],
        )
        request = build_plan_request(payload, [], [], binding=binding)
        evidence = ensure_directory(home / "evidence")
        conn = init_db(evidence / "provider-probe.db")
        actor = config.actor()

        def once(request, selected):
            try:
                return client.transport(request, selected)
            except TransportError as exc:
                exc.retryable = False
                raise

        try:
            run = create_run(conn, actor, "ui", uuid.uuid4().hex, payload)
            error = None
            try:
                result = metered_complete(
                    request,
                    binding,
                    {"conn": conn, "actor": actor, "run": run, "purpose": "plan"},
                    transport=once,
                )
                generated = PlanningResultV1.model_validate(result.parsed_output)
                accepted = (
                    len(generated.plan.todos) == 1
                    and generated.plan.todos[0].title == "LIVE_MODEL_OK"
                )
                if not accepted:
                    error = "INVALID_OUTPUT"
            except AppError as exc:
                accepted, error = False, exc.code
            calls = [
                row_dict(row)
                for row in conn.execute("SELECT * FROM calls WHERE run_id=?", (run["id"],))
            ]
            report = {
                "schema_version": 1,
                "kind": "provider_smoke",
                "live": live,
                "qualified_for_office": False,
                "provider": "xai",
                "model_id": model_id,
                "run_id": run["id"],
                "observed_at": datetime.now(UTC).isoformat(),
                "accepted": accepted,
                "error_code": error,
                "calls": [
                    {
                        key: row[key]
                        for key in (
                            "id",
                            "state",
                            "provider_request_id",
                            "input_tokens_total",
                            "cached_input_tokens",
                            "cache_write_tokens",
                            "output_tokens_total",
                            "reasoning_tokens",
                            "usage_basis",
                            "raw_usage_json",
                            "finish_reason",
                            "latency_ms",
                        )
                    }
                    for row in calls
                ],
            }
        finally:
            conn.close()
        data = canonical_json(report).encode("utf-8")
        path = evidence / ("provider-" + ("live-" if live else "mock-") + run["id"] + ".json")
        temp = evidence / (uuid.uuid4().hex + ".part")
        with guard_directory(evidence):
            try:
                with temp.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                rename_new(temp, path, expected_sha256=hashlib.sha256(data).hexdigest())
            finally:
                if temp.exists():
                    temp.unlink()
        return {"report": str(path), "live": live, "accepted": accepted, "error_code": error}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Synthetic xAI probe; at most one generation HTTP request"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--allow-live", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_probe(args.config, args.model, allow_live=args.allow_live)
    except AppError as exc:
        print(canonical_json({"error_code": exc.code, "message": exc.message_ko}))
        return 1
    print(canonical_json(result))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
