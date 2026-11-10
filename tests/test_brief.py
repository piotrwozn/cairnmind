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

from devbrain import cli
from devbrain.brief import MAX_LINES, build_brief
from devbrain.config import Settings
from devbrain.plans import PlanManager
from devbrain.service import create_app
from devbrain.store import Store
from devbrain.verification import run_check


def git(root, *args, when=None):
    env = dict(os.environ)
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env).stdout


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Cairnmind fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "app.py").write_text("value = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture", when=datetime.now(timezone.utc) - timedelta(days=2))
    store = Store(tmp_path / "memory")
    project = store.add_project("demo", root)
    return store, project, root, Settings.load(store.data_dir)


def check_profile(root):
    script = root / "check_unit.py"
    script.write_text("print('checked')\n")
    config = root / ".devbrain" / "checks.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(f"[checks.unit]\nargv = {json.dumps([sys.executable, str(script)])}\ncwd = \".\"\n"
                      "timeout_seconds = 5\noutput_limit_bytes = 4096\nextra_inputs = []\n")


def waiting_task(store, project, content="Validate the login token", **metadata):
    source = store.snapshot(project["id"], f"fixture://{content}", content, "manual")
    return store.remember(project["id"], {"kind": "task", "content": content,
                                          "metadata": {"state": "needs_test", **metadata}}, [source["id"]])


def lines(brief, basis=None):
    return [line["text"] for line in brief["lines"] if basis in (None, line["basis"])]


def test_empty_project_brief_says_what_is_missing(workspace):
    store, project, root, _ = workspace
    brief = build_brief(store, project["id"])
    assert brief["text"].splitlines()[0] == f"Brief for demo in {root}"
    assert "machine = measured now by Cairnmind; reported = stored claim, not verified" in brief["text"]
    assert lines(brief) == ["No checkpoint has been saved for this project.",
                            "No plan is active in this worktree.",
                            "No task is in progress, waiting for a test or waiting for a review.",
                            "No proposed findings are waiting for review."]
    assert {line["basis"] for line in brief["lines"]} == {"machine"}


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
    note = store.snapshot(project["id"], "fixture://note", "Sessions last one hour.", "manual")
    store.remember(project["id"], {"kind": "fact", "content": "Sessions last one hour."}, [note["id"]])

    brief = build_brief(store, project["id"], now=later + timedelta(hours=2))
    machine, reported = lines(brief, "machine"), lines(brief, "reported")
    assert any(text.startswith("Latest checkpoint saved 3 hours ago (") for text in machine)
    assert "Checkpoint summary: Retry limit half done" in reported
    assert "Checkpoint next: write the test" in reported
    assert not any("empty" in text for text in reported)
    assert "Active plan: Harden login (version 1, 2 steps)." in machine
    assert "Next task (ready): Add the retry limit" in machine
    assert "Blocked: Document the limit (unfinished dependencies)" in machine
    assert 'Checks for "Validate the login token" [needs_test]: fresh' in machine
    assert "1 commit since the checkpoint was saved:" in machine
    commit = next(text for text in machine if "Raise the value" in text)
    assert "\x1b" not in brief["text"] and commit.endswith("Raise the value\\x1b[31m")
    assert "add the check profile" not in brief["text"]
    assert "1 proposed finding waiting for review:" in machine
    assert "  fact: Sessions last one hour." in reported
    assert not any("evidence_" in text for text in lines(brief))
    assert len(brief["text"].splitlines()) <= MAX_LINES


def test_check_evidence_is_stale_after_an_edit_and_reported_claims_are_not_machine_lines(workspace):
    store, project, root, settings = workspace
    check_profile(root)
    task = waiting_task(store, project, required_profiles=["unit"])
    waiting_task(store, project, "Review the session expiry", test_passed=True)
    run_check(store, settings, project["id"], "unit", task["id"])
    (root / "app.py").write_text("value = 3\n")
    brief = build_brief(store, project["id"])
    stale = next(text for text in lines(brief, "machine") if "Validate the login token" in text and "Checks" in text)
    assert "]: stale: no successful matching run" in stale
    reported = next(text for text in lines(brief, "reported") if "Review the session expiry" in text and "Checks" in text)
    assert "]: reported: historical/client-reported evidence is not a machine check" in reported
    assert not any("Review the session expiry" in text and "Checks" in text for text in lines(brief, "machine"))


def test_brief_never_exceeds_forty_lines_and_says_what_it_left_out(workspace):
    store, project, _, _ = workspace
    store.checkpoint(project["id"], {f"field{index}": f"value {index}" for index in range(12)})
    for index in range(30):
        waiting_task(store, project, f"Task number {index}\nsecond line never shown")
        note = store.snapshot(project["id"], f"fixture://note{index}", f"Fact number {index}", "manual")
        store.remember(project["id"], {"kind": "fact", "content": f"Fact number {index}"}, [note["id"]])
    brief = build_brief(store, project["id"])
    text = brief["text"].splitlines()
    assert len(text) <= MAX_LINES == 40
    assert "The checkpoint has 8 more fields; see `cairnmind context`." in lines(brief)
    assert "24 more tasks in progress are not listed." in lines(brief)
    assert "  and 26 more." in lines(brief)
    assert "second line never shown" not in brief["text"]


def test_brief_in_a_folder_without_git_says_commits_cannot_be_listed(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    store = Store(tmp_path / "memory")
    project = store.add_project("plain", root)
    store.checkpoint(project["id"], {"summary": "started"})
    assert "This worktree is not a Git repository, so commits cannot be listed." in lines(build_brief(store, project["id"]))


def test_brief_rejects_an_unregistered_worktree(workspace, tmp_path):
    store, project, _, _ = workspace
    with pytest.raises(ValueError, match="registered project root"):
        build_brief(store, project["id"], tmp_path)


def test_http_brief_is_authenticated_and_never_calls_a_model(workspace):
    store, project, root, settings = workspace
    store.checkpoint(project["id"], {"summary": "over HTTP"})

    class NoModel:
        def __getattr__(self, name):
            pytest.fail(f"The brief reached the model client: {name}")

    with TestClient(create_app(settings, ollama=NoModel(), background_inference=False)) as client:
        assert client.get("/brief", params={"project": "demo"}).status_code in {401, 403}
        result = client.get("/brief", params={"project": "demo", "worktree": str(root)},
                            headers={"Authorization": f"Bearer {settings.token}"})
    assert result.status_code == 200, result.text
    assert "Checkpoint summary: over HTTP" in result.json()["text"]
    assert result.json()["worktree"] == str(root)


def test_cli_brief_prints_text_or_json_and_forwards_the_session(tmp_path, monkeypatch):
    calls = []

    def answered(settings, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"text": "Brief for demo in /repo\n[machine]  ok", "lines": [{"basis": "machine", "text": "ok"}]}

    monkeypatch.setattr(cli, "request", answered)
    runner = CliRunner()
    shown = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "data"), "brief", "demo"])
    assert shown.exit_code == 0, shown.output or repr(shown.exception)
    assert shown.output == "Brief for demo in /repo\n[machine]  ok\n"
    assert calls == [("GET", "/brief", {"params": {"project": "demo"}})]
    scoped = runner.invoke(cli.app, ["--data-dir", str(tmp_path / "data"), "brief", "demo",
                                     "--worktree", str(tmp_path), "--json"])
    assert json.loads(scoped.output)["lines"] == [{"basis": "machine", "text": "ok"}]
    assert calls[-1][2] == {"params": {"project": "demo", "worktree": str(tmp_path.resolve())}}
