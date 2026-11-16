from datetime import timedelta

from .approval_support import NOW


def test_expired_notice_removes_buttons_without_running_step(notice):
    notice.show()
    later = NOW + timedelta(minutes=30)
    notice.queue(later)
    notice.drain(later)
    assert notice.approval()["state"] == "expired"
    assert notice.adapter.messages["1"].actions == ()
    assert "기한이 지나" in notice.adapter.messages["1"].text
    assert notice.conn.execute("SELECT state FROM steps").fetchone()[0] == "awaiting_approval"


def test_expired_unsent_card_never_reaches_transport(notice):
    notice.queue()
    later = NOW + timedelta(minutes=31)
    notice.queue(later)
    notice.drain(later)
    assert all(not message.actions for message in notice.adapter.messages.values())
    notice.queue(later)
    assert notice.drain(later) == []
