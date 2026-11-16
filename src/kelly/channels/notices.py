"""Approval cards and durable write-back for every run source."""

import uuid
from datetime import datetime, timedelta, timezone

from ..contracts import AppError
from ..db import get_run, row_dict, transaction
from ..engine import resolve_approval
from . import actions


def _time(value):
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=9))).strftime(
        "%Y-%m-%d %H:%M KST"
    )


def card(approval):
    proposal = approval["proposal_json"]
    text = (
        "업무 실행 승인이 필요합니다.\n"
        f"작업: {proposal.get('tool') or '화면에서 확인해 주세요'}\n"
        f"대상: {proposal.get('destination') or '화면에서 확인해 주세요'}\n"
        f"받는 사람: {proposal.get('recipient_ref') or '화면에서 확인해 주세요'}\n"
        f"영향: {proposal.get('impact') or '화면에서 확인해 주세요'}\n"
        f"승인 기한: {_time(approval['expires_at'])}"
    )
    for field, label in (
        ("reason", "이유"), ("evidence", "근거"), ("reversible_alternative", "되돌릴 수 있는 대안")
    ):
        if proposal.get(field):
            text += f"\n{label}: {proposal[field]}"
    return text


def outcome(approval):
    if approval["state"] == "expired":
        return "기한이 지나 실행하지 않았어요."
    label = "승인됨" if approval["state"] in {"approved", "consumed"} else "거절됨"
    if approval["state"] == "cancelled":
        label = "취소됨"
    who = approval["resolved_by"] or approval["actor_id"]
    when = approval["resolved_at"] or approval["consumed_at"] or approval["expires_at"]
    return f"{label} · {who} · {_time(when)}"


def authority(conn, actor, connection, run_id, notice_id):
    notice = row_dict(conn.execute(
        "SELECT n.*,a.run_id FROM approval_notices n JOIN approvals a ON a.id=n.approval_id "
        "WHERE n.id=?", (notice_id,),
    ).fetchone())
    run = get_run(conn, run_id, actor)
    binding = conn.execute(
        "SELECT * FROM channel_bindings WHERE id=?", (connection["id"],)
    ).fetchone()
    if (
        notice is None or notice["run_id"] != run_id or notice["user_id"] != actor.id
        or notice["binding_id"] != connection["id"]
        or notice["binding_revision"] != connection["revision"]
        or run["project_id"] != connection["project_id"]
        or (run["result_json"] or {}).get("archived")
        or binding is None or binding["state"] != "paired"
        or binding["user_id"] != actor.id or binding["revision"] != connection["revision"]
        or binding["address_ref"] != str(connection["chat_id"])
    ):
        raise AppError("POLICY_DENIED")
    return {
        "actor_id": actor.id, "project_id": run["project_id"], "connection_id": connection["id"],
        "pairing_revision": connection["revision"], "chat_id": connection["chat_id"],
        "user_id": connection["user_id"], "automation_id": run["automation_id"],
        "automation_revision": run["automation_revision"],
    }


def queue(conn, actor, connection, adapter, *, now, enqueue):
    if connection["chat_id"] is None:
        return
    binding = conn.execute(
        "SELECT * FROM channel_bindings WHERE id=?", (connection["id"],)
    ).fetchone()
    if binding["state"] != "paired" or binding["revision"] != connection["revision"]:
        return
    approvals = conn.execute(
        "SELECT a.* FROM approvals a JOIN runs r ON r.id=a.run_id "
        "WHERE a.actor_id=? AND r.actor_id=? AND r.project_id=? "
        "AND COALESCE(json_extract(r.result_json,'$.archived'),0)=0 ORDER BY a.created_at,a.id",
        (actor.id, actor.id, connection["project_id"]),
    ).fetchall()
    for raw in approvals:
        approval = row_dict(raw)
        if approval["state"] == "pending" and datetime.fromisoformat(approval["expires_at"]) <= now:
            try:
                resolve_approval(
                    conn, approval["id"], actor, "reject", approval["proposal_hash"],
                    approval["revision"], now=now,
                )
            except AppError as error:
                if error.code != "APPROVAL_EXPIRED":
                    raise
            approval = row_dict(conn.execute(
                "SELECT * FROM approvals WHERE id=?", (approval["id"],)
            ).fetchone())
        notice = row_dict(conn.execute(
            "SELECT * FROM approval_notices WHERE approval_id=? AND user_id=? AND channel=?",
            (approval["id"], actor.id, binding["channel"]),
        ).fetchone())
        if notice is None:
            if approval["state"] != "pending" or not adapter.caps.buttons:
                continue
            with transaction(conn):
                notice_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO approval_notices(id,approval_id,user_id,channel,binding_id,"
                    "binding_revision,state) VALUES (?,?,?,?,?,?,'queued')",
                    (notice_id, approval["id"], actor.id, binding["channel"],
                     connection["id"], connection["revision"]),
                )
                markup = {"inline_keyboard": [[
                    {"text": label, "callback_data": actions.create(
                        conn, actor, connection, kind="approval", target_id=approval["id"],
                        decision=decision, now=now,
                    )} for label, decision in (("승인", "approve"), ("거절", "reject"))
                ]]}
                enqueue(conn, actor, connection["id"], approval["run_id"], text=card(approval),
                        reply_markup=markup, notice_id=notice_id, notice_role="card", now=now)
            continue
        if (notice["binding_id"], notice["binding_revision"]) != (
            connection["id"], connection["revision"]
        ):
            continue
        if approval["state"] != "pending":
            # A decision before initial delivery cancels the obsolete buttons.
            conn.execute(
                "UPDATE outbox SET state='cancelled' WHERE json_extract(payload_json,'$.notice_id')=? "
                "AND json_extract(payload_json,'$.notice_role')='card' AND state IN ('pending','retry_wait')",
                (notice["id"],),
            )
            original = conn.execute(
                "SELECT remote_id FROM outbox WHERE delivery_kind='approval_card' "
                "AND json_extract(payload_json,'$.notice_id')=? AND state='sent'",
                (notice["id"],),
            ).fetchone()
            enqueue(
                conn, actor, connection["id"], approval["run_id"], text=outcome(approval),
                notice_id=notice["id"], notice_role="outcome",
                update_ref=original["remote_id"] if original and adapter.caps.edit_message else None,
                now=now,
            )
            run = get_run(conn, approval["run_id"], actor)
            step = row_dict(conn.execute(
                "SELECT * FROM steps WHERE id=? AND run_id=?",
                (approval["step_id"], run["id"]),
            ).fetchone())
            if approval["state"] == "consumed" and step["state"] in {
                "succeeded", "failed", "outcome_unknown", "cancelled"
            }:
                text = {
                    "succeeded": "승인한 작업을 완료했습니다.", "failed": "승인한 작업을 완료하지 못했습니다.",
                    "outcome_unknown": "보냈는지 확인 중입니다.",
                    "cancelled": "업무가 취소되었습니다.",
                }[step["state"]]
                receipt = (step["output_json"] or {}).get("receipt")
                if receipt:
                    from ..contracts import canonical_json

                    text += "\n처리 결과: " + canonical_json(receipt)
                enqueue(conn, actor, connection["id"], run["id"], text=text,
                        notice_id=notice["id"], notice_role="result", now=now)
