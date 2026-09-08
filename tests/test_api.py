"""Real ASGI security boundary; no engine, database, or parser substitutes."""

from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from birkin_lite import db, usage
from birkin_lite.api import create_app
from birkin_lite.config import AppConfig, create_config, load_config
from birkin_lite.contracts import (
    SAFE_MESSAGES,
    ActionProposal,
    Actor,
    ModelBinding,
    ModelRequest,
    RunRequest,
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


def test_actual_call_metadata_is_scoped_and_has_no_prompt_or_private_route(local_api):
    client, app, path = local_api
    headers = {**authenticate(client, app), "Idempotency-Key": "visible-model"}
    run_id = client.post("/api/runs", json=REQUEST, headers=headers).json()["run"]["id"]
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
    }
    assert "PRIVATE_SYNTHETIC_CONTEXT" not in detail.text and "PRIVATE_ROUTE" not in detail.text


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
