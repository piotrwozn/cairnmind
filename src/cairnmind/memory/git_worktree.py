"""Shared Git worktree root, command, and file-hashing helpers for verification and scope tracking."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from cairnmind.storage.sql import unregistered_root
from cairnmind.storage.storage_query import db_find


def _root(store, project_id, worktree=None) -> Path:
    project = store.resolve_project(project_id)
    root = Path(worktree if worktree is not None else project["root"]).expanduser().resolve()
    registered = db_find(store, "project_roots", {"project_id": project["id"]}, columns=("path",))
    if str(root) not in {str(Path(row["path"]).resolve()) for row in registered}:
        raise ValueError(unregistered_root(root, project))
    return root


def _git(root, *args) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise ValueError("Cannot inspect Git worktree")
    return result.stdout


def _file_hash(path: Path) -> str:
    """SHA-256 of a file's content. Every call reads the file.

    A remembered hash keyed by the file's size and times would be faster and wrong: a
    store through a shared memory mapping changes the content without changing any time,
    and on a Windows drive mounted in WSL a process can set the change time back. Only
    reading the content keeps the promise that any edit changes the fingerprint.
    """
    before = path.stat()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256")
    after = path.stat()

    def identity(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)

    if identity(before) != identity(after):
        raise ValueError("input changed while being read")
    return digest.hexdigest()


def require_git_tree(root: Path, *, git_fn=None) -> bool:
    """A registered root is the root of a Git worktree or a directory inside one (#174).

    A project registered at a subdirectory of a repository is that directory: Git lists its files
    alone, relative to it. HEAD is the repository's, so any commit still moves it. A root the
    repository ignores is refused: Git would list none of its files, and an edit there would leave
    the fingerprint unchanged. Returns whether the root is a subdirectory of its worktree.
    """
    run_git = git_fn or _git
    top = Path(os.fsdecode(run_git(root, "rev-parse", "--show-toplevel")).strip()).resolve()
    if root == top:
        return False
    if not root.is_relative_to(top):
        raise ValueError("Registered root is not inside its Git worktree")
    ignored = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", "."],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
    )
    if ignored.returncode == 0:
        raise ValueError("Registered root is ignored by Git, which lists none of its files")
    if ignored.returncode != 1:
        raise ValueError("Cannot inspect Git worktree")
    return True
