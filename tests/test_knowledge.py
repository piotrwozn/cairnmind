from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from cairnmind.agent import EXCERPT_CHARS, INITIAL_HITS, KnowledgeAgent, SYSTEM, question_excerpt
from cairnmind.config import Settings
from cairnmind.ollama import Ollama, OllamaUnavailable
from cairnmind.retrieval import Retriever, public_record
from cairnmind.store import Store
from cairnmind.support import SUPPORT_SYSTEM


class FakeOllama:
    """Replies in order. Support-check calls are kept apart: they go to support_calls and are
    answered from ``support`` (default: supported), so call counts of the answer stay as they were."""

    def __init__(self, replies=None, vector=None, support=None):
        self.replies = list(replies or [])
        self.vector = vector or [0.0, 1.0, 0.0]
        self.calls = []
        self.support_calls = []
        self.support = support
        self.embedding_calls = []

    async def chat(self, messages, tools=None, **kwargs):
        if messages and messages[0].get("content") == SUPPORT_SYSTEM:
            self.support_calls.append({"messages": list(messages), "tools": tools, "kwargs": kwargs})
            if isinstance(self.support, Exception):
                raise self.support
            reply = self.support or {"content": json.dumps({"supported": True, "reason": "Stated."})}
            return {"message": {"role": "assistant", **reply}, "done_reason": "stop"}
        self.calls.append({"messages": list(messages), "tools": tools, "kwargs": kwargs})
        if not self.replies:
            raise OllamaUnavailable("No local test response installed")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(messages, tools)
        return {"message": {"role": "assistant", **reply}}

    async def embed(self, texts):
        self.embedding_calls.append(texts)
        return [self.vector[:] for _ in texts]

    async def model_identity(self, name):
        return name + "@test-digest"

    async def models(self):
        return {"models": []}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = Store(tmp_path / "data")
    project = store.add_project("example", root)
    settings = Settings(data_dir=tmp_path / "data")
    return store, project, settings


def document(store, project, text, uri="notes://decision", kind="document", metadata=None):
    source = store.snapshot(project["id"], uri, text, kind, metadata=metadata)
    chunk_id = str(uuid4())
    store.command("INSERT INTO chunks CONTENT :doc", {"doc": {
        "id": chunk_id, "project_id": project["id"], "source_id": source["id"], "text": text,
        "line_start": 1, "line_end": len(text.splitlines()), "metadata": {}, "is_current": True,
    }})
    return source, chunk_id


def final(source_id, quote, answer="Odpowiedź oparta na źródle.", unknowns=None):
    return {"content": json.dumps({"answer": answer,
                                  "citations": [{"source_id": source_id, "quote": quote}],
                                  "unknowns": unknowns or []})}


def calls(*items):
    return {"content": "", "tool_calls": [
        {"function": {"name": name, "arguments": args}} for name, args in items
    ]}


def test_lexical_search_is_scoped_current_and_fts_safe(workspace, tmp_path):
    store, project, settings = workspace
    old, _ = document(store, project, "Authentication uses bearer tokens")
    current, _ = document(store, project, "Authentication uses cookies")
    second_root = tmp_path / "other"
    second_root.mkdir()
    other = store.add_project("other", second_root)
    document(store, other, "Authentication private other project", "private://other")
    retriever = Retriever(store, settings, FakeOllama())
    hits = retriever.search(project["id"], 'Authentication " OR 1=1 --')
    assert current["id"] in {h["source_id"] for h in hits}
    assert old["id"] not in {h["source_id"] for h in hits}
    assert all(h["project_id"] == project["id"] for h in hits)


async def test_semantic_search_cross_language_and_model_namespace(workspace):
    store, project, settings = workspace
    source, chunk = document(store, project, "Refresh token expiration policy")
    model = FakeOllama()
    retriever = Retriever(store, settings, model)
    await retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": chunk}})
    assert retriever.search(project["id"], "wygasanie uwierzytelnienia") == []
    hits = await retriever.asearch(project["id"], "wygasanie uwierzytelnienia")
    assert hits[0]["source_id"] == source["id"]
    await retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": chunk}})
    assert len(model.embedding_calls) == 2  # one chunk + one query; duplicate job performs no inference
    assert store.query("SELECT count(*) AS total FROM embeddings")[0]["total"] == 1

    async def changed_identity(name):
        return name + "@other-weights"

    model.model_identity = changed_identity
    assert await retriever.asearch(project["id"], "wygasanie uwierzytelnienia") == []


def test_graph_walk_follows_four_hop_evidence_chain(workspace):
    store, project, settings = workspace
    source, _ = document(store, project, "Evidence for dependency topology")
    ids = [str(uuid4()) for _ in range(5)]
    labels = ["decision-login", "module", "function", "caller", "test"]
    for node_id, label in zip(ids, labels):
        store.upsert_node({"id": node_id, "project_id": project["id"], "kind": "symbol", "label": label,
                           "source_id": source["id"], "metadata": {}})
    for previous, following in zip(ids, ids[1:]):
        store.upsert_edge({"id": str(uuid4()), "project_id": project["id"], "from_id": previous,
                           "to_id": following, "kind": "depends_on", "source_id": source["id"], "status": "confirmed"})
    graph = Retriever(store, settings, FakeOllama()).graph(project["id"], "decision-login")
    assert {n["id"] for n in graph["nodes"]} == set(ids)
    assert len(graph["edges"]) == 4
    assert graph["sources"][0]["id"] == source["id"]


def test_context_exposes_task_state_and_acceptance(workspace):
    store, project, settings = workspace
    source, _ = document(store, project, "Implement login; tests and review required")
    store.remember(project["id"], {"kind": "task", "content": "Login implementation",
        "metadata": {"state": "needs_review", "requires_test": True, "requires_review": True,
                     "acceptance_criteria": ["Cookies are secure"]}}, [source["id"]])
    context = Retriever(store, settings, FakeOllama()).context(project["id"])
    assert context["records"][0]["metadata"]["state"] == "needs_review"
    assert "verification" in context["instruction"] or "verified" in context["instruction"]


async def test_agent_uses_tools_then_returns_validated_citation(workspace):
    store, project, settings = workspace
    quote = "Login uses secure HttpOnly cookies."
    source, _ = document(store, project, quote)
    model = FakeOllama([
        calls(("search", {"query": "Login cookies"}), ("graph", {"query": "Login"})),
        calls(("read_source", {"source_id": source["id"]})),
        final(source["id"], quote),
    ])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "Jak działa Login?")
    assert result["status"] == "complete"
    assert result["sources"][0]["id"] == source["id"]
    assert result["sources"][0]["quote"] == quote
    tools = {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert "ask" not in tools and "shell" not in tools
    assert "UNTRUSTED DATA" in model.calls[0]["messages"][0]["content"]
    assert model.embedding_calls


@pytest.mark.parametrize("citation", [
    {"source_id": "invented-source", "quote": "real evidence"},
    {"source_id": [], "quote": "real evidence"},
    {"quote": "real evidence"},
])
async def test_agent_rejects_unread_or_malformed_citations(workspace, citation):
    store, project, settings = workspace
    document(store, project, "real evidence")
    model = FakeOllama([{"content": json.dumps({"answer": "Unsupported claim",
                        "citations": [citation], "unknowns": []})}])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "real evidence")
    assert result["status"] == "partial"
    assert "Unsupported claim" not in result["answer"]


async def test_agent_rejects_invented_quote_and_plain_prose(workspace):
    store, project, settings = workspace
    source, _ = document(store, project, "real evidence")
    for reply in [final(source["id"], "invented evidence"), {"content": "I am sure this works"}]:
        result = await KnowledgeAgent(store, settings, FakeOllama([reply])).ask(project["id"], "real evidence")
        assert result["status"] == "partial"


@pytest.mark.parametrize("literal", [True, False])
def test_citation_passage_named_quote_excerpt_is_checked_like_quote(workspace, literal):
    # The catalog names its passages quote_excerpt and models answer with that name.
    store, project, settings = workspace
    text = "The retry budget is five attempts per request."
    source = store.snapshot(project["id"], "note://retry", text, "note")
    passage = "retry budget is five attempts" if literal else "retry budget is six attempts"
    output = json.dumps({"answer": "Five attempts.", "unknowns": [],
                         "citations": [{"source_id": source["id"], "quote_excerpt": passage}]})
    result = KnowledgeAgent(store, settings, FakeOllama())._final(
        output, project["id"], {source["id"]: source}, time.monotonic())
    if literal:
        assert result["status"] == "complete" and result["answer"] == "Five attempts."
        assert result["sources"][0]["quote"] == passage
    else:
        assert result["status"] == "partial" and result["diagnostics"]["code"] == "citation_invalid"
        assert "Five attempts." not in result["answer"] and not result["sources"]


SUPPORTED_QUOTE = "The retry budget is five attempts per request."


async def _judged(workspace, support, reply=None, relative_path=None):
    store, project, settings = workspace
    metadata = {"relative_path": relative_path} if relative_path else None
    source, _ = document(store, project, SUPPORTED_QUOTE, uri="notes://retry", metadata=metadata)
    model = FakeOllama([reply or final(source["id"], SUPPORTED_QUOTE, "Five attempts.")], support=support)
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "What is the retry budget?")
    return result, model


async def test_answer_the_support_check_accepts_stays_complete(workspace):
    result, model = await _judged(workspace, None)
    assert result["status"] == "complete" and result["answer"] == "Five attempts."
    assert len(model.calls) == 1 and len(model.support_calls) == 1
    judge = model.support_calls[0]
    assert judge["tools"] is None and set(judge["kwargs"]["format"]["required"]) == {"supported", "reason"}
    payload = json.loads(judge["messages"][1]["content"])
    assert payload["question"] == "What is the retry budget?" and payload["answer"] == "Five attempts."
    assert payload["citations"][0]["quote"] == SUPPORTED_QUOTE
    assert payload["citations"][0]["secondary_file"] is False and payload["citations"][0]["line_start"] == 1
    assert result["diagnostics"]["support_check"] == "supported"
    assert result["diagnostics"]["model_calls"][-1]["phase"] == "support"


async def test_answer_the_support_check_rejects_is_not_complete(workspace):
    verdict = {"content": json.dumps({"supported": False, "reason": "The quote sets the value\nin a test."})}
    result, model = await _judged(workspace, verdict)
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "citation_invalid"
    assert "Five attempts." not in result["answer"]
    assert any("The quote sets the value in a test." in gap for gap in result["unknowns"])
    assert result["sources"][0]["quote"] == SUPPORTED_QUOTE  # verified quotes are kept, not changed
    assert result["diagnostics"]["support_check"] == "unsupported"
    assert result["diagnostics"]["model_calls"][-1]["phase"] == "support"


@pytest.mark.parametrize("support", [
    {"content": "yes, supported"},
    {"content": json.dumps({"supported": "yes", "reason": "Stated."})},
    {"content": json.dumps({"supported": True})},
    OllamaUnavailable("judge down"),
])
async def test_unreadable_or_failed_support_check_counts_as_no(workspace, support):
    result, model = await _judged(workspace, support)
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "citation_invalid"
    assert "Five attempts." not in result["answer"]
    assert any("Nie udało się sprawdzić" in gap for gap in result["unknowns"])
    assert result["diagnostics"]["support_check"] == "failed" and len(model.support_calls) == 1


async def test_support_check_timeout_is_never_complete(workspace):
    store, project, settings = workspace
    source, _ = document(store, project, SUPPORTED_QUOTE, uri="notes://retry")
    settings.ask_timeout = 2

    class SlowJudge(FakeOllama):
        async def chat(self, messages, tools=None, **kwargs):
            if messages[0].get("content") == SUPPORT_SYSTEM:
                await asyncio.sleep(10)
            return await super().chat(messages, tools, **kwargs)

    model = SlowJudge([final(source["id"], SUPPORTED_QUOTE, "Five attempts.")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "What is the retry budget?")
    assert result["status"] != "complete" and result["diagnostics"]["code"] == "timeout"
    assert "Five attempts." not in result["answer"]


@pytest.mark.parametrize("reply", ["unknown", "invalid"])
async def test_unknown_answers_and_failed_validation_never_reach_the_support_check(workspace, reply):
    store, project, settings = workspace
    source, _ = document(store, project, SUPPORTED_QUOTE, uri="notes://retry")
    answer = (final(source["id"], SUPPORTED_QUOTE, "unknown", ["The budget is not stated."]) if reply == "unknown"
              else final(source["id"], "An invented passage.", "Six attempts."))
    model = FakeOllama([answer, answer])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "What is the retry budget?")
    assert result["status"] == "partial" and model.support_calls == []
    assert "support_check" not in result["diagnostics"]


async def test_support_check_is_told_when_a_quote_comes_from_a_test(workspace):
    result, model = await _judged(workspace, None, relative_path="tests/test_retry.py")
    citation = json.loads(model.support_calls[0]["messages"][1]["content"])["citations"][0]
    assert citation["path"] == "tests/test_retry.py" and citation["secondary_file"] is True


@pytest.mark.parametrize("failure", ["json", "record_id", "quote"])
async def test_final_protocol_repairs_once_with_inspected_source_schema(workspace, failure):
    store, project, settings = workspace
    quote = "The current policy is exact and source backed."
    source, chunk_id = document(store, project, quote)
    record = store.remember(project["id"], {"kind": "fact", "content": quote}, [source["id"]])
    bad = ({"content": "Here is an answer, without the required JSON."} if failure == "json" else
           final(record["id"], quote, "Unsupported first candidate") if failure == "record_id" else
           final(source["id"], "A paraphrase absent from the source", "Unsupported first candidate"))
    model = FakeOllama([bad, final(source["id"], quote, "Supported corrected answer")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "current policy exact source")
    assert result["status"] == "complete"
    assert result["answer"] == "Supported corrected answer"
    assert result["sources"][0]["id"] == source["id"]
    assert result["sources"][0]["quote"] == quote
    assert len(model.calls) == 2
    initial = json.loads(model.calls[0]["messages"][1]["content"])
    assert initial["citation_catalog"][0]["source_id"] == source["id"]
    assert quote in initial["citation_catalog"][0]["quote_excerpt"]
    assert "Record IDs, task IDs, plan IDs" in model.calls[0]["messages"][0]["content"]
    repaired = model.calls[1]
    assert repaired["tools"] is None
    schema = repaired["kwargs"]["format"]
    allowed = schema["properties"]["citations"]["items"]["properties"]["source_id"]["enum"]
    assert allowed == [source["id"]]
    assert record["id"] not in allowed and chunk_id not in allowed
    assert schema["additionalProperties"] is False
    assert schema["properties"]["citations"]["maxItems"] == 8


async def test_final_repair_sees_the_excerpt_the_first_call_showed(workspace):
    store, project, settings = workspace
    quote = "The retry budget is five attempts per request."
    text = "\n".join([f"Retry note {index}: background about earlier retry experiments." for index in range(12)]
                     + [quote])
    assert 600 < len(text) < 900 and text.index(quote) > 600
    source, _ = document(store, project, text)
    model = FakeOllama([{"content": "not JSON"}, final(source["id"], quote, "Five attempts.")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "retry budget")
    assert result["status"] == "complete" and len(model.calls) == 2
    first = json.loads(model.calls[0]["messages"][1]["content"])
    shown = next(hit["text"] for hit in first["initial_evidence"] if hit["source_id"] == source["id"])
    assert quote in shown
    # The first call keeps its short catalog; the evidence itself is shown in full.
    assert first["citation_catalog"][0]["excerpt_truncated"] is True
    assert quote not in first["citation_catalog"][0]["quote_excerpt"]
    repair = json.loads(model.calls[1]["messages"][1]["content"])
    entry = next(item for item in repair["citation_catalog"] if item["source_id"] == source["id"])
    assert entry["quote_excerpt"] == shown and entry["excerpt_truncated"] is False


def _filler(count, start=0):
    return [f"    note_{index} = 'background line {index} about unrelated matters'" for index in range(start, start + count)]


def test_question_excerpt_returns_a_short_text_whole():
    text = "line one\nretry budget = 5\nline three"
    assert question_excerpt(text, "retry budget", 1000) == (text, 0, 2)


def test_question_excerpt_takes_whole_lines_that_hold_most_question_words():
    lines = [*_filler(40), "RETRY_BUDGET = 5  # attempts per request", *_filler(40, 40)]
    text = "\n".join(lines)
    excerpt, first, last = question_excerpt(text, "What is the retry budget per request?", 600)
    assert "RETRY_BUDGET = 5" in excerpt and len(excerpt) <= 600
    assert excerpt == "\n".join(lines[first:last + 1]) and excerpt in text
    # The excerpt is centred on the matching line, with context on both sides.
    assert first < 40 < last and abs((40 - first) - (last - 40)) <= 2


def test_question_excerpt_without_matching_words_is_the_beginning():
    lines = _filler(80)
    excerpt, first, last = question_excerpt("\n".join(lines), "nothing here matches", 500)
    assert first == 0 and excerpt == "\n".join(lines[:last + 1]) and len(excerpt) <= 500
    assert len(excerpt) + 1 + len(lines[last + 1]) > 500


def test_question_excerpt_cuts_a_single_long_line_to_its_beginning():
    text = "x" * 3000
    assert question_excerpt(text, "retry", 1000) == ("x" * 1000, 0, 0)


async def test_initial_hit_lines_describe_the_excerpt_not_the_chunk(workspace):
    store, project, settings = workspace
    lines = [*_filler(60), "RETRY_BUDGET = 5  # retry budget per request", *_filler(19, 60)]
    text = "\n".join(lines)
    assert len(text) > EXCERPT_CHARS
    source, _ = document(store, project, text, uri="notes://retry-settings")
    model = FakeOllama([final(source["id"], "RETRY_BUDGET = 5", "Five.")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "What is the retry budget?")
    assert result["status"] == "complete"
    hit = json.loads(model.calls[0]["messages"][1]["content"])["initial_evidence"][0]
    assert "RETRY_BUDGET = 5" in hit["text"] and len(hit["text"]) <= EXCERPT_CHARS
    assert hit["text"] == "\n".join(lines[hit["line_start"] - 1:hit["line_end"]])
    assert hit["line_start"] > 1 and hit["line_end"] < len(lines)


async def test_ten_initial_excerpts_leave_room_for_a_search_round(workspace):
    store, project, settings = workspace
    sources = []
    for index in range(INITIAL_HITS + 2):
        body = [f'    settings["retry_budget_{index}_{line}"] = "five attempts"  # retry budget, "quoted" {line}'
                for line in range(40)]
        sources.append(document(store, project, "\n".join(body), uri=f"notes://retry-{index}")[0])
    quote = 'settings["retry_budget_0_0"] = "five attempts"'
    model = FakeOllama([calls(("search", {"query": "retry budget settings"})), final(sources[0]["id"], quote)])
    # Many quotes: every one of them is escaped twice when the guard measures the messages.
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "What is the retry budget?")
    assert len(model.calls) == 2 and result["diagnostics"]["code"] != "context_budget"
    first = json.loads(model.calls[0]["messages"][1]["content"])
    assert len(first["initial_evidence"]) == INITIAL_HITS
    assert all(len(hit["text"]) <= EXCERPT_CHARS for hit in first["initial_evidence"])
    used = sum(len(json.dumps(message, ensure_ascii=False)) for message in model.calls[1]["messages"])
    assert used <= max(12000, settings.num_ctx * 2)


def test_repair_catalog_keeps_its_budget_with_whole_excerpts(workspace):
    store, project, settings = workspace
    agent = KnowledgeAgent(store, settings, FakeOllama())
    evidence = {}
    for index in range(6):
        text = f"Excerpt {index}: " + "literal evidence line\n" * 70
        source = store.snapshot(project["id"], f"note://budget-{index}", text, "note")
        source["_catalog_excerpt"] = text
        evidence[source["id"]] = source
    catalog = agent._citation_catalog(project["id"], evidence)
    assert catalog and all(not item["excerpt_truncated"] for item in catalog)
    assert sum(len(json.dumps(item, ensure_ascii=False)) + 2 for item in catalog) + 2 <= 6000
    assert len(catalog) < 6


@pytest.mark.parametrize("failure", ["source_id", "quote"])
async def test_final_protocol_repair_still_rejects_invalid_evidence(workspace, failure):
    store, project, settings = workspace
    quote = "Only inspected literal evidence may be cited."
    source, _ = document(store, project, quote)
    invalid = final("invented-source" if failure == "source_id" else source["id"],
                    quote if failure == "source_id" else "Invented quote", "Unsupported repaired claim")
    model = FakeOllama([{"content": "not JSON"}, invalid, final(source["id"], quote)])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "inspected literal evidence")
    assert result["status"] == "partial"
    assert "Unsupported repaired claim" not in result["answer"]
    assert not result["sources"]
    assert len(model.calls) == 2 and len(model.replies) == 1


async def test_final_repair_does_not_import_foreign_project_source(workspace, tmp_path):
    store, project, settings = workspace
    local, _ = document(store, project, "Local visible policy.")
    other = store.add_project("private", tmp_path / "private")
    private, _ = document(store, other, "PRIVATE_FOREIGN_MARKER")
    model = FakeOllama([final(private["id"], "PRIVATE_FOREIGN_MARKER"),
                        final(private["id"], "PRIVATE_FOREIGN_MARKER", "PRIVATE_FOREIGN_MARKER")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "Local visible policy")
    assert result["status"] == "partial"
    assert "PRIVATE_FOREIGN_MARKER" not in json.dumps(result)
    allowed = model.calls[1]["kwargs"]["format"]["properties"]["citations"]["items"]["properties"]["source_id"]["enum"]
    assert allowed == [local["id"]] and private["id"] not in allowed


async def test_citation_catalog_never_expands_other_worktree_record_pointers(workspace, tmp_path):
    from cairnmind.ingest import index_project
    store, project, settings = workspace
    first = Path(project["root"])
    second = tmp_path / "second-worktree"
    second.mkdir()
    store.add_project_root(project["id"], second)
    private_text = "BRANCH_SETTING = 'ONLY_BRANCH_A_BYTES'"
    (first / "branch_private.py").write_text(private_text + "\n")
    public_text = "VISIBLE_BRANCH_B_POLICY = 'current'"
    (second / "policy.py").write_text(public_text + "\n")
    index_project(store, project["id"])
    sources = store.query("SELECT id,uri FROM sources WHERE project_id=:project AND is_current=true",
                          {"project": project["id"]})
    source_a = next(row for row in sources if row["uri"] == str(first / "branch_private.py"))
    source_b = next(row for row in sources if row["uri"] == str(second / "policy.py"))
    record = store.remember(project["id"], {"kind": "fact", "content": "Shared project note referencing earlier code",
                                            "status": "confirmed"}, [source_a["id"]])
    model = FakeOllama([calls(("records", {"query": record["id"]})),
                        {"content": "malformed final JSON"}, final(source_b["id"], public_text)])
    agent = KnowledgeAgent(store, settings, model)
    result = await agent.ask(project["id"], "VISIBLE_BRANCH_B_POLICY", worktree=str(second))
    assert result["status"] == "complete"
    assert len(model.calls) == 3
    assert "ONLY_BRANCH_A_BYTES" not in json.dumps(model.calls)
    allowed = model.calls[2]["kwargs"]["format"]["properties"]["citations"]["items"]["properties"]["source_id"]["enum"]
    assert source_a["id"] not in allowed and source_b["id"] in allowed
    evidence = {}
    await agent._tool("records", {"query": record["id"]}, project["id"], evidence, worktree=str(second))
    assert source_a["id"] in evidence and not evidence[source_a["id"]].get("_historical_requested")
    assert agent._citation_catalog(project["id"], evidence, worktree=str(second)) == []
    # Even an in-scope hydrated pointer is not a model-visible source excerpt.
    assert agent._citation_catalog(project["id"], evidence, worktree=str(first)) == []
    historical = await agent._tool("read_source", {"source_id": source_a["id"], "current": False},
                                   project["id"], evidence, worktree=str(second))
    assert private_text in historical["content"]
    catalog = agent._citation_catalog(project["id"], evidence, worktree=str(second))
    assert len(catalog) == 1 and catalog[0]["source_id"] == source_a["id"]
    assert catalog[0]["historical"] is True and private_text in catalog[0]["quote_excerpt"]


@pytest.mark.parametrize("during_repair", [False, True])
async def test_final_repair_never_accepts_code_changed_during_generation(workspace, during_repair):
    from cairnmind.ingest import index_project
    store, project, settings = workspace
    path = Path(project["root"]) / "policy.py"
    quote = 'POLICY = "original"'
    path.write_text(quote + "\n")
    index_project(store, project["id"])
    source = store.query("SELECT id FROM sources WHERE uri=:uri AND is_current=true", {"uri": str(path)})[0]

    def changed_reply(messages, tools):
        path.write_text('POLICY = "changed"\n')
        return final(source["id"], quote, "The original policy is still current")

    replies = [{"content": "malformed JSON"}, changed_reply] if during_repair else [changed_reply]
    model = FakeOllama(replies)
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "POLICY original")
    assert result["status"] == "partial"
    assert "original policy is still current" not in result["answer"]
    assert len(model.calls) == (2 if during_repair else 1)
    assert any("Cytowany fragment" in message for message in result["unknowns"])


async def test_current_read_clears_earlier_historical_intent_before_final_freshness_check(workspace):
    from cairnmind.ingest import index_project
    store, project, settings = workspace
    path = Path(project["root"]) / "policy.py"
    quote = 'POLICY = "original"'
    path.write_text(quote + "\n")
    index_project(store, project["id"])
    source = store.query("SELECT id FROM sources WHERE uri=:uri AND is_current=true", {"uri": str(path)})[0]

    def changed_reply(messages, tools):
        path.write_text('POLICY = "changed"\n')
        return final(source["id"], quote, "The original policy is still current")

    model = FakeOllama([calls(("read_source", {"source_id": source["id"], "current": False})),
                        calls(("current_code", {"source_id": source["id"]})), changed_reply])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "POLICY original")
    assert result["status"] == "partial"
    assert len(model.calls) == 3  # A freshness race is not a format-repair opportunity.
    assert "original policy is still current" not in result["answer"]
    assert any("Cytowany fragment" in message for message in result["unknowns"])


async def test_final_repair_obeys_total_step_budget(workspace):
    store, project, settings = workspace
    source, _ = document(store, project, "Budgeted source evidence.")
    settings.max_steps = 1
    model = FakeOllama([{"content": "not JSON"}, final(source["id"], "Budgeted source evidence.")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "Budgeted source evidence")
    assert result["status"] == "partial"
    assert len(model.calls) == 1
    assert any("kroków" in gap for gap in result["unknowns"])


async def test_final_repair_compacts_previous_state_within_total_context_budget(workspace, monkeypatch):
    store, project, settings = workspace
    source, _ = document(store, project, "Budgeted source evidence.")
    settings.num_ctx = 1  # The existing minimum budget is 12000 serialized characters.
    model = FakeOllama([{"content": "not JSON"}, final(source["id"], "Budgeted source evidence.")])
    agent = KnowledgeAgent(store, settings, model)
    monkeypatch.setattr(agent.retriever, "context", lambda *args: {"padding": "x" * 6500})
    result = await agent.ask(project["id"], "Budgeted source evidence")
    assert result["status"] == "complete"
    assert len(model.calls) == 2
    repaired = model.calls[1]
    size = (sum(len(json.dumps(message, ensure_ascii=False)) for message in repaired["messages"])
            + len(json.dumps(repaired["kwargs"]["format"], ensure_ascii=False)))
    assert size <= 12000 and repaired["tools"] is None
    assert result["diagnostics"]["repair_outcome"] == "succeeded"


async def test_final_repair_refuses_contract_that_cannot_fit_total_context_budget(workspace):
    store, project, settings = workspace
    source, _ = document(store, project, "Budgeted source evidence.")
    settings.num_ctx = 1
    contract = {"type": "object", "properties": {
        f"field{i}": {"type": "string", "description": "x" * 512} for i in range(9)}}
    model = FakeOllama([{"content": "not JSON"}])
    result = await KnowledgeAgent(store, settings, model).ask(
        project["id"], "Budgeted source evidence", answer_schema=contract)
    assert result["status"] == "partial"
    assert len(model.calls) == 1
    assert result["diagnostics"]["repair_outcome"] == "budget_exhausted"
    assert any("budżetu kontekstu" in gap for gap in result["unknowns"])


async def test_final_repair_outage_preserves_original_partial_result(workspace):
    store, project, settings = workspace
    document(store, project, "Available source evidence.")
    model = FakeOllama([{"content": "not JSON"}, OllamaUnavailable("Repair runtime failed")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "Available source evidence")
    assert result["status"] == "partial"
    assert len(model.calls) == 2
    assert any("Niepoprawny format" in gap for gap in result["unknowns"])
    assert any("podczas poprawy" in gap for gap in result["unknowns"])


async def test_final_repair_timeout_preserves_original_partial_with_total_deadline(workspace):
    store, project, settings = workspace
    document(store, project, "Available source evidence.")
    settings.ask_timeout = 2

    class SlowRepair(FakeOllama):
        async def chat(self, messages, tools=None, **kwargs):
            if self.calls:
                self.calls.append({"messages": list(messages), "tools": tools, "kwargs": kwargs})
                await asyncio.sleep(10)
            return await super().chat(messages, tools, **kwargs)

    model = SlowRepair([{"content": "not JSON"}])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "Available source evidence")
    assert result["status"] == "partial"
    assert len(model.calls) == 2
    assert result["elapsed_seconds"] < 3
    assert any("Niepoprawny format" in gap for gap in result["unknowns"])
    assert any("limicie czasu" in gap for gap in result["unknowns"])


async def test_valid_final_still_uses_one_model_call(workspace):
    store, project, settings = workspace
    source, _ = document(store, project, "A valid response needs no repair.")
    model = FakeOllama([final(source["id"], "A valid response needs no repair.")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "valid response repair")
    assert result["status"] == "complete"
    assert len(model.calls) == 1 and "format" not in model.calls[0]["kwargs"]


def answer_contract(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


@pytest.mark.parametrize("encoded", [False, True])
async def test_typed_answer_retains_public_string_and_uses_one_call(workspace, encoded):
    store, project, settings = workspace
    quote = 'The current revision is {"version":2}.'
    source, _ = document(store, project, quote)
    contract = answer_contract({"version": {"type": "integer"}})
    value = {"version": 2}
    model = FakeOllama([final(source["id"], quote, json.dumps(value) if encoded else value)])
    result = await KnowledgeAgent(store, settings, model).ask(
        project["id"], "Retrieve the current revision", answer_schema=contract)
    assert result["status"] == "complete"
    assert result["answer"] == '{"version":2}'
    assert result["sources"][0]["id"] == source["id"]
    assert len(model.calls) == 1 and "format" not in model.calls[0]["kwargs"]
    prompt = json.loads(model.calls[0]["messages"][1]["content"])
    assert prompt["answer_schema"]["properties"]["version"] == {"type": "integer"}
    assert "Return the value directly" in model.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize("bad_quote", [False, True])
async def test_typed_nested_unknown_repairs_contract_and_citations_together(workspace, bad_quote):
    from cairnmind.answer_contract import prepare_answer_schema

    store, project, settings = workspace
    quote = '{"facts":{"answer":"unknown"},"scope":"No other-project data is available here."}'
    source, _ = document(store, project, quote)
    contract = answer_contract({"answer": {"type": "string"}})
    invalid = {"answer": {"secret_marker": "unknown"}}
    original = final(source["id"], "absent quote" if bad_quote else quote,
                     json.dumps(invalid), ["No other-project value is available."])
    model = FakeOllama([original, final(source["id"], quote, {"answer": "unknown"},
                                      ["No other-project value is available."])])
    result = await KnowledgeAgent(store, settings, model).ask(
        project["id"], "Return scope: no other-project data is available here", answer_schema=contract)
    assert result["status"] == "partial"  # Genuine unknowns remain visible.
    assert result["answer"] == '{"answer":"unknown"}'
    assert result["sources"][0]["id"] == source["id"]
    assert len(model.calls) == 2 and model.calls[1]["tools"] is None
    assert model.calls[1]["kwargs"]["format"]["properties"]["answer"] == prepare_answer_schema(contract)
    feedback = json.loads(model.calls[1]["messages"][-1]["content"])["validation_errors"]
    assert any("expected type string" in error for error in feedback)
    assert any("Cytowany fragment" in error for error in feedback) == bad_quote


async def test_typed_repeated_bad_shape_never_escapes_as_candidate_answer(workspace):
    store, project, settings = workspace
    quote = 'A scoped result is explicitly unknown.'
    source, _ = document(store, project, quote)
    invalid = {"answer": {"secret_marker": "unknown"}}
    bad = final(source["id"], quote, json.dumps(invalid), ["Not available."])
    model = FakeOllama([bad, bad, final(source["id"], quote, {"answer": "unknown"})])
    result = await KnowledgeAgent(store, settings, model).ask(
        project["id"], "Retrieve scoped result", answer_schema=answer_contract({"answer": {"type": "string"}}))
    assert result["status"] == "partial"
    assert "secret_marker" not in result["answer"]
    assert result["answer"] != json.dumps(invalid)
    assert len(model.calls) == 2 and len(model.replies) == 1
    assert any("expected type string" in error for error in result["unknowns"])


async def test_typed_missing_required_fact_is_not_filled_from_contract(workspace):
    store, project, settings = workspace
    quote = "No identifier has been assigned."
    source, _ = document(store, project, quote)
    bad = final(source["id"], quote, {}, ["The identifier is missing."])
    model = FakeOllama([bad, bad])
    result = await KnowledgeAgent(store, settings, model).ask(
        project["id"], "Read the assigned identifier", answer_schema=answer_contract({"id": {"type": "string"}}))
    assert result["status"] == "partial" and result["answer"] != '{}'
    assert "unknown" not in result["answer"]
    repair_schema = model.calls[1]["kwargs"]["format"]["properties"]["answer"]
    assert repair_schema["properties"]["id"] == {"type": "string"}
    assert "default" not in json.dumps(repair_schema) and "const" not in json.dumps(repair_schema)
    assert len(model.calls) == 2


async def test_typed_answer_keeps_foreign_source_guard(workspace, tmp_path):
    store, project, settings = workspace
    visible, _ = document(store, project, "Visible local identifier is public.")
    foreign_project = store.add_project("foreign", tmp_path / "foreign")
    foreign, _ = document(store, foreign_project, "FOREIGN_PRIVATE_VALUE")
    bad = final(foreign["id"], "FOREIGN_PRIVATE_VALUE", {"id": "FOREIGN_PRIVATE_VALUE"})
    model = FakeOllama([bad, bad])
    result = await KnowledgeAgent(store, settings, model).ask(
        project["id"], "Visible local identifier", answer_schema=answer_contract({"id": {"type": "string"}}))
    assert result["status"] == "partial" and "FOREIGN_PRIVATE_VALUE" not in json.dumps(result)
    allowed = model.calls[1]["kwargs"]["format"]["properties"]["citations"]["items"]["properties"]["source_id"]["enum"]
    assert allowed == [visible["id"]] and foreign["id"] not in allowed


async def test_typed_contract_repair_keeps_current_source_freshness_guard(workspace):
    from cairnmind.ingest import index_project

    store, project, settings = workspace
    path = Path(project["root"]) / "policy.py"
    quote = 'REVISION = "original"'
    path.write_text(quote + "\n")
    index_project(store, project["id"])
    source = store.query("SELECT id FROM sources WHERE uri=:uri AND is_current=true", {"uri": str(path)})[0]

    def edit_then_repair(messages, tools):
        path.write_text('REVISION = "changed"\n')
        return final(source["id"], quote, {"revision": "original"})

    model = FakeOllama([final(source["id"], quote, {"revision": {"wrong": "original"}}), edit_then_repair])
    result = await KnowledgeAgent(store, settings, model).ask(
        project["id"], "Read current revision", answer_schema=answer_contract({"revision": {"type": "string"}}))
    assert result["status"] == "partial" and result["answer"] != '{"revision":"original"}'
    assert len(model.calls) == 2
    assert any("Cytowany fragment" in error for error in result["unknowns"])


async def test_typed_contract_error_keeps_total_step_budget(workspace):
    store, project, settings = workspace
    source, _ = document(store, project, "The revision is two.")
    settings.max_steps = 1
    model = FakeOllama([final(source["id"], "The revision is two.", {"version": "wrong type"}),
                        final(source["id"], "The revision is two.", {"version": 2})])
    result = await KnowledgeAgent(store, settings, model).ask(
        project["id"], "Read revision", answer_schema=answer_contract({"version": {"type": "integer"}}))
    assert result["status"] == "partial" and len(model.calls) == 1
    assert "wrong type" not in result["answer"]


@pytest.mark.parametrize("oversized", [False, True])
async def test_typed_answer_json_is_never_silently_truncated(workspace, oversized):
    store, project, settings = workspace
    settings.answer_tokens = 20  # 100 serialized answer characters.
    value = {"value": "x" * (200 if oversized else 81)}
    quote = json.dumps(value, separators=(",", ":"))
    source, _ = document(store, project, quote)
    reply = final(source["id"], quote, value)
    model = FakeOllama([reply, reply])
    result = await KnowledgeAgent(store, settings, model).ask(
        project["id"], "Read bounded value", answer_schema=answer_contract({"value": {"type": "string"}}))
    if oversized:
        assert result["status"] == "partial" and result["answer"] != quote[:100]
        assert any("budżet" in error for error in result["unknowns"])
    else:
        assert result["status"] == "complete" and json.loads(result["answer"]) == value
        assert len(model.calls) == 1


async def test_invalid_answer_schema_is_rejected_before_inference(workspace):
    store, project, settings = workspace
    model = FakeOllama()
    with pytest.raises(ValueError, match="answer_schema"):
        await KnowledgeAgent(store, settings, model).ask(project["id"], "Read a fact",
            answer_schema={"$ref": "https://invalid.example/schema"})
    assert not model.calls and not model.embedding_calls


@pytest.mark.parametrize(("schema", "value"), [
    ({"type": "array", "items": {"type": "string"}}, ["alpha", "beta"]),
    ({"type": "string"}, "A literal string"),
    ({"type": "string"}, '{"a":"string that resembles JSON"}'),
    ({"type": "string"}, '"quoted JSON string"'),
    ({"type": ["string", "null"]}, "null"),
    ({"type": ["string", "integer"]}, "1"),
    ({"type": ["string", "boolean"]}, "false"),
    ({"type": "integer"}, 2),
    ({"type": "number"}, 2.5),
    ({"type": "boolean"}, False),
    ({"type": "null"}, None),
])
def test_typed_root_values_are_returned_as_encoded_public_strings(workspace, schema, value):
    store, project, settings = workspace
    quote = "A source-backed typed result."
    source, _ = document(store, project, quote)
    agent = KnowledgeAgent(store, settings, FakeOllama())
    evidence = {}
    agent._source(source["id"], project["id"], evidence, current=False)
    reply = final(source["id"], quote, value)
    result = agent._final(reply["content"], project["id"], evidence, time.monotonic(), answer_schema=schema)
    assert result["status"] == "complete"
    assert isinstance(result["answer"], str) and json.loads(result["answer"]) == value


async def test_model_outage_returns_unavailable_with_retrieved_evidence(workspace):
    store, project, settings = workspace
    source, _ = document(store, project, "Authentication settings")
    model = FakeOllama([OllamaUnavailable("Model is not installed")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "Authentication")
    assert result["status"] == "unavailable"
    assert source["id"] in {s["id"] for s in result["sources"]}
    assert any("not installed" in gap for gap in result["unknowns"])


async def test_total_agent_deadline_is_bounded(workspace):
    store, project, settings = workspace
    document(store, project, "Authentication settings")
    settings.ask_timeout = 0.01
    model = FakeOllama()

    async def slow_chat(*args, **kwargs):
        await asyncio.sleep(2)

    model.chat = slow_chat
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "Authentication")
    # A real HTTP database may consume the tiny budget before any evidence arrives.
    assert result["status"] in {"partial", "unavailable"}
    assert result["elapsed_seconds"] < 0.5


async def test_project_scope_blocks_source_read_from_other_project(workspace, tmp_path):
    store, project, settings = workspace
    other = store.add_project("other", tmp_path / "other")
    secret, _ = document(store, other, "private project evidence")
    model = FakeOllama([
        calls(("read_source", {"source_id": secret["id"]})),
        final(secret["id"], "private project evidence"),
    ])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "Find private project evidence")
    assert result["status"] == "partial"
    assert not result["sources"]
    assert "outside this project" in model.calls[1]["messages"][-1]["content"]


async def test_current_code_is_refreshed_before_answer(workspace):
    from cairnmind.ingest import index_project

    store, project, settings = workspace
    path = Path(project["root"]) / "auth.py"
    path.write_text('POLICY = "old-cookie"\n')
    index_project(store, project["id"])
    path.write_text('POLICY = "secure-cookie"\n')

    def reply(messages, tools):
        current = store.query("SELECT id FROM sources WHERE uri=:uri AND is_current=true", {"uri": str(path)})[0]
        return final(current["id"], 'POLICY = "secure-cookie"')

    result = await KnowledgeAgent(store, settings, FakeOllama([reply])).ask(project["id"], "POLICY cookie")
    assert result["status"] == "complete"
    assert result["sources"][0]["quote"] == 'POLICY = "secure-cookie"'


async def test_extraction_requires_quote_and_keeps_records_proposed(workspace):
    store, project, settings = workspace
    text = "We decided to use auth.py.\nImplement secure cookies."
    source = store.snapshot(project["id"], "transcript://example", json.dumps({"text": text}),
                            "transcript", metadata={"normalized_text": text, "role": "user"})
    model = FakeOllama([{"content": json.dumps({"records": [
        {"kind": "decision", "key": "auth", "content": "Use auth.py", "evidence": "We decided to use auth.py."},
        {"kind": "fact", "key": "bad", "content": "Unsupported fact", "evidence": "No such text"},
    ]})}])
    agent = KnowledgeAgent(store, settings, model)
    await agent.extract({"project_id": project["id"], "payload": {"source_id": source["id"]}})
    records = store.list_records(project["id"])
    assert len(records) == 1
    assert records[0]["status"] == "proposed"
    assert records[0]["metadata"]["evidence"] == "We decided to use auth.py."


async def test_transcript_citations_use_normalized_text_without_losing_raw(workspace):
    store, project, settings = workspace
    quote = "Decision one.\nDecision two."
    source = store.snapshot(project["id"], "transcript://normalized", json.dumps({"text": quote}),
                            "transcript", metadata={"normalized_text": quote})
    model = FakeOllama([calls(("read_source", {"source_id": source["id"]})), final(source["id"], quote)])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "Decision")
    assert result["status"] == "complete"
    assert result["sources"][0]["line_end"] == 2
    assert store.get_source(source["id"])["content"] == json.dumps({"text": quote})


async def test_ollama_http_protocol_and_invalid_vectors(workspace):
    _, _, settings = workspace
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            assert body["think"] is False and body["stream"] is False
            assert body["options"]["num_ctx"] == 16384
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "{}"}})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": settings.embedding_model, "digest": "abc123"}]})
        return httpx.Response(200, json={"embeddings": [[0.0, 0.0]]})

    async with httpx.AsyncClient(base_url=settings.ollama_url, transport=httpx.MockTransport(respond)) as client:
        ollama = Ollama(settings, client)
        await ollama.chat([{"role": "user", "content": "hello"}])
        assert await ollama.model_identity(settings.embedding_model) == settings.embedding_model + "@abc123"
        with pytest.raises(OllamaUnavailable, match="zero vector"):
            await ollama.embed(["hello"])
    assert len(seen) == 3


def test_ollama_refuses_remote_provider(workspace):
    _, _, settings = workspace
    settings.ollama_url = "https://remote.example"
    with pytest.raises(ValueError, match="loopback"):
        Ollama(settings)


async def test_records_pagination_and_state_filters_include_last_plan_step(workspace):
    store, project, settings = workspace
    source, _ = document(store, project, "The plan consists of 26 steps.")
    for step in range(26):
        store.remember(project["id"], {"kind": "task", "key": f"step-{step}",
            "content": f"Step {step}: " + "acceptance details " * 20,
            "metadata": {"state": "needs_review" if step == 25 else "todo", "step_index": step,
                         "plan_id": "migration"}}, [source["id"]])
    agent = KnowledgeAgent(store, settings, FakeOllama())
    evidence = {}
    page = await agent._tool("records", {"kind": "task", "limit": 20}, project["id"], evidence)
    tail = await agent._tool("records", {"kind": "task", "offset": 20}, project["id"], evidence)
    assert page["total"] == 26 and page["has_more"] is True
    assert tail["has_more"] is False and len(tail["records"]) == 6
    assert len({r["id"] for r in page["records"] + tail["records"]}) == 26
    filtered = await agent._tool("records", {"kind": "task", "state": "needs_review"}, project["id"], evidence)
    assert filtered["total"] == 1
    assert filtered["records"][0]["metadata"]["step_index"] == 25
    by_id = await agent._tool("records", {"query": filtered["records"][0]["id"]}, project["id"], evidence)
    assert by_id["total"] == 1
    context = agent.retriever.context(project["id"])
    assert context["total_records"] == 26 and context["omitted_records"] > 0


async def test_record_history_exposes_previous_task_state_and_rejects_other_project(workspace, tmp_path):
    store, project, settings = workspace
    source, _ = document(store, project, "Implement cookies; then verify tests.")
    task = store.remember(project["id"], {"kind": "task", "content": "Cookie work",
                                         "metadata": {"state": "in_progress"}}, [source["id"]])
    store.remember(project["id"], {"id": task["id"], "expected_version": task["version"],
                                  "metadata": {"state": "needs_test"}}, [source["id"]])
    agent = KnowledgeAgent(store, settings, FakeOllama())
    history = await agent._tool("record_history", {"record_id": task["id"]}, project["id"], {})
    assert [r["metadata"]["state"] for r in history["records"]] == ["in_progress", "needs_test"]
    assert history["total"] == 2 and history["has_more"] is False
    other = store.add_project("foreign", tmp_path / "foreign")
    with pytest.raises(ValueError, match="does not exist in this project"):
        await agent._tool("record_history", {"record_id": task["id"]}, other["id"], {})


def test_graph_reports_node_budget_truncation(workspace):
    store, project, settings = workspace
    source, _ = document(store, project, "Topology evidence")
    for index in range(7):
        store.upsert_node({"id": f"node-{index}", "project_id": project["id"], "kind": "module",
                           "label": f"module-{index}", "source_id": source["id"], "metadata": {}})
    for index in range(6):
        store.upsert_edge({"id": f"edge-{index}", "project_id": project["id"], "from_id": f"node-{index}",
                           "to_id": f"node-{index+1}", "kind": "imports", "source_id": source["id"], "status": "observed"})
    graph = Retriever(store, settings, FakeOllama()).graph(project["id"], "node-0", limit=2)
    assert graph["truncated"] is True and graph["node_budget"] == 2
    assert len(graph["nodes"]) <= 2


async def test_missing_key_and_existing_key_conflict_are_preserved_as_proposals(workspace):
    store, project, settings = workspace
    previous, _ = document(store, project, "Use bearer tokens.", "decision://old")
    original = store.remember(project["id"], {"kind": "decision", "key": "authentication",
                                             "content": "Bearer tokens", "status": "confirmed"}, [previous["id"]])
    source, _ = document(store, project, "Use secure cookies. Also create tests.", "decision://new")
    response = {"content": json.dumps({"records": [
        {"kind": "decision", "key": "authentication", "content": "Use secure cookies", "evidence": "Use secure cookies."},
        {"kind": "task", "content": "Create tests", "evidence": "Also create tests.",
         "metadata": {"state": "done", "requires_test": True}},
    ]})}
    agent = KnowledgeAgent(store, settings, FakeOllama([response]))
    await agent.extract({"project_id": project["id"], "payload": {"source_id": source["id"]}})
    records = store.list_records(project["id"])
    assert len(records) == 3
    current = next(r for r in records if r["id"] == original["id"])
    assert current["status"] == "confirmed" and current["content"] == "Bearer tokens"
    conflict = next(r for r in records if r["content"] == "Use secure cookies")
    assert conflict["status"] == "proposed"
    assert conflict["metadata"]["possible_conflicts"] == [original["id"]]
    task = next(r for r in records if r["kind"] == "task")
    assert task["metadata"]["state"] == "needs_test" and task["key"]
    node = store.query("SELECT FROM nodes WHERE id=:id", {"id": f"record:{task['id']}"})[0]
    assert node["metadata"]["state"] == "needs_test"
    assert store.query("SELECT count(*) AS total FROM edges WHERE from_id=:id AND kind='supported_by'",
                       {"id": f"record:{task['id']}"})[0]["total"] == 1


async def test_model_digest_change_reembeds_but_old_sources_are_skipped(workspace):
    store, project, settings = workspace
    _, old_chunk = document(store, project, "Old source text", "file://versioned")
    _, new_chunk = document(store, project, "New source text", "file://versioned")
    model = FakeOllama()
    retriever = Retriever(store, settings, model)
    await retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": old_chunk}})
    assert not model.embedding_calls
    await retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": new_chunk}})
    assert len(model.embedding_calls) == 1

    async def changed_identity(name):
        return name + "@new-digest"

    model.model_identity = changed_identity
    await retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": new_chunk}})
    assert len(model.embedding_calls) == 2
    assert store.query("SELECT count(*) AS total FROM embeddings WHERE chunk_id=:id",
                       {"id": new_chunk})[0]["total"] == 2


async def test_crlf_code_quote_uses_text_view_and_preserves_raw_snapshot(workspace):
    from cairnmind.ingest import index_project

    store, project, settings = workspace
    path = Path(project["root"]) / "windows.py"
    raw = b'FIRST = "first"\r\nSECOND = "second"\r\n'
    path.write_bytes(raw)
    index_project(store, project["id"])
    source = store.query("SELECT FROM sources WHERE uri=:uri AND is_current=true", {"uri": str(path)})[0]
    quote = 'FIRST = "first"\nSECOND = "second"'
    model = FakeOllama([final(source["id"], quote)])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "FIRST SECOND")
    assert result["status"] == "complete"
    assert result["sources"][0]["view"] == "normalized_text"
    assert store.get_source(source["id"])["content"].encode() == raw


async def test_missing_current_code_is_not_reported_complete_but_history_is_readable(workspace):
    from cairnmind.ingest import index_project

    store, project, settings = workspace
    path = Path(project["root"]) / "removed.py"
    quote = 'LEGACY_AUTH = "cookie"'
    path.write_text(quote + "\n")
    index_project(store, project["id"])
    source_id = store.query("SELECT id FROM sources WHERE uri=:uri AND is_current=true", {"uri": str(path)})[0]["id"]
    path.unlink()
    result = await KnowledgeAgent(store, settings, FakeOllama([final(source_id, quote)])).ask(project["id"], "LEGACY_AUTH")
    assert result["status"] != "complete"
    assert result["freshness"]["warnings"]
    historical = FakeOllama([calls(("read_source", {"source_id": source_id, "current": False})), final(source_id, quote)])
    history_result = await KnowledgeAgent(store, settings, historical).ask(project["id"], "Jak wyglądał dawny plik?")
    assert history_result["status"] == "complete"
    assert history_result["sources"][0]["is_current"] is False


async def test_large_normalized_source_metadata_never_echoes_into_read_or_answer(workspace):
    store, project, settings = workspace
    normalized = "Evidence line one.\n" + "x" * 100_000 + "\nLast evidence."
    raw = json.dumps({"original_wrapper": normalized})
    metadata = {"normalized_text": normalized, "analysis": {"calls": ["large-call"] * 30_000},
                "role": "user", "client": "claude", "session_id": "session"}
    source = store.snapshot(project["id"], "message://large-source", raw, "message", metadata=metadata)
    model = FakeOllama([calls(("read_source", {"source_id": source["id"]})),
                        final(source["id"], "Evidence line one.")])
    agent = KnowledgeAgent(store, settings, model)
    result = await agent.ask(project["id"], "What does the evidence say?")
    assert result["status"] == "complete"
    assert "normalized_text" not in result["sources"][0]["metadata"]
    assert "analysis" not in result["sources"][0]["metadata"]
    assert len(json.dumps(result)) < 5000
    read = json.loads(model.calls[1]["messages"][-1]["content"])
    assert len(read["content"]) <= 4000 and read["has_more"] is True
    assert len(json.dumps(read)) < 6000
    assert "normalized_text" not in read["metadata"] and "analysis" not in read["metadata"]
    archived = store.get_source(source["id"])
    assert archived["content"] == raw and archived["metadata"] == metadata


async def test_task_metadata_is_bounded_with_explicit_totals(workspace):
    store, project, settings = workspace
    source, _ = document(store, project, "Acceptance source.")
    criteria = [f"Criterion {i}: " + "requirement " * 100 for i in range(50)]
    record = store.remember(project["id"], {"kind": "task", "key": "large-task", "content": "Task details " * 100,
        "metadata": {"state": "needs_test", "requires_test": True, "acceptance_criteria": criteria,
                     "evidence": "raw evidence " * 20_000, "arbitrary_payload": {"data": "x" * 100_000}}},
        [source["id"]])
    agent = KnowledgeAgent(store, settings, FakeOllama())
    context = agent.retriever.context(project["id"])
    assert len(json.dumps(context)) < 8500
    shown = context["records"][0]
    assert shown["content_truncated"] is True
    assert shown["metadata"]["state"] == "needs_test"
    assert shown["metadata"]["acceptance_criteria_total"] == 50
    assert shown["metadata"]["truncated"] is True
    assert "arbitrary_payload" not in shown["metadata"]
    assert len(json.dumps(shown["metadata"], ensure_ascii=False)) <= 800
    page = await agent._tool("records", {"query": record["id"]}, project["id"], {})
    history = await agent._tool("record_history", {"record_id": record["id"]}, project["id"], {})
    for output in (page, history):
        item = output["records"][0]
        assert item["metadata"]["acceptance_criteria_total"] == 50
        assert len(json.dumps(output)) < 2500
    assert store.list_records(project["id"])[0]["metadata"]["acceptance_criteria"] == criteria


def test_final_citations_have_fixed_count_and_excerpt_budget(workspace):
    store, project, settings = workspace
    evidence, citations = {}, []
    quote = "A literal supporting evidence passage. " * 30
    for index in range(9):
        source = store.snapshot(project["id"], f"note://{index}", quote, "note",
                                metadata={"analysis": "x" * 100_000, "role": "user"})
        evidence[source["id"]] = source
        citations.append({"source_id": source["id"], "quote": quote})
    text = json.dumps({"answer": "Bounded supported answer.", "citations": citations, "unknowns": []})
    result = KnowledgeAgent(store, settings, FakeOllama())._final(text, project["id"], evidence, time.monotonic())
    assert len(result["sources"]) == 8
    assert sum(len(s["quote"]) for s in result["sources"]) <= 6000
    assert all("analysis" not in s["metadata"] for s in result["sources"])
    assert len(result["freshness"]["source_hashes"]) <= 8


async def test_native_ann_source_replacement_and_project_isolation(workspace, tmp_path):
    store, project, settings = workspace
    old, old_chunk = document(store, project, "Old authentication", "file://native-ann")
    model = FakeOllama()
    retriever = Retriever(store, settings, model)
    await retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": old_chunk}})
    assert (await retriever.asearch(project["id"], "uwierzytelnianie"))[0]["source_id"] == old["id"]
    current, new_chunk = document(store, project, "New authentication", "file://native-ann")
    registry = store.query("SELECT FROM vector_indexes WHERE project_id=:project", {"project": project["id"]})[0]
    # Invalidation removes old documents from the actual native HNSW index type.
    assert store.query(f"SELECT count(*) AS total FROM {registry['type_name']}")[0]["total"] == 0
    await retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": new_chunk}})
    other = store.add_project("foreign-ann", tmp_path / "foreign-ann")
    _, foreign_chunk = document(store, other, "Private authentication", "file://foreign-ann")
    await retriever.embed_pending({"project_id": other["id"], "payload": {"chunk_id": foreign_chunk}})
    hits = await retriever.asearch(project["id"], "uwierzytelnianie")
    assert hits and {hit["source_id"] for hit in hits} == {current["id"]}
    assert {hit["project_id"] for hit in hits} == {project["id"]}


def test_native_traversal_does_not_follow_inactive_edge(workspace):
    store, project, settings = workspace
    stable, _ = document(store, project, "Graph evidence", "graph://stable")
    edge_source, _ = document(store, project, "Previous import relation", "graph://changed", kind="code")
    for node_id, label in (("entry-node", "entry-node"), ("unreachable", "other-module")):
        store.upsert_node({"id": node_id, "project_id": project["id"], "kind": "module", "label": label,
                           "source_id": stable["id"], "metadata": {}})
    store.upsert_edge({"id": "changing-edge", "project_id": project["id"], "from_id": "entry-node",
                       "to_id": "unreachable", "kind": "imports", "source_id": edge_source["id"], "status": "observed"})
    store.snapshot(project["id"], "graph://changed", "Import removed", "code")
    native_edge = store.query("SELECT FROM edges WHERE id=:id", {"id": "changing-edge"})[0]
    assert native_edge["is_current"] is False and native_edge["@cat"] == "e"
    graph = Retriever(store, settings, FakeOllama()).graph(project["id"], "entry-node")
    assert {node["id"] for node in graph["nodes"]} == {"entry-node"}
    assert graph["edges"] == []


async def test_embedding_cancels_when_project_is_forgotten_during_inference(workspace, tmp_path):
    store, project, settings = workspace
    _, chunk = document(store, project, "Cancel this pending embedding.")
    model = FakeOllama()

    async def forget_before_reply(texts):
        store.forget_project(project["id"])
        return [[0.0, 1.0, 0.0] for _ in texts]

    model.embed = forget_before_reply
    await Retriever(store, settings, model).embed_pending(
        {"project_id": project["id"], "payload": {"chunk_id": chunk}},
    )
    for collection in ("projects", "embeddings", "vector_indexes"):
        assert store.query(f"SELECT count(*) AS total FROM {collection}")[0]["total"] == 0
    payload = store.export()
    assert payload["projects"] == [] and payload["vector_indexes"] == []
    backup = store.backup(tmp_path / "cancelled.devbrain.json.gz")
    assert Store._validated_backup(backup)["projects"] == []


def test_registry_publication_cancels_when_project_is_forgotten_during_ddl(workspace, tmp_path, monkeypatch):
    store, project, settings = workspace
    retriever = Retriever(store, settings, FakeOllama())
    original_command = store.command
    forgotten = False

    def race_after_ddl(sql, params=None):
        nonlocal forgotten
        result = original_command(sql, params)
        if not forgotten and sql.startswith("CREATE DOCUMENT TYPE vector_"):
            forgotten = True
            store.forget_project(project["id"])
        return result

    monkeypatch.setattr(store, "command", race_after_ddl)
    assert retriever._ensure_vector_index(project["id"], "model@digest", 3) is None
    assert forgotten is True
    assert store.query("SELECT FROM vector_indexes") == []
    assert store.query("SELECT FROM embeddings") == []
    backup = store.backup(tmp_path / "ddl-cancelled.devbrain.json.gz")
    assert Store._validated_backup(backup)["projects"] == []


def check_profile(root):
    path = root / ".devbrain" / "checks.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text("[checks.unit]\nargv = " + json.dumps(
        [sys.executable, "-c", "print('Actual local check output')"]) + '\ncwd = "."\n')


def git_fixture(root):
    for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                 ("config", "user.email", "fixture@example.invalid")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "app.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "Fixture"], check=True, capture_output=True)


async def test_reported_test_flags_and_done_progress_are_never_fresh_machine_proof(workspace):
    store, project, settings = workspace
    check_profile(Path(project["root"]))
    quote = "The developer reported that tests passed."
    source, _ = document(store, project, quote)
    task = store.remember(project["id"], {"kind": "task", "content": "Legacy tests",
        "metadata": {"state": "needs_test", "requires_test": True, "required_profiles": ["unit"],
                     "test_passed": True, "test_source_ids": [source["id"]]}}, [source["id"]])
    model = FakeOllama([calls(("records", {"query": task["id"]}), ("check_status", {"task_id": task["id"]})),
                        final(source["id"], quote, "To zgłoszony wynik. Brakuje aktualnego sprawdzenia maszynowego.")])
    agent = KnowledgeAgent(store, settings, model)
    result = await agent.ask(project["id"], "Czy testy są aktualnie zweryfikowane?")
    assert result["status"] == "complete"
    outputs = [json.loads(message["content"]) for message in model.calls[1]["messages"] if message["role"] == "tool"]
    assert outputs[0]["records"][0]["test_evidence_kind"] == "reported"
    assert outputs[1]["status"] == "reported" and outputs[1]["run_ids"] == []
    assert outputs[1]["reported_test_evidence"] is True
    assert "never\nfresh machine proof" in SYSTEM
    advertised = {item["function"]["name"] for item in model.calls[0]["tools"]}
    assert {"next_task", "check_status"} <= advertised
    assert not advertised.intersection({"run_check", "shell", "save_plan", "remember"})
    with pytest.raises(ValueError, match="not permitted"):
        await agent._tool("run_check", {"task_id": task["id"]}, project["id"], {})
    assert store.query("SELECT id FROM check_runs") == []


async def test_check_status_reads_real_run_sources_and_current_worktree_scope(workspace, tmp_path):
    from cairnmind.verification import run_check

    store, project, settings = workspace
    root = Path(project["root"])
    git_fixture(root)
    check_profile(root)
    source = store.snapshot(project["id"], "task://actual", "Verify the actual code", "manual")
    task = store.remember(project["id"], {"kind": "task", "content": "Actual checks",
        "metadata": {"state": "needs_test", "requires_test": True, "required_profiles": ["unit"]}}, [source["id"]])
    run = run_check(store, settings, project["id"], "unit", task["id"])
    agent, evidence = KnowledgeAgent(store, settings, FakeOllama()), {}
    result = await agent._tool("check_status", {"task_id": task["id"]}, project["id"], evidence)
    assert result["status"] == "fresh" and result["run_ids"] == [run["id"]]
    assert result["runs"][0]["outcome"] == "completed"
    assert result["runs"][0]["code_after"]["fingerprint"] == run["code_after"]["fingerprint"]
    assert set(run["source_ids"].values()) <= set(evidence)
    assert {s["id"] for s in result["sources"]} == set(run["source_ids"].values())
    assert len(json.dumps(result)) < 5000
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    git_fixture(alternate)
    check_profile(alternate)
    store.add_project_root(project["id"], alternate)
    other_scope = await agent._tool("check_status", {"task_id": task["id"]}, project["id"], {}, alternate)
    assert other_scope["status"] != "fresh" and other_scope["runs"] == []
    (root / "app.py").write_text("value = 2\n")
    output_id = run["source_ids"]["stdout"]
    raced = agent._final(json.dumps({"answer": "Current machine proof.", "citations": [
        {"source_id": output_id, "quote": "Actual local check output"}], "unknowns": []}),
        project["id"], evidence, time.monotonic())
    assert raced["status"] == "partial" and "Current machine proof" not in raced["answer"]
    assert (await agent._tool("check_status", {"task_id": task["id"]}, project["id"], {}))["status"] == "stale"
    other = store.add_project("private", tmp_path / "private")
    with pytest.raises(ValueError, match="does not exist in this project"):
        await agent._tool("check_status", {"task_id": task["id"]}, other["id"], {})
    assert len(store.query("SELECT id FROM check_runs")) == 1


async def test_workflow_projects_managed_metadata_with_explicit_bounds(workspace):
    from cairnmind.plans import PlanManager

    store, project, settings = workspace
    saved = PlanManager(store).save(project["id"], {"title": "Large plan", "goal": "Goal " * 2000,
        "constraints": [f"Constraint {i}: " + "long " * 500 for i in range(20)]},
        [{"key": f"step-{i}", "content": f"Task {i}: " + "detail " * 200,
          "metadata": {"requires_test": True, "required_profiles": ["unit"]}} for i in range(10)],
        "large", activate=True)
    agent = KnowledgeAgent(store, settings, FakeOllama())
    workflow = await agent._tool("next_task", {}, project["id"], {})
    assert workflow["plan"]["id"] == saved["plan"]["id"]
    assert workflow["plan"]["constraints_total"] == 20
    assert workflow["plan"]["truncated"] is True
    assert "tasks" not in workflow and "step_ids_total" in workflow["plan"]
    assert workflow["next_task"]["id"] == saved["tasks"][0]["id"]
    metadata = workflow["next_task"]["metadata"]
    assert metadata["plan_ref"] == saved["plan"]["id"] and metadata["step_key"] == "step-0"
    assert metadata["plan_context_hash"] == saved["plan"]["context_hash"]
    assert metadata["required_profiles"] == ["unit"]
    public = public_record({**saved["tasks"][0], "metadata": {**saved["tasks"][0]["metadata"],
                                                            "check_run_ids": ["reference-only"]}})
    assert public["metadata"]["check_run_ids"] == ["reference-only"]
    context = agent.retriever.context(project["id"])
    assert context["omitted_records"] > 0 and context["workflow"]["plan"]["version"] == 1
    assert len(json.dumps(context)) < 8500 and len(json.dumps(workflow)) < 3500
    page = await agent._tool("records", {"limit": 1}, project["id"], {})
    assert page["workflow"]["next_task"]["id"] == workflow["next_task"]["id"]
    assert page["has_more"] is True


async def test_plan_request_goals_and_constraints_are_native_fulltext_retrievable(workspace):
    from cairnmind.ingest import index_pending_sources
    from cairnmind.plans import PlanManager

    store, project, settings = workspace
    saved = PlanManager(store).save(project["id"], {"title": "Calibration", "goal": "orbital calibrator",
        "constraints": ["Avoid plutonium couplings"]}, [{"key": "start", "content": "Begin"}], "goal")
    counts = index_pending_sources(store)
    assert counts["sources"] == 1 and counts["chunks"] > 0
    retriever = Retriever(store, settings, FakeOllama())
    for query in ("orbital calibrator", "plutonium couplings"):
        hits = retriever.search(project["id"], query)
        assert hits and saved["plan"]["source_ids"][0] in {hit["source_id"] for hit in hits}
    updated = PlanManager(store).save(project["id"], {"id": saved["plan"]["id"], "goal": "neutrino compensator",
        "constraints": ["Prefer lithium couplings"]}, [{"key": "start", "content": "Begin revised implementation"}], "goal-updated",
        expected_version=1, task_versions={task["id"]: task["version"] for task in saved["tasks"]})
    assert index_pending_sources(store)["sources"] == 1
    original_source = store.get_source(saved["plan"]["source_ids"][0])
    current_source = store.get_source(updated["plan"]["source_ids"][-1])
    assert original_source["uri"] == current_source["uri"] == f"plan://{saved['plan']['id']}"
    assert original_source["is_current"] is False and "orbital calibrator" in original_source["content"]
    assert retriever.search(project["id"], "orbital calibrator") == []
    assert retriever.search(project["id"], "plutonium") == []
    assert current_source["id"] in {hit["source_id"] for hit in retriever.search(project["id"], "neutrino compensator")}
    revised = retriever.search(project["id"], "revised implementation")
    assert any(hit.get("record_id") == updated["tasks"][0]["id"] for hit in revised)
    assert all(hit["source_id"] == current_source["id"] for hit in revised)
    graph = retriever.graph(project["id"], f"plan:{saved['plan']['id']}")
    assert f"plan:{saved['plan']['id']}" in {node["id"] for node in graph["nodes"]}
    model = FakeOllama([final(current_source["id"], "neutrino compensator")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "neutrino compensator")
    assert result["status"] == "complete" and result["sources"][0]["kind"] == "plan"


async def test_current_worktree_filters_native_fts_ann_graph_and_direct_source_reads_before_limits(workspace, tmp_path):
    from cairnmind.ingest import index_project

    store, project, settings = workspace
    root_a, root_b = Path(project["root"]), tmp_path / "worktree-b"
    root_b.mkdir()
    for root in (root_a, root_b):
        git_fixture(root)
    store.add_project_root(project["id"], root_b)
    text_a = "def shared_function():\n    return 'alpha-current'\n"
    text_b = "def shared_function():\n    return 'bravo-current'\n"
    (root_a / "app.py").write_text(text_a)
    # More wrong-root candidates than the native k*3 search budget: dropping
    # them after ranking would lose the eligible result entirely.
    for index in range(6):
        (root_a / f"a_{index}.py").write_text(text_a)
    (root_b / "app.py").write_text(text_b)
    assert not index_project(store, project["id"])["errors"]
    code_rows = store.query("SELECT FROM sources WHERE project_id=:project AND kind='code'", {"project": project["id"]})
    source_b = next(source for source in code_rows if source["metadata"]["root"] == str(root_b))
    sources_a = [source for source in code_rows if source["metadata"]["root"] == str(root_a)]
    shared, _ = document(store, project, "Organization keeps public API compatibility.", "manual://shared", kind="manual")
    fact = store.remember(project["id"], {"kind": "fact", "content": "Organization keeps public API compatibility.",
                                         "status": "confirmed"}, [shared["id"]])
    scoped, _ = document(store, project, "Alpha deployment procedure", "manual://a-only", kind="manual",
                         metadata={"root": str(root_a)})
    store.remember(project["id"], {"kind": "fact", "content": "Alpha deployment procedure",
                                  "metadata": {"root": str(root_a)}}, [scoped["id"]])
    model = FakeOllama(vector=[1.0, 0.0, 0.0])
    retriever = Retriever(store, settings, model)
    for chunk in store.query("SELECT FROM chunks WHERE project_id=:project", {"project": project["id"]}):
        source = store.get_source(chunk["source_id"])
        model.vector = ([1.0, 0.0, 0.0] if source["id"] in {s["id"] for s in sources_a}
                        else [0.6, 0.8, 0.0] if source["id"] == source_b["id"] else [0.0, 1.0, 0.0])
        await retriever.embed_pending({"project_id": project["id"], "payload": {"chunk_id": chunk["id"]}})
    lexical = retriever.search(project["id"], "shared_function", limit=1, worktree=root_b)
    semantic = retriever.search(project["id"], "semantics_only", limit=1, query_vector=[1.0, 0.0, 0.0],
                                embedding_key=retriever.embedding_key, worktree=root_b)
    assert lexical[0]["source_id"] == semantic[0]["source_id"] == source_b["id"]
    assert retriever.search(project["id"], "alpha", worktree=root_b) == []
    assert retriever.search(project["id"], "Alpha deployment", worktree=root_b) == []
    assert shared["id"] in {hit["source_id"] for hit in retriever.search(project["id"], "Organization", worktree=root_b)}
    symbols = store.query("SELECT id,source_id FROM nodes WHERE kind='symbol' AND label='shared_function'")
    for symbol in symbols:
        store.upsert_edge({"id": f"shared-link-{symbol['id']}", "project_id": project["id"],
                           "from_id": f"record:{fact['id']}", "to_id": symbol["id"],
                           "kind": "uses_api", "source_id": shared["id"]})
    graph = retriever.graph(project["id"], "Organization", limit=40, worktree=root_b)
    assert f"record:{fact['id']}" in {node["id"] for node in graph["nodes"]}
    assert any(node["source_id"] == source_b["id"] for node in graph["nodes"])
    assert not {source["id"] for source in sources_a}.intersection(source["id"] for source in graph["sources"])
    assert all(node["source_id"] not in {s["id"] for s in sources_a} for node in graph["nodes"])
    assert fact["id"] in {record["id"] for record in retriever.context(project["id"], root_b)["records"]}
    model.vector = [1.0, 0.0, 0.0]
    model.replies = [calls(("search", {"query": "shared_function"}), ("graph", {"query": "shared_function"}),
                           ("current_code", {"source_id": sources_a[0]["id"]})),
                     final(source_b["id"], "return 'bravo-current'", "Current B returns bravo-current.")]
    agent = KnowledgeAgent(store, settings, model, retriever)
    answer = await agent.ask(project["id"], "What does shared_function currently return?", worktree=root_b)
    assert answer["status"] == "complete" and answer["sources"][0]["id"] == source_b["id"]
    tool_results = [json.loads(message["content"]) for message in model.calls[1]["messages"] if message["role"] == "tool"]
    assert "outside the requested worktree" in tool_results[-1]["error"]
    historical = await agent._tool("read_source", {"source_id": sources_a[0]["id"], "current": False},
                                   project["id"], {}, root_b)
    assert "alpha-current" in historical["content"]
    with pytest.raises(ValueError, match="registered"):
        retriever.search(project["id"], "shared_function", worktree=tmp_path / "unregistered")


def _record(store, project, key, status, content):
    source = store.snapshot(project["id"], f"notes://{key}", f"Note for {key}", "document")
    return store.remember(project["id"], {"kind": "decision", "key": key, "status": status,
                                          "content": content}, [source["id"]])


def _record_ids(retriever, project, query, limit=8):
    return [hit["record_id"] for hit in retriever.search(project["id"], query, limit=limit) if hit.get("record_id")]


def test_disputed_record_never_outranks_confirmed_record(workspace, monkeypatch):
    import cairnmind.retrieval as retrieval_module
    store, project, settings = workspace
    # The disputed finding repeats the query terms, so full-text search ranks it first.
    disputed = _record(store, project, "session-a", "disputed",
                       "Session timeout: the session timeout is one hour; session timeout applies everywhere.")
    confirmed = _record(store, project, "session-b", "confirmed", "Session timeout is one day.")
    retriever = Retriever(store, settings, FakeOllama())
    monkeypatch.setattr(retrieval_module, "RECORD_STATUS_PENALTY", {"confirmed": 0, "proposed": 0, "disputed": 0})
    assert _record_ids(retriever, project, "session timeout")[:2] == [disputed["id"], confirmed["id"]]
    monkeypatch.undo()
    assert _record_ids(retriever, project, "session timeout")[:2] == [confirmed["id"], disputed["id"]]


def test_status_order_never_hides_records_behind_many_matching_chunks(workspace):
    store, project, settings = workspace
    for index in range(10):
        document(store, project, f"Retry budget notes, part {index}: retry budget review.", uri=f"notes://chunk-{index}")
    proposed = _record(store, project, "retry-proposed", "proposed", "The retry budget is five attempts.")
    disputed = _record(store, project, "retry-disputed", "disputed", "The retry budget is unlimited.")
    found = _record_ids(Retriever(store, settings, FakeOllama()), project, "retry budget", limit=5)
    assert found == [proposed["id"], disputed["id"]]




def test_strong_disputed_record_is_not_buried_under_weak_confirmed_matches(workspace):
    store, project, settings = workspace
    for index in range(8):
        _record(store, project, f"cookie-{index}", "confirmed", f"The login page uses session cookie {index}.")
    disputed = _record(store, project, "timeout", "disputed",
                       "Session timeout: the session timeout is one hour; session timeout applies everywhere.")
    found = _record_ids(Retriever(store, settings, FakeOllama()), project, "session timeout", limit=6)
    assert disputed["id"] in found[:4]


@pytest.mark.parametrize("path, secondary", [
    ("tests/test_config.py", True), ("src/pkg/test/helpers.py", True), ("pkg/config_test.go", True),
    ("web/app.spec.ts", True), ("web/app.test.js", True), ("uv.lock", True), ("web/package-lock.json", True),
    ("static/app.min.js", True), ("src/pkg/config.py", False), ("docs/testing.md", False),
    ("src/pkg/contest.py", False), ("src/latest/config.py", False), ("README.md", False),
])
def test_secondary_source_recognises_tests_lock_files_and_minified_text(path, secondary):
    from cairnmind.retrieval import secondary_source
    assert secondary_source(path) is secondary
    assert secondary_source(path.replace("/", "\\")) is secondary


def _file(store, project, relative_path, text):
    return document(store, project, text, uri=f"file://{relative_path}", kind="note",
                    metadata={"relative_path": relative_path})[0]


def _source_ids(retriever, project, query, limit=8):
    return [hit["source_id"] for hit in retriever.search(project["id"], query, limit=limit)]


def test_definition_outranks_a_test_of_similar_score(workspace, monkeypatch):
    import cairnmind.retrieval as retrieval_module
    store, project, settings = workspace
    # The test repeats the query terms, so full-text search ranks it first on its own.
    test = _file(store, project, "tests/test_settings.py",
                 "def test_default():\n    # retry budget: the retry budget default\n"
                 "    assert Settings().budget == 5  # retry budget")
    definition = _file(store, project, "src/pkg/settings.py", "    budget: int = 5  # retry budget")
    retriever = Retriever(store, settings, FakeOllama())
    monkeypatch.setattr(retrieval_module, "SECONDARY_SOURCE_PENALTY", 0)
    assert _source_ids(retriever, project, "retry budget")[:2] == [test["id"], definition["id"]]
    monkeypatch.undo()
    hits = retriever.search(project["id"], "retry budget")
    assert [hit["source_id"] for hit in hits][:2] == [definition["id"], test["id"]]
    assert all("_path" not in hit for hit in hits)


def test_a_query_only_a_test_matches_still_finds_the_test(workspace):
    store, project, settings = workspace
    test = _file(store, project, "tests/test_journal.py", "def test_rollover():\n    # journal rollover keeps order")
    for index in range(8):
        _file(store, project, f"src/pkg/module_{index}.py", f"def unrelated_{index}():\n    return {index}")
    assert _source_ids(Retriever(store, settings, FakeOllama()), project, "journal rollover order") == [test["id"]]


def test_a_better_matching_test_moves_down_only_by_the_penalty(workspace):
    from cairnmind.retrieval import SECONDARY_SOURCE_PENALTY
    store, project, settings = workspace
    test = _file(store, project, "tests/test_cache.py", "def test_cache_eviction_order():\n    # cache eviction order")
    weak = [_file(store, project, f"src/pkg/cache_{index}.py", f"CACHE_{index} = {{}}  # cache")
            for index in range(SECONDARY_SOURCE_PENALTY + 3)]
    found = _source_ids(Retriever(store, settings, FakeOllama()), project, "cache eviction order", limit=20)
    # Ahead of every weaker match beyond the penalty, never dropped.
    assert found.index(test["id"]) == SECONDARY_SOURCE_PENALTY - 1
    assert set(found) == {test["id"], *(source["id"] for source in weak)}


def test_confirmed_record_beats_slightly_more_relevant_disputed_records(workspace, monkeypatch):
    import cairnmind.retrieval as retrieval_module
    store, project, settings = workspace
    crowd = [_record(store, project, f"evict-{index}", "disputed",
                     "Cache eviction: cache eviction runs hourly; cache eviction is aggressive.")
             for index in range(2)]
    confirmed = _record(store, project, "evict-policy", "confirmed", "The cache eviction policy is LRU.")
    retriever = Retriever(store, settings, FakeOllama())
    monkeypatch.setattr(retrieval_module, "RECORD_STATUS_PENALTY", {"confirmed": 0, "proposed": 0, "disputed": 0})
    assert _record_ids(retriever, project, "cache eviction")[-1] == confirmed["id"]  # least relevant on its own
    monkeypatch.undo()
    found = _record_ids(retriever, project, "cache eviction")
    assert found[0] == confirmed["id"] and set(found[1:]) == {record["id"] for record in crowd}

