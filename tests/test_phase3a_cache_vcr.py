"""Phase 3a tests: Level 0 BOM/CRLF citation matching, persistent verdict cache + cache clear CLI,
and deterministic VCR record/replay evaluation pipeline without live Ollama.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

import pytest
from typer.testing import CliRunner

from cairnmind import cli
from cairnmind.citation_match import locate_quote, quote_weight
from cairnmind.config import Settings
from cairnmind.extraction_evaluation import run_extraction_evaluation
from cairnmind.vcr import CassetteMissError, VcrOllama, deterministic_uuid4, normalize_for_key, request_key
from cairnmind.verdict_cache import (
    VERIFIER_VERSION, VerdictCache, context_window_text, prompt_fingerprint, verdict_cache_key,
)

ROOT = Path(__file__).resolve().parents[1]
CASSETTE_EXTRACTION = ROOT / "tests" / "cassettes" / "eval_extraction.json"
runner = CliRunner()


# --- 1. Level 0 determinism: BOM + CRLF/LF + latency budget ---

def test_level0_locate_quote_handles_bom_crlf_and_stays_under_10ms():
    bom_crlf = "\ufeffdef connect(host: str) -> int:\r\n    timeout = 30\r\n    return timeout\r\n"
    lf_quote = "def connect(host: str) -> int:\n    timeout = 30"
    span = locate_quote(lf_quote, bom_crlf)
    assert span is not None
    matched = bom_crlf[span[0]:span[1]]
    assert matched == "def connect(host: str) -> int:\r\n    timeout = 30"
    assert not matched.startswith("\ufeff")

    # Quote itself carrying BOM + CRLF against LF file
    lf_file = "def connect(host: str) -> int:\n    timeout = 30\n    return timeout\n"
    bom_quote = "\ufeffdef connect(host: str) -> int:\r\n    timeout = 30"
    span2 = locate_quote(bom_quote, lf_file)
    assert span2 is not None
    assert lf_file[span2[0]:span2[1]] == "def connect(host: str) -> int:\n    timeout = 30"

    # Paraphrase / altered number still rejected deterministically
    assert locate_quote("timeout = 31", bom_crlf) is None
    assert quote_weight("\ufeff   ") == 0

    # Latency check: 100 lookups finish well under 10 ms per lookup
    started = time.perf_counter()
    for _ in range(100):
        assert locate_quote(lf_quote, bom_crlf) is not None
    avg_ms = ((time.perf_counter() - started) / 100) * 1000
    assert avg_ms < 10.0, f"Level 0 lookup took {avg_ms:.2f} ms"


# --- 2. Persistent verdict cache & strict key invalidation ---

def test_verdict_cache_persists_and_invalidates_on_every_input_dimension(tmp_path):
    base_source = {
        "id": "src-1",
        "uri": "src/app.py",
        "sha256": "a" * 64,
        "quote": "TIMEOUT = 30",
        "line_start": 5,
        "line_end": 5,
        "content": "\n".join(f"line {i}" if i != 5 else "TIMEOUT = 30" for i in range(1, 15)),
        "metadata": {"relative_path": "src/app.py"},
    }
    base_args = dict(
        stage="support",
        question="What is TIMEOUT?",
        claim="TIMEOUT is 30.",
        sources=[base_source],
        model_id="qwen2.5:14b@sha256:abc",
        prompt_hash=prompt_fingerprint("SYS", {"type": "object"}),
        verification_mode="strict_llm",
        verifier_version=VERIFIER_VERSION,
    )
    k0 = verdict_cache_key(**base_args)

    # Every single dimension must change the cache key
    assert verdict_cache_key(**{**base_args, "question": "What is RETRY?"}) != k0
    assert verdict_cache_key(**{**base_args, "claim": "TIMEOUT is 60."}) != k0
    assert verdict_cache_key(**{**base_args, "model_id": "qwen2.5:32b@sha256:def"}) != k0
    assert verdict_cache_key(**{**base_args, "prompt_hash": prompt_fingerprint("SYS_V2", {})}) != k0
    assert verdict_cache_key(**{**base_args, "verification_mode": "nli"}) != k0
    assert verdict_cache_key(**{**base_args, "verifier_version": VERIFIER_VERSION + 1}) != k0
    # Changed file fingerprint
    assert verdict_cache_key(**{**base_args, "sources": [{**base_source, "sha256": "b" * 64}]}) != k0
    # Changed quote
    assert verdict_cache_key(**{**base_args, "sources": [{**base_source, "quote": "TIMEOUT = 31"}]}) != k0
    # Changed path / secondary_file
    assert verdict_cache_key(**{**base_args, "sources": [{**base_source, "metadata": {"relative_path": "tests/test_app.py"}}]}) != k0
    # Changed surrounding context window (line 4 changed while line 5 quote & sha256 simulated separately)
    mutated_content = base_source["content"].replace("line 4", "line 4 EDITED")
    assert context_window_text(mutated_content, 5, 5) != context_window_text(base_source["content"], 5, 5)
    assert verdict_cache_key(**{**base_args, "sources": [{**base_source, "content": mutated_content}]}) != k0

    # Whitespace normalization in question/claim preserves hit
    assert verdict_cache_key(**{**base_args, "question": "  What   is  TIMEOUT? "}) == k0

    # Persistent read/write across instances + CLI clear & status
    cache1 = VerdictCache(tmp_path)
    assert cache1.get(k0) is None
    assert cache1.put(k0, "support", {"supported": True, "reason": "Stated directly."}) is True

    cache2 = VerdictCache(tmp_path)
    loaded = cache2.get(k0)
    assert loaded == {"supported": True, "reason": "Stated directly."}

    status_res = runner.invoke(cli.app, ["--data-dir", str(tmp_path), "cache", "status"])
    assert status_res.exit_code == 0, status_res.output
    stats = json.loads(status_res.stdout)
    assert stats["entries"] == 1 and stats["total_hits"] == 1

    clear_res = runner.invoke(cli.app, ["--data-dir", str(tmp_path), "cache", "clear"])
    assert clear_res.exit_code == 0, clear_res.output
    cleared = json.loads(clear_res.stdout)
    assert cleared["status"] == "cleared" and cleared["deleted_entries"] == 1
    assert cache2.get(k0) is None


# --- 3. VCR record/replay layer & fail-closed CassetteMissError ---

def test_vcr_record_replay_normalizes_paths_timestamps_and_fails_closed_on_miss(tmp_path):
    class DummyLive:
        settings = Settings(data_dir=tmp_path)

        async def model_identity(self, name: str) -> str:
            return f"{name}@sha256:{'1' * 64}"

        async def model_digest(self, name: str) -> str:
            return "sha256:" + "1" * 64

        async def chat(self, messages, tools=None, **kwargs):
            return {"message": {"content": '{"ok": true}'}, "eval_count": 10}

        async def embed(self, texts):
            return [[0.25, 0.75] for _ in texts]

    cassette = tmp_path / "suite.json"
    recorder = VcrOllama.record(DummyLive(), cassette, suite="unit")

    async def record_pass():
        ident = await recorder.model_identity("qwen2.5:14b")
        assert ident.startswith("qwen2.5:14b@sha256:")
        c = await recorder.chat([{"role": "user", "content": "path /tmp/cairnmind-eval-abc123/repo at 2026-11-27T10:00:00Z"}])
        e = await recorder.embed(["hello world"])
        return c, e

    c1, e1 = asyncio.run(record_pass())
    recorder.save()
    assert cassette.is_file()

    # Replay offline with DIFFERENT temp path and DIFFERENT timestamp -> STILL HITS!
    replayer = VcrOllama.replay(cassette)

    async def replay_pass():
        ident = await replayer.model_identity("qwen2.5:14b")
        assert ident.startswith("qwen2.5:14b@sha256:")
        c = await replayer.chat([{"role": "user", "content": "path /tmp/cairnmind-eval-zzz999/repo at 2027-01-01T00:00:00+00:00"}])
        e = await replayer.embed(["hello world"])
        return c, e

    c2, e2 = asyncio.run(replay_pass())
    assert c2 == c1 and e2 == e1 and replayer.replay_hits == 2

    # Missing prompt raises CassetteMissError immediately (never falls back to network)
    with pytest.raises(CassetteMissError, match="cassette miss"):
        asyncio.run(replayer.chat([{"role": "user", "content": "never recorded prompt"}]))
    with pytest.raises(CassetteMissError, match="cassette miss"):
        asyncio.run(replayer.embed(["never recorded embedding"]))

    # Normalization check
    assert normalize_for_key("/tmp/cairnmind-eval-aaaa/x") == normalize_for_key("/var/tmp/cairnmind-eval-bbbb/x")
    assert request_key("chat", {"a": 1, "b": 2}) == request_key("chat", {"b": 2, "a": 1})

    # Deterministic UUID generator repeatability
    with deterministic_uuid4("test-seed") as g1:
        seq1 = [g1.next_uuid(), g1.next_hex(12)]
    with deterministic_uuid4("test-seed") as g2:
        seq2 = [g2.next_uuid(), g2.next_hex(12)]
    assert seq1 == seq2


# --- 4. End-to-end golden cassette replay in CLI (exit 0 offline; exit 1 on miss) ---

def test_eval_extraction_vcr_replay_passes_offline_and_exits_1_on_miss(tmp_path):
    assert CASSETTE_EXTRACTION.is_file(), f"Missing golden cassette {CASSETTE_EXTRACTION}"
    data_dir = tmp_path / "data"
    Settings.load(data_dir, create=True)

    # Replay golden cassette via CLI without Ollama or ArcadeDB -> exit 0!
    res = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "eval", "extraction", "--runs", "1", "--replay", str(CASSETTE_EXTRACTION)],
    )
    assert res.exit_code == 0, res.output
    report = json.loads(res.stdout)
    assert report["status"] == "passed"
    assert report["runs"][0]["kept_lost"] == []
    assert report["runs"][0]["failures"] == []

    # Mutated / empty cassette -> CassetteMissError -> CLI exits 1 (NEVER exit 3!)
    empty_cassette = tmp_path / "empty.json"
    empty_cassette.write_text(json.dumps({
        "format": "cairnmind-vcr-cassette",
        "format_version": 1,
        "suite": "eval_extraction",
        "model_identities": {"qwen2.5:14b": "qwen2.5:14b@sha256:" + "0" * 64},
        "model_digests": {"qwen2.5:14b": "sha256:" + "0" * 64},
        "entries": {},
    }), encoding="utf-8")
    miss = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "eval", "extraction", "--runs", "1", "--replay", str(empty_cassette)],
    )
    assert miss.exit_code == 1
    assert "cassette miss" in miss.output.lower()

    # --record and --replay together refused (exit 2)
    both = runner.invoke(
        cli.app,
        ["--data-dir", str(data_dir), "eval", "extraction",
         "--record", str(tmp_path / "a.json"), "--replay", str(CASSETTE_EXTRACTION)],
    )
    assert both.exit_code == 2

    # Direct Python API also propagates CassetteMissError
    with pytest.raises(CassetteMissError):
        run_extraction_evaluation(
            Settings.load(data_dir),
            ROOT / "docs" / "evaluation" / "extraction-transcripts.json",
            runs=1,
            ollama=VcrOllama.replay(empty_cassette),
        )


def test_ci_workflows_wire_eval_replay_on_pr_and_eval_live_separately():
    ci_yml = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    live_yml = (ROOT / ".github" / "workflows" / "eval-live.yml").read_text(encoding="utf-8")
    assert "eval-replay:" in ci_yml
    assert "--replay tests/cassettes/eval_extraction.json" in ci_yml
    assert "workflow_dispatch:" in live_yml
    assert "eval repository ." in live_yml
