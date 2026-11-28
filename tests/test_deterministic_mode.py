"""Deterministic-only mode on a real store (#274): `knowledge_agent: disabled` in config.json.

No model server exists in these tests. The client given to the service in-process is Untouched, which records every
attribute anything asks of it (a test then checks the list is empty), or a counting stand-in once models are
configured. The tests that run a real service (local_service) use NoGeneration: its models() answers, and embed
and chat raise, so a model call fails the test. The configured-but-down case is here too, as the guard of what
must not change.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_external_mcp import clean_environment
from test_resume_mcp_protocol import ack_arguments, local_service, rejected, tool_result
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.agents import knowledge_mode
from cairnmind.agents.ollama import OllamaUnavailable
from cairnmind.agents.retrieval import Retriever
from cairnmind.cli_cmds import first_start
from cairnmind.cli_cmds.topology import MODELS_DISABLED
from cairnmind.config import Settings
from cairnmind.memory.sessions import SessionManager
from cairnmind.scip.ingest import index_project_if_changed
from cairnmind.service import create_app
from cairnmind.store import Store

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


def test_status_names_the_topology_the_service_runs_with(workspace):
    settings, store, project, root = workspace
    model = Untouched()
    with TestClient(create_app(settings, ollama=model)) as client:
        report = status_of(client, settings)
    place = report["topology"]
    assert place["lines"][1] == MODELS_DISABLED
    assert place["storage"]["writer"]["role"] == "writer" and place["storage"]["engine"] == report["storage"]["engine"]
    assert place["models"]["evaluated"] is None and place["this_machine"][0]["url"] == settings.url
    assert model.touched == []


def test_held_jobs_run_once_after_models_are_configured_and_a_restart_changes_nothing(workspace):
    settings, store, project, root = workspace
    source = store.snapshot(project["id"], "conversation://1", TRANSCRIPT, "transcript")
    store.enqueue("extract", project["id"], {"source_id": source["id"]}, "extract:" + source["id"])
    for start in (1, 2):  # the second start is a restart in the same mode
        model = Untouched()
        with TestClient(create_app(settings, ollama=model)) as client:
            wait_for(lambda: status_of(client, settings)["jobs_held"], "the held jobs")
            wait_for(lambda: status_of(client, settings)["background"].get("last_scan_at"), "a scan")
            time.sleep(0.6)
            report = status_of(client, settings)
        # no jobs loop ran: the model was not asked, no fault was recorded, no worker is claimed
        assert model.touched == [] and report["background"]["model_error"] is None
        assert report["background"]["inference"]["worker_running"] is False
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


def test_the_embedding_fingerprint_is_not_requested_without_models(workspace, monkeypatch):
    from cairnmind.memory import memory_fingerprint
    settings, store, project, root = workspace
    asked = []

    async def check(client, **kwargs):
        asked.append(client)
        return {"status": "ok"}
    monkeypatch.setattr(memory_fingerprint, "client_of", lambda models: SimpleNamespace())
    monkeypatch.setattr(memory_fingerprint, "check", check)
    with TestClient(create_app(settings, ollama=Untouched())) as client:  # disabled
        wait_for(lambda: status_of(client, settings)["background"].get("last_scan_at"), "a scan")
        time.sleep(0.4)
        report = status_of(client, settings)
    assert asked == [] and "embedding_fingerprint" not in report["background"]
    with TestClient(create_app(replace(settings, knowledge_agent="enabled"), ollama=Down())) as client:
        wait_for(lambda: asked, "the fingerprint request of a configured installation")  # the stand-in does work


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


QUESTION = "Where are the ledger amounts stored?"


def without_time(answer):
    return {key: value for key, value in answer.items() if key != "elapsed_seconds"}


def test_ask_without_models_lists_full_text_matches_through_the_api_the_cli_and_mcp(workspace):
    import anyio
    settings, store, project, root = workspace
    other = store.add_project("other", repository(root.parent / "other"))
    foreign = SessionManager(store, settings).prepare(other["id"], "codex", timeout=0)["session_id"]
    headers = {"Authorization": f"Bearer {settings.token}"}

    async def through_mcp():
        parameters = StdioServerParameters(
            command=sys.executable, cwd=str(root.parent), env=clean_environment(),
            args=["-m", "cairnmind", "--data-dir", str(settings.data_dir), "mcp", "--client", "external",
                  "--project", str(root)])
        async with stdio_client(parameters) as (reader, writer), ClientSession(reader, writer) as client:
            await client.initialize()
            return tool_result(await client.call_tool("ask", {"project": project["id"], "question": QUESTION}))

    with local_service(settings):  # the service reads `disabled` and has a model client that fails the test
        def post(**body):
            return httpx.post(f"{settings.url}/ask", headers=headers, json={"project": "demo", **body}, timeout=30)
        asked, logged = post(question=QUESTION), post(question=QUESTION, log=True)
        nothing = post(question="zebra quokka")
        unknown = post(project="nothing", question="?")
        refused = post(question="?", session_id=foreign)
        blank = post(question="   ")  # refused as with models, not answered "no passage"
        cli_result = CliRunner().invoke(cli.app, ["--data-dir", str(settings.data_dir), "ask", "demo", QUESTION])
        mcp_result = anyio.run(through_mcp)
        cached = httpx.get(f"{settings.url}/status", headers=headers, timeout=30).json()["cached_answers"]
    assert asked.status_code == 200
    body = asked.json()
    assert body["status"] == "unavailable" and body["diagnostics"]["code"] == "models_not_configured"
    assert body["sources"] == [] and body["answer"] == first_start.ASK_OFF and "citations" not in body
    matches = body["full_text_matches"]
    assert 1 <= len(matches) <= 8 and all(len(match["excerpt"]) <= 400 and match["match"] == "full_text"
                                         for match in matches)
    notes = [match for match in matches if match["uri"].endswith("notes.md")]
    assert notes and "ledger stores amounts in cents" in notes[0]["excerpt"]
    assert logged.status_code == 200 and without_time(logged.json()) == without_time(body)
    assert not (settings.data_dir / "answer-log").exists()  # nothing was answered, so nothing is kept
    assert nothing.json()["full_text_matches"] == [] and nothing.json()["answer"] == first_start.ASK_NO_MATCH
    assert nothing.json()["status"] == "unavailable" and cached == 0
    assert unknown.status_code == 404 and refused.status_code == 400  # the project and the session are still checked
    assert blank.status_code == 400 and "Question must contain" in blank.text
    # NoGeneration.embed raises, but Retriever.asearch turns that into this warning: it would pass unseen
    assert not any("Semantic query" in warning for warning in body["freshness"]["warnings"])
    assert cli_result.exit_code == 0, cli_result.output  # the convention for every answer status
    assert without_time(json.loads(cli_result.stdout)) == without_time(body)
    assert without_time(mcp_result) == without_time(body)


def test_full_text_matches_stay_in_the_project_the_worktree_and_the_session(workspace):
    settings, store, project, root = workspace
    other_root = repository(root.parent / "other")
    (other_root / "ledger.md").write_text("The ledger of the other project keeps cents.\n")
    other = store.add_project("other", other_root)
    feature = root.parent / "feature"
    feature.mkdir()
    (feature / "feature.md").write_text("The ledger feature keeps cents.\n")
    store.add_project_root(project["id"], feature)
    index_project_if_changed(store, other["id"])
    session = SessionManager(store, settings).prepare(project["id"], "codex", worktree=str(feature), timeout=0)
    model = Untouched()
    headers = {"Authorization": f"Bearer {settings.token}"}

    def names(client, under, **body):
        reply = client.post("/ask", headers=headers, json={"project": "demo", "question": "ledger cents", **body})
        assert reply.status_code == 200, reply.text
        uris = [match["uri"] for match in reply.json()["full_text_matches"]]
        assert all(uri.startswith(str(under)) for uri in uris), uris  # a basename alone would hide a leak
        return {Path(uri).name for uri in uris}

    with TestClient(create_app(settings, start_worker=False, ollama=model)) as client:  # the lifespan runs the queue
        assert names(client, root, worktree=str(root)) == {"notes.md"}  # not the feature root, not the other project
        assert names(client, feature, worktree=str(feature)) == {"feature.md"}
        assert names(client, feature, session_id=session["session_id"]) == {"feature.md"}  # the session's worktree
    assert model.touched == []


async def test_full_text_matches_leave_out_what_ask_would_hold_back(tmp_path):
    from uuid import uuid4

    from cairnmind.agents.agent import KnowledgeAgent
    store = Store(tmp_path / "data")
    pid = store.add_project("ledger", tmp_path)["id"]
    remote = {"provider": "openai_compatible", "base_url": "https://models.example/v1", "allow_remote": True}

    def chunked(text, kind, uri, metadata=None):
        source = store.snapshot(pid, uri, text, kind, metadata=metadata)
        store.command("INSERT INTO chunks CONTENT :doc", {"doc": {
            "id": str(uuid4()), "project_id": pid, "source_id": source["id"], "text": text, "line_start": 1,
            "line_end": len(text.splitlines()), "metadata": {}, "is_current": True}})
        return source

    chat = chunked(TRANSCRIPT, "transcript", "conversation://1")
    doc = chunked("The ledger stores amounts in cents, Postgres.", "document", "notes://ledger")
    store.remember(pid, {"kind": "decision", "content": "Postgres holds the ledger, said in chat."}, [chat["id"]])
    store.remember(pid, {"kind": "fact", "content": "Ledger amounts are cents, says the note, Postgres."}, [doc["id"]])
    question, model = "Which database holds the ledger Postgres?", Untouched()
    held = Settings(data_dir=tmp_path / "data", memory_chat=remote, knowledge_agent="disabled")
    found = await knowledge_mode.text_matches(KnowledgeAgent(store, held, ollama=model), pid, question)
    texts = json.dumps(found["matches"])
    assert chat["id"] not in {match["source_id"] for match in found["matches"]} and "said in chat" not in texts
    assert doc["id"] in {match["source_id"] for match in found["matches"]}
    assert found["held_back"]["chunks"] >= 1 and found["held_back"]["records"] >= 1
    assert first_start.MATCHES_HELD_BACK in knowledge_mode.unavailable_answer(found)["unknowns"]
    only_held = await knowledge_mode.text_matches(KnowledgeAgent(store, held, ollama=model), pid, "chose")
    said = knowledge_mode.unavailable_answer(only_held)  # found, then held back: never "found no passage"
    assert only_held["matches"] == [] and only_held["held_back"] and only_held["left_out"]
    assert said["answer"] == first_start.ASK_LEFT_OUT and first_start.MATCHES_HELD_BACK in said["unknowns"]
    external = chunked("An external report says the ledger is kept in Postgres.", "document", "notes://external",
                       {"origin_evidence": "degraded", "origin_client": "external"})
    local = Settings(data_dir=tmp_path / "data", knowledge_agent="disabled")
    everything = await knowledge_mode.text_matches(KnowledgeAgent(store, local, ollama=model), pid, question)
    assert chat["id"] in {match["source_id"] for match in everything["matches"]} and everything["held_back"] is None
    marked = [match for match in everything["matches"] if match["source_id"] == external["id"]]
    assert marked and marked[0]["metadata"]["origin_evidence"] == "degraded"  # the mark of #177 survives
    assert model.touched == []


async def test_a_file_changed_since_indexing_is_said_to_be_left_out(workspace):
    from cairnmind.agents.agent import KnowledgeAgent
    settings, store, project, root = workspace
    index_project_if_changed(store, project["id"])
    (root / "notes.md").write_text("The ledger amounts are kept in cents, since the file was edited.\n")
    found = await knowledge_mode.text_matches(KnowledgeAgent(store, settings, ollama=Untouched()), project["id"],
                                              "ledger amounts")
    assert found["matches"] == [] and found["left_out"]
    assert any("changed since it was indexed" in warning and "notes.md" in warning for warning in found["warnings"])
    assert knowledge_mode.unavailable_answer(found)["answer"] == first_start.ASK_LEFT_OUT


def test_with_models_configured_ask_never_lists_matches(workspace, monkeypatch):
    settings, store, project, root = workspace

    class ServerDown(Untouched):
        async def chat(self, *args, **kwargs):
            raise OllamaUnavailable("the model server is down")

        embed = chat

    def never(*args, **kwargs):
        raise AssertionError("text_matches is reached only without models")
    monkeypatch.setattr(knowledge_mode, "text_matches", never)
    with TestClient(create_app(replace(settings, knowledge_agent="enabled", ask_timeout=20), start_worker=False,
                               ollama=ServerDown())) as client:
        reply = client.post("/ask", headers={"Authorization": f"Bearer {settings.token}"},
                            json={"project": "demo", "question": QUESTION})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["diagnostics"]["code"] == "model_unavailable" and "full_text_matches" not in body
    assert set(body) == {"status", "answer", "sources", "unknowns", "freshness", "elapsed_seconds", "diagnostics",
                         "cached"}


async def test_the_ack_gate_and_plans_work_over_mcp_without_models(workspace):
    settings, store, project, root = workspace
    note = store.snapshot(project["id"], "manual://editor-note", "The editor note a record cites")
    record = {"kind": "decision", "content": "Keep VALUE at 1", "metadata": {}}
    parameters = StdioServerParameters(
        command=sys.executable, cwd=str(root.parent), env=clean_environment(),
        args=["-m", "cairnmind", "--data-dir", str(settings.data_dir), "mcp", "--client", "external",
              "--project", str(root)])
    with local_service(settings):
        async with stdio_client(parameters) as (reader, writer), ClientSession(reader, writer) as client:
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
    assert any("Amounts are kept in cents" in json.dumps(row) for row in run("records", "demo"))
    with local_service(settings):  # the service: the memory commands and the session context go through it
        snapshot = run("memory", "snapshot", "demo")
        shown = run("memory", "show", snapshot["path"], text=True)
        previewed = run("memory", "publish", "demo", "--worktree", str(root), "--preview")
        assert previewed  # a preview of the confirmed records: nothing written, no model asked
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


def test_process_memory_without_models_says_why_and_with_models_is_unchanged(workspace):
    """The /background-inference/enable response carries the sentence the Process memory panel shows (#294)."""
    settings, store, project, root = workspace
    body = {"project": project["id"], "worktree": str(root)}
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, ollama=Untouched())) as client:  # disabled
        off = client.post("/background-inference/enable", json=body, headers=headers).json()
        # models pull ran after the start: the advice is the restart, not the command that already ran
        (settings.data_dir / "config.json").write_text(json.dumps({"knowledge_agent": "enabled"}), encoding="utf-8")
        restart = client.post("/background-inference/enable", json=body, headers=headers).json()
    assert off["worker_running"] is False and off["notice"] == first_start.PROCESS_OFF
    assert "cairnmind models pull" in off["notice"] and "cairnmind models pull" in first_start.MESSAGES[first_start.PROCESS_OFF]
    assert restart["notice"] == first_start.PROCESS_RESTART and "models pull" not in restart["notice"]
    with TestClient(create_app(replace(settings, knowledge_agent="enabled"), ollama=Down())) as client:
        on = client.post("/background-inference/enable", json=body, headers=headers).json()
    assert "notice" not in on
