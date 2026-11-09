"""Source-only extraction grammars and honest rejection counts on real ArcadeDB."""
from __future__ import annotations

import copy
import json
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from devbrain import agent as agent_module
from devbrain.agent import KnowledgeAgent
from devbrain.config import Settings
from devbrain.store import Store


HISTORY_CANARY = "UNRELATED_EXISTING_RECORD_CONTENT_MUST_NOT_ENTER_MODEL_INPUT"
REJECTED_CANARY = "PRIVATE_REJECTED_MODEL_EVIDENCE_MUST_NOT_ENTER_DIAGNOSTICS"


class ControlledOllama:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), "tools": tools, "kwargs": copy.deepcopy(kwargs)})
        if not self.replies:
            raise AssertionError("A semantic rejection must not start another inference loop")
        return {"message": {"role": "assistant", "content": self.replies.pop(0)},
                "done_reason": "stop", "prompt_eval_count": 120, "eval_count": 40}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = Store(tmp_path / "memory")
    project = store.add_project("grounded-extraction", root)
    return store, project, Settings(data_dir=store.data_dir)


def short_source():
    prefix = "Use explicit authentication and retain literal source evidence. "
    return prefix + "x" * (127 - len(prefix))


def snapshot(store, project, text):
    return store.snapshot(project["id"], f"note://{uuid4()}", text, "note", metadata={"role": "user"})


def claimed_job(store, project, item):
    ident = store.enqueue("extract", project["id"], {"source_id": item["id"]}, f"extract:{item['id']}")
    job = store.claim_job("grounding-test", lease_seconds=120, kinds=["extract"])
    assert job and job["id"] == ident
    return job


def record(evidence, *, content="A grounded project fact.", key="grounded-fact", metadata=None):
    return {"kind": "fact", "key": key, "content": content, "evidence": evidence, "metadata": metadata or {}}


def output(*records):
    return json.dumps({"records": records})


def diagnostics(store, job):
    return store.query("SELECT diagnostics FROM jobs WHERE id=:id", {"id": job["id"]})[0]["diagnostics"]


def memory_state(store):
    return {name: store.query(f"SELECT FROM {name} ORDER BY id")
            for name in ("records", "record_revisions", "nodes", "edges")}


def evidence_property(schema):
    return schema["properties"]["records"]["items"]["properties"]["evidence"]


async def test_short_source_is_the_only_evidence_in_initial_and_repair_prompts_and_dedupe_stays_server_side(workspace):
    store, project, settings = workspace
    text = short_source()
    assert len(text) == 127
    item = snapshot(store, project, text)
    unrelated = snapshot(store, project, "An unrelated earlier conversation.")
    store.remember(project["id"], {"kind": "decision", "key": "unrelated", "content": HISTORY_CANARY},
                   [unrelated["id"]])
    duplicate = "A grounded recurring project fact."
    store.remember(project["id"], {"kind": "fact", "key": "already-known", "content": duplicate}, [item["id"]])
    job = claimed_job(store, project, item)
    before = memory_state(store)
    model = ControlledOllama(["{", output(record(text, content=duplicate))])
    await KnowledgeAgent(store, settings, model).extract(job)
    assert len(model.calls) == 2
    for call in model.calls:
        assert call["tools"] is None
        request = json.dumps(call)
        assert HISTORY_CANARY not in request
        assert duplicate not in request
        assert unrelated["id"] not in request
        payload = json.loads(call["messages"][-1]["content"])
        assert payload["text"] == text and "existing" not in payload
        assert evidence_property(call["kwargs"]["format"])["enum"] == [text]
    assert memory_state(store) == before
    current = diagnostics(store, job)
    assert current["repair_attempted"] is True and current["repair_outcome"] == "succeeded"
    assert current["counts"]["skipped_records"] == 1 and current["counts"]["written_records"] == 0
    assert set(current["rejection_counts"].values()) == {0}


@pytest.mark.parametrize("text", [short_source(), "a" * 12000, "".join(f"{i:05d}|" for i in range(2000))],
                         ids=["short-literal", "repeated-windows", "full-visible-source"])
def test_evidence_grammar_is_bounded_unique_literal_and_does_not_mutate_the_static_schema(text):
    original = copy.deepcopy(agent_module.EXTRACTION_SCHEMA)
    schema = agent_module._extraction_schema(text)
    Draft202012Validator.check_schema(schema)
    passages = evidence_property(schema)["enum"]
    assert 1 <= len(passages) <= 38
    assert len(passages) == len(set(passages))
    assert all(isinstance(value, str) and value.strip() and len(value) <= 400 and value in text
               for value in passages)
    assert agent_module.EXTRACTION_SCHEMA == original
    assert "enum" not in evidence_property(original)
    if len(text) <= 400:
        assert passages == [text]
    if len(set(text)) > 1 and len(text) == 12000:
        assert passages[0] == text[:400] and passages[1] == text[320:720]
        coverage = set()
        for value in passages:
            start = text.index(value)
            coverage.update(range(start, start + len(value)))
        assert coverage == set(range(len(text)))
    # A caller cannot poison the next extraction by mutating its private schema.
    evidence_property(schema)["enum"].append(REJECTED_CANARY)
    assert REJECTED_CANARY not in json.dumps(agent_module.EXTRACTION_SCHEMA)
    assert REJECTED_CANARY not in json.dumps(agent_module._extraction_schema(text))


async def test_source_suffix_outside_the_visible_budget_never_enters_generation(workspace):
    store, project, settings = workspace
    visible = "".join(f"{i:05d}|" for i in range(2000))
    hidden = "OUTSIDE_VISIBLE_SOURCE_BUDGET_PRIVATE_CANARY"
    item = snapshot(store, project, visible + hidden)
    job = claimed_job(store, project, item)
    model = ControlledOllama([output()])
    await KnowledgeAgent(store, settings, model).extract(job)
    assert len(model.calls) == 1
    call = model.calls[0]
    assert hidden not in json.dumps(call)
    payload = json.loads(call["messages"][-1]["content"])
    assert payload["text"] == visible
    passages = evidence_property(call["kwargs"]["format"])["enum"]
    assert len(passages) <= 38 and all(value in visible and len(value) <= 400 for value in passages)
    assert store.list_records(project["id"]) == []


async def test_backend_ignoring_the_grammar_cannot_write_any_of_six_absent_quotes(workspace):
    store, project, settings = workspace
    text = short_source()
    item = snapshot(store, project, text)
    job = claimed_job(store, project, item)
    before = memory_state(store)
    quotes = [REJECTED_CANARY * 4, *(f"{REJECTED_CANARY}-{i}" for i in range(1, 6))]
    assert len(quotes[0]) > len(text)
    model = ControlledOllama([output(*(record(quote, key=f"absent-{i}", content=f"Invented claim {i}.")
                                      for i, quote in enumerate(quotes)))])
    await KnowledgeAgent(store, settings, model).extract(job)
    assert len(model.calls) == 1
    assert memory_state(store) == before
    current = diagnostics(store, job)
    assert current["repair_attempted"] is False
    assert current["counts"] == {"candidate_records": 6, "accepted_records": 0,
                                  "skipped_records": 6, "written_records": 0}
    assert current["rejection_counts"] == {"blank_content": 0, "blank_evidence": 0,
                                            "quote_absent_source": 6, "reserved_metadata": 0}
    assert {"quote_absent_source", "proposal_rejected"} <= set(current["validation_codes"])
    assert REJECTED_CANARY not in json.dumps(current)


async def test_rejection_reasons_overlap_honestly_without_double_counting_skipped_records(workspace):
    store, project, settings = workspace
    text = short_source()
    item = snapshot(store, project, text)
    job = claimed_job(store, project, item)
    proposals = [
        record(text, key="blank", content=" \t "),
        record(" \t", key="blank-evidence"),
        record(text, key="blank-reserved", content=" \n", metadata={"plan_ref": REJECTED_CANARY}),
        record(" \t", key="evidence-reserved", metadata={"machine_verified": True}),
        record(REJECTED_CANARY, key="absent-reserved", metadata={"test_passed": True}),
        record(text, key="valid", content="A valid fact survives the rejected candidates."),
    ]
    model = ControlledOllama([output(*proposals)])
    await KnowledgeAgent(store, settings, model).extract(job)
    records = store.list_records(project["id"])
    assert len(records) == 1 and records[0]["key"] == "valid"
    assert records[0]["metadata"]["evidence"] == text
    assert len(model.calls) == 1
    current = diagnostics(store, job)
    assert current["repair_attempted"] is False
    assert current["counts"] == {"candidate_records": 6, "accepted_records": 1,
                                  "skipped_records": 5, "written_records": 1}
    assert current["rejection_counts"] == {"blank_content": 2, "blank_evidence": 2,
                                            "quote_absent_source": 3, "reserved_metadata": 3}
    assert set(current["rejection_counts"]) | {"proposal_rejected"} <= set(current["validation_codes"])
    assert REJECTED_CANARY not in json.dumps(current)


@pytest.mark.parametrize("text", [short_source(), " \n\t "], ids=["known-source", "empty-visible-source"])
async def test_empty_extraction_is_valid_and_empty_visible_source_has_no_invalid_empty_enum(workspace, text):
    store, project, settings = workspace
    item = snapshot(store, project, text)
    job = claimed_job(store, project, item)
    model = ControlledOllama([output()])
    await KnowledgeAgent(store, settings, model).extract(job)
    assert len(model.calls) == 1 and store.list_records(project["id"]) == []
    schema = model.calls[0]["kwargs"]["format"]
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid({"records": []})
    if not text.strip():
        assert schema["properties"]["records"]["maxItems"] == 0
        assert "enum" not in evidence_property(schema)
    current = diagnostics(store, job)
    assert current["repair_attempted"] is False and current["counts"]["written_records"] == 0
