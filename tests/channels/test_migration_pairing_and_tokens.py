import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest

from kelly import db
from kelly.channels import router
from kelly.migrations import downgrade, upgrade

from .approval_support import NOW, OWNER, callback, pending
from .fake import FakeAdapter


@pytest.mark.parametrize("used", [False, True])
def test_v01_database_copy_up_down_preserves_pairing_and_legacy_actions(tmp_path, used):
    source, target = tmp_path / "v01.db", tmp_path / "upgraded.db"
    opaque = "synthetic_legacy_token_1234"
    with closing(sqlite3.connect(source, isolation_level=None)) as old:
        old.row_factory = sqlite3.Row
        old.executescript(Path(db.__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        db.set_value(old, "schema_version", 1)
        run_id = pending(old)
        db.set_value(old, "telegram:connection:personal", {
            "id": "personal", "actor_id": "owner", "project_id": "sales", "revision": 2,
            "update_epoch": 1, "user_id": 101, "chat_id": 101,
            "credential_fingerprint": FakeAdapter.credential_fingerprint,
        })
        ref = {
            "kind": "approval", "target_id": "approval", "decision": "approve", "actor_id": "owner",
            "project_id": "sales", "connection_id": "personal", "pairing_revision": 2,
            "proposal_hash": old.execute("SELECT proposal_hash FROM approvals").fetchone()[0],
            "expected_revision": 1, "expires_at": (NOW + timedelta(minutes=10)).isoformat(), "used": used,
        }
        db.set_value(old, "telegram:callback:" + opaque, ref, now=NOW.isoformat())
        markup = {"inline_keyboard": [[{"text": "승인", "callback_data": "approve:" + opaque}]]}
        db.set_value(old, "telegram:approval_notice:approval", markup)
        payload = {"text": "업무 실행 승인이 필요합니다.", "artifact_id": None,
                   "artifact_hash": None, "filename": None, "reply_markup": markup}
        authority = {"actor_id": "owner", "project_id": "sales", "connection_id": "personal",
                     "pairing_revision": 2, "chat_id": 101, "user_id": 101,
                     "automation_id": None, "automation_revision": None}
        from kelly.contracts import canonical_json, digest

        old.execute(
            "INSERT INTO outbox(id,run_id,delivery_kind,recipient_ref,content_hash,payload_json,"
            "authority_json,state,remote_id,created_at) VALUES ('legacy-notice',?,'message','owner',"
            "?,?,?,'sent','7',?)",
            (run_id, digest(payload), canonical_json(payload), canonical_json(authority), NOW.isoformat()),
        )
        with closing(sqlite3.connect(target)) as copy:
            old.backup(copy)
    with closing(db.init_db(target)) as conn:
        assert conn.execute("SELECT user_id,address_ref,state,revision FROM channel_bindings").fetchone()[:] == (
            "owner", "101", "paired", 2
        )
        action = conn.execute("SELECT * FROM channel_actions").fetchone()
        assert action["token_hash"] == hashlib.sha256(opaque.encode()).hexdigest()
        assert action["expires_at"] == (NOW + timedelta(minutes=30)).isoformat()
        assert bool(action["used_at"]) == used
        assert db.get_value(conn, "telegram:callback:" + opaque) is None
        assert db.get_run(conn, run_id, OWNER)["source"] == "ui"
        assert conn.execute("SELECT sent_ref,state FROM approval_notices").fetchone()[:] == ("7", "sent")
        downgrade(conn)
        assert db.get_value(conn, "telegram:callback:" + opaque) == ref
        assert json.loads(conn.execute("SELECT value_json FROM kv WHERE key='schema_version'").fetchone()[0]) == 1
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert "resolved_by" not in {row[1] for row in conn.execute("PRAGMA table_info(approvals)")}
        restored = db.row_dict(conn.execute("SELECT * FROM outbox").fetchone())
        assert restored["payload_json"] == payload
        assert restored["delivery_kind"] == "message" and restored["content_hash"] == digest(payload)
    with closing(db.init_db(target)) as conn:
        received = router.ingest_update(
            conn, OWNER, "personal", callback(2, "approve:" + opaque),
            now=NOW + timedelta(minutes=20), client=FakeAdapter()
        )
        assert received["state"] == ("rejected" if used else "enqueued")
        assert conn.execute("SELECT state FROM approvals").fetchone()[0] == ("pending" if used else "approved")
    with closing(sqlite3.connect(source)) as original:
        assert db.get_value(original, "schema_version") == 1


def test_second_connection_that_saw_version_one_does_not_upgrade_again(tmp_path):
    path = tmp_path / "v01.db"
    first, second = (sqlite3.connect(path, isolation_level=None) for _ in range(2))
    with closing(first), closing(second):
        for conn in (first, second):
            conn.row_factory = sqlite3.Row
        first.executescript(Path(db.__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        db.set_value(first, "schema_version", 1)
        # Both connections read version 1 before either one takes the write lock.
        assert db.get_value(second, "schema_version") == 1
        upgrade(first)
        upgrade(second)
        assert db.get_value(second, "schema_version") == 2
        assert second.execute("SELECT count(*) FROM channel_bindings").fetchone()[0] == 0
