"""What the activity panel shows, as pure functions of the broker's answers (#144).

No I/O and no Textual: the panel (terminal_activity) renders these rows. Every text from memory
passes `terminal_safe` here; a state is a word from a fixed set, never a raw value.
"""
from __future__ import annotations

import time

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


def group_tasks(tasks, *, small=False, extra=0):
    """Groups in a fixed order, newest change first; `hidden` counts the rows a cap leaves out.

    `extra` rows of free height go to the capped groups in order; a group shown whole also gives
    back the row its "+ N more" line took.
    """
    rows, groups = live_tasks(tasks), []
    for state, label, cap in GROUPS:
        cap = min(cap, 2) if small else cap
        items = sorted((task for task in rows if task_state(task) == state), key=_updated, reverse=True)
        if state == "blocked" and not items:
            continue
        hidden = max(0, len(items) - cap)
        if hidden and extra > 0:
            shown = hidden if hidden <= extra + 1 else extra
            extra -= shown if shown < hidden else hidden - 1
            cap, hidden = cap + shown, hidden - shown
        groups.append({"state": state, "label": label, "total": len(items), "items": items[:cap], "hidden": hidden})
    return groups


def task_summary(groups):
    """(label, count) of each non-empty group, for the one-line summary of a short popup."""
    return [(group["label"], group["total"]) for group in groups if group["total"]]


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
    """(client, lifecycle word, live) per started slot; an unknown lifecycle is "None", never the raw value.

    A slot that was never started is left out: it has nothing to say under "Running now".
    """
    rows = []
    for slot in slots if isinstance(slots, list) else []:
        if isinstance(slot, dict) and slot.get("run_id"):
            client = slot.get("client") or slot.get("profile_id") or slot.get("id") or ""
            word = LIFECYCLE.get(slot.get("lifecycle"), "None") if slot.get("run_id") else "None"
            rows.append((first_line(client) if isinstance(client, str) else "", word, slot.get("lifecycle") in _LIVE
                         and bool(slot.get("run_id"))))
    return rows


def run_is_live(run) -> bool:
    """Only a live run has a process to stop, interrupt or take control of."""
    return isinstance(run, dict) and run.get("lifecycle", run.get("state")) in _LIVE


def session_label(run, translate=lambda text: text) -> str:
    """One row of Sessions: client, short id and start time tell runs of one folder apart (#154).

    The lifecycle is a fixed word, never the raw value; the folder goes last so a narrow list cuts it.
    """
    client = first_line(run.get("client")) or "?"
    short = first_line(run.get("id"))[:7]
    word = translate(LIFECYCLE.get(run.get("lifecycle", run.get("state")), "Unknown"))
    parts = [f"{client} {short}".strip(), word]
    started = run.get("created_at")
    if isinstance(started, (int, float)) and not isinstance(started, bool) and 0 < started < 2**33:
        parts.append(time.strftime("%Y-%m-%d %H:%M", time.localtime(started)))
    memory = run.get("memory") if isinstance(run.get("memory"), dict) else {}
    parts.append(translate("Memory acknowledged" if memory.get("acknowledged") else "Memory acknowledgement required"))
    parts.append(first_line(run.get("worktree")) or translate("Account login"))
    return " · ".join(parts)


KIND_WORDS = {"info": "info", "warn": "warning", "error": "error"}
_KIND_ROLE = {"info": "label", "warn": "warn", "error": "danger"}


def notice_parts(entry, width, words, project, folder, today, *, two_rows=False):
    """(role, text) parts of one notice; `words` holds the translated kind words and, under None, "No project".

    Another project is named before the text; with no project selected nothing is "another".
    """
    stamp = entry["when"].strftime("%H:%M" if entry["when"].date() == today else "%m-%d")
    label = ""
    if project and (entry["project_id"] != project or (entry["worktree"] and folder and entry["worktree"] != folder)):
        label = "[" + first_line(entry["project_name"] or entry["project_id"] or words[None]) + "] "
    text = label + first_line(entry["text"])
    count = f" (×{entry['count']})" if entry["count"] > 1 else ""
    word, role = words[entry["kind"]], _KIND_ROLE[entry["kind"]]
    if two_rows:  # time and kind above the text, so the text keeps the whole width
        return [("label", f"  {stamp} "), (role, word), ("label", count), ("value", "\n    "), ("stored", fit(text, width - 4))]
    pad = max(cell_len(words[kind]) for kind in KIND_WORDS)
    room = width - cell_len(f"  {stamp} ") - pad - 1 - cell_len(count)
    return [("label", f"  {stamp} "), (role, word + " " * (pad - cell_len(word) + 1)), ("stored", fit(text, room)),
            ("label", count)]


def items_of(value):
    rows = value.get("items") if isinstance(value, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
