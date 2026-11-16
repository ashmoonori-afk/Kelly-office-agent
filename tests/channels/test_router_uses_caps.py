"""Routing decisions follow transport capabilities, with the existing database format."""

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal

import pytest

from kelly import db
from kelly.channels import router
from kelly.channels.base import InboundEvent
from kelly.contracts import Actor, AppError, canonical_json

from .fake import FakeAdapter

NOW = datetime(2026, 9, 8, tzinfo=UTC)
OWNER = Actor(id="owner", project_ids=["sales"])


def event(event_id, text, *, kind: Literal["text", "pair"] = "text", action=None):
    return InboundEvent(
        event_id,
        kind,
        hashlib.sha256(text.encode()).hexdigest(),
        len(text.encode()),
        user_id=101,
        chat_id=101,
        private=True,
        human=True,
        text=text,
        action=action,
    )


def paired(conn, adapter):
    router.register_connection(conn, OWNER, "personal", "sales", now=NOW, client=adapter)
    code = router.create_pairing(conn, OWNER, "personal", now=NOW)["code"]
    router.ingest_event(
        conn, OWNER, "personal", event(1, "/pair " + code, kind="pair", action=code), now=NOW
    )
    received = router.ingest_event(conn, OWNER, "personal", event(2, "Synthetic request"), now=NOW)
    assert received is not None
    conn.execute("UPDATE runs SET state='succeeded' WHERE id=?", (received["run_id"],))
    return received["run_id"]


@pytest.mark.parametrize(
    "capability,error",
    [
        ("buttons", "UNSUPPORTED_CAPABILITY"),
        ("max_text", "INVALID_INPUT"),
        ("max_action_bytes", "UNSUPPORTED_CAPABILITY"),
    ],
)
def test_unsupported_delivery_fails_before_claim_or_transport(tmp_path, capability, error):
    adapter = FakeAdapter()
    adapter.name = "telegram"  # A vendor name cannot override capabilities.
    adapter.caps = replace(adapter.caps, **{capability: False if capability == "buttons" else 3})
    with closing(db.init_db(tmp_path / "state.db")) as conn:
        run_id = paired(conn, adapter)
        markup = (
            None
            if capability == "max_text"
            else {
                "inline_keyboard": [[{"text": "Approve", "callback_data": "approve:" + "a" * 24}]]
            }
        )
        router.prepare_delivery(
            conn, OWNER, "personal", run_id, text="Complete", reply_markup=markup, now=NOW
        )
        result = router.deliver_once(adapter, conn, tmp_path, OWNER, now=NOW)
        assert (result["state"], result["error_code"], result["attempt_count"]) == (
            "failed",
            error,
            0,
        )
    assert adapter.send_calls == 0 and adapter.messages == {}


def test_supported_delivery_works_for_a_different_adapter_name(tmp_path):
    adapter = FakeAdapter()
    with closing(db.init_db(tmp_path / "state.db")) as conn:
        run_id = paired(conn, adapter)
        router.prepare_delivery(conn, OWNER, "personal", run_id, text="Complete", now=NOW)
        result = router.deliver_once(adapter, conn, tmp_path, OWNER, now=NOW)
        assert (result["state"], result["remote_id"], result["attempt_count"]) == ("sent", "1", 1)
    assert adapter.messages["1"].text == "Complete"


def test_files_disabled_rejects_valid_document_before_opening(tmp_path):
    adapter = FakeAdapter()
    adapter.caps = replace(adapter.caps, files=False)
    with closing(db.init_db(tmp_path / "state.db")) as conn:
        run_id = paired(conn, adapter)
        content = b"Synthetic result"
        checksum = hashlib.sha256(content).hexdigest()
        (tmp_path / "exports").mkdir()
        (tmp_path / "exports/result.txt").write_bytes(content)
        conn.execute(
            "INSERT INTO artifacts(id,actor_id,project_id,run_id,kind,relative_path,sha256,media_type,"
            "byte_size,provenance_json,validation_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "result",
                OWNER.id,
                "sales",
                run_id,
                "result",
                "exports/result.txt",
                checksum,
                "text/plain",
                len(content),
                canonical_json({"filename": "result.txt"}),
                canonical_json({"status": "pass"}),
                NOW.isoformat(),
            ),
        )
        conn.execute(
            "UPDATE runs SET result_json=? WHERE id=?",
            (
                canonical_json(
                    {
                        "outputs": {
                            "publish": {
                                "receipt": {
                                    "run_id": run_id,
                                    "artifact_id": "result",
                                    "artifact_hash": checksum,
                                    "validation": {"status": "pass"},
                                }
                            }
                        }
                    }
                ),
                run_id,
            ),
        )
        router.prepare_delivery(conn, OWNER, "personal", run_id, artifact_id="result", now=NOW)
        result = router.deliver_once(adapter, conn, tmp_path, OWNER, now=NOW)
        assert (result["state"], result["error_code"], result["attempt_count"]) == (
            "failed",
            "UNSUPPORTED_CAPABILITY",
            0,
        )
    assert adapter.send_calls == 0


def test_push_adapter_is_not_polled(tmp_path):
    adapter = FakeAdapter()
    adapter.caps = replace(adapter.caps, inbound="push")
    with closing(db.init_db(tmp_path / "state.db")) as conn:
        paired(conn, adapter)
        with pytest.raises(AppError) as caught:
            router.poll_once(adapter, conn, OWNER, "personal", now=NOW)
    assert caught.value.code == "UNSUPPORTED_CAPABILITY" and adapter.cursors == []


def test_batch_cursor_advances_only_after_each_event_commits(tmp_path):
    adapter = FakeAdapter((event(3, "Third request"), event(4, "Fourth request")))
    with closing(db.init_db(tmp_path / "state.db")) as conn:
        paired(conn, adapter)
        conn.execute(
            "CREATE TRIGGER fail_fourth BEFORE INSERT ON runs WHEN NEW.idempotency_key='personal:4' "
            "BEGIN SELECT RAISE(ABORT,'synthetic fourth event failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic fourth event failure"):
            router.poll_once(adapter, conn, OWNER, "personal", now=NOW)
        assert db.get_value(conn, "telegram:cursor:personal") == 4
        assert (
            conn.execute("SELECT event_key FROM inbox_events ORDER BY id").fetchall()[-1][0] == "3"
        )
        conn.execute("DROP TRIGGER fail_fourth")
        result = router.poll_once(adapter, conn, OWNER, "personal", now=NOW)
        assert len(result) == 1
        received = result[0]
        assert received is not None and received["event_key"] == "4"
        assert db.get_value(conn, "telegram:cursor:personal") == 5
    assert adapter.cursors == ["3", "4"]


@pytest.mark.parametrize("buttons,queued", [(True, 1), (False, 0)])
def test_approval_notice_requires_buttons_before_creating_tokens(tmp_path, buttons, queued):
    adapter = FakeAdapter()
    adapter.caps = replace(adapter.caps, buttons=buttons)
    with closing(db.init_db(tmp_path / "state.db")) as conn:
        run_id = paired(conn, adapter)
        conn.execute("UPDATE runs SET state='awaiting_approval' WHERE id=?", (run_id,))
        conn.execute(
            "INSERT INTO steps(id,run_id,ordinal,kind,effect,state,spec_json) "
            "VALUES('step',?,0,'apply','external_write','awaiting_approval','{}')",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO approvals(id,run_id,step_id,actor_id,proposal_hash,proposal_json,expires_at,created_at) "
            "VALUES('approval',?,'step',?,'synthetic-proposal','{}',?,?)",
            (run_id, OWNER.id, "2026-09-08T00:05:00+00:00", NOW.isoformat()),
        )
        router.queue_results(conn, OWNER, "personal", adapter, now=lambda: NOW)
        assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == queued
        assert (
            conn.execute("SELECT count(*) FROM channel_actions").fetchone()[0]
            == queued * 2
        )
        if queued:
            row = db.row_dict(conn.execute("SELECT * FROM outbox").fetchone())
            assert row is not None
            payload = row["payload_json"]
            assert payload["text"].startswith("업무 실행 승인이 필요합니다.")
            assert [button["text"] for button in payload["reply_markup"]["inline_keyboard"][0]] == [
                "승인",
                "거절",
            ]
    assert adapter.send_calls == 0
