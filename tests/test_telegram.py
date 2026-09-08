import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from birkin_lite import db
from birkin_lite.contracts import Actor, AppError
from birkin_lite.telegram import (
    MAX_RESPONSE_BYTES,
    TelegramClient,
    TelegramError,
    cancel_delivery,
    create_pairing,
    deliver_once,
    ingest_update,
    poll_once,
    prepare_delivery,
    register_connection,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)
OWNER = Actor(id="owner", project_ids=["sales"])
TOKEN = SecretStr("123:" + "a" * 24)


def message(update_id, text="요청 결과를 정리해 주세요.", **changes):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": 101, "is_bot": False},
            "chat": {"id": 101, "type": "private"},
            "text": text,
            **changes,
        },
    }


def paired(tmp_path):
    conn = db.init_db(tmp_path / "state.db")
    register_connection(conn, OWNER, "telegram", "sales", now=NOW)
    pair = create_pairing(conn, OWNER, "telegram", now=NOW)
    result = ingest_update(conn, OWNER, "telegram", message(1, "/start " + pair["code"]), now=NOW)
    assert result["state"] == "enqueued" and result["run_id"] is None
    return conn


def succeeded_request(conn):
    received = ingest_update(conn, OWNER, "telegram", message(2), now=NOW)
    conn.execute("UPDATE runs SET state='succeeded' WHERE id=?", (received["run_id"],))
    return received["run_id"]


def test_client_has_no_constructor_io_and_sends_plain_text_without_secret_logs(caplog):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 9, "chat": {"id": 101}}}
        )

    with (
        caplog.at_level(logging.DEBUG),
        TelegramClient(TOKEN, http_transport=httpx.MockTransport(transport)) as client,
    ):
        assert requests == []
        result = client.send_message(101, "<b>그대로 표시</b>")
    assert result["message_id"] == 9
    body = json.loads(requests[0].content)
    assert body["text"] == "<b>그대로 표시</b>" and "parse_mode" not in body
    assert body["link_preview_options"] == {"is_disabled": True}
    assert TOKEN.get_secret_value() not in caplog.text


def test_poll_checks_webhook_and_uses_only_committed_next_offset(tmp_path):
    conn = paired(tmp_path)
    requests = []

    def transport(request):
        requests.append((request.url.path.rsplit("/", 1)[1], json.loads(request.content)))
        if requests[-1][0] == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        return httpx.Response(200, json={"ok": True, "result": [message(2)]})

    with TelegramClient(TOKEN, http_transport=httpx.MockTransport(transport)) as client:
        first = poll_once(client, conn, OWNER, "telegram", now=NOW, timeout=0)
        second = poll_once(client, conn, OWNER, "telegram", now=NOW, timeout=0)
    assert first[0]["run_id"] == second[0]["run_id"]
    assert [method for method, _ in requests] == ["getWebhookInfo", "getUpdates", "getUpdates"]
    assert requests[1][1]["offset"] == 2 and requests[2][1]["offset"] == 3
    assert requests[1][1]["allowed_updates"] == ["message", "callback_query"]
    assert requests[1][1]["limit"] == 10
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    conn.close()


def test_existing_webhook_is_reported_without_mutation():
    methods = []

    def transport(request):
        methods.append(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json={"ok": True, "result": {"url": "https://example.com/hook"}})

    with (
        TelegramClient(TOKEN, http_transport=httpx.MockTransport(transport)) as client,
        pytest.raises(TelegramError) as caught,
    ):
        client.check_webhook()
    assert caught.value.code == "TELEGRAM_WEBHOOK_ACTIVE"
    assert methods == ["getWebhookInfo"]


def test_client_rejects_response_batch_over_ten_updates():
    with (
        TelegramClient(
            TOKEN,
            http_transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"ok": True, "result": [message(i) for i in range(11)]}
                )
            ),
        ) as client,
        pytest.raises(TelegramError) as caught,
    ):
        client.get_updates()
    assert caught.value.code == "TELEGRAM_INVALID_RESPONSE"


@pytest.mark.parametrize(
    "result",
    [None, {"message_id": True, "chat": {"id": 101}}, {"message_id": 5, "chat": {"id": 202}}],
)
def test_unverifiable_success_is_unknown(result):
    with (
        TelegramClient(
            TOKEN,
            http_transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"ok": True, "result": result})
            ),
        ) as client,
        pytest.raises(TelegramError) as caught,
    ):
        client.send_message(101, "완료")
    assert caught.value.ambiguous


def test_oversized_response_and_network_errors_never_expose_token():
    with (
        TelegramClient(
            TOKEN,
            http_transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
            ),
        ) as client,
        pytest.raises(TelegramError) as caught,
    ):
        client.send_message(101, "완료")
    assert caught.value.code == "TELEGRAM_RESPONSE_LIMIT" and caught.value.ambiguous

    def disconnected(request):
        raise httpx.ReadTimeout(str(request.url), request=request)

    with (
        TelegramClient(TOKEN, http_transport=httpx.MockTransport(disconnected)) as client,
        pytest.raises(TelegramError) as caught,
    ):
        client.send_message(101, "완료")
    assert TOKEN.get_secret_value() not in str(caught.value)
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "changes", [{"forward_origin": {"type": "user"}}, {"document": {"file_id": "external-file"}}]
)
def test_unsupported_forward_and_attachment_are_durable_rejections(tmp_path, changes):
    conn = paired(tmp_path)
    result = ingest_update(conn, OWNER, "telegram", message(2, **changes), now=NOW)
    assert result["state"] == "rejected" and result["run_id"] is None
    assert db.get_value(conn, "telegram:cursor:telegram") == 3
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    conn.close()


def test_pairing_code_is_never_persisted_in_inbox_or_kv(tmp_path):
    conn = db.init_db(tmp_path / "state.db")
    register_connection(conn, OWNER, "telegram", "sales", now=NOW)
    pair = create_pairing(conn, OWNER, "telegram", now=NOW)
    ingest_update(conn, OWNER, "telegram", message(1, "/pair " + pair["code"]), now=NOW)
    saved = [
        row[0]
        for row in conn.execute(
            "SELECT value_json FROM kv UNION ALL SELECT payload_json FROM inbox_events"
        )
    ]
    assert all(pair["code"] not in value for value in saved)
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    conn.close()


def test_approval_word_is_only_an_ordinary_request(tmp_path):
    conn = paired(tmp_path)
    result = ingest_update(conn, OWNER, "telegram", message(2, "승인"), now=NOW)
    assert result["run_id"] is not None
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("output_format", ["text", "md", "csv", "xlsx", "docx"])
def test_explicit_format_command_creates_one_run_with_only_task_body(tmp_path, output_format):
    conn = paired(tmp_path)
    received = ingest_update(
        conn, OWNER, "telegram", message(2, "/" + output_format + " 합성 자료 정리"), now=NOW
    )
    assert received["state"] == "enqueued"
    request = db.get_run(conn, received["run_id"], OWNER)["request_json"]
    assert request["request"] == "합성 자료 정리" and request["output_format"] == output_format
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize(
    "text,error",
    [
        ("/xlsx", "INVALID_INPUT"),
        ("/docx   ", "INVALID_INPUT"),
        ("/pdf 업무", "UNSUPPORTED_CAPABILITY"),
        ("/approve 업무", "UNSUPPORTED_CAPABILITY"),
    ],
)
def test_empty_or_unknown_format_command_is_rejected_without_run(tmp_path, text, error):
    conn = paired(tmp_path)
    received = ingest_update(conn, OWNER, "telegram", message(2, text), now=NOW)
    assert received["state"] == "rejected" and received["run_id"] is None
    assert received["payload_json"]["error_code"] == error
    assert db.get_value(conn, "telegram:cursor:telegram") == 3
    conn.close()


def test_text_delivery_reuses_unique_outbox_and_preserves_run_result(tmp_path):
    conn = paired(tmp_path)
    run_id = succeeded_request(conn)
    first = prepare_delivery(conn, OWNER, "telegram", run_id, text="완료했습니다.", now=NOW)
    duplicate = prepare_delivery(conn, OWNER, "telegram", run_id, text="완료했습니다.", now=NOW)
    assert duplicate["id"] == first["id"]
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 7, "chat": {"id": 101}}}
        )

    with TelegramClient(TOKEN, http_transport=httpx.MockTransport(transport)) as client:
        sent = deliver_once(client, conn, tmp_path, OWNER, now=NOW)
        assert deliver_once(client, conn, tmp_path, OWNER, now=NOW) is None
    assert sent["state"] == "sent" and sent["remote_id"] == "7" and sent["attempt_count"] == 1
    assert len(requests) == 1 and db.get_run(conn, run_id, OWNER)["state"] == "succeeded"
    conn.close()


def test_cancel_pending_delivery_after_run_succeeds(tmp_path):
    conn = paired(tmp_path)
    run_id = succeeded_request(conn)
    row = prepare_delivery(conn, OWNER, "telegram", run_id, text="완료", now=NOW)
    assert cancel_delivery(conn, row["id"], OWNER, now=NOW)["state"] == "cancelled"
    assert db.get_run(conn, run_id, OWNER)["state"] == "succeeded"
    with pytest.raises(AppError):
        cancel_delivery(conn, row["id"], Actor(id="other", project_ids=["sales"]), now=NOW)
    conn.close()


def test_token_rotation_invalidates_pairing_and_prepared_delivery_without_http(tmp_path):
    conn = paired(tmp_path)
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": []})

    with TelegramClient(TOKEN, http_transport=httpx.MockTransport(transport)) as original:
        bound = register_connection(conn, OWNER, "telegram", "sales", now=NOW, client=original)
    run_id = succeeded_request(conn)
    prepare_delivery(conn, OWNER, "telegram", run_id, text="완료", now=NOW)
    with TelegramClient(
        SecretStr("456:" + "b" * 24), http_transport=httpx.MockTransport(transport)
    ) as rotated:
        failed = deliver_once(rotated, conn, tmp_path, OWNER, now=NOW)
    connection = db.get_value(conn, "telegram:connection:telegram")
    assert connection["revision"] == bound["revision"] + 1
    assert connection["user_id"] is None and connection["chat_id"] is None
    assert failed["state"] == "failed" and failed["attempt_count"] == 0
    assert requests == []
    assert TOKEN.get_secret_value() not in json.dumps(connection)
    conn.close()


def test_poll_evaluates_runtime_clock_after_long_poll_before_pairing(tmp_path):
    conn = db.init_db(tmp_path / "state.db")
    register_connection(conn, OWNER, "telegram", "sales", now=NOW)
    pair = create_pairing(conn, OWNER, "telegram", now=NOW)
    current = [NOW]

    def transport(request):
        if request.url.path.endswith("getWebhookInfo"):
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        current[0] = NOW + timedelta(minutes=11)
        return httpx.Response(
            200, json={"ok": True, "result": [message(1, "/pair " + pair["code"])]}
        )

    with TelegramClient(TOKEN, http_transport=httpx.MockTransport(transport)) as client:
        result = poll_once(client, conn, OWNER, "telegram", now=lambda: current[0])
    assert result[0]["state"] == "rejected"
    assert db.get_value(conn, "telegram:connection:telegram")["user_id"] is None
    assert db.get_value(conn, "telegram:cursor:telegram") == 2
    conn.close()


@pytest.mark.parametrize("bot_id", ["123", "456"])
def test_token_epoch_preserves_old_receipt_and_accepts_low_new_update_ids(tmp_path, bot_id):
    from test_telegram_recovery import (
        CONNECTION,
        callback,
        completed_document,
        pending_approval,
    )
    from test_telegram_recovery import (
        paired as recovery_paired,
    )

    from birkin_lite.artifacts import open_artifact
    from birkin_lite.telegram import create_callback

    conn = recovery_paired(tmp_path)
    no_requests = httpx.MockTransport(
        lambda _: pytest.fail("Local registration must not call HTTP")
    )
    with TelegramClient(TOKEN, http_transport=no_requests) as original:
        register_connection(conn, OWNER, CONNECTION, "sales", now=NOW, client=original)
    old_id, artifact_id, content = completed_document(conn, tmp_path)
    old_run = db.get_run(conn, old_id, OWNER)
    prepare_delivery(conn, OWNER, CONNECTION, old_id, artifact_id=artifact_id, now=NOW)
    _, approval_id = pending_approval(conn)
    old_callback = create_callback(
        conn, OWNER, CONNECTION, kind="approval", target_id=approval_id, now=NOW
    )
    ingest_update(conn, OWNER, CONNECTION, message(9000), now=NOW)
    assert db.get_value(conn, "telegram:cursor:" + CONNECTION) == 9001
    requests, pair = [], {}

    def transport(request):
        method, body = request.url.path.rsplit("/", 1)[1], json.loads(request.content)
        requests.append((method, body))
        if method == "getWebhookInfo":
            result = {"url": ""}
        elif method == "getUpdates":
            assert body["offset"] == 0
            result = [
                message(2, "새 연결의 새 요청"),
                message(1, "/pair " + pair["code"]),
                callback(3, old_callback),
            ]
        else:
            assert method == "answerCallbackQuery"
            result = True
        return httpx.Response(200, json={"ok": True, "result": result})

    with TelegramClient(
        SecretStr(bot_id + ":" + "b" * 24), http_transport=httpx.MockTransport(transport)
    ) as rotated:
        bound = register_connection(conn, OWNER, CONNECTION, "sales", now=NOW, client=rotated)
        assert bound["update_epoch"] == 2 and bound["user_id"] is None
        assert db.get_value(conn, "telegram:cursor:" + CONNECTION) == 0
        pair.update(create_pairing(conn, OWNER, CONNECTION, now=NOW))
        received = poll_once(rotated, conn, OWNER, CONNECTION, now=NOW)
        assert [row["state"] for row in received] == ["enqueued", "enqueued", "rejected"]
        assert received[1]["run_id"] != old_id
        assert received[1]["event_key"] == "2:2"
        failed = deliver_once(rotated, conn, tmp_path, OWNER, now=NOW)
    assert failed["state"] == "failed" and failed["attempt_count"] == 0
    assert all(method not in {"sendMessage", "sendDocument"} for method, _ in requests)
    assert db.get_value(conn, "telegram:cursor:" + CONNECTION) == 4
    assert db.get_run(conn, old_id, OWNER) == old_run
    assert (
        conn.execute("SELECT state FROM approvals WHERE id=?", (approval_id,)).fetchone()[0]
        == "pending"
    )
    assert {row[0] for row in conn.execute("SELECT event_key FROM inbox_events")} >= {
        "1",
        "2",
        "9000",
        "2:1",
        "2:2",
        "2:3",
    }
    with open_artifact(tmp_path, conn, artifact_id, OWNER, "sales") as original_file:
        assert original_file.read() == content
    conn.close()
