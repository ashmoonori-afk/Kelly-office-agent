"""Compatibility import for one release; new callers should import kelly."""

import warnings

from kelly import __version__ as __version__

warnings.warn(
    "birkin_lite is deprecated; import kelly instead.",
    DeprecationWarning,
    stacklevel=2,
)
