"""A person's verdict on a record listed by `cairnmind impact` (issue #8, the actions)."""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from cairnmind.cli import app
from cairnmind.evidence import _is_audit, evidence_states
from cairnmind.impact import build_impact
from cairnmind.impact_verdicts import record_verdict
from cairnmind.inspector_api import attach_inspector_routes
from cairnmind.store import _clean

from test_cockpit_server import cockpit as cockpit, login
from test_impact import AUTH, link, workspace as workspace

RENEWED = AUTH.replace('":refreshed"', '":renewed"')


def entry(report, record):
    return next((row for row in report["records"] if row["id"] == record["id"]), None)


def verdicts(store, pid):
    return store.query("SELECT id,uri,is_current,content,metadata FROM sources WHERE project_id=:project "
                       "AND kind='manual' AND metadata.impact_verdict=true", {"project": pid})


def give(store, pid, record, verdict="still_valid", note=None, since="HEAD"):
    shown = entry(build_impact(store, pid, since), record)
    return record_verdict(store, pid, record["id"], shown["fingerprint"], verdict, since, note=note)


@pytest.fixture
def changed(workspace):
    store, pid, root, records = workspace
    (root / "src" / "auth.py").write_text(RENEWED)
    return workspace


def test_a_verdict_is_shown_on_the_next_scan_and_only_for_its_record(changed):
    store, pid, root, records = changed
    before = build_impact(store, pid, "HEAD")
    assert all(row["verdict"] is None and len(row["fingerprint"]) == 64 for row in before["records"])
    assert "checked:" not in before["text"]
    saved = give(store, pid, records["rule"], "not_related", note="  only the name matches ")
    assert saved["already_saved"] is False and saved["verdict"]["note"] == "only the name matches"
    after = build_impact(store, pid, "HEAD")
    rule, task = entry(after, records["rule"]), entry(after, records["task"])
    assert rule["verdict"]["verdict"] == "not_related" and rule["verdict"]["note"] == "only the name matches"
    assert task["verdict"] is None
    day = rule["verdict"]["at"][:10]
    lines = after["text"].splitlines()
    names = lines.index("  - names refresh_token in src/auth.py, which was modified; "
                        "its definition changed (link proposed)")
    assert lines[names + 1] == f"  checked: not related ({day}): only the name matches"
    assert after["text"].count("checked:") == 1
    # Everything printed before is still printed, in the same order.
    assert [line for line in lines if not line.startswith("  checked:")] == before["text"].splitlines()


def test_saving_the_same_verdict_twice_stores_one_source(changed):
    store, pid, root, records = changed
    first = give(store, pid, records["rule"])
    second = give(store, pid, records["rule"])
    assert second["already_saved"] is True and second["audit_source_id"] == first["audit_source_id"]
    assert len(verdicts(store, pid)) == 1


def test_a_changed_verdict_replaces_the_current_one_and_keeps_the_earlier_as_history(changed):
    store, pid, root, records = changed
    first = give(store, pid, records["rule"])
    second = give(store, pid, records["rule"], "needs_update", note="Say that tokens are renewed.")
    rows = {row["id"]: row for row in verdicts(store, pid)}
    earlier, now = rows[first["audit_source_id"]], rows[second["audit_source_id"]]
    assert len(rows) == 2 and earlier["is_current"] is False and now["is_current"] is True
    assert earlier["uri"] == now["uri"] and json.loads(earlier["content"])["verdict"] == "still_valid"
    assert entry(build_impact(store, pid, "HEAD"), records["rule"])["verdict"]["verdict"] == "needs_update"


def test_editing_the_definition_again_makes_the_entry_unreviewed(changed):
    store, pid, root, records = changed
    give(store, pid, records["rule"])
    seen = entry(build_impact(store, pid, "HEAD"), records["rule"])
    # Another definition of the same file is not what the record names: the verdict stands.
    (root / "src" / "auth.py").write_text(RENEWED.replace("return None", "return False"))
    assert entry(build_impact(store, pid, "HEAD"), records["rule"])["verdict"]["verdict"] == "still_valid"
    (root / "src" / "auth.py").write_text(RENEWED.replace('":renewed"', '":rotated"'))
    again = entry(build_impact(store, pid, "HEAD"), records["rule"])
    assert again["verdict"] is None and again["fingerprint"] != seen["fingerprint"]
    with pytest.raises(ValueError, match="changed since this list was shown"):
        record_verdict(store, pid, records["rule"]["id"], seen["fingerprint"], "still_valid", "HEAD")


def test_a_verdict_on_a_record_linked_to_a_constant_follows_the_constant_s_value(workspace):
    store, pid, root, records = workspace
    constant = store.query("SELECT id FROM nodes WHERE project_id=:project AND kind='symbol' AND label='SESSION_MINUTES'",
                           {"project": pid})[0]["id"]
    link(store, pid, records["rule"], constant, records["rule"]["source_ids"][0])
    (root / "src" / "auth.py").write_text(AUTH.replace("= 30", "= 45"))
    shown = entry(build_impact(store, pid, "HEAD"), records["rule"])
    assert {item["symbol"] for item in shown["links"] if "symbol" in item} == {"SESSION_MINUTES"}
    give(store, pid, records["rule"])
    # An edit elsewhere in the file leaves the constant, so the verdict still holds.
    (root / "src" / "auth.py").write_text(AUTH.replace("= 30", "= 45").replace("return None", "return False"))
    again = entry(build_impact(store, pid, "HEAD"), records["rule"])
    assert again["fingerprint"] == shown["fingerprint"] and again["verdict"]["verdict"] == "still_valid"
    # A new value of the constant is a new entry, not yet reviewed.
    (root / "src" / "auth.py").write_text(AUTH.replace("= 30", "= 60"))
    later = entry(build_impact(store, pid, "HEAD"), records["rule"])
    assert later["fingerprint"] != shown["fingerprint"] and later["verdict"] is None


def test_editing_the_record_makes_the_entry_unreviewed(changed):
    store, pid, root, records = changed
    give(store, pid, records["rule"])
    rule = records["rule"]
    store.remember(pid, {"id": rule["id"], "expected_version": rule["version"],
                         "content": "Sessions last 60 minutes."}, rule["source_ids"])
    assert entry(build_impact(store, pid, "HEAD"), rule)["verdict"] is None


@pytest.mark.parametrize("note", [None, "", "   "])
def test_needs_update_without_a_note_is_refused(changed, note):
    store, pid, root, records = changed
    with pytest.raises(ValueError, match="requires a note"):
        give(store, pid, records["rule"], "needs_update", note=note)
    assert verdicts(store, pid) == []


def test_a_stale_fingerprint_a_record_not_listed_and_an_unknown_verdict_are_refused(changed):
    store, pid, root, records = changed
    rule = entry(build_impact(store, pid, "HEAD"), records["rule"])
    with pytest.raises(ValueError, match="changed since this list was shown"):
        record_verdict(store, pid, rule["id"], "0" * 64, "still_valid", "HEAD")
    with pytest.raises(LookupError, match="not in the impact list"):
        record_verdict(store, pid, records["revoke"]["id"], rule["fingerprint"], "still_valid", "HEAD")
    with pytest.raises(ValueError, match="must be still_valid"):
        record_verdict(store, pid, rule["id"], rule["fingerprint"], "wrong", "HEAD")
    assert verdicts(store, pid) == []


def test_a_record_of_another_project_is_refused(changed, tmp_path):
    store, pid, root, records = changed
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = store.add_project("other", other_root)["id"]
    note = store.snapshot(other, "chat://other", "Other.", kind="conversation")["id"]
    foreign = store.remember(other, {"kind": "fact", "content": "Belongs elsewhere."}, [note])
    shown = entry(build_impact(store, pid, "HEAD"), records["rule"])
    with pytest.raises(LookupError):
        record_verdict(store, pid, foreign["id"], shown["fingerprint"], "still_valid", "HEAD")
    # Naming the other project with this project's record and worktree is refused too.
    with pytest.raises(ValueError):
        record_verdict(store, other, records["rule"]["id"], shown["fingerprint"], "still_valid", "HEAD",
                       str(root))
    assert verdicts(store, pid) == [] and verdicts(store, other) == []


def test_records_their_revisions_and_a_task_state_are_identical_after_verdicts(changed):
    store, pid, root, records = changed

    def stored():
        return {name: sorted(json.dumps(_clean(row), sort_keys=True, default=str) for row in store.query(
            f"SELECT FROM {name} WHERE project_id=:project", {"project": pid}))
            for name in ("records", "record_revisions")}

    before = stored()
    give(store, pid, records["rule"], "needs_update", note="Check the renewal.")
    give(store, pid, records["task"], "still_valid")
    give(store, pid, records["task"], "not_related")
    assert stored() == before
    task = store.query("SELECT FROM records WHERE id=:id", {"id": records["task"]["id"]})[0]
    assert task["metadata"]["state"] == "todo" and task["status"] == records["task"]["status"]
    tasks = store.query("SELECT id FROM records WHERE project_id=:project AND kind='task'", {"project": pid})
    assert [row["id"] for row in tasks] == [records["task"]["id"]]  # no task was created


def test_the_verdict_source_is_an_audit_note_and_never_evidence(changed):
    store, pid, root, records = changed
    saved = give(store, pid, records["rule"])
    source = store.get_source(saved["audit_source_id"])
    assert source["uri"].startswith("review://impact/") and source["kind"] == "manual" and _is_audit(source)
    assert saved["audit_source_id"] not in store.query("SELECT source_ids FROM records WHERE id=:id",
                                                       {"id": records["rule"]["id"]})[0]["source_ids"]
    # Even a record that cited nothing but the verdict would have no evidence.
    cited = {**records["rule"], "source_ids": [saved["audit_source_id"]]}
    assert evidence_states(store, pid, [cited])[cited["id"]]["evidence_state"] == "orphaned"


def test_pending_lists_only_entries_without_a_current_verdict(changed):
    store, pid, root, records = changed
    give(store, pid, records["rule"])
    runner = CliRunner()
    base = ["--data-dir", str(store.data_dir), "impact", "impact", "--since", "HEAD"]
    every = runner.invoke(app, base)
    assert every.exit_code == 0, every.output
    assert "Sessions last 30 minutes." in every.output and "  checked: still valid (" in every.output
    pending = runner.invoke(app, [*base, "--pending"])
    assert pending.exit_code == 0, pending.output
    assert "Sessions last 30 minutes." not in pending.output and "Rotate refresh tokens." in pending.output
    assert pending.output.rstrip().splitlines()[-1] == "1 record already checked and not shown (--pending)"
    as_json = json.loads(runner.invoke(app, [*base, "--pending", "--json"]).output)
    assert [row["id"] for row in as_json["records"]] == [records["task"]["id"]]


def test_the_command_records_a_verdict_and_refuses_inside_a_coding_session(changed, monkeypatch):
    store, pid, root, records = changed
    shown = entry(build_impact(store, pid, "HEAD"), records["rule"])
    command = ["--data-dir", str(store.data_dir), "impact-verdict", "impact", records["rule"]["id"],
               "--fingerprint", shown["fingerprint"], "--verdict", "still_valid", "--since", "HEAD"]
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.setenv(name, "logical-session")
        refused = CliRunner().invoke(app, command)
        assert refused.exit_code == 2 and "coding session cannot record" in refused.output
        monkeypatch.delenv(name)
    assert verdicts(store, pid) == []
    saved = CliRunner().invoke(app, command)
    assert saved.exit_code == 0, saved.output
    assert json.loads(saved.output)["verdict"]["verdict"] == "still_valid" and len(verdicts(store, pid)) == 1
    missing = CliRunner().invoke(app, [*command[:4], records["revoke"]["id"], *command[5:]])
    assert missing.exit_code == 2 and "not in the impact list" in missing.output


def test_the_inspector_route_takes_a_person_s_verdict_and_no_session(changed):
    store, pid, root, records = changed
    service = FastAPI()
    attach_inspector_routes(service, store, store.settings)
    client = TestClient(service)
    headers = {"Authorization": "Bearer " + store.settings.token}
    shown = entry(build_impact(store, pid, "HEAD"), records["rule"])
    body = {"project": pid, "worktree": str(root), "since": "HEAD", "record_id": shown["id"],
            "fingerprint": shown["fingerprint"], "verdict": "needs_update", "note": "Mention renewal."}

    def post(payload, auth=True):
        return client.post("/inspector/impact/verdict", json=payload, headers=headers if auth else {})

    assert post(body, auth=False).status_code == 401
    assert post({**body, "session_id": "s"}).status_code == 422
    assert post({**body, "note": None}).status_code == 400
    assert post({**body, "fingerprint": "0" * 64}).status_code == 409
    assert post({**body, "record_id": records["revoke"]["id"]}).status_code == 404
    assert post({**body, "project": "no-such-project"}).status_code == 404
    assert verdicts(store, pid) == []
    saved = post(body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["verdict"]["verdict"] == "needs_update" and len(verdicts(store, pid)) == 1


def test_the_broker_lets_through_the_verdict_and_no_other_new_write(cockpit):
    settings, service, client, runtime, project, requests, _ = cockpit
    headers = login(service, client)
    body = {"project": project["id"], "since": "HEAD", "record_id": "a", "fingerprint": "0" * 64,
            "verdict": "still_valid"}
    assert client.post("/api/inspector/impact/verdict", json=body, headers=headers).status_code == 200
    method, path, content = requests[-1]
    assert (method, path) == ("POST", "/inspector/impact/verdict") and json.loads(content) == body
    sent = len(requests)
    for path in ("impact", "impact/verdict/extra", "impact/verdicts", "impact/x/verdict", "verdict"):
        assert client.post("/api/inspector/" + path, json=body, headers=headers).status_code == 405
    assert len(requests) == sent
