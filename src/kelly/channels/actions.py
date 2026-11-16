"""Opaque, single-use actions bound to the approval and paired owner."""

import hashlib
import re
import secrets
from datetime import datetime, timedelta

from ..contracts import AppError
from ..db import automation_proposal_hash, get_automation, get_run, row_dict
from ..engine import resolve_approval


def create(conn, actor, connection, *, kind, target_id, decision, now):
    if (
        kind not in {"approval", "automation_enable"}
        or decision not in {"approve", "reject"}
        or (kind == "automation_enable" and decision != "approve")
    ):
        raise AppError("INVALID_INPUT")
    expiry = now + timedelta(minutes=10)
    if kind == "approval":
        target = row_dict(conn.execute(
            "SELECT * FROM approvals WHERE id=? AND actor_id=?", (target_id, actor.id)
        ).fetchone())
        if target is None:
            raise AppError("NOT_FOUND", status=404)
        run = get_run(conn, target["run_id"], actor)
        if target["state"] != "pending" or run["state"] != "awaiting_approval":
            raise AppError("CONFLICT", status=409)
        project_id, proposal_hash = run["project_id"], target["proposal_hash"]
        expiry = datetime.fromisoformat(target["expires_at"])
    else:
        target = get_automation(conn, target_id, actor)
        project_id, proposal_hash = target["project_id"], automation_proposal_hash(target)
    if project_id != connection["project_id"] or expiry <= now:
        raise AppError("POLICY_DENIED")
    opaque = secrets.token_urlsafe(18)
    conn.execute(
        "INSERT INTO channel_actions (token_hash,kind,approval_id,target_id,decision,user_id,"
        "project_id,binding_id,binding_revision,proposal_hash,expected_revision,expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (hashlib.sha256(opaque.encode()).hexdigest(), kind,
         target_id if kind == "approval" else None, target_id, decision, actor.id,
         project_id, connection["id"], connection["revision"], proposal_hash,
         target["revision"], expiry.isoformat()),
    )
    return decision + ":" + opaque


def consume(conn, actor, connection, data, *, now, enable):
    if not isinstance(data, str) or not re.fullmatch(
        r"(?:approve|reject):[A-Za-z0-9_-]{20,40}", data
    ):
        raise AppError("POLICY_DENIED")
    decision, opaque = data.split(":", 1)
    ref = row_dict(conn.execute(
        "SELECT * FROM channel_actions WHERE token_hash=?",
        (hashlib.sha256(opaque.encode()).hexdigest(),),
    ).fetchone())
    binding = conn.execute(
        "SELECT * FROM channel_bindings WHERE id=?", (connection["id"],)
    ).fetchone()
    if (
        ref is None
        or ref["used_at"] is not None
        or binding is None
        or binding["state"] != "paired"
        or binding["revision"] != connection["revision"]
        or binding["user_id"] != actor.id
        or binding["address_ref"] != str(connection["chat_id"])
        or (ref["user_id"], ref["project_id"], ref["binding_id"],
            ref["binding_revision"], ref["decision"])
        != (actor.id, connection["project_id"], connection["id"], connection["revision"], decision)
    ):
        raise AppError("POLICY_DENIED")
    if datetime.fromisoformat(ref["expires_at"]) <= now:
        raise AppError("APPROVAL_EXPIRED", status=410)
    if ref["kind"] == "approval":
        result = resolve_approval(
            conn, ref["target_id"], actor, decision, ref["proposal_hash"],
            ref["expected_revision"], now=now, resolved_by=ref["user_id"],
            resolved_via=binding["channel"],
        )
    elif ref["kind"] == "automation_enable":
        result = enable(
            conn, actor, connection["id"], ref["target_id"],
            expected_revision=ref["expected_revision"], confirmed_hash=ref["proposal_hash"], now=now,
        )
    else:
        raise AppError("POLICY_DENIED")
    conn.execute(
        "UPDATE channel_actions SET used_at=? WHERE token_hash=?",
        (now.isoformat(), ref["token_hash"]),
    )
    return result


def acknowledgement(conn, actor, connection, event):
    from dataclasses import replace

    if event.action and event.user_id == connection["user_id"] and event.chat_id == connection["chat_id"]:
        ref = conn.execute(
            "SELECT used_at FROM channel_actions WHERE token_hash=? AND user_id=? AND binding_id=?",
            (hashlib.sha256(event.action.split(":")[-1].encode()).hexdigest(),
             actor.id, connection["id"]),
        ).fetchone()
        if ref and ref["used_at"]:
            return replace(event, ack_text="이미 처리된 요청입니다.")
    return event
