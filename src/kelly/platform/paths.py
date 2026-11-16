"""Select the native data home once; explicit configuration stays authoritative."""

import os
import sys
from pathlib import Path

from ..contracts import AppError

match sys.platform:
    case "win32":
        def default_home() -> Path:
            return Path(os.environ["LOCALAPPDATA"]) / "Kelly"
    case "darwin":
        def default_home() -> Path:
            return Path.home() / "Library" / "Application Support" / "Kelly"
    case _:
        def default_home() -> Path:
            raise AppError("UNSUPPORTED_CAPABILITY")
