"""Explicit current-user logon registration and authenticated, graceful local stop."""

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
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
        descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_APPEND | os.O_BINARY)
    except BaseException:
        windows_native.close_handle(handle)
        raise
    with os.fdopen(descriptor, "a", encoding="utf-8", buffering=1) as stream:
        yield stream


def run_hidden(config_path):
    path = Path(config_path).absolute()
    settings = config.load_config(path)
    home = config.home_path(settings, path)
    logs = ensure_directory(home / "logs")
    with guard_directory(logs), ExitStack() as stack:
        stdout = stack.enter_context(_log_stream(logs / "server.stdout.log"))
        stderr = stack.enter_context(_log_stream(logs / "server.stderr.log"))
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
