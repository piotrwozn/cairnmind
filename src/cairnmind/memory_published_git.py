"""Published memory read out of a Git commit, read-only, without a store (#172).

`memory diff` and the main-branch import read the published directory of a commit with
`git ls-tree` and `git cat-file`: no checkout, no working tree, no index and nothing written. The
directory of a project is `<project root>/.memory/cairnmind-published`, where the project root is the
directory the command runs on (a subdirectory of the repository when the project is one, #174). The
entries are held to the same rules as a directory on disk: only the three static files and
`<kind>/<uuid>.md`, no links, no submodules, and the package budgets of format version 1.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from .memory_paths import PUBLISHED_RELATIVE, is_reserved_memory
from .memory_published_check import DIRECTORIES, STATIC_FILES
from .memory_published_format import MAX_FILE_BYTES, MAX_FILES, MAX_PACKAGE_BYTES, canonical_uuid
from .terminal_safe import safe_text

# The largest code file whose passages are compared; a larger one is left unchecked.
MAX_CHECKED_BYTES = 32 * 1024 * 1024


class GitReadError(ValueError):
    """A commit or repository that cannot be read."""


def _git(root, *args, data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(["git", "--literal-pathspecs", "-C", str(root), *args], input=data,
                                capture_output=True, timeout=120,
                                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitReadError(f"Git could not be run in {safe_text(root)}: {exc.__class__.__name__}") from None
    if result.returncode:
        reason = safe_text(result.stderr.decode("utf-8", "replace").strip().split("\n")[0][:200])
        raise GitReadError(f"git {args[0]} failed in {safe_text(root)}: {reason or 'no message'}")
    return result.stdout


def resolve(root, revision) -> str:
    """The full commit ID a revision names; anything that could read as an option is refused."""
    if (not isinstance(revision, str) or not revision.strip() or revision.startswith("-") or len(revision) > 256
            or any(character in revision for character in "\0\r\n")):
        raise ValueError("A revision must be a commit name such as HEAD, a branch or a commit ID")
    found = _git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", revision + "^{commit}")
    commit = found.decode("ascii", "replace").strip()
    if len(commit) not in (40, 64) or any(character not in "0123456789abcdef" for character in commit):
        raise GitReadError("Git did not name a commit")
    return commit


def prefix_of(root) -> str:
    """The project root relative to the top of its repository: '' or 'dir/sub/'."""
    if not Path(root).is_dir():
        raise GitReadError(f"{safe_text(root)} is not a directory")
    return _git(root, "rev-parse", "--show-prefix").decode("utf-8", "surrogateescape").strip("\n")


def list_tree(root, commit, path=None):
    """[(mode, type, object ID, size or None, repository path)] of every entry under `path` at `commit`."""
    raw = _git(root, "ls-tree", "-r", "-z", "-l", "--full-tree", commit, *(["--", path] if path else []))
    entries = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.partition(b"\t")
        mode, kind, oid, size = meta.decode("ascii", "replace").split()
        entries.append((mode, kind, oid, int(size) if size.isdigit() else None,
                        name.decode("utf-8", "surrogateescape")))
    return entries


def read_blobs(root, oids) -> dict:
    """{object ID: bytes} through one `git cat-file --batch`."""
    oids = sorted(set(oids))
    if not oids:
        return {}
    raw = _git(root, "cat-file", "--batch", data="".join(oid + "\n" for oid in oids).encode())
    blobs, position = {}, 0
    while position < len(raw):
        end = raw.index(b"\n", position)
        header = raw[position:end].decode("ascii", "replace").split()
        position = end + 1
        if len(header) != 3:
            raise GitReadError(f"Git could not read object {safe_text(header[0] if header else '?')}")
        size = int(header[2])
        blobs[header[0]] = raw[position:position + size]
        position += size + 1
    return blobs


def _entry_error(relative, mode, kind, size):
    parts = relative.split("/")
    if mode == "120000":
        return "symbolic links are not allowed"
    if kind != "blob":
        return "only files are allowed here, no links or directories"
    if len(parts) == 1 and parts[0] in STATIC_FILES:
        return None if size is not None and size <= MAX_FILE_BYTES else "the file is larger than 1 MiB"
    if not (len(parts) == 2 and parts[0] in DIRECTORIES and parts[1].endswith(".md")
            and canonical_uuid(parts[1][:-3])):
        return "unknown entry"
    return None if size is not None and size <= MAX_FILE_BYTES else "the file is larger than 1 MiB"


def published_files(root, revision):
    """(commit ID, {relative path: bytes}, [(path, reason)]) of the published directory at a commit.

    A commit without the directory gives no files and no errors: nothing was published there. With errors,
    the files that could be read are still returned; a strict reader refuses them all, a lenient one (the
    base of `memory diff`) compares what it can.
    """
    commit = resolve(root, revision)
    prefix = prefix_of(root)
    base = prefix + PUBLISHED_RELATIVE.as_posix()
    for directory in (prefix + ".memory", base):
        found = _entry_at(root, commit, directory)
        if found is not None and found[1] != "tree":
            # The directory itself, or .memory above it, is a link, a submodule or a file: refused as on disk.
            return commit, {}, [(directory[len(prefix):], "not a directory (or a symbolic link)")]
    wanted, errors, total = {}, [], 0
    for mode, kind, oid, size, name in list_tree(root, commit, base):
        if not name.startswith(base + "/"):
            continue
        relative = name[len(base) + 1:]
        try:
            relative.encode("utf-8")
        except UnicodeEncodeError:
            errors.append((safe_text(relative.encode("utf-8", "surrogateescape").decode("utf-8", "replace")),
                           "the name is not UTF-8"))
            continue
        error = _entry_error(relative, mode, kind, size)
        if error:
            errors.append((relative, error))
            continue
        wanted[relative] = oid
        total += size
        if len(wanted) > MAX_FILES or total > MAX_PACKAGE_BYTES:
            return commit, {}, [("", "the package exceeds 100,000 files or 256 MiB")]
    blobs = read_blobs(root, wanted.values())
    return commit, {relative: blobs[oid] for relative, oid in wanted.items()}, errors


def _entry_at(root, commit, path):
    """(mode, type) of the one entry at `path` in a commit, not recursing into it; None when absent."""
    raw = _git(root, "ls-tree", "-z", "--full-tree", commit, "--", path)
    for entry in raw.split(b"\0"):
        meta, _, name = entry.partition(b"\t")
        if entry and name.decode("utf-8", "surrogateescape") == path:
            mode, kind, _ = meta.decode("ascii", "replace").split()
            return mode, kind
    return None


def project_files(root, commit):
    """{project-relative path: (object ID, size)} of the files of the project at a commit.

    Links are excluded, and so are the reserved memory directories: a passage is only ever compared with
    the project's own code, never with a published file (which always holds its own quote) or the archive.
    """
    prefix = prefix_of(root)
    files = {}
    for mode, kind, oid, size, name in list_tree(root, commit, prefix.rstrip("/") or None):
        relative = name[len(prefix):]
        if kind == "blob" and mode != "120000" and name.startswith(prefix) and not _memory_path(relative):
            files[relative] = (oid, size)
    return files


def _memory_path(relative) -> bool:
    """A path in a reserved memory directory, of this project or of a project nested in it (#174)."""
    return is_reserved_memory(relative) or ".memory" in (part.lower() for part in relative.split("/")[:-1])
