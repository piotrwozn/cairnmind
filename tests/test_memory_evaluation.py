"""Positive extraction and portable recall use real isolated ArcadeDB stores."""
from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from cairnmind.config import Settings
from cairnmind.memory_evaluation import run_memory_evaluation
from cairnmind.ollama import OllamaUnavailable
from cairnmind.store import Store


class MemoryFixtureOllama:
    """Answers solely from the literal source/hits supplied on each call."""

    def __init__(self, *, extraction="valid", wrong_recall=False, malformed_unknown=False):
        self.extraction = extraction
        self.wrong_recall = wrong_recall
        self.malformed_unknown = malformed_unknown
        self.extraction_calls = 0
        self.recall_calls = 0
        self.schemas = []

    async def model_identity(self, name):
        return name + "@memory-controlled-fixture"

    async def embed(self, texts):
        return [[1., .2, .1, .3] for _ in texts]

    async def chat(self, messages, **kwargs):
        prompt = json.loads(messages[1]["content"])
        if "text" in prompt and "source_id" in prompt:
            self.extraction_calls += 1
            text = prompt["text"]
            kind = re.search(r"memory_(fact|decision|task)_", text)[1]
            metadata = {"state": "needs_test"} if kind == "task" else {}
            evidence = "An invented quote absent from the source." if self.extraction == "rejected" else text
            records = [] if self.extraction == "empty" else [{
                "kind": kind, "key": re.search(r"memory_\w+", text)[0], "content": text,
                "evidence": evidence, "metadata": metadata}]
            content = {"records": records}
        else:
            self.recall_calls += 1
            self.schemas.append(prompt.get("answer_schema"))
            question = prompt["question"]
            hits = list(prompt.get("initial_evidence", []))
            hits += [{"source_id": item["source_id"], "text": item["quote_excerpt"]}
                     for item in prompt.get("citation_catalog", [])]
            for message in messages[2:]:
                if message.get("role") == "tool":
                    hits += json.loads(message["content"]).get("hits", [])
            case = re.search(r"memory_(fact|decision|task)_[a-f0-9]+", question)
            selected = next((hit for hit in hits if case and case[0] in hit.get("text", "")), None)
            if case and selected is None:
                return {"message": {"content": "", "tool_calls": [{"function": {
                    "name": "search", "arguments": {"query": case[0]}}}]},
                    "prompt_eval_count": 80, "eval_count": 12}
            if selected:
                text = selected["text"]
                if case[1] == "fact":
                    answer = {"port": int(re.search(r"TCP port (\d+)", text)[1])}
                elif case[1] == "decision":
                    answer = {"engine": "WrongEngine" if self.wrong_recall else
                              re.search(r"uses (\w+) as its database", text)[1]}
                else:
                    answer = {"state": re.search(r"current task state is (\w+)", text)[1]}
                content = {"answer": answer, "citations": [{"source_id": selected["source_id"], "quote": text}],
                           "unknowns": []}
            else:
                if self.malformed_unknown:
                    return {"message": {"content": "This is not the requested JSON envelope."},
                            "prompt_eval_count": 80, "eval_count": 30}
                citation = [{"source_id": hits[0]["source_id"], "quote": hits[0]["text"]}] if hits else []
                content = {"answer": {"answer": "unknown"}, "citations": citation,
                           "unknowns": ["This project's evidence does not contain another project's secret."]}
        return {"message": {"content": json.dumps(content)}, "done_reason": "stop",
                "prompt_eval_count": 80, "eval_count": 30}


class MissingMemoryModels:
    async def model_identity(self, name):
        raise OllamaUnavailable("Required fixture model is not installed")


class MidrunUnavailable(MemoryFixtureOllama):
    async def embed(self, texts):
        raise OllamaUnavailable("Synthetic model became unavailable after extraction")


def test_positive_extraction_then_portable_recall_preserves_user_data_and_uses_separate_namespaces(tmp_path, monkeypatch):
    import cairnmind.memory_evaluation as evaluation

    settings = Settings.load(tmp_path / "user-data", create=True)
    store = Store(settings.data_dir)
    user_root = tmp_path / "user-project"
    user_root.mkdir()
    project = store.add_project("Real user project", user_root)
    user_canary = "USER_CONTENT_NEVER_BELONGS_IN_SYNTHETIC_EVALUATION"
    store.snapshot(project["id"], "manual://user", user_canary, "manual")
    before = store.export()
    monkeypatch.setenv("DEVBRAIN_ARCADE_DATABASE", settings.arcade_database)
    isolated = []

    def tracked_store(data_dir, **kwargs):
        isolated.append(kwargs["settings"].arcade_database)
        return Store(data_dir, **kwargs)

    monkeypatch.setattr(evaluation, "Store", tracked_store)
    model = MemoryFixtureOllama()
    report = run_memory_evaluation(settings, ollama=model)
    assert report["status"] == "passed", json.dumps(report, indent=2)
    assert len(isolated) == len(set(isolated)) == 2
    assert settings.arcade_database not in isolated
    assert model.extraction_calls == 3
    assert len(report["extraction"]) == len(report["recall"]) == 3
    assert all(row["status"] == "passed" for row in report["extraction"] + report["recall"])
    assert all(report["deterministic_checks"].values())
    assert report["inference"] == "injected_adapter"
    assert report["isolation"]["databases_cleaned"] is True
    assert report["isolation"]["native_clients_started"] is False
    path = Path(report["report_path"])
    assert path.is_file() and path.stat().st_mode & 0o777 == 0o600
    serialized = path.read_text()
    assert user_canary not in serialized
    assert settings.token not in serialized and settings.arcade_password not in serialized
    assert "ArcadeDB" not in json.dumps(model.schemas)
    assert "needs_test" not in json.dumps(model.schemas)
    after = store.export()
    before.pop("exported_at")
    after.pop("exported_at")
    assert before == after
    names = store.client._request("GET", "/api/v1/databases").json()["result"]
    assert all(name not in names for name in isolated)


@pytest.mark.parametrize("mode", ["empty", "rejected"])
def test_valid_empty_or_all_rejected_jobs_fail_a_positive_extraction_fixture(tmp_path, mode):
    settings = Settings.load(tmp_path / "data", create=True)
    report = run_memory_evaluation(settings, ollama=MemoryFixtureOllama(extraction=mode))
    assert report["status"] == "failed"
    assert report["failure_stage"] == "positive_extraction"
    assert len(report["extraction"]) == 3
    assert all(row["status"] == "failed" for row in report["extraction"])
    assert all(row["diagnostics"]["counts"]["written_records"] == 0 for row in report["extraction"])
    assert report["recall"] == []
    assert report["isolation"]["databases_cleaned"] is True


def test_wrong_cited_recall_fails_even_after_three_successful_extractions(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    report = run_memory_evaluation(settings, ollama=MemoryFixtureOllama(wrong_recall=True))
    assert report["status"] == "failed"
    assert all(row["status"] == "passed" for row in report["extraction"])
    assert [row["id"] for row in report["recall"] if row["status"] == "failed"] == ["decision"]


def test_runtime_unavailable_after_extraction_is_failed_not_not_run(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    report = run_memory_evaluation(settings, ollama=MidrunUnavailable())
    assert report["status"] == "failed"
    assert all(row["status"] == "passed" for row in report["extraction"])
    assert report["failure_stage"] == "recall"
    assert report["isolation"]["databases_cleaned"] is True


def test_malformed_unknown_fallback_does_not_pass_abstention(tmp_path):
    settings = Settings.load(tmp_path / "data", create=True)
    report = run_memory_evaluation(settings, ollama=MemoryFixtureOllama(malformed_unknown=True))
    assert report["status"] == "failed"
    assert all(row["status"] == "passed" for row in report["recall"])
    assert report["deterministic_checks"]["cross_project_abstention"] is False


def test_missing_models_are_not_run_without_creating_fixture_databases(tmp_path, monkeypatch):
    import cairnmind.memory_evaluation as evaluation

    def forbidden_store(*args, **kwargs):
        raise AssertionError("Missing-model preflight must not create a database")

    monkeypatch.setattr(evaluation, "Store", forbidden_store)
    settings = Settings(data_dir=tmp_path / "data")
    report = run_memory_evaluation(settings, ollama=MissingMemoryModels())
    assert report["status"] == "not_run"
    assert report["failure_stage"] == "model_availability"
    assert report["extraction"] == report["recall"] == []
    assert report["isolation"]["databases_cleaned"] is True
