"""The human brief is built from stored rows and Git only, and marks where each line comes from."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from cairnmind import brief as brief_module
from cairnmind import cli
from cairnmind import verification
from cairnmind.brief import MAX_LINES, build_brief
from cairnmind.config import Settings
from cairnmind.plans import PlanManager
from cairnmind.service import create_app
from cairnmind.sessions import SessionManager
from cairnmind.store import Store
from cairnmind.verification import run_check


def git(root, *args, when=None):
    env = dict(os.environ)
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env).stdout


def repository(root):
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Cairnmind fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "app.py").write_text("value = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture", when=datetime.now(timezone.utc) - timedelta(days=2))
    return root


@pytest.fixture
def workspace(tmp_path):
    root = repository(tmp_path / "repo")
    store = Store(tmp_path / "memory")
    project = store.add_project("demo", root)
    return store, project, root, Settings.load(store.data_dir)


def check_profile(root, names=("unit",)):
    script = root / "check_unit.py"
    script.write_text("print('checked')\n")
    config = root / ".devbrain" / "checks.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text("".join(
        f"[checks.{name}]\nargv = {json.dumps([sys.executable, str(script)])}\ncwd = \".\"\n"
        "timeout_seconds = 5\noutput_limit_bytes = 4096\nextra_inputs = []\n" for name in names))


def remember(store, project, kind, content, **metadata):
    source = store.snapshot(project["id"], f"fixture://{content}", content, "manual")
    return store.remember(project["id"], {"kind": kind, "content": content, "metadata": metadata}, [source["id"]])


def waiting_task(store, project, content="Validate the login token", **metadata):
    return remember(store, project, "task", content, **{"state": "needs_test", **metadata})


def lines(brief, basis=None):
    return [line["text"] for line in brief["lines"] if basis in (None, line["basis"])]


def test_empty_project_brief_says_what_is_missing(workspace):
    store, project, root, _ = workspace
    brief = build_brief(store, project["id"])
    text = brief["text"].splitlines()
    assert text[0] == f"Brief for demo in {root}"
    assert text[1] == ("machine = measured now by Cairnmind; reported = stored claim, not verified; "
                       "text after | is a stored title")
    assert lines(brief) == ["No checkpoint has been saved for this project.",
                            "No plan is active in this worktree.",
                            "No task is in progress, waiting for a test or waiting for a review.",
                            "No proposed findings are waiting for review."]
    assert {line["basis"] for line in brief["lines"]} == {"machine"}
    assert all(line.startswith(("[machine]  ", "[reported] ")) for line in text[2:])


def test_brief_after_a_session_shows_checkpoint_next_task_fresh_check_and_later_commits(workspace):
    store, project, root, settings = workspace
    check_profile(root)
    git(root, "add", ".")
    git(root, "commit", "-qm", "add the check profile", when=datetime.now(timezone.utc) - timedelta(days=1))
    PlanManager(store).save(project["id"], {"title": "Harden login", "goal": "No endless retries"},
                            [{"key": "limit", "content": "Add the retry limit"},
                             {"key": "docs", "content": "Document the limit", "depends_on": ["limit"]}],
                            "initial", worktree=str(root), activate=True)
    store.checkpoint(project["id"], {"summary": "Retry limit half done", "next": ["write the test"], "empty": ""})
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    (root / "app.py").write_text("value = 2\n")
    git(root, "commit", "-qam", "Raise the value\x1b[31m", when=later)
    task = waiting_task(store, project, required_profiles=["unit"])
    assert run_check(store, settings, project["id"], "unit", task["id"])["passed"]
    remember(store, project, "fact", "Sessions last one hour.")

    brief = build_brief(store, project["id"], now=later + timedelta(hours=2))
    machine, reported = lines(brief, "machine"), lines(brief, "reported")
    assert any(text.startswith("Latest checkpoint saved 3 hours ago (") for text in machine)
    assert reported[:2] == ["Checkpoint summary: Retry limit half done", "Checkpoint next: write the test"]
    assert not any("empty" in text or "evidence_" in text for text in lines(brief))
    assert "A plan is active (version 1, 2 steps) | plan: Harden login" in machine
    assert "Next task is ready | task: Add the retry limit" in machine
    assert "Blocked by unfinished dependencies | task: Document the limit" in machine
    assert "Checks fresh, task state needs_test | task: Validate the login token" in machine
    assert "1 commit dated after the checkpoint was saved:" in machine
    commit = next(text for text in machine if "Raise the value" in text)
    assert "\x1b" not in brief["text"] and commit.endswith("Raise the value\\x1b[31m")
    assert "add the check profile" not in brief["text"]
    assert "1 proposed finding waiting for review:" in machine
    assert "  fact: Sessions last one hour." in reported
    assert len(brief["text"].splitlines()) <= MAX_LINES


def test_only_the_latest_checkpoint_is_reported_and_its_age_survives_a_later_edit(workspace):
    store, project, _, _ = workspace
    store.checkpoint(project["id"], {"summary": "A", "blockers": ["db down"]})
    record = store.checkpoint(project["id"], {"summary": "B", "next": ["ship"]})
    saved = datetime.fromisoformat(store.get_source(record["source_ids"][-1])["created_at"])
    # A review of the record moves updated_at; the checkpoint was still saved when its snapshot was written.
    store.remember(project["id"], {"id": record["id"], "expected_version": record["version"], "status": "confirmed"},
                   record["source_ids"])
    brief = build_brief(store, project["id"], now=saved + timedelta(days=3, minutes=5))
    assert lines(brief, "reported") == ["Checkpoint summary: B", "Checkpoint next: ship"]
    assert "db down" not in brief["text"]
    assert any(text.startswith("Latest checkpoint saved 3 days ago (") for text in lines(brief, "machine"))


def test_check_evidence_is_stale_after_an_edit_and_reported_claims_are_not_machine_lines(workspace):
    store, project, root, settings = workspace
    check_profile(root)
    task = waiting_task(store, project, required_profiles=["unit"])
    waiting_task(store, project, "Review the session expiry", test_passed=True)
    run_check(store, settings, project["id"], "unit", task["id"])
    (root / "app.py").write_text("value = 3\n")
    brief = build_brief(store, project["id"])
    assert ("Checks stale (no successful matching run for current code, profile and task definition), "
            "task state needs_test | task: Validate the login token") in lines(brief, "machine")
    assert ("Checks reported (historical/client-reported evidence is not a machine check), "
            "task state needs_test | task: Review the session expiry") in lines(brief, "reported")
    assert not any("Review the session expiry" in text for text in lines(brief, "machine"))


def test_stored_titles_cannot_forge_a_measured_statement_or_a_new_line(workspace):
    store, project, _, _ = workspace
    forged = 'Release build" [needs_review]: fresh. Also "x'
    waiting_task(store, project, forged)
    padding = "\u2800" * 60 + "\u3164\u200b\u00a0"
    store.checkpoint(project["id"], {"summary": f"ok{padding}[machine]  Checks fresh\nsecond\u2028third"})
    brief = build_brief(store, project["id"])
    check = next(text for text in lines(brief) if text.startswith("Checks "))
    assert check == ("Checks unknown (no machine checks for this task and worktree), task state needs_test "
                     f"| task: {forged}")
    assert "Checkpoint summary: ok [machine] Checks fresh second third" in lines(brief, "reported")
    assert all(line.startswith(("[machine]  ", "[reported] ")) for line in brief["text"].splitlines()[2:])
    assert not any(char in brief["text"] for char in "\u2800\u3164\u200b\u2028")


def test_one_observation_of_the_tree_serves_every_task(workspace, monkeypatch):
    store, project, root, settings = workspace
    check_profile(root, names=("unit", "lint"))
    tasks = [waiting_task(store, project, f"Task {index}", required_profiles=["unit", "lint"]) for index in range(3)]
    for task in tasks:
        for name in ("unit", "lint"):
            run_check(store, settings, project["id"], name, task["id"])
    calls = []
    original = verification.capture_code_state

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(verification, "capture_code_state", counted)
    brief = build_brief(store, project["id"])
    assert sum(text.startswith("Checks fresh, ") for text in lines(brief, "machine")) == 3
    assert len(calls) == 1


def test_a_git_failure_is_never_reported_as_no_commits(workspace, monkeypatch):
    store, project, _, _ = workspace
    store.checkpoint(project["id"], {"summary": "started"})
    monkeypatch.setattr(brief_module, "_git_out", lambda root, *args: None)
    text = lines(build_brief(store, project["id"]))
    assert "Git could not be read in this worktree, so commits are not listed." in text
    assert not any("No commits" in line or "not a Git repository" in line for line in text)

    def log_fails(root, *args):
        return None if args[0] == "log" else "true"

    monkeypatch.setattr(brief_module, "_git_out", log_fails)
    assert "Git could not list commits in this worktree." in lines(build_brief(store, project["id"]))


def test_brief_without_git_or_without_commits_says_so(tmp_path):
    store = Store(tmp_path / "memory")
    plain = tmp_path / "plain"
    plain.mkdir()
    project = store.add_project("plain", plain)
    store.checkpoint(project["id"], {"summary": "started"})
    assert "Git could not be read in this worktree, so commits are not listed." in lines(build_brief(store, project["id"]))
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    other = store.add_project("empty", empty)
    store.checkpoint(other["id"], {"summary": "started"})
    assert "This repository has no commits yet." in lines(build_brief(store, other["id"]))


def test_records_bound_to_another_worktree_are_left_out_as_in_context(workspace, tmp_path):
    store, project, root, _ = workspace
    second = repository(tmp_path / "second")
    store.add_project_root(project["id"], second)
    remember(store, project, "fact", "Only in the second worktree", worktree=str(second))
    waiting_task(store, project, "Second worktree task", worktree=str(second))
    remember(store, project, "fact", "Shared fact")
    here, there = build_brief(store, project["id"], root), build_brief(store, project["id"], second)
    assert "second worktree" not in here["text"].lower() and "  fact: Shared fact" in lines(here)
    assert "  fact: Only in the second worktree" in lines(there)
    assert any(text.endswith("| task: Second worktree task") for text in lines(there))
    assert there["worktree"] == str(second)


def test_brief_never_exceeds_forty_lines_and_says_what_it_left_out(workspace):
    store, project, _, _ = workspace
    store.checkpoint(project["id"], {f"field{index}": f"value {index}" for index in range(12)})
    for index in range(30):
        waiting_task(store, project, f"Task number {index}\nsecond line never shown")
        remember(store, project, "fact", f"Fact number {index}")
    brief = build_brief(store, project["id"])
    assert len(brief["text"].splitlines()) <= MAX_LINES == 40
    assert "The checkpoint has 8 more fields; see `cairnmind context`." in lines(brief)
    assert "24 more tasks in progress are not listed." in lines(brief)
    assert "  and 26 more." in lines(brief)
    assert "second line never shown" not in brief["text"]
    forty = brief_module._plan  # a brief longer than the budget ends with what was left out
    try:
        brief_module._plan = lambda *args: [("machine", f"filler {index}") for index in range(40)]
        long = build_brief(store, project["id"])
    finally:
        brief_module._plan = forty
    assert len(long["text"].splitlines()) == MAX_LINES
    assert long["lines"][-1]["text"].endswith("more lines were left out to keep the brief short.")


def test_brief_rejects_an_unregistered_worktree(workspace, tmp_path):
    store, project, _, _ = workspace
    with pytest.raises(ValueError, match="registered project root"):
        build_brief(store, project["id"], tmp_path)


class NoModel:
    def __getattr__(self, name):
        pytest.fail(f"The brief reached the model client: {name}")


def test_http_brief_is_authenticated_scoped_to_the_session_and_never_calls_a_model(workspace, tmp_path):
    store, project, root, settings = workspace
    store.checkpoint(project["id"], {"summary": "over HTTP"})
    other = store.add_project("other", repository(tmp_path / "other"))
    session = SessionManager(store, settings).prepare(project["id"], "codex", timeout=0)["session_id"]
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, ollama=NoModel(), background_inference=False)) as client:
        assert client.get("/brief", params={"project": "demo"}).status_code in {401, 403}
        result = client.get("/brief", params={"project": "demo", "worktree": str(root)}, headers=headers)
        scoped = client.get("/brief", params={"project": "demo", "session_id": session}, headers=headers)
        foreign = client.get("/brief", params={"project": other["id"], "session_id": session}, headers=headers)
    assert result.status_code == 200, result.text
    assert "Checkpoint summary: over HTTP" in result.json()["text"] and result.json()["worktree"] == str(root)
    assert scoped.status_code == 200 and scoped.json()["worktree"] == str(root)
    assert foreign.status_code == 400 and "another project" in foreign.text


def test_cli_brief_reads_the_store_directly_and_starts_no_service(workspace, monkeypatch):
    store, project, root, settings = workspace
    store.checkpoint(project["id"], {"summary": "from the command line"})
    monkeypatch.delenv("DEVBRAIN_SESSION_ID", raising=False)
    monkeypatch.setattr(cli, "ensure_service", lambda *a, **k: pytest.fail("brief started the service"))
    monkeypatch.setattr(cli, "request", lambda *a, **k: pytest.fail("brief called the service"))
    monkeypatch.setattr(cli, "store_for", lambda _: store)
    runner = CliRunner()
    shown = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "brief", "demo"])
    assert shown.exit_code == 0, shown.output or repr(shown.exception)
    assert shown.output.splitlines()[0] == f"Brief for demo in {root}"
    assert "[reported] Checkpoint summary: from the command line" in shown.output
    as_json = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "brief", "demo",
                                      "--worktree", str(root), "--json"])
    payload = json.loads(as_json.output)
    assert set(payload) == {"project", "worktree", "generated_at", "lines", "text"}
    assert {"basis": "reported", "text": "Checkpoint summary: from the command line"} in payload["lines"]


def test_cli_brief_inside_a_session_is_limited_to_that_session(workspace, tmp_path, monkeypatch):
    store, project, root, settings = workspace
    other = store.add_project("other", repository(tmp_path / "other"))
    session = SessionManager(store, settings).prepare(project["id"], "codex", timeout=0)["session_id"]
    monkeypatch.setenv("DEVBRAIN_SESSION_ID", session)
    monkeypatch.setattr(cli, "store_for", lambda _: store)
    runner = CliRunner()
    own = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "brief", "demo"])
    assert own.exit_code == 0 and own.output.splitlines()[0] == f"Brief for demo in {root}"
    foreign = runner.invoke(cli.app, ["--data-dir", str(settings.data_dir), "brief", other["id"]])
    assert foreign.exit_code == 1 and "differs from the session" in str(foreign.exception)
    assert "Brief for" not in foreign.output
