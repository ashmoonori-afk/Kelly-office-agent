"""Real Darwin children block on signals while the parent enforces memory limits."""

import signal
import sys

import pytest

from kelly import sandbox
from kelly.contracts import AppError

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin physical footprint")


def test_over_limit_child_is_killed_by_process_group(monkeypatch):
    real_killpg = sandbox.os.killpg
    killed = []

    def killpg(pid, sig):
        killed.append((pid, sig))
        real_killpg(pid, sig)

    monkeypatch.setattr(sandbox.os, "killpg", killpg)
    code = (
        "import sys,signal;sys.stdin.read();"
        "allocation=bytearray(128*1024*1024);signal.pause()"
    )
    with pytest.raises(AppError) as error:
        sandbox._run_worker([sys.executable, "-I", "-c", code], {}, timeout_s=5, memory_mb=32)
    assert error.value.code == "LIMIT_EXCEEDED"
    assert len(killed) == 1 and killed[0][1] == signal.SIGKILL


def test_under_limit_child_finishes_normally():
    code = "import sys,json;sys.stdin.read();print(json.dumps({'format':'txt'}))"
    result = sandbox._run_worker(
        [sys.executable, "-I", "-c", code], {}, timeout_s=5, memory_mb=128,
    )
    assert result["format"] == "txt"
    assert 0 < result["sandbox"]["peak_committed_bytes"] <= 128 * 1024 * 1024


def test_accounting_failure_is_fail_closed(monkeypatch):
    from kelly.platform import _darwin_memory

    def unavailable(pid):
        raise PermissionError("synthetic accounting denial")

    monkeypatch.setattr(_darwin_memory, "footprint", unavailable)
    code = "import sys;sys.stdin.read();print('{}')"
    with pytest.raises(AppError) as error:
        sandbox._run_worker([sys.executable, "-I", "-c", code], {}, timeout_s=5, memory_mb=128)
    assert error.value.code == "UNSUPPORTED_CAPABILITY"
