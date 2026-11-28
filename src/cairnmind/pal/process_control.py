"""Cross-platform process group management, detached spawns, and tree termination.

POSIX uses start_new_session=True and os.killpg(pgid, SIGTERM/SIGKILL).
Windows uses CREATE_NEW_PROCESS_GROUP / CREATE_NO_WINDOW and Win32 Job Objects
so timeouts and cancellations terminate the entire child process tree cleanly.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from contextlib import suppress

IS_WINDOWS = sys.platform == "win32"

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectBasicProcessIdList = 3
    _JobObjectExtendedLimitInformation = 9
    _ERROR_MORE_DATA = 234

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


def quote_arg(value) -> str:
    """Quote a single CLI argument for display or command execution (Windows cmd/PS vs POSIX sh)."""
    text = str(value)
    return subprocess.list2cmdline([text]) if IS_WINDOWS else shlex.quote(text)


def format_command(argv) -> str:
    """Format argv for display or command execution (list2cmdline on Windows, shlex.join on POSIX)."""
    items = [str(part) for part in argv]
    return subprocess.list2cmdline(items) if IS_WINDOWS else shlex.join(items)


def detached_popen_kwargs() -> dict:
    """Keyword arguments for spawning a long-lived background process (service, engine, broker)."""
    if not IS_WINDOWS or sys.platform != "win32":
        return {"start_new_session": True}
    # Win32 CreateProcessW forbids combining DETACHED_PROCESS with CREATE_NO_WINDOW;
    # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW runs headless and detaches Ctrl+C.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    return {"creationflags": flags}


def group_popen_kwargs() -> dict:
    """Keyword arguments for spawning a child process in its own controllable group."""
    if not IS_WINDOWS or sys.platform != "win32":
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}


def terminate_pid(pid: int, *, force: bool = False) -> None:
    """Send SIGTERM/SIGKILL on POSIX, or TerminateProcess on Windows.

    Raises ProcessLookupError if the process does not exist.
    """
    if not IS_WINDOWS:
        sig = getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM
        os.kill(pid, sig)
        return
    if sys.platform != "win32":
        raise OSError("Win32 process termination is only available on Windows")
    handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == 87:  # ERROR_INVALID_PARAMETER: no such PID
            raise ProcessLookupError(pid)
        raise ctypes.WinError(err)
    try:
        if not _kernel32.TerminateProcess(handle, 1):
            err = ctypes.get_last_error()
            if err == 5:  # ERROR_ACCESS_DENIED can happen when process already exited
                from cairnmind.pal import process_identity
                if not process_identity.alive(pid):
                    raise ProcessLookupError(pid)
            raise ctypes.WinError(err)
    finally:
        _kernel32.CloseHandle(handle)


def terminate_group(pgid: int, *, force: bool = False) -> None:
    """Terminate a process group on POSIX, or the leader PID on Windows when no Job handle exists."""
    if not IS_WINDOWS:
        sig = getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM
        killpg = getattr(os, "killpg", None)
        if killpg is not None:
            killpg(pgid, sig)
        else:
            os.kill(pgid, sig)
        return
    terminate_pid(pgid, force=force)


class ProcessJob:
    """Tracks a child process and its descendants for reliable tree termination.

    On Windows, wraps a Win32 Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    On POSIX, delegates to process-group signals on the leader PID.
    """

    def __init__(self) -> None:
        self._handle = None
        self._pid: int | None = None
        if IS_WINDOWS and sys.platform == "win32":
            handle = _kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(
                handle, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
            ):
                err = ctypes.get_last_error()
                _kernel32.CloseHandle(handle)
                raise ctypes.WinError(err)
            self._handle = handle

    def attach(self, pid: int) -> bool:
        """Associate pid with this job. Returns False if the process already exited or cannot be attached."""
        self._pid = pid
        if not IS_WINDOWS or sys.platform != "win32":
            return True
        if not self._handle:
            return False
        proc = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not proc:
            return False
        try:
            return bool(_kernel32.AssignProcessToJobObject(self._handle, proc))
        finally:
            _kernel32.CloseHandle(proc)

    def running(self) -> bool | None:
        """Whether any process in this job/group still runs; None when unknown."""
        if not IS_WINDOWS:
            if self._pid is None:
                return False
            from cairnmind.pal import process_identity
            killpg = getattr(os, "killpg", None)
            if killpg is not None:
                try:
                    killpg(self._pid, 0)
                except ProcessLookupError:
                    return False
                except OSError:
                    pass
            result = process_identity.group_running(self._pid)
            return None if result is None else result
        if sys.platform != "win32":
            return False
        if not self._handle:
            return False
        # JOBOBJECT_BASIC_PROCESS_ID_LIST starts with two DWORDs:
        # NumberOfAssignedProcesses, NumberOfProcessIdsInList, followed by ULONG_PTR[n].
        buf = ctypes.create_string_buffer(64)
        returned = wintypes.DWORD(0)
        ok = _kernel32.QueryInformationJobObject(
            self._handle, _JobObjectBasicProcessIdList, buf, len(buf), ctypes.byref(returned)
        )
        if not ok:
            err = ctypes.get_last_error()
            if err == _ERROR_MORE_DATA:
                return True
            return None
        assigned = wintypes.DWORD.from_buffer(buf, 0).value
        return assigned > 0

    def terminate(self, *, grace_seconds: float = 0.5) -> None:
        """Terminate all processes in the job/group."""
        if not IS_WINDOWS:
            if self._pid is None:
                return
            killpg = getattr(os, "killpg", None)
            if killpg is None:
                return
            try:
                killpg(self._pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + grace_seconds
            while time.monotonic() < deadline:
                if self.running() is False:
                    return
                time.sleep(0.02)
            sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            with suppress(ProcessLookupError):
                killpg(self._pid, sigkill)
            return
        if sys.platform != "win32":
            return
        if self._handle:
            _kernel32.TerminateJobObject(self._handle, 1)
        elif self._pid is not None:
            with suppress(ProcessLookupError):
                terminate_pid(self._pid, force=True)

    def close(self) -> None:
        if sys.platform == "win32" and self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None
