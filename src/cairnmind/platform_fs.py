"""Cross-platform filesystem, ownership, directory-sync and identity helpers (ADR-05).

On POSIX systems (Linux, WSL, macOS), every check enforces the exact existing
``st_uid`` and ``st_mode`` invariants and ``O_NOFOLLOW`` / ``O_DIRECTORY`` flags.
On native Windows, POSIX-only flags degrade safely and NTFS per-user directories
under ``%LOCALAPPDATA%`` are accepted without failing on synthetic ``st_uid == 0``
or POSIX mode masks.
"""
from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import stat
import subprocess
import sys

IS_WINDOWS: bool = sys.platform == "win32"

O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
O_CLOEXEC: int = getattr(os, "O_CLOEXEC", 0)
O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)
O_DIRECTORY: int = getattr(os, "O_DIRECTORY", 0)


def current_uid() -> int | None:
    """Return ``os.getuid()`` on POSIX, or ``None`` on Windows."""
    return os.getuid() if hasattr(os, "getuid") else None


def owner_matches(info: os.stat_result, uid: int | None = None) -> bool:
    """Whether ``info`` belongs to the current user (always True on Windows)."""
    if not hasattr(os, "getuid"):
        return True
    expected = os.getuid() if uid is None else uid
    return info.st_uid == expected


def owner_is_root_or_current(info: os.stat_result) -> bool:
    """Whether ``info`` belongs to root or the current user (always True on Windows)."""
    if not hasattr(os, "getuid"):
        return True
    return info.st_uid in (0, os.getuid())


def private_mode_ok(info: os.stat_result, expected: int) -> bool:
    """Whether ``info`` has exact POSIX mode ``expected`` (always True on Windows)."""
    if IS_WINDOWS:
        return True
    return stat.S_IMODE(info.st_mode) == expected


def no_group_other_access(info: os.stat_result) -> bool:
    """Whether group and others have no mode bits set (always True on Windows)."""
    if IS_WINDOWS:
        return True
    return (stat.S_IMODE(info.st_mode) & 0o077) == 0


def safe_fchmod(fd: int, mode: int) -> None:
    """Call ``os.fchmod(fd, mode)`` where supported; no-op on Windows."""
    if hasattr(os, "fchmod"):
        os.fchmod(fd, mode)


def sync_directory(directory: Path | str, *, nofollow: bool = False) -> None:
    """fsync a directory on POSIX; no-op on Windows where directories cannot be opened as FDs."""
    if IS_WINDOWS:
        return
    flags = os.O_RDONLY | O_DIRECTORY | (O_NOFOLLOW if nofollow else 0)
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def pread_bytes(fd: int, size: int, offset: int) -> bytes:
    """Read ``size`` bytes at ``offset`` without changing the descriptor offset."""
    if hasattr(os, "pread"):
        return os.pread(fd, size, offset)
    if size <= 0:
        return b""
    saved = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        chunks = []
        remaining = size
        while remaining > 0:
            part = os.read(fd, remaining)
            if not part:
                break
            chunks.append(part)
            remaining -= len(part)
        return b"".join(chunks)
    finally:
        os.lseek(fd, saved, os.SEEK_SET)


def default_user_data_base(environ: Mapping[str, str] | None = None, *, platform_name: str | None = None) -> Path:
    """Base directory for per-user application data.

    - Linux / WSL / macOS: ``~/.local/share`` (preserving existing installations).
    - Windows: ``%LOCALAPPDATA%`` (fallback ``%APPDATA%``, then ``~/AppData/Local``).
    """
    plat = sys.platform if platform_name is None else platform_name
    if plat == "win32":
        env = os.environ if environ is None else environ
        local = env.get("LOCALAPPDATA") or env.get("APPDATA")
        if local:
            return Path(local)
        return Path.home() / "AppData" / "Local"
    return Path("~/.local/share").expanduser()


def windows_machine_guid() -> str | None:
    """Read ``HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid`` on Windows, or ``None``."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
    except OSError:
        return None
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned:
            return cleaned
    return None


def terminate_windows_tree(pid: int, *, timeout: float = 5.0) -> None:
    """Terminate a process and all its descendants on Windows via ``taskkill /F /T``."""
    if not IS_WINDOWS or pid <= 0:
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=flags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass
