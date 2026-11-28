"""A short human brief of where work stopped, built only from stored data.

No model is asked. Every line below the title and the legend says where it comes
from: ``machine`` lines are measured or computed now by Cairnmind, ``reported``
lines repeat what an agent or a person stored and were not verified. Stored
titles never stand in the middle of a measured statement: on a ``machine`` line
they come last, after a ``|``.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairnmind.cli_cmds.first_start import BRIEF_REVIEW, BRIEF_START
from cairnmind.pal.terminal_safe import _line
from cairnmind.storage.domain_types import MemoryRecord, ProjectRecord, TaskRecord
from cairnmind.storage.storage_query import db_find
from cairnmind.store import DERIVED_RECORD_METADATA, degraded_origin

MAX_LINES = 40
ACTIVE_STATES = ("in_progress", "needs_test", "needs_review")
_CHECKPOINT_KEYS = ("summary", "done", "completed", "in_progress", "next", "next_steps", "blockers", "notes")
# One part for each mark the printed lines use (#326); the separator is the " | " that precedes a stored title.
_LEGEND = {"machine": "[machine] = measured now by Cairnmind", "reported": "[reported] = stored by an assistant or a "
           "person, not verified", "|": "text after | is a stored title"}


def _title(record: MemoryRecord | Mapping[str, Any]) -> str:
    return _line(str(record.get("content", "")).strip().split("\n", 1)[0], 90)


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _age(then: datetime, now: datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "less than a minute ago"


def _flat(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "; ".join(_flat(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_flat(item)}" for key, item in value.items())
    return str(value)


def _git_out(root: Path | str, *args: str) -> str | None:
    """Git's output, or None when Git could not be run or failed; a failure is never an empty answer."""
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=15, check=False,
                                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else None


def _checkpoint_saved(
    store: Any,
    checkpoint: MemoryRecord | Mapping[str, Any],
) -> tuple[datetime | None, str | None]:
    """When the latest checkpoint was saved, and who left it unverified: None, "external" (an external
    client's session) or "third_party" (the built-in agent after a third-party tool result)."""
    from cairnmind.memory.evidence import third_party_origin

    def cause(metadata: Any) -> str | None:
        if not degraded_origin(metadata):
            return None
        return "third_party" if third_party_origin(metadata) else "external"
    # A later edit of the record (a review, for example) moves updated_at; the snapshot
    # written by the checkpoint itself keeps the time the checkpoint was really saved.
    times: list[tuple[datetime, str | None]] = []
    for source_id in checkpoint.get("source_ids") or []:
        try:
            source = store.get_source(source_id)
        except ValueError:
            continue
        created = _time(source.get("created_at"))
        if str(source.get("uri", "")).startswith("checkpoint://") and created is not None:
            times.append((created, cause(source.get("metadata"))))
    if not times:
        return _time(checkpoint.get("updated_at")), cause(checkpoint.get("metadata"))
    return max(times, key=lambda item: item[0])


def _checkpoint(
    store: Any,
    records: Sequence[MemoryRecord | Mapping[str, Any]],
    now: datetime,
) -> tuple[datetime | None, list[tuple[str, str]]]:
    checkpoint = next((r for r in records if r["kind"] == "checkpoint" and r.get("key") == "latest"), None)
    if checkpoint is None:
        return None, [("machine", "No checkpoint has been saved for this project.")]
    saved, external = _checkpoint_saved(store, checkpoint)
    when = f"{_age(saved, now)} ({saved.isoformat(timespec='seconds')})" if saved else "at an unknown time"
    lines = [("machine", f"Latest checkpoint saved {when}.")]
    if external == "external":
        lines.append(("machine", "It was saved by an external client's session (history unarchived, "
                                 "evidence degraded); nothing in it is verified."))
    elif external == "third_party":
        lines.append(("machine", "It was saved by the built-in agent after a third-party tool result in its "
                                 "session (evidence degraded); nothing in it is verified."))
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


def _plan(
    plans: Any,
    project_id: str,
    root: str,
    by_id: Mapping[str, TaskRecord | MemoryRecord | Mapping[str, Any]],
) -> list[tuple[str, str]]:
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


def _verification(
    store: Any,
    tasks: Sequence[TaskRecord | MemoryRecord | Mapping[str, Any]],
    root: str,
) -> list[tuple[str, str]]:
    from cairnmind.memory.verification import capture_code_state, verification_status
    active = [task for task in tasks if (task.get("metadata") or {}).get("state", "todo") in ACTIVE_STATES]
    if not active:
        return [("machine", "No task is in progress, waiting for a test or waiting for a review.")]
    observed: dict[tuple[str, ...], dict[str, Any]] = {}

    def capture(
        store: Any,
        project_id: str,
        worktree: Path | str | None = None,
        extra_inputs: Sequence[str] | None = None,
    ) -> dict[str, Any]:
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
        state = _line((task.get("metadata") or {}).get("state", "todo"), 20)
        lines.append((basis, f"Checks {_line(status['status'], 20)}{reason}, task state {state} | task: {_title(task)}"))
        from cairnmind.memory.scope import drift_warning
        warning = drift_warning(status.get("scope_drift"))
        if warning:
            lines.append(("machine", f"{warning} | task: {_title(task)}"))
    if len(active) > 6:
        lines.append(("machine", f"{len(active) - 6} more tasks in progress are not listed."))
    return lines


def _commits(root: Path | str, saved: datetime | None) -> list[tuple[str, str]]:
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


def _waiting(records: Sequence[MemoryRecord | Mapping[str, Any]]) -> list[MemoryRecord | Mapping[str, Any]]:
    # Tasks move through their own states and a checkpoint is not a finding, so neither waits for this review.
    return [r for r in records if r.get("status") == "proposed" and r["kind"] not in ("checkpoint", "task")]


def _next(
    project: ProjectRecord | Mapping[str, Any],
    records: Sequence[MemoryRecord | Mapping[str, Any]],
) -> str | None:
    """The next step for a person (#287): a first finding for an empty project, a review for waiting ones."""
    if not records:
        return BRIEF_START.format(project=shlex.quote(_line(project["name"], 60)))
    return BRIEF_REVIEW if _waiting(records) else None


def _proposed(records: Sequence[MemoryRecord | Mapping[str, Any]]) -> list[tuple[str, str]]:
    waiting = _waiting(records)
    if not waiting:
        return [("machine", "No proposed findings are waiting for review.")]
    lines = [("machine", f"{len(waiting)} proposed finding{'' if len(waiting) == 1 else 's'} waiting for review:")]
    lines += [("reported", f"  {r['kind']}: {_title(r)}") for r in waiting[:4]]
    if len(waiting) > 4:
        lines.append(("machine", f"  and {len(waiting) - 4} more."))
    return lines


def _scoped_records(store: Any, project_id: str, root: Path) -> list[dict[str, Any]]:
    """The same worktree rule as `context`: a record bound to another root is not part of this brief."""
    from cairnmind.agents.retrieval import Retriever
    from cairnmind.memory.memory_paths import bound_metadata, memory_bindings
    roots = [Path(row["path"]).resolve() for row in db_find(
        store, "project_roots", {"project_id": project_id}, columns=("path",))]
    bindings = memory_bindings(store, project_id)
    return [record for record in store.list_records(project_id) if Retriever._matches_scope(
        bound_metadata(store, project_id, record.get("metadata"), bindings=bindings), root, roots)]


def build_brief(
    store: Any,
    project_id: str,
    worktree: Path | str | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the brief as marked lines plus ready text; reads only, asks no model."""
    from cairnmind.memory.plans import PlanManager
    from cairnmind.memory.verification import _root
    project = store.resolve_project(project_id)
    root = _root(store, project["id"], worktree)
    now = now or datetime.now(UTC)
    records = _scoped_records(store, project["id"], root)
    tasks = [r for r in records if r["kind"] == "task"]
    saved, lines = _checkpoint(store, records, now)
    lines += _plan(PlanManager(store), project["id"], str(root), {task["id"]: task for task in tasks})
    lines += _verification(store, tasks, str(root))
    lines += _commits(root, saved)
    lines += _proposed(records)
    budget = MAX_LINES - 2
    if len(lines) > budget:
        lines = lines[:budget - 1] + [("machine", f"{len(lines) - budget + 1} more lines were left out to keep the brief short.")]
    used = {basis for basis, _ in lines} | {"|" for _, text in lines if " | task: " in text or " | plan: " in text}
    head = [f"Brief for {_line(project['name'], 60)} in {_line(root, 120)}",
            "; ".join(part for key, part in _LEGEND.items() if key in used)]
    marked = [{"basis": basis, "text": text} for basis, text in lines]
    text = "\n".join(head + [f"{'[' + line['basis'] + ']':<10} {line['text']}" for line in marked])
    return {"project": {"id": project["id"], "name": project["name"]}, "worktree": str(root),
            "generated_at": now.isoformat(timespec="seconds"), "lines": marked, "text": text,
            "next": _next(project, records)}
