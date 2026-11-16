"""One local workflow worker, with independent scheduler and Telegram I/O lanes."""

import asyncio
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from . import automation, config, db, engine, planning, providers, retention, telegram, tools
from .contracts import AppError, digest
from .safe_io import ensure_directory


def _clock():
    return datetime.now(UTC)


class Runtime:
    def __init__(self, base_config, config_path, *, model_transport=None, telegram_transport=None):
        self.base_config = config.AppConfig.model_validate(base_config.model_dump())
        self.config_path = Path(config_path).absolute()
        self.home = config.home_path(self.base_config, self.config_path)
        self.db_path = self.home / "state.db"
        self.actor = self.base_config.actor()
        self._model_transport = model_transport
        self._telegram_transport = telegram_transport
        self._lock = engine.DaemonLock(self.home / "daemon.lock")
        self._stopping = threading.Event()
        self._work_guard = threading.Lock()
        self._snapshot_guard = threading.Lock()
        self._catalog = config.resolve_catalog(
            self.base_config, self.config_path, api_key_present=False
        )
        self._services = {}
        self._executors = {}
        self._tasks = []
        self._shutdown_task = None
        self._wake = None
        self._daemon_session_id = None
        self.last_errors = {}

    def catalog_snapshot(self):
        """A detached, non-secret observation; reading this never calls a provider."""
        with self._snapshot_guard:
            return self._catalog.model_dump()

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *_):
        await self.stop()

    async def start(self):
        if self._executors:
            raise AppError("ALREADY_RUNNING", status=409)
        ensure_directory(self.home)
        self._lock.__enter__()
        self._stopping.clear()
        self._shutdown_task = None
        self._wake = asyncio.Event()
        self._executors = {
            lane: ThreadPoolExecutor(max_workers=1, thread_name_prefix="blite-" + lane)
            for lane in ("work", "scheduler", "poll", "outbox")
        }
        try:
            await self._submit("work", self._bootstrap)
            self._tasks = [
                asyncio.create_task(self._loop("work", self.worker_once, 0.2)),
                asyncio.create_task(self._loop("scheduler", self.scheduler_once, 15)),
                asyncio.create_task(self._loop("poll", self.poll_once, 1)),
                asyncio.create_task(self._loop("outbox", self.outbox_once, 1)),
            ]
        except BaseException:
            await self.stop()
            raise
        return self

    async def _submit(self, lane, function):
        # Shield the actual executor future. Cancelling an asyncio waiter cannot stop its thread.
        future = asyncio.get_running_loop().run_in_executor(self._executors[lane], function)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await future
            raise

    async def _loop(self, lane, function, interval):
        while not self._stopping.is_set():
            worked = False
            try:
                worked = await self._submit(lane, function)
                self.last_errors.pop(lane, None)
            except (AppError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
                # Public diagnostics must not expose provider exception text or credentials.
                self.last_errors[lane] = exc.code if isinstance(exc, AppError) else "RUNTIME_ERROR"
            if self._stopping.is_set():
                break
            delay = 0 if worked and lane in {"work", "outbox"} else interval
            if delay:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except TimeoutError:
                    pass

    async def stop(self):
        if not self._executors:
            return
        if self._shutdown_task is None:
            self._stopping.set()
            self._wake.set()
            self._shutdown_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self):
        # No task cancellation: bounded HTTP/parser calls first finish and settle their DB rows.
        await asyncio.gather(*self._tasks, return_exceptions=True)
        closed = await asyncio.gather(
            *(
                self._submit(lane, lambda lane=lane: self._close_service(lane))
                for lane in self._executors
            ),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in closed):
            self.last_errors["shutdown"] = "CLIENT_CLOSE_ERROR"
        if self._daemon_session_id is not None:
            try:
                await self._submit("scheduler", self._record_shutdown)
            except (AppError, OSError, ValueError, TypeError, sqlite3.Error):
                self.last_errors["shutdown"] = "OBSERVATION_WRITE_ERROR"
        for executor in self._executors.values():
            executor.shutdown(wait=True)
        self._executors.clear()
        self._tasks.clear()
        self._lock.__exit__(None, None, None)

    def _bootstrap(self):
        with closing(db.init_db(self.db_path)) as conn:
            engine.resume_after_restart(conn, previous_process_is_dead=True)
            self._daemon_session_id = uuid.uuid4().hex
            automation.record_daemon_observation(
                conn, self.actor, self._daemon_session_id, "start", _clock()
            )
            self._work_service(conn, force=True)

    def _record_shutdown(self):
        with closing(db.init_db(self.db_path)) as conn:
            automation.record_daemon_observation(
                conn, self.actor, self._daemon_session_id, "stop", _clock()
            )

    async def refresh(self):
        """Explicit settings changes may perform read-only account model discovery."""
        if not self._executors or self._stopping.is_set():
            raise AppError("RUNTIME_STOPPED", status=409)

        def refresh_catalog():
            with self._work_guard, closing(db.init_db(self.db_path)) as conn:
                self._work_service(conn, force=True)
            return self.catalog_snapshot()

        return await self._submit("work", refresh_catalog)

    def _close_service(self, lane):
        service = self._services.pop(lane, None)
        if service is not None and service[1] is not None:
            service[1].close()

    def _work_service(self, conn, *, force=False):
        current = config.runtime_config(self.base_config, conn)
        key = config.read_xai_key(self.home)
        signature = (digest(current), key)
        saved = self._services.get("work")
        if force or saved is None or saved[0] != signature:
            self._close_service("work")
            client = (
                providers.XAIClient(key.get_secret_value(), http_transport=self._model_transport)
                if key is not None and any(p.binding.enabled for p in current.models)
                else None
            )
            self._services["work"] = (signature, client)
            catalog = config.resolve_catalog(
                current,
                self.config_path,
                api_key_present=key is not None,
                lookup_models=client.list_models if client is not None else None,
            )
            with self._snapshot_guard:
                self._catalog = catalog
        else:
            # Re-open the pinned local qualification bytes before routing. Account availability
            # is the last real discovery observation, refreshed by explicit settings/check-models.
            available = [b.model_id for b in self._catalog.bindings if b.available]
            catalog = config.resolve_catalog(
                current,
                self.config_path,
                api_key_present=key is not None,
                lookup_models=(lambda: available) if self._catalog.availability_checked else None,
            )
            with self._snapshot_guard:
                self._catalog = catalog
        return current, self._services["work"][1], self._catalog.bindings

    def _telegram_service(self, conn, lane):
        current = config.runtime_config(self.base_config, conn)
        token = config.read_telegram_token(self.home) if current.telegram.enabled else None
        signature = (current.telegram.model_dump(), token)
        saved = self._services.get(lane)
        if saved is None or saved[0] != signature:
            self._close_service(lane)
            client = (
                telegram.TelegramClient(token, http_transport=self._telegram_transport)
                if token is not None
                else None
            )
            self._services[lane] = (signature, client)
        client = self._services[lane][1]
        if client is not None:
            telegram.register_connection(
                conn,
                self.actor,
                current.telegram.connection_id,
                current.telegram.project_id,
                now=_clock(),
                client=client,
            )
        return current, client

    def _guard(self, conn, run_id, current):
        run = db.require_automation_run(conn, run_id, self.actor)
        if run["state"] == "cancel_requested":
            raise AppError("CANCELLED")
        if digest(config.runtime_config(self.base_config, conn)) != digest(current):
            raise AppError("POLICY_DENIED")
        saved = self._services.get("work")
        if saved is not None and saved[0][1] != config.read_xai_key(self.home):
            raise AppError("POLICY_DENIED")
        return run

    def _guarded_tools(self, conn, run_id, current):
        def wrap(tool):
            def execute(args, context):
                self._guard(conn, run_id, current)
                return tool.execute(args, context)

            return engine.Tool(tool.effect, execute, tool.args_schema)

        return {name: wrap(tool) for name, tool in tools.registry(self.home).items()}

    def _next_run(self, conn):
        for row in conn.execute(
            "SELECT * FROM runs WHERE actor_id=? AND state IN "
            "('queued','running','checking','ready','awaiting_input','awaiting_approval',"
            "'cancel_requested') ORDER BY created_at,id",
            (self.actor.id,),
        ):
            run = db.row_dict(row)
            if run["project_id"] not in self.actor.project_ids:
                continue
            if run["state"] == "awaiting_input" and db.get_value(
                conn, "prepare_pending:" + run["id"]
            ) != {"revision": run["revision"]}:
                continue
            if (
                run["state"] == "awaiting_approval"
                and not conn.execute(
                    "SELECT 1 FROM approvals WHERE run_id=? AND "
                    "(state='approved' OR (state='pending' AND expires_at<=?))",
                    (run["id"], _clock().isoformat()),
                ).fetchone()
            ):
                continue
            return run
        return None

    def _prepare_error(self, conn, run, code):
        with db.transaction(conn):
            fresh = db.get_run(conn, run["id"], self.actor)
            # A cancellation or newer input wins over an old planning response/failure.
            if fresh["revision"] != run["revision"]:
                return
            target = (
                "awaiting_input"
                if code in {"MISSING_INPUT", "CONTEXT_LIMIT", "FORMULA_UNVERIFIED"}
                else "failed"
            )
            if fresh["state"] == "queued" and target == "awaiting_input":
                fresh = engine.transition_run(conn, run["id"], "queued", "preparing")
            if fresh["state"] != target:
                engine.transition_run(conn, run["id"], fresh["state"], target)
            conn.execute("UPDATE runs SET error_code=? WHERE id=?", (code, run["id"]))
        engine._finish(conn, run["id"], self.actor)

    def worker_once(self):
        """Prepare or execute at most one step; SQLite belongs to this calling thread."""
        with self._work_guard:
            if self._stopping.is_set() or self._lock.handle is None:
                return False
            with closing(db.init_db(self.db_path)) as conn:
                run = self._next_run(conn)
                if run is None:
                    return False
                if self._stopping.is_set():
                    return False
                if run["state"] == "cancel_requested":
                    engine.execute_next(
                        conn,
                        run["id"],
                        actor=self.actor,
                        tools=tools.registry(self.home),
                        complete=None,
                        now=_clock(),
                        home=self.home,
                    )
                    return True
                marker_key = "prepare_pending:" + run["id"]
                marker = db.get_value(conn, marker_key)
                handled = False
                try:
                    current, client, catalog = self._work_service(conn)
                    if self._stopping.is_set():
                        return False
                    registered = self._guarded_tools(conn, run["id"], current)
                    policy = current.privacy_for(run["project_id"])

                    def transport(request, binding):
                        self._guard(conn, run["id"], current)
                        if client is None:
                            raise AppError("NO_CAPABLE_MODEL")
                        return client.transport(request, binding)

                    def price(binding):
                        return next(
                            (
                                p.price_snapshot
                                for p in current.models
                                if (p.binding.provider, p.binding.model_id)
                                == (binding.provider, binding.model_id)
                            ),
                            None,
                        )

                    def complete_plan(request, binding, context):
                        self._guard(conn, run["id"], current)
                        return providers.metered_complete(
                            request,
                            binding,
                            context,
                            transport=transport,
                            price_snapshot=price(binding),
                        )

                    def complete(todo, inputs, context):
                        self._guard(conn, run["id"], current)
                        return engine.complete_model_todo(
                            todo,
                            inputs,
                            context,
                            capability_catalog=catalog,
                            privacy_policy=policy,
                            transport=transport,
                            price_snapshot=price,
                        )

                    if run["state"] in {"queued", "awaiting_input"}:
                        self._guard(conn, run["id"], current)
                        planning.prepare_run(
                            conn,
                            self.home,
                            run["id"],
                            self.actor,
                            tools=registered,
                            capability_catalog=catalog,
                            privacy_policy=policy,
                            complete=complete_plan,
                        )
                        handled = True
                    else:
                        self._guard(conn, run["id"], current)
                        engine.execute_next(
                            conn,
                            run["id"],
                            actor=self.actor,
                            tools=registered,
                            complete=complete,
                            now=_clock(),
                            check_sources=tools.check_sources,
                            home=self.home,
                        )
                except (AppError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
                    code = exc.code if isinstance(exc, AppError) else "RUNTIME_ERROR"
                    self._prepare_error(conn, run, code)
                    handled = True
                finally:
                    if handled and marker is not None:
                        with db.transaction(conn):
                            if db.get_value(conn, marker_key) == marker:
                                conn.execute("DELETE FROM kv WHERE key=?", (marker_key,))
                return True

    def scheduler_once(self):
        if self._stopping.is_set() or self._lock.handle is None:
            return False
        with closing(db.init_db(self.db_path)) as conn:
            automation.tick_automations(
                conn, self.home, self.actor, _clock, daemon_session_id=self._daemon_session_id
            )
            last = db.get_value(conn, "retention:last", {}).get("at")
            if last is None or (_clock() - datetime.fromisoformat(last)).total_seconds() >= 86400:
                with self._work_guard:
                    if not self._stopping.is_set():
                        retention.expire_completed(conn, self.home, self.actor, now=_clock())
        return False

    def poll_once(self):
        if self._stopping.is_set() or self._lock.handle is None:
            return False
        with closing(db.init_db(self.db_path)) as conn:
            current, client = self._telegram_service(conn, "poll")
            if client is None:
                return False

            def clock():
                latest = config.runtime_config(self.base_config, conn)
                if (
                    latest.telegram != current.telegram
                    or config.read_telegram_token(self.home) != self._services["poll"][0][1]
                ):
                    raise AppError("POLICY_DENIED")
                return _clock()

            telegram.poll_once(client, conn, self.actor, current.telegram.connection_id, now=clock)
        return True

    def _queue_results(self, conn, connection_id):
        rows = conn.execute(
            "SELECT * FROM runs WHERE actor_id=? AND source IN ('telegram','automation') "
            "AND state IN ('succeeded','awaiting_approval','awaiting_input','failed','expired',"
            "'rejected') AND COALESCE(json_extract(result_json,'$.archived'),0)=0 ORDER BY created_at,id",
            (self.actor.id,),
        ).fetchall()
        for row in rows:
            run = db.row_dict(row)
            try:
                markup = None
                if run["state"] == "awaiting_approval":
                    approval = db.row_dict(
                        conn.execute(
                            "SELECT * FROM approvals WHERE run_id=? AND state='pending' "
                            "AND expires_at>? ORDER BY created_at LIMIT 1",
                            (run["id"], _clock().isoformat()),
                        ).fetchone()
                    )
                    if approval is None:
                        continue
                    key = "telegram:approval_notice:" + approval["id"]
                    markup = db.get_value(conn, key)
                    if markup is None:
                        markup = {
                            "inline_keyboard": [
                                [
                                    {
                                        "text": text,
                                        "callback_data": telegram.create_callback(
                                            conn,
                                            self.actor,
                                            connection_id,
                                            kind="approval",
                                            target_id=approval["id"],
                                            decision=decision,
                                            now=_clock(),
                                        ),
                                    }
                                    for text, decision in (("승인", "approve"), ("거절", "reject"))
                                ]
                            ]
                        }
                        db.set_value(conn, key, markup)
                status = {
                    "succeeded": "업무를 완료했습니다.",
                    "awaiting_approval": "업무 실행 승인이 필요합니다.",
                    "awaiting_input": "업무에 필요한 입력을 화면에서 보완해 주세요.",
                    "failed": "업무를 완료하지 못했습니다. 화면에서 상태를 확인해 주세요.",
                    "expired": "업무 승인이 만료되었습니다.",
                    "rejected": "업무 승인이 거절되었습니다.",
                }[run["state"]]
                telegram.prepare_delivery(
                    conn,
                    self.actor,
                    connection_id,
                    run["id"],
                    text=status,
                    reply_markup=markup,
                    now=_clock(),
                )
                if run["state"] == "succeeded":
                    for output in (run["result_json"] or {}).get("outputs", {}).values():
                        receipt = output.get("receipt") if isinstance(output, dict) else None
                        if isinstance(receipt, dict) and receipt.get("run_id") == run["id"]:
                            telegram.prepare_delivery(
                                conn,
                                self.actor,
                                connection_id,
                                run["id"],
                                artifact_id=receipt["artifact_id"],
                                now=_clock(),
                            )
            except AppError:
                # Pausing, re-pairing or revising authority must suppress an old delivery.
                continue

    def outbox_once(self):
        if self._stopping.is_set() or self._lock.handle is None:
            return False
        with closing(db.init_db(self.db_path)) as conn:
            current, client = self._telegram_service(conn, "outbox")
            if client is None:
                return False
            self._queue_results(conn, current.telegram.connection_id)
            if self._stopping.is_set():
                return False

            def before_send():
                latest = config.runtime_config(self.base_config, conn)
                if (
                    self._stopping.is_set()
                    or latest.telegram != current.telegram
                    or config.read_telegram_token(self.home) != self._services["outbox"][0][1]
                ):
                    raise AppError("POLICY_DENIED")

            return (
                telegram.deliver_once(
                    client,
                    conn,
                    self.home,
                    self.actor,
                    now=_clock(),
                    connection_id=current.telegram.connection_id,
                    before_send=before_send,
                )
                is not None
            )
