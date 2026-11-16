import ctypes as c
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import SecretStr

from kelly.contracts import AppError
from kelly.platform import secrets

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Native macOS Keychain")


@pytest.mark.parametrize("plain", [b"", b"synthetic-secret", b"\x00\xff" * 32768])
def test_native_binary_roundtrip_and_deletion(plain: bytes) -> None:
    reference = secrets.protect(plain)
    try:
        assert len(reference) <= 128
        assert secrets.unprotect(reference) == plain
    finally:
        secrets.delete(reference)
    with pytest.raises(AppError) as error:
        secrets.unprotect(reference)
    assert error.value.code == "SECRET_STORE_ERROR"


def test_reference_is_scoped_to_keychain_and_slot(keychains, monkeypatch) -> None:
    reference = secrets.protect(b"synthetic-isolation", "slot-one")
    try:
        with pytest.raises(AppError):
            secrets.unprotect(reference, "slot-two")
        secrets.delete(reference, "slot-two")
        monkeypatch.setenv("KELLY_KEYCHAIN_PATH", str(keychains[1]))
        with pytest.raises(AppError):
            secrets.unprotect(reference, "slot-one")
        secrets.delete(reference, "slot-one")
        monkeypatch.setenv("KELLY_KEYCHAIN_PATH", str(keychains[0]))
        assert secrets.unprotect(reference, "slot-one") == b"synthetic-isolation"
    finally:
        monkeypatch.setenv("KELLY_KEYCHAIN_PATH", str(keychains[0]))
        secrets.delete(reference, "slot-one")


def test_invalid_custom_keychain_does_not_fall_back(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KELLY_KEYCHAIN_PATH", str(tmp_path / "missing.keychain-db"))
    with pytest.raises(AppError) as error:
        secrets.protect(b"synthetic-missing")
    assert error.value.code == "SECRET_STORE_ERROR"


def test_keychain_disappearing_after_open_cannot_redirect_creation(tmp_path, monkeypatch) -> None:
    from kelly.platform.secrets import macos

    @contextmanager
    def vanished():
        handle = c.c_void_p()
        assert macos._security.SecKeychainOpen(bytes(tmp_path / "gone"), c.byref(handle)) == 0
        try:
            yield handle
        finally:
            macos._core.CFRelease(handle)

    monkeypatch.setattr(macos, "_keychain", vanished)
    with pytest.raises(AppError):
        secrets.protect(b"synthetic-vanished")


def test_overwrite_and_clear_retire_items_in_another_process(tmp_path: Path) -> None:
    from kelly.config import store_secret, stored_secret

    path = tmp_path / "auth" / "xai.dpapi"
    store_secret(tmp_path, "xai", SecretStr("synthetic-original"))
    old = path.read_bytes()
    result = subprocess.run(
        [sys.executable, "-c",
         ("from pathlib import Path; from pydantic import SecretStr; "
         "from kelly.config import stored_secret, store_secret; import sys; "
         "home=Path(sys.argv[1]); "
         "assert stored_secret(home,'xai').get_secret_value()=='synthetic-original'; "
         "store_secret(home,'xai',SecretStr('synthetic-replacement'))"), str(tmp_path)],
        env=os.environ.copy(), capture_output=True, text=True, timeout=30, check=True,
    )
    assert result.stdout == ""
    actual = stored_secret(tmp_path, "xai")
    assert actual is not None and actual.get_secret_value() == "synthetic-replacement"
    with pytest.raises(AppError):
        secrets.unprotect(old, str(path))
    current = path.read_bytes()
    store_secret(tmp_path, "xai", SecretStr(""))
    with pytest.raises(AppError):
        secrets.unprotect(current, str(path))
    assert not path.exists()


def test_failed_publication_keeps_old_secret_and_removes_new_item(tmp_path, monkeypatch) -> None:
    from kelly.config import store_secret, stored_secret
    from kelly.platform.secrets import store

    store_secret(tmp_path, "xai", SecretStr("synthetic-original"))
    created = []
    real_protect = store.protect

    def record(data: bytes, scope: str) -> bytes:
        reference = real_protect(data, scope)
        created.append(reference)
        return reference

    def fail(source: Path, destination: Path) -> None:
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(store, "protect", record)
    monkeypatch.setattr(store, "replace_file", fail)
    with pytest.raises(OSError):
        store_secret(tmp_path, "xai", SecretStr("synthetic-replacement"))
    actual = stored_secret(tmp_path, "xai")
    assert actual is not None and actual.get_secret_value() == "synthetic-original"
    assert len(created) == 1
    with pytest.raises(AppError):
        secrets.unprotect(created[0], str(tmp_path / "auth/xai.dpapi"))
    assert [path.name for path in (tmp_path / "auth").iterdir()] == ["xai.dpapi"]
