import io

import pytest

from birkin_lite import office, tools
from birkin_lite.artifacts import import_source
from birkin_lite.contracts import Actor, AppError, RunRequest, TaskSpec
from birkin_lite.db import (
    allowed_sources,
    create_run,
    get_run,
    get_task_spec,
    get_value,
    init_db,
    set_value,
    supply_inputs,
)

OWNER = Actor(id="owner", project_ids=["sales", "other"])


def prepared(tmp_path):
    conn = init_db(tmp_path / "state.db")
    home = tmp_path / "files"
    first = import_source(
        home, conn, io.BytesIO(b"first"), {"filename": "first.txt"}, OWNER, "sales"
    )
    extra = import_source(
        home, conn, io.BytesIO(b"extra"), {"filename": "extra.txt"}, OWNER, "sales"
    )
    run = create_run(
        conn,
        OWNER,
        "ui",
        "request",
        RunRequest(
            request="자료를 정리해 줘",
            project_id="sales",
            artifact_ids=[first["id"]],
            output_format="docx",
        ),
    )
    return conn, home, first, extra, run


def task(source_ids):
    return TaskSpec(
        intent="자료 정리",
        source_ids=source_ids,
        inputs={"user_corrections": ["추가 자료도 포함해 주세요"]},
        acceptance=["자료의 근거 포함"],
    )


def test_only_user_confirmation_expands_scope_with_cas_and_immutable_request(tmp_path):
    conn, home, first, extra, run = prepared(tmp_path)
    set_value(conn, "task_spec:" + run["id"], task([extra["id"]]).model_dump())
    assert allowed_sources(conn, run, OWNER) == [first["id"]]
    with pytest.raises(AppError):
        get_task_spec(conn, run["id"], OWNER)
    forged = {**run, "request_json": {**run["request_json"], "artifact_ids": [extra["id"]]}}
    assert allowed_sources(conn, forged, OWNER) == [first["id"]]
    updated = supply_inputs(
        conn, run["id"], OWNER, task([first["id"], extra["id"]]), run["revision"]
    )
    assert updated["revision"] == run["revision"] + 1
    assert updated["policy_json"]["scope_revision"] == 2
    assert allowed_sources(conn, run, OWNER) == [first["id"], extra["id"]]
    assert get_task_spec(conn, run["id"], OWNER).inputs["user_corrections"]
    for key in ("request_json", "request_hash", "idempotency_key"):
        assert updated[key] == run[key]
    with pytest.raises(AppError) as error:
        supply_inputs(conn, run["id"], OWNER, task([]), run["revision"])
    assert error.value.code == "CONFLICT"
    assert get_value(conn, "confirmed_sources:" + run["id"]) == [extra["id"]]
    context = {"conn": conn, "home": home, "run": run, "actor": OWNER}
    assert (
        tools.extract({"artifact_id": extra["id"]}, context)["evidence"]["items"][0]["quote"]
        == "extra"
    )
    generated = office.create_document("office-report-v1", {"title": "정리"}, [], context)
    assert extra["id"] in generated["provenance_json"]["source_versions"]
    conn.close()


def test_scope_rechecks_deletion_owner_project_state_and_total_limits(tmp_path):
    conn, home, _first, extra, run = prepared(tmp_path)
    for actor in (Actor(id="intruder", project_ids=["sales"]), Actor(id="owner", project_ids=[])):
        with pytest.raises(AppError):
            supply_inputs(conn, run["id"], actor, task([extra["id"]]), run["revision"])
    conn.execute("UPDATE artifacts SET project_id='other' WHERE id=?", (extra["id"],))
    with pytest.raises(AppError):
        supply_inputs(conn, run["id"], OWNER, task([extra["id"]]), run["revision"])
    assert get_value(conn, "confirmed_sources:" + run["id"]) is None
    with pytest.raises(AppError) as error:
        supply_inputs(
            conn, run["id"], OWNER, task([f"extra{i}" for i in range(20)]), run["revision"]
        )
    assert error.value.code == "LIMIT_EXCEEDED"
    conn.execute(
        "UPDATE artifacts SET project_id='sales',byte_size=? WHERE id=?",
        (100 * 1024 * 1024, extra["id"]),
    )
    with pytest.raises(AppError) as error:
        supply_inputs(conn, run["id"], OWNER, task([extra["id"]]), run["revision"])
    assert error.value.code == "LIMIT_EXCEEDED"
    conn.execute("UPDATE artifacts SET byte_size=5 WHERE id=?", (extra["id"],))
    conn.execute("UPDATE runs SET state='running' WHERE id=?", (run["id"],))
    with pytest.raises(AppError):
        supply_inputs(conn, run["id"], OWNER, task([extra["id"]]), run["revision"])
    conn.execute("UPDATE runs SET state='awaiting_input' WHERE id=?", (run["id"],))
    updated = supply_inputs(conn, run["id"], OWNER, task([extra["id"]]), run["revision"])
    assert updated["state"] == "awaiting_input"
    conn.execute(
        "UPDATE artifacts SET deleted_at='2026-09-08T00:00:00Z' WHERE id=?", (extra["id"],)
    )
    with pytest.raises(AppError):
        allowed_sources(conn, run, OWNER)
    with pytest.raises(AppError):
        tools.check_sources({"conn": conn, "home": home, "run": run, "actor": OWNER})
    conn.close()


def test_general_read_cannot_gain_scope_but_explicit_project_search_can(tmp_path):
    conn, home, _first, extra, run = prepared(tmp_path)
    context = {"conn": conn, "home": home, "run": run, "actor": OWNER}
    with pytest.raises(AppError):
        tools.extract({"artifact_id": extra["id"]}, context)
    with pytest.raises(AppError):
        office.extract(extra["id"], {}, {}, context)
    result = tools.search({"query": "extra", "source_ids": [extra["id"]]}, context)
    assert result["evidence"]["items"][0]["artifact_id"] == extra["id"]
    assert tools.search({"query": "extra"}, context)["evidence"]["items"] == []
    supply_inputs(conn, run["id"], OWNER, task([extra["id"]]), run["revision"])
    assert (
        tools.search({"query": "extra"}, context)["evidence"]["items"][0]["artifact_id"]
        == extra["id"]
    )
    assert get_run(conn, run["id"], OWNER)["request_json"] == run["request_json"]
    conn.close()


def test_input_change_expires_existing_authorization_atomically(tmp_path):
    conn = init_db(tmp_path / "state.db")
    run = create_run(conn, OWNER, "ui", "approval", RunRequest(request="정리", project_id="sales"))
    conn.execute(
        "INSERT INTO steps (id,run_id,ordinal,kind,effect,state,spec_json) "
        "VALUES ('s1',?,0,'apply','external_write','awaiting_approval','{}')",
        (run["id"],),
    )
    conn.execute(
        "INSERT INTO approvals (id,run_id,step_id,actor_id,proposal_hash,proposal_json,state,expires_at,created_at) "
        "VALUES ('a1',?,'s1','owner','hash','{}','approved','2099-01-01T00:00:00Z','2026-09-08T00:00:00Z')",
        (run["id"],),
    )
    updated = supply_inputs(conn, run["id"], OWNER, task([]), run["revision"])
    approval = conn.execute("SELECT state,revision,resolved_at FROM approvals").fetchone()
    assert approval["state"] == "expired" and approval["revision"] == 2 and approval["resolved_at"]
    assert updated["policy_json"]["scope_revision"] == 2
    with pytest.raises(AppError):
        supply_inputs(conn, run["id"], OWNER, task([]), run["revision"])
    assert get_run(conn, run["id"], OWNER)["policy_json"]["scope_revision"] == 2
    conn.close()
