"""How large the store is (#184): rows and approximate bytes per collection, and disk use of the data directory.

Read only, through a store from ``Store.open_readonly``: nothing is started, created or written. No query
returns text, a vector or a whole row, and nothing from a record or a source is printed; the store's own
names (source kinds, namespaces, the project's name) are made safe for a terminal first.

What it costs, so that a large store is not slow to measure:

- Row counts are ``count(*)`` queries. Sources, chunks and jobs are counted per group (kind, current or
  replaced, state) in one GROUP BY pass each, which the engine runs without returning any text.
- Bytes are the length of each row's JSON form, measured in the engine (``@this.toJSON().length()``), so
  the text is never sent here. For a collection of at most EXACT_ROWS rows the lengths are summed exactly.
  Above that, SAMPLE_POINTS windows of SAMPLE_WINDOW rows are read from random points of the unique id
  index and the mean is multiplied by the exact count; such figures are marked ``sampled``.
- Chunks are grouped by the kind of their source, which needs one row per source: its id and kind
  (sources are paged by id) and its chunk count. These rows carry no text.
- Disk use is a walk over the data directory that reads file sizes only.
"""
from __future__ import annotations

import os
from pathlib import Path
import random
import time

from .brief import _line
from .config import BACKUP_SUFFIXES
from .terminal_safe import safe_text

EXACT_ROWS = 1000
SAMPLE_POINTS = 8
SAMPLE_WINDOW = 32
KIND_PAGE = 10000
# A fixed seed: the same store gives the same figures each time it is measured.
SEED = 184
STATES = ("current", "replaced")
_LENGTH = "@this.toJSON().length()"


def _quote(name: str) -> str:
    return f"`{name}`"


class _Collection:
    """One collection, optionally limited to a project: its count and the length of its rows."""

    def __init__(self, store, name, project_id=None, key="project_id"):
        self.store, self.name, self.project_id = store, name, project_id
        self.where = f"{key}=:project" if project_id else ""
        self.params = {"project": project_id} if project_id else {}

    def query(self, select, where="", params=None, tail=""):
        # The caller's condition (a range of ids) comes first: the engine then walks the id index.
        condition = " AND ".join(part for part in (where, self.where) if part)
        return self.store.query(f"SELECT {select} FROM {_quote(self.name)}{' WHERE ' if condition else ''}"
                                f"{condition}{tail}",
                                {**self.params, **(params or {})})

    def rows(self) -> int:
        found = self.query("count(*) AS n")
        return int(found[0].get("n") or 0) if found else 0

    def exact_bytes(self) -> int:
        found = self.query(f"sum({_LENGTH}) AS b")
        return int(found[0].get("b") or 0) if found else 0

    def sample(self, fields=""):
        """Rows of up to SAMPLE_POINTS windows from random points of the id index, each with its length."""
        generator, seen, sample = random.Random(SEED), set(), []
        for _ in range(SAMPLE_POINTS):
            start = f"{generator.getrandbits(32):08x}"
            for row in self.query(f"id, {fields}{_LENGTH} AS b", "id >= :origin", {"origin": start},
                                  f" ORDER BY id LIMIT {SAMPLE_WINDOW}"):
                if row.get("id") not in seen:
                    seen.add(row.get("id"))
                    sample.append(row)
        return sample


def _measure(collection, fields=""):
    """(rows, bytes, sampled, sample) of a collection; the sample is empty when the bytes are exact."""
    rows = collection.rows()
    if rows <= EXACT_ROWS:
        return rows, collection.exact_bytes() if rows else 0, False, []
    sample = collection.sample(fields)
    if not sample:
        return rows, None, True, []
    return rows, round(sum(int(row.get("b") or 0) for row in sample) / len(sample) * rows), True, sample


def _estimate(groups, sample, key, total_rows, total_bytes):
    """Bytes per group from the sampled rows of that group, else from the mean of the whole collection."""
    mean = (total_bytes / total_rows) if total_rows and total_bytes is not None else None
    sums, counts = {}, {}
    for row in sample:
        group = key(row)
        sums[group] = sums.get(group, 0) + int(row.get("b") or 0)
        counts[group] = counts.get(group, 0) + 1
    for group, value in groups.items():
        local = sums[group] / counts[group] if counts.get(group) else mean
        value["bytes"] = None if local is None else round(local * value["rows"])


def _state(value) -> str:
    return "current" if value is True else "replaced"


def _kinds(groups):
    """{(kind, state): {rows, bytes}} as a list of kinds, each with its current and replaced rows."""
    kinds = sorted({kind for kind, _ in groups}, key=lambda kind: (str(kind)))
    return [{"kind": kind, **{state: groups.get((kind, state), {"rows": 0, "bytes": 0}) for state in STATES}}
            for kind in kinds]


def _sources(store, project_id):
    collection = _Collection(store, "sources", project_id)
    rows, size, sampled, sample = _measure(collection, "kind, is_current, ")
    select = "kind, is_current, count(*) AS n" + ("" if sampled else f", sum({_LENGTH}) AS b")
    groups = {}
    for row in collection.query(select, tail=" GROUP BY kind, is_current"):
        groups[(row.get("kind"), _state(row.get("is_current")))] = {
            "rows": int(row.get("n") or 0), "bytes": int(row.get("b") or 0)}
    if sampled:
        _estimate(groups, sample, lambda row: (row.get("kind"), _state(row.get("is_current"))), rows, size)
    return (rows, size, sampled), {"sampled": sampled, "kinds": _kinds(groups)}


def _source_kinds(store, project_id):
    """The kind of every source of the store or project, read in pages by id; no text is read."""
    collection, kinds, after = _Collection(store, "sources", project_id), {}, ""
    while True:
        page = collection.query("id, kind", "id > :position", {"position": after}, f" ORDER BY id LIMIT {KIND_PAGE}")
        kinds.update((row.get("id"), row.get("kind")) for row in page)
        if len(page) < KIND_PAGE:
            return kinds
        after = page[-1].get("id")


def _chunks(store, project_id):
    collection = _Collection(store, "chunks", project_id)
    rows, size, sampled, sample = _measure(collection, "source_id, is_current, ")
    kinds = _source_kinds(store, project_id)
    select = "source_id, is_current, count(*) AS n" + ("" if sampled else f", sum({_LENGTH}) AS b")
    groups = {}
    for row in collection.query(select, tail=" GROUP BY source_id, is_current"):
        group = groups.setdefault((kinds.get(row.get("source_id")), _state(row.get("is_current"))),
                                  {"rows": 0, "bytes": 0})
        group["rows"] += int(row.get("n") or 0)
        group["bytes"] += int(row.get("b") or 0)
    if sampled:
        _estimate(groups, sample, lambda row: (kinds.get(row.get("source_id")), _state(row.get("is_current"))),
                  rows, size)
    return (rows, size, sampled), {"sampled": sampled, "kinds": _kinds(groups)}


def _vector_types(store, project_id):
    """Every vector_* type in the schema, with its registry entry (namespace, dimension, project) if any.

    For one project only the types registered to it; a type without a registry entry is shown for the store."""
    from .store import EXPORT_TYPES
    registry = {row.get("type_name"): row for row in store.query(
        "SELECT type_name, namespace, dimension, project_id FROM vector_indexes")}
    names = sorted(str(row.get("name")) for row in store.query("SELECT name FROM schema:types")
                   if str(row.get("name", "")).startswith("vector_") and row.get("name") not in EXPORT_TYPES)
    found = []
    for name in names:
        entry = registry.get(name) or {}
        if project_id and entry.get("project_id") != project_id:
            continue
        found.append({"name": name, "namespace": entry.get("namespace"), "dimension": entry.get("dimension"),
                      "project_id": entry.get("project_id")})
    return found


def _collections(store, project_id):
    from .store import EXPORT_TYPES
    measured = {}
    for name in EXPORT_TYPES:
        if name in ("sources", "chunks"):
            continue
        key = "id" if name == "projects" else "project_id"
        measured[name] = _measure(_Collection(store, name, project_id, key))[:3]
    return measured


def _tree(path: Path) -> dict:
    """The size of the files under a path (or of the file), without following links; sizes only."""
    total = files = 0
    if path.is_file() and not path.is_symlink():
        return {"path": str(path), "exists": True, "files": 1, "bytes": path.stat().st_size}
    if not path.is_dir() or path.is_symlink():
        return {"path": str(path), "exists": False, "files": 0, "bytes": 0}
    for root, directories, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue  # removed while walking
            files += 1
            total += info.st_size
    return {"path": str(path), "exists": True, "files": files, "bytes": total}


def _backups(directory: Path) -> dict:
    groups = {name: {"count": 0, "bytes": 0} for name in ("scheduled", "before_schema", "before_restore", "other")}
    if directory.is_dir() and not directory.is_symlink():
        for path in directory.iterdir():
            if not path.is_file() or path.is_symlink() or path.name.startswith("."):
                continue
            stem = next((path.name[:-len(suffix)] for suffix in BACKUP_SUFFIXES if path.name.endswith(suffix)), None)
            group = ("scheduled" if stem is not None and stem.isdigit() else
                     "before_schema" if path.name.startswith("before-schema-") else
                     "before_restore" if path.name.startswith("before-restore-") else "other")
            groups[group]["count"] += 1
            groups[group]["bytes"] += path.stat().st_size
    return {"path": str(directory), **groups}


def _disk(settings) -> dict:
    data = Path(settings.data_dir)
    agents = data / "runtime" / "agents"
    journals = _tree(agents)
    sessions = sum(1 for path in agents.iterdir() if path.is_dir() and not path.is_symlink()) \
        if journals["exists"] else 0
    journals = {"path": journals["path"], "exists": journals["exists"], "sessions": sessions,
                "files": journals["files"], "bytes": journals["bytes"]}
    return {"data_dir": _tree(data), "databases": _tree(data / "arcade" / "databases"),
            "database": _tree(data / "arcade" / "databases" / settings.arcade_database),
            "engine_log": _tree(data / "arcade" / "engine.log"), "engine_logs": _tree(data / "arcade" / "log"),
            "backups": _backups(data / "backups"), "agent_journals": journals}


def measure(store, project_id: str | None = None) -> dict:
    """The size report of the store, or of one project's rows; the disk part is always the whole directory."""
    started = time.monotonic()
    project = None
    if project_id:
        found = store.query("SELECT id, name FROM projects WHERE id=:id", {"id": project_id})
        if not found:
            raise ValueError(f"Unknown project: {project_id}")
        project = {"id": found[0]["id"], "name": found[0].get("name")}
    measured = _collections(store, project_id)
    measured["sources"], sources = _sources(store, project_id)
    measured["chunks"], chunks = _chunks(store, project_id)
    vectors = _vector_types(store, project_id)
    for vector in vectors:
        vector["rows"], vector["bytes"], vector["sampled"] = _measure(_Collection(store, vector["name"], project_id))[:3]
        measured[vector["name"]] = (vector["rows"], vector["bytes"], vector["sampled"])
    namespaces = {}
    for vector in vectors:
        entry = namespaces.setdefault(vector["namespace"], {"namespace": vector["namespace"], "types": 0, "rows": 0,
                                                            "bytes": 0, "sampled": False})
        entry["types"] += 1
        entry["rows"] += vector["rows"]
        entry["bytes"] = None if entry["bytes"] is None or vector["bytes"] is None else entry["bytes"] + vector["bytes"]
        entry["sampled"] = entry["sampled"] or vector["sampled"]
    jobs = {str(row.get("state")): int(row.get("n") or 0)
            for row in _Collection(store, "jobs", project_id).query("state, count(*) AS n", tail=" GROUP BY state")}
    from .store import EXPORT_TYPES
    order = [*EXPORT_TYPES, *(vector["name"] for vector in vectors)]
    report = {
        "project": project,
        "method": {"exact_rows": EXACT_ROWS, "sample_rows": SAMPLE_POINTS * SAMPLE_WINDOW,
                   "bytes": "length of each row's JSON form in the engine; equal to UTF-8 bytes for ASCII text"},
        "collections": [{"name": name, "rows": measured[name][0], "bytes": measured[name][1],
                         "sampled": measured[name][2]} for name in order],
        "sources": sources, "chunks": chunks,
        "vectors": {"types": vectors,
                    "namespaces": sorted(namespaces.values(), key=lambda row: str(row["namespace"]))},
        "jobs": jobs, "disk": _disk(store.settings),
    }
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["text"] = render(report)
    return report


def _size(value, sampled=False) -> str:
    if value is None:
        return "unknown"
    number, unit = float(value), "B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            break
        number /= 1024
    shown = f"{int(number)} B" if unit == "B" else f"{number:.1f} {unit}"
    return ("~" if sampled else "") + shown


def _name(value, limit=40) -> str:
    return "(none)" if value is None else _line(value, limit) or "(blank)"


def render(report) -> str:
    """The report for a person: counts and sizes only, with every name from the store made safe."""
    project = report["project"]
    lines = ["Store size" + (f" of project {_name(project['name'], 60)}" if project else " (all projects)"),
             f"Bytes are approximate ({report['method']['bytes']}); ~ marks a figure estimated from "
             f"{report['method']['sample_rows']} sampled rows.", "", "Collections"]
    width = max(len(row["name"]) for row in report["collections"])
    lines += [f"  {row['name']:<{width}}  {row['rows']:>10}  {_size(row['bytes'], row['sampled']):>12}"
              for row in report["collections"]]
    for title, part in (("Sources by kind", report["sources"]), ("Chunks by source kind", report["chunks"])):
        lines += ["", f"{title}: current / replaced"]
        lines += [f"  {_name(row['kind']):<20}  {row['current']['rows']:>8} "
                  f"{_size(row['current']['bytes'], part['sampled']):>11}  /  {row['replaced']['rows']:>8} "
                  f"{_size(row['replaced']['bytes'], part['sampled']):>11}" for row in part["kinds"]] or ["  none"]
    lines += ["", "Vectors by namespace"]
    lines += [f"  {_name(row['namespace'], 60)}: {row['types']} type(s), {row['rows']} rows, "
              f"{_size(row['bytes'], row['sampled'])}" for row in report["vectors"]["namespaces"]] or ["  none"]
    lines += ["", "Jobs by state"]
    lines += [f"  {_name(state)}: {count}" for state, count in sorted(report["jobs"].items())] or ["  none"]
    disk = report["disk"]
    lines += ["", "Disk" + (" (the whole data directory, not only this project)" if project else ""),
              f"  data directory   {_size(disk['data_dir']['bytes']):>12}  {safe_text(disk['data_dir']['path'])}"]
    for label, key in (("databases", "databases"), ("this database", "database"), ("engine.log", "engine_log"),
                       ("engine logs", "engine_logs")):
        part = disk[key]
        lines.append(f"  {label:<15}  {_size(part['bytes']) if part['exists'] else 'absent':>12}")
    backups = disk["backups"]
    for label, key in (("scheduled", "scheduled"), ("before-schema", "before_schema"),
                       ("before-restore", "before_restore"), ("other", "other")):
        lines.append(f"  backups {label:<15} {backups[key]['count']:>4} file(s) {_size(backups[key]['bytes']):>12}")
    journals = disk["agent_journals"]
    lines.append(f"  agent journals   {journals['sessions']:>4} session(s) {_size(journals['bytes']):>12}")
    lines.append(f"Measured in {report['elapsed_seconds']:.3f} s.")
    return "\n".join(lines)
