"""Token usage of Codex, read from what history import stored for a project.

Import keeps every complete rollout line as a source ``transcript://codex/...`` under the project that import
attributed the event to (``ingest._import_history_locked``), the raw line as its content, so the
``token_usage_record`` events of a project are in the store: no rollout file is opened, attribution is the
import's own, and a moved or deleted rollout does not matter. What is not seen: rollouts that were never
imported, and events still waiting in the ingestion inbox (``waiting_in_inbox`` counts those of this
project). In a record ``usage`` is the usage of one response and ``thread_token_usage`` the running sum of its
thread, so the usage of a period is the sum of its responses, each counted once even if the rollout was
imported from two paths; the running sums are only used to notice a lost response. No cost is computed:
Codex is a subscription client. Nothing is written; the functions that query take the store as an argument.
"""
from __future__ import annotations

import json

_MARKER = "token_usage_record"
PAGE = 2000


def _counters(usage):
    """(input, output) of a usage object, or None when either is not a plausible count."""
    if not isinstance(usage, dict):
        return None
    values = tuple(usage.get(key) for key in ("input_tokens", "output_tokens"))
    return values if all(type(v) is int and 0 <= v <= 10**12 for v in values) else None


def stored_records(store, project_id: str, start):
    """The raw lines of this project's Codex usage records created at or after ``start``, in pages.

    An event of the period was imported after it happened, so ``created_at`` of its source is not before
    ``start``; older sources are never read. ``_`` in LIKE is a wildcard, so the lines are checked again.
    """
    rows = _pages(store, "SELECT id,created_at,content FROM sources WHERE project_id=:project AND is_current=true "
                         "AND uri LIKE :prefix AND created_at>=:start AND content LIKE :marker",
                  {"project": project_id, "prefix": "transcript://codex/%", "marker": f"%{_MARKER}%",
                   "start": start.isoformat(timespec="microseconds")})
    yield from (row["content"] for row in rows if isinstance(row.get("content"), str))


def _pages(store, sql, params):
    """The rows of ``sql`` (a SELECT with id and created_at, ending in its conditions), in pages by created_at, id.

    A page starts after the last row of the one before, not at an offset. The store is read without a lock, so
    rows may be added or removed between pages: a row that stays unchanged the whole time is still read exactly
    once, where with SKIP a removed earlier row would shift an unread one out of every page. A row whose
    created_at is rewritten meanwhile can be read twice; a caller that counts must tell rows apart by id.
    """
    last = {}
    while True:
        after = " AND (created_at>:last_at OR (created_at=:last_at AND id>:last_id))" if last else ""
        rows = store.query(f"{sql}{after} ORDER BY created_at,id LIMIT {PAGE}", {**params, **last})
        yield from rows
        if len(rows) < PAGE:
            return
        last = {"last_at": rows[-1]["created_at"], "last_id": rows[-1]["id"]}


def waiting_in_inbox(store, project_id: str, start) -> int:
    """Codex usage records of the period in the ingestion inbox that belong, or may belong, to this project.

    Import parks an event there when no project matched its working directory or the directory belongs to
    another project than the one requested; ``created_at`` is the time of that import, so like
    ``stored_records`` only rows from ``start`` on are looked at, in pages. Counted: rows whose line really is
    a usage record (a message that merely mentions the name is not) and whose directory resolves to this
    project, or is unknown. Rows of directories of other projects, or of none we know, are not ours.
    """
    waiting, ours, seen = 0, {}, set()
    for row in _pages(store, "SELECT id,created_at,cwd,raw FROM ingestion_inbox WHERE client=:client "
                             "AND created_at>=:start AND raw LIKE :marker",
                      {"client": "codex", "marker": f"%{_MARKER}%", "start": start.isoformat(timespec="microseconds")}):
        # Import parks a line again under the same id with a new time; such a row can come back on a later page.
        if row.get("id") in seen:
            continue
        seen.add(row.get("id"))
        try:
            event = json.loads(row.get("raw"))
            if not isinstance(event, dict) or event.get("type") != _MARKER:
                continue
        except (ValueError, TypeError, RecursionError):
            continue
        cwd = row.get("cwd")
        if cwd not in ours:
            try:
                ours[cwd] = cwd is None or store.resolve_project(cwd)["id"] == project_id
            except ValueError:
                ours[cwd] = False
        waiting += ours[cwd]
    return waiting


def codex_units(records, start, now, parse_time, counts: dict) -> list[tuple]:
    """(time, input, output, complete) of every Codex response inside the period.

    A record that cannot be read is counted in ``codex_unreadable`` and makes every unit incomplete: a
    lost response could be on any day. A response is also incomplete when the thread's running sum does not
    grow by exactly its usage from the previous response seen, which means a response between them is missing.
    """
    seen = {}
    for raw in records:
        try:
            event = json.loads(raw)
            if event.get("type") != _MARKER:
                continue
            payload, at = event["payload"], parse_time(event["timestamp"])
            mine, total = _counters(payload["usage"]), _counters(payload["thread_token_usage"])
            key = tuple(payload[name] for name in ("thread_id", "turn_id", "response_id"))
            if at is None or mine is None or total is None or not all(isinstance(part, str) for part in key):
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            counts["codex_unreadable"] += 1
            continue
        seen.setdefault(key, (at, mine, total))
    counts["codex_records"] += len(seen)
    units, last = [], {}
    for key, (at, mine, total) in sorted(seen.items(), key=lambda item: item[1][0]):
        before = last.get(key[0])
        last[key[0]] = total
        if start <= at <= now:
            grows = before is None or all(total[i] == before[i] + mine[i] for i in (0, 1))
            units.append((at, mine[0], mine[1], grows and not counts["codex_unreadable"]))
    return units
