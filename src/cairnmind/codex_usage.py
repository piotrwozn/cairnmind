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
    skip = 0
    while True:
        rows = store.query(
            "SELECT content FROM sources WHERE project_id=:project AND is_current=true "
            "AND uri LIKE :prefix AND created_at>=:start AND content LIKE :marker "
            f"ORDER BY created_at,id SKIP {skip} LIMIT {PAGE}",
            {"project": project_id, "prefix": "transcript://codex/%", "marker": f"%{_MARKER}%",
             "start": start.isoformat(timespec="microseconds")})
        yield from (row["content"] for row in rows if isinstance(row.get("content"), str))
        if len(rows) < PAGE:
            return
        skip += PAGE


def waiting_in_inbox(store, project_id: str) -> int:
    """Codex usage records in the ingestion inbox that belong, or may belong, to this project.

    Import parks an event there when no project matched its working directory or the directory belongs to
    another project than the one requested. Counted: records whose directory resolves to this project, and
    records of no known directory. Records of directories of other projects, or of none we know, are not ours.
    """
    waiting = 0
    for row in store.query("SELECT cwd,count(*) AS n FROM ingestion_inbox WHERE client=:client AND raw LIKE :marker "
                           "GROUP BY cwd", {"client": "codex", "marker": f"%{_MARKER}%"}):
        cwd = row.get("cwd")
        try:
            ours = cwd is None or store.resolve_project(cwd)["id"] == project_id
        except ValueError:
            ours = False
        waiting += int(row["n"]) if ours else 0
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
