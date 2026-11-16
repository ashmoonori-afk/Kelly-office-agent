"""In-memory channel transport with observable messages and acknowledgements."""

from kelly.channels.base import (
    ChannelAddress,
    ChannelCaps,
    InboundEvent,
    OutboundMessage,
    SentRef,
    require_message,
)
from kelly.contracts import AppError


class FakeAdapter:
    """A mutable transport store; events and delivery receipts never need a network."""

    name = "fake"
    credential_fingerprint = "synthetic-credential"

    def __init__(self, events: tuple[InboundEvent, ...] = (), *, caps: ChannelCaps | None = None):
        self.caps = caps or ChannelCaps(True, True, True, 4096, 64, "pull")
        self.events = events
        self.messages: dict[str, OutboundMessage] = {}
        self.files: dict[str, bytes] = {}
        self.acknowledged: list[str | None] = []
        self.cursors: list[str | None] = []
        self.send_calls = 0

    def receive(
        self, cursor: str | None, *, timeout: int = 25
    ) -> tuple[list[InboundEvent], str | None]:
        self.cursors.append(cursor)
        events = sorted(
            (event for event in self.events if cursor is None or event.event_id >= int(cursor)),
            key=lambda event: event.event_id,
        )[:10]
        return events, str(events[-1].event_id + 1) if events else cursor

    def send(self, to: ChannelAddress, message: OutboundMessage) -> SentRef:
        self.send_calls += 1
        require_message(self.caps, message)
        message_id = str(len(self.messages) + 1)
        self.messages[message_id] = message
        if message.file is not None:
            self.files[message_id] = message.file.read()
        return SentRef(to, message_id)

    def update(self, ref: SentRef, message: OutboundMessage) -> None:
        if not self.caps.edit_message:
            raise AppError("UNSUPPORTED_CAPABILITY")
        require_message(self.caps, message)
        self.messages[ref.message_id] = message

    def ack(self, event: InboundEvent) -> None:
        if event.kind == "action":
            self.acknowledged.append(event.ack_id)
