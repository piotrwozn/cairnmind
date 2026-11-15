"""Only the allowed sensitivity classes go to a memory model server on another computer (#181).

Every model here is a fake: nothing leaves the test. A role is remote when its profile names a
server that is not loopback, exactly as `cairnmind models set --allow-remote` saves it.
"""
from __future__ import annotations

from dataclasses import replace
import json
from uuid import uuid4

import httpx
import pytest

from cairnmind import memory_egress, memory_models
from cairnmind.agent import KnowledgeAgent
from cairnmind.config import Settings
from cairnmind.ollama import OllamaUnavailable
from cairnmind.retrieval import Retriever
from cairnmind.service import schedule_embeddings
from cairnmind.store import Store

REMOTE = {"provider": "openai_compatible", "base_url": "https://models.example/v1", "allow_remote": True}
TRANSCRIPT = "User: we chose Postgres for the ledger.\nAssistant: noted, Postgres for the ledger."
DOCUMENT = "The ledger stores amounts in cents."


class FakeModels:
    """Records every text that would have left; replies in order."""

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.chat_calls, self.embedded = [], []

    async def chat(self, messages, tools=None, **kwargs):
        self.chat_calls.append(json.dumps(messages, ensure_ascii=False))
        if not self.replies:
            raise OllamaUnavailable("no reply installed")
        return {"message": {"role": "assistant", **self.replies.pop(0)}}

    async def embed(self, texts):
        self.embedded.append(list(texts))
        return [[0.0, 1.0, 0.0] for _ in texts]

    async def model_identity(self, name):
        return name + "@fake"

    async def models(self):
        return {"models": []}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = Store(tmp_path / "data")
    project = store.add_project("ledger", root)
    return store, project, tmp_path / "data"


def chunked(store, project, text, kind, uri):
    source = store.snapshot(project["id"], uri, text, kind)
    chunk_id = str(uuid4())
    store.command("INSERT INTO chunks CONTENT :doc", {"doc": {
        "id": chunk_id, "project_id": project["id"], "source_id": source["id"], "text": text,
        "line_start": 1, "line_end": len(text.splitlines()), "metadata": {}, "is_current": True}})
    return source, chunk_id


def embed_job(project, chunk_id):
    return {"project_id": project["id"], "payload": {"chunk_id": chunk_id}}


async def test_a_remote_embedding_server_never_gets_a_transcript_chunk_and_gets_a_document_chunk(workspace):
    store, project, data = workspace
    settings = Settings(data_dir=data, memory_embedding=dict(REMOTE))
    model = FakeModels()
    retriever = Retriever(store, settings, model)
    doc, doc_chunk = chunked(store, project, DOCUMENT, "document", "notes://ledger")
    chat, chat_chunk = chunked(store, project, TRANSCRIPT, "transcript", "conversation://1")
    await retriever.embed_pending(embed_job(project, doc_chunk))
    await retriever.embed_pending(embed_job(project, chat_chunk))
    assert model.embedded == [[DOCUMENT]]
    # Left unembedded, the transcript is still found by text search.
    assert chat["id"] in {hit["source_id"] for hit in retriever.search(project["id"], "Postgres ledger")}


async def test_with_the_local_ollama_every_chunk_is_embedded_as_before(workspace):
    store, project, data = workspace
    model = FakeModels()
    retriever = Retriever(store, Settings(data_dir=data), model)
    _, doc_chunk = chunked(store, project, DOCUMENT, "document", "notes://ledger")
    _, chat_chunk = chunked(store, project, TRANSCRIPT, "transcript", "conversation://1")
    await retriever.embed_pending(embed_job(project, doc_chunk))
    await retriever.embed_pending(embed_job(project, chat_chunk))
    assert model.embedded == [[DOCUMENT], [TRANSCRIPT]]


def held_extraction(store, project, settings, model, source):
    agent = KnowledgeAgent(store, settings, ollama=model)
    job_id = store.enqueue("extract", project["id"], {"source_id": source["id"]}, "extract:" + source["id"])
    job = store.claim_job("inference", kinds=["extract"])
    return agent, job_id, job


async def test_a_remote_chat_server_leaves_a_transcript_unextracted_and_makes_no_call(workspace):
    store, project, data = workspace
    settings = Settings(data_dir=data, memory_chat=dict(REMOTE))
    model = FakeModels()
    source = store.snapshot(project["id"], "conversation://1", TRANSCRIPT, "transcript")
    agent, job_id, job = held_extraction(store, project, settings, model, source)
    await agent.extract(job)
    store.finish_job(job_id, worker=job["worker"], expected_attempts=job["attempts"])
    row = store.query("SELECT state, diagnostics FROM jobs WHERE id=:id", {"id": job_id})[0]
    assert row["state"] == "completed" and row["diagnostics"]["code"] == "remote_not_allowed"
    assert model.chat_calls == []
    assert store.query("SELECT count(*) AS n FROM records WHERE project_id=:p", {"p": project["id"]})[0]["n"] == 0


async def test_adding_conversation_to_the_setting_lets_the_held_work_run_without_reimporting(workspace):
    store, project, data = workspace
    settings = Settings(data_dir=data, memory_chat=dict(REMOTE), memory_embedding=dict(REMOTE))
    source = store.snapshot(project["id"], "conversation://1", TRANSCRIPT, "transcript")
    agent, job_id, job = held_extraction(store, project, settings, FakeModels(), source)
    await agent.extract(job)
    store.finish_job(job_id, worker=job["worker"], expected_attempts=job["attempts"])
    _, doc_chunk = chunked(store, project, DOCUMENT, "document", "notes://ledger")
    _, chat_chunk = chunked(store, project, TRANSCRIPT, "transcript", "conversation://2")
    report = {}
    # The default keeps it held: nothing is released, and the repair never queues the transcript chunk.
    assert memory_egress.release_held_extractions(store, settings) == 0
    assert schedule_embeddings(store, "e@fake", settings=settings, report=report) == 1
    assert report == {"embedding_held_back": 1}
    queued = {row["payload"]["chunk_id"] for row in store.query("SELECT payload FROM jobs WHERE kind='embed'")}
    assert queued == {doc_chunk}
    # The owner adds conversation (config.json, then a restart): the same rows are processed.
    allowed = replace(settings, memory_remote_classes=["project", "conversation"])
    assert memory_egress.release_held_extractions(store, allowed) == 1
    assert store.query("SELECT state FROM jobs WHERE id=:id", {"id": job_id})[0]["state"] == "pending"
    assert schedule_embeddings(store, "e@fake", settings=allowed, report=report) == 2
    assert report == {"embedding_held_back": 0}
    model = FakeModels()
    await Retriever(store, allowed, model).embed_pending(embed_job(project, chat_chunk))
    assert model.embedded == [[TRANSCRIPT]]


def answer(text="The sources say nothing about the database.", unknowns=("No source names the database.",)):
    return {"content": json.dumps({"answer": text, "citations": [], "unknowns": list(unknowns)})}


async def test_ask_leaves_out_transcript_evidence_for_a_remote_chat_server_and_says_so(workspace):
    store, project, data = workspace
    pid = project["id"]
    chat, _ = chunked(store, project, TRANSCRIPT, "transcript", "conversation://1")
    doc, _ = chunked(store, project, DOCUMENT, "document", "notes://ledger")
    store.remember(pid, {"kind": "decision", "content": "Postgres holds the ledger, said in chat."}, [chat["id"]])
    store.remember(pid, {"kind": "fact", "content": "Ledger amounts are cents, says the note."}, [doc["id"]])
    settings = Settings(data_dir=data, memory_chat=dict(REMOTE))
    read_chat = {"content": "", "tool_calls": [{"function": {"name": "read_source",
                                                             "arguments": {"source_id": chat["id"]}}}]}
    model = FakeModels([read_chat, answer()])
    result = await KnowledgeAgent(store, settings, ollama=model).ask(pid, "Which database holds the ledger Postgres?")
    sent = "\n".join(model.chat_calls)
    assert "we chose Postgres" not in sent and "said in chat" not in sent
    assert "says the note" in sent  # project material still goes
    assert "held back" in sent  # the refused read_source says why, to the model as well
    held = result["diagnostics"]["held_back"]
    assert held["classes"] == ["conversation", "third_party"]
    assert held["chunks"] >= 1 and held["records"] >= 1 and held["sources"] >= 1
    assert result["status"] != "complete"
    assert memory_egress.HELD_BACK_UNKNOWN in result["unknowns"]


async def test_the_graph_and_history_tools_of_an_answer_leave_held_records_out(workspace):
    store, project, data = workspace
    pid = project["id"]
    chat = store.snapshot(pid, "conversation://1", TRANSCRIPT, "transcript")
    doc = store.snapshot(pid, "notes://ledger", DOCUMENT, "document")
    held = store.remember(pid, {"kind": "decision", "content": "Postgres ledger, said in chat."}, [chat["id"]])
    kept = store.remember(pid, {"kind": "fact", "content": "Postgres ledger amounts in cents."}, [doc["id"]])
    settings = Settings(data_dir=data, memory_chat=dict(REMOTE))
    agent = KnowledgeAgent(store, settings, ollama=FakeModels())
    open_graph = json.dumps(agent.retriever.graph(pid, "Postgres ledger"))
    assert "said in chat" in open_graph  # outside an answer (a coding client's read, #183) nothing is gated
    gate = memory_egress.ask_gate(settings, store)
    token = memory_egress.ASK.set(gate)
    try:
        graph = json.dumps(agent.retriever.graph(pid, "Postgres ledger"))
        history = await agent._tool("record_history", {"record_id": held["id"]}, pid, {})
        own = await agent._tool("record_history", {"record_id": kept["id"]}, pid, {})
    finally:
        memory_egress.ASK.reset(token)
    assert "said in chat" not in graph and "conversation://1" not in graph and "amounts in cents" in graph
    assert history["records"] == [] and own["records"]
    assert gate.summary()["records"] >= 1


def gated(settings, store, read):
    gate = memory_egress.ask_gate(settings, store)
    token = memory_egress.ASK.set(gate)
    try:
        return gate, read()
    finally:
        memory_egress.ASK.reset(token)


def test_a_chat_record_recited_to_a_project_file_stays_out_of_an_answer(workspace):
    store, project, data = workspace
    pid = project["id"]
    chat = store.snapshot(pid, "conversation://1", TRANSCRIPT, "transcript")
    doc = store.snapshot(pid, "notes://ledger", DOCUMENT, "document")
    record = store.remember(pid, {"kind": "decision", "content": "Postgres holds the ledger, said in chat."},
                            [chat["id"]])
    store.remember(pid, {"id": record["id"], "expected_version": record["version"]}, [doc["id"]])
    settings = Settings(data_dir=data, memory_chat=dict(REMOTE))
    agent = KnowledgeAgent(store, settings, ollama=FakeModels())
    _, context = gated(settings, store, lambda: json.dumps(agent.retriever.context(pid, project["root"])))
    assert "said in chat" not in context
    _, graph = gated(settings, store, lambda: json.dumps(agent.retriever.graph(pid, "Postgres ledger")))
    assert "said in chat" not in graph


def test_the_plan_node_of_an_external_plan_edited_natively_stays_out_of_the_graph(workspace):
    from cairnmind.plans import PlanManager
    store, project, data = workspace
    pid, root = project["id"], project["root"]
    external = {"origin_client": "external", "origin_session_id": "x1", "origin_history": "unarchived",
                "origin_evidence": "degraded"}
    manager = PlanManager(store)
    with store.write_origin(external):
        first = manager.save(pid, {"title": "Zephyrplan migration", "goal": "externalgoaltext quasar"},
                             [{"key": "a", "content": "Step A"}], "one", worktree=root, activate=True)
    # A native save keeps the plan's own mark, but its newest snapshot carries none.
    manager.save(pid, {"id": first["plan"]["id"]},
                 [{"key": "a", "content": "Step A"}, {"key": "b", "content": "Step B"}], "two",
                 expected_version=first["plan"]["version"],
                 task_versions={task["id"]: task["version"] for task in first["tasks"]})
    settings = Settings(data_dir=data, memory_chat=dict(REMOTE))
    agent = KnowledgeAgent(store, settings, ollama=FakeModels())
    assert "externalgoaltext" in json.dumps(agent.retriever.graph(pid, "Zephyrplan", 24, root))  # ungated read
    gate, graph = gated(settings, store, lambda: json.dumps(agent.retriever.graph(pid, "Zephyrplan", 24, root)))
    assert "externalgoaltext" not in graph and "Zephyrplan migration" not in graph
    assert gate.summary()["records"] >= 1


def test_the_repair_looks_up_a_held_chunk_once_and_not_on_every_pass(workspace, monkeypatch):
    store, project, data = workspace
    settings = Settings(data_dir=data, memory_embedding=dict(REMOTE))
    chunked(store, project, TRANSCRIPT, "transcript", "conversation://1")
    known, report, looked_up = {}, {}, []
    original = memory_egress.source_rows
    monkeypatch.setattr(memory_egress, "source_rows", lambda reader, ids: looked_up.append(list(ids))
                        or original(reader, ids))
    for _ in range(3):
        assert schedule_embeddings(store, "e@fake", settings=settings, report=report, known_held=known) == 0
        assert report == {"embedding_held_back": 1}
    assert len(looked_up) == 1
    # Allowed now: the same chunk is queued, whatever an earlier repair remembered.
    allowed = replace(settings, memory_remote_classes=["project", "conversation"])
    assert schedule_embeddings(store, "e@fake", settings=allowed, report=report, known_held=known) == 1
    assert report == {"embedding_held_back": 0}


async def test_ask_with_the_local_ollama_sends_the_same_evidence_as_before(workspace):
    store, project, data = workspace
    pid = project["id"]
    chunked(store, project, TRANSCRIPT, "transcript", "conversation://1")
    model = FakeModels([answer()])
    result = await KnowledgeAgent(store, Settings(data_dir=data), ollama=model).ask(pid, "Which database Postgres?")
    assert "we chose Postgres" in model.chat_calls[0]
    assert "held_back" not in result["diagnostics"]
    assert memory_egress.HELD_BACK_UNKNOWN not in result["unknowns"]


def mock_http():
    def server(request):
        if request.url.host == "models.example":
            return httpx.Response(200, json={"data": [{"id": "qwen3.5:9b"}]})
        return httpx.Response(200, json={"models": []})
    return httpx.Client(transport=httpx.MockTransport(server))


def test_doctor_shows_per_role_where_text_goes_and_which_classes_are_held_back(tmp_path):
    settings = Settings(data_dir=tmp_path, memory_chat=dict(REMOTE))
    with mock_http() as http:
        result = memory_models.doctor_models(settings, http)
    assert result["remote_classes"] == {"setting": ["project"]}
    assert result["chat"]["destination"] == "remote"
    assert result["chat"]["sends"] == ["project"]
    assert result["chat"]["held_back"] == ["conversation", "third_party"]
    assert "conversation" in result["chat"]["content_sent_to"] and "models.example" in result["chat"]["content_sent_to"]
    assert result["embedding"]["destination"] == "this computer"
    assert result["embedding"]["sends"] == ["project", "conversation", "third_party"]
    assert result["embedding"]["held_back"] == []


@pytest.mark.parametrize("value", ["project", ["project", "secret"], [1], ["project", "project"], None])
def test_an_invalid_setting_sends_no_class_and_doctor_says_why(tmp_path, value):
    settings = Settings(data_dir=tmp_path, memory_chat=dict(REMOTE), memory_remote_classes=value)
    with mock_http() as http:
        result = memory_models.doctor_models(settings, http)
    assert result["remote_classes"]["error"]
    assert result["chat"]["sends"] == [] and result["chat"]["held_back"] == ["project", "conversation", "third_party"]
    assert memory_egress.held_back_class(settings, "chat", {"kind": "code", "metadata": {}}) == "project"
    # A local role is not affected by the setting at all.
    assert memory_egress.held_back_class(settings, "embedding", {"kind": "transcript", "metadata": {}}) is None


def test_by_default_only_project_text_goes_to_a_remote_model_server(tmp_path):
    assert Settings(data_dir=tmp_path).memory_remote_classes == ["project"]


def test_the_unknown_an_answer_gets_is_a_fixed_sentence_with_a_polish_translation():
    from cairnmind.terminal_i18n import tr
    from cairnmind.terminal_i18n_messages import fixed_message
    polish = fixed_message(memory_egress.HELD_BACK_UNKNOWN, lambda text: tr(text, "pl"))
    assert polish != memory_egress.HELD_BACK_UNKNOWN and "memory_remote_classes" in polish
