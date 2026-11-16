"""Install the locked runtime; verify restart, recovery, local key removal and uninstall."""

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


def server_cycle(entrypoint, config_path, work, port):
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
import io
import json
from contextlib import closing
from pathlib import Path
from importlib.metadata import distribution, metadata
from importlib.resources import files
from kelly import artifacts, db
from kelly.config import AppConfig, create_config

assert metadata('kelly-office-agent')['License-Expression'] == 'MIT'
assert 'Kelly' in metadata('kelly-office-agent').get_payload()
assert {'kelly', 'blite'} <= {p.name for p in distribution('kelly-office-agent').entry_points}
resources = files('kelly')
assert resources.joinpath('schema.sql').is_file()
assert all(resources.joinpath('licenses', name).is_file() for name in ('sbom.json', 'third-party.txt'))
assert all(resources.joinpath('static', name).is_file() for name in ('index.html', 'app.js', 'app.css', 'kelly-companion.png', 'favicon.svg'))
settings = AppConfig(home='data', port=int(sys.argv[2]))
create_config(Path(sys.argv[1]), settings)
home = Path(sys.argv[1]).parent / 'data'
with closing(db.init_db(home / 'state.db')) as conn:
    source = artifacts.import_source(home, conn, io.BytesIO(b'Kelly package recovery check.'), {'filename': 'recovery.txt'}, settings.actor(), 'default')
    db.set_value(conn, 'install-check', {'source_id': source['id']})
print(json.dumps({key: source[key] for key in ('id', 'relative_path', 'sha256')}))
"""
    source = json.loads(run([str(python), "-c", setup, str(config_path), str(port)], cwd=work))
    doctor = json.loads(run([str(entrypoint), "doctor", "--config", str(config_path)], cwd=work))
    if not doctor["ok"] or any(doctor["credentials_present"].values()):
        raise RuntimeError("Installed doctor did not pass without credentials")
    for _ in range(2):
        server_cycle(entrypoint, config_path, work, port)
    local_key = """
import sys
from pathlib import Path
from pydantic import SecretStr
from kelly import config
home = Path(sys.argv[1])
config.store_secret(home, 'xai', SecretStr(sys.argv[2]))
assert (config.stored_secret(home, 'xai') is not None) == bool(sys.argv[2])
"""
    run(
        [str(python), "-c", local_key, str(work / "data"), "synthetic-install-check-only"], cwd=work
    )
    snapshot, restored = work / "snapshot", work / "restored"
    backup = json.loads(
        run(
            [str(entrypoint), "backup", "--config", str(config_path), "--output", str(snapshot)],
            cwd=work,
        )
    )
    recovered = json.loads(
        run(
            [str(entrypoint), "restore", "--input", str(snapshot), "--home", str(restored)],
            cwd=work,
        )
    )
    if not recovered["authority_reset"] or recovered["files_restored"] != backup["files_backed_up"]:
        raise RuntimeError("Installed backup and restore disagreed")
    run([str(python), "-c", local_key, str(work / "data"), ""], cwd=work)
    recovery_check = """
import sys
from pathlib import Path
from kelly import artifacts, config, db, operations
for folder in sys.argv[1:]:
    home = Path(folder)
    with operations._database(home) as conn:
        source = db.get_value(conn, 'install-check')['source_id']
        with artifacts.open_artifact(home, conn, source, config.AppConfig().actor(), 'default') as stream:
            assert stream.read() == b'Kelly package recovery check.'
    assert config.stored_secret(home, 'xai') is None
assert not (Path(sys.argv[2]) / 'auth').exists()
assert not (Path(sys.argv[3]) / 'auth').exists()
"""
    run(
        [str(python), "-c", recovery_check, str(work / "data"), str(snapshot), str(restored)],
        cwd=work,
    )
    restored_config = restored / "config.json"
    restored_doctor = json.loads(
        run([str(entrypoint), "doctor", "--config", str(restored_config)], cwd=work)
    )
    if not restored_doctor["ok"] or any(restored_doctor["credentials_present"].values()):
        raise RuntimeError("Restored doctor did not pass without credentials")
    server_cycle(entrypoint, restored_config, work, port)
    run([uv, "pip", "uninstall", "--python", str(python), "kelly-office-agent"], cwd=work)
    if entrypoint.exists() or entrypoint.with_name("blite.exe").exists():
        raise RuntimeError("Uninstall left an entrypoint behind")
    run(
        [
            str(python),
            "-c",
            "import importlib.util; assert importlib.util.find_spec('kelly') is None",
        ],
        cwd=work,
    )
    if hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest() != original_lock:
        raise RuntimeError("Dependency lock changed")
    for home in (work / "data", snapshot, restored):
        if (
            hashlib.sha256((home / source["relative_path"]).read_bytes()).hexdigest()
            != source["sha256"]
        ):
            raise RuntimeError("Recovery or uninstall changed the managed source")
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
        "restart": True,
        "backup_restore": True,
        "restored_server_health": True,
        "local_synthetic_key_removed": True,
        "credentials_excluded_from_backup": True,
        "provider_key_revocation_verified": False,
        "source_sha256_preserved": source["sha256"],
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
