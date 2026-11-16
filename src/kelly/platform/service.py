"""Native current-user service implementation selected once at import."""

import sys

from ..contracts import AppError

if sys.platform == "win32":
    from . import _windows_service as _implementation
else:
    from . import _launchd_service as _implementation

_log_stream = _implementation._log_stream
_prune_logs = _implementation._prune_logs


def manage(config_path, action, **kwargs):
    """Keep enable/disable compatibility alongside install/uninstall."""
    action = {"install": "enable", "uninstall": "disable"}.get(action, action)
    if sys.platform not in {"win32", "darwin"}:
        raise AppError("UNSUPPORTED_CAPABILITY")
    return _implementation.manage(config_path, action, **kwargs)
