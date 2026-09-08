"""Actual Windows handles, junctions and no-replace publication regressions."""

import hashlib
import io
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from birkin_lite import safe_io
from birkin_lite._vendor.office.errors import DocumentError, DocumentErrorCode
from birkin_lite.contracts import AppError


@pytest.mark.parametrize("tail", ["../outside", "file:stream", "trailing.", "trailing ", "NUL"])
def test_reject_ambiguous_names_and_traversal(tmp_path, tail):
    with pytest.raises(AppError) as error:
        safe_io.validate_root(tmp_path / tail)
    assert error.value.code == "POLICY_DENIED"


def test_validate_root_permits_missing_tail_but_not_files(tmp_path):
    missing = tmp_path / "future" / "nested"
    assert safe_io.validate_root(missing) == missing
    assert not missing.exists()
    (tmp_path / "file").write_bytes(b"source")
    with pytest.raises(AppError):
        safe_io.validate_root(tmp_path / "file" / "nested")
    with pytest.raises(AppError):
        safe_io.validate_root(Path("relative"))


@pytest.mark.skipif(os.name != "nt", reason="Windows handle guarantees")
def test_read_only_and_original_immutable_with_replacement_blocked(tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes("변경하지 않는 원본".encode())
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    with safe_io.open_regular(source, tmp_path) as stream:
        assert hashlib.sha256(stream.read()).hexdigest() == original
        with pytest.raises(io.UnsupportedOperation):
            stream.write(b"changed")
        with pytest.raises(OSError):
            source.write_bytes(b"changed")
        with pytest.raises(OSError):
            source.rename(tmp_path / "replacement.txt")
        with pytest.raises(OSError):
            tmp_path.rename(tmp_path.with_name(tmp_path.name + "-swapped"))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original


@pytest.mark.skipif(os.name != "nt", reason="Windows handle guarantees")
def test_junction_in_ancestor_is_rejected_even_with_missing_tail(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "source.txt").write_bytes(b"outside")
    junction = tmp_path / "alias"
    # Native PowerShell creates only the named junction; cleanup unlinks that entry.
    command = "New-Item -ItemType Junction -Path $env:TEST_JUNCTION -Target $env:TEST_TARGET"
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        env=os.environ | {"TEST_JUNCTION": str(junction), "TEST_TARGET": str(target)},
        check=True,
        capture_output=True,
        timeout=30,
    )
    try:
        assert junction.is_junction()
        for candidate in [junction, junction / "missing" / "tail"]:
            with pytest.raises(AppError) as error:
                safe_io.validate_root(candidate)
            assert error.value.code == "POLICY_DENIED"
        with pytest.raises(AppError), safe_io.open_regular(junction / "source.txt", tmp_path):
            pass
        assert (target / "source.txt").read_bytes() == b"outside"
    finally:
        junction.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle guarantees")
def test_reject_outside_root_and_identity_mismatch(tmp_path, monkeypatch):
    root = tmp_path / "managed"
    root.mkdir()
    source = tmp_path / "outside.txt"
    source.write_bytes(b"outside")
    with pytest.raises(AppError), safe_io.open_regular(source, root):
        pass
    source = root / "inside.txt"
    source.write_bytes(b"inside")

    def changed(*args):
        raise DocumentError(DocumentErrorCode.PERMISSION_DENIED, "emit", "identity changed")

    monkeypatch.setattr(safe_io._identity, "ensure_path_identity", changed)
    with pytest.raises(AppError) as error, safe_io.open_regular(source, root) as stream:
        assert stream.read() == b"inside"
    assert error.value.code == "SOURCE_CHANGED"


@pytest.mark.skipif(os.name != "nt", reason="Windows native no-replace publication")
def test_native_publish_no_replace_and_same_directory(tmp_path):
    source, destination = tmp_path / "draft.tmp", tmp_path / "결과.txt"
    source.write_bytes(b"verified draft")
    with safe_io.guard_directory(tmp_path):
        safe_io.rename_new(source, destination)
    assert not source.exists()
    assert destination.read_bytes() == b"verified draft"
    source.write_bytes(b"second draft")
    with pytest.raises(AppError) as error:
        safe_io.rename_new(source, destination)
    assert error.value.code == "OUTPUT_EXISTS"
    assert source.read_bytes() == b"second draft"
    assert destination.read_bytes() == b"verified draft"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(AppError):
        safe_io.rename_new(source, elsewhere / "target")


@pytest.mark.skipif(os.name != "nt", reason="Windows native no-replace publication")
def test_source_cannot_be_swapped_during_native_publication(tmp_path, monkeypatch):
    source, destination = tmp_path / "draft", tmp_path / "published"
    source.write_bytes(b"verified bytes")
    rename = safe_io._rename_handle

    def attempt_swap(handle, target):
        with pytest.raises(OSError):
            source.rename(tmp_path / "swapped")
        with pytest.raises(OSError):
            source.write_bytes(b"unverified bytes")
        rename(handle, target)

    monkeypatch.setattr(safe_io, "_rename_handle", attempt_swap)
    safe_io.rename_new(source, destination)
    assert destination.read_bytes() == b"verified bytes"


@pytest.mark.skipif(os.name != "nt", reason="Windows native no-replace publication")
def test_hash_must_match_validated_bytes_before_publication(tmp_path):
    source, destination = tmp_path / "draft", tmp_path / "published"
    source.write_bytes(b"verified bytes")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    source.write_bytes(b"swapped before publication entry")
    with pytest.raises(AppError) as error:
        safe_io.rename_new(source, destination, expected_sha256=expected)
    assert error.value.code == "SOURCE_CHANGED"
    assert not destination.exists()
    assert source.read_bytes() == b"swapped before publication entry"
    source.write_bytes(b"verified bytes")
    safe_io.rename_new(source, destination, expected_sha256=expected)
    assert destination.read_bytes() == b"verified bytes"


@pytest.mark.skipif(os.name != "nt", reason="Windows native no-replace publication")
def test_publication_rejects_destination_created_after_precheck(tmp_path, monkeypatch):
    source, destination = tmp_path / "draft", tmp_path / "published"
    source.write_bytes(b"draft")
    rename = safe_io._rename_handle

    def create_collision(handle, target):
        target.write_bytes(b"racing destination")
        rename(handle, target)

    monkeypatch.setattr(safe_io, "_rename_handle", create_collision)
    with pytest.raises(AppError) as error:
        safe_io.rename_new(source, destination)
    assert error.value.code == "OUTPUT_EXISTS"
    assert source.read_bytes() == b"draft"
    assert destination.read_bytes() == b"racing destination"


def test_publication_fails_closed_on_unsupported_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(safe_io.os, "name", "unsupported")
    with pytest.raises(AppError) as error:
        safe_io.rename_new(tmp_path / "source", tmp_path / "target")
    assert error.value.code == "UNSUPPORTED_CAPABILITY"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle guarantees")
def test_symbolic_links_are_rejected(tmp_path):
    source = tmp_path / "real.txt"
    source.write_bytes(b"immutable")
    alias = tmp_path / "link.txt"
    try:
        alias.symlink_to(source)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Host does not grant Windows symbolic link creation privilege")
        raise
    assert alias.is_symlink()
    with pytest.raises(AppError), safe_io.open_regular(alias, tmp_path):
        pass
    assert source.read_bytes() == b"immutable"


@pytest.mark.skipif(os.name != "nt", reason="Windows native no-replace publication")
@pytest.mark.parametrize("iteration", range(10))
def test_repeated_hash_publication_with_long_unicode_paths(tmp_path, iteration):
    directory = tmp_path / ("긴_폴더_" * 15) / f"비ASCII_😀_{iteration}"
    directory.mkdir(parents=True)
    for payload in ["보고서 본문".encode(), b"different", bytes(range(255))]:
        name = uuid.uuid4().hex
        source = directory / f".{name}.tmp"
        destination = directory / f"결과_😀_{name}.txt"
        source.write_bytes(payload)
        safe_io.rename_new(source, destination, expected_sha256=hashlib.sha256(payload).hexdigest())
        assert not source.exists()
        assert destination.read_bytes() == payload


@pytest.mark.skipif(os.name != "nt", reason="Windows native FILE_RENAME_INFO buffer contract")
def test_native_filename_does_not_read_beyond_its_buffer(tmp_path, monkeypatch):
    import ctypes

    allocate = ctypes.create_string_buffer

    def poisoned_allocation(size):
        # Give native over-reads deterministic nonzero allocator padding.
        buffer = allocate(size + 64)
        ctypes.memset(ctypes.addressof(buffer) + size, ord("Z"), 62)
        return buffer

    monkeypatch.setattr(ctypes, "create_string_buffer", poisoned_allocation)
    source, destination = tmp_path / "draft", tmp_path / "exact-result.txt"
    source.write_bytes(b"exact bytes")
    safe_io.rename_new(
        source, destination, expected_sha256=hashlib.sha256(b"exact bytes").hexdigest()
    )
    assert destination.read_bytes() == b"exact bytes"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.skipif(os.name != "nt", reason="Windows directory guards during creation")
def test_directory_creation_holds_each_parent_before_child_mkdir(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    future = managed / "future"
    destination = future / "exports"
    mkdir = Path.mkdir
    attempts = []

    def try_parent_swap(path, *args, **kwargs):
        if path in {future, destination}:
            with pytest.raises(OSError):
                path.parent.rename(tmp_path / f"moved-{path.parent.name}")
            attempts.append(path)
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", try_parent_swap)
    assert safe_io.ensure_directory(destination) == destination
    assert attempts == [future, destination]
    assert destination.is_dir()
    assert safe_io.ensure_directory(destination) == destination


@pytest.mark.skipif(os.name != "nt", reason="Windows junction substitution during creation")
def test_directory_creation_rejects_new_parent_junction_before_creating_child(
    tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.mkdir()
    managed = tmp_path / "managed"
    moved = tmp_path / "moved"
    mkdir = Path.mkdir

    def replace_new_directory(path, *args, **kwargs):
        result = mkdir(path, *args, **kwargs)
        if path == managed:
            path.rename(moved)
            subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "New-Item -ItemType Junction -Path $env:TEST_JUNCTION -Target $env:TEST_TARGET",
                ],
                env=os.environ | {"TEST_JUNCTION": str(managed), "TEST_TARGET": str(outside)},
                check=True,
                capture_output=True,
                timeout=30,
            )
        return result

    monkeypatch.setattr(Path, "mkdir", replace_new_directory)
    try:
        with pytest.raises(AppError) as error:
            safe_io.ensure_directory(managed / "must-not-exist" / "exports")
        assert error.value.code == "POLICY_DENIED"
        assert managed.is_junction()
        assert list(outside.iterdir()) == []
        assert list(moved.iterdir()) == []
    finally:
        if managed.is_junction():
            managed.rmdir()
