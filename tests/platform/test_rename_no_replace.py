"""A collision or changed draft cannot overwrite a published result."""

import hashlib
import os
from pathlib import Path

import pytest

from kelly.contracts import AppError
from kelly.platform import safe_io


def test_publication_preserves_existing_result_and_unpublished_draft(tmp_path: Path) -> None:
    source, target = tmp_path / "draft", tmp_path / "result"
    source.write_bytes(b"new draft")
    target.write_bytes(b"existing result")
    with pytest.raises(AppError) as caught:
        safe_io.rename_new(source, target)
    assert caught.value.code == "OUTPUT_EXISTS"
    assert target.read_bytes() == b"existing result"
    assert source.read_bytes() == b"new draft"


def test_verified_draft_moves_once_and_keeps_its_hash(tmp_path: Path) -> None:
    source, target = tmp_path / "draft", tmp_path / "result"
    source.write_bytes(b"verified bytes")
    expected = hashlib.sha256(b"verified bytes").hexdigest()
    safe_io.rename_new(source, target, expected_sha256=expected)
    assert not source.exists()
    assert target.read_bytes() == b"verified bytes"


def test_changed_draft_is_not_published(tmp_path: Path) -> None:
    source, target = tmp_path / "draft", tmp_path / "result"
    source.write_bytes(b"changed bytes")
    with pytest.raises(AppError) as caught:
        safe_io.rename_new(
            source, target, expected_sha256=hashlib.sha256(b"original bytes").hexdigest()
        )
    assert caught.value.code == "SOURCE_CHANGED"
    assert not target.exists()


def test_collision_at_atomic_publish_preserves_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = tmp_path / "draft", tmp_path / "result"
    source.write_bytes(b"draft")
    if os.name == "nt":
        publish = safe_io._rename_handle

        def create_collision(handle, destination):
            destination.write_bytes(b"racing result")
            return publish(handle, destination)

        monkeypatch.setattr(safe_io, "_rename_handle", create_collision)
    else:
        link = os.link

        def create_collision(*args, **kwargs):
            target.write_bytes(b"racing result")
            return link(*args, **kwargs)

        monkeypatch.setattr(os, "link", create_collision)
    with pytest.raises(AppError) as caught:
        safe_io.rename_new(source, target)
    assert caught.value.code == "OUTPUT_EXISTS"
    assert source.read_bytes() == b"draft"
    assert target.read_bytes() == b"racing result"
