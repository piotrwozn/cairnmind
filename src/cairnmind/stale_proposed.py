"""Proposed findings that nobody has touched for a while, oldest first (#189). Read-only.

A proposed record stays proposed until a person confirms or disputes it (or something replaces it), and
nothing else points out the old ones. This module only lists them: the record's id, kind, how long since
it was last written, how many sources stand behind it and when its newest evidence existed. It writes
nothing and asks no model. Acting on an entry is the person's existing review (the inspector's Confirm and
Dispute, with the record version check and an audit note), never something this list does.

Checkpoints and tasks are left out, as in ``cairnmind brief``: a checkpoint is not a finding, and a task
moves through its own states and is confirmed with its plan.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .sql import ACT_NOTE_ORIGINS, parse_utc

DEFAULT_DAYS = 30
MAX_DAYS = 3650
MAX_ITEMS = 500
CONTENT_LIMIT = 300
LEFT_OUT_KINDS = ("checkpoint", "task")
BASES = ("stated", "observed", "mixed", "unknown")
NEXT_STEP = ("Nothing here changes a record. Confirm or dispute each one in the memory inspector (Ctrl+B i, "
             "view Proposed): open it with Enter, then Confirm or Dispute.")


def valid_days(value) -> int:
    if type(value) is not int or not 0 <= value <= MAX_DAYS:
        raise ValueError(f"Days must be a whole number from 0 to {MAX_DAYS}")
    return value


def _evidence_sources(store, project_id, records) -> dict:
    """Per record, how many of its sources are evidence: a review or other act note says who judged it, not why."""
    ids = sorted({source for record in records for source in record.get("source_ids") or [] if isinstance(source, str)})
    notes = set()
    for first in range(0, len(ids), 500):
        rows = store.query("SELECT id, metadata.origin AS origin FROM sources WHERE project_id=:project AND id IN :ids",
                           {"project": project_id, "ids": ids[first:first + 500]})
        notes.update(row["id"] for row in rows if row.get("origin") in ACT_NOTE_ORIGINS)
    return {record["id"]: len([source for source in dict.fromkeys(record.get("source_ids") or [])
                               if isinstance(source, str) and source not in notes]) for record in records}


def _item(record, now, sources):
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    updated = parse_utc(record.get("updated_at"))
    content = str(record.get("content") or "")
    basis = metadata.get("evidence_basis")
    return {"id": record["id"], "kind": record.get("kind"), "version": record.get("version"),
            "updated_at": record.get("updated_at"),
            "age_days": max(0, (now - updated).days) if updated else None,
            "sources": sources, "evidence_latest": metadata.get("evidence_latest"),
            "evidence_basis": basis if basis in BASES else "unknown",
            "content": content[:CONTENT_LIMIT], "content_truncated": len(content) > CONTENT_LIMIT}


def stale_proposed(store, project_id, *, days=DEFAULT_DAYS, matches=None, now=None, limit=MAX_ITEMS) -> dict:
    """Proposed findings of the project last written `days` or more days ago, the longest untouched first.

    `matches(record)` keeps a record in the asked worktree (the inspector's scope rule); None keeps all.
    A record whose write time cannot be read is listed first, with an unknown age: it is never hidden.
    """
    valid_days(days)
    if type(limit) is not int or not 1 <= limit <= MAX_ITEMS:
        raise ValueError(f"Limit must be between 1 and {MAX_ITEMS}")
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    rows = store.query("SELECT FROM records WHERE project_id=:project AND status='proposed' ORDER BY id",
                       {"project": project_id})
    waiting = []
    for row in rows:
        row = {key: value for key, value in row.items() if not key.startswith("@")}
        if row.get("kind") in LEFT_OUT_KINDS or (matches is not None and not matches(row)):
            continue
        updated = parse_utc(row.get("updated_at"))
        if updated is None or updated <= cutoff:
            waiting.append((updated, row))
    waiting.sort(key=lambda pair: (pair[0] is not None, pair[0] or now, pair[1]["id"]))
    shown = [row for _, row in waiting[:limit]]
    counts = _evidence_sources(store, project_id, shown)
    return {"project_id": project_id, "days": days, "now": now.isoformat(), "total": len(waiting),
            "truncated": len(waiting) > limit, "items": [_item(row, now, counts[row["id"]]) for row in shown]}


def listing(inspector, project, worktree=None, days=DEFAULT_DAYS, limit=MAX_ITEMS) -> dict:
    """The inspector's list: its project and worktree rules (a worktree must be a registered root of the project)."""
    scope = inspector._scope(project, worktree)
    return stale_proposed(inspector.store, scope[0]["id"], days=days, limit=limit,
                          matches=lambda row: inspector._matches(scope, row))


def stale_text(result, project_name) -> str:
    """What `cairnmind proposed` prints: stored text is one literal line behind a bar, never a line of its own."""
    from .brief import _line
    days, total = result["days"], result["total"]
    head = (f"Proposed findings in {_line(project_name, 60)} not updated for {days} "
            f"day{'' if days == 1 else 's'} or more: {total}" + (", oldest first." if total else "."))
    lines = [head]
    for item in result["items"]:
        age = "age unknown" if item["age_days"] is None else f"{item['age_days']} day{'' if item['age_days'] == 1 else 's'}"
        evidence = str(item.get("evidence_latest") or "")[:10]
        when = f"{_line(evidence, 10)} ({item['evidence_basis']})" if evidence else "unknown"
        lines += ["", f"{_line(item['id'], 80)}  {_line(item.get('kind') or '', 30)}  not updated for {age}",
                  f"  sources: {item['sources']} · newest evidence: {when}",
                  f"  | {_line(item['content'].strip().split(chr(10), 1)[0], 120)}"]
    if result["truncated"]:
        lines += ["", f"Only the {len(result['items'])} oldest are shown."]
    if total:
        lines += ["", NEXT_STEP]
    return "\n".join(lines)
