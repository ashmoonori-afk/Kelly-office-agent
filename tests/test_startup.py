"""Task Scheduler is a stubbed tool boundary; hidden serve/HTTP stop run for real."""

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_cli import ROOT, _environment, _free_port

from birkin_lite import config, engine, startup
from birkin_lite.api import create_app
from birkin_lite.contracts import AppError


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "config.json"
    config.create_config(path, config.AppConfig(home="data", port=_free_port()))
    return path, tmp_path / "data"


def test_registration_tool_commands_are_explicit_current_user_and_exact_scope(workspace):
    path, home = workspace
    commands = []

    def task_tool(script):
        commands.append(script)
        return {"state": "enabled" if "Register-ScheduledTask -" in script else "disabled"}

    status = startup.manage(path, "status", task_runner=task_tool)
    assert status["state"] == "disabled"
    assert "Register-ScheduledTask -" not in commands[-1]
    assert "Unregister-ScheduledTask -" not in commands[-1]
    assert not home.exists()
    enabled = startup.manage(path, "enable", task_runner=task_tool)
    command = commands[-1]
    assert enabled["state"] == "enabled" and enabled["task_name"] == status["task_name"]
    for required in (
        "WindowsIdentity]::GetCurrent().User.Value",
        "-AtLogOn -User $owner",
        "-LogonType Interactive -RunLevel Limited",
        "-MultipleInstances IgnoreNew",
        "-ExecutionTimeLimit ([TimeSpan]::Zero)",
        "pythonw.exe",
        "birkin_lite.startup",
        "$task.Principal.UserId -ne $owner",
        "$task.Actions[0].Arguments -ne $arguments",
        "$task.Actions[0].Execute -ne $execute",
        "$task.Description -ne $description",
    ):
        assert required in command
    assert "Start-ScheduledTask" not in command
    assert "-Password" not in command and "-RunLevel Highest" not in command
    disabled = startup.manage(path, "disable", task_runner=task_tool)
    assert disabled["state"] == "disabled"
    assert "Unregister-ScheduledTask -TaskPath '\\' -TaskName $name" in commands[-1]
    assert "Stop-ScheduledTask" not in commands[-1]
    with pytest.raises(AppError) as error:
        startup.manage(path, "enable", task_runner=lambda _: {"state": "conflict"})
    assert error.value.code == "CONFLICT"
    assert startup._literal("a' ; $(anything)") == "'a'' ; $(anything)'"


def test_stop_endpoint_rejects_unauthenticated_requests_and_unconfigured_host(workspace):
    path, _ = workspace
    origin = "http://127.0.0.1:" + str(config.load_config(path).port)
    app = create_app(path, start_background=False)
    with TestClient(app, base_url=origin) as client:
        assert (
            client.post("/api/server/stop", json={}, headers={"Origin": origin}).status_code == 401
        )
        boot = client.post(
            "/api/bootstrap", json={"code": app.state.bootstrap_secret}, headers={"Origin": origin}
        )
        assert boot.status_code == 200
        assert (
            client.post("/api/server/stop", json={}, headers={"Origin": origin}).status_code == 403
        )
        response = client.post(
            "/api/server/stop",
            json={},
            headers={"Origin": origin, "X-CSRF-Token": boot.json()["csrf_token"]},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_registration_timeout_reports_unknown_without_command_or_path(monkeypatch, workspace):
    def timed_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 30, output="private-path-and-command")

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(AppError) as error:
        startup.manage(workspace[0], "enable")
    assert error.value.code == "OUTCOME_UNKNOWN"
    assert "private-path-and-command" not in str(error.value)


def test_stop_timeout_preserves_live_lock_and_reports_unknown(workspace):
    path, home = workspace
    home.mkdir()
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.path == "/api/bootstrap":
            return httpx.Response(200, json={"csrf_token": "synthetic-csrf"})
        return httpx.Response(202, json={"state": "stopping"})

    with engine.DaemonLock(home / "daemon.lock"):
        result = startup.stop(path, timeout=0, http_transport=httpx.MockTransport(transport))
        assert result == {"state": "unknown", "reason": "shutdown_timeout"}
        with pytest.raises(AppError) as error, engine.DaemonLock(home / "daemon.lock"):
            pass
        assert error.value.code == "ALREADY_RUNNING"
    assert [request.url.path for request in calls] == ["/api/bootstrap", "/api/server/stop"]
    assert calls[1].headers["X-CSRF-Token"] == "synthetic-csrf"
    assert all(request.url.host == "127.0.0.1" for request in calls)


def test_actual_pythonw_hidden_server_authenticates_stop_and_flushes_logs(workspace):
    path, home = workspace
    port = config.load_config(path).port
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    process = subprocess.Popen(
        [str(pythonw), "-m", "birkin_lite.startup", "--run", str(path)],
        cwd=ROOT,
        env=_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=1
        ) as client:
            deadline = time.monotonic() + 30
            healthy = False
            while time.monotonic() < deadline:
                assert process.poll() is None, "pythonw exited before health became ready"
                try:
                    healthy = client.get("/api/health").json() == {"ok": True}
                    if healthy:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(0.1)
            assert healthy
            rejected = client.post(
                "/api/server/stop", headers={"Origin": f"http://127.0.0.1:{port}"}, json={}
            )
            assert rejected.status_code == 401 and process.poll() is None
        stopped = subprocess.run(
            [sys.executable, "-m", "birkin_lite", "stop", "--config", str(path)],
            cwd=ROOT,
            env=_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert stopped.returncode == 0
        assert json.loads(stopped.stdout) == {"state": "stopped"}
        assert process.wait(timeout=10) == 0
        logs = (home / "logs" / "server.stderr.log").read_text(encoding="utf-8")
        assert "Application shutdown complete." in logs
        assert "Traceback" not in logs
        assert "kelly serve --access-code" in (home / "logs" / "server.stdout.log").read_text(
            encoding="utf-8"
        )
        with engine.DaemonLock(home / "daemon.lock"):
            pass
    finally:
        if process.poll() is None:
            # Cleanup uses the same authenticated stop; never infer/kill another PID.
            startup.stop(path)
            process.wait(timeout=10)
