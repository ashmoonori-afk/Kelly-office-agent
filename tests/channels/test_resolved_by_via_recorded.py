import pytest


@pytest.mark.parametrize("index,state", [(0, "approved"), (1, "rejected")])
def test_button_records_owner_channel_and_time(notice, index, state):
    notice.press(notice.show().actions[0][index].data)
    approval = notice.approval()
    assert (approval["state"], approval["resolved_by"], approval["resolved_via"]) == (
        state, "owner", "telegram"
    )
    assert approval["resolved_at"] == "2026-09-08T00:00:00+00:00"
