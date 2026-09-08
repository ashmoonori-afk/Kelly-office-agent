"""One bounded parser process; its stdin gate opens only after OS containment."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .contracts import AppError

MAX_RESULT_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024


def _worker_environment():
    # Explicit allowlist: no provider keys, Telegram tokens, user HOME or PYTHONPATH.
    return {
        key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ
    }


def _run_worker(command, request, *, timeout_s, memory_mb):
    started = time.monotonic()
    payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_REQUEST_BYTES:
        raise AppError("LIMIT_EXCEEDED")
    process = None
    job = None
    output = bytearray()
    oversized = threading.Event()
    read_failure = threading.Event()
    assigned = False

    def stop():
        if job is not None and assigned:
            job.terminate()
        elif process is not None and process.poll() is None:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()

    def read_output():
        try:
            while chunk := process.stdout.read(64 * 1024):
                if len(output) + len(chunk) > MAX_RESULT_BYTES:
                    oversized.set()
                    stop()
                    return
                output.extend(chunk)
        except OSError:
            read_failure.set()

    try:
        if os.name == "nt":
            from ._vendor.office._winjob import WindowsJob

            job = WindowsJob.create(memory_mb * 1024 * 1024)
        elif os.name != "posix":
            raise AppError("UNSUPPORTED_CAPABILITY")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_worker_environment(),
            start_new_session=os.name == "posix",
            # Suspend the venv launcher so it cannot spawn outside the Job before assignment.
            creationflags=(subprocess.CREATE_NO_WINDOW | 0x4) if os.name == "nt" else 0,
        )
        if job is not None:
            job.assign(process.pid)
            if not job.contains(process.pid):
                raise AppError("UNSUPPORTED_CAPABILITY")
            assigned = True
            job.resume(process.pid)
        # Worker cannot parse a file before this write. Assignment failure remains fail-closed.
        thread = threading.Thread(target=read_output, daemon=True)
        thread.start()
        process.stdin.write(payload)
        process.stdin.close()
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            stop()
            process.wait(timeout=5)
            raise AppError("PARSER_TIMEOUT") from None
        thread.join(timeout=5)
        if thread.is_alive():
            stop()
            raise AppError("PARSER_FAILED")
        if oversized.is_set():
            raise AppError("LIMIT_EXCEEDED")
        if read_failure.is_set() or process.returncode != 0:
            raise AppError("PARSER_FAILED")
        try:
            response = json.loads(output)
        except (UnicodeError, ValueError):
            raise AppError("PARSER_FAILED") from None
        if not isinstance(response, dict):
            raise AppError("PARSER_FAILED")
        if response.get("error"):
            raise AppError(
                response["error"] if isinstance(response["error"], str) else "PARSER_FAILED"
            )
        # Only parser responses receive resource receipts; diagnostic workers preserve their output.
        if "format" in response:
            response["sandbox"] = {
                "memory_limit_bytes": memory_mb * 1024 * 1024,
                "peak_committed_bytes": job.peak_memory_bytes() if job is not None else None,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        return response
    except AppError:
        raise
    except (OSError, ValueError):
        raise AppError("UNSUPPORTED_CAPABILITY") from None
    finally:
        if process is not None:
            stop()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
        if job is not None:
            job.close()


def parse_file(
    path: Path, filename: str, operation="preflight", args=None, timeout_s=30, memory_mb=512
) -> dict:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or not isinstance(filename, str)
        or not filename
        or len(filename) > 240
        or any(char in filename for char in "/\\:\x00")
        or not isinstance(operation, str)
        or len(operation) > 64
        or (args is not None and not isinstance(args, dict))
        or type(timeout_s) not in (int, float)
        or not 0 < timeout_s <= 30
        or type(memory_mb) is not int
        or not 32 <= memory_mb <= 512
    ):
        raise AppError("INVALID_INPUT")
    worker = Path(__file__).with_name("parser_worker.py")
    return _run_worker(
        [sys.executable, "-I", str(worker), str(memory_mb)],
        {"path": str(path), "filename": filename, "operation": operation, "args": args or {}},
        timeout_s=timeout_s,
        memory_mb=memory_mb,
    )
