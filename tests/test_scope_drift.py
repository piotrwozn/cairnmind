"""Scope drift between a task and the files that changed (issue #11): a warning, never a block."""
from __future__ import annotations

import json
import stat
import subprocess

import pytest
from typer.testing import CliRunner

from cairnmind.cli import app
from cairnmind.config import Settings
from cairnmind.memory import MemoryManager
from cairnmind.plans import PlanManager
from cairnmind.scope import drift_warning, record_baseline, scope_drift
from cairnmind.store import Store, _clean
from cairnmind.verification import task_definition_hash, verification_status


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    (root / "src/auth").mkdir(parents=True)
    (root / "src/billing").mkdir()
    (root / "docs").mkdir()
    (root / "src/auth/login.py").write_text("def login():\n    return True\n")
    (root / "src/auth/tokens.py").write_text("TTL = 30\n")
    (root / "src/billing/invoice.py").write_text("RATE = 1\n")
    (root / "docs/login.md").write_text("# Login\n")
    (root / ".gitignore").write_text("cache/\n")
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    store = Store(tmp_path / "memory")
    project = store.add_project("drift", root)
    return store, project["id"], root, Settings.load(store.data_dir)


def scoped_plan(store, pid, root, scope=("src/auth/**",), key="scoped"):
    saved = PlanManager(store).save(pid, {"title": "Harden login", "goal": "g"}, [
        {"key": "auth", "content": "Change the login", "metadata": {"scope": list(scope)}},
        {"key": "other", "content": "Unscoped step"}], key, worktree=str(root), activate=True)
    return saved["plan"], next(task for task in saved["tasks"] if task["metadata"]["step_key"] == "auth")


def start(store, pid, root, saved, task):
    plan = PlanManager(store).get(pid, saved["id"])["plan"]
    return store.remember(pid, {"id": task["id"], "expected_version": task["version"], "expected_plan_version": plan["version"],
                                "metadata": {"state": "in_progress"}, "worktree": str(root)}, task["source_ids"])


def test_a_file_edited_outside_the_scope_is_named(workspace):
    """The acceptance scenario of the issue."""
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)
    assert scope_drift(store, task, root)["status"] == "in_scope"
    (root / "src/auth/login.py").write_text("def login():\n    return False\n")
    assert scope_drift(store, task, root)["status"] == "in_scope"
    (root / "src/billing/invoice.py").write_text("RATE = 2\n")
    drift = scope_drift(store, task, root)
    assert (drift["status"], drift["out_of_scope"], drift["changed"]) == ("out_of_scope", ["src/billing/invoice.py"], 2)
    assert drift_warning(drift) == ("1 changed file outside the task's scope: src/billing/invoice.py. "
                                    "This is a warning, not a block.")


def test_added_deleted_and_committed_changes_count_and_ignored_files_do_not(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root, scope=("src/auth/", "docs/login.md"))
    task = start(store, pid, root, saved, task)
    (root / "src/auth/new.py").write_text("NEW = 1\n")          # added, in scope
    (root / "docs/login.md").write_text("# Login, changed\n")    # edited, in scope
    (root / "docs/other.md").write_text("# Other\n")             # added, out of scope
    (root / "src/billing/invoice.py").unlink()                   # deleted, out of scope
    (root / "cache").mkdir()
    (root / "cache/tmp.bin").write_text("ignored by Git: not seen")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "work")                           # committing changes nothing about what changed
    drift = scope_drift(store, task, root)
    assert drift["out_of_scope"] == ["docs/other.md", "src/billing/invoice.py"] and drift["changed"] == 4


def test_a_file_that_was_already_modified_when_the_task_started_is_not_reported(workspace):
    store, pid, root, settings = workspace
    (root / "src/billing/invoice.py").write_text("RATE = 5  # somebody's unfinished edit\n")
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)
    assert scope_drift(store, task, root)["status"] == "in_scope"
    (root / "src/billing/invoice.py").write_text("RATE = 6\n")
    assert scope_drift(store, task, root)["out_of_scope"] == ["src/billing/invoice.py"]


def test_without_a_note_of_the_start_the_answer_is_unknown_never_in_scope(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    drift = scope_drift(store, task, root)  # the task has not started
    assert drift["status"] == "unknown" and "was not noted when the task started" in drift["reasons"][0]
    assert drift_warning(drift).startswith("Scope drift is unknown:")
    unscoped = next(row for row in PlanManager(store).get(pid, saved["id"])["tasks"] if row["metadata"]["step_key"] == "other")
    assert scope_drift(store, unscoped, root) is None and drift_warning(None) is None
    assert "scope_drift" not in verification_status(store, unscoped, root)


def test_the_note_is_taken_once_when_the_task_enters_in_progress(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    folder = store.data_dir / "scope-baselines"
    assert not folder.exists()
    task = start(store, pid, root, saved, task)
    notes = list(folder.glob("*.json"))
    assert len(notes) == 1 and stat.S_IMODE(notes[0].stat().st_mode) == 0o600
    taken = notes[0].read_text()
    (root / "src/billing/invoice.py").write_text("RATE = 2\n")
    plan = PlanManager(store).get(pid, saved["id"])["plan"]
    # A later progress write that stays in_progress does not move the starting point.
    task = store.remember(pid, {"id": task["id"], "expected_version": task["version"], "expected_plan_version": plan["version"],
                                "metadata": {"state": "in_progress", "note": "still working"}, "worktree": str(root)},
                          task["source_ids"])
    assert notes[0].read_text() == taken
    assert scope_drift(store, task, root)["out_of_scope"] == ["src/billing/invoice.py"]


def test_the_note_stays_on_this_machine(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)
    note = next((store.data_dir / "scope-baselines").glob("*.json")).read_text()
    digest = json.loads(note)["paths"]["src/auth/login.py"]
    MemoryManager(store).snapshot(pid)
    exported = "".join(path.read_text() for path in (root / ".memory").rglob("*.json"))
    assert digest not in exported and "scope-baselines" not in exported
    assert '"scope": ["src/auth/**"]' in exported or '"scope":["src/auth/**"]' in exported  # the declared scope travels
    stored = json.dumps([_clean(row) for name in ("records", "record_revisions", "plan_revisions", "nodes", "worktree_states")
                         for row in store.query(f"SELECT FROM {name}")], default=str)
    assert digest not in stored


def test_drift_is_a_warning_that_changes_no_record_and_blocks_nothing(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)
    (root / "src/billing/invoice.py").write_text("RATE = 2\n")

    def rows():
        return sorted(json.dumps(_clean(row), sort_keys=True, default=str)
                      for name in ("records", "record_revisions", "plans", "plan_revisions", "check_runs")
                      for row in store.query(f"SELECT FROM {name}"))

    before = rows()
    status = verification_status(store, task, root)
    assert status["scope_drift"]["status"] == "out_of_scope" and status["status"] == "unknown"  # the check result is its own
    assert rows() == before
    plan = PlanManager(store).get(pid, saved["id"])["plan"]
    done = store.remember(pid, {"id": task["id"], "expected_version": task["version"], "expected_plan_version": plan["version"],
                                "metadata": {"state": "done"}, "worktree": str(root)}, task["source_ids"])
    assert done["metadata"]["state"] == "done"  # out of scope, and still completed


def test_scope_is_part_of_the_plan_but_not_of_what_a_check_proved(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    assert task_definition_hash(task) == task_definition_hash({**task, "metadata": {
        key: value for key, value in task["metadata"].items() if key != "scope"}})
    with pytest.raises(ValueError, match="must be changed through PlanManager"):
        store.remember(pid, {"id": task["id"], "expected_version": task["version"], "expected_plan_version": saved["version"],
                             "metadata": {"scope": ["**"]}}, task["source_ids"])


@pytest.mark.parametrize("scope", [["/etc/passwd"], ["../outside/**"], ["src/.git/config"], ["!src/auth/**"], [""], "src/**",
                                   ["a\nb"], ["C:\\\\x"], ["x"] * 65, [3]])
def test_a_malformed_scope_is_refused(workspace, scope):
    store, pid, root, settings = workspace
    note = store.snapshot(pid, "note://x", "x", "note")
    with pytest.raises(ValueError, match="scope"):
        store.remember(pid, {"kind": "task", "content": "T", "metadata": {"scope": scope}}, [note["id"]])


def test_a_note_that_cannot_be_trusted_gives_unknown(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)
    note = next((store.data_dir / "scope-baselines").glob("*.json"))
    damaged = json.loads(note.read_text())
    note.write_text(json.dumps({**damaged, "task_id": "another-task"}))
    assert scope_drift(store, task, root)["status"] == "unknown"
    note.write_text("not json")
    assert scope_drift(store, task, root)["status"] == "unknown"
    note.write_text(json.dumps({**damaged, "status": "unknown", "unknowns": ["unmerged Git input: x"]}))
    drift = scope_drift(store, task, root)
    assert drift["status"] == "unknown" and drift["reasons"] == ["unmerged Git input: x"]


def test_an_unmanaged_task_and_a_direct_note(workspace):
    store, pid, root, settings = workspace
    note = store.snapshot(pid, "note://x", "x", "note")
    task = store.remember(pid, {"kind": "task", "content": "Standalone", "metadata": {
        "state": "in_progress", "scope": ["docs/**"]}}, [note["id"]])  # created already in progress, in the project root
    (root / "src/auth/login.py").write_text("changed\n")
    assert scope_drift(store, task)["out_of_scope"] == ["src/auth/login.py"]
    assert record_baseline(store, {**task, "metadata": {}}, root) is False


def test_plan_next_and_the_model_view_carry_the_warning(workspace, monkeypatch):
    from cairnmind.mcp_server import _next_task_view

    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    start(store, pid, root, saved, task)
    (root / "src/billing/invoice.py").write_text("RATE = 2\n")
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    shown = CliRunner().invoke(app, ["--data-dir", str(store.data_dir), "plan", "next", "drift"])
    assert shown.exit_code == 0, shown.output
    result = json.loads(shown.output)
    assert result["warnings"] == ["1 changed file outside the task's scope: src/billing/invoice.py. This is a warning, not a block."]
    assert result["verification"]["scope_drift"]["out_of_scope"] == ["src/billing/invoice.py"]
    view = _next_task_view({**PlanManager(store).next_task(pid, None, str(root)), "warnings": result["warnings"]})
    assert view["warnings"] == result["warnings"] and view["task"]["metadata"]["scope"] == ["src/auth/**"]
    assert "warnings" not in _next_task_view(PlanManager(store).next_task(pid, None, str(root)))
