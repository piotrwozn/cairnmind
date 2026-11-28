"""Fast-tier edge-case tests for evaluation.py, retrieval.py, and integrations.py."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from cairnmind.agents import evaluation, retrieval
from cairnmind.config import Settings
from cairnmind.memory import integrations
from cairnmind.storage.arcade import ArcadeError


def test_evaluation_schema_measured_ollama_and_fixture_mcp_edges(tmp_path, monkeypatch):
    # _case_answer_schema rejects unknown expected field
    with pytest.raises(ValueError, match="field type"):
        evaluation._case_answer_schema(SimpleNamespace(expected={"unknown_field_xyz": "x"}))

    # MeasuredOllama._initial_evidence skips non-dict hits; _candidate handles unusual shapes
    ev = evaluation.MeasuredOllama._initial_evidence([
        {"role": "user", "content": json.dumps({"initial_evidence": ["bad-hit", {"source_id": "s1", "uri": "u1"}]})}
    ])
    assert ev["initial_evidence_total"] == 2 and len(ev["initial_evidence"]) == 1

    assert evaluation.MeasuredOllama._candidate({"message": "not-a-dict"}) == {"message_type": "str"}
    cand = evaluation.MeasuredOllama._candidate({
        "message": {"content": 123, "tool_calls": ["not-a-dict", {"function": {"name": "fn", "arguments": {"a": 1}}}]},
        "done_reason": "stop",
    })
    assert cand["content_type"] == "int" and cand["tool_calls"][0] == {"call_type": "str"}
    cand2 = evaluation.MeasuredOllama._candidate({"message": {"tool_calls": "not-a-list"}})
    assert cand2["tool_calls_type"] == "str"

    # fixture_mcp: rejects non-eval database; with eval DB registers & validates all 4 tools
    settings = Settings.load(tmp_path / "data", create=True)
    with pytest.raises(ValueError, match="isolated evaluation namespace"):
        evaluation.fixture_mcp(settings.data_dir, session_id="s1")

    eval_settings = replace(settings, arcade_database="cairnmind_eval_test")
    monkeypatch.setattr("cairnmind.agents.evaluation.Settings.load", lambda d: eval_settings)

    tools_by_name = {}

    class _FakeMCP:
        def __init__(self, name):
            self.ran = False

        def tool(self):
            def deco(fn):
                tools_by_name[fn.__name__] = fn
                return fn
            return deco

        def run(self, transport="stdio"):
            self.ran = True

    fake_store = SimpleNamespace(
        resolve_project=lambda pid: {"id": pid, "name": "Recall fixture" if pid == "p-ok" else "Other"},
        list_records=lambda pid: [{"id": "r1"}],
        get_source=lambda sid: {"id": sid, "project_id": "p-ok" if sid == "s-ok" else "p-other",
                                "uri": "u", "content": "c", "sha256": "h"},
    )
    fake_sessions = SimpleNamespace(
        resume_context=lambda sid: {"session_id": sid},
        ack=lambda sid, pid, ph, rb: {"acked": True},
        status=lambda sid: {"project_id": "p-ok"},
        require_ack=lambda sid: None,
    )
    monkeypatch.setattr("mcp.server.fastmcp.FastMCP", _FakeMCP)
    monkeypatch.setattr("cairnmind.agents.evaluation.Store", lambda d, settings=None: fake_store)
    monkeypatch.setattr("cairnmind.memory.sessions.SessionManager", lambda s, st: fake_sessions)

    # First run with session_id=None to verify binding guards
    evaluation.fixture_mcp(settings.data_dir, session_id=None)
    for call in (
        lambda: tools_by_name["resume_context"](),
        lambda: tools_by_name["ack_resume"]("p", "h", {}),
        lambda: tools_by_name["memory"]("p-ok"),
    ):
        with pytest.raises(ValueError, match="binding is required"):
            call()

    # Second run with session_id="s1" to exercise happy + scope mismatch paths
    tools_by_name.clear()
    evaluation.fixture_mcp(settings.data_dir, session_id="s1")
    assert tools_by_name["resume_context"]() == {"session_id": "s1"}
    assert tools_by_name["ack_resume"]("p", "h", {}) == {"acked": True}
    assert tools_by_name["memory"]("p-ok")["records"] == [{"id": "r1"}]
    assert tools_by_name["read_source"]("p-ok", "s-ok")["id"] == "s-ok"
    with pytest.raises(ValueError, match="differs from bound"):
        tools_by_name["memory"]("p-wrong")
    with pytest.raises(ValueError, match="outside fixture scope"):
        tools_by_name["read_source"]("p-ok", "s-bad")
    fake_sessions.status = lambda sid: {"project_id": "p-other"}
    with pytest.raises(ValueError, match="Only the recall fixture"):
        tools_by_name["memory"]("p-other")


def test_retrieval_and_integrations_edge_branches(tmp_path, monkeypatch):
    # retrieval.py pure helpers
    assert retrieval.json_value('{"a": 1}') == {"a": 1}
    assert retrieval.json_value("{bad", "fallback") == "fallback"
    assert retrieval.secondary_source("") is False
    assert retrieval.public_metadata("[]", ("a",)) == {"truncated": True}

    meta = retrieval.public_metadata(
        {"summary": "x" * 100, "next_steps": ["a", "b", "c"], "evidence": {"nested": "ignored"}},
        ("summary", "next_steps", "evidence"),
        budget=130,
    )
    assert meta["truncated"] is True and meta["omitted_fields"] >= 1

    rec = retrieval.public_record({
        "id": "r1", "kind": "fact", "key": "k" * 250, "content": "hello",
        "source_ids": [f"s{i}" for i in range(10)], "metadata": {},
    })
    assert rec["key_truncated"] is True and rec["source_ids_truncated"] is True

    with pytest.raises(ValueError, match="Invalid embedding vector"):
        retrieval.validate_vector([0.0, 0.0])

    root = tmp_path.resolve()
    assert retrieval.Retriever._matches_scope("[]", root, [root]) is False
    assert retrieval.Retriever._matches_scope({"path": "relative/a.py"}, root, [root], file_path=True) is False
    assert retrieval.Retriever._matches_scope({"path": str((tmp_path.parent / "other.py").resolve())}, root, [root], file_path=True) is False

    fake_store = SimpleNamespace(resolve_project=lambda p: {"id": "p1"})
    ret = retrieval.Retriever(fake_store, Settings(data_dir=tmp_path / "data"), ollama=SimpleNamespace())
    assert ret._project("p1") == "p1"

    # integrations.py pure config & doctor error branches
    arr_file = tmp_path / "arr.json"
    arr_file.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        integrations._load_json(arr_file)

    bad_mcp = tmp_path / "bad_mcp.json"
    bad_mcp.write_text(json.dumps({"mcpServers": "not-dict"}), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid mcpServers"):
        integrations._merge_json_mcp(bad_mcp, Settings(data_dir=tmp_path / "data"))

    # doctor with failing java probe, failing /api/ps, failing nvidia-smi
    settings = Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(
        "cairnmind.pal.platform_hints.host_system",
        lambda: "windows" if sys.platform == "win32" else "linux",
    )
    monkeypatch.setattr("cairnmind.storage.engine.resolve_java", lambda s: (_ for _ in ()).throw(ArcadeError("no java")))
    monkeypatch.setattr("cairnmind.storage.engine.managed_java_executable", lambda s: None)
    monkeypatch.setattr("cairnmind.memory.integrations.shutil.which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    monkeypatch.setattr("cairnmind.memory.integrations.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(OSError("exec err")))

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [{"name": settings.chat_model, "digest": "d1"}]})
        if req.url.path.endswith("/api/ps"):
            return httpx.Response(500, text="ps err")
        return httpx.Response(503, text="down")

    real_client = integrations.httpx.Client
    monkeypatch.setattr(integrations.httpx, "Client", lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr("cairnmind.storage.database_credentials.doctor_block", lambda s: {"available": False})
    monkeypatch.setattr("cairnmind.memory.memory_models.doctor_models", lambda s, c: {"ok": True})
    monkeypatch.setattr("cairnmind.scip.scip_manager.scip_status", lambda s: {"mode": "off"})
    report = integrations.doctor(settings)
    assert report["java"]["compatible"] is False and report["ollama"]["available"] is True
    assert report["gpu"]["available"] is False and "error" in report["gpu"]
