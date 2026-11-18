"""Symbolic links of the operating system itself, told apart from links a user could plant (#229).

On macOS /tmp, /var and /etc are symbolic links into /private, and the default temporary directory is
under /var/folders. A rule that refuses a symbolic link anywhere in a path refuses those too, so a package,
a checkout or a data directory under /tmp could not be used. Every such rule asks this module instead.

One rule: a link counts as the system's only when it is in the fixed table of the running platform below,
the path starts with it, and, checked once on this machine, it is owned by root, sits in a
directory owned by root that nobody else can write (`/`), and leads in one step to the target the table names, itself
free of links. A table entry that fails any check is not accepted at all.

Why this does not reopen what those rules close: they refuse a link a user could have planted, in a
project, in the data directory or on the way to a path given to a command. A user can change neither the
table nor `/`, so can neither add a system link nor redirect one; and only the start of a path is ever
replaced, so a link anywhere below it (`/tmp/x -> elsewhere`) is still a link and is still refused.
Linux has no such links: its table is empty and every rule answers as before.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from functools import cache
import os
from pathlib import Path
import stat
import sys

Links = tuple[tuple[Path, Path], ...]

# The links each platform ships, as (link, target). Nothing else is ever accepted.
SYSTEM_LINKS: dict[str, tuple[tuple[str, str], ...]] = {
    "darwin": (("/tmp", "/private/tmp"), ("/var", "/private/var"), ("/etc", "/private/etc")),
}


def owned_by_system(link: Path) -> bool:
    """Whether `link` is a symbolic link owned by root in a root-owned directory only root can write."""
    try:
        info, parent = os.lstat(link), os.stat(link.parent)
    except OSError:
        return False
    return (stat.S_ISLNK(info.st_mode) and info.st_uid == 0 and parent.st_uid == 0
            and not parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def verified_links(table: Iterable[tuple[str | os.PathLike, str | os.PathLike]],
                   owned: Callable[[Path], bool] = owned_by_system) -> Links:
    """The entries of `table` that pass the checks on this machine; the rest are left out (fail closed)."""
    found = []
    for link, target in table:
        link, target = Path(link), Path(target)
        if not (link.is_absolute() and target.is_absolute()):
            continue
        try:
            leads_to = Path(os.path.realpath(link, strict=True))
            # One hop straight to the target: a chain through another link could later be redirected.
            one_hop = Path(os.path.normpath(link.parent / os.readlink(link)))
        except (OSError, RuntimeError):
            continue
        if (owned(link) and leads_to == one_hop == target and Path(os.path.realpath(target)) == target
                and target.is_dir()):
            found.append((link, target))
    return tuple(found)


@cache
def platform_links() -> Links:
    """The system links of this machine, checked once per process."""
    return verified_links(SYSTEM_LINKS.get(sys.platform, ()), owned_by_system)


def system_spelling(path: str | os.PathLike, links: Links | None = None) -> Path:
    """`path` with a leading system link replaced by its target. Lexical: nothing else is followed or read."""
    path = Path(path)
    if path.is_absolute():
        for link, target in (platform_links() if links is None else links):
            if path.is_relative_to(link):
                return target / path.relative_to(link)
    return path


def passes_planted_link(path: str | os.PathLike, links: Links | None = None) -> bool:
    """Whether `path` or a directory above it is a symbolic link other than a system link."""
    path = system_spelling(path, links)
    return any(part.is_symlink() for part in (path, *path.parents))
