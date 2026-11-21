"""Whether the knowledge agent's models are configured: a choice stored in config.json (#274).

"enabled": answers, extraction and embeddings use the configured models; a model server that is down or a model
that is missing is a fault, reported as before. "disabled": deterministic-only mode, a stated condition: no model
is asked, extraction and embedding jobs stay pending (held), `ask` answers unavailable. "" until `cairnmind init`
chooses, which counts as enabled; a config.json from before 0.13.0 has no such key and is read as enabled
(config.Settings.load). Only init (once), `models pull`, `models set` and a person editing config.json change it,
and the service reads it when it starts.
"""
from __future__ import annotations

import fcntl

from . import config
from .first_start import (ASK_OFF, CHOSE_DISABLED, CHOSE_ENABLED, GET_MODELS, KNOWLEDGE_DISABLED,
                          KNOWLEDGE_INVALID, MODEL_MISSING, MODELS_NOT_CONFIGURED, NEEDS_MODELS,
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
        if str(entry.get("error", "")).startswith("Invalid profile"):
            raise ValueError(entry["error"])  # a configured but broken profile is not "no models": the person fixes it
        url =entry.get("base_url") or settings.ollama_url
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


def unavailable_answer() -> dict:
    """What /ask returns in deterministic-only mode: the answer shape of the agent, status unavailable, no source."""
    return {"status": "unavailable", "answer": ASK_OFF, "sources": [], "unknowns": [GET_MODELS],
            "freshness": {}, "elapsed_seconds": 0.0, "cached": False,
            "diagnostics": {"stage": "ask", "code": UNAVAILABLE_CODE}}


def status_fields(pipeline: dict, settings=None) -> dict:
    """What /status adds in deterministic-only mode; pipeline is store.job_summary. When config.json says enabled
    by now (models pull or set ran) the running service only reports that it must restart; it never switches."""
    held = sum(pipeline[group][state] for group in ("indexing", "extraction") for state in ("pending", "running"))
    advice = GET_MODELS
    if settings is not None and config.Settings.load(settings.data_dir).knowledge_agent == "enabled":
        advice = RESTART_FOR_MODELS
    return {"models": MODELS_NOT_CONFIGURED, "jobs_held": held, "get_models": advice}


def doctor_fields(settings) -> dict:
    """What doctor adds: nothing when enabled, the information line when disabled, the refusal for another value."""
    try:
        on = enabled(settings)
    except ValueError as exc:
        return {"knowledge_agent": str(exc)}
    return {} if on else {"knowledge_agent": KNOWLEDGE_DISABLED, "get_models": GET_MODELS}
