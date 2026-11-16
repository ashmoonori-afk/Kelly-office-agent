from contextlib import closing

import pytest
from test_engine import plan

from kelly import db, engine

from .approval_support import NOW, OWNER, Scenario, pair
from .fake import FakeAdapter


@pytest.mark.parametrize("via", ["telegram", "web"])
def test_web_task_notice_decision_inbox_resume_and_chat_result(web, tmp_path, via):
    client, headers = web
    created = client.post("/api/runs", headers=headers,
                          json={"request": "합성 결과 전달", "project_id": "sales"})
    assert created.status_code == 202
    run_id = created.json()["run"]["id"]
    effects = []
    tools = {
        "source.inspect": engine.Tool("read", lambda _a, _c: {"source": {"format": "txt"}}),
        "source.extract": engine.Tool("read", lambda _a, _c: {"evidence": []}),
        "artifact.publish": engine.Tool(
            "external_write", lambda _a, _c: effects.append("sent") or {"receipt": {"id": "synthetic-receipt"}}
        ),
    }
    with closing(db.init_db(tmp_path / "state.db")) as conn:
        synthetic = plan()
        synthetic.todos[-1].executor.args.update(destination="exports/synthetic.txt", recipient_ref="owner")
        engine.install_plan(conn, run_id, synthetic, OWNER, tools=tools)
        for _ in range(3):
            engine.execute_next(conn, run_id, actor=OWNER, tools=tools, complete=None, now=NOW)
        assert effects == []
        adapter = FakeAdapter()
        pair(conn, adapter)
        scenario = Scenario(conn, adapter, tmp_path, run_id)
        card = scenario.show()
        assert all(value in card.text for value in (
            "artifact.publish", "exports/synthetic.txt", "owner", "2026-09-08 09:30 KST"
        ))
        approval = client.get("/api/approvals").json()["approvals"][0]
        if via == "telegram":
            assert scenario.press(card.actions[0][0].data)["state"] == "enqueued"
        else:
            response = client.post(f"/api/approvals/{approval['id']}/resolve", headers=headers, json={
                "decision": "approve", "proposal_hash": approval["proposal_hash"],
                "expected_revision": approval["revision"],
            })
            assert response.status_code == 200
        scenario.queue()
        scenario.drain()
        assert adapter.messages["1"].actions == ()
        assert "승인됨 · owner · 2026-09-08 09:00 KST" in adapter.messages["1"].text
        assert client.get("/api/approvals").json()["approvals"] == []
        resolved = client.get(f"/api/runs/{run_id}").json()["approvals"][0]
        assert (resolved["state"], resolved["resolved_by"], resolved["resolved_via"]) == (
            "approved", "owner", via
        )
        engine.execute_next(conn, run_id, actor=OWNER, tools=tools, complete=None, now=NOW)
        assert db.get_run(conn, run_id, OWNER)["state"] == "succeeded"
        assert effects == ["sent"]
        scenario.queue()
        sent = scenario.drain()
        assert len(sent) == 1 and sent[0]["delivery_kind"] == "approval_result"
        assert "synthetic-receipt" in adapter.messages["2"].text
        assert scenario.approval()["state"] == "consumed"
        assert scenario.press(card.actions[0][0].data, 3)["state"] == "rejected"
        assert effects == ["sent"]
        scenario.queue()
        assert scenario.drain() == []
