"""Whether the knowledge agent's models are configured: a choice stored in config.json (#274).

"enabled": answers, extraction and embeddings use the configured models; a model server that is down or a model
that is missing is a fault, reported as before. "disabled": deterministic-only mode, a stated condition: no model
is asked, extraction and embedding jobs stay pending (held), `ask` makes no answer (unavailable) and lists full-text matches. "" until `cairnmind init`
chooses, which counts as enabled; a config.json from before 0.13.0 has no such key and is read as enabled
(config.Settings.load). Only init (once), `models pull`, `models set` and a person editing config.json change it,
and the service reads it when it starts.
"""
from __future__ import annotations

import asyncio
import fcntl
import time

from . import config
from .first_start import (ASK_LEFT_OUT, ASK_NO_MATCH, ASK_OFF, ASK_TIMED_OUT, CHOSE_DISABLED, CHOSE_ENABLED, GET_MODELS, KNOWLEDGE_DISABLED,
                          KNOWLEDGE_INVALID, MATCHES_HELD_BACK, MODEL_MISSING, MODELS_NOT_CONFIGURED, NEEDS_MODELS,
                          RESTART_FOR_MODELS, SERVER_DOWN, TURNED_ON)

UNAVAILABLE_CODE = "models_not_configured"


def enabled(settings) -> bool:
    """True for "enabled" and for "" (not chosen yet); False for "disabled"; any other value is refused."""
    value = getattr(settings, "knowledge_agent", "")
    if value in ("", "enabled"):
        return True
    if value == "disabled":
        return False
    raise ValueError(KNOWLEDGE_INVALID.format(value=repr(value)[:80]))


def _store(settings, value: str) -> str:
    """Save knowledge_agent in config.json under the settings lock, as engine_docker.store_engine_mode saves
    engine_mode; return the value stored before."""
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (settings.data_dir / "config.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stored = config.Settings.load(settings.data_dir)
        before = stored.knowledge_agent
        if before != value:
            stored.knowledge_agent = value
            stored.save()
    settings.knowledge_agent = value
    return before


def missing_models(settings) -> list[str]:
    """Why the configured models cannot be used now, one phrase per server or model, or [] when each role's server
    answers and lists its model. Read-only: the requests doctor makes (doctor_models), never a download."""
    import httpx
    from .memory_models import ROLES, doctor_models
    with httpx.Client(timeout=1.5, trust_env=False) as http:
        found = doctor_models(settings, http)
    reasons = []
    for role in ROLES:
        entry = found[role]
        if entry.get("refused") or str(entry.get("error", "")).startswith("Invalid profile"):
            raise ValueError(entry["error"])  # a configured but broken profile is not "no models": the person fixes it
        url = entry.get("base_url") or settings.ollama_url
        if not entry.get("reachable"):
            reason = SERVER_DOWN.format(url=url) if entry.get("base_url") else str(entry.get("error"))
        elif not entry.get("model_available"):
            reason = MODEL_MISSING.format(url=url, model=entry.get("model"))
        else:
            continue
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def choose(settings) -> str | None:
    """The first choice of `cairnmind init`: the sentence saying what was chosen and saved, or None when a value is
    stored already (an invalid one raises). Downloads nothing; a server that does not answer is not a failure."""
    if settings.knowledge_agent != "":
        enabled(settings)
        return None
    where = settings.data_dir / "config.json"
    reasons = missing_models(settings)
    if reasons:
        _store(settings, "disabled")
        return CHOSE_DISABLED.format(reason="; ".join(reasons), config=where) + " " + GET_MODELS
    _store(settings, "enabled")
    return CHOSE_ENABLED.format(chat=settings.chat_model, embedding=settings.embedding_model, config=where)


def turn_on(settings) -> str | None:
    """After `models pull` succeeded or `models set` passed its check. TURNED_ON when that changed what was
    stored ("disabled", or an invalid value the person's command replaces); None when it already counted as
    enabled."""
    before = _store(settings, "enabled")
    return None if before in ("", "enabled") else TURNED_ON.format(config=settings.data_dir / "config.json")


def needs_models(settings, command: str) -> str | None:
    """Why `command` does not run in deterministic-only mode, or None."""
    return None if enabled(settings) else NEEDS_MODELS.format(command=command) + " " + GET_MODELS


MATCH_LIMIT = 8  # as many as an answer lists sources (agent._result)
MATCH_EXCERPT_CHARS = 400  # the excerpt of an answer's source (agent._result)


def _without_copies(matches: list[dict]) -> list[dict]:
    """A record and the note it was made from, with the same words, are one passage: the record is kept, since it
    carries the status (#313). Other passages with equal text in other sources stay: each is a place."""
    def text(match):
        return " ".join(str(match.get("excerpt", "")).split())
    records = {(match["source_id"], text(match)) for match in matches if match.get("status")}
    return [match for match in matches if match.get("status") or (match["source_id"], text(match)) not in records]


async def text_matches(agent, project_id: str, question: str, worktree=None, also_held=frozenset()) -> dict:
    """The passages a full-text search finds for the question's words, through exactly the filters of the first,
    lexical search of `ask` (KnowledgeAgent._ask_impl): Retriever.search without a query vector, then agent._hits,
    with the sensitivity gate set as KnowledgeAgent.ask sets it. No model is asked. Search results, never an
    answer: unavailable_answer presents them."""
    from .agent import INITIAL_HITS
    from .memory_egress import ASK, ask_gate
    if not isinstance(question, str) or not question.strip() or len(question) > 16000:
        raise ValueError("Question must contain 1–16000 characters")  # as KnowledgeAgent._ask_impl refuses it
    started = time.monotonic()
    agent.retriever.warnings.clear()
    gate = ask_gate(agent.settings, agent.store, also_held)
    token = ASK.set(gate)
    evidence: dict = {}
    try:
        hits = await asyncio.to_thread(agent.retriever.search, project_id, question, INITIAL_HITS, worktree=worktree)
        shown = await asyncio.to_thread(agent._hits, hits, project_id, evidence, worktree, question, False,
                                        MATCH_EXCERPT_CHARS)
    finally:
        ASK.reset(token)
    matches = []
    refreshed = 0
    for item in shown:
        if item.get("refreshed"):
            refreshed += 1  # the file changed since it was indexed: _hits shows its start, not a passage with the words
            agent.retriever.warnings.append(f"Current source changed since it was indexed: {item.get('uri', '')}")
            continue
        source = evidence[item["source_id"]]
        match = {"id": item["id"], "source_id": item["source_id"], "uri": item.get("uri", source["uri"]),
                 "kind": item.get("kind") or source["kind"], "match": "full_text"}
        match.update({key: item[key] for key in ("line_start", "line_end") if key in item})
        match["excerpt"] = item["text"]
        match.update({key: item[key] for key in ("status", "confirmed_by", "confirmation", "metadata") if key in item})
        matches.append(match)
    matches = _without_copies(matches)[:MATCH_LIMIT]
    held_back = gate.summary() if gate else None
    # Found but not shown: held by the gate, unverifiable on disk (_hits drops it) or changed since indexing.
    left_out = bool(held_back) or len(shown) < len(hits) or refreshed > 0
    return {"matches": matches, "held_back": held_back, "left_out": left_out,
            "warnings": list(dict.fromkeys(w[:240] for w in agent.retriever.warnings))[-8:],
            "elapsed_seconds": round(time.monotonic() - started, 3)}


def unavailable_answer(found: dict, index_errors=()) -> dict:
    """What /ask returns in deterministic-only mode: no answer (status unavailable, no source), and the passages
    text_matches found, in their own key, said by the answer sentence to be search results and not an answer."""
    errors = [str(error)[:240] for error in list(index_errors)[:5]]
    unknowns = [GET_MODELS, *([MATCHES_HELD_BACK] if found["held_back"] else []), *errors]
    diagnostics = {"stage": "ask", "code": UNAVAILABLE_CODE}
    if found["held_back"]:
        diagnostics["held_back"] = found["held_back"]
    answer = (ASK_OFF if found["matches"] else ASK_TIMED_OUT if found.get("timed_out")
              else ASK_LEFT_OUT if found["left_out"] else ASK_NO_MATCH)
    return {"status": "unavailable", "answer": answer, "sources": [],
            "full_text_matches": found["matches"], "unknowns": list(dict.fromkeys(unknowns))[:8],
            "freshness": {"warnings": [*found["warnings"], *errors][-8:]},
            "elapsed_seconds": found["elapsed_seconds"], "cached": False, "diagnostics": diagnostics}


def status_fields(pipeline: dict, settings=None) -> dict:
    """What /status adds in deterministic-only mode; pipeline is store.job_summary. When config.json says enabled
    by now (models pull or set ran) the running service only reports that it must restart; it never switches."""
    held = sum(pipeline[group][state] for group in ("indexing", "extraction") for state in ("pending", "running"))
    advice = RESTART_FOR_MODELS if restart_pending(settings) else GET_MODELS
    return {"models": MODELS_NOT_CONFIGURED, "jobs_held": held, "get_models": advice}


def restart_pending(settings) -> bool:
    """config.json says enabled although this service started without models."""
    try:
        return settings is not None and config.Settings.load(settings.data_dir).knowledge_agent == "enabled"
    except (OSError, ValueError):  # a config.json being edited by hand must not take /status down
        return False


def doctor_fields(settings) -> dict:
    """What doctor adds: nothing when enabled, the information line when disabled, the refusal for another value."""
    try:
        on = enabled(settings)
    except ValueError as exc:
        return {"knowledge_agent": str(exc)}
    return {} if on else {"knowledge_agent": KNOWLEDGE_DISABLED, "get_models": GET_MODELS}
