"""Private file permissions, symlink/reparse guards, atomic replace, and directory sync.

POSIX keeps exact owner/mode (0600/0700) and O_NOFOLLOW checks unchanged.
Windows enforces equivalent fail-closed guarantees using NTFS Protected DACLs
(access restricted to the current user SID and SYSTEM) and reparse-point checks.
"""
from __future__ import annotations

import errno
import functools
import os
from pathlib import Path
import stat
import sys
import time

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
    _INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000

    _TOKEN_QUERY = 0x0008
    _TokenUser = 1
    _SDDL_REVISION_1 = 1
    _ACCESS_ALLOWED_ACE_TYPE = 0

    _SYSTEM_SID_STR = "S-1-5-18"
    _ADMINS_SID_STR = "S-1-5-32-544"
    _OWNER_RIGHTS_SID_STR = "S-1-3-4"
    _ERROR_INVALID_FUNCTION = 1
    _ERROR_NOT_SUPPORTED = 50
    _RETRYABLE_WINERRORS = frozenset({5, 32, 33})

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", _SID_AND_ATTRIBUTES)]

    class _ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class _ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    class _ACCESS_ALLOWED_ACE(ctypes.Structure):
        _fields_ = [
            ("Header", _ACE_HEADER),
            ("Mask", wintypes.DWORD),
            ("SidStart", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    _kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetFileAttributesW.restype = wintypes.DWORD
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    _advapi32.GetAclInformation.restype = wintypes.BOOL
    _advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.GetAce.restype = wintypes.BOOL

    @functools.cache
    def current_user_sid() -> str:
        """Return the current process user's Windows SID string (e.g. 'S-1-5-21-...')."""
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            needed = wintypes.DWORD(0)
            _advapi32.GetTokenInformation(token, _TokenUser, None, 0, ctypes.byref(needed))
            buf = ctypes.create_string_buffer(needed.value)
            if not _advapi32.GetTokenInformation(token, _TokenUser, buf, needed.value, ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            user = ctypes.cast(buf, ctypes.POINTER(_TOKEN_USER)).contents
            return _sid_to_string(user.User.Sid)
        finally:
            _kernel32.CloseHandle(token)

    def _sid_to_string(sid_ptr) -> str:
        str_ptr = ctypes.c_void_p()
        if not _advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_ptr)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(str_ptr.value)
        finally:
            _kernel32.LocalFree(str_ptr)

    def _set_private_dacl(path: Path, *, directory: bool = False) -> None:
        sid = current_user_sid()
        flags = "OICI" if directory else ""
        sddl = f"D:P(A;{flags};FA;;;{sid})(A;{flags};FA;;;SY)"
        sd = ctypes.c_void_p()
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(sd), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            present = wintypes.BOOL()
            defaulted = wintypes.BOOL()
            dacl = ctypes.c_void_p()
            if not _advapi32.GetSecurityDescriptorDacl(sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not present.value or not dacl.value:
                raise OSError("Failed to build private Windows DACL")
            sec_info = _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
            status = _advapi32.SetNamedSecurityInfoW(
                str(path), _SE_FILE_OBJECT, sec_info, None, None, dacl, None
            )
            if status != 0:
                raise ctypes.WinError(status)
        finally:
            _kernel32.LocalFree(sd)

    def _check_private_dacl(path: Path) -> bool:
        """True iff owner is current user (or Administrators/SYSTEM) and every ACCESS_ALLOWED ACE
        targets only current user, OWNER RIGHTS, SYSTEM, or BUILTIN\\Administrators (equivalent to
        POSIX owner-only 0600/0700 where only the owner and root have access; Everyone, Users,
        Authenticated Users, and other accounts are refused)."""
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        sd = ctypes.c_void_p()
        status = _advapi32.GetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sd),
        )
        if status != 0:
            return False
        try:
            if not owner.value or not dacl.value:
                return False
            me = current_user_sid()
            allowed_owners = {me, _SYSTEM_SID_STR, _ADMINS_SID_STR}
            allowed_sids = {me, _SYSTEM_SID_STR, _ADMINS_SID_STR, _OWNER_RIGHTS_SID_STR}
            owner_sid = _sid_to_string(owner.value)
            if owner_sid not in allowed_owners:
                return False
            size_info = _ACL_SIZE_INFORMATION()
            if not _advapi32.GetAclInformation(dacl, ctypes.byref(size_info), ctypes.sizeof(size_info), 2):
                return False
            if size_info.AceCount == 0:
                return False
            has_user_ace = False
            sid_offset = _ACCESS_ALLOWED_ACE.SidStart.offset
            for index in range(size_info.AceCount):
                ace_ptr = ctypes.c_void_p()
                if not _advapi32.GetAce(dacl, index, ctypes.byref(ace_ptr)):
                    return False
                header = ctypes.cast(ace_ptr, ctypes.POINTER(_ACE_HEADER)).contents
                if header.AceType == _ACCESS_ALLOWED_ACE_TYPE:
                    ace_sid = _sid_to_string(ace_ptr.value + sid_offset)
                    if ace_sid not in allowed_sids:
                        return False
                    if ace_sid == me or (ace_sid == _OWNER_RIGHTS_SID_STR and owner_sid == me):
                        has_user_ace = True
            return has_user_ace
        finally:
            _kernel32.LocalFree(sd)


def is_reparse_point(path: Path | str) -> bool:
    """True when path is a symlink (POSIX) or symlink/junction/reparse point (Windows)."""
    path = Path(path)
    if not IS_WINDOWS:
        return path.is_symlink()
    attrs = _kernel32.GetFileAttributesW(str(path))
    if attrs == _INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def is_private_stat(info: os.stat_result, path: Path | None = None, *,
                    directory: bool = False, strict: bool = True) -> bool:
    """Check whether an open/statted file or directory is owned by this user and private."""
    if directory and not stat.S_ISDIR(info.st_mode):
        return False
    if not directory and not stat.S_ISREG(info.st_mode):
        return False
    if not IS_WINDOWS:
        if info.st_uid != os.getuid():
            return False
        mode = stat.S_IMODE(info.st_mode)
        if strict:
            return mode == (0o700 if directory else 0o600)
        return (mode & 0o077) == 0
    if path is None:
        return False
    if is_reparse_point(path):
        return False
    return _check_private_dacl(path)


def make_private(path: Path | str, *, directory: bool = False) -> None:
    """Restrict path to owner-only (0600/0700 on POSIX, Protected DACL on Windows). Never follows symlinks."""
    target = Path(path)
    if is_reparse_point(target):
        raise OSError(errno.ELOOP, "Refusing to change permissions on a symbolic link", str(target))
    if not IS_WINDOWS:
        os.chmod(target, 0o700 if directory else 0o600)
        return
    _set_private_dacl(target, directory=directory)


def make_private_best_effort(path: Path | str, *, directory: bool = False) -> None:
    """Best-effort equivalent of make_private for housekeeping (e.g. rotated logs)."""
    target = Path(path)
    if not IS_WINDOWS:
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except (FileNotFoundError, PermissionError):
            return
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return
            raise
        try:
            st = os.fstat(fd)
            if (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)):
                os.fchmod(fd, 0o700 if directory else 0o600)
        except PermissionError:
            pass
        finally:
            os.close(fd)
        return
    try:
        if not target.exists() or is_reparse_point(target):
            return
        if (target.is_dir() if directory else target.is_file()):
            _set_private_dacl(target, directory=directory)
    except (FileNotFoundError, PermissionError):
        return


def ensure_private_dir(path: Path | str, *, error: str = "Directory must belong to the user and be private.") -> Path:
    """Create a private directory if absent, and verify ownership and permissions."""
    target = Path(path)
    if not IS_WINDOWS:
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = target.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError(error)
        return target
    existed = target.exists() or is_reparse_point(target)
    if existed:
        if is_reparse_point(target) or not target.is_dir():
            raise ValueError(error)
        if not _check_private_dacl(target):
            raise ValueError(error)
        return target
    target.mkdir(parents=True, exist_ok=True)
    if is_reparse_point(target):
        raise ValueError(error)
    try:
        _set_private_dacl(target, directory=True)
    except OSError as exc:
        raise ValueError(error) from exc
    if not _check_private_dacl(target):
        raise ValueError(error)
    return target


def open_no_follow(path: Path | str, flags: int, mode: int = 0o600, *, private_on_create: bool = True) -> int:
    """Open path without following a symlink/reparse point.

    On Windows, strips POSIX-only flags (O_NOFOLLOW/O_CLOEXEC/O_NONBLOCK), rejects reparse
    points before and after open, and applies a private DACL when creating a new file.
    """
    target = Path(path)
    if not IS_WINDOWS:
        return os.open(target, flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), mode)
    if is_reparse_point(target):
        raise OSError(errno.ELOOP, "Symbolic link not followed", str(target))
    creating = bool(flags & os.O_CREAT)
    existed = target.exists()
    win_flags = flags
    for name in ("O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK", "O_DIRECTORY"):
        win_flags &= ~getattr(os, name, 0)
    win_flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(target, win_flags, mode)
    try:
        if is_reparse_point(target):
            raise OSError(errno.ELOOP, "Symbolic link not followed", str(target))
        if creating and not existed and private_on_create:
            _set_private_dacl(target, directory=False)
    except BaseException:
        os.close(fd)
        raise
    return fd


def atomic_replace(src: Path | str, dst: Path | str) -> None:
    """os.replace with short bounded retries on Windows sharing/AV violations."""
    if not IS_WINDOWS:
        os.replace(src, dst)
        return
    delay = 0.01
    for attempt in range(6):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            if attempt == 5 or getattr(exc, "winerror", None) not in _RETRYABLE_WINERRORS:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def fsync_directory(path: Path | str) -> None:
    """Fsync a directory descriptor on POSIX; no-op on Windows (NTFS does not support dir fsync)."""
    if IS_WINDOWS:
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def current_uid() -> int | None:
    """os.getuid() on POSIX, None on Windows."""
    return None if IS_WINDOWS else os.getuid()


def pread_at(fd: int, count: int, offset: int) -> bytes:
    """Read up to count bytes at offset without moving the file position (POSIX os.pread fallback on Windows)."""
    if hasattr(os, "pread"):
        return os.pread(fd, count, offset)
    saved = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, count)
    finally:
        os.lseek(fd, saved, os.SEEK_SET)
