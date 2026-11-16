"""Doctor reports local platform health without decrypting any secret."""

from types import SimpleNamespace

import pytest

from kelly import config, operations
from kelly.platform import doctor


@pytest.mark.parametrize("system", ["macos", "windows"])
@pytest.mark.parametrize("healthy", [True, False])
def test_doctor_checks_native_prerequisites(tmp_path, monkeypatch, system, healthy):
    path = tmp_path / "config.json"
    config.create_config(path, config.AppConfig(home="data"))
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "synthetic-tool" if healthy else None)
    monkeypatch.setattr(
        doctor.ctypes, "CDLL", lambda _: SimpleNamespace(SecItemCopyMatching=True),
    )
    monkeypatch.setattr(
        doctor.ctypes, "WinDLL",
        lambda *args, **kwargs: SimpleNamespace(CryptProtectData=True, CryptUnprotectData=True),
        raising=False,
    )
    monkeypatch.setattr(
        doctor, "checks", doctor._macos_checks if system == "macos" else doctor._windows_checks,
    )
    monkeypatch.setattr(
        config, "stored_secret", lambda *args: pytest.fail("doctor decrypted a credential"),
    )
    report = operations.doctor(path)
    assert report["ok"] is healthy
    assert report["network_calls"] == 0
    assert report["platform_checks"] == (
        {"keychain": True, "launchctl": healthy} if system == "macos"
        else {"dpapi": True, "powershell": healthy}
    )
