"""Operational diagnostics use stored evidence and never silently retry history."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from cairnmind.ingest import import_history
from cairnmind.integrations import _transcript_watermark, drain_hook_spool
from cairnmind.service import create_app, finish_job_if_present
from cairnmind.sessions import SessionManager
from cairnmind.store import JobNotFoundError, Store, job_diagnostics
from cairnmind.sql import json_text


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("VALUE = 1\n")
    for args in (("init", "-q"), ("config", "user.name", "Jobs fixture"),
                 ("config", "user.email", "jobs@example.invalid"), ("add", "."), ("commit", "-qm", "initial")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    store = Store(tmp_path / "memory")
    project = store.add_project("jobs", root)
    return store, project, root


def failed_job(store, project, *, kind="extract", payload=None, key="one", error="original private model text"):
    jid = store.enqueue(kind, project["id"], payload or {"source_id": "source-one"}, key)
    job = store.claim_job("fixture-worker", kinds=[kind])
    assert job["id"] == jid
    store.finish_job(jid, error, worker="fixture-worker")
    return jid


def test_scoped_retry_is_one_attempt_cas_and_preserves_history(workspace, tmp_path):
    store, project, _ = workspace
    jid = failed_job(store, project)
    other = store.add_project("other", tmp_path / "other")
    other_id = failed_job(store, other, key="other")
    original = store.query("SELECT FROM jobs WHERE id=:id", {"id": jid})[0]
    peer = Store(store.data_dir)

    def retry(connection):
        try:
            connection.retry_job(project["id"], jid, 1)
            return "retried"
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(retry, [store, peer])) == ["conflict", "retried"]
    current = store.query("SELECT FROM jobs WHERE id=:id", {"id": jid})[0]
    assert current["state"] == "pending" and current["attempts"] == 1
    assert current["last_error"] == original["error"]
    assert [event["event"] for event in current["attempt_history"]] == ["finished", "retry_requested"]
    assert all(event["error"] == original["error"] for event in current["attempt_history"])
    assert store.job_status(other["id"], other_id)["state"] == "failed"
    with pytest.raises(JobNotFoundError):
        store.retry_job(other["id"], jid, 1)
    assert store.claim_job("second", kinds=["extract"])["attempts"] == 2
    with pytest.raises(ValueError, match="conflict"):
        store.retry_job(project["id"], jid, 2)


def test_diagnostics_are_bounded_and_fenced_to_current_attempt(workspace):
    store, project, _ = workspace
    jid = store.enqueue("extract", project["id"], {}, "diagnostics")
    job = store.claim_job("first", kinds=["extract"])
    diagnostics = {"stage": "extract", "code": "invalid_model_format", "repair_attempted": True,
                   "repair_outcome": "failed", "raw_response": "PRIVATE", "validation_codes": ["invalid_json"] * 12,
                   "model_calls": [{"phase": "repair", "done_reason": "length", "eval_count": 12, "secret": "PRIVATE"}],
                   "budget": {"elapsed_seconds": 2.0, "timeout_seconds": float("inf"), "secret": 3},
                   "counts": {"accepted_records": 0, "written_records": 0}}
    assert store.set_job_diagnostics(jid, diagnostics, expected_attempts=job["attempts"], worker="first")
    store.command("UPDATE jobs SET lease_until=:lease WHERE id=:id", {"lease": time.time() - 1, "id": jid})
    new = store.claim_job("second", kinds=["extract"])
    assert new["attempts"] == 2 and new["diagnostics"] == {}
    assert not store.set_job_diagnostics(jid, diagnostics, expected_attempts=1, worker="first")
    assert not store.set_job_diagnostics(jid, diagnostics, expected_attempts=2, worker="first")
    assert store.set_job_diagnostics(jid, diagnostics, expected_attempts=2, worker="second")
    store.finish_job(jid, "PRIVATE", worker="second")
    public = store.job_status(project["id"], jid)
    assert "PRIVATE" not in json.dumps(public)
    assert public["diagnostics"] == job_diagnostics(diagnostics)
    assert len(public["diagnostics"]["validation_codes"]) == 8
    assert "timeout_seconds" not in public["diagnostics"]["budget"]
    assert public["attempt_history"][0]["event"] == "lease_expired"
    assert public["attempt_history"][0]["diagnostics"]["repair_attempted"] is True
    assert not store.set_job_diagnostics(jid, {}, expected_attempts=2, worker="second")
    store.forget_project(project["id"])
    assert not store.set_job_diagnostics(jid, diagnostics)


def test_stale_finish_cannot_fail_reclaimed_attempt_with_the_same_worker(workspace):
    store, project, _ = workspace
    jid = store.enqueue("extract", project["id"], {}, "stale-finish")
    first = store.claim_job("inference", kinds=["extract"])
    store.command("UPDATE jobs SET lease_until=:lease WHERE id=:id", {"lease": time.time() - 1, "id": jid})
    second = store.claim_job("inference", kinds=["extract"])
    with pytest.raises(ValueError, match="stale"):
        store.finish_job(jid, "old failure", worker="inference", expected_attempts=first["attempts"])
    finish_job_if_present(store, jid, "old failure", worker="inference", expected_attempts=first["attempts"])
    assert store.job_status(project["id"], jid)["state"] == "running"
    assert store.job_status(project["id"], jid)["attempts"] == second["attempts"]
    finish_job_if_present(store, jid, worker="inference", expected_attempts=second["attempts"])
    assert store.job_status(project["id"], jid)["state"] == "completed"


@pytest.mark.parametrize("error,code", [
    ("/private/path: Unknown transcript schema", "unknown_transcript_schema"),
    ("/private/path: Invalid complete JSONL record: PRIVATE", "invalid_jsonl"),
    ("[Errno 2] No such file or directory: '/private/path'", "transcript_missing"),
])
def test_public_legacy_errors_are_actionable_without_raw_exceptions(workspace, error, code):
    store, project, _ = workspace
    jid = failed_job(store, project, kind="import", payload={"path": "/history", "client": "claude", "secret": "PRIVATE"}, error=error)
    public = store.job_status(project["id"], jid)
    assert public["error_code"] == code and public["error"]
    assert public["coverage"]["status"] == "scope_unknown"
    assert "PRIVATE" not in json.dumps(public) and "/private/path" not in json.dumps(public)
    assert store.recent_job_errors(project["id"])[0]["error_code"] == code
    assert store.query("SELECT error FROM jobs WHERE id=:id", {"id": jid})[0]["error"] == error


def test_list_filters_pipeline_counts_and_legacy_global_retry_audit(workspace, tmp_path):
    store, project, _ = workspace
    first = failed_job(store, project, kind="import", payload={"path": "/history", "client": "codex"})
    other = store.add_project("other", tmp_path / "other")
    second = failed_job(store, other, kind="embed", key="two")
    assert [row["id"] for row in store.list_jobs(project["id"], state="failed", kind="import")] == [first]
    assert store.job_summary(project["id"])["archive"]["failed"] == 1
    assert store.job_summary(project["id"])["indexing"]["failed"] == 0
    assert store.job_summary()["indexing"]["failed"] == 1
    for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"state": "done"}):
        with pytest.raises(ValueError):
            store.list_jobs(project["id"], **kwargs)
    assert store.retry_jobs()["retried"] == 2
    for pid, jid in ((project["id"], first), (other["id"], second)):
        job = store.job_status(pid, jid)
        assert job["state"] == "pending" and job["retry_count"] == 1
        assert job["attempt_history"][-1]["event"] == "retry_requested"
    assert store.retry_jobs()["retried"] == 0


def test_diagnostics_and_attempt_audit_round_trip_existing_schema(workspace, tmp_path):
    store, project, _ = workspace
    jid = failed_job(store, project)
    store.retry_job(project["id"], jid, 1)
    before = store.export(project["id"])
    backup = store.backup(tmp_path / "jobs.json.gz")
    store.forget_project(project["id"])
    store.restore(backup)
    after = store.export(project["id"])
    assert before["jobs"] == after["jobs"]
    assert after["jobs"][0]["attempt_history"][0]["error"] == "original private model text"


def test_published_capture_proof_is_pure_and_does_not_rewrite_failed_history(workspace, tmp_path, monkeypatch):
    store, project, root = workspace
    history = tmp_path / "transcript.jsonl"
    history.write_text(json_text({"type": "user", "cwd": str(root), "sessionId": "native",
                              "message": {"role": "user", "content": "Remember this instruction"}}) + "\n")
    manager = SessionManager(store, store.settings)
    initial = manager.prepare(project["id"], "claude", timeout=0)
    capture = _transcript_watermark(history)
    payload = {"event_id": "capture-one", "devbrain_session_id": initial["session_id"],
               "session_id": "native", "hook_event_name": "SessionStart", "cwd": str(root),
               "transcript_path": str(history), "transcript_watermark": capture}
    manager.note_event(payload, "claude")
    import_history(store, history, project["id"], "claude")
    published = manager.prepare(project["id"], "claude", session_id=initial["session_id"], timeout=0)
    assert published["status"] == "ready"
    assert published["packet"]["archive_watermarks"][0]["status"] == "complete"
    jid = failed_job(store, project, kind="import", payload={"path": str(history), "client": "claude",
        "origin_event_id": payload["event_id"], "requested_watermark": capture}, error="Old import failure")
    legacy = failed_job(store, project, kind="import", payload={"path": str(history), "client": "claude"}, key="legacy")
    before = store.export(project["id"])
    history.unlink()  # A status result proves the captured horizon, not current disk availability.
    monkeypatch.setattr(SessionManager, "_archive", lambda *a, **k: pytest.fail("status attempted archive repair"))
    monkeypatch.setattr(SessionManager, "prepare", lambda *a, **k: pytest.fail("status prepared a new packet"))
    complete = store.job_status(project["id"], jid)
    assert complete["state"] == "failed" and complete["coverage"]["status"] == "captured_complete"
    assert store.job_status(project["id"], legacy)["coverage"]["status"] == "scope_unknown"
    assert store.job_summary(project["id"])["archive"]["failed"] == 2
    store.list_jobs(project["id"])
    store.recent_job_errors(project["id"])
    after = store.export(project["id"])
    before.pop("exported_at")
    after.pop("exported_at")
    assert after == before


def test_foreign_capture_receipt_and_invalid_published_source_set_never_complete(workspace):
    store, project, _ = workspace
    capture = {"size": 3, "device": 1, "inode": 2, "head_bytes": 3,
               "head_sha256": hashlib.sha256(b"abc").hexdigest()}
    jid = failed_job(store, project, kind="import", payload={"path": "/history", "client": "claude",
        "origin_event_id": "missing", "requested_watermark": capture})
    assert store.job_status(project["id"], jid)["coverage"]["reason"] == "capture_receipt_unavailable"
    store.command("INSERT INTO session_events CONTENT :event", {"event": {
        "id": "missing", "project_id": project["id"], "client": "claude", "transcript_path": "/history",
        "transcript_watermark": capture}})
    proof = {"id": "proof", "project_id": project["id"], "created_at": "now", "payload": {"archive_watermarks": [
        {**capture, "path": "/history", "client": "claude", "status": "complete", "archived_offset": 3,
         "source_count": 1, "source_ids_hash": hashlib.sha256(json_text(["missing-source"]).encode()).hexdigest()}]}}
    store.command("INSERT INTO resume_packets CONTENT :proof", {"proof": proof})
    assert store.job_status(project["id"], jid)["coverage"]["reason"] == "published_source_set_unavailable"
    store.command("UPDATE resume_packets SET payload=:payload WHERE id='proof'", {"payload": {
        "archive_watermarks": [{**capture, "path": "/history", "client": "claude", "status": "partial"}]}})
    assert store.job_status(project["id"], jid)["coverage"]["status"] == "captured_blocked"


def test_hook_import_job_retains_the_exact_durable_capture_receipt(workspace, tmp_path):
    store, project, root = workspace
    history = tmp_path / "hook.jsonl"
    history.write_text('{}\n')
    capture = _transcript_watermark(history)
    session = SessionManager(store, store.settings).prepare(project["id"], "claude", timeout=0)
    payload = {"event_id": "native-hook", "client": "claude", "cwd": str(root), "session_id": "native",
               "devbrain_session_id": session["session_id"], "hook_event_name": "SessionStart",
               "transcript_path": str(history), "transcript_watermark": capture}
    spool = store.data_dir / "hook-events"
    spool.mkdir()
    (spool / "native-hook.json").write_text(json.dumps(payload))
    result = drain_hook_spool(store.settings, store)
    assert result["queued"] == result["events"] == 1 and not result["errors"]
    queued = store.query("SELECT payload FROM jobs WHERE kind='import'")[0]["payload"]
    receipt = store.query("SELECT transcript_watermark FROM session_events WHERE id='native-hook'")[0]
    assert queued["origin_event_id"] == payload["event_id"]
    assert queued["requested_watermark"] == receipt["transcript_watermark"] == capture


def test_outer_ask_timeout_reports_only_known_safe_diagnostics(workspace, monkeypatch):
    store, _, _ = workspace
    app = create_app(store.settings, start_worker=False)

    async def timeout(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(app.state.inference, "submit", timeout)
    with TestClient(app) as client:
        result = client.post("/ask", json={"project": "jobs", "question": "What next?"},
                             headers={"Authorization": f"Bearer {store.settings.token}"})
    assert result.status_code == 200 and result.json()["status"] == "partial"
    assert result.json()["diagnostics"] == {"stage": "ask", "code": "queue_timeout",
        "budget": {"timeout_seconds": store.settings.ask_timeout + 5}}


def test_http_jobs_are_authenticated_scoped_and_retry_requires_native_ack(workspace, tmp_path):
    store, project, _ = workspace
    jid = failed_job(store, project, kind="import", payload={"path": "/history", "client": "claude"})
    other = store.add_project("other", tmp_path / "other")
    session = SessionManager(store, store.settings).prepare(project["id"], "codex", timeout=0)
    headers = {"Authorization": f"Bearer {store.settings.token}"}
    with TestClient(create_app(store.settings, start_worker=False)) as client:
        assert client.get("/jobs", params={"project": "jobs"}).status_code == 401
        assert client.get("/jobs", params={"project": "missing"}, headers=headers).status_code == 404
        assert client.get(f"/jobs/{jid}", params={"project": other["name"]}, headers=headers).status_code == 404
        assert client.get("/jobs", params={"project": "jobs", "limit": 101}, headers=headers).status_code == 400
        assert client.get("/jobs", params={"project": "other", "session_id": session["session_id"]}, headers=headers).status_code == 400
        blocked = client.post(f"/jobs/{jid}/retry", headers=headers,
            json={"project": "jobs", "expected_attempts": 1, "session_id": session["session_id"]})
        assert blocked.status_code == 409
        ok = client.post(f"/jobs/{jid}/retry", headers=headers, json={"project": "jobs", "expected_attempts": 1})
        assert ok.status_code == 200 and ok.json()["job"]["state"] == "pending"
        assert client.post(f"/jobs/{jid}/retry", headers=headers,
            json={"project": "jobs", "expected_attempts": 1}).status_code == 409
        status = client.get("/status", headers=headers).json()
        assert status["pipeline_jobs"]["archive"]["pending"] == 1
