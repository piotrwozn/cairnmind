"""Reserved portable-memory artifacts and read-only bindings of foreign paths.

Bindings change a local scope view, never the archived source or record itself.
Foreign paths are normalized lexically: resolving them on this machine could
follow an unrelated host's symlink and would change their provenance.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


MEMORY_RELATIVE = Path(".memory/devbrain")


def is_reserved_memory(relative: str | Path) -> bool:
    # Called for every file of a worktree: a path that does not mention the directory
    # cannot normalise into it.
    if ".memory" not in os.fspath(relative):
        return False
    path = Path(os.path.normpath(relative))
    return not path.is_absolute() and path.is_relative_to(MEMORY_RELATIVE)


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
