"""Native secret protection, selected once; protected bytes are platform-specific."""

import sys
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

from ...contracts import AppError

if sys.platform == "darwin":
    from .macos import delete, locked, protect, unprotect
elif sys.platform == "win32":
    from .windows import delete, locked, protect, unprotect
else:
    def protect(data: bytes, scope: str = "") -> bytes:
        raise AppError("UNSUPPORTED_CAPABILITY")

    unprotect = protect

    def delete(data: bytes, scope: str = "") -> None:
        raise AppError("UNSUPPORTED_CAPABILITY")

    def locked(folder: Path) -> AbstractContextManager[None]:
        return nullcontext()

__all__ = ["delete", "locked", "protect", "unprotect"]
