"""The transcript extraction evaluation (#160): its set, its sources and its scoring, without a model."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from cairnmind.config import Settings
from cairnmind.extraction_evaluation import (load_transcripts, run_extraction_evaluation, score_transcript,
                                             summarize, transcript_sources)
from cairnmind.ollama import OllamaUnavailable

SET = Path(__file__).resolve().parents[1] / "docs" / "evaluation" / "extraction-transcripts.json"


def test_the_shipped_set_is_valid_and_mixes_clients_noise_and_kept_records():
    transcripts = load_transcripts(SET)
    assert {item["client"] for item in transcripts} == {"api", "claude", "codex"}
    labels = [event["expect"]["label"] for item in transcripts for event in item["events"]]
    assert {"kept", "protocol", "host", "one_off", "narration"} <= set(labels)
    kept = [kept for item in transcripts for event in item["events"] for kept in event["expect"]["keep"]]
    assert len(kept) >= 12 and {k["kind"] for k in kept} >= {"decision", "task", "constraint", "fact", "question"}


def test_sources_follow_what_the_product_archives_and_extracts():
    transcripts = {item["id"]: item for item in load_transcripts(SET)}
    agent = transcript_sources(transcripts["agent-trial"])
    assert all(item["source"]["kind"] == "agent_transcript" and item["extracted"] for item in agent)
    narration = agent[0]["source"]
    assert narration["metadata"]["role"] == "assistant"
    assert narration["metadata"]["normalized_text"].startswith("I'll start by calling resume_context")
    assert json.loads(narration["content"])["data"]["tool_calls"][0]["name"] == "resume_context"
    claude = transcript_sources(transcripts["claude-session"])
    # A tool result is a role=user line in Claude's file but never the person's words: not extracted.
    assert [item["extracted"] for item in claude] == [True, True, False, True, True, True, True, True]
    assert claude[3]["source"]["metadata"]["normalized_text"].startswith("Tool: mcp__cairnmind__ack_resume")
    codex = transcript_sources(transcripts["codex-session"])
    assert codex[2]["extracted"] is False and codex[2]["source"]["metadata"]["role"] == "tool"
    assert all(item["source"]["kind"] == "transcript" for item in claude + codex)


def _transcript():
    return {"id": "t", "client": "api", "events": [
        {"event": {"role": "user", "content": "We use ArcadeDB."},
         "expect": {"label": "kept", "keep": [{"id": "db", "kind": "decision", "match": ["arcadedb"]}]}},
        {"event": {"role": "assistant", "content": "Calling resume_context."},
         "expect": {"label": "protocol", "keep": []}},
        {"event": {"role": "user", "content": "Uploads must finish in 300 ms."},
         "expect": {"label": "kept", "keep": [{"id": "budget", "kind": "constraint", "match": ["300", "ms"]}]}},
    ]}


def test_scoring_counts_kept_duplicates_rejected_and_lost():
    result = score_transcript(_transcript(), {
        0: [{"kind": "decision", "content": "The project uses ArcadeDB."}],
        1: [{"kind": "task", "content": "Call resume_context."},
            {"kind": "fact", "content": "The database is arcadedb."}],
        2: []})
    assert [row["verdict"] for row in result["records"]] == ["kept", "rejected", "duplicate"]
    assert result["kept_found"] == ["db"] and result["kept_lost"] == ["budget"]
    summary = summarize([result])
    assert summary | {"proposals_by_label": None} == {
        "proposals": 3, "kept": 1, "rejected": 1, "duplicates": 1, "rejected_share": 0.6667,
        "proposals_by_label": None, "kept_found": 1, "kept_total": 2, "kept_lost": ["budget"]}
    assert summary["proposals_by_label"] == {"kept": 1, "protocol": 2}


@pytest.mark.parametrize("change, message", [
    (lambda data: data.update(format_version=2), "format_version"),
    (lambda data: data["transcripts"][0].update(client="other"), "client"),
    (lambda data: data["transcripts"][0]["events"][0]["expect"].update(keep=[]), "label kept"),
    (lambda data: data["transcripts"][0]["events"][1]["expect"].update(label="kept"), "label kept"),
    (lambda data: data["transcripts"][0]["events"][0]["expect"]["keep"][0].update(match=["("]), "db"),
    (lambda data: data["transcripts"][0]["events"][2]["expect"]["keep"][0].update(id="db"), "unique id"),
])
def test_a_malformed_set_is_refused_naming_the_entry(tmp_path, change, message):
    data = {"format_version": 1, "transcripts": [_transcript()]}
    change(data)
    path = tmp_path / "set.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_transcripts(path)


class EchoOllama:
    """Proposes one record per message quoting its whole text: the worst case of a literal extractor."""

    def __init__(self, unavailable=False):
        self.unavailable = unavailable
        self.calls = []

    async def model_identity(self, name):
        if self.unavailable:
            raise OllamaUnavailable("no model")
        return name + "@fixture"

    async def chat(self, messages, **kwargs):
        prompt = json.loads(messages[1]["content"])
        self.calls.append(copy.deepcopy(prompt))
        record = {"kind": "fact", "key": None, "content": prompt["text"][:200], "evidence": prompt["text"][:400]}
        return {"message": {"content": json.dumps({"records": [record]})}, "done_reason": "stop"}


def test_the_run_scores_every_extracted_message_and_writes_a_private_report(tmp_path):
    path = tmp_path / "set.json"
    path.write_text(json.dumps({"format_version": 1, "transcripts": [_transcript()]}), encoding="utf-8")
    model = EchoOllama()
    report = run_extraction_evaluation(Settings(data_dir=tmp_path / "data"), path, runs=2,
                                       report_dir=tmp_path / "reports", ollama=model)
    # The protocol message produced a record, so the run fails even though nothing was lost.
    assert report["status"] == "failed" and len(report["runs"]) == 2 and len(model.calls) == 6
    assert report["runs"][0]["summary"]["kept_lost"] == [] and report["runs"][0]["summary"]["rejected"] == 1
    written = Path(report["report_path"])
    assert json.loads(written.read_text(encoding="utf-8"))["status"] == "failed"
    assert (written.stat().st_mode & 0o777) == 0o600


def test_a_missing_model_is_not_run_and_bad_run_counts_are_refused(tmp_path):
    report = run_extraction_evaluation(Settings(data_dir=tmp_path / "data"), SET, runs=1,
                                       report_dir=tmp_path / "reports", ollama=EchoOllama(unavailable=True))
    assert report["status"] == "not_run" and report["runs"] == []
    with pytest.raises(ValueError, match="runs"):
        run_extraction_evaluation(Settings(data_dir=tmp_path / "data"), SET, runs=0, ollama=EchoOllama())
