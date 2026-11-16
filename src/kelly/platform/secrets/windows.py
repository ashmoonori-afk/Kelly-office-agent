"""Current-user DPAPI, preserving the v0.1 encrypted byte format."""

from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

from ...contracts import AppError


def _crypt_secret(data, *, protect):
    """Windows current-user DPAPI; never machine-wide and never a UI prompt."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise AppError("SECRET_STORE_ERROR")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        if target.size > 65536:
            raise AppError("SECRET_STORE_ERROR")
        return ctypes.string_at(target.data, target.size)
    finally:
        if not protect:
            ctypes.memset(target.data, 0, target.size)
        kernel.LocalFree(target.data)


def protect(data: bytes, scope: str = "") -> bytes:
    return _crypt_secret(data, protect=True)


def unprotect(data: bytes, scope: str = "") -> bytes:
    return _crypt_secret(data, protect=False)


def delete(data: bytes, scope: str = "") -> None:
    """DPAPI has no external item; the caller owns the encrypted file."""


def locked(folder: Path) -> AbstractContextManager[None]:
    return nullcontext()
