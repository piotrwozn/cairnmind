"""Private file permissions, symlink/reparse guards, atomic replace, and directory sync.

POSIX keeps exact owner/mode (0600/0700) and O_NOFOLLOW checks unchanged.
Windows enforces equivalent fail-closed guarantees using NTFS Protected DACLs
(access restricted to the current user SID, SYSTEM, and Administrators) created
atomically via CreateFileW/CreateDirectoryW SECURITY_ATTRIBUTES and reparse-point checks.
"""
from __future__ import annotations

import errno
import functools
import os
import secrets
import stat
import sys
import time
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _FILE_ATTRIBUTE_DIRECTORY = 0x0010
    _FILE_ATTRIBUTE_NORMAL = 0x0080
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _CREATE_NEW = 1
    _CREATE_ALWAYS = 2
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _TRUNCATE_EXISTING = 5

    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000

    _TOKEN_QUERY = 0x0008
    _TokenUser = 1
    _TokenGroups = 2
    _TokenAppContainerSid = 31
    _SE_GROUP_ENABLED = 0x00000004
    _SDDL_REVISION_1 = 1
    _ACCESS_ALLOWED_ACE_TYPE = 0

    _SYSTEM_SID_STR = "S-1-5-18"
    _ADMINS_SID_STR = "S-1-5-32-544"
    _OWNER_RIGHTS_SID_STR = "S-1-3-4"
    _ERROR_ACCESS_DENIED = 5
    _ERROR_ALREADY_EXISTS = 183
    _DELETE = 0x00010000
    _FileRenameInfoEx = 22
    _FILE_RENAME_FLAG_REPLACE_IF_EXISTS = 0x00000001
    _FILE_RENAME_FLAG_POSIX_SEMANTICS = 0x00000002
    _RETRYABLE_WINERRORS = frozenset({5, 32, 33})

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", _SID_AND_ATTRIBUTES)]

    class _TOKEN_APPCONTAINER_INFORMATION(ctypes.Structure):
        _fields_ = [("TokenAppContainer", ctypes.c_void_p)]

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
    _kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SECURITY_ATTRIBUTES)]
    _kernel32.CreateDirectoryW.restype = wintypes.BOOL
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL

    class _FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_int64),
            ("LastAccessTime", ctypes.c_int64),
            ("LastWriteTime", ctypes.c_int64),
            ("ChangeTime", ctypes.c_int64),
            ("FileAttributes", wintypes.DWORD),
        ]

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
    _advapi32.GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorOwner.restype = wintypes.BOOL
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

    def _sid_to_string(sid_ptr: Any) -> str:
        str_ptr = ctypes.c_void_p()
        if not _advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_ptr)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not str_ptr.value:
                raise OSError("Failed to convert SID to string")
            return ctypes.wstring_at(str_ptr.value)
        finally:
            _kernel32.LocalFree(str_ptr)

    @functools.cache
    def _windows_current_user_sid() -> str:
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

    @functools.cache
    def _current_appcontainer_sid() -> str | None:
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
            return _sid_to_string(info.TokenAppContainer)
        except OSError:
            return None
        finally:
            _kernel32.CloseHandle(token)

    @functools.cache
    def _current_token_is_admin() -> bool:
        """True when the current process token holds an enabled BUILTIN\\Administrators SID."""
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            return False
        try:
            needed = wintypes.DWORD(0)
            _advapi32.GetTokenInformation(token, _TokenGroups, None, 0, ctypes.byref(needed))
            if not needed.value:
                return False
            buf = ctypes.create_string_buffer(needed.value)
            if not _advapi32.GetTokenInformation(token, _TokenGroups, buf, needed.value, ctypes.byref(needed)):
                return False
            count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
            offset = ctypes.sizeof(ctypes.c_void_p)
            array_type = _SID_AND_ATTRIBUTES * count
            groups = ctypes.cast(ctypes.addressof(buf) + offset, ctypes.POINTER(array_type)).contents
            for index in range(count):
                entry = groups[index]
                if not entry.Sid or not (entry.Attributes & _SE_GROUP_ENABLED):
                    continue
                if _sid_to_string(entry.Sid) == _ADMINS_SID_STR:
                    return True
            return False
        except OSError:
            return False
        finally:
            _kernel32.CloseHandle(token)

    def _private_sddl(*, directory: bool = False) -> str:
        sid = _windows_current_user_sid()
        flags = "OICI" if directory else ""
        aces = [
            f"(A;{flags};FA;;;{sid})",
            f"(A;{flags};FA;;;SY)",
            f"(A;{flags};FA;;;BA)",
        ]
        ac_sid = _current_appcontainer_sid()
        if ac_sid:
            aces.append(f"(A;{flags};FA;;;{ac_sid})")
        return "D:P" + "".join(aces)

    def _build_private_sd(*, directory: bool = False) -> ctypes.c_void_p:
        sd = ctypes.c_void_p()
        sddl = _private_sddl(directory=directory)
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, _SDDL_REVISION_1, ctypes.byref(sd), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return sd

    def _set_private_dacl(path: Path, *, directory: bool = False) -> None:
        sd = _build_private_sd(directory=directory)
        try:
            present = wintypes.BOOL()
            defaulted = wintypes.BOOL()
            dacl = ctypes.c_void_p()
            if not _advapi32.GetSecurityDescriptorDacl(
                sd, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
            ):
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
        targets only current user, OWNER RIGHTS, SYSTEM, BUILTIN\\Administrators (equivalent to
        POSIX root), or the current AppContainer SID. Everyone, Users, Authenticated Users, and
        other accounts are always refused fail-closed."""
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
            me = _windows_current_user_sid()
            allowed_owners = {me, _SYSTEM_SID_STR, _ADMINS_SID_STR}
            allowed_sids = {me, _SYSTEM_SID_STR, _ADMINS_SID_STR, _OWNER_RIGHTS_SID_STR}
            ac_sid = _current_appcontainer_sid()
            if ac_sid:
                allowed_sids.add(ac_sid)
            owner_sid = _sid_to_string(owner.value)
            if owner_sid not in allowed_owners:
                return False
            size_info = _ACL_SIZE_INFORMATION()
            if not _advapi32.GetAclInformation(dacl, ctypes.byref(size_info), ctypes.sizeof(size_info), 2):
                return False
            if size_info.AceCount == 0:
                return False
            has_user_ace = False
            is_admin = _current_token_is_admin()
            sid_offset = _ACCESS_ALLOWED_ACE.SidStart.offset
            for index in range(size_info.AceCount):
                ace_ptr = ctypes.c_void_p()
                if not _advapi32.GetAce(dacl, index, ctypes.byref(ace_ptr)):
                    return False
                header = ctypes.cast(ace_ptr, ctypes.POINTER(_ACE_HEADER)).contents
                if header.AceType == _ACCESS_ALLOWED_ACE_TYPE:
                    if not ace_ptr.value:
                        return False
                    ace_sid = _sid_to_string(ace_ptr.value + sid_offset)
                    if ace_sid not in allowed_sids:
                        return False
                    if (ace_sid == me
                            or (ace_sid == _OWNER_RIGHTS_SID_STR and owner_sid == me)
                            or (ace_sid == _ADMINS_SID_STR and is_admin)):
                        has_user_ace = True
            return has_user_ace
        finally:
            _kernel32.LocalFree(sd)


    def current_user_sid() -> str | None:
        """Return the current process user's Windows SID string on Windows, or None on POSIX."""
        return _windows_current_user_sid()

    def is_reparse_point(path: Path | str) -> bool:
        """True when path is a symlink (POSIX) or symlink/junction/reparse point (Windows)."""
        attrs = _kernel32.GetFileAttributesW(str(Path(path)))
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
        if path is None or is_reparse_point(path):
            return False
        return _check_private_dacl(path)

    def make_private(path: Path | str, *, directory: bool = False) -> None:
        """Restrict path to owner-only (0600/0700 on POSIX, Protected DACL on Windows). Never follows symlinks."""
        target = Path(path)
        if is_reparse_point(target):
            raise OSError(errno.ELOOP, "Refusing to change permissions on a symbolic link", str(target))
        _set_private_dacl(target, directory=directory)

    def make_executable(path: Path | str) -> None:
        """Restrict a downloaded runtime binary to owner-only executable (0700 on POSIX, Protected DACL on Windows)."""
        target = Path(path)
        if is_reparse_point(target):
            raise OSError(errno.ELOOP, "Refusing to change permissions on a symbolic link", str(target))
        _set_private_dacl(target, directory=False)

    def make_private_fd(fd: int, path: Path | str, *, directory: bool = False) -> None:
        """Apply owner-only permissions to an already-open descriptor (fchmod on POSIX, DACL on Windows)."""
        make_private(path, directory=directory)

    def make_private_best_effort(path: Path | str, *, directory: bool = False) -> None:
        """Best-effort equivalent of make_private for housekeeping (e.g. rotated logs)."""
        target = Path(path)
        try:
            if not target.exists() or is_reparse_point(target):
                return
            if (target.is_dir() if directory else target.is_file()):
                _set_private_dacl(target, directory=directory)
        except (FileNotFoundError, PermissionError):
            return

    def ensure_private_dir(path: Path | str, *, error: str = "Directory must belong to the user and be private.") -> Path:
        """Create a private directory atomically if absent, and verify ownership and permissions."""
        target = Path(path)
        if target.exists() or is_reparse_point(target):
            if is_reparse_point(target) or not target.is_dir():
                raise ValueError(error)
            if not _check_private_dacl(target):
                raise ValueError(error)
            return target
        if target.parent != target and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        sd = _build_private_sd(directory=True)
        try:
            sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), sd, False)
            created = _kernel32.CreateDirectoryW(str(target), ctypes.byref(sa))
            if not created:
                err = ctypes.get_last_error()
                if err != _ERROR_ALREADY_EXISTS:
                    raise ValueError(error) from ctypes.WinError(err)
        finally:
            _kernel32.LocalFree(sd)
        if is_reparse_point(target) or not target.is_dir() or not _check_private_dacl(target):
            raise ValueError(error)
        return target

    def open_no_follow(path: Path | str, flags: int, mode: int = 0o600, *, private_on_create: bool = True) -> int:
        """Open path without following a symlink/reparse point.

        On Windows, uses CreateFileW with FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ|WRITE|DELETE,
        non-inheritable handles, and a Protected DACL SECURITY_ATTRIBUTES on creation (zero race window).
        """
        target = Path(path)
        if is_reparse_point(target):
            raise OSError(errno.ELOOP, "Symbolic link not followed", str(target))
        access_mode = flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
        if access_mode == os.O_WRONLY:
            desired_access = _GENERIC_WRITE
        elif access_mode == os.O_RDWR:
            desired_access = _GENERIC_READ | _GENERIC_WRITE
        else:
            desired_access = _GENERIC_READ
        creating = bool(flags & os.O_CREAT)
        if creating and (flags & os.O_EXCL):
            disposition = _CREATE_NEW
        elif creating and (flags & os.O_TRUNC):
            disposition = _CREATE_ALWAYS
        elif creating:
            disposition = _OPEN_ALWAYS
        elif flags & os.O_TRUNC:
            disposition = _TRUNCATE_EXISTING
        else:
            disposition = _OPEN_EXISTING
        share_mode = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
        attrs = _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT
        sd = _build_private_sd(directory=False) if (creating and private_on_create) else None
        try:
            sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), sd, False)
            handle = _kernel32.CreateFileW(
                str(target), desired_access, share_mode, ctypes.byref(sa), disposition, attrs, None
            )
            if handle == _INVALID_HANDLE_VALUE:
                err = ctypes.get_last_error()
                if err == _ERROR_ACCESS_DENIED and target.is_dir():
                    raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(target))
                raise ctypes.WinError(err)
        finally:
            if sd is not None:
                _kernel32.LocalFree(sd)
        try:
            info = _BY_HANDLE_FILE_INFORMATION()
            if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise OSError(errno.ELOOP, "Symbolic link not followed", str(target))
            if info.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
                raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(target))
            crt_flags = os.O_BINARY
            if access_mode == os.O_RDONLY:
                crt_flags |= os.O_RDONLY
            if flags & os.O_APPEND:
                crt_flags |= os.O_APPEND
            fd = msvcrt.open_osfhandle(handle, crt_flags)
        except BaseException:
            _kernel32.CloseHandle(handle)
            raise
        return fd

    def _fchmod_if_posix(fd: int, mode: int) -> None:
        return None

    def _win_posix_replace(src: Path | str, dst: Path | str) -> bool:
        """Use NTFS FileRenameInfoEx with FILE_RENAME_FLAG_POSIX_SEMANTICS to atomically replace even an open file."""
        dst_abs = os.path.abspath(str(dst))
        name_bytes = dst_abs.encode("utf-16-le")
        # FILE_RENAME_INFO layout: DWORD Flags (+ pad), HANDLE RootDirectory, DWORD FileNameLength, WCHAR FileName[1]
        class _FILE_RENAME_INFO(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", wintypes.WCHAR * 1),
            ]
        name_offset = _FILE_RENAME_INFO.FileName.offset
        total_size = max(ctypes.sizeof(_FILE_RENAME_INFO), name_offset + len(name_bytes) + 2)
        buf = ctypes.create_string_buffer(total_size)
        header = _FILE_RENAME_INFO.from_buffer(buf)
        header.Flags = _FILE_RENAME_FLAG_REPLACE_IF_EXISTS | _FILE_RENAME_FLAG_POSIX_SEMANTICS
        header.RootDirectory = None
        header.FileNameLength = len(name_bytes)
        ctypes.memmove(ctypes.addressof(buf) + name_offset, name_bytes, len(name_bytes))
        handle = _kernel32.CreateFileW(
            str(src), _DELETE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None, _OPEN_EXISTING, _FILE_FLAG_OPEN_REPARSE_POINT, None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            return False
        try:
            return bool(_kernel32.SetFileInformationByHandle(handle, _FileRenameInfoEx, buf, total_size))
        finally:
            _kernel32.CloseHandle(handle)

    def atomic_replace(src: Path | str, dst: Path | str) -> None:
        """Atomic file replacement: os.replace on POSIX; POSIX-semantics FileRenameInfoEx + bounded retry on Windows."""
        if _win_posix_replace(src, dst):
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
        return None

    def current_uid() -> int | None:
        """os.getuid() on POSIX, None on Windows."""
        return None

    def owner_uid_mismatch(path: Path | str) -> tuple[int, int] | None:
        """Return `(file_uid, effective_uid)` on POSIX when non-root euid differs from path owner; None on Windows."""
        return None

    def pread_at(fd: int, count: int, offset: int) -> bytes:
        """Read up to count bytes at offset without moving the file position (POSIX os.pread fallback on Windows)."""
        saved = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            return os.read(fd, count)
        finally:
            os.lseek(fd, saved, os.SEEK_SET)

    def change_time_ns(path: Path | str, status: os.stat_result) -> int:
        """Inode/metadata change time in Unix nanoseconds: st_ctime_ns on POSIX, NTFS ChangeTime on Windows."""
        handle = _kernel32.CreateFileW(
            str(path), 0x80,  # FILE_READ_ATTRIBUTES
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None, _OPEN_EXISTING, 0x02200000,  # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            return status.st_ctime_ns
        try:
            info = _FILE_BASIC_INFO()
            if not _kernel32.GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
                return status.st_ctime_ns
            # Windows FILETIME: 100ns ticks since 1601-01-01; Unix epoch offset is 11644473600s.
            return (info.ChangeTime - 116444736000000000) * 100
        finally:
            _kernel32.CloseHandle(handle)

else:
    def current_user_sid() -> str | None:
        """Return the current process user's Windows SID string on Windows, or None on POSIX."""
        return None

    def is_reparse_point(path: Path | str) -> bool:
        """True when path is a symlink (POSIX) or symlink/junction/reparse point (Windows)."""
        return Path(path).is_symlink()

    def is_private_stat(info: os.stat_result, path: Path | None = None, *,
                        directory: bool = False, strict: bool = True) -> bool:
        """Check whether an open/statted file or directory is owned by this user and private."""
        if directory and not stat.S_ISDIR(info.st_mode):
            return False
        if not directory and not stat.S_ISREG(info.st_mode):
            return False
        if info.st_uid != os.getuid():
            return False
        mode = stat.S_IMODE(info.st_mode)
        if strict:
            return mode == (0o700 if directory else 0o600)
        return (mode & 0o077) == 0

    def make_private(path: Path | str, *, directory: bool = False) -> None:
        """Restrict path to owner-only (0600/0700 on POSIX, Protected DACL on Windows). Never follows symlinks."""
        target = Path(path)
        if is_reparse_point(target):
            raise OSError(errno.ELOOP, "Refusing to change permissions on a symbolic link", str(target))
        os.chmod(target, 0o700 if directory else 0o600)

    def make_executable(path: Path | str) -> None:
        """Restrict a downloaded runtime binary to owner-only executable (0700 on POSIX, Protected DACL on Windows)."""
        target = Path(path)
        if is_reparse_point(target):
            raise OSError(errno.ELOOP, "Refusing to change permissions on a symbolic link", str(target))
        os.chmod(target, 0o700)

    def make_private_fd(fd: int, path: Path | str, *, directory: bool = False) -> None:
        """Apply owner-only permissions to an already-open descriptor (fchmod on POSIX, DACL on Windows)."""
        os.fchmod(fd, 0o700 if directory else 0o600)

    def make_private_best_effort(path: Path | str, *, directory: bool = False) -> None:
        """Best-effort equivalent of make_private for housekeeping (e.g. rotated logs)."""
        target = Path(path)
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

    def ensure_private_dir(path: Path | str, *, error: str = "Directory must belong to the user and be private.") -> Path:
        """Create a private directory atomically if absent, and verify ownership and permissions."""
        target = Path(path)
        try:
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
        except FileExistsError:
            raise ValueError(error) from None
        info = target.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError(error)
        return target

    def open_no_follow(path: Path | str, flags: int, mode: int = 0o600, *, private_on_create: bool = True) -> int:
        """Open path without following a symlink/reparse point."""
        return os.open(Path(path), flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), mode)

    def _fchmod_if_posix(fd: int, mode: int) -> None:
        os.fchmod(fd, mode)

    def _win_posix_replace(src: Path | str, dst: Path | str) -> bool:
        return False

    def atomic_replace(src: Path | str, dst: Path | str) -> None:
        """Atomic file replacement: os.replace on POSIX; POSIX-semantics FileRenameInfoEx + bounded retry on Windows."""
        os.replace(src, dst)

    def fsync_directory(path: Path | str) -> None:
        """Fsync a directory descriptor on POSIX; no-op on Windows (NTFS does not support dir fsync)."""
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def current_uid() -> int | None:
        """os.getuid() on POSIX, None on Windows."""
        return os.getuid()

    def owner_uid_mismatch(path: Path | str) -> tuple[int, int] | None:
        """Return `(file_uid, effective_uid)` on POSIX when non-root euid differs from path owner; None on Windows."""
        if not hasattr(os, "geteuid"):
            return None
        try:
            st = Path(path).stat()
            euid = os.geteuid()
        except OSError:
            return None
        if st.st_uid != euid and euid != 0:
            return st.st_uid, euid
        return None

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

    def change_time_ns(path: Path | str, status: os.stat_result) -> int:
        """Inode/metadata change time in Unix nanoseconds: st_ctime_ns on POSIX, NTFS ChangeTime on Windows."""
        return status.st_ctime_ns


def write_durable_private(path: Path | str, data: bytes | str, *, mode: int = 0o600,
                          prefix: str = ".cairnmind-") -> Path:
    """Atomically write a private file (zero-window creation, flush, fsync, replace, dir fsync)."""
    target = Path(path)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    tmp = target.parent / f"{prefix}{secrets.token_hex(8)}.tmp"
    fd = open_no_follow(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, private_on_create=True)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
            _fchmod_if_posix(handle.fileno(), mode)
        atomic_replace(tmp, target)
        fsync_directory(target.parent)
    finally:
        tmp.unlink(missing_ok=True)
    return target


_MAX_INT64 = (1 << 63) - 1


def decode_file_id(value: Any) -> int | None:
    """Decode a stored st_dev/st_ino identifier (accepts non-negative int or decimal string)."""
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def encode_file_id(value: int | str) -> int | str:
    """Encode st_dev/st_ino for JSON/ArcadeDB: keep signed 64-bit ints, stringify values > 2^63-1 (#441)."""
    parsed = decode_file_id(value)
    if parsed is None:
        raise ValueError("Invalid file identity value")
    return str(parsed) if parsed > _MAX_INT64 else parsed
