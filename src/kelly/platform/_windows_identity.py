"""Windows file pinning retained from operations."""

from contextlib import contextmanager

from .._vendor.office import path_identity, windows_native
from .._vendor.office.errors import DocumentError
from ..contracts import AppError


@contextmanager
def identity_guard(path):
    # A data-read handle enforces delete-sharing denial; metadata-only handles
    # are insufficient on Windows. SQLite may read/write the same pinned file.
    try:
        expected = path_identity.regular_path_identity(path)
        native = windows_native.api()
        handle = windows_native.open_handle(
            path,
            directory=False,
            access=native.GENERIC_READ,
            share=native.FILE_SHARE_READ | native.FILE_SHARE_WRITE,
        )
    except (OSError, DocumentError):
        raise AppError("POLICY_DENIED") from None
    try:
        if windows_native.handle_identity(handle) != expected:
            raise AppError("SOURCE_CHANGED")
        yield
    finally:
        path_identity.close_guard(handle)
