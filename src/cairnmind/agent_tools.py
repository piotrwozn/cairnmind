"""Scoped file tools and explicit one-use command approvals for Cairnmind agents.

File traversal uses directory descriptors and never follows a project symlink.
An approved shell command runs as the user; this module is not an OS sandbox.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import inspect
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import sys
from uuid import uuid4

from .agent_journal import AgentJournal, JournalError
from .agent_write_guard import ChangeLedger, change_of, change_preview, configures_execution
from .config import ENV_PREFIXES
from .path_spelling import entry_identity, identity, is_alias
from .process_identity import group_running, recorded, start as process_start


MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 128 * 1024
MAX_COMMAND_OUTPUT_BYTES = 512 * 1024
MAX_WALK_ENTRIES = 20000
MAX_SEARCH_BYTES = 16 * 1024 * 1024
# Stopping a command's process group: SIGTERM, up to TERM_GRACE_SECONDS for the
# group to end, SIGKILL to what is left, then up to KILL_WAIT_SECONDS until no
# process of the group runs. Past that the result says processes may remain.
TERM_GRACE_SECONDS = 0.5
KILL_WAIT_SECONDS = 1.0
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SECRET_ENV = re.compile(r"(?:TOKEN|PASSWORD|SECRET|API_?KEY|AUTHORIZATION|CREDENTIAL)", re.I)
_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_MEMORY_NAMES = ("cairnmind", "devbrain", "cairnmind-published")
_MEMORY_PAIRS = tuple((".memory", name) for name in _MEMORY_NAMES)
# A command's first step: enter the verified directory by its descriptor, close
# it, then become the shell. A /proc/self/fd/N path does not exist on macOS and
# /dev/fd/N is no working directory there (#227). os.fchdir in a preexec_fn
# would do the same, but the parent runs threads (asyncio.to_thread) and Python
# documents preexec_fn as unsafe with threads; this keeps subprocess's own
# fork/exec. -I -S: no environment, site or current directory reaches it.
# Python starts with SIGPIPE and SIGXFSZ ignored and exec keeps that, so the
# step restores their defaults, as subprocess does before a direct exec.
_ENTER_DIRECTORY = ("import os,sys,signal as s;d=int(sys.argv[1]);os.fchdir(d);os.close(d);"
                    "s.signal(s.SIGPIPE,s.SIG_DFL);s.signal(s.SIGXFSZ,s.SIG_DFL);"
                    "os.execv(sys.argv[2],sys.argv[2:])")


def _tool(name, description, properties, required):
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False}}


_PATH = {"type": "string", "minLength": 1, "maxLength": 4096}
_HASH = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
FILE_TOOLS = [
    _tool("list_files", "List project files; symlinks and protected runtime/Git memory directories are not traversed.",
          {"path": _PATH, "recursive": {"type": "boolean"},
           "limit": {"type": "integer", "minimum": 1, "maximum": 2000}}, []),
    _tool("read_file", "Read UTF-8 project text. sha256 identifies the whole file, even for a line slice.",
          {"path": _PATH, "start_line": {"type": "integer", "minimum": 1},
           "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000}}, ["path"]),
    _tool("search_files", "Search a literal string in project UTF-8 files with bounded results.",
          {"query": {"type": "string", "minLength": 1, "maxLength": 2000}, "path": _PATH,
           "case_sensitive": {"type": "boolean"},
           "max_results": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["query"]),
    _tool("write_file", "Create a file exclusively with expected_sha256=null, or replace the exact version read. "
          "Replacing needs every line of that version read whole in this conversation; use apply_patch to change a part.",
          {"path": _PATH, "content": {"type": "string", "maxLength": MAX_FILE_BYTES},
           "expected_sha256": {"anyOf": [_HASH, {"type": "null"}]}},
          ["path", "content", "expected_sha256"]),
    _tool("apply_patch", "Replace one exact nonempty passage in the exact file version read; old_text must occur once.",
          {"path": _PATH, "expected_sha256": _HASH, "old_text": {"type": "string", "minLength": 1},
           "new_text": {"type": "string"}}, ["path", "expected_sha256", "old_text", "new_text"]),
    _tool("delete_file", "Delete the exact regular file version read. Directories are not deleted.",
          {"path": _PATH, "expected_sha256": _HASH}, ["path", "expected_sha256"]),
    _tool("run_command", "Request one human-approved shell command, in this project's selected directory.",
          {"command": {"type": "string", "minLength": 1, "maxLength": 16000}, "cwd": _PATH,
           "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 3600}}, ["command"]),
]
_SCHEMAS = {row["name"]: row["parameters"] for row in FILE_TOOLS}
_MUTATIONS = {"write_file", "apply_patch", "delete_file", "run_command"}


class ToolError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _bounded_int(value, default, minimum, maximum):
    value = default if value is None else value
    if type(value) is not int or not minimum <= value <= maximum:
        raise ToolError("invalid_arguments", "Integer tool limit is invalid")
    return value


def _windows_reading(parts):
    """The casefolded parts as a Windows file system would read them, for the protected-name comparison only.

    Windows separates on a backslash, ends a name at a colon (`.git::$INDEX_ALLOCATION` is the directory itself)
    and drops trailing dots and spaces, so `.git.`, `.git `, `.GIT\\hooks` and `.git::$INDEX_ALLOCATION` all name
    `.git` there (#230), the way git's own NTFS check reads them. On Linux these are ordinary names: the parts
    the tools open stay as they were, and only a name that reads as a protected one is refused.
    """
    read = []
    for part in parts:
        for piece in part.split("\\"):
            piece = piece.split(":", 1)[0].rstrip(". ").casefold()
            if piece:
                read.append(piece)
    return tuple(read)


def _parts(value, *, allow_root=False):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
        raise ToolError("invalid_path", "A project-relative path is required")
    path = PurePosixPath(value)
    if path.is_absolute() or re.match(r"^[A-Za-z]:", value) or ".." in path.parts:
        raise ToolError("outside_worktree", "Path must stay inside the selected worktree")
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if not parts and not allow_root:
        raise ToolError("invalid_path", "A regular file path is required")
    # Compared without case, as memory_paths does: on a case-insensitive file system `.GIT/hooks` is
    # `.git/hooks` (#230) and `.Memory/Cairnmind-Published` is the published directory (#170). A short alias
    # (`GIT~1`) and any other spelling are refused by identity when the directories are opened.
    folded = _windows_reading(parts)
    if any(part == ".git" for part in folded) or any(
        folded[index:index + 2] in _MEMORY_PAIRS for index in range(len(folded))
    ):
        raise ToolError("protected_path", "Git internals and managed memory must use their dedicated interfaces")
    return parts


def _group_exists(pgid):
    """False only when the kernel says no process (not even a zombie) has group pgid."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    return True


def _group_running(pgid):
    """Whether a process of group pgid still runs. A zombie has ended and only
    waits to be reaped; a signalled process counts until the kernel has ended it.
    It reads every process of the machine, so callers run it off the event loop."""
    if not _group_exists(pgid):
        return False
    # Cannot tell (None), so never claim the group has ended.
    return group_running(pgid) is not False


async def _group_ended(pgid, seconds):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    delay = .002
    # The process scan runs in a thread so that a machine with many processes
    # does not freeze the loop (and the UI on it) during a stop.
    while await asyncio.to_thread(_group_running, pgid):
        now = loop.time()
        if now >= deadline:
            return False
        await asyncio.sleep(min(delay, deadline - now))
        delay = min(delay * 2, .05)
    return True


async def _settle(task):
    """Wait until task is done even if the waiter is cancelled meanwhile (the
    task itself is not cancelled). Returns whether a cancellation arrived."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


def _stopped(task):
    """A finished stop task's verdict: True only when the group was confirmed gone."""
    return not task.cancelled() and task.exception() is None and task.result() is True


class ToolExecutor:
    def __init__(self, settings, worktree, *, session_id, run_id, generation, journal=None,
                 approve=None, command_timeout_seconds=120):
        self.settings = settings
        supplied = Path(worktree).expanduser()
        if supplied.is_symlink():
            raise ToolError("outside_worktree", "The selected worktree cannot be a symlink")
        self.worktree = supplied.resolve()
        if not self.worktree.is_dir():
            raise ToolError("invalid_worktree", "The selected worktree must exist")
        self._root_identity = (self.worktree.stat().st_dev, self.worktree.stat().st_ino)
        self.session_id, self.run_id, self.generation = session_id, run_id, generation
        self.journal = journal if journal is not None else AgentJournal(settings, session_id)
        self.approve = approve
        self.command_timeout_seconds = command_timeout_seconds
        self.current_tool_call_id = None
        self._running = {}
        self._starting = {}
        self._stopping = {}
        self._left_running = set()  # cancelled actions whose processes were not confirmed gone
        self._interrupted = set()
        self._interrupt_epoch = 0
        # Files changed since the person's last approval of a command or a third-party call (#176),
        # read back from the journal so a restart does not forget an unapproved write.
        self.changes = ChangeLedger()
        events = getattr(self.journal, "events", None)
        if callable(events):
            try:
                self.changes.replay(events(), lambda value: "/".join(_parts(value)))
            except (JournalError, OSError, ValueError, TypeError):
                self.changes.complete = False

    @contextmanager
    def _root(self):
        fd = os.open(self.worktree, _FLAGS)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self._root_identity:
                raise ToolError("worktree_changed", "The selected worktree identity changed")
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _spelled(directory, name):
        if is_alias(name, directory):
            raise ToolError("invalid_path", "A short alias of a file name is not accepted; use its full name")

    @staticmethod
    def _not_protected(directory, found, in_memory):
        """Refuse an entry of `directory` that is `.git` (or, in `.memory`, a managed memory directory) under
        another spelling: decided by identity, which no spelling changes (#230)."""
        if found is not None and any(entry_identity(name, directory) == found
                                     for name in (".git", *(_MEMORY_NAMES if in_memory else ()))):
            raise ToolError("protected_path", "Git internals and managed memory must use their dedicated interfaces")

    @contextmanager
    def _directory(self, parts, *, create=False, leaf=None):
        with self._root() as root:
            current = os.dup(root)
            in_memory = False
            try:
                for part in parts:
                    self._spelled(current, part)
                    try:
                        following = os.open(part, _FLAGS, dir_fd=current)
                    except FileNotFoundError:
                        if not create:
                            raise
                        os.mkdir(part, 0o755, dir_fd=current)
                        following = os.open(part, _FLAGS, dir_fd=current)
                    found = identity(os.fstat(following))
                    try:
                        self._not_protected(current, found, in_memory)
                    except ToolError:
                        os.close(following)
                        raise
                    in_memory = found == entry_identity(".memory", current)
                    os.close(current)
                    current = following
                if leaf is not None:
                    self._spelled(current, leaf)
                    self._not_protected(current, entry_identity(leaf, current), in_memory)
                yield current
            finally:
                os.close(current)

    @contextmanager
    def _parent(self, value, *, create=False):
        parts = _parts(value)
        with self._directory(parts[:-1], create=create, leaf=parts[-1]) as parent:
            yield parent, parts[-1], "/".join(parts)

    @staticmethod
    def _bytes_at(parent, leaf):
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ToolError("not_regular_file", "Only regular project files are supported")
            if info.st_size > MAX_FILE_BYTES:
                raise ToolError("file_too_large", "Project file exceeds the tool byte budget")
            raw = handle.read(MAX_FILE_BYTES + 1)
            after = os.fstat(handle.fileno())
            if len(raw) > MAX_FILE_BYTES:
                raise ToolError("file_too_large", "Project file exceeds the tool byte budget")
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ):
                raise ToolError("file_changed", "Project file changed while being read")
            return raw, after

    def _read(self, path):
        with self._parent(path) as (parent, leaf, relative):
            raw, info = self._bytes_at(parent, leaf)
        return relative, raw, info

    @staticmethod
    def _check_hash(raw, expected):
        if not isinstance(expected, str) or not _DIGEST.fullmatch(expected):
            raise ToolError("invalid_arguments", "An exact sha256 from read_file is required")
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ToolError("file_changed", "File differs from the version read; read it again before editing")

    def _write(self, path, content, expected):
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ToolError("file_too_large", "New UTF-8 content exceeds the tool byte budget")
        raw = content.encode("utf-8")
        if expected is not None and (not isinstance(expected, str) or not _DIGEST.fullmatch(expected)):
            raise ToolError("invalid_arguments", "An exact sha256 or null for an exclusive create is required")
        with self._parent(path, create=expected is None) as (parent, leaf, relative):
            previous, info = None, None
            if expected is not None:
                previous, info = self._bytes_at(parent, leaf)
                self._check_hash(previous, expected)
            temporary = ".cairnmind-api-" + uuid4().hex
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         stat.S_IMODE(info.st_mode) if info else 0o644, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    if info is not None:
                        os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if expected is None:
                    # link() provides exclusive publication without clobbering a
                    # file another process created after the initial request.
                    os.link(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                else:
                    current, current_info = self._bytes_at(parent, leaf)
                    self._check_hash(current, expected)
                    if (info.st_dev, info.st_ino, info.st_mode) != (
                        current_info.st_dev, current_info.st_ino, current_info.st_mode
                    ):
                        raise ToolError("file_changed", "File identity changed while preparing the edit")
                    os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return {"ok": True, "path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                "created": expected is None, "previous_sha256": expected}

    @staticmethod
    def _directory_names(fd):
        names, truncated = [], False
        with os.scandir(fd) as entries:
            for entry in entries:
                if len(names) >= MAX_WALK_ENTRIES:
                    truncated = True
                    break
                names.append(entry.name)
        return sorted(names), truncated

    def _walk(self, path=".", recursive=True, budget=None):
        parts = _parts(path, allow_root=True)
        budget = budget if budget is not None else {"count": 0, "truncated": False}

        def descend(fd, prefix, depth):
            names, truncated = self._directory_names(fd)
            budget["truncated"] |= truncated
            for name in names:
                relative = (*prefix, name)
                try:
                    _parts("/".join(relative))
                except ToolError:
                    continue
                budget["count"] += 1
                if budget["count"] > MAX_WALK_ENTRIES:
                    budget["truncated"] = True
                    return
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                kind = "symlink" if stat.S_ISLNK(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "file"
                yield {"path": "/".join(relative), "type": kind, "bytes": info.st_size}
                if recursive and kind == "directory":
                    if depth >= 32:
                        budget["truncated"] = True
                        continue
                    try:
                        child = os.open(name, _FLAGS, dir_fd=fd)
                    except (FileNotFoundError, NotADirectoryError, OSError):
                        budget["truncated"] = True
                        continue
                    try:
                        yield from descend(child, relative, depth + 1)
                    finally:
                        os.close(child)
                    if budget["count"] > MAX_WALK_ENTRIES:
                        return

        with self._directory(parts) as fd:
            yield from descend(fd, parts, 0)

    def _list(self, args):
        limit = _bounded_int(args.get("limit"), 500, 1, 2000)
        recursive = args.get("recursive", False)
        if type(recursive) is not bool:
            raise ToolError("invalid_arguments", "recursive must be a boolean")
        budget = {"count": 0, "truncated": False}
        rows = []
        iterator = self._walk(args.get("path", "."), recursive, budget)
        try:
            for row in iterator:
                if len(rows) >= limit:
                    budget["truncated"] = True
                    break
                rows.append(row)
        finally:
            iterator.close()
        return {"ok": True, "files": rows, "truncated": budget["truncated"]}

    def _read_file(self, args):
        first = _bounded_int(args.get("start_line"), 1, 1, 100000000)
        limit = _bounded_int(args.get("max_lines"), 2000, 1, 2000)
        relative, raw, _ = self._read(args["path"])
        text = raw.decode("utf-8")
        if "\0" in text:
            raise ToolError("binary_file", "Only UTF-8 text files are supported")
        lines = text.splitlines(keepends=True)
        rows = lines[first - 1:first - 1 + limit]
        selected = "".join(rows)
        content = selected[:MAX_TEXT_CHARS]
        # end_line: the last line delivered whole; a character cut can end inside a line.
        whole, size = 0, 0
        for row in rows:
            size += len(row)
            if size > MAX_TEXT_CHARS:
                break
            whole += 1
        return {"ok": True, "path": relative, "content": content, "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw), "start_line": first, "end_line": first - 1 + whole, "total_lines": len(lines),
                "truncated": first != 1 or first - 1 + limit < len(lines) or len(selected) > MAX_TEXT_CHARS}

    def _search(self, args):
        query = args.get("query")
        if not isinstance(query, str) or not 1 <= len(query) <= 2000:
            raise ToolError("invalid_arguments", "A bounded nonempty literal search query is required")
        sensitive = args.get("case_sensitive", True)
        if type(sensitive) is not bool:
            raise ToolError("invalid_arguments", "case_sensitive must be a boolean")
        maximum = _bounded_int(args.get("max_results"), 100, 1, 1000)
        path = args.get("path", ".")
        _parts(path, allow_root=True)
        needle = query if sensitive else query.casefold()
        hits, scanned, total_bytes = [], 0, 0
        budget = {"count": 0, "truncated": False}
        try:
            with self._directory(_parts(path, allow_root=True)):
                pass
            candidates = self._walk(path, True, budget)
        except NotADirectoryError:
            candidates = iter([{"path": path, "type": "file", "bytes": 0}])
        try:
            for row in candidates:
                if row["type"] != "file":
                    continue
                if scanned >= 3000 or total_bytes + row["bytes"] > MAX_SEARCH_BYTES:
                    budget["truncated"] = True
                    break
                try:
                    relative, raw, _ = self._read(row["path"])
                    scanned += 1
                    total_bytes += len(raw)
                    text = raw.decode("utf-8")
                    if "\0" in text:
                        continue
                except (ToolError, OSError, UnicodeError):
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    haystack = line if sensitive else line.casefold()
                    if needle in haystack:
                        if len(hits) >= maximum:
                            budget["truncated"] = True
                            break
                        hits.append({"path": relative, "line": number, "text": line[:1000]})
                if len(hits) >= maximum:
                    budget["truncated"] = True
                    break
        finally:
            close = getattr(candidates, "close", None)
            if close:
                close()
        return {"ok": True, "matches": hits, "scanned_files": scanned, "scanned_bytes": total_bytes,
                "truncated": budget["truncated"]}

    def _file_effect(self, name, args):
        if name == "write_file":
            return self._write(args["path"], args["content"], args["expected_sha256"])
        relative, raw, _ = self._read(args["path"])
        self._check_hash(raw, args["expected_sha256"])
        if name == "apply_patch":
            old, new = args["old_text"], args["new_text"]
            text = raw.decode("utf-8")
            if not isinstance(old, str) or not old or not isinstance(new, str) or text.count(old) != 1:
                raise ToolError("patch_ambiguous", "old_text must be nonempty and occur exactly once")
            return self._write(relative, text.replace(old, new, 1), args["expected_sha256"])
        with self._parent(relative) as (parent, leaf, _):
            current, _ = self._bytes_at(parent, leaf)
            self._check_hash(current, args["expected_sha256"])
            os.unlink(leaf, dir_fd=parent)
            os.fsync(parent)
        return {"ok": True, "path": relative, "deleted": True, "previous_sha256": args["expected_sha256"]}

    def _clean_environment(self):
        from .api_credentials import credential_environment_names
        from .cockpit_state import CockpitState

        excluded = set(credential_environment_names())
        try:
            for profile in CockpitState(self.settings).load()["profiles"]:
                reference = profile.get("credential_ref")
                if isinstance(reference, str) and reference.startswith("env:"):
                    excluded.add(reference[4:])
        except (ValueError, OSError, TypeError, KeyError):
            raise ToolError("configuration_error", "Credential environment configuration cannot be validated") from None
        environment = {}
        for name, value in os.environ.items():
            if (name in excluded or _SECRET_ENV.search(name) or name in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"}
                    or name.startswith((*ENV_PREFIXES, "BASH_FUNC_")) or self.journal.redact(value) != value):
                continue
            environment[name] = value
        environment.update(PWD=str(self.worktree), TERM="dumb")
        return environment

    async def _terminate(self, process, birth):
        """Stop the command's process group and wait, bounded, until none of it runs.

        Returns True once no process of the group is left running and False when
        that could not be confirmed within the bounds: the caller then reports
        that processes may remain and never claims they are gone (#175). An
        unexpected failure counts as not confirmed; it never escapes. If the
        stop itself is cancelled (the event loop ending), SIGKILL is still sent
        before the cancellation propagates.
        """
        try:
            return await self._signal_group(process.pid, process, birth)
        except asyncio.CancelledError:
            self._kill_group(process, birth)
            raise
        except Exception:
            self._kill_group(process, birth)
            return False

    @staticmethod
    def _kill_group(process, birth):
        try:  # best effort, unless the leader id now names another process
            if recorded(process.pid, birth) is True:
                os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            pass

    @staticmethod
    async def _signal_group(pgid, process, birth):
        def send(signum):
            """False when the group no longer exists at all."""
            try:
                os.killpg(pgid, signum)
            except ProcessLookupError:
                return False
            except PermissionError:
                pass  # a member we may not signal (setuid): the wait below decides
            return True

        # A still-owned leader proves the group. After it has been reaped, an
        # existing group remains reserved by its children until they exit, so
        # a reused leader id means the group had already ended, and a missing
        # leader (reaped, whether or not asyncio has recorded it yet) leaves
        # the id naming only what remains of this group. When the leader's
        # identity cannot be read, nothing is signalled and nothing confirmed.
        owned = recorded(pgid, birth)
        if owned is not True:
            return owned is False
        if not send(signal.SIGTERM):
            return True
        await _group_ended(pgid, TERM_GRACE_SECONDS)
        owned = recorded(pgid, birth)
        if owned is not True:
            return owned is False
        # Whatever the scan saw, SIGKILL what is left of the group: harmless
        # to zombies, and it ends a member the scan could misjudge.
        if not send(signal.SIGKILL):
            return True
        # A signal is delivered asynchronously: the processes end when the
        # kernel next runs them. Do not return before they have.
        return await _group_ended(pgid, KILL_WAIT_SECONDS)

    def _stop(self, process, birth):
        """One shared stop per running command, so whoever asked for it can wait for its end."""
        owner, task = self._stopping.get(process.pid, (None, None))
        if owner is not process:
            task = asyncio.ensure_future(self._terminate(process, birth))
            self._stopping[process.pid] = (process, task)
        return task

    async def interrupt(self):
        self._interrupt_epoch += 1
        # An exec may already be running before asyncio has connected its
        # pipes. Wait for the shielded spawn and let _command register/stop it.
        starting = list(self._starting.values())
        if starting:
            await asyncio.gather(*(asyncio.shield(task) for task in starting), return_exceptions=True)
            await asyncio.sleep(0)
        stops = []
        for pid, (process, birth) in list(self._running.items()):
            self._interrupted.add(pid)
            stops.append(self._stop(process, birth))
        if stops:
            await asyncio.shield(asyncio.gather(*stops, return_exceptions=True))

    async def close(self):
        await self.interrupt()

    async def _command(self, args, action_id, call_id):
        epoch = self._interrupt_epoch
        command = args["command"]
        if not isinstance(command, str) or not command.strip() or len(command) > 16000 or "\0" in command:
            raise ToolError("invalid_arguments", "A bounded nonempty shell command is required")
        if self.journal.redact(command) != command:
            raise ToolError("credential_in_command", "Registered credentials cannot be passed in command arguments")
        parts = _parts(args.get("cwd", "."), allow_root=True)
        configured = self.command_timeout_seconds
        requested = args.get("timeout_seconds", configured)
        if (type(configured) not in (int, float) or not 0.1 <= configured <= 3600
                or type(requested) not in (int, float) or not 0.1 <= requested <= 3600):
            raise ToolError("invalid_arguments", "Command timeout must be between 0.1 and 3600 seconds")
        timeout = min(configured, requested)
        if not sys.executable:
            raise ToolError("command_unavailable", "No interpreter is known to start the command in its directory")
        with self._directory(parts) as directory:
            cwd = str(self.worktree.joinpath(*parts))
            request = {"action_id": action_id, "command": command, "cwd": cwd,
                       "session_id": self.session_id, "run_id": self.run_id, "generation": self.generation,
                       "timeout_seconds": timeout, "tool_call_id": call_id, **self.changes.fields()}
            self.journal.append("approval_requested", request)
            if self.approve is None:
                return {"ok": False, "error_code": "approval_required", "message": "This command requires human approval"}
            decision = self.approve(deepcopy(request))
            if inspect.isawaitable(decision):
                decision = await decision
            approved = decision is True
            self.journal.append("approval_decided", {**request, "approved": approved})
            if not approved:
                return {"ok": False, "error_code": "approval_denied", "message": "Command was not approved"}
            self.changes.approved()  # the person has now seen these files
            if epoch != self._interrupt_epoch:
                return {"ok": False, "interrupted": True, "error_code": "command_interrupted",
                        "message": "Command was interrupted before execution"}
            environment = self._clean_environment()
            # The host supplies the callback; model arguments cannot grant this.
            self.journal.append("action_started", self._action(action_id, "run_command", args, call_id))
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-I", "-S", "-c", _ENTER_DIRECTORY, str(directory),
                "/bin/bash", "--noprofile", "--norc", "-c", command,
                pass_fds=(directory,), env=environment,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True))
            self._starting[action_id] = spawn
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # Do not lose ownership of a process which exec'd before its
                # coroutine returned. Finish setup, terminate, then propagate.
                await _settle(spawn)  # a further cancellation must not lose it either
                if spawn.cancelled() or spawn.exception() is not None:
                    raise  # no process was started: the cancellation is what the caller must see
                process = spawn.result()
                stop = asyncio.ensure_future(self._terminate(process, process_start(process.pid)))
                await _settle(stop)
                if not _stopped(stop):
                    self._left_running.add(action_id)
                raise
            finally:
                self._starting.pop(action_id, None)
        birth = process_start(process.pid)
        self._running[process.pid] = (process, birth)
        counts, buffers = [0, 0], [bytearray(), bytearray()]

        async def drain(stream, index):
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    return
                counts[index] += len(chunk)
                remaining = MAX_COMMAND_OUTPUT_BYTES - len(buffers[index])
                if remaining > 0:
                    buffers[index].extend(chunk[:remaining])

        tasks = [asyncio.create_task(drain(process.stdout, 0)), asyncio.create_task(drain(process.stderr, 1))]
        wait = asyncio.create_task(process.wait())
        combined = asyncio.gather(wait, *tasks)
        timed_out = False

        async def stop_and_wait():
            """Stop the group, or join the stop in progress, and wait until it has
            finished. A cancellation meanwhile is raised only after that (#175)."""
            stop = self._stop(process, birth)
            if await _settle(stop):
                raise asyncio.CancelledError
            return _stopped(stop)

        try:
            if epoch != self._interrupt_epoch:
                self._interrupted.add(process.pid)
                await stop_and_wait()
            try:
                await asyncio.wait_for(asyncio.shield(combined), timeout)
            except asyncio.TimeoutError:
                timed_out = True
                await stop_and_wait()
                try:
                    await asyncio.wait_for(asyncio.shield(combined), 1)
                except asyncio.TimeoutError:
                    combined.cancel()
                    await asyncio.gather(combined, return_exceptions=True)
            # The output can end before an interrupt has finished stopping the
            # group (a process that closed its output, one that ignores SIGTERM):
            # never return while that stop is still in progress.
            owner, stop = self._stopping.get(process.pid, (None, None))
            remaining = owner is process and not await stop_and_wait()
            interrupted = process.pid in self._interrupted
            result = {"ok": process.returncode == 0 and not timed_out and not interrupted,
                      "exit_code": process.returncode, "stdout": buffers[0].decode("utf-8", errors="replace"),
                      "stderr": buffers[1].decode("utf-8", errors="replace"), "stdout_bytes": counts[0],
                      "stderr_bytes": counts[1], "timed_out": timed_out, "interrupted": interrupted,
                      "truncated": any(count > MAX_COMMAND_OUTPUT_BYTES for count in counts)}
            if remaining:
                # Signalled with SIGKILL but still running after the bound: the
                # effect is not finished, so the action stays uncertain.
                result["processes_remaining"] = True
            if not result["ok"]:
                result["error_code"] = "command_interrupted" if interrupted else "command_timeout" if timed_out else "command_failed"
            return result
        except asyncio.CancelledError:
            self._interrupted.add(process.pid)
            # Further cancellations (a second Ctrl+C) must not cut this wait short.
            stop = self._stop(process, birth)
            await _settle(stop)
            if not _stopped(stop):
                self._left_running.add(action_id)
            try:
                await asyncio.wait_for(asyncio.shield(combined), 1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                combined.cancel()
                await asyncio.gather(combined, return_exceptions=True)
            raise
        finally:
            self._running.pop(process.pid, None)
            if self._stopping.get(process.pid, (None,))[0] is process:
                del self._stopping[process.pid]
            self._interrupted.discard(process.pid)
            for task in [wait, *tasks]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(wait, *tasks, return_exceptions=True)

    async def _approve_write(self, name, args, parts, action_id, call_id):
        """One-use approval of a change to a file that configures execution (#176); None when approved.

        The preview is checked against the version the model read, as the effect is, so the change asked
        about is the change made; a stale or impossible change is refused before anyone is asked."""
        relative = "/".join(parts)
        before = None
        if name == "write_file" and args["expected_sha256"] is None:
            try:
                self._read(relative)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(relative)
        else:
            _, raw, _ = self._read(relative)
            self._check_hash(raw, args["expected_sha256"])
            before = raw.decode("utf-8") if name == "apply_patch" else raw.decode("utf-8", errors="replace")
        if name == "write_file":
            after = args["content"]
        elif name == "apply_patch":
            if before.count(args["old_text"]) != 1:
                raise ToolError("patch_ambiguous", "old_text must be nonempty and occur exactly once")
            after = before.replace(args["old_text"], args["new_text"], 1)
        else:
            after = None
        diff, more = change_preview(before, after)
        request = {"kind": "file_write", "action_id": action_id, "tool": name, "path": relative,
                   "change": change_of(name, args), "diff": diff, "diff_more_lines": more,
                   "session_id": self.session_id, "run_id": self.run_id, "generation": self.generation,
                   "tool_call_id": call_id}
        self.journal.append("approval_requested", request)
        if self.approve is None:
            return {"ok": False, "error_code": "approval_required", "path": relative,
                    "message": "A change to a file that configures execution requires human approval"}
        decision = self.approve(deepcopy(request))
        if inspect.isawaitable(decision):
            decision = await decision
        approved = decision is True
        self.journal.append("approval_decided", {**request, "approved": approved})
        if not approved:
            return {"ok": False, "error_code": "approval_denied", "path": relative,
                    "message": "The change to this file was not approved"}
        return None

    def _action(self, action_id, name, args, call_id):
        return {"action_id": action_id, "name": name, "arguments": deepcopy(args),
                "session_id": self.session_id, "run_id": self.run_id, "generation": self.generation,
                "tool_call_id": call_id}

    async def execute(self, name, arguments):
        from jsonschema import Draft202012Validator

        if not isinstance(name, str) or name not in _SCHEMAS or not isinstance(arguments, dict):
            return {"ok": False, "error_code": "invalid_tool", "message": "Unknown tool or invalid arguments"}
        if not Draft202012Validator(_SCHEMAS[name]).is_valid(arguments):
            return {"ok": False, "error_code": "invalid_arguments", "message": "Tool arguments do not match its schema"}
        args = deepcopy(arguments)
        # SDKs may invoke tools concurrently. Capture the engine's binding
        # before the first await instead of rereading mutable shared state.
        call_id = self.current_tool_call_id
        action_id = uuid4().hex
        effect = name in _MUTATIONS
        started = False
        try:
            if name == "run_command":
                result = await self._command(args, action_id, call_id)
                # Approval refusals never count as started side effects.
                started = result.get("error_code") not in {"approval_required", "approval_denied"}
            elif effect:
                parts = _parts(args["path"])
                refused = (await self._approve_write(name, args, parts, action_id, call_id)
                           if configures_execution(parts) else None)
                if refused is not None:
                    result = refused  # a refused change never counts as started
                else:
                    self.journal.append("action_started", self._action(action_id, name, args, call_id))
                    started = True
                    result = self._file_effect(name, args)
                    self.changes.record(result["path"], change_of(name, args, result))
            elif name == "read_file":
                result = self._read_file(args)
            elif name == "list_files":
                result = self._list(args)
            else:
                result = self._search(args)
        except asyncio.CancelledError:
            # There may have been an effect even if the return value was lost.
            # A file change cancelled while the person was still being asked never started (#176).
            if effect and (started or name == "run_command"):
                data = self._action(action_id, name, args, call_id)
                if action_id in self._left_running:
                    data["processes_remaining"] = True  # SIGKILLed, still running at the bound
                self.journal.append("action_interrupted", data)
            self._left_running.discard(action_id)
            if started and name != "run_command":
                # The file may have changed: list it as if it did.
                self.changes.record("/".join(_parts(args["path"])), change_of(name, args))
            raise
        except ToolError as exc:
            result = {"ok": False, "error_code": exc.code, "message": str(exc)}
        except UnicodeError:
            result = {"ok": False, "error_code": "binary_file", "message": "Only valid UTF-8 project text is supported"}
        except FileExistsError:
            result = {"ok": False, "error_code": "file_exists", "message": "Exclusive create refused: file already exists"}
        except FileNotFoundError:
            result = {"ok": False, "error_code": "not_found", "message": "Project file or directory does not exist"}
        except OSError:
            result = {"ok": False, "error_code": "filesystem_error", "message": "Project operation was refused or failed"}
        redacted = self.journal.redact(result)
        if redacted != result and isinstance(redacted, dict):
            redacted["masked"] = True  # a registered secret was replaced: this is not the real text (#157)
        result = redacted
        if started:
            # A command whose processes may still run has no settled outcome either.
            event_type = ("action_interrupted" if result.get("interrupted") or result.get("processes_remaining")
                          else "action_completed")
            self.journal.append(event_type, {**self._action(action_id, name, args, call_id), "result": result})
        return result
