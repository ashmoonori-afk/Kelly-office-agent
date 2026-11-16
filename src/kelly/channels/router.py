"""One private Telegram connection, durable ingress and independent result delivery."""

import hashlib
import hmac
import re
import secrets
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

from pydantic import JsonValue, ValidationError

from ..artifacts import metadata, open_artifact
from ..contracts import Actor, AppError, RunRequest, canonical_json, digest
from ..db import (
    automation_proposal_hash,
    create_run,
    get_automation,
    get_run,
    get_value,
    require_automation_run,
    require_project,
    row_dict,
    set_automation_enabled,
    set_value,
    transaction,
)
from ..engine import resolve_approval
from .base import (
    Action,
    ChannelAdapter,
    ChannelAddress,
    ChannelError,
    InboundEvent,
    OutboundMessage,
    _markup,
    _message_text,
    _private_id,
    require_message,
)


def _now(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise AppError("INVALID_INPUT")
    return value.astimezone(UTC)


def _connection(conn, actor, connection_id, *, paired=False):
    value = get_value(conn, "telegram:connection:" + connection_id)
    if not isinstance(value, dict) or value.get("actor_id") != actor.id:
        raise AppError("NOT_FOUND", status=404)
    require_project(actor, value["project_id"])
    if paired and (value.get("user_id") is None or value.get("chat_id") is None):
        raise AppError("POLICY_DENIED")
    return value


def _bind_client(conn, actor, connection_id, client, now):
    with nullcontext(conn) if conn.in_transaction else transaction(conn):
        connection = _connection(conn, actor, connection_id)
        previous = connection.get("credential_fingerprint")
        if previous != client.credential_fingerprint:
            if previous is not None:
                connection.update(
                    revision=connection["revision"] + 1,
                    update_epoch=connection.get("update_epoch", 1) + 1,
                    user_id=None,
                    chat_id=None,
                )
                set_value(conn, "telegram:cursor:" + connection_id, 0, now=now.isoformat())
            connection["credential_fingerprint"] = client.credential_fingerprint
            set_value(conn, "telegram:connection:" + connection_id, connection, now=now.isoformat())
        return connection


def register_connection(conn, actor, connection_id, project_id, *, now, client=None):
    now = _now(now)
    require_project(actor, project_id)
    if not isinstance(connection_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", connection_id
    ):
        raise AppError("INVALID_INPUT")
    with transaction(conn):
        old = get_value(conn, "telegram:connection:" + connection_id)
        if old is not None:
            value = _connection(conn, actor, connection_id)
            if value["project_id"] != project_id:
                raise AppError("CONFLICT", status=409)
            return (
                _bind_client(conn, actor, connection_id, client, now)
                if client is not None
                else value
            )
        value = {
            "id": connection_id,
            "actor_id": actor.id,
            "project_id": project_id,
            "revision": 1,
            "update_epoch": 1,
            "user_id": None,
            "chat_id": None,
        }
        set_value(conn, "telegram:connection:" + connection_id, value, now=now.isoformat())
        return (
            _bind_client(conn, actor, connection_id, client, now) if client is not None else value
        )


def create_pairing(conn, actor, connection_id, *, now):
    now = _now(now)
    code = secrets.token_urlsafe(24)
    expires = (now + timedelta(minutes=10)).isoformat()
    with transaction(conn):
        connection = _connection(conn, actor, connection_id)
        set_value(
            conn,
            "telegram:pair:" + connection_id,
            {
                "code_hash": hashlib.sha256(code.encode()).hexdigest(),
                "expires_at": expires,
                "attempts": 0,
                "used": False,
                "connection_revision": connection["revision"],
            },
            now=now.isoformat(),
        )
    return {"code": code, "expires_at": expires}


def _identity(event: InboundEvent):
    if not event.human or not event.private:
        raise AppError("POLICY_DENIED")
    user_id, chat_id = _private_id(event.user_id), _private_id(event.chat_id)
    if user_id != chat_id:
        raise AppError("POLICY_DENIED")
    return user_id, chat_id


def _authenticate(connection, user_id, chat_id):
    if (connection["user_id"], connection["chat_id"]) != (user_id, chat_id):
        raise AppError("POLICY_DENIED")


def _pair(conn, connection, code, user_id, chat_id, now):
    key = "telegram:pair:" + connection["id"]
    pair = get_value(conn, key)
    if (
        not isinstance(pair, dict)
        or pair["used"]
        or pair["attempts"] >= 5
        or datetime.fromisoformat(pair["expires_at"]) <= now
        or pair["connection_revision"] != connection["revision"]
    ):
        raise AppError("POLICY_DENIED")
    if not hmac.compare_digest(pair["code_hash"], hashlib.sha256(code.encode()).hexdigest()):
        pair["attempts"] += 1
        set_value(conn, key, pair, now=now.isoformat())
        raise AppError("POLICY_DENIED")
    pair["used"] = True
    set_value(conn, key, pair, now=now.isoformat())
    connection = {
        **connection,
        "user_id": user_id,
        "chat_id": chat_id,
        "revision": connection["revision"] + 1,
    }
    set_value(conn, "telegram:connection:" + connection["id"], connection, now=now.isoformat())


def create_callback(conn, actor, connection_id, *, kind, target_id, decision="approve", now):
    now = _now(now)
    if (
        kind not in {"approval", "automation_enable"}
        or decision not in {"approve", "reject"}
        or (kind == "automation_enable" and decision != "approve")
    ):
        raise AppError("INVALID_INPUT")
    with transaction(conn):
        connection = _connection(conn, actor, connection_id, paired=True)
        expiry = now + timedelta(minutes=10)
        if kind == "approval":
            target = row_dict(
                conn.execute(
                    "SELECT * FROM approvals WHERE id=? AND actor_id=?", (target_id, actor.id)
                ).fetchone()
            )
            if target is None:
                raise AppError("NOT_FOUND", status=404)
            run = get_run(conn, target["run_id"], actor)
            if target["state"] != "pending" or run["state"] != "awaiting_approval":
                raise AppError("CONFLICT", status=409)
            project_id, proposal_hash = run["project_id"], target["proposal_hash"]
            expiry = min(expiry, datetime.fromisoformat(target["expires_at"]))
        else:
            target = get_automation(conn, target_id, actor)
            project_id, proposal_hash = target["project_id"], automation_proposal_hash(target)
        if project_id != connection["project_id"] or expiry <= now:
            raise AppError("POLICY_DENIED")
        opaque = secrets.token_urlsafe(18)
        ref = {
            "kind": kind,
            "target_id": target_id,
            "decision": decision,
            "actor_id": actor.id,
            "project_id": project_id,
            "connection_id": connection_id,
            "pairing_revision": connection["revision"],
            "proposal_hash": proposal_hash,
            "expected_revision": target["revision"],
            "expires_at": expiry.isoformat(),
            "used": False,
        }
        set_value(conn, "telegram:callback:" + opaque, ref, now=now.isoformat())
    return decision + ":" + opaque


def enable_automation(
    conn, actor, connection_id, automation_id, *, expected_revision, confirmed_hash, now
):
    """Explicit local/button confirmation also fixes the paired destination revision."""
    now = _now(now)
    with nullcontext(conn) if conn.in_transaction else transaction(conn):
        connection = _connection(conn, actor, connection_id, paired=True)
        bot = get_automation(conn, automation_id, actor)
        if bot["project_id"] != connection["project_id"]:
            raise AppError("POLICY_DENIED")
        bot = set_automation_enabled(
            conn,
            automation_id,
            expected_revision,
            True,
            actor,
            now=now,
            confirmed_hash=confirmed_hash,
        )
        set_value(
            conn,
            "telegram:automation:" + automation_id,
            {
                "automation_revision": bot["revision"],
                "connection_id": connection_id,
                "pairing_revision": connection["revision"],
                "actor_id": actor.id,
                "project_id": bot["project_id"],
            },
            now=now.isoformat(),
        )
        return bot


def _callback(conn, actor, connection, data, now):
    if not isinstance(data, str) or not re.fullmatch(
        r"(?:approve|reject):[A-Za-z0-9_-]{20,40}", data
    ):
        raise AppError("POLICY_DENIED")
    decision, opaque = data.split(":", 1)
    key = "telegram:callback:" + opaque
    ref = get_value(conn, key)
    if (
        not isinstance(ref, dict)
        or ref.get("used") is not False
        or (
            ref.get("actor_id"),
            ref.get("project_id"),
            ref.get("connection_id"),
            ref.get("pairing_revision"),
            ref.get("decision"),
        )
        != (actor.id, connection["project_id"], connection["id"], connection["revision"], decision)
    ):
        raise AppError("POLICY_DENIED")
    if datetime.fromisoformat(ref["expires_at"]) <= now:
        raise AppError("APPROVAL_EXPIRED", status=410)
    try:
        if ref["kind"] == "approval":
            result = resolve_approval(
                conn,
                ref["target_id"],
                actor,
                decision,
                ref["proposal_hash"],
                ref["expected_revision"],
                now=now,
            )
        elif ref["kind"] == "automation_enable":
            result = enable_automation(
                conn,
                actor,
                connection["id"],
                ref["target_id"],
                expected_revision=ref["expected_revision"],
                confirmed_hash=ref["proposal_hash"],
                now=now,
            )
        else:
            raise AppError("POLICY_DENIED")
    except AppError as error:
        if error.code == "APPROVAL_EXPIRED":
            ref["used"] = True
            set_value(conn, key, ref, now=now.isoformat())
        raise
    ref["used"] = True
    set_value(conn, key, ref, now=now.isoformat())
    return result


def ingest_event(conn, actor: Actor, connection_id, event: InboundEvent, *, now, client=None):
    """Persist one bounded update, its effect and the next offset in one transaction."""
    now = _now(now)
    if client is not None:
        _bind_client(conn, actor, connection_id, client, now)
    if event.byte_size <= 65536 and event.kind == "action" and client is not None:
        try:
            client.ack(event)
        except (ChannelError, AppError):
            pass  # A lost UI acknowledgement never grants or blocks durable authorization.
    with transaction(conn):
        connection = _connection(conn, actor, connection_id)
        epoch = connection.get("update_epoch", 1)
        key = (str(epoch) + ":" if epoch != 1 else "") + str(event.event_id)
        existing = row_dict(
            conn.execute(
                "SELECT * FROM inbox_events WHERE source='telegram' AND connection_id=? AND event_key=?",
                (connection_id, key),
            ).fetchone()
        )
        if existing:
            if (
                existing["actor_id"] != actor.id
                or existing["payload_json"].get("raw_sha256") != event.raw_sha256
            ):
                raise AppError("CONFLICT", status=409)
            return existing
        payload: dict[str, JsonValue] = {
            "update_id": event.event_id,
            "raw_sha256": event.raw_sha256,
        }
        conn.execute(
            "INSERT INTO inbox_events(source,connection_id,event_key,actor_id,payload_json,received_at) VALUES ('telegram',?,?,?,?,?)",
            (connection_id, key, actor.id, canonical_json(payload), now.isoformat()),
        )
        state, run_id = "rejected", None
        try:
            if event.error_code is not None:
                raise AppError(event.error_code)
            if event.kind == "action":
                user_id, chat_id = _identity(event)
                _authenticate(connection, user_id, chat_id)
                if client is None:
                    raise AppError("INVALID_INPUT")
                payload["callback_result"] = _callback(conn, actor, connection, event.action, now)
                state = "enqueued"
            else:
                user_id, chat_id = _identity(event)
                if event.unsupported or event.kind == "file":
                    raise AppError("UNSUPPORTED_CAPABILITY")
                text = _message_text(event.text, 4096)
                pair = event.kind == "pair"
                if pair:
                    _pair(conn, connection, event.action, user_id, chat_id, now)
                    payload["kind"], state = "pairing", "enqueued"
                else:
                    _authenticate(connection, user_id, chat_id)
                    request, output_format = text, "text"
                    if text.startswith("/"):
                        parts = text.split(maxsplit=1)
                        match parts[0][1:]:
                            case "text" | "md" | "csv" | "xlsx" | "docx" as output_format:
                                pass
                            case _:
                                raise AppError("UNSUPPORTED_CAPABILITY")
                        request = parts[1].strip() if len(parts) == 2 else ""
                    payload.update(kind="request", pairing_revision=connection["revision"])
                    run = create_run(
                        conn,
                        actor,
                        "telegram",
                        connection_id + ":" + key,
                        RunRequest(
                            request=request,
                            project_id=connection["project_id"],
                            output_format=output_format,
                        ),
                        {
                            "telegram_delivery": {
                                "connection_id": connection_id,
                                "pairing_revision": connection["revision"],
                            }
                        },
                        now=now.isoformat(),
                    )
                    run_id, state = run["id"], "enqueued"
        except (AppError, ValidationError) as error:
            payload["error_code"] = error.code if isinstance(error, AppError) else "INVALID_INPUT"
        conn.execute(
            "UPDATE inbox_events SET payload_json=?,state=?,run_id=? WHERE source='telegram' AND connection_id=? AND event_key=?",
            (canonical_json(payload), state, run_id, connection_id, key),
        )
        cursor_key = "telegram:cursor:" + connection_id
        cursor = get_value(conn, cursor_key, 0)
        set_value(conn, cursor_key, max(cursor, event.event_id + 1), now=now.isoformat())
        return row_dict(
            conn.execute(
                "SELECT * FROM inbox_events WHERE source='telegram' AND connection_id=? AND event_key=?",
                (connection_id, key),
            ).fetchone()
        )


def poll_once(client, conn, actor, connection_id, *, now, timeout=25):
    """Use a clock callable at runtime so expiry is checked after blocking polling."""
    _bind_client(conn, actor, connection_id, client, _now(now() if callable(now) else now))
    if client.caps.inbound != "pull":
        raise AppError("UNSUPPORTED_CAPABILITY")
    cursor = get_value(conn, "telegram:cursor:" + connection_id)
    events, _ = client.receive(str(cursor) if cursor is not None else None, timeout=timeout)
    return [
        ingest_event(
            conn, actor, connection_id, event, now=now() if callable(now) else now, client=client
        )
        for event in events
    ]


def _delivery_authority(conn, actor, connection_id, run_id):
    if (get_run(conn, run_id, actor)["result_json"] or {}).get("archived"):
        raise AppError("POLICY_DENIED")
    connection = _connection(conn, actor, connection_id, paired=True)
    run = require_automation_run(conn, run_id, actor)
    if run["project_id"] != connection["project_id"] or run["state"] not in {
        "succeeded",
        "awaiting_approval",
        "awaiting_input",
        "failed",
        "expired",
        "rejected",
    }:
        raise AppError("POLICY_DENIED")
    if run["source"] == "telegram":
        grant = run["policy_json"].get("telegram_delivery", {})
        event = row_dict(
            conn.execute(
                "SELECT * FROM inbox_events WHERE source='telegram' AND connection_id=? AND run_id=? AND actor_id=? AND state='enqueued'",
                (connection_id, run_id, actor.id),
            ).fetchone()
        )
        if (
            grant != {"connection_id": connection_id, "pairing_revision": connection["revision"]}
            or event is None
            or event["payload_json"].get("pairing_revision") != connection["revision"]
        ):
            raise AppError("POLICY_DENIED")
    elif run["source"] == "automation":
        bot = get_automation(conn, run["automation_id"], actor)
        config = bot["config_json"]
        grant = get_value(conn, "telegram:automation:" + bot["id"])
        expected = {
            "automation_revision": bot["revision"],
            "connection_id": connection_id,
            "pairing_revision": connection["revision"],
            "actor_id": actor.id,
            "project_id": bot["project_id"],
        }
        if (
            grant != expected
            or config["delivery"] != {"channel": "telegram", "recipient_ref": "owner"}
            or config["authority"]["send_result_to_owner"] is not True
            or run["policy_json"].get("automation_config_hash") != digest(config)
        ):
            raise AppError("POLICY_DENIED")
    else:
        raise AppError("POLICY_DENIED")
    return {
        "actor_id": actor.id,
        "project_id": run["project_id"],
        "connection_id": connection_id,
        "pairing_revision": connection["revision"],
        "chat_id": connection["chat_id"],
        "user_id": connection["user_id"],
        "automation_id": run["automation_id"],
        "automation_revision": run["automation_revision"],
    }


def _deliverable_artifact(conn, actor, run_id, artifact_id):
    run = get_run(conn, run_id, actor)
    artifact = metadata(conn, artifact_id, actor, run["project_id"])
    if (
        run["state"] != "succeeded"
        or artifact["run_id"] != run_id
        or artifact["kind"] not in {"draft", "result"}
        or not artifact["validation_json"]
        or artifact["validation_json"].get("status") != "pass"
    ):
        raise AppError("POLICY_DENIED")
    outputs = (run["result_json"] or {}).get("outputs", {})
    for output in outputs.values():
        if not isinstance(output, dict):
            continue
        receipt, created = output.get("receipt", {}), output.get("artifact", {})
        if (
            isinstance(receipt, dict)
            and (receipt.get("run_id"), receipt.get("artifact_id"), receipt.get("artifact_hash"))
            == (run_id, artifact_id, artifact["sha256"])
            and isinstance(receipt.get("validation"), dict)
            and receipt["validation"].get("status") == "pass"
        ):
            return artifact
        if (
            artifact["kind"] == "draft"
            and isinstance(created, dict)
            and (created.get("artifact_id"), created.get("sha256"))
            == (artifact_id, artifact["sha256"])
        ):
            return artifact
    raise AppError("POLICY_DENIED")


def prepare_delivery(
    conn, actor, connection_id, run_id, *, text="", artifact_id=None, reply_markup=None, now
):
    now = _now(now)
    if text:
        _message_text(text, 1024 if artifact_id else 4096)
    if not text and artifact_id is None:
        raise AppError("INVALID_INPUT")
    if reply_markup is not None:
        _markup(reply_markup)
        if artifact_id is not None:
            raise AppError("INVALID_INPUT")
    with transaction(conn):
        authority = _delivery_authority(conn, actor, connection_id, run_id)
        payload = {
            "text": text,
            "artifact_id": artifact_id,
            "artifact_hash": None,
            "filename": None,
            "reply_markup": reply_markup,
        }
        if artifact_id is not None:
            artifact = _deliverable_artifact(conn, actor, run_id, artifact_id)
            payload.update(
                artifact_hash=artifact["sha256"], filename=artifact["provenance_json"]["filename"]
            )
        content_hash, kind = digest(payload), "document" if artifact_id else "message"
        old = row_dict(
            conn.execute(
                "SELECT * FROM outbox WHERE run_id=? AND delivery_kind=? AND recipient_ref='owner' AND content_hash=?",
                (run_id, kind, content_hash),
            ).fetchone()
        )
        if old:
            if old["authority_json"] != authority:
                raise AppError("CONFLICT", status=409)
            return old
        outbox_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO outbox(id,run_id,delivery_kind,recipient_ref,content_hash,payload_json,authority_json,created_at) VALUES (?,?,?,'owner',?,?,?,?)",
            (
                outbox_id,
                run_id,
                kind,
                content_hash,
                canonical_json(payload),
                canonical_json(authority),
                now.isoformat(),
            ),
        )
        return row_dict(conn.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone())


def cancel_delivery(conn, outbox_id, actor, *, now):
    _now(now)
    with transaction(conn):
        row = row_dict(conn.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone())
        if row is None:
            raise AppError("NOT_FOUND", status=404)
        get_run(conn, row["run_id"], actor)
        conn.execute(
            "UPDATE outbox SET state='cancelled' WHERE id=? AND state IN ('pending','retry_wait')",
            (outbox_id,),
        )
        return row_dict(conn.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone())


def deliver_once(client, conn, home, actor, *, now, connection_id=None, before_send=None):
    """Claim one due row, then make one request; DB failures preserve sending for recovery."""
    now = _now(now)
    row = row_dict(
        conn.execute(
            "SELECT o.* FROM outbox o JOIN runs r ON r.id=o.run_id WHERE r.actor_id=? AND o.state IN ('pending','retry_wait') AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=?) AND (? IS NULL OR json_extract(o.authority_json,'$.connection_id')=?) ORDER BY o.created_at,o.id LIMIT 1",
            (actor.id, now.isoformat(), connection_id, connection_id),
        ).fetchone()
    )
    if row is None:
        return None
    sent = False
    try:
        authority, payload = row["authority_json"], row["payload_json"]
        _bind_client(conn, actor, authority["connection_id"], client, now)
        if payload["artifact_id"]:
            if not client.caps.files:
                raise AppError("UNSUPPORTED_CAPABILITY")
            record = _deliverable_artifact(conn, actor, row["run_id"], payload["artifact_id"])
            if (
                record["sha256"] != payload["artifact_hash"]
                or record["run_id"] != row["run_id"]
                or record["kind"] not in {"draft", "result"}
            ):
                raise AppError("SOURCE_CHANGED")
            held = open_artifact(home, conn, payload["artifact_id"], actor, authority["project_id"])
        else:
            held = nullcontext(None)
        with held as stream:
            message = OutboundMessage(
                text=payload["text"],
                actions=tuple(
                    tuple(Action(button["text"], button["callback_data"]) for button in row)
                    for row in (payload["reply_markup"] or {}).get("inline_keyboard", [])
                ),
                file=stream,
                filename=payload["filename"],
            )
            require_message(client.caps, message)
            with transaction(conn):
                if before_send is not None:
                    before_send()
                current = _delivery_authority(
                    conn, actor, authority["connection_id"], row["run_id"]
                )
                if current != authority or digest(payload) != row["content_hash"]:
                    raise AppError("POLICY_DENIED")
                claimed = conn.execute(
                    "UPDATE outbox SET state='sending',attempt_count=attempt_count+1 WHERE id=? AND state IN ('pending','retry_wait') AND attempt_count<3",
                    (row["id"],),
                )
                if claimed.rowcount != 1:
                    raise AppError("CONFLICT", status=409)
            result = client.send(ChannelAddress(authority["chat_id"]), message)
            sent = True
        with transaction(conn):
            conn.execute(
                "UPDATE outbox SET state='sent',remote_id=?,sent_at=?,error_code=NULL WHERE id=? AND state='sending'",
                (result.message_id, now.isoformat(), row["id"]),
            )
    except (ChannelError, AppError, OSError) as error:
        code = error.code if isinstance(error, AppError) else "SOURCE_CHANGED"
        with transaction(conn):
            fresh = row_dict(
                conn.execute("SELECT * FROM outbox WHERE id=?", (row["id"],)).fetchone()
            )
            state, due = "failed", None
            if sent or (isinstance(error, ChannelError) and error.ambiguous):
                state = "unknown"
            elif (
                isinstance(error, ChannelError)
                and error.retry_after is not None
                and fresh["attempt_count"] < 3
            ):
                state, due = "retry_wait", (now + timedelta(seconds=error.retry_after)).isoformat()
            conn.execute(
                "UPDATE outbox SET state=?,next_attempt_at=?,error_code=? WHERE id=? AND state IN ('pending','retry_wait','sending')",
                (state, due, code, row["id"]),
            )
    return row_dict(conn.execute("SELECT * FROM outbox WHERE id=?", (row["id"],)).fetchone())


def open_adapter(token, *, http_transport=None) -> ChannelAdapter:
    """Construct the configured transport without exposing it to the worker."""
    from .telegram import TelegramAdapter

    return TelegramAdapter(token, http_transport=http_transport)


def ingest_update(conn, actor, connection_id, update, *, now, client=None):
    """Compatibility entry point for callers that still supply Telegram wire payloads."""
    from .telegram import parse_update

    now = _now(now)
    return ingest_event(conn, actor, connection_id, parse_update(update), now=now, client=client)


def queue_results(conn, actor, connection_id, adapter: ChannelAdapter, *, now):
    rows = conn.execute(
        "SELECT * FROM runs WHERE actor_id=? AND source IN ('telegram','automation') "
        "AND state IN ('succeeded','awaiting_approval','awaiting_input','failed','expired',"
        "'rejected') AND COALESCE(json_extract(result_json,'$.archived'),0)=0 ORDER BY created_at,id",
        (actor.id,),
    ).fetchall()
    for row in rows:
        run = row_dict(row)
        try:
            markup = None
            if run["state"] == "awaiting_approval":
                if not adapter.caps.buttons:
                    raise AppError("UNSUPPORTED_CAPABILITY")
                approval = row_dict(
                    conn.execute(
                        "SELECT * FROM approvals WHERE run_id=? AND state='pending' "
                        "AND expires_at>? ORDER BY created_at LIMIT 1",
                        (run["id"], now().isoformat()),
                    ).fetchone()
                )
                if approval is None:
                    continue
                key = "telegram:approval_notice:" + approval["id"]
                markup = get_value(conn, key)
                if markup is None:
                    markup = {
                        "inline_keyboard": [
                            [
                                {
                                    "text": text,
                                    "callback_data": create_callback(
                                        conn,
                                        actor,
                                        connection_id,
                                        kind="approval",
                                        target_id=approval["id"],
                                        decision=decision,
                                        now=now(),
                                    ),
                                }
                                for text, decision in (("승인", "approve"), ("거절", "reject"))
                            ]
                        ]
                    }
                    set_value(conn, key, markup)
            status = {
                "succeeded": "업무를 완료했습니다.",
                "awaiting_approval": "업무 실행 승인이 필요합니다.",
                "awaiting_input": "업무에 필요한 입력을 화면에서 보완해 주세요.",
                "failed": "업무를 완료하지 못했습니다. 화면에서 상태를 확인해 주세요.",
                "expired": "업무 승인이 만료되었습니다.",
                "rejected": "업무 승인이 거절되었습니다.",
            }[run["state"]]
            prepare_delivery(
                conn,
                actor,
                connection_id,
                run["id"],
                text=status,
                reply_markup=markup,
                now=now(),
            )
            if run["state"] == "succeeded":
                for output in (run["result_json"] or {}).get("outputs", {}).values():
                    receipt = output.get("receipt") if isinstance(output, dict) else None
                    if isinstance(receipt, dict) and receipt.get("run_id") == run["id"]:
                        prepare_delivery(
                            conn,
                            actor,
                            connection_id,
                            run["id"],
                            artifact_id=receipt["artifact_id"],
                            now=now(),
                        )
        except AppError:
            # Pausing, re-pairing or revising authority must suppress an old delivery.
            continue
