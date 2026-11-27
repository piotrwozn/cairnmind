"""Fast-tier tests for Cairnmind UI Inspector backend endpoints, security, and CLI."""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.ui import ui_server
from cairnmind.ui.ui_auth import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, UiSessionAuth
from cairnmind.ui.ui_server import (
    LiveUiAdapter,
    UiEventBus,
    create_ui_app,
    resolve_ui_dist,
    safe_read_file_context,
)

UI_BASE_URL = "http://127.0.0.1:4310"


def _auth_client(app) -> tuple[TestClient, dict[str, str]]:
    """Create a loopback TestClient with valid session Bearer headers for `app`."""
    auth: UiSessionAuth = app.state.ui_auth
    client = TestClient(app, base_url=UI_BASE_URL)
    headers = {"Authorization": f"Bearer {auth.session_token}"}
    return client, headers


def test_mock_snapshot_file_context_reverify_drop_and_sse(tmp_path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html><body>OK</body></html>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('ok');", encoding="utf-8")

    app = create_ui_app(default_mock=True, static_dir=dist)
    client, headers = _auth_client(app)
    with client:
        # Static SPA & security headers
        root_res = client.get("/")
        assert root_res.status_code == 200
        assert root_res.headers["x-content-type-options"] == "nosniff"
        assert root_res.headers["referrer-policy"] == "no-referrer"
        assert root_res.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in root_res.headers["content-security-policy"]
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/provenance").status_code == 200
        assert client.get("/api/nonexistent").status_code == 404
        # Redundant /api/* alias is removed; only canonical /api/ui/* exists
        assert client.get("/api/snapshot?mock=1", headers=headers).status_code == 404

        # Initial snapshot
        snap_res = client.get("/api/ui/snapshot?mock=1", headers=headers)
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
            headers=headers,
        )
        assert ctx_res.status_code == 200
        ctx_body = ctx_res.json()
        assert ctx_body["path"] == "src/cairnmind/sessions.py"
        assert len(ctx_body["current_disk_sha256"]) == 64
        assert ctx_body["lines"][0]["number"] == 10

        assert client.get(
            "/api/ui/file-context",
            params={"path": "../secret.txt", "mock": 1},
            headers=headers,
        ).status_code == 403
        assert client.get(
            "/api/ui/file-context",
            params={"path": ".git/config", "mock": 1},
            headers=headers,
        ).status_code == 403
        assert client.get(
            "/api/ui/file-context",
            params={"path": "does/not/exist.py", "mock": 1},
            headers=headers,
        ).status_code == 404

        # SSE bootstrap frame (`once=true`)
        sse_res = client.get("/api/ui/events", params={"mock": 1, "once": "true"}, headers=headers)
        assert sse_res.status_code == 200
        assert "text/event-stream" in sse_res.headers["content-type"]
        assert "event: state.updated" in sse_res.text
        assert '"state_rev": 42' in sse_res.text

        # Reverify action bumps state_rev and updates audit log
        rev_res = client.post("/api/ui/actions/reverify?mock=1", headers=headers)
        assert rev_res.status_code == 200
        rev_body = rev_res.json()
        assert rev_body["ok"] is True
        assert rev_body["state_rev"] == 43
        assert rev_body["stale_claims"] >= 1

        # Drop session lock removes session and rejects second drop with 404
        drop_res = client.post("/api/ui/actions/sessions/sess-codex-3c81/drop?mock=1", headers=headers)
        assert drop_res.status_code == 200
        assert drop_res.json()["dropped"] is True
        assert drop_res.json()["state_rev"] == 44
        assert client.post("/api/ui/actions/sessions/sess-codex-3c81/drop?mock=1", headers=headers).status_code == 404

        snap_after = client.get("/api/ui/snapshot?mock=1", headers=headers).json()
        assert snap_after["state_rev"] == 44
        assert all(s["id"] != "sess-codex-3c81" for s in snap_after["sessions"])


def test_ui_security_auth_csrf_host_and_origin_guards(tmp_path):
    """Verify standalone UI enforces session token, CSRF double-submit, Host (DNS rebinding), and Origin checks."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><html><body>OK</body></html>", encoding="utf-8")

    app = create_ui_app(default_mock=True, static_dir=dist, host="127.0.0.1", port=4310)
    auth: UiSessionAuth = app.state.ui_auth

    with TestClient(app, base_url=UI_BASE_URL) as client:
        # 1. Unauthenticated /api/ui/* calls are rejected with 401
        assert client.get("/api/ui/snapshot?mock=1").status_code == 401
        assert client.get("/api/ui/file-context", params={"path": "src/cairnmind/sessions.py", "mock": 1}).status_code == 401
        assert client.get("/api/ui/events", params={"mock": 1, "once": "true"}).status_code == 401
        assert client.post("/api/ui/plans?mock=1", json={"title": "x", "goal": ""}).status_code == 401
        assert client.post("/api/ui/facts?mock=1", json={"key": "k", "title": "t", "content": "c"}).status_code == 401
        assert client.post("/api/ui/actions/sessions/sess-codex-3c81/drop?mock=1").status_code == 401

        # Wrong bearer token -> 401
        assert client.get("/api/ui/snapshot?mock=1", headers={"Authorization": "Bearer wrong"}).status_code == 401

        # 2. Invalid Host header (DNS rebinding) -> 403 even with valid bearer token
        assert client.get(
            "/api/ui/snapshot?mock=1",
            headers={"Host": "evil.example:4310", "Authorization": f"Bearer {auth.session_token}"},
        ).status_code == 403
        assert client.get("/", headers={"Host": "evil.example:4310"}).status_code == 403

        # 3. Cross-origin / cross-site headers -> 403 even with valid bearer token
        assert client.post(
            "/api/ui/actions/reverify?mock=1",
            headers={"Origin": "https://evil.example", "Authorization": f"Bearer {auth.session_token}"},
        ).status_code == 403
        assert client.get(
            "/api/ui/snapshot?mock=1",
            headers={"Sec-Fetch-Site": "cross-site", "Authorization": f"Bearer {auth.session_token}"},
        ).status_code == 403

        # 4. Invalid bootstrap token -> 401
        assert client.get("/auth/bootstrap?token=bogus", follow_redirects=False).status_code == 401

        # 5. Valid bootstrap sets HttpOnly session cookie + CSRF cookie and redirects 303 without token in URL
        boot = client.get(f"/auth/bootstrap?token={auth.session_token}&mock=1", follow_redirects=False)
        assert boot.status_code == 303
        assert boot.headers["location"] == "/?mock=1"
        assert auth.session_token not in boot.headers["location"]
        assert boot.cookies.get(SESSION_COOKIE) == auth.session_token
        assert boot.cookies.get(CSRF_COOKIE) == auth.csrf_token

        # Cookie-authenticated GET snapshot and SSE work without custom header (EventSource compatible)
        assert client.get("/api/ui/snapshot?mock=1").status_code == 200
        sse = client.get("/api/ui/events", params={"mock": 1, "once": "true"})
        assert sse.status_code == 200
        assert "event: state.updated" in sse.text

        # Cookie-authenticated mutations AND file-context REQUIRE X-Cairnmind-UI-Token CSRF header
        assert client.post("/api/ui/actions/reverify?mock=1").status_code == 403
        assert client.post("/api/ui/actions/reverify?mock=1", headers={CSRF_HEADER: "bad"}).status_code == 403
        assert client.get(
            "/api/ui/file-context",
            params={"path": "src/cairnmind/sessions.py", "mock": 1},
        ).status_code == 403

        # With valid CSRF header and matching loopback Origin -> succeeds
        csrf_headers = {CSRF_HEADER: auth.csrf_token, "Origin": UI_BASE_URL}
        assert client.post("/api/ui/actions/reverify?mock=1", headers=csrf_headers).status_code == 200
        assert client.get(
            "/api/ui/file-context",
            params={"path": "src/cairnmind/sessions.py", "mock": 1},
            headers=csrf_headers,
        ).status_code == 200


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
    with TestClient(app, base_url=UI_BASE_URL) as client:
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
        def update_where(self, tbl, *, set_fields=None, remove_fields=(), where=()):
            if tbl == "sessions" and set_fields:
                sessions[0] = dict(set_fields)
            return []
        def insert_document(self, tbl, doc):
            return doc

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
                if params and " id=:" in sql:
                    sid = next(iter(params.values()))
                    return [s for s in sessions if s["id"] == sid]
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

    monkeypatch.setattr("cairnmind.memory.evidence.evidence_states", lambda s, pid, recs, quotes=True: {
        r["id"]: {"evidence_state": "anchored", "quote_survives": True} for r in recs
    })
    monkeypatch.setattr("cairnmind.memory.verification.capture_code_state", lambda s, pid, root: {
        "branch": "main", "head": "1234567890ab", "fingerprint": file_sha,
    })
    monkeypatch.setattr("cairnmind.memory.verification.verification_status", lambda s, task, root: {"stale": False})
    monkeypatch.setattr("cairnmind.scip.ingest.index_project_if_changed", lambda s, pid: {"errors": []})
    monkeypatch.setattr("cairnmind.storage.sql.update_document", lambda tx, tbl, doc, where, params: sessions.__setitem__(0, doc))
    monkeypatch.setattr("cairnmind.storage.sql.insert_document", lambda tx, tbl, doc: doc)

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

    # Mock launch invokes uvicorn.Server.run on 127.0.0.1:4310 with bootstrap URL
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
    assert "http://127.0.0.1:4310/auth/bootstrap?token=" in ok.output
    # Extract URL and verify token matches the app's UiSessionAuth
    url_token = ok.output.split("serving on ", 1)[1].split()[0]
    parsed = urlparse(url_token)
    qs = parse_qs(parsed.query)
    assert qs["mock"] == ["1"]
    assert started["app"].state.ui_auth.verify_session_token(qs["token"][0])


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
                if " id=:" in sql:
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

    monkeypatch.setattr("cairnmind.memory.evidence.evidence_states", lambda s, pid, recs, quotes=True: {
        "c-missing": {"evidence_state": "orphaned", "quote_survives": False},
        "c-mutated": {"evidence_state": "evidence_replaced", "quote_survives": False},
        "c-drift": {"evidence_state": "evidence_replaced", "quote_survives": True},
        "c-unver": {"evidence_state": "unverified", "quote_survives": None},
        "t-fail": {"evidence_state": "unverified", "quote_survives": None},
        "t-stale": {"evidence_state": "unverified", "quote_survives": None},
    })
    monkeypatch.setattr("cairnmind.memory.verification.capture_code_state", lambda s, pid, root: {"branch": None, "head": None, "fingerprint": None})
    monkeypatch.setattr("cairnmind.memory.verification.verification_status", lambda s, task, root: {"stale": task["id"] == "t-stale"})

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

    # attach_ui_routes in read-only mode (as service.py mounts it) blocks mutations
    host_app = FastAPI()
    bus = ui_server.attach_ui_routes(host_app, DriftStore(), SimpleNamespace(), read_only=True)
    assert host_app.state.ui_event_bus is bus
    with TestClient(host_app) as client:
        assert client.get("/api/ui/snapshot").status_code == 200
        assert client.get("/api/ui/file-context", params={"path": "mod.py"}).status_code == 200
        assert client.post("/api/ui/actions/reverify").status_code == 403
        assert client.post("/api/ui/facts", json={"key": "k", "title": "t", "content": "c"}).status_code == 403
        assert client.post("/api/ui/detected-decisions/c-unver/decide", json={"action": "accept"}).status_code == 403


def test_cockpit_phases_1_to_4_mock_and_project_prefix():
    """Verify Ask, Drift Monitor, Drafts + 409 CAS, Versions, Reindex, Doctor, Export & project_id prefixes."""
    app = create_ui_app(default_mock=True)
    client, headers = _auth_client(app)
    with client:
        # 1. Capabilities, origin badges, plans, and multi-project prefix under /api/ui
        snap = client.get("/api/ui/projects/demo-proj/snapshot?mock=1", headers=headers).json()
        assert snap["capabilities"]["writable"] is True
        assert snap["capabilities"]["multi_project"] is True
        assert len(snap["plans"]) >= 3
        assert all("origin" in t for t in snap["tasks"])
        assert all("origin" in c for c in snap["claims"])
        assert all("origin" in a for a in snap["audit_log"])

        # 2. Phase 1: Interactive Ask (VERIFIED, UNKNOWN, and Tier 0 FTS fallback)
        ask_ok = client.post(
            "/api/ui/ask?mock=1",
            json={"query": "Jakie są główne komponenty i przepływ danych?"},
            headers=headers,
        )
        assert ask_ok.status_code == 200
        ask_body = ask_ok.json()
        assert ask_body["status"] == "VERIFIED"
        assert len(ask_body["citations"]) >= 2

        ask_unk = client.post(
            "/api/ui/ask?mock=1",
            json={"query": "How does Stripe billing work?"},
            headers=headers,
        )
        assert ask_unk.status_code == 200
        assert ask_unk.json()["status"] == "UNKNOWN"
        assert ask_unk.json()["citations"] == []

        ask_fts = client.post(
            "/api/ui/ask?mock=1",
            json={"query": "Które zadania lub dowody są teraz nieaktualne?", "verifier_mode": "fts_fallback"},
            headers=headers,
        )
        assert ask_fts.status_code == 200
        assert ask_fts.json()["status"] == "PARTIAL"
        assert ask_fts.json()["fallback_mode"] is True
        assert ask_fts.json()["tier_used"] == "tier_0_fts_evidence"

        # 3. Phase 2: Plan & Drift Monitor + Detected Decisions pagination/filtering + Accept/Ignore
        drift = client.get(
            "/api/ui/drift",
            params={"mock": 1, "source": "github", "status": "pending", "page": 1, "page_size": 2},
            headers=headers,
        )
        assert drift.status_code == 200
        drift_body = drift.json()
        assert drift_body["approved_plan"]["id"] == "plan-release-012"
        assert len(drift_body["drift_items"]) >= 1
        assert len(drift_body["test_freshness"]) == 5
        assert len(drift_body["detected_decisions"]["items"]) <= 2
        assert drift_body["detected_decisions"]["total"] >= 1

        first_det_id = drift_body["detected_decisions"]["items"][0]["id"]
        dec = client.post(
            f"/api/ui/detected-decisions/{first_det_id}/decide?mock=1",
            json={"action": "accept"},
            headers=headers,
        )
        assert dec.status_code == 200
        assert dec.json()["decision"]["status"] == "accepted"
        assert dec.json()["created_claim_id"] is not None

        # Simulate live file edit -> repo_fingerprint_changed event
        sim_edit = client.post(
            "/api/ui/mock/simulate-file-edit?mock=1",
            json={"file_path": "src/cairnmind/evidence.py"},
            headers=headers,
        )
        assert sim_edit.status_code == 200
        assert sim_edit.json()["old_sha256"] != sim_edit.json()["new_sha256"]

        # 4. Phase 3: Draft Plan, Quick Task, Add Fact, Pre-check Warning & 409 stale_plan_version
        created_plan = client.post(
            "/api/ui/plans?mock=1",
            json={
                "title": "JIRA-5555 Webhook Backoff",
                "goal": "Add exponential backoff without triggering execution.",
                "status": "draft",
                "external_ref": "JIRA-5555",
                "linked_files": ["src/cairnmind/plans.py"],
                "steps": [{"key": "add-backoff", "title": "Implement backoff helper"}],
                "expected_version": 0,
            },
            headers=headers,
        )
        assert created_plan.status_code == 200
        draft_id = created_plan.json()["plan"]["id"]
        assert created_plan.json()["plan"]["status"] == "draft"
        assert created_plan.json()["plan"]["version"] == 1

        # Simulate concurrent MCP edit -> bumps version to 2
        sim_conflict = client.post(
            "/api/ui/mock/simulate-conflict?mock=1",
            json={"plan_id": draft_id, "client": "codex-cli"},
            headers=headers,
        )
        assert sim_conflict.status_code == 200
        assert sim_conflict.json()["current_version"] == 2

        # Saving with stale expected_version=1 MUST return 409 stale_plan_version with diff
        conflict_res = client.patch(
            f"/api/ui/plans/{draft_id}?mock=1",
            json={"expected_version": 1, "goal": "My local stale edit"},
            headers=headers,
        )
        assert conflict_res.status_code == 409
        conflict_detail = conflict_res.json()["detail"]
        assert conflict_detail["code"] == "stale_plan_version"
        assert conflict_detail["expected_version"] == 1
        assert conflict_detail["current_version"] == 2
        assert len(conflict_detail["diff"]) >= 1

        # Saving with fresh expected_version=2 succeeds
        ok_patch = client.patch(
            f"/api/ui/plans/{draft_id}?mock=1",
            json={"expected_version": 2, "goal": "Merged goal"},
            headers=headers,
        )
        assert ok_patch.status_code == 200
        assert ok_patch.json()["plan"]["version"] == 3

        # Promote draft with missing/drifted file triggers pre-check confirmation first
        precheck = client.post(
            "/api/ui/plans/plan-draft-stale-paths/promote?mock=1",
            json={"expected_version": 1, "force": False},
            headers=headers,
        )
        assert precheck.status_code == 200
        assert precheck.json()["requires_confirmation"] is True
        assert "src/cairnmind/legacy_verifier.py" in precheck.json()["precheck"]["missing_files"]

        forced = client.post(
            "/api/ui/plans/plan-draft-stale-paths/promote?mock=1",
            json={"expected_version": 1, "force": True},
            headers=headers,
        )
        assert forced.status_code == 200
        assert forced.json()["ok"] is True
        assert forced.json()["plan"]["status"] == "active"

        # Quick Task + Add Fact
        qt = client.post(
            "/api/ui/tasks?mock=1",
            json={"title": "Add jitter test", "scope": ["src/cairnmind/plans.py"], "expected_version": 0},
            headers=headers,
        )
        assert qt.status_code == 200
        assert qt.json()["task"]["state"] == "draft"

        fact = client.post(
            "/api/ui/facts?mock=1",
            json={
                "kind": "decision",
                "key": "ui.adapter.project_prefix",
                "title": "Keep UI API behind thin adapter",
                "content": "Allows switching cleanly behind /api/ui.",
                "source_path": "src/cairnmind/plans.py",
                "expected_version": 0,
            },
            headers=headers,
        )
        assert fact.status_code == 200
        assert fact.json()["claim"]["origin"]["category"] == "HUMAN_REPO"

        # 5. Phase 4: Version Switcher, Diff, Clone Version as Draft, Reindex, Doctor & Safe Export
        vers = client.get(
            "/api/ui/versions/plan/plan-release-012",
            params={"mock": 1, "from_version": 1, "to_version": 3},
            headers=headers,
        )
        assert vers.status_code == 200
        vers_body = vers.json()
        assert vers_body["from_version"] == 1
        assert vers_body["to_version"] == 3
        assert len(vers_body["diff"]) >= 1

        cloned = client.post(
            "/api/ui/versions/plan/plan-release-012/clone-draft?mock=1",
            json={"source_version": 1},
            headers=headers,
        )
        assert cloned.status_code == 200
        assert cloned.json()["draft_plan"]["status"] == "draft"

        reidx = client.post("/api/ui/actions/reindex?mock=1", headers=headers)
        assert reidx.status_code == 200
        assert reidx.json()["ok"] is True

        doc = client.get("/api/ui/doctor?mock=1", headers=headers)
        assert doc.status_code == 200
        assert doc.json()["backend_mode"] == "mock"
        assert len(doc.json()["scaling_thresholds"]) >= 3

        exp = client.post("/api/ui/actions/export-snapshot?mock=1", json={"mode": "team", "preview": True}, headers=headers)
        assert exp.status_code == 200
        assert exp.json()["redaction_gate"]["status"] in {"PASS", "REDACTED"}
        assert exp.json()["summary"]["held_degraded"] >= 1


def test_cockpit_read_only_mode_and_redaction_gate(monkeypatch):
    """Verify --read-only disables mutations (403) and redaction gate scrubs secrets."""
    from cairnmind.ui.ui_cockpit import scan_and_redact_payload

    redacted, gate = scan_and_redact_payload({
        "token": "Bearer supersecrettokenvalue1234567890abcdef",
        "aws": "AKIAIOSFODNN7EXAMPLE",
        "safe": "normal text",
    })
    assert gate["status"] == "REDACTED"
    assert gate["secrets_redacted"] == 2
    assert "[REDACTED:" in redacted["token"]
    assert "[REDACTED:" in redacted["aws"]
    assert redacted["safe"] == "normal text"

    ro_app = create_ui_app(default_mock=True, read_only=True)
    client, headers = _auth_client(ro_app)
    with client:
        snap = client.get("/api/ui/snapshot?mock=1", headers=headers).json()
        assert snap["capabilities"]["writable"] is False
        assert snap["capabilities"]["read_only_reason"]

        assert client.post("/api/ui/plans?mock=1", json={"title": "Blocked", "goal": ""}, headers=headers).status_code == 403
        assert client.post("/api/ui/tasks?mock=1", json={"title": "Blocked", "expected_version": 0}, headers=headers).status_code == 403
        assert client.post("/api/ui/facts?mock=1", json={"key": "k", "title": "t", "content": "c", "expected_version": 0}, headers=headers).status_code == 403
        assert client.post("/api/ui/actions/reindex?mock=1", headers=headers).status_code == 403
        assert client.post("/api/ui/actions/reverify?mock=1", headers=headers).status_code == 403
        # Read-only preview export and doctor still work cleanly
        assert client.get("/api/ui/doctor?mock=1", headers=headers).status_code == 200
        assert client.post("/api/ui/actions/export-snapshot?mock=1", json={"mode": "team", "preview": True}, headers=headers).status_code == 200
        assert client.post("/api/ui/actions/export-snapshot?mock=1", json={"mode": "team", "preview": False}, headers=headers).status_code == 403

    # CLI --read-only flag integration
    runner = CliRunner()
    started: dict = {}

    class FakeServer:
        def __init__(self, config):
            started["app"] = config.app

        def run(self):
            started["ran"] = True

    monkeypatch.setattr("uvicorn.Server", FakeServer)
    res = runner.invoke(cli.app, ["ui", "--mock", "--read-only", "--no-open"])
    assert res.exit_code == 0, res.output
    assert "[read-only]" in res.output
    assert started["ran"] is True


@pytest.mark.unproven
def test_live_adapter_cockpit_endpoints_respect_person_proof(tmp_path, monkeypatch):
    """Exercise LiveUiAdapter cockpit endpoints and verify person_proof cannot be bypassed."""
    repo = tmp_path / "live_proj"
    repo.mkdir()
    mod = repo / "engine.py"
    mod.write_text("def run_engine():\n    return 'ok'\n", encoding="utf-8")
    mod_sha = ui_server._sha256_bytes(mod.read_bytes())

    plans_tbl: list[dict] = [
        {
            "id": "p-live-1",
            "project_id": "proj1",
            "title": "Live Plan",
            "goal": "Initial goal",
            "status": "draft",
            "version": 1,
            "constraints": [],
            "metadata": {"status": "draft", "linked_files": ["engine.py"], "file_fingerprints": {"engine.py": mod_sha}, "steps": []},
            "created_at": "2026-11-26T10:00:00Z",
            "updated_at": "2026-11-26T10:00:00Z",
        }
    ]
    records_tbl: list[dict] = [
        {
            "id": "r-1",
            "project_id": "proj1",
            "kind": "decision",
            "key": "adr:engine_mode",
            "content": "run_engine returns ok deterministically",
            "status": "proposed",
            "version": 1,
            "source_ids": ["s-1"],
            "metadata": {"evidence": "return 'ok'", "origin": "adr"},
            "updated_at": "2026-11-26T10:00:00Z",
        },
        {
            "id": "r-confirmed",
            "project_id": "proj1",
            "kind": "decision",
            "key": "adr:locked",
            "content": "person confirmed record",
            "status": "confirmed",
            "version": 2,
            "source_ids": ["s-1"],
            "metadata": {"evidence": "return 'ok'", "origin": "adr"},
            "updated_at": "2026-11-26T10:00:00Z",
        },
    ]
    plan_revs: list[dict] = []

    class FakeTx:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def insert_document(self, tbl, doc):
            if tbl == "plans":
                plans_tbl.append(doc)
            elif tbl == "plan_revisions":
                plan_revs.append(doc)
            elif tbl == "records":
                records_tbl.append(doc)
            return doc
        def update_where(self, tbl, *, set_fields=None, remove_fields=(), where=()):
            from cairnmind.storage.storage_query import normalize_where
            target_id = next((c.value for c in normalize_where(where) if getattr(c, "field", None) == "id"), None)
            if tbl == "plans" and set_fields:
                for i, p in enumerate(plans_tbl):
                    if p["id"] == target_id:
                        plans_tbl[i] = dict(set_fields)
            elif tbl == "records" and set_fields:
                for i, r in enumerate(records_tbl):
                    if r["id"] == target_id:
                        records_tbl[i] = dict(set_fields)
            return [dict(set_fields)] if set_fields else []

    class LiveFakeStore:
        def resolve_project(self, identifier):
            return {"id": "proj1", "name": "live-demo", "root": str(repo)}

        def transaction(self):
            return FakeTx()

        def find(self, collection, where=(), *, columns=None, order_by=(), limit=None, distinct=False):
            from cairnmind.storage.storage_query import normalize_where
            conds = normalize_where(where)
            target_id = next((c.value for c in conds if getattr(c, "field", None) == "id" and getattr(c, "op", None) == "="), None)
            if collection == "projects":
                rows = [{"id": "proj1", "name": "live-demo", "root": str(repo)}]
            elif collection == "plans":
                rows = [p for p in plans_tbl if target_id is None or p["id"] == target_id]
            elif collection == "plan_revisions":
                rows = list(plan_revs)
            elif collection == "records":
                rows = [r for r in records_tbl if target_id is None or r["id"] == target_id]
            elif collection == "sources":
                rows = [{"id": "s-1", "uri": str(mod), "kind": "code", "sha256": mod_sha, "is_current": True, "metadata": {"path": "engine.py"}}]
            else:
                rows = []
            return rows[:limit] if limit is not None else rows

        def count(self, collection, where=()):
            return len(self.find(collection, where))

        def query(self, sql, params=None):
            if "FROM projects" in sql:
                return [{"id": "proj1", "name": "live-demo", "root": str(repo)}]
            if "FROM plans" in sql:
                return list(plans_tbl)
            if "FROM plan_revisions" in sql:
                return list(plan_revs)
            if "FROM records" in sql:
                if "count(*)" in sql:
                    return [{"c": len(records_tbl)}]
                return list(records_tbl)
            if "FROM sources" in sql:
                return [{"id": "s-1", "uri": str(mod), "kind": "code", "sha256": mod_sha, "is_current": True, "metadata": {"path": "engine.py"}}]
            return []

    monkeypatch.setattr("cairnmind.memory.evidence.evidence_states", lambda s, pid, recs, quotes=True: {
        r["id"]: {"evidence_state": "anchored", "quote_survives": True} for r in recs
    })
    monkeypatch.setattr("cairnmind.memory.verification.capture_code_state", lambda s, pid, root: {
        "branch": "main", "head": "abcdef123456", "fingerprint": mod_sha,
    })
    monkeypatch.setattr("cairnmind.memory.verification.verification_status", lambda s, task, root: {"stale": False})
    monkeypatch.setattr("cairnmind.scip.ingest.index_project_if_changed", lambda s, pid: {"changed": 1, "errors": []})

    app = create_ui_app(store=LiveFakeStore(), settings=SimpleNamespace(knowledge_agent="disabled"), default_project="proj1")
    client, headers = _auth_client(app)
    with client:
        # Offline Tier 0 FTS Ask
        ask_res = client.post("/api/ui/ask", json={"query": "run_engine deterministic"}, headers=headers)
        assert ask_res.status_code == 200
        assert ask_res.json()["fallback_mode"] is True
        assert len(ask_res.json()["citations"]) == 1

        # Drift Monitor & Accept detected decision stays `proposed` without person_proof
        drift_res = client.get("/api/ui/drift", headers=headers)
        assert drift_res.status_code == 200
        assert drift_res.json()["detected_decisions"]["total"] == 2
        dec_res = client.post("/api/ui/detected-decisions/r-1/decide", json={"action": "accept"}, headers=headers)
        assert dec_res.status_code == 200
        assert dec_res.json()["decision"]["record_status"] == "proposed"
        assert dec_res.json()["decision"]["requires_person_confirmation"] is True
        assert records_tbl[0]["status"] == "proposed"

        # Mutating an already person-confirmed record without person_proof is refused (403)
        locked_res = client.post("/api/ui/detected-decisions/r-confirmed/decide", json={"action": "ignore"}, headers=headers)
        assert locked_res.status_code == 403
        assert records_tbl[1]["status"] == "confirmed"

        # Plan Update CAS 409 & success + Promote + Clone Version
        assert client.patch("/api/ui/plans/p-live-1", json={"expected_version": 99, "goal": "stale"}, headers=headers).status_code == 409
        upd = client.patch("/api/ui/plans/p-live-1", json={"expected_version": 1, "goal": "updated"}, headers=headers)
        assert upd.status_code == 200 and upd.json()["plan"]["version"] == 2
        prom = client.post("/api/ui/plans/p-live-1/promote", json={"expected_version": 2, "force": False}, headers=headers)
        assert prom.status_code == 200 and prom.json()["ok"] is True

        vers = client.get("/api/ui/versions/plan/p-live-1", headers=headers)
        assert vers.status_code == 200 and len(vers.json()["versions"]) >= 1
        assert client.post("/api/ui/versions/plan/p-live-1/clone-draft", json={"source_version": 2}, headers=headers).status_code == 200

        # Create Task, Fact (stored as proposed without person_proof), Reindex, Doctor, Export
        assert client.post("/api/ui/tasks", json={"title": "Live quick task", "expected_version": 0}, headers=headers).status_code == 200
        fact_res = client.post(
            "/api/ui/facts",
            json={"kind": "fact", "key": "k.live", "title": "t", "content": "c", "source_path": "engine.py", "expected_version": 0},
            headers=headers,
        )
        assert fact_res.status_code == 200
        assert fact_res.json()["claim"]["status"] == "proposed"
        assert records_tbl[-1]["status"] == "proposed"
        assert client.post("/api/ui/actions/reindex", headers=headers).status_code == 200
        assert client.get("/api/ui/doctor", headers=headers).status_code == 200
        assert client.post("/api/ui/actions/export-snapshot", json={"mode": "team", "preview": True}, headers=headers).status_code == 200


def test_cockpit_edge_branches_and_coverage(tmp_path, monkeypatch):
    """Cover error, filter, clone, diff, fingerprint, and read-only store edge branches in ui_cockpit."""
    from cairnmind.ui import ui_cockpit

    # Origin classification, diff, and safe_file_fingerprint branches
    assert ui_cockpit.classify_record_origin({"origin_evidence": "degraded"})["category"] == "MCP_DEGRADED"
    assert ui_cockpit.classify_record_origin({"metadata": {"origin": "ui_human"}})["category"] == "HUMAN_REPO"
    assert ui_cockpit.classify_record_origin({"status": "proposed"}, provenance_status="STALE")["category"] == "HUMAN_REPO"
    assert ui_cockpit.classify_audit_origin({"client": "external"})["category"] == "MCP_DEGRADED"
    assert ui_cockpit.classify_audit_origin({"client": "cairnmind-ui"})["category"] == "HUMAN_REPO"

    diff = ui_cockpit.compute_snapshot_diff({"a": 1, "removed": "x"}, {"a": 2, "added": "y"})
    assert {d["change_type"] for d in diff} == {"added", "removed", "modified"}

    repo = tmp_path / "fp_repo"
    repo.mkdir()
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    assert ui_cockpit.safe_file_fingerprint(repo, "") == (False, None)
    assert ui_cockpit.safe_file_fingerprint(repo, "../escape.py") == (False, None)
    assert ui_cockpit.safe_file_fingerprint(repo, ".git/HEAD") == (False, None)
    assert ui_cockpit.safe_file_fingerprint(repo, "missing.py") == (False, None)
    ok, sha = ui_cockpit.safe_file_fingerprint(repo, "ok.py")
    assert ok is True and sha and len(sha) == 64

    # Mock cockpit additional branches
    app = create_ui_app(default_mock=True)
    client, headers = _auth_client(app)
    with client:
        # Ask: Polish and English prompts, general lexical match, general lexical unknown (#60)
        pl_dec = client.post("/api/ui/ask?mock=1", json={"query": "Jakie decyzje wykryto?"}, headers=headers).json()
        assert pl_dec["status"] == "VERIFIED" and "Zatwierdzone decyzje" in pl_dec["answer"]
        en_arch = client.post("/api/ui/ask?mock=1", json={"query": "What are the main architecture components?"}, headers=headers).json()
        assert en_arch["status"] == "VERIFIED" and "four-stage verification pipeline" in en_arch["answer"]
        en_dec = client.post("/api/ui/ask?mock=1", json={"query": "Which recent decisions were detected?"}, headers=headers).json()
        assert en_dec["status"] == "VERIFIED" and "Confirmed decisions" in en_dec["answer"]
        en_stale = client.post(
            "/api/ui/ask?mock=1",
            json={"query": "Which tasks or claims are stale?", "verifier_mode": "fts_fallback"},
            headers=headers,
        ).json()
        assert en_stale["status"] == "PARTIAL"
        assert "No LLM configured" in en_stale["answer"] and "Detected" in en_stale["answer"]
        en_lex = client.post("/api/ui/ask?mock=1", json={"query": "SessionManager require_ack"}, headers=headers).json()
        assert en_lex["status"] == "VERIFIED" and "Found" in en_lex["answer"]
        en_unk = client.post("/api/ui/ask?mock=1", json={"query": "zzz_completely_absent_token_999"}, headers=headers).json()
        assert en_unk["status"] == "UNKNOWN" and "No evidence found" in en_unk["answer"]

        # Drift filters & decision ignore + 404
        assert client.get("/api/ui/drift", params={"mock": 1, "directory": "docs/adr/", "source": "local_file"}, headers=headers).status_code == 200
        assert client.post("/api/ui/detected-decisions/det-adr-001/decide?mock=1", json={"action": "ignore"}, headers=headers).status_code == 200
        assert client.post("/api/ui/detected-decisions/missing-id/decide?mock=1", json={"action": "ignore"}, headers=headers).status_code == 404

        # Active plan creation, full patch fields, 404s, task on plan (404 + 409 + ok)
        created_active = client.post(
            "/api/ui/plans?mock=1",
            json={"title": "Direct Active", "goal": "g", "status": "active", "linked_files": ["src/cairnmind/plans.py"], "steps": [{"key": "s1", "title": "t1"}]},
            headers=headers,
        )
        assert created_active.status_code == 200
        pid = created_active.json()["plan"]["id"]
        assert client.patch("/api/ui/plans/missing-plan?mock=1", json={"expected_version": 1, "title": "x"}, headers=headers).status_code == 404
        assert client.patch(
            f"/api/ui/plans/{pid}?mock=1",
            json={
                "expected_version": 1,
                "title": "Renamed",
                "goal": "New goal",
                "external_ref": "JIRA-1",
                "constraints": ["c1"],
                "linked_files": ["src/cairnmind/sessions.py"],
                "steps": [{"key": "s2", "title": "t2"}],
                "status": "draft",
            },
            headers=headers,
        ).status_code == 200
        assert client.post("/api/ui/plans/missing-plan/promote?mock=1", json={"expected_version": 1}, headers=headers).status_code == 404
        assert client.post(f"/api/ui/plans/{pid}/promote?mock=1", json={"expected_version": 99}, headers=headers).status_code == 409

        # Task attached to plan: 404, 409, and valid v2
        assert client.post("/api/ui/tasks?mock=1", json={"title": "t", "plan_id": "nope", "expected_version": 1}, headers=headers).status_code == 404
        assert client.post("/api/ui/tasks?mock=1", json={"title": "t", "plan_id": pid, "expected_version": 99}, headers=headers).status_code == 409
        assert client.post("/api/ui/tasks?mock=1", json={"title": "t", "plan_id": pid, "expected_version": 2}, headers=headers).status_code == 200

        # Versions fallback for task/claim/404 + clone claim & task & missing version
        assert client.get("/api/ui/versions/task/TSK-103?mock=1", headers=headers).status_code == 200
        assert client.get("/api/ui/versions/claim/clm-plan-cas?mock=1", headers=headers).status_code == 200
        assert client.get("/api/ui/versions/plan/nope?mock=1", headers=headers).status_code == 404
        assert client.post("/api/ui/versions/plan/plan-release-012/clone-draft?mock=1", json={"source_version": 99}, headers=headers).status_code == 404
        assert client.post("/api/ui/versions/claim/clm-occ-gate/clone-draft?mock=1", json={"source_version": 1}, headers=headers).status_code == 200
        assert client.post("/api/ui/versions/task/TSK-103/clone-draft?mock=1", json={"source_version": 3}, headers=headers).status_code == 200

        # Mock simulation 404s & external client conflict
        assert client.post("/api/ui/mock/simulate-conflict?mock=1", json={"plan_id": "nope"}, headers=headers).status_code == 404
        assert client.post("/api/ui/mock/simulate-conflict?mock=1", json={"plan_id": pid, "client": "external"}, headers=headers).status_code == 200
        assert client.post("/api/ui/mock/simulate-file-edit?mock=1", json={"file_path": "missing.py"}, headers=headers).status_code == 404

    # Live adapter error, read-only store, and fallback branches
    class ReadOnlyStore:
        read_only = True
        not_writer = None

        def resolve_project(self, identifier):
            return {"id": "p1", "name": "ro", "root": str(repo)}

        def query(self, sql, params=None):
            if "FROM projects" in sql:
                return [{"id": "p1", "name": "ro", "root": str(repo)}]
            if "FROM plans" in sql:
                return [{
                    "id": "p-ro",
                    "project_id": "p1",
                    "title": "RO Plan",
                    "goal": "g",
                    "version": 1,
                    "metadata": {"status": "draft", "linked_files": ["gone.py", "ok.py"], "file_fingerprints": {"ok.py": "0" * 64}},
                }]
            if "FROM records" in sql:
                if "count(*)" in sql:
                    return [{"c": 2}]
                return [
                    {"id": "rec-t1", "kind": "task", "key": "t1", "content": "Task 1", "status": "confirmed", "version": 1, "source_ids": [], "metadata": {"plan_ref": "p-ro", "state": "blocked"}},
                    {"id": "rec-c1", "kind": "decision", "key": "github:pr-1", "content": "PR decision", "status": "proposed", "version": 1, "source_ids": [], "metadata": {"evidence": "x"}},
                ]
            if "FROM check_runs" in sql:
                return [{"id": "chk-1", "task_id": "rec-t1", "profile_id": "unit", "passed": False}]
            return []

    monkeypatch.setattr("cairnmind.memory.evidence.evidence_states", lambda s, pid, recs, quotes=True: {
        r["id"]: {"evidence_state": "evidence_replaced", "quote_survives": False} for r in recs
    })
    monkeypatch.setattr("cairnmind.memory.verification.capture_code_state", lambda s, pid, root: {"branch": "main", "head": "1234567", "fingerprint": "abc"})
    monkeypatch.setattr("cairnmind.memory.verification.verification_status", lambda s, task, root: {"stale": True})

    ro_adapter = LiveUiAdapter(ReadOnlyStore(), None, UiEventBus(), default_project="p1")
    assert ro_adapter.capabilities()["writable"] is False
    assert ro_adapter.ask(ui_cockpit.UiAskRequest(query="nonexistent_zzz"))["status"] == "UNKNOWN"
    drift_ro = ro_adapter.drift_monitor(source="github", directory="./", status="pending")
    assert drift_ro["detected_decisions"]["total"] == 1
    assert ro_adapter.doctor()["writable"] is False
    assert ro_adapter.get_versions("task", "rec-t1")["current_version"] == 1
    assert ro_adapter.get_versions("claim", "rec-c1")["current_version"] == 1

    # Verify not_writer refusal and live non-mock simulation rejection
    class NotWriterStore(ReadOnlyStore):
        read_only = False
        not_writer = "another host owns the writer lock"

    nw_adapter = LiveUiAdapter(NotWriterStore(), SimpleNamespace(knowledge_agent="enabled"), UiEventBus(), default_project="p1")
    assert nw_adapter.capabilities()["writable"] is False
    assert nw_adapter.capabilities()["llm_configured"] is True

    live_app = create_ui_app(store=NotWriterStore(), settings=SimpleNamespace(knowledge_agent="enabled"), default_project="p1")
    client, headers = _auth_client(live_app)
    with client:
        assert client.post("/api/ui/mock/simulate-conflict", json={"plan_id": "p-ro"}, headers=headers).status_code == 400
        assert client.post("/api/ui/mock/simulate-file-edit", json={"file_path": "ok.py"}, headers=headers).status_code == 400
        assert client.post("/api/ui/plans", json={"title": "Blocked", "goal": ""}, headers=headers).status_code == 403


def test_main_service_mounts_ui_routes_strictly_read_only(tmp_path):
    """Service-token callers on the main service must never write or bypass person_proof via /api/ui/*."""
    from cairnmind.config import Settings
    from cairnmind.service import create_app
    from cairnmind.store import Store

    settings = Settings.load(tmp_path / "svc-data", create=True)
    store = Store.open_memory(tmp_path / "svc-data")
    try:
        svc_app = create_app(settings, start_worker=False, store=store)
        headers = {"Authorization": f"Bearer {settings.token}"}
        with TestClient(svc_app) as client:
            assert client.post("/api/ui/facts", json={"key": "k", "title": "t", "content": "c"}, headers=headers).status_code == 403
            assert client.post("/api/ui/plans", json={"title": "p", "goal": ""}, headers=headers).status_code == 403
            assert client.post("/api/ui/detected-decisions/any/decide", json={"action": "accept"}, headers=headers).status_code == 403
    finally:
        store.close()

