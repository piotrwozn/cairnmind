"""Cross-platform exclusive and shared file locks (flock on POSIX, LockFileEx on Windows).

On Windows, locks a single byte at offset 2**62 (OffsetHigh = 0x40000000) so mandatory
byte-range locking never blocks reads or writes to real file content at offset 0.
Handles opened via locked_path are marked non-inheritable.
"""
from __future__ import annotations

import errno
import os
import sys
from contextlib import contextmanager, suppress
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

if sys.platform != "win32":
    import fcntl

    def _lock_exclusive_fd(fd: int, *, blocking: bool) -> None:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(fd, flags)

    def _lock_shared_fd(fd: int, *, blocking: bool) -> None:
        flags = fcntl.LOCK_SH | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(fd, flags)

    def _unlock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
else:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_NOT_LOCKED = 158
    # Offset 2**62 keeps mandatory Windows locks completely clear of lockfile contents (e.g. PID/stamps).
    _LOCK_OFFSET_LOW = 0
    _LOCK_OFFSET_HIGH = 0x40000000

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

    def _overlapped() -> _OVERLAPPED:
        return _OVERLAPPED(Offset=_LOCK_OFFSET_LOW, OffsetHigh=_LOCK_OFFSET_HIGH)

    def _win_lock(handle: int, *, exclusive: bool, blocking: bool) -> None:
        base_flags = _LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0
        # Fast path: non-blocking attempt hits the OS kernel directly with zero stale state.
        ov = _overlapped()
        if _kernel32.LockFileEx(handle, base_flags | _LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, ctypes.byref(ov)):
            return
        error = ctypes.get_last_error()
        if error != _ERROR_LOCK_VIOLATION:
            raise ctypes.WinError(error)
        # If THIS exact handle already held one or more locks (re-lock or shared<->exclusive conversion),
        # UnlockFileEx releases only this handle's own locks (returns ERROR_NOT_LOCKED otherwise).
        unlocked = False
        while _kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(_overlapped())):
            unlocked = True
        if unlocked:
            ov_retry = _overlapped()
            flags = base_flags | (0 if blocking else _LOCKFILE_FAIL_IMMEDIATELY)
            if _kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(ov_retry)):
                return
            error = ctypes.get_last_error()
            if error == _ERROR_LOCK_VIOLATION:
                raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
            raise ctypes.WinError(error)
        if not blocking:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        ov_block = _overlapped()
        if not _kernel32.LockFileEx(handle, base_flags, 0, 1, 0, ctypes.byref(ov_block)):
            error = ctypes.get_last_error()
            if error == _ERROR_LOCK_VIOLATION:
                raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
            raise ctypes.WinError(error)

    def _lock_exclusive_fd(fd: int, *, blocking: bool) -> None:
        _win_lock(msvcrt.get_osfhandle(fd), exclusive=True, blocking=blocking)

    def _lock_shared_fd(fd: int, *, blocking: bool) -> None:
        _win_lock(msvcrt.get_osfhandle(fd), exclusive=False, blocking=blocking)

    def _unlock_fd(fd: int) -> None:
        handle = msvcrt.get_osfhandle(fd)
        while _kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(_overlapped())):
            pass
        error = ctypes.get_last_error()
        if error not in (0, _ERROR_NOT_LOCKED):
            raise ctypes.WinError(error)


def _fileno(file_or_fd) -> int:
    return file_or_fd if isinstance(file_or_fd, int) else file_or_fd.fileno()


def lock_exclusive(file_or_fd, *, blocking: bool = True) -> None:
    """Acquire an exclusive lock on an open file object or file descriptor.

    Raises BlockingIOError when blocking=False and the lock is already held.
    """
    _lock_exclusive_fd(_fileno(file_or_fd), blocking=blocking)


def lock_shared(file_or_fd, *, blocking: bool = True) -> None:
    """Acquire a shared lock on an open file object or file descriptor.

    Raises BlockingIOError when blocking=False and an exclusive lock is held.
    """
    _lock_shared_fd(_fileno(file_or_fd), blocking=blocking)


def unlock(file_or_fd) -> None:
    """Release an exclusive or shared lock on an open file object or file descriptor."""
    _unlock_fd(_fileno(file_or_fd))


@contextmanager
def locked_path(path: Path, *, blocking: bool = True):
    """Open path in append mode (non-inheritable) and hold an exclusive lock for the context."""
    with Path(path).open("a", encoding="utf-8") as handle:
        os.set_inheritable(handle.fileno(), False)
        lock_exclusive(handle, blocking=blocking)
        try:
            yield handle
        finally:
            with suppress(OSError):
                unlock(handle)
