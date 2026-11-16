"""One behavioral contract, replayed through Telegram HTTP and an in-memory adapter."""

import hashlib
import io
import json
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest
from pydantic import SecretStr

from kelly.channels.base import Action, ChannelAddress, InboundEvent, OutboundMessage
from kelly.channels.telegram import TelegramAdapter
from kelly.contracts import AppError

from .fake import FakeAdapter

ACTION = "approve:" + "a" * 24
PAYLOADS = [
    {
        "update_id": 3,
        "callback_query": {
            "id": "query-3",
            "from": {"id": 101, "is_bot": False},
            "message": {"message_id": 7, "chat": {"id": 101, "type": "private"}},
            "data": ACTION,
        },
    },
    {
        "update_id": 2,
        "message": {
            "from": {"id": 101, "is_bot": False},
            "chat": {"id": 101, "type": "private"},
            "text": "Synthetic request",
        },
    },
    {
        "update_id": 4,
        "message": {
            "from": {"id": 101, "is_bot": False},
            "chat": {"id": 101, "type": "private"},
            "text": "/pair synthetic-code",
        },
    },
]


def fake_event(payload, kind, *, text=None, action=None, ack_id=None):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return InboundEvent(
        payload["update_id"],
        kind,
        hashlib.sha256(raw).hexdigest(),
        len(raw),
        user_id=101,
        chat_id=101,
        private=True,
        human=True,
        text=text,
        action=action,
        ack_id=ack_id,
    )


@pytest.fixture(params=["telegram", "fake"])
def channel(request):
    acks, texts, files = [], {}, {}
    events = (
        fake_event(PAYLOADS[0], "action", action=ACTION, ack_id="query-3"),
        fake_event(PAYLOADS[1], "text", text="Synthetic request"),
        fake_event(PAYLOADS[2], "pair", text="/pair synthetic-code", action="synthetic-code"),
    )
    if request.param == "fake":
        adapter = FakeAdapter(events)
        yield (
            adapter,
            lambda ref: adapter.messages[ref.message_id].text,
            adapter.acknowledged,
            adapter.files,
        )
        return

    def remote(req):
        method = req.url.path.rsplit("/", 1)[1]
        if method == "sendDocument":
            document = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + req.headers["content-type"].encode() + b"\r\n\r\n" + req.content
            )
            parts = {
                part.get_param("name", header="content-disposition"): part
                for part in document.iter_parts()
            }
            message_id = str(len(texts) + 1)
            files[message_id] = parts["document"].get_payload(decode=True)
            texts[message_id] = parts["caption"].get_content().strip()
            result = {"message_id": int(message_id), "chat": {"id": 101}}
        else:
            body = json.loads(req.content)
            if method == "getWebhookInfo":
                result = {"url": ""}
            elif method == "getUpdates":
                result = [
                    payload for payload in PAYLOADS if payload["update_id"] >= body.get("offset", 0)
                ]
            elif method == "answerCallbackQuery":
                acks.append(body["callback_query_id"])
                result = True
            else:
                assert method in {"sendMessage", "editMessageText"}
                assert body["chat_id"] == 101
                assert "parse_mode" not in body
                assert body["link_preview_options"] == {"is_disabled": True}
                message_id = str(body.get("message_id", len(texts) + 1))
                texts[message_id] = body["text"]
                if "reply_markup" in body:
                    assert body["reply_markup"] == (
                        {"inline_keyboard": []} if method == "editMessageText" else
                        {"inline_keyboard": [[{"text": "Approve", "callback_data": ACTION}]]}
                    )
                result = {"message_id": int(message_id), "chat": {"id": 101}}
        return httpx.Response(200, json={"ok": True, "result": result})

    with TelegramAdapter(
        SecretStr("123:" + "a" * 24), http_transport=httpx.MockTransport(remote)
    ) as adapter:
        yield adapter, lambda ref: texts[ref.message_id], acks, files


def test_receive_preserves_identity_content_order_and_next_cursor(channel):
    adapter, _, _, _ = channel
    events, cursor = adapter.receive(None)
    assert [
        (e.event_id, e.kind, e.user_id, e.chat_id, e.private, e.human, e.text, e.action, e.ack_id)
        for e in events
    ] == [
        (2, "text", 101, 101, True, True, "Synthetic request", None, None),
        (3, "action", 101, 101, True, True, None, ACTION, "query-3"),
        (4, "pair", 101, 101, True, True, "/pair synthetic-code", "synthetic-code", None),
    ]
    assert cursor == "5"
    assert all(e.raw_sha256 and e.byte_size > 0 for e in events)


@pytest.mark.parametrize("cursor,ids,next_cursor", [("3", [3, 4], "5"), ("5", [], "5")])
def test_receive_respects_cursor_and_preserves_empty_cursor(channel, cursor, ids, next_cursor):
    adapter, _, _, _ = channel
    events, advanced = adapter.receive(cursor)
    assert [e.event_id for e in events] == ids
    assert advanced == next_cursor


def test_ack_only_acknowledges_action_events(channel):
    adapter, _, acks, _ = channel
    events, _ = adapter.receive(None)
    for event in events:
        adapter.ack(event)
    assert acks == ["query-3"]


def test_send_delivers_plain_text_and_buttons_with_a_receipt(channel):
    adapter, read_text, _, _ = channel
    ref = adapter.send(
        ChannelAddress(101), OutboundMessage("<b>Literal text</b>", ((Action("Approve", ACTION),),))
    )
    assert ref.address == ChannelAddress(101) and ref.message_id == "1"
    assert read_text(ref) == "<b>Literal text</b>"


def test_update_changes_the_original_message(channel):
    adapter, read_text, _, _ = channel
    ref = adapter.send(ChannelAddress(101), OutboundMessage("Before"))
    adapter.update(ref, OutboundMessage("After"))
    assert read_text(ref) == "After"


def test_send_file_delivers_original_bytes_with_caption(channel):
    adapter, read_text, _, files = channel
    with io.BytesIO(b"Synthetic verified output\n") as stream:
        ref = adapter.send(
            ChannelAddress(101), OutboundMessage("Result", file=stream, filename="result.txt")
        )
    assert files[ref.message_id] == b"Synthetic verified output\n"
    assert read_text(ref) == "Result"


def test_send_rejects_text_over_the_advertised_limit(channel):
    adapter, _, _, _ = channel
    with pytest.raises(AppError) as caught:
        adapter.send(ChannelAddress(101), OutboundMessage("x" * 4097))
    assert caught.value.code == "INVALID_INPUT"
