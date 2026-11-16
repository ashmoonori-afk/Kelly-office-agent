"""All macOS tests and their children use disposable, explicitly selected keychains."""

import ctypes as c
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def keychains(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, ...]]:
    if sys.platform != "darwin":
        yield ()
        return
    security = c.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    core.CFRelease.argtypes = [c.c_void_p]
    core.CFRelease.restype = None
    security.SecKeychainCreate.argtypes = [
        c.c_char_p, c.c_uint32, c.c_void_p, c.c_ubyte, c.c_void_p, c.POINTER(c.c_void_p),
    ]
    security.SecKeychainUnlock.argtypes = [c.c_void_p, c.c_uint32, c.c_void_p, c.c_ubyte]
    security.SecKeychainDelete.argtypes = [c.c_void_p]
    folder = tmp_path_factory.mktemp("keychains")
    paths = tuple(folder / f"{name}.keychain-db" for name in ("primary", "other"))
    handles = []
    try:
        for path in paths:
            password = uuid.uuid4().hex.encode()
            handle = c.c_void_p()
            assert security.SecKeychainCreate(
                bytes(path), len(password), password, False, None, c.byref(handle),
            ) == 0
            handles.append(handle)
            assert security.SecKeychainUnlock(handle, len(password), password, True) == 0
        yield paths
    finally:
        for handle in handles:
            try:
                assert security.SecKeychainDelete(handle) == 0
            finally:
                core.CFRelease(handle)


@pytest.fixture(autouse=True)
def isolated_keychain(keychains: tuple[Path, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    if keychains:
        monkeypatch.setenv("KELLY_KEYCHAIN_PATH", str(keychains[0]))
