"""Explicit current-user logon registration and authenticated, graceful local stop."""

import base64
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from pathlib import Path

import httpx
from pydantic import SecretStr

from . import config, operations
from ._vendor.office import windows_native
from .contracts import AppError
from .safe_io import ensure_directory, guard_directory, validate_root


def _literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _task_spec(config_path):
    path = Path(config_path).absolute()
    settings = config.load_config(path)
    home = config.home_path(settings, path)
    executable = Path(sys.executable).with_name("pythonw.exe")
    validate_root(executable.parent)
    with operations._identity_guard(executable):
        pass
    identity = hashlib.sha256(str(home).casefold().encode()).hexdigest()[:20]
    return {
        "name": "KellyOfficeAgent-" + identity,
        "description": "Kelly Office Agent current-user startup " + identity,
        "execute": str(executable),
        "arguments": subprocess.list2cmdline(["-m", "birkin_lite.startup", "--run", str(path)]),
        "working_directory": str(path.parent),
    }


def _powershell(spec, action):
    fields = "\n".join(f"${key}={_literal(value)}" for key, value in spec.items())
    # All dynamic values are single-quoted literals; no user data becomes code.
    body = r"""
$ErrorActionPreference='Stop'
$owner=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
try { $task=Get-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction Stop }
catch {
  if ($_.CategoryInfo.Category -ne [Management.Automation.ErrorCategory]::ObjectNotFound) { throw }
  $task=$null
}
if ($task -and ($task.Description -ne $description -or $task.Principal.UserId -ne $owner -or @($task.Actions).Count -ne 1 -or $task.Actions[0].Execute -ne $execute -or $task.Actions[0].Arguments -ne $arguments -or $task.Actions[0].WorkingDirectory -ne $working_directory)) {
  @{state='conflict'} | ConvertTo-Json -Compress
  exit 0
}
"""
    if action == "enable":
        body += r"""
$entry=New-ScheduledTaskAction -Execute $execute -Argument $arguments -WorkingDirectory $working_directory
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $owner
$principal=New-ScheduledTaskPrincipal -UserId $owner -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskPath '\' -TaskName $name -Description $description -Action $entry -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
@{state='enabled'} | ConvertTo-Json -Compress
"""
    elif action == "disable":
        body += r"""
if ($task) { Unregister-ScheduledTask -TaskPath '\' -TaskName $name -Confirm:$false }
@{state='disabled'} | ConvertTo-Json -Compress
"""
    else:
        body += r"""
@{state=$(if ($task -and $task.Settings.Enabled) {'enabled'} else {'disabled'})} | ConvertTo-Json -Compress
"""
    return fields + body


def _run_task_tool(script):
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        base64.b64encode(script.encode("utf-16-le")).decode(),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise AppError("OUTCOME_UNKNOWN") from None
    if result.returncode:
        raise AppError("POLICY_DENIED")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise AppError("INVALID_INPUT") from None


def manage(config_path, action, *, task_runner=None):
    """Registration occurs only for an explicit enable call, never during install/serve."""
    if action not in {"enable", "disable", "status"}:
        raise AppError("INVALID_INPUT")
    spec = _task_spec(config_path)
    result = (task_runner or _run_task_tool)(_powershell(spec, action))
    if result == {"state": "conflict"}:
        raise AppError("CONFLICT", status=409)
    if (
        not isinstance(result, dict)
        or set(result) != {"state"}
        or result["state"] not in {"enabled", "disabled"}
    ):
        raise AppError("INVALID_INPUT")
    return {"task_name": spec["name"], **result}


@contextmanager
def _log_stream(path):
    import msvcrt

    native = windows_native.api()
    handle = windows_native.open_handle(
        path,
        directory=False,
        access=native.GENERIC_READ | 0x40000000,
        share=native.FILE_SHARE_READ,
        disposition=4,
    )  # OPEN_ALWAYS
    try:
        if windows_native.handle_information(handle).nNumberOfLinks != 1:
            raise OSError("linked logs are refused")
        descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_APPEND | os.O_BINARY)
    except BaseException:
        windows_native.close_handle(handle)
        raise
    with os.fdopen(descriptor, "a", encoding="utf-8", buffering=1) as stream:
        yield stream


def _prune_logs(logs, kind, current, now):
    """Delete only expired app logs, through an exclusive non-link handle."""
    import ctypes
    from ctypes import wintypes

    cutoff = now - timedelta(days=30)
    native = windows_native.api()
    dispose = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
    dispose.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    dispose.restype = wintypes.BOOL
    with guard_directory(logs):
        for path in logs.iterdir():
            match = re.fullmatch(rf"server\.{kind}(?:\.(\d{{4}}-\d{{2}}-\d{{2}}))?\.log", path.name)
            if not match or path == current:
                continue
            try:
                if (
                    match[1]
                    and datetime.combine(
                        date.fromisoformat(match[1]) + timedelta(days=1), day_time(), UTC
                    )
                    >= cutoff
                ):
                    continue
                handle = windows_native.open_handle(
                    path, directory=False, access=native.GENERIC_READ | 0x00010000, share=0
                )  # DELETE access; any open reader/writer prevents this exclusive claim.
            except (OSError, ValueError, OverflowError):
                continue
            try:
                info = windows_native.handle_information(handle)
                stamp = (
                    info.ftLastWriteTime.dwHighDateTime << 32
                ) | info.ftLastWriteTime.dwLowDateTime
                if (
                    info.nNumberOfLinks != 1
                    or stamp / 10_000_000 - 11_644_473_600 >= cutoff.timestamp()
                ):
                    continue
                remove = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile, class 4.
                if not dispose(handle, 4, ctypes.byref(remove), ctypes.sizeof(remove)):
                    continue  # A locked/read-only file stays for a later maintenance pass.
            finally:
                windows_native.close_handle(handle)


class _DailyLog(io.TextIOBase):
    """UTF-8 append stream; backwards wall clocks never rotate or expire files."""

    encoding = "utf-8"

    def __init__(self, logs, kind, clock):
        self.logs, self.kind, self.clock = logs, kind, clock
        self._lock = threading.RLock()
        self._opened = self._stream = self._day = self._latest = self._cleaned_at = None
        self.maintain()

    def maintain(self):
        now = self.clock().astimezone(UTC)
        with self._lock, guard_directory(self.logs):
            if self.closed or self._latest is not None and now < self._latest:
                return
            self._latest = now
            if self._day is None or now.date() > self._day:
                path = self.logs / f"server.{self.kind}.{now.date().isoformat()}.log"
                opened = _log_stream(path)
                try:
                    stream = opened.__enter__()
                except OSError:
                    if self._stream is None:
                        raise
                    return  # Keep the current stream usable; retry rotation next minute/write.
                if self._opened is not None:
                    self._opened.__exit__(None, None, None)
                self._opened, self._stream, self._day, self._path = opened, stream, now.date(), path
            if self._cleaned_at is None or now - self._cleaned_at >= timedelta(seconds=60):
                _prune_logs(self.logs, self.kind, self._path, now)
                self._cleaned_at = now

    def write(self, value):
        self.maintain()
        with self._lock:
            return self._stream.write(value)

    def flush(self):
        with self._lock:
            if not self.closed and self._stream is not None:
                self._stream.flush()

    def writable(self):
        return True

    def close(self):
        with self._lock:
            if not self.closed:
                super().close()
                if self._opened is not None:
                    self._opened.__exit__(None, None, None)


@contextmanager
def _log_streams(logs, *, clock=lambda: datetime.now(UTC), maintenance_seconds=60):
    with guard_directory(logs), ExitStack() as stack:
        streams = [
            stack.enter_context(_DailyLog(logs, kind, clock)) for kind in ("stdout", "stderr")
        ]
        stop = threading.Event()

        def maintain():
            while not stop.wait(maintenance_seconds):
                for stream in streams:
                    try:
                        stream.maintain()
                    except (OSError, AppError):
                        pass  # No deletion on failure; retry without logging private exception text.

        worker = threading.Thread(target=maintain, name="kelly-log-maintenance", daemon=True)
        worker.start()
        try:
            yield streams
        finally:
            stop.set()
            worker.join()


def run_hidden(config_path):
    path = Path(config_path).absolute()
    settings = config.load_config(path)
    home = config.home_path(settings, path)
    logs = ensure_directory(home / "logs")
    with _log_streams(logs) as (stdout, stderr), ExitStack() as stack:
        stack.enter_context(redirect_stdout(stdout))
        stack.enter_context(redirect_stderr(stderr))
        # pythonw has no console streams. Install real streams before uvicorn imports.
        from .__main__ import main

        return main(["serve", "--config", str(path)])


def stop(config_path, *, timeout=35, http_transport=None):
    """A timeout means unknown. Never signal or terminate an arbitrary PID."""
    path = Path(config_path).absolute()
    settings = config.load_config(path)
    home = config.home_path(settings, path)
    if not home.exists():
        return {"state": "stopped"}

    def stopped():
        try:
            with operations._stopped(home):
                return True
        except AppError as error:
            if error.code != "ALREADY_RUNNING":
                raise
            return False

    if stopped():
        return {"state": "stopped"}
    origin = f"http://127.0.0.1:{settings.port}"
    code = secrets.token_urlsafe(32)
    config.store_secret(home, "bootstrap", SecretStr(code))
    deadline = time.monotonic() + timeout
    with httpx.Client(
        base_url=origin, timeout=5, trust_env=False, transport=http_transport
    ) as client:
        try:
            bootstrap = client.post(
                "/api/bootstrap", headers={"Origin": origin}, json={"code": code}
            )
            if bootstrap.status_code != 200:
                raise AppError("AUTH_REQUIRED")
            body = bootstrap.json()
            csrf = body.get("csrf_token") if isinstance(body, dict) else None
            if not isinstance(csrf, str) or not csrf:
                raise AppError("AUTH_REQUIRED")
            response = client.post(
                "/api/server/stop", headers={"Origin": origin, "X-CSRF-Token": csrf}, json={}
            )
            if response.status_code != 202:
                raise AppError("POLICY_DENIED")
        except httpx.RequestError:
            pass  # The stop may have reached the server; observe its OS lock below.
    while time.monotonic() < deadline:
        if stopped():
            return {"state": "stopped"}
        time.sleep(0.1)
    return {"state": "stopped"} if stopped() else {"state": "unknown", "reason": "shutdown_timeout"}


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--run":
        raise SystemExit(2)
    raise SystemExit(run_hidden(sys.argv[2]))
