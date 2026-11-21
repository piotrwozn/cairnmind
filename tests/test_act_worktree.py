"""A person's act names the worktree it happened in, with its branch and commit (#247)."""
from __future__ import annotations

import json
import subprocess
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from cairnmind import conflicts
from cairnmind.findings import save_finding
from cairnmind.inspector import Inspector
from cairnmind.inspector_api import attach_inspector_routes
from cairnmind.memory_acts import act_context, confirm
from cairnmind.plans import PlanManager
from cairnmind.store import Store

from conftest import as_person


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
    assert act_context(store, pid)["branch"] == "feat-a"
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
    assert act_context(store, pid)["branch"] == "feat-a"


def test_a_review_names_the_worktree_the_inspector_is_bound_to(two_worktrees):
    store, pid, root, other = two_worktrees
    record = decision(store, pid)
    result = Inspector(store).review(pid, record["id"], "confirm", 1, worktree=str(other))
    audit = audit_of(store, result["audit_source_id"])
    assert (audit["branch"], audit["commit"]) == ("feat-b", git(other, "rev-parse", "HEAD"))
    # Without a worktree it is what it was: the project's root.
    second = decision(store, pid)
    plain = Inspector(store).review(pid, second["id"], "dispute", 1, reason="No")
    assert audit_of(store, plain["audit_source_id"])["branch"] == "feat-a"


def test_the_inspector_routes_pass_the_worktree_to_every_confirmation(two_worktrees):
    store, pid, root, other = two_worktrees
    app = FastAPI()
    attach_inspector_routes(app, store, store.settings)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + store.settings.token}
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
