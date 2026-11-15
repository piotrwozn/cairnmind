"""Choose which record entries fit the character budget of Retriever.context (#188).

Entries arrive in presentation order (context_place, #213): the checkpoint and the open tasks,
then decisions and constraints in force, other confirmed records, new findings, and finished
tasks last, each group most recently updated first. Before that order is applied, a stated share of
the budget is reserved for the most recently updated proposed records, so new findings are
seen even when many confirmed records or open tasks exist, and another for the confirmed decisions
and constraints in force, so open tasks do not push the rules out (#241); the checkpoint is taken
between the two, as before #241, so the rules reserve cannot push it out of a small budget, and tasks
get the rest. Each
reserve always grows to hold the newest of its records when that fits what is left of the budget,
so a long finding with metadata beside an active plan is not passed over for an older, shorter one.
A reserve is not lost when there are few such records: the main pass fills the whole budget. An
entry that does not fit is skipped and filling goes on, so one long entry does not hide every
shorter one after it.

Only kind, status and update order decide the place; nothing the record text says does.
Selection is read-only: nothing is deleted and no status changes.
"""
from __future__ import annotations

import json
from collections import Counter

# A quarter of the budget, as proposed in #188 and the research answer in #19.
PROPOSED_SHARE = 0.25
# The rules in force (#241). In Retriever.context the budget is about 6,800 characters without an active
# plan; a short confirmed decision or constraint takes about 560-600 of them with its confirmation mark and
# sources, a long one about 1,100, an open task 650-1,400 and the checkpoint about 1,000. 0.3 (about
# 2,000) holds three short rules or a long and a short one, and leaves at least 45% (about 3,000) to the
# checkpoint and the open tasks after both reserves when neither grows. The checkpoint is taken before the
# rules reserve, so a reserve grown to a long rule never pushes it out of a small budget (2,500 beside an
# active plan), as it was not before #241.
RULES_SHARE = 0.3


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


def rule_in_force(entry: dict) -> bool:
    """A confirmed decision or constraint: place 2 of context_place, from the stored kind and status."""
    return entry.get("status") == "confirmed" and entry.get("kind") in ("decision", "constraint")


def entry_size(entry: dict) -> int:
    return len(json.dumps(entry, ensure_ascii=False))


def select_entries(entries: list[dict], reserve: list[bool], budget: int,
                   share: float = PROPOSED_SHARE, rules_share: float = RULES_SHARE) -> tuple[list[dict], list[dict]]:
    """Return the selected entries in presentation order and omitted counts per kind and status.

    ``reserve[i]`` marks an entry that may use the reserved share; such entries are tried for the
    reserve in the order given, which is most recently updated first. The first of them is taken
    whenever it fits the whole budget: the reserve grows to its size when the share is smaller.
    Then the checkpoint is taken when it fits what is left, and ``rules_share`` is reserved the same
    way for the rules in force (rule_in_force), the first of them taken whenever it fits what is left.
    """
    if len(entries) != len(reserve):
        raise ValueError("Every entry needs a reserve flag")
    sizes = [entry_size(entry) for entry in entries]
    chosen: set[int] = set()
    used = 0
    checkpoint = [entry.get("kind") == "checkpoint" for entry in entries]
    for flags, part in ((reserve, share), (checkpoint, 1.0), ([rule_in_force(entry) for entry in entries], rules_share)):
        reserved = int(max(0, budget) * min(max(part, 0.0), 1.0))
        newest = next((index for index, flag in enumerate(flags) if flag and index not in chosen), None)
        if newest is not None and sizes[newest] <= budget - used:
            reserved = max(reserved, sizes[newest])
        taken = 0
        for index, size in enumerate(sizes):
            if flags[index] and index not in chosen and taken + size <= reserved and used + size <= budget:
                chosen.add(index)
                taken += size
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
