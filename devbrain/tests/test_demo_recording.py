"""Real Store/session/check regression coverage; no provider calls or fake chats."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from devbrain.config import Settings
from devbrain.sessions import SessionManager


spec = importlib.util.spec_from_file_location("demo_recording", Path(__file__).parents[1] / "demo/run_demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


@pytest.fixture
def capture_workspace(tmp_path):
    output = tmp_path / "capture"
    settings, store, project, source = demo.setup(output, Settings.load(tmp_path / "engine"))
    try:
        yield output, settings, store, project, source
    finally:
        store.client.server_command(f"drop database {settings.arcade_database}")


def acknowledge(gateway):
    packet = gateway.resume_context()["packet"]
    readback = {name: ({"id": packet[field]["id"], "version": packet[field]["version"]}
                       if packet[field] else None)
                for name, field in (("plan", "active_plan"), ("task", "active_task"))}
    gateway.ack_resume(packet["id"], packet["hash"], readback)


def test_demo_keeps_real_ack_source_and_stale_evidence_contracts(capture_workspace):
    output, settings, store, project, source = capture_workspace
    manager = SessionManager(store, settings)
    codex_session = manager.prepare(project["id"], "codex")
    codex = demo.DemoGateway(output, codex_session["session_id"])
    with pytest.raises(ValueError, match="resume_required"):
        codex.memory()
    acknowledge(codex)
    assert not codex.memory()["records"]
    assert codex.read_source(source["id"])["content"] == source["content"]
    quote = "All prices and totals must use integer cents."
    saved = codex.remember_decision("Use integer cents for prices and totals.", source["id"],
                                    quote, "Verify checkout totals and validation")
    assert saved["decision"]["source_ids"] == [source["id"]]
    assert saved["task"]["metadata"]["required_profiles"] == ["unit"]
    with pytest.raises(ValueError, match="resume_required"):
        codex.memory()  # A write invalidates the old packet; the gateway cannot bypass that.

    claude_session = manager.prepare(project["id"], "claude")
    claude = demo.DemoGateway(output, claude_session["session_id"])
    packet = claude.resume_context()["packet"]
    assert packet["active_task"]["id"] == saved["task"]["id"]
    acknowledge(claude)
    assert any(row["content"] == saved["decision"]["content"] for row in claude.memory()["records"])
    assert quote in claude.read_source(source["id"])["content"]
    with pytest.raises(ValueError, match="read-only"):
        claude.remember_decision("Other decision", source["id"], quote, "Other task")

    result = demo.check_and_change(settings, store, project, saved["task"])
    assert result["fresh"]["status"] == "fresh"
    assert result["stale"]["status"] == "stale"
    assert result["run"]["origin"] == "local_runner"
    assert result["run"]["code_before"]["fingerprint"] != result["current_code"]["fingerprint"]
    changed_check = subprocess.run([sys.executable, "-m", "unittest", "test_checkout"],
                                   cwd=project["root"], capture_output=True, text=True)
    assert changed_check.returncode != 0  # The showcased one-line edit is a genuine regression.
    assert "FAILED" in changed_check.stderr


def test_demo_rejects_fabricated_citations_and_out_of_scope_sources(capture_workspace, tmp_path):
    output, settings, store, project, source = capture_workspace
    session = SessionManager(store, settings).prepare(project["id"], "codex")
    gateway = demo.DemoGateway(output, session["session_id"])
    acknowledge(gateway)
    with pytest.raises(ValueError, match="verbatim"):
        gateway.remember_decision("Use cents", source["id"], "This quote was invented.", "Check checkout")
    assert not store.list_records(project["id"])
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = store.add_project("Other", other_root)
    other_source = store.snapshot(other["id"], "sample://other", "Unrelated content")
    with pytest.raises(ValueError, match="outside"):
        gateway.read_source(other_source["id"])
    with pytest.raises(ValueError, match="new or empty"):
        demo.setup(output, settings)
    assert (output / "checkout/DESIGN.md").is_file()


def test_native_validation_requires_real_calls_and_exact_recall(capture_workspace):
    output, settings, store, project, source = capture_workspace
    session = SessionManager(store, settings).prepare(project["id"], "codex")
    gateway = demo.DemoGateway(output, session["session_id"])
    acknowledge(gateway)
    gateway.memory()
    gateway.read_source(source["id"])
    saved = gateway.remember_decision("Use integer cents.", source["id"],
                                      "All prices and totals must use integer cents.", "Verify checkout")
    answer = {"decision": saved["decision"]["content"], "task_id": saved["task"]["id"],
              "source_id": source["id"], "quote": saved["quote"]}
    result = {"session_id": session["session_id"], "answer": answer}
    demo.validate_native(output, "codex", result, saved["decision"], saved["task"], source)
    with pytest.raises(ValueError, match="required real tool calls"):
        demo.validate_native(output, "claude", result, saved["decision"], saved["task"], source)
    with pytest.raises(ValueError, match="wrong task_id"):
        demo.validate_native(output, "codex", {**result, "answer": {**answer, "task_id": "invented"}},
                             saved["decision"], saved["task"], source)


def test_terminal_cast_keeps_scene_order_and_exact_duration(tmp_path):
    capture = {"scenes": [{"title": "Remember", "subtitle": "Actual result", "command": "read_source",
                           "lines": [{"text": "A literal source quote.", "tone": "green"}], "duration": 15},
                          {"title": "Check", "subtitle": "Actual check", "command": "verification_status",
                           "lines": ["stale"], "duration": 23}]}
    path = tmp_path / "demo.cast"
    demo.write_cast(path, capture)
    header, *events = [json.loads(line) for line in path.read_text().splitlines()]
    assert header["version"] == 2 and header["title"] == "DevBrain terminal demo"
    assert [row[0] for row in events] == [0, 15, 38]
    assert "A literal source quote." in events[0][2]
    assert "stale" in events[1][2]
    assert "> read_source\r\n" in events[0][2]
    assert "Remember" not in events[0][2] and "Actual result" not in events[0][2]
    assert events[0][2].startswith("\x1b[0m\x1b[38;2;51;255;102;48;2;0;0;0m")
