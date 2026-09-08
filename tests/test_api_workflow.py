"""HTTP workflows use the real database, parser, planner and background executor."""

import hashlib
import io
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from birkin_lite import db
from birkin_lite.api import create_app
from birkin_lite.config import AppConfig, create_config
from birkin_lite.contracts import TaskSpec

ORIGIN = "http://127.0.0.1:8765"
CSV = "부서,금액\n영업,1\n지원,2\n".encode()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    path = tmp_path / "config.json"
    create_config(path, AppConfig(home="data", project_ids=["sales"]))
    return path, tmp_path / "data"


@contextmanager
def browser(config_path, *, background=True, raise_server_exceptions=True):
    requests = []

    def remote(request):
        requests.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(503, json={"error": "No external call belongs in a D workflow"})

    transport = httpx.MockTransport(remote)
    app = create_app(
        config_path,
        start_background=background,
        model_transport=transport,
        telegram_transport=transport,
    )
    with TestClient(
        app, base_url=ORIGIN, raise_server_exceptions=raise_server_exceptions
    ) as client:
        response = client.post(
            "/api/bootstrap", json={"code": app.state.bootstrap_secret}, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 200, response.text
        client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]})
        yield client, requests


def upload(client, content=CSV):
    response = client.post(
        "/api/artifacts",
        params={"project_id": "sales", "filename": "sales.csv"},
        content=content,
        headers={"Content-Type": "text/csv"},
    )
    assert response.status_code == 201, response.text
    return response.json()["artifact"]


def submit(client, source_id, *, request="금액 합계 계산해줘", key="sum-once"):
    payload = {
        "request": request,
        "project_id": "sales",
        "artifact_ids": [source_id],
        "quality_mode": "balanced",
        "output_format": "xlsx",
    }
    response = client.post("/api/runs", json=payload, headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    return response.json()["run"], payload


def wait_run(client, run_id, wanted, *, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get("/api/runs/" + run_id)
        assert response.status_code == 200, response.text
        detail = response.json()
        state = detail["run"]["state"]
        if state == wanted:
            return detail
        if state in {"failed", "cancelled", "expired", "rejected", "outcome_unknown"}:
            pytest.fail(f"Unexpected run state {state}: {detail['run']['error_code']}")
        time.sleep(0.1)
    pytest.fail(f"Run did not reach {wanted}; last state was {state}")


def assert_xlsx(client, detail):
    receipts = [
        value["receipt"]
        for value in detail["run"]["result_json"]["outputs"].values()
        if "receipt" in value
    ]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["run_id"] == detail["run"]["id"]
    assert receipt["validation"]["status"] == "pass"
    response = client.get(f"/api/artifacts/{receipt['artifact_id']}/download")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert response.headers["content-disposition"].startswith("attachment;")
    assert hashlib.sha256(response.content).hexdigest() == receipt["artifact_hash"]
    workbook = load_workbook(io.BytesIO(response.content), data_only=True)
    assert list(workbook["Result"].values) == [("그룹", "금액"), ("전체", "3.00")]
    workbook.close()
    assert detail["usage"]["usage_state"] == "not_used"
    assert detail["usage"]["total_tokens"] == 0
    assert all(step["state"] == "succeeded" for step in detail["steps"])
    return receipt


def test_http_csv_upload_background_d_run_download_and_duplicate_are_one_workflow(workspace):
    config_path, home = workspace
    with browser(config_path) as (client, requests):
        source = upload(client)
        assert source["sha256"] == hashlib.sha256(CSV).hexdigest()
        run, payload = submit(client, source["id"])
        detail = wait_run(client, run["id"], "succeeded")
        receipt = assert_xlsx(client, detail)
        assert client.get(f"/api/artifacts/{source['id']}/download").content == CSV
        listed = client.get("/api/artifacts", params={"project_id": "sales"}).json()["artifacts"]
        assert any(item["id"] == source["id"] for item in listed)
        again = client.post("/api/runs", json=payload, headers={"Idempotency-Key": "sum-once"})
        assert again.status_code == 202 and again.json()["run"]["id"] == run["id"]
        after = client.get("/api/runs/" + run["id"]).json()
        assert after["steps"] == detail["steps"]
        assert after["run"]["result_json"] == detail["run"]["result_json"]
        assert any(artifact["id"] == receipt["artifact_id"] for artifact in after["artifacts"])
        assert requests == []
    conn = db.init_db(home / "state.db")
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    conn.close()


def test_http_input_correction_wakes_waiting_run_and_finishes_same_work(workspace):
    config_path, _ = workspace
    with browser(config_path) as (client, requests):
        source = upload(client)
        run, _ = submit(client, source["id"], request="표 합계 내줘")
        waiting = wait_run(client, run["id"], "awaiting_input")
        correction = TaskSpec(
            intent="aggregate",
            source_ids=[source["id"]],
            inputs={"value_columns": ["금액"]},
            acceptance=["금액 합계 검증 후 결과표 저장"],
        ).model_dump(mode="json")
        response = client.post(
            f"/api/runs/{run['id']}/input",
            json={"expected_revision": waiting["run"]["revision"], "task_spec": correction},
        )
        assert response.status_code == 202, response.text
        assert response.json()["run"]["id"] == run["id"]
        finished = wait_run(client, run["id"], "succeeded")
        assert_xlsx(client, finished)
        assert finished["task_spec"] == correction
        assert requests == []


def test_http_input_revision_scope_and_prepare_marker_commit_atomically(workspace):
    config_path, home = workspace
    with browser(config_path, background=False, raise_server_exceptions=False) as (client, _):
        response = client.post(
            "/api/runs",
            json={"request": "금액 합계 계산해줘", "project_id": "sales"},
            headers={"Idempotency-Key": "input-marker"},
        )
        assert response.status_code == 202
        run = response.json()["run"]
        conn = db.init_db(home / "state.db")
        before = tuple(
            conn.execute(
                "SELECT revision,policy_json FROM runs WHERE id=?", (run["id"],)
            ).fetchone()
        )
        marker = "prepare_pending:" + run["id"]
        for operation in ("INSERT", "UPDATE"):
            conn.execute(
                f"CREATE TRIGGER fail_prepare_{operation.lower()} BEFORE {operation} ON kv "
                "WHEN NEW.key LIKE 'prepare_pending:%' "
                "BEGIN SELECT RAISE(ABORT,'synthetic prepare marker failure'); END"
            )
        spec = TaskSpec(
            intent="aggregate", inputs={"value_columns": ["금액"]}, acceptance=["합계 검증"]
        ).model_dump(mode="json")
        body = {"expected_revision": run["revision"], "task_spec": spec}
        failed = client.post(f"/api/runs/{run['id']}/input", json=body)
        assert failed.status_code >= 400
        assert "synthetic prepare marker" not in failed.text
        assert (
            tuple(
                conn.execute(
                    "SELECT revision,policy_json FROM runs WHERE id=?", (run["id"],)
                ).fetchone()
            )
            == before
        )
        assert db.get_value(conn, marker) is None
        assert db.get_value(conn, "task_spec:" + run["id"]) is None
        for operation in ("insert", "update"):
            conn.execute(f"DROP TRIGGER fail_prepare_{operation}")
        accepted = client.post(f"/api/runs/{run['id']}/input", json=body)
        assert accepted.status_code == 202, accepted.text
        revision = accepted.json()["run"]["revision"]
        assert revision == run["revision"] + 1
        assert db.get_value(conn, marker) == {"revision": revision}
        assert db.get_value(conn, "task_spec:" + run["id"]) == spec
        stale = client.post(f"/api/runs/{run['id']}/input", json=body)
        assert stale.status_code == 409
        assert db.get_value(conn, marker) == {"revision": revision}
        conn.close()


def test_http_automation_preview_exact_enable_edit_and_pause(workspace):
    config_path, _ = workspace
    with browser(config_path, background=False) as (client, requests):
        config = {
            "name": "합계 예약",
            "project_id": "sales",
            "recipe": "aggregate",
            "instruction": "금액 합계 계산해줘",
            "trigger": {"kind": "once", "at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
        }
        created = client.post("/api/automations", json=config)
        assert created.status_code == 201, created.text
        preview = created.json()
        bot = preview["automation"]
        assert bot["enabled"] == 0 and len(preview["proposal_hash"]) == 64
        listed = client.get("/api/automations", params={"project_id": "sales"}).json()[
            "automations"
        ]
        assert (
            next(item for item in listed if item["id"] == bot["id"])["proposal_hash"]
            == preview["proposal_hash"]
        )
        path = "/api/automations/" + bot["id"]
        denied = client.patch(
            path,
            json={
                "expected_revision": bot["revision"],
                "enabled": True,
                "confirmed_hash": "0" * 64,
            },
        )
        assert denied.status_code == 400 and denied.json()["error"]["code"] == "POLICY_DENIED"
        enabled = client.patch(
            path,
            json={
                "expected_revision": bot["revision"],
                "enabled": True,
                "confirmed_hash": preview["proposal_hash"],
            },
        )
        assert enabled.status_code == 200, enabled.text
        live = enabled.json()["automation"]
        assert live["enabled"] == 1 and live["revision"] == bot["revision"] + 1
        stale_revision = client.patch(
            path, json={"expected_revision": bot["revision"], "enabled": False}
        )
        assert stale_revision.status_code == 409
        revised_config = {**config, "name": "수정된 합계 예약"}
        edited = client.patch(
            path, json={"expected_revision": live["revision"], "config": revised_config}
        )
        assert edited.status_code == 200, edited.text
        edited_body = edited.json()
        assert edited_body["automation"]["enabled"] == 0
        assert edited_body["proposal_hash"] != preview["proposal_hash"]
        revision = edited_body["automation"]["revision"]
        stale = client.patch(
            path,
            json={
                "expected_revision": revision,
                "enabled": True,
                "confirmed_hash": preview["proposal_hash"],
            },
        )
        assert stale.status_code == 400 and stale.json()["error"]["code"] == "POLICY_DENIED"
        activated = client.patch(
            path,
            json={
                "expected_revision": revision,
                "enabled": True,
                "confirmed_hash": edited_body["proposal_hash"],
            },
        )
        assert activated.status_code == 200
        revision = activated.json()["automation"]["revision"]
        paused = client.patch(path, json={"expected_revision": revision, "enabled": False})
        assert paused.status_code == 200 and paused.json()["automation"]["enabled"] == 0
        assert requests == []


def test_http_rejects_oversized_binary_and_unsupported_file_before_import(workspace):
    config_path, home = workspace
    with browser(config_path, background=False) as (client, requests):
        oversized = client.post(
            "/api/artifacts",
            params={"project_id": "sales", "filename": "large.csv"},
            content=b"x" * (25 * 1024 * 1024 + 1),
            headers={"Content-Type": "text/csv"},
        )
        assert oversized.status_code == 413
        unsupported = client.post(
            "/api/artifacts",
            params={"project_id": "sales", "filename": "program.exe"},
            content=b"MZ",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert unsupported.status_code == 415
        assert requests == []
    conn = db.init_db(home / "state.db")
    assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    conn.close()
