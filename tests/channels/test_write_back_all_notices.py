from dataclasses import replace

import pytest

from kelly import db
from kelly.channels import router

from .approval_support import NOW, OWNER


@pytest.mark.parametrize("edit", [True, False])
def test_resolution_updates_each_notice_or_sends_one_followup(notice, edit):
    card = notice.show()
    connection = db.get_value(notice.conn, "telegram:connection:personal")
    connection.update(id="secondary", chat_id=202, user_id=202)
    db.set_value(notice.conn, "telegram:connection:secondary", connection)
    notice.conn.execute(
        "INSERT INTO channel_bindings VALUES ('secondary','owner','secondary','202','paired',2,?)",
        (NOW.isoformat(),),
    )
    router.queue_results(notice.conn, OWNER, "secondary", notice.adapter, now=lambda: NOW)
    assert notice.drain()[0]["state"] == "sent"
    notice.adapter.caps = replace(notice.adapter.caps, edit_message=edit)
    assert notice.press(card.actions[0][0].data)["state"] == "enqueued"
    for connection_id in ("personal", "secondary"):
        router.queue_results(notice.conn, OWNER, connection_id, notice.adapter, now=lambda: NOW)
    assert [row["state"] for row in notice.drain()] == ["sent", "sent"]
    outcomes = [message for message in notice.adapter.messages.values() if "승인됨" in message.text]
    assert len(outcomes) == 2
    assert all(message.actions == () and "owner" in message.text and "09:00 KST" in message.text
               for message in outcomes)
    assert {row[0] for row in notice.conn.execute("SELECT state FROM approval_notices")} == {"updated"}
    for connection_id in ("personal", "secondary"):
        router.queue_results(notice.conn, OWNER, connection_id, notice.adapter, now=lambda: NOW)
    assert notice.drain() == []
