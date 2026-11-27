"""Path names on a file system that ignores letter case (#230).

A Windows drive seen from WSL (under /mnt/c) and macOS by default ignore letter case: `.GIT/hooks` is the
folder `.git/hooks`. A Windows drive also answers to the short 8.3 alias of a long name: `GIT~1` is `.git`.
So a guard that compares names as text compares them case-folded, and where text cannot decide (a short
alias, a worktree spelled another way) the answer comes from identity: device and inode.

On a file system that respects case every check here answers as an exact comparison did: names that differ
only in case are different files there, and their identities differ.
"""
from __future__ import annotations

import os
from pathlib import Path


def identity(info) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def entry_identity(name: str, directory: int | Path) -> tuple[int, int] | None:
    """The identity of `name` in the open directory `directory`, without following a link; None when absent."""
    try:
        if isinstance(directory, int):
            return identity(os.stat(name, dir_fd=directory, follow_symlinks=False))
        return identity(os.stat(Path(directory) / name, follow_symlinks=False))
    except OSError:
        return None


def is_alias(name: str, directory: int | Path) -> bool:
    """Whether `name` reaches an entry of the open directory `directory` that is listed under another name,
    letter case aside: the short 8.3 alias of a Windows drive. Such an alias always holds a '~', so only those
    names are looked up; a name that is listed (as `foo~1` on Linux) is not an alias."""
    if "~" not in name or entry_identity(name, directory) is None:
        return False
    folded = name.casefold()
    with os.scandir(directory) as entries:
        return not any(entry.name.casefold() == folded for entry in entries)


def has_alias(path: Path, root: Path) -> bool:
    """Whether a part of `path` below `root` is a short alias (see is_alias); False when it cannot be read."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    folder = root
    for part in parts:
        if "~" in part:
            if hasattr(os, "O_DIRECTORY") and os.stat in os.supports_dir_fd:
                try:
                    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                except OSError:
                    return False
                try:
                    if is_alias(part, fd):
                        return True
                finally:
                    os.close(fd)
            elif is_alias(part, folder):
                return True
        folder = folder / part
    return False


def same_or_below(path: Path, directory: tuple[int, int] | None) -> bool:
    """Whether `path` or one of its parents is the directory of identity `directory`, however it is spelled."""
    if directory is None:
        return False
    for candidate in (path, *path.parents):
        try:
            if identity(os.stat(candidate)) == directory:
                return True
        except OSError:
            continue
    return False


def lies_in(path, directory) -> bool:
    """`path.is_relative_to(directory)` that also sees the two spelled differently (both resolved)."""
    path, directory = Path(path), Path(directory)
    return path.is_relative_to(directory) or same_or_below(path, path_identity(directory))


def path_identity(path) -> tuple[int, int] | None:
    try:
        return identity(os.stat(path))
    except OSError:
        return None


def ignores_case(directory: Path) -> bool:
    """Whether the file system of `directory` ignores letter case: decided by its deepest name with a letter,
    which must name the same directory when its case is swapped."""
    for candidate in (directory, *directory.parents):
        name = candidate.name
        if name.swapcase() == name:
            continue
        own, swapped = path_identity(candidate), path_identity(candidate.parent / name.swapcase())
        return own is not None and own == swapped
    return False


def relative_on_disk(path, base) -> Path | None:
    """`path` relative to `base`, both resolved, each name spelled as the directory lists it; None when `path`
    does not lie strictly below `base`. On a file system that ignores case `REPO/Services/Alpha` below `Repo`
    is `services/alpha`, the names a walk of `Repo` sees. A name that cannot be read is kept as given."""
    path, base = Path(path), Path(base)
    if path.is_relative_to(base):
        parts = path.relative_to(base).parts
    else:
        wanted = path_identity(base)
        top = None if wanted is None else next(
            (parent for parent in path.parents if path_identity(parent) == wanted), None)
        if top is None:
            return None
        parts = path.relative_to(top).parts
    if not parts:
        return None
    spelled, folder = [], base
    for part in parts:
        name = _listed_name(folder, part)
        spelled.append(name)
        folder = folder / name
    return Path(*spelled)


def _listed_name(folder: Path, part: str) -> str:
    """The name under which `folder` lists its entry `part` (the same entry, by identity); `part` itself when
    it is listed or cannot be read."""
    try:
        wanted = identity(os.stat(folder / part, follow_symlinks=False))
        with os.scandir(folder) as entries:
            names = [entry.name for entry in entries]
        if part in names:
            return part
        return next((name for name in names
                     if identity(os.stat(folder / name, follow_symlinks=False)) == wanted), part)
    except OSError:
        return part
