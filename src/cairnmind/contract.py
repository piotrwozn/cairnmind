"""A plan read as a contract: each acceptance criterion with the check that proves it.

``build_contract`` lists the criteria of a plan's steps with how each one stands, and works
out which check profiles the plan refers to but the worktree does not declare. For those it
prepares the text to add to the profile file, from commands that were proposed with the
criteria. ``write_contract`` adds that text and nothing else.

Nothing here runs a command. A proposed command is text, shown as a diff; it becomes a
profile only when a person asks for the file to be written, and it runs only when that
person later runs the check. A profile that is already declared is never changed.
"""
from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import tempfile

from cairnmind.brief import _line, _title
from cairnmind.plans import PlanManager, _digest
from cairnmind.store import _one
from cairnmind.verification import _PROFILE_PATHS, _root, criteria_status, load_profiles

_SUMMARY = {"machine_fresh": "proven by a machine run", "machine_stale": "its check must be run again",
            "machine_missing": "its check has not been run here", "machine_unknown": "its check cannot be evaluated",
            "manual_reported": "reported as accepted, not machine-checked", "manual_pending": "waits for a manual assessment"}


def _plan(store, project_id, plan_id, root):
    if plan_id is None:
        state = _one(store, "SELECT active_plan_id FROM worktree_states WHERE id=:id",
                     {"id": _digest([project_id, str(root)])}, False)
        plan_id = (state or {}).get("active_plan_id")
        if not plan_id:
            raise ValueError("No plan is active in this worktree; name one with --plan-id")
    return PlanManager(store).get(project_id, plan_id)


def _profile_file(root: Path) -> Path:
    """The file profiles are declared in: the one that exists, else the current location."""
    present = [root / path for path in _PROFILE_PATHS if (root / path).exists()]
    return present[0] if present else root / _PROFILE_PATHS[0]


def _block(name: str, argv: list[str]) -> str:
    # JSON strings are valid TOML basic strings and escape every control character.
    return f"[checks.{json.dumps(name) if not name.replace('_', '').replace('-', '').isalnum() else name}]\nargv = {json.dumps(argv)}\n"


def build_contract(store, project_id, plan_id=None, worktree=None):
    """The criteria of a plan with their state, and the profile text that could be added. Read-only."""
    project = store.resolve_project(project_id)
    root = _root(store, project["id"], worktree)
    selected = _plan(store, project["id"], plan_id, root)
    declared = load_profiles(store, project["id"], root)  # raises when two files declare profiles
    path = _profile_file(root)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    captured: dict = {}

    def capture(store_, project_, root_, extra):
        from cairnmind.verification import capture_code_state
        key = tuple(extra or ())
        if key not in captured:
            captured[key] = capture_code_state(store_, project_, root_, extra)
        return captured[key]

    steps, counts, wanted, proposed = [], {}, {}, {}
    for task in selected["tasks"]:
        metadata = task.get("metadata", {})
        if task.get("status") == "superseded" or metadata.get("plan_archived"):
            continue
        rows = criteria_status(store, task, root, capture=capture)
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            if row["check"] and row["check"] not in declared:
                wanted.setdefault(row["check"], []).append(task["id"])
        for name, argv in (metadata.get("check_proposals") or {}).items():
            proposed.setdefault(name, []).append(argv)
        steps.append({"task_id": task["id"], "step_key": metadata.get("step_key"), "title": _title(task),
                      "state": metadata.get("state", "todo"), "criteria": rows})
    additions, undecided = {}, []
    for name in sorted(wanted):
        commands = [argv for argv in proposed.get(name, [])]
        distinct = [argv for index, argv in enumerate(commands) if argv not in commands[:index]]
        if len(distinct) == 1:
            additions[name] = distinct[0]
        else:
            undecided.append({"check": name, "reason": "no command was proposed" if not distinct
                              else "different commands were proposed", "proposals": distinct})
    addition = "".join(("\n" if current.strip() or index else "") + _block(name, argv)
                       for index, (name, argv) in enumerate(additions.items()))
    updated = (current if not current or current.endswith("\n") else current + "\n") + addition if addition else current
    relative = path.relative_to(root).as_posix()
    diff = "".join(difflib.unified_diff(current.splitlines(keepends=True), updated.splitlines(keepends=True),
                                        fromfile=f"a/{relative}", tofile=f"b/{relative}")) if addition else ""
    lines = [f"Contract of plan {_line(selected['plan'].get('title', ''), 80)} ({_line(selected['plan']['id'], 60)}) "
             f"in {_line(root)}",
             "Each criterion with the check that proves it. Nothing was run and nothing was written."]
    for step in steps:
        if not step["criteria"]:
            continue
        lines += ["", f"[{_line(step['state'], 30)}] {step['title']} ({_line(step['task_id'], 60)})"]
        for row in step["criteria"]:
            link = f"check {_line(row['check'], 64)}" if row["check"] else "no check"
            reason = f" ({_line(row['reason'], 120)})" if row.get("reason") else ""
            lines.append(f"  - {row['status']}: {_line(row['text'], 160)} [{link}]{reason}")
    if not any(step["criteria"] for step in steps):
        lines.append("No step of this plan declares an acceptance criterion.")
    else:
        lines += ["", "; ".join(f"{count} {state} ({_SUMMARY[state]})" for state, count in sorted(counts.items()))]
    if additions:
        lines += ["", f"Proposed additions to {_line(relative)}. Review each command: it runs with your rights when "
                      "you run the check.", diff.rstrip("\n")]
    for row in undecided:
        lines += ["", f"Check {_line(row['check'], 64)} is not declared in {_line(relative)}: {row['reason']}. "
                      "Declare it yourself."]
    return {"project": {"id": project["id"], "name": project["name"]}, "worktree": str(root),
            "plan": {"id": selected["plan"]["id"], "title": selected["plan"].get("title"),
                     "version": selected["plan"].get("version")},
            "steps": steps, "counts": counts, "profile_file": str(path), "additions": additions,
            "undecided": undecided, "diff": diff, "updated_text": updated if addition else None,
            "text": "\n".join(lines)}


def write_contract(store, project_id, plan_id=None, worktree=None):
    """Add the proposed profiles to the profile file. Runs nothing; changes no declared profile."""
    contract = build_contract(store, project_id, plan_id, worktree)
    if not contract["additions"]:
        return {**contract, "written": False}
    path, root = Path(contract["profile_file"]), Path(contract["worktree"])
    folder = path.parent
    if folder.is_symlink() or path.is_symlink() or not folder.resolve().is_relative_to(root):
        raise ValueError("Check profile configuration must stay inside its worktree")
    folder.mkdir(exist_ok=True)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    expected = contract["updated_text"]
    if not expected.startswith(before):
        raise ValueError("The profile file changed while the contract was prepared; look at the diff again")
    descriptor, name = tempfile.mkstemp(prefix=".cairnmind-checks-", dir=folder)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(expected)
        if path.exists():
            os.chmod(name, path.stat().st_mode & 0o777)
        else:
            os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    # What was written must be what the reader accepts, with every earlier profile as it was.
    load_profiles(store, contract["project"]["id"], root)
    return {**contract, "written": True,
            "text": contract["text"] + f"\n\nWritten to {_line(path)}. No command was run."}
