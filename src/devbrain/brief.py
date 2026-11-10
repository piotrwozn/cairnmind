"""A short human brief of where work stopped, built only from stored data.

No model is asked. Every line says where it comes from: ``machine`` lines are
measured or computed now by Cairnmind, ``reported`` lines repeat what an agent
or a person stored and were not verified.
"""
from __future__ import annotations

from datetime import datetime, timezone

from devbrain.store import DERIVED_RECORD_METADATA
from devbrain.terminal_safe import safe_text

MAX_LINES = 40
ACTIVE_STATES = ("in_progress", "needs_test", "needs_review")
_CHECKPOINT_KEYS = ("summary", "done", "completed", "in_progress", "next", "next_steps", "blockers", "notes")
_LEGEND = "machine = measured now by Cairnmind; reported = stored claim, not verified"


def _line(value, limit=160):
    text = " ".join(safe_text(value).split())
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


def _checkpoint(records, now):
    checkpoint = next((r for r in records if r["kind"] == "checkpoint" and r.get("key") == "latest"), None)
    if checkpoint is None:
        return None, [("machine", "No checkpoint has been saved for this project.")]
    saved = _time(checkpoint.get("updated_at"))
    when = f"{_age(saved, now)} ({saved.isoformat(timespec='seconds')})" if saved else "at an unknown time"
    lines = [("machine", f"Latest checkpoint saved {when}.")]
    state = checkpoint.get("metadata") if isinstance(checkpoint.get("metadata"), dict) else {}
    # Fields the store derives itself are not part of what the agent reported.
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
    lines = [("machine", f"Active plan: {_line(plan['title'], 90)} (version {plan['version']}, "
                         f"{len(plan['step_ids'])} steps).")]
    if task is not None:
        lines.append(("machine", f"Next task ({_line(result['reason'], 30)}): {_title(task)}"))
    elif result["reason"] == "completed":
        lines.append(("machine", "Every step of the active plan is done."))
    else:
        lines.append(("machine", "No task can start: every remaining step is blocked."))
    blocked = result.get("blocked", [])
    for entry in blocked[:4]:
        reasons = ", ".join(_line(reason, 40).replace("_", " ") for reason in entry["reasons"])
        blocked_task = by_id.get(entry["task_id"])
        lines.append(("machine", f"Blocked: {_title(blocked_task) if blocked_task else _line(entry['task_id'])} "
                                 f"({reasons})"))
    if len(blocked) > 4:
        lines.append(("machine", f"{len(blocked) - 4} more blocked steps."))
    return lines


def _verification(store, tasks, root):
    from devbrain.verification import verification_status
    active = [task for task in tasks if task["metadata"].get("state", "todo") in ACTIVE_STATES]
    if not active:
        return [("machine", "No task is in progress, waiting for a test or waiting for a review.")]
    lines = []
    for task in active[:6]:
        status = verification_status(store, task, root)
        reason = f": {_line(status['reasons'][0], 100)}" if status.get("reasons") else ""
        # "reported" is a claim carried from an agent or another machine, never a measurement.
        basis = "reported" if status["status"] == "reported" else "machine"
        lines.append((basis, f"Checks for \"{_title(task)}\" [{_line(task['metadata'].get('state', 'todo'), 20)}]: "
                             f"{_line(status['status'], 20)}{reason}"))
    if len(active) > 6:
        lines.append(("machine", f"{len(active) - 6} more tasks in progress are not listed."))
    return lines


def _commits(root, saved):
    from devbrain.ingest import _git
    if saved is None:
        return []
    if not _git(root, "rev-parse", "--git-dir"):
        return [("machine", "This worktree is not a Git repository, so commits cannot be listed.")]
    # Git compares whole seconds; a commit made in the checkpoint's own second came before it.
    since = f"--since={int(saved.timestamp()) + 1}"
    subjects = [line for line in _git(root, "log", since, "--format=%h %s", "-n", "200").splitlines() if line.strip()]
    if not subjects:
        return [("machine", "No commits since the checkpoint was saved.")]
    count = "200 or more" if len(subjects) == 200 else str(len(subjects))
    lines = [("machine", f"{count} commit{'' if len(subjects) == 1 else 's'} since the checkpoint was saved:")]
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


def build_brief(store, project_id, worktree=None, *, now=None):
    """Return the brief as marked lines plus ready text; reads only, asks no model."""
    from devbrain.plans import PlanManager
    from devbrain.verification import _root
    project = store.resolve_project(project_id)
    root = _root(store, project["id"], worktree)
    now = now or datetime.now(timezone.utc)
    records = store.list_records(project["id"])
    tasks = [r for r in records if r["kind"] == "task"]
    saved, lines = _checkpoint(records, now)
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
