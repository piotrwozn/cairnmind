"""Owner-local cockpit preferences and explicitly scoped project discovery."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from .api_profiles import normalize_profile
from .mcp_servers import apply_project_edit, validate_project_mcp_servers
from .stages import validate_project_stages

DEFAULT_PROFILES = [{"id": "codex", "client": "codex", "label": "Codex"},
                    {"id": "claude", "client": "claude", "label": "Claude"}]
DEFAULT_SLOTS = [{"id": "codex", "profile_id": "codex"}, {"id": "claude", "profile_id": "claude"}]
_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,159}$")
_EXCLUDED = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
             "target", ".memory", ".cairnmind", ".devbrain", ".codex", ".claude", ".cache", ".local", ".ssh"}


def _snapshot(root: Path) -> str | None:
    from .memory_paths import snapshot_directory
    found = snapshot_directory(root)
    return str(found) if found is not None and (found / "manifest.json").is_file() else None


def normalize_path(value: str | Path) -> Path:
    """Translate an explicit Windows drive path, never invoke a shell."""
    text = str(value).strip()
    if not text or "\0" in text or len(text) > 4096:
        raise ValueError("Give a valid folder path.")
    if re.match(r"^[A-Za-z]:[\\/]", text):
        try:
            result = subprocess.run(["wslpath", "-u", text], capture_output=True, text=True, timeout=3, check=True)
            text = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            text = "/mnt/" + text[0].lower() + "/" + text[3:].replace("\\", "/")
    return Path(text).expanduser().resolve()


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("The cockpit folder must belong to the user and have permissions 0700.")


@contextmanager
def _private_lock(path: Path, *, blocking=True):
    _private_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("The cockpit lock must be a private file of the user.")
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def _atomic(path: Path, value: dict) -> None:
    _private_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=".cockpit-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


class CockpitState:
    def __init__(self, settings):
        self.root = settings.data_dir / "cockpit"
        self.path = self.root / "preferences.json"

    def load(self) -> dict:
        defaults = {"version": 2, "profiles": deepcopy(DEFAULT_PROFILES), "scan_roots": [],
                    "default_slots": deepcopy(DEFAULT_SLOTS), "project_slots": {}, "last_project": None,
                    "project_stages": {}, "project_mcp_servers": {}, "language": "en", "view": "focus",
                    "reduced_motion": False}
        if not self.path.exists():
            return defaults
        if self.path.is_symlink():
            raise ValueError("The cockpit settings file must not be a link.")
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or value.get("version") not in {1, 2}:
            raise ValueError("Unsupported version of the cockpit settings.")
        defaults.update(value)
        if (not isinstance(defaults["language"], str) or defaults["language"] not in {"en", "pl"}
                or not isinstance(defaults["view"], str) or defaults["view"] not in {"focus", "cockpit"}
                or type(defaults["reduced_motion"]) is not bool):
            raise ValueError("Invalid display preferences")
        rows = defaults["profiles"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
            raise ValueError("Invalid list of profiles.")
        defaults["profiles"] = [normalize_profile(row) for row in rows]
        if len({row["id"] for row in defaults["profiles"]}) != len(rows):
            raise ValueError("Profile identifiers must be unique.")
        return defaults

    @staticmethod
    def _slots(value, profiles):
        if not isinstance(value, list) or not 1 <= len(value) <= 32:
            raise ValueError("A set of panes has 1 to 32 panes.")
        result, seen = [], set()
        for row in value:
            if (not isinstance(row, dict) or set(row) != {"id", "profile_id"}
                    or not isinstance(row["id"], str) or not _ID.fullmatch(row["id"])
                    or row["id"] in seen or not isinstance(row["profile_id"], str)
                    or row["profile_id"] not in profiles):
                raise ValueError("Invalid client pane configuration.")
            result.append(dict(row))
            seen.add(row["id"])
        return result

    def update(self, changes: dict) -> dict:
        if not isinstance(changes, dict) or set(changes) - {"profiles", "scan_roots", "default_slots", "project_slots", "project_stages", "project_mcp_servers", "project_mcp_server_edit", "last_project", "language", "view", "reduced_motion"}:
            raise ValueError("Unsupported field of the cockpit settings.")
        if {"project_mcp_servers", "project_mcp_server_edit"} <= set(changes):
            raise ValueError("Invalid MCP server lists.")
        with _private_lock(self.root / "preferences.lock"):
            value = self.load()
            original_version = value["version"]
            for key, allowed in (("language", {"en", "pl"}), ("view", {"focus", "cockpit"})):
                if key in changes:
                    if not isinstance(changes[key], str) or changes[key] not in allowed:
                        raise ValueError("Invalid display preference: " + key)
                    value[key] = changes[key]
            if "reduced_motion" in changes:
                if type(changes["reduced_motion"]) is not bool:
                    raise ValueError("Reduced motion must be a boolean")
                value["reduced_motion"] = changes["reduced_motion"]
            if "profiles" in changes:
                rows = changes["profiles"]
                if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
                    raise ValueError("Give 1 to 32 profiles.")
                profiles = [normalize_profile(row) for row in rows]
                if len({row["id"] for row in profiles}) != len(profiles):
                    raise ValueError("Profile identifiers must be unique.")
                value["profiles"] = profiles
            known = {p["id"] for p in value["profiles"]}
            if "scan_roots" in changes:
                roots = changes["scan_roots"]
                if not isinstance(roots, list) or len(roots) > 32:
                    raise ValueError("Invalid list of folders to scan.")
                selected = list(dict.fromkeys(str(normalize_path(p)) for p in roots if isinstance(p, str)))
                if len(selected) != len(roots) or any(not Path(p).is_dir() for p in selected):
                    raise ValueError("Folders to scan must exist and be unique.")
                value["scan_roots"] = selected
            if "default_slots" in changes:
                value["default_slots"] = self._slots(changes["default_slots"], known)
            else:
                self._slots(value["default_slots"], known)
            if "project_slots" in changes:
                mapping = changes["project_slots"]
                if not isinstance(mapping, dict) or len(mapping) > 1000:
                    raise ValueError("Invalid sets of project panes.")
                value["project_slots"] = {_project_id(k): self._slots(v, known) for k, v in mapping.items()}
            else:
                for slots in value["project_slots"].values():
                    self._slots(slots, known)
            # The stage map is checked for shape only; a slot it names may be removed later
            # and the router then reports it as unknown instead of choosing another.
            value["project_stages"] = validate_project_stages(changes.get("project_stages", value["project_stages"]))
            # Third-party MCP servers of the built-in agent (#126): only this person route writes them.
            servers = changes.get("project_mcp_servers", value["project_mcp_servers"])
            if "project_mcp_server_edit" in changes:
                # One project's list, checked against what the editor showed, under this lock.
                servers = apply_project_edit(value["project_mcp_servers"], changes["project_mcp_server_edit"])
            value["project_mcp_servers"] = validate_project_mcp_servers(servers)
            if "last_project" in changes:
                value["last_project"] = None if changes["last_project"] is None else _project_id(changes["last_project"])
            if original_version == 1:
                # Read-only load never migrates. Preserve the exact original
                # bytes before the first successful v2 write, under the lock.
                original = self.path.read_bytes()
                backup = self.root / ("preferences-v1-backup-" + str(time.time_ns()) + ".json")
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
            value["version"] = 2
            _atomic(self.path, value)
            return value


def _project_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 160 or "\0" in value:
        raise ValueError("Invalid project identifier.")
    return value


class ProjectDiscovery:
    """A bounded, cancellable scan of roots explicitly supplied by the user."""
    def __init__(self):
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._state = {"id": None, "state": "idle", "roots": [], "projects": [], "errors": [],
                       "directories": 0, "truncated": False}

    def status(self):
        with self._lock:
            value = deepcopy(self._state)
            value["candidates"] = deepcopy(value["projects"])
            return value

    def cancel(self):
        self._cancel.set()
        return self.status()

    def start(self, roots, *, max_directories=20000):
        if not isinstance(roots, list) or not 1 <= len(roots) <= 32:
            raise ValueError("Choose folders to scan.")
        paths = list(dict.fromkeys(normalize_path(p) for p in roots if isinstance(p, str)))
        if len(paths) != len(roots) or any(not p.is_dir() for p in paths):
            raise ValueError("The chosen folders must exist and be unique.")
        if self._thread and self._thread.is_alive():
            raise ValueError("A scan is already running; cancel it first.")
        self._cancel = threading.Event()
        with self._lock:
            self._state = {"id": str(uuid4()), "state": "running", "roots": list(map(str, paths)), "projects": [],
                           "errors": [], "directories": 0, "truncated": False, "started_at": time.time()}
        self._thread = threading.Thread(target=self._scan, args=(paths, max_directories), daemon=True)
        self._thread.start()
        return self.status()

    def _scan(self, roots, maximum):
        seen, found = set(), set()
        def onerror(error):
            with self._lock:
                if len(self._state["errors"]) < 100:
                    self._state["errors"].append({"path": str(error.filename), "code": type(error).__name__})
        for selected in roots:
            for directory, dirs, _ in os.walk(selected, followlinks=False, onerror=onerror):
                if self._cancel.is_set():
                    break
                path = Path(directory)
                if str(path) in seen:
                    dirs[:] = []
                    continue
                seen.add(str(path))
                with self._lock:
                    self._state["directories"] = len(seen)
                if len(seen) > maximum:
                    with self._lock:
                        self._state["truncated"] = True
                    self._cancel.set()
                    break
                if (path / ".git").exists() and str(path) not in found:
                    found.add(str(path))
                    item = {"name": path.name, "root": str(path),
                            "memory_snapshot": _snapshot(path)}
                    with self._lock:
                        self._state["projects"].append(item)
                dirs[:] = [name for name in sorted(dirs) if name not in _EXCLUDED and not (path / name).is_symlink()]
            if self._cancel.is_set():
                break
        with self._lock:
            self._state["state"] = "cancelled" if self._cancel.is_set() else "completed"
            self._state["ended_at"] = time.time()
