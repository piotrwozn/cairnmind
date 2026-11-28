"""A person's act names the worktree it happened in, with its branch and commit (#247)."""
from __future__ import annotations

import json
import subprocess
from uuid import uuid4

import pytest
from conftest import as_person
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_published_memory import reviewed
from test_published_memory_secrets import SHAPES
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.memory import conflicts
from cairnmind.memory.findings import save_finding
from cairnmind.memory.memory_acts import act_context, confirm
from cairnmind.memory.memory_published_release import release
from cairnmind.memory.plans import PlanManager
from cairnmind.service import create_app
from cairnmind.store import Store
from cairnmind.terminal.inspector import Inspector
from cairnmind.terminal.inspector_api import attach_inspector_routes


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def two_worktrees(tmp_path):
    root = tmp_path / "clone-a"
    root.mkdir()
    for args in (("init", "-q", "-b", "feat-a"), ("config", "user.name", "Ada Nowak"),
                 ("config", "user.email", "ada@example.invalid")):
        git(root, *args)
    (root / "README.md").write_text("demo\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "first")
    other = tmp_path / "wt-b"
    git(root, "worktree", "add", "-q", "-b", "feat-b", str(other))
    (other / "more.md").write_text("more\n")
    git(other, "add", ".")
    git(other, "commit", "-qm", "second")
    store = Store(tmp_path / "database")
    project = store.add_project("acts", root)
    store.add_project_root(project["id"], other)
    return store, project["id"], root, other


def audit_of(store, source_id):
    return json.loads(store.get_source(source_id)["content"])


def decision(store, pid):
    source = store.snapshot(pid, f"note://{uuid4()}", "Use cookies.", "note")
    return store.remember(pid, {"kind": "decision", "content": "Use cookies."}, [source["id"]])


def test_the_context_names_the_worktree_it_was_asked_about(two_worktrees):
    store, pid, root, other = two_worktrees
    assert act_context(store, pid, root)["branch"] == "feat-a"
    # Two roots and none named: the worktree is not known, so no branch or commit is claimed, never the root's.
    unnamed = act_context(store, pid)
    assert (unnamed["branch"], unnamed["commit"], unnamed["actor_name"]) == (None, None, "Ada Nowak")
    named = act_context(store, pid, other)
    assert (named["branch"], named["commit"]) == ("feat-b", git(other, "rev-parse", "HEAD"))
    assert named["commit"] != git(root, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="not a registered project root"):
        act_context(store, pid, other.parent)  # Never a silent fall back to the root for a folder it does not know.


def test_a_detached_head_is_recorded_as_detached_with_its_commit(two_worktrees):
    store, pid, root, other = two_worktrees
    commit = git(other, "rev-parse", "HEAD")
    git(other, "checkout", "-q", "--detach")
    context = act_context(store, pid, other)
    assert context["branch"] == "detached HEAD" and context["commit"] == commit
    assert act_context(store, pid, root)["branch"] == "feat-a"


def test_a_project_with_one_root_names_that_root_when_no_worktree_is_given(tmp_path):
    solo = tmp_path / "solo"
    solo.mkdir()
    for args in (("init", "-q", "-b", "only"), ("config", "user.name", "Ada Nowak"),
                 ("config", "user.email", "ada@example.invalid")):
        git(solo, *args)
    (solo / "a.md").write_text("a\n")
    git(solo, "add", ".")
    git(solo, "commit", "-qm", "first")
    store = Store(tmp_path / "database")
    pid = store.add_project("solo", solo)["id"]
    assert act_context(store, pid)["branch"] == "only"


def test_a_failed_branch_read_is_not_recorded_as_a_detached_head(two_worktrees, monkeypatch):
    store, pid, root, other = two_worktrees
    import cairnmind.memory.verification as verification
    real = verification._git

    def failing(path, *args):
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            raise subprocess.TimeoutExpired("git", 20)
        return real(path, *args)
    monkeypatch.setattr(verification, "_git", failing)
    context = act_context(store, pid, other)
    assert context["branch"] is None and context["commit"] == git(other, "rev-parse", "HEAD")


def test_a_review_names_the_worktree_the_inspector_is_bound_to(two_worktrees):
    store, pid, root, other = two_worktrees
    record = decision(store, pid)
    result = Inspector(store).review(pid, record["id"], "confirm", 1, worktree=str(other))
    audit = audit_of(store, result["audit_source_id"])
    assert (audit["branch"], audit["commit"]) == ("feat-b", git(other, "rev-parse", "HEAD"))
    # Without a worktree, and with two roots, the audit does not claim a branch it cannot know.
    second = decision(store, pid)
    plain = Inspector(store).review(pid, second["id"], "dispute", 1, reason="No")
    unknown = audit_of(store, plain["audit_source_id"])
    assert (unknown["branch"], unknown["commit"]) == (None, None)


def test_the_inspector_routes_pass_the_worktree_to_every_confirmation(two_worktrees, workspace_person):
    store, pid, root, other = two_worktrees
    app = FastAPI()
    attach_inspector_routes(app, store, store.settings)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + store.settings.token, **workspace_person}
    git(other, "checkout", "-q", "--detach")
    commit = git(other, "rev-parse", "HEAD")
    expected = ("detached HEAD", commit)

    record = decision(store, pid)
    reviewed = client.post(f"/inspector/records/{record['id']}/review", headers=headers, json={
        "project": pid, "action": "confirm", "expected_version": 1, "worktree": str(other)})
    assert reviewed.status_code == 200, reviewed.text
    audit = audit_of(store, reviewed.json()["audit_source_id"])
    assert (audit["branch"], audit["commit"]) == expected

    saved = PlanManager(store).save(pid, {"title": "Ship", "goal": "Ship it", "constraints": []},
                                    [{"key": "one", "content": "Step one"}], "ship", worktree=str(other))
    plan = client.post(f"/inspector/plans/{saved['plan']['id']}/confirm", headers=headers, json={
        "project": pid, "expected_plan_version": saved["plan"]["version"], "worktree": str(other)})
    assert plan.status_code == 200, plan.text
    audit = audit_of(store, plan.json()["audit_source_id"])
    assert (audit["branch"], audit["commit"]) == expected

    refused = client.post(f"/inspector/plans/{saved['plan']['id']}/confirm", headers=headers, json={
        "project": pid, "expected_plan_version": 99, "worktree": str(other.parent)})
    assert refused.status_code == 400 and "not a registered project root" in refused.json()["detail"]


def test_a_conflict_decision_a_form_and_a_plan_confirmation_name_the_worktree(two_worktrees):
    store, pid, root, other = two_worktrees
    commit = git(other, "rev-parse", "HEAD")
    first = store.snapshot(pid, "transcript://old", "Sessions last a day.", "transcript")
    existing = as_person(store, pid, {"kind": "decision", "key": "ttl", "content": "Sessions last a day."},
                         [first["id"]])
    second = store.snapshot(pid, "transcript://new", "Sessions last an hour.", "transcript")
    candidate = store.remember(pid, {"kind": "decision", "key": "ttl~a", "content": "Sessions last an hour.",
                                     "metadata": {"possible_conflicts": [existing["id"]], "suggested_key": "ttl"}},
                               [second["id"]])
    decided = conflicts.resolve(Inspector(store), pid, candidate["id"], existing["id"], "supersede", 1, 1,
                                keep=candidate["id"], worktree=str(other))
    audit = audit_of(store, decided["audit_source_id"])
    assert (audit["branch"], audit["commit"]) == ("feat-b", commit)
    with store.transaction():
        found = save_finding(store, pid, "8d2e4f60-1a3b-4c5d-9e7f-0a1b2c3d4e5f", "Never push for the user.",
                             "constraint", "confirmed", worktree=str(other))
    metadata = store.get_source(found["source"]["id"])["metadata"]
    assert (metadata["branch"], metadata["commit"]) == ("feat-b", commit)
    saved = PlanManager(store).save(pid, {"title": "Ship", "goal": "Ship it", "constraints": []},
                                    [{"key": "one", "content": "Step one"}], "ship", worktree=str(other))
    done = confirm(Inspector(store), pid, saved["plan"]["id"], saved["plan"]["version"], worktree=str(other))
    assert audit_of(store, done["audit_source_id"])["branch"] == "feat-b"


def test_the_conflict_route_and_the_findings_route_pass_the_worktree(two_worktrees, workspace_person):
    store, pid, root, other = two_worktrees
    commit = git(other, "rev-parse", "HEAD")
    first = store.snapshot(pid, "transcript://old", "Sessions last a day.", "transcript")
    existing = as_person(store, pid, {"kind": "decision", "key": "ttl", "content": "Sessions last a day."},
                         [first["id"]])
    second = store.snapshot(pid, "transcript://new", "Sessions last an hour.", "transcript")
    candidate = store.remember(pid, {"kind": "decision", "key": "ttl~a", "content": "Sessions last an hour.",
                                     "metadata": {"possible_conflicts": [existing["id"]], "suggested_key": "ttl"}},
                               [second["id"]])
    app = FastAPI()
    attach_inspector_routes(app, store, store.settings)
    headers = {"Authorization": "Bearer " + store.settings.token, **workspace_person}
    decided = TestClient(app).post("/inspector/conflicts/resolve", headers=headers, json={
        "project": pid, "record_id": candidate["id"], "other_id": existing["id"], "action": "supersede",
        "expected_version": 1, "other_expected_version": 1, "keep": candidate["id"], "worktree": str(other)})
    assert decided.status_code == 200, decided.text
    audit = audit_of(store, decided.json()["audit_source_id"])
    assert (audit["branch"], audit["commit"]) == ("feat-b", commit)
    with TestClient(create_app(store.settings, start_worker=False, ollama=object()), headers=headers) as client:
        found = client.post("/findings", json={"project": pid, "key": str(uuid4()), "content": "Never push for me.",
                                               "kind": "constraint", "status": "confirmed", "worktree": str(other)})
    assert found.status_code == 200, found.text
    metadata = store.get_source(found.json()["source"]["id"])["metadata"]
    assert (metadata["branch"], metadata["commit"]) == ("feat-b", commit)


def test_a_release_names_the_worktree_it_was_made_in(two_worktrees):
    store, pid, root, other = two_worktrees
    record = reviewed(store, pid, "fact", f"The AWS documentation uses {SHAPES['aws_access_key']} as its key.", "aws")
    released = release(store, pid, record["id"], "The documentation example, not a real key.", worktree=str(other))
    audit = audit_of(store, released["release_source_id"])
    assert (audit["branch"], audit["commit"]) == ("feat-b", git(other, "rev-parse", "HEAD"))


def test_plan_save_on_the_command_line_stores_a_plan_and_a_confirm_plan_is_refused(two_worktrees, person_terminal,
                                                                                  tmp_path, monkeypatch):
    store, pid, root, other = two_worktrees
    from cairnmind.memory import confirmation
    for name in ("CAIRNMIND_SESSION_ID", "CAIRNMIND_PACKET_ID", *confirmation.CLIENT_MARKERS):
        monkeypatch.delenv(name, raising=False)
    document = tmp_path / "plan.json"
    document.write_text(json.dumps({"plan": {"title": "Ship", "goal": "Ship it", "constraints": []},
                                    "steps": [{"key": "one", "content": "Step one"}]}), encoding="utf-8")
    run = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "plan", "save", pid, str(document),
                                       "--idempotency-key", "k1", "--worktree", str(other)])
    assert run.exit_code == 0, run.output
    # A save is no person's act (#214), in a person's terminal either: no note names a person.
    assert not [row for row in store.query("SELECT uri FROM sources WHERE project_id=:p", {"p": pid})
                if row["uri"].startswith(("person://", "review://"))]
    sent = {}
    monkeypatch.setattr(cli, "request", lambda settings, method, path, json=None: sent.update(json) or {})
    confirmed = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "memory", "confirm-plan", pid,
                                             str(uuid4()), "--expected-version", "1", "--worktree", str(other)])
    assert confirmed.exit_code == 2 and "no longer confirms a plan" in " ".join(confirmed.output.split())
    assert sent == {}
