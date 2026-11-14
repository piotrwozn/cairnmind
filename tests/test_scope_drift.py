"""Scope drift between a task and the files that changed (issue #11): a warning, never a block."""
from __future__ import annotations

import json
import os
import stat
import subprocess

import pytest
from typer.testing import CliRunner

from cairnmind.cli import app
from cairnmind.config import Settings
from cairnmind.memory import MemoryManager
from cairnmind.plans import PlanManager
from cairnmind.scope import drift_warning, record_baseline, scope_drift
import cairnmind.scope as scope_module
from cairnmind.store import Store
from cairnmind.sql import clean_row
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


def move(store, pid, root, saved, task, state):
    plan = PlanManager(store).get(pid, saved["id"])["plan"]
    return store.remember(pid, {"id": task["id"], "expected_version": task["version"], "expected_plan_version": plan["version"],
                                "metadata": {"state": state}, "worktree": str(root)}, task["source_ids"])


def start(store, pid, root, saved, task):
    return move(store, pid, root, saved, task, "in_progress")


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
    drift = scope_drift(store, task, root)  # the task has not started: nothing to say, and no warning
    assert drift["status"] == "not_started" and drift_warning(drift) is None
    task = start(store, pid, root, saved, task)
    next((store.data_dir / "scope-baselines").glob("*.json")).unlink()  # as on another machine
    drift = scope_drift(store, task, root)
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
    digest = json.loads(note)["paths"]["src/auth/login.py"][3]
    MemoryManager(store).snapshot(pid)
    exported = "".join(path.read_text() for path in (root / ".memory").rglob("*.json"))
    assert digest not in exported and "scope-baselines" not in exported
    assert '"scope": ["src/auth/**"]' in exported or '"scope":["src/auth/**"]' in exported  # the declared scope travels
    stored = json.dumps([clean_row(row) for name in ("records", "record_revisions", "plan_revisions", "nodes", "worktree_states")
                         for row in store.query(f"SELECT FROM {name}")], default=str)
    assert digest not in stored


def test_drift_is_a_warning_that_changes_no_record_and_blocks_nothing(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)
    (root / "src/billing/invoice.py").write_text("RATE = 2\n")

    def rows():
        return sorted(json.dumps(clean_row(row), sort_keys=True, default=str)
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
                                   ["a\nb"], ["C:\\\\x"], ["x"] * 65, [3], ["./src/**"], ["#src"], ["src/[z-a]/**"]])
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
    for paths in ({name: "0" * 24 for name in damaged["paths"]}, [], {"src/auth/login.py": [1, 2]}, None):
        note.write_text(json.dumps({**damaged, "paths": paths}))  # an earlier or a broken form
        assert scope_drift(store, task, root)["status"] == "unknown"
    note.write_text(json.dumps([1, 2]))
    assert scope_drift(store, task, root)["status"] == "unknown"
    assert verification_status(store, task, root)["scope_drift"]["status"] == "unknown"


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


def test_staging_or_committing_a_file_is_not_a_change(workspace):
    store, pid, root, settings = workspace
    (root / "src/billing/invoice.py").write_text("RATE = 5\n")  # modified and unstaged when the task starts
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)
    git(root, "add", "src/billing/invoice.py")
    assert scope_drift(store, task, root)["status"] == "in_scope"
    git(root, "commit", "-qm", "somebody else's work")
    drift = scope_drift(store, task, root)
    assert (drift["status"], drift["changed"]) == ("in_scope", 0)
    os.utime(root / "src/billing/invoice.py", ns=(1, 1))  # touched, same content
    assert scope_drift(store, task, root)["status"] == "in_scope"
    (root / "src/billing/invoice.py").chmod(0o755)
    assert scope_drift(store, task, root)["out_of_scope"] == ["src/billing/invoice.py"]


def test_an_unchanged_tree_is_compared_without_reading_a_file(workspace, monkeypatch):
    import cairnmind.verification as verification

    store, pid, root, settings = workspace
    for item in root.rglob("*"):
        if item.is_file() and ".git" not in item.parts:
            os.utime(item, (1000, 1000))  # written long ago: only such a file is trusted without being read
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)
    read = []
    real = verification._file_hash
    monkeypatch.setattr(verification, "_file_hash", lambda path: read.append(path) or real(path))
    assert scope_drift(store, task, root)["status"] == "in_scope" and read == []
    (root / "src/billing/invoice.py").write_text("RATE = 2\n")
    assert scope_drift(store, task, root)["status"] == "out_of_scope" and [item.name for item in read] == ["invoice.py"]
    # A file rewritten with the same content is read once, and its new times are then noted.
    (root / "docs/login.md").write_text("# Login\n")
    os.utime(root / "docs/login.md", (2000, 2000))
    del read[:]
    assert scope_drift(store, task, root)["changed"] == 1 and "login.md" in [item.name for item in read]
    del read[:]
    assert scope_drift(store, task, root)["changed"] == 1 and "login.md" not in [item.name for item in read]


def test_a_file_written_just_before_the_start_is_read_again(workspace, monkeypatch):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    (root / "src/billing/invoice.py").write_text("RATE = 1\n")  # written a moment before the task starts
    task = start(store, pid, root, saved, task)
    note = json.loads(next((store.data_dir / "scope-baselines").glob("*.json")).read_text())
    assert note["paths"]["src/billing/invoice.py"][1] == -1
    before = (root / "src/billing/invoice.py").stat()
    (root / "src/billing/invoice.py").write_text("RATE = 2\n")  # same size
    os.utime(root / "src/billing/invoice.py", ns=(before.st_atime_ns, before.st_mtime_ns))  # and the same time
    assert scope_drift(store, task, root)["out_of_scope"] == ["src/billing/invoice.py"]


def test_going_back_to_work_keeps_the_first_note(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)
    (root / "src/billing/invoice.py").write_text("RATE = 2\n")
    task = move(store, pid, root, saved, task, "needs_review")
    assert scope_drift(store, task, root)["status"] == "out_of_scope"
    task = start(store, pid, root, saved, task)
    assert scope_drift(store, task, root)["out_of_scope"] == ["src/billing/invoice.py"]


def test_a_finished_task_keeps_the_answer_it_had_when_it_was_done(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)
    (root / "src/auth/login.py").write_text("def login():\n    return False\n")
    task = move(store, pid, root, saved, task, "done")
    (root / "src/billing/invoice.py").write_text("RATE = 2\n")  # the next task's work
    drift = scope_drift(store, task, root)
    assert (drift["status"], drift["changed"], drift["out_of_scope"]) == ("in_scope", 1, []) and drift["finished_at"]
    assert drift_warning(drift) is None
    note = next((store.data_dir / "scope-baselines").glob("*.json"))
    assert json.loads(note.read_text())["paths"] == {} and note.stat().st_size < 2000  # only the answer is kept
    task = move(store, pid, root, saved, task, "needs_review")  # taken up again: a new start, from the tree as it is now
    assert scope_drift(store, task, root)["changed"] == 0
    (root / "docs/login.md").write_text("# changed\n")
    assert scope_drift(store, task, root)["out_of_scope"] == ["docs/login.md"]
    plan = PlanManager(store).get(pid, saved["id"])["plan"]
    task = store.remember(pid, {"id": task["id"], "expected_version": task["version"], "expected_plan_version": plan["version"],
                                "metadata": {"state": "done"}}, task["source_ids"])  # finished without naming the worktree
    drift = scope_drift(store, task, root)
    assert drift["out_of_scope"] == ["docs/login.md"] and drift["finished_at"]
    note.write_text(json.dumps({**json.loads(note.read_text()), "final": {"status": "in_scope"}}))
    assert scope_drift(store, task, root)["status"] == "unknown"  # a kept answer that is not whole
    note.unlink()
    drift = scope_drift(store, task, root)
    assert drift["status"] == "not_recorded" and drift_warning(drift) is None
    task = move(store, pid, root, saved, task, "todo")
    assert scope_drift(store, task, root)["status"] == "not_started"


def test_a_scope_declared_on_a_task_that_is_already_being_worked_on_starts_there(workspace):
    store, pid, root, settings = workspace
    note = store.snapshot(pid, "note://x", "x", "note")
    task = store.remember(pid, {"kind": "task", "content": "Running", "metadata": {"state": "needs_test"}}, [note["id"]])
    (root / "docs/login.md").write_text("# before the scope was declared\n")
    task = store.remember(pid, {"id": task["id"], "expected_version": task["version"],
                                "metadata": {"scope": ["src/**"]}}, [note["id"]])
    assert scope_drift(store, task)["status"] == "in_scope"
    (root / "docs/login.md").write_text("# after\n")
    assert scope_drift(store, task)["out_of_scope"] == ["docs/login.md"]


def test_a_file_that_only_became_ignored_or_stopped_being_ignored_is_not_a_change(workspace):
    store, pid, root, settings = workspace
    (root / "notes").mkdir()
    (root / "notes/todo.txt").write_text("untracked, not ignored\n")
    (root / "cache").mkdir()
    (root / "cache/old.bin").write_text("ignored when the task starts\n")
    saved, task = scoped_plan(store, pid, root, scope=("src/auth/**", ".gitignore"))
    task = start(store, pid, root, saved, task)
    (root / ".gitignore").write_text("notes/\n")  # notes/ becomes ignored, cache/ no longer is
    drift = scope_drift(store, task, root)
    assert (drift["status"], drift["changed"]) == ("in_scope", 1)
    (root / "cache/new.bin").write_text("made during the task\n")
    assert scope_drift(store, task, root)["out_of_scope"] == ["cache/new.bin"]


def test_a_repository_without_a_first_commit_is_compared_as_well(workspace, tmp_path):
    store, pid, root, settings = workspace
    fresh = tmp_path / "fresh"
    (fresh / "docs").mkdir(parents=True)
    (fresh / "docs/a.md").write_text("a\n")
    git(fresh, "init", "-q")
    other = store.add_project("fresh", fresh)["id"]
    note = store.snapshot(other, "note://x", "x", "note")
    task = store.remember(other, {"kind": "task", "content": "T", "metadata": {"state": "in_progress", "scope": ["src/**"]}},
                          [note["id"]])
    assert scope_drift(store, task)["status"] == "in_scope"
    (fresh / "docs/a.md").write_text("b\n")
    assert scope_drift(store, task)["out_of_scope"] == ["docs/a.md"]


def test_a_slow_note_does_not_hold_the_write_and_a_finish_meanwhile_closes_it(workspace, monkeypatch):
    import threading

    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    gate, real = threading.Event(), scope_module.path_states

    def slow(*args, **kwargs):
        if threading.current_thread().name == "cairnmind-scope-note" and not gate.is_set():
            gate.wait(20)
        return real(*args, **kwargs)
    monkeypatch.setattr(scope_module, "path_states", slow)
    monkeypatch.setattr(scope_module, "_WAIT_SECONDS", 0.2)
    task = start(store, pid, root, saved, task)  # returns although the tree has not been read yet
    assert scope_drift(store, task, root)["status"] == "unknown"
    task = move(store, pid, root, saved, task, "done")  # nothing to close yet
    gate.set()
    for thread in threading.enumerate():
        if thread.name == "cairnmind-scope-note":
            thread.join(20)
    drift = scope_drift(store, task, root)
    assert drift["status"] == "in_scope" and drift["finished_at"]


def test_files_cairnmind_writes_itself_are_not_drift(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)
    (root / ".cairnmind").mkdir()
    (root / ".cairnmind/checks.toml").write_text("[profiles.unit]\nargv = ['true']\n")
    (root / ".codex").mkdir()
    for name in ("AGENTS.md", "CLAUDE.md", ".mcp.json", ".codex/config.toml", "AGENTS.md.cairnmind-backup-1"):
        (root / name).write_text("written by an installation\n")
    assert scope_drift(store, task, root)["status"] == "in_scope"
    (root / ".codex/other.toml").write_text("mine\n")
    assert scope_drift(store, task, root)["out_of_scope"] == [".codex/other.toml"]


def test_a_file_name_that_is_not_text_is_shown_safely(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)
    (root / os.fsdecode(b"src/billing/\xff\x1b[31mred.py")).write_text("x\n")
    drift = scope_drift(store, task, root)
    assert drift["status"] == "out_of_scope" and drift["out_of_scope_total"] == 1
    json.dumps(drift).encode("utf-8")  # no lone surrogate
    shown = drift["out_of_scope"][0] + drift_warning(drift)
    assert "\x1b" not in shown and "\n" not in shown
    assert verification_status(store, task, root)["scope_drift"] == drift


def test_the_note_is_taken_only_after_the_write_is_committed(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    folder = store.data_dir / "scope-baselines"
    with pytest.raises(RuntimeError):
        with store.transaction():
            start(store, pid, root, saved, task)
            assert not folder.exists()
            raise RuntimeError("the write is abandoned")
    assert not folder.exists()
    with store.transaction():
        task = start(store, pid, root, saved, task)
        assert not folder.exists()  # the writer lock is still held here
    assert len(list(folder.glob("*.json"))) == 1


def test_a_task_that_already_carries_something_else_as_scope_stays_writable(workspace, monkeypatch):
    store, pid, root, settings = workspace
    note = store.snapshot(pid, "note://x", "x", "note")
    with monkeypatch.context() as patch:  # as written by a version that knew no rule for this name
        patch.setattr("cairnmind.store.validate_scope", lambda scope: None)
        task = store.remember(pid, {"kind": "task", "content": "Old", "metadata": {"scope": "the whole project"}}, [note["id"]])
    task = store.remember(pid, {"id": task["id"], "expected_version": task["version"],
                                "metadata": {"state": "in_progress"}}, [note["id"]])
    assert task["metadata"]["scope"] == "the whole project"
    assert scope_drift(store, task) is None and "scope_drift" not in verification_status(store, task)
    assert not (store.data_dir / "scope-baselines").exists()
    with pytest.raises(ValueError, match="scope"):
        store.remember(pid, {"id": task["id"], "expected_version": task["version"], "metadata": {"scope": "src"}}, [note["id"]])


def test_an_open_note_is_kept_however_old_and_leftovers_of_a_broken_write_are_removed(workspace):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)
    folder = store.data_dir / "scope-baselines"
    mine = next(folder.glob("*.json"))
    (folder / ".note-left").write_text("{")
    for item in folder.iterdir():
        os.utime(item, (1, 1))
    note = store.snapshot(pid, "note://x", "x", "note")
    store.remember(pid, {"kind": "task", "content": "Another", "metadata": {"state": "in_progress", "scope": ["docs/**"]}},
                   [note["id"]])
    assert mine.exists() and not (folder / ".note-left").exists() and len(list(folder.glob("*.json"))) == 2
    (root / "src/billing/invoice.py").write_text("RATE = 2\n")
    assert scope_drift(store, task, root)["out_of_scope"] == ["src/billing/invoice.py"]


def test_a_long_scope_fits_the_model_view_and_the_brief_and_contract_say_the_warning(workspace):
    from cairnmind.brief import _verification
    from cairnmind.contract import build_contract
    from cairnmind.mcp_server import _next_task_view

    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root, scope=["src/auth/**"] + [f"docs/{'x' * 480}{n}/**" for n in range(63)])
    shown = PlanManager(store).next_task(pid, None, str(root))
    shown["task"] = {**shown["task"], "metadata": {**shown["task"]["metadata"], "acceptance_criteria": ["first", "second"]}}
    view = _next_task_view(shown)
    assert len(json.dumps(view["task"]["metadata"])) <= 7000 and "scope" in view["task"]["metadata_truncated_fields"]
    assert view["task"]["metadata"]["acceptance_criteria"] == ["first", "second"]  # advice gives way first
    task = start(store, pid, root, saved, task)
    assert all("scope" not in line for _, line in _verification(store, [task], root))
    (root / "src/billing/invoice.py").write_text("RATE = 2\n")
    assert any("1 changed file outside the task's scope: src/billing/invoice.py" in line
               for _, line in _verification(store, [task], root))
    contract = build_contract(store, pid, worktree=str(root))
    assert "1 changed file outside the task's scope: src/billing/invoice.py" in contract["text"]


def test_a_worktree_that_cannot_be_read_gives_unknown(workspace, monkeypatch):
    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    task = start(store, pid, root, saved, task)

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 20)
    monkeypatch.setattr(scope_module.subprocess, "run", slow)
    drift = verification_status(store, task, root)["scope_drift"]
    assert drift["status"] == "unknown" and "cannot be read" in drift["reasons"][0]


def test_the_service_gives_the_warning_to_a_model_and_the_resume_packet_carries_the_scope(workspace):
    from fastapi.testclient import TestClient

    from cairnmind.mcp_server import _next_task_view
    from cairnmind.service import create_app

    class NoModel:
        async def models(self):
            return {"models": []}

    store, pid, root, settings = workspace
    saved, task = scoped_plan(store, pid, root)
    start(store, pid, root, saved, task)
    headers = {"Authorization": f"Bearer {settings.token}"}
    expected = ["1 changed file outside the task's scope: src/billing/invoice.py. This is a warning, not a block."]
    with TestClient(create_app(settings, start_worker=False, ollama=NoModel())) as client:
        body = {"project": pid, "worktree": str(root)}
        assert "warnings" not in client.post("/plans/next", json=body, headers=headers).json()
        (root / "src/billing/invoice.py").write_text("RATE = 2\n")
        result = client.post("/plans/next", json=body, headers=headers).json()
        assert result["warnings"] == expected and _next_task_view(result)["warnings"] == expected
        prepared = client.post("/sessions/prepare", json={**body, "client": "claude"}, headers=headers).json()
        assert prepared["packet"]["active_task"]["scope"] == ["src/auth/**"]
        long = PlanManager(store).save(pid, {"title": "Long", "goal": "the goal stays"}, [
            {"key": "a", "content": "Step", "metadata": {"scope": [f"docs/{'x' * 480}{n}/**" for n in range(64)]}}],
            "long", worktree=str(root), activate=True)
        prepared = client.post("/sessions/prepare", json={**body, "client": "claude"}, headers=headers).json()
        packet = prepared["packet"]
        assert packet["active_plan"]["id"] == long["plan"]["id"] and packet["active_plan"]["goal"] == "the goal stays"
        assert packet["active_task"]["scope_omitted"] == 64 and "scope" not in packet["active_task"]


def test_a_command_that_only_reads_leaves_the_note_as_it_is(workspace, monkeypatch):
    store, pid, root, settings = workspace
    for item in root.rglob("*"):
        if item.is_file() and ".git" not in item.parts:
            os.utime(item, (1000, 1000))
    saved, task = scoped_plan(store, pid, root)
    start(store, pid, root, saved, task)
    (root / "docs/login.md").write_text("# Login\n")  # the same content with new times: a store that writes notes them
    os.utime(root / "docs/login.md", (2000, 2000))
    note = next((store.data_dir / "scope-baselines").glob("*.json"))
    before = (note.read_bytes(), note.stat().st_mtime_ns)
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    shown = CliRunner().invoke(app, ["--data-dir", str(store.data_dir), "plan", "next", "drift"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["verification"]["scope_drift"]["status"] == "in_scope"
    assert (note.read_bytes(), note.stat().st_mtime_ns) == before
