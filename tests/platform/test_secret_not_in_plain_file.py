import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from kelly.config import store_secret


@pytest.mark.skipif(sys.platform not in {"win32", "darwin"}, reason="Native secret store")
def test_secret_absent_from_all_created_files(tmp_path: Path, keychains) -> None:
    plain = "synthetic-plaintext-sentinel-67a32"
    store_secret(tmp_path, "xai", SecretStr(plain))
    roots = [tmp_path, *(path.parent for path in keychains)]
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                assert plain.encode() not in data
                assert plain.encode("utf-16-le") not in data
