"""The stamp of the extractor on every record automatic extraction writes (#192).

``metadata.extractor`` holds the hand-bumped extractor version, a hash of the prompt and the schema
and the model as name@digest. Only the extraction worker sets it; a model reply, a client write or an
update cannot. It describes how a record was made, not who trusts it, so it travels in portable memory.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import subprocess
import time
from pathlib import Path

import pytest
from conftest import as_person

import cairnmind.agents.agent as agent_module
import cairnmind.agents.extraction_input as extraction_input
import cairnmind.memory.memory as memory_module
import cairnmind.memory.memory_models as memory_models
from cairnmind.agents.agent import KnowledgeAgent
from cairnmind.agents.extraction_evaluation import run_extraction_evaluation
from cairnmind.agents.extraction_input import EXTRACTOR_VERSION, extractor_fingerprint, extractor_stamp
from cairnmind.agents.ollama import Ollama, OllamaUnavailable
from cairnmind.agents.retrieval import public_record
from cairnmind.config import Settings
from cairnmind.memory.memory import MemoryManager
from cairnmind.store import EXTRACTOR_MODEL_CHARS, Store, validate_extractor_stamp

# One line per extractor version. Changing an instruction or the schema changes the fingerprint: bump
# EXTRACTOR_VERSION in extraction_input.py and add its fingerprint here; never edit an earlier line.
PINNED_FINGERPRINTS = {
    1: "47babfb3205ae8de11ee144c3acab4797d532ff03bd60b2087a1242996fcbfea",
    2: "07e52e9bcab68c5b351cc10cf6a25abcab893f48ac5e0f30b5186316403e63da",
}

QUOTE = "Use auth.py for explicit authentication."
DIGEST = "a" * 64


def test_the_prompt_and_schema_cannot_change_without_a_version_bump():
    assert EXTRACTOR_VERSION in PINNED_FINGERPRINTS, "add the fingerprint of the new extractor version"
    assert extractor_fingerprint() == PINNED_FINGERPRINTS[EXTRACTOR_VERSION], (
        "an extraction instruction or the schema changed: bump EXTRACTOR_VERSION and pin the new fingerprint")
    assert sorted(PINNED_FINGERPRINTS) == list(range(1, EXTRACTOR_VERSION + 1))


@pytest.mark.parametrize("name", ["SYSTEM", "CONVERSATION_RULE", "ASSISTANT_RULE", "TASK_STATE_INSTRUCTION",
                                  "REPAIR_INSTRUCTION", "REPAIR_FIT_TRUNCATED", "REPAIR_FIT", "REPAIR_RULE"])
def test_every_instruction_is_part_of_the_fingerprint(monkeypatch, name):
    before = extractor_fingerprint()
    monkeypatch.setattr(extraction_input, name, getattr(extraction_input, name) + " Changed.")
    assert extractor_fingerprint() != before


def test_the_schema_and_the_passage_windows_are_part_of_the_fingerprint(monkeypatch):
    before = extractor_fingerprint()
    schema = copy.deepcopy(extraction_input.EXTRACTION_SCHEMA)
    schema["properties"]["records"]["maxItems"] = 7
    monkeypatch.setattr(extraction_input, "EXTRACTION_SCHEMA", schema)
    assert extractor_fingerprint() != before
    monkeypatch.undo()
    monkeypatch.setattr(extraction_input, "PASSAGE_CHARS", 401)
    assert extractor_fingerprint() != before


@pytest.mark.parametrize("name, value", [
    ("REPAIR_DRAFT_CHARS", 2001), ("EXTRACTION_TOKENS", 1801), ("REPAIR_TOKENS", 3601),
    # How the prompt is built and which messages are skipped, not only the named instructions.
    ("public_source_metadata", lambda metadata: {}),
    ("HOST_MESSAGE", re.compile(r"(?!)")),
    ("_without_tool_calls", lambda source: None),
    ("_without_own_block", lambda segments: segments),
])
def test_the_built_request_the_correction_and_the_budgets_are_part_of_the_fingerprint(monkeypatch, name, value):
    before = extractor_fingerprint()
    monkeypatch.setattr(extraction_input, name, value)
    assert extractor_fingerprint() != before


def test_the_extract_job_sends_the_correction_and_the_budgets_the_fingerprint_covers(workspace):
    store, _, source, job, settings = workspace
    calls = []

    class Invalid(Model):
        async def chat(self, messages, *args, **kwargs):
            calls.append((messages, kwargs))
            if len(calls) == 1:
                return {"message": {"content": "not json " * 400}, "done_reason": "length"}
            return await super().chat(messages, *args, **kwargs)

    asyncio.run(KnowledgeAgent(store, settings, ollama=Invalid()).extract(job))
    request = extraction_input.extraction_request(store.get_source(source["id"]))
    assert json.loads(calls[1][0][1]["content"]) == extraction_input.repair_prompt(
        source["id"], request["text"], "output_truncated", "not json " * 400)
    assert calls[0][1]["options"]["num_predict"] == min(extraction_input.EXTRACTION_TOKENS, settings.answer_tokens)
    assert calls[1][1]["options"]["num_predict"] <= extraction_input.REPAIR_TOKENS
    assert calls[0][1]["format"] == extraction_input.extraction_schema(request["text"], request["segments"])


@pytest.mark.parametrize("name", ["", "m" * 395, "m" * 1000, "qwen3.5:9b", "a@b"])
@pytest.mark.parametrize("digest", [DIGEST, "sha256:" + "f" * 128, None])
def test_the_stamp_is_always_one_the_store_accepts(name, digest):
    stamp = extractor_stamp(name, digest)
    assert validate_extractor_stamp(stamp) == stamp
    assert stamp["model"].endswith("@" + (digest or "unknown"))
    if name and len(name) + 1 + len(digest or "unknown") <= EXTRACTOR_MODEL_CHARS:
        assert stamp["model"].startswith(name + "@")
    # Two long names that share their start still get different stamps.
    assert extractor_stamp("m" * 999 + "x", digest)["model"] != extractor_stamp("m" * 999 + "y", digest)["model"]


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = Store(tmp_path / "memory")
    project = store.add_project("extractor-stamp", root)
    source = store.snapshot(project["id"], "note://policy", "Raw archived policy envelope.", "note",
                            metadata={"normalized_text": QUOTE, "role": "user"})
    store.enqueue("extract", project["id"], {"source_id": source["id"]}, "policy")
    job = store.claim_job("stamp-worker", lease_seconds=120, kinds=["extract"])
    return store, project, source, job, Settings(data_dir=store.data_dir)


class Model:
    """A chat that proposes one grounded record, and a model list with or without a digest."""

    def __init__(self, digest=DIGEST, metadata=None):
        self.digest = digest
        self.metadata = metadata or {}

    async def chat(self, messages, *_args, **_kwargs):
        prompt = json.loads(messages[1]["content"])
        evidence = QUOTE if QUOTE in prompt.get("text", "") else prompt["text"][:400]
        return {"message": {"content": json.dumps({"records": [{
            "kind": "decision", "key": None, "content": "Decided: " + evidence[:200],
            "evidence": evidence, "metadata": self.metadata}]})}, "done_reason": "stop"}

    async def model_digest(self, name):
        if isinstance(self.digest, Exception):
            raise self.digest
        return self.digest


class NoDigestLookup:
    """An injected adapter from before this change: it has no model_digest at all."""

    async def chat(self, messages, *args, **kwargs):
        return await Model().chat(messages, *args, **kwargs)


def expected(settings, digest=DIGEST):
    return {"version": EXTRACTOR_VERSION, "prompt_sha256": extractor_fingerprint()[:16],
            "model": f"{settings.chat_model}@{digest or 'unknown'}"}


def extracted(store, project):
    return [row for row in store.list_records(project["id"])
            if row["metadata"].get("origin") == "automatic_extraction"]


async def test_an_extract_job_stamps_every_record_with_version_prompt_and_model(workspace):
    store, project, source, job, settings = workspace
    await KnowledgeAgent(store, settings, ollama=Model()).extract(job)
    [record] = extracted(store, project)
    assert record["metadata"]["extractor"] == expected(settings)
    assert record["metadata"]["extracted_by"] == settings.chat_model
    # The #168 mark is a different, local-only mark, and stays as it was.
    assert record["metadata"]["extraction_source_id"] == source["id"]
    [revision] = store.record_history(record["id"])
    assert revision["metadata"]["extractor"] == expected(settings)


@pytest.mark.parametrize("model", [Model(digest=None), Model(digest=OllamaUnavailable("down")),
                                   Model(digest=RuntimeError("odd")), NoDigestLookup()],
                         ids=["no-digest", "list-unavailable", "lookup-error", "no-lookup"])
async def test_a_model_without_a_digest_is_written_as_unknown_and_never_fails_extraction(workspace, model):
    store, project, _, job, settings = workspace
    await KnowledgeAgent(store, settings, ollama=model).extract(job)
    [record] = extracted(store, project)
    assert record["metadata"]["extractor"] == expected(settings, digest=None)
    assert store.job_status(project["id"], job["id"])["diagnostics"]["code"] == "ok"


def test_a_slow_model_list_near_the_deadline_never_costs_the_extraction(workspace, monkeypatch):
    store, project, _, job, settings = workspace
    # The model uses half the time limit; a lookup allowed its own 10 s would outlast the rest.
    monkeypatch.setattr(agent_module, "EXTRACTION_TIMEOUT", 6.0)

    class Slow(Model):
        async def chat(self, messages, *args, **kwargs):
            await asyncio.sleep(3)
            return await super().chat(messages, *args, **kwargs)

        async def model_digest(self, name):
            await asyncio.sleep(30)
            return DIGEST

    asyncio.run(KnowledgeAgent(store, settings, ollama=Slow()).extract(job))
    [record] = extracted(store, project)
    assert record["metadata"]["extractor"] == expected(settings, digest=None)
    assert store.job_status(project["id"], job["id"])["diagnostics"]["code"] == "ok"


async def test_no_lookup_is_made_when_the_deadline_leaves_no_room(tmp_path):
    asked = []

    class Counting(Model):
        async def model_digest(self, name):
            asked.append(name)
            return DIGEST

    agent = KnowledgeAgent(None, Settings(data_dir=tmp_path), ollama=Counting())
    assert (await agent.extractor_stamp(time.monotonic()))["model"].endswith("@unknown")
    assert asked == []
    assert (await agent.extractor_stamp(time.monotonic() + 60))["model"].endswith("@" + DIGEST)


def test_a_long_chat_model_name_still_extracts_with_a_bounded_stamp(tmp_path):
    (tmp_path / "project").mkdir()
    store = Store(tmp_path / "memory")
    project = store.add_project("long-name", tmp_path / "project")
    source = store.snapshot(project["id"], "note://policy", QUOTE, "note")
    store.enqueue("extract", project["id"], {"source_id": source["id"]}, "policy")
    job = store.claim_job("stamp-worker", lease_seconds=120, kinds=["extract"])
    settings = Settings(data_dir=store.data_dir, chat_model="m" * 395)
    asyncio.run(KnowledgeAgent(store, settings, ollama=Model()).extract(job))
    [record] = extracted(store, project)
    stamp = record["metadata"]["extractor"]
    assert stamp == extractor_stamp(settings.chat_model, DIGEST)
    assert len(stamp["model"]) <= EXTRACTOR_MODEL_CHARS and stamp["model"].startswith("m" * 100)


async def test_a_model_reply_cannot_set_the_stamp(workspace):
    store, project, _, job, settings = workspace
    forged = {"version": 99, "prompt_sha256": "f" * 16, "model": "forged@" + "b" * 64}
    await KnowledgeAgent(store, settings, ollama=Model(metadata={"extractor": forged})).extract(job)
    assert extracted(store, project) == []
    assert store.job_status(project["id"], job["id"])["diagnostics"]["rejection_counts"]["reserved_metadata"] == 1


def test_a_client_can_neither_set_nor_change_nor_clear_the_stamp(workspace):
    store, project, source, _, settings = workspace
    stamp = expected(settings)
    forged = {"version": 99, "prompt_sha256": "f" * 16, "model": "forged@" + "b" * 64}
    own = store.remember(project["id"], {"kind": "fact", "key": "client", "content": "A client fact.",
                                         "metadata": {"extractor": forged, "origin": "automatic_extraction"}},
                         [source["id"]])
    assert "extractor" not in own["metadata"]
    record = store.remember(project["id"], {"kind": "fact", "key": "worker", "content": "A worker fact.",
                                            "status": "proposed"}, [source["id"]],
                            extracted_from=source["id"], extractor=stamp)
    assert record["metadata"]["extractor"] == stamp
    # Confirmed the way a person does (#177): the store refuses that status from any other write.
    changed = as_person(store, project["id"], {"id": record["id"], "expected_version": 1,
                                               "metadata": {"extractor": forged}}, [source["id"]])
    assert changed["status"] == "confirmed"
    # A review or an edit keeps how the record was made; the local #168 mark is dropped as before.
    assert changed["metadata"]["extractor"] == stamp and "extraction_source_id" not in changed["metadata"]
    cleared = store.remember(project["id"], {"id": record["id"], "expected_version": 2,
                                             "metadata": {"extractor": None}}, [source["id"]])
    assert cleared["metadata"]["extractor"] == stamp
    with pytest.raises(ValueError, match="extractor stamp"):
        store.remember(project["id"], {"kind": "fact", "key": "other", "content": "Other."}, [source["id"]],
                       extractor=stamp)


@pytest.mark.parametrize("stamp", [
    {"version": 0, "prompt_sha256": "a" * 16, "model": "m@x"},
    {"version": True, "prompt_sha256": "a" * 16, "model": "m@x"},
    {"version": 1, "prompt_sha256": "A" * 16, "model": "m@x"},
    {"version": 1, "prompt_sha256": "a" * 16, "model": "no-at"},
    {"version": 1, "prompt_sha256": "a" * 16, "model": "m@x", "extra": 1},
    "1",
])
def test_the_store_refuses_a_malformed_stamp(workspace, stamp):
    store, project, source, _, _ = workspace
    with pytest.raises(ValueError, match="extractor stamp"):
        store.remember(project["id"], {"kind": "fact", "key": "bad", "content": "Bad."}, [source["id"]],
                       extracted_from=source["id"], extractor=stamp)


def test_clients_see_the_stamp_in_the_bounded_record_view():
    stamp = {"version": 1, "prompt_sha256": "a" * 16, "model": "qwen3.5:9b@" + DIGEST}
    view = public_record({"id": "r", "kind": "fact", "content": "A fact.",
                          "metadata": {"origin": "automatic_extraction", "extractor": {**stamp, "junk": {"x": 1}}}})
    assert view["metadata"]["extractor"] == stamp


async def test_ollama_reports_a_listed_digest_and_none_for_anything_else(tmp_path):
    model = Ollama(Settings(data_dir=tmp_path))
    model._model_cache = (time.monotonic(), {"models": [
        {"name": "listed:1b", "digest": DIGEST}, {"name": "plain:1b"},
        {"name": "odd:1b", "digest": "../../etc"}, "not-a-row"]})
    assert await model.model_digest("listed:1b") == DIGEST
    # A row without a digest is what a server other than Ollama lists (memory_openai.OpenAICompatible).
    assert await model.model_digest("plain:1b") is None
    assert await model.model_digest("odd:1b") is None
    assert await model.model_digest("missing:1b") is None


def test_the_extraction_evaluation_report_carries_the_same_stamp_as_the_records(tmp_path, workspace):
    store, project, _, job, settings = workspace
    path = tmp_path / "set.json"
    path.write_text(json.dumps({"format_version": 1, "transcripts": [{"id": "t", "client": "api", "events": [
        {"event": {"role": "user", "content": QUOTE},
         "expect": {"label": "kept", "keep": [{"id": "auth", "kind": "decision", "match": ["auth"]}]}}]}]}),
        encoding="utf-8")

    class Listed(Model):
        async def model_identity(self, name):
            return f"{name}@{DIGEST}"

    report = run_extraction_evaluation(settings, path, runs=1, report_dir=tmp_path / "reports", ollama=Listed())
    asyncio.run(KnowledgeAgent(store, settings, ollama=Listed()).extract(job))
    [record] = extracted(store, project)
    assert report["extractor"] == record["metadata"]["extractor"] == expected(settings)
    assert json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))["extractor"] == report["extractor"]


def test_an_unavailable_model_still_writes_the_stamp_into_a_report_that_did_not_run(tmp_path):
    class Unlisted(Model):
        async def model_identity(self, name):
            raise OllamaUnavailable("not installed")

    path = tmp_path / "set.json"
    path.write_text(json.dumps({"format_version": 1, "transcripts": [{"id": "t", "client": "api", "events": [
        {"event": {"role": "user", "content": QUOTE},
         "expect": {"label": "kept", "keep": [{"id": "auth", "kind": "decision", "match": ["auth"]}]}}]}]}),
        encoding="utf-8")
    settings = Settings(data_dir=tmp_path / "data")
    report = run_extraction_evaluation(settings, path, runs=1, report_dir=tmp_path / "reports",
                                       ollama=Unlisted(digest=None))
    assert report["status"] == "not_run" and report["extractor"] == expected(settings, digest=None)


def _repository(root):
    root.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.name", "Stamp fixture"),
                 ("config", "user.email", "stamp@example.invalid")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "app.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True, capture_output=True)
    return root


def test_the_stamp_survives_portable_memory_export_and_import_without_the_local_mark(tmp_path):
    root = _repository(tmp_path / "origin-repository")
    store = Store(tmp_path / "origin-database")
    project = store.add_project("stamp-portable", root)
    source = store.snapshot(project["id"], "note://decision", QUOTE, "note")
    stamp = {"version": 1, "prompt_sha256": "a" * 16, "model": "qwen3.5:9b@" + DIGEST}
    record = store.remember(project["id"], {"kind": "decision", "key": "auth", "content": "Use auth.py.",
                                            "metadata": {"origin": "automatic_extraction"}},
                            [source["id"]], extracted_from=source["id"], extractor=stamp)
    published = MemoryManager(store).snapshot(project["id"])
    target_root = tmp_path / "target-repository"
    subprocess.run(["git", "clone", "-q", str(root), str(target_root)], check=True, capture_output=True)
    target = Store(tmp_path / "target-database")
    result = MemoryManager(target).import_snapshot(Path(published["manifest_path"]), target_root)
    assert result["status"] == "imported"
    restored = next(row for row in target.list_records(project["id"]) if row["id"] == record["id"])
    assert restored["metadata"]["extractor"] == stamp
    assert "extraction_source_id" not in restored["metadata"]
    [revision] = target.record_history(record["id"])
    assert revision["metadata"]["extractor"] == stamp


def test_without_an_adapter_the_evaluation_uses_the_client_of_the_extract_job(tmp_path, monkeypatch):
    built = []

    class Profiled(Model):
        async def model_identity(self, name):
            return f"{name}@{DIGEST}"

    def from_settings(settings, **_kwargs):
        built.append(settings)
        return Profiled(digest=None)

    monkeypatch.setattr(memory_models, "from_settings", from_settings)
    path = tmp_path / "set.json"
    path.write_text(json.dumps({"format_version": 1, "transcripts": [{"id": "t", "client": "api", "events": [
        {"event": {"role": "user", "content": QUOTE},
         "expect": {"label": "kept", "keep": [{"id": "auth", "kind": "decision", "match": ["auth"]}]}}]}]}),
        encoding="utf-8")
    settings = Settings(data_dir=tmp_path / "data")
    report = run_extraction_evaluation(settings, path, runs=1, report_dir=tmp_path / "reports")
    # A server that lists no digest (an OpenAI-compatible profile) gives name@unknown here as on the records.
    assert built == [settings] and report["extractor"] == expected(settings, digest=None)
    assert report["inference"] == "local_ollama"


@pytest.mark.parametrize("stamp, origin", [
    ({"version": 99, "prompt_sha256": "f" * 16, "model": "x@" + DIGEST, "extra": 1}, "automatic_extraction"),
    ("1@unknown", "automatic_extraction"),
    ({"version": 1, "prompt_sha256": "a" * 16, "model": "m@" + DIGEST}, "api"),
    ({"version": 1, "prompt_sha256": "a" * 16, "model": "m@" + DIGEST}, None),
])
def test_a_package_cannot_carry_a_stamp_the_worker_could_not_have_written(stamp, origin):
    metadata = {"extractor": stamp, **({"origin": origin} if origin else {})}
    record = {"id": "r", "metadata": dict(metadata)}
    revision = {"id": "v", "snapshot": {"id": "r", "metadata": dict(metadata)}}
    memory_module._strip_local_marks("records", record)
    memory_module._strip_local_marks("record_revisions", revision)
    assert "extractor" not in record["metadata"] and "extractor" not in revision["snapshot"]["metadata"]
    # The portable state hash reads rows the same way, so a package and the store compare equal.
    assert "extractor" not in memory_module._logical("records", {"id": "r", "metadata": dict(metadata)})["metadata"]


def test_a_package_keeps_a_stamp_the_worker_could_have_written():
    stamp = {"version": 1, "prompt_sha256": "a" * 16, "model": "m@" + DIGEST}
    record = {"id": "r", "metadata": {"extractor": stamp, "origin": "automatic_extraction",
                                      "extraction_source_id": "s"}}
    memory_module._strip_local_marks("records", record)
    assert record["metadata"] == {"extractor": stamp, "origin": "automatic_extraction"}
