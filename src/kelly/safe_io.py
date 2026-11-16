"""Compatibility import for the native safe I/O implementation."""

import sys

from .platform import safe_io as implementation

sys.modules[__name__] = implementation
