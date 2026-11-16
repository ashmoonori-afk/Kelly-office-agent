from kelly.engine import resolve_approval

from .approval_support import NOW, OWNER


def test_web_decision_updates_existing_messenger_notice(notice):
    notice.show()
    approval = notice.approval()
    resolve_approval(
        notice.conn, approval["id"], OWNER, "reject", approval["proposal_hash"],
        approval["revision"], now=NOW, reject_reason="Synthetic correction required",
    )
    notice.queue()
    assert notice.drain()[0]["state"] == "sent"
    assert notice.adapter.messages["1"].actions == ()
    assert "거절됨 · owner · 2026-09-08 09:00 KST" in notice.adapter.messages["1"].text
    assert notice.approval()["resolved_via"] == "web"
    assert notice.approval()["reject_reason"] == "Synthetic correction required"
    notice.queue()
    assert notice.drain() == []
