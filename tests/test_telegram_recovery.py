"""Adversarial persistence tests; only the remote Telegram boundary is replaced."""

import io
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from birkin_lite import db
from birkin_lite.contracts import ActionProposal, Actor, RunRequest, canonical_json, digest
from birkin_lite.telegram import (
    TelegramClient,
    create_callback,
    create_pairing,
    deliver_once,
    ingest_update,
    prepare_delivery,
    register_connection,
)

OWNER = Actor(id="owner", project_ids=["sales"])
NOW = datetime(2026, 9, 8, tzinfo=UTC)
TOKEN = SecretStr("123:" + "a" * 24)
CONNECTION = "personal"


def message(update_id, text, *, user=101, chat=101, chat_type="private", is_bot=False):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user, "is_bot": is_bot},
            "chat": {"id": chat, "type": chat_type},
            "text": text,
        },
    }


def callback(update_id, data, *, user=101, chat=101):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "query-" + str(update_id),
            "from": {"id": user, "is_bot": False},
            "message": {"message_id": 50, "chat": {"id": chat, "type": "private"}},
            "data": data,
        },
    }


def paired(tmp_path):
    conn = db.init_db(tmp_path / "state.db")
    register_connection(conn, OWNER, CONNECTION, "sales", now=NOW)
    pairing = create_pairing(conn, OWNER, CONNECTION, now=NOW)
    ingest_update(conn, OWNER, CONNECTION, message(1, "/pair " + pairing["code"]), now=NOW)
    return conn


def pending_approval(conn):
    """Seed a persisted pending action; these tests never perform its external effect."""
    run = db.create_run(
        conn, OWNER, "ui", "pending-action", RunRequest(request="결과 전달", project_id="sales")
    )
    step_id, approval_id = "pending-step", "pending-approval"
    proposal = ActionProposal(
        actor_id=OWNER.id,
        project_id="sales",
        run_id=run["id"],
        step_id=step_id,
        tool="artifact.publish",
        args={"recipient_ref": "owner"},
        source_hashes={},
        draft_hash=digest({}),
        recipient_ref="owner",
        scope_revision=1,
        expires_at=(NOW + timedelta(minutes=5)).isoformat(),
        impact="표시된 결과 전달",
    )
    conn.execute("UPDATE runs SET state='awaiting_approval' WHERE id=?", (run["id"],))
    conn.execute(
        "INSERT INTO steps(id,run_id,ordinal,kind,effect,state,spec_json) "
        "VALUES(?,?,0,'apply','external_write','awaiting_approval','{}')",
        (step_id, run["id"]),
    )
    conn.execute(
        "INSERT INTO approvals(id,run_id,step_id,actor_id,proposal_hash,proposal_json,expires_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            approval_id,
            run["id"],
            step_id,
            OWNER.id,
            digest(proposal),
            canonical_json(proposal),
            proposal.expires_at,
            NOW.isoformat(),
        ),
    )
    return run, approval_id


def fail_cursor_write(conn):
    conn.execute(
        "CREATE TRIGGER fail_cursor BEFORE UPDATE ON kv "
        "WHEN NEW.key='telegram:cursor:personal' "
        "BEGIN SELECT RAISE(ABORT,'injected cursor persistence failure'); END"
    )


def test_pair_code_expires_locks_after_five_attempts_and_cannot_be_reused(tmp_path):
    conn = db.init_db(tmp_path / "state.db")
    register_connection(conn, OWNER, CONNECTION, "sales", now=NOW)
    expired = create_pairing(conn, OWNER, CONNECTION, now=NOW)
    after = NOW + timedelta(minutes=10)
    ingest_update(conn, OWNER, CONNECTION, message(1, "/pair " + expired["code"]), now=after)
    ingest_update(conn, OWNER, CONNECTION, message(2, "Unauthorized after expiry"), now=after)
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    locked = create_pairing(conn, OWNER, CONNECTION, now=after)
    for update_id in range(3, 8):
        ingest_update(conn, OWNER, CONNECTION, message(update_id, "/pair wrong-code"), now=after)
    ingest_update(conn, OWNER, CONNECTION, message(8, "/pair " + locked["code"]), now=after)
    ingest_update(conn, OWNER, CONNECTION, message(9, "Unauthorized after lockout"), now=after)
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    fresh = create_pairing(conn, OWNER, CONNECTION, now=after)
    ingest_update(conn, OWNER, CONNECTION, message(10, "/pair " + fresh["code"]), now=after)
    ingest_update(
        conn,
        OWNER,
        CONNECTION,
        message(11, "/pair " + fresh["code"], user=202, chat=202),
        now=after,
    )
    ingest_update(
        conn,
        OWNER,
        CONNECTION,
        message(12, "Wrong user after code replay", user=202, chat=202),
        now=after,
    )
    ingest_update(conn, OWNER, CONNECTION, message(13, "Paired owner request"), now=after)
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize(
    "spoof", [{"user": 202}, {"chat": 202}, {"chat_type": "group"}, {"is_bot": True}]
)
def test_pairing_authority_requires_exact_private_user_and_chat(tmp_path, spoof):
    conn = paired(tmp_path)
    ingest_update(conn, OWNER, CONNECTION, message(2, "Run this request", **spoof), now=NOW)
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    conn.close()


def test_pairing_does_not_authorize_another_bot_connection(tmp_path):
    conn = paired(tmp_path)
    register_connection(conn, OWNER, "other-bot", "sales", now=NOW)
    ingest_update(conn, OWNER, "other-bot", message(2, "Same user and chat on wrong bot"), now=NOW)
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    conn.close()


def test_inbox_run_and_cursor_rollback_together_then_reopen_deduplicates(tmp_path):
    conn = paired(tmp_path)
    cursor = db.get_value(conn, "telegram:cursor:" + CONNECTION)
    fail_cursor_write(conn)
    update = message(2, "명시한 자료의 요약 초안 작성")
    with pytest.raises(sqlite3.IntegrityError):
        ingest_update(conn, OWNER, CONNECTION, update, now=NOW)
    assert db.get_value(conn, "telegram:cursor:" + CONNECTION) == cursor
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM inbox_events WHERE event_key='2'").fetchone()[0] == 0
    conn.execute("DROP TRIGGER fail_cursor")
    accepted = ingest_update(conn, OWNER, CONNECTION, update, now=NOW)
    conn.close()
    conn = db.init_db(tmp_path / "state.db")
    duplicate = ingest_update(conn, OWNER, CONNECTION, update, now=NOW)
    assert duplicate["run_id"] == accepted["run_id"]
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM inbox_events WHERE event_key='2'").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize(
    "broken", ["oversized", "callback_message", "callback_chat", "callback_sender"]
)
def test_invalid_payload_with_valid_id_is_rejected_without_blocking_next_update(tmp_path, broken):
    from birkin_lite.telegram import MAX_UPDATE_BYTES

    conn = paired(tmp_path)
    update = callback(2, "approve:" + "a" * 24)
    if broken == "oversized":
        update = message(2, "x" * (MAX_UPDATE_BYTES + 1))
    elif broken == "callback_message":
        update["callback_query"]["message"] = []
    elif broken == "callback_chat":
        update["callback_query"]["message"]["chat"] = []
    else:
        update["callback_query"]["from"] = []
    with TelegramClient(
        TOKEN,
        http_transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"ok": True, "result": True})
        ),
    ) as client:
        rejected = ingest_update(conn, OWNER, CONNECTION, update, now=NOW, client=client)
    assert rejected["state"] == "rejected" and rejected["run_id"] is None
    assert db.get_value(conn, "telegram:cursor:" + CONNECTION) == 3
    stored = conn.execute("SELECT payload_json FROM inbox_events WHERE event_key='2'").fetchone()[0]
    assert len(stored.encode()) < 1024
    accepted = ingest_update(conn, OWNER, CONNECTION, message(3, "다음 정상 요청"), now=NOW)
    assert accepted["run_id"] is not None
    assert db.get_value(conn, "telegram:cursor:" + CONNECTION) == 4
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    conn.close()


def test_opaque_callback_approval_and_nonce_rollback_with_cursor(tmp_path):
    conn = paired(tmp_path)
    _, approval_id = pending_approval(conn)
    data = create_callback(conn, OWNER, CONNECTION, kind="approval", target_id=approval_id, now=NOW)
    assert data.startswith("approve:") and len(data.encode()) <= 64
    assert approval_id not in data and OWNER.id not in data
    requests = []

    def remote(request):
        requests.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"ok": True, "result": True})

    with TelegramClient(TOKEN, http_transport=httpx.MockTransport(remote)) as client:
        ingest_update(conn, OWNER, CONNECTION, message(2, "승인"), now=NOW)
        assert conn.execute("SELECT state FROM approvals").fetchone()[0] == "pending"
        ingest_update(conn, OWNER, CONNECTION, callback(3, data, user=202), now=NOW, client=client)
        assert conn.execute("SELECT state FROM approvals").fetchone()[0] == "pending"
        fail_cursor_write(conn)
        with pytest.raises(sqlite3.IntegrityError):
            ingest_update(conn, OWNER, CONNECTION, callback(4, data), now=NOW, client=client)
        assert conn.execute("SELECT state FROM approvals").fetchone()[0] == "pending"
        conn.execute("DROP TRIGGER fail_cursor")
        ingest_update(conn, OWNER, CONNECTION, callback(4, data), now=NOW, client=client)
        assert conn.execute("SELECT state,revision FROM approvals").fetchone()[:] == ("approved", 2)
        ingest_update(conn, OWNER, CONNECTION, callback(5, data), now=NOW, client=client)
        assert conn.execute("SELECT state,revision FROM approvals").fetchone()[:] == ("approved", 2)
    assert requests and set(requests) == {"answerCallbackQuery"}
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    conn.close()


def test_expired_callback_and_foreign_actor_never_approve(tmp_path):
    from birkin_lite.contracts import AppError

    conn = paired(tmp_path)
    _, approval_id = pending_approval(conn)
    with pytest.raises(AppError):
        create_callback(
            conn,
            Actor(id="other", project_ids=["sales"]),
            CONNECTION,
            kind="approval",
            target_id=approval_id,
            now=NOW,
        )
    data = create_callback(conn, OWNER, CONNECTION, kind="approval", target_id=approval_id, now=NOW)
    with TelegramClient(
        TOKEN,
        http_transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"ok": True, "result": True})
        ),
    ) as client:
        ingest_update(
            conn,
            OWNER,
            CONNECTION,
            callback(2, data),
            now=NOW + timedelta(minutes=5),
            client=client,
        )
    assert conn.execute("SELECT state FROM approvals").fetchone()[0] != "approved"
    conn.close()


@pytest.mark.parametrize("change", ["decision_prefix", "proposal_hash", "run_state"])
def test_callback_rechecks_bound_decision_and_current_proposal(tmp_path, change):
    conn = paired(tmp_path)
    run, approval_id = pending_approval(conn)
    data = create_callback(conn, OWNER, CONNECTION, kind="approval", target_id=approval_id, now=NOW)
    if change == "decision_prefix":
        data = data.replace("approve:", "reject:", 1)
    elif change == "proposal_hash":
        conn.execute("UPDATE approvals SET proposal_hash=?", (digest({"changed": True}),))
    else:
        conn.execute("UPDATE runs SET state='cancelled' WHERE id=?", (run["id"],))
    with TelegramClient(
        TOKEN,
        http_transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"ok": True, "result": True})
        ),
    ) as client:
        ingest_update(conn, OWNER, CONNECTION, callback(2, data), now=NOW, client=client)
    assert conn.execute("SELECT state FROM approvals").fetchone()[0] == "pending"
    conn.close()


def completed_document(conn, home):
    from birkin_lite.artifacts import create_draft, publish_draft

    received = ingest_update(
        conn, OWNER, CONNECTION, message(2, "결과 파일 작성 후 나에게 전달"), now=NOW
    )
    run_id = received["run_id"]
    content = b"Verified result. Send only to the paired owner.\n"
    draft = create_draft(
        home, conn, io.BytesIO(content), "result.txt", OWNER, "sales", run_id, provenance={}
    )
    receipt = publish_draft(
        home, conn, draft["id"], OWNER, "sales", run_id=run_id, step_id="publish"
    )
    conn.execute(
        "UPDATE runs SET state='succeeded',result_json=? WHERE id=?",
        (canonical_json({"outputs": {"publish": {"receipt": receipt}}}), run_id),
    )
    return run_id, receipt["artifact_id"], content


@pytest.mark.parametrize("failure", ["timeout", "sent_persistence_crash"])
def test_uncertain_document_delivery_never_resends_or_regenerates(tmp_path, failure):
    from birkin_lite.artifacts import open_artifact
    from birkin_lite.engine import execute_next, resume_after_restart
    from birkin_lite.tools import registry

    conn = paired(tmp_path)
    run_id, artifact_id, content = completed_document(conn, tmp_path)
    delivery = prepare_delivery(conn, OWNER, CONNECTION, run_id, artifact_id=artifact_id, now=NOW)
    duplicate = prepare_delivery(conn, OWNER, CONNECTION, run_id, artifact_id=artifact_id, now=NOW)
    assert duplicate["id"] == delivery["id"]
    before = [
        tuple(row) for row in conn.execute("SELECT id,sha256,revision FROM artifacts ORDER BY id")
    ]
    requests = []

    def remote(request):
        requests.append(request.url.path.rsplit("/", 1)[-1])
        assert content in request.content
        if failure == "timeout":
            raise httpx.ReadTimeout("Synthetic response lost", request=request)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 901, "chat": {"id": 101}}}
        )

    if failure == "sent_persistence_crash":
        conn.execute(
            "CREATE TRIGGER fail_sent BEFORE UPDATE ON outbox WHEN NEW.state='sent' "
            "BEGIN SELECT RAISE(ABORT,'injected sent persistence failure'); END"
        )
    with TelegramClient(TOKEN, http_transport=httpx.MockTransport(remote)) as client:
        if failure == "sent_persistence_crash":
            with pytest.raises(sqlite3.IntegrityError, match="injected sent persistence failure"):
                deliver_once(client, conn, tmp_path, OWNER, now=NOW)
            assert conn.execute("SELECT state FROM outbox").fetchone()[0] == "sending"
        else:
            deliver_once(client, conn, tmp_path, OWNER, now=NOW)
            assert conn.execute("SELECT state FROM outbox").fetchone()[0] == "unknown"
        conn.close()
        conn = db.init_db(tmp_path / "state.db")
        resume_after_restart(conn, previous_process_is_dead=True)
        assert conn.execute("SELECT state FROM outbox").fetchone()[0] == "unknown"
        assert deliver_once(client, conn, tmp_path, OWNER, now=NOW + timedelta(hours=1)) is None
    assert requests == ["sendDocument"]
    assert (
        execute_next(
            conn,
            run_id,
            actor=OWNER,
            tools=registry(tmp_path),
            complete=None,
            now=NOW,
            home=tmp_path,
        )["state"]
        == "succeeded"
    )
    assert [
        tuple(row) for row in conn.execute("SELECT id,sha256,revision FROM artifacts ORDER BY id")
    ] == before
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    with open_artifact(tmp_path, conn, artifact_id, OWNER, "sales") as saved:
        assert saved.read() == content
    conn.close()


def test_retry_after_delays_known_unsent_delivery_and_stops_after_three_attempts(tmp_path):
    conn = paired(tmp_path)
    received = ingest_update(conn, OWNER, CONNECTION, message(2, "작업 완료 알림"), now=NOW)
    run_id = received["run_id"]
    conn.execute("UPDATE runs SET state='succeeded' WHERE id=?", (run_id,))
    prepare_delivery(conn, OWNER, CONNECTION, run_id, text="완료", now=NOW)
    requests = []

    def remote(request):
        requests.append(request)
        return httpx.Response(
            429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 10}}
        )

    with TelegramClient(TOKEN, http_transport=httpx.MockTransport(remote)) as client:
        first = deliver_once(client, conn, tmp_path, OWNER, now=NOW)
        assert first["state"] == "retry_wait" and first["attempt_count"] == 1
        assert deliver_once(client, conn, tmp_path, OWNER, now=NOW + timedelta(seconds=9)) is None
        assert len(requests) == 1
        deliver_once(client, conn, tmp_path, OWNER, now=NOW + timedelta(seconds=10))
        final = deliver_once(client, conn, tmp_path, OWNER, now=NOW + timedelta(seconds=20))
        assert final["state"] == "failed" and final["attempt_count"] == 3
        assert deliver_once(client, conn, tmp_path, OWNER, now=NOW + timedelta(hours=1)) is None
    assert len(requests) == 3
    assert db.get_run(conn, run_id, OWNER)["state"] == "succeeded"
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("parameters", [None, [], {"retry_after": "10"}, {"retry_after": True}])
def test_malformed_retry_metadata_is_unknown_and_never_retried(tmp_path, parameters):
    conn = paired(tmp_path)
    received = ingest_update(conn, OWNER, CONNECTION, message(2, "작업 상태 알려줘"), now=NOW)
    run_id = received["run_id"]
    conn.execute("UPDATE runs SET state='succeeded' WHERE id=?", (run_id,))
    prepare_delivery(conn, OWNER, CONNECTION, run_id, text="접수됨", now=NOW)
    requests = []

    def remote(request):
        requests.append(request)
        return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": parameters})

    with TelegramClient(TOKEN, http_transport=httpx.MockTransport(remote)) as client:
        result = deliver_once(client, conn, tmp_path, OWNER, now=NOW)
        assert result["state"] == "unknown"
        assert deliver_once(client, conn, tmp_path, OWNER, now=NOW + timedelta(hours=1)) is None
    assert len(requests) == 1
    assert db.get_run(conn, run_id, OWNER)["state"] == "succeeded"
    conn.close()


def test_preflight_pass_draft_and_unreferenced_result_cannot_be_delivered(tmp_path):
    from birkin_lite.artifacts import create_draft, publish_draft
    from birkin_lite.contracts import AppError

    conn = paired(tmp_path)
    received = ingest_update(conn, OWNER, CONNECTION, message(2, "결과 파일 준비"), now=NOW)
    run_id = received["run_id"]
    draft = create_draft(
        tmp_path,
        conn,
        io.BytesIO(b"Unreviewed intermediate output."),
        "draft.txt",
        OWNER,
        "sales",
        run_id,
        provenance={},
    )
    assert draft["validation_json"]["status"] == "pass"
    for state in ("running", "succeeded"):
        conn.execute("UPDATE runs SET state=? WHERE id=?", (state, run_id))
        with pytest.raises(AppError):
            prepare_delivery(conn, OWNER, CONNECTION, run_id, artifact_id=draft["id"], now=NOW)
    receipt = publish_draft(
        tmp_path, conn, draft["id"], OWNER, "sales", run_id=run_id, step_id="publish"
    )
    with pytest.raises(AppError):
        prepare_delivery(
            conn, OWNER, CONNECTION, run_id, artifact_id=receipt["artifact_id"], now=NOW
        )
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    conn.close()


def test_document_delivery_rechecks_receipt_before_network_send(tmp_path):
    conn = paired(tmp_path)
    run_id, artifact_id, _ = completed_document(conn, tmp_path)
    prepare_delivery(conn, OWNER, CONNECTION, run_id, artifact_id=artifact_id, now=NOW)
    result = db.get_run(conn, run_id, OWNER)["result_json"]
    result["outputs"]["publish"]["receipt"]["artifact_hash"] = "0" * 64
    conn.execute("UPDATE runs SET result_json=? WHERE id=?", (canonical_json(result), run_id))
    with TelegramClient(
        TOKEN,
        http_transport=httpx.MockTransport(
            lambda _: pytest.fail("Stale result receipt reached Telegram")
        ),
    ) as client:
        delivery = deliver_once(client, conn, tmp_path, OWNER, now=NOW)
    assert delivery["state"] == "failed" and delivery["attempt_count"] == 0
    conn.close()
