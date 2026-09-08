"""Real ASGI security boundary; no engine, database, or parser substitutes."""

from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from birkin_lite import db, usage
from birkin_lite.api import create_app
from birkin_lite.automation import (
    AutomationConfig,
    record_daemon_observation,
    tick_automations,
)
from birkin_lite.config import AppConfig, create_config, load_config, stored_secret
from birkin_lite.contracts import (
    SAFE_MESSAGES,
    ActionProposal,
    Actor,
    ModelBinding,
    ModelRequest,
    PriceSnapshot,
    RunRequest,
    TokenRates,
    Usage,
    canonical_json,
    digest,
)

ORIGIN = "http://127.0.0.1:8765"
OWNER = Actor(id="owner", project_ids=["sales"])
REQUEST = {"request": "업무 내용을 정리해 주세요", "project_id": "sales"}


@pytest.fixture
def local_api(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("COMMAND_CODE_API_KEY", raising=False)
    path = tmp_path / "config.json"
    create_config(path, AppConfig(home="data", project_ids=["sales"]))
    app = create_app(path, start_background=False)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, app, path


def authenticate(client, app):
    response = client.post(
        "/api/bootstrap", json={"code": app.state.bootstrap_secret}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200, response.text
    token = response.json()["csrf_token"]
    assert isinstance(token, str) and len(token) >= 32
    return {"Origin": ORIGIN, "X-CSRF-Token": token}


def error(response, status, code=None):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/json")
    value = response.json()["error"]
    assert set(value) == {"code", "message_ko", "recoverable", "run_id"}
    assert value["message_ko"] in SAFE_MESSAGES.values()
    assert type(value["recoverable"]) is bool
    if code is not None:
        assert value["code"] == code
    return value


def count_runs(path):
    with closing(db.init_db(path.parent / "data" / "state.db")) as conn:
        return conn.execute("SELECT count(*) FROM runs").fetchone()[0]


@pytest.mark.parametrize("purge_original", [False, True])
def test_memory_delete_explicit_file_removal_is_scoped(local_api, purge_original):
    client, app, config_path = local_api
    headers = authenticate(client, app)
    created = client.post(
        "/api/memories",
        headers=headers,
        json={
            "project_id": "sales",
            "args": {"category": "preference", "text": "합성 보관 기준"},
        },
    )
    assert created.status_code == 201
    record = created.json()["memory"]
    stored = config_path.parent / "data/memories" / (record["artifact_id"] + ".md")
    original = stored.read_bytes()
    body = {
        "project_id": "private",
        "expected_revision": record["revision"],
        "purge_original": purge_original,
    }
    refused = client.request("DELETE", "/api/memories/" + record["id"], headers=headers, json=body)
    assert refused.status_code == 404 and stored.read_bytes() == original
    body["project_id"] = "sales"
    result = client.request("DELETE", "/api/memories/" + record["id"], headers=headers, json=body)
    assert result.status_code == 200 and result.json()["purge_pending"] is False
    assert stored.exists() is not purge_original
    if stored.exists():
        assert stored.read_bytes() == original
    assert client.get("/api/memories?project_id=sales").json()["memories"] == []


def planner_call_fixture():
    binding = ModelBinding(
        provider="xai",
        model_id="synthetic-model",
        roles=["M"],
        role="M",
        enabled=True,
        available=True,
        allowed_projects=["sales"],
        capabilities=["structured_output", "planning"],
        context_window=32768,
        max_output=1000,
        metering_mode="api",
        quality_evidence_id="fixture",
    )
    request = ModelRequest(
        model_id=binding.model_id,
        prompt_version="v1",
        output_schema={"type": "object"},
        context="PRIVATE_SYNTHETIC_CONTEXT",
        max_output=100,
    )
    return binding, request


def test_actual_call_metadata_is_scoped_and_has_no_prompt_or_private_route(local_api):
    client, app, path = local_api
    headers = {**authenticate(client, app), "Idempotency-Key": "visible-model"}
    run_id = client.post("/api/runs", json=REQUEST, headers=headers).json()["run"]["id"]
    binding, request = planner_call_fixture()
    with closing(db.init_db(path.parent / "data" / "state.db")) as conn:
        usage.begin_call(
            conn,
            run_id,
            None,
            binding,
            request,
            actor=OWNER,
            purpose="plan",
            now=datetime.now(UTC),
            route={"role": "M", "reason": "PRIVATE_ROUTE"},
        )
    detail = client.get("/api/runs/" + run_id)
    calls = detail.json()["calls"]
    assert len(calls) == 1
    assert (calls[0]["step_id"], calls[0]["model_id"], calls[0]["role"], calls[0]["state"]) == (
        None,
        "synthetic-model",
        "M",
        "reserved",
    )
    assert set(calls[0]) == {
        "id",
        "step_id",
        "logical_call_no",
        "transport_attempt_no",
        "provider",
        "model_id",
        "purpose",
        "state",
        "role",
        "reasoning_effort",
        "input_tokens_total",
        "cached_input_tokens",
        "output_tokens_total",
        "reasoning_tokens",
        "usage_basis",
        "actual_micro_usd",
        "price_stale",
    }
    assert calls[0]["actual_micro_usd"] is None and calls[0]["price_stale"] is True
    assert detail.json()["automation_observation"] is None
    assert "PRIVATE_SYNTHETIC_CONTEXT" not in detail.text and "PRIVATE_ROUTE" not in detail.text


@pytest.mark.parametrize(
    "unknown_attempt,unpriced", [(None, False), (1, False), (2, False), (None, True)]
)
def test_attempt_costs_preserve_real_retry_groups_unknown_totals_and_scope(
    local_api, unknown_attempt, unpriced
):
    client, app, path = local_api
    headers = {**authenticate(client, app), "Idempotency-Key": "attempt-costs"}
    run_id = client.post("/api/runs", json=REQUEST, headers=headers).json()["run"]["id"]
    binding, request = planner_call_fixture()
    now = datetime.now(UTC)
    snapshot = (
        None
        if unpriced
        else PriceSnapshot(
            rates=TokenRates(input=1000000, output=2000000),
            cache_write_billing="included",
            checked_at=now.isoformat(),
            source_url="https://example.invalid/PRIVATE_PRICE_SOURCE",
        )
    )
    with closing(db.init_db(path.parent / "data" / "state.db")) as conn:
        previous, expected = None, []
        for attempt in (1, 2):
            record = usage.begin_call(
                conn,
                run_id,
                None,
                binding,
                request,
                actor=OWNER,
                purpose="plan",
                now=now,
                price_snapshot=snapshot,
                retry_of=previous,
                route={"role": "M", "reason": "PRIVATE_ROUTE"},
            )
            usage.start_call(conn, record["id"], OWNER)
            if attempt == unknown_attempt:
                usage.mark_usage_unknown(conn, record["id"], "TIMEOUT", actor=OWNER, now=now)
                expected.append(None)
            else:
                settled = usage.settle_call(
                    conn,
                    record["id"],
                    Usage(
                        input_tokens_total=100 * attempt,
                        cached_input_tokens=0,
                        output_tokens_total=10 * attempt,
                        usage_basis="provider",
                    ),
                    {"prompt_tokens": 100 * attempt},
                    actor=OWNER,
                    now=now,
                    request_id="PRIVATE_PROVIDER_REQUEST",
                    finish_reason="stop",
                )
                expected.append(None if unpriced else 120 * attempt)
                assert settled["actual_micro_usd"] == expected[-1]
            previous = record["id"]
        released = usage.begin_call(
            conn,
            run_id,
            None,
            binding,
            request,
            actor=OWNER,
            purpose="plan",
            now=now,
            price_snapshot=snapshot,
            route={"repair_errors": ["PLAN_INVALID"]},
        )
        usage.release_call(conn, released["id"], actor=OWNER, now=now)
        other = Actor(id="another-owner", project_ids=["sales"])
        hidden = db.create_run(conn, other, "ui", "hidden-cost", RunRequest(**REQUEST))
        usage.begin_call(
            conn,
            hidden["id"],
            None,
            binding,
            request,
            actor=other,
            purpose="plan",
            now=now,
            price_snapshot=snapshot,
        )
    response = client.get("/api/runs/" + run_id)
    assert response.status_code == 200
    detail = response.json()
    calls = detail["calls"]
    assert [row["logical_call_no"] for row in calls] == [1, 1, 2]
    assert [row["transport_attempt_no"] for row in calls] == [1, 2, 3]
    assert [row["actual_micro_usd"] for row in calls] == [*expected, None]
    assert [row["price_stale"] for row in calls] == [unpriced] * 3
    assert calls[-1]["state"] == "released"
    total = None if any(value is None for value in expected) else sum(expected)
    assert detail["usage"]["actual_micro_usd"] == total
    assert detail["usage"]["by_step"]["planning"]["actual_micro_usd"] == total
    assert detail["usage"]["known_actual_micro_usd"] == sum(
        value for value in expected if value is not None
    )
    assert detail["usage"]["released_calls"] == 1
    assert detail["usage"]["unknown_calls"] == int(unknown_attempt is not None)
    assert detail["usage"]["usage_state"] == ("unknown" if unknown_attempt else "provider")
    assert "PRIVATE_" not in response.text and hidden["id"] not in response.text
    error(client.get("/api/runs/" + hidden["id"]), 404)


@pytest.mark.parametrize(
    "delay,changed",
    [
        (0, None),
        (1, None),
        (0, "schema_version"),
        (0, "actor_id"),
        (0, "project_id"),
        (0, "automation_id"),
        (0, "event_key"),
        (0, "missing"),
    ],
)
def test_run_observation_uses_its_scoped_event_and_safe_snapshot(local_api, delay, changed):
    client, app, path = local_api
    authenticate(client, app)
    start = datetime(2026, 9, 8, tzinfo=UTC)
    scheduled = start + timedelta(minutes=5)
    with closing(db.init_db(path.parent / "data" / "state.db")) as conn:
        bot = db.create_automation(
            conn,
            OWNER,
            AutomationConfig(
                name="관측 검사",
                project_id="sales",
                recipe="summarize",
                instruction="합성 업무",
                trigger={"kind": "once", "at": scheduled.isoformat()},
            ),
            now=start,
        )
        db.set_automation_enabled(
            conn,
            bot["id"],
            bot["revision"],
            True,
            OWNER,
            confirmed_hash=db.automation_proposal_hash(bot),
            now=start,
        )
        record_daemon_observation(conn, OWNER, "PRIVATE_SESSION", "start", start)
        run = tick_automations(
            conn,
            path.parent / "data",
            OWNER,
            scheduled + timedelta(seconds=delay),
            daemon_session_id="PRIVATE_SESSION",
        )[0]
        event = conn.execute(
            "SELECT event_key FROM inbox_events WHERE run_id=?", (run["id"],)
        ).fetchone()
        key = "automation_observation:" + bot["id"] + ":" + event["event_key"]
        snapshot = db.get_value(conn, key)
        snapshot["private_context"] = "PRIVATE_EXTRA"
        if changed == "missing":
            snapshot = None
        elif changed:
            snapshot[changed] = "PRIVATE_WRONG_SCOPE"
        db.set_value(conn, key, snapshot)
    response = client.get("/api/runs/" + run["id"])
    assert response.status_code == 200
    observation = response.json()["automation_observation"]
    if changed:
        assert observation is None
    else:
        assert observation == {
            name: snapshot[name]
            for name in (
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
        assert observation["scheduled_daemon_state"] == ("unknown" if delay else "observed")
        assert observation["pc_power_state"] == "unknown"
        assert observation["scheduled_utc"] == scheduled.isoformat()
    assert "PRIVATE_" not in response.text


def test_bootstrap_sets_private_cookie_and_consumes_code_once(local_api):
    client, app, _ = local_api
    code = app.state.bootstrap_secret
    health = client.get("/api/health")
    assert health.status_code == 200 and health.json() == {"ok": True}
    assert code not in health.text
    error(client.get("/api/session"), 401)
    response = client.post("/api/bootstrap", json={"code": code}, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "domain=" not in cookie and code not in cookie
    session = client.get("/api/session")
    assert session.status_code == 200
    assert session.json() == {
        "csrf_token": response.json()["csrf_token"],
        "actor": {"id": "owner", "project_ids": ["sales"]},
    }
    replay = client.post("/api/bootstrap", json={"code": code}, headers={"Origin": ORIGIN})
    assert replay.status_code in {401, 403, 409}
    assert "set-cookie" not in replay.headers


def test_wrong_bootstrap_code_does_not_consume_real_code(local_api):
    client, app, _ = local_api
    rejected = client.post(
        "/api/bootstrap", json={"code": "wrong-local-code"}, headers={"Origin": ORIGIN}
    )
    assert rejected.status_code in {401, 403}
    assert "set-cookie" not in rejected.headers
    authenticate(client, app)


@pytest.mark.parametrize(
    "host", ["evil.example", "localhost:8765", "127.0.0.1:8766", "127.0.0.1.evil:8765"]
)
def test_rebinding_hosts_are_rejected_even_for_health(local_api, host):
    client, _, _ = local_api
    response = client.get("/api/health", headers={"Host": host})
    assert response.status_code in {400, 403}
    error(response, response.status_code)
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "origin",
    [None, "null", "https://127.0.0.1:8765", "http://localhost:8765", "https://evil.example"],
)
def test_mutation_requires_exact_origin_even_with_cookie_and_csrf(local_api, origin):
    client, app, path = local_api
    headers = authenticate(client, app)
    if origin is None:
        headers.pop("Origin")
    else:
        headers["Origin"] = origin
    headers["Idempotency-Key"] = "wrong-origin"
    response = client.post("/api/runs", json=REQUEST, headers=headers)
    assert response.status_code in {400, 403}
    error(response, response.status_code)
    assert count_runs(path) == 0
    assert "access-control-allow-origin" not in response.headers


def test_cross_site_metadata_and_preflight_do_not_authorize_a_request(local_api):
    client, app, path = local_api
    headers = authenticate(client, app)
    headers.update({"Sec-Fetch-Site": "cross-site", "Idempotency-Key": "cross-site"})
    response = client.post("/api/runs", json=REQUEST, headers=headers)
    assert response.status_code in {400, 403}
    response = client.options(
        "/api/runs",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in response.headers
    assert count_runs(path) == 0


@pytest.mark.parametrize("csrf", [None, "wrong-token"])
def test_csrf_is_required_and_bound_to_the_session(local_api, csrf):
    client, app, path = local_api
    headers = authenticate(client, app)
    if csrf is None:
        headers.pop("X-CSRF-Token")
    else:
        headers["X-CSRF-Token"] = csrf
    headers["Idempotency-Key"] = "wrong-csrf"
    response = client.post("/api/runs", json=REQUEST, headers=headers)
    assert response.status_code in {400, 403}
    error(response, response.status_code)
    assert count_runs(path) == 0


def test_csrf_without_session_and_forged_session_are_rejected(local_api):
    client, app, path = local_api
    headers = authenticate(client, app)
    cookies = list(client.cookies.jar)
    assert len(cookies) == 1
    client.cookies.clear()
    headers["Idempotency-Key"] = "no-session"
    error(client.post("/api/runs", json=REQUEST, headers=headers), 401)
    client.cookies.set(cookies[0].name, "forged-session-value")
    error(client.get("/api/session"), 401)
    assert count_runs(path) == 0


@pytest.mark.parametrize(
    "route",
    [
        "/api/runs?project_id=sales",
        "/api/artifacts?project_id=sales",
        "/api/automations?project_id=sales",
        "/api/folders?project_id=sales",
        "/api/memories?project_id=sales",
        "/api/usage?project_id=sales",
        "/api/settings",
        "/api/approvals",
    ],
)
def test_private_read_routes_require_session(local_api, route):
    client, _, _ = local_api
    error(client.get(route), 401)


@pytest.mark.parametrize(
    "resource", ["runs", "artifacts", "automations", "folders", "memories", "usage"]
)
def test_other_project_queries_return_not_found(local_api, resource):
    client, app, _ = local_api
    authenticate(client, app)
    error(client.get(f"/api/{resource}", params={"project_id": "other"}), 404, "NOT_FOUND")


def test_request_cannot_supply_actor_or_grant_project_access(local_api):
    client, app, path = local_api
    headers = {**authenticate(client, app), "Idempotency-Key": "scope"}
    error(client.post("/api/runs", json={**REQUEST, "actor_id": "admin"}, headers=headers), 400)
    error(
        client.post("/api/runs", json={**REQUEST, "project_id": "private"}, headers=headers),
        404,
        "NOT_FOUND",
    )
    assert count_runs(path) == 0


@pytest.mark.parametrize(
    "body",
    [
        b'{"request":"first","request":"second","project_id":"sales"}',
        b'{"request":"secret\\u0000value","project_id":"sales","unknown":true}',
        b'{"request":',
        b'"not an object"',
        b'{"request":NaN,"project_id":"sales"}',
    ],
)
def test_json_parser_rejects_ambiguous_or_invalid_inputs_without_echo(local_api, body):
    client, app, path = local_api
    headers = {
        **authenticate(client, app),
        "Idempotency-Key": "invalid-json",
        "Content-Type": "application/json",
    }
    response = client.post("/api/runs", content=body, headers=headers)
    error(response, 400, "INVALID_INPUT")
    assert "secret" not in response.text and "Traceback" not in response.text
    assert count_runs(path) == 0


def test_body_limit_is_applied_before_json_parsing(local_api):
    client, app, path = local_api
    headers = {
        **authenticate(client, app),
        "Idempotency-Key": "oversize",
        "Content-Type": "application/json",
    }
    response = client.post("/api/runs", content=b" " * (65536 + 1), headers=headers)
    assert response.status_code in {400, 413}
    error(response, response.status_code, "LIMIT_EXCEEDED")
    assert count_runs(path) == 0


def test_run_request_is_literal_json_and_idempotency_is_strict(local_api):
    client, app, path = local_api
    headers = authenticate(client, app)
    error(client.post("/api/runs", json=REQUEST, headers=headers), 400)
    headers["Idempotency-Key"] = "same-user-request"
    payload = {**REQUEST, "request": '<img src=x onerror="alert(1)"> & 문서 내용'}
    first = client.post("/api/runs", json=payload, headers=headers)
    assert first.status_code == 202, first.text
    assert first.headers["content-type"].startswith("application/json")
    run = first.json()["run"]
    assert run["request_json"]["request"] == payload["request"]
    repeated = client.post("/api/runs", json=payload, headers=headers)
    assert repeated.status_code == 202 and repeated.json()["run"]["id"] == run["id"]
    error(client.post("/api/runs", json=REQUEST, headers=headers), 409, "CONFLICT")
    assert count_runs(path) == 1


def test_settings_are_revisioned_and_cannot_change_identity_or_expose_keys(local_api):
    client, app, path = local_api
    headers = authenticate(client, app)
    initial = client.get("/api/settings")
    assert initial.status_code == 200
    initial = initial.json()
    assert initial["key_present"] is False and initial["telegram_key_present"] is False
    revision = initial["revision"]
    for field, value in [
        ("home", "C:/private"),
        ("actor_id", "admin"),
        ("project_ids", ["private"]),
    ]:
        response = client.patch(
            "/api/settings", json={"expected_revision": revision, field: value}, headers=headers
        )
        error(response, 400, "INVALID_INPUT")
        assert "C:/private" not in response.text
    patch = {
        "expected_revision": revision,
        "telegram": {"enabled": False, "connection_id": "telegram", "project_id": "sales"},
    }
    changed = client.patch("/api/settings", json=patch, headers=headers)
    assert changed.status_code == 200, changed.text
    assert client.get("/api/settings").json()["revision"] > revision
    error(client.patch("/api/settings", json=patch, headers=headers), 409, "CONFLICT")
    saved = load_config(path)
    assert saved.actor_id == "owner" and saved.project_ids == ["sales"] and saved.home == "data"


def test_commandcode_secret_save_status_delete_without_catalog_refresh(local_api, monkeypatch):
    client, app, path = local_api
    headers = authenticate(client, app)
    initial = client.get("/api/settings").json()
    assert initial["commandcode_key_present"] is False
    app.state.catalog = {"statuses": initial["model_status"]}
    catalog = app.state.catalog
    secret = "SYNTHETIC_COMMANDCODE_KEY_NO_REMOTE_USE"
    body = {"provider": "commandcode", "value": secret}
    saved = client.post("/api/secrets", json=body, headers=headers)
    assert saved.status_code == 200
    assert saved.json() == {"provider": "commandcode", "stored": True}
    home = path.parent / "data"
    assert stored_secret(home, "commandcode").get_secret_value() == secret
    status = client.get("/api/settings")
    assert status.json()["commandcode_key_present"] is True
    assert status.json()["config"] == initial["config"]
    assert app.state.catalog is catalog
    assert secret not in status.text and secret not in saved.text
    monkeypatch.setenv("COMMAND_CODE_API_KEY", "SYNTHETIC_ENV_KEY")
    error(client.post("/api/secrets", json=body, headers=headers), 409, "SECRET_ENV_ACTIVE")
    assert stored_secret(home, "commandcode").get_secret_value() == secret
    monkeypatch.delenv("COMMAND_CODE_API_KEY")
    deleted = client.post("/api/secrets", json={**body, "value": ""}, headers=headers)
    assert deleted.status_code == 200 and deleted.json()["stored"] is False
    assert client.get("/api/settings").json()["commandcode_key_present"] is False
    assert stored_secret(home, "commandcode") is None
    assert app.state.catalog is catalog


def test_paths_and_provider_key_inputs_do_not_leak_in_error_responses(local_api):
    client, app, path = local_api
    headers = authenticate(client, app)
    secret = "test-key-that-must-not-appear-in-errors"
    response = client.post(
        "/api/secrets", json={"provider": "unsupported", "value": secret}, headers=headers
    )
    error(response, 400, "INVALID_INPUT")
    assert secret not in response.text
    hidden = str(path.parent / "private-nonexistent-folder")
    response = client.post(
        "/api/folders",
        json={"project_id": "sales", "path": hidden, "explicit_confirmed": True},
        headers=headers,
    )
    assert response.status_code in {400, 404}
    error(response, response.status_code)
    assert hidden not in response.text and str(path.parent) not in response.text


def pending_approval(path, *, expires_at, owner=OWNER):
    """Seed a valid durable pending record; the HTTP route resolves the actual engine function."""
    conn = db.init_db(path.parent / "data" / "state.db")
    run = db.create_run(conn, owner, "ui", "approval-fixture", RunRequest.model_validate(REQUEST))
    step_id, approval_id = "approval-step", "approval-id"
    proposal = ActionProposal(
        actor_id=owner.id,
        project_id="sales",
        run_id=run["id"],
        step_id=step_id,
        tool="artifact.publish",
        args={"destination": "owner"},
        source_hashes={},
        draft_hash="a" * 64,
        destination="owner",
        recipient_ref="owner",
        scope_revision=1,
        expires_at=expires_at.isoformat(),
        impact="명시한 결과만 소유자에게 전달",
    )
    now = datetime.now(UTC).isoformat()
    with db.transaction(conn):
        conn.execute("UPDATE runs SET state='awaiting_approval' WHERE id=?", (run["id"],))
        conn.execute(
            "INSERT INTO steps (id,run_id,ordinal,kind,effect,state,spec_json) "
            "VALUES (?,?,0,'apply','external_write','awaiting_approval','{}')",
            (step_id, run["id"]),
        )
        conn.execute(
            "INSERT INTO approvals "
            "(id,run_id,step_id,actor_id,proposal_hash,proposal_json,expires_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                approval_id,
                run["id"],
                step_id,
                owner.id,
                digest(proposal),
                canonical_json(proposal),
                expires_at.isoformat(),
                now,
            ),
        )
    conn.close()
    return approval_id, digest(proposal), run["id"]


@pytest.mark.parametrize("changed", ["hash", "revision"])
def test_stale_approval_cannot_resolve_current_proposal(local_api, changed):
    client, app, path = local_api
    headers = authenticate(client, app)
    approval_id, proposal_hash, _ = pending_approval(
        path, expires_at=datetime.now(UTC) + timedelta(minutes=10)
    )
    body = {"decision": "approve", "proposal_hash": proposal_hash, "expected_revision": 1}
    body["proposal_hash" if changed == "hash" else "expected_revision"] = (
        "b" * 64 if changed == "hash" else 2
    )
    error(client.post(f"/api/approvals/{approval_id}/resolve", json=body, headers=headers), 409)
    with closing(db.init_db(path.parent / "data" / "state.db")) as conn:
        row = conn.execute(
            "SELECT state,revision FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        assert tuple(row) == ("pending", 1)
    body.update(proposal_hash=proposal_hash, expected_revision=1)
    resolved = client.post(f"/api/approvals/{approval_id}/resolve", json=body, headers=headers)
    assert resolved.status_code == 200 and resolved.json() == {
        "id": approval_id,
        "state": "approved",
    }
    error(client.post(f"/api/approvals/{approval_id}/resolve", json=body, headers=headers), 409)


def test_expired_approval_is_durably_expired(local_api):
    client, app, path = local_api
    headers = authenticate(client, app)
    approval_id, proposal_hash, run_id = pending_approval(
        path, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    response = client.post(
        f"/api/approvals/{approval_id}/resolve",
        json={"decision": "approve", "proposal_hash": proposal_hash, "expected_revision": 1},
        headers=headers,
    )
    error(response, 410, "APPROVAL_EXPIRED")
    with closing(db.init_db(path.parent / "data" / "state.db")) as conn:
        assert conn.execute("SELECT state FROM approvals").fetchone()[0] == "expired"
        assert db.get_run(conn, run_id, OWNER)["state"] == "expired"


def test_other_actor_records_are_hidden_even_when_id_is_known(local_api):
    client, app, path = local_api
    headers = authenticate(client, app)
    approval_id, proposal_hash, run_id = pending_approval(
        path,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        owner=Actor(id="other-owner", project_ids=["sales"]),
    )
    error(client.get(f"/api/runs/{run_id}"), 404, "NOT_FOUND")
    error(client.post(f"/api/runs/{run_id}/cancel", json={}, headers=headers), 404, "NOT_FOUND")
    response = client.post(
        f"/api/approvals/{approval_id}/resolve",
        json={"decision": "approve", "proposal_hash": proposal_hash, "expected_revision": 1},
        headers=headers,
    )
    error(response, 404, "NOT_FOUND")
    assert client.get("/api/approvals").json() == {"approvals": []}
