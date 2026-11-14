"""What extraction reads from a conversation (#160): protocol turns left out, rules for the rest."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from cairnmind.agent import KnowledgeAgent
from cairnmind.config import Settings
from cairnmind.extraction_evaluation import load_transcripts, transcript_sources
from cairnmind.extraction_input import ASSISTANT_RULE, CONVERSATION_RULE, SYSTEM, extraction_request
from cairnmind.integrations import launch_prompt

SET = Path(__file__).resolve().parents[1] / "docs" / "evaluation" / "extraction-transcripts.json"


def agent_message(role, content, **data):
    event = {"type": "message", "seq": 1, "data": {"role": role, "content": content, **data}}
    return {"id": "s1", "project_id": "p", "kind": "agent_transcript", "content": json.dumps(event),
            "metadata": {"client": "api", "role": role, "normalized_text": content}}


def claude_line(role, blocks):
    from cairnmind.ingest import _event_text
    event = {"type": role, "sessionId": "s", "message": {"role": role, "content": blocks}}
    text, parsed_role, _, _ = _event_text(event, "auto")
    return {"id": "s2", "project_id": "p", "kind": "transcript", "content": json.dumps(event),
            "metadata": {"client": "claude", "role": parsed_role, "normalized_text": text}}


def test_a_note_or_document_is_read_exactly_as_before():
    note = {"id": "n", "project_id": "p", "kind": "manual", "content": "We use ArcadeDB.", "metadata": {"role": "user"}}
    request = extraction_request(note)
    assert request["system"] == SYSTEM and request["text"] == "We use ArcadeDB."
    assert extraction_request({**note, "kind": "agent_answer"}) is None


def test_a_conversation_message_gets_the_rule_and_assistant_lines_the_higher_bar():
    person = extraction_request(agent_message("user", "We decided to store originals in object storage."))
    assert person["system"] == SYSTEM + CONVERSATION_RULE
    assistant = extraction_request(agent_message("assistant", "I'll call resume_context first.",
                                                 tool_calls=[{"id": "c", "name": "resume_context", "arguments": {}}]))
    assert assistant["system"] == SYSTEM + CONVERSATION_RULE + ASSISTANT_RULE
    # The built-in agent's text is given as written: its tool calls were never part of it.
    assert assistant["text"] == "I'll call resume_context first."


def test_the_launchers_first_message_is_left_out_but_a_person_naming_the_protocol_is_not():
    launched = launch_prompt("4c2b9e7f1a3d", "/home/dev/.local/share/cairnmind/runtime/packets/4c2b.json")
    assert extraction_request(agent_message("user", launched)) is None
    assert extraction_request(claude_line("user", launched)) is None
    # The same words written by the assistant are not the launcher's message.
    assert extraction_request(agent_message("assistant", launched)) is not None
    # A project about the protocol keeps its records: names of protocol tools are not filtered.
    decision = "Decision: resume_context must return the packet hash, also for a partial packet."
    assert extraction_request(agent_message("user", decision))["text"] == decision
    quoted = "The launcher says: " + launched
    assert extraction_request(agent_message("user", quoted))["text"] == quoted


def test_codex_environment_block_is_left_out_only_when_it_is_the_whole_message():
    block = "<environment_context>\n  <cwd>/home/dev/x</cwd>\n  <shell>bash</shell>\n</environment_context>"
    assert extraction_request(agent_message("user", block)) is None
    mixed = block + "\nWe switched to Alembic."
    assert extraction_request(agent_message("user", mixed))["text"] == mixed


def test_a_claude_tool_call_is_left_out_of_what_extraction_reads():
    only_call = claude_line("assistant", [{"type": "tool_use", "id": "t", "name": "mcp__cairnmind__ack_resume",
                                           "input": {"packet_id": "pkt_1", "packet_hash": "ab"}}])
    assert only_call["metadata"]["normalized_text"].startswith("Tool: mcp__cairnmind__ack_resume")
    assert extraction_request(only_call) is None
    spoken = claude_line("assistant", [{"type": "text", "text": "The scheduler uses local time."},
                                       {"type": "tool_use", "id": "t", "name": "Read", "input": {"path": "a.py"}}])
    request = extraction_request(spoken)
    assert request["text"] == request["prompt"]["text"] == "The scheduler uses local time."
    # Every quote of the reduced text is a literal passage of the archived source.
    assert request["text"] in spoken["metadata"]["normalized_text"]
    plain = claude_line("assistant", [{"type": "text", "text": "Done."}])
    assert extraction_request(plain)["text"] == "Done."


class CountingOllama:
    def __init__(self):
        self.texts = []

    async def chat(self, messages, **kwargs):
        self.texts.append(json.loads(messages[1]["content"])["text"])
        return {"message": {"content": json.dumps({"records": []})}, "done_reason": "stop"}


def test_the_evaluation_sets_host_and_tool_only_lines_never_reach_the_model(tmp_path):
    model = CountingOllama()
    agent = KnowledgeAgent(None, Settings(data_dir=tmp_path), ollama=model)
    skipped = []
    for transcript in load_transcripts(SET):
        for item in transcript_sources(transcript):
            if not item["extracted"]:
                continue
            diagnostics = {"model_calls": [], "validation_codes": [], "rejection_counts": {},
                           "counts": {"candidate_records": 0, "accepted_records": 0, "skipped_records": 0}}
            if asyncio.run(agent.propose(item["source"], diagnostics)) is None:
                assert diagnostics["code"] == "source_skipped"
                skipped.append((transcript["id"], item["index"], item["expect"]["label"]))
    assert skipped == [("claude-session", 0, "host"), ("claude-session", 3, "protocol"),
                       ("codex-session", 0, "host"), ("codex-session", 1, "host")]
    assert not any("Tool: " in text for text in model.texts)
