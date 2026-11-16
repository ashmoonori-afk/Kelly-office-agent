"""Compatibility imports for existing Telegram integrations."""

from ..channels.router import (
    cancel_delivery,
    create_callback,
    create_pairing,
    deliver_once,
    enable_automation,
    ingest_update,
    poll_once,
    prepare_delivery,
    register_connection,
)
from ..channels.telegram import MAX_RESPONSE_BYTES, MAX_UPDATE_BYTES, TelegramClient, TelegramError

__all__ = [
    "MAX_RESPONSE_BYTES",
    "MAX_UPDATE_BYTES",
    "TelegramClient",
    "TelegramError",
    "cancel_delivery",
    "create_callback",
    "create_pairing",
    "deliver_once",
    "enable_automation",
    "ingest_update",
    "poll_once",
    "prepare_delivery",
    "register_connection",
]
