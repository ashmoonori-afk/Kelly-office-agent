"""Darwin physical footprint accounting, including compressed memory."""

import ctypes as c
import errno
from typing import Final


class _RusageInfo(c.Structure):
    # sys/resource.h, rusage_info_v0 (RUSAGE_INFO_V0).
    _fields_ = [
        ("uuid", c.c_ubyte * 16),
        ("user_time", c.c_uint64),
        ("system_time", c.c_uint64),
        ("pkg_idle_wkups", c.c_uint64),
        ("interrupt_wkups", c.c_uint64),
        ("pageins", c.c_uint64),
        ("wired_size", c.c_uint64),
        ("resident_size", c.c_uint64),
        ("phys_footprint", c.c_uint64),
        ("proc_start_abstime", c.c_uint64),
        ("proc_exit_abstime", c.c_uint64),
    ]


_LIB: Final = c.CDLL("/usr/lib/libproc.dylib", use_errno=True)
_LIB.proc_pid_rusage.argtypes = [c.c_int, c.c_int, c.c_void_p]
_LIB.proc_pid_rusage.restype = c.c_int


def footprint(pid: int) -> int:
    info = _RusageInfo()
    if _LIB.proc_pid_rusage(pid, 0, c.byref(info)):
        error = c.get_errno()
        raise OSError(error or errno.EIO, "process memory sample failed")
    return info.phys_footprint
