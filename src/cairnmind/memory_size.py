"""How close the portable package of a project is to its publication limits (#191).

`memory status` shows the package that a snapshot would write now against each limit it enforces: the rows
of the package, its bytes and the bytes of its manifest. From WARN_FRACTION of any one of them it says so,
before ``MemoryManager.snapshot`` refuses to publish. The refusal itself is not changed here.

What it costs. Nothing is exported and no row is read into this process:

- Rows are ``count(*)`` per table of the package, for one project (the project index).
- Bytes are ``sum(@this.toJSON().length())``, summed in the engine, so no text crosses to this process; the
  sources are summed in one GROUP BY pass by kind and by current or replaced version, which also gives the
  figures behind the open question of what belongs in a package (transcripts, replaced versions).
  The engine still reads every row of the project once, as a snapshot would, but keeps none; the memory used
  here is a few numbers per table, and the time grows with the size of the project, not with the package
  being built.
- The figure is an estimate, not the size a snapshot would write: it measures the stored form of a row, the
  package writes the logical form (some stored fields are left out) and a manifest line per row is added
  from MANIFEST_LINE bytes. It is marked as such. Rows are exact up to rows written meanwhile.
- A measurement is kept for CACHE_SECONDS per project, so that a panel that asks for the status often does
  not repeat the pass; ``fresh=True`` skips it.
"""
from __future__ import annotations

import time

WARN_FRACTION = 0.7
CACHE_SECONDS = 60
# One descriptor of the manifest: the row id, sha256 (64), path ("objects/<sha>.json") and byte count, with their keys.
MANIFEST_LINE = 240
MANIFEST_BASE = 4096
_cache: dict = {}


def _entry(used, limit):
    return {"used": used, "limit": limit, "fraction": round(used / limit, 4)}


def _measure(store, project_id, tables):
    from .store_size import _LENGTH, _Collection
    rows, size, sources = 0, 0, {"current": 0, "replaced": 0, "transcripts": 0}
    for table in tables:
        collection = _Collection(store, table, project_id)
        if table == "sources":
            for row in collection.query(f"kind, is_current, count(*) AS n, sum({_LENGTH}) AS b",
                                        tail=" GROUP BY kind, is_current"):
                n, b = int(row.get("n") or 0), int(row.get("b") or 0)
                rows, size = rows + n, size + b
                sources["current" if row.get("is_current") is True else "replaced"] += b
                if row.get("kind") == "agent_transcript":
                    sources["transcripts"] += b
            continue
        found = collection.query(f"count(*) AS n, sum({_LENGTH}) AS b")
        if found:
            rows, size = rows + int(found[0].get("n") or 0), size + int(found[0].get("b") or 0)
    return rows, size, sources


def package_size(store, project_id, tables, *, max_rows, max_bytes, max_manifest_bytes,
                 fresh=False, now=time.monotonic):
    """The package against its limits, with a warning from WARN_FRACTION of any of them; None when it cannot be measured."""
    key, moment = (id(store), project_id), now()
    limits = (max_rows, max_bytes, max_manifest_bytes)
    kept = _cache.get(key)
    if not fresh and kept and moment - kept[0] < CACHE_SECONDS and kept[1] == limits:
        return kept[2]
    try:
        rows, size, sources = _measure(store, project_id, tables)
    except (OSError, ValueError, RuntimeError):
        return None
    manifest = MANIFEST_BASE + rows * MANIFEST_LINE
    figures = {"rows": _entry(rows, max_rows), "bytes": _entry(size + manifest, max_bytes),
               "manifest_bytes": _entry(manifest, max_manifest_bytes)}
    near = [name for name, figure in figures.items() if figure["fraction"] >= WARN_FRACTION]
    result = {"measured": "estimate", "warn_at": WARN_FRACTION, **figures,
              "sources_bytes": sources, "warning": near or None}
    _cache[key] = (moment, limits, result)
    if len(_cache) > 64:
        _cache.pop(next(iter(_cache)))
    return result
