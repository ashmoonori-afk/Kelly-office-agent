"""Numbered migrations from the original version-one Kelly database."""

import hashlib
import json
from pathlib import Path

from ..contracts import canonical_json, digest


def _script(conn, direction):
    # execute() preserves the caller's transaction; executescript() would commit it.
    for statement in Path(__file__).with_name(f"002_approval_notices.{direction}.sql").read_text(
        encoding="utf-8"
    ).split(";"):
        if statement.strip():
            conn.execute(statement)


def sync_binding(conn, connection, now):
    paired = connection["chat_id"] is not None
    conn.execute(
        "INSERT INTO channel_bindings VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "address_ref=excluded.address_ref,state=excluded.state,revision=excluded.revision,"
        "paired_at=excluded.paired_at",
        (connection["id"], connection["actor_id"], "telegram",
         str(connection["chat_id"]) if paired else "pairing:" + connection["id"],
         "paired" if paired else "pairing", connection["revision"], now if paired else None),
    )


def upgrade(conn):
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        # Another connection may have upgraded between the caller's version check and this lock.
        version = conn.execute("SELECT value_json FROM kv WHERE key='schema_version'").fetchone()
        if json.loads(version[0]) != 1:
            return
        _script(conn, "up")
        for row in conn.execute("SELECT * FROM kv WHERE key LIKE 'telegram:connection:%'"):
            sync_binding(conn, json.loads(row["value_json"]), row["updated_at"])
        for row in conn.execute("SELECT * FROM kv WHERE key LIKE 'telegram:callback:%'").fetchall():
            ref = json.loads(row["value_json"])
            opaque = row["key"].split(":")[-1]
            approval = conn.execute(
                "SELECT expires_at FROM approvals WHERE id=?", (ref["target_id"],)
            ).fetchone() if ref["kind"] == "approval" else None
            conn.execute(
                "INSERT INTO channel_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (hashlib.sha256(opaque.encode()).hexdigest(), ref["kind"],
                 ref["target_id"] if ref["kind"] == "approval" else None, ref["target_id"],
                 ref["decision"], ref["actor_id"], ref["project_id"], ref["connection_id"],
                 ref["pairing_revision"], ref["proposal_hash"], ref["expected_revision"],
                 approval["expires_at"] if approval else ref["expires_at"],
                 row["updated_at"] if ref["used"] else None, opaque, ref["expires_at"]),
            )
        conn.execute("DELETE FROM kv WHERE key LIKE 'telegram:callback:%'")
        for marker in conn.execute(
            "SELECT * FROM kv WHERE key LIKE 'telegram:approval_notice:%'"
        ).fetchall():
            approval_id = marker["key"].split(":")[-1]
            for row in conn.execute(
                "SELECT o.* FROM outbox o JOIN approvals a ON a.run_id=o.run_id WHERE a.id=?",
                (approval_id,),
            ).fetchall():
                payload, authority = json.loads(row["payload_json"]), json.loads(row["authority_json"])
                if payload.get("reply_markup") != json.loads(marker["value_json"]):
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO approval_notices VALUES (?,?,?,'telegram',?,?,?,?)",
                    (row["id"], approval_id, authority["actor_id"], authority["connection_id"],
                     authority["pairing_revision"], row["remote_id"],
                     "sent" if row["state"] == "sent" else "queued"),
                )
                payload.update(notice_id=row["id"], notice_role="card", update_ref=None)
                conn.execute(
                    "UPDATE outbox SET delivery_kind='approval_card',payload_json=?,content_hash=? WHERE id=?",
                    (canonical_json(payload), digest(payload), row["id"]),
                )
        conn.execute("UPDATE kv SET value_json='2' WHERE key='schema_version'")


def downgrade(conn):
    """Restore legacy tokens; new hash-only tokens are revoked on rollback."""
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        for row in conn.execute("SELECT * FROM channel_actions WHERE legacy_token IS NOT NULL"):
            ref = {
                "kind": row["kind"], "target_id": row["target_id"], "decision": row["decision"],
                "actor_id": row["user_id"], "project_id": row["project_id"],
                "connection_id": row["binding_id"], "pairing_revision": row["binding_revision"],
                "proposal_hash": row["proposal_hash"], "expected_revision": row["expected_revision"],
                "expires_at": row["legacy_expires_at"], "used": row["used_at"] is not None,
            }
            conn.execute(
                "INSERT INTO kv VALUES (?, ?, 1, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                ("telegram:callback:" + row["legacy_token"], json.dumps(ref)),
            )
        for row in conn.execute(
            "SELECT o.* FROM outbox o JOIN approval_notices n ON n.id=o.id "
            "JOIN kv k ON k.key='telegram:approval_notice:'||n.approval_id "
            "WHERE o.delivery_kind='approval_card'"
        ).fetchall():
            payload = json.loads(row["payload_json"])
            for field in ("notice_id", "notice_role", "update_ref"):
                payload.pop(field)
            conn.execute(
                "UPDATE outbox SET delivery_kind='message',payload_json=?,content_hash=? WHERE id=?",
                (canonical_json(payload), digest(payload), row["id"]),
            )
        # Pending C2 deliveries cannot be interpreted by the older dispatcher.
        conn.execute(
            "UPDATE outbox SET state='cancelled' WHERE delivery_kind LIKE 'approval_%' "
            "AND state IN ('pending','retry_wait')"
        )
        _script(conn, "down")
        conn.execute("UPDATE kv SET value_json='1' WHERE key='schema_version'")
