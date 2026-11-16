"""Synthetic approval scenario shared by channel integration tests."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from kelly import db
from kelly.channels import router
from kelly.contracts import Actor, RunRequest, canonical_json, digest

from .fake import FakeAdapter

NOW = datetime(2026, 9, 8, tzinfo=UTC)
OWNER = Actor(id="owner", project_ids=["sales"])


def message(event_id, text):
    return {"update_id": event_id, "message": {
        "message_id": event_id, "from": {"id": 101, "is_bot": False},
        "chat": {"id": 101, "type": "private"}, "text": text,
    }}


def callback(event_id, data, *, user=101):
    return {"update_id": event_id, "callback_query": {
        "id": f"query-{event_id}", "from": {"id": user, "is_bot": False},
        "message": {"message_id": 1, "chat": {"id": user, "type": "private"}}, "data": data,
    }}


def pair(conn, adapter):
    router.register_connection(conn, OWNER, "personal", "sales", now=NOW, client=adapter)
    code = router.create_pairing(conn, OWNER, "personal", now=NOW)["code"]
    router.ingest_update(conn, OWNER, "personal", message(1, "/pair " + code), now=NOW)


def pending(conn):
    run = db.create_run(
        conn, OWNER, "ui", "web-request", RunRequest(request="합성 결과 전달", project_id="sales")
    )
    conn.execute("UPDATE runs SET state='awaiting_approval' WHERE id=?", (run["id"],))
    conn.execute(
        "INSERT INTO steps(id,run_id,ordinal,kind,effect,state,spec_json) "
        "VALUES('step',?,0,'apply','external_write','awaiting_approval','{}')", (run["id"],),
    )
    proposal = {
        "tool": "artifact.publish", "destination": "exports/synthetic.txt", "recipient_ref": "owner",
        "impact": "선택한 결과를 표시된 대상에 전달합니다.", "project_id": "sales",
    }
    conn.execute(
        "INSERT INTO approvals(id,run_id,step_id,actor_id,proposal_hash,proposal_json,expires_at,created_at) "
        "VALUES('approval',?,'step',?,?,?,?,?)",
        (run["id"], OWNER.id, digest(proposal), canonical_json(proposal),
         (NOW + timedelta(minutes=30)).isoformat(), NOW.isoformat()),
    )
    return run["id"]


@dataclass(frozen=True, slots=True)
class Scenario:
    conn: db.sqlite3.Connection
    adapter: FakeAdapter
    home: db.Path
    run_id: str

    def approval(self):
        return db.row_dict(self.conn.execute("SELECT * FROM approvals").fetchone())

    def queue(self, now=NOW):
        router.queue_results(self.conn, OWNER, "personal", self.adapter, now=lambda: now)

    def drain(self, now=NOW):
        rows = []
        while (row := router.deliver_once(self.adapter, self.conn, self.home, OWNER, now=now)):
            rows.append(row)
            assert len(rows) < 20
        return rows

    def press(self, data, event_id=2, *, user=101, now=NOW):
        return router.ingest_update(
            self.conn, OWNER, "personal", callback(event_id, data, user=user),
            now=now, client=self.adapter,
        )

    def show(self):
        self.queue()
        assert [row["state"] for row in self.drain()] == ["sent"]
        return self.adapter.messages["1"]
