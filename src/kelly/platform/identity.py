"""Pin mutable file identity without imposing immutable-content checks."""

import os
import stat
from contextlib import contextmanager

from .._vendor.office import path_identity
from .._vendor.office.errors import DocumentError
from ..contracts import AppError


@contextmanager
def _posix_identity_guard(path):
    with _posix_io._directory(path.parent) as (_, parent):
        try:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=parent,
            )
        except OSError:
            raise AppError("POLICY_DENIED") from None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AppError("POLICY_DENIED")
            try:
                path_identity.ensure_open_identity(stream.fileno(), path.name, parent)
                yield
                path_identity.ensure_open_identity(stream.fileno(), path.name, parent)
            except (OSError, DocumentError):
                raise AppError("SOURCE_CHANGED") from None


if os.name == "nt":
    from ._windows_identity import identity_guard as _native_identity_guard
else:
    from . import _posix_io

    _native_identity_guard = _posix_identity_guard

identity_guard = _native_identity_guard
