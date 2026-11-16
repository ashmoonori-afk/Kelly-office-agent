"""Handle-checked local reads and Windows-only, no-overwrite publication."""

import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path, PureWindowsPath
from typing import BinaryIO

from ._vendor.office import path_identity as _identity
from ._vendor.office import secure_file_open as _files
from ._vendor.office import windows_native as _windows
from ._vendor.office.errors import DocumentError
from .contracts import AppError

_is_reserved = getattr(os.path, "isreserved", lambda name: PureWindowsPath(name).is_reserved())


def _checked(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or str(path).startswith("\\\\"):
        raise AppError("POLICY_DENIED")
    for part in path.parts[1:]:
        if (
            part in {".", ".."}
            or part.endswith((".", " "))
            or any(ord(char) < 32 or ord(char) == 127 or char in ':*?"<>|' for char in part)
            or _is_reserved(part)
        ):
            raise AppError("POLICY_DENIED")
    return path


def _chain(path: Path) -> list[Path]:
    return [*reversed(path.parents), path]


@contextmanager
def _directories(
    path: Path, *, allow_missing: bool = False, create: bool = False
) -> Iterator[Path]:
    with ExitStack() as stack:
        opened = []
        for current in _chain(_checked(path)):
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                if allow_missing:
                    break
                if not create:
                    raise AppError("INVALID_INPUT") from None
                try:
                    # Every ancestor is already held; never create an unchecked tail.
                    try:
                        current.mkdir()
                    except FileExistsError:
                        pass
                    metadata = current.lstat()
                except OSError as exc:
                    raise AppError("POLICY_DENIED") from exc
            except OSError as exc:
                raise AppError("POLICY_DENIED") from exc
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & 0x400
            ):
                raise AppError("POLICY_DENIED")
            try:
                expected = _identity.directory_identity(current)
                if os.name == "nt":
                    native = _windows.api()
                    handle = _windows.open_handle(
                        current,
                        directory=True,
                        # Metadata-only handles do not enforce delete-sharing denial.
                        access=native.FILE_READ_ATTRIBUTES | 0x0001,  # FILE_LIST_DIRECTORY
                        share=native.FILE_SHARE_READ | native.FILE_SHARE_WRITE,
                    )
                    if _windows.handle_identity(handle) != expected:
                        _windows.close_handle(handle)
                        raise AppError("SOURCE_CHANGED")
                else:
                    handle = _identity.open_directory_guard(current, expected)
            except (OSError, DocumentError) as exc:
                raise AppError("POLICY_DENIED") from exc
            stack.callback(_identity.close_guard, handle)
            opened.append((current, expected))
        yield path
        try:
            for current, expected in opened:
                _identity.ensure_directory_identity(current, expected)
        except (OSError, DocumentError) as exc:
            raise AppError("SOURCE_CHANGED") from exc


def validate_root(path: Path) -> Path:
    """Validate existing ancestors; missing tails may subsequently be created.

    This check does not authorize later writes: hold guard_directory while writing.
    """
    path = _checked(path)
    with _directories(path, allow_missing=True):
        return path


def ensure_directory(path: Path) -> Path:
    """Create missing components under held ancestors, validating each before descent.

    Callers must still hold guard_directory during subsequent writes.
    """
    if os.name != "nt":
        raise AppError("UNSUPPORTED_CAPABILITY")
    path = _checked(path)
    with _directories(path, create=True):
        return path


@contextmanager
def guard_directory(path: Path) -> Iterator[Path]:
    """Hold every ancestor against replacement for the entire Windows operation."""
    if os.name != "nt":
        raise AppError("UNSUPPORTED_CAPABILITY")
    with _directories(_checked(path)) as directory:
        yield directory


@contextmanager
def open_regular(path: Path, root: Path) -> Iterator[BinaryIO]:
    """Read a stable regular file beneath root without following any aliases."""
    path, root = _checked(path), _checked(root)
    if path == root or not path.is_relative_to(root):
        raise AppError("POLICY_DENIED")
    with guard_directory(path.parent):
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
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise AppError("SOURCE_CHANGED")
            except (OSError, DocumentError) as exc:
                raise AppError("SOURCE_CHANGED") from exc


def _rename_handle(handle: int, destination: Path) -> None:
    """FILE_RENAME_INFO uses Win32 class 3; ReplaceIfExists remains false."""
    import ctypes
    from ctypes import wintypes

    class RenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOL),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    encoded = _windows.windows_path(destination).encode("utf-16-le")
    # Win32 reads a terminated FileName even though FileNameLength excludes its NUL.
    size = max(
        ctypes.sizeof(RenameInfo),
        RenameInfo.FileName.offset + len(encoded) + ctypes.sizeof(wintypes.WCHAR),
    )
    buffer = ctypes.create_string_buffer(size)
    info = RenameInfo.from_buffer(buffer)
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.FileName.offset, encoded, len(encoded))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    rename = kernel32.SetFileInformationByHandle
    rename.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    rename.restype = wintypes.BOOL
    if not rename(handle, 3, buffer, size):
        raise ctypes.WinError(ctypes.get_last_error())


def rename_new(src: Path, dst: Path, *, expected_sha256: str | None = None) -> None:
    """Rename held, optionally hash-verified bytes; never replace a target."""
    if os.name != "nt":
        raise AppError("UNSUPPORTED_CAPABILITY")
    src, dst = _checked(src), _checked(dst)
    if src.parent != dst.parent or src == dst:
        raise AppError("POLICY_DENIED")
    if expected_sha256 is not None and not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise AppError("INVALID_INPUT")
    with guard_directory(dst.parent):
        handle = None
        try:
            try:
                dst.lstat()
            except FileNotFoundError:
                pass
            else:
                raise AppError("OUTPUT_EXISTS", status=409)
            native = _windows.api()
            expected = _identity.regular_path_identity(src)
            handle = _windows.open_handle(
                src,
                directory=False,
                access=native.GENERIC_READ | 0x00010000,  # DELETE permits rename by this handle.
                share=native.FILE_SHARE_READ,
            )
            if _windows.handle_identity(handle) != expected:
                raise AppError("SOURCE_CHANGED")
            with os.fdopen(_windows.descriptor(handle), "rb") as stream:
                owned_handle, handle = handle, None
                if (
                    expected_sha256 is not None
                    and hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256
                ):
                    raise AppError("SOURCE_CHANGED")
                _rename_handle(owned_handle, dst)
                if _identity.regular_path_identity(dst) != expected:
                    raise AppError("SOURCE_CHANGED")
        except OSError as exc:
            code = (
                "OUTPUT_EXISTS" if getattr(exc, "winerror", None) in {80, 183} else "POLICY_DENIED"
            )
            raise AppError(code, status=409 if code == "OUTPUT_EXISTS" else 400) from exc
        except DocumentError as exc:
            raise AppError("POLICY_DENIED") from exc
        finally:
            if handle is not None:
                _windows.close_handle(handle)
