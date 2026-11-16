"""Native defaults do not change explicitly configured data homes."""

import os
import sys
from pathlib import Path

import pytest

from kelly.config import AppConfig, home_path
from kelly.platform.paths import default_home


def test_default_home_uses_native_account_data_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-data"))
    expected = (
        Path(os.environ["LOCALAPPDATA"]) / "Kelly"
        if sys.platform == "win32"
        else Path.home() / "Library" / "Application Support" / "Kelly"
    )
    assert default_home() == expected


def test_explicit_relative_home_stays_relative_to_config(tmp_path: Path) -> None:
    assert home_path(AppConfig(home="custom-data"), tmp_path / "config.json") == (
        tmp_path / "custom-data"
    )
