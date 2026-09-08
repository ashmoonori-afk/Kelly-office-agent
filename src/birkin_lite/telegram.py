"""One private Telegram connection, durable ingress and independent result delivery."""

import hashlib
import hmac
import json
import logging
import re
import secrets
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import SecretStr, ValidationError

from .artifacts import metadata, open_artifact
from .contracts import Actor, AppError, RunRequest, canonical_json, digest
from .db import (
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
from .engine import resolve_approval

MAX_UPDATE_BYTES = 65536
MAX_RESPONSE_BYTES = 1024 * 1024


def _now(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise AppError("INVALID_INPUT")
    return value.astimezone(UTC)


class TelegramError(AppError):
    def __init__(self, code, *, ambiguous=False, retry_after=None):
        super().__init__(code)
        self.ambiguous = ambiguous
        self.retry_after = retry_after


class _TokenFilter(logging.Filter):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def filter(self, record):
        record.msg = record.getMessage().replace(self.token, "[REDACTED]")
        record.args = ()
        return True


class TelegramClient:
    """No I/O at construction; each explicit method makes one HTTPS attempt."""

    def __init__(self, token: SecretStr, *, http_transport=None):
        if not isinstance(token, SecretStr) or not re.fullmatch(
            r"[0-9]{1,20}:[A-Za-z0-9_-]{20,128}", token.get_secret_value()
        ):
            raise AppError("TELEGRAM_AUTH_ERROR")
        value = token.get_secret_value()
        self.credential_fingerprint = hashlib.sha256(
            b"birkin-lite:telegram-token:v1\0" + value.encode()
        ).hexdigest()
        self._filter = _TokenFilter(value)
        self._loggers = [
            logging.getLogger(name) for name in ("httpx", "httpcore.http11", "httpcore.http2")
        ]
        for logger in self._loggers:
            logger.addFilter(self._filter)
        self._client = httpx.Client(
            base_url="https://api.telegram.org/bot" + value + "/",
            verify=True,
            follow_redirects=False,
            trust_env=False,
            transport=http_transport
            if http_transport is not None
            else httpx.HTTPTransport(retries=0, trust_env=False),
        )
        self._webhook_checked = False

    def close(self):
        self._client.close()
        for logger in self._loggers:
            logger.removeFilter(self._filter)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _request(self, method, *, payload=None, files=None, timeout=30, sending=False):
        try:
            arguments = {"data": payload, "files": files} if files else {"json": payload or {}}
            with self._client.stream("POST", method, timeout=timeout, **arguments) as response:
                data = bytearray()
                for chunk in response.iter_bytes(65536):
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise TelegramError("TELEGRAM_RESPONSE_LIMIT", ambiguous=sending)
                try:
                    body = json.loads(data)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise TelegramError("TELEGRAM_INVALID_RESPONSE", ambiguous=sending) from None
                if not isinstance(body, dict) or type(body.get("ok")) is not bool:
                    raise TelegramError("TELEGRAM_INVALID_RESPONSE", ambiguous=sending)
                if response.is_success and body["ok"] is True and "result" in body:
                    return body["result"]
                error_code = body.get("error_code", response.status_code)
                if body["ok"] is False and error_code == 429:
                    parameters = body.get("parameters")
                    retry = parameters.get("retry_after") if isinstance(parameters, dict) else None
                    if type(retry) is int and 1 <= retry <= 86400:
                        raise TelegramError("TELEGRAM_RATE_LIMIT", retry_after=retry)
                    raise TelegramError("TELEGRAM_INVALID_RESPONSE", ambiguous=sending)
                ambiguous = sending and (response.status_code >= 500 or body["ok"] is True)
                raise TelegramError("TELEGRAM_SEND_FAILED", ambiguous=ambiguous)
        except httpx.RequestError:
            raise TelegramError("TELEGRAM_NETWORK_ERROR", ambiguous=sending) from None

    def check_webhook(self):
        result = self._request("getWebhookInfo")
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            raise TelegramError("TELEGRAM_INVALID_RESPONSE")
        if result["url"]:
            raise TelegramError("TELEGRAM_WEBHOOK_ACTIVE")
        self._webhook_checked = True
        return True

    def get_updates(self, offset=None, timeout=25):
        if (
            (offset is not None and (type(offset) is not int or offset < 0))
            or type(timeout) is not int
            or not 0 <= timeout <= 50
        ):
            raise AppError("INVALID_INPUT")
        payload = {
            "timeout": timeout,
            "limit": 10,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._request("getUpdates", payload=payload, timeout=timeout + 10)
        if not isinstance(result, list) or len(result) > 10:
            raise TelegramError("TELEGRAM_INVALID_RESPONSE")
        return result

    def answer_callback_query(self, callback_query_id):
        if not isinstance(callback_query_id, str) or not 1 <= len(callback_query_id) <= 256:
            raise AppError("INVALID_INPUT")
        return self._request(
            "answerCallbackQuery", payload={"callback_query_id": callback_query_id}, timeout=5
        )

    def send_message(self, chat_id, text, reply_markup=None):
        _message_text(text, 4096)
        _private_id(chat_id)
        payload = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if reply_markup is not None:
            payload["reply_markup"] = _markup(reply_markup)
        return self._sent(self._request("sendMessage", payload=payload, sending=True), chat_id)

    def send_document(self, chat_id, stream, filename, caption=""):
        _private_id(chat_id)
        if (
            not isinstance(filename, str)
            or not 1 <= len(filename) <= 240
            or re.search(r"[\\/:\x00-\x1f]", filename)
        ):
            raise AppError("INVALID_INPUT")
        if caption:
            _message_text(caption, 1024)
        return self._sent(
            self._request(
                "sendDocument",
                payload={"chat_id": str(chat_id), "caption": caption},
                files={"document": (filename, stream, "application/octet-stream")},
                timeout=60,
                sending=True,
            ),
            chat_id,
        )

    @staticmethod
    def _sent(result, chat_id):
        if (
            not isinstance(result, dict)
            or type(result.get("message_id")) is not int
            or result["message_id"] <= 0
            or not isinstance(result.get("chat"), dict)
            or type(result["chat"].get("id")) is not int
            or result["chat"]["id"] != chat_id
        ):
            raise TelegramError("TELEGRAM_INVALID_RESPONSE", ambiguous=True)
        return result


def _private_id(value):
    if type(value) is not int or not 1 <= value < 2**52:
        raise AppError("POLICY_DENIED")
    return value


def _message_text(text, limit):
    try:
        valid = (
            isinstance(text, str) and text.strip() and len(text.encode("utf-16-le")) // 2 <= limit
        )
    except UnicodeError:
        valid = False
    if not valid:
        raise AppError("INVALID_INPUT")


def _markup(value):
    if not isinstance(value, dict) or set(value) != {"inline_keyboard"}:
        raise AppError("INVALID_INPUT")
    rows = value["inline_keyboard"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
        raise AppError("INVALID_INPUT")
    for row in rows:
        if not isinstance(row, list) or not 1 <= len(row) <= 4:
            raise AppError("INVALID_INPUT")
        for button in row:
            if not isinstance(button, dict) or set(button) != {"text", "callback_data"}:
                raise AppError("INVALID_INPUT")
            _message_text(button["text"], 100)
            if not isinstance(button["callback_data"], str) or not re.fullmatch(
                r"(?:approve|reject):[A-Za-z0-9_-]{20,40}", button["callback_data"]
            ):
                raise AppError("INVALID_INPUT")
    return value


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


def _identity(message, *, callback=False):
    sender = message.get("from")
    original = message.get("message") if callback else message
    chat = original.get("chat") if isinstance(original, dict) else None
    if (
        not isinstance(sender, dict)
        or sender.get("is_bot") is not False
        or not isinstance(chat, dict)
        or chat.get("type") != "private"
    ):
        raise AppError("POLICY_DENIED")
    user_id, chat_id = _private_id(sender.get("id")), _private_id(chat.get("id"))
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


def ingest_update(conn, actor: Actor, connection_id, update, *, now, client=None):
    """Persist one bounded update, its effect and the next offset in one transaction."""
    now = _now(now)
    try:
        raw = json.dumps(
            update, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8", errors="surrogatepass")
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise AppError("INVALID_INPUT") from None
    if (
        not isinstance(update, dict)
        or type(update.get("update_id")) is not int
        or update["update_id"] < 0
    ):
        raise AppError("INVALID_INPUT")
    raw_hash = hashlib.sha256(raw).hexdigest()
    if client is not None:
        _bind_client(conn, actor, connection_id, client, now)
    query = update.get("callback_query")
    if len(raw) <= MAX_UPDATE_BYTES and isinstance(query, dict) and client is not None:
        try:
            client.answer_callback_query(query.get("id"))
        except (TelegramError, AppError):
            pass  # A lost UI acknowledgement never grants or blocks durable authorization.
    with transaction(conn):
        connection = _connection(conn, actor, connection_id)
        epoch = connection.get("update_epoch", 1)
        key = (str(epoch) + ":" if epoch != 1 else "") + str(update["update_id"])
        existing = row_dict(
            conn.execute(
                "SELECT * FROM inbox_events WHERE source='telegram' AND connection_id=? AND event_key=?",
                (connection_id, key),
            ).fetchone()
        )
        if existing:
            if (
                existing["actor_id"] != actor.id
                or existing["payload_json"].get("raw_sha256") != raw_hash
            ):
                raise AppError("CONFLICT", status=409)
            return existing
        payload = {"update_id": update["update_id"], "raw_sha256": raw_hash}
        conn.execute(
            "INSERT INTO inbox_events(source,connection_id,event_key,actor_id,payload_json,received_at) VALUES ('telegram',?,?,?,?,?)",
            (connection_id, key, actor.id, canonical_json(payload), now.isoformat()),
        )
        state, run_id = "rejected", None
        try:
            if len(raw) > MAX_UPDATE_BYTES:
                raise AppError("LIMIT_EXCEEDED")
            if isinstance(query, dict):
                user_id, chat_id = _identity(query, callback=True)
                _authenticate(connection, user_id, chat_id)
                if client is None:
                    raise AppError("INVALID_INPUT")
                payload["callback_result"] = _callback(
                    conn, actor, connection, query.get("data"), now
                )
                state = "enqueued"
            else:
                message = update.get("message")
                if not isinstance(message, dict):
                    raise AppError("UNSUPPORTED_CAPABILITY")
                user_id, chat_id = _identity(message)
                if any(
                    k in message
                    for k in (
                        "forward_origin",
                        "forward_from",
                        "forward_sender_name",
                        "forward_from_chat",
                        "document",
                        "photo",
                        "audio",
                        "video",
                        "voice",
                    )
                ):
                    raise AppError("UNSUPPORTED_CAPABILITY")
                text = message.get("text")
                _message_text(text, 4096)
                pair = re.fullmatch(r"/(?:pair|start) ([A-Za-z0-9_-]{1,128})", text)
                if pair:
                    _pair(conn, connection, pair[1], user_id, chat_id, now)
                    payload["kind"], state = "pairing", "enqueued"
                else:
                    _authenticate(connection, user_id, chat_id)
                    request, output_format = text, "text"
                    if text.startswith("/"):
                        parts = text.split(maxsplit=1)
                        output_format = parts[0][1:]
                        if output_format not in {"text", "md", "csv", "xlsx", "docx"}:
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
        set_value(conn, cursor_key, max(cursor, update["update_id"] + 1), now=now.isoformat())
        return row_dict(
            conn.execute(
                "SELECT * FROM inbox_events WHERE source='telegram' AND connection_id=? AND event_key=?",
                (connection_id, key),
            ).fetchone()
        )


def poll_once(client, conn, actor, connection_id, *, now, timeout=25):
    """Use a clock callable at runtime so expiry is checked after blocking polling."""
    _bind_client(conn, actor, connection_id, client, _now(now() if callable(now) else now))
    if not client._webhook_checked:
        client.check_webhook()
    updates = client.get_updates(
        get_value(conn, "telegram:cursor:" + connection_id), timeout=timeout
    )
    if any(
        not isinstance(update, dict)
        or type(update.get("update_id")) is not int
        or update["update_id"] < 0
        for update in updates
    ):
        raise TelegramError("TELEGRAM_INVALID_RESPONSE")
    updates.sort(key=lambda update: update["update_id"])
    return [
        ingest_update(
            conn, actor, connection_id, update, now=now() if callable(now) else now, client=client
        )
        for update in updates
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
            if stream is not None:
                result = client.send_document(
                    authority["chat_id"], stream, payload["filename"], payload["text"]
                )
            else:
                result = client.send_message(
                    authority["chat_id"], payload["text"], payload["reply_markup"]
                )
            sent = True
        with transaction(conn):
            conn.execute(
                "UPDATE outbox SET state='sent',remote_id=?,sent_at=?,error_code=NULL WHERE id=? AND state='sending'",
                (str(result["message_id"]), now.isoformat(), row["id"]),
            )
    except (TelegramError, AppError, OSError) as error:
        code = error.code if isinstance(error, AppError) else "SOURCE_CHANGED"
        with transaction(conn):
            fresh = row_dict(
                conn.execute("SELECT * FROM outbox WHERE id=?", (row["id"],)).fetchone()
            )
            state, due = "failed", None
            if sent or (isinstance(error, TelegramError) and error.ambiguous):
                state = "unknown"
            elif (
                isinstance(error, TelegramError)
                and error.retry_after is not None
                and fresh["attempt_count"] < 3
            ):
                state, due = "retry_wait", (now + timedelta(seconds=error.retry_after)).isoformat()
            conn.execute(
                "UPDATE outbox SET state=?,next_attempt_at=?,error_code=? WHERE id=? AND state IN ('pending','retry_wait','sending')",
                (state, due, code, row["id"]),
            )
    return row_dict(conn.execute("SELECT * FROM outbox WHERE id=?", (row["id"],)).fetchone())
