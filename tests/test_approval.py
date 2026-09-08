import pytest
from test_engine import NOW, OWNER, setup_run

from birkin_lite.contracts import ActionProposal, PlanV1, canonical_json, digest
from birkin_lite.db import get_run, row_dict
from birkin_lite.engine import execute_next, install_plan, resolve_approval


def pending(tmp_path):
    conn, rid, tools, seen = setup_run(tmp_path, external=True)
    plan = get_run(conn, rid, OWNER)["plan_json"]["current"]
    last = plan["todos"][-1]
    last["executor"]["args"] = {"destination": "test-destination", "recipient_ref": "owner"}
    last["inputs"] = [{"step_id": "t1", "output_key": "evidence"}]
    install_plan(conn, rid, PlanV1.model_validate(plan), OWNER, tools=tools)
    for _ in range(3):
        execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    approval = row_dict(conn.execute("SELECT * FROM approvals").fetchone())
    assert approval["state"] == "pending" and seen == ["inspect", "extract"]
    proposal = ActionProposal.model_validate(approval["proposal_json"])
    assert proposal.recipient_ref == "owner" and proposal.destination == "test-destination"
    assert approval["proposal_hash"] == digest(proposal)
    resolve_approval(conn, approval["id"], OWNER, "approve", approval["proposal_hash"], 1, now=NOW)
    return conn, rid, tools, seen, approval


@pytest.mark.parametrize("changed", ["recipient", "destination", "output", "scope"])
def test_approved_proposal_change_is_detected_before_external_effect(tmp_path, changed):
    conn, rid, tools, seen, _ = pending(tmp_path)
    if changed in {"recipient", "destination"}:
        row = row_dict(conn.execute("SELECT * FROM steps WHERE ordinal=2").fetchone())
        args = row["spec_json"]["todo"]["executor"]["args"]
        args["recipient_ref" if changed == "recipient" else "destination"] = "different"
        conn.execute(
            "UPDATE steps SET spec_json=? WHERE id=?", (canonical_json(row["spec_json"]), row["id"])
        )
    elif changed == "output":
        conn.execute(
            "UPDATE steps SET output_json=? WHERE ordinal=1",
            (canonical_json({"evidence": ["changed"]}),),
        )
    else:
        conn.execute(
            "UPDATE runs SET policy_json=? WHERE id=?", (canonical_json({"scope_revision": 2}), rid)
        )
    result = execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    assert result["state"] == "failed" and result["error_code"] == "SOURCE_CHANGED"
    assert seen == ["inspect", "extract"]
    assert conn.execute("SELECT state FROM approvals").fetchone()[0] == "approved"
    conn.close()


def test_approval_consumption_rolls_back_if_step_claim_fails(tmp_path):
    conn, rid, tools, seen, _ = pending(tmp_path)
    conn.execute("""CREATE TRIGGER interrupt_claim AFTER UPDATE OF state ON approvals
        WHEN NEW.state='consumed' BEGIN
        UPDATE steps SET state='cancelled' WHERE id=NEW.step_id;
        END""")
    result = execute_next(conn, rid, actor=OWNER, tools=tools, complete=None, now=NOW)
    assert result["state"] == "failed" and seen == ["inspect", "extract"]
    approval = row_dict(conn.execute("SELECT * FROM approvals").fetchone())
    assert approval["state"] == "approved" and approval["consumed_at"] is None
    conn.close()
