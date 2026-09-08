"""Build offline; install the locked runtime in a new venv; run, stop, and uninstall."""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def environment():
    names = (
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "PATH",
        "PATHEXT",
        "USERPROFILE",
        "LOCALAPPDATA",
        "UV_CACHE_DIR",
    )
    value = {key: os.environ[key] for key in names if key in os.environ}
    value.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return value


def run(command, *, cwd, timeout=120):
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        # Dependency tool diagnostics can include user-specific cache paths.
        raise RuntimeError(
            f"Local package check failed at {Path(command[0]).name}; exit={result.returncode}"
        )
    return result.stdout


def main():
    if os.name != "nt":
        raise RuntimeError("This check requires Windows")
    root = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required; no automatic tool installation")
    evidence = root / ".birkin-lite"
    evidence.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="install-check-", dir=evidence))
    original_lock = hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
    requirements, dist, venv = work / "requirements.txt", work / "dist", work / "venv"
    run(
        [
            uv,
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str(requirements),
        ],
        cwd=root,
    )
    run([uv, "build", "--wheel", "--offline", "--out-dir", str(dist)], cwd=root)
    (wheel,) = dist.glob("*.whl")
    run([uv, "venv", "--python", sys.executable, str(venv)], cwd=root)
    python = venv / "Scripts" / "python.exe"
    entrypoint = venv / "Scripts" / "kelly.exe"
    run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "-r",
            str(requirements),
        ],
        cwd=work,
    )
    run([uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)], cwd=work)
    run([uv, "pip", "check", "--python", str(python)], cwd=work)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config_path = work / "config.json"
    setup = """
import sys
from pathlib import Path
from importlib.metadata import distribution, metadata
from importlib.resources import files
from birkin_lite.config import AppConfig, create_config
from birkin_lite.db import init_db

assert metadata('kelly-office-agent')['License-Expression'] == 'MIT'
assert 'Kelly' in metadata('kelly-office-agent').get_payload()
assert {'kelly', 'blite'} <= {p.name for p in distribution('kelly-office-agent').entry_points}
resources = files('birkin_lite')
assert resources.joinpath('schema.sql').is_file()
assert all(resources.joinpath('licenses', name).is_file() for name in ('sbom.json', 'third-party.txt'))
assert all(resources.joinpath('static', name).is_file() for name in ('index.html', 'app.js', 'app.css', 'kelly-companion.png', 'favicon.svg'))
create_config(Path(sys.argv[1]), AppConfig(home='data', port=int(sys.argv[2])))
init_db(Path(sys.argv[1]).parent / 'data' / 'state.db').close()
"""
    run([str(python), "-c", setup, str(config_path), str(port)], cwd=work)
    doctor = json.loads(run([str(entrypoint), "doctor", "--config", str(config_path)], cwd=work))
    if not doctor["ok"] or any(doctor["credentials_present"].values()):
        raise RuntimeError("Installed doctor did not pass without credentials")
    server = subprocess.Popen(
        [str(entrypoint), "serve", "--config", str(config_path)],
        cwd=work,
        env=environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 30
        healthy = False
        while time.monotonic() < deadline and server.poll() is None:
            try:
                with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                    healthy = json.load(response) == {"ok": True}
                if healthy:
                    break
            except urllib.error.URLError:
                time.sleep(0.1)
        if not healthy:
            raise RuntimeError("Installed server did not become healthy")
        stopped = json.loads(
            run([str(entrypoint), "stop", "--config", str(config_path)], cwd=work, timeout=45)
        )
        _, server_log = server.communicate(timeout=10)
        if (
            stopped != {"state": "stopped"}
            or server.returncode != 0
            or "Application shutdown complete." not in server_log
        ):
            raise RuntimeError("Installed server did not stop gracefully")
    finally:
        if server.poll() is None:
            # Failure remains unknown if authenticated stop cannot finish. Never kill by PID.
            run([str(entrypoint), "stop", "--config", str(config_path)], cwd=work, timeout=45)
            server.communicate(timeout=10)
    run([uv, "pip", "uninstall", "--python", str(python), "kelly-office-agent"], cwd=work)
    if entrypoint.exists() or entrypoint.with_name("blite.exe").exists():
        raise RuntimeError("Uninstall left an entrypoint behind")
    run(
        [
            str(python),
            "-c",
            "import importlib.util; assert importlib.util.find_spec('birkin_lite') is None",
        ],
        cwd=work,
    )
    if hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest() != original_lock:
        raise RuntimeError("Dependency lock changed")
    report = {
        "scope": "isolated_venv_on_existing_windows_host",
        "distribution": "kelly-office-agent",
        "entrypoint": "kelly",
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "lock_sha256": original_lock,
        "locked_dependencies": True,
        "doctor": doctor["ok"],
        "server_health": True,
        "graceful_stop": True,
        "authenticated_stop": True,
        "license_resources": True,
        "ui_resources": True,
        "uninstalled": True,
        "provider_calls": 0,
        "data_preserved": (work / "data" / "state.db").is_file(),
    }
    (work / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"evidence": str(work.relative_to(root) / "result.json"), **report}))


if __name__ == "__main__":
    main()
