"""Extraction cannot commit against changed evidence, expired time or a later attempt."""
from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest

import cairnmind.agent as agent_module
import cairnmind.ollama as ollama_module
from cairnmind.agent import KnowledgeAgent
from cairnmind.arcade import ArcadeError
from cairnmind.config import Settings
from cairnmind.memory_openai import OpenAICompatible
from cairnmind.ollama import Ollama, OllamaUnavailable
from cairnmind.store import Store, TASK_STATES


QUOTE = "Use auth.py for explicit authentication."


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = Store(tmp_path / "memory")
    project = store.add_project("extraction-safety", root)
    source = store.snapshot(project["id"], "note://policy", "Raw archived policy envelope.", "note",
                            metadata={"normalized_text": QUOTE, "role": "user"})
    jid = store.enqueue("extract", project["id"], {"source_id": source["id"]}, "policy")
    job = store.claim_job("initial-worker", lease_seconds=120, kinds=["extract"])
    assert job and job["id"] == jid
    return store, project, source, job, Settings(data_dir=store.data_dir)


class DelayedProposal:
    """Await once before an optional concurrent edit; no provider is contacted."""

    def __init__(self, change=None):
        self.change = change
        self.calls = 0

    async def chat(self, *_args, **_kwargs):
        self.calls += 1
        await asyncio.sleep(0)
        if self.change:
            self.change()
        return {"message": {"content": json.dumps({"records": [{
            "kind": "decision", "key": "authentication", "content": "Use explicit authentication.",
            "evidence": QUOTE, "metadata": {},
        }]})}}


def memory_snapshot(store):
    return {name: store.query(f"SELECT FROM {name} ORDER BY id")
            for name in ("records", "record_revisions", "nodes", "edges")}


def test_extraction_default_budget_covers_one_repair_and_lease_outlives_the_watchdog():
    assert agent_module.extraction_work_budget() == 180.0
    assert agent_module.extraction_outer_timeout() == 185.0
    assert agent_module.inference_lease_seconds() == 270.0


@pytest.mark.parametrize("budget", [30.0, .05])
def test_short_extraction_budget_keeps_a_separate_watchdog_and_lease(monkeypatch, budget):
    monkeypatch.setattr(agent_module, "EXTRACTION_TIMEOUT", budget)
    assert agent_module.extraction_work_budget() == budget
    assert agent_module.extraction_outer_timeout() > budget
    assert agent_module.extraction_outer_timeout() - budget == 5.0
    assert agent_module.inference_lease_seconds() >= budget + 90.0
    assert agent_module.inference_lease_seconds() >= 120.0


@pytest.mark.parametrize("change", ["readable_view", "superseded_source", "forgotten_project"])
async def test_changed_evidence_during_model_await_never_creates_proposals(workspace, change):
    store, project, source, job, settings = workspace
    before = memory_snapshot(store)

    def mutate():
        if change == "readable_view":
            metadata = {**source["metadata"], "normalized_text": "A corrected readable policy view."}
            store.command("UPDATE sources SET metadata=:metadata WHERE id=:id",
                          {"metadata": metadata, "id": source["id"]})
        elif change == "superseded_source":
            store.snapshot(project["id"], source["uri"], "A revised immutable policy snapshot.", "note")
        else:
            store.forget_project(project["id"])

    model = DelayedProposal(mutate)
    with pytest.raises(ValueError, match="no proposals committed"):
        await KnowledgeAgent(store, settings, model).extract(job)
    assert model.calls == 1
    assert memory_snapshot(store) == before
    if change == "forgotten_project":
        assert not store.query("SELECT id FROM jobs WHERE id=:id", {"id": job["id"]})
        assert not store.query("SELECT id FROM sources WHERE project_id=:project", {"project": project["id"]})
    else:
        # The failed proposal must not change the archived raw evidence itself.
        archived = store.get_source(source["id"])
        assert archived["content"] == source["content"]
        assert archived["sha256"] == hashlib.sha256(source["content"].encode()).hexdigest()
        diagnostic = store.query("SELECT diagnostics FROM jobs WHERE id=:id", {"id": job["id"]})[0]["diagnostics"]
        assert diagnostic["code"] == "source_changed"
        assert diagnostic["counts"]["written_records"] == 0


@pytest.mark.parametrize("deadline_after", ["record", "semantic_edge"])
async def test_deadline_after_staged_writes_rolls_back_record_revision_and_graph(
        workspace, monkeypatch, deadline_after):
    store, project, source, job, settings = workspace
    store.upsert_node({"id": "auth-module", "project_id": project["id"], "source_id": source["id"],
                       "kind": "file", "label": "auth.py", "is_current": True, "metadata": {}})
    before = memory_snapshot(store)
    monkeypatch.setattr(agent_module, "EXTRACTION_TIMEOUT", 30.0)
    # Advance this agent's clock only, after an actual database write. No sleep,
    # model timeout shortcut or mocked transaction stands in for rollback.
    real_monotonic, elapsed = time.monotonic, [0.0]
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(monotonic=lambda: real_monotonic() + elapsed[0]))
    staged = []
    if deadline_after == "record":
        original = store.remember

        def remember(*args, **kwargs):
            result = original(*args, **kwargs)
            staged.append(result["id"])
            elapsed[0] = 100.0
            return result

        monkeypatch.setattr(store, "remember", remember)
    else:
        original = store.upsert_edge

        def upsert_edge(edge, tx=None):
            result = original(edge, tx)
            if edge.get("kind") == "relates_to" and edge["id"] not in staged:
                staged.append(edge["id"])
                elapsed[0] = 100.0
            return result

        monkeypatch.setattr(store, "upsert_edge", upsert_edge)
    model = DelayedProposal()
    with pytest.raises(TimeoutError):
        await KnowledgeAgent(store, settings, model).extract(job)
    assert len(staged) == 1 and model.calls == 1
    assert memory_snapshot(store) == before
    diagnostic = store.query("SELECT diagnostics FROM jobs WHERE id=:id", {"id": job["id"]})[0]["diagnostics"]
    assert diagnostic["code"] == "timeout"
    assert diagnostic["counts"]["written_records"] == 0


async def test_old_attempt_cannot_commit_or_overwrite_a_replacement_attempt(workspace):
    store, project, source, job, settings = workspace
    before = memory_snapshot(store)
    replacement = []
    marker = {"stage": "extract", "code": "replacement_attempt"}

    def replace_attempt():
        store.finish_job(job["id"], "The original attempt failed.", worker=job["worker"])
        store.retry_job(project["id"], job["id"], job["attempts"])
        current = store.claim_job("replacement-worker", lease_seconds=120, kinds=["extract"])
        assert current and current["attempts"] == job["attempts"] + 1
        assert store.set_job_diagnostics(current["id"], marker, expected_attempts=current["attempts"],
                                         worker=current["worker"])
        replacement.append(copy.deepcopy(store.query("SELECT FROM jobs WHERE id=:id", {"id": job["id"]})[0]))

    model = DelayedProposal(replace_attempt)
    with pytest.raises(ValueError, match="stale_job.*no proposals committed"):
        await KnowledgeAgent(store, settings, model).extract(job)
    assert model.calls == 1 and len(replacement) == 1
    assert memory_snapshot(store) == before
    assert store.query("SELECT FROM jobs WHERE id=:id", {"id": job["id"]})[0] == replacement[0]
    assert store.get_source(source["id"])["sha256"] == source["sha256"]


class SixProposals:
    def __init__(self, task_metadata=None):
        self.task_metadata = task_metadata
        self.calls = 0

    async def chat(self, *_args, **_kwargs):
        self.calls += 1
        records = [{"kind": "fact", "key": f"fact-{index}", "content": f"Grounded proposal {index}.",
                    "evidence": QUOTE, "metadata": {}} for index in range(6)]
        if self.task_metadata is not None:
            records[1].update(kind="task", metadata=self.task_metadata)
        return {"message": {"content": json.dumps({"records": records})}}


@pytest.mark.parametrize("metadata,reason", [
    ({"state": "unknown"}, "task_state_invalid"),
    ({"depends_on": "PRIVATE_INVALID_METADATA"}, "task_dependencies_invalid"),
    ({"depends_on": ["PRIVATE_MISSING_TASK_ID"]}, "task_dependencies_invalid"),
    ({"acceptance_criteria": "PRIVATE_INVALID_METADATA"}, "task_metadata_invalid"),
    ({"requires_test": "PRIVATE_INVALID_METADATA"}, "task_metadata_invalid"),
])
async def test_grounded_proposals_with_invalid_task_metadata_report_safe_reason_and_roll_back(
        workspace, monkeypatch, metadata, reason):
    store, project, _source, job, settings = workspace
    before = memory_snapshot(store)
    model_metadata = metadata
    if reason == "task_state_invalid":
        # A bad model state now receives output repair before persistence. Inject
        # this separate failure at Store's boundary, after the first real write,
        # to retain its authoritative validation and atomic rollback coverage.
        model_metadata = {"state": "todo"}
        original = store.remember

        def invalidate_task_at_persistence(project_id, record, sources, *args, **kwargs):
            if record["kind"] == "task":
                record = copy.deepcopy(record)
                record["metadata"]["state"] = metadata["state"]
            return original(project_id, record, sources, *args, **kwargs)

        monkeypatch.setattr(store, "remember", invalidate_task_at_persistence)
    model = SixProposals(model_metadata)
    with pytest.raises(ValueError, match="persistence_failed.*no proposals committed") as caught:
        await KnowledgeAgent(store, settings, model).extract(job)
    diagnostic = store.job_status(project["id"], job["id"])["diagnostics"]
    assert diagnostic["failure_reason"] == reason
    assert diagnostic["persistence_stage"] == "write_record"
    assert diagnostic["counts"]["accepted_records"] == 6
    assert diagnostic["counts"]["written_records"] == 0
    assert memory_snapshot(store) == before
    assert "PRIVATE_" not in str(caught.value) + json.dumps(diagnostic)
    assert model.calls == 1


@pytest.mark.parametrize("error,reason", [
    (ArcadeError("PRIVATE_DATABASE_QUERY"), "storage_error"),
    (ValueError("Active record key already exists; PRIVATE_RECORD_CONTENT"), "record_key_conflict"),
    (RuntimeError("PRIVATE_UNEXPECTED_ERROR"), "unexpected_persistence_error"),
])
async def test_persistence_error_diagnostics_never_expose_exception_text(workspace, monkeypatch, error, reason):
    store, project, _source, job, settings = workspace
    before = memory_snapshot(store)
    original, calls = store.remember, []

    def fail_after_first_write(*args, **kwargs):
        calls.append(True)
        if len(calls) > 1:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "remember", fail_after_first_write)
    with pytest.raises(ValueError, match="persistence_failed.*no proposals committed") as caught:
        await KnowledgeAgent(store, settings, SixProposals()).extract(job)
    diagnostic = store.job_status(project["id"], job["id"])["diagnostics"]
    assert diagnostic["failure_reason"] == reason
    assert diagnostic["persistence_stage"] == "write_record"
    assert diagnostic["counts"]["written_records"] == 0
    assert len(calls) >= 2 and memory_snapshot(store) == before
    assert "PRIVATE_" not in str(caught.value) + json.dumps(diagnostic)


async def test_transaction_failure_after_success_diagnostics_is_reported_as_failed(workspace, monkeypatch):
    store, project, _source, job, settings = workspace
    before = memory_snapshot(store)
    original, failed = store.client.transaction, []

    @contextmanager
    def fail_commit_once():
        with original() as transaction:
            yield transaction
            if not failed:
                failed.append(True)
                raise ArcadeError("PRIVATE_COMMIT_ERROR")

    monkeypatch.setattr(store.client, "transaction", fail_commit_once)
    with pytest.raises(ValueError, match="persistence_failed.*no proposals committed") as caught:
        await KnowledgeAgent(store, settings, SixProposals()).extract(job)
    diagnostic = store.job_status(project["id"], job["id"])["diagnostics"]
    assert diagnostic["code"] == "persistence_failed"
    assert diagnostic["failure_reason"] == "storage_error"
    assert diagnostic["persistence_stage"] == "commit_transaction"
    assert diagnostic["counts"]["written_records"] == 0
    assert failed == [True] and memory_snapshot(store) == before
    assert "PRIVATE_" not in str(caught.value) + json.dumps(diagnostic)


def task_batch(metadata):
    records = [{"kind": "fact", "key": f"state-fact-{index}", "content": f"Grounded state proposal {index}.",
                "evidence": QUOTE, "metadata": {}} for index in range(6)]
    records[1].update(kind="task", metadata=copy.deepcopy(metadata))
    return records


class TaskStateReplies:
    def __init__(self, batches, before_reply=None):
        self.batches = list(batches)
        self.before_reply = before_reply
        self.calls = []

    async def chat(self, messages, tools=None, **kwargs):
        if self.before_reply:
            self.before_reply()
        self.calls.append({"messages": copy.deepcopy(messages), "tools": tools, **copy.deepcopy(kwargs)})
        assert self.batches, "Only the existing single repair is allowed"
        await asyncio.sleep(0)
        return {"message": {"content": json.dumps({"records": self.batches.pop(0)})}}


async def test_invalid_task_state_uses_one_repair_before_any_record_write(workspace):
    store, project, source, job, settings = workspace
    before = memory_snapshot(store)
    initial_job = store.job_status(project["id"], job["id"])

    def no_memory_writes_before_reply():
        assert memory_snapshot(store) == before

    model = TaskStateReplies([task_batch({"state": "PRIVATE_INVALID_TASK_STATE"}),
                              task_batch({"state": "blocked"})], no_memory_writes_before_reply)
    await KnowledgeAgent(store, settings, model).extract(job)
    records = store.list_records(project["id"])
    assert len(model.calls) == 2 and len(records) == 6
    assert next(record for record in records if record["kind"] == "task")["metadata"]["state"] == "blocked"
    current = store.job_status(project["id"], job["id"])
    for field in ("attempts", "state", "worker", "lease_until"):
        assert current[field] == initial_job[field]
    diagnostic = current["diagnostics"]
    assert diagnostic["code"] == "ok"
    assert diagnostic["validation_codes"] == ["task_state_invalid"]
    assert diagnostic["repair_attempted"] is True and diagnostic["repair_outcome"] == "succeeded"
    assert diagnostic["counts"]["accepted_records"] == diagnostic["counts"]["written_records"] == 6
    assert store.get_source(source["id"])["sha256"] == source["sha256"]
    assert "PRIVATE_" not in json.dumps(diagnostic)
    for call in model.calls:
        description = call["format"]["properties"]["records"]["items"]["properties"]["metadata"]["description"]
        assert "For kind=task only" in description
        assert all(state in description and state in call["messages"][0]["content"] for state in TASK_STATES)
        assert call["tools"] is None
    assert json.loads(model.calls[1]["messages"][-1]["content"])["validation_codes"] == ["task_state_invalid"]


@pytest.mark.parametrize("state", ["PRIVATE_INVALID_TASK_STATE", None, [], {}, True, 3, ""],
                         ids=["unknown", "null", "list", "object", "boolean", "number", "empty"])
async def test_twice_invalid_task_state_fails_one_attempt_without_record_revision_or_graph_writes(workspace, state):
    store, project, source, job, settings = workspace
    before = memory_snapshot(store)
    model = TaskStateReplies([task_batch({"state": state}), task_batch({"state": state})])
    with pytest.raises(ValueError, match="invalid_output.*no proposals committed") as caught:
        await KnowledgeAgent(store, settings, model).extract(job)
    store.finish_job(job["id"], str(caught.value), worker=job["worker"])
    await asyncio.sleep(0)
    current = store.job_status(project["id"], job["id"])
    diagnostic = current["diagnostics"]
    assert current["state"] == "failed" and current["attempts"] == job["attempts"] == 1
    assert current["error_code"] == "invalid_output"
    assert len(model.calls) == 2
    assert diagnostic["code"] == "invalid_output" and diagnostic["validation_codes"] == ["task_state_invalid"]
    assert diagnostic["repair_attempted"] is True and diagnostic["repair_outcome"] == "failed"
    assert diagnostic["counts"]["accepted_records"] == diagnostic["counts"]["written_records"] == 0
    assert "persistence_stage" not in diagnostic
    assert memory_snapshot(store) == before
    assert store.get_source(source["id"])["sha256"] == source["sha256"]
    assert "PRIVATE_" not in str(caught.value) + json.dumps(diagnostic)


@pytest.mark.parametrize("metadata", [{}] + [{"state": state} for state in sorted(TASK_STATES)],
                         ids=["omitted"] + sorted(TASK_STATES))
async def test_canonical_task_states_and_omission_keep_existing_store_policy_without_repair(workspace, metadata):
    store, project, _source, job, settings = workspace
    model = TaskStateReplies([task_batch(metadata)])
    await KnowledgeAgent(store, settings, model).extract(job)
    records = store.list_records(project["id"])
    task = next(record for record in records if record["kind"] == "task")
    expected = metadata.get("state", "todo")
    assert len(model.calls) == 1 and len(records) == 6
    if expected == "done":
        assert task["metadata"]["reported_state"] == "done"
        assert task["metadata"]["state"] == "todo"  # An extracted claim is still not verification.
    else:
        assert task["metadata"]["state"] == expected
    diagnostic = store.job_status(project["id"], job["id"])["diagnostics"]
    assert diagnostic["code"] == "ok" and diagnostic["repair_attempted"] is False
    assert diagnostic["validation_codes"] == [] and diagnostic["counts"]["written_records"] == 6


@pytest.mark.parametrize("kind", ["decision", "fact", "constraint", "question", "preference"])
def test_non_task_state_metadata_remains_open_in_generation_schema_and_output_validation(kind):
    metadata = {"state": {"application_state": "PRIVATE_NON_TASK_STATE"}}
    record = {"kind": kind, "content": "A source-backed application state.", "evidence": QUOTE,
              "metadata": metadata}
    output = {"records": [record]}
    schema = agent_module._extraction_schema(QUOTE)
    agent_module.Draft202012Validator.check_schema(schema)
    assert agent_module.Draft202012Validator(schema).is_valid(output)
    records, failure = agent_module._extraction_output(json.dumps(output))
    assert failure is None and records == [record]
    assert records[0]["metadata"] == metadata


class CompletionReplies:
    """Synthetic completion metadata; exercise the real agent and Store."""

    def __init__(self, replies, before_reply=None):
        self.replies = list(replies)
        self.before_reply = before_reply
        self.calls = []

    async def chat(self, messages, tools=None, **kwargs):
        if self.before_reply:
            self.before_reply()
        self.calls.append({"messages": copy.deepcopy(messages), "tools": tools, **copy.deepcopy(kwargs)})
        assert self.replies, "Only the existing single repair is allowed"
        reply = self.replies.pop(0)
        if callable(reply):
            return await reply()
        reason, content = reply
        await asyncio.sleep(0)
        return {"done_reason": reason, "eval_count": kwargs["options"]["num_predict"],
                "message": {"content": content}}


def truncated_record_json():
    return json.dumps({"records": [{"kind": "fact", "key": "truncated-only",
                                    "content": "PRIVATE_TRUNCATED_PREFIX", "evidence": QUOTE}]})


@pytest.mark.parametrize("answer_tokens", [417, 1200, 1800, 5000])
async def test_length_repair_grows_once_and_never_parses_or_commits_the_valid_prefix(
        workspace, monkeypatch, answer_tokens):
    store, project, source, job, settings = workspace
    settings.answer_tokens = answer_tokens
    before = memory_snapshot(store)
    initial_job = store.job_status(project["id"], job["id"])
    parsed, original = [], agent_module._extraction_output

    def observe_parse(text):
        parsed.append(text)
        return original(text)

    monkeypatch.setattr(agent_module, "_extraction_output", observe_parse)

    def no_write_before_complete_reply():
        assert memory_snapshot(store) == before

    complete = json.dumps({"records": task_batch({"state": "todo"})})
    model = CompletionReplies([("length", truncated_record_json()), ("stop", complete)],
                              no_write_before_complete_reply)
    await KnowledgeAgent(store, settings, model).extract(job)
    assert parsed == [complete]  # Even parseable length-marked output is not trusted.
    assert len(model.calls) == 2
    initial = min(1800, answer_tokens)
    assert model.calls[0]["options"] == {"num_predict": initial}
    assert model.calls[1]["options"] == {"num_predict": min(2 * initial, 3600)}
    assert settings.answer_tokens == answer_tokens and settings.ask_timeout == 60.0
    assert agent_module.extraction_work_budget() == 180.0
    assert agent_module.extraction_outer_timeout() == 185.0
    assert agent_module.inference_lease_seconds() == 270.0
    assert model.calls[0]["format"] == model.calls[1]["format"] == agent_module._extraction_schema(QUOTE)
    assert all(call["tools"] is None and call["format"]["properties"]["records"]["maxItems"] == 6
               for call in model.calls)
    records = store.list_records(project["id"])
    assert len(records) == 6 and all(row.get("key") != "truncated-only" for row in records)
    assert "PRIVATE_TRUNCATED_PREFIX" not in json.dumps(memory_snapshot(store))
    current = store.job_status(project["id"], job["id"])
    for field in ("attempts", "state", "worker", "lease_until"):
        assert current[field] == initial_job[field]
    diagnostic = current["diagnostics"]
    assert diagnostic["code"] == "ok" and diagnostic["validation_codes"] == ["output_truncated"]
    assert diagnostic["repair_attempted"] is True and diagnostic["repair_outcome"] == "succeeded"
    assert diagnostic["counts"]["candidate_records"] == diagnostic["counts"]["written_records"] == 6
    assert [row["done_reason"] for row in diagnostic["model_calls"]] == ["length", "stop"]
    assert store.get_source(source["id"])["sha256"] == source["sha256"]


@pytest.mark.parametrize("initial_complete", [False, True], ids=["partial-first", "parseable-first"])
@pytest.mark.parametrize("repair_complete", [False, True], ids=["partial-repair", "parseable-repair"])
async def test_second_length_is_invalid_output_before_parse_or_any_transaction(
        workspace, monkeypatch, initial_complete, repair_complete):
    store, project, source, job, settings = workspace
    before = memory_snapshot(store)
    parsed = []

    def must_not_parse(text):
        parsed.append(text)
        raise AssertionError("Length-marked output must be rejected before JSON validation")

    monkeypatch.setattr(agent_module, "_extraction_output", must_not_parse)
    payloads = [truncated_record_json() if complete else '{"records":[{"content":"PRIVATE_TRUNCATED'
                for complete in (initial_complete, repair_complete)]
    model = CompletionReplies([("length", value) for value in payloads])
    with pytest.raises(ValueError, match="invalid_output.*no proposals committed") as caught:
        await KnowledgeAgent(store, settings, model).extract(job)
    store.finish_job(job["id"], str(caught.value), worker=job["worker"])
    await asyncio.sleep(0)
    current = store.job_status(project["id"], job["id"])
    diagnostic = current["diagnostics"]
    assert current["state"] == "failed" and current["attempts"] == job["attempts"] == 1
    assert current["error_code"] == "invalid_output"
    assert len(model.calls) == 2 and parsed == []
    assert [call["options"]["num_predict"] for call in model.calls] == [1200, 2400]
    assert diagnostic["code"] == "invalid_output" and diagnostic["validation_codes"] == ["output_truncated"]
    assert diagnostic["repair_attempted"] is True and diagnostic["repair_outcome"] == "failed"
    assert diagnostic["counts"]["candidate_records"] == diagnostic["counts"]["accepted_records"] == diagnostic["counts"]["written_records"] == 0
    assert "persistence_stage" not in diagnostic and memory_snapshot(store) == before
    assert store.get_source(source["id"])["sha256"] == source["sha256"]
    assert "PRIVATE_" not in str(caught.value) + json.dumps(diagnostic)


@pytest.mark.parametrize("answer_tokens", [-1, 0, True, False, None, "1200", 1200.0,
                                           float("nan"), float("inf"), float("-inf")],
                         ids=["negative", "zero", "true", "false", "null", "text", "float", "nan", "inf", "minus-inf"])
async def test_invalid_extraction_generation_budget_never_reaches_model_or_memory(workspace, answer_tokens):
    store, project, source, job, settings = workspace
    settings.answer_tokens = answer_tokens
    before = memory_snapshot(store)
    model = CompletionReplies([])
    with pytest.raises(ValueError, match="generation_budget_invalid.*no proposals committed"):
        await KnowledgeAgent(store, settings, model).extract(job)
    diagnostic = store.job_status(project["id"], job["id"])["diagnostics"]
    assert model.calls == [] and memory_snapshot(store) == before
    assert diagnostic["code"] == "generation_budget_invalid" and diagnostic["repair_attempted"] is False
    assert diagnostic["counts"]["written_records"] == 0 and "persistence_stage" not in diagnostic
    assert store.get_source(source["id"])["sha256"] == source["sha256"]


@pytest.mark.parametrize("termination", ["timeout", "cancel"])
async def test_length_repair_keeps_existing_deadline_and_cancel_without_late_writes(
        workspace, monkeypatch, termination):
    store, project, _source, job, settings = workspace
    before = memory_snapshot(store)
    entered = asyncio.Event()
    if termination == "timeout":
        monkeypatch.setattr(agent_module, "EXTRACTION_TIMEOUT", .5)

    async def blocked_repair():
        entered.set()
        await asyncio.Event().wait()

    model = CompletionReplies([("length", truncated_record_json()), blocked_repair])
    task = asyncio.create_task(KnowledgeAgent(store, settings, model).extract(job))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if termination == "cancel":
            task.cancel()
        with pytest.raises(TimeoutError if termination == "timeout" else asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    await asyncio.sleep(0)
    assert len(model.calls) == 2
    assert [call["options"]["num_predict"] for call in model.calls] == [1200, 2400]
    assert memory_snapshot(store) == before
    diagnostic = store.job_status(project["id"], job["id"])["diagnostics"]
    assert diagnostic["code"] == ("timeout" if termination == "timeout" else "cancelled")
    assert diagnostic["repair_outcome"] == ("budget_exhausted" if termination == "timeout" else "cancelled")
    assert diagnostic["repair_attempted"] is True and diagnostic["validation_codes"] == ["output_truncated"]
    assert diagnostic["counts"]["written_records"] == 0 and "persistence_stage" not in diagnostic

def slow_model(elapsed, call_seconds):
    """A cut first answer and a good repair; each call moves the agent's fake clock by its time (#266)."""
    def reply(reason, content):
        async def call():
            elapsed[0] += call_seconds
            return {"done_reason": reason, "message": {"content": content}}
        return call
    good = json.dumps({"records": [{"kind": "decision", "key": "authentication",
                                    "content": "Use explicit authentication.", "evidence": QUOTE, "metadata": {}}]})
    return CompletionReplies([reply("length", truncated_record_json()), reply("stop", good)])


def fake_clock(monkeypatch):
    real_monotonic, elapsed = time.monotonic, [0.0]
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(monotonic=lambda: real_monotonic() + elapsed[0]))
    return elapsed


async def test_a_job_with_a_cut_answer_and_a_repair_of_60_s_each_fits_the_budget(workspace, monkeypatch):
    # Measured on Apple silicon (#266): calls of 52 s, so one cut answer (1,200 tokens at 19 a second = 63 s) and
    # its repair are two calls of about 60 s. The old budget of 120 s could not hold them.
    store, project, _source, job, settings = workspace
    model = slow_model(fake_clock(monkeypatch), 60.0)
    await KnowledgeAgent(store, settings, model).extract(job)
    diagnostic = store.job_status(project["id"], job["id"])["diagnostics"]
    assert diagnostic["code"] == "ok" and diagnostic["repair_outcome"] == "succeeded"
    assert diagnostic["budget"]["timeout_seconds"] == 180.0 and diagnostic["budget"]["elapsed_seconds"] >= 120.0
    assert len(store.list_records(project["id"])) == 1


async def test_the_same_two_calls_failed_on_the_budget_of_120_s(workspace, monkeypatch):
    store, project, _source, job, settings = workspace
    monkeypatch.setattr(agent_module, "EXTRACTION_TIMEOUT", 120.0)
    model = slow_model(fake_clock(monkeypatch), 60.0)
    with pytest.raises(TimeoutError):
        await KnowledgeAgent(store, settings, model).extract(job)
    assert store.list_records(project["id"]) == []


async def test_a_job_over_the_budget_is_still_failed_and_names_the_budget(workspace, monkeypatch):
    store, project, _source, job, settings = workspace
    model = slow_model(fake_clock(monkeypatch), 95.0)  # 190 s in all
    with pytest.raises(TimeoutError):
        await KnowledgeAgent(store, settings, model).extract(job)
    diagnostic = store.job_status(project["id"], job["id"])["diagnostics"]
    assert diagnostic["code"] == "timeout" and diagnostic["repair_outcome"] == "budget_exhausted"
    assert diagnostic["budget"]["timeout_seconds"] == 180.0 and diagnostic["budget"]["elapsed_seconds"] >= 180.0
    assert diagnostic["counts"]["written_records"] == 0 and store.list_records(project["id"]) == []


# ---- the read limit of an extraction call is what its job has left, not ask_timeout (#266) -------------------------

GOOD = json.dumps({"records": [{"kind": "decision", "key": "authentication",
                                "content": "Use explicit authentication.", "evidence": QUOTE, "metadata": {}}]})


def http_model(monkeypatch, elapsed, calls):
    """A real Ollama client over a fake transport that obeys the read limit the request carries, as httpx does.

    ``calls`` is a list of (seconds, reason, content): the call takes that long on the fake clock, or, when it is
    longer than the read limit, is cut off at the limit with a ReadTimeout. Each request's timeouts are recorded."""
    seen, real = [], httpx.AsyncClient

    def handler(request):
        timeout = request.extensions["timeout"]
        seen.append(timeout)
        seconds, reason, content = calls.pop(0)
        if reason == "down":
            raise httpx.ConnectError("refused", request=request)
        if seconds > timeout["read"]:
            elapsed[0] += timeout["read"]
            raise httpx.ReadTimeout("timed out", request=request)
        elapsed[0] += seconds
        return httpx.Response(200, json={"done_reason": reason, "message": {"role": "assistant", "content": content}})

    monkeypatch.setattr(ollama_module.httpx, "AsyncClient",
                        lambda **kw: real(**kw, transport=httpx.MockTransport(handler)))
    return seen


async def run_job(workspace, monkeypatch, calls, expect_error=None):
    store, project, _source, job, settings = workspace
    assert settings.ask_timeout == 60.0
    elapsed = fake_clock(monkeypatch)
    seen = http_model(monkeypatch, elapsed, calls)
    agent = KnowledgeAgent(store, settings, Ollama(settings))
    if expect_error:
        with pytest.raises(expect_error):
            await agent.extract(job)
    else:
        await agent.extract(job)
    return seen, store.job_status(project["id"], job["id"])["diagnostics"], store.list_records(project["id"])


async def test_a_cut_answer_and_a_repair_of_63_s_complete_with_ask_timeout_60(workspace, monkeypatch):
    # Before #266 the 63-s call was cut at 60 s by the client and the job failed as model_unavailable (read_timeout).
    seen, diagnostic, records = await run_job(workspace, monkeypatch, [
        (63.0, "length", truncated_record_json()), (63.0, "stop", GOOD)])
    assert diagnostic["code"] == "ok" and diagnostic["repair_outcome"] == "succeeded" and len(records) == 1
    assert diagnostic["budget"]["elapsed_seconds"] >= 126.0
    # The read limit handed to the client is what the budget has left: the whole of it, then the rest (#266).
    assert seen[0]["read"] == pytest.approx(180.0, abs=1.0)
    assert seen[1]["read"] == pytest.approx(180.0 - 63.0, abs=1.0)
    assert seen[0]["connect"] == seen[1]["connect"] == 60.0  # connect stays what it was


async def test_a_call_that_never_answers_ends_at_the_budget_not_at_ask_timeout(workspace, monkeypatch):
    seen, diagnostic, records = await run_job(workspace, monkeypatch, [(float("inf"), "stop", "")], TimeoutError)
    assert diagnostic["code"] == "timeout" and diagnostic["repair_outcome"] == "not_needed"
    assert diagnostic["budget"]["elapsed_seconds"] >= 179.0 and records == []
    assert len(seen) == 1 and seen[0]["read"] > 170.0


@pytest.mark.parametrize("first", [178.0, 179.99])
async def test_a_repair_with_almost_no_budget_left_fails_as_the_budget(workspace, monkeypatch, first):
    seen, diagnostic, records = await run_job(workspace, monkeypatch, [
        (first, "length", truncated_record_json()), (63.0, "stop", GOOD)], TimeoutError)
    assert diagnostic["code"] == "timeout" and diagnostic["repair_outcome"] == "budget_exhausted"
    assert diagnostic["repair_attempted"] is True and records == []
    assert 0.9 < seen[1]["read"] < 3.0  # what was left, never less than the floor of 1 s


async def test_an_unreachable_model_server_is_still_model_unavailable_at_once(workspace, monkeypatch):
    seen, diagnostic, records = await run_job(workspace, monkeypatch, [(0.0, "down", "")], ValueError)
    assert diagnostic["code"] == "model_unavailable" and diagnostic["budget"]["elapsed_seconds"] < 5.0
    assert len(seen) == 1 and records == []


async def test_a_question_keeps_ask_timeout_as_its_read_limit(workspace, monkeypatch):
    _store, _project, _source, _job, settings = workspace
    seen = http_model(monkeypatch, fake_clock(monkeypatch), [(float("inf"), "stop", "")])
    with pytest.raises(OllamaUnavailable) as raised:
        await Ollama(settings).chat([{"role": "user", "content": "q"}])
    assert raised.value.error_code == "read_timeout" and seen[0]["read"] == 60.0


async def test_the_openai_compatible_client_takes_the_same_read_limit(workspace, monkeypatch):
    _store, _project, _source, _job, settings = workspace
    seen = http_model(monkeypatch, fake_clock(monkeypatch), [(1.0, "stop", "")] * 2)
    client = OpenAICompatible(settings, "chat", {"base_url": "http://127.0.0.1:1/v1"})
    with pytest.raises(OllamaUnavailable) as raised:  # the fake reply is Ollama-shaped, which this client rejects
        await client.chat([{"role": "user", "content": "q"}], read_timeout=42.0)
    assert raised.value.error_code == "invalid_response"
    assert seen[0]["read"] == 42.0 and seen[0]["connect"] == 60.0
    with pytest.raises(OllamaUnavailable):
        await client.chat([{"role": "user", "content": "q"}])
    assert seen[1]["read"] == 60.0
