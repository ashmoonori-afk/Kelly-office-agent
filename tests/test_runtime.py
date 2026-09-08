"""Real SQLite, OS lock, executor and clients; only outbound HTTP is simulated."""

import asyncio
import hashlib
import json
import threading
from contextlib import closing, contextmanager
from datetime import UTC, datetime

import httpx
import pytest

from birkin_lite import config, db, engine, telegram, tools
from birkin_lite.contracts import AppError, PlanV1, RunRequest, TaskSpec, canonical_json
from birkin_lite.runtime import Runtime

TEXT = "동료에게 짧은 인사말 작성"
CANDIDATE = {"sections": {"content": {"paragraphs": ["함께 좋은 하루를 시작해요."]}}}
REVIEW = {"validation": {"status": "pass", "checked_layers": ["semantic_review"]}}


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)


def make_runtime(tmp_path, **kwargs):
    return Runtime(config.AppConfig(project_ids=["sales"]), tmp_path / "config.json", **kwargs)


@contextmanager
def manual_worker(runtime):
    # Exercise the public single-iteration method without a competing background loop.
    with runtime._lock:
        runtime._bootstrap()
        try:
            yield runtime
        finally:
            for lane in list(runtime._services):
                runtime._close_service(lane)


def create(runtime, key="job", *, text=TEXT, quality="balanced"):
    with closing(db.init_db(runtime.db_path)) as conn:
        return db.create_run(
            conn,
            runtime.actor,
            "ui",
            key,
            RunRequest(request=text, project_id="sales", quality_mode=quality),
        )


def read(runtime, run_id):
    with closing(db.init_db(runtime.db_path)) as conn:
        return db.get_run(conn, run_id, runtime.actor)


def planned():
    return {
        "task_spec": {"intent": "greeting", "source_ids": [], "acceptance": [TEXT]},
        "plan": {
            "requirements": [{"id": "r1", "text": TEXT}],
            "todos": [
                {
                    "id": "t1",
                    "title": TEXT,
                    "requirement_ids": ["r1"],
                    "kind": "simple",
                    "reason": "짧은 인사말 작성",
                    "signals": ["extraction"],
                    "clarity": "clear",
                    "usefulness": "required",
                    "executor": {"type": "model", "role": "L", "task": "analyze"},
                    "output_schema": "ReportSectionsV1",
                    "done_when": [TEXT],
                }
            ],
        },
    }


def qualified_config(tmp_path, monkeypatch):
    """Synthetic qualification fixture, never evidence of a live provider evaluation."""
    monkeypatch.setenv("XAI_API_KEY", "synthetic-key")
    tasks = ["planning", "analyze", "semantic_review"]
    cases = []
    for role in ["L", "M", "H"]:
        for i in range(20):
            cases.append(
                {
                    "case_id": f"{role}{i}",
                    "role": role,
                    "task": tasks[i % 3],
                    "request_id": f"fixture-{role}-{i}",
                    "usage": {
                        "input_tokens_total": 10,
                        "output_tokens_total": 5,
                        "usage_basis": "provider",
                    },
                    "fixture_sha256": hashlib.sha256(f"{role}{i}".encode()).hexdigest(),
                    "output_sha256": "2" * 64,
                    "schema_pass": True,
                    "numeric_pass": True,
                    "source_pass": True,
                    "policy_pass": True,
                    "accepted": True,
                    "reference_accepted": True,
                }
            )
    report = config.QualificationReport.model_validate(
        {
            "evidence_id": "fixture",
            "provider": "xai",
            "model_id": "fixture-model",
            "mode": "live",
            "completed_at": "2026-09-08T00:00:00Z",
            "cases": cases,
        }
    )
    folder = tmp_path / "evaluations"
    folder.mkdir()
    data = canonical_json(report).encode()
    (folder / "fixture.json").write_bytes(data)
    return config.AppConfig(
        project_ids=["sales"],
        privacy=[{"project_id": "sales", "allow_cloud": True, "allowed_providers": ["xai"]}],
        models=[
            {
                "binding": {
                    "provider": "xai",
                    "model_id": "fixture-model",
                    "roles": ["L", "M", "H"],
                    "enabled": True,
                    "allowed_projects": ["sales"],
                    "capabilities": ["structured_output", *tasks],
                    "context_window": 32768,
                    "max_output": 2048,
                    "metering_mode": "api",
                    "quality_evidence_id": "fixture",
                },
                "quality_evidence_sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
    )


def response(output, call_no):
    return httpx.Response(
        200,
        json={
            "id": f"fixture-request-{call_no}",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(output),
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 30,
                "completion_tokens": 20,
                "total_tokens": 50,
                "completion_tokens_details": {"reasoning_tokens": 0},
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        },
    )


def model_http(seen, *, before_generate=None):
    def handle(request):
        seen.append((request.method, request.url.path, threading.get_ident()))
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        payload = json.loads(request.content)
        schema = payload["response_format"]["json_schema"]["schema"]
        if "plan" in schema["properties"]:
            output = planned()
        elif "validation" in schema["properties"]:
            output = REVIEW
        else:
            if before_generate:
                before_generate()
            output = CANDIDATE
        return response(output, len(seen))

    return handle


async def wait_for(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_default_offline_missing_input_runs_once_per_current_revision(tmp_path):
    network = []
    transport = httpx.MockTransport(lambda request: network.append(request))
    runtime = make_runtime(tmp_path, model_transport=transport, telegram_transport=transport)
    run = create(runtime, text="회의록 요약해 줘")
    with manual_worker(runtime):
        assert runtime.worker_once()  # deterministic recipe preparation
        assert runtime.worker_once()  # known missing source enters awaiting_input
        waiting = read(runtime, run["id"])
        assert waiting["state"] == "awaiting_input"
        assert runtime.worker_once() is False
        with closing(db.init_db(runtime.db_path)) as conn:
            db.set_value(conn, "prepare_pending:" + run["id"], {"revision": 1})
        assert runtime.worker_once() is False  # stale marker cannot re-run planning
        with closing(db.init_db(runtime.db_path)) as conn:
            changed = db.supply_inputs(
                conn,
                run["id"],
                runtime.actor,
                TaskSpec(intent="summarize", source_ids=[], acceptance=["요약"]),
                expected_revision=waiting["revision"],
            )
            assert changed["state"] == "awaiting_input"
        assert runtime.worker_once()
        with closing(db.init_db(runtime.db_path)) as conn:
            assert db.get_value(conn, "prepare_pending:" + run["id"]) is None
        assert runtime.worker_once()
        assert runtime.worker_once() is False
        assert runtime.scheduler_once() is False
        assert runtime.poll_once() is False and runtime.outbox_once() is False
    assert network == []
    assert read(runtime, run["id"])["state"] == "awaiting_input"


def test_prepare_failure_is_durable_and_not_repeated(tmp_path):
    runtime = make_runtime(tmp_path)
    run = create(runtime)
    with manual_worker(runtime):
        assert runtime.worker_once()
        failed = read(runtime, run["id"])
        assert failed["state"] == "failed" and failed["error_code"] == "NO_CAPABLE_MODEL"
        assert failed["result_json"]["partial"] is True
        assert runtime.worker_once() is False
        assert read(runtime, run["id"]) == failed


def test_real_metering_high_review_and_snapshot_are_connected(tmp_path, monkeypatch):
    base = qualified_config(tmp_path, monkeypatch)
    seen = []
    runtime = Runtime(
        base, tmp_path / "config.json", model_transport=httpx.MockTransport(model_http(seen))
    )
    run = create(runtime, quality="high")

    async def scenario():
        async with runtime:
            await wait_for(lambda: read(runtime, run["id"])["state"] in engine.TERMINAL)
            before = len(seen)
            snapshot = runtime.catalog_snapshot()
            assert snapshot["statuses"][0]["state"] == "ready"
            snapshot["statuses"][0]["state"] = "tampered"
            assert runtime.catalog_snapshot()["statuses"][0]["state"] == "ready"
            assert len(seen) == before

    asyncio.run(scenario())
    finished = read(runtime, run["id"])
    assert finished["state"] == "succeeded", finished
    assert finished["plan_json"]["current"]["todos"][0]["kind"] == "simple"
    with closing(db.init_db(runtime.db_path)) as conn:
        calls = [
            db.row_dict(row) for row in conn.execute("SELECT * FROM calls ORDER BY logical_call_no")
        ]
        assert [c["purpose"] for c in calls] == ["plan", "generate", "review"]
        assert [c["route_json"]["role"] for c in calls] == ["M", "L", "H"]
        assert all(c["state"] == "settled" and c["usage_basis"] == "provider" for c in calls)
        assert sum(c["input_tokens_total"] for c in calls) == 90
    assert {item[2] for item in seen} != {threading.get_ident()}


@pytest.mark.parametrize("cancel_waiter", [False, True])
def test_shutdown_drains_actual_thread_before_client_and_lock_release(
    tmp_path, monkeypatch, cancel_waiter
):
    base = qualified_config(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    seen, closed = [], []

    def block():
        entered.set()
        assert release.wait(10)
        assert closed == []

    class ObservedTransport(httpx.MockTransport):
        def close(self):
            closed.append(release.is_set())
            super().close()

    runtime = Runtime(
        base,
        tmp_path / "config.json",
        model_transport=ObservedTransport(model_http(seen, before_generate=block)),
    )
    first = create(runtime, "first")
    second = create(runtime, "second")

    async def scenario():
        await runtime.start()
        try:
            await wait_for(entered.is_set)
            stopping = asyncio.create_task(runtime.stop())
            await asyncio.sleep(0.05)
            assert not stopping.done() and closed == []
            with (
                pytest.raises(AppError) as locked,
                engine.DaemonLock(runtime.home / "daemon.lock"),
            ):
                pass
            assert locked.value.code == "ALREADY_RUNNING"
            with closing(db.init_db(runtime.db_path)) as conn:
                assert (
                    conn.execute("SELECT count(*) FROM calls WHERE state='in_flight'").fetchone()[0]
                    == 1
                )
            assert read(runtime, second["id"])["state"] == "queued"
            if cancel_waiter:
                stopping.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await stopping
                assert closed == []
            release.set()
            await runtime.stop()
            if not cancel_waiter:
                await stopping
        finally:
            release.set()
            await runtime.stop()

    asyncio.run(scenario())
    assert closed == [True]
    assert read(runtime, first["id"])["state"] == "succeeded"
    assert read(runtime, second["id"])["state"] == "queued"
    with engine.DaemonLock(runtime.home / "daemon.lock"):
        pass


def test_refresh_applies_settings_without_claiming_qualification(tmp_path, monkeypatch):
    base = qualified_config(tmp_path, monkeypatch)
    (tmp_path / "evaluations" / "fixture.json").unlink()
    seen = []
    runtime = Runtime(
        base, tmp_path / "config.json", model_transport=httpx.MockTransport(model_http(seen))
    )

    async def scenario():
        async with runtime:
            assert runtime.catalog_snapshot()["statuses"][0]["state"] == "unqualified"
            with closing(db.init_db(runtime.db_path)) as conn:
                models = base.model_dump()["models"]
                models[0]["binding"]["enabled"] = False
                db.set_value(conn, "settings", {"models": models})
            snapshot = await runtime.refresh()
            assert snapshot["statuses"][0]["state"] == "disabled"
            assert snapshot["availability_checked"] is False

    asyncio.run(scenario())
    assert [method for method, *_ in seen] == ["GET"]


def test_cancellation_does_not_need_model_or_valid_credentials(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    run = create(runtime)
    with manual_worker(runtime):
        with closing(db.init_db(runtime.db_path)) as conn:
            engine.request_cancel(conn, run["id"], runtime.actor)
        monkeypatch.setenv("XAI_API_KEY", "invalid\nkey")
        assert runtime.worker_once()
        assert read(runtime, run["id"])["state"] == "cancelled"


def test_restart_recovers_real_running_tool_then_executes_once(tmp_path):
    runtime = make_runtime(tmp_path)
    run = create(runtime)
    plan = PlanV1.model_validate(planned()["plan"])
    todo = plan.todos[0]
    todo.kind, todo.signals = "mechanical", ["fixed_transform"]
    from birkin_lite.contracts import ToolExecutor

    todo.executor = ToolExecutor(tool_name="memory.search", args={"query": "인사말"})
    todo.output_schema = "MemoryListV1"
    with closing(db.init_db(runtime.db_path)) as conn:
        engine.install_plan(
            conn, run["id"], plan, runtime.actor, tools=tools.registry(runtime.home)
        )
        conn.execute(
            "UPDATE steps SET state='running',attempt_count=1 WHERE run_id=?", (run["id"],)
        )
    with manual_worker(runtime):
        assert runtime.worker_once()
        assert runtime.worker_once() is False
    assert read(runtime, run["id"])["state"] == "succeeded"
    with closing(db.init_db(runtime.db_path)) as conn:
        assert conn.execute("SELECT attempt_count FROM steps").fetchone()[0] == 2


def test_long_poll_disabling_connection_discards_late_update(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:" + "a" * 24)
    base = config.AppConfig(
        project_ids=["sales"], telegram={"enabled": True, "project_id": "sales"}
    )
    paths = []
    runtime = None

    def handle(request):
        paths.append(request.url.path)
        if request.url.path.endswith("getWebhookInfo"):
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        assert request.url.path.endswith("getUpdates")
        with closing(db.init_db(runtime.db_path)) as conn:
            db.set_value(conn, "settings", {"telegram": {"enabled": False, "project_id": "sales"}})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "from": {"id": 101},
                            "chat": {"id": 101, "type": "private"},
                            "text": TEXT,
                        },
                    }
                ],
            },
        )

    runtime = Runtime(
        base, tmp_path / "config.json", telegram_transport=httpx.MockTransport(handle)
    )
    with manual_worker(runtime):
        with pytest.raises(AppError) as denied:
            runtime.poll_once()
        assert denied.value.code == "POLICY_DENIED"
        assert runtime.poll_once() is False
    with closing(db.init_db(runtime.db_path)) as conn:
        assert conn.execute("SELECT count(*) FROM inbox_events").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert len(paths) == 2


def test_ui_results_never_enter_telegram_outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:" + "a" * 24)
    base = config.AppConfig(
        project_ids=["sales"], telegram={"enabled": True, "project_id": "sales"}
    )
    network = []
    runtime = Runtime(
        base,
        tmp_path / "config.json",
        telegram_transport=httpx.MockTransport(lambda request: network.append(request)),
    )
    run = create(runtime)
    with closing(db.init_db(runtime.db_path)) as conn:
        telegram.register_connection(
            conn, runtime.actor, "telegram", "sales", now=datetime.now(UTC)
        )
        conn.execute("UPDATE runs SET state='succeeded' WHERE id=?", (run["id"],))
    with manual_worker(runtime):
        assert runtime.outbox_once() is False
    with closing(db.init_db(runtime.db_path)) as conn:
        assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert network == []


def received_run(runtime, conn, connection_id):
    now = datetime.now(UTC)
    telegram.register_connection(conn, runtime.actor, connection_id, "sales", now=now)
    pair = telegram.create_pairing(conn, runtime.actor, connection_id, now=now)

    def update(number, text):
        return {
            "update_id": number,
            "message": {
                "message_id": number,
                "from": {"id": 101, "is_bot": False},
                "chat": {"id": 101, "type": "private"},
                "text": text,
            },
        }

    telegram.ingest_update(
        conn,
        runtime.actor,
        connection_id,
        update(1, "/start " + pair["code"]),
        now=now,
    )
    received = telegram.ingest_update(
        conn,
        runtime.actor,
        connection_id,
        update(2, TEXT),
        now=now,
    )
    conn.execute("UPDATE runs SET state='succeeded' WHERE id=?", (received["run_id"],))
    return received["run_id"]


def test_outbox_never_selects_previous_connection_after_settings_change(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:" + "a" * 24)
    base = config.AppConfig(
        project_ids=["sales"],
        telegram={"enabled": True, "project_id": "sales", "connection_id": "new"},
    )
    network = []
    runtime = Runtime(
        base,
        tmp_path / "config.json",
        telegram_transport=httpx.MockTransport(lambda request: network.append(request)),
    )
    with closing(db.init_db(runtime.db_path)) as conn:
        old_run = received_run(runtime, conn, "old")
        old = telegram.prepare_delivery(
            conn,
            runtime.actor,
            "old",
            old_run,
            text="이전 연결 결과",
            now=datetime.now(UTC),
        )
    with manual_worker(runtime):
        assert runtime.outbox_once() is False
    with closing(db.init_db(runtime.db_path)) as conn:
        delivery = db.row_dict(
            conn.execute("SELECT * FROM outbox WHERE id=?", (old["id"],)).fetchone()
        )
        assert delivery["state"] == "pending" and delivery["attempt_count"] == 0
    assert network == []


def test_outbox_rechecks_disable_between_queue_and_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:" + "a" * 24)
    base = config.AppConfig(
        project_ids=["sales"],
        telegram={"enabled": True, "project_id": "sales"},
    )
    network = []
    runtime = Runtime(
        base,
        tmp_path / "config.json",
        telegram_transport=httpx.MockTransport(lambda request: network.append(request)),
    )
    with closing(db.init_db(runtime.db_path)) as conn:
        received_run(runtime, conn, "telegram")
        db.set_value(conn, "settings", {"telegram": base.telegram.model_dump()})
        # Inject the settings change at the actual queue commit, before delivery claim.
        conn.executescript("""
            CREATE TRIGGER disable_after_queue AFTER INSERT ON outbox BEGIN
                UPDATE kv SET value_json='{"telegram":{"enabled":false,"project_id":"sales"}}'
                WHERE key='settings';
            END;
        """)
    with manual_worker(runtime):
        assert runtime.outbox_once()
        assert runtime.outbox_once() is False
    with closing(db.init_db(runtime.db_path)) as conn:
        delivery = db.row_dict(conn.execute("SELECT * FROM outbox").fetchone())
        assert delivery["state"] == "failed" and delivery["attempt_count"] == 0
        assert delivery["error_code"] == "POLICY_DENIED"
    assert network == []


def test_later_failure_preserves_actual_completed_tool_output(tmp_path):
    runtime = make_runtime(tmp_path)
    run = create(runtime)
    plan = PlanV1.model_validate(planned()["plan"])
    first = plan.todos[0].model_copy(deep=True)
    first.id, first.kind, first.signals = "remember", "mechanical", ["fixed_transform"]
    from birkin_lite.contracts import ToolExecutor

    first.executor = ToolExecutor(tool_name="memory.search", args={"query": "인사말"})
    first.output_schema = "MemoryListV1"
    plan.todos[0].depends_on = ["remember"]
    plan.todos.insert(0, first)
    with closing(db.init_db(runtime.db_path)) as conn:
        engine.install_plan(
            conn, run["id"], plan, runtime.actor, tools=tools.registry(runtime.home)
        )
    with manual_worker(runtime):
        assert runtime.worker_once()
        with closing(db.init_db(runtime.db_path)) as conn:
            before = dict(conn.execute("SELECT * FROM steps WHERE ordinal=0").fetchone())
            assert before["state"] == "succeeded"
        assert runtime.worker_once()
        assert runtime.worker_once() is False
    finished = read(runtime, run["id"])
    assert finished["state"] == "failed" and finished["error_code"] == "NO_CAPABLE_MODEL"
    assert finished["result_json"]["outputs"] == {"remember": {"memories": []}}
    with closing(db.init_db(runtime.db_path)) as conn:
        assert dict(conn.execute("SELECT * FROM steps WHERE ordinal=0").fetchone()) == before


def test_blocked_long_poll_does_not_hold_workflow_or_delivery_lane(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:" + "a" * 24)
    base = config.AppConfig(
        project_ids=["sales"],
        telegram={"enabled": True, "project_id": "sales"},
    )
    polling, release, sent = threading.Event(), threading.Event(), threading.Event()
    observed = []

    def handle(request):
        method = request.url.path.rsplit("/", 1)[-1]
        observed.append((method, threading.get_ident()))
        if method == "getWebhookInfo":
            result = {"url": ""}
        elif method == "getUpdates":
            polling.set()
            assert release.wait(10)
            result = []
        else:
            assert method == "sendMessage"
            sent.set()
            result = {"message_id": 901, "chat": {"id": 101}}
        return httpx.Response(200, json={"ok": True, "result": result})

    runtime = Runtime(
        base,
        tmp_path / "config.json",
        telegram_transport=httpx.MockTransport(handle),
    )
    run = create(runtime)
    with closing(db.init_db(runtime.db_path)) as conn:
        received_run(runtime, conn, "telegram")

    async def scenario():
        await runtime.start()
        try:
            await wait_for(lambda: polling.is_set() and sent.is_set())
            await wait_for(lambda: read(runtime, run["id"])["state"] == "failed")
            stopping = asyncio.create_task(runtime.stop())
            await asyncio.sleep(0.05)
            assert not stopping.done()
            release.set()
            await stopping
        finally:
            release.set()
            await runtime.stop()

    asyncio.run(scenario())
    poll_thread = next(thread for method, thread in observed if method == "getUpdates")
    delivery_thread = next(thread for method, thread in observed if method == "sendMessage")
    assert poll_thread != delivery_thread
    assert sum(method == "sendMessage" for method, _ in observed) == 1
    with closing(db.init_db(runtime.db_path)) as conn:
        assert conn.execute("SELECT state FROM outbox").fetchone()[0] == "sent"


@pytest.mark.parametrize("stage", ["prepare", "tool", "model"])
def test_automation_revocation_is_checked_before_each_execution_boundary(
    tmp_path, monkeypatch, stage
):
    base = qualified_config(tmp_path, monkeypatch)
    seen = []
    runtime = Runtime(
        base,
        tmp_path / "config.json",
        model_transport=httpx.MockTransport(model_http(seen)),
    )
    with closing(db.init_db(runtime.db_path)) as conn:
        bot = db.create_automation(
            conn,
            runtime.actor,
            {
                "name": "권한 검사",
                "project_id": "sales",
                "recipe": "summarize",
                "instruction": TEXT,
                "trigger": {"kind": "once", "at": "2030-01-01T00:00:00Z"},
            },
        )
        bot = db.set_automation_enabled(
            conn,
            bot["id"],
            bot["revision"],
            True,
            runtime.actor,
            confirmed_hash=db.automation_proposal_hash(bot),
        )
        run = db.create_run(
            conn,
            runtime.actor,
            "automation",
            "event",
            RunRequest(request=TEXT, project_id="sales"),
            automation_id=bot["id"],
            automation_revision=bot["revision"],
        )
        if stage == "prepare":
            conn.execute("UPDATE automations SET enabled=0")
        else:
            plan = PlanV1.model_validate(planned()["plan"])
            if stage == "tool":
                from birkin_lite.contracts import ToolExecutor

                todo = plan.todos[0]
                todo.kind, todo.signals = "mechanical", ["fixed_transform"]
                todo.executor = ToolExecutor(tool_name="memory.search", args={"query": "인사말"})
                todo.output_schema = "MemoryListV1"
            engine.install_plan(
                conn, run["id"], plan, runtime.actor, tools=tools.registry(runtime.home)
            )
            conn.executescript("""
                CREATE TRIGGER pause_at_claim AFTER UPDATE OF state ON steps
                WHEN NEW.state='running' BEGIN
                    UPDATE automations SET enabled=0;
                END;
            """)
    with manual_worker(runtime):
        assert runtime.worker_once()
        assert runtime.worker_once() is False
    failed = read(runtime, run["id"])
    assert failed["state"] == "failed" and failed["error_code"] == "POLICY_DENIED"
    assert [method for method, *_ in seen] == ["GET"]
    with closing(db.init_db(runtime.db_path)) as conn:
        assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM steps WHERE output_json IS NOT NULL").fetchone()[0]
            == 0
        )


def test_qualification_bytes_are_rechecked_before_routing_without_another_discovery(
    tmp_path, monkeypatch
):
    base = qualified_config(tmp_path, monkeypatch)
    seen = []
    runtime = Runtime(
        base,
        tmp_path / "config.json",
        model_transport=httpx.MockTransport(model_http(seen)),
    )
    run = create(runtime)
    with manual_worker(runtime):
        assert runtime.catalog_snapshot()["statuses"][0]["state"] == "ready"
        (tmp_path / "evaluations" / "fixture.json").write_text("{}", encoding="utf-8")
        assert runtime.worker_once()
        assert runtime.catalog_snapshot()["statuses"][0]["state"] == "unqualified"
    assert read(runtime, run["id"])["error_code"] == "NO_CAPABLE_MODEL"
    assert [method for method, *_ in seen] == ["GET"]


def test_client_close_failure_does_not_leave_exclusive_lock_stuck(tmp_path, monkeypatch):
    base = qualified_config(tmp_path, monkeypatch)

    class FailingClose(httpx.MockTransport):
        def close(self):
            super().close()
            raise OSError("private transport detail must not escape")

    runtime = Runtime(
        base,
        tmp_path / "config.json",
        model_transport=FailingClose(model_http([])),
    )

    async def scenario():
        await runtime.start()
        await runtime.stop()

    asyncio.run(scenario())
    assert runtime.last_errors["shutdown"] == "CLIENT_CLOSE_ERROR"
    assert runtime._executors == {}
    with engine.DaemonLock(runtime.home / "daemon.lock"):
        pass
