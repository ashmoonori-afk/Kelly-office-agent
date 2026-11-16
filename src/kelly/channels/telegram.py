"""Telegram wire protocol, bounded update parsing and channel adapter."""

import hashlib
import json
import logging
import re

import httpx
from pydantic import SecretStr

from ..contracts import AppError
from .base import (
    ChannelAddress,
    ChannelCaps,
    ChannelError,
    InboundEvent,
    OutboundMessage,
    SentRef,
    _markup,
    _message_text,
    _private_id,
    require_message,
)

MAX_UPDATE_BYTES = 65536
MAX_RESPONSE_BYTES = 1024 * 1024
TelegramError = ChannelError


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

    name = "telegram"
    caps = ChannelCaps(True, True, True, 4096, 64, "pull")

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

    def receive(self, cursor: str | None, *, timeout: int = 25):
        if not self._webhook_checked:
            self.check_webhook()
        updates = self.get_updates(int(cursor) if cursor is not None else None, timeout=timeout)
        if any(
            not isinstance(update, dict)
            or type(update.get("update_id")) is not int
            or update["update_id"] < 0
            for update in updates
        ):
            raise TelegramError("TELEGRAM_INVALID_RESPONSE")
        updates.sort(key=lambda update: update["update_id"])
        events = [parse_update(update) for update in updates]
        return events, str(events[-1].event_id + 1) if events else cursor

    def send(self, to: ChannelAddress, message: OutboundMessage) -> SentRef:
        require_message(self.caps, message)
        if message.file is not None:
            result = self.send_document(to.chat_id, message.file, message.filename, message.text)
        else:
            markup = (
                {
                    "inline_keyboard": [
                        [{"text": action.text, "callback_data": action.data} for action in row]
                        for row in message.actions
                    ]
                }
                if message.actions
                else None
            )
            result = self.send_message(to.chat_id, message.text, markup)
        return SentRef(to, str(result["message_id"]))

    def update(self, ref: SentRef, message: OutboundMessage) -> None:
        require_message(self.caps, message)
        if message.file is not None:
            raise AppError("UNSUPPORTED_CAPABILITY")
        _private_id(ref.address.chat_id)
        _message_text(message.text, self.caps.max_text)
        payload = {
            "chat_id": ref.address.chat_id,
            "message_id": int(ref.message_id),
            "text": message.text,
            "link_preview_options": {"is_disabled": True},
        }
        if message.actions:
            payload["reply_markup"] = _markup(
                {
                    "inline_keyboard": [
                        [{"text": action.text, "callback_data": action.data} for action in row]
                        for row in message.actions
                    ]
                }
            )
        self._sent(
            self._request("editMessageText", payload=payload, sending=True), ref.address.chat_id
        )

    def ack(self, event: InboundEvent) -> None:
        if event.kind == "action":
            self.answer_callback_query(event.ack_id)


def parse_update(update) -> InboundEvent:
    """Normalize a bounded wire update; rejected events still retain their durable identity."""
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
    query = update.get("callback_query")
    callback = isinstance(query, dict)
    message = query if callback else update.get("message")
    original = query.get("message") if isinstance(query, dict) else message
    sender = message.get("from") if isinstance(message, dict) else None
    chat = original.get("chat") if isinstance(original, dict) else None
    text = message.get("text") if isinstance(message, dict) else None
    pair = (
        re.fullmatch(r"/(?:pair|start) ([A-Za-z0-9_-]{1,128})", text)
        if isinstance(text, str)
        else None
    )
    unsupported = isinstance(message, dict) and any(
        key in message
        for key in (
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
    )
    return InboundEvent(
        event_id=update["update_id"],
        kind="action" if callback else "pair" if pair else "file" if unsupported else "text",
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        user_id=sender.get("id") if isinstance(sender, dict) else None,
        chat_id=chat.get("id") if isinstance(chat, dict) else None,
        private=isinstance(chat, dict) and chat.get("type") == "private",
        human=isinstance(sender, dict) and sender.get("is_bot") is False,
        text=text,
        action=query.get("data") if callback else pair[1] if pair else None,
        ack_id=query.get("id") if callback else None,
        unsupported=unsupported and not callback,
        error_code="LIMIT_EXCEEDED"
        if len(raw) > MAX_UPDATE_BYTES
        else "UNSUPPORTED_CAPABILITY"
        if not isinstance(message, dict)
        else None,
    )


# The legacy client is also the adapter, so existing injected transports keep working.
TelegramAdapter = TelegramClient
