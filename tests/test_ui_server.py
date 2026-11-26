"""Fast-tier tests for Cairnmind UI Inspector backend endpoints, security, and CLI."""
from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from cairnmind import cli, ui_server
from cairnmind.ui_server import (
    LiveUiAdapter,
    UiEventBus,
    create_ui_app,
    resolve_ui_dist,
    safe_read_file_context,
)


def test_mock_snapshot_file_context_reverify_drop_and_sse(tmp_path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html><body>OK</body></html>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('ok');", encoding="utf-8")

    app = create_ui_app(default_mock=True, static_dir=dist)
    with TestClient(app) as client:
        # Static SPA & security headers
        root_res = client.get("/")
        assert root_res.status_code == 200
        assert root_res.headers["x-content-type-options"] == "nosniff"
        assert root_res.headers["referrer-policy"] == "no-referrer"
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/provenance").status_code == 200
        assert client.get("/api/nonexistent").status_code == 404

        # Initial snapshot
        snap_res = client.get("/api/ui/snapshot?mock=1")
        assert snap_res.status_code == 200
        assert snap_res.headers["cache-control"] == "no-store"
        snap = snap_res.json()
        assert snap["project"]["mode"] == "mock"
        assert snap["state_rev"] == 42
        assert len(snap["sessions"]) == 3
        assert len(snap["tasks"]) == 5
        assert len(snap["claims"]) == 5
        assert len(snap["graph"]["nodes"]) > 10
        assert len(snap["graph"]["edges"]) > 10
        assert len(snap["audit_log"]) >= 6

        # Mock file-context + path traversal rejection
        ctx_res = client.get(
            "/api/ui/file-context",
            params={"path": "src/cairnmind/sessions.py", "lineStart": 10, "lineEnd": 22, "mock": 1},
        )
        assert ctx_res.status_code == 200
        ctx_body = ctx_res.json()
        assert ctx_body["path"] == "src/cairnmind/sessions.py"
        assert len(ctx_body["current_disk_sha256"]) == 64
        assert ctx_body["lines"][0]["number"] == 10

        assert client.get(
            "/api/ui/file-context",
            params={"path": "../secret.txt", "mock": 1},
        ).status_code == 403
        assert client.get(
            "/api/ui/file-context",
            params={"path": ".git/config", "mock": 1},
        ).status_code == 403
        assert client.get(
            "/api/ui/file-context",
            params={"path": "does/not/exist.py", "mock": 1},
        ).status_code == 404

        # SSE bootstrap frame (`once=true`)
        sse_res = client.get("/api/ui/events", params={"mock": 1, "once": "true"})
        assert sse_res.status_code == 200
        assert "text/event-stream" in sse_res.headers["content-type"]
        assert "event: state.updated" in sse_res.text
        assert '"state_rev": 42' in sse_res.text

        # Reverify action bumps state_rev and updates audit log
        rev_res = client.post("/api/ui/actions/reverify?mock=1")
        assert rev_res.status_code == 200
        rev_body = rev_res.json()
        assert rev_body["ok"] is True
        assert rev_body["state_rev"] == 43
        assert rev_body["stale_claims"] >= 1

        # Drop session lock removes session and rejects second drop with 404
        drop_res = client.post("/api/ui/actions/sessions/sess-codex-3c81/drop?mock=1")
        assert drop_res.status_code == 200
        assert drop_res.json()["dropped"] is True
        assert drop_res.json()["state_rev"] == 44
        assert client.post("/api/ui/actions/sessions/sess-codex-3c81/drop?mock=1").status_code == 404

        snap_after = client.get("/api/ui/snapshot?mock=1").json()
        assert snap_after["state_rev"] == 44
        assert all(s["id"] != "sess-codex-3c81" for s in snap_after["sessions"])


def test_safe_read_file_context_security_and_limits(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "app.py"
    src.write_text("line1\nline2\nline3\n", encoding="utf-8")
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    binary = repo / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02binary")
    big = repo / "huge.txt"
    big.write_bytes(b"a" * (ui_server.MAX_CONTEXT_FILE_BYTES + 16))
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    ok = safe_read_file_context(repo, "app.py", 1, 2)
    assert ok["path"] == "app.py"
    assert [line["text"] for line in ok["lines"]] == ["line1", "line2"]
    assert ok["total_lines"] == 4

    for bad_path, code in [
        ("", 400),
        ("bad\x00path.py", 400),
        ("../outside.txt", 403),
        (str(outside), 403),
        (".git/HEAD", 403),
        ("missing.py", 404),
        ("blob.bin", 415),
        ("huge.txt", 413),
    ]:
        try:
            safe_read_file_context(repo, bad_path, 1, 10)
            raise AssertionError(f"Expected HTTPException({code}) for {bad_path!r}")
        except Exception as exc:
            assert getattr(exc, "status_code", None) == code


def test_event_bus_and_missing_bundle_fallback(tmp_path, monkeypatch):
    bus = UiEventBus(maxlen=5)
    entry = bus.record_audit({
        "session_id": "s1",
        "client": "claude-code",
        "mcp_tool": "memory.remember",
        "verdict": "REJECTED",
        "reason_code": "VERBATIM_QUOTE_NOT_FOUND",
        "message": "Quote not found",
        "arguments": {},
        "violation_detail": {"rule": "VERBATIM_QUOTE_NOT_FOUND"},
    })
    assert entry["verdict"] == "REJECTED"
    assert len(bus.recent_audit()) == 1
    assert len(bus.recent_events()) == 1
    assert bus.recent_events()[0]["type"] == "mcp.write.rejected"

    monkeypatch.setattr(ui_server, "resolve_ui_dist", lambda override=None: None)
    app = create_ui_app(default_mock=True)
    with TestClient(app) as client:
        res = client.get("/")
        assert res.status_code == 503
        assert "Cairnmind UI bundle not built" in res.text


def test_live_adapter_with_fake_store(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    repo.mkdir()
    sample = repo / "core.py"
    sample.write_text("def check_gate():\n    return True\n", encoding="utf-8")
    file_sha = ui_server._sha256_bytes(sample.read_bytes())

    sessions = [{
        "id": "sess-1",
        "project_id": "p1",
        "client": "claude-code",
        "worktree": str(repo),
        "active_packet_id": "pkt-1",
        "acknowledged_packet_id": "pkt-1",
        "updated_at": "2026-11-26T10:00:00Z",
    }]
    records = [
        {
            "id": "rec-claim-1",
            "project_id": "p1",
            "kind": "decision",
            "key": "core.check_gate",
            "content": "check_gate returns True",
            "status": "confirmed",
            "version": 1,
            "source_ids": ["src-1"],
            "metadata": {"evidence": "return True"},
            "updated_at": "2026-11-26T10:00:00Z",
        },
        {
            "id": "rec-task-1",
            "project_id": "p1",
            "kind": "task",
            "key": "step-1",
            "content": "Verify core gate",
            "status": "confirmed",
            "version": 2,
            "source_ids": ["src-1"],
            "metadata": {
                "plan_ref": "plan-1",
                "step_key": "step-1",
                "state": "in_progress",
                "scope": ["core.py"],
                "required_profiles": ["unit"],
                "acceptance_criteria": ["passes unit tests"],
            },
            "updated_at": "2026-11-26T10:00:00Z",
        },
    ]
    sources = [{
        "id": "src-1",
        "project_id": "p1",
        "uri": str(sample),
        "kind": "code",
        "sha256": file_sha,
        "is_current": True,
        "metadata": {"path": "core.py"},
    }]
    plans = [{"id": "plan-1", "project_id": "p1", "title": "Core Plan"}]
    checks = [{
        "id": "chk-1",
        "project_id": "p1",
        "task_id": "rec-task-1",
        "profile_id": "unit",
        "passed": True,
        "profile_hash": "abcdef123456",
        "ended_at": "2026-11-26T10:01:00Z",
    }]

    class FakeTx:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def command(self, sql, params=None):
            return []

    class FakeStore:
        def resolve_project(self, identifier):
            if identifier in {"p1", "demo"}:
                return {"id": "p1", "name": "demo", "root": str(repo)}
            raise ValueError("Unknown project")

        def transaction(self):
            return FakeTx()

        def query(self, sql, params=None):
            if "FROM projects" in sql:
                return [{"id": "p1", "name": "demo", "root": str(repo)}]
            if "FROM sessions" in sql:
                if params and "id" in params:
                    return [s for s in sessions if s["id"] == params["id"]]
                return list(sessions)
            if "FROM resume_acks" in sql:
                return [{"session_id": "sess-1", "packet_id": "pkt-1", "packet_hash": "1234567890abcdef"}]
            if "FROM records" in sql:
                if "count(*)" in sql:
                    return [{"c": len(records)}]
                return list(records)
            if "FROM sources" in sql:
                return list(sources)
            if "FROM plans" in sql:
                return list(plans)
            if "FROM check_runs" in sql:
                return list(checks)
            if "FROM session_events" in sql:
                return [{
                    "id": "sev-1",
                    "session_id": "sess-1",
                    "kind": "write_refused",
                    "detail": "resume_required: stale packet",
                    "recorded_at": "2026-11-26T10:02:00Z",
                }]
            return []

    monkeypatch.setattr("cairnmind.evidence.evidence_states", lambda s, pid, recs, quotes=True: {
        r["id"]: {"evidence_state": "anchored", "quote_survives": True} for r in recs
    })
    monkeypatch.setattr("cairnmind.verification.capture_code_state", lambda s, pid, root: {
        "branch": "main", "head": "1234567890ab", "fingerprint": file_sha,
    })
    monkeypatch.setattr("cairnmind.verification.verification_status", lambda s, task, root: {"stale": False})
    monkeypatch.setattr("cairnmind.ingest.index_project_if_changed", lambda s, pid: {"errors": []})
    monkeypatch.setattr("cairnmind.sql.update_document", lambda tx, tbl, doc, where, params: sessions.__setitem__(0, doc))
    monkeypatch.setattr("cairnmind.sql.insert_document", lambda tx, tbl, doc: doc)

    bus = UiEventBus()
    adapter = LiveUiAdapter(FakeStore(), SimpleNamespace(), bus, default_project="p1")
    snap = adapter.snapshot()
    assert snap["project"]["mode"] == "live"
    assert snap["project"]["name"] == "demo"
    assert len(snap["sessions"]) == 1 and snap["sessions"][0]["sync_status"] == "SYNCED"
    assert len(snap["claims"]) == 1 and snap["claims"][0]["provenance_status"] == "VERIFIED"
    assert len(snap["tasks"]) == 1 and snap["tasks"][0]["test_gate"]["status"] == "PASS"
    assert snap["audit_log"][0]["reason_code"] == "OCC_STALE_RESUME_PACKET"

    ctx = adapter.file_context("core.py", 1, 5)
    assert ctx["path"] == "core.py"

    reverify = adapter.reverify()
    assert reverify["ok"] is True and reverify["verified_claims"] == 1

    dropped = adapter.drop_session("sess-1")
    assert dropped["dropped"] is True


def test_cli_ui_command_validation_and_launch(monkeypatch):
    runner = CliRunner()
    # Reject non-loopback host
    bad_host = runner.invoke(cli.app, ["ui", "--host", "0.0.0.0", "--mock", "--no-open"])
    assert bad_host.exit_code != 0
    assert "loopback" in (bad_host.output or "").lower()

    # Reject invalid port
    bad_port = runner.invoke(cli.app, ["ui", "--port", "70000", "--mock", "--no-open"])
    assert bad_port.exit_code != 0

    # Mock launch invokes uvicorn.Server.run on 127.0.0.1:4310
    started: dict = {}

    class FakeServer:
        def __init__(self, config):
            started["host"] = config.host
            started["port"] = config.port
            started["app"] = config.app

        def run(self):
            started["ran"] = True

    monkeypatch.setattr("uvicorn.Server", FakeServer)
    ok = runner.invoke(cli.app, ["ui", "--port", "4310", "--mock", "--no-open"])
    assert ok.exit_code == 0, ok.output
    assert started["host"] == "127.0.0.1"
    assert started["port"] == 4310
    assert started["ran"] is True
    assert "http://127.0.0.1:4310/?mock=1" in ok.output


def test_resolve_ui_dist_finds_override(tmp_path):
    dist = tmp_path / "custom_dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    assert resolve_ui_dist(dist) == dist.resolve()
    assert json.loads(json.dumps(str(resolve_ui_dist(dist)))) == str(dist.resolve())


def test_ui_server_additional_edge_branches(tmp_path, monkeypatch):
    import asyncio
    from fastapi import FastAPI, HTTPException

    # _classify_reason_code branches
    assert ui_server._classify_reason_code("resume_required stale") == "OCC_STALE_RESUME_PACKET"
    assert ui_server._classify_reason_code("quote missing") == "VERBATIM_QUOTE_NOT_FOUND"
    assert ui_server._classify_reason_code("file hash drift") == "FILE_FINGERPRINT_MISMATCH"
    assert ui_server._classify_reason_code("test check failed") == "TEST_GATE_UNVERIFIED"
    assert ui_server._classify_reason_code("other") == "OCC_STALE_RESUME_PACKET"

    # _find_quote_lines branches (missing file, huge file, quote not found, quote found)
    assert ui_server._find_quote_lines(tmp_path / "nope.py", "abc") == (1, 25)
    f = tmp_path / "sample.py"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    assert ui_server._find_quote_lines(f, "beta\ngamma") == (2, 3)
    assert ui_server._find_quote_lines(f, "not-present") == (1, 4)
    huge = tmp_path / "huge.py"
    huge.write_bytes(b"x" * (ui_server.MAX_CONTEXT_FILE_BYTES + 10))
    assert ui_server._find_quote_lines(huge, "x") == (1, 25)

    # resolve_ui_dist via CAIRNMIND_UI_DIST env var
    env_dist = tmp_path / "env_dist"
    env_dist.mkdir()
    (env_dist / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setenv("CAIRNMIND_UI_DIST", str(env_dist))
    assert resolve_ui_dist() == env_dist.resolve()

    # safe_read_file_context missing root
    try:
        safe_read_file_context(tmp_path / "no_such_root", "a.py")
        raise AssertionError("Expected 404")
    except HTTPException as exc:
        assert exc.status_code == 404

    # UiEventBus subscriber fan-out and full-queue eviction
    async def exercise_bus():
        bus = UiEventBus(maxlen=5)
        token = bus.subscribe()
        _, q = token
        for i in range(130):
            bus.publish("state.updated", {"seq": i})
        await asyncio.sleep(0.05)
        assert not q.empty()
        bus.unsubscribe(token)

    asyncio.run(exercise_bus())

    # LiveUiAdapter error & drift branches (empty projects, unknown project, mutated/drifted/missing claims, failed/stale checks)
    class EmptyStore:
        def resolve_project(self, identifier):
            raise ValueError("Unknown project")

        def query(self, sql, params=None):
            return []

    empty_adapter = LiveUiAdapter(EmptyStore(), SimpleNamespace(), UiEventBus())
    for call in (lambda: empty_adapter.snapshot(), lambda: empty_adapter.snapshot("bad")):
        try:
            call()
            raise AssertionError("Expected 404")
        except HTTPException as exc:
            assert exc.status_code == 404

    repo = tmp_path / "drift_repo"
    repo.mkdir()
    present = repo / "mod.py"
    present.write_text("def f():\n    return 2\n", encoding="utf-8")

    class DriftStore:
        def resolve_project(self, identifier):
            return {"id": "p1", "name": "drift", "root": str(repo)}

        def query(self, sql, params=None):
            if "FROM projects" in sql:
                return [{"id": "p1", "name": "drift", "root": str(repo)}]
            if "FROM sessions" in sql:
                if params and "id" in params:
                    return []
                return [
                    {"id": "s-drop", "status": "dropped", "client": "codex"},
                    {"id": "s-stale", "client": "external", "worktree": str(repo), "active_packet_id": "p2", "acknowledged_packet_id": "p1"},
                ]
            if "FROM records" in sql:
                if "count(*)" in sql:
                    return [{"c": 5}]
                return [
                    {"id": "c-missing", "kind": "fact", "key": "k1", "content": "missing", "status": "confirmed", "version": 1, "source_ids": ["s-miss"], "metadata": {"evidence": "x"}},
                    {"id": "c-mutated", "kind": "fact", "key": "k2", "content": "mutated", "status": "confirmed", "version": 1, "source_ids": ["s-mod"], "metadata": {"evidence": "return 1"}},
                    {"id": "c-drift", "kind": "fact", "key": "k3", "content": "drifted", "status": "confirmed", "version": 1, "source_ids": ["s-mod"], "metadata": {"evidence": "return 2"}},
                    {"id": "c-unver", "kind": "fact", "key": "k4", "content": "unver", "status": "proposed", "version": 1, "source_ids": [], "metadata": {}},
                    {"id": "t-fail", "kind": "task", "key": "t1", "content": "Fail task", "status": "confirmed", "version": 1, "source_ids": [], "metadata": {"state": "blocked", "scope": ["mod.py"]}},
                    {"id": "t-stale", "kind": "task", "key": "t2", "content": "Stale task", "status": "confirmed", "version": 1, "source_ids": [], "metadata": {"state": "needs_test"}},
                ]
            if "FROM sources" in sql:
                return [
                    {"id": "s-miss", "uri": "gone.py", "kind": "code", "sha256": "0" * 64, "is_current": False, "metadata": {"path": "gone.py"}},
                    {"id": "s-mod", "uri": "mod.py", "kind": "code", "sha256": "1" * 64, "is_current": False, "metadata": {"path": "mod.py"}},
                ]
            if "FROM check_runs" in sql:
                return [
                    {"id": "chk-f", "task_id": "t-fail", "profile_id": "unit", "passed": False, "profile_hash": "abc12345"},
                    {"id": "chk-s", "task_id": "t-stale", "profile_id": "unit", "passed": True, "profile_hash": "def12345"},
                ]
            return []

    monkeypatch.setattr("cairnmind.evidence.evidence_states", lambda s, pid, recs, quotes=True: {
        "c-missing": {"evidence_state": "orphaned", "quote_survives": False},
        "c-mutated": {"evidence_state": "evidence_replaced", "quote_survives": False},
        "c-drift": {"evidence_state": "evidence_replaced", "quote_survives": True},
        "c-unver": {"evidence_state": "unverified", "quote_survives": None},
        "t-fail": {"evidence_state": "unverified", "quote_survives": None},
        "t-stale": {"evidence_state": "unverified", "quote_survives": None},
    })
    monkeypatch.setattr("cairnmind.verification.capture_code_state", lambda s, pid, root: {"branch": None, "head": None, "fingerprint": None})
    monkeypatch.setattr("cairnmind.verification.verification_status", lambda s, task, root: {"stale": task["id"] == "t-stale"})

    drift_adapter = LiveUiAdapter(DriftStore(), SimpleNamespace(), UiEventBus())
    snap = drift_adapter.snapshot()
    claims_by_id = {c["id"]: c for c in snap["claims"]}
    assert claims_by_id["c-missing"]["drift_kind"] == "FILE_MISSING"
    assert claims_by_id["c-mutated"]["drift_kind"] == "QUOTE_MUTATED"
    assert claims_by_id["c-drift"]["drift_kind"] == "FILE_DRIFT_QUOTE_INTACT"
    assert claims_by_id["c-unver"]["provenance_status"] == "UNVERIFIED"
    tasks_by_id = {t["id"]: t for t in snap["tasks"]}
    assert tasks_by_id["t-fail"]["test_gate"]["status"] == "FAIL"
    assert tasks_by_id["t-stale"]["test_gate"]["status"] == "STALE_CODE"
    assert len(snap["sessions"]) == 1 and snap["sessions"][0]["sync_status"] == "OUTDATED_ACK"

    try:
        drift_adapter.drop_session("nonexistent")
        raise AssertionError("Expected 404")
    except HTTPException as exc:
        assert exc.status_code == 404

    # attach_ui_routes + live endpoints via TestClient
    host_app = FastAPI()
    bus = ui_server.attach_ui_routes(host_app, DriftStore(), SimpleNamespace())
    assert host_app.state.ui_event_bus is bus
    with TestClient(host_app) as client:
        assert client.get("/api/ui/snapshot").status_code == 200
        assert client.get("/api/ui/file-context", params={"path": "mod.py"}).status_code == 200
        assert client.post("/api/ui/actions/reverify").status_code == 200
        assert client.post("/api/ui/actions/sessions/missing/drop").status_code == 404
