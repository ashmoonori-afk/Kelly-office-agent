"""Actual local CLI lifecycle and scheduled office work without external services."""

import hashlib
import io
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from birkin_lite.config import AppConfig, create_config
from birkin_lite.engine import DaemonLock

pytestmark = pytest.mark.skipif(os.name != "nt", reason="CLI access codes use Windows DPAPI")
ROOT = Path(__file__).resolve().parents[1]


def _environment():
    # An allowlist excludes real provider credentials and proxy configuration.
    names = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT", "USERPROFILE")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return env


def _free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _launch(path, *, access_code=False):
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    command = [sys.executable, "-m", "birkin_lite", "serve", "--config", str(path)]
    if access_code:
        command.append("--access-code")
    return subprocess.Popen(
        command,
        cwd=ROOT,
        env=_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        startupinfo=startup,
        creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
    )


def _ready(process, client):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        assert process.poll() is None, "CLI exited before its local health endpoint was ready"
        try:
            response = client.get("/api/health")
            if response.status_code == 200:
                assert response.json() == {"ok": True}
                return
        except httpx.RequestError:
            pass
        time.sleep(0.1)
    pytest.fail("CLI did not become healthy within 30 seconds")


def _access_code(path):
    process = _launch(path, access_code=True)
    try:
        stdout, stderr = process.communicate(timeout=15)
        code = SecretStr(stdout.strip())
        return_code = process.returncode
        valid_shape = bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", code.get_secret_value()))
        secret_in_stderr = code.get_secret_value() in stderr if valid_shape else False
        assert return_code == 0
        assert valid_shape, "Access-code command did not return one opaque code"
        assert not secret_in_stderr
        return code
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def _graceful_stop(process):
    # Codex/pytest may have no console. A short hidden helper attaches only to the
    # server's private console to send the real Windows break event to uvicorn.
    helper = r"""
import ctypes
import sys
from ctypes import wintypes

kernel = ctypes.WinDLL('kernel32', use_last_error=True)
handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
kernel.AttachConsole.argtypes = [wintypes.DWORD]
kernel.AttachConsole.restype = wintypes.BOOL
kernel.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
kernel.SetConsoleCtrlHandler.restype = wintypes.BOOL
kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
kernel.FreeConsole.restype = wintypes.BOOL
# The Windows venv redirector can attach its child even with CREATE_NO_WINDOW.
kernel.FreeConsole()
if not kernel.AttachConsole(int(sys.argv[1])):
    raise SystemExit(2)
handler = handler_type(lambda event: True)
try:
    if not kernel.SetConsoleCtrlHandler(handler, True):
        raise SystemExit(3)
    if not kernel.GenerateConsoleCtrlEvent(1, int(sys.argv[1])):
        raise SystemExit(4)
finally:
    kernel.FreeConsole()
"""
    signaler = subprocess.run(
        [sys.executable, "-c", helper, str(process.pid)],
        env=_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert signaler.returncode == 0, "Could not send CTRL_BREAK_EVENT to the CLI console"
    stdout, stderr = process.communicate(timeout=20)
    # Uvicorn re-raises SIGBREAK after cleanup, so its exit status can be nonzero.
    assert "Application shutdown complete." in stderr
    return stdout + stderr


def _bootstrap(client, origin, code):
    response = client.post(
        "/api/bootstrap", headers={"Origin": origin}, json={"code": code.get_secret_value()}
    )
    assert response.status_code == 200
    assert response.json()["csrf_token"]
    assert client.get("/api/session").status_code == 200
    return response.json()["csrf_token"]


def test_actual_cli_bootstrap_lock_graceful_restart_and_session_invalidation(tmp_path):
    path = tmp_path / "config.json"
    home = tmp_path / "data"
    port = _free_port()
    create_config(
        path,
        AppConfig(home=str(home), actor_id="owner", project_ids=["sales"], port=port),
    )
    origin = f"http://127.0.0.1:{port}"
    processes = []
    with httpx.Client(base_url=origin, trust_env=False, timeout=1) as client:
        try:
            first = _launch(path)
            processes.append(first)
            _ready(first, client)
            first_code = _access_code(path)
            _bootstrap(client, origin, first_code)

            # Another port distinguishes the shared-home lock from port binding.
            second_path = tmp_path / "second-config.json"
            create_config(
                second_path,
                AppConfig(home=str(home), project_ids=["sales"], port=_free_port()),
            )
            duplicate = _launch(second_path)
            processes.append(duplicate)
            _, duplicate_stderr = duplicate.communicate(timeout=20)
            assert duplicate.returncode != 0
            assert "이 데이터 폴더의 서버가 이미 실행 중입니다." in duplicate_stderr
            assert client.get("/api/health").status_code == 200

            first_logs = _graceful_stop(first)
            secret_in_logs = first_code.get_secret_value() in first_logs
            assert not secret_in_logs
            with DaemonLock(home / "daemon.lock"):
                pass

            stopped_code = _launch(path, access_code=True)
            processes.append(stopped_code)
            stopped_stdout, _ = stopped_code.communicate(timeout=15)
            assert stopped_code.returncode != 0
            leaked_code = first_code.get_secret_value() in stopped_stdout
            assert not leaked_code

            restarted = _launch(path)
            processes.append(restarted)
            _ready(restarted, client)
            assert client.get("/api/session").status_code == 401
            second_code = _access_code(path)
            assert first_code != second_code
            _bootstrap(client, origin, second_code)
            second_logs = _graceful_stop(restarted)
            secret_in_logs = second_code.get_secret_value() in second_logs
            assert not secret_in_logs
            with DaemonLock(home / "daemon.lock"):
                pass
        finally:
            # Forceful termination is cleanup only, never the successful stop path.
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)


def test_actual_daemon_once_event_completes_csv_to_xlsx_without_duplicate(tmp_path):
    from openpyxl import load_workbook

    path = tmp_path / "config.json"
    home = tmp_path / "data"
    port = _free_port()
    create_config(
        path,
        AppConfig(home=str(home), actor_id="owner", project_ids=["sales"], port=port),
    )
    csv = "부서,금액\n영업,1\n지원,2\n".encode()
    origin = f"http://127.0.0.1:{port}"
    process = _launch(path)
    with httpx.Client(base_url=origin, trust_env=False, timeout=30) as client:
        try:
            _ready(process, client)
            code = _access_code(path)
            csrf = _bootstrap(client, origin, code)
            client.headers.update({"Origin": origin, "X-CSRF-Token": csrf})
            uploaded = client.post(
                "/api/artifacts",
                params={"project_id": "sales", "filename": "synthetic-sales.csv"},
                content=csv,
                headers={"Content-Type": "text/csv"},
            )
            assert uploaded.status_code == 201
            source = uploaded.json()["artifact"]
            assert source["sha256"] == hashlib.sha256(csv).hexdigest()

            scheduled_at = datetime.now(UTC) + timedelta(seconds=2)
            created = client.post(
                "/api/automations",
                json={
                    "name": "실제 시계 합계 검증",
                    "project_id": "sales",
                    "recipe": "aggregate",
                    "instruction": "금액 합계 계산해줘",
                    "source_scope": {"artifact_ids": [source["id"]]},
                    "output_format": "xlsx",
                    "trigger": {"kind": "once", "at": scheduled_at.isoformat()},
                },
            )
            assert created.status_code == 201
            preview = created.json()
            bot = preview["automation"]
            assert bot["enabled"] == 0
            enabled = client.patch(
                "/api/automations/" + bot["id"],
                json={
                    "expected_revision": bot["revision"],
                    "enabled": True,
                    "confirmed_hash": preview["proposal_hash"],
                },
            )
            assert enabled.status_code == 200
            active = enabled.json()["automation"]
            assert active["enabled"] == 1

            # No injected clock/tick/worker: the CLI's real 15-second scheduler
            # must create the run and its normal worker must finish every step.
            deadline = time.monotonic() + 120
            detail = None
            state = "no_run"
            while time.monotonic() < deadline:
                assert process.poll() is None, "CLI stopped before its scheduled work finished"
                listed = client.get("/api/runs", params={"project_id": "sales"})
                assert listed.status_code == 200
                runs = listed.json()["runs"]
                assert len(runs) <= 1, "A once automation created duplicate runs"
                if runs:
                    run = runs[0]
                    assert run["source"] == "automation" and run["automation_id"] == bot["id"]
                    state = run["state"]
                    if state == "succeeded":
                        response = client.get("/api/runs/" + run["id"])
                        assert response.status_code == 200
                        detail = response.json()
                        break
                    assert state not in {
                        "failed",
                        "cancelled",
                        "rejected",
                        "expired",
                        "outcome_unknown",
                        "awaiting_input",
                        "awaiting_approval",
                    }, f"Scheduled D work unexpectedly reached {state}"
                time.sleep(0.2)
            assert detail is not None, f"Scheduled work timed out in state {state}"
            receipts = [
                value["receipt"]
                for value in detail["run"]["result_json"]["outputs"].values()
                if "receipt" in value
            ]
            assert len(receipts) == 1
            receipt = receipts[0]
            assert receipt["run_id"] == run["id"]
            assert receipt["validation"]["status"] == "pass"
            assert all(step["state"] == "succeeded" for step in detail["steps"])
            assert detail["calls"] == []
            assert detail["usage"]["total_tokens"] == 0
            assert detail["usage"]["usage_state"] == "not_used"
            downloaded = client.get(f"/api/artifacts/{receipt['artifact_id']}/download")
            assert downloaded.status_code == 200
            assert hashlib.sha256(downloaded.content).hexdigest() == receipt["artifact_hash"]
            workbook = load_workbook(io.BytesIO(downloaded.content), data_only=True)
            try:
                assert list(workbook["Result"].values) == [("그룹", "금액"), ("전체", "3.00")]
            finally:
                workbook.close()
            original = client.get(f"/api/artifacts/{source['id']}/download")
            assert original.status_code == 200 and original.content == csv

            # Leave the live daemon up for another full scheduler interval.
            # Repeated real clock observations must not enqueue or regenerate.
            time.sleep(16)
            listed = client.get("/api/runs", params={"project_id": "sales"})
            assert [item["id"] for item in listed.json()["runs"]] == [run["id"]]
            after = client.get("/api/runs/" + run["id"]).json()
            assert after["steps"] == detail["steps"]
            assert after["run"]["result_json"] == detail["run"]["result_json"]
            assert after["artifacts"] == detail["artifacts"]
            assert after["calls"] == []
            logs = _graceful_stop(process)
            secret_in_logs = code.get_secret_value() in logs
            assert not secret_in_logs
            with DaemonLock(home / "daemon.lock"):
                pass
            with closing(
                sqlite3.connect((home / "state.db").as_uri() + "?mode=ro", uri=True)
            ) as conn:
                assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
                assert conn.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
                events = conn.execute(
                    "SELECT connection_id,run_id,payload_json,received_at FROM inbox_events "
                    "WHERE source='automation' AND state='enqueued'"
                ).fetchall()
                assert len(events) == 1
                connection_id, event_run_id, payload, received_at = events[0]
                event = json.loads(payload)
                assert (connection_id, event_run_id) == (bot["id"], run["id"])
                assert event["automation_revision"] == active["revision"]
                assert event["source_ids"] == [source["id"]]
                assert datetime.fromisoformat(event["scheduled_utc"]) == scheduled_at
                assert datetime.fromisoformat(received_at) >= scheduled_at
                assert conn.execute("SELECT count(*) FROM inbox_events").fetchone()[0] == 1
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
