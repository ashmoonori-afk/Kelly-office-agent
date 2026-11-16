"""Channel values and the transport contract; no persistence or approval decisions."""

import re
from dataclasses import dataclass
from typing import BinaryIO, Literal, Protocol

from ..contracts import AppError


@dataclass(frozen=True, slots=True)
class ChannelCaps:
    buttons: bool
    edit_message: bool
    files: bool
    max_text: int
    max_action_bytes: int
    inbound: Literal["pull", "push"]


@dataclass(frozen=True, slots=True)
class ChannelAddress:
    chat_id: int


@dataclass(frozen=True, slots=True)
class Action:
    text: str
    data: str


@dataclass(frozen=True, slots=True)
class InboundEvent:
    event_id: int
    kind: Literal["text", "action", "file", "pair"]
    raw_sha256: str
    byte_size: int
    user_id: int | None = None
    chat_id: int | None = None
    private: bool = False
    human: bool = False
    text: str | None = None
    action: str | None = None
    ack_id: str | None = None
    unsupported: bool = False
    error_code: str | None = None
    ack_text: str | None = None


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    text: str = ""
    actions: tuple[tuple[Action, ...], ...] = ()
    file: BinaryIO | None = None
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class SentRef:
    address: ChannelAddress
    message_id: str


class ChannelError(AppError):
    def __init__(self, code, *, ambiguous=False, retry_after=None):
        super().__init__(code)
        self.ambiguous = ambiguous
        self.retry_after = retry_after


class ChannelAdapter(Protocol):
    name: str
    caps: ChannelCaps
    credential_fingerprint: str

    def receive(
        self, cursor: str | None, *, timeout: int = 25
    ) -> tuple[list[InboundEvent], str | None]: ...

    def send(self, to: ChannelAddress, message: OutboundMessage) -> SentRef: ...

    def update(self, ref: SentRef, message: OutboundMessage) -> None: ...

    def ack(self, event: InboundEvent) -> None: ...


def require_message(caps: ChannelCaps, message: OutboundMessage) -> None:
    """Reject unsupported delivery before claiming an outbox row or doing I/O."""
    if (message.actions and not caps.buttons) or (message.file is not None and not caps.files):
        raise AppError("UNSUPPORTED_CAPABILITY")
    if message.text:
        _message_text(message.text, min(caps.max_text, 1024) if message.file else caps.max_text)
    for row in message.actions:
        for action in row:
            if len(action.data.encode()) > caps.max_action_bytes:
                raise AppError("UNSUPPORTED_CAPABILITY")


def _private_id(value):
    if type(value) is not int or not 1 <= value < 2**52:
        raise AppError("POLICY_DENIED")
    return value


def _message_text(text, limit) -> str:
    if not isinstance(text, str):
        raise AppError("INVALID_INPUT")
    try:
        valid = (
            isinstance(text, str) and text.strip() and len(text.encode("utf-16-le")) // 2 <= limit
        )
    except UnicodeError:
        valid = False
    if not valid:
        raise AppError("INVALID_INPUT")
    return text


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
