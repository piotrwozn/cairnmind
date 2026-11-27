"""Fast-tier edge-case tests for ingest.py helpers and service.py routes & background loops."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from cairnmind.scip import ingest
from cairnmind import service
from cairnmind.config import Settings
from cairnmind.store import JobNotFoundError
from cairnmind.storage.store_writer import StoreNotWriter


def test_ingest_helpers_reparse_and_import_edges(tmp_path, monkeypatch):
    # _metadata handles JSON string and invalid JSON
    assert ingest._metadata('{"a": 1}') == {"a": 1}
    assert ingest._metadata("{bad") == {}

    # _git handles OSError cleanly
    monkeypatch.setattr(ingest.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert ingest._git(tmp_path, "status") == ""
    monkeypatch.undo()

    # _NestedMemory.covers returns False outside root
    nested = ingest._NestedMemory(None, tmp_path / "root")
    assert nested.covers(tmp_path / "outside" / ".memory") is False

    # _ingestion_lock deadline expired and BlockingIOError retry
    fake_store = SimpleNamespace(data_dir=tmp_path / "data")
    fake_store.data_dir.mkdir()
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        with ingest._ingestion_lock(fake_store, "history", "x", deadline=0.0):
            pass

    attempts = {"n": 0}
    orig_lock = ingest.lock_exclusive

    def flaky_lock(fd, blocking=True):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise BlockingIOError()
        return orig_lock(fd, blocking=blocking)

    monkeypatch.setattr(ingest, "lock_exclusive", flaky_lock)
    with ingest._ingestion_lock(fake_store, "history", "y", deadline=1e18):
        pass
    assert attempts["n"] >= 2
    monkeypatch.undo()

    # _validate_reparse_options error paths
    with pytest.raises(ValueError, match="auto, codex, or claude"):
        ingest._validate_reparse_options("bogus", 10, None)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        ingest._validate_reparse_options("auto", 0, None)
    with pytest.raises(ValueError, match="finite"):
        ingest._validate_reparse_options("auto", 10, float("inf"))

    # _reparse_history_sources_locked error branches (deadline, oversized, bad JSON, bad view, changed, chunk mismatch)
    f = tmp_path / "hist.jsonl"
    f.write_text("", encoding="utf-8")

    class _ReparseStore:
        def __init__(self, sources, tx_sources=None, tx_chunks=None):
            self.sources = sources
            self.tx_sources = tx_sources
            self.tx_chunks = tx_chunks or []

        def query(self, sql, params=None):
            if "FROM sources" in sql:
                return list(self.sources)
            return []

        def _require_project(self, tx, pid):
            pass

        @contextmanager
        def transaction(self):
            tx = SimpleNamespace(
                query=lambda sql, params=None: list(self.tx_sources) if "FROM sources" in sql else list(self.tx_chunks),
                command=lambda sql, params=None: None,
            )
            yield tx

    # Deadline exceeded
    with pytest.raises(TimeoutError):
        ingest._reparse_history_sources_locked(_ReparseStore([{"id": "s1"}]), f, "p1", deadline=0.0)

    # Oversized content + invalid JSON
    res = ingest._reparse_history_sources_locked(
        _ReparseStore([
            {"id": "big", "content": "x" * (ingest.MAX_FILE_BYTES + 10), "metadata": {}},
            {"id": "badjson", "content": "{broken", "metadata": {}},
        ]),
        f,
        "p1",
    )
    assert any("reparse budget" in e for e in res["errors"])

    # Build a valid Claude metadata event to hit non-str view, source changed, and chunk mismatch
    ev_line = json.dumps({"type": "progress", "sessionId": "sess-1", "cwd": str(tmp_path)}) + "\n"
    ev_sha = hashlib.sha256(ev_line.encode()).hexdigest()
    ev_uri = f"transcript://claude/{ingest._identifier(f, 'sess-1')}/0/{ev_sha}"
    base_meta = {
        "byte_offset": 0, "session_id": "sess-1", "client": "claude",
        "role": "unknown", "normalized_text": "", "cwd": str(tmp_path),
        "schema_warning": ingest.UNKNOWN_TRANSCRIPT_WARNING,
    }
    monkeypatch.setattr(ingest, "_source_project", lambda s, pid, cwd: ("p1", None))
    monkeypatch.setattr(ingest, "_technical_metadata_client", lambda ev: "claude")

    src_bad_view = {"id": "s-v", "project_id": "p1", "uri": ev_uri, "sha256": ev_sha, "content": ev_line,
                    "metadata": {**base_meta, "masked": True, "indexed_text": 123}}
    res_v = ingest._reparse_history_sources_locked(_ReparseStore([src_bad_view]), f, "p1")
    assert any("chunk projection" in e for e in res_v["errors"])

    src_ok = {"id": "s-ok", "project_id": "p1", "uri": ev_uri, "sha256": ev_sha, "content": ev_line,
              "kind": "transcript_unknown", "metadata": base_meta}
    res_chg = ingest._reparse_history_sources_locked(_ReparseStore([src_ok], tx_sources=[]), f, "p1")
    assert any("changed during reparse" in e for e in res_chg["errors"])

    res_chk = ingest._reparse_history_sources_locked(_ReparseStore([src_ok], tx_sources=[src_ok], tx_chunks=[]), f, "p1")
    assert any("chunk projection" in e for e in res_chk["errors"])

    # _import_history_locked invalid client & directory recursion
    with pytest.raises(ValueError, match="auto, codex, or claude"):
        ingest._import_history_locked(fake_store, tmp_path, None, "invalid")

    sub = tmp_path / "histdir"
    sub.mkdir()
    (sub / "a.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(ingest, "import_history", lambda s, p, pid, c: {"files": 1, "archived": 0, "chunks": 0, "inbox": 0, "unknown": 0, "repaired": 0, "errors": ["e"]})
    agg = ingest._import_history_locked(fake_store, sub, None, "auto")
    assert agg["files"] == 1 and agg["errors"] == ["e"]


def test_service_routes_and_background_loops_edges(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()

    class _FakeStore:
        def __init__(self):
            self.data_dir = settings.data_dir
            self.settings = settings
            self.not_writer = None
            self.relative_paths = False
            self.stamp = 1
            self.scip_jobs = [
                {"id": "j-scip-1", "project_id": "p1", "worker": "scip", "attempts": 1,
                 "payload": json.dumps({"languages": ["go"], "root": str(proj_dir)})},
                {"id": "j-scip-2", "project_id": "p1", "worker": "scip", "attempts": 1,
                 "payload": {"languages": ["rust"], "root": str(proj_dir)}},
            ]
            self.import_jobs = [
                {"id": "j-imp-1", "project_id": "p1", "worker": "archive", "attempts": 1,
                 "payload": json.dumps({"path": str(proj_dir), "client": "auto"})},
            ]
            self.writer_fail_once = True
            self.finished = []

        def resolve_project(self, project):
            if project == "missing":
                raise ValueError("Unknown project")
            return {"id": "p1", "name": "P1", "root": str(proj_dir)}

        def list_projects(self):
            return [{"id": "p1", "name": "P1", "root": str(proj_dir)}]

        def query(self, sql, params=None):
            if "FROM records" in sql:
                return []
            if "FROM check_runs" in sql:
                return []
            if "FROM project_roots" in sql:
                return [{"path": str(proj_dir)}]
            return []

        def list_jobs(self, pid, state, kind, limit):
            return [{"id": "j1", "kind": "embed", "state": "failed"}]

        def job_summary(self, pid):
            return {"pending": 0}

        def job_status(self, pid, job_id):
            if job_id == "missing":
                raise JobNotFoundError("no such job")
            return {"id": job_id, "state": "done"}

        def retry_job(self, pid, job_id, expected_attempts, include_omissions=False):
            if job_id == "missing":
                raise JobNotFoundError("no such job")
            return {"id": job_id, "kind": "embed", "state": "pending"}

        def claim_job(self, worker, lease, kinds):
            if self.writer_fail_once and worker == "scip":
                self.writer_fail_once = False
                raise StoreNotWriter("writer lost")
            if worker == "scip" and self.scip_jobs:
                return self.scip_jobs.pop(0)
            if worker == "archive" and self.import_jobs:
                return self.import_jobs.pop(0)
            return None

        def finish_job(self, job_id, error=None, **kw):
            self.finished.append((job_id, error))

        def cache_stamp(self, pid):
            return self.stamp

        def backup(self, path):
            path.write_bytes(b"zip")

        def current_origin(self):
            return {}

        def checkpoint(self, pid, state):
            return {"ok": True}

        @contextmanager
        def write_origin(self, mark):
            yield

        @contextmanager
        def session_write(self, session_id):
            yield

        @contextmanager
        def transaction(self):
            yield self

    fake_store = _FakeStore()
    fake_model = SimpleNamespace(
        model_identity=lambda m: (_ for _ in ()).throw(RuntimeError("no digest")),
        aclose=lambda: asyncio.sleep(0),
    )

    monkeypatch.setattr("cairnmind.agents.retrieval.Retriever", lambda *a, **k: SimpleNamespace(context=lambda pid, sc: {"ok": True}))
    monkeypatch.setattr(
        "cairnmind.agents.agent.KnowledgeAgent",
        lambda *a, **k: SimpleNamespace(ask=lambda *aa, **kk: asyncio.sleep(0, result={"status": "complete", "answer": "yes", "sources": []})),
    )
    monkeypatch.setattr(
        "cairnmind.memory.plans.PlanManager",
        lambda s: SimpleNamespace(
            list=lambda pid: [],
            get=lambda pid, plid: {"id": plid},
            save=lambda *a: {"saved": True},
            activate=lambda *a: {"activated": True},
        ),
    )
    monkeypatch.setattr(
        "cairnmind.memory.sessions.SessionManager",
        lambda *a, **k: SimpleNamespace(
            require_scope=lambda sid, pid: str(proj_dir),
            status=lambda sid: {"session_id": sid, "project_id": "p1", "worktree": str(proj_dir), "acknowledged": True},
            authorize_write=lambda sid, pid, pkt: str(proj_dir),
        ),
    )
    monkeypatch.setattr(
        "cairnmind.memory.memory.MemoryManager",
        lambda *a, **k: SimpleNamespace(tick=lambda: (_ for _ in ()).throw(RuntimeError("mem tick err"))),
    )
    monkeypatch.setattr("cairnmind.service.WRITER_LOST_RETRY_SECONDS", 0.01)
    monkeypatch.setattr("cairnmind.scip.ingest.index_project", lambda s, pid: {"files": 1})
    monkeypatch.setattr("cairnmind.scip.ingest.import_history", lambda s, p, pid, c: {"errors": []})
    monkeypatch.setattr("cairnmind.scip.ingest.poll_imports", lambda s: {"errors": []})
    monkeypatch.setattr("cairnmind.scip.ingest.forget_missing_projects", lambda s, pids: None)
    monkeypatch.setattr("cairnmind.scip.ingest.index_project_if_changed", lambda s, pid: {"changed": 1, "errors": ["idx warn"]})
    monkeypatch.setattr("cairnmind.scip.ingest.index_pending_sources", lambda s: {"errors": []})
    monkeypatch.setattr("cairnmind.scip.ingest.tree_signature", lambda s, pid: "abcd" * 8)
    monkeypatch.setattr("cairnmind.memory.integrations.drain_hook_spool", lambda st, s: {"errors": []})
    monkeypatch.setattr("cairnmind.memory.verification.recover_interrupted_runs", lambda s, st: None)
    monkeypatch.setattr("cairnmind.memory.verification.load_profiles", lambda s, pid, sc: {})
    monkeypatch.setattr(
        "cairnmind.memory.verification.capture_code_state",
        lambda s, pid, sc, inp: {"status": "fresh", "fingerprint": "fp1"},
    )
    monkeypatch.setattr("cairnmind.scip.scip_ingest.enqueue_project_scip", lambda *a, **k: None)

    scip_calls = {"n": 0}

    def fake_scip_index(*a, **k):
        scip_calls["n"] += 1
        if scip_calls["n"] == 2:
            raise RuntimeError("scip failed")
        return {"errors": []}

    monkeypatch.setattr("cairnmind.scip.scip_ingest.index_project_scip", fake_scip_index)

    from dataclasses import replace
    fast_settings = replace(settings, poll_seconds=1, scip_mode="auto")
    app = service.create_app(fast_settings, start_worker=True, background_inference=False, store=fake_store, ollama=fake_model)
    headers = {"Authorization": f"Bearer {fast_settings.token}"}

    with TestClient(app) as client:
        # Let background tasks tick briefly (scanner + scip_worker + memory_snapshots)
        import time
        time.sleep(0.2)

        # /jobs routes (200 + 404s)
        assert client.get("/jobs", params={"project": "p1"}, headers=headers).status_code == 200
        assert client.get("/jobs", params={"project": "missing"}, headers=headers).status_code == 404
        assert client.get("/jobs/j1", params={"project": "p1"}, headers=headers).status_code == 200
        assert client.get("/jobs/missing", params={"project": "p1"}, headers=headers).status_code == 404
        assert client.get("/jobs/j1", params={"project": "missing"}, headers=headers).status_code == 404
        assert client.post("/jobs/j1/retry", json={"project": "p1", "expected_attempts": 1}, headers=headers).status_code == 200
        assert client.post("/jobs/j1/retry", json={"project": "missing", "expected_attempts": 1}, headers=headers).status_code == 404
        assert client.post("/jobs/missing/retry", json={"project": "p1", "expected_attempts": 1}, headers=headers).status_code == 404

        # /plans routes
        assert client.get("/plans", params={"project": "p1"}, headers=headers).status_code == 200
        assert client.get("/plans/pl1", params={"project": "p1"}, headers=headers).status_code == 200

        # /checks missing task -> 400
        assert client.get("/checks", params={"project": "p1", "task_id": "missing"}, headers=headers).status_code == 400

        # /ask with index_errors downgrades complete -> partial and logs answer
        res_ask = client.post("/ask", json={"project": "p1", "question": "Hello?", "log": True}, headers=headers)
        assert res_ask.status_code == 200 and res_ask.json()["status"] == "partial"

        # /ask without index_errors caches response on second call
        monkeypatch.setattr("cairnmind.scip.ingest.index_project_if_changed", lambda s, pid: {"changed": 0, "errors": []})
        res1 = client.post("/ask", json={"project": "p1", "question": "Cached?", "log": True}, headers=headers)
        res2 = client.post("/ask", json={"project": "p1", "question": "Cached?", "log": True}, headers=headers)
        assert res1.json()["cached"] is False and res2.json()["cached"] is True

        # /ingest & /import & /shutdown
        assert client.post("/ingest", json={"project": "p1"}, headers=headers).status_code == 200
        assert client.post("/import", json={"project": "p1", "path": str(proj_dir)}, headers=headers).status_code == 200
        called = []
        app.state.shutdown = lambda: called.append(True)
        assert client.post("/shutdown", headers=headers).json() == {"stopping": True}
        assert called == [True]
