"""Deterministic-only mode on a real store (#274): `knowledge_agent: disabled` in config.json.

No model server exists in these tests. The client given to the service is either Untouched, which records every
attribute anything asks of it (a test then checks the list is empty), or a counting stand-in once models are
configured. The configured-but-down case is here too, as the guard of what must not change.
"""
from __future__ import annotations

from dataclasses import replace
import json
import logging
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest
from typer.testing import CliRunner

from cairnmind import cli, first_start, knowledge_mode
from cairnmind.config import Settings
from cairnmind.ingest import index_project_if_changed
from cairnmind.ollama import OllamaUnavailable
from cairnmind.retrieval import Retriever
from cairnmind.service import create_app
from cairnmind.sessions import SessionManager
from cairnmind.store import Store

from test_external_mcp import clean_environment
from test_resume_mcp_protocol import ack_arguments, local_service, rejected, tool_result

TRANSCRIPT = "User: we chose Postgres for the ledger.\nAssistant: noted, Postgres for the ledger."


class Untouched:
    """A model client nothing may use: every attribute asked of it is recorded and refused."""

    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AttributeError(name)


class Counting:
    """The models once they are configured: embeds and extracts, and remembers what it was asked."""

    def __init__(self):
        self.embedded, self.extracted = [], []

    async def models(self):
        return {"models": []}

    async def model_identity(self, name):
        return name + "@fake"

    async def embed(self, texts):
        self.embedded.extend(texts)
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def chat(self, messages, tools=None, **kwargs):
        self.extracted.append(json.loads(messages[-1]["content"])["source_id"])
        return {"message": {"content": '{"records":[]}'}}

    async def aclose(self):
        pass


class Down(Untouched):
    """A configured model server that does not answer."""

    async def models(self):
        raise OllamaUnavailable("the model server is down")


def repository(root):
    root.mkdir(parents=True)
    (root / ".gitignore").write_text("__pycache__/\n")
    (root / "auth.py").write_text("def authenticate(token):\n    return token == 'valid'\n")
    (root / "notes.md").write_text("The ledger stores amounts in cents.\n")
    (root / ".devbrain").mkdir()
    check = [sys.executable, "-c", "print('checked')"]
    (root / ".devbrain" / "checks.toml").write_text("[checks.unit]\nargv = " + json.dumps(check) + "\n")
    for arguments in (("init",), ("config", "user.email", "fixture@example.invalid"),
                      ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)
    return root


@pytest.fixture
def workspace(tmp_path):
    """A registered project of two files, and settings that say `disabled`; the tests replace that where needed."""
    root = repository(tmp_path / "repo")
    settings = replace(Settings.load(tmp_path / "memory", create=True), knowledge_agent="disabled", poll_seconds=0.2)
    settings.save()
    store = Store(settings.data_dir)
    return settings, store, store.add_project("demo", root), root


def wait_for(found, what, seconds=40):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = found()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def status_of(client, settings):
    return client.get("/status", headers={"Authorization": f"Bearer {settings.token}"}).json()


def jobs(store):
    return sorted((row["id"], row["kind"], row["state"], row.get("attempts") or 0)
                  for row in store.query("SELECT id, kind, state, attempts FROM jobs"))


def test_serve_indexes_files_holds_jobs_and_logs_nothing_without_models(workspace, caplog):
    settings, store, project, root = workspace
    model = Untouched()
    with caplog.at_level(logging.DEBUG), TestClient(create_app(settings, ollama=model)) as client:
        wait_for(lambda: status_of(client, settings)["background"].get("last_scan_at"), "the first scan")
        time.sleep(0.6)  # a few more passes of the scanner, which index nothing new
        report = status_of(client, settings)
        pending = store.query("SELECT count(*) AS n FROM jobs WHERE state='pending' AND (kind='embed' OR kind='extract')")
    assert report["models"] == "not configured (deterministic-only mode)" and report["get_models"] == first_start.GET_MODELS
    assert report["jobs_held"] == pending[0]["n"] > 0
    background = report["background"]
    assert background["model_error"] is None and background["scanner_error"] is None
    assert background["inference"]["worker_running"] is False and background["inference"]["mode"] == "disabled"
    assert store.query("SELECT count(*) AS n FROM chunks WHERE project_id=:p", {"p": project["id"]})[0]["n"] > 0
    assert {row[2] for row in jobs(store) if row[1] == "embed"} == {"pending"}  # held: not failed, not tried
    assert model.touched == []
    noisy = [record.getMessage() for record in caplog.records
             if record.levelno >= logging.WARNING and record.name.startswith("cairnmind")]
    assert noisy == []


def test_held_jobs_run_once_after_models_are_configured_and_a_restart_changes_nothing(workspace):
    settings, store, project, root = workspace
    source = store.snapshot(project["id"], "conversation://1", TRANSCRIPT, "transcript")
    store.enqueue("extract", project["id"], {"source_id": source["id"]}, "extract:" + source["id"])
    for start in (1, 2):  # the second start is a restart in the same mode
        with TestClient(create_app(settings, ollama=Untouched())) as client:
            wait_for(lambda: status_of(client, settings)["jobs_held"], "the held jobs")
            wait_for(lambda: status_of(client, settings)["background"].get("last_scan_at"), "a scan")
            time.sleep(0.6)
        held = jobs(store)
        if start == 1:
            first = held
    assert held == first and {row[2] for row in held} == {"pending"} and {row[3] for row in held} == {0}
    assert any(row[1] == "extract" for row in held)

    assert knowledge_mode.turn_on(settings) is not None  # `models pull` or `models set`: the agent is on
    models = Counting()
    with TestClient(create_app(settings, ollama=models)) as client:
        wait_for(lambda: not store.query("SELECT id FROM jobs WHERE state IN ['pending','running'] "
                                         "AND (kind='embed' OR kind='extract')"), "the held jobs to run")
        report = status_of(client, settings)
    done = jobs(store)
    assert {row[2] for row in done if row[1] in ("embed", "extract")} == {"completed"}
    assert {row[3] for row in done if row[1] in ("embed", "extract")} == {1}  # each job ran once
    texts = sorted(row["text"] for row in store.query("SELECT text FROM chunks WHERE project_id=:p",
                                                      {"p": project["id"]}))
    assert texts and sorted(models.embedded) == texts  # the model was asked once for each chunk
    assert store.query("SELECT count(*) AS n FROM embeddings")[0]["n"] == len(texts)
    assert source["id"] in models.extracted and len(models.extracted) == len(set(models.extracted))  # once each
    assert "models" not in report and report["background"]["inference"]["worker_running"] is True


@pytest.mark.parametrize("stored", ["", "enabled"])
def test_a_configured_installation_with_its_server_down_is_still_a_fault(workspace, stored):
    settings, store, project, root = workspace
    settings = replace(settings, knowledge_agent=stored)
    with TestClient(create_app(settings, ollama=Down())) as client:
        report = wait_for(lambda: (r := status_of(client, settings))["background"]["model_error"] and r, "the fault")
    assert "the model server is down" in report["background"]["model_error"]
    assert "models" not in report and "jobs_held" not in report
    assert report["background"]["inference"]["worker_running"] is True
    assert {row[2] for row in jobs(store) if row[1] == "embed"} == {"pending"}  # waiting, not failed


def test_ask_without_models_is_unavailable_through_the_api_the_cli_and_mcp(workspace):
    import anyio
    settings, store, project, root = workspace
    other = store.add_project("other", repository(root.parent / "other"))
    foreign = SessionManager(store, settings).prepare(other["id"], "codex", timeout=0)["session_id"]
    expected = knowledge_mode.unavailable_answer()
    assert expected["status"] == "unavailable" and expected["sources"] == []
    headers = {"Authorization": f"Bearer {settings.token}"}

    async def through_mcp():
        parameters = StdioServerParameters(
            command=sys.executable, cwd=str(root.parent), env=clean_environment(),
            args=["-m", "cairnmind", "--data-dir", str(settings.data_dir), "mcp", "--client", "external",
                  "--project", str(root)])
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                return tool_result(await client.call_tool("ask", {"project": project["id"],
                                                                  "question": "What is decided?"}))

    with local_service(settings):  # the service reads `disabled` and has a model client that fails the test
        def post(**body):
            return httpx.post(f"{settings.url}/ask", headers=headers, json={"project": "demo", **body}, timeout=30)
        asked, logged = post(question="What is decided?"), post(question="Again?", log=True)
        unknown = post(project="nothing", question="?")
        refused = post(question="?", session_id=foreign)
        cli_result = CliRunner().invoke(cli.app, ["--data-dir", str(settings.data_dir), "ask", "demo", "What is decided?"])
        mcp_result = anyio.run(through_mcp)
    assert asked.status_code == 200 and asked.json() == expected
    assert logged.status_code == 200 and logged.json() == expected
    assert not (settings.data_dir / "answer-log").exists()  # nothing was answered, so nothing is kept
    assert unknown.status_code == 404 and refused.status_code == 400  # the project and the session are still checked
    assert cli_result.exit_code == 0, cli_result.output  # the convention for every answer status
    assert json.loads(cli_result.stdout) == expected
    assert mcp_result == expected


async def test_the_ack_gate_and_plans_work_over_mcp_without_models(workspace):
    settings, store, project, root = workspace
    note = store.snapshot(project["id"], "manual://editor-note", "The editor note a record cites")
    record = {"kind": "decision", "content": "Keep VALUE at 1", "metadata": {}}
    parameters = StdioServerParameters(
        command=sys.executable, cwd=str(root.parent), env=clean_environment(),
        args=["-m", "cairnmind", "--data-dir", str(settings.data_dir), "mcp", "--client", "external",
              "--project", str(root)])
    with local_service(settings):
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                context = tool_result(await client.call_tool("resume_context", {}))
                assert context["status"] == "ready" and context["packet"]["evidence"] == "degraded"
                rejected(await client.call_tool("remember", {"project": project["id"], "record": record,
                                                             "sources": [note["id"]]}))  # no ACK yet
                tool_result(await client.call_tool("ack_resume", ack_arguments(context["packet"])))
                saved = tool_result(await client.call_tool("save_plan", {
                    "project": project["id"], "plan": {"title": "Ledger", "goal": "Keep amounts in cents"},
                    "steps": [{"key": "verify", "content": "Verify the amounts"}], "idempotency_key": "plan-1",
                    "worktree": str(root), "activate": True}))
                assert saved
                step = tool_result(await client.call_tool("next_task", {"project": project["id"],
                                                                        "worktree": str(root)}))
                assert step

                async def read_and_ack_again():  # each write changes what the session must have read: a new ACK
                    fresh = tool_result(await client.call_tool("resume_context", {}))
                    tool_result(await client.call_tool("ack_resume", ack_arguments(fresh["packet"])))
                await read_and_ack_again()
                kept = tool_result(await client.call_tool("remember", {"project": project["id"], "record": record,
                                                                       "sources": [note["id"]]}))
                await read_and_ack_again()
                tool_result(await client.call_tool("checkpoint", {"project": project["id"],
                                                                  "state": {"summary": "without models"}}))
                tool_result(await client.call_tool("check_status", {"project": project["id"]}))
    stored = store.query("SELECT FROM records WHERE id=:id", {"id": kept["id"]})[0]
    assert stored["status"] == "proposed" and stored["metadata"]["origin_evidence"] == "degraded"


def test_each_deterministic_command_works_without_models(workspace):
    settings, store, project, root = workspace
    runner = CliRunner()
    base = ["--data-dir", str(settings.data_dir)]

    def run(*args, text=False):
        result = runner.invoke(cli.app, [*base, *args])
        assert result.exit_code == 0, result.output or repr(result.exception)
        return result.stdout if text else json.loads(result.stdout)

    assert run("init")["knowledge_agent"] == "disabled"  # a stored choice is kept, and nothing is asked
    run("remember", "demo", "Amounts are kept in cents", "--kind", "decision")
    task = run("task", "add", "demo", "Verify amounts", "--plan", "ledger", "--step", "1", "--requires-test")
    verified = run("check", "run", "demo", "unit", "--task", task["id"])
    assert verified["passed"] and verified["code_before"]["fingerprint"] == verified["code_after"]["fingerprint"]
    assert "Brief for demo" in run("brief", "demo", text=True)
    run("impact", "demo", "--since", "HEAD", text=True)
    assert run("task", "list", "demo")
    with local_service(settings):  # the service: the memory commands and the session context go through it
        snapshot = run("memory", "snapshot", "demo")
        shown = run("memory", "show", snapshot["path"], text=True)
        context = httpx.get(f"{settings.url}/context", params={"project": "demo"},
                            headers={"Authorization": f"Bearer {settings.token}"}, timeout=30)
    assert "Amounts are kept in cents" in shown
    assert context.status_code == 200
    # Indexing makes chunks and their full-text index with no model, and search finds them by words.
    index_project_if_changed(store, project["id"])
    model = Untouched()
    hits = Retriever(store, settings, model).search(project["id"], "authenticate")
    assert hits and "authenticate" in json.dumps(hits)
    assert model.touched == []
