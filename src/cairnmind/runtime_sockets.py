"""Where the private tmux sockets of the workspace and of native runs live (#235).

Linux and WSL keep the place earlier versions used, ``<gettempdir()>/cairnmind-<kind>-<uid>-<namespace>``, so a
workspace started before an upgrade is still found after it; TMPDIR is seldom set there and the same in every
session of a user.

On macOS a terminal app, an ssh session and launchd each see another TMPDIR (a long path under /var/folders), so a
workspace started in one was not found from another and the socket path came within a few bytes of the limit. There
the sockets live in one short directory per user, ``/private/tmp/cairnmind-<uid>/<kind>-<namespace>``: /tmp is a link
to /private/tmp, and that system prefix is resolved rather than refused. The per-user directory is created 0700 and
must be a real directory of the user, never a link: anyone can create names in /tmp, so one that someone else made
first is refused, never used.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile

KINDS = ("ui", "native")
SYSTEM_TEMP = "/tmp"
# sun_path holds 104 bytes on macOS and 108 on Linux, the closing NUL included.
SUN_PATH = {"darwin": 104, "linux": 108}
# A socket path longer than this is refused before tmux is asked to bind it; under both limits above.
MAX_SOCKET_PATH = 100


def _darwin(platform: str | None) -> bool:
    return (platform or sys.platform) == "darwin"


def socket_base(*, platform: str | None = None, uid: int | None = None, system_temp: str | None = None) -> Path:
    """The directory under which the socket directories are made: per user on macOS, the temporary one on Linux."""
    if not _darwin(platform):
        return Path(tempfile.gettempdir())
    uid = os.getuid() if uid is None else uid
    return Path(os.path.realpath(system_temp or SYSTEM_TEMP)) / f"cairnmind-{uid}"


def socket_directory(kind: str, namespace: str, *, platform: str | None = None, uid: int | None = None,
                     system_temp: str | None = None) -> Path:
    """The private directory that holds the one ``tmux.sock`` of a workspace (``ui``) or of native runs."""
    if kind not in KINDS:
        raise ValueError(f"Unknown socket kind: {kind!r}")
    uid = os.getuid() if uid is None else uid
    base = socket_base(platform=platform, uid=uid, system_temp=system_temp)
    return base / (f"{kind}-{namespace}" if _darwin(platform) else f"cairnmind-{kind}-{uid}-{namespace}")


def prepare_socket_base(*, platform: str | None = None, system_temp: str | None = None) -> None:
    """Create the per-user base on macOS and check it; nothing on Linux, where the base is the temporary directory.

    The resolved system directory must belong to root or to the user, and be sticky when others can write to it, so
    nobody else can remove or replace the per-user directory once it is checked.
    """
    if not _darwin(platform):
        return
    base = socket_base(platform="darwin", system_temp=system_temp)
    parent = os.lstat(base.parent)
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid not in (0, os.getuid())
            or (parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX)):
        raise ValueError("The system temporary folder is not safe for private sockets.")
    try:
        os.mkdir(base, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(base)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("The socket folder must belong to the user, have permissions 0700 and not be a link.")
