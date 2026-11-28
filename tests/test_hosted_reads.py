"""A built-in agent on a hosted model reads memory only in the classes the person allowed (#183).

The model is a scripted fake and memory a fake client or a store in a temporary directory: nothing leaves
the test. Hosted means a Copilot connection or an API profile whose address is not loopback. The four
reads are read_source, memory_records, the quotes of an ask answer and the record previews of the resume
packet; the setting is memory_remote_classes. Nothing here is specific to an operating system: on macOS
every test runs as on Linux (the store tests use the same temporary ArcadeDB data directory).
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_agent_memory import ack  # noqa: F401 - a fixture
from test_agent_memory import workspace as workspace
from test_api_agent import Memory, ScriptedProvider, call, reply, tools_results
from test_api_agent import setup as setup  # noqa: F401 - a fixture
from test_deterministic_mode import Untouched
from test_remote_classes import DOCUMENT, TRANSCRIPT, FakeModels, answer, chunked

from cairnmind.agents.agent import KnowledgeAgent
from cairnmind.agents.agent_memory import AgentMemoryClient
from cairnmind.agents.api_agent import AgentEngine
from cairnmind.agents.knowledge_mode import text_matches
from cairnmind.config import Settings
from cairnmind.memory import memory_egress
from cairnmind.service import create_app
from cairnmind.terminal.terminal_i18n_messages import AGENT_MESSAGES, ASK_MESSAGES

HOSTED = {"provider": "openai", "base_url": "https://api.openai.com/v1"}
LOCAL = {"provider": "ollama", "base_url": "http://127.0.0.1:11434"}
HELD = ["conversation", "third_party"]


def source_result(kind, text, sensitivity, **metadata):
    return {"source": {"id": "s-" + kind, "kind": kind, "metadata": metadata}, "view": "raw", "text": text,
            "sensitivity": sensitivity}


class Reading(Memory):
    """The service's answers to the two reads, by source id; the calls are recorded."""

    def __init__(self, sources, records=None):
        super().__init__()
        self.sources, self.records = sources, records or {"items": []}

    async def execute(self, name, arguments):
        if name == "read_source":
            self.calls.append((name, arguments))
            return deepcopy(self.sources[arguments["source_id"]])
        if name == "memory_records":
            self.calls.append((name, arguments))
            return deepcopy(self.records)
        return await super().execute(name, arguments)


class Copilot(ScriptedProvider):
    """A Copilot connection: the SDK calls the engine's tool handler itself; the requests are recorded."""

    def __init__(self, *calls):
        super().__init__()
        self.calls = calls

    async def run_turn(self, prompt, tools, handler, *, system="", on_delta=None, on_event=None):
        for item in self.calls:
            self.requests.append({"messages": [await handler(item["name"], item["arguments"])], "tools": tools})
        return {"text": "Done.", "usage": {}}


async def read(setup, connection, memory, *calls, client="api", settings=None):
    base, run, profile, _ = setup
    run = {**run, "client": client}
    provider = Copilot(*calls) if client == "copilot" else ScriptedProvider(reply(*calls), reply(text="Done."))
    agent = AgentEngine(settings or base, run, {**profile, **connection}, provider=provider, memory=memory)
    try:
        await agent.turn("Read memory")
        return tools_results(agent), provider, agent
    finally:
        await agent.close()


SOURCES = {"chat": source_result("transcript", TRANSCRIPT, "conversation"),
           "doc": source_result("document", DOCUMENT, "project"),
           "own": source_result("agent_transcript", "tool output of this run", "conversation",
                                session_id="session-one"),
           "other": source_result("agent_transcript", "tool output of another run", "conversation",
                                  session_id="session-two"),
           "bare": {"source": {"id": "s-bare"}, "text": "text with no class"}}


def test_hosted_means_copilot_or_an_address_that_is_not_loopback(setup):
    settings = setup[0]
    held = memory_egress.coding_held_classes
    assert held(settings, "copilot", {}) == {"conversation", "third_party"}
    assert held(settings, "api", HOSTED) == {"conversation", "third_party"}
    assert held(settings, "api", {"base_url": "http://192.168.1.20:11434"}) == {"conversation", "third_party"}
    assert held(settings, "api", {"provider": "openai_compatible"}) == {"conversation", "third_party"}  # unreadable
    assert held(settings, "api", LOCAL) == frozenset() and held(settings, "api", {"base_url": "http://localhost:1/"}) == frozenset()
    allowed = replace(settings, memory_remote_classes=["project", "conversation"])
    assert held(allowed, "api", HOSTED) == {"third_party"}
    broken = replace(settings, memory_remote_classes="project")
    assert held(broken, "api", HOSTED) == {"project", "conversation", "third_party"}  # an invalid setting holds all


@pytest.mark.parametrize("connection, client", [(HOSTED, "api"), ({}, "copilot")])
async def test_a_hosted_run_is_refused_a_conversation_source_and_the_text_never_reaches_the_provider(
        setup, connection, client):
    memory = Reading(SOURCES)
    results, provider, _ = await read(setup, connection, memory, call("read_source", {"source_id": "chat"}),
                                      call("read_source", {"source_id": "doc"}),
                                      call("read_source", {"source_id": "bare"}), client=client)
    refused, document, unknown = results[-3:]
    assert refused["error_code"] == "held_back_by_class" and refused["class"] == "conversation"
    assert "conversation" in refused["message"] and "memory_remote_classes" in refused["message"]
    assert "we chose Postgres" not in json.dumps(provider.requests) and "text" not in refused
    assert DOCUMENT in document["text"]  # project material is read
    assert unknown["error_code"] == "held_back_by_class"  # a result with no class is held, never passed
    assert "text with no class" not in json.dumps(provider.requests)


async def test_a_local_run_reads_the_same_sources_as_before(setup):
    results, _, _ = await read(setup, LOCAL, Reading(SOURCES), call("read_source", {"source_id": "chat"}),
                               call("read_source", {"source_id": "bare"}))
    assert TRANSCRIPT in results[-2]["text"] and results[-1]["text"] == "text with no class"


async def test_adding_conversation_to_the_setting_lets_a_hosted_run_read_it(setup):
    allowed = replace(setup[0], memory_remote_classes=["project", "conversation"])
    results, _, _ = await read(setup, HOSTED, Reading(SOURCES), call("read_source", {"source_id": "chat"}),
                               settings=allowed)
    assert TRANSCRIPT in results[-1]["text"]


async def test_a_hosted_run_still_reads_what_its_own_session_archived_and_nothing_of_another(setup):
    results, _, _ = await read(setup, HOSTED, Reading(SOURCES), call("read_source", {"source_id": "own"}),
                               call("read_source", {"source_id": "other"}))
    own, other = results[-2:]
    assert own["text"] == "tool output of this run" and other["error_code"] == "held_back_by_class"


@pytest.mark.parametrize("connection, client", [(HOSTED, "api"), ({}, "copilot")])
async def test_a_hosted_run_lists_held_records_without_their_content_and_says_so(setup, connection, client):
    records = {"items": [
        {"id": "r-chat", "kind": "decision", "status": "proposed", "version": 2, "sensitivity": "conversation",
         "content": "Postgres, said in chat.", "metadata": {"note": "private"}, "source_ids": ["chat"]},
        {"id": "r-doc", "kind": "fact", "status": "proposed", "version": 1, "sensitivity": "project",
         "content": "Amounts are cents.", "source_ids": ["doc"]},
        {"id": "r-none", "kind": "fact", "status": "proposed", "version": 1, "content": "no class"}],
        "total": 3, "next_cursor": None}
    results, provider, _ = await read(setup, connection, Reading(SOURCES, records), call("memory_records"),
                                      client=client)
    shown = results[-1]
    chat, doc, none = shown["items"]
    assert chat == {"id": "r-chat", "kind": "decision", "status": "proposed", "version": 2,
                    "sensitivity": "conversation", "held_back": True}
    assert doc["content"] == "Amounts are cents."
    assert none == {"id": "r-none", "kind": "fact", "status": "proposed", "version": 1, "held_back": True}
    assert shown["held_back"]["records"] == 2 and shown["held_back"]["classes"] == HELD
    assert "memory_remote_classes" in shown["held_back"]["message"] and shown["total"] == 3
    assert "said in chat" not in json.dumps(provider.requests) and "private" not in json.dumps(provider.requests)


async def test_a_local_run_lists_every_record_as_before(setup):
    records = {"items": [{"id": "r", "kind": "decision", "content": "Postgres, said in chat."}]}
    results, _, _ = await read(setup, LOCAL, Reading(SOURCES, records), call("memory_records"))
    assert results[-1] == records


class Writing(Reading):
    """A service that answers a write with the whole stored record, as store._remember does."""

    async def execute(self, name, arguments):
        result = await super().execute(name, arguments)
        if name in ("remember", "checkpoint"):
            return {"id": "r-chat", "kind": "decision", "key": "postgres-chosen-in-chat-with-anna",
                    "active_key": '["p", "decision", "postgres-chosen-in-chat-with-anna"]', "status": "proposed", "version": 3,
                    "content": "Postgres, said in chat.", "metadata": {"note": "private"}, "source_ids": ["doc"]}
        return result


def update_a_held_record(memory):
    """Read and ACK, then update a held record by id without sending content."""
    record = {"id": "r-chat", "expected_version": 2, "status": "proposed"}
    return (call("resume_context"), call("ack_resume", memory.ack_arguments()),
            call("remember", {"record": record, "sources": ["doc"]}))


@pytest.mark.parametrize("connection, client", [(HOSTED, "api"), ({}, "copilot")])
async def test_a_hosted_run_is_not_handed_a_held_records_text_by_updating_it(setup, connection, client):
    memory = Writing(SOURCES)
    results, provider, _ = await read(setup, connection, memory, *update_a_held_record(memory), client=client)
    written = results[-1]
    assert written["id"] == "r-chat" and written["version"] == 3 and written["status"] == "proposed"
    assert not {"content", "metadata", "key", "active_key"} & set(written)
    sent = json.dumps(provider.requests)
    assert "said in chat" not in sent and "private" not in sent and "anna" not in sent


class Compacting(Reading):
    """A checkpoint result that, as store.checkpoint does, holds metadata merged in from an earlier checkpoint."""

    async def execute(self, name, arguments):
        result = await super().execute(name, arguments)
        if name == "checkpoint":
            return {"id": "r-cp", "kind": "checkpoint", "key": "postgres-chosen-in-chat-with-anna", "status": "proposed",
                    "version": 2, "metadata": {"decisions": "we chose Postgres, said in chat", "goal": "Finish"}}
        return result


COMPACT = {"state": {"goal": "Finish", "results": [], "next_step": "Review"}}


def compact_calls(memory):
    return (call("resume_context"), call("ack_resume", memory.ack_arguments()), call("compact_context", COMPACT))


@pytest.mark.parametrize("connection, client", [(HOSTED, "api"), ({}, "copilot")])
async def test_a_hosted_run_that_compacts_its_context_sends_no_checkpoint_metadata_or_key(setup, connection, client):
    memory = Compacting(SOURCES)
    results, provider, agent = await read(setup, connection, memory, *compact_calls(memory), client=client)
    sent = json.dumps(provider.requests) + json.dumps(agent.journal.events())
    assert "said in chat" not in sent and "anna" not in sent
    saved = next(row for row in results if "context_reset_pending" in row)
    assert saved["checkpoint"]["id"] == "r-cp" and "metadata" not in saved["checkpoint"]


async def test_a_local_run_that_compacts_keeps_the_whole_checkpoint_result(setup):
    memory = Compacting(SOURCES)
    results, _, _ = await read(setup, LOCAL, memory, *compact_calls(memory))
    assert next(row for row in results if "context_reset_pending" in row)["checkpoint"]["metadata"]["goal"] == "Finish"


async def test_a_local_run_is_handed_the_whole_written_record(setup):
    memory = Writing(SOURCES)
    results, _, _ = await read(setup, LOCAL, memory, *update_a_held_record(memory))
    assert results[-1]["content"] == "Postgres, said in chat."


def test_the_engine_tells_its_own_memory_client_what_a_hosted_run_must_not_receive(setup):
    settings, run, profile, _ = setup
    for connection, expected in ((HOSTED, HELD), (LOCAL, [])):
        agent = AgentEngine(settings, run, {**profile, **connection}, provider=ScriptedProvider())
        assert agent.memory.held == expected


async def test_the_memory_client_sends_the_held_classes_with_ask_and_prepare(tmp_path):
    import httpx
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"session_id": "session-one", "status": "ready"})

    memory = AgentMemoryClient(Settings(tmp_path / "data"), "p", tmp_path, "session-one",
                               transport=httpx.MockTransport(handler), held=frozenset(HELD))
    await memory.prepare()
    await memory.execute("ask", {"question": "What was decided?"})
    assert [body["held_classes"] for _, body in seen] == [HELD, HELD]


@pytest.fixture
def ledger(tmp_path):
    from cairnmind.store import Store
    store = Store(tmp_path / "data")
    project = store.add_project("ledger", tmp_path)
    chat, _ = chunked(store, project, TRANSCRIPT, "transcript", "conversation://1")
    doc, _ = chunked(store, project, DOCUMENT + " Postgres.", "document", "notes://ledger")
    store.remember(project["id"], {"kind": "decision", "content": "Postgres holds the ledger, said in chat."},
                   [chat["id"]])
    store.remember(project["id"], {"kind": "fact", "content": "Ledger amounts are cents, says the note."},
                   [doc["id"]])
    return store, project, tmp_path / "data"


async def test_ask_for_a_hosted_run_leaves_conversation_out_says_so_and_a_local_run_gets_it(ledger):
    store, project, data = ledger
    question = "Which database holds the ledger Postgres?"
    hosted_model, local_model = FakeModels([answer()]), FakeModels([answer()])
    settings = Settings(data_dir=data)  # the memory models are local: only the hosted run holds classes back
    hosted = await KnowledgeAgent(store, settings, ollama=hosted_model).ask(
        project["id"], question, also_held=frozenset(HELD))
    sent = "\n".join(hosted_model.chat_calls)
    assert "we chose Postgres" not in sent and "said in chat" not in sent and "says the note" in sent
    assert hosted["diagnostics"]["held_back"]["classes"] == HELD
    assert memory_egress.HOSTED_ASK_UNKNOWN in hosted["unknowns"] and hosted["status"] != "complete"
    assert all("we chose Postgres" not in source["quote"] for source in hosted["sources"])
    local = await KnowledgeAgent(store, settings, ollama=local_model).ask(project["id"], question)
    assert "we chose Postgres" in "\n".join(local_model.chat_calls) and "held_back" not in local["diagnostics"]
    assert memory_egress.HOSTED_ASK_UNKNOWN not in local["unknowns"]


async def test_without_models_the_full_text_matches_of_a_hosted_run_leave_conversation_out(ledger):
    store, project, data = ledger
    agent = KnowledgeAgent(store, Settings(data_dir=data, knowledge_agent="disabled"), ollama=Untouched())
    hosted = await text_matches(agent, project["id"], "Postgres ledger", also_held=frozenset(HELD))
    local = await text_matches(agent, project["id"], "Postgres ledger")
    assert "we chose Postgres" not in json.dumps(hosted["matches"]) and hosted["held_back"]["chunks"] >= 1
    assert "we chose Postgres" in json.dumps(local["matches"]) and local["held_back"] is None


def test_the_service_takes_the_held_classes_of_a_run_only_as_known_classes(ledger):
    store, project, data = ledger
    settings = replace(Settings.load(data, create=True), knowledge_agent="disabled")
    settings.save()
    headers = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, ollama=Untouched(), start_worker=False)) as client:
        def ask(**body):
            return client.post("/ask", headers=headers, json={"project": "ledger", "question": "Postgres ledger",
                                                              **body})
        hosted, local, invalid = ask(held_classes=HELD), ask(held_classes=[]), ask(held_classes=["everything"])
    assert "we chose Postgres" not in json.dumps(hosted.json()["full_text_matches"])
    assert hosted.json()["diagnostics"]["held_back"]["classes"] == HELD
    # The reason given is the hosted model's, not a remote model server's, which a hosted run does not have.
    assert memory_egress.HOSTED_ASK_UNKNOWN in hosted.json()["unknowns"]
    assert not any("remote model server" in gap for gap in hosted.json()["unknowns"])
    assert "we chose Postgres" in json.dumps(local.json()["full_text_matches"])
    assert invalid.status_code == 422


def test_a_hosted_sessions_resume_packet_leaves_held_previews_out_and_counts_them(workspace):
    store, project, root, manager = workspace
    chat = store.snapshot(project["id"], "conversation://1", TRANSCRIPT, "transcript")
    doc = store.snapshot(project["id"], "notes://ledger", DOCUMENT, "document")
    store.remember(project["id"], {"kind": "decision", "content": "Postgres holds the ledger, said in chat."},
                   [chat["id"]])
    store.remember(project["id"], {"kind": "constraint", "content": "Ledger amounts are cents, says the note."},
                   [doc["id"]])
    session = uuid4().hex
    hosted = manager.prepare(project["id"], "api", session_id=session, timeout=0, held_classes=HELD)
    plain = manager.prepare(project["id"], "codex", timeout=0)
    packet, everything = hosted["packet"], plain["packet"]
    shown = json.dumps(packet["records"])
    assert "said in chat" not in shown and "says the note" in shown
    assert packet["held_back"]["records"] == 1 and packet["held_back"]["classes"] == HELD
    assert packet["omitted_records"] == 0 and packet["state_hash"] == everything["state_hash"]
    assert "said in chat" in json.dumps(everything["records"]) and "held_back" not in everything
    assert packet["status"] == "ready" and ack(manager, hosted)["status"] == "acknowledged"
    # A packet rebuilt after a state change, by a refresh or by a prepare that names no classes, keeps the hold.
    (root / "app.py").write_text("VALUE = 2\n")  # the code changes, so the stored packet is no longer current
    rebuilt = manager.resume_context(session)
    assert rebuilt["packet"]["id"] != packet["id"] and rebuilt["packet"]["held_back"]["records"] == 1
    assert "said in chat" not in json.dumps(rebuilt["packet"]["records"])
    restarted = manager.prepare(project["id"], "api", session_id=session, timeout=0, reason="resume")
    assert "said in chat" not in json.dumps(restarted["packet"]["records"])
    assert "said in chat" not in json.dumps(manager.status(session)["packet"]["records"])
    allowed = manager.prepare(project["id"], "api", session_id=session, timeout=0, reason="refresh",
                              held_classes=[])
    assert "said in chat" in json.dumps(allowed["packet"]["records"]) and "held_back" not in allowed["packet"]


def test_every_sentence_a_hosted_run_is_told_has_a_polish_translation():
    refusals = [memory_egress.HOSTED_READ_REFUSED.format(name=name) for name in memory_egress.CLASSES]
    for text in (*refusals, memory_egress.HOSTED_RECORDS_HELD, memory_egress.HOSTED_PACKET_NOTE):
        assert AGENT_MESSAGES[text] != text and "memory_remote_classes" in AGENT_MESSAGES[text]
    assert ASK_MESSAGES[memory_egress.HOSTED_ASK_UNKNOWN] != memory_egress.HOSTED_ASK_UNKNOWN
