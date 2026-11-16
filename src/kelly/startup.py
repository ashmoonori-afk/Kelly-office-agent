"""Explicit current-user logon registration and authenticated, graceful local stop."""

import io
import secrets
import sys
import threading
import time
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import SecretStr

from . import config, operations
from .contracts import AppError
from .platform import service
from .safe_io import ensure_directory, guard_directory

manage = service.manage
_log_stream = service._log_stream
_prune_logs = service._prune_logs


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
