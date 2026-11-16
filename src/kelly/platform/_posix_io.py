"""Descriptor-relative file operations without symlink traversal or replacement."""

import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO, Final

from .._vendor.office import path_identity as _identity
from .._vendor.office import secure_file_open as _files
from .._vendor.office.errors import DocumentError
from ..contracts import AppError
from ._io_paths import checked as _checked

_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@contextmanager
def _directory(
    path: Path, *, create: bool = False, allow_missing: bool = False
) -> Iterator[tuple[Path, int]]:
    if os.name != "posix":
        raise AppError("UNSUPPORTED_CAPABILITY")
    path = _checked(path)
    with ExitStack() as stack:
        opened: list[tuple[int, str, int]] = []
        try:
            descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
            stack.callback(os.close, descriptor)
            for name in path.parts[1:]:
                if create:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except FileNotFoundError:
                    if allow_missing:
                        break
                    raise
                stack.callback(os.close, child)
                opened.append((descriptor, name, child))
                descriptor = child
        except OSError as exc:
            raise AppError("POLICY_DENIED") from exc
        yield path, descriptor
        try:
            for parent, name, child in opened:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                held = os.fstat(child)
                if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                    raise AppError("SOURCE_CHANGED")
        except OSError as exc:
            raise AppError("SOURCE_CHANGED") from exc


def validate_root(path: Path) -> Path:
    with _directory(path, allow_missing=True):
        return _checked(path)


def ensure_directory(path: Path) -> Path:
    with _directory(path, create=True):
        return _checked(path)


@contextmanager
def guard_directory(path: Path) -> Iterator[Path]:
    """Hold and recheck ancestors; mutations use the descriptor-based helpers."""
    with _directory(path) as (checked, _):
        yield checked


@contextmanager
def open_regular(path: Path, root: Path) -> Iterator[BinaryIO]:
    path, root = _checked(path), _checked(root)
    if path == root or not path.is_relative_to(root):
        raise AppError("POLICY_DENIED")
    with _directory(path.parent):
        try:
            descriptor = _files.open_regular(path, root)
        except (OSError, DocumentError) as exc:
            raise AppError("POLICY_DENIED") from exc
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            yield stream
            try:
                _identity.ensure_path_identity(stream.fileno(), path)
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns
                ):
                    raise AppError("SOURCE_CHANGED")
            except (OSError, DocumentError) as exc:
                raise AppError("SOURCE_CHANGED") from exc


@contextmanager
def open_new(path: Path) -> Iterator[BinaryIO]:
    path = _checked(path)
    with _directory(path.parent) as (_, parent):
        try:
            descriptor = os.open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
        except FileExistsError as exc:
            raise AppError("OUTPUT_EXISTS", status=409) from exc
        except OSError as exc:
            raise AppError("POLICY_DENIED") from exc
        with os.fdopen(descriptor, "wb") as stream:
            yield stream
            stream.flush()
            try:
                _identity.ensure_open_identity(stream.fileno(), path.name, parent)
            except (OSError, DocumentError) as exc:
                raise AppError("SOURCE_CHANGED") from exc


def rename_new(src: Path, dst: Path, *, expected_sha256: str | None = None) -> None:
    """Publish a verified inode with link/unlink; an existing destination survives."""
    if os.name != "posix":
        raise AppError("UNSUPPORTED_CAPABILITY")
    src, dst = _checked(src), _checked(dst)
    if src.parent != dst.parent or src == dst:
        raise AppError("POLICY_DENIED")
    if expected_sha256 is not None and not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise AppError("INVALID_INPUT")
    with _directory(src.parent) as (_, parent):
        linked = False
        try:
            descriptor = os.open(src.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise AppError("POLICY_DENIED")
                if (
                    expected_sha256 is not None
                    and hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256
                ):
                    raise AppError("SOURCE_CHANGED")
                _identity.ensure_open_identity(stream.fileno(), src.name, parent)
                os.link(
                    src.name, dst.name, src_dir_fd=parent, dst_dir_fd=parent,
                    follow_symlinks=False,
                )
                linked = True
                _identity.ensure_open_identity(stream.fileno(), dst.name, parent)
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise AppError("SOURCE_CHANGED")
                _identity.ensure_open_identity(stream.fileno(), src.name, parent)
                os.unlink(src.name, dir_fd=parent)
                linked = False
                os.fsync(parent)
        except FileExistsError as exc:
            raise AppError("OUTPUT_EXISTS", status=409) from exc
        except (OSError, DocumentError) as exc:
            raise AppError("POLICY_DENIED") from exc
        finally:
            if linked:
                os.unlink(dst.name, dir_fd=parent)


def replace_file(src: Path, dst: Path) -> None:
    """Replace an explicitly mutable private slot within its held parent."""
    src, dst = _checked(src), _checked(dst)
    if src.parent != dst.parent or src == dst:
        raise AppError("POLICY_DENIED")
    with _directory(src.parent) as (_, parent):
        try:
            descriptor = os.open(src.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise AppError("POLICY_DENIED")
                os.replace(src.name, dst.name, src_dir_fd=parent, dst_dir_fd=parent)
                _identity.ensure_open_identity(stream.fileno(), dst.name, parent)
            os.fsync(parent)
        except (OSError, DocumentError) as exc:
            raise AppError("POLICY_DENIED") from exc


def unlink_file(path: Path, *, missing_ok: bool = False) -> None:
    path = _checked(path)
    with _directory(path.parent) as (_, parent):
        try:
            os.unlink(path.name, dir_fd=parent)
            os.fsync(parent)
        except FileNotFoundError:
            if not missing_ok:
                raise
