"""How much of what extraction proposes from conversations a person would keep (#160).

``cairnmind eval extraction`` reads invented transcripts (by default
``docs/evaluation/extraction-transcripts.json``), turns every event into the source the
product would archive (the built-in agent's journal events as ``agent_transcript``, Claude
Code and Codex lines through the transcript parser as ``transcript``), gives each source
that the product would extract to the production extraction (``KnowledgeAgent.propose``,
the same prompt, schema, repair and grounding checks) and scores the proposals against the
records a person would keep. Nothing is stored: no database, no engine, no user project.
The local model is not deterministic, so the set is run several times.

Scoring is mechanical. A proposal *matches* a kept item when every regular expression of
the item's ``match`` occurs in the proposal's content, ignoring case. A proposal matching a
kept item of its own event is *kept*; one matching a kept item of another event of the same
transcript is a *duplicate*; any other is *rejected*. A person rejects duplicates too, so the
rejected share is (rejected + duplicates) / proposals. A kept item is *found* in a run when
at least one proposal of its event matches it. Pass: every kept item found in every run and
no proposal from an event labelled ``protocol`` or ``host``.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from . import __version__
from .agent_memory import extracted_message
from .config import Settings
from .ingest import _event_text
from .ollama import Ollama, OllamaUnavailable
from .sql import json_text

CLIENTS = {"api", "claude", "codex"}
LABELS = {"kept", "protocol", "host", "one_off", "narration"}
KINDS = {"decision", "task", "fact", "constraint", "question", "preference"}
NEVER_PROPOSE = {"protocol", "host"}


def _bad(where: str, why: str):
    return ValueError(f"Invalid extraction transcripts ({where}): {why}")


def load_transcripts(path: Path) -> list[dict]:
    """The transcripts of ``path``, validated. Raises ValueError naming the first bad entry."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read the extraction transcripts {path}: {exc}") from None
    if not isinstance(data, dict) or data.get("format_version") != 1 or not isinstance(data.get("transcripts"), list):
        raise _bad(str(path), "expected format_version 1 and a transcripts list")
    seen_transcripts, seen_keep = set(), set()
    for transcript in data["transcripts"]:
        tid = transcript.get("id") if isinstance(transcript, dict) else None
        if not isinstance(tid, str) or not tid or tid in seen_transcripts:
            raise _bad(str(tid), "each transcript needs a unique id")
        seen_transcripts.add(tid)
        if transcript.get("client") not in CLIENTS:
            raise _bad(tid, "client must be one of " + ", ".join(sorted(CLIENTS)))
        events = transcript.get("events")
        if not isinstance(events, list) or not events:
            raise _bad(tid, "events must be a nonempty list")
        for index, item in enumerate(events):
            where = f"{tid}[{index}]"
            expect = item.get("expect") if isinstance(item, dict) else None
            if not isinstance(item.get("event") if isinstance(item, dict) else None, dict) or not isinstance(expect, dict):
                raise _bad(where, "each event needs an event object and an expect object")
            keep = expect.get("keep")
            if expect.get("label") not in LABELS or not isinstance(keep, list):
                raise _bad(where, "expect needs a label of " + ", ".join(sorted(LABELS)) + " and a keep list")
            if (expect["label"] == "kept") != bool(keep):
                raise _bad(where, "label kept goes with a nonempty keep list, and only with one")
            for kept in keep:
                kid = kept.get("id") if isinstance(kept, dict) else None
                match = kept.get("match") if isinstance(kept, dict) else None
                if not isinstance(kid, str) or not kid or kid in seen_keep or kept.get("kind") not in KINDS:
                    raise _bad(where, "each kept item needs a unique id and a record kind")
                if not isinstance(match, list) or not match or not all(isinstance(term, str) and term for term in match):
                    raise _bad(where, f"kept item {kid} needs a nonempty match list")
                for term in match:
                    try:
                        re.compile(term)
                    except re.error as exc:
                        raise _bad(where, f"kept item {kid}: {exc}") from None
                seen_keep.add(kid)
    return data["transcripts"]


def transcript_sources(transcript: dict) -> list[dict]:
    """The sources the product would archive for this transcript's events, each with ``extracted``
    (whether the product would give it to extraction) and the event's ``expect``."""
    sources = []
    for index, item in enumerate(transcript["events"]):
        event = item["event"]
        if transcript["client"] == "api":
            journal = {"type": "message", "seq": index + 1, "session_id": transcript["id"], "data": event}
            data = event
            role = data.get("role", "metadata")
            normalized = data.get("content")
            if not isinstance(normalized, str) or not normalized.strip():
                normalized = json_text(data)
            source = {"kind": "agent_transcript", "content": json_text(journal) + "\n",
                      "metadata": {"client": "api", "role": role, "session_id": transcript["id"],
                                   "origin": "agent_runtime", "normalized_text": normalized}}
            extracted = extracted_message(journal)
        else:
            normalized, role, detected, recognized = _event_text(event, transcript["client"])
            source = {"kind": "transcript" if recognized else "transcript_unknown", "content": json.dumps(event),
                      "metadata": {"client": detected, "role": role, "session_id": transcript["id"],
                                   "normalized_text": normalized}}
            extracted = bool(normalized) and role in {"user", "assistant"}
        source["id"] = f"eval:{transcript['id']}:{index}"
        source["project_id"] = "eval"
        sources.append({"source": source, "extracted": extracted, "index": index, "expect": item["expect"]})
    return sources


def _matches(kept: dict, content: str) -> bool:
    return all(re.search(term, content, re.IGNORECASE) for term in kept["match"])


def score_transcript(transcript: dict, proposals: dict[int, list[dict]]) -> dict:
    """Judge the proposals (by event index) of one run against the transcript's kept items."""
    events = transcript["events"]
    rows, found = [], set()
    for index, records in sorted(proposals.items()):
        own = events[index]["expect"]["keep"]
        others = [kept for position, item in enumerate(events) if position != index for kept in item["expect"]["keep"]]
        for record in records:
            content = record.get("content", "")
            mine = [kept["id"] for kept in own if _matches(kept, content)]
            verdict = "kept" if mine else "duplicate" if any(_matches(k, content) for k in others) else "rejected"
            found.update(mine)
            rows.append({"transcript": transcript["id"], "event": index, "label": events[index]["expect"]["label"],
                         "kind": record.get("kind"), "content": content, "evidence": record.get("evidence"),
                         "verdict": verdict, **({"kept_ids": mine} if mine else {})})
    expected = [kept["id"] for item in events for kept in item["expect"]["keep"]]
    return {"records": rows, "kept_found": [kid for kid in expected if kid in found],
            "kept_lost": [kid for kid in expected if kid not in found]}


def summarize(run: list[dict]) -> dict:
    """Counts of one run over all transcripts."""
    records = [row for result in run for row in result["records"]]
    rejected = sum(row["verdict"] != "kept" for row in records)
    by_label = {}
    for row in records:
        by_label[row["label"]] = by_label.get(row["label"], 0) + 1
    return {"proposals": len(records), "kept": len(records) - rejected,
            "rejected": sum(row["verdict"] == "rejected" for row in records),
            "duplicates": sum(row["verdict"] == "duplicate" for row in records),
            "rejected_share": round(rejected / len(records), 4) if records else 0.0,
            "proposals_by_label": dict(sorted(by_label.items())),
            "kept_found": sum(len(result["kept_found"]) for result in run),
            "kept_total": sum(len(result["kept_found"]) + len(result["kept_lost"]) for result in run),
            "kept_lost": [kid for result in run for kid in result["kept_lost"]]}


def _diagnostics() -> dict:
    return {"code": "source_invalid", "repair_attempted": False, "repair_outcome": "not_needed",
            "validation_codes": [], "model_calls": [],
            "rejection_counts": {"blank_content": 0, "blank_evidence": 0, "quote_absent_source": 0, "reserved_metadata": 0},
            "counts": {"candidate_records": 0, "accepted_records": 0, "skipped_records": 0, "written_records": 0}}


async def _run_once(agent, transcripts: list[dict]) -> tuple[list[dict], list[dict]]:
    results, failures = [], []
    for transcript in transcripts:
        proposals = {}
        for item in transcript_sources(transcript):
            if not item["extracted"]:
                continue
            diagnostics = _diagnostics()
            try:
                records = await agent.propose(item["source"], diagnostics)
            except ValueError:
                records = []
                failures.append({"transcript": transcript["id"], "event": item["index"], "code": diagnostics["code"]})
            proposals[item["index"]] = records or []
        results.append(score_transcript(transcript, proposals))
    return results, failures


def run_extraction_evaluation(settings: Settings, transcripts_path: Path, *, runs: int = 3,
                              report_dir: Path | None = None, ollama=None) -> dict:
    """Run the production extraction on every transcript ``runs`` times and write a report."""
    from .agent import KnowledgeAgent

    if type(runs) is not int or not 1 <= runs <= 20:
        raise ValueError("runs must be an integer from 1 to 20")
    transcripts = load_transcripts(transcripts_path)
    started = time.monotonic()
    model = ollama or Ollama(settings)
    report = {"format_version": 1, "cairnmind_version": __version__, "evaluation_mode": "transcript_extraction",
              "inference": "injected_adapter" if ollama is not None else "local_ollama", "status": "not_run",
              "model": settings.chat_model, "transcripts": str(transcripts_path), "runs": [],
              "limits": ["Invented conversations; they show the kinds of noise seen in a trial, not every project.",
                         "Matching is by regular expressions over the proposed content; read the records too."]}
    try:
        report["model_identity"] = asyncio.run(model.model_identity(settings.chat_model))
        agent = KnowledgeAgent(None, settings, ollama=model)
        for number in range(runs):
            results, failures = asyncio.run(_run_once(agent, transcripts))
            report["runs"].append({"run": number + 1, "summary": summarize(results), "failures": failures,
                                   "records": [row for result in results for row in result["records"]]})
        lost = any(run["summary"]["kept_lost"] for run in report["runs"])
        protocol = any(row["label"] in NEVER_PROPOSE for run in report["runs"] for row in run["records"])
        report["status"] = "failed" if lost or protocol else "passed"
    except OllamaUnavailable as exc:
        report.update(status="failed" if report["runs"] else "not_run", error=str(exc)[:500])
    report["elapsed_seconds"] = round(time.monotonic() - started, 4)
    output = Path(report_dir or settings.data_dir / "evaluations").expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = output / ("extraction-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid4().hex[:8] + ".json")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    report["report_path"] = str(path)
    return report
