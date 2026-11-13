"""`cairnmind mcp --client external`: the Cursor/Cline/Zed scenario over real stdio MCP, HTTP and ArcadeDB.

No real Cursor, Cline or Zed is involved: the MCP SDK's own stdio client stands in for them, and
its ACKs show the protocol, not that a model read the packet.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest
from typer.testing import CliRunner

from cairnmind.cli import app
from cairnmind.config import Settings, env_names
from cairnmind.mcp_server import ServiceClient, create_server
from cairnmind.store import ORIGIN_METADATA, Store
import test_resume_mcp_protocol
from test_resume_mcp_protocol import ack_arguments, local_service, rejected, tool_result

LAUNCHER_VARIABLES = [key for name in ("SESSION_ID", "PACKET_ID", "PROJECT", "CLIENT", "CONTEXT_FILE")
                      for key in env_names(name)]


class FixtureModel:
    """Answers without a model; the scenario needs `ask` to answer, not to be right."""

    async def models(self):
        return {"models": []}

    async def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    async def chat(self, messages, tools=None, **kwargs):
        return {"message": {"content": json.dumps({"answer": "Nothing is decided yet.", "citations": [],
                                                   "unknowns": ["fixture model"]})}}


def repository(root):
    root.mkdir()
    (root / "app.py").write_text("VALUE = 1\n")
    for args in (("init",), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    return root


def clean_environment(**extra):
    environment = {key: value for key, value in os.environ.items() if key not in LAUNCHER_VARIABLES}
    return {**environment, **extra}


def plain(text):
    # Colour codes go first and leave nothing behind: Rich styles each dash of "--client" separately.
    return " ".join(re.sub(r"[^\w\s:-]", " ", re.sub(r"\x1b\[[0-9;]*m", "", text)).split())


@pytest.fixture
def clean_env(monkeypatch):
    for key in LAUNCHER_VARIABLES:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("variable", ["CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID", "CAIRNMIND_PACKET_ID",
                                      "DEVBRAIN_PACKET_ID"])
def test_a_launcher_bound_process_never_takes_an_external_session(tmp_path, clean_env, monkeypatch, variable):
    settings = Settings.load(tmp_path / "memory", create=True)
    monkeypatch.setenv(variable, "launcher-value")
    with pytest.raises(ValueError, match="launcher"):
        ServiceClient(settings, external={"project": str(tmp_path), "worktree": None})
    # A forwarded but empty variable (how project files pass them) proves nothing and is no reason to refuse.
    monkeypatch.setenv(variable, "")
    assert ServiceClient(settings, external={"project": str(tmp_path), "worktree": None}).session_id is None


async def test_an_external_bridge_is_unbound_until_resume_context(tmp_path, clean_env):
    settings = Settings.load(tmp_path / "memory", create=True)
    bridge = ServiceClient(settings, external={"project": str(tmp_path), "worktree": None})
    for endpoint in ("/remember", "/checkpoint", "/plans/save", "/sessions/ack"):
        with pytest.raises(RuntimeError, match="resume_required: call resume_context"):
            await bridge.post(endpoint, {"project": "p", "packet_id": "x"})
    with pytest.raises(RuntimeError, match="Session mismatch"):
        await bridge.post("/remember", {"session_id": "someone-else"})
    native = ServiceClient(settings)
    with pytest.raises(RuntimeError, match="verified launcher"):
        native.require_session()
    external = create_server(settings, bridge)
    assert "history: unarchived" in external.instructions
    assert "history: unarchived" not in create_server(settings, native).instructions


def test_the_cli_names_only_native_and_external(tmp_path, clean_env):
    runner = CliRunner()
    base = ["--data-dir", str(tmp_path / "memory")]
    for args, message in ((["mcp", "--client", "cursor"], "--client must be native or external"),
                          (["mcp", "--project", str(tmp_path)], "--project and --worktree are only for --client external"),
                          (["mcp", "--client", "native", "--worktree", str(tmp_path)],
                           "--project and --worktree are only for --client external")):
        result = runner.invoke(app, [*base, *args])
        assert result.exit_code != 0
        assert message in plain(result.output), result.output


def test_a_refused_external_start_writes_nothing_to_stdout(tmp_path):
    settings = Settings.load(tmp_path / "memory", create=True)
    result = subprocess.run([sys.executable, "-m", "cairnmind", "--data-dir", str(settings.data_dir), "mcp",
                             "--client", "external", "--project", str(tmp_path)],
                            env=clean_environment(CAIRNMIND_SESSION_ID="launcher-session"),
                            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert result.returncode == 1 and result.stdout == ""
    assert "cannot start where the Cairnmind launcher set" in result.stderr


async def test_the_cursor_scenario_ask_packet_ack_remember(tmp_path, clean_env, monkeypatch):
    root = repository(tmp_path / "repo")
    settings = Settings.load(tmp_path / "memory", create=True)
    store = Store(settings.data_dir)
    project = store.add_project("external-mcp", root)
    note = store.snapshot(project["id"], "manual://editor-note", "The editor note a record cites")
    parameters = StdioServerParameters(
        command=sys.executable, cwd=str(tmp_path),
        args=["-m", "cairnmind", "--data-dir", str(settings.data_dir), "mcp", "--client", "external",
              "--project", str(root)],
        env=clean_environment())
    record = {"kind": "decision", "content": "Cursor decided to keep VALUE at 1",
              "metadata": {"origin_client": "codex", "origin_evidence": "verified"}}
    # The in-thread service answers `ask` with the fixture model instead of refusing every model call.
    monkeypatch.setattr(test_resume_mcp_protocol, "NoGeneration", FixtureModel)
    with local_service(settings):
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                initialized = await client.initialize()
                assert "history: unarchived" in (initialized.instructions or "")
                answer = tool_result(await client.call_tool("ask", {"project": project["id"],
                                                                    "question": "What is decided about VALUE?"}))
                assert "answer" in answer
                assert not store.query("SELECT FROM sessions")
                rejected(await client.call_tool("remember", {"project": project["id"], "record": record,
                                                             "sources": [note["id"]]}))
                assert not store.query("SELECT FROM sessions")

                context = tool_result(await client.call_tool("resume_context", {}))
                packet = context["packet"]
                assert context["status"] == "ready", context
                assert (packet["client"], packet["history"], packet["evidence"]) == ("external", "unarchived",
                                                                                      "degraded")
                assert packet["worktree"] == str(root.resolve())
                rejected(await client.call_tool("remember", {"project": project["id"], "record": record,
                                                             "sources": [note["id"]]}))
                again = tool_result(await client.call_tool("resume_context", {}))
                assert again["session_id"] == context["session_id"] and again["packet"]["id"] == packet["id"]
                assert len(store.query("SELECT FROM sessions")) == 1

                acknowledged = tool_result(await client.call_tool("ack_resume", ack_arguments(packet)))
                assert acknowledged["packet_id"] == packet["id"]
                saved = tool_result(await client.call_tool("remember", {"project": project["id"], "record": record,
                                                                        "sources": [note["id"]]}))
    stored = store.query("SELECT FROM records WHERE id=:id", {"id": saved["id"]})[0]
    assert {key: stored["metadata"][key] for key in ORIGIN_METADATA} == {
        "origin_client": "external", "origin_session_id": context["session_id"],
        "origin_history": "unarchived", "origin_evidence": "degraded"}
    session = store.query("SELECT FROM sessions")[0]
    assert (session["id"], session["client"], session["native_session_id"]) == (context["session_id"], "external", None)
    assert session["worktree"] == str(root.resolve())


async def test_an_unknown_native_transcript_keeps_the_external_client_from_writing(tmp_path, clean_env):
    from cairnmind.ingest import import_history

    root = repository(tmp_path / "repo")
    settings = Settings.load(tmp_path / "memory", create=True)
    store = Store(settings.data_dir)
    project = store.add_project("external-blocked", root)
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(json.dumps({"type": "user", "cwd": str(root), "sessionId": "native",
                                      "message": {"role": "user", "content": "Native work"}}) + "\n"
                          + '{"new_schema":true}\n')
    import_history(store, transcript, project["id"], "claude")
    parameters = StdioServerParameters(
        command=sys.executable, cwd=str(root),
        args=["-m", "cairnmind", "--data-dir", str(settings.data_dir), "mcp", "--client", "external"],
        env=clean_environment())
    with local_service(settings):
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                context = tool_result(await client.call_tool("resume_context", {}))
                assert context["status"] == "partial"
                assert any("Unparsed or conflicting" in text for text in context["unknowns"])
                rejected(await client.call_tool("ack_resume", ack_arguments(context["packet"])))
                rejected(await client.call_tool("checkpoint", {"project": project["id"], "state": {"x": 1}}))
    assert not store.query("SELECT FROM resume_acks")
    assert not store.query("SELECT FROM records WHERE kind='checkpoint'")


def test_relative_paths_are_resolved_by_the_bridge(tmp_path, clean_env, monkeypatch):
    settings = Settings.load(tmp_path / "memory", create=True)
    (tmp_path / "editor").mkdir()
    monkeypatch.chdir(tmp_path / "editor")
    bridge = ServiceClient(settings, external={"project": "../repo", "worktree": "../repo/."})
    assert bridge.external == {"project": str(tmp_path / "repo"), "worktree": str(tmp_path / "repo")}
    named = ServiceClient(settings, external={"project": "external-mcp", "worktree": None})
    assert named.external == {"project": "external-mcp", "worktree": None}


async def test_a_relative_project_names_the_project_seen_from_the_editor(tmp_path, clean_env):
    root = repository(tmp_path / "repo")
    (tmp_path / "editor").mkdir()
    settings = Settings.load(tmp_path / "memory", create=True)
    store = Store(settings.data_dir)
    store.add_project("external-relative", root)
    # The bridge runs in the editor's directory, the service in the test's own: "../repo" means the former.
    assert os.getcwd() != str(tmp_path / "editor")
    parameters = StdioServerParameters(
        command=sys.executable, cwd=str(tmp_path / "editor"),
        args=["-m", "cairnmind", "--data-dir", str(settings.data_dir), "mcp", "--client", "external",
              "--project", "../repo"],
        env=clean_environment())
    with local_service(settings):
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                context = tool_result(await client.call_tool("resume_context", {}))
    assert context["packet"]["worktree"] == str(root.resolve())
    assert store.query("SELECT client FROM sessions")[0]["client"] == "external"
