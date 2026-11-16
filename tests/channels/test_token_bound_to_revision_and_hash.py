import pytest

from kelly import db
from kelly.contracts import digest

from .approval_support import NOW


@pytest.mark.parametrize("change", ["revision", "hash", "decision", "user", "pairing", "binding_user"])
def test_changed_authority_cannot_reuse_button(notice, change):
    data = notice.show().actions[0][0].data
    user = 101
    if change == "revision":
        notice.conn.execute("UPDATE approvals SET revision=revision+1")
    elif change == "hash":
        notice.conn.execute("UPDATE approvals SET proposal_hash=?", (digest({"new": True}),))
    elif change == "decision":
        data = data.replace("approve:", "reject:")
    elif change == "user":
        user = 202
    elif change == "binding_user":
        notice.conn.execute("UPDATE channel_bindings SET user_id='other'")
    else:
        connection = db.get_value(notice.conn, "telegram:connection:personal")
        connection["revision"] += 1
        db.set_value(notice.conn, "telegram:connection:personal", connection, now=NOW.isoformat())
    assert notice.press(data, user=user)["state"] == "rejected"
    assert notice.approval()["state"] == "pending"
    assert notice.conn.execute("SELECT count(*) FROM channel_actions WHERE used_at IS NOT NULL").fetchone()[0] == 0
