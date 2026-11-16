import pytest

from kelly.channels import router
from kelly.contracts import AppError

from .approval_support import NOW, OWNER


def test_web_run_approval_has_one_durable_notice(notice):
    notice.queue()
    notice.queue()
    assert notice.conn.execute("SELECT source FROM runs").fetchone()[0] == "ui"
    assert notice.conn.execute("SELECT count(*) FROM approval_notices").fetchone()[0] == 1
    assert notice.conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    assert notice.conn.execute("SELECT count(*) FROM channel_actions").fetchone()[0] == 2
    assert notice.adapter.messages == {}
    assert notice.drain()[0]["state"] == "sent"
    assert len(notice.adapter.messages["1"].actions[0]) == 2


def test_notice_does_not_grant_arbitrary_web_delivery(notice):
    with pytest.raises(AppError) as error:
        router.prepare_delivery(
            notice.conn, OWNER, "personal", notice.run_id, text="Unrelated", now=NOW
        )
    assert error.value.code == "POLICY_DENIED"
