"""Cross-platform advisory file locks (POSIX flock and Windows LockFileEx).

On POSIX systems (Linux, WSL, macOS), delegates directly to ``fcntl.flock`` so
semantics and lock compatibility with earlier releases stay identical. On
native Windows, uses Win32 ``LockFileEx`` / ``UnlockFileEx`` on byte 0 so both
blocking and non-blocking shared/exclusive locks work without moving the file
pointer or hitting ``msvcrt.locking``'s 10-second deadlock timeout.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
from pathlib import Path
import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    fcntl = None  # type: ignore[assignment]

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_SHARING_VIOLATION = 32
    _ERROR_NOT_LOCKED = 158

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", ctypes.c_void_p),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LockFileEx = _kernel32.LockFileEx
    _LockFileEx.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _LockFileEx.restype = wintypes.BOOL

    _UnlockFileEx = _kernel32.UnlockFileEx
    _UnlockFileEx.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _UnlockFileEx.restype = wintypes.BOOL
else:
    import fcntl


def _fileno(fd_or_handle) -> int:
    return fd_or_handle if isinstance(fd_or_handle, int) else fd_or_handle.fileno()


def lock_file(fd_or_handle, *, exclusive: bool = True, blocking: bool = True) -> None:
    """Acquire an advisory lock on an open file descriptor or file object.

    Raises ``BlockingIOError`` when ``blocking=False`` and the lock is held by
    another handle or process.
    """
    if sys.platform != "win32":
        operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(fd_or_handle, operation)
        return
    fd = _fileno(fd_or_handle)
    handle = msvcrt.get_osfhandle(fd)
    flags = (_LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0) | (0 if blocking else _LOCKFILE_FAIL_IMMEDIATELY)
    overlapped = _OVERLAPPED()
    ok = _LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped))
    if not ok:
        error = ctypes.get_last_error()
        if not blocking and error in (_ERROR_LOCK_VIOLATION, _ERROR_SHARING_VIOLATION):
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        raise ctypes.WinError(error)


def unlock_file(fd_or_handle) -> None:
    """Release an advisory lock acquired by ``lock_file``."""
    if sys.platform != "win32":
        fcntl.flock(fd_or_handle, fcntl.LOCK_UN)
        return
    fd = _fileno(fd_or_handle)
    handle = msvcrt.get_osfhandle(fd)
    overlapped = _OVERLAPPED()
    ok = _UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
    if not ok:
        error = ctypes.get_last_error()
        if error != _ERROR_NOT_LOCKED:
            raise ctypes.WinError(error)


@contextmanager
def file_lock(path: Path | str, *, exclusive: bool = True, blocking: bool = True):
    """Open ``path`` and hold an advisory file lock for the duration of the block."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as handle:
        lock_file(handle, exclusive=exclusive, blocking=blocking)
        try:
            yield handle
        finally:
            try:
                unlock_file(handle)
            except OSError:
                pass
