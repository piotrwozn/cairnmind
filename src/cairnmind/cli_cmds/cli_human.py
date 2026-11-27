"""Human-readable CLI error mapping, additive JSON error codes, and NO_COLOR/TTY hygiene.

Stable machine codes remain in ``--json`` outputs and audit logs, while interactive terminal runs
translate core engine refusals into actionable remediation messages. Warnings and hints are always
sent to ``stderr`` so ``stdout`` stays valid JSON whenever ``--json`` or a pipe is used.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any

from cairnmind.cli_cmds import first_start
from cairnmind.terminal.terminal_i18n import tr

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
DEFAULT_CHECK_CMD = "cairnmind check run"

# Stable machine error codes exposed additively in JSON envelopes and mapped to human remediation lines.
FINGERPRINT_MISMATCH = "fingerprint_mismatch"
STALE_PLAN_VERSION = "stale_plan_version"
ORIGIN_EVIDENCE_DEGRADED = "origin_evidence_degraded"
NO_VERBATIM_EVIDENCE = "no_verbatim_evidence"

_NO_VERBATIM_CODES = frozenset({"citation_invalid", "missing_evidence"})


def strip_ansi(text: str) -> str:
    """Remove ANSI SGR/CSI escape sequences from text."""
    return ANSI_ESCAPE.sub("", text)


def color_enabled(stream: Any = None, environ: dict[str, str] | None = None) -> bool:
    """Whether ANSI colours and terminal styling may be emitted on ``stream``.

    Disabled when ``NO_COLOR`` is set to any non-empty value (no-color.org), when ``TERM=dumb``,
    or when ``stream`` is not attached to a terminal (pipe, file, script).
    """
    env = os.environ if environ is None else environ
    if env.get("NO_COLOR"):
        return False
    if str(env.get("TERM", "")).lower() == "dumb":
        return False
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    try:
        return bool(isatty()) if callable(isatty) else False
    except OSError:
        return False


def classify_error(message: str, *, cmd: str | None = None) -> dict[str, str] | None:
    """Classify an engine refusal or error text into a stable code and human template key."""
    text = str(message or "")
    lower = text.lower()
    if "task completion requires fresh machine check evidence" in lower or "fingerprint_mismatch" in lower:
        return {
            "error_code": FINGERPRINT_MISMATCH,
            "template": first_start.FINGERPRINT_MISMATCH,
            "cmd": cmd or DEFAULT_CHECK_CMD,
        }
    if "plan version conflict" in lower or "stale_plan_version" in lower:
        return {
            "error_code": STALE_PLAN_VERSION,
            "template": first_start.STALE_PLAN_VERSION,
        }
    if "origin_evidence: degraded" in lower or "origin_evidence=degraded" in lower:
        return {
            "error_code": ORIGIN_EVIDENCE_DEGRADED,
            "template": first_start.DEGRADED_ORIGIN_NOTE,
        }
    if ("the quoted passage does not occur" in lower
            or "the answer has no verified citations" in lower
            or "no_verbatim_evidence" in lower):
        return {
            "error_code": NO_VERBATIM_EVIDENCE,
            "template": first_start.NO_VERBATIM_EVIDENCE,
        }
    return None


def format_human_error(message: str, *, language: str = "en", cmd: str | None = None) -> str | None:
    """Return a concise, actionable CLI message for known engine refusals, or None if unmapped."""
    info = classify_error(message, cmd=cmd)
    if info is None:
        return None
    template = info["template"]
    if info["error_code"] == FINGERPRINT_MISMATCH:
        return tr(template, language, cmd=info["cmd"])
    return tr(template, language)


def error_code_for_ask(result: Any) -> str | None:
    """Return the additive machine ``error_code`` for an ``/ask`` response when applicable."""
    if not isinstance(result, dict) or result.get("status") == "complete":
        return None
    diagnostics = result.get("diagnostics")
    code = diagnostics.get("code") if isinstance(diagnostics, dict) else None
    if code in _NO_VERBATIM_CODES:
        return NO_VERBATIM_EVIDENCE
    return None


def has_degraded_origin(payload: Any) -> bool:
    """Whether a record, source or ``/ask`` result carries ``origin_evidence: degraded``."""
    if not isinstance(payload, dict):
        return False
    if payload.get("origin_evidence") == "degraded":
        return True
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and metadata.get("origin_evidence") == "degraded":
        return True
    sources = payload.get("sources")
    if isinstance(sources, list):
        return any(has_degraded_origin(item) for item in sources)
    return False


def enrich_ask_json(result: Any) -> Any:
    """Additively attach ``error_code`` to ``/ask`` JSON results without altering existing fields."""
    if not isinstance(result, dict) or "error_code" in result:
        return result
    code = error_code_for_ask(result)
    if code is None:
        return result
    return {**result, "error_code": code}


def json_error_payload(message: str, *, exit_code: int, cmd: str | None = None) -> dict[str, Any]:
    """Structured error object preserving the original text alongside an additive ``error_code``."""
    payload: dict[str, Any] = {"error": str(message), "exit_code": exit_code}
    info = classify_error(message, cmd=cmd)
    if info is not None:
        payload["error_code"] = info["error_code"]
    return payload
