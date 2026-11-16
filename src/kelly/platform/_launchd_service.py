"""LaunchAgent registration and descriptor-relative POSIX server logs."""

import hashlib
import os
import plistlib
import re
import stat
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from pathlib import Path

from .. import config
from ..contracts import AppError


def _run_launchctl(arguments):
    try:
        return subprocess.run(
            ["/bin/launchctl", *arguments], capture_output=True, check=False, timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise AppError("OUTCOME_UNKNOWN") from None
    except OSError:
        raise AppError("UNSUPPORTED_CAPABILITY") from None


def manage(config_path, action, *, task_runner=None):
    """Register only the exact Kelly plist; foreign definitions are conflicts."""
    from . import _posix_io

    if action not in {"enable", "disable", "status"}:
        raise AppError("INVALID_INPUT")
    path = Path(config_path).absolute()
    settings = config.load_config(path)
    home = config.home_path(settings, path)
    identity = hashlib.sha256(str(home).encode()).hexdigest()[:20]
    label = "org.kelly.office-agent." + identity
    folder = Path.home() / "Library" / "LaunchAgents"
    plist = folder / (label + ".plist")
    definition = {
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "kelly.startup", "--run", str(path)],
        "WorkingDirectory": str(path.parent),
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LimitLoadToSessionType": "Aqua",
    }
    if plist.exists() or plist.is_symlink():
        with _posix_io.open_regular(plist, folder) as stream:
            try:
                current = plistlib.load(stream)
            except (ValueError, plistlib.InvalidFileException):
                raise AppError("CONFLICT", status=409) from None
        if current != definition:
            raise AppError("CONFLICT", status=409)
    run = task_runner or _run_launchctl
    domain = f"gui/{os.getuid()}"
    target = domain + "/" + label
    result = run(["print", target])
    if result.returncode not in {0, 113}:
        raise AppError("POLICY_DENIED")
    loaded = result.returncode == 0
    if loaded and not plist.exists():
        raise AppError("CONFLICT", status=409)
    if action == "enable":
        _posix_io.ensure_directory(folder)
        if not plist.exists():
            with _posix_io.open_new(plist) as stream:
                plistlib.dump(definition, stream)
        if not loaded and run(["bootstrap", domain, str(plist)]).returncode:
            raise AppError("POLICY_DENIED")
        loaded = True
    elif action == "disable":
        if loaded and run(["bootout", target]).returncode:
            raise AppError("POLICY_DENIED")
        if plist.exists():
            _posix_io.unlink_file(plist)
        loaded = False
    return {"task_name": label, "state": "enabled" if loaded else "disabled"}


@contextmanager
def _log_stream(path):
    import fcntl

    from . import _posix_io

    with _posix_io._directory(path.parent) as (_, parent):
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600, dir_fd=parent,
        )
        with os.fdopen(descriptor, "a", encoding="utf-8", buffering=1) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("linked logs are refused")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield stream


def _prune_logs(logs, kind, current, now):
    import fcntl

    from . import _posix_io

    cutoff = now - timedelta(days=30)
    with _posix_io._directory(logs) as (_, parent):
        for path in logs.iterdir():
            match = re.fullmatch(rf"server\.{kind}(?:\.(\d{{4}}-\d{{2}}-\d{{2}}))?\.log", path.name)
            if not match or path == current:
                continue
            try:
                if match[1] and datetime.combine(
                    date.fromisoformat(match[1]) + timedelta(days=1), day_time(), UTC
                ) >= cutoff:
                    continue
                descriptor = os.open(
                    path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent,
                )
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_mtime >= cutoff.timestamp()
                    ):
                        continue
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    current_info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == (current_info.st_dev, current_info.st_ino):
                        os.unlink(path.name, dir_fd=parent)
            except (OSError, ValueError, OverflowError):
                continue
