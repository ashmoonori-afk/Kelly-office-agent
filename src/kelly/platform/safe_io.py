"""Choose native file operations once, preserving the implementation namespace."""

import os
import sys

match os.name:
    case "nt":
        from . import _windows_io as implementation
    case _:
        from . import _posix_io as implementation

sys.modules[__name__] = implementation
