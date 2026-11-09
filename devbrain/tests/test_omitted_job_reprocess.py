"""A completed all-rejected extraction requires explicit, scoped reprocessing."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from devbrain.service import create_app
from devbrain.sessions import SessionManager
from devbrain.store import JobNotFoundError, Store, job_diagnostics


OMITTED = {"stage": "extract", "code": "completed_with_omissions", "repair_attempted": False,
           "repair_outcome": "not_needed", "validation_codes": ["proposal_rejected"],
           "counts": {"candidate_records": 6, "accepted_records": 0, "written_records": 0, "skipped_records": 6}}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("VALUE = 1\n")
    for args in (("init", "-q"), ("config", "user.name", "Omissions fixture"),
                 ("config", "user.email", "omissions@example.invalid"), ("add", "."), ("commit", "-qm", "initial")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    store = Store(tmp_path / "memory")
    project = store.add_project("omissions", root)
    source = store.snapshot(project["id"], "conversation://source", "Use explicit evidence.", "transcript")
    store.remember(project["id"], {"kind": "constraint", "content": "Preserve the archive."}, [source["id"]])
    return store, project, root, source


def completed(store, project, source, *, diagnostics=None, kind="extract"):
    jid = store.enqueue(kind, project["id"], {"source_id": source["id"]}, "same-job")
    first = store.claim_job("inference", kinds=[kind])
    store.finish_job(jid, "Original historical failure", worker=first["worker"], expected_attempts=1)
    store.retry_job(project["id"], jid, 1)
    second = store.claim_job("inference", kinds=[kind])
    assert second["attempts"] == 2
    store.set_job_diagnostics(jid, diagnostics if diagnostics is not None else deepcopy(OMITTED),
                              expected_attempts=2, worker=second["worker"])
    store.finish_job(jid, worker=second["worker"], expected_attempts=2)
    return jid


def snapshot(store):
    result = store.export()
    result.pop("exported_at")
    return result


def test_default_retry_and_legacy_global_retry_leave_completed_omissions_unchanged(workspace):
    store, project, _, source = workspace
    jid = completed(store, project, source)
    before = snapshot(store)
    with pytest.raises(ValueError, match="conflict"):
        store.retry_job(project["id"], jid, 2)
    with pytest.raises(ValueError, match="conflict"):
        store.retry_job(project["id"], jid, 2, include_omissions=False)
    assert store.retry_jobs()["retried"] == 0
    assert snapshot(store) == before


def test_explicit_all_rejected_retry_preserves_job_identity_history_and_project_data(workspace):
    store, project, _, source = workspace
    jid = completed(store, project, source)
    before = snapshot(store)
    result = store.retry_job(project["id"], jid, 2, include_omissions=True)
    assert result["id"] == jid and result["state"] == "pending" and result["attempts"] == 2
    assert result["retry_count"] == 2
    assert result["attempt_history"][-1]["event"] == "retry_requested"
    assert result["attempt_history"][-1]["state"] == "completed"
    assert result["attempt_history"][-1]["diagnostics"] == OMITTED
    after = snapshot(store)
    assert len(after["jobs"]) == len(before["jobs"]) == 1
    assert after["jobs"][0]["last_error"] == "Original historical failure"
    assert after["jobs"][0]["attempt_history"][:-1] == before["jobs"][0]["attempt_history"]
    operational = {"jobs", "journal_state", "journal_events"}
    assert {key: value for key, value in after.items() if key not in operational} == {
        key: value for key, value in before.items() if key not in operational}
    previous_cursor = next(row for row in before["journal_state"] if row["id"] == "current")
    current_cursor = next(row for row in after["journal_state"] if row["id"] == "current")
    assert current_cursor["epoch"] == previous_cursor["epoch"]
    assert current_cursor["seq"] > previous_cursor["seq"]
    new_events = [row for row in after["journal_events"] if row["epoch"] == current_cursor["epoch"]
                  and row["seq"] > previous_cursor["seq"]]
    assert any(row["project_id"] == project["id"] and row["entity_type"] == "job"
               and row["entity_id"] == jid and row["op"] == "upsert" for row in new_events)
    assert store.claim_job("new-worker", kinds=["extract"])["attempts"] == 3
    with pytest.raises(ValueError, match="conflict"):
        store.retry_job(project["id"], jid, 2, include_omissions=True)


def test_omissions_retry_uses_project_and_attempt_cas_across_connections(workspace, tmp_path):
    store, project, _, source = workspace
    jid = completed(store, project, source)
    other = store.add_project("other", tmp_path / "other")
    with pytest.raises(JobNotFoundError):
        store.retry_job(other["id"], jid, 2, include_omissions=True)
    with pytest.raises(ValueError, match="conflict"):
        store.retry_job(project["id"], jid, 1, include_omissions=True)
    peer = Store(store.data_dir)

    def retry(connection):
        try:
            connection.retry_job(project["id"], jid, 2, include_omissions=True)
            return "retried"
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(retry, [store, peer])) == ["conflict", "retried"]
    assert store.job_status(project["id"], jid)["retry_count"] == 2


@pytest.mark.parametrize("patch", [
    {"code": "ok"},
    {"validation_codes": []},
    {"validation_codes": "proposal_rejected"},
    {"counts": {"candidate_records": 0, "accepted_records": 0, "written_records": 0, "skipped_records": 0}},
    {"counts": {"candidate_records": 6, "accepted_records": 6, "written_records": 0, "skipped_records": 6}},
    {"counts": {"candidate_records": 6, "accepted_records": 1, "written_records": 1, "skipped_records": 5}},
    {"counts": {"candidate_records": 6, "accepted_records": 0, "written_records": 0, "skipped_records": 5}},
    {"counts": {"candidate_records": 6, "accepted_records": False, "written_records": 0, "skipped_records": 6}},
    {"counts": {"candidate_records": 6.0, "accepted_records": 0, "written_records": 0, "skipped_records": 6}},
    {"counts": {"candidate_records": 6, "accepted_records": 0, "written_records": 0}},
])
def test_empty_deduplicated_partial_success_or_malformed_diagnostics_are_ineligible(workspace, patch):
    store, project, _, source = workspace
    diagnostics = {**deepcopy(OMITTED), **patch}
    jid = completed(store, project, source, diagnostics=diagnostics)
    before = snapshot(store)
    with pytest.raises(ValueError, match="conflict"):
        store.retry_job(project["id"], jid, 2, include_omissions=True)
    assert snapshot(store) == before


def test_other_job_kinds_and_nonboolean_opt_in_are_rejected(workspace):
    store, project, _, source = workspace
    jid = completed(store, project, source, kind="import")
    with pytest.raises(ValueError, match="conflict"):
        store.retry_job(project["id"], jid, 2, include_omissions=True)
    for invalid in (1, 0, "true", None):
        with pytest.raises(ValueError, match="boolean"):
            store.retry_job(project["id"], jid, 2, include_omissions=invalid)


def test_rejection_counts_are_bounded_fixed_integer_fields_only():
    raw = {"rejection_counts": {"blank_content": 2, "blank_evidence": False, "quote_absent_source": 3,
                              "reserved_metadata": 6, "PRIVATE_SOURCE": "PRIVATE_VALUE"}}
    result = job_diagnostics(raw)
    assert result == {"rejection_counts": {"blank_content": 2, "quote_absent_source": 3, "reserved_metadata": 6}}
    assert "PRIVATE" not in json.dumps(result)
    assert job_diagnostics({"rejection_counts": {"blank_content": -1, "blank_evidence": 1.0,
                            "quote_absent_source": 10**16, "reserved_metadata": None}}) == {"rejection_counts": {}}
    assert job_diagnostics({"rejection_counts": "PRIVATE_VALUE"}) == {}
    assert job_diagnostics({}) == {}


def test_http_omissions_opt_in_is_strict_and_requires_current_project_ack(workspace, tmp_path):
    store, project, _, source = workspace
    jid = completed(store, project, source)
    other = store.add_project("other", tmp_path / "other")
    manager = SessionManager(store, store.settings)
    session = manager.prepare(project["id"], "codex", timeout=0)
    headers = {"Authorization": f"Bearer {store.settings.token}"}
    body = {"project": project["name"], "expected_attempts": 2, "include_omissions": True}
    with TestClient(create_app(store.settings, start_worker=False)) as client:
        assert client.post(f"/jobs/{jid}/retry", json=body).status_code == 401
        assert client.post(f"/jobs/{jid}/retry", json={**body, "project": other["name"]}, headers=headers).status_code == 404
        for invalid in ("true", 1, None):
            assert client.post(f"/jobs/{jid}/retry", json={**body, "include_omissions": invalid}, headers=headers).status_code == 422
        assert client.post(f"/jobs/{jid}/retry", json={"project": project["name"], "expected_attempts": 2}, headers=headers).status_code == 409
        scoped = {**body, "session_id": session["session_id"]}
        assert client.post(f"/jobs/{jid}/retry", json=scoped, headers=headers).status_code == 409
        packet = session["packet"]
        manager.ack(session["session_id"], packet["id"], packet["hash"], {"plan": None, "task": None})
        accepted = client.post(f"/jobs/{jid}/retry", json=scoped, headers=headers)
        assert accepted.status_code == 200 and accepted.json()["job"]["id"] == jid
        assert accepted.json()["job"]["attempts"] == 2 and accepted.json()["job"]["state"] == "pending"
        assert client.post(f"/jobs/{jid}/retry", json=scoped, headers=headers).status_code == 409
