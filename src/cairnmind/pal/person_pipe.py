r"""Windows Named Pipe server/client holding the in-memory person key for standalone TUI (#214).

On POSIX, the open workspace keeps a random 64-hex key inside its private tmux server (in memory only:
never a file, command line, or environment variable). On native Windows there is no tmux, so the open
standalone Textual UI creates a single-instance Named Pipe (`\\.\pipe\cairnmind-person-<namespace>`)
with a Protected DACL restricted to the current user SID (`D:P(A;;GA;;;<user_sid>)`) and
`FILE_FLAG_FIRST_PIPE_INSTANCE`. While the UI runs, only the local service (running as the same user)
queries the pipe to verify the X-Cairnmind-Person header; when the UI exits, the pipe disappears.

Security boundary (same as POSIX tmux server): prevents any CLI command, MCP tool, coding agent, or
service-token-only HTTP call from performing a person's act. It does not claim protection against a
malicious process running under the same Windows user account that connects directly to the pipe.
"""
from __future__ import annotations

import re
import threading

from cairnmind.pal.private_files import IS_WINDOWS, current_user_sid
from cairnmind.pal.runtime_sockets import workspace_namespace

_KEY = re.compile(r"[0-9a-f]{64}")
PIPE_PREFIX = r"\\.\pipe\cairnmind-person-"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _PIPE_ACCESS_DUPLEX = 0x00000003
    _FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    _PIPE_TYPE_MESSAGE = 0x00000004
    _PIPE_READMODE_MESSAGE = 0x00000002
    _PIPE_WAIT = 0x00000000
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    _ERROR_PIPE_CONNECTED = 535
    _ERROR_BROKEN_PIPE = 109
    _ERROR_PIPE_BUSY = 231
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _OPEN_EXISTING = 3
    _SECURITY_SQOS_PRESENT = 0x00100000
    _SECURITY_IDENTIFICATION = 0x00010000
    _SDDL_REVISION_1 = 1
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _TOKEN_QUERY = 0x0008
    _TokenUser = 1
    _TokenAppContainerSid = 31

    class _TOKEN_APPCONTAINER_INFORMATION(ctypes.Structure):
        _fields_ = [("TokenAppContainer", ctypes.c_void_p)]

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", _SID_AND_ATTRIBUTES)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    _kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_SECURITY_ATTRIBUTES),
    ]
    _kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    _kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _kernel32.ConnectNamedPipe.restype = wintypes.BOOL
    _kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    _kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _kernel32.FlushFileBuffers.restype = wintypes.BOOL
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _kernel32.CancelIoEx.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    _kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL


def pipe_name_for(data_dir) -> str:
    """Canonical Windows Named Pipe path for one installation's open workspace."""
    return PIPE_PREFIX + workspace_namespace(data_dir)


def is_valid_pipe_name(settings, value) -> bool:
    """True iff ``value`` is the exact expected Named Pipe name for ``settings.data_dir``."""
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    return value == pipe_name_for(settings.data_dir)


def _current_appcontainer_sid() -> str | None:
    """Return this process's AppContainer package SID when running inside one, else None."""
    if not IS_WINDOWS:
        return None
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        needed = wintypes.DWORD(0)
        _advapi32.GetTokenInformation(token, _TokenAppContainerSid, None, 0, ctypes.byref(needed))
        if not needed.value:
            return None
        buf = ctypes.create_string_buffer(needed.value)
        if not _advapi32.GetTokenInformation(token, _TokenAppContainerSid, buf, needed.value, ctypes.byref(needed)):
            return None
        info = ctypes.cast(buf, ctypes.POINTER(_TOKEN_APPCONTAINER_INFORMATION)).contents
        if not info.TokenAppContainer:
            return None
        str_ptr = ctypes.c_void_p()
        if not _advapi32.ConvertSidToStringSidW(info.TokenAppContainer, ctypes.byref(str_ptr)):
            return None
        try:
            return ctypes.wstring_at(str_ptr.value)
        finally:
            _kernel32.LocalFree(str_ptr)
    finally:
        _kernel32.CloseHandle(token)


def _process_owner_sid(pid: int) -> str | None:
    if not IS_WINDOWS or pid <= 0:
        return None
    proc = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not proc:
        return None
    try:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(proc, _TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            needed = wintypes.DWORD(0)
            _advapi32.GetTokenInformation(token, _TokenUser, None, 0, ctypes.byref(needed))
            if not needed.value:
                return None
            buf = ctypes.create_string_buffer(needed.value)
            if not _advapi32.GetTokenInformation(token, _TokenUser, buf, needed.value, ctypes.byref(needed)):
                return None
            user = ctypes.cast(buf, ctypes.POINTER(_TOKEN_USER)).contents
            str_ptr = ctypes.c_void_p()
            if not _advapi32.ConvertSidToStringSidW(user.User.Sid, ctypes.byref(str_ptr)):
                return None
            try:
                return ctypes.wstring_at(str_ptr.value)
            finally:
                _kernel32.LocalFree(str_ptr)
        finally:
            _kernel32.CloseHandle(token)
    finally:
        _kernel32.CloseHandle(proc)


class PersonPipeServer:
    """Owner-only Windows Named Pipe server holding one in-memory 64-hex person key."""

    def __init__(self, data_dir, key: str):
        if not IS_WINDOWS:
            raise OSError("PersonPipeServer is only supported on Windows")
        if not _KEY.fullmatch(key):
            raise ValueError("The key must be 64 lowercase hexadecimal characters.")
        self.pipe_name = pipe_name_for(data_dir)
        self._key_bytes = (key + "\n").encode("ascii")
        self._stop = threading.Event()
        self._handle = None
        self._sd = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        sid = current_user_sid()
        ac_sid = _current_appcontainer_sid()
        sddl = f"D:P(A;;GA;;;{sid})(A;;GA;;;{ac_sid})" if ac_sid else f"D:P(A;;GA;;;{sid})"
        sd = ctypes.c_void_p()
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(sd), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sa = _SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = sd.value
        sa.bInheritHandle = False
        handle = _kernel32.CreateNamedPipeW(
            self.pipe_name,
            _PIPE_ACCESS_DUPLEX | _FILE_FLAG_FIRST_PIPE_INSTANCE,
            _PIPE_TYPE_MESSAGE | _PIPE_READMODE_MESSAGE | _PIPE_WAIT,
            1,
            256,
            256,
            1000,
            ctypes.byref(sa),
        )
        if handle == _INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            _kernel32.LocalFree(sd)
            raise ctypes.WinError(err)
        self._sd = sd
        self._handle = handle
        self._thread = threading.Thread(target=self._serve, name="cairnmind-person-pipe", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        buf = ctypes.create_string_buffer(64)
        read = wintypes.DWORD(0)
        written = wintypes.DWORD(0)
        while not self._stop.is_set():
            connected = _kernel32.ConnectNamedPipe(self._handle, None)
            if not connected and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                if self._stop.is_set():
                    break
                continue
            try:
                if self._stop.is_set():
                    break
                if _kernel32.ReadFile(self._handle, buf, len(buf), ctypes.byref(read), None):
                    if buf.raw[:read.value].strip() == b"GET":
                        _kernel32.WriteFile(
                            self._handle, self._key_bytes, len(self._key_bytes), ctypes.byref(written), None
                        )
                        _kernel32.FlushFileBuffers(self._handle)
            finally:
                _kernel32.DisconnectNamedPipe(self._handle)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            for _ in range(40):
                if self._handle is not None:
                    _kernel32.CancelIoEx(self._handle, None)
                try:
                    h = _kernel32.CreateFileW(
                        self.pipe_name, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None
                    )
                    if h != _INVALID_HANDLE_VALUE:
                        _kernel32.CloseHandle(h)
                except OSError:
                    pass
                self._thread.join(timeout=0.05)
                if not self._thread.is_alive():
                    break
        if self._handle is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._sd is not None:
            _kernel32.LocalFree(self._sd)
            self._sd = None

    def __enter__(self) -> PersonPipeServer:
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.close()


def read_pipe_key(settings, pipe_name: str, *, timeout_ms: int = 2000) -> tuple[str, str | None]:
    """Query the open workspace's Named Pipe for its in-memory person key.

    Returns ("key", key) only when the pipe exists, its server process belongs to the current user,
    and it answers with a valid 64-hex key. Fails closed with ("unavailable", None) otherwise.
    """
    if not IS_WINDOWS or not is_valid_pipe_name(settings, pipe_name):
        return "unavailable", None
    handle = _INVALID_HANDLE_VALUE
    for _ in range(3):
        handle = _kernel32.CreateFileW(
            pipe_name,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            _SECURITY_SQOS_PRESENT | _SECURITY_IDENTIFICATION,
            None,
        )
        if handle != _INVALID_HANDLE_VALUE:
            break
        err = ctypes.get_last_error()
        if err == _ERROR_PIPE_BUSY:
            if not _kernel32.WaitNamedPipeW(pipe_name, timeout_ms):
                return "unavailable", None
            continue
        return "unavailable", None
    if handle == _INVALID_HANDLE_VALUE:
        return "unavailable", None
    try:
        server_pid = wintypes.ULONG(0)
        if not _kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(server_pid)):
            return "unavailable", None
        owner_sid = _process_owner_sid(int(server_pid.value))
        if owner_sid != current_user_sid():
            return "unavailable", None
        request = b"GET\n"
        written = wintypes.DWORD(0)
        if not _kernel32.WriteFile(handle, request, len(request), ctypes.byref(written), None):
            return "unavailable", None
        buf = ctypes.create_string_buffer(128)
        read = wintypes.DWORD(0)
        if not _kernel32.ReadFile(handle, buf, len(buf), ctypes.byref(read), None):
            return "unavailable", None
        raw = buf.raw[:read.value].decode("ascii", errors="ignore").strip()
        if _KEY.fullmatch(raw):
            return "key", raw
        return "unavailable", None
    except OSError:
        return "unavailable", None
    finally:
        _kernel32.CloseHandle(handle)
