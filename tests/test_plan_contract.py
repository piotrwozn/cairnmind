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


def write(store, pid, root, plan_id=None):
    """Write what the read-only command proposed, with the code it printed."""
    shown = build_contract(store, pid, plan_id, str(root))
    return write_contract(store, pid, shown["write_code"], plan_id, str(root))


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
    written = write(store, pid, root)
    assert written["written"] is True and sorted(load_profiles(store, pid, root)) == ["lint", "unit"]
    assert "count as stale" in written["text"] and "nothing was written" not in written["text"]
    git(root, "add", ".")
    git(root, "commit", "-qm", "declare the checks")
    assert [row["status"] for row in criteria_status(store, task, root)] == ["machine_missing", "machine_missing", "manual_pending"]
    for profile in ("unit", "lint"):
        assert run_check(store, settings, pid, profile, task["id"], str(root))["passed"] is True
    status = verification_status(store, task, root)
    assert [(row["status"], row["check"]) for row in status["criteria"]] == [
        ("machine_fresh", "unit"), ("machine_fresh", "lint"), ("manual_pending", None)]
    assert all(row["run_id"] for row in status["criteria"][:2]) and status["criteria"][2]["run_id"] is None
    # Only a run made here by the local runner proves anything: the same rows marked as imported do not.
    store.command("UPDATE check_runs SET origin='portable_import' WHERE task_id=:task", {"task": task["id"]})
    assert [row["status"] for row in criteria_status(store, task, root)] == ["machine_missing", "machine_missing", "manual_pending"]
    store.command("UPDATE check_runs SET origin='local_runner' WHERE task_id=:task", {"task": task["id"]})
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
    contract = write(store, pid, root)
    text = (folder / "checks.toml").read_text()
    assert text.startswith(mine) and contract["additions"] == {"lint": PASSES}
    profiles = load_profiles(store, pid, root)
    assert profiles["unit"]["argv"] == ["my-own-command"] and profiles["lint"]["argv"] == PASSES
    assert build_contract(store, pid, worktree=str(root))["write_code"] is None
    with pytest.raises(ValueError, match="no proposed profile to add"):
        write_contract(store, pid, contract["write_code"] or "0" * 12, worktree=str(root))
    assert (folder / "checks.toml").read_text() == text


def test_a_project_that_declares_profiles_in_the_earlier_file_gets_them_added_there(workspace):
    store, pid, root, settings = workspace
    plan(store, pid, root)
    (root / ".devbrain").mkdir()
    (root / ".devbrain/checks.toml").write_text('[checks.other]\nargv = ["true"]\n')
    contract = write(store, pid, root)
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
    hostile = ["sh", "-c", 'echo "x"\n[checks.evil]\nargv = ["rm", "-rf", "/"]', "\x1b[2K\rforged",
               "emoji \U0001F600 and a delete\x7fcharacter", "back\\slash \u2028 separator", "zażółć"]
    plan(store, pid, root, criteria=[{"text": "Safe.", "check": "unit", "argv": hostile}], key="hostile")
    contract = write(store, pid, root)
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
    write(store, pid, root)
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
    code = shown.output.split("--write ")[1].split()[0]
    wrong = runner.invoke(app, ["--data-dir", str(store.data_dir), "plan", "contract", "contract", "--write", "000000000000"])
    assert wrong.exit_code != 0 and not (root / ".cairnmind").exists()
    written = runner.invoke(app, ["--data-dir", str(store.data_dir), "plan", "contract", "contract", "--write", code])
    assert written.exit_code == 0 and "No command was run." in written.output
    assert (root / ".cairnmind/checks.toml").is_file()
    as_json = runner.invoke(app, ["--data-dir", str(store.data_dir), "plan", "contract", "contract", "--json"])
    assert json.loads(as_json.output)["counts"] == {"machine_missing": 2, "manual_pending": 1}


def test_a_write_needs_the_code_of_the_proposal_that_was_shown(workspace):
    store, pid, root, settings = workspace
    plan(store, pid, root)
    shown = build_contract(store, pid, worktree=str(root))
    assert shown["write_code"] in shown["text"]
    # The plan changes after the person looked: another command is now proposed under the same name.
    PlanManager(store).save(pid, {"title": "Other", "goal": "g"}, [{"key": "x", "content": "X", "metadata": {
        "acceptance_criteria": [{"text": "Unit tests pass.", "check": "unit", "argv": ["something", "else"]}]}}],
        "other-plan", worktree=str(root), activate=True)
    with pytest.raises(ValueError, match="The proposal changed since it was shown"):
        write_contract(store, pid, shown["write_code"], worktree=str(root))
    assert not (root / ".cairnmind").exists()


def test_restating_a_criterion_as_text_removes_its_link_and_omitting_a_command_withdraws_it(workspace):
    store, pid, root, settings = workspace
    saved, task = plan(store, pid, root)

    def resave(criteria, key):
        current = PlanManager(store).get(pid, saved["id"])
        result = PlanManager(store).save(
            pid, {"id": saved["id"], "title": "Ship login", "goal": "Login works"},
            [{"key": "implement", "content": "Implement login", "metadata": {"acceptance_criteria": criteria}},
             {"key": "document", "content": "Document login"}], key, worktree=str(root),
            expected_version=current["plan"]["version"],
            task_versions={row["id"]: row["version"] for row in current["tasks"]})
        return next(row for row in result["tasks"] if row["metadata"]["step_key"] == "implement")["metadata"]

    withdrawn = resave([{"text": "Unit tests pass.", "check": "unit"}, "The linter is clean."], "second")
    assert withdrawn["criterion_checks"] == {"Unit tests pass.": "unit"} and "check_proposals" not in withdrawn
    contract = build_contract(store, pid, worktree=str(root))
    assert contract["additions"] == {} and contract["undecided"][0]["reason"] == "no command was proposed"
    plain = resave(["Unit tests pass.", "The linter is clean."], "third")
    assert "criterion_checks" not in plain and "check_proposals" not in plain
    task = next(row for row in PlanManager(store).get(pid, saved["id"])["tasks"] if row["metadata"]["step_key"] == "implement")
    assert [row["status"] for row in criteria_status(store, task, root)] == ["manual_pending", "manual_pending"]
    assert "criteria" not in verification_status(store, task, root)  # a task without links answers as it always did


@pytest.mark.parametrize("check", [["unit"], {"name": "unit"}, 3])
def test_a_check_that_is_not_a_name_is_refused_cleanly(workspace, check):
    store, pid, root, settings = workspace
    note = store.snapshot(pid, "note://x", "x", "note")
    with pytest.raises(ValueError, match="criterion_checks must link"):
        store.remember(pid, {"kind": "task", "content": "T", "metadata": {
            "acceptance_criteria": [{"text": "A", "check": check, "argv": ["true"]}]}}, [note["id"]])


def test_changing_only_a_proposed_command_keeps_reported_evidence_and_the_definition(workspace):
    store, pid, root, settings = workspace
    note = store.snapshot(pid, "note://x", "x", "note")
    task = store.remember(pid, {"kind": "task", "content": "T", "metadata": {
        "acceptance_criteria": [{"text": "A", "check": "unit", "argv": ["first"]}],
        "acceptance_passed": True, "acceptance_source_ids": [note["id"]]}}, [note["id"]])
    before = task_definition_hash(task)
    changed = store.remember(pid, {"id": task["id"], "expected_version": task["version"], "metadata": {
        "acceptance_criteria": [{"text": "A", "check": "unit", "argv": ["second"]}]}}, [note["id"]])
    assert changed["metadata"]["check_proposals"] == {"unit": ["second"]}
    assert changed["metadata"]["acceptance_passed"] is True and task_definition_hash(changed) == before
    relinked = store.remember(pid, {"id": task["id"], "expected_version": changed["version"], "metadata": {
        "acceptance_criteria": [{"text": "A", "check": "lint"}]}}, [note["id"]])
    assert "acceptance_passed" not in relinked["metadata"] and task_definition_hash(relinked) != before


def test_profiles_written_in_a_form_text_cannot_be_added_to_are_left_alone(workspace):
    store, pid, root, settings = workspace
    plan(store, pid, root)
    (root / ".cairnmind").mkdir()
    inline = 'checks = { other = { argv = ["true"] } }\n'
    (root / ".cairnmind/checks.toml").write_text(inline)
    contract = build_contract(store, pid, worktree=str(root))
    assert contract["additions"] == {} and contract["write_code"] is None and contract["diff"] == ""
    assert {row["reason"] for row in contract["undecided"]} == {"it cannot be added to the profile file as that file is written"}
    assert (root / ".cairnmind/checks.toml").read_text() == inline


def test_the_existing_bytes_of_the_profile_file_are_kept_including_line_endings(workspace):
    store, pid, root, settings = workspace
    plan(store, pid, root)
    (root / ".cairnmind").mkdir()
    mine = b'# mine\r\n[checks.other]\r\nargv = ["true"]\r\n'
    (root / ".cairnmind/checks.toml").write_bytes(mine)
    write(store, pid, root)
    after = (root / ".cairnmind/checks.toml").read_bytes()
    assert after.startswith(mine) and b"\n" not in after.replace(b"\r\n", b"")
    assert sorted(load_profiles(store, pid, root)) == ["lint", "other", "unit"]


def test_a_file_where_the_profile_directory_should_be_is_an_error_not_a_crash(workspace):
    store, pid, root, settings = workspace
    plan(store, pid, root)
    shown = build_contract(store, pid, worktree=str(root))
    (root / ".cairnmind").write_text("not a directory")
    with pytest.raises(ValueError):
        write_contract(store, pid, shown["write_code"], worktree=str(root))


def test_an_imported_record_cannot_carry_links_the_store_would_refuse(workspace):
    from cairnmind import memory as portable

    for metadata in ({"acceptance_criteria": ["A"], "criterion_checks": {"B": "unit"}},
                     {"acceptance_criteria": ["A"], "criterion_checks": {"A": "not a name"}},
                     {"acceptance_criteria": ["A"], "criterion_checks": {"A": "unit"}, "check_proposals": {"unit": []}},
                     {"acceptance_criteria": ["A"], "criterion_checks": {"A": "unit"}, "check_proposals": {"lint": ["x"]}}):
        with pytest.raises(ValueError, match="criterion_checks must link|check_proposals must give"):
            portable.validate_criterion_links(metadata, metadata["acceptance_criteria"])
    portable.validate_criterion_links({"acceptance_criteria": ["A"], "criterion_checks": {"A": "unit"},
                                       "check_proposals": {"unit": ["pytest", "-q"]}}, ["A"])


def test_the_next_task_view_lists_the_check_of_each_criterion_it_shows(workspace):
    from cairnmind.mcp_server import _next_task_view

    store, pid, root, settings = workspace
    plan(store, pid, root)
    view = _next_task_view(PlanManager(store).next_task(pid, None, str(root)))
    metadata = view["task"]["metadata"]
    assert metadata["acceptance_criteria"] == ["Unit tests pass.", "The linter is clean.", "A reviewer read the migration notes."]
    assert metadata["criterion_checks"] == ["unit", "lint", None]
    assert len(json.dumps(metadata)) < 7000
