"""LaunchAgent lifecycle against a stateful fake launchctl, never the user domain."""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from kelly import config, startup
from kelly.contracts import AppError
from kelly.platform import _launchd_service

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS LaunchAgent filesystem")


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / "config.json"
    config.create_config(path, config.AppConfig(home="data"))
    loaded = set()
    definitions = []

    def launchctl(arguments):
        action, target, *rest = arguments
        if action == "print":
            return subprocess.CompletedProcess(arguments, 0 if target in loaded else 113)
        if action == "bootstrap":
            definition = plistlib.loads(Path(rest[0]).read_bytes())
            service = target + "/" + definition["Label"]
            assert service not in loaded, "duplicate bootstrap"
            loaded.add(service)
            definitions.append(definition)
        elif action == "bootout":
            assert target in loaded, "duplicate bootout"
            loaded.remove(target)
        else:
            pytest.fail("unexpected launchctl action")
        return subprocess.CompletedProcess(arguments, 0)

    return path, launchctl, loaded, definitions


def test_install_uninstall_are_idempotent_with_exact_plist(agent):
    path, launchctl, loaded, definitions = agent
    assert startup.manage(path, "status", task_runner=launchctl)["state"] == "disabled"
    first = startup.manage(path, "install", task_runner=launchctl)
    assert startup.manage(path, "install", task_runner=launchctl) == first
    assert startup.manage(path, "status", task_runner=launchctl) == first
    assert first["state"] == "enabled" and len(loaded) == 1
    assert definitions == [{
        "Label": first["task_name"],
        "ProgramArguments": [sys.executable, "-m", "kelly.startup", "--run", str(path)],
        "WorkingDirectory": str(path.parent),
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LimitLoadToSessionType": "Aqua",
    }]
    removed = startup.manage(path, "uninstall", task_runner=launchctl)
    assert startup.manage(path, "uninstall", task_runner=launchctl) == removed
    assert removed["state"] == "disabled" and not loaded
    assert not list((path.parent / "Library" / "LaunchAgents").glob("*.plist"))


def test_foreign_plist_survives_install_and_uninstall(agent):
    path, launchctl, _, _ = agent
    startup.manage(path, "install", task_runner=launchctl)
    plist, = (path.parent / "Library" / "LaunchAgents").glob("*.plist")
    foreign = plistlib.dumps({"Label": "org.synthetic.foreign"})
    plist.write_bytes(foreign)
    for action in ("install", "uninstall", "status"):
        with pytest.raises(AppError) as error:
            startup.manage(path, action, task_runner=launchctl)
        assert error.value.code == "CONFLICT"
        assert plist.read_bytes() == foreign


def test_launchctl_error_does_not_delete_registration(agent):
    path, launchctl, _, _ = agent
    startup.manage(path, "install", task_runner=launchctl)

    def denied(arguments):
        return subprocess.CompletedProcess(arguments, 0 if arguments[0] == "print" else 1)

    with pytest.raises(AppError) as error:
        _launchd_service.manage(path, "disable", task_runner=denied)
    assert error.value.code == "POLICY_DENIED"
    assert len(list((path.parent / "Library" / "LaunchAgents").glob("*.plist"))) == 1


@pytest.mark.parametrize("operation", ["log", "identity"])
def test_special_file_is_refused_without_waiting_for_a_peer(tmp_path, operation):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    code = """
import sys
from pathlib import Path
from kelly.contracts import AppError
from kelly.platform._launchd_service import _log_stream
from kelly.platform.identity import identity_guard
guard = _log_stream if sys.argv[2] == "log" else identity_guard
try:
    with guard(Path(sys.argv[1])):
        raise AssertionError("FIFO accepted")
except (OSError, AppError):
    pass
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(path), operation],
        capture_output=True, check=False, timeout=5,
    )
    assert result.returncode == 0, result.stderr.decode()
