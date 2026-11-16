import sqlite3

import pytest

from kelly.channels import router

from .approval_support import NOW, OWNER, callback


def test_second_press_and_replayed_update_leave_decision_and_token_unchanged(notice):
    data = notice.show().actions[0][0].data
    first = notice.press(data)
    approval = notice.approval()
    used = notice.conn.execute("SELECT used_at FROM channel_actions WHERE used_at IS NOT NULL").fetchone()[0]
    assert first["state"] == "enqueued"
    second = notice.press(data, 3)
    assert second["state"] == "rejected"
    assert second["payload_json"]["error_code"] == "POLICY_DENIED"
    assert notice.press(data) == first
    assert notice.adapter.ack_texts == [None, "이미 처리된 요청입니다.", "이미 처리된 요청입니다."]
    assert notice.approval() == approval
    assert notice.conn.execute("SELECT used_at FROM channel_actions WHERE used_at IS NOT NULL").fetchone()[0] == used
    assert notice.conn.execute("SELECT count(*) FROM inbox_events").fetchone()[0] == 3


def test_cursor_failure_rolls_back_inbound_token_and_decision_together(notice):
    data = notice.show().actions[0][0].data
    notice.conn.execute(
        "CREATE TRIGGER fail_cursor BEFORE UPDATE ON kv WHEN NEW.key='telegram:cursor:personal' "
        "BEGIN SELECT RAISE(ABORT,'synthetic cursor failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic cursor failure"):
        notice.press(data)
    assert notice.approval()["state"] == "pending"
    assert notice.conn.execute("SELECT count(*) FROM channel_actions WHERE used_at IS NOT NULL").fetchone()[0] == 0
    assert notice.conn.execute("SELECT count(*) FROM inbox_events").fetchone()[0] == 1
    notice.conn.execute("DROP TRIGGER fail_cursor")
    assert notice.press(data)["state"] == "enqueued"


def test_outer_transaction_abort_restores_resolved_press(notice):
    data = notice.show().actions[0][0].data
    notice.conn.execute(
        "CREATE TRIGGER fail_decision BEFORE UPDATE ON approvals "
        "BEGIN SELECT RAISE(ABORT,'synthetic decision failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        router.ingest_update(
            notice.conn, OWNER, "personal", callback(2, data), now=NOW, client=notice.adapter
        )
    assert notice.conn.execute("SELECT count(*) FROM inbox_events").fetchone()[0] == 1
