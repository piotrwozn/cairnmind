"""Bounded model recovery and atomic memory writes on the real ArcadeDB engine."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
from uuid import uuid4

import pytest

import devbrain.agent as agent_module
import devbrain.service as service_module
from devbrain.agent import KnowledgeAgent
from devbrain.config import Settings
from devbrain.ollama import OllamaUnavailable
from devbrain.store import Store


PRIVATE = "PRIVATE_METADATA_CREDENTIAL_DO_NOT_ECHO"
BAD_RESPONSE = "PRIVATE_UNTRUSTED_MODEL_RESPONSE"


class ScriptedOllama:
    """Deterministic transport; storage, retrieval and transactions remain real."""

    def __init__(self, replies, *, context_chars=None):
        self.replies = list(replies)
        self.calls = []
        self.context_chars = context_chars

    async def chat(self, messages, tools=None, **kwargs):
        call = {"messages": copy.deepcopy(messages), "tools": tools, "kwargs": copy.deepcopy(kwargs)}
        self.calls.append(call)
        if self.context_chars is not None and kwargs.get("format"):
            size = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
            size += len(json.dumps(kwargs["format"], ensure_ascii=False))
            if size > self.context_chars:
                raise OllamaUnavailable("context limit", error_code="context_length")
        if not self.replies:
            raise AssertionError("The bounded model-call allowance was exceeded")
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(call)
        if inspect.isawaitable(reply):
            reply = await reply
        if isinstance(reply, BaseException):
            raise reply
        return {"message": {"role": "assistant", **reply}, "done_reason": "stop",
                "prompt_eval_count": 120, "eval_count": 30, "total_duration": 1000,
                "untrusted_runtime_detail": PRIVATE}

    async def embed(self, texts):
        return [[0.0, 1.0, 0.0] for _ in texts]

    async def model_identity(self, name):
        return name + "@memory-stabilization"


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = Store(tmp_path / "memory")
    project = store.add_project("memory-stabilization", root)
    settings = Settings(data_dir=store.data_dir)
    return store, project, settings


def source(store, project, text, *, uri=None, searchable=False):
    item = store.snapshot(project["id"], uri or f"note://{uuid4()}", text, "note",
                          metadata={"role": "user", "private_metadata": PRIVATE,
                                    "analysis": {"credential": PRIVATE}})
    if searchable:
        store.command("INSERT INTO chunks CONTENT :document", {"document": {
            "id": str(uuid4()), "project_id": project["id"], "source_id": item["id"], "text": text,
            "line_start": 1, "line_end": len(text.splitlines()), "metadata": {}, "is_current": True,
        }})
    return item


def final(source_id, quote, *, answer="A source-backed answer."):
    return {"content": json.dumps({"answer": answer,
                                  "citations": [{"source_id": source_id, "quote": quote}],
                                  "unknowns": []})}


def extraction(*records):
    return {"content": json.dumps({"records": records})}


def candidate(quote, *, key="auth-policy", content="Use explicit authentication.", kind="decision", **metadata):
    return {"kind": kind, "key": key, "content": content, "evidence": quote, "metadata": metadata}


def extraction_job(store, project, item):
    job_id = store.enqueue("extract", project["id"], {"source_id": item["id"]}, f"extract:{item['id']}")
    job = store.claim_job("memory-stabilization", lease_seconds=120, kinds=["extract"])
    assert job and job["id"] == job_id
    return job


def job_row(store, job):
    return store.query("SELECT FROM jobs WHERE id=:id", {"id": job["id"]})[0]


def memory_snapshot(store):
    # @rid is stable within one database and makes partial graph writes visible.
    return {name: store.query(f"SELECT FROM {name} ORDER BY id")
            for name in ("records", "record_revisions", "nodes", "edges")}


def safe_diagnostics(value):
    encoded = json.dumps(value, allow_nan=False)
    assert PRIVATE not in encoded and BAD_RESPONSE not in encoded
    assert len(encoded) <= 20_000
    assert isinstance(value.get("stage"), str)
    assert isinstance(value.get("code"), str)


def service_job_fixture(workspace, monkeypatch, model, *, kind="extract"):
    """Run the real service queue and Store; unrelated CPU schedulers are idle."""
    store, project, settings = workspace
    settings.token = "synthetic-service-token"
    item = source(store, project, "Do not commit memory after extraction has timed out.")
    job_id = store.enqueue(kind, project["id"], {"source_id": item["id"]}, f"budget:{kind}:{item['id']}")
    for name in ("devbrain.integrations.drain_hook_spool", "devbrain.ingest.poll_imports",
                 "devbrain.ingest.index_project", "devbrain.ingest.index_pending_sources"):
        monkeypatch.setattr(name, lambda *_args, **_kwargs: {"errors": []})
    monkeypatch.setattr("devbrain.memory.MemoryManager.tick", lambda _self: {"errors": []})
    monkeypatch.setattr("devbrain.verification.recover_interrupted_runs", lambda *_args: None)
    monkeypatch.setattr(Store, "backup", lambda *_args: None)
    claims, original_claim = [], Store.claim_job

    def claim(self, worker, lease_seconds=60, kinds=None, **kwargs):
        row = original_claim(self, worker, lease_seconds, kinds, **kwargs)
        if worker == "inference" and row:
            claims.append((row["id"], row["attempts"], lease_seconds))
        return row

    monkeypatch.setattr(Store, "claim_job", claim)
    app = service_module.create_app(settings, ollama=model)
    return app, job_id, claims


class ServiceScriptedOllama(ScriptedOllama):
    model_identity = None  # These tests enqueue one job; no embedding-repair scan.

    async def models(self):
        return {"models": []}


async def wait_for_failed_job(store, project_id, job_id):
    async with asyncio.timeout(10):
        while True:
            row = store.job_status(project_id, job_id)
            if row["state"] == "failed":
                return row
            await asyncio.sleep(.01)


@pytest.mark.parametrize("phase", ["initial", "repair"])
@pytest.mark.parametrize("returns_after_cancel", [False, True])
async def test_service_inner_extraction_timeout_wins_without_late_write_or_second_attempt(
        workspace, monkeypatch, phase, returns_after_cancel):
    store, project, _settings = workspace
    monkeypatch.setattr(agent_module, "EXTRACTION_TIMEOUT", .05)
    cancelled, settled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked(_call):
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            if not returns_after_cancel:
                raise
            # A transport that returns after cancellation still cannot commit.
            return extraction(candidate("Do not commit memory after extraction has timed out."))
        finally:
            settled.set()
        raise AssertionError("The timed-out provider must not run later")

    replies = ([{"content": BAD_RESPONSE}] if phase == "repair" else []) + [blocked]
    model = ServiceScriptedOllama(replies)
    app, job_id, claims = service_job_fixture(workspace, monkeypatch, model)
    before = memory_snapshot(store)
    async with app.router.lifespan_context(app):
        row = await wait_for_failed_job(store, project["id"], job_id)
        assert cancelled.is_set() and settled.is_set()
        assert row["attempts"] == 1 and len(claims) == 1
        assert claims[0] == (job_id, 1, agent_module.inference_lease_seconds())
        diagnostic = row["diagnostics"]
        safe_diagnostics(diagnostic)
        assert diagnostic["code"] == "timeout"  # Outer cancellation used to win.
        assert diagnostic["budget"]["timeout_seconds"] == .05
        assert diagnostic["counts"]["written_records"] == 0
        assert diagnostic["repair_attempted"] is (phase == "repair")
        assert diagnostic["repair_outcome"] == ("budget_exhausted" if phase == "repair" else "not_needed")
        assert len(model.calls) == (2 if phase == "repair" else 1)
        release.set()
        await asyncio.sleep(.02)
        assert memory_snapshot(store) == before
        assert store.job_status(project["id"], job_id)["attempts"] == 1
    assert memory_snapshot(store) == before and len(claims) == 1


@pytest.mark.parametrize("kind", ["extract", "embed"])
async def test_service_watchdog_still_stops_stalled_work_and_embedding_keeps_thirty_seconds(
        workspace, monkeypatch, kind):
    store, project, _settings = workspace
    cancelled, entered = asyncio.Event(), asyncio.Event()
    requested_timeouts = []
    monkeypatch.setattr(agent_module, "extraction_outer_timeout", lambda: .05)

    async def stalled(_self, _job):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    # Deliberately no inner agent deadline: the independent watchdog must work.
    monkeypatch.setattr(KnowledgeAgent, "extract", stalled)
    monkeypatch.setattr("devbrain.retrieval.Retriever.embed_pending", stalled)

    class ServiceAsyncio:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        def wait_for(self, operation, timeout):
            requested_timeouts.append(timeout)
            # Observe the real embedding budget without sleeping for 30 seconds.
            return asyncio.wait_for(operation, .05 if kind == "embed" else timeout)

    monkeypatch.setattr(service_module, "asyncio", ServiceAsyncio())
    app, job_id, claims = service_job_fixture(workspace, monkeypatch, ServiceScriptedOllama([]), kind=kind)
    before = memory_snapshot(store)
    async with app.router.lifespan_context(app):
        row = await wait_for_failed_job(store, project["id"], job_id)
        assert entered.is_set() and cancelled.is_set()
        assert row["attempts"] == 1 and row["error_code"] == "job_timeout"
        assert job_row(store, {"id": job_id})["error"] == "TimeoutError"
        assert requested_timeouts == ([30] if kind == "embed" else [.05])
        assert claims == [(job_id, 1, 210.0)]
    assert memory_snapshot(store) == before and len(claims) == 1


async def test_valid_first_answer_keeps_one_call_and_adds_safe_diagnostics(workspace):
    store, project, settings = workspace
    quote = "Authentication requires a literal source-backed policy."
    item = source(store, project, quote, searchable=True)
    model = ScriptedOllama([final(item["id"], quote)])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "Authentication literal policy")
    assert result["status"] == "complete"
    assert result["sources"][0]["quote"] == quote
    assert len(model.calls) == 1
    safe_diagnostics(result["diagnostics"])
    assert result["diagnostics"]["repair_attempted"] is False


async def test_answer_diagnostics_normalize_untrusted_runtime_counters(workspace):
    store, project, settings = workspace
    quote = "Runtime counters are not project evidence."
    item = source(store, project, quote, searchable=True)

    class UnsafeCounters(ScriptedOllama):
        async def chat(self, *args, **kwargs):
            result = await super().chat(*args, **kwargs)
            return {**result, "done_reason": PRIVATE, "prompt_eval_count": True,
                    "eval_count": -1, "total_duration": PRIVATE}

    model = UnsafeCounters([final(item["id"], quote)])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "Runtime counters evidence")
    assert result["status"] == "complete"
    diagnostic = result["diagnostics"]
    safe_diagnostics(diagnostic)
    assert len(diagnostic["model_calls"]) == 1
    call = diagnostic["model_calls"][0]
    for name in ("prompt_eval_count", "eval_count", "total_duration"):
        assert name not in call or (type(call[name]) is int and 0 <= call[name] <= 2**63 - 1)


async def test_compact_answer_repair_fits_after_a_large_inspected_tool_exchange(workspace):
    store, project, settings = workspace
    settings.num_ctx = 8000
    settings.max_steps = 4
    quote = "A bounded repair must retain this exact authentication decision."
    item = source(store, project, quote + "\n" + "context detail " * 900, searchable=True)
    tool_reply = {"content": "", "tool_calls": [{"function": {"name": "read_source", "arguments": {
        "source_id": item["id"], "line_start": 1, "line_end": 3}}}]}
    bad = {"content": BAD_RESPONSE + " invalid prose " * 450}
    model = ScriptedOllama([tool_reply, bad, final(item["id"], quote)], context_chars=16000)
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "authentication exact decision")
    assert result["status"] == "complete", result["unknowns"]
    assert len(model.calls) == 3
    repair = model.calls[-1]
    assert repair["tools"] is None
    assert all(message["role"] != "tool" and "tool_calls" not in message for message in repair["messages"])
    # Replaying the read history and full malformed output cannot fit this backend.
    history_size = sum(len(json.dumps(m, ensure_ascii=False)) for m in model.calls[1]["messages"])
    assert history_size + len(bad["content"]) > model.context_chars
    assert result["sources"][0]["quote"] == quote
    safe_diagnostics(result["diagnostics"])
    assert result["diagnostics"]["repair_attempted"] is True
    assert result["diagnostics"]["repair_outcome"] == "succeeded"


async def test_answer_repair_catalog_contains_only_scoped_inspected_sources(workspace, tmp_path):
    store, project, settings = workspace
    quote = "ScopedAuthentication has one documented decision."
    item = source(store, project, quote, searchable=True)
    unseen = source(store, project, "UNINSPECTED_PRIVATE_TEXT")
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_project = store.add_project("foreign", foreign_root)
    foreign = source(store, foreign_project, "FOREIGN_PRIVATE_TEXT")
    malformed = final(foreign["id"], "unsupported quote", answer=BAD_RESPONSE)
    model = ScriptedOllama([malformed, final(item["id"], quote)])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "ScopedAuthentication decision")
    assert result["status"] == "complete"
    repair = model.calls[1]
    allowed = repair["kwargs"]["format"]["properties"]["citations"]["items"]["properties"]["source_id"]["enum"]
    assert allowed == [item["id"]]
    assert unseen["id"] not in allowed and foreign["id"] not in allowed
    prompt = json.dumps(repair["messages"])
    assert "UNINSPECTED_PRIVATE_TEXT" not in prompt and "FOREIGN_PRIVATE_TEXT" not in prompt
    assert PRIVATE not in prompt
    safe_diagnostics(result["diagnostics"])


async def test_answer_repair_cannot_turn_a_paraphrase_into_verified_evidence(workspace):
    store, project, settings = workspace
    quote = "Only this literal authentication sentence is source-backed."
    item = source(store, project, quote, searchable=True)
    model = ScriptedOllama([{"content": BAD_RESPONSE}, final(item["id"], "An invented paraphrase")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "authentication source-backed")
    assert result["status"] == "partial"
    assert "An invented paraphrase" not in result["answer"]
    assert len(model.calls) == 2
    safe_diagnostics(result["diagnostics"])


async def test_extraction_structured_generation_repairs_once_and_preserves_job_lease(workspace):
    store, project, settings = workspace
    settings.answer_tokens = 417
    quote = "Use explicit authentication for every request."
    item = source(store, project, quote)
    job = extraction_job(store, project, item)
    before = job_row(store, job)
    model = ScriptedOllama([{"content": BAD_RESPONSE}, extraction(candidate(quote))])
    await KnowledgeAgent(store, settings, model).extract(job)
    records = store.list_records(project["id"])
    assert len(records) == 1 and records[0]["status"] == "proposed"
    assert records[0]["metadata"]["evidence"] == quote
    assert len(model.calls) == 2
    for call in model.calls:
        assert call["tools"] is None
        schema = call["kwargs"]["format"]
        assert isinstance(schema, dict) and schema["type"] == "object"
        assert schema["properties"]["records"]["maxItems"] == 6
        assert 0 < call["kwargs"]["options"]["num_predict"] <= 417
    after = job_row(store, job)
    for key in ("state", "worker", "lease_until", "attempts"):
        assert after[key] == before[key]
    diagnostic = after["diagnostics"]
    safe_diagnostics(diagnostic)
    assert diagnostic["repair_attempted"] is True
    assert diagnostic["repair_outcome"] == "succeeded"
    assert diagnostic["counts"]["written_records"] == 1


@pytest.mark.parametrize("first", [BAD_RESPONSE, json.dumps({"records": {"kind": "decision"}})])
async def test_twice_invalid_extraction_raises_without_any_memory_write(workspace, first):
    store, project, settings = workspace
    item = source(store, project, "A private source-backed policy.")
    job = extraction_job(store, project, item)
    before = memory_snapshot(store)
    model = ScriptedOllama([{"content": first}, {"content": BAD_RESPONSE}])
    with pytest.raises(ValueError) as error:
        await KnowledgeAgent(store, settings, model).extract(job)
    assert BAD_RESPONSE not in str(error.value) and PRIVATE not in str(error.value)
    assert memory_snapshot(store) == before
    assert len(model.calls) == 2
    diagnostic = job_row(store, job)["diagnostics"]
    safe_diagnostics(diagnostic)
    assert diagnostic["repair_attempted"] is True
    assert diagnostic["repair_outcome"] == "failed"


@pytest.mark.parametrize("phase", ["initial", "repair"])
async def test_cancelled_extraction_has_no_late_commit_and_keeps_safe_diagnostics(workspace, phase):
    store, project, settings = workspace
    item = source(store, project, "Do not publish memory before inference completes.")
    job = extraction_job(store, project, item)
    before = memory_snapshot(store)
    entered = asyncio.Event()

    async def blocked(_call):
        entered.set()
        await asyncio.Event().wait()

    replies = ([{"content": BAD_RESPONSE}] if phase == "repair" else []) + [blocked]
    model = ScriptedOllama(replies)
    task = asyncio.create_task(KnowledgeAgent(store, settings, model).extract(job))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert memory_snapshot(store) == before
    diagnostic = job_row(store, job)["diagnostics"]
    safe_diagnostics(diagnostic)
    assert diagnostic["code"] == "cancelled"
    assert diagnostic["repair_attempted"] is (phase == "repair")
    assert diagnostic["repair_outcome"] == ("cancelled" if phase == "repair" else "not_needed")
    assert len(model.calls) == (2 if phase == "repair" else 1)


async def test_cancellation_requested_at_model_completion_prevents_commit(workspace):
    store, project, settings = workspace
    quote = "Memory must not commit after cancellation was requested."
    item = source(store, project, quote)
    job = extraction_job(store, project, item)
    before = memory_snapshot(store)

    def cancel_before_returning(_call):
        asyncio.current_task().cancel()
        return extraction(candidate(quote))

    model = ScriptedOllama([cancel_before_returning])
    task = asyncio.create_task(KnowledgeAgent(store, settings, model).extract(job))
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert memory_snapshot(store) == before
    diagnostic = job_row(store, job)["diagnostics"]
    safe_diagnostics(diagnostic)
    assert diagnostic["code"] == "cancelled"
    assert diagnostic["counts"]["written_records"] == 0


async def test_extraction_skips_nonliteral_evidence_without_repairing_facts(workspace):
    store, project, settings = workspace
    quote = "Use explicit authentication for every request."
    item = source(store, project, quote)
    job = extraction_job(store, project, item)
    model = ScriptedOllama([extraction(candidate(quote), candidate("NO SUCH LITERAL SOURCE", key="invented",
                                                                content="Invented policy."))])
    await KnowledgeAgent(store, settings, model).extract(job)
    records = store.list_records(project["id"])
    assert len(records) == 1 and records[0]["metadata"]["evidence"] == quote
    assert records[0]["content"] != "Invented policy."
    assert len(model.calls) == 1
    diagnostic = job_row(store, job)["diagnostics"]
    safe_diagnostics(diagnostic)
    assert diagnostic["repair_attempted"] is False
    assert diagnostic["counts"]["accepted_records"] == 1
    assert diagnostic["counts"]["skipped_records"] == 1
    assert diagnostic["counts"]["written_records"] == 1


async def test_one_proposal_cannot_inject_machine_proof_or_poison_a_valid_batch(workspace):
    store, project, settings = workspace
    quote = "Use explicit authentication for every request."
    item = source(store, project, quote)
    job = extraction_job(store, project, item)
    forged = candidate(quote, key="forged-proof", content="Authentication was machine verified.", kind="task",
                       state="done", check_run_ids=["forged-check"], test_passed=True, machine_verified=True)
    model = ScriptedOllama([extraction(candidate(quote), forged)])
    await KnowledgeAgent(store, settings, model).extract(job)
    records = store.list_records(project["id"])
    assert len(records) == 1 and records[0]["kind"] == "decision"
    assert records[0]["metadata"]["evidence"] == quote
    assert len(model.calls) == 1
    diagnostic = job_row(store, job)["diagnostics"]
    safe_diagnostics(diagnostic)
    assert diagnostic["repair_attempted"] is False
    assert diagnostic["counts"]["written_records"] == 1
    assert diagnostic["counts"]["skipped_records"] == 1


@pytest.mark.parametrize("failure", ["second_record", "second_semantic_edge"])
async def test_extraction_storage_failure_rolls_back_the_entire_batch(workspace, monkeypatch, failure):
    store, project, settings = workspace
    first = "Use auth.py for authentication."
    second = "Test auth.py before accepting authentication."
    item = source(store, project, first + "\n" + second)
    store.upsert_node({"id": "auth-module", "project_id": project["id"], "kind": "file", "label": "auth.py",
                       "source_id": item["id"], "is_current": True, "metadata": {}})
    job = extraction_job(store, project, item)
    before = memory_snapshot(store)
    count = 0
    if failure == "second_record":
        original = store.remember

        def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError(PRIVATE)
            return original(*args, **kwargs)

        monkeypatch.setattr(store, "remember", fail_second)
    else:
        original = store.upsert_edge

        def fail_second(edge, tx=None):
            nonlocal count
            if edge.get("kind") == "relates_to":
                count += 1
                if count == 2:
                    raise RuntimeError(PRIVATE)
            return original(edge, tx)

        monkeypatch.setattr(store, "upsert_edge", fail_second)
    model = ScriptedOllama([extraction(candidate(first, key="auth", content="Use auth.py."),
                                     candidate(second, key="test", content="Test auth.py."))])
    with pytest.raises((ValueError, RuntimeError)):
        await KnowledgeAgent(store, settings, model).extract(job)
    assert count == 2
    assert memory_snapshot(store) == before
    assert len(model.calls) == 1
    diagnostic = job_row(store, job)["diagnostics"]
    safe_diagnostics(diagnostic)
    assert diagnostic["counts"]["written_records"] == 0


async def test_repeated_valid_extraction_is_idempotent(workspace):
    store, project, settings = workspace
    quote = "Use explicit authentication for every request."
    item = source(store, project, quote)
    job = extraction_job(store, project, item)
    model = ScriptedOllama([extraction(candidate(quote)), extraction(candidate(quote))])
    agent = KnowledgeAgent(store, settings, model)
    await agent.extract(job)
    committed = memory_snapshot(store)
    await agent.extract(job)
    assert memory_snapshot(store) == committed
    assert len(store.list_records(project["id"])) == 1
    assert len(model.calls) == 2


async def test_extraction_enforces_the_six_record_schema_before_writing(workspace):
    store, project, settings = workspace
    quote = "Only bounded extraction batches are accepted."
    item = source(store, project, quote)
    job = extraction_job(store, project, item)
    oversized = extraction(*(candidate(quote, key=f"fact-{i}", content=f"Fact {i}.") for i in range(7)))
    model = ScriptedOllama([oversized, extraction()])
    await KnowledgeAgent(store, settings, model).extract(job)
    assert store.list_records(project["id"]) == []
    assert len(model.calls) == 2
    assert job_row(store, job)["diagnostics"]["repair_attempted"] is True


async def test_extraction_links_records_to_named_symbols_and_nested_files(workspace):
    store, project, settings = workspace
    quote = "refresh_token() in src/auth/session.py keeps sessions for one hour."
    item = source(store, project, quote)
    nodes = {
        "symbol-refresh": ("symbol", "refresh_token"),
        "file-session": ("file", "src/auth/session.py"),
        "symbol-unrelated": ("symbol", "logout_user"),
        "file-unrelated": ("file", "src/billing/invoice.py"),
    }
    for node_id, (kind, label) in nodes.items():
        store.upsert_node({"id": node_id, "project_id": project["id"], "kind": kind, "label": label,
                           "source_id": item["id"], "is_current": True, "metadata": {}})
    job = extraction_job(store, project, item)
    model = ScriptedOllama([extraction(candidate(quote, key="session-ttl", content="Sessions last one hour."))])
    await KnowledgeAgent(store, settings, model).extract(job)
    linked = {edge["to_id"] for edge in store.query(
        "SELECT FROM edges WHERE project_id=:project AND kind='relates_to'", {"project": project["id"]})}
    assert linked == {"symbol-refresh", "file-session"}


async def test_single_character_quote_is_not_accepted_as_evidence(workspace):
    store, project, settings = workspace
    text = "Authentication requires a literal source-backed policy."
    item = source(store, project, text, searchable=True)
    invented = "Sessions never expire."
    model = ScriptedOllama([final(item["id"], "A", answer=invented), final(item["id"], "A", answer=invented)])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "Authentication literal policy")
    assert result["status"] == "partial"
    assert not any(source.get("quote") == "A" for source in result["sources"])


async def test_complete_answer_reports_quote_coverage(workspace):
    store, project, settings = workspace
    quote = "Authentication requires a literal source-backed policy."
    item = source(store, project, quote, searchable=True)
    model = ScriptedOllama([final(item["id"], quote, answer="Authentication requires a literal policy.")])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], "Authentication literal policy")
    assert result["status"] == "complete"
    assert result["coverage"] == {"answer_terms": 4, "terms_in_quotes": 4, "ratio": 1.0}


async def test_format_repair_keeps_verified_draft_citations(workspace):
    store, project, settings = workspace
    card = "Provenance card: the session port fact comes from the canonical note."
    canonical = "Canonical note: the session service listens on TCP port 28267."
    first = source(store, project, card, searchable=True)
    second = source(store, project, canonical, searchable=True)
    draft = {"content": json.dumps({"answer": "port 28267", "unknowns": [], "citations": [
        {"source_id": first["id"], "quote": card}, {"source_id": second["id"], "quote": canonical}]})}
    repaired = {"content": json.dumps({"answer": {"port": 28267}, "unknowns": [], "citations": [
        {"source_id": second["id"], "quote": canonical}]})}
    model = ScriptedOllama([draft, repaired])
    schema = {"type": "object", "properties": {"port": {"type": "integer"}}, "required": ["port"],
              "additionalProperties": False}
    result = await KnowledgeAgent(store, settings, model).ask(
        project["id"], "Which TCP port does the session service use? Provenance card canonical note",
        answer_schema=schema)
    assert result["status"] == "complete" and json.loads(result["answer"]) == {"port": 28267}
    assert {source["id"] for source in result["sources"]} == {first["id"], second["id"]}
    assert result["diagnostics"]["repair_attempted"] is True


async def test_repair_ignores_malformed_draft_citations_and_measures_typed_values(workspace):
    store, project, settings = workspace
    canonical = "Canonical note: the session service listens on TCP port 28267."
    item = source(store, project, canonical, searchable=True)
    draft = {"content": json.dumps({"answer": "port 28267", "unknowns": [], "citations": [
        {"source_id": item["id"], "quote": ["not", "a", "string"]},
        {"source_id": ["not", "an", "id"], "quote": canonical}]})}
    repaired = {"content": json.dumps({"answer": {"port": 28267}, "unknowns": [], "citations": [
        {"source_id": item["id"], "quote": canonical}]})}
    schema = {"type": "object", "properties": {"port": {"type": "integer"}}, "required": ["port"],
              "additionalProperties": False}
    result = await KnowledgeAgent(store, settings, ScriptedOllama([draft, repaired])).ask(
        project["id"], "Which TCP port does the session service use? Canonical note", answer_schema=schema)
    assert result["status"] == "complete" and [s["id"] for s in result["sources"]] == [item["id"]]
    assert result["coverage"] == {"answer_terms": 1, "terms_in_quotes": 1, "ratio": 1.0}


async def test_file_links_match_absolute_labels_by_exact_file_name(workspace):
    store, project, settings = workspace
    quote = "token_store.py and session.py hold the refresh logic."
    item = source(store, project, quote)
    for node_id, label in {"posix": "/home/dev/proj/src/auth/token_store.py",
                           "windows": "C:\\proj\\src\\session.py",
                           "wildcard-decoy": "/home/dev/proj/src/tokenXstore.py"}.items():
        store.upsert_node({"id": node_id, "project_id": project["id"], "kind": "file", "label": label,
                           "source_id": item["id"], "is_current": True, "metadata": {}})
    job = extraction_job(store, project, item)
    model = ScriptedOllama([extraction(candidate(quote, key="refresh", content="Refresh logic lives in two files."))])
    await KnowledgeAgent(store, settings, model).extract(job)
    linked = {edge["to_id"] for edge in store.query(
        "SELECT FROM edges WHERE project_id=:project AND kind='relates_to'", {"project": project["id"]})}
    assert linked == {"posix", "windows"}
