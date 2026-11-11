"""A plan read as a contract: each acceptance criterion with the check that proves it.

``build_contract`` lists the criteria of a plan's steps with how each one stands, and works
out which check profiles the plan refers to but the worktree does not declare. For those it
prepares the text to add to the profile file, from commands that were proposed with the
criteria, and a short code that names exactly that proposal. ``write_contract`` adds the
text only when it is given the code of the proposal a person has looked at.

Nothing here runs a command. A proposed command is text, shown as a diff; it becomes a
profile only when a person asks for the file to be written, and it runs only when that
person later runs the check. A profile that is already declared is never changed, and the
file is replaced only by a text that the profile reader accepts with every earlier profile
unchanged.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib

from cairnmind.brief import _line, _title
from cairnmind.plans import PlanManager, _digest
from cairnmind.store import _PROFILE_NAME, _one, valid_argv
from cairnmind.verification import _PROFILE_PATHS, _root, criteria_status, load_profiles

_SUMMARY = {"machine_fresh": "proven by a machine run", "machine_stale": "its check must be run again",
            "machine_missing": "its check has not been run here", "machine_unknown": "its check cannot be evaluated",
            "manual_reported": "reported as accepted, not machine-checked", "manual_pending": "waits for a manual assessment"}
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


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


def _quote(value: str) -> str:
    """A TOML basic string: every control character escaped, everything else written as it is."""
    escaped = []
    for char in value:
        code = ord(char)
        if char in '\\"':
            escaped.append("\\" + char)
        elif code < 0x20 or code == 0x7F:
            escaped.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            raise ValueError("A proposed command contains text that cannot be written to the profile file")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def _block(name: str, argv: list[str], eol: str = "\n") -> str:
    key = name if _BARE_KEY.fullmatch(name) else _quote(name)
    return f"[checks.{key}]{eol}argv = [{', '.join(_quote(part) for part in argv)}]{eol}"


def _read(path: Path) -> str:
    """The file exactly as it is: no line ending is translated on the way in or out."""
    if not path.exists():
        return ""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _appended(current: str, additions: dict) -> str:
    eol = "\r\n" if "\r\n" in current else "\n"
    text = current if not current or current.endswith("\n") else current + eol
    for name, argv in additions.items():
        text += (eol if text.strip() else "") + _block(name, argv, eol)
    return text


def _acceptable(current: str, updated: str, additions: dict) -> bool:
    """Whether the profile reader would see every earlier profile unchanged and exactly the additions beside them."""
    try:
        before = tomllib.loads(current).get("checks", {}) if current.strip() else {}
        after = tomllib.loads(updated).get("checks", {})
    except (tomllib.TOMLDecodeError, AttributeError):
        return False
    return (isinstance(before, dict) and isinstance(after, dict)
            and after == {**before, **{name: {"argv": argv} for name, argv in additions.items()}}
            and not set(before) & set(additions))


def build_contract(store, project_id, plan_id=None, worktree=None, *, written=False):
    """The criteria of a plan with their state, and the profile text that could be added. Read-only."""
    project = store.resolve_project(project_id)
    root = _root(store, project["id"], worktree)
    selected = _plan(store, project["id"], plan_id, root)
    declared = load_profiles(store, project["id"], root)  # raises when two files declare profiles
    path = _profile_file(root)
    current = _read(path)
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
        proposals = metadata.get("check_proposals")
        for name, argv in (proposals.items() if isinstance(proposals, dict) else ()):
            proposed.setdefault(name, []).append(argv)
        steps.append({"task_id": task["id"], "step_key": metadata.get("step_key"), "title": _title(task),
                      "state": metadata.get("state", "todo"), "criteria": rows})
    additions, undecided = {}, []
    for name in sorted(wanted):
        commands = proposed.get(name, [])
        distinct = [argv for index, argv in enumerate(commands) if argv not in commands[:index]]
        # Whatever a record carries, only a name and a command the store itself would accept are offered.
        if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name) or not all(valid_argv(argv) for argv in distinct):
            undecided.append({"check": name, "reason": "the proposed name or command is not valid", "proposals": []})
        elif len(distinct) == 1:
            additions[name] = distinct[0]
        else:
            undecided.append({"check": name, "reason": "no command was proposed" if not distinct
                              else "different commands were proposed", "proposals": distinct})
    updated = _appended(current, additions) if additions else current
    if additions and not _acceptable(current, updated, additions):
        # For example profiles written as one inline table: text cannot simply be added below them.
        undecided += [{"check": name, "reason": "it cannot be added to the profile file as that file is written",
                       "proposals": [argv]} for name, argv in additions.items()]
        additions, updated = {}, current
    relative = path.relative_to(root).as_posix()
    diff = "".join(difflib.unified_diff(current.splitlines(keepends=True), updated.splitlines(keepends=True),
                                        fromfile=f"a/{relative}", tofile=f"b/{relative}")) if additions else ""
    code = hashlib.sha256(json.dumps([relative, current, additions], sort_keys=True).encode()).hexdigest()[:12] if additions else None
    lines = [f"Contract of plan {_line(selected['plan'].get('title', ''), 80)} ({_line(selected['plan']['id'], 60)}) "
             f"in {_line(root)}",
             "Each criterion with the check that proves it. Nothing was run"
             + ("." if written else " and nothing was written.")]
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
                      "you run the check.",
                  "\n".join(_line(line, 400) if not line.startswith(("+", "-", " ", "@")) else line[0] + _line(line[1:], 400)
                            for line in diff.rstrip("\n").split("\n")),
                  "", f"To add exactly these profiles: cairnmind plan contract {_line(project['name'], 60)} "
                      f"--plan-id {_line(selected['plan']['id'], 60)} --write {code}",
                  "Adding them changes the worktree, so check runs made before count as stale and must be run again."]
    for row in undecided:
        lines += ["", f"Check {_line(row['check'], 64)} is not declared in {_line(relative)}: {row['reason']}. "
                      "Declare it yourself."]
    return {"project": {"id": project["id"], "name": project["name"]}, "worktree": str(root),
            "plan": {"id": selected["plan"]["id"], "title": selected["plan"].get("title"),
                     "version": selected["plan"].get("version")},
            "steps": steps, "counts": counts, "profile_file": str(path), "additions": additions,
            "undecided": undecided, "diff": diff, "write_code": code, "updated_text": updated if additions else None,
            "text": "\n".join(lines)}


def write_contract(store, project_id, code, plan_id=None, worktree=None):
    """Add the proposed profiles named by ``code`` to the profile file. Runs nothing; changes no declared profile."""
    contract = build_contract(store, project_id, plan_id, worktree)
    if not contract["additions"]:
        raise ValueError("There is no proposed profile to add; run the command without --write to see the contract")
    if code != contract["write_code"]:
        raise ValueError("The proposal changed since it was shown, or the code is not the one printed with it; "
                         "look at the diff again")
    path, root = Path(contract["profile_file"]), Path(contract["worktree"])
    folder = path.parent
    if folder.is_symlink() or path.is_symlink() or (folder.exists() and not folder.is_dir()) \
            or not folder.resolve().is_relative_to(root):
        raise ValueError("Check profile configuration must be a regular file in a directory inside its worktree")
    folder.mkdir(exist_ok=True)
    expected = contract["updated_text"]
    if not expected.startswith(_read(path)):
        raise ValueError("The profile file changed while the contract was prepared; look at the diff again")
    descriptor, name = tempfile.mkstemp(prefix=".cairnmind-checks-", dir=folder)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(expected)
        os.chmod(name, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    after = build_contract(store, project_id, plan_id, worktree, written=True)
    return {**after, "written": True, "additions": contract["additions"], "diff": contract["diff"],
            "text": after["text"] + f"\n\nWritten to {_line(path)}: {', '.join(_line(name, 64) for name in contract['additions'])}. "
                                    "No command was run. Check runs made before this change count as stale."}
