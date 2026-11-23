"""What the checks of one question's files share (issue #416).

The check of a hit's file on disk (``KnowledgeAgent._source``) needs the project, its registered folders, the memory
bindings and the sources an import brought. They are the same for every file of a question, and the checks read them
again for each file: about a dozen database requests each. ``check_scope`` reads them once, when a question starts, and
every check of that question is handed the result. It is made for one question and dropped with it, so a root added or
removed meanwhile is seen by the next question. Within a question it is a snapshot: it spans the wait for the
embedding model and the model's own tool calls, so a root removed in that time still counts as registered until the
question ends (the file itself is still read and hashed at each check).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import project_folders
from .memory_paths import imported_source_ids, memory_bindings
from .sql import unregistered_root


@dataclass(frozen=True)
class CheckScope:
    project: dict  # the project row, read-only by use
    root: Path  # the worktree the question is asked in
    roots: tuple  # the project's registered roots, resolved
    bindings: tuple
    folders: tuple  # project_folders.folder_states: the state of each registered folder, read once
    imported: frozenset  # ids of the sources an imported archive brought


def check_scope(store, project_id, worktree=None) -> CheckScope:
    project = store.resolve_project(project_id)
    rows = store.query("SELECT id,path,machine,host FROM project_roots WHERE project_id=:project", {"project": project["id"]})
    roots = tuple(Path(row["path"]).resolve() for row in rows)
    root = Path(worktree if worktree is not None else project["root"]).expanduser().resolve()
    if root not in roots:
        raise ValueError(unregistered_root(root, project))
    return CheckScope(project, root, roots, tuple(memory_bindings(store, project["id"])),
                      tuple(project_folders.folder_states(store, project, rows)),
                      frozenset(imported_source_ids(store, project["id"])))
