"""Native directory traversal refuses aliases and keeps writes on held ancestors."""

import os
import subprocess
from pathlib import Path

import pytest

from kelly.contracts import AppError
from kelly.platform import safe_io


def test_symlinked_ancestor_cannot_receive_a_managed_write(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(outside)],
            check=True, capture_output=True, timeout=30,
        )
    else:
        alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AppError) as caught:
        safe_io.ensure_directory(alias / "nested")
    assert caught.value.code == "POLICY_DENIED"
    assert list(outside.iterdir()) == []


def test_creation_stays_on_held_parent_when_ancestor_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        # Native Windows guards deny the replacement itself.
        directory = safe_io.ensure_directory(tmp_path / "managed")
        with safe_io.guard_directory(directory), pytest.raises(OSError):
            directory.rename(tmp_path / "moved")
        return
    managed = safe_io.ensure_directory(tmp_path / "managed")
    moved, outside = tmp_path / "moved", tmp_path / "outside"
    outside.mkdir()
    native_open = os.open

    def swap_before_create(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777,
        *, dir_fd: int | None = None,
    ) -> int:
        if os.fspath(path) in {"draft", str(managed / "draft")} and flags & os.O_CREAT:
            managed.rename(moved)
            managed.symlink_to(outside, target_is_directory=True)
        return native_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_before_create)
    with pytest.raises(AppError) as caught, safe_io.open_new(managed / "draft") as stream:
        stream.write(b"synthetic private bytes")
    assert caught.value.code == "SOURCE_CHANGED"
    assert list(outside.iterdir()) == []
    assert (moved / "draft").read_bytes() == b"synthetic private bytes"
