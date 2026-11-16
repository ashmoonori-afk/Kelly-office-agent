"""Task Scheduler is a stubbed tool boundary; hidden serve/HTTP stop run for real."""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_cli import ROOT, _environment, _free_port

from kelly import config, engine, startup
from kelly.api import create_app
from kelly.contracts import AppError
from kelly.platform import _windows_service


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "config.json"
    config.create_config(path, config.AppConfig(home="data", port=_free_port()))
    return path, tmp_path / "data"


def test_registration_tool_commands_are_explicit_current_user_and_exact_scope(workspace, monkeypatch):
    path, home = workspace
    # Exercise the unchanged Windows backend on either host with a synthetic executable.
    if os.name != "nt":
        (path.parent / "pythonw.exe").write_bytes(b"synthetic executable")
        monkeypatch.setattr(sys, "executable", str(path.parent / "python.exe"))
    commands = []

    def task_tool(script):
        commands.append(script)
        return {"state": "enabled" if "Register-ScheduledTask -" in script else "disabled"}

    status = _windows_service.manage(path, "status", task_runner=task_tool)
    assert status["state"] == "disabled"
    assert "Register-ScheduledTask -" not in commands[-1]
    assert "Unregister-ScheduledTask -" not in commands[-1]
    assert not home.exists()
    enabled = _windows_service.manage(path, "enable", task_runner=task_tool)
    command = commands[-1]
    assert enabled["state"] == "enabled" and enabled["task_name"] == status["task_name"]
    for required in (
        "WindowsIdentity]::GetCurrent().User.Value",
        "-AtLogOn -User $owner",
        "-LogonType Interactive -RunLevel Limited",
        "-MultipleInstances IgnoreNew",
        "-ExecutionTimeLimit ([TimeSpan]::Zero)",
        "pythonw.exe",
        "kelly.startup",
        "$task.Principal.UserId -ne $owner",
        "$task.Actions[0].Arguments -ne $arguments",
        "$task.Actions[0].Execute -ne $execute",
        "$task.Description -ne $description",
    ):
        assert required in command
    assert "Start-ScheduledTask" not in command
    assert "-Password" not in command and "-RunLevel Highest" not in command
    disabled = _windows_service.manage(path, "disable", task_runner=task_tool)
    assert disabled["state"] == "disabled"
    assert "Unregister-ScheduledTask -TaskPath '\\' -TaskName $name" in commands[-1]
    assert "Stop-ScheduledTask" not in commands[-1]
    with pytest.raises(AppError) as error:
        _windows_service.manage(path, "enable", task_runner=lambda _: {"state": "conflict"})
    assert error.value.code == "CONFLICT"
    assert _windows_service._literal("a' ; $(anything)") == "'a'' ; $(anything)'"


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
    monkeypatch.setattr(Path, "home", lambda: workspace[0].parent)

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
    pythonw = Path(sys.executable).with_name("pythonw.exe") if os.name == "nt" else Path(sys.executable)
    process = subprocess.Popen(
        [str(pythonw), "-m", "kelly.startup", "--run", str(path)],
        cwd=ROOT,
        env=_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
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
            [sys.executable, "-m", "kelly", "stop", "--config", str(path)],
            cwd=ROOT,
            env=_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert stopped.returncode == 0
        assert json.loads(stopped.stdout) == {"state": "stopped"}
        assert process.wait(timeout=10) == 0
        logs = "".join(
            p.read_text(encoding="utf-8") for p in (home / "logs").glob("server.stderr.*.log")
        )
        assert "Application shutdown complete." in logs
        assert "Traceback" not in logs
        assert "kelly serve --access-code" in "".join(
            p.read_text(encoding="utf-8") for p in (home / "logs").glob("server.stdout.*.log")
        )
        with engine.DaemonLock(home / "daemon.lock"):
            pass
    finally:
        if process.poll() is None:
            # Cleanup uses the same authenticated stop; never infer/kill another PID.
            startup.stop(path)
            process.wait(timeout=10)


def test_actual_daily_log_thread_rotates_idle_stream_and_respects_thirty_days(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    cutoff = now - timedelta(days=30)
    contents = {
        "server.stdout.2026-08-08.log": "expired dated",
        "server.stderr.log": "expired legacy",
        "server.stdout.log": "legacy exact boundary",
        "server.stderr.2026-08-09.log": "thirty-day period still overlaps",
        "server.stdout.2027-01-01.log": "future file",
        "notes.log": "unrelated",
        "server.stdout.2026-02-30.log": "invalid date",
    }
    for name, text in contents.items():
        path = logs / name
        path.write_text(text, encoding="utf-8")
        stamp = cutoff.timestamp() if name == "server.stdout.log" else cutoff.timestamp() - 1
        os.utime(path, (stamp, stamp))
    with startup._log_streams(logs, clock=lambda: now, maintenance_seconds=0.01) as (out, err):
        out.write("first day\n")
        assert not (logs / "server.stdout.2026-08-08.log").exists()
        assert not (logs / "server.stderr.log").exists()
        for name in set(contents) - {"server.stdout.2026-08-08.log", "server.stderr.log"}:
            assert (logs / name).read_text(encoding="utf-8") == contents[name]
        first = logs / "server.stdout.2026-09-08.log"
        now += timedelta(days=1)
        rotated = logs / "server.stdout.2026-09-09.log"
        deadline = time.monotonic() + 3
        while not rotated.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert rotated.exists()  # Real maintenance thread rotates even with no new writes.
        out.write("second day\n")
        err.write("safe diagnostic\n")
        assert first.read_text(encoding="utf-8") == "first day\n"
        assert rotated.read_text(encoding="utf-8") == "second day\n"
    assert out.closed and err.closed


def test_log_clock_rollback_keeps_active_stream_and_defers_expiry(tmp_path):
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    with startup._log_streams(tmp_path, clock=lambda: now) as (out, _):
        first = out._path
        original = now
        now -= timedelta(days=50)
        out.write("rollback\n")
        assert out._path == first and out._latest == original
        pending = tmp_path / "server.stdout.2026-01-01.log"
        pending.write_text("keep until clock recovers", encoding="utf-8")
        os.utime(pending, (0, 0))
        out.maintain()
        assert pending.exists()
        now = original + timedelta(minutes=1)
        out.maintain()
        assert not pending.exists()
        assert first.read_text(encoding="utf-8") == "rollback\n"


def test_log_expiry_preserves_open_files_hardlinks_and_junctions(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    source = outside / "private.txt"
    source.write_text("unrelated data", encoding="utf-8")
    os.utime(source, (0, 0))
    alias = logs / "server.stdout.2026-01-01.log"
    os.link(source, alias)
    junction = logs / "server.stdout.2026-01-02.log"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert created.returncode == 0
    else:
        junction.symlink_to(outside, target_is_directory=True)
    busy = logs / "server.stdout.log"
    busy.write_text("older daemon", encoding="utf-8")
    os.utime(busy, (0, 0))
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    with startup._log_stream(busy), startup._log_streams(logs, clock=lambda: now) as (out, _):
        assert busy.exists() and alias.exists() and junction.is_dir()
        out.write("current daemon\n")
    with startup._log_streams(logs, clock=lambda: now):
        assert not busy.exists()  # Only after the older stream closes.
    assert source.read_text(encoding="utf-8") == "unrelated data"
    assert alias.exists() and junction.is_dir()


def test_log_open_rejects_hardlinked_current_filename(tmp_path):
    now = datetime(2026, 9, 8, tzinfo=UTC)
    original = tmp_path / "original.txt"
    original.write_text("do not append", encoding="utf-8")
    os.link(original, tmp_path / "server.stdout.2026-09-08.log")
    with pytest.raises(OSError), startup._log_streams(tmp_path, clock=lambda: now):
        pass
    assert original.read_text(encoding="utf-8") == "do not append"
