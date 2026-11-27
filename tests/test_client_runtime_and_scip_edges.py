"""Edge-case tests for client_runtime.py and scip_manager.py (pure fast-tier)."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from cairnmind.agents import client_runtime as cr
from cairnmind.scip import scip_manager as sm
from cairnmind.config import Settings
from cairnmind.pal.private_files import ensure_private_dir
from cairnmind.scip.scip_registry import INDEXERS


def test_native_runtime_validation_memory_status_and_start_edges(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    rt = cr.NativeRuntime(settings)
    ensure_private_dir(rt.directory)

    # get_run rejects invalid ID; _guard rejects dead run
    with pytest.raises(ValueError, match="Invalid native run ID"):
        rt.get_run("../bad")

    # _memory_identity without session_id returns {}; with httpx error returns {}; with valid session returns fields
    assert rt._memory_identity({}) == {}
    monkeypatch.setattr(
        cr.httpx,
        "get",
        lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"session_id": "s1", "native_session_id": "ns1", "acknowledged": True, "stale": False, "status": "ready"},
        ),
    )
    assert rt._memory_identity({"session_id": "s1"})["native_session_id"] == "ns1"

    # memory_status updates registry when native_session_id returned
    rt._save([{"id": "run1", "session_id": "s1", "lifecycle": "exited", "generation": 1, "tmux_session": "cairnmind_run1"}])
    monkeypatch.setattr(rt, "_refresh", lambda row: row)
    ms = rt.memory_status("run1")
    assert ms["native_session_id"] == "ns1"
    assert rt._read()[0]["native_session_id"] == "ns1"

    # _guard rejects non-live run
    with pytest.raises(ValueError, match="no longer running"):
        rt._guard("run1")

    # start_project / start / start_auth / _spawn validation errors
    monkeypatch.setattr(rt, "diagnostics", lambda: {"available": False})
    with pytest.raises(ValueError, match="tmux_missing_or_unsupported"):
        rt.start_project({"id": "p1", "root": str(tmp_path)})
    with pytest.raises(ValueError, match="tmux_missing_or_unsupported"):
        rt.start({"id": "p1", "root": str(tmp_path)}, "codex")
    with pytest.raises(ValueError, match="tmux_missing_or_unsupported"):
        rt.start_auth("codex")
    with pytest.raises(ValueError, match="tmux_missing_or_unsupported"):
        rt._spawn([], {"id": "p1"}, "codex", "s1", {"cwd": str(tmp_path)}, 1, "coding")

    monkeypatch.setattr(rt, "diagnostics", lambda: {"available": True})
    with pytest.raises(ValueError, match="one to eight"):
        rt.start_project({"id": "p1", "root": str(tmp_path)}, slots=[{"id": "s1", "client": "invalid"}])
    with pytest.raises(ValueError, match="unique"):
        rt.start_project({"id": "p1", "root": str(tmp_path)}, slots=[{"id": "s1", "client": "codex"}, {"id": "s1", "client": "codex"}])
    with pytest.raises(ValueError, match="Unsupported native client"):
        rt.start({"id": "p1", "root": str(tmp_path)}, "invalid")
    with pytest.raises(ValueError, match="Invalid native slot ID"):
        rt.start({"id": "p1", "root": str(tmp_path)}, "codex", slot_id="../bad")
    with pytest.raises(ValueError, match="Invalid terminal run ID"):
        rt._spawn([], {"id": "p1"}, "codex", "s1", {"cwd": str(tmp_path)}, 1, "coding", run_id="../bad")

    # start_auth returns existing live auth run
    rt._save([{"id": "auth1", "client": "codex", "purpose": "auth", "lifecycle": "running", "generation": 1}])
    monkeypatch.setattr("cairnmind.agents.client_auth.auth_command", lambda c: ["codex", "login"])
    assert rt.start_auth("codex")["id"] == "auth1"

    # _spawn rollback on tmux error marks failed and re-raises
    monkeypatch.setattr(rt, "_tmux", lambda *a, **k: (_ for _ in ()).throw(OSError("tmux failed")))
    monkeypatch.setattr(rt, "_kill_session", lambda s: None)
    rows = []
    with pytest.raises(OSError, match="tmux failed"):
        rt._spawn(rows, {"id": "p1"}, "codex", "s1", {"cwd": str(tmp_path), "command": ["echo"], "env": {}}, 1, "coding", run_id="runfail")
    assert rows[0]["lifecycle"] == "failed" and rows[0]["error_code"] == "native_spawn_failed"


@pytest.mark.asyncio
async def test_native_runtime_subscribe_validation(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    rt = cr.NativeRuntime(settings)
    ensure_private_dir(rt.directory)
    rt._save([{"id": "run1", "lifecycle": "exited", "generation": 1, "tmux_session": "cairnmind_run1"}])
    monkeypatch.setattr(rt, "_refresh", lambda row: row)
    monkeypatch.setattr(rt, "_pane", lambda row: None)

    with pytest.raises(ValueError, match="unavailable"):
        await rt.subscribe("run1", 1, "conn-1")

    monkeypatch.setattr(rt, "_pane", lambda row: {"dead": False, "pid": 1, "pane": "%1"})
    with pytest.raises(ValueError, match="connection ID"):
        await rt.subscribe("run1", 1, "../bad")


def test_scip_manager_java_redirect_process_and_ecosystem_error_paths(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data")
    fake_java = tmp_path / "java.exe"
    fake_java.write_text("bin", encoding="utf-8")

    # _resolve_java_for_scip: managed java hit, resolve_java hit, exception fallback to shutil.which
    monkeypatch.setattr("cairnmind.storage.engine.managed_java_executable", lambda s: str(fake_java))
    assert sm._resolve_java_for_scip(settings) == str(fake_java)
    monkeypatch.setattr("cairnmind.storage.engine.managed_java_executable", lambda s: None)
    monkeypatch.setattr("cairnmind.storage.engine.resolve_java", lambda s: "/usr/bin/java")
    assert sm._resolve_java_for_scip(settings) == "/usr/bin/java"
    monkeypatch.setattr("cairnmind.storage.engine.resolve_java", lambda s: (_ for _ in ()).throw(RuntimeError("no java")))
    monkeypatch.setattr(sm.shutil, "which", lambda name: "/fallback/java" if name == "java" else None)
    assert sm._find_host_tool(settings, "java", passive=False) == "/fallback/java"

    # _ecosystem_binary_path fallback for unknown installer & _SafeRedirectHandler validation
    unknown_spec = SimpleNamespace(indexer_id="custom", ecosystem_installer="custom")
    assert sm._ecosystem_binary_path(tmp_path, unknown_spec, "linux") == tmp_path / "custom"
    handler = sm._SafeRedirectHandler()
    with pytest.raises(sm.ScipError, match="non-HTTPS"):
        handler.redirect_request(None, None, 302, "Found", {}, "http://evil.example.com/bin")

    # _run_managed_process TimeoutExpired kills process & raises ScipRunError
    class _TimeoutProc:
        pid = 1

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls <= 2:
                raise subprocess.TimeoutExpired(["x"], timeout or 1)
            return b"", b""

        def kill(self):
            pass

    monkeypatch.setattr(sm.subprocess, "Popen", lambda *a, **k: _TimeoutProc())
    monkeypatch.setattr(sm, "ProcessJob", lambda: SimpleNamespace(attach=lambda p: True, terminate=lambda: None, close=lambda: None))
    with pytest.raises(sm.ScipRunError, match="timed out"):
        sm._run_managed_process(["slow"], timeout=1)

    # _install_ecosystem error branches for go, dotnet, composer, and unsupported installer
    monkeypatch.setattr(sm.shutil, "which", lambda name: None)
    for idx_id, expected in [("scip-go", "go executable"), ("scip-dotnet", "dotnet executable"), ("scip-php", "composer executable")]:
        with pytest.raises(sm.ScipError, match=expected):
            sm._install_ecosystem(settings, INDEXERS[idx_id], tmp_path, "linux", 10)

    monkeypatch.setattr(sm.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sm, "_run_managed_process", lambda *a, **k: (1, "out", "err"))
    for idx_id in ("scip-go", "scip-dotnet", "scip-php"):
        with pytest.raises(sm.ScipError, match="failed"):
            sm._install_ecosystem(settings, INDEXERS[idx_id], tmp_path, "linux", 10)

    with pytest.raises(sm.ScipError, match="Unsupported ecosystem installer"):
        sm._install_ecosystem(settings, unknown_spec, tmp_path, "linux", 10)
