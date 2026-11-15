"""Reserved portable-memory artifacts and read-only bindings of foreign paths.

Bindings change a local scope view, never the archived source or record itself.
Foreign paths are normalized lexically: resolving them on this machine could
follow an unrelated host's symlink and would change their provenance.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from .path_spelling import relative_on_disk


# Snapshots are written to the first directory. The second is where versions up to 0.9.0 wrote
# them: it is still read, and still reserved, so that a package left there is neither indexed
# nor counted as project code.
MEMORY_RELATIVE = Path(".memory/cairnmind")
EARLIER_MEMORY_RELATIVE = Path(".memory/devbrain")
MEMORY_DIRECTORIES = (MEMORY_RELATIVE, EARLIER_MEMORY_RELATIVE)
# The published memory of a team (#170): one file per record, committed with a branch. It is not an
# archive, so it is not in MEMORY_DIRECTORIES (where snapshots are looked for), but it is reserved like
# one: a commit that only changes memory then changes no code fingerprint, index or scope check.
PUBLISHED_RELATIVE = Path(".memory/cairnmind-published")
RESERVED_DIRECTORIES = (*MEMORY_DIRECTORIES, PUBLISHED_RELATIVE)
# Pathspecs that keep Git from listing the reserved directories at all: a snapshot of a large project
# holds tens of thousands of objects, and the worktree is listed at every code-state capture (#158).
# Case-sensitive in Git; is_reserved_memory, which ignores case, still filters what Git lists.
GIT_PATHSPEC = ("--", ".", *(f":(exclude){directory.as_posix()}" for directory in RESERVED_DIRECTORIES))


def _in_reserved(path: Path, directory: Path) -> bool:
    """Whether ``path`` lies in ``directory``, whose last two names (`.memory/<name>`) are compared without
    case: on a file system that ignores case `.Memory/Cairnmind` is the reserved directory (#230). The
    names before them (the place of a project registered below a root) are compared exactly."""
    size = len(directory.parts)
    head = size - 2
    return (len(path.parts) >= size and path.parts[:head] == directory.parts[:head]
            and tuple(part.casefold() for part in path.parts[head:size]) == directory.parts[head:])


def is_reserved_memory(relative: str | Path) -> bool:
    # Called for every file of a worktree: a path that does not mention the directory
    # cannot normalise into it. Case-folded, because the reserved names are compared without case.
    if ".memory" not in os.fspath(relative).casefold():
        return False
    path = Path(os.path.normpath(relative))
    return not path.is_absolute() and any(_in_reserved(path, directory) for directory in RESERVED_DIRECTORIES)


def nested_reserved(store, base: str | Path) -> tuple[Path, ...]:
    """The reserved directories of every registered project root strictly below ``base``, relative to it (#174).

    One repository can hold several projects, each registered at its own subdirectory and publishing
    its memory there. What one project's memory writes is then neither indexed, fingerprinted nor
    scope-checked as code of a project registered above it. A directory of the same name that is no
    registered project's is ordinary files.

    A root counts by where it lies in its repository, not only by its path: in another worktree of the
    same repository, a project registered at ``services/a`` of the main checkout still reserves
    ``services/a/.memory/...`` there.
    """
    base = Path(base).resolve()
    roots = {Path(row["path"]).resolve() for row in store.query("SELECT path FROM project_roots")
             if isinstance(row.get("path"), str) and row["path"]}
    # Below `base` by identity too, spelled as a walk of `base` lists the names: on a file system that ignores
    # case a project registered as `REPO/Services/Alpha` is the folder `services/alpha` of `Repo` (#230).
    below = {relative for relative in (relative_on_disk(root, base) for root in roots) if relative is not None}
    place = _repository_place(base) if roots else None
    if place is not None:
        common, prefix = place
        for root in roots:
            other = _repository_place(root)
            if other is not None and other[0] == common and len(other[1]) > len(prefix) \
                    and other[1][:len(prefix)] == prefix:
                below.add(Path(*other[1][len(prefix):]))
    return tuple(sorted(relative / directory for relative in below for directory in RESERVED_DIRECTORIES))


def _repository_place(path: Path) -> tuple[Path, tuple[str, ...]] | None:
    """(common Git directory, parts of the path below its worktree's top), or None outside Git.

    Read from the files where they say it (a `.git` directory), so that no process is started for
    each registered root; a `.git` file (a linked worktree or a submodule) is asked of Git.
    """
    for top in (path, *path.parents):
        marker = top / ".git"
        try:
            if marker.is_dir():
                return marker.resolve(), path.relative_to(top).parts
            if not marker.exists():
                continue
        except OSError:
            return None
        try:
            result = subprocess.run(["git", "-C", str(path), "rev-parse", "--git-common-dir", "--show-prefix"],
                                    capture_output=True, timeout=20, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        except (OSError, subprocess.SubprocessError):
            return None
        lines = os.fsdecode(result.stdout).split("\n")
        if result.returncode or len(lines) < 2 or not lines[0]:
            return None
        return (path / lines[0]).resolve(), Path(lines[1]).parts
    return None


def is_reserved_below(relative: str | Path, directories: tuple[Path, ...]) -> bool:
    """Whether a path relative to a root lies in one of ``directories`` (from nested_reserved)."""
    if not directories or ".memory" not in os.fspath(relative).casefold():
        return False
    path = Path(os.path.normpath(relative))
    return not path.is_absolute() and any(_in_reserved(path, directory) for directory in directories)


def git_pathspec(directories: tuple[Path, ...] = ()) -> tuple[str, ...]:
    """GIT_PATHSPEC, also leaving out ``directories``; taken literally, since they are names of real folders."""
    return (*GIT_PATHSPEC, *(f":(exclude,literal){directory.as_posix()}" for directory in directories))


def snapshot_directory(root: str | Path) -> Path | None:
    """The directory of a worktree that holds a snapshot to read: the current one, else the earlier one."""
    for directory in MEMORY_DIRECTORIES:
        manifest = Path(root) / directory / "manifest.json"
        if manifest.exists() or manifest.is_symlink():
            return Path(root) / directory
    return None


def is_memory_path(path: str | Path, root: str | Path) -> bool:
    """Match the exact reserved subtree without following foreign symlinks."""
    path, root = Path(path), Path(os.path.abspath(root))
    path = Path(os.path.abspath(path if path.is_absolute() else root / path))
    return path.is_relative_to(root) and is_reserved_memory(path.relative_to(root))


def _absolute(value):
    if not isinstance(value, str) or not value or "\0" in value:
        return None
    path = Path(value)
    return Path(os.path.normpath(value)) if path.is_absolute() else None


def memory_bindings(store, project_id: str) -> list[tuple[Path, Path]]:
    """Return unambiguous, project-scoped mappings into registered local roots."""
    registered = {Path(row["path"]).resolve() for row in store.query(
        "SELECT path FROM project_roots WHERE project_id=:project", {"project": project_id})}
    rows = store.query(
        "SELECT origin_root,local_root FROM memory_bindings WHERE project_id=:project",
        {"project": project_id})
    candidates: dict[Path, set[Path]] = {}
    for row in rows:
        origin, local = _absolute(row.get("origin_root")), _absolute(row.get("local_root"))
        if origin is not None and local is not None and local.resolve() in registered:
            candidates.setdefault(origin, set()).add(local.resolve())
    return sorted(((origin, next(iter(locals_))) for origin, locals_ in candidates.items()
                   if len(locals_) == 1), key=lambda pair: (-len(pair[0].parts), str(pair[0])))


def imported_source_ids(store, project_id: str) -> set[str]:
    """Receipt membership distinguishes imported archives from local evidence."""
    rows = store.query("SELECT source_ids FROM memory_imports WHERE project_id=:project", {"project": project_id})
    return {source_id for row in rows for source_id in (row.get("source_ids") or []) if isinstance(source_id, str)}


def bound_metadata(store, project_id: str, metadata, *, bindings=None) -> dict:
    """Map scope-bearing fields; leave every other provenance field untouched."""
    if isinstance(metadata, (str, bytes)):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return {}
    if not isinstance(metadata, dict):
        return {}
    result = dict(metadata)
    mappings = memory_bindings(store, project_id) if bindings is None else bindings
    for field in ("root", "worktree", "worktree_root", "path"):
        path = _absolute(result.get(field))
        if path is None:
            continue
        for origin, local in mappings:
            if path.is_relative_to(origin):
                result[field] = str(local / path.relative_to(origin))
                break
    return result
