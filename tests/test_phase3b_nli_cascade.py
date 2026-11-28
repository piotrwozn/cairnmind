"""Phase 3b tests: ±4-line context windows for support verification, Tier-1 ONNX NLI triage (ADR-11),
secondary_file escalation, contradiction rejection, offline fail-closed needs_llm_review, and CLI flags.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.agents.agent import KnowledgeAgent
from cairnmind.agents.nli_verifier import (
    NEEDS_LLM_REVIEW_CODE,
    NliScores,
    NliUnavailable,
    OnnxNliScorer,
    evaluate_tier1_nli,
    resolve_nli_model_dir,
)
from cairnmind.agents.ollama import OllamaUnavailable
from cairnmind.agents.support import support_citations, support_messages
from cairnmind.config import Settings

runner = CliRunner()


def _source(quote: str, *, full_text: str | None = None, path: str = "src/app.py",
            line_start: int = 5, line_end: int = 5) -> tuple[dict, dict]:
    content = full_text if full_text is not None else quote
    sid = "src-1"
    cited = {
        "id": sid, "uri": path, "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "kind": "code", "quote": quote, "line_start": line_start, "line_end": line_end,
        "is_current": True, "metadata": {"relative_path": path},
    }
    ev = {sid: {**cited, "content": content}}
    return cited, ev


# --- 1. ±4-line context window catches surrounding negation/deprecation ---

def test_support_citations_includes_surrounding_context_window():
    lines = [
        "# module header",
        "# DEPRECATED: old timeout below is disabled",
        "if False:",
        "    TIMEOUT_SECONDS = 999",
        "TIMEOUT_SECONDS = 30",
        "MAX_RETRIES = 3",
    ]
    full_text = "\n".join(lines)
    cited, ev = _source("    TIMEOUT_SECONDS = 999", full_text=full_text, line_start=4, line_end=4)
    items = support_citations([cited], ev)
    assert len(items) == 1
    assert items[0]["quote"] == "    TIMEOUT_SECONDS = 999"
    assert "if False:" in items[0]["context"]
    assert "DEPRECATED" in items[0]["context"]

    msgs = support_messages("What is TIMEOUT_SECONDS?", "999", [cited], ev)
    payload = json.loads(msgs[1]["content"])
    assert "if False:" in payload["citations"][0]["context"]


# --- 2. Tier-1 NLI triage (ADR-11): entailment, contradiction, uncertain, secondary_file ---

def test_tier1_nli_triage_rules_and_context_premise():
    full_text = "\n".join([
        "# deprecated legacy path",
        "if False:",
        "    PORT = 9000",
        "PORT = 8080",
    ])
    cited, ev = _source("    PORT = 9000", full_text=full_text, line_start=3, line_end=3)
    seen_premises: list[str] = []

    def high_entail(premise: str, hypothesis: str) -> NliScores:
        seen_premises.append(premise)
        return NliScores(entailment=0.95, neutral=0.04, contradiction=0.01)

    def high_contra(premise: str, hypothesis: str) -> NliScores:
        return NliScores(entailment=0.02, neutral=0.03, contradiction=0.95)

    def uncertain(premise: str, hypothesis: str) -> NliScores:
        return NliScores(entailment=0.60, neutral=0.35, contradiction=0.05)

    # Entailment >= 0.90 on primary file -> accept (and premise includes surrounding context window!)
    d_accept = evaluate_tier1_nli("What is PORT?", "9000", [cited], evidence=ev, scorer=high_entail)
    assert d_accept.decision == "accept"
    assert seen_premises and "if False:" in seen_premises[0]

    # Contradiction >= 0.90 -> reject without LLM
    d_reject = evaluate_tier1_nli("What is PORT?", "9000", [cited], evidence=ev, scorer=high_contra)
    assert d_reject.decision == "reject"

    # Uncertain zone (< 0.90) -> escalate to Tier-2 LLM
    d_esc = evaluate_tier1_nli("What is PORT?", "9000", [cited], evidence=ev, scorer=uncertain)
    assert d_esc.decision == "escalate"

    # ADR-11: secondary_file=True (test/fixture/lock) ALWAYS escalates, even when scorer would return 0.99!
    test_cited, test_ev = _source("PORT = 9000", full_text="PORT = 9000\n", path="tests/test_config.py",
                                  line_start=1, line_end=1)
    d_sec = evaluate_tier1_nli("What is PORT?", "9000", [test_cited], evidence=test_ev, scorer=high_entail)
    assert d_sec.decision == "escalate"
    assert d_sec.reason == "secondary_file_requires_llm"


def test_nli_model_dir_and_checksum_and_offline_missing_model_escalate(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRNMIND_MODEL_DIR", str(tmp_path / "missing-nli"))
    assert resolve_nli_model_dir(tmp_path) == tmp_path / "missing-nli"
    cited, ev = _source("PORT = 8080", line_start=1, line_end=1)
    decision = evaluate_tier1_nli("What is PORT?", "8080", [cited], evidence=ev, data_dir=tmp_path)
    assert decision.decision == "escalate"
    assert "nli_unavailable" in decision.reason

    # Corrupt model dir raises NliUnavailable cleanly without downloading anything
    with pytest.raises(NliUnavailable):
        OnnxNliScorer(tmp_path / "missing-nli")


# --- 3. Full _support_gate cascade: Tier-1 fast accept/reject, Tier-2 escalation, offline fail-closed ---

def test_support_gate_tier1_accept_skips_llm_reject_blocks_and_offline_escalation_fails_closed(tmp_path):
    class CountingOllama:
        def __init__(self, *, unavailable: bool = False):
            self.unavailable = unavailable
            self.calls = 0

        async def chat(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.unavailable:
                raise OllamaUnavailable("offline")
            return {"message": {"content": json.dumps({"supported": True, "reason": "Confirmed by Tier-2 LLM."})}}

    class MinimalStore:
        def resolve_project(self, pid):
            return {"id": pid, "name": "demo"}

        def query(self, *args, **kwargs):
            return []

    settings = Settings(data_dir=tmp_path)
    cited, ev = _source("TIMEOUT = 30", full_text="TIMEOUT = 30\n", path="src/config.py", line_start=1, line_end=1)
    complete_result = {
        "status": "complete",
        "answer": "TIMEOUT is 30.",
        "sources": [cited],
        "unknowns": [],
        "freshness": {"warnings": []},
        "diagnostics": {"code": "ok"},
    }

    # Case 1: Tier-1 accept skips BOTH Tier-2 LLM calls (0 LLM calls!)
    llm1 = CountingOllama()
    agent1 = KnowledgeAgent(MinimalStore(), settings, ollama=llm1)
    diag1 = {"model_calls": []}
    out1 = asyncio.run(agent1._support_gate(
        "p1", "What is TIMEOUT?", complete_result, ev, 0.0, diag1, plain=True,
        verifier="nli", nli_scorer=lambda p, h: NliScores(0.96, 0.03, 0.01),
    ))
    assert out1["status"] == "complete"
    assert llm1.calls == 0
    assert diag1["verification_tier"] == 1
    assert diag1["support_check"] == "nli_supported"
    assert diag1["names_check_skipped"] == "tier1_nli_fast"

    # Case 2: Tier-1 contradiction rejects with 0 LLM calls
    llm2 = CountingOllama()
    agent2 = KnowledgeAgent(MinimalStore(), settings, ollama=llm2)
    diag2 = {"model_calls": []}
    out2 = asyncio.run(agent2._support_gate(
        "p1", "What is TIMEOUT?", complete_result, ev, 0.0, diag2, plain=True,
        verifier="nli", nli_scorer=lambda p, h: NliScores(0.01, 0.02, 0.97),
    ))
    assert out2["status"] == "partial"
    assert out2["diagnostics"]["code"] == "answer_unsupported"
    assert llm2.calls == 0
    assert diag2["verification_tier"] == 1
    assert diag2["support_check"] == "nli_contradicted"

    # Case 3: Tier-1 uncertain escalates to Tier-2 LLM; when LLM is offline -> fail closed with needs_llm_review!
    llm3 = CountingOllama(unavailable=True)
    agent3 = KnowledgeAgent(MinimalStore(), settings, ollama=llm3)
    diag3 = {"model_calls": []}
    out3 = asyncio.run(agent3._support_gate(
        "p1", "What is TIMEOUT?", complete_result, ev, 0.0, diag3, plain=True,
        verifier="nli", nli_scorer=lambda p, h: NliScores(0.55, 0.40, 0.05),
    ))
    assert out3["status"] == "partial"
    assert out3["diagnostics"]["code"] == NEEDS_LLM_REVIEW_CODE
    assert llm3.calls == 1
    assert diag3["verification_tier"] == 2
    assert "nli_escalated" in diag3


# --- 4. CLI flags: --fast, --verifier nli, --strict-llm validation ---

def test_ask_cli_verifier_flags_validate_and_forward(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    Settings.load(data_dir, create=True)
    # Conflict: --strict-llm + --fast -> exit 2
    bad = runner.invoke(cli.app, ["--data-dir", str(data_dir), "ask", "demo", "q?", "--fast", "--strict-llm"])
    assert bad.exit_code == 2
    bad2 = runner.invoke(cli.app, ["--data-dir", str(data_dir), "ask", "demo", "q?", "--verifier", "invalid"])
    assert bad2.exit_code == 2

    captured = {}
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    monkeypatch.setattr(cli, "_refuse_other_version", lambda settings: None)

    def fake_request(settings, method, path, **kwargs):
        captured["json"] = kwargs.get("json")
        return {"status": "complete", "answer": "30", "sources": [], "unknowns": [], "diagnostics": {"code": "ok"}}

    monkeypatch.setattr(cli, "request", fake_request)
    ok_fast = runner.invoke(cli.app, ["--data-dir", str(data_dir), "ask", "demo", "What is TIMEOUT?", "--fast", "--json"])
    assert ok_fast.exit_code == 0, ok_fast.output
    assert captured["json"]["verifier"] == "nli"

    # Default omits "verifier" for backwards compatibility
    ok_default = runner.invoke(cli.app, ["--data-dir", str(data_dir), "ask", "demo", "What is TIMEOUT?", "--json"])
    assert ok_default.exit_code == 0, ok_default.output
    assert "verifier" not in captured["json"]
