"""Local platform prerequisites; never decrypt credentials or contact providers."""

import ctypes
import shutil
import sys


def _macos_checks() -> dict[str, bool]:
    try:
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        keychain = bool(security.SecItemCopyMatching)
    except (OSError, AttributeError):
        keychain = False
    return {"keychain": keychain, "launchctl": shutil.which("launchctl") is not None}


def _windows_checks() -> dict[str, bool]:
    try:
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        dpapi = bool(crypt.CryptProtectData and crypt.CryptUnprotectData)
    except (OSError, AttributeError):
        dpapi = False
    return {"dpapi": dpapi, "powershell": shutil.which("powershell.exe") is not None}


checks = _windows_checks if sys.platform == "win32" else _macos_checks
