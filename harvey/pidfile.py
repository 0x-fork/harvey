"""The heartbeat loop's PID file, and a liveness check that never kills.

``os.kill(pid, 0)`` is the POSIX idiom for "does this process exist?", but on
Windows ``os.kill`` with any signal other than CTRL_C_EVENT/CTRL_BREAK_EVENT
calls TerminateProcess — the "check" would kill the loop it was checking. So
Windows asks the kernel instead (OpenProcess + GetExitCodeProcess).
"""

import logging
import os
import sys
from pathlib import Path

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.pidfile")

PID_FILE = PROJECT_ROOT / "data" / "harvey.pid"

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def _pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means the process exists but belongs to someone else.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """True if a process with this PID is running. Never signals or kills it."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def read_pid(path: Path | None = None) -> int | None:
    path = path or PID_FILE
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def write_pid_file(path: Path | None = None) -> None:
    """Record this process as the running loop (best effort)."""
    path = path or PID_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()))
    except OSError as e:
        logger.warning("Could not write PID file: %s", e)


def remove_pid_file(path: Path | None = None) -> None:
    """Remove the PID file — only if it still names this process, so a loop
    exiting late never erases a newer loop's record."""
    path = path or PID_FILE
    if read_pid(path) == os.getpid():
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not remove PID file: %s", e)
