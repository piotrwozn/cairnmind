"""Cross-platform exclusive file locks (flock on POSIX, LockFileEx on Windows)."""
from __future__ import annotations

from contextlib import contextmanager
import errno
from pathlib import Path
import sys

IS_WINDOWS = sys.platform == "win32"

if not IS_WINDOWS:
    import fcntl
else:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_NOT_LOCKED = 158

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_OVERLAPPED),
    ]
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_OVERLAPPED),
    ]
    _kernel32.UnlockFileEx.restype = wintypes.BOOL


def _fileno(file_or_fd) -> int:
    return file_or_fd if isinstance(file_or_fd, int) else file_or_fd.fileno()


def lock_exclusive(file_or_fd, *, blocking: bool = True) -> None:
    """Acquire an exclusive lock on an open file object or file descriptor.

    Raises BlockingIOError when blocking=False and the lock is already held.
    """
    fd = _fileno(file_or_fd)
    if not IS_WINDOWS:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(fd, flags)
        return
    handle = msvcrt.get_osfhandle(fd)
    flags = _LOCKFILE_EXCLUSIVE_LOCK | (0 if blocking else _LOCKFILE_FAIL_IMMEDIATELY)
    overlapped = _OVERLAPPED()
    if not _kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
        error = ctypes.get_last_error()
        if error == _ERROR_LOCK_VIOLATION:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        raise ctypes.WinError(error)


def lock_shared(file_or_fd, *, blocking: bool = True) -> None:
    """Acquire a shared lock on an open file object or file descriptor.

    Raises BlockingIOError when blocking=False and an exclusive lock is held.
    """
    fd = _fileno(file_or_fd)
    if not IS_WINDOWS:
        flags = fcntl.LOCK_SH | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(fd, flags)
        return
    handle = msvcrt.get_osfhandle(fd)
    flags = 0 if blocking else _LOCKFILE_FAIL_IMMEDIATELY
    overlapped = _OVERLAPPED()
    if not _kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
        error = ctypes.get_last_error()
        if error == _ERROR_LOCK_VIOLATION:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        raise ctypes.WinError(error)


def unlock(file_or_fd) -> None:
    """Release an exclusive lock on an open file object or file descriptor."""
    fd = _fileno(file_or_fd)
    if not IS_WINDOWS:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    handle = msvcrt.get_osfhandle(fd)
    overlapped = _OVERLAPPED()
    if not _kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_LOCKED:
            return
        raise ctypes.WinError(error)


@contextmanager
def locked_path(path: Path, *, blocking: bool = True):
    """Open path in append mode and hold an exclusive lock for the context."""
    with Path(path).open("a", encoding="utf-8") as handle:
        lock_exclusive(handle, blocking=blocking)
        try:
            yield handle
        finally:
            try:
                unlock(handle)
            except OSError:
                pass
