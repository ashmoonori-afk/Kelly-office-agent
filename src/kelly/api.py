"""Authenticated loopback HTTP; wire existing operations without a second workflow engine."""

import asyncio
import hashlib
import json
import os
import secrets
import tempfile
import time
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import Field, SecretStr, ValidationError, model_validator
from starlette.exceptions import HTTPException

from . import (
    artifacts,
    automation,
    config,
    db,
    engine,
    memory,
    providers,
    retention,
    telegram,
    usage,
)
from .contracts import AppError, Id, PrivacyPolicy, RunRequest, Sha256, StrictModel, TaskSpec
from .runtime import Runtime

Revision = Annotated[int, Field(ge=1)]
JSON_LIMIT = 65536
COOKIE = "kelly_session"


class Empty(StrictModel):
    pass


class Bootstrap(StrictModel):
    code: Annotated[str, Field(min_length=1, max_length=256)]


class InputPatch(StrictModel):
    expected_revision: Revision
    task_spec: TaskSpec


class PinInput(StrictModel):
    pinned: bool


class AutomationPatch(StrictModel):
    expected_revision: Revision
    config: automation.AutomationConfig | None = None
    enabled: bool | None = None
    confirmed_hash: Sha256 | None = None

    @model_validator(mode="after")
    def one_action(self):
        if self.config is not None:
            if "enabled" in self.model_fields_set or "confirmed_hash" in self.model_fields_set:
                raise ValueError("One action required")
        elif self.enabled is None:
            raise ValueError("One action required")
        return self


class FolderInput(StrictModel):
    project_id: Id
    path: Annotated[str, Field(min_length=1, max_length=4096)]
    explicit_confirmed: Literal[True]


class ApprovalInput(StrictModel):
    decision: Literal["approve", "reject"]
    proposal_hash: Sha256
    expected_revision: Revision


class SettingsPatch(StrictModel):
    expected_revision: Revision
    models: list[config.ModelProfile] | None = Field(default=None, max_length=20)
    privacy: list[PrivacyPolicy] | None = Field(default=None, max_length=100)
    telegram: config.TelegramConfig | None = None


class SecretInput(StrictModel):
    provider: Literal["xai", "telegram", "commandcode"]
    value: SecretStr


class MemoryInput(StrictModel):
    project_id: Id
    args: memory.MemorySaveArgs


class MemoryPatch(MemoryInput):
    expected_revision: Revision


class MemoryDelete(StrictModel):
    project_id: Id
    expected_revision: Revision
    purge_original: bool = False


def _error(error):
    return JSONResponse(
        {
            "error": {
                "code": error.code,
                "message_ko": error.message_ko,
                "recoverable": error.status < 500,
                "run_id": None,
            }
        },
        status_code=error.status,
    )


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("Invalid constant")


def _length(request, limit):
    value = request.headers.get("content-length")
    if value is not None:
        if not value.isascii() or not value.isdecimal():
            raise AppError("INVALID_INPUT")
        if len(value) > 10 or int(value) > limit:
            raise AppError("LIMIT_EXCEEDED", status=413)


async def _body(request, schema=Empty):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise AppError("INVALID_INPUT")
    _length(request, JSON_LIMIT)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > JSON_LIMIT:
            raise AppError("LIMIT_EXCEEDED", status=413)
        data.extend(chunk)
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_invalid_constant
        )
        return schema.model_validate(value)
    except (ValueError, UnicodeError, RecursionError):
        raise AppError("INVALID_INPUT") from None


def _public_artifact(row, conn=None):
    pin = db.get_value(conn, "artifact_pin:" + row["id"], {}) if conn is not None else {}
    return {
        **{key: value for key, value in row.items() if key != "relative_path"},
        "pinned": pin.get("pinned") is True and pin.get("sha256") == row["sha256"],
    }


def _public_delivery(row):
    return {
        key: row[key]
        for key in (
            "id",
            "run_id",
            "delivery_kind",
            "state",
            "attempt_count",
            "next_attempt_at",
            "remote_id",
            "created_at",
            "sent_at",
            "error_code",
        )
    }


def _public_call(row):
    return {
        **{
            key: row[key]
            for key in (
                "id",
                "step_id",
                "logical_call_no",
                "transport_attempt_no",
                "provider",
                "model_id",
                "purpose",
                "state",
                "input_tokens_total",
                "cached_input_tokens",
                "output_tokens_total",
                "reasoning_tokens",
                "usage_basis",
                "actual_micro_usd",
            )
        },
        "role": row["route_json"].get("role"),
        "reasoning_effort": row["route_json"].get("reasoning_effort"),
        "price_stale": row["price_snapshot_json"].get("stale", True),
    }


def _public_automation_observation(conn, run):
    if run["automation_id"] is None:
        return None
    event = conn.execute(
        "SELECT event_key FROM inbox_events WHERE source='automation' "
        "AND run_id=? AND actor_id=? AND connection_id=?",
        (run["id"], run["actor_id"], run["automation_id"]),
    ).fetchone()
    if event is None:
        return None
    value = db.get_value(
        conn, "automation_observation:" + run["automation_id"] + ":" + event["event_key"]
    )
    if not isinstance(value, dict) or any(
        value.get(key) != expected
        for key, expected in {
            "schema_version": 1,
            "actor_id": run["actor_id"],
            "project_id": run["project_id"],
            "automation_id": run["automation_id"],
            "event_key": event["event_key"],
        }.items()
    ):
        return None
    return {
        key: value.get(key)
        for key in (
            "schema_version",
            "scheduled_utc",
            "event_received_at",
            "started_at",
            "previous_tick_at",
            "tick_at",
            "observed_at",
            "clock_rollback",
            "scheduled_daemon_state",
            "pc_power_state",
        )
    }


def _bot(row):
    return {"automation": row, "proposal_hash": db.automation_proposal_hash(row)}


def create_app(
    config_path,
    *,
    start_background=True,
    bootstrap_secret=None,
    model_transport=None,
    telegram_transport=None,
    stop_callback=None,
):
    config_path = Path(config_path).absolute()
    base = config.load_config(config_path)
    home = config.home_path(base, config_path)
    actor = base.actor()
    origin = f"http://127.0.0.1:{base.port}"
    host = f"127.0.0.1:{base.port}"
    sessions = {}
    upload_lock = asyncio.Lock()
    settings_lock = asyncio.Lock()
    bootstrap_lock = asyncio.Lock()
    runtime = (
        Runtime(
            base,
            config_path,
            model_transport=model_transport,
            telegram_transport=telegram_transport,
        )
        if start_background
        else None
    )
    offline_lock = engine.DaemonLock(home / "daemon.lock")

    def in_db(operation):
        with closing(db.init_db(home / "state.db")) as conn:
            return operation(conn)

    async def call(operation):
        future = asyncio.create_task(asyncio.to_thread(in_db, operation))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # A disconnect cannot close an upload handle while its importer is still running.
            await future
            raise

    async def new_code(value=None):
        code = value or secrets.token_urlsafe(32)
        await asyncio.to_thread(config.store_secret, home, "bootstrap", SecretStr(code))
        app.state.bootstrap_secret = code
        app.state.bootstrap_expires = time.monotonic() + 600
        app.state.bootstrap_attempts = 0

    @asynccontextmanager
    async def lifespan(app):
        started = False
        try:
            if runtime is not None:
                await runtime.start()
            else:
                offline_lock.__enter__()
            started = True
            await call(lambda conn: None)
            await new_code(bootstrap_secret)
            yield
        finally:
            sessions.clear()
            if started:
                if runtime is not None:
                    await runtime.stop()
                else:
                    offline_lock.__exit__(None, None, None)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.state.bootstrap_secret = bootstrap_secret or secrets.token_urlsafe(32)
    app.state.catalog = None

    @app.middleware("http")
    async def boundary(request, next_call):
        try:
            if request.headers.get("host") != host or request.headers.get("sec-fetch-site") not in {
                None,
                "none",
                "same-origin",
            }:
                raise AppError("POLICY_DENIED", status=403)
            mutation = request.method not in {"GET", "HEAD"}
            if mutation and request.headers.get("origin") != origin:
                raise AppError("POLICY_DENIED", status=403)
            if request.method == "OPTIONS":
                raise AppError("POLICY_DENIED", status=403)
            public = request.url.path in {
                "/",
                "/api/health",
                "/api/bootstrap",
                "/static/app.js",
                "/static/app.css",
                "/static/kelly-companion.png",
                "/static/favicon.svg",
            }
            if not public:
                cookie = request.cookies.get(COOKIE) or request.cookies.get("blite_session", "")
                session = sessions.get(hashlib.sha256(cookie.encode()).hexdigest())
                if session is None or session["expires"] <= time.monotonic():
                    raise AppError("AUTH_REQUIRED", status=401)
                if mutation and not secrets.compare_digest(
                    request.headers.get("x-csrf-token", ""), session["csrf_token"]
                ):
                    raise AppError("POLICY_DENIED", status=403)
                request.state.session = session
            response = await next_call(request)
        except AppError as error:
            response = _error(error)
        except (ValidationError, ValueError, UnicodeError):
            response = _error(AppError("INVALID_INPUT"))
        except Exception:  # noqa: BLE001 -- last HTTP boundary must never return raw exceptions.
            response = _error(AppError("INTERNAL_ERROR", status=500))
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'",
            }
        )
        return response

    @app.exception_handler(AppError)
    async def app_error(_, error):
        return _error(error)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, error):
        return _error(AppError("INVALID_INPUT"))

    @app.exception_handler(HTTPException)
    async def http_error(_, error):
        return _error(AppError("NOT_FOUND", status=404))

    @app.get("/")
    @app.get("/static/{filename}")
    async def static(filename="index.html"):
        media = {
            "index.html": "text/html",
            "app.js": "text/javascript",
            "app.css": "text/css",
            "kelly-companion.png": "image/png",
            "favicon.svg": "image/svg+xml",
        }
        if filename not in media:
            raise AppError("NOT_FOUND", status=404)
        data = await asyncio.to_thread((Path(__file__).parent / "static" / filename).read_bytes)
        return Response(data, media_type=media[filename])

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.post("/api/server/stop", status_code=202)
    async def stop_server(request: Request):
        await _body(request)
        if stop_callback is None:
            raise AppError("UNSUPPORTED_CAPABILITY", status=409)
        stop_callback()
        return {"state": "stopping"}

    @app.post("/api/bootstrap")
    async def bootstrap(request: Request):
        body = await _body(request, Bootstrap)
        async with bootstrap_lock:
            saved = await asyncio.to_thread(config.stored_secret, home, "bootstrap")
            # `blite access` may explicitly rotate the code under the same Windows account.
            if saved is not None and saved.get_secret_value() != app.state.bootstrap_secret:
                app.state.bootstrap_secret = saved.get_secret_value()
                app.state.bootstrap_expires = time.monotonic() + 600
                app.state.bootstrap_attempts = 0
            if (
                app.state.bootstrap_attempts >= 5
                or app.state.bootstrap_expires <= time.monotonic()
                or not secrets.compare_digest(body.code, app.state.bootstrap_secret)
            ):
                app.state.bootstrap_attempts += 1
                raise AppError("AUTH_REQUIRED", status=401)
            await new_code()
            now = time.monotonic()
            for key in list(sessions):
                if sessions[key]["expires"] <= now:
                    del sessions[key]
            while len(sessions) >= 10:
                del sessions[next(iter(sessions))]
            cookie, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            sessions[hashlib.sha256(cookie.encode()).hexdigest()] = {
                "csrf_token": csrf,
                "expires": now + 43200,
            }
        response = JSONResponse({"csrf_token": csrf})
        response.set_cookie(
            COOKIE, cookie, httponly=True, samesite="strict", max_age=43200, path="/"
        )
        return response

    @app.get("/api/session")
    async def session(request: Request):
        return {"csrf_token": request.state.session["csrf_token"], "actor": actor.model_dump()}

    def project(request):
        value = request.query_params.get("project_id", actor.project_ids[0])
        db.require_project(actor, value)
        return value

    @app.post("/api/artifacts", status_code=201)
    async def upload(request: Request):
        project_id = project(request)
        metadata = {
            "filename": request.query_params.get("filename"),
            "media_type": request.headers.get("content-type", "").split(";", 1)[0],
        }
        artifacts._filename(metadata)
        _length(request, artifacts.MAX_BYTES)
        async with upload_lock:
            # ponytail: one bounded upload in flight; stream to managed temp only if 25 MiB is raised.
            with tempfile.SpooledTemporaryFile(max_size=artifacts.MAX_BYTES) as stream:
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > artifacts.MAX_BYTES:
                        raise AppError("LIMIT_EXCEEDED", status=413)
                    stream.write(chunk)
                stream.seek(0)
                row = await call(
                    lambda conn: artifacts.import_source(
                        home, conn, stream, metadata, actor, project_id
                    )
                )
        return {"artifact": _public_artifact(row)}

    @app.get("/api/artifacts")
    async def list_artifacts(request: Request):
        project_id = project(request)
        return {
            "artifacts": await call(
                lambda conn: [
                    _public_artifact(db.row_dict(row))
                    for row in conn.execute(
                        "SELECT * FROM artifacts WHERE actor_id=? AND project_id=? AND kind='source' "
                        "AND deleted_at IS NULL ORDER BY created_at DESC,id DESC LIMIT 100",
                        (actor.id, project_id),
                    )
                ]
            )
        }

    @app.get("/api/artifacts/{artifact_id}/download")
    async def download(artifact_id: str):
        def read(conn):
            row = db.row_dict(
                conn.execute(
                    "SELECT * FROM artifacts WHERE id=? AND actor_id=?", (artifact_id, actor.id)
                ).fetchone()
            )
            if row is None:
                raise AppError("NOT_FOUND", status=404)
            record = artifacts.metadata(conn, artifact_id, actor, row["project_id"])
            with artifacts.open_artifact(
                home, conn, artifact_id, actor, row["project_id"]
            ) as stream:
                data = stream.read(artifacts.MAX_BYTES + 1)
            name = record["provenance_json"].get("filename", Path(record["relative_path"]).name)
            return data, record["media_type"], name

        data, media, name = await call(read)
        return Response(
            data,
            media_type=media,
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe="")},
        )

    @app.post("/api/runs", status_code=202)
    async def create_run(request: Request):
        body = await _body(request, RunRequest)
        key = request.headers.get("idempotency-key")
        if not key or len(key) > 256 or not key.isascii() or any(ord(c) < 33 for c in key):
            raise AppError("INVALID_INPUT")
        return {"run": await call(lambda conn: db.create_run(conn, actor, "ui", key, body))}

    @app.post("/api/artifacts/{artifact_id}/pin")
    async def pin(artifact_id: str, request: Request):
        body = await _body(request, PinInput)

        def change(conn):
            row = conn.execute(
                "SELECT project_id FROM artifacts WHERE id=? AND actor_id=?",
                (artifact_id, actor.id),
            ).fetchone()
            if row is None:
                raise AppError("NOT_FOUND", status=404)
            return retention.pin_artifact(conn, artifact_id, actor, row[0], body.pinned)

        return await call(change)

    @app.get("/api/runs")
    async def list_runs(request: Request):
        project_id = project(request)
        return {
            "runs": await call(
                lambda conn: [
                    db.row_dict(row)
                    for row in conn.execute(
                        "SELECT * FROM runs WHERE actor_id=? AND project_id=? ORDER BY created_at DESC,id DESC LIMIT 100",
                        (actor.id, project_id),
                    )
                ]
            )
        }

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str):
        def read(conn):
            run = db.get_run(conn, run_id, actor)
            spec = db.get_task_spec(conn, run_id, actor)
            return {
                "run": run,
                "automation_observation": _public_automation_observation(conn, run),
                "task_spec": spec.model_dump() if spec else None,
                "steps": [
                    db.row_dict(row)
                    for row in conn.execute(
                        "SELECT * FROM steps WHERE run_id=? ORDER BY ordinal", (run_id,)
                    )
                ],
                "artifacts": [
                    _public_artifact(db.row_dict(row), conn)
                    for row in conn.execute(
                        "SELECT * FROM artifacts WHERE run_id=? AND actor_id=? AND project_id=? AND deleted_at IS NULL",
                        (run_id, actor.id, run["project_id"]),
                    )
                ],
                "approvals": [
                    db.row_dict(row)
                    for row in conn.execute(
                        "SELECT * FROM approvals WHERE run_id=? AND actor_id=?", (run_id, actor.id)
                    )
                ],
                "deliveries": [
                    _public_delivery(db.row_dict(row))
                    for row in conn.execute("SELECT * FROM outbox WHERE run_id=?", (run_id,))
                ],
                "usage": usage.summarize_usage(conn, run_id, actor),
                "calls": [
                    _public_call(db.row_dict(row))
                    for row in conn.execute(
                        "SELECT * FROM calls WHERE run_id=? ORDER BY transport_attempt_no",
                        (run_id,),
                    )
                ],
            }

        return await call(read)

    @app.post("/api/runs/{run_id}/input", status_code=202)
    async def input_run(run_id: str, request: Request):
        body = await _body(request, InputPatch)
        return {
            "run": await call(
                lambda conn: db.supply_inputs(
                    conn, run_id, actor, body.task_spec, expected_revision=body.expected_revision
                )
            )
        }

    @app.post("/api/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str, request: Request):
        await _body(request)
        return {"run": await call(lambda conn: engine.request_cancel(conn, run_id, actor))}

    @app.get("/api/automations")
    async def list_automations(request: Request):
        project_id = project(request)
        return {
            "automations": await call(
                lambda conn: [
                    {**row, "proposal_hash": db.automation_proposal_hash(row)}
                    for raw in conn.execute(
                        "SELECT * FROM automations WHERE actor_id=? AND project_id=? "
                        "ORDER BY created_at DESC LIMIT 100",
                        (actor.id, project_id),
                    )
                    if (row := db.row_dict(raw))
                ]
            )
        }

    @app.post("/api/automations", status_code=201)
    async def create_automation(request: Request):
        body = await _body(request, automation.AutomationConfig)
        return _bot(await call(lambda conn: db.create_automation(conn, actor, body)))

    @app.patch("/api/automations/{automation_id}")
    async def patch_automation(automation_id: str, request: Request):
        body = await _body(request, AutomationPatch)

        def patch(conn):
            current = db.get_automation(conn, automation_id, actor)
            if body.config is not None:
                return db.update_automation(
                    conn,
                    automation_id,
                    actor,
                    body.config,
                    expected_revision=body.expected_revision,
                )
            if body.enabled and current["config_json"]["delivery"]["channel"] == "telegram":
                settings = config.runtime_config(base, conn)
                if not settings.telegram.enabled:
                    raise AppError("POLICY_DENIED")
                return telegram.enable_automation(
                    conn,
                    actor,
                    settings.telegram.connection_id,
                    automation_id,
                    expected_revision=body.expected_revision,
                    confirmed_hash=body.confirmed_hash,
                    now=datetime.now(UTC),
                )
            return db.set_automation_enabled(
                conn,
                automation_id,
                body.expected_revision,
                body.enabled,
                actor,
                confirmed_hash=body.confirmed_hash,
            )

        return _bot(await call(patch))

    @app.post("/api/folders", status_code=201)
    async def folder(request: Request):
        body = await _body(request, FolderInput)
        return {
            "folder": await call(
                lambda conn: config.register_folder(
                    conn, home, actor, body.project_id, body.path, explicit_confirmed=True
                )
            )
        }

    @app.get("/api/folders")
    async def folders(request: Request):
        project_id = project(request)

        def read(conn):
            result = []
            for row in conn.execute(
                "SELECT value_json FROM kv WHERE key LIKE 'folder:%' LIMIT 1000"
            ):
                value = json.loads(row[0])
                if (value["actor_id"], value["project_id"]) == (actor.id, project_id):
                    result.append({"id": value["id"], "name": Path(value["path"]).name})
            return result

        return {"folders": await call(read)}

    @app.get("/api/approvals")
    async def approvals():
        return {
            "approvals": await call(
                lambda conn: [
                    db.row_dict(row)
                    for row in conn.execute(
                        "SELECT a.* FROM approvals a JOIN runs r ON r.id=a.run_id WHERE a.actor_id=? AND r.actor_id=? "
                        "AND a.state='pending' ORDER BY a.created_at LIMIT 100",
                        (actor.id, actor.id),
                    )
                    if row["proposal_json"]
                    and db.row_dict(row)["proposal_json"].get("project_id") in actor.project_ids
                ]
            )
        }

    @app.post("/api/approvals/{approval_id}/resolve")
    async def resolve(approval_id: str, request: Request):
        body = await _body(request, ApprovalInput)
        await call(
            lambda conn: engine.resolve_approval(
                conn,
                approval_id,
                actor,
                body.decision,
                body.proposal_hash,
                body.expected_revision,
                now=datetime.now(UTC),
            )
        )
        return {
            "id": approval_id,
            "state": "approved" if body.decision == "approve" else "rejected",
        }

    @app.post("/api/deliveries/{delivery_id}/cancel")
    async def cancel_delivery(delivery_id: str, request: Request):
        await _body(request)
        row = await call(
            lambda conn: telegram.cancel_delivery(conn, delivery_id, actor, now=datetime.now(UTC))
        )
        return {"delivery": _public_delivery(row)}

    @app.post("/api/telegram/pairing")
    async def pairing(request: Request):
        await _body(request)

        def pair(conn):
            settings = config.runtime_config(base, conn)
            key = config.read_telegram_token(home)
            if key is None or not settings.telegram.enabled:
                raise AppError("TELEGRAM_AUTH_ERROR")
            with telegram.TelegramClient(key, http_transport=telegram_transport) as client:
                telegram.register_connection(
                    conn,
                    actor,
                    settings.telegram.connection_id,
                    settings.telegram.project_id,
                    now=datetime.now(UTC),
                    client=client,
                )
                return telegram.create_pairing(
                    conn, actor, settings.telegram.connection_id, now=datetime.now(UTC)
                )

        return await call(pair)

    def settings_data(conn):
        settings = config.runtime_config(base, conn)
        row = conn.execute("SELECT revision FROM kv WHERE key='settings'").fetchone()
        catalog = runtime.catalog_snapshot() if runtime is not None else app.state.catalog
        catalog = catalog or config.resolve_catalog(settings, config_path).model_dump()
        connection = db.get_value(conn, "telegram:connection:" + settings.telegram.connection_id)
        public_connection = (
            {
                key: connection[key]
                for key in ("id", "project_id", "revision", "user_id", "chat_id")
                if key in connection
            }
            if connection
            else None
        )
        return {
            "config": settings.model_dump(),
            "revision": row[0] if row else 1,
            "model_status": catalog["statuses"],
            "key_present": config.read_xai_key(home) is not None,
            "telegram_key_present": config.read_telegram_token(home) is not None,
            "commandcode_key_present": config.read_commandcode_key(home) is not None,
            "telegram_connection": public_connection,
            "capabilities": {
                "inputs": list(artifacts.MEDIA),
                "outputs": ["text", "md", "csv", "xlsx", "docx"],
                "ocr": False,
                "visual_verified": False,
            },
        }

    @app.get("/api/settings")
    async def settings():
        return await call(settings_data)

    async def refresh():
        if runtime is not None:
            await runtime.refresh()
        else:
            app.state.catalog = None

    @app.patch("/api/settings")
    async def patch_settings(request: Request):
        body = await _body(request, SettingsPatch)
        fields = body.model_dump(exclude_unset=True, exclude={"expected_revision"})
        if not fields or any(value is None for value in fields.values()):
            raise AppError("INVALID_INPUT")

        def patch(conn):
            with db.transaction(conn):
                row = conn.execute("SELECT revision FROM kv WHERE key='settings'").fetchone()
                revision = row[0] if row else 1
                if revision != body.expected_revision:
                    raise AppError("CONFLICT", status=409)
                merged = {**db.get_value(conn, "settings", {}), **fields}
                config.AppConfig.model_validate({**base.model_dump(), **merged})
                if row is None:
                    db.set_value(conn, "settings", {})
                db.set_value(conn, "settings", merged)

        async with settings_lock:
            await call(patch)
            await refresh()
        return await call(settings_data)

    @app.post("/api/secrets")
    async def secret(request: Request):
        body = await _body(request, SecretInput)
        env = {
            "xai": "XAI_API_KEY",
            "telegram": "TELEGRAM_BOT_TOKEN",
            "commandcode": "COMMAND_CODE_API_KEY",
        }[body.provider]
        if os.environ.get(env, "").strip():
            raise AppError("SECRET_ENV_ACTIVE", status=409)
        async with settings_lock:
            await asyncio.to_thread(config.store_secret, home, body.provider, body.value)
            if body.provider != "commandcode":
                await refresh()
        return {"provider": body.provider, "stored": bool(body.value.get_secret_value().strip())}

    @app.post("/api/settings/check-models")
    async def check_models(request: Request):
        await _body(request)
        async with settings_lock:
            if runtime is not None:
                await runtime.refresh()
            else:

                def discover(conn):
                    settings = config.runtime_config(base, conn)
                    key = config.read_xai_key(home)
                    if key is None:
                        return config.resolve_catalog(settings, config_path).model_dump()
                    with providers.XAIClient(
                        key.get_secret_value(), http_transport=model_transport
                    ) as client:
                        return config.resolve_catalog(
                            settings, config_path, lookup_models=client.list_models
                        ).model_dump()

                app.state.catalog = await call(discover)
        return await call(settings_data)

    @app.get("/api/usage")
    async def usage_data(request: Request):
        project_id = project(request)
        local = datetime.now(ZoneInfo("Asia/Seoul"))
        day = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC).isoformat()
        month = (
            local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            .astimezone(UTC)
            .isoformat()
        )

        def read(conn):
            rows = [
                db.row_dict(row)
                for row in conn.execute(
                    "SELECT c.* FROM calls c JOIN runs r ON r.id=c.run_id WHERE r.actor_id=? AND r.project_id=? "
                    "AND c.created_at>=?",
                    (actor.id, project_id, month),
                )
            ]
            run_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM runs WHERE actor_id=? AND project_id=? "
                    "ORDER BY created_at DESC LIMIT 100",
                    (actor.id, project_id),
                )
            ]
            return {
                "today": usage._summarize([row for row in rows if row["created_at"] >= day]),
                "month": usage._summarize(rows),
                "runs": [
                    {"run_id": run_id, **usage.summarize_usage(conn, run_id, actor)}
                    for run_id in run_ids
                ],
            }

        return await call(read)

    @app.get("/api/memories")
    async def memories(request: Request):
        project_id = project(request)
        args = memory.MemorySearchArgs(query=request.query_params.get("query", ""), limit=20)
        return {
            "memories": await call(
                lambda conn: memory.search_memory(home, conn, actor, project_id, args)
            )
        }

    @app.post("/api/memories", status_code=201)
    async def save_memory(request: Request):
        body = await _body(request, MemoryInput)
        return jsonable_encoder(
            {
                "memory": await call(
                    lambda conn: memory.save_memory(
                        home, conn, actor, body.project_id, body.args, explicit_confirmed=True
                    )
                )
            }
        )

    @app.patch("/api/memories/{memory_id}")
    async def edit_memory(memory_id: str, request: Request):
        body = await _body(request, MemoryPatch)
        return {
            "memory": await call(
                lambda conn: memory.edit_memory(
                    home,
                    conn,
                    actor,
                    body.project_id,
                    memory_id,
                    body.args,
                    expected_revision=body.expected_revision,
                    explicit_confirmed=True,
                )
            )
        }

    @app.delete("/api/memories/{memory_id}")
    async def delete_memory(memory_id: str, request: Request):
        body = await _body(request, MemoryDelete)
        return await call(
            lambda conn: memory.delete_memory(
                home,
                conn,
                actor,
                body.project_id,
                memory_id,
                expected_revision=body.expected_revision,
                explicit_confirmed=True,
                purge_original=body.purge_original,
            )
        )

    return app
