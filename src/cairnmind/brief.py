"""A short human brief of where work stopped, built only from stored data.

No model is asked. Every line below the title and the legend says where it comes
from: ``machine`` lines are measured or computed now by Cairnmind, ``reported``
lines repeat what an agent or a person stored and were not verified. Stored
titles never stand in the middle of a measured statement: on a ``machine`` line
they come last, after a ``|``.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import unicodedata

from cairnmind.store import DERIVED_RECORD_METADATA, degraded_origin
from cairnmind.terminal_safe import safe_text

MAX_LINES = 40
ACTIVE_STATES = ("in_progress", "needs_test", "needs_review")
_CHECKPOINT_KEYS = ("summary", "done", "completed", "in_progress", "next", "next_steps", "blockers", "notes")
_LEGEND = "machine = measured now by Cairnmind; reported = stored claim, not verified; text after | is a stored title"
# Characters that render as empty cells without being whitespace could push forged text onto a new terminal row.
_BLANK = {0x2800, 0x3164, 0x115F, 0x1160, 0xFFA0}


def _line(value, limit=160):
    raw = "".join(" " if ord(char) in _BLANK or unicodedata.category(char) in ("Cf", "Zs", "Zl", "Zp") else char
                  for char in str(value))
    text = " ".join(safe_text(raw).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _title(record):
    return _line(str(record.get("content", "")).strip().split("\n", 1)[0], 90)


def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _age(then, now):
    seconds = max(0, int((now - then).total_seconds()))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "less than a minute ago"


def _flat(value):
    if isinstance(value, (list, tuple)):
        return "; ".join(_flat(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_flat(item)}" for key, item in value.items())
    return str(value)


def _git_out(root, *args):
    """Git's output, or None when Git could not be run or failed; a failure is never an empty answer."""
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=15, check=False,
                                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else None


def _checkpoint_saved(store, checkpoint):
    """When the latest checkpoint was saved, and whether an external client's session saved it."""
    # A later edit of the record (a review, for example) moves updated_at; the snapshot
    # written by the checkpoint itself keeps the time the checkpoint was really saved.
    times = []
    for source_id in checkpoint.get("source_ids") or []:
        try:
            source = store.get_source(source_id)
        except ValueError:
            continue
        if str(source.get("uri", "")).startswith("checkpoint://") and _time(source.get("created_at")):
            times.append((_time(source.get("created_at")), degraded_origin(source.get("metadata"))))
    if not times:
        return _time(checkpoint.get("updated_at")), degraded_origin(checkpoint.get("metadata"))
    return max(times, key=lambda item: item[0])


def _checkpoint(store, records, now):
    checkpoint = next((r for r in records if r["kind"] == "checkpoint" and r.get("key") == "latest"), None)
    if checkpoint is None:
        return None, [("machine", "No checkpoint has been saved for this project.")]
    saved, external = _checkpoint_saved(store, checkpoint)
    when = f"{_age(saved, now)} ({saved.isoformat(timespec='seconds')})" if saved else "at an unknown time"
    lines = [("machine", f"Latest checkpoint saved {when}.")]
    if external:
        lines.append(("machine", "It was saved by an external client's session (history unarchived, "
                                 "evidence degraded); nothing in it is verified."))
    # The record's metadata accumulates the fields of every earlier checkpoint;
    # only the content is exactly what the latest checkpoint said.
    try:
        state = json.loads(checkpoint.get("content") or "")
    except ValueError:
        state = None
    if not isinstance(state, dict):
        lines.append(("machine", "The checkpoint content cannot be read as fields; see `cairnmind context`."))
        return saved, lines
    keys = [key for key in (*(k for k in _CHECKPOINT_KEYS if k in state), *(k for k in state if k not in _CHECKPOINT_KEYS))
            if key not in DERIVED_RECORD_METADATA and state[key] not in (None, "", [], {})]
    shown = keys[:4]
    lines += [("reported", f"Checkpoint {_line(key, 40)}: {_line(_flat(state[key]))}") for key in shown]
    if len(shown) < len(keys):
        lines.append(("machine", f"The checkpoint has {len(keys) - len(shown)} more fields; see `cairnmind context`."))
    return saved, lines


def _plan(plans, project_id, root, by_id):
    result = plans.next_task(project_id, worktree=root)
    plan, task = result.get("plan"), result.get("task")
    if plan is None:
        return [("machine", "No plan is active in this worktree.")]
    lines = [("machine", f"A plan is active (version {plan['version']}, {len(plan['step_ids'])} steps) "
                         f"| plan: {_line(plan['title'], 90)}")]
    if task is not None:
        lines.append(("machine", f"Next task is {_line(result['reason'], 30).replace('_', ' ')} | task: {_title(task)}"))
    elif result["reason"] == "completed":
        lines.append(("machine", "Every step of the active plan is done."))
    else:
        lines.append(("machine", "No task can start: every remaining step is blocked."))
    blocked = result.get("blocked", [])
    for entry in blocked[:4]:
        blocked_task = by_id.get(entry["task_id"])
        title = _title(blocked_task) if blocked_task else _line(entry["task_id"])
        if "unfinished_dependencies" in entry["reasons"]:
            lines.append(("machine", f"Blocked by unfinished dependencies | task: {title}"))
        else:
            # The blocked state alone is whatever an agent stored on the task.
            lines.append(("reported", f"Marked as blocked | task: {title}"))
    if len(blocked) > 4:
        lines.append(("machine", f"{len(blocked) - 4} more blocked steps."))
    return lines


def _verification(store, tasks, root):
    from cairnmind.verification import capture_code_state, verification_status
    active = [task for task in tasks if task["metadata"].get("state", "todo") in ACTIVE_STATES]
    if not active:
        return [("machine", "No task is in progress, waiting for a test or waiting for a review.")]
    observed = {}

    def capture(store, project_id, worktree=None, extra_inputs=None):
        # One observation of the tree serves every task of this brief; hashing it per task would be slow.
        key = tuple(sorted(extra_inputs or ()))
        if key not in observed:
            observed[key] = capture_code_state(store, project_id, worktree, extra_inputs)
        return observed[key]

    lines = []
    for task in active[:6]:
        status = verification_status(store, task, root, capture=capture)
        reason = f" ({_line(status['reasons'][0], 100)})" if status.get("reasons") else ""
        # "reported" is a claim carried from an agent or another machine, never a measurement.
        basis = "reported" if status["status"] == "reported" else "machine"
        state = _line(task["metadata"].get("state", "todo"), 20)
        lines.append((basis, f"Checks {_line(status['status'], 20)}{reason}, task state {state} | task: {_title(task)}"))
        from cairnmind.scope import drift_warning
        warning = drift_warning(status.get("scope_drift"))
        if warning:
            lines.append(("machine", f"{warning} | task: {_title(task)}"))
    if len(active) > 6:
        lines.append(("machine", f"{len(active) - 6} more tasks in progress are not listed."))
    return lines


def _commits(root, saved):
    if saved is None:
        return []
    if _git_out(root, "rev-parse", "--is-inside-work-tree") is None:
        return [("machine", "Git could not be read in this worktree, so commits are not listed.")]
    # Git compares whole seconds; a commit made in the checkpoint's own second came before it.
    log = _git_out(root, "log", f"--since={int(saved.timestamp()) + 1}", "--format=%h %s", "-n", "200")
    if log is None:
        if _git_out(root, "rev-parse", "--verify", "-q", "HEAD") is None:
            return [("machine", "This repository has no commits yet.")]
        return [("machine", "Git could not list commits in this worktree.")]
    subjects = [line for line in log.splitlines() if line.strip()]
    if not subjects:
        return [("machine", "No commits dated after the checkpoint was saved.")]
    count = "200 or more" if len(subjects) == 200 else str(len(subjects))
    lines = [("machine", f"{count} commit{'' if len(subjects) == 1 else 's'} dated after the checkpoint was saved:")]
    lines += [("machine", f"  {_line(subject, 100)}") for subject in subjects[:5]]
    if len(subjects) > 5:
        lines.append(("machine", f"  and {len(subjects) - 5 if len(subjects) < 200 else 'at least 195'} older."))
    return lines


def _proposed(records):
    # Tasks move through their own states and a checkpoint is not a finding, so neither waits for this review.
    waiting = [r for r in records if r.get("status") == "proposed" and r["kind"] not in ("checkpoint", "task")]
    if not waiting:
        return [("machine", "No proposed findings are waiting for review.")]
    lines = [("machine", f"{len(waiting)} proposed finding{'' if len(waiting) == 1 else 's'} waiting for review:")]
    lines += [("reported", f"  {r['kind']}: {_title(r)}") for r in waiting[:4]]
    if len(waiting) > 4:
        lines.append(("machine", f"  and {len(waiting) - 4} more."))
    return lines


def _scoped_records(store, project_id, root):
    """The same worktree rule as `context`: a record bound to another root is not part of this brief."""
    from cairnmind.memory_paths import bound_metadata, memory_bindings
    from cairnmind.retrieval import Retriever
    roots = [Path(row["path"]).resolve() for row in store.query(
        "SELECT path FROM project_roots WHERE project_id=:project", {"project": project_id})]
    bindings = memory_bindings(store, project_id)
    return [record for record in store.list_records(project_id) if Retriever._matches_scope(
        bound_metadata(store, project_id, record.get("metadata"), bindings=bindings), root, roots)]


def build_brief(store, project_id, worktree=None, *, now=None):
    """Return the brief as marked lines plus ready text; reads only, asks no model."""
    from cairnmind.plans import PlanManager
    from cairnmind.verification import _root
    project = store.resolve_project(project_id)
    root = _root(store, project["id"], worktree)
    now = now or datetime.now(timezone.utc)
    records = _scoped_records(store, project["id"], root)
    tasks = [r for r in records if r["kind"] == "task"]
    saved, lines = _checkpoint(store, records, now)
    lines += _plan(PlanManager(store), project["id"], str(root), {task["id"]: task for task in tasks})
    lines += _verification(store, tasks, str(root))
    lines += _commits(root, saved)
    lines += _proposed(records)
    head = [f"Brief for {_line(project['name'], 60)} in {_line(root, 120)}", _LEGEND]
    budget = MAX_LINES - len(head)
    if len(lines) > budget:
        lines = lines[:budget - 1] + [("machine", f"{len(lines) - budget + 1} more lines were left out to keep the brief short.")]
    marked = [{"basis": basis, "text": text} for basis, text in lines]
    text = "\n".join(head + [f"{'[' + line['basis'] + ']':<10} {line['text']}" for line in marked])
    return {"project": {"id": project["id"], "name": project["name"]}, "worktree": str(root),
            "generated_at": now.isoformat(timespec="seconds"), "lines": marked, "text": text}
