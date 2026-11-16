"""Explicit file-keychain access; never search other keychains for a reference."""

import ctypes as c
import fcntl
import hashlib
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from ...contracts import AppError


class _Attribute(c.Structure):
    """Mutable native attribute buffer passed across the C ABI."""
    _fields_ = [("tag", c.c_uint32), ("length", c.c_uint32), ("data", c.c_char_p)]


_security = c.CDLL("/System/Library/Frameworks/Security.framework/Security")
_core = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_core.CFRelease.argtypes = [c.c_void_p]
_core.CFRelease.restype = None
_security.SecKeychainOpen.argtypes = [c.c_char_p, c.POINTER(c.c_void_p)]
_security.SecKeychainGetStatus.argtypes = [c.c_void_p, c.POINTER(c.c_uint32)]
_security.SecKeychainItemCreateNew.argtypes = [
    c.c_uint32, c.c_uint32, c.c_uint32, c.c_void_p, c.POINTER(c.c_void_p),
]
_security.SecKeychainItemSetAttribute.argtypes = [c.c_void_p, c.POINTER(_Attribute)]
_security.SecKeychainItemAddNoUI.argtypes = [c.c_void_p, c.c_void_p]
_security.SecKeychainFindGenericPassword.argtypes = [
    c.c_void_p, c.c_uint32, c.c_char_p, c.c_uint32, c.c_char_p,
    c.POINTER(c.c_uint32), c.POINTER(c.c_void_p), c.POINTER(c.c_void_p),
]
_security.SecKeychainItemDelete.argtypes = [c.c_void_p]
_security.SecKeychainItemFreeContent.argtypes = [c.c_void_p, c.c_void_p]
_security.SecKeychainSetUserInteractionAllowed.argtypes = [c.c_ubyte]
for _name in (
    "SecKeychainOpen", "SecKeychainGetStatus",
    "SecKeychainItemCreateNew", "SecKeychainItemSetAttribute", "SecKeychainItemAddNoUI",
    "SecKeychainFindGenericPassword",
    "SecKeychainItemDelete", "SecKeychainItemFreeContent", "SecKeychainSetUserInteractionAllowed",
):
    getattr(_security, _name).restype = c.c_int32

_PREFIX: Final = b"KELLY-KC-1:"


def _check(status: int) -> None:
    if status != 0:
        raise AppError("SECRET_STORE_ERROR")


@contextmanager
def _keychain() -> Iterator[c.c_void_p]:
    path = os.environ.get(
        "KELLY_KEYCHAIN_PATH", str(Path.home() / "Library/Keychains/login.keychain-db")
    )
    if not path or "\0" in path or not Path(path).is_absolute():
        raise AppError("SECRET_STORE_ERROR")
    _check(_security.SecKeychainSetUserInteractionAllowed(False))
    handle = c.c_void_p()
    _check(_security.SecKeychainOpen(os.fsencode(path), c.byref(handle)))
    try:
        status = c.c_uint32()
        _check(_security.SecKeychainGetStatus(handle, c.byref(status)))
        if not status.value & 1:  # kSecUnlockStateStatus
            raise AppError("SECRET_STORE_ERROR")
        yield handle
    finally:
        _core.CFRelease(handle)


def _service(scope: str) -> bytes:
    return b"org.kelly.secret.v1." + hashlib.sha256(os.fsencode(scope)).hexdigest().encode()


def _account(data: bytes) -> bytes:
    if not re.fullmatch(rb"KELLY-KC-1:[0-9a-f]{32}", data):
        raise AppError("SECRET_STORE_ERROR")
    return data[len(_PREFIX):]


def protect(data: bytes, scope: str = "") -> bytes:
    if len(data) > 65536:
        raise AppError("SECRET_STORE_ERROR")
    account = uuid.uuid4().hex.encode()
    service = _service(scope)
    with _keychain() as keychain:
        item = c.c_void_p()
        _check(_security.SecKeychainItemCreateNew(
            int.from_bytes(b"genp"), int.from_bytes(b"aapl"), len(data), data, c.byref(item),
        ))
        try:
            for tag, value in ((b"svce", service), (b"acct", account)):
                attribute = _Attribute(int.from_bytes(tag), len(value), value)
                _check(_security.SecKeychainItemSetAttribute(item, c.byref(attribute)))
            # Unlike AddGenericPassword, AddNoUI never falls back to the default keychain.
            _check(_security.SecKeychainItemAddNoUI(keychain, item))
        finally:
            _core.CFRelease(item)
    return _PREFIX + account


def unprotect(data: bytes, scope: str = "") -> bytes:
    account, service = _account(data), _service(scope)
    length, buffer = c.c_uint32(), c.c_void_p()
    with _keychain() as keychain:
        _check(_security.SecKeychainFindGenericPassword(
            keychain, len(service), service, len(account), account,
            c.byref(length), c.byref(buffer), None,
        ))
        try:
            if length.value > 65536:
                raise AppError("SECRET_STORE_ERROR")
            return c.string_at(buffer, length.value)
        finally:
            c.memset(buffer, 0, length.value)
            _security.SecKeychainItemFreeContent(None, buffer)


def delete(data: bytes, scope: str = "") -> None:
    account, service = _account(data), _service(scope)
    item = c.c_void_p()
    with _keychain() as keychain:
        status = _security.SecKeychainFindGenericPassword(
            keychain, len(service), service, len(account), account, None, None, c.byref(item),
        )
        if status == -25300:  # errSecItemNotFound: clearing twice is harmless.
            return
        _check(status)
        try:
            _check(_security.SecKeychainItemDelete(item))
        finally:
            _core.CFRelease(item)


@contextmanager
def locked(folder: Path) -> Iterator[None]:
    """Serialize readers and writers across threads/processes without another file."""
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)
