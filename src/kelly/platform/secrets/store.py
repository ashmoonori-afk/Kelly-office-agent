"""Credential slot persistence shared by config readers and writers."""

import os
import re
import uuid
from pathlib import Path
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from ...contracts import AppError, canonical_json
from ...safe_io import (
    ensure_directory,
    guard_directory,
    open_new,
    open_regular,
    replace_file,
    unlink_file,
)
from . import delete, locked, protect, unprotect

_PROVIDERS: Final = frozenset({"xai", "telegram", "bootstrap", "commandcode"})


class _Secret(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    provider: str
    value: Annotated[str, Field(min_length=1, max_length=8192)]


def _read(path: Path, root: Path) -> bytes:
    with open_regular(path, root) as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise AppError("SECRET_STORE_ERROR")
    return data


def stored_secret(home: Path, provider: str) -> SecretStr | None:
    if provider not in _PROVIDERS:
        raise AppError("INVALID_INPUT")
    root = Path(home).absolute()
    path = root / "auth" / (provider + ".dpapi")
    if not path.parent.exists():
        return None
    with guard_directory(path.parent), locked(path.parent):
        if not path.exists():
            return None
        data = unprotect(_read(path, root), str(path))
    try:
        value = _Secret.model_validate_json(data)
    except ValidationError:
        raise AppError("SECRET_STORE_ERROR") from None
    if value.provider != provider:
        raise AppError("SECRET_STORE_ERROR")
    return SecretStr(value.value)


def store_secret(home: Path, provider: str, value: SecretStr) -> None:
    """Publish only protected bytes; retire the old item after successful publication."""
    if provider not in _PROVIDERS or not isinstance(value, SecretStr):
        raise AppError("INVALID_INPUT")
    plain = value.get_secret_value().strip()
    if plain:
        validate_secret(plain, provider)
    root = Path(home).absolute()
    folder = ensure_directory(root / "auth")
    path = folder / (provider + ".dpapi")
    scope = str(path)
    with guard_directory(folder), locked(folder):
        previous = _read(path, root) if path.exists() else None
        if not plain:
            if previous is not None:
                delete(previous, scope)
                unlink_file(path)
            return
        encrypted = protect(canonical_json({"provider": provider, "value": plain}).encode(), scope)
        temp = folder / (uuid.uuid4().hex + ".tmp")
        published = False
        try:
            with open_new(temp) as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            replace_file(temp, path)
            published = True
        finally:
            try:
                unlink_file(temp, missing_ok=True)
            finally:
                if not published:
                    delete(encrypted, scope)
        if previous is not None:
            delete(previous, scope)


def validate_secret(value: str, provider: str) -> None:
    if len(value) > 8192 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise AppError("INVALID_INPUT")
    if provider == "telegram" and not re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,128}", value):
        raise AppError("INVALID_INPUT")
