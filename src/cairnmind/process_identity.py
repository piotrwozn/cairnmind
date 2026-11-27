"""Who a process is: its birth, its command line, its executable, whether it or its group still runs (#226).

A process id is reused once its process has been reaped, so a stop or a signal checks first that the id
still names the process it recorded: the same birth (start time) or the same command line. Linux and WSL
read procfs; macOS has no /proc and asks the kernel through sysctl(3) and proc_pidpath(3) instead. Every
function answers None (or "cannot tell") when the identity cannot be established, never a guess: callers
then refuse to stop or signal. A birth is an opaque string, compared only with one taken on the same
system: clock ticks since boot on Linux, seconds.microseconds since the epoch on macOS.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path
import struct
import sys

# <sys/sysctl.h> of macOS: the names of the queries and the layout of struct kinfo_proc, whose first member
# is struct extern_proc: p_un (its p_starttime, a struct timeval, at offset 0), p_vmspace, p_sigacts,
# p_flag, p_stat (offset 36) and p_pid (offset 40). sizeof(struct kinfo_proc) is 648 on 64-bit macOS.
_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, _KERN_PROC_PGRP, _KERN_ARGMAX, _KERN_PROCARGS2 = 1, 14, 1, 2, 8, 49
_KINFO_PROC_SIZE = 648
_STAT_OFFSET, _PID_OFFSET = 36, 40
_SZOMB = 5
_PIDPATH_SIZE = 4096  # PROC_PIDPATHINFO_MAXSIZE


def _platform() -> str:
    return sys.platform


_PID_MAX = 2**31 - 1  # pid_t is a 32-bit int on Linux and macOS


def _pid(pid) -> int | None:
    # 0 and negative ids name the kernel or a group on macOS, never one process; a larger one would be
    # masked to another id by a C int.
    return pid if type(pid) is int and 0 < pid <= _PID_MAX else None


def start(pid, *, running: bool = False) -> str | None:
    """The birth of process pid, or None when there is no such process or it cannot be read.

    With running=True a zombie (ended, waiting to be reaped) has no birth either.
    """
    pid = _pid(pid)
    if pid is None:
        return None
    system = _platform()
    if system.startswith("linux"):
        try:
            # comm may contain spaces, closing parentheses and cut UTF-8; state follows it, starttime is field 22.
            fields = Path(f"/proc/{pid}/stat").read_text(errors="replace").rsplit(")", 1)[1].split()
            return None if running and fields[0] == "Z" else fields[19]
        except (OSError, IndexError):
            return None
    if system == "darwin":
        return _darwin_birth(_darwin_kinfo(pid), pid, running)
    if system == "win32":
        return _windows_birth(pid, running=running)
    return None


def command_line(pid) -> list[str] | None:
    """The argument vector of process pid as it was executed, or None when it cannot be read."""
    pid = _pid(pid)
    if pid is None:
        return None
    system = _platform()
    if system.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        # A kernel thread or a zombie has an empty command line: nothing to identify it by.
        if not raw:
            return None
        parts = raw.split(b"\0")
        return [os.fsdecode(part) for part in (parts[:-1] if parts[-1] == b"" else parts)]
    if system == "darwin":
        return _darwin_arguments(_darwin_procargs(pid))
    return None


def executable(pid) -> str | None:
    """The resolved path of the program process pid runs, or None when it cannot be read."""
    pid = _pid(pid)
    if pid is None:
        return None
    system = _platform()
    if system.startswith("linux"):
        try:
            return str(Path(f"/proc/{pid}/exe").resolve(strict=True))
        except (OSError, RuntimeError):
            return None
    if system == "darwin":
        return _darwin_path(pid)
    return None


def recorded(pid, birth) -> bool | None:
    """Whether pid may still be signalled as the process recorded with birth.

    True when it still has that birth or no process has that id any more (what is left of its group is still
    its own), False when the id names another process, None when that cannot be told: callers then refuse.
    """
    current = start(pid)
    if current is not None:
        return birth is not None and current == birth
    if _pid(pid) is None:
        return None
    # No birth: either the process is gone, which the kernel affirms, or its identity cannot be read.
    return None if alive(pid) else True


def alive(pid) -> bool:
    """Whether process pid exists (a zombie still does); an id this user may not signal exists too."""
    pid = _pid(pid)
    if pid is None:
        return False
    if _platform() == "win32":
        return _windows_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_alive(pid: int) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _windows_birth(pid: int, *, running: bool = False) -> str | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        if running:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != STILL_ACTIVE:
                return None
        creation, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return str(ticks) if ticks else None
    finally:
        kernel32.CloseHandle(handle)


def group_running(pgid) -> bool | None:
    """Whether a process of group pgid still runs: None when that cannot be told.

    A zombie has ended and only waits to be reaped. It reads every process of the machine (Linux) or asks the
    kernel for the group (macOS), so callers run it off an event loop.
    """
    pgid = _pid(pgid)
    if pgid is None:
        return None
    system = _platform()
    if system.startswith("linux"):
        return _linux_group_running(pgid)
    if system == "darwin":
        return _darwin_group_running(_darwin_group(pgid))
    return None


def _linux_group_running(pgid):
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    group = str(pgid).encode()
    for name in names:
        if not name.isdigit():
            continue
        try:
            # comm is arbitrary bytes (cut at 15, maybe inside a letter) and may
            # contain spaces and closing parentheses; state, ppid, pgrp follow it.
            fields = Path(f"/proc/{name}/stat").read_bytes().rsplit(b")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(fields) <= 2 or fields[2] != group:
            continue
        if fields[0] not in {b"Z", b"X"}:
            return True
        # /proc lists thread groups: the stat of one whose main thread has
        # exited says Z while its other threads still run.
        try:
            if len(os.listdir(f"/proc/{name}/task")) > 1:
                return True
        except OSError:
            continue
    return False


# --- macOS: the parsing is plain Python and tested everywhere; the calls below it run only on a Mac --------

def _darwin_birth(record, pid, running):
    """p_starttime of one struct kinfo_proc, checked to be the record of pid."""
    if not record or len(record) != _KINFO_PROC_SIZE:
        return None
    seconds, micros = struct.unpack_from("=qi", record, 0)
    status, owner = record[_STAT_OFFSET], struct.unpack_from("=i", record, _PID_OFFSET)[0]
    if owner != pid or seconds <= 0 or not 0 <= micros < 1_000_000 or (running and status == _SZOMB):
        return None
    return f"{seconds}.{micros:06d}"


def _darwin_group_running(records):
    """Whether one of a group's struct kinfo_proc records is not a zombie; None when they cannot be read."""
    if records is None or len(records) % _KINFO_PROC_SIZE:
        return None
    for offset in range(0, len(records), _KINFO_PROC_SIZE):
        if struct.unpack_from("=i", records, offset + _PID_OFFSET)[0] <= 0:
            return None
        if records[offset + _STAT_OFFSET] != _SZOMB:
            return True
    return False


def _darwin_arguments(buffer):
    """argv from a KERN_PROCARGS2 buffer: argc, the executable path, NUL padding, then argc strings."""
    if not buffer or len(buffer) < 4:
        return None
    argc = struct.unpack_from("=i", buffer, 0)[0]
    end = buffer.find(b"\0", 4)
    if argc <= 0 or end < 0:
        return None
    position, arguments = end, []
    while position < len(buffer) and buffer[position] == 0:
        position += 1
    while len(arguments) < argc:
        end = buffer.find(b"\0", position)
        if end < 0:
            return None  # cut short: not the whole command line, so no identity
        arguments.append(os.fsdecode(buffer[position:end]))
        position = end + 1
    return arguments


@functools.cache
def _libc():
    import ctypes

    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    library.sysctl.restype = ctypes.c_int
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    return library


def _sysctl(mib, size):
    """The answer of sysctl(3) to mib in a buffer of size bytes; b"" for no answer, None on failure."""
    import ctypes

    try:
        library = _libc()
        names = (ctypes.c_int * len(mib))(*mib)
        length = ctypes.c_size_t(size)
        buffer = ctypes.create_string_buffer(size)
        if library.sysctl(names, len(mib), buffer, ctypes.byref(length), None, 0) != 0:
            return None
        return buffer.raw[:min(length.value, size)]
    except (OSError, AttributeError, ValueError, TypeError):
        return None


def _darwin_kinfo(pid):
    return _sysctl([_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid], _KINFO_PROC_SIZE)


def _darwin_group(pgid):
    # The group can grow between the two calls: room for more, and a retry if it still did not fit.
    import ctypes

    for _ in range(3):
        try:
            length = ctypes.c_size_t(0)
            names = (ctypes.c_int * 4)(_CTL_KERN, _KERN_PROC, _KERN_PROC_PGRP, pgid)
            if _libc().sysctl(names, 4, None, ctypes.byref(length), None, 0) != 0:
                return None
        except (OSError, AttributeError, ValueError, TypeError):
            return None
        found = _sysctl([_CTL_KERN, _KERN_PROC, _KERN_PROC_PGRP, pgid], length.value + 16 * _KINFO_PROC_SIZE)
        if found is not None:
            return found
    return None


def _darwin_procargs(pid):
    limit = _sysctl([_CTL_KERN, _KERN_ARGMAX], 4)
    if not limit or len(limit) != 4:
        return None
    size = struct.unpack("=i", limit)[0]
    return _sysctl([_CTL_KERN, _KERN_PROCARGS2, pid], size) if 0 < size <= 64 * 1024 * 1024 else None


def _darwin_path(pid):
    import ctypes

    try:
        buffer = ctypes.create_string_buffer(_PIDPATH_SIZE)
        length = _libc().proc_pidpath(pid, buffer, _PIDPATH_SIZE)
    except (OSError, AttributeError, ValueError, TypeError):
        return None
    if length <= 0:
        return None
    try:
        return str(Path(os.fsdecode(buffer.raw[:length])).resolve(strict=True))
    except (OSError, RuntimeError):
        return None
