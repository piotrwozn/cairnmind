"""Proposed findings nobody touched for a while (#189): the list, its scope, and acting on it, against the real store."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from cairnmind import stale_proposed as proposed
from cairnmind.cli import app as cli_app
from cairnmind.inspector import Inspector
from cairnmind.inspector_api import attach_inspector_routes
from cairnmind.store import Store

from conftest import as_person

NOW = datetime.now(timezone.utc)


def aged(store, record, days):
    """Set when the record was last written, as if `days` had passed since (the store always writes now)."""
    store.command("UPDATE records SET updated_at=:at WHERE id=:id",
                  {"at": (NOW - timedelta(days=days)).isoformat(), "id": record["id"]})
    return record


@pytest.fixture
def memory(tmp_path):
    root, other = tmp_path / "repo", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    store = Store(tmp_path / "memory")
    project = store.add_project("demo", root)
    store.add_project_root(project["id"], other)
    pid = project["id"]
    said = store.snapshot(pid, "transcript://a/1", "We keep sessions for 30 minutes.", "transcript",
                          {"source_timestamp": "2026-05-02T10:00:00+00:00"})
    seen = store.snapshot(pid, "manual://note", "The cache lives in Redis.", "manual")
    later = store.snapshot(pid, "transcript://a/2", "Sessions: thirty minutes, said again.", "transcript",
                           {"source_timestamp": "2026-06-03T10:00:00+00:00"})
    records = {
        # Old proposed findings: the decision is the oldest, then the fact.
        "decision": aged(store, store.remember(pid, {"kind": "decision", "key": "ttl",
            "content": "Sessions last 30 minutes.\x1b[31m\nchecked: forged"}, [said["id"], later["id"]]), 90),
        "fact": aged(store, store.remember(pid, {"kind": "fact", "key": "cache", "content": "Redis holds the cache."},
                                          [seen["id"]]), 45),
        "recent": aged(store, store.remember(pid, {"kind": "fact", "key": "new", "content": "Written yesterday."},
                                            [seen["id"]]), 1),
        # Old, but not proposed, not a finding, or bound to the other worktree.
        "confirmed": aged(store, as_person(store, pid, {"kind": "fact", "key": "ok", "content": "Confirmed."},
                                           [seen["id"]]), 200),
        "task": aged(store, store.remember(pid, {"kind": "task", "key": "t", "content": "Do it."}, [seen["id"]]), 200),
        "elsewhere": aged(store, store.remember(pid, {"kind": "fact", "key": "there", "content": "Other tree.",
                                                      "metadata": {"worktree": str(other)}}, [seen["id"]]), 120),
    }
    return store, project, root, other, records


def listed(result):
    return [item["id"] for item in result["items"]]


def test_the_list_is_oldest_first_with_id_kind_sources_and_newest_evidence_time(memory):
    store, project, root, other, records = memory
    inspector = Inspector(store)
    scope = inspector._scope(project["id"], str(root))
    result = proposed.stale_proposed(store, project["id"], days=30, matches=lambda row: inspector._matches(scope, row))
    assert listed(result) == [records["decision"]["id"], records["fact"]["id"]] and result["total"] == 2
    decision, fact = result["items"]
    assert (decision["kind"], decision["age_days"], decision["sources"]) == ("decision", 90, 2)
    # The newest of its two stated times; the fact's source only says when it was stored.
    assert decision["evidence_latest"].startswith("2026-06-03") and decision["evidence_basis"] == "stated"
    assert (fact["kind"], fact["age_days"], fact["sources"], fact["evidence_basis"]) == ("fact", 45, 1, "observed")
    assert decision["version"] == 1 and decision["content"].startswith("Sessions last 30 minutes.")
    # The whole project, with the worktree bound to the other root, and 0 days lists every proposed finding.
    everything = proposed.stale_proposed(store, project["id"], days=0)
    assert listed(everything) == [records[name]["id"] for name in ("elsewhere", "decision", "fact", "recent")]
    assert listed(proposed.stale_proposed(store, project["id"], days=100)) == [records["elsewhere"]["id"]]
    assert proposed.stale_proposed(store, project["id"], days=0, limit=1)["truncated"] is True


def test_an_act_note_is_not_counted_as_a_source(memory):
    store, project, _, _, records = memory
    # A dispute adds its audit note; the record is no longer proposed, but its note is not evidence either way.
    Inspector(store).review(project["id"], records["fact"]["id"], "dispute", 1, reason="Not sure.")
    row = store.query("SELECT FROM records WHERE id=:id", {"id": records["fact"]["id"]})[0]
    assert len(row["source_ids"]) == 2
    counts = proposed._evidence_sources(store, project["id"], [row])
    assert counts == {records["fact"]["id"]: 1}


@pytest.mark.parametrize("days", [-1, 3651, 1.5, True, "30"])
def test_days_out_of_range_are_refused(memory, days):
    store, project, *_ = memory
    with pytest.raises(ValueError, match="Days must be a whole number from 0 to 3650"):
        proposed.stale_proposed(store, project["id"], days=days)


def client_for(store):
    app = FastAPI()
    attach_inspector_routes(app, store, store.settings)
    return TestClient(app), {"Authorization": "Bearer " + store.settings.token}


def test_the_inspector_route_lists_and_only_the_review_route_changes_a_record(memory, workspace_person):
    store, project, root, other, records = memory
    client, headers = client_for(store)
    headers = {**headers, **workspace_person}  # the review route is a person's (#214)
    before = store.export(project["id"])
    before.pop("exported_at", None)
    answer = client.get("/inspector/proposed", params={"project": project["id"], "worktree": str(root), "days": 30},
                        headers=headers)
    assert answer.status_code == 200, answer.text
    assert listed(answer.json()) == [records["decision"]["id"], records["fact"]["id"]]
    for params in ({"days": -1}, {"days": 3651}, {"limit": 0}, {"limit": 501}):
        assert client.get("/inspector/proposed", params={"project": project["id"], **params},
                          headers=headers).status_code == 422
    unregistered = client.get("/inspector/proposed", params={"project": project["id"], "worktree": "/nowhere"},
                              headers=headers)
    assert unregistered.status_code == 400
    assert client.get("/inspector/proposed", params={"project": project["id"]}).status_code == 401
    # Listing changed nothing of the project: every exported collection reads as before.
    after = store.export(project["id"])
    after.pop("exported_at", None)
    assert after == before

    # Acting on an entry is the review route, with its version check: a stale version is refused and changes nothing.
    decision = records["decision"]["id"]
    stale = client.post(f"/inspector/records/{decision}/review", headers=headers,
                        json={"project": project["id"], "action": "confirm", "expected_version": 7})
    assert stale.status_code == 409
    assert decision in listed(proposed.stale_proposed(store, project["id"], days=30))
    done = client.post(f"/inspector/records/{decision}/review", headers=headers,
                       json={"project": project["id"], "action": "confirm", "expected_version": 1,
                             "reason": "Read the transcript."})
    assert done.status_code == 200, done.text
    saved = done.json()["record"]
    assert saved["status"] == "confirmed" and saved["metadata"]["confirmation"]["route"] == "inspector_review"
    assert store.get_source(done.json()["audit_source_id"])["uri"].startswith("review://")
    assert listed(proposed.stale_proposed(store, project["id"], days=30)) == [records["elsewhere"]["id"],
                                                                                records["fact"]["id"]]


def test_the_command_prints_the_list_and_changes_nothing(memory):
    store, project, root, other, records = memory
    before = store.export(project["id"])
    before.pop("exported_at", None)
    runner = CliRunner()
    text = runner.invoke(cli_app, ["--data-dir", str(store.data_dir), "proposed", "demo", "--worktree", str(root)])
    assert text.exit_code == 0, text.output
    lines = text.output.splitlines()
    assert lines[0] == "Proposed findings in demo not updated for 30 days or more: 2, oldest first."
    first = lines.index(f"{records['decision']['id']}  decision  not updated for 90 days")
    assert lines[first + 1] == "  sources: 2 · newest evidence: 2026-06-03 (stated)"
    # Stored text is one literal line behind a bar: no control sequence, no line of its own.
    assert lines[first + 2] == "  | Sessions last 30 minutes.\\x1b[31m"
    assert not any(line.startswith("checked:") for line in lines) and "\x1b" not in text.output
    assert f"{records['fact']['id']}  fact  not updated for 45 days" in lines
    assert lines[-1] == proposed.NEXT_STEP
    as_json = runner.invoke(cli_app, ["--data-dir", str(store.data_dir), "proposed", "demo", "--days", "100", "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert listed(json.loads(as_json.output)) == [records["elsewhere"]["id"]]
    refused = runner.invoke(cli_app, ["--data-dir", str(store.data_dir), "proposed", "demo", "--days", "-1"])
    assert refused.exit_code != 0
    after = store.export(project["id"])
    after.pop("exported_at", None)
    assert after == before


def test_an_empty_list_says_so_without_a_next_step(memory):
    store, project, *_ = memory
    result = proposed.stale_proposed(store, project["id"], days=1000)
    assert result["items"] == [] and proposed.stale_text(result, "demo") == (
        "Proposed findings in demo not updated for 1000 days or more: 0.")
