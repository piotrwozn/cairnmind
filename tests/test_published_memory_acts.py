"""A person's acts recorded so that they can be published (#170): who, on which branch, and what exactly."""
from __future__ import annotations

import hashlib
import json
import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from cairnmind import cli, conflicts
from cairnmind.findings import save_finding
from cairnmind.inspector import Inspector, InspectorNotFound
from cairnmind.memory_acts import confirm, covered_by_local_act
from cairnmind.inspector_api import attach_inspector_routes
from cairnmind.memory_published_format import definition_hash, identity_hash, plan_hash, task_fields
from cairnmind.plans import PlanManager
from cairnmind.store import Store

from conftest import as_person


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for args in (("init", "-q", "-b", "feature"), ("config", "user.name", "Ada Nowak"),
                 ("config", "user.email", "ada@example.invalid")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "README.md").write_text("demo\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "first"], check=True, capture_output=True)
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True,
                            text=True).stdout.strip()
    store = Store(tmp_path / "database")
    project = store.add_project("acts", root)
    return store, project["id"], root, commit


def audit_of(store, source_id):
    return json.loads(store.get_source(source_id)["content"])


def test_a_review_audit_names_the_person_branch_and_commit_and_hashes_a_task_definition(workspace):
    store, pid, root, commit = workspace
    source = store.snapshot(pid, "note://a", "Use cookies.", "note")
    record = store.remember(pid, {"kind": "decision", "content": "Use cookies."}, [source["id"]])
    result = Inspector(store).review(pid, record["id"], "confirm", 1)
    audit = audit_of(store, result["audit_source_id"])
    assert (audit["actor_name"], audit["branch"], audit["commit"]) == ("Ada Nowak", "feature", commit)
    assert audit["content_sha256"] == hashlib.sha256(b"Use cookies.").hexdigest()
    assert "definition_sha256" not in audit
    assert audit["identity_sha256"] == identity_hash("decision", record["key"], None)
    task = store.remember(pid, {"kind": "task", "content": "Ship it", "metadata": {"requires_test": True}}, [source["id"]])
    reviewed = Inspector(store).review(pid, task["id"], "confirm", task["version"])
    expected = definition_hash({"text": "Ship it", **task_fields(task["metadata"])})
    assert audit_of(store, reviewed["audit_source_id"])["definition_sha256"] == expected


def test_a_confirmed_record_no_person_covered_can_be_confirmed_again_but_not_twice(workspace):
    store, pid, root, _ = workspace
    source = store.snapshot(pid, "note://cli", "Agents may write facts.", "note")
    # What `remember --confirmed` or `tasks add` writes in a person's terminal (#177): confirmed here,
    # but with no review that hashes what was judged, so no act that travels covers it.
    record = as_person(store, pid, {"kind": "fact", "content": "Agents may write facts."}, [source["id"]])
    inspector = Inspector(store)
    confirmed = inspector.review(pid, record["id"], "confirm", 1)["record"]
    assert confirmed["version"] == 2
    with pytest.raises(ValueError, match="conflict"):
        inspector.review(pid, record["id"], "confirm", 2)
    # An edit after the review is not covered by it any more, so a person may confirm it again.
    edited = store.remember(pid, {"id": record["id"], "expected_version": 2, "content": "Agents may write notes."},
                            confirmed["source_ids"])
    assert edited["status"] == "proposed"  # The edit was not a person's: it is proposed again (#177).
    assert inspector.review(pid, record["id"], "confirm", edited["version"])["record"]["status"] == "confirmed"


def test_confirm_plan_confirms_the_definition_and_every_step_in_one_act(workspace, tmp_path):
    store, pid, root, commit = workspace
    saved = PlanManager(store).save(pid, {"title": "Ship", "goal": "Ship it", "constraints": ["Small"]},
                                    [{"key": "one", "content": "Step one"},
                                     {"key": "two", "content": "Step two", "depends_on": ["one"]}],
                                    "ship", worktree=str(root))
    inspector = Inspector(store)
    with pytest.raises(ValueError, match="plan version conflict"):
        confirm(inspector, pid, saved["plan"]["id"], saved["plan"]["version"] + 1)
    with pytest.raises(InspectorNotFound):
        confirm(inspector, pid, "00000000-0000-4000-8000-000000000000", 1)
    result = confirm(inspector, pid, saved["plan"]["id"], saved["plan"]["version"], reason="Looks right")
    assert [task["status"] for task in result["tasks"]] == ["confirmed", "confirmed"]
    assert [task["metadata"]["state"] for task in result["tasks"]] == ["todo", "todo"]
    audit = audit_of(store, result["audit_source_id"])
    core = {"title": "Ship", "goal": "Ship it", "constraints": ["Small"],
            "steps": [[task["id"], task["metadata"]["step_key"]] for task in saved["tasks"]]}
    assert audit["action"] == "confirm_plan" and audit["plan_sha256"] == plan_hash(core)
    assert audit["actor_name"] == "Ada Nowak" and audit["commit"] == commit and audit["reason"] == "Looks right"
    assert set(audit["definition_sha256"]) == {task["id"] for task in saved["tasks"]}
    assert result["plan_version"] == PlanManager(store).get(pid, saved["plan"]["id"])["plan"]["version"]
    app = FastAPI()
    attach_inspector_routes(app, store, store.settings)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + store.settings.token}
    stale = client.post(f"/inspector/plans/{saved['plan']['id']}/confirm", headers=headers,
                        json={"project": pid, "expected_plan_version": 1})
    assert stale.status_code == 409 and "plan version conflict" in stale.json()["detail"]


def test_a_coding_session_cannot_confirm_a_plan_from_the_command_line(workspace, monkeypatch):
    store, pid, root, _ = workspace
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "agent-session")
    result = CliRunner().invoke(cli.app, ["--data-dir", str(store.data_dir), "memory", "confirm-plan", pid,
                                          "00000000-0000-4000-8000-000000000000", "--expected-version", "1"])
    assert result.exit_code == 2 and "coding session cannot confirm a plan" in result.output


def test_a_conflict_decision_and_a_typed_finding_name_the_person(workspace):
    store, pid, root, commit = workspace
    first = store.snapshot(pid, "transcript://old", "Sessions last a day.", "transcript")
    existing = as_person(store, pid, {"kind": "decision", "key": "ttl", "content": "Sessions last a day."},
                         [first["id"]])
    second = store.snapshot(pid, "transcript://new", "Sessions last an hour.", "transcript")
    candidate = store.remember(pid, {"kind": "decision", "key": "ttl~a", "content": "Sessions last an hour.",
                                     "metadata": {"possible_conflicts": [existing["id"]], "suggested_key": "ttl"}},
                               [second["id"]])
    decided = conflicts.resolve_conflict(store, pid, candidate["id"], existing["id"], "supersede", 1, 1,
                                         keep=candidate["id"])
    audit = audit_of(store, decided["audit_source_id"])
    assert (audit["actor_name"], audit["branch"], audit["commit"]) == ("Ada Nowak", "feature", commit)
    # The audit hashes kind, key and replaced record as the decision leaves them: renamed, and replacing the other.
    kept = decided["record"]
    assert (kept["key"], kept["supersedes"]) == ("ttl", existing["id"])
    assert audit["identity_sha256"][candidate["id"]] == identity_hash("decision", "ttl", existing["id"])
    with store.transaction() as tx:
        assert covered_by_local_act(tx, kept)
    with store.transaction():
        saved = save_finding(store, pid, "8d2e4f60-1a3b-4c5d-9e7f-0a1b2c3d4e5f", "Never push for the user.",
                             "constraint", "confirmed")
    metadata = store.get_source(saved["source"]["id"])["metadata"]
    assert (metadata["actor_name"], metadata["branch"], metadata["commit"]) == ("Ada Nowak", "feature", commit)


def test_a_client_can_neither_forge_nor_clear_the_publication_mark(workspace):
    store, pid, root, _ = workspace
    source = store.snapshot(pid, "note://forged", "A fact.", "note")
    record = store.remember(pid, {"kind": "fact", "content": "A fact.",
                                  "metadata": {"publication": {"path": "fact/forged.md", "reviewed_by": []}}},
                            [source["id"]])
    assert "publication" not in record["metadata"]
