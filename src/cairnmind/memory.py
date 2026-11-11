"""Project-only, evidence-preserving portable memory; no model or Git execution.

Content-addressed JSON objects are published before an atomic manifest switch.
Operational authority (sessions, ACKs, leases and local check provenance) is
never restored from a portable package.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from uuid import UUID, uuid4

from .memory_paths import EARLIER_MEMORY_RELATIVE, MEMORY_RELATIVE, snapshot_directory
from .store import KINDS, STATUSES, TASK_STATES, _clean, _insert, _json, _now, _one, _set

# Written into every new manifest, and so into its identifier. Manifests written under the
# earlier name are read as they are: rewriting one would change its identifier and its history.
FORMAT = "cairnmind-portable-memory"
FORMATS = (FORMAT, "devbrain-portable-memory")
VERSION = 1
TABLES = ("sources", "records", "record_revisions", "plans", "plan_revisions", "nodes", "edges", "check_runs")
HISTORY_LIMIT = 5
MAX_OBJECT_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ROWS = 1_000_000
_SHA = re.compile(r"^[a-f0-9]{64}$")
_CODE_EDGES = {"imports", "defines", "calls", "references", "test_imports", "snapshot_of"}
_GLOBAL_NODES = KINDS | {"plan", "source"}
_LOCAL_SOURCE_KINDS = {"code", "document", "file", "check_output", "check_profile"}


class MemoryConflict(ValueError):
    """Identity, ancestry, local edits or publication require explicit resolution."""


def _bytes(value):
    return _json(value).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _manifest_id(manifest):
    return _digest({key: value for key, value in manifest.items() if key != "snapshot_id"})


def _logical(table, original):
    row = {k: deepcopy(v) for k, v in _clean(original).items() if k not in {"in", "out"}}
    if table == "sources":
        row.pop("current_key", None)
    if table in {"nodes", "edges"}:
        row.pop("is_current", None)
    if table == "records":
        row.pop("active_key", None)
    if table == "record_revisions" and isinstance(row.get("snapshot"), dict):
        row["snapshot"] = _logical("records", row["snapshot"])
    if table == "plan_revisions" and isinstance(row.get("snapshot"), dict):
        row["snapshot"] = deepcopy(row["snapshot"])
        row["snapshot"]["tasks"] = [_logical("records", task) for task in row["snapshot"].get("tasks", [])]
    if table == "check_runs":
        provenance = row.pop("portable_provenance", None)
        if row.get("origin") == "portable_import" and isinstance(provenance, dict):
            row["origin"] = provenance.get("original_origin", "unknown")
    return row


def _state_hash(payload):
    # Local parser/index/vector/source-currentness changes are not knowledge edits.
    state = {table: sorted([_logical(table, row) for row in payload.get(table, [])], key=lambda r: r["id"])
             for table in ("records", "record_revisions", "plans", "plan_revisions")}
    state["edges"] = sorted([_logical("edges", row) for row in payload.get("edges", [])
                             if row.get("kind") not in _CODE_EDGES
                             and row.get("kind") not in {"supported_by", "depends_on", "supersedes", "has_step"}],
                            key=lambda r: r["id"])
    # Raw conversational/manual memory is portable state even before extraction.
    # Parser metadata, local file reindexing and runner outputs are derived noise.
    state["source_heads"] = sorted([
        {key: row.get(key) for key in ("id", "uri", "sha256", "is_current")}
        for row in payload.get("sources", []) if row.get("kind") not in _LOCAL_SOURCE_KINDS], key=lambda r: r["id"])
    return _digest(state)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property in memory package")
        result[key] = value
    return result


def _read(path, limit):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Memory package file is missing or is a symbolic link")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Memory package exceeds its byte budget")
    return raw


def _parse(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON number")))
        _bytes(value)
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid memory package JSON") from exc


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"Invalid memory {label}")
    return value


def _version(value):
    if type(value) is not int or not 1 <= value <= MAX_ROWS:
        raise ValueError("Invalid memory entity version")


def _validate(payload, project_id, active_plan_id=None):
    maps = {}
    for table in TABLES:
        rows = payload.get(table)
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise ValueError("Invalid memory collection")
        mapping = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid memory entity")
            identifier = _text(row.get("id"), "entity ID")
            if identifier in mapping or row.get("project_id") != project_id:
                raise ValueError("Duplicate or foreign-project memory entity")
            mapping[identifier] = row
        maps[table] = mapping

    def source_refs(refs):
        if not isinstance(refs, list) or not all(isinstance(s, str) and s in maps["sources"] for s in refs):
            raise ValueError("Missing or foreign memory source reference")

    def record(row):
        if (not isinstance(row, dict) or row.get("project_id") != project_id
                or row.get("kind") not in KINDS or row.get("status") not in STATUSES):
            raise ValueError("Invalid memory record")
        _text(row.get("id"), "record ID")
        if not isinstance(row.get("content"), str) or not row["content"].strip():
            raise ValueError("Invalid memory record content")
        _text(row.get("key"), "record key")
        _version(row.get("version"))
        source_refs(row.get("source_ids"))
        if not row["source_ids"]:
            raise ValueError("Memory records require evidence")
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("Invalid memory record metadata")
        if row.get("supersedes") and row["supersedes"] not in maps["records"]:
            raise ValueError("Missing superseded memory record")
        if row["kind"] != "task":
            return  # Checkpoint/fact metadata is arbitrary data, not a task contract.
        if metadata.get("state", "todo") not in TASK_STATES:
            raise ValueError("Invalid memory task state")
        for flag in ("requires_test", "requires_review", "test_passed", "review_passed", "acceptance_passed"):
            if flag in metadata and type(metadata[flag]) is not bool:
                raise ValueError("Invalid memory task boolean")
        criteria = metadata.get("acceptance_criteria", [])
        if not isinstance(criteria, list) or not all(isinstance(c, str) and c.strip() for c in criteria):
            raise ValueError("Invalid memory acceptance criteria")
        profiles = metadata.get("required_profiles", [])
        if not isinstance(profiles, list) or not all(isinstance(p, str) and p.strip() for p in profiles):
            raise ValueError("Invalid memory required check profiles")
        links = metadata.get("criterion_checks", {})
        if not isinstance(links, dict) or not all(isinstance(text, str) and isinstance(check, str) and check.strip()
                                                  for text, check in links.items()):
            raise ValueError("Invalid memory criterion checks")
        proposals = metadata.get("check_proposals", {})
        if not isinstance(proposals, dict) or not all(
                isinstance(argv, list) and all(isinstance(part, str) for part in argv) for argv in proposals.values()):
            raise ValueError("Invalid memory check proposals")
        if "step_index" in metadata and (type(metadata["step_index"]) is not int or metadata["step_index"] < 0):
            raise ValueError("Invalid memory plan step index")
        for name in ("test_source_ids", "review_source_ids", "acceptance_source_ids"):
            if name in metadata:
                source_refs(metadata[name])
        dependencies = metadata.get("depends_on", [])
        if not isinstance(dependencies, list) or not all(
                isinstance(i, str) and i in maps["records"] and maps["records"][i].get("kind") == "task"
                for i in dependencies):
            raise ValueError("Missing memory task dependency")
        if metadata.get("plan_ref") is not None and metadata["plan_ref"] not in maps["plans"]:
            raise ValueError("Missing managed memory plan")
        runs = metadata.get("check_run_ids", [])
        if not isinstance(runs, list) or not all(isinstance(i, str) and i in maps["check_runs"]
                and maps["check_runs"][i].get("task_id") == row["id"] for i in runs):
            raise ValueError("Missing memory check evidence")

    def plan(row):
        if not isinstance(row, dict) or row.get("project_id") != project_id:
            raise ValueError("Invalid memory plan")
        _version(row.get("version"))
        if (not isinstance(row.get("title"), str) or not row["title"].strip()
                or not isinstance(row.get("goal"), str) or not isinstance(row.get("constraints"), list)
                or not all(isinstance(c, str) for c in row["constraints"])):
            raise ValueError("Invalid memory plan definition")
        source_refs(row.get("source_ids"))
        steps = row.get("step_ids")
        if not isinstance(steps, list) or len(steps) != len(set(steps)) or not all(
                isinstance(i, str) and i in maps["records"] and maps["records"][i].get("kind") == "task"
                for i in steps):
            raise ValueError("Missing managed memory plan step")

    for source in maps["sources"].values():
        content = source.get("content")
        if (not isinstance(content, str) or not isinstance(source.get("metadata"), dict)
                or type(source.get("is_current")) is not bool
                or hashlib.sha256(content.encode()).hexdigest() != source.get("sha256")):
            raise ValueError("Corrupt memory source snapshot")
        _text(source.get("uri"), "source URI")
        _text(source.get("kind"), "source kind")
    current_uris = [source["uri"] for source in maps["sources"].values() if source["is_current"]]
    if len(current_uris) != len(set(current_uris)):
        raise ValueError("Memory contains competing current snapshots for one source URI")
    for row in maps["records"].values():
        record(row)
    tasks = {i: r for i, r in maps["records"].items() if r["kind"] == "task"}
    remaining = {i: len(set(r["metadata"].get("depends_on", []))) for i, r in tasks.items()}
    dependents = {i: [] for i in tasks}
    for identifier, row in tasks.items():
        for dependency in set(row["metadata"].get("depends_on", [])):
            dependents[dependency].append(identifier)
    ready = [i for i, count in remaining.items() if count == 0]
    visited = 0
    while ready:
        identifier = ready.pop()
        visited += 1
        for dependent in dependents[identifier]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)
    if visited != len(tasks):
        raise ValueError("Memory task dependencies contain a cycle")
    for row in maps["plans"].values():
        plan(row)
    for table, owner, validator in (("record_revisions", "records", record), ("plan_revisions", "plans", plan)):
        revisions = {}
        for row in maps[table].values():
            key = "record_id" if table == "record_revisions" else "plan_id"
            identifier = row.get(key)
            _version(row.get("version"))
            if (identifier not in maps[owner] or row["id"] != f"{identifier}:{row['version']}"
                    or row["version"] > maps[owner][identifier]["version"]):
                raise ValueError("Invalid memory revision identity")
            snapshot = row.get("snapshot")
            if table == "plan_revisions":
                if not isinstance(snapshot, dict) or not isinstance(snapshot.get("tasks"), list):
                    raise ValueError("Invalid memory plan revision")
                for task in snapshot["tasks"]:
                    record(task)
                    task_revision = maps["record_revisions"].get(f"{task['id']}:{task['version']}")
                    if not task_revision or task_revision.get("snapshot") != task:
                        raise ValueError("Plan revision task does not match its immutable record revision")
                if [task["id"] for task in snapshot["tasks"]] != snapshot.get("plan", {}).get("step_ids"):
                    raise ValueError("Plan revision tasks do not match managed step order")
                snapshot = snapshot.get("plan")
            validator(snapshot)
            if snapshot.get("id") != identifier or snapshot.get("version") != row["version"]:
                raise ValueError("Memory revision does not match its owner")
            revisions.setdefault(identifier, {})[row["version"]] = snapshot
        for identifier, current in maps[owner].items():
            versions = revisions.get(identifier, {})
            if (len(versions) != current["version"] or not versions or min(versions) != 1
                    or max(versions) != current["version"] or versions[current["version"]] != current):
                raise ValueError("Incomplete or inconsistent immutable memory revision history")
    for node in maps["nodes"].values():
        _text(node.get("kind"), "node kind")
        if not isinstance(node.get("label"), str) or not isinstance(node.get("metadata", {}), dict):
            raise ValueError("Invalid logical memory graph node")
        if node.get("source_id"):
            source_refs([node["source_id"]])
    for edge in maps["edges"].values():
        if any(not isinstance(edge.get(k), str) or edge[k] not in maps["nodes"] for k in ("from_id", "to_id")):
            raise ValueError("Missing logical memory graph endpoint")
        _text(edge.get("kind"), "edge kind")
        if edge.get("source_id"):
            source_refs([edge["source_id"]])
    expected, owned_kinds = set(), {}
    for row in maps["records"].values():
        identifier = f"record:{row['id']}"
        node = maps["nodes"].get(identifier)
        if (not node or node["kind"] != row["kind"] or node["label"] != row["content"][:1000]
                or node.get("source_id") != row["source_ids"][0]):
            raise ValueError("Canonical memory record graph does not match its current head")
        owned_kinds[identifier] = {"supported_by", "depends_on", "supersedes"}
        expected.update((identifier, "supported_by", f"source:{source}", source) for source in row["source_ids"])
        dependencies = row["metadata"].get("depends_on", [])
        if isinstance(dependencies, (list, str)):
            expected.update((identifier, "depends_on", f"record:{dependency}", row["source_ids"][0])
                            for dependency in dependencies if f"record:{dependency}" in maps["nodes"])
        if row.get("supersedes"):
            expected.add((identifier, "supersedes", f"record:{row['supersedes']}", row["source_ids"][0]))
    for row in maps["plans"].values():
        identifier, sources = f"plan:{row['id']}", row["source_ids"]
        node = maps["nodes"].get(identifier)
        if (not node or node["kind"] != "plan" or node["label"] != row["title"]
                or node.get("source_id") != (sources[-1] if sources else None)):
            raise ValueError("Canonical memory plan graph does not match its current head")
        owned_kinds[identifier] = {"supported_by", "has_step"}
        expected.update((identifier, "supported_by", f"source:{source}", source) for source in sources)
        expected.update((identifier, "has_step", f"record:{step}", sources[-1] if sources else None) for step in row["step_ids"])
    actual = [(edge["from_id"], edge["kind"], edge["to_id"], edge.get("source_id"))
              for edge in maps["edges"].values() if edge["kind"] in owned_kinds.get(edge["from_id"], set())]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError("Canonical memory relationships do not match current records and plans")
    for run in maps["check_runs"].values():
        task = maps["records"].get(run.get("task_id"))
        if not task or task["kind"] != "task" or not isinstance(run.get("source_ids"), dict):
            raise ValueError("Invalid historical memory check")
        source_refs([run.get("profile_source_id"), *run["source_ids"].values()])
    if active_plan_id is not None and active_plan_id not in maps["plans"]:
        raise ValueError("Missing active memory plan")
    return maps


def _load_snapshot_impl(path):
    path = Path(path).expanduser().absolute()
    if path.is_dir():
        path /= "manifest.json"
    if any(parent.is_symlink() for parent in [path, *path.parents]):
        raise ValueError("Memory package paths cannot traverse symbolic links")
    # Snapshot paths are exact files, never unpacked archive paths.
    raw = _read(path, MAX_MANIFEST_BYTES)
    manifest = _parse(raw)
    if (not isinstance(manifest, dict) or manifest.get("format") not in FORMATS
            or type(manifest.get("format_version")) is not int or manifest["format_version"] != VERSION):
        raise ValueError("Unsupported portable memory format")
    project = manifest.get("project")
    if not isinstance(project, dict):
        raise ValueError("Missing memory project identity")
    try:
        if str(UUID(project.get("id"))) != project["id"]:
            raise ValueError("Noncanonical project UUID")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid memory project identity") from exc
    _text(project.get("name"), "project name")
    _text(manifest.get("origin_root"), "origin root")
    if not Path(manifest["origin_root"]).is_absolute():
        raise ValueError("Memory origin root must be absolute")
    aliases = manifest.get("root_aliases", [])
    if not isinstance(aliases, list) or not all(isinstance(r, str) and Path(r).is_absolute() for r in aliases):
        raise ValueError("Invalid memory root bindings")
    ancestors = manifest.get("ancestors", [])
    if (not isinstance(ancestors, list) or len(ancestors) > 4096
            or not all(isinstance(i, str) and _SHA.fullmatch(i) for i in ancestors)):
        raise ValueError("Invalid memory ancestry")
    parent = manifest.get("parent_id")
    if parent is not None and (not isinstance(parent, str) or not _SHA.fullmatch(parent)
                               or not ancestors or ancestors[0] != parent):
        raise ValueError("Invalid memory parent snapshot")
    if manifest.get("snapshot_id") != _manifest_id(manifest):
        raise ValueError("Memory manifest checksum mismatch")
    if len(ancestors) != len(set(ancestors)) or manifest["snapshot_id"] in ancestors:
        raise ValueError("Memory snapshot ancestry contains a cycle")
    objects = manifest.get("objects")
    if not isinstance(objects, dict) or set(objects) != set(TABLES):
        raise ValueError("Invalid memory object collections")
    base = path.parent if path.parent.name != "manifests" else path.parent.parent
    payload, total, count = {}, len(raw), 0
    for table in TABLES:
        descriptors = objects[table]
        if not isinstance(descriptors, list):
            raise ValueError("Invalid memory object list")
        payload[table] = []
        for descriptor in descriptors:
            count += 1
            if count > MAX_ROWS or not isinstance(descriptor, dict):
                raise ValueError("Memory object count exceeds budget")
            digest = descriptor.get("sha256")
            if (not isinstance(digest, str) or not _SHA.fullmatch(digest)
                    or descriptor.get("path") != f"objects/{digest}.json"
                    or type(descriptor.get("bytes")) is not int or not 0 <= descriptor["bytes"] <= MAX_OBJECT_BYTES):
                raise ValueError("Invalid memory content-addressed object")
            object_path = base / "objects" / f"{digest}.json"
            if (base / "objects").is_symlink() or object_path.resolve().parent != (base / "objects").resolve():
                raise ValueError("Memory object path escapes its package")
            data = _read(object_path, MAX_OBJECT_BYTES)
            total += len(data)
            if (total > MAX_TOTAL_BYTES or len(data) != descriptor["bytes"]
                    or hashlib.sha256(data).hexdigest() != digest):
                raise ValueError("Memory object checksum or budget mismatch")
            row = _parse(data)
            if not isinstance(row, dict) or row.get("id") != descriptor.get("id"):
                raise ValueError("Memory object identity mismatch")
            payload[table].append(row)
    _validate(payload, project["id"], manifest.get("active_plan_id"))
    if manifest.get("state_hash") != _state_hash(payload):
        raise ValueError("Memory portable-state checksum mismatch")
    if manifest.get("counts") != {table: len(payload[table]) for table in TABLES}:
        raise ValueError("Memory manifest counts do not match objects")
    return path, raw, manifest, payload


def _load_snapshot(path):
    try:
        return _load_snapshot_impl(path)
    except (OSError, KeyError, TypeError, AttributeError, RecursionError, OverflowError) as exc:
        raise ValueError("Invalid or unreadable portable memory package") from exc


def _summary(path, manifest, status="valid"):
    return {"status": status, "path": str(path), "manifest_path": str(path), "format_version": VERSION,
            **{key: deepcopy(manifest.get(key)) for key in
               ("project", "snapshot_id", "parent_id", "state_hash", "origin_root", "active_plan_id", "counts",
                "created_at", "processing", "completeness")}}


def inspect_snapshot(path):
    """Validate the complete package without constructing or mutating a Store."""
    path, _, manifest, _ = _load_snapshot(path)
    return _summary(path, manifest)


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Memory publication cannot replace symbolic links")
    fd, temporary = tempfile.mkstemp(prefix=".devbrain-memory-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class MemoryManager:
    def __init__(self, store):
        self.store = store

    def _scope(self, project_id, worktree=None, reader=None):
        reader = reader or self.store
        project = _one(reader, "SELECT FROM projects WHERE id=:id", {"id": project_id})
        root = Path(worktree if worktree is not None else project["root"]).expanduser().resolve()
        if not reader.query("SELECT id FROM project_roots WHERE project_id=:project AND path=:path",
                            {"project": project_id, "path": str(root)}):
            raise ValueError("Memory worktree must be a registered project root")
        return project, root

    @staticmethod
    def _path(root):
        return root / MEMORY_RELATIVE / "manifest.json"

    @staticmethod
    def _earlier_package(root):
        """What a reader should know about a package left in the earlier directory, or None."""
        earlier, current = (root / directory / "manifest.json" for directory in (EARLIER_MEMORY_RELATIVE, MEMORY_RELATIVE))
        if not (earlier.exists() or earlier.is_symlink()):
            return None
        note = {"path": str(earlier.parent), "state": "only_copy" if not current.exists() else "superseded"}
        if current.exists():
            try:
                old, new = _parse(_read(earlier, MAX_MANIFEST_BYTES)), _parse(_read(current, MAX_MANIFEST_BYTES))
                if old.get("snapshot_id") not in [new.get("snapshot_id"), *new.get("ancestors", [])]:
                    # Someone still publishes there with an earlier version; that snapshot is not in this history.
                    note["state"] = "diverged"
            except (OSError, ValueError, AttributeError, TypeError):
                note["state"] = "unreadable"
        return note

    @contextmanager
    def _publisher_lock(self, project_id, worktree=None):
        _, root = self._scope(project_id, worktree)
        folder = self._path(root).parent
        if not root.is_dir() or any(p.is_symlink() for p in (root / ".memory", folder)):
            raise ValueError("Memory publication needs a real project directory")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(folder / ".publish.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _capture(self, project_id, worktree=None, reader=None, authorize=None):
        if reader is None:
            with self.store.transaction() as tx:
                return self._capture(project_id, worktree, tx, authorize)
        if authorize is not None:
            authorize()
        project, root = self._scope(project_id, worktree, reader)
        payload = {table: sorted([_logical(table, r) for r in reader.query(
            f"SELECT FROM {table} WHERE project_id=:project", {"project": project_id})], key=lambda r: r["id"])
                   for table in TABLES}
        focus = _one(reader, "SELECT active_plan_id FROM worktree_states WHERE project_id=:project AND worktree=:root",
                     {"project": project_id, "root": str(root)}, False)
        active = focus.get("active_plan_id") if focus else None
        _validate(payload, project_id, active)
        aliases = [str(root)] + [r["origin_root"] for r in reader.query(
            "SELECT origin_root FROM memory_bindings WHERE project_id=:project AND local_root=:root",
            {"project": project_id, "root": str(root)})]
        jobs = reader.query("SELECT kind,state FROM jobs WHERE project_id=:project", {"project": project_id})
        processing = {kind: {state: sum(j.get("kind") == kind and j.get("state") == state for j in jobs)
                             for state in ("pending", "running", "failed", "completed")}
                      for kind in ("extract", "embed", "import")}
        return {"project": {"id": project_id, "name": project["name"]}, "origin_root": str(root),
                "root_aliases": sorted(set(aliases)), "active_plan_id": active,
                "counts": {table: len(payload[table]) for table in TABLES},
                "state_hash": _state_hash(payload), "processing": processing,
                "completeness": {"meaning": "committed_rows_only", "models_required": False,
                    "pending_analysis": processing["extract"]["pending"] + processing["extract"]["running"],
                    "failed_analysis": processing["extract"]["failed"]}}, payload

    def preview(self, project_id, worktree=None):
        captured, payload = self._capture(project_id, worktree)
        return {"status": "preview", **captured, "path": str(self._path(Path(captured["origin_root"]))),
                "worktree": captured["origin_root"], "bytes": sum(len(_bytes(r)) for rows in payload.values() for r in rows),
                "contains_raw_sources": bool(payload["sources"]), "model_calls": 0}

    def _known_manifest(self, project_id, path):
        return _one(self.store, "SELECT FROM memory_snapshots WHERE project_id=:project AND path=:path ORDER BY created_at DESC,id DESC LIMIT 1",
                    {"project": project_id, "path": str(path)}, False)

    def _receipt(self, tx, project_id, path, raw, manifest, origin, worktree=None):
        self.store._require_project(tx, project_id)
        row = {"id": _digest([project_id, str(path), manifest["snapshot_id"]]), "project_id": project_id,
               "snapshot_id": manifest["snapshot_id"], "parent_id": manifest.get("parent_id"),
               "state_hash": manifest["state_hash"], "ancestors": manifest.get("ancestors", []),
               "worktree": str(worktree if worktree is not None else manifest["origin_root"]),
               "active_plan_id": manifest.get("active_plan_id"),
               "path": str(path), "manifest_sha": hashlib.sha256(raw).hexdigest(),
               "created_at": _now(), "origin": origin}
        if not tx.query("SELECT id FROM memory_snapshots WHERE id=:id", {"id": row["id"]}):
            _insert(tx, "memory_snapshots", row)

    def snapshot(self, project_id, worktree=None, *, authorize=None):
        with self._publisher_lock(project_id, worktree):
            captured, payload = self._capture(project_id, worktree, authorize=authorize)
            root, path = Path(captured["origin_root"]), self._path(Path(captured["origin_root"]))
            if not root.is_dir() or any(p.is_symlink() for p in
                    (root / ".memory", path.parent, path.parent / "objects", path.parent / "manifests")):
                raise ValueError("Memory publication needs a real project directory")
            previous, previous_raw = None, None
            known = self._known_manifest(project_id, path)
            if path.exists() or path.is_symlink():
                _, previous_raw, previous, _ = _load_snapshot(path)
                if (not known or known.get("manifest_sha") != hashlib.sha256(previous_raw).hexdigest()
                        or previous["project"]["id"] != project_id):
                    raise MemoryConflict("Memory manifest changed externally; inspect/import it before publishing")
            elif known:
                raise MemoryConflict("Previously published memory manifest is missing")
            # Nothing was published in this directory yet. A package left in the earlier one gets the
            # same scrutiny as a manifest here would: it must be the one this store published or accepted.
            earlier, earlier_path = None, root / EARLIER_MEMORY_RELATIVE / "manifest.json"
            if previous is None and (earlier_path.exists() or earlier_path.is_symlink()):
                _, earlier_raw, earlier, _ = _load_snapshot(earlier_path)
                receipt = self._known_manifest(project_id, earlier_path)
                if (not receipt or receipt.get("manifest_sha") != hashlib.sha256(earlier_raw).hexdigest()
                        or earlier["project"]["id"] != project_id):
                    raise MemoryConflict(f"The memory package in {EARLIER_MEMORY_RELATIVE} was not published or imported "
                                         "by this store; inspect/import it before publishing")
            objects = {}
            for table, rows in payload.items():
                objects[table] = []
                for row in rows:
                    data, digest = _bytes(row), _digest(row)
                    if len(data) > MAX_OBJECT_BYTES:
                        raise ValueError("Memory object exceeds its byte budget")
                    objects[table].append({"id": row["id"], "sha256": digest,
                                           "path": f"objects/{digest}.json", "bytes": len(data)})
            content_hash = _digest({**{k: v for k, v in captured.items() if k not in {"processing", "completeness"}},
                                    "objects": objects})
            tip = _one(self.store, "SELECT FROM memory_snapshots WHERE project_id=:project ORDER BY created_at DESC,id DESC LIMIT 1",
                       {"project": project_id}, False)
            if previous and tip and tip["snapshot_id"] != previous["snapshot_id"]:
                if previous["snapshot_id"] not in tip.get("ancestors", []):
                    raise MemoryConflict("Published and accepted memory snapshot histories diverged")
            if (previous and previous.get("content_hash") == content_hash
                    and (not tip or tip["snapshot_id"] == previous["snapshot_id"])):
                return _summary(path, previous, "unchanged")
            if (earlier and earlier.get("content_hash") == content_hash
                    and (not tip or tip["snapshot_id"] == earlier["snapshot_id"])):
                # What is in the earlier directory is already this state: no second copy is written.
                return _summary(earlier_path, earlier, "unchanged")
            parent = tip["snapshot_id"] if tip else previous["snapshot_id"] if previous else None
            ancestors = ([parent] + (tip.get("ancestors", []) if tip else previous.get("ancestors", []) if previous else [])) if parent else []
            manifest = {"format": FORMAT, "format_version": VERSION, **captured, "objects": objects,
                        "created_at": _now(), "parent_id": parent, "ancestors": list(dict.fromkeys(ancestors))[:4096],
                        "content_hash": content_hash}
            manifest["snapshot_id"] = _manifest_id(manifest)
            raw = _bytes(manifest)
            if (len(raw) > MAX_MANIFEST_BYTES or sum(len(rows) for rows in objects.values()) > MAX_ROWS
                    or len(raw) + sum(d["bytes"] for rows in objects.values() for d in rows) > MAX_TOTAL_BYTES):
                raise ValueError("Memory package exceeds its publication byte or row budget")
            for table, rows in payload.items():
                for descriptor, row in zip(objects[table], rows, strict=True):
                    target = path.parent / descriptor["path"]
                    data = _bytes(row)
                    if target.exists():
                        if _read(target, MAX_OBJECT_BYTES) != data:
                            raise MemoryConflict("Existing memory object changed externally")
                    else:
                        _atomic_write(target, data)
            _atomic_write(path.parent / "manifests" / f"{manifest['snapshot_id']}.json", raw)
            # Compare immediately before the only mutable pointer replacement.
            if ((previous_raw is None and path.exists()) or (previous_raw is not None and _read(path, MAX_MANIFEST_BYTES) != previous_raw)):
                raise MemoryConflict("Memory manifest changed during publication")
            _atomic_write(path, raw)
            with self.store.transaction() as tx:
                self._receipt(tx, project_id, path, raw, manifest, "local")
            self._retain(path, manifest)
            return _summary(path, manifest, "created")

    def _retain(self, path, latest):
        wanted = [latest["snapshot_id"], *latest.get("ancestors", [])]
        referenced, kept = set(), set()
        for identifier in wanted:
            if len(kept) >= HISTORY_LIMIT:
                break
            manifest_path = path.parent / "manifests" / f"{identifier}.json"
            if not manifest_path.exists():
                continue
            _, _, manifest, _ = _load_snapshot(manifest_path)
            kept.add(identifier)
            referenced.update(d["sha256"] for rows in manifest["objects"].values() for d in rows)
        # Only owned, content-addressed files are collected after successful publication.
        for directory, allowed in (("manifests", kept), ("objects", referenced)):
            for entry in (path.parent / directory).glob("*.json"):
                if not entry.is_symlink() and _SHA.fullmatch(entry.stem) and entry.stem not in allowed:
                    entry.unlink()

    def import_snapshot(self, path, root, name=None, dry_run=False, *, expected_snapshot_id=None, expected_project_id=None,
                        authorize=None):
        path, raw, manifest, payload = _load_snapshot(path)
        project_id, snapshot_id = manifest["project"]["id"], manifest["snapshot_id"]
        if ((expected_snapshot_id is not None and expected_snapshot_id != snapshot_id)
                or (expected_project_id is not None and expected_project_id != project_id)):
            raise MemoryConflict("Memory package identity changed after inspection")
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("Memory import target must be an existing directory")
        self.store.require_safe_project_root(root)
        chosen_name = manifest["project"]["name"] if name is None else _text(name, "project name").strip()
        result = {"status": "dry_run" if dry_run else "imported", "project": {"id": project_id, "name": chosen_name, "root": str(root)},
                  "snapshot_id": snapshot_id, "counts": manifest["counts"], "active_plan_id": manifest.get("active_plan_id"),
                  "verification": "historical", "model_calls": 0}
        with self.store.transaction() as tx:
            if authorize is not None:
                authorize()
            current = _one(tx, "SELECT FROM projects WHERE id=:id", {"id": project_id}, False)
            conflicts = tx.query("SELECT id FROM projects WHERE (name=:name OR root=:root) AND id!=:id",
                                 {"name": chosen_name, "root": str(root), "id": project_id})
            roots = tx.query("SELECT project_id FROM project_roots WHERE path=:path", {"path": str(root)})
            if conflicts or any(r["project_id"] != project_id for r in roots):
                raise MemoryConflict("Memory target name or root belongs to another project identity")
            prior = _one(tx, "SELECT FROM memory_imports WHERE project_id=:project ORDER BY imported_at DESC,id DESC LIMIT 1",
                         {"project": project_id}, False)
            if prior and prior["snapshot_id"] == snapshot_id:
                if not roots:
                    raise MemoryConflict("An already imported snapshot is bound to a different local root")
                return {**result, "status": "unchanged", "project": current or result["project"]}
            if current:
                captured, existing_payload = self._capture(project_id, current["root"], tx)
                published = _one(tx, "SELECT FROM memory_snapshots WHERE project_id=:project ORDER BY created_at DESC,id DESC LIMIT 1",
                                 {"project": project_id}, False)
                baseline = published or prior
                exact_current = existing_payload == payload
                if ((baseline and (baseline["snapshot_id"] not in manifest.get("ancestors", [])
                                   or (captured["state_hash"] != baseline["state_hash"] and not exact_current)))
                        or (not baseline and not exact_current)):
                    raise MemoryConflict("Memory histories diverged or local knowledge changed")
                focus = _one(tx, "SELECT active_plan_id FROM worktree_states WHERE project_id=:project AND worktree=:root",
                             {"project": project_id, "root": str(root)}, False)
                selected = focus.get("active_plan_id") if focus else None
                focus_baseline = _one(tx, "SELECT active_plan_id FROM memory_snapshots WHERE project_id=:project "
                                     "AND worktree=:root ORDER BY created_at DESC,id DESC LIMIT 1",
                                     {"project": project_id, "root": str(root)}, False)
                previous_focus = focus_baseline.get("active_plan_id") if focus_baseline else None
                if selected != previous_focus and selected != manifest.get("active_plan_id"):
                    raise MemoryConflict("The local worktree's active plan changed since its memory baseline")
            else:
                existing_payload = {table: [] for table in TABLES}
            existing = {table: {r["id"]: r for r in rows} for table, rows in existing_payload.items()}
            for table in TABLES:
                identifiers = [row["id"] for row in payload[table]]
                for start in range(0, len(identifiers), 1000):
                    if any(row.get("project_id") != project_id for row in tx.query(
                            f"SELECT project_id FROM {table} WHERE id IN :ids", {"ids": identifiers[start:start + 1000]})):
                        raise MemoryConflict("A memory entity ID belongs to another local project")
            for table in ("record_revisions", "plan_revisions", "check_runs"):
                for row in payload[table]:
                    if row["id"] in existing[table] and _logical(table, existing[table][row["id"]]) != row:
                        raise MemoryConflict("An immutable memory revision or check conflicts locally")
            for table in ("records", "record_revisions", "plans", "plan_revisions"):
                incoming = {row["id"]: row for row in payload[table]}
                if any(identifier not in incoming for identifier in existing[table]):
                    raise MemoryConflict("Incoming memory omits existing immutable knowledge history")
                if table in {"records", "plans"} and any(
                        incoming[identifier]["version"] < row["version"]
                        or (incoming[identifier]["version"] == row["version"] and incoming[identifier] != row)
                        for identifier, row in existing[table].items()):
                    raise MemoryConflict("Incoming memory regresses an existing knowledge head")
            for row in payload["sources"]:
                old = existing["sources"].get(row["id"])
                if old and any(old.get(k) != row.get(k) for k in ("project_id", "uri", "content", "sha256")):
                    raise MemoryConflict("An immutable memory source conflicts locally")
            if current and any(r["id"] not in {i["id"] for i in payload["records"]} for r in existing_payload["records"]):
                raise MemoryConflict("Incoming memory omits existing knowledge records")
            origins = sorted(set([manifest["origin_root"], *manifest.get("root_aliases", [])]))
            for origin in origins:
                if any(r["local_root"] != str(root) for r in tx.query(
                        "SELECT local_root FROM memory_bindings WHERE project_id=:project AND origin_root=:origin",
                        {"project": project_id, "origin": origin})):
                    raise MemoryConflict("Memory origin already has a different local root binding")
            if dry_run:
                return result
            from .journal import Journal
            Journal.reset(tx, reason="memory_import")
            if not current:
                _insert(tx, "projects", {**result["project"], "created_at": _now()})
            if not roots:
                _insert(tx, "project_roots", {"id": str(uuid4()), "project_id": project_id, "path": str(root)})
            for origin in origins:
                binding = {"id": _digest([project_id, origin, str(root)]), "project_id": project_id,
                           "origin_root": origin, "local_root": str(root)}
                if not tx.query("SELECT id FROM memory_bindings WHERE id=:id", {"id": binding["id"]}):
                    _insert(tx, "memory_bindings", binding)
            # Clear only affected derived unique keys before applying a complete
            # final record set; legitimate key swaps must not depend on row order.
            for row in payload["records"]:
                if row["id"] in existing["records"]:
                    _set(tx, "records", {"active_key": None}, "id=:id", {"id": row["id"]})
            for row in payload["sources"]:
                if (row["id"] in existing["sources"] and not (row["kind"] in {"code", "document", "file"}
                        and isinstance(row["metadata"].get("path"), str))):
                    _set(tx, "sources", {"current_key": None}, "id=:id", {"id": row["id"]})
            for table in TABLES:
                for original in payload[table]:
                    row, old = deepcopy(original), existing[table].get(original["id"])
                    if table == "sources":
                        if old:
                            if not (row["kind"] in {"code", "document", "file"}
                                    and isinstance(row["metadata"].get("path"), str)):
                                _set(tx, "sources", {"is_current": row["is_current"],
                                    "current_key": _json([project_id, row["uri"]]) if row["is_current"] else None},
                                     "id=:id", {"id": row["id"]})
                                _set(tx, "chunks", {"is_current": row["is_current"]}, "source_id=:id", {"id": row["id"]})
                            continue
                        foreign_file = row["kind"] in {"code", "document", "file"} and isinstance(row["metadata"].get("path"), str)
                        current_source = row["is_current"] and not foreign_file
                        row.update(is_current=current_source,
                                   current_key=_json([project_id, row["uri"]]) if current_source else None)
                    elif table == "records":
                        row["active_key"] = _json([project_id, row["kind"], row["key"]]) if row["status"] != "superseded" else None
                    elif table == "check_runs":
                        if old:
                            continue  # A genuine local matching run retains its local authority.
                        row["portable_provenance"] = {"snapshot_id": snapshot_id, "original_origin": row.get("origin", "unknown"),
                                                      "origin_root": manifest["origin_root"]}
                        row["origin"] = "portable_import"
                    elif table == "nodes":
                        if old and row["kind"] not in _GLOBAL_NODES:
                            continue
                        row["is_current"] = row["kind"] in _GLOBAL_NODES
                        self.store.upsert_node(row, tx)
                        continue
                    elif table == "edges":
                        if old and row["kind"] in _CODE_EDGES:
                            continue
                        row["is_current"] = row["kind"] not in _CODE_EDGES
                        self.store.upsert_edge(row, tx)
                        continue
                    if old:
                        if table in {"record_revisions", "plan_revisions"}:
                            continue
                        _set(tx, table, row, "id=:id", {"id": row["id"]})
                    else:
                        _insert(tx, table, row)
            # Canonical relationships describe the final heads, not a union of
            # old and new dependency/plan membership projections.
            from .plans import _plan_graph
            records = {r["id"]: r for r in payload["records"]}
            for record in payload["records"]:
                self.store._record_graph(tx, record)
            for plan in payload["plans"]:
                _plan_graph(self.store, tx, plan, [records[i] for i in plan["step_ids"]])
            # Search is usable without GPU; chunks are derived locally, never imported claims.
            from .ingest import _chunks, _identifier
            from .agent import source_text
            for source in payload["sources"]:
                if tx.query("SELECT id FROM chunks WHERE source_id=:source LIMIT 1", {"source": source["id"]}):
                    continue
                saved = _one(tx, "SELECT is_current FROM sources WHERE id=:id", {"id": source["id"]})
                for number, (text, first, last) in enumerate(_chunks(source_text(source))):
                    _insert(tx, "chunks", {"id": _identifier(project_id, source["id"], number, first, last, text),
                        "project_id": project_id, "source_id": source["id"], "text": text,
                        "line_start": first, "line_end": last, "is_current": saved["is_current"],
                        "metadata": {"source_kind": source["kind"], "role": source["metadata"].get("role", "unknown")}})
            active = manifest.get("active_plan_id")
            if active:
                from .plans import PlanManager
                PlanManager(self.store).activate(project_id, active, str(root))
            else:
                focus = _one(tx, "SELECT FROM worktree_states WHERE project_id=:project AND worktree=:root",
                             {"project": project_id, "root": str(root)}, False)
                if focus and focus.get("active_plan_id") is not None:
                    _set(tx, "worktree_states", {"active_plan_id": None, "version": focus["version"] + 1,
                         "updated_at": _now()}, "id=:id", {"id": focus["id"]})
            receipt = {"id": _digest([project_id, snapshot_id]), "project_id": project_id, "snapshot_id": snapshot_id,
                       "state_hash": manifest["state_hash"], "ancestors": manifest.get("ancestors", []),
                       "origin_root": manifest["origin_root"], "local_root": str(root),
                       "source_ids": [s["id"] for s in payload["sources"] if s["id"] not in existing["sources"]],
                       "imported_at": _now()}
            _insert(tx, "memory_imports", receipt)
            self._receipt(tx, project_id, path, raw, manifest, "import", str(root))
        return result

    def configure_auto(self, project_id, enabled, interval_minutes=15, worktree=None):
        if type(enabled) is not bool or type(interval_minutes) is not int or not 1 <= interval_minutes <= 10080:
            raise ValueError("Memory schedule requires a boolean and interval from 1 to 10080 minutes")
        _, root = self._scope(project_id, worktree)
        with self.store.transaction() as tx:
            row = {"id": project_id, "project_id": project_id, "enabled": enabled,
                   "interval_minutes": interval_minutes, "worktree": str(root), "updated_at": _now()}
            if tx.query("SELECT id FROM memory_settings WHERE id=:id", {"id": project_id}):
                _set(tx, "memory_settings", row, "id=:id", {"id": project_id})
            else:
                _insert(tx, "memory_settings", row)
        return {"status": "configured", **self.status(project_id, str(root))}

    def status(self, project_id, worktree=None):
        project, root = self._scope(project_id, worktree)
        # Shown: the package there is to read. Written next: always the current directory.
        found = snapshot_directory(root)
        path = found / "manifest.json" if found is not None else self._path(root)
        schedule = _one(self.store, "SELECT FROM memory_settings WHERE id=:id", {"id": project_id}, False) or {}
        result = {"project_id": project_id, "path": str(path), "write_path": str(self._path(root)),
                  "earlier_package": self._earlier_package(root), "latest": None,
                  "auto": {"enabled": False, "interval_minutes": 15, "worktree": str(root),
                           "last_checked_at": None, "last_success_at": None, "error": None}}
        result["auto"].update({k: v for k, v in schedule.items() if k in result["auto"]})
        jobs = self.store.query("SELECT kind,state FROM jobs WHERE project_id=:project", {"project": project_id})
        result["processing"] = {kind: {state: sum(r.get("kind") == kind and r.get("state") == state for r in jobs)
                                       for state in ("pending", "running", "failed", "completed")}
                                for kind in ("extract", "embed", "import")}
        def count(table):
            rows = self.store.query(f"SELECT count(*) AS total FROM {table} WHERE project_id=:project", {"project": project_id})
            return int(rows[0].get("total", 0)) if rows else 0
        result["rebuilding"] = {"code_index_pending": not bool(project.get("graph_fingerprint")),
                                "chunks": count("chunks"), "embedding_rows": count("embeddings"),
                                "model_required_to_read_memory": False}
        imported = _one(self.store, "SELECT snapshot_id,imported_at,origin_root,local_root FROM memory_imports "
                        "WHERE project_id=:project ORDER BY imported_at DESC,id DESC LIMIT 1", {"project": project_id}, False)
        result["last_import"] = imported
        result["verification"] = "Imported checks are historical; local execution and a new session ACK remain required."
        if path.exists() or path.is_symlink():
            try:
                result["latest"] = inspect_snapshot(path)
            except (OSError, ValueError):
                result["error"] = "invalid_snapshot"
        return result

    def tick(self, now=None):
        moment = datetime.now(timezone.utc).timestamp() if now is None else now.timestamp() if isinstance(now, datetime) else float(now)
        if not math.isfinite(moment):
            raise ValueError("Invalid memory schedule time")
        results, errors = [], []
        for row in self.store.query("SELECT FROM memory_settings WHERE enabled=true"):
            checked = row.get("last_checked_at")
            if checked and moment - datetime.fromisoformat(checked).timestamp() < row["interval_minutes"] * 60:
                continue
            timestamp = datetime.fromtimestamp(moment, timezone.utc).isoformat()
            try:
                result = self.snapshot(row["project_id"], row["worktree"])
                values = {"last_checked_at": timestamp, "last_success_at": timestamp, "error": None}
                results.append({"project_id": row["project_id"], **result})
            except (OSError, ValueError, RuntimeError) as exc:
                code = "memory_conflict" if isinstance(exc, MemoryConflict) else "snapshot_failed"
                values = {"last_checked_at": timestamp, "error": code}
                errors.append({"project_id": row["project_id"], "code": code})
            with self.store.transaction() as tx:
                _set(tx, "memory_settings", values, "id=:id", {"id": row["id"]})
        return {"status": "completed", "projects": results, "errors": errors}
