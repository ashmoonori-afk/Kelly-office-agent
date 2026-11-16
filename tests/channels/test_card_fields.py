from datetime import timedelta

from .approval_support import NOW


def test_card_names_current_action_target_recipient_impact_and_expiry(notice):
    card = notice.show()
    for field in ("artifact.publish", "exports/synthetic.txt", "owner",
                  "선택한 결과를 표시된 대상에 전달합니다.", "2026-09-08 09:30 KST"):
        assert field in card.text
    assert [action.text for action in card.actions[0]] == ["승인", "거절"]
    assert {row[0] for row in notice.conn.execute("SELECT expires_at FROM channel_actions")} == {
        (NOW + timedelta(minutes=30)).isoformat()
    }
    assert notice.press(card.actions[0][0].data, now=NOW + timedelta(minutes=20))["state"] == "enqueued"
