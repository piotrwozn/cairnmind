"""File discovery, exclusion rules, text chunking, and tree-state hashing for ingestion."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pathspec

from cairnmind.memory import project_folders
from cairnmind.memory.memory_paths import is_memory_path, is_reserved_below, nested_reserved
from cairnmind.pal.path_spelling import path_identity
from cairnmind.scip.parsing import parser_identity
from cairnmind.storage.storage_query import db_find

MAX_FILE_BYTES = 2 * 1024 * 1024
EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", ".nuxt", "dist", "build",
    "target", "vendor", ".idea", ".cairnmind", ".devbrain", ".terraform", ".ssh", ".aws", ".azure", ".gnupg",
    ".codex", ".claude",
}


def excluded_directory(name: str) -> bool:
    """Whether the indexer skips a directory of this name. A name with a leading dot (Git's, credentials',
    clients') is compared without case: on a file system that ignores case `.SSH` is `.ssh` (#230). The
    others (`Build`, `Vendor`) are compared exactly, as different folders on a file system that respects case."""
    return name in EXCLUDED_DIRS or (name[:1] == "." and name.casefold() in EXCLUDED_DIRS)


SECRET_NAMES = {".env", ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.json", "id_rsa", "id_ed25519"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".sqlite", ".sqlite3", ".db"}

_MANAGED_MCP_FILES = frozenset({
    ".mcp.json",
    ".cursor/mcp.json",
    ".vscode/mcp.json",
    ".roo/mcp.json",
    ".codex/config.toml",
    ".gemini/settings.json",
    ".zed/settings.json",
})
_BACKUP_MARKERS = (".cairnmind.bak", ".cairnmind-backup-", ".devbrain-backup-")

MANAGED_FILES = frozenset({"AGENTS.md", "CLAUDE.md", "cairnmind-recorder.md"})  # where `cairnmind install` writes its block; the recorder subagent's body (#376)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _metadata(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


def _identifier(*parts: Any) -> str:
    return hashlib.sha256("\x00".join(str(p) for p in parts).encode()).hexdigest()


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, timeout=15,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, check=False,
        )
        return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _is_secret(path: Path) -> bool:
    lower = path.name.lower()
    return (
        lower in SECRET_NAMES or lower.startswith(".env.") or lower.endswith(".credentials.json")
        or path.suffix.lower() in SECRET_SUFFIXES
    )


def _is_managed_mcp_config(path: Path, root: Path) -> bool:
    """Skip generated MCP preset files and Cairnmind backups so adding a preset never creates false drift."""
    if any(marker in path.name for marker in _BACKUP_MARKERS):
        return True
    try:
        rel = path.relative_to(root).as_posix().casefold()
    except ValueError:
        return False
    return rel in _MANAGED_MCP_FILES


def _roots(store, project: dict) -> list[Path]:
    rows = db_find(store, "project_roots", {"project_id": project["id"]}, columns=("path",))
    return list(dict.fromkeys([Path(project["root"]).resolve(), *[Path(r["path"]).resolve() for r in rows]]))


class _NestedMemory:
    """The memory directories of the projects registered below a root (#174), which are their memory.

    Every such directory lies under a `.memory` directory, so the registered roots are only read once a
    path below the root has a part of that name: a walk or a freshness check of a tree with none costs
    no query, as before #174."""

    def __init__(self, store, root: Path):
        self.store, self.root, self._bases = store, root, None

    def covers(self, path: Path) -> bool:
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            return False
        if ".memory" not in (part.casefold() for part in parts):
            return False
        if self._bases is None:
            self._bases = nested_reserved(self.store, self.root)
        return is_reserved_below(Path(*parts), self._bases)


def _walk_args(store, root: Path, errors: list[OSError], links: list | None = None):
    """(excluded, errors, nested, links) for _files: what a walk of a root leaves out besides its own reserved
    memory, the data directory and the memory directories of the projects registered below it (#174)."""
    return (store.data_dir,), errors, _NestedMemory(store, root), links


def _files(root: Path, excluded: tuple[Path, ...] = (), errors: list[OSError] | None = None,
           nested: _NestedMemory | None = None, links: list | None = None):
    """Walk with nested `.gitignore` and `.cairnmindignore` rules. Symlinks are deliberately not followed.

    Inside a Git worktree, candidate files are verified in one batched `git check-ignore --stdin -z --no-index`
    call so Git negations (e.g. `!important.env`) and `.git/info/exclude` are honoured canonically.

    A directory that cannot be listed is appended to `errors`: the walk is then incomplete,
    and its caller must not treat the files it did not see as deleted. `nested`: the memory
    directories of projects registered below the root (#174), also left out. `links` receives
    (path, what it says) of every symlink in the directories the walk lists and leaves out: resolving an import follows
    them, so a link that is moved, added or removed moves an import (#451).
    """
    from cairnmind.scip.ignore_rules import CAIRNMINDIGNORE, git_check_ignore, is_git_worktree

    git_rules: dict[Path, pathspec.GitIgnoreSpec] = {}
    cm_rules: dict[Path, pathspec.GitIgnoreSpec] = {}
    # Also by identity: on a file system that ignores case the root may be registered with other letter
    # case than the data directory is spelled with, and no text comparison sees that one lies in the other (#230).
    excluded_ids = {path_identity(base) for base in excluded} - {None}

    def left_out(path: Path) -> bool:
        return (any(path.is_relative_to(base) for base in excluded) or (nested is not None and nested.covers(path))
                or (bool(excluded_ids) and path_identity(path) in excluded_ids))

    def link(item: Path) -> bool:
        if not item.is_symlink():
            return False
        if links is not None:
            # What the link says, one call: a chain, a loop and a broken link need no more, as every link inside the
            # root is noted too. (A link outside the root that leads back in is not: it is treated as today.)
            try:
                target = os.readlink(item)
                if os.name == "nt" and target.startswith("\\\\?\\") and not target.startswith("\\\\?\\UNC\\"):
                    target = target[4:]
                links.append((str(item), target))
            except OSError:  # replaced under the walk: noted as gone, the signature differs and a pass resolves again
                links.append((str(item), None))
        return True

    def matches(rules_map: dict[Path, pathspec.GitIgnoreSpec], item: Path, is_dir: bool = False) -> bool:
        answer = False
        for base, spec in sorted(rules_map.items(), key=lambda row: len(row[0].parts)):
            if item.is_relative_to(base):
                relative = item.relative_to(base).as_posix() + ("/" if is_dir else "")
                checked = spec.check_file(relative)
                if checked.include is not None:
                    answer = checked.include
        return answer

    use_git = is_git_worktree(root)
    candidates: list[Path] = []
    for directory, dirs, names in os.walk(root, followlinks=False,
                                          onerror=errors.append if errors is not None else None):
        folder = Path(directory)
        if is_memory_path(folder, root) or left_out(folder):
            dirs[:] = []
            continue
        ignore = folder / ".gitignore"
        if ignore.is_file() and not ignore.is_symlink():
            with suppress(OSError):
                git_rules[folder] = pathspec.GitIgnoreSpec.from_lines(ignore.read_text(encoding="utf-8", errors="replace").splitlines())
        cm_ignore = folder / CAIRNMINDIGNORE
        if cm_ignore.is_file() and not cm_ignore.is_symlink():
            with suppress(OSError):
                cm_rules[folder] = pathspec.GitIgnoreSpec.from_lines(cm_ignore.read_text(encoding="utf-8", errors="replace").splitlines())

        dirs[:] = sorted(
            name for name in sorted(dirs)  # sorted before the links are noted: the same order every round
            if not (link(folder / name) or excluded_directory(name)) and not is_memory_path(folder / name, root)
            # A memory package kept deeper in the tree, such as a sample, is somebody's stored memory
            # and not text of this project: its rows are not indexed as sources.
            and not (folder.name.casefold() == ".memory" and name.casefold() in ("cairnmind", "devbrain"))
            and not left_out(folder / name)
            and not matches(cm_rules, folder / name, True)
            and not matches(git_rules, folder / name, True)
        )
        for name in sorted(names):
            path = folder / name
            if link(path) or _is_secret(path) or _is_managed_mcp_config(path, root) or matches(cm_rules, path):
                continue
            if use_git:
                candidates.append(path)
            elif not matches(git_rules, path):
                yield path

    if use_git and candidates:
        rel_map = [(path, path.relative_to(root).as_posix()) for path in candidates]
        git_ignored = git_check_ignore(root, [rel for _, rel in rel_map], no_index=True)
        for path, rel in rel_map:
            if git_ignored is not None:
                if rel not in git_ignored:
                    yield path
            elif not matches(git_rules, path):
                yield path


def _indexed_spans(lines: list[str], uri: str) -> list[tuple[int, int]]:
    """The [first, end) line ranges that are indexed: all of a file, except, in the managed files (AGENTS.md, CLAUDE.md and the recorder subagent's file), the lines of
    Cairnmind's own block (#313), found as extraction finds it. An unclosed block is indexed as it is."""
    if PurePosixPath(uri.replace("\\", "/")).name not in MANAGED_FILES:
        return [(0, len(lines))]
    from cairnmind.agents.extraction_input import OWN_BLOCK
    text, spans, start = "\n".join(lines), [], 0
    for match in OWN_BLOCK.finditer(text):
        spans.append((start, text.count("\n", 0, match.start())))
        start = text.count("\n", 0, match.end()) + 1
    spans.append((start, len(lines)))
    return [(first, end) for first, end in spans if end > first]


def _chunks(text: str, uri: str = ""):
    """The windows of a text as (content, first line, last line), numbered as the file is. ``uri`` names the file: the
    managed block of AGENTS.md and CLAUDE.md is left out of it; the source text itself stays whole."""
    lines = text.splitlines()
    for first, stop in _indexed_spans(lines, uri):
        start = first
        while start < stop:
            end = start
            size = 0
            while end < stop and end - start < 80:
                length = len(lines[end]) + 1
                if end > start and size + length > 6000:
                    break
                size += length
                end += 1
            if end == start:
                end += 1
            content = "\n".join(lines[start:end])
            # A minified line still remains fully archived and searchable in pieces.
            if len(content) > 6000:
                for offset in range(0, len(content), 6000):
                    yield content[offset:offset + 6000], start + 1, end
            else:
                yield content, start + 1, end
            start = max(start + 1, end - 12) if end < stop else end


def _file_identity(status: os.stat_result, ctime_ns: int | None = None) -> tuple:
    return (status.st_dev, status.st_ino, status.st_mode, status.st_size,
            status.st_mtime_ns, status.st_ctime_ns if ctime_ns is None else ctime_ns)


def _tree_state(
    store,
    project_id: str,
    listing: dict | None = None,
    *,
    files_fn=None,
    parser_identity_fn=None,
) -> tuple[str, int, bool, list[dict]]:
    """The signature, the newest file time seen, whether every directory could be listed, and the folders
    that are not available on this machine (#371): those are named in the signature and never walked.
    `listing` receives what the walk found, so that the indexing that follows does not walk again (#409)."""
    from cairnmind.pal.private_files import change_time_ns

    walk_files = _files if files_fn is None else files_fn
    get_parser_id = parser_identity if parser_identity_fn is None else parser_identity_fn
    project = store.resolve_project(project_id)
    entries: list[Any] = [get_parser_id()]
    newest, errors, away = 0, [], []
    links: list[tuple[str, str]] = []
    if listing is not None:
        listing.update(files={}, errors=errors, links=links)
    for folder in project_folders.folder_states(store, project):
        root = Path(folder["root"])
        if folder["state"] != "available":
            entries.append([folder["root"], folder["state"]])
            away.append(folder)
            continue
        walked = [] if listing is None else listing["files"].setdefault(folder["root"], [])
        for path in walk_files(root, *_walk_args(store, root, errors, links)):
            walked.append(path)
            try:
                status = os.stat(path, follow_symlinks=False)
            except OSError:
                entries.append([str(path), None])
                continue
            ctime_ns = change_time_ns(path, status)
            entries.append([str(path), status.st_size, status.st_mtime_ns, ctime_ns])
            newest = max(newest, status.st_mtime_ns, ctime_ns)
    entries.extend(links)  # a link moved, added or removed alone changes the signature, so a pass runs (#451)
    return hashlib.sha256(_json(entries).encode()).hexdigest(), newest, not errors, away
