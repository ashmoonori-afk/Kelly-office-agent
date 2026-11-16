"""Real DB/enqueue/planning boundaries; source inspection and models are finite fixtures."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_automation import ANCHOR, OWNER, config, enabled, event
from test_prepare_run import POLICY, binding, planned, result

from kelly import db
from kelly.contracts import AppError, PrivacyPolicy, canonical_json
from kelly.planning import prepare_run
from kelly.tools import registry


@pytest.fixture
def bot_work(tmp_path):
    conn = db.init_db(tmp_path / "state.db")
    source = {"artifact_id": "source1", "format": "txt", "sha256": "a" * 64}
    conn.execute(
        "INSERT INTO artifacts (id,actor_id,project_id,kind,relative_path,sha256,media_type,"
        "byte_size,provenance_json,created_at) VALUES "
        "('source1','owner','sales','source','sources/fixture.txt',?,'text/plain',1,'{}',?)",
        (source["sha256"], ANCHOR.isoformat()),
    )
    bot = enabled(
        conn,
        config(
            instruction="두 대안을 비교하고 근거를 표시",
            source_scope={"artifact_ids": ["source1"]},
        ),
    )
    registered = registry(tmp_path)
    registered["source.inspect"] = replace(
        registered["source.inspect"], execute=lambda *_: {"source": dict(source)}
    )

    def enqueue(index=0):
        when = ANCHOR + timedelta(minutes=5 * index)
        return db.enqueue_event_once(event(bot, when), conn, actor=OWNER, now=when)

    def output():
        value = planned()
        value["task_spec"]["source_ids"] = ["source1"]
        analysis = value["plan"]["todos"][0]
        analysis.update(
            id="t2", depends_on=["t1"], inputs=[{"step_id": "t1", "output_key": "evidence"}]
        )
        extract = {
            **analysis,
            "id": "t1",
            "title": "원문 추출",
            "kind": "mechanical",
            "signals": ["fixed_transform"],
            "depends_on": [],
            "inputs": [{"artifact_id": "source1"}],
            "executor": {
                "type": "tool",
                "tool_name": "source.extract",
                "args": {"artifact_id": "source1"},
            },
            "output_schema": "EvidenceListV1",
        }
        value["plan"]["todos"] = [extract, analysis]
        return value

    def prepare(run, complete=None, **overrides):
        return prepare_run(
            conn,
            tmp_path,
            run["id"],
            OWNER,
            tools=registered,
            capability_catalog=overrides.pop("capability_catalog", [binding()]),
            privacy_policy=overrides.pop("privacy_policy", POLICY),
            complete=complete or (lambda *_: result(output())),
            **overrides,
        )

    def finish(run):
        # Seed prior completion only; no parser, model transport or Office quality claim.
        conn.execute("UPDATE runs SET state='succeeded' WHERE id=?", (run["id"],))
        conn.execute(
            "UPDATE steps SET state='succeeded',attempt_count=1,output_json=? WHERE run_id=?",
            (canonical_json({"old": True}), run["id"]),
        )

    yield conn, bot, source, registered, enqueue, output, prepare, finish
    conn.close()


def test_exact_source_reuses_only_structure_and_revalidates_current_binding(bot_work):
    conn, _, _, _, enqueue, _, prepare, finish = bot_work
    first = prepare(enqueue())
    finish(first)
    old_steps = [dict(row) for row in conn.execute("SELECT * FROM steps")]
    current = binding().model_copy(update={"model_id": "new-qualified"})
    second = prepare(
        enqueue(1),
        lambda *_: pytest.fail("Exact-source reuse called a planner"),
        capability_catalog=[current],
    )
    assert second["state"] == "running" and second["id"] != first["id"]
    assert second["plan_json"]["history"] == [] and second["plan_json"]["revision"] == 1
    assert db.get_value(conn, "route_bindings:" + second["id"])["t2"]["model_id"] == "new-qualified"
    for step in conn.execute("SELECT * FROM steps WHERE run_id=?", (second["id"],)):
        assert step["state"] == "pending" and step["attempt_count"] == 0
        assert step["output_json"] is None and step["validation_json"] is None
        assert (
            "model_grounding" in json.loads(step["spec_json"])["todo"]["verify_with"]
            or step["kind"] == "tool"
        )
    assert [
        dict(row) for row in conn.execute("SELECT * FROM steps WHERE run_id=?", (first["id"],))
    ] == old_steps
    assert (
        conn.execute("SELECT count(*) FROM approvals WHERE run_id=?", (second["id"],)).fetchone()[0]
        == 0
    )


def test_changed_source_replans_with_structure_and_no_completed_ids(bot_work):
    _, _, source, _, enqueue, output, prepare, finish = bot_work
    first = prepare(enqueue())
    finish(first)
    source["sha256"] = "b" * 64
    calls = []

    def reassess(request, selected, context):
        payload = json.loads(request.context.split("\nDATA\n", 1)[1])
        assert payload["previous_plan"] == first["plan_json"]["current"]
        assert payload["completed_ids"] == []
        assert payload["validation_errors"] == ["SOURCE_CHANGED"]
        assert payload["sources"][0]["sha256"] == "b" * 64
        calls.append(context["purpose"])
        return result(output())

    prepare(enqueue(1), reassess)
    assert calls == ["plan"]


@pytest.mark.parametrize("change", ["unavailable", "privacy", "tool", "disabled", "config_hash"])
def test_reuse_never_bypasses_current_execution_boundaries(bot_work, change):
    conn, bot, _, registered, enqueue, _, prepare, finish = bot_work
    first = prepare(enqueue())
    finish(first)
    second = enqueue(1)
    overrides = {}
    if change == "unavailable":
        overrides["capability_catalog"] = []
    elif change == "privacy":
        overrides["privacy_policy"] = PrivacyPolicy(project_id="sales")
    elif change == "tool":
        registered.pop("source.extract")
    elif change == "disabled":
        db.set_automation_enabled(conn, bot["id"], bot["revision"], False, OWNER)
    else:
        policy = {**second["policy_json"], "automation_config_hash": "f" * 64}
        conn.execute(
            "UPDATE runs SET policy_json=? WHERE id=?", (canonical_json(policy), second["id"])
        )
    if change == "disabled":
        assert prepare(second)["state"] == "cancelled"
    else:
        with pytest.raises(AppError):
            prepare(second, lambda *_: pytest.fail("Denied work called a planner"), **overrides)
    assert (
        conn.execute("SELECT count(*) FROM steps WHERE run_id=?", (second["id"],)).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("change", ["inputs", "format", "scope", "prior_actor", "prior_failure"])
def test_mismatched_context_does_not_supply_a_prior_plan(bot_work, change):
    conn, _, _, _, enqueue, output, prepare, finish = bot_work
    first = prepare(enqueue())
    finish(first)
    second = enqueue(1)
    if change == "inputs":
        spec = db.get_task_spec(conn, second["id"], OWNER)
        spec.inputs["user_corrections"] = "대안 A와 B"
        db.set_value(conn, "task_spec:" + second["id"], spec)
    elif change == "format":
        value = {**second["request_json"], "output_format": "md"}
        conn.execute(
            "UPDATE runs SET request_json=? WHERE id=?", (canonical_json(value), second["id"])
        )
    elif change == "scope":
        value = {**second["policy_json"], "scope_revision": 99}
        conn.execute(
            "UPDATE runs SET policy_json=? WHERE id=?", (canonical_json(value), second["id"])
        )
    elif change == "prior_actor":
        conn.execute("UPDATE runs SET actor_id='other' WHERE id=?", (first["id"],))
    else:
        conn.execute("UPDATE runs SET state='failed' WHERE id=?", (first["id"],))
    calls = []

    def cold(request, *_):
        payload = json.loads(request.context.split("\nDATA\n", 1)[1])
        assert "previous_plan" not in payload
        value = output()
        value["task_spec"]["inputs"] = payload["task_spec"]["inputs"]
        calls.append(True)
        return result(value)

    prepare(second, cold)
    assert calls == [True]


def test_unversioned_search_rechecks_plan_even_with_unchanged_attachments(bot_work):
    conn, _, _, _, enqueue, output, prepare, finish = bot_work
    first = prepare(enqueue())
    plan = first["plan_json"]
    plan["current"]["todos"][0]["executor"] = {
        "type": "tool",
        "tool_name": "source.search",
        "args": {"query": "대안"},
    }
    conn.execute("UPDATE runs SET plan_json=? WHERE id=?", (canonical_json(plan), first["id"]))
    finish(first)
    calls = []

    def reassess(request, *_):
        payload = json.loads(request.context.split("\nDATA\n", 1)[1])
        assert payload["previous_plan"] == plan["current"]
        assert payload["completed_ids"] == []
        calls.append(True)
        return result(output())

    prepare(enqueue(1), reassess)
    assert calls == [True]
