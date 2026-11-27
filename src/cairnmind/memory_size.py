"""How close the portable package of a project is to its publication limits (#191).

`memory status` shows the package that a snapshot would write now against each limit it enforces: the rows
of the package, its bytes, the bytes of its manifest and its largest single object. From WARN_FRACTION of
any one of them it says so, before ``MemoryManager.snapshot`` refuses to publish. The refusal itself is not
changed here. The code graph is not part of a package (#261): ``only`` limits the nodes and edges that are
counted to the ones a snapshot writes (the code nodes that records link to with relates_to are left out of the
count, so the figure is below the real one by as many as there are; it is an estimate).

What it costs. Nothing is exported and no row is read into this process:

- Rows are ``count(*)`` per table of the package, for one project (the project index).
- Characters are summed in the engine (the length of each row's JSON form), so no text crosses to this
  process. The sources are summed in one GROUP BY pass by kind and by current or replaced version, which also
  gives the figures behind the open question of what belongs in a package (transcripts, replaced versions).
- A snapshot counts UTF-8 bytes and the engine's ``length()`` counts characters, so for text beyond ASCII
  (Japanese, Cyrillic, emoji) characters alone say about half of the real size. The extra bytes are counted
  by the engine as well, by width: a character beyond ASCII adds 1 byte, one from U+0800 adds a second, one
  beyond the basic plane a third. Each of the three counts is one statement that removes every other
  character with a regular expression and sums what is left (``_WIDTHS``); the second and the third are
  skipped when the one before found nothing, so a store of plain text costs one such pass.
- The engine stops a statement whose regular expressions run longer than its ``regexTimeout`` (about one
  second, about 30 MB of text). A stopped statement is repeated over the rows of one id prefix at a time (16
  parts, and each of those in 16 again), each part with its own time. If a part is still stopped, the figure
  falls back to characters and says so: ``exact_bytes`` is False, the number is a lower bound and the
  terminal tells the person that the package can be larger. It is never presented as exact then.
- The largest row of the package is the ``max`` of the characters; only the rows that could be near the
  object limit (more than WARN_FRACTION of it, with 4 bytes to a character) are measured in bytes.
- The engine reads every row of the project once per statement, keeps none, and the memory used here is a few
  numbers per table. The time grows with the size of the project, not with the package being built: about
  0.15 s per MB for text with Japanese, Cyrillic or emoji (25 s for 168 MB) and 0.05 s per MB for plain
  ASCII (8 s for 168 MB), measured on a development machine in #191. A store near its 512 MB limit takes
  about 25 s of plain text and over a minute of such text.
- That is more than a request may take (the terminal waits 20 seconds for a status), so the measurement runs
  in a thread that ``package_size`` waits for WAIT_SECONDS. If it is not ready, the status carries the last
  figure with its age, or ``{"measured": "pending"}``, and goes on; the next status finds the result in the
  cache. One measurement per project runs at a time.
- The figure is an estimate, not the size a snapshot would write: it measures the stored form of a row, the
  package writes the logical form (some stored fields are left out) and a manifest line per row is added
  from MANIFEST_LINE bytes. It is marked as such. Rows are exact up to rows written meanwhile.
- A measurement is kept for CACHE_SECONDS per project, or for CACHE_FACTOR times as long as it took when
  that is more, so that a slow measurement is not repeated on every look at the panel. A row written
  meanwhile is not counted until it expires. The result says how old it is (``age_seconds``) and the
  terminal shows it; ``fresh=True`` skips the cache.
"""
from __future__ import annotations

import threading
import time

WARN_FRACTION = 0.7
WAIT_SECONDS = 8
CACHE_SECONDS = 60
CACHE_FACTOR = 30
# One descriptor of the manifest: the row id, sha256 (64), path ("objects/<sha>.json") and byte count, with their keys.
MANIFEST_LINE = 240
MANIFEST_BASE = 4096
# The characters of a row that each pass keeps (the engine reads the escapes): every character beyond ASCII,
# those from U+0800 on, those beyond the basic plane. A character in the first adds 1 byte, in the second 1 more,
# in the third 1 more, so the sum of the three counts is the bytes beyond the characters.
_BEYOND_ASCII = "\\u0080-\\uD7FF\\uE000-\\uFFFF\\ud800\\udc00-\\udbff\\udfff"
_WIDTHS = (_BEYOND_ASCII, "\\u0800-\\uD7FF\\uE000-\\uFFFF\\ud800\\udc00-\\udbff\\udfff", "\\ud800\\udc00-\\udbff\\udfff")
_DIGITS = "0123456789abcdef"
_cache: dict = {}
_running: dict = {}
_lock = threading.Lock()


def _entry(used, limit):
    return {"used": used, "limit": limit, "fraction": round(used / limit, 4)}


def _kept(pattern):
    """The characters of a row's JSON form that are in ``pattern``, as an engine expression."""
    return f"@this.toJSON().normalize('NFC','[^{pattern}]').length()"


def _bytes_expression():
    """The UTF-8 bytes of a row's JSON form, as one engine expression (for the few rows that are measured alone)."""
    from .store_size import _LENGTH
    return "(" + _LENGTH + "".join(f" + {_kept(pattern)}" for pattern in _WIDTHS) + ")"


def _parts(low, high, prefix):
    """Sixteen ranges of ids that together are the range [low, high): by the digit after ``prefix``."""
    edges = [low] + [prefix + digit for digit in _DIGITS[1:]] + [high]
    return list(zip(edges, edges[1:]))


def _in_range(low, high):
    where, params = [], {}
    if low is not None:
        where.append("id >= :low")
        params["low"] = low
    if high is not None:
        where.append("id < :high")
        params["high"] = high
    return " AND ".join(where), params


def _measure(store, project_id, tables, max_object_bytes, only):
    """Rows, bytes, whether the bytes are exact, the largest row and the source figures of one project."""
    rows, size, largest, exact = 0, 0, 0, True
    sources = {"current": 0, "replaced": 0, "transcripts": 0}
    for table in tables:
        table_rows, table_exact = store.backend.measure_package_table(
            table, project_id, only=only.get(table), group_sources=(table == "sources")
        )
        if not table_exact:
            exact = False
        for row in table_rows:
            n, b = int(row.get("n") or 0), int(row.get("b") or 0)
            rows, size, largest = rows + n, size + b, max(largest, int(row.get("m") or 0))
            if table == "sources":
                sources["current" if row.get("is_current") is True else "replaced"] += b
                if row.get("kind") == "agent_transcript":
                    sources["transcripts"] += b
    # A character is at most 4 bytes: only a row longer than this can come near the object limit in bytes.
    floor = int(max_object_bytes * WARN_FRACTION / 4)
    if largest > floor:
        try:
            for table in tables:
                found_max = store.backend.measure_package_max_bytes(
                    table, project_id, floor_chars=floor, only=only.get(table)
                )
                if found_max is not None:
                    largest = max(largest, int(found_max))
        except RuntimeError:
            exact = False
    return rows, size, exact, largest, sources


class _Job:
    """One measurement running in its own thread; ``result`` is None when the store could not be measured."""

    def __init__(self):
        self.done = threading.Event()
        self.result = None


def _build(store, project_id, tables, limits, now, only):
    max_rows, max_bytes, max_manifest_bytes, max_object_bytes = limits
    rows, size, exact, largest, sources = _measure(store, project_id, tables, max_object_bytes, only)
    manifest = MANIFEST_BASE + rows * MANIFEST_LINE
    figures = {"rows": _entry(rows, max_rows), "bytes": _entry(size + manifest, max_bytes),
               "manifest_bytes": _entry(manifest, max_manifest_bytes),
               "object_bytes": _entry(largest, max_object_bytes)}
    near = [name for name, figure in figures.items() if figure["fraction"] >= WARN_FRACTION]
    return {"measured": "estimate", "exact_bytes": exact, "warn_at": WARN_FRACTION, **figures,
            "sources_bytes": sources, "warning": near or None}


def _run(job, key, store, project_id, tables, limits, now, only):
    started = now()
    try:
        result = _build(store, project_id, tables, limits, now, only)
        with _lock:
            _cache[key] = (started, limits, result, max(CACHE_SECONDS, CACHE_FACTOR * (now() - started)))
            if len(_cache) > 64:
                _cache.pop(next(iter(_cache)))
        job.result = result
    except (OSError, ValueError, RuntimeError):
        job.result = None
    finally:
        with _lock:
            _running.pop(key, None)
        job.done.set()


def package_size(store, project_id, tables, *, max_rows, max_bytes, max_manifest_bytes, max_object_bytes,
                 fresh=False, wait=WAIT_SECONDS, now=time.monotonic, only=None):
    """The package against its limits, with a warning from WARN_FRACTION of any of them; None when it cannot be measured.

    A measurement of a large project takes longer than a request may (the terminal waits 20 seconds for a
    status), so it runs in a thread and is waited for ``wait`` seconds. If it is not ready, the last figure is
    returned with its age, or ``{"measured": "pending"}`` when there is none; the thread goes on and the next
    status finds the figure in the cache."""
    key, moment = (id(store), project_id), now()
    limits = (max_rows, max_bytes, max_manifest_bytes, max_object_bytes)
    with _lock:
        kept = _cache.get(key)
        if kept and kept[1] != limits:
            kept = None
        if not fresh and kept and moment - kept[0] < kept[3]:
            return {**kept[2], "age_seconds": int(moment - kept[0])}
        job = _running.get(key)
        if job is None:
            job = _running[key] = _Job()
            threading.Thread(target=_run, args=(job, key, store, project_id, tables, limits, now, only or {}),
                             daemon=True, name="package-size").start()
    if job.done.wait(wait):
        return None if job.result is None else {**job.result, "age_seconds": 0}
    if kept:
        return {**kept[2], "age_seconds": int(moment - kept[0])}
    return {"measured": "pending", "warn_at": WARN_FRACTION, "warning": None}
