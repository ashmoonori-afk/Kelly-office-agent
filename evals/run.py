"""Frozen evaluation contracts and accounting; no live workload runner or API client."""

import argparse
import hashlib
import json
import math
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from birkin_lite.contracts import StrictModel, Usage

CATEGORIES = ("table", "extraction", "summary", "multi_document", "conflict_missing", "automation")
KINDS = ("mechanical", "simple", "standard", "complex")
PROTOCOL = {
    "version": "birkin-eval-v1.1",
    "arms": {
        "A": "Content generation starts at H; deterministic tools remain unchanged.",
        "B": "Content starts at L, then M/H only after the same failed checks.",
        "C": "TODO kind selects D/L/M/H during planning; production correction rules apply.",
    },
    "cache": {
        "cold": "New isolated cache, empty extraction/response entries; provider cache is observed.",
        "warm": "Identical case/arm replay after cold; include warm-up usage in a separate ledger.",
    },
    "shared": [
        "source bytes and scope",
        "requirements and output format",
        "registered tools and verifiers",
        "privacy and effects",
        "context and output limits",
        "model catalog and quality evidence",
        "one M planning call when needed",
        "repair/replan/attempt ceilings",
    ],
    "limits": {"todos": 8, "logical_model_calls": 12},
    "live_runner_available": False,
}


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fingerprint(value):
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def unique(values):
    if len(values) != len(set(values)):
        raise ValueError("duplicate identity")


def number(value):
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("finite decimal string required") from exc
    if not isinstance(value, str) or not parsed.is_finite():
        raise ValueError("finite decimal string required")
    return parsed


class Source(StrictModel):
    id: str
    filename: str
    text: str
    locators: dict[str, str]
    fixture: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def locations(self):
        if not self.id or not self.text or not self.locators:
            raise ValueError("source and exact locator quotes are required")
        for location, quote in self.locators.items():
            if not location.startswith(self.id + ":") or not quote or quote not in self.text:
                raise ValueError("locator quote must resolve in its source")
        return self


class Assertion(StrictModel):
    id: str
    text: str
    locators: list[str]
    expected_kind: Literal["mechanical", "simple", "standard", "complex"]


class Gold(StrictModel):
    numbers: dict[str, str]
    units: dict[str, str]
    numeric_locators: dict[str, list[str]]
    required_assertions: list[Assertion] = Field(min_length=1, max_length=20)
    allowed_facts: list[str] = Field(min_length=1)
    forbidden_facts: list[str] = Field(min_length=1)
    allowed_effects: list[Literal["read", "draft", "external_write"]]
    expected_kind: Literal["mechanical", "simple", "standard", "complex"]
    expected_missing_input: bool
    expected_outcome: str
    max_todos: int = Field(ge=1, le=8)
    human_required: bool


class Case(StrictModel):
    id: str
    category: Literal[
        "table", "extraction", "summary", "multi_document", "conflict_missing", "automation"
    ]
    cohort: Literal["simple", "complex", "repeated_bot"]
    request: str = Field(min_length=1, max_length=8000)
    output_format: Literal["text", "md", "csv", "xlsx", "docx"]
    sources: list[Source]
    inputs: dict
    tools: list[str]
    verifiers: list[str]
    scenario: dict = Field(default_factory=dict)
    gold: Gold

    @model_validator(mode="after")
    def consistent(self):
        unique([item.id for item in self.sources])
        unique([item.id for item in self.gold.required_assertions])
        locations = {key for source in self.sources for key in source.locators}
        for item in self.gold.required_assertions:
            if not item.id or not item.text or not set(item.locators) <= locations:
                raise ValueError("assertion references an unknown locator")
        if set(self.gold.numbers) != set(self.gold.units) or set(self.gold.numbers) != set(
            self.gold.numeric_locators
        ):
            raise ValueError("every numeric gold needs a unit and exact locations")
        for key, value in self.gold.numbers.items():
            number(value)
            if (
                not self.gold.numeric_locators[key]
                or not set(self.gold.numeric_locators[key]) <= locations
            ):
                raise ValueError("numeric gold needs resolving evidence")
        if set(self.gold.allowed_facts) & set(self.gold.forbidden_facts):
            raise ValueError("a fact cannot be both allowed and forbidden")
        if self.category == "automation" and not self.scenario:
            raise ValueError("automation cases need initial state and ordered events")
        return self


def load_cases(path=None):
    raw = read_json(path or Path(__file__).with_name("cases.json"))
    if set(raw) != {"version", "cases"} or raw["version"] != PROTOCOL["version"]:
        raise ValueError("unknown frozen corpus version")
    cases = [Case.model_validate(item) for item in raw["cases"]]
    unique([case.id for case in cases])
    if Counter(case.category for case in cases) != Counter(dict.fromkeys(CATEGORIES, 20)):
        raise ValueError("exactly twenty cases in each of six categories are required")
    unique([fingerprint([case.request, [s.text for s in case.sources]]) for case in cases])
    return cases


def condition_hash(case):
    """The future executor must additionally bind concrete tool/model/policy version hashes."""
    return fingerprint({"protocol": PROTOCOL, "case": case.model_dump()})


class Call(StrictModel):
    id: str
    purpose: Literal["plan", "generate", "repair", "review", "replan"]
    role: Literal["L", "M", "H"]
    model_id: str = Field(min_length=1)
    state: Literal["settled", "unknown", "released"]
    usage: Usage
    actual_micro_usd: int | None = Field(default=None, ge=0)
    price_stale: bool = True
    escalated: bool = False


class ObservedTodo(StrictModel):
    id: str
    title: str = Field(min_length=1, max_length=200)
    requirement_ids: list[str]
    kind: Literal["mechanical", "simple", "standard", "complex"]
    usefulness: Literal["required", "useful", "optional"]
    executed: bool
    purpose: Literal["content", "support"] = "content"
    completed_before_repair: bool = False
    reexecuted: bool = False


class Observation(StrictModel):
    id: str
    case_id: str
    arm: Literal["A", "B", "C"]
    cache: Literal["cold", "warm"]
    execution: Literal["offline_fixture", "offline_workload", "live"]
    condition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_ref: str = Field(min_length=1, max_length=512)
    workload_complete: bool
    output_text: str = Field(max_length=131072)
    output_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    numbers: dict[str, str]
    units: dict[str, str]
    numeric_locators: dict[str, list[str]]
    facts: list[str]
    coverage: dict[str, list[str]]
    effects: list[Literal["read", "draft", "external_write", "destructive"]]
    missing_input: bool
    outcome: str
    scenario_result: dict = Field(default_factory=dict)
    todos: list[ObservedTodo]
    calls: list[Call]
    latency_ms: int = Field(ge=0)
    cache_lookups: int = Field(ge=0)
    cache_hits: int = Field(ge=0)
    repair_scope: list[str]
    numeric_verified: bool
    source_verified: bool
    effect_verified: bool
    output_verified: bool

    @model_validator(mode="after")
    def consistent(self):
        if sha(self.output_text) != self.output_sha256:
            raise ValueError("observation output hash mismatch")
        unique([todo.id for todo in self.todos])
        unique([call.id for call in self.calls])
        if self.cache_hits > self.cache_lookups:
            raise ValueError("cache hits exceed lookups")
        if not set(self.repair_scope) <= {todo.id for todo in self.todos}:
            raise ValueError("repair scope references an unknown TODO")
        for value in self.numbers.values():
            number(value)
        if set(self.numbers) != set(self.units) or set(self.numbers) != set(self.numeric_locators):
            raise ValueError("observed numbers require matching units and locations")
        return self


def validate_observations(raw, cases):
    records = [Observation.model_validate(item) for item in raw]
    unique([record.id for record in records])
    unique([(record.case_id, record.arm, record.cache) for record in records])
    by_id = {case.id: case for case in cases}
    environments = defaultdict(set)
    for record in records:
        case = by_id.get(record.case_id)
        if case is None or record.condition_hash != condition_hash(case):
            raise ValueError("case, input, scope, tools, verifiers or protocol mismatch")
        environments[(record.case_id, record.execution)].add(record.environment_hash)
    if any(len(values) != 1 for values in environments.values()):
        raise ValueError("A/B/C and cold/warm must use an identical environment")
    return records


def blind_id(record):
    return "b_" + sha(record.id + ":" + record.output_sha256)[:24]


def blind_packet(records, cases):
    by_id = {case.id: case for case in cases}
    return [
        {
            "blind_id": blind_id(record),
            "output_sha256": record.output_sha256,
            "request": by_id[record.case_id].request,
            "sources": [source.model_dump() for source in by_id[record.case_id].sources],
            "required_assertions": [
                item.model_dump() for item in by_id[record.case_id].gold.required_assertions
            ],
            "output_text": record.output_text,
            "todos": [
                {"title": todo.title, "requirement_ids": todo.requirement_ids}
                for todo in record.todos
            ],
        }
        for record in sorted(records, key=blind_id)
    ]


class HumanLabel(StrictModel):
    blind_id: str
    output_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer_id: str = Field(min_length=1, max_length=80)
    blinded: Literal[True]
    adopted: bool
    coverage_correct: bool
    facts_grounded: bool
    usable: bool
    todo_decomposition_appropriate: bool
    rationale: str = Field(min_length=1, max_length=2000)


def import_labels(raw, records):
    labels = [HumanLabel.model_validate(item) for item in raw]
    unique([(label.blind_id, label.reviewer_id) for label in labels])
    expected = {blind_id(record): record.output_sha256 for record in records}
    for label in labels:
        if expected.get(label.blind_id) != label.output_sha256:
            raise ValueError("unknown blinded item or stale output label")
        if label.adopted and not (
            label.coverage_correct
            and label.facts_grounded
            and label.usable
            and label.todo_decomposition_appropriate
        ):
            raise ValueError("adoption requires all human rubric checks")
    return labels


def checks(case, record):
    gold = case.gold
    requirements = {item.id: item for item in gold.required_assertions}
    matched = sum(
        key in record.coverage and set(item.locators) <= set(record.coverage[key])
        for key, item in requirements.items()
    )
    locations = {key for source in case.sources for key in source.locators}
    planned = {
        key
        for todo in record.todos
        if todo.usefulness != "optional"
        for key in todo.requirement_ids
    }
    numeric = set(record.numbers) == set(gold.numbers) and all(
        number(record.numbers[key]) == number(value)
        and record.units[key] == gold.units[key]
        and set(gold.numeric_locators[key]) <= set(record.numeric_locators[key]) <= locations
        for key, value in gold.numbers.items()
    )
    classified = [
        (
            KINDS.index(todo.kind),
            KINDS.index(
                "mechanical" if todo.purpose == "support" else requirements[key].expected_kind
            ),
        )
        for todo in record.todos
        for key in todo.requirement_ids
        if key in requirements
    ]
    result = {
        "numbers": numeric and record.numeric_verified,
        "facts": set(record.facts) <= set(gold.allowed_facts)
        and not any(fact in record.output_text for fact in gold.forbidden_facts),
        "coverage": matched == len(requirements)
        and set(record.coverage) == set(requirements)
        and all(set(values) <= locations for values in record.coverage.values()),
        "plan_coverage": set(requirements) <= planned,
        "source": record.source_verified,
        "effect": set(record.effects) <= set(gold.allowed_effects) and record.effect_verified,
        "output": record.output_verified,
        "missing_input": record.missing_input == gold.expected_missing_input,
        "outcome": record.outcome == gold.expected_outcome,
        "scenario": case.category != "automation"
        or record.scenario_result == case.scenario["expected"],
        "todo_limit": len(record.todos) <= gold.max_todos,
        "mechanical_model_calls": gold.expected_kind != "mechanical"
        or not any(call.state != "released" for call in record.calls),
        "bounded_repair": not any(
            todo.completed_before_repair and todo.reexecuted for todo in record.todos
        ),
    }
    return {
        "passed": record.workload_complete and all(result.values()),
        "checks": result,
        "coverage_count": matched,
        "requirement_count": len(requirements),
        "classification_exact": sum(actual == expected for actual, expected in classified),
        "classification_under": sum(actual < expected for actual, expected in classified),
        "classification_over": sum(actual > expected for actual, expected in classified),
        "classification_count": len(classified),
        "unnecessary_todos": sum(
            todo.executed
            and (todo.usefulness == "optional" or not set(todo.requirement_ids) & set(requirements))
            for todo in record.todos
        ),
    }


def wilson(successes, count):
    if count == 0:
        return None
    z = 1.959963984540054
    p = successes / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    half = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def complete_sum(values):
    values = list(values)
    return sum(values) if all(value is not None for value in values) else None


def summarize(records, cases, labels):
    by_id = {case.id: case for case in cases}
    judgments = defaultdict(list)
    for label in labels:
        judgments[label.blind_id].append(label)
    evaluated = [(record, checks(by_id[record.case_id], record)) for record in records]
    live = bool(records) and all(record.execution == "live" for record in records)
    acceptance_evaluated = sum(
        not by_id[record.case_id].gold.human_required or bool(judgments[blind_id(record)])
        for record in records
    )
    quality_ready = live and acceptance_evaluated == len(records)
    accepted = [
        record
        for record, result in evaluated
        if result["passed"]
        and (
            all(label.adopted for label in judgments[blind_id(record)])
            if judgments[blind_id(record)]
            else not by_id[record.case_id].gold.human_required
        )
    ]
    calls = [call for record in records for call in record.calls if call.state != "released"]
    tokens = complete_sum(
        complete_sum([call.usage.input_tokens_total, call.usage.output_tokens_total])
        for call in calls
    )
    costs = complete_sum(call.actual_micro_usd if not call.price_stale else None for call in calls)
    unknown = sum(
        call.state == "unknown"
        or call.usage.usage_basis == "unknown"
        or call.usage.input_tokens_total is None
        or call.usage.output_tokens_total is None
        for call in calls
    )
    coverage = sum(result["coverage_count"] for _, result in evaluated)
    required = sum(result["requirement_count"] for _, result in evaluated)
    count = len(records)
    classified = sum(result["classification_count"] for _, result in evaluated)
    todos = [todo for record in records for todo in record.todos if todo.executed]
    hits = sum(record.cache_hits for record in records)
    lookups = sum(record.cache_lookups for record in records)
    n_accepted = len(accepted)
    return {
        "tasks": count,
        "structured_contract_passes": sum(result["passed"] for _, result in evaluated),
        "live_quality_verified": quality_ready,
        "accepted_tasks": n_accepted if quality_ready else None,
        "acceptance_evaluated_tasks": acceptance_evaluated,
        "quality_pass_rate": n_accepted / count if quality_ready else None,
        "quality_wilson_95": wilson(n_accepted, count) if quality_ready else None,
        "human_labeled_tasks": sum(bool(judgments[blind_id(record)]) for record in records),
        "total_tokens": tokens,
        "known_tokens_lower_bound": sum(
            (call.usage.input_tokens_total or 0) + (call.usage.output_tokens_total or 0)
            for call in calls
        ),
        "cached_input_tokens": complete_sum(call.usage.cached_input_tokens for call in calls),
        "reasoning_tokens_subset": complete_sum(call.usage.reasoning_tokens for call in calls),
        "actual_micro_usd": costs,
        "known_micro_usd_lower_bound": sum(call.actual_micro_usd or 0 for call in calls),
        "tokens_per_accepted_task": tokens / n_accepted
        if quality_ready and n_accepted and tokens is not None
        else None,
        "cost_per_accepted_task_micro_usd": costs / n_accepted
        if quality_ready and n_accepted and costs is not None
        else None,
        "unknown_usage_rate": unknown / len(calls) if calls else 0.0,
        "provider_usage_only": all(call.usage.usage_basis == "provider" for call in calls),
        "transport_attempts": len(calls),
        "planning_tokens": complete_sum(
            complete_sum([call.usage.input_tokens_total, call.usage.output_tokens_total])
            for call in calls
            if call.purpose in {"plan", "replan"}
        ),
        "repair_rate": sum(any(c.purpose == "repair" for c in r.calls) for r in records) / count
        if count
        else None,
        "escalation_rate": sum(any(c.escalated for c in r.calls) for r in records) / count
        if count
        else None,
        "repair_scope_todos": sum(len(record.repair_scope) for record in records),
        "time_to_accepted_ms_p50": percentile([r.latency_ms for r in accepted], 0.5)
        if quality_ready
        else None,
        "time_to_accepted_ms_p95": percentile([r.latency_ms for r in accepted], 0.95)
        if quality_ready
        else None,
        "requirement_coverage": coverage / required if required else None,
        "requirement_coverage_wilson_95": wilson(coverage, required),
        "classification_accuracy": sum(r["classification_exact"] for _, r in evaluated) / classified
        if classified
        else None,
        "classification_wilson_95": wilson(
            sum(r["classification_exact"] for _, r in evaluated), classified
        ),
        "classification_under": sum(r["classification_under"] for _, r in evaluated),
        "classification_over": sum(r["classification_over"] for _, r in evaluated),
        "unnecessary_todos": sum(r["unnecessary_todos"] for _, r in evaluated),
        "deterministic_share": sum(todo.kind == "mechanical" for todo in todos) / len(todos)
        if todos
        else None,
        "cache_hit_rate": hits / lookups if lookups else None,
        "failed_checks": dict(
            Counter(
                key
                for _, result in evaluated
                for key, passed in result["checks"].items()
                if not passed
            )
        ),
    }


def score(cases, records, labels=()):
    groups = defaultdict(list)
    categories = defaultdict(list)
    for record in records:
        case = next(case for case in cases if case.id == record.case_id)
        groups[(record.execution, record.arm, record.cache, case.cohort)].append(record)
        categories[(record.execution, record.arm, record.cache, case.category)].append(record)
    reports = {
        "/".join(key): summarize(values, cases, labels) for key, values in sorted(groups.items())
    }
    comparisons = []
    for cache in ("cold", "warm"):
        for cohort in ("simple", "complex", "repeated_bot"):
            for challenger in ("B", "C"):
                a = groups.get(("live", "A", cache, cohort), [])
                c = groups.get(("live", challenger, cache, cohort), [])
                same = bool(a) and {r.case_id for r in a} == {r.case_id for r in c}
                baseline = reports.get(f"live/A/{cache}/{cohort}", {})
                candidate = reports.get(f"live/{challenger}/{cache}/{cohort}", {})
                full_labels = same and all(
                    r["acceptance_evaluated_tasks"] == r["tasks"] for r in (baseline, candidate)
                )
                measured = full_labels and all(
                    r["provider_usage_only"]
                    and not r["unknown_usage_rate"]
                    and r["tokens_per_accepted_task"] is not None
                    for r in (baseline, candidate)
                )
                denominator = baseline.get("tokens_per_accepted_task")
                savings = (
                    1 - candidate["tokens_per_accepted_task"] / denominator
                    if measured and denominator
                    else None
                )
                comparisons.append(
                    {
                        "cache": cache,
                        "cohort": cohort,
                        "challenger": challenger,
                        "matched_cases": len(a) if same else 0,
                        "token_reduction": savings,
                        "cost_reduction": 1
                        - candidate["cost_per_accepted_task_micro_usd"]
                        / baseline["cost_per_accepted_task_micro_usd"]
                        if measured
                        and baseline["cost_per_accepted_task_micro_usd"]
                        and candidate["cost_per_accepted_task_micro_usd"] is not None
                        else None,
                        "quality_delta": candidate["quality_pass_rate"]
                        - baseline["quality_pass_rate"]
                        if full_labels
                        else None,
                        "release_target_proven": None,
                        "note": "Descriptive paired observations; no causal or noninferiority claim.",
                    }
                )
    return {
        "evidence_layer": "imported_observations_not_reexecuted",
        "live_runner_available": False,
        "corpus_hash": fingerprint([case.model_dump() for case in cases]),
        "observed_tasks": len(records),
        "expected_matrix_tasks": len(cases) * 6,
        "matrix_complete": len(records) == len(cases) * 6,
        "live_matrix_complete": len(records) == len(cases) * 6
        and all(record.execution == "live" for record in records),
        "groups": reports,
        "categories": {
            "/".join(key): summarize(values, cases, labels)
            for key, values in sorted(categories.items())
        },
        "comparisons": comparisons,
    }


def boundary_smoke():
    """Exercise the real routing/context/metering boundary with a non-network callable."""
    from birkin_lite.context import build_model_request
    from birkin_lite.contracts import (
        Actor,
        ModelBinding,
        ModelResult,
        PlanV1,
        PrivacyPolicy,
        Requirement,
        RunRequest,
        TaskFacts,
        TodoV1,
    )
    from birkin_lite.db import create_run, init_db
    from birkin_lite.engine import install_plan
    from birkin_lite.providers import TransportError, metered_complete
    from birkin_lite.routing import choose_model, normalize_todo
    from birkin_lite.usage import summarize_usage

    actor = Actor(id="eval-owner", project_ids=["eval"])
    todo = TodoV1(
        id="t1",
        title="명시된 회의 할 일 추출",
        requirement_ids=["r1"],
        kind="simple",
        reason="명시된 항목을 추출한다",
        signals=["extraction"],
        clarity="clear",
        usefulness="required",
        executor={"type": "model", "role": "L", "task": "meeting_actions"},
        output_schema="MeetingActionsV1",
        done_when=["근거에 없는 담당자를 만들지 않는다"],
    )
    candidate = ModelBinding(
        provider="offline-fixture",
        model_id="fixture-boundary-only",
        roles=["L"],
        enabled=True,
        available=True,
        allowed_projects=["eval"],
        capabilities=["structured_output", "meeting_actions"],
        context_window=32768,
        max_output=1024,
        quality_evidence_id="fixture-not-quality",
    )
    binding = choose_model(
        normalize_todo(todo, TaskFacts()),
        [candidate],
        PrivacyPolicy(project_id="eval", allowed_providers=["offline-fixture"], allow_cloud=True),
    )
    attempts = []

    def transport(request, selected):
        attempts.append(selected.model_id)
        if len(attempts) == 1:
            raise TransportError(
                "PROVIDER_RATE_LIMITED",
                retryable=True,
                status=429,
                usage=Usage(input_tokens_total=7, output_tokens_total=2, usage_basis="provider"),
            )
        return ModelResult(
            parsed_output={"actions": []},
            normalized_usage=Usage(
                input_tokens_total=11, output_tokens_total=3, usage_basis="provider"
            ),
            finish_reason="stop",
            latency_ms=1,
        )

    with tempfile.TemporaryDirectory(prefix="blite-eval-") as directory:
        conn = init_db(Path(directory) / "state.db")
        try:
            run = create_run(
                conn,
                actor,
                "ui",
                "boundary",
                RunRequest(request="회의 할 일 추출", project_id="eval"),
            )
            run = install_plan(
                conn,
                run["id"],
                PlanV1(requirements=[Requirement(id="r1", text="회의 할 일 추출")], todos=[todo]),
                actor,
                tools={},
            )
            context = {
                "conn": conn,
                "actor": actor,
                "run": run,
                "todo": todo,
                "purpose": "generate",
            }
            request = build_model_request(todo, {}, context, binding=binding)
            metered_complete(request, binding, context, transport=transport, wait=lambda _: None)
            totals = summarize_usage(conn, run["id"], actor)
        finally:
            conn.close()
    if len(attempts) != 2 or totals["total_tokens"] != 23 or totals["actual_micro_usd"] is not None:
        raise AssertionError("metering boundary lost failed usage or invented a price")
    return {
        "evidence_layer": "offline_boundary_smoke",
        "live": False,
        "network_calls": 0,
        "transport_fixture_attempts": len(attempts),
        "fixture_tokens_including_failed": 23,
        "actual_micro_usd": None,
        "workload_executed": False,
        "model_quality_pass_rate": None,
        "token_reduction": None,
    }


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("evaluation input exceeds 16 MiB")

    def reject_constant(_):
        raise ValueError("nonfinite JSON")

    def no_duplicate_keys(pairs):
        unique([key for key, _ in pairs])
        return dict(pairs)

    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=reject_constant,
        object_pairs_hook=no_duplicate_keys,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "manifest", "smoke", "blind", "score"])
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = load_cases(args.cases)
    records = (
        validate_observations(read_json(args.observations), cases) if args.observations else []
    )
    labels = import_labels(read_json(args.labels), records) if args.labels else []
    if args.command == "validate":
        report = {
            "evidence_layer": "corpus_schema_only",
            "cases": len(cases),
            "categories": dict(Counter(case.category for case in cases)),
            "corpus_hash": fingerprint([case.model_dump() for case in cases]),
            "workload_executed": False,
            "model_quality_pass_rate": None,
            "token_reduction": None,
        }
    elif args.command == "manifest":
        report = {
            "protocol": PROTOCOL,
            "jobs": [
                {
                    "case_id": case.id,
                    "arm": arm,
                    "cache": cache,
                    "condition_hash": condition_hash(case),
                }
                for case in cases
                for arm in ("A", "B", "C")
                for cache in ("cold", "warm")
            ],
        }
    elif args.command == "smoke":
        report = boundary_smoke()
    elif args.command == "blind":
        report = blind_packet(records, cases)
    else:
        report = score(cases, records, labels)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
