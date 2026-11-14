"""What the activity panel shows, as pure functions of the broker's answers (#144).

No I/O and no Textual: the panel (terminal_activity) renders these rows. Every text from memory
passes `terminal_safe` here; a state is a word from a fixed set, never a raw value.
"""
from __future__ import annotations

from rich.cells import cell_len

from .terminal_safe import safe_text

# (state, label, rows shown); Blocked is left out while it is empty.
GROUPS = (("in_progress", "In progress", 3), ("needs_review", "Needs review", 3), ("needs_test", "Needs test", 3),
          ("blocked", "Blocked", 2), ("todo", "To do", 3), ("done", "Done recently", 3))
_STATES = {state for state, _, _ in GROUPS}
LIFECYCLE = {"running": "Running", "starting": "Starting", "stopping": "Stopping", "exited": "Ended",
             "failed": "Failed", "interrupted": "Interrupted"}
_LIVE = {"running", "starting", "stopping"}
STAGES = {"in_progress": "implement", "needs_review": "review", "needs_test": "test"}


def first_line(value) -> str:
    text = safe_text(value) if isinstance(value, str) else ""
    return next((line.strip() for line in text.replace("\t", " ").splitlines() if line.strip()), "")


def fit(text, width: int) -> str:
    """One row: cut by cell width, ending in `…`; a wide character never crosses the edge."""
    text = str(text)
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    out, used = [], 0
    for character in text:
        size = cell_len(character)
        if used + size > width - 1:
            break
        out.append(character)
        used += size
    return "".join(out) + "…"


def _metadata(task):
    value = task.get("metadata")
    return value if isinstance(value, dict) else {}


def task_state(task) -> str:
    state = _metadata(task).get("state")
    return state if state in _STATES else "todo"


def _updated(row) -> str:
    value = row.get("updated_at")
    return value if isinstance(value, str) else ""


def _plan_of(task):
    value = _metadata(task).get("plan_ref")
    return value if isinstance(value, str) and value else None


def live_tasks(tasks):
    return [task for task in tasks or [] if isinstance(task, dict) and task.get("status") != "superseded"]


def group_tasks(tasks, *, small=False):
    """Groups in a fixed order, newest change first; `hidden` counts the rows a cap leaves out."""
    rows, groups = live_tasks(tasks), []
    for state, label, cap in GROUPS:
        cap = min(cap, 2) if small else cap
        items = sorted((task for task in rows if task_state(task) == state), key=_updated, reverse=True)
        if state == "blocked" and not items:
            continue
        groups.append({"state": state, "label": label, "total": len(items), "items": items[:cap],
                       "hidden": max(0, len(items) - cap)})
    return groups


def plan_progress(tasks, plan_id):
    mine = [task for task in live_tasks(tasks) if plan_id and _plan_of(task) == plan_id]
    return sum(task_state(task) == "done" for task in mine), len(mine)


def finished_plans(tasks, plans, active_id, limit=3):
    """Plans other than the active one whose every step is done, by their last change (not a finish time)."""
    steps = {}
    for task in live_tasks(tasks):
        if (plan := _plan_of(task)) and plan != active_id:
            steps.setdefault(plan, []).append(task)
    titles = {row["id"]: row for row in plans or [] if isinstance(row, dict) and isinstance(row.get("id"), str)}
    found = []
    for plan, items in steps.items():
        if all(task_state(task) == "done" for task in items):
            last = max(_updated(task) for task in items)
            title = titles.get(plan, {}).get("title")
            found.append({"id": plan, "title": title if isinstance(title, str) and title else plan,
                          "date": last[5:10] if len(last) >= 10 else "", "count": len(items), "last": last})
    found.sort(key=lambda row: row["last"], reverse=True)
    return found[:limit]


def running_rows(slots):
    """(client, lifecycle word) per slot; an unknown lifecycle is "None", never the raw value."""
    rows = []
    for slot in slots if isinstance(slots, list) else []:
        if isinstance(slot, dict):
            client = slot.get("client") or slot.get("profile_id") or slot.get("id") or ""
            word = LIFECYCLE.get(slot.get("lifecycle"), "None") if slot.get("run_id") else "None"
            rows.append((first_line(client) if isinstance(client, str) else "", word, slot.get("lifecycle") in _LIVE
                         and bool(slot.get("run_id"))))
    return rows


def items_of(value):
    rows = value.get("items") if isinstance(value, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
