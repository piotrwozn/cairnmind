"""Choose which record entries fit the character budget of Retriever.context (#188).

Entries arrive in presentation order (context_place, #213): the checkpoint and the open tasks,
then decisions and constraints in force, other confirmed records, new findings, and finished
tasks last, each group most recently updated first. Before that order is applied, a stated share of
the budget is reserved for the most recently updated proposed records, so new findings are
seen even when many confirmed records or open tasks exist. The reserve always grows to hold the
newest of those records when it fits the whole budget, so a long finding with metadata beside an
active plan is not passed over for an older, shorter one. The reserve is not lost when there
are few findings: the main pass fills the whole budget. An entry that does not fit is skipped
and filling goes on, so one long entry does not hide every shorter one after it.

Only kind, status and update order decide the place; nothing the record text says does.
Selection is read-only: nothing is deleted and no status changes.
"""
from __future__ import annotations

import json
from collections import Counter

# A quarter of the budget, as proposed in #188 and the research answer in #19.
PROPOSED_SHARE = 0.25


def context_place(kind: str, status: str, finished: bool) -> tuple[int, bool]:
    """Sort key of a record in the context; smaller comes first (#213).

    0 the checkpoint, 1 open tasks (the active plan's steps), 2 confirmed decisions and
    constraints, 3 other confirmed records, 4 the rest (proposed and disputed findings),
    5 finished tasks. Confirmed comes first inside a place. ``finished`` is the task's own
    state ``done``; status and state are stored fields, never read from the record text.
    """
    if kind == "checkpoint":
        place = 0
    elif kind == "task":
        place = 5 if finished else 1
    elif status == "confirmed":
        place = 2 if kind in ("decision", "constraint") else 3
    else:
        place = 4
    return place, status != "confirmed"


def entry_size(entry: dict) -> int:
    return len(json.dumps(entry, ensure_ascii=False))


def select_entries(entries: list[dict], reserve: list[bool], budget: int,
                   share: float = PROPOSED_SHARE) -> tuple[list[dict], list[dict]]:
    """Return the selected entries in presentation order and omitted counts per kind and status.

    ``reserve[i]`` marks an entry that may use the reserved share; such entries are tried for the
    reserve in the order given, which is most recently updated first. The first of them is taken
    whenever it fits the whole budget: the reserve grows to its size when the share is smaller.
    """
    if len(entries) != len(reserve):
        raise ValueError("Every entry needs a reserve flag")
    sizes = [entry_size(entry) for entry in entries]
    chosen: set[int] = set()
    used = 0
    reserved = int(max(0, budget) * min(max(share, 0.0), 1.0))
    newest = next((index for index, flag in enumerate(reserve) if flag), None)
    if newest is not None and sizes[newest] <= budget:
        reserved = max(reserved, sizes[newest])
    for index, size in enumerate(sizes):
        if reserve[index] and used + size <= reserved:
            chosen.add(index)
            used += size
    for index, size in enumerate(sizes):
        if index not in chosen and used + size <= budget:
            chosen.add(index)
            used += size
    selected = [entry for index, entry in enumerate(entries) if index in chosen]
    left = Counter((str(entry.get("kind")), str(entry.get("status")))
                   for index, entry in enumerate(entries) if index not in chosen)
    omitted = [{"kind": kind, "status": status, "count": count} for (kind, status), count in sorted(left.items())]
    return selected, omitted
