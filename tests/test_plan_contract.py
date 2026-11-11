"""A plan as a contract: acceptance criteria linked to the checks that prove them (issue #13)."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from cairnmind.cli import app
from cairnmind.config import Settings
from cairnmind.contract import build_contract, write_contract
from cairnmind.plans import PlanManager
from cairnmind.store import Store, _clean
from cairnmind.verification import (criteria_status, load_profiles, run_check, task_definition_hash,
                                    verification_status)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "app.py").write_text("value = 1\n")
    (root / "passes.py").write_text("print('ok')\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    store = Store(tmp_path / "memory")
    project = store.add_project("contract", root)
    return store, project["id"], root, Settings.load(store.data_dir)


PASSES = [sys.executable, "passes.py"]
CRITERIA = [{"text": "Unit tests pass.", "check": "unit", "argv": PASSES},
            {"text": "The linter is clean.", "check": "lint", "argv": PASSES},
            "A reviewer read the migration notes."]


def plan(store, pid, root, criteria=CRITERIA, key="contract-1", **extra):
    saved = PlanManager(store).save(pid, {"title": "Ship login", "goal": "Login works"}, [
        {"key": "implement", "content": "Implement login", "metadata": {"acceptance_criteria": criteria}},
        {"key": "document", "content": "Document login"}], key, worktree=str(root), activate=True, **extra)
    return saved["plan"], next(task for task in saved["tasks"] if task["metadata"]["step_key"] == "implement")


def test_criteria_are_stored_as_text_with_the_link_beside_them(workspace):
    store, pid, root, settings = workspace
    _, task = plan(store, pid, root)
    metadata = task["metadata"]
    # The list every version reads stays a list of strings.
    assert metadata["acceptance_criteria"] == ["Unit tests pass.", "The linter is clean.", "A reviewer read the migration notes."]
    assert metadata["criterion_checks"] == {"Unit tests pass.": "unit", "The linter is clean.": "lint"}
    assert metadata["check_proposals"] == {"unit": PASSES, "lint": PASSES}
    unlinked = store.remember(pid, {"kind": "task", "content": "Plain", "metadata": {"acceptance_criteria": ["Works."]}},
                              task["source_ids"])
    assert "criterion_checks" not in unlinked["metadata"] and "check_proposals" not in unlinked["metadata"]


def test_the_definition_hash_of_a_task_without_links_is_what_it_always_was(workspace):
    store, pid, root, settings = workspace
    plain = {"content": "Plain", "metadata": {"acceptance_criteria": ["Works."], "required_profiles": ["unit"]}}
    before = task_definition_hash(plain)
    assert task_definition_hash({**plain, "metadata": {**plain["metadata"], "criterion_checks": {}}}) == before
    linked = task_definition_hash({**plain, "metadata": {**plain["metadata"], "criterion_checks": {"Works.": "unit"}}})
    assert linked != before  # which check proves a criterion is part of the definition


@pytest.mark.parametrize("criteria, message", [
    ([{"text": "A", "check": "unit", "extra": 1}], "A criterion is text, or an object"),
    ([{"check": "unit"}], "A criterion is text, or an object"),
    ([{"text": "A", "argv": ["true"]}], "only for a named check"),
    ([{"text": "A", "check": "unit"}, {"text": "A", "check": "lint"}], "cannot be linked to two checks"),
    ([{"text": "A", "check": "unit", "argv": ["a"]}, {"text": "B", "check": "unit", "argv": ["b"]}], "Two different commands"),
    ([{"text": "A", "check": "not a name"}], "criterion_checks must link"),
    ([{"text": "A", "check": "unit", "argv": []}], "check_proposals must give"),
    ([{"text": "A", "check": "unit", "argv": ["ok", 3]}], "check_proposals must give"),
    ([{"text": " ", "check": "unit"}], "acceptance_criteria must be a list of nonempty strings"),
])
def test_malformed_criteria_are_refused(workspace, criteria, message):
    store, pid, root, settings = workspace
    note = store.snapshot(pid, "note://x", "x", "note")
    with pytest.raises(ValueError, match=message):
        store.remember(pid, {"kind": "task", "content": "T", "metadata": {"acceptance_criteria": criteria}}, [note["id"]])


def test_a_link_cannot_be_changed_on_a_managed_task_outside_the_plan(workspace):
    store, pid, root, settings = workspace
    saved, task = plan(store, pid, root)
    with pytest.raises(ValueError, match="must be changed through PlanManager"):
        store.remember(pid, {"id": task["id"], "expected_version": task["version"], "expected_plan_version": saved["version"],
                             "metadata": {"criterion_checks": {"A reviewer read the migration notes.": "unit"}}},
                       task["source_ids"])


def test_two_machine_fresh_and_one_manual_pending_after_the_checks_run(workspace):
    """The acceptance scenario of the issue."""
    store, pid, root, settings = workspace
    _, task = plan(store, pid, root)
    before = [row["status"] for row in criteria_status(store, task, root)]
    assert before == ["machine_unknown", "machine_unknown", "manual_pending"]  # the profiles are not declared yet
    written = write_contract(store, pid, worktree=str(root))
    assert written["written"] is True and sorted(load_profiles(store, pid, root)) == ["lint", "unit"]
    git(root, "add", ".")
    git(root, "commit", "-qm", "declare the checks")
    assert [row["status"] for row in criteria_status(store, task, root)] == ["machine_missing", "machine_missing", "manual_pending"]
    for profile in ("unit", "lint"):
        assert run_check(store, settings, pid, profile, task["id"], str(root))["passed"] is True
    status = verification_status(store, task, root)
    assert [(row["status"], row["check"]) for row in status["criteria"]] == [
        ("machine_fresh", "unit"), ("machine_fresh", "lint"), ("manual_pending", None)]
    assert all(row["run_id"] for row in status["criteria"][:2]) and status["criteria"][2]["run_id"] is None
    contract = build_contract(store, pid, worktree=str(root))
    assert contract["counts"] == {"machine_fresh": 2, "manual_pending": 1} and contract["additions"] == {}
    assert "2 machine_fresh (proven by a machine run); 1 manual_pending (waits for a manual assessment)" in contract["text"]
    # An edit makes the machine results stale; the manual criterion is untouched by it.
    (root / "app.py").write_text("value = 2\n")
    assert [row["status"] for row in criteria_status(store, task, root)] == ["machine_stale", "machine_stale", "manual_pending"]


def test_the_contract_shows_a_diff_and_writes_nothing_until_asked(workspace):
    store, pid, root, settings = workspace
    plan(store, pid, root)
    contract = build_contract(store, pid, worktree=str(root))
    assert not (root / ".cairnmind").exists()
    assert contract["additions"] == {"lint": PASSES, "unit": PASSES}
    assert "+[checks.unit]" in contract["diff"] and f"+argv = {json.dumps(PASSES)}" in contract["diff"]
    assert "Nothing was run and nothing was written." in contract["text"]
    assert "it runs with your rights when you run the check" in contract["text"]
    assert not any(run for run in store.query("SELECT FROM check_runs"))


def test_writing_adds_profiles_and_never_changes_one_that_is_declared(workspace):
    store, pid, root, settings = workspace
    plan(store, pid, root)
    folder = root / ".cairnmind"
    folder.mkdir()
    mine = '# my checks\n[checks.unit]\nargv = ["my-own-command"]\ntimeout_seconds = 30\n'
    (folder / "checks.toml").write_text(mine)
    contract = write_contract(store, pid, worktree=str(root))
    text = (folder / "checks.toml").read_text()
    assert text.startswith(mine) and contract["additions"] == {"lint": PASSES}
    profiles = load_profiles(store, pid, root)
    assert profiles["unit"]["argv"] == ["my-own-command"] and profiles["lint"]["argv"] == PASSES
    again = write_contract(store, pid, worktree=str(root))
    assert again["written"] is False and (folder / "checks.toml").read_text() == text


def test_a_project_that_declares_profiles_in_the_earlier_file_gets_them_added_there(workspace):
    store, pid, root, settings = workspace
    plan(store, pid, root)
    (root / ".devbrain").mkdir()
    (root / ".devbrain/checks.toml").write_text('[checks.other]\nargv = ["true"]\n')
    contract = write_contract(store, pid, worktree=str(root))
    assert contract["profile_file"] == str(root / ".devbrain/checks.toml") and not (root / ".cairnmind").exists()
    assert sorted(load_profiles(store, pid, root)) == ["lint", "other", "unit"]


def test_a_check_without_one_proposed_command_is_left_to_the_user(workspace):
    store, pid, root, settings = workspace
    criteria = [{"text": "Unit tests pass.", "check": "unit"}, {"text": "Fast.", "check": "bench", "argv": ["bench", "--quick"]}]
    saved = PlanManager(store).save(pid, {"title": "Two steps", "goal": "g"}, [
        {"key": "a", "content": "A", "metadata": {"acceptance_criteria": criteria}},
        {"key": "b", "content": "B", "metadata": {"acceptance_criteria": [
            {"text": "Fast enough.", "check": "bench", "argv": ["bench", "--full"]}]}}],
        "two-steps", worktree=str(root), activate=True)
    contract = build_contract(store, pid, saved["plan"]["id"], str(root))
    assert contract["additions"] == {} and contract["diff"] == ""
    assert {row["check"]: row["reason"] for row in contract["undecided"]} == {
        "unit": "no command was proposed", "bench": "different commands were proposed"}
    assert "Declare it yourself." in contract["text"]


def test_a_proposed_command_is_written_as_data_and_cannot_break_out_of_its_line(workspace):
    store, pid, root, settings = workspace
    hostile = ["sh", "-c", 'echo "x"\n[checks.evil]\nargv = ["rm", "-rf", "/"]', "\x1b[2K\rforged"]
    plan(store, pid, root, criteria=[{"text": "Safe.", "check": "unit", "argv": hostile}], key="hostile")
    contract = write_contract(store, pid, worktree=str(root))
    profiles = load_profiles(store, pid, root)
    assert list(profiles) == ["unit"] and profiles["unit"]["argv"] == hostile  # one profile, the argument kept whole
    assert "\x1b" not in contract["text"] and "\r" not in contract["text"]


def test_the_contract_runs_nothing_and_changes_no_record(workspace):
    store, pid, root, settings = workspace
    plan(store, pid, root)

    def everything():
        return {name: sorted(json.dumps(_clean(row), sort_keys=True, default=str) for row in store.query(f"SELECT FROM {name}"))
                for name in ("records", "record_revisions", "plans", "plan_revisions", "check_runs", "jobs")}

    before = everything()
    build_contract(store, pid, worktree=str(root))
    write_contract(store, pid, worktree=str(root))
    assert everything() == before


def test_the_command_shows_the_contract_and_writes_only_with_the_flag(workspace, monkeypatch):
    store, pid, root, settings = workspace
    plan(store, pid, root)
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    runner = CliRunner()
    shown = runner.invoke(app, ["--data-dir", str(store.data_dir), "plan", "contract", "contract"])
    assert shown.exit_code == 0, shown.output
    assert "machine_unknown: Unit tests pass. [check unit]" in shown.output and not (root / ".cairnmind").exists()
    written = runner.invoke(app, ["--data-dir", str(store.data_dir), "plan", "contract", "contract", "--write"])
    assert written.exit_code == 0 and "No command was run." in written.output
    assert (root / ".cairnmind/checks.toml").is_file()
    as_json = runner.invoke(app, ["--data-dir", str(store.data_dir), "plan", "contract", "contract", "--json"])
    assert json.loads(as_json.output)["counts"] == {"machine_missing": 2, "manual_pending": 1}
