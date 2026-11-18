"""Model servers that refuse structured output (#231).

Ollama with an MLX build of a model (Apple silicon) answers every chat request that has ``format`` with
HTTP 501 ``{"error": "structured output is unavailable"}``; without ``format`` the same request is
answered. Ollama.chat then asks once more without ``format``, with the schema stated in the system
message. The caller validates the JSON it gets back exactly as it does with ``format`` (the answer's
correction and its citations, the support check, extraction, the check of ``models set``): nothing is
accepted on the server's word, and an answer that fails validation stays partial.

Only that refusal counts: HTTP 501 and an error that says structured output is unavailable. Any other
error of a structured request is reported as before.

What was refused is remembered per server, model and digest for the life of the process, so a question
does not first send a request that is refused. The decision is not kept across processes: a newer
server may offer structured output for the same digest, and one refused request per process finds out.
What was seen is also written to models.capabilities.json in the data directory, only so that
``doctor`` (another process, which sends no model requests) can name it.
"""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
import re
import time
from typing import Any

REFUSED = "structured_output_unavailable"
# The error text of the refusal; the status must be 501 as well (ollama._api_error_code).
REFUSAL_TEXT = re.compile(r"structured output[^\n]{0,40}\b(?:is )?(?:unavailable|not available)")
DOCTOR_TEXT = "structured output: unavailable on this server"
DOCTOR_HANDLED = ("Cairnmind asks this model without a response format, with the schema in the prompt, and "
                  "validates the JSON itself; an answer that fails validation stays partial.")
FILE = "models.capabilities.json"
LOCK = "models.capabilities.lock"
MAX_RECORDS = 50
INSTRUCTION = ("This server cannot enforce the response format. Return only one JSON object, with no other text "
               "and no code fence, that is valid under this JSON schema: ")
_FENCE = re.compile(r"\A```(?:json)?[ \t]*\n(.*)\n```\Z", re.S)

_refused: set[tuple[str, str, str | None]] = set()


def _key(server: str, model: str, digest: str | None) -> tuple[str, str, str | None]:
    return str(server).rstrip("/"), str(model), digest


def _name(server: str, model: str, digest: str | None) -> str:
    return "|".join(item or "-" for item in _key(server, model, digest))


def forget() -> None:
    """Forget what this process has seen (tests)."""
    _refused.clear()


def seen_for(server: str, model: str) -> bool:
    """Whether any digest of this model on this server refused structured output in this process."""
    server, model, _ = _key(server, model, None)
    return any(key[:2] == (server, model) for key in _refused)


def refused(server: str, model: str, digest: str | None) -> bool:
    return _key(server, model, digest) in _refused


def remember(data_dir, server: str, model: str, digest: str | None) -> None:
    """Remember the refusal for this process, and note it for doctor. A note that cannot be written is
    left out: it only informs doctor, and the answer must not fail because of it."""
    _refused.add(_key(server, model, digest))
    try:
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (path / LOCK).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            records = dict(_read(path))
            records[_name(server, model, digest)] = {
                "structured_output": False, "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if len(records) > MAX_RECORDS:  # the oldest observations go first
                records = dict(sorted(records.items(), key=lambda item: item[1]["observed_at"])[-MAX_RECORDS:])
            from .memory_models import _write_json
            _write_json(path / FILE, {"version": 1, "records": records})
    except OSError:
        pass


def _read(data_dir) -> dict:
    try:
        document = json.loads((Path(data_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = document.get("records") if isinstance(document, dict) and document.get("version") == 1 else None
    if not isinstance(rows, dict):
        return {}
    return {name: row for name, row in rows.items()
            if isinstance(name, str) and isinstance(row, dict) and row.get("structured_output") is False
            and isinstance(row.get("observed_at"), str)}


def doctor_note(data_dir, server: str, model: str, digest: str | None) -> dict | None:
    """Read-only, for doctor: what was seen of this server, model and digest, or None."""
    record = _read(data_dir).get(_name(server, model, digest))
    if record is None:
        return None
    return {"refused": True, "text": DOCTOR_TEXT, "observed_at": record["observed_at"][:32],
            "handled": DOCTOR_HANDLED}


def prompted_messages(messages: list[dict], schema: Any) -> list[dict]:
    """The same messages, the schema stated in the system message (a new one first when there is none).
    The caller's list and its messages are not changed."""
    text = INSTRUCTION + json.dumps(schema, sort_keys=True, ensure_ascii=False)
    result = [dict(message) for message in messages]
    if result and result[0].get("role") == "system" and isinstance(result[0].get("content"), str):
        result[0]["content"] = result[0]["content"] + "\n\n" + text
    else:
        result.insert(0, {"role": "system", "content": text})
    return result


def unwrap(reply: dict) -> dict:
    """The reply, its content without one enclosing ```json fence; anything else is left for the caller's
    validation as it is."""
    message = reply.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        fenced = _FENCE.match(content.strip())
        if fenced:
            reply = {**reply, "message": {**message, "content": fenced.group(1).strip()}}
    return {**reply, "structured_output": "prompted"}
