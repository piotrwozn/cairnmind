"""Canonical ignore matching shared by file indexing, snapshot export and memory audit.

Combines Git's own `git check-ignore --stdin -z` (for `.gitignore`, nested ignore files,
negations like `!important.env` and `.git/info/exclude`) with `.cairnmindignore` specs,
while rejecting symlinks that escape the project root and detecting binary files via NUL bytes.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Sequence

import pathspec

CAIRNMINDIGNORE = ".cairnmindignore"
GITIGNORE = ".gitignore"
BINARY_PROBE_BYTES = 8192


def is_binary_bytes(data: bytes) -> bool:
    """Return True if raw bytes contain a NUL byte (canonical binary-file heuristic)."""
    return b"\x00" in data[:BINARY_PROBE_BYTES] or b"\x00" in data


def escapes_root(path: Path, root: Path) -> bool:
    """Return True if `path` (or any symlink along it) resolves outside `root`."""
    try:
        resolved_root = root.expanduser().resolve()
        resolved_path = path.expanduser().resolve()
    except OSError:
        return True
    return not resolved_path.is_relative_to(resolved_root)


def is_git_worktree(root: Path) -> bool:
    """Return True if `root` is inside a Git worktree."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, timeout=15,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.decode("utf-8", "replace").strip() == "true"


def git_check_ignore(root: Path, rel_paths: Sequence[str], *, no_index: bool = True) -> set[str] | None:
    """Batch-check relative POSIX paths with `git check-ignore --stdin -z`.

    Returns the set of ignored paths, or None if Git is unavailable or `root` is not a Git worktree.
    Chunks large batches to avoid oversized stdin buffers.
    """
    cleaned = [p for p in rel_paths if isinstance(p, str) and p and "\x00" not in p]
    if not cleaned:
        return set()
    ignored: set[str] = set()
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    base_cmd = ["git", "-C", str(root), "check-ignore", "--stdin", "-z"]
    if no_index:
        base_cmd.append("--no-index")
    for offset in range(0, len(cleaned), 4000):
        batch = cleaned[offset:offset + 4000]
        payload = ("\x00".join(batch) + "\x00").encode("utf-8", "surrogateescape")
        try:
            result = subprocess.run(base_cmd, input=payload, capture_output=True, timeout=30,
                                    env=env, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 1:
            continue  # Exit 1 means none of the paths in this batch were ignored.
        if result.returncode != 0:
            return None  # Exit 128 etc.: not a git repo or unreadable ignore state.
        for item in result.stdout.split(b"\x00"):
            if item:
                ignored.add(item.decode("utf-8", "surrogateescape"))
    return ignored


def load_spec_file(path: Path) -> pathspec.GitIgnoreSpec | None:
    """Load one ignore file without following symlinks."""
    if path.is_symlink() or not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return pathspec.GitIgnoreSpec.from_lines(lines)


class IgnoreMatcher:
    """Canonical matcher combining `.gitignore` (via Git when available) and `.cairnmindignore`."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self._git_repo = is_git_worktree(self.root)
        self._cairnmind_specs = self._collect_specs(CAIRNMINDIGNORE)
        self._git_specs = {} if self._git_repo else self._collect_specs(GITIGNORE)

    def _collect_specs(self, filename: str) -> dict[Path, pathspec.GitIgnoreSpec]:
        specs: dict[Path, pathspec.GitIgnoreSpec] = {}
        root_file = self.root / filename
        if root_file.is_file() and not root_file.is_symlink():
            spec = load_spec_file(root_file)
            if spec is not None:
                specs[self.root] = spec
        for dirpath, dirs, names in os.walk(self.root, followlinks=False):
            folder = Path(dirpath)
            dirs[:] = [d for d in dirs if d != ".git" and not (folder / d).is_symlink()]
            if folder == self.root:
                continue
            if filename in names:
                spec = load_spec_file(folder / filename)
                if spec is not None:
                    specs[folder] = spec
        return specs

    @staticmethod
    def _matches_specs(root: Path, specs: dict[Path, pathspec.GitIgnoreSpec], rel_posix: str,
                       is_dir: bool = False) -> bool:
        if not specs:
            return False
        target = root / rel_posix
        answer = False
        for base, spec in sorted(specs.items(), key=lambda row: len(row[0].parts)):
            if target.is_relative_to(base):
                relative = target.relative_to(base).as_posix() + ("/" if is_dir else "")
                checked = spec.check_file(relative)
                if checked.include is not None:
                    answer = checked.include
        return answer

    def ignored_set(self, rel_paths: Sequence[str]) -> set[str]:
        """Return the subset of `rel_paths` (POSIX relative to root) ignored by `.cairnmindignore` or `.gitignore`."""
        normalized = [p.lstrip("./") for p in rel_paths if isinstance(p, str) and p]
        if not normalized:
            return set()
        ignored: set[str] = set()
        remaining: list[str] = []
        for rel in normalized:
            if self._matches_specs(self.root, self._cairnmind_specs, rel):
                ignored.add(rel)
            else:
                remaining.append(rel)
        if not remaining:
            return ignored
        if self._git_repo:
            git_hits = git_check_ignore(self.root, remaining, no_index=True)
            if git_hits is not None:
                ignored.update(git_hits)
                return ignored
            # Fallback if git failed unexpectedly
            if not self._git_specs:
                self._git_specs = self._collect_specs(GITIGNORE)
        for rel in remaining:
            if self._matches_specs(self.root, self._git_specs, rel):
                ignored.add(rel)
        return ignored

    def is_ignored(self, rel_path: str) -> bool:
        rel = rel_path.lstrip("./")
        return rel in self.ignored_set([rel])
