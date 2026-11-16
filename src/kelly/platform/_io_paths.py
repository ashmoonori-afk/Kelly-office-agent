"""Shared lexical path policy for native file operations."""

import os
from pathlib import Path, PureWindowsPath

from ..contracts import AppError

_is_reserved = getattr(os.path, "isreserved", lambda name: PureWindowsPath(name).is_reserved())


def checked(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or str(path).startswith("\\\\"):
        raise AppError("POLICY_DENIED")
    for part in path.parts[1:]:
        if (
            part in {".", ".."}
            or part.endswith((".", " "))
            or any(ord(char) < 32 or ord(char) == 127 or char in ':*?"<>|\\' for char in part)
            or _is_reserved(part)
        ):
            raise AppError("POLICY_DENIED")
    return path
