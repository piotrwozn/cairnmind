"""Changed weights behind the same model name on an OpenAI-compatible embedding server (#212).

Such a server lists no digest, so the identity of its embedding model is only the server and the name
(memory_openai.OpenAICompatible.model_identity). If it starts serving other weights under that name, with
the same dimension, vectors of two models would be searched together. To detect it, Cairnmind sends one
fixed, short request to the embedding role (FINGERPRINT_TEXT: a constant that holds no memory content)
when the service starts and before `rebuild-vectors` rebuilds vectors, and compares the vector with the
one stored for that identity in models.fingerprints.json.

- The first answer for an identity is stored as its reference; the identity is unchanged.
- The same answer (cosine similarity at least SIMILARITY: a server's float noise is far smaller) changes
  nothing.
- Another answer marks the identity changed. While it is, the identity cannot be named: no new vector is
  made, and search uses full-text search only, so old and new vectors are never mixed. `doctor` reports
  it. If the earlier weights come back, the next check clears the mark.
- `rebuild-vectors` accepts the new answer: the reference is replaced and the identity gets the next
  generation (NAME@SERVER~wN), a new vector namespace. Vectors of every earlier generation are kept apart
  and never searched again, as after changing the model; a running service queues the missing vectors.

The local Ollama (and an Ollama profile) reports a digest: that is the identity and no request is sent.
"""
from __future__ import annotations

import fcntl
import json
import math
from pathlib import Path
import time
from typing import Any

from .ollama import OllamaUnavailable

FINGERPRINT_TEXT = "Cairnmind embedding fingerprint: a fixed sentence that is the same on every computer."
FILE = "models.fingerprints.json"
LOCK = "models.fingerprints.lock"
SIMILARITY = 0.9999
MAX_RECORDS = 50
MAX_DIMENSION = 16384
CHANGED = "embedding_changed"
UNCHECKED = "embedding_unchecked"
CHANGED_TEXT = ("The embedding model answered the fixed check differently: other weights are served under the "
                "same name. No new vectors are made and search uses text only until you run cairnmind "
                "rebuild-vectors PROJECT.")
UNCHECKED_TEXT = "The embedding model has not answered the fixed check since the service started."
NEXT_STEP = ("Run cairnmind rebuild-vectors PROJECT: it accepts the new weights and their vectors are kept apart "
             "from the earlier ones.")

_cache: dict[Path, tuple[tuple[int, int, int], dict]] = {}


def _valid(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    vector, generation = record.get("vector"), record.get("generation")
    return (isinstance(vector, list) and 0 < len(vector) <= MAX_DIMENSION
            and all(type(value) in (int, float) and math.isfinite(value) for value in vector)
            and type(generation) is int and generation >= 0 and type(record.get("changed", False)) is bool)


def _read(data_dir) -> dict:
    """The stored records by base identity; an unreadable or invalid file counts as none (first check)."""
    path = Path(data_dir) / FILE
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = document.get("records") if isinstance(document, dict) and document.get("version") == 1 else None
    records = {name: row for name, row in rows.items() if isinstance(name, str) and _valid(row)} if isinstance(
        rows, dict) else {}
    _cache[path] = (key, records)
    return records


def _write(data_dir, records: dict) -> None:
    from .memory_models import _write_json
    if len(records) > MAX_RECORDS:  # the oldest checked identities go first
        records = dict(sorted(records.items(), key=lambda item: item[1].get("checked_at", ""))[-MAX_RECORDS:])
    _write_json(Path(data_dir) / FILE, {"version": 1, "records": records})


def _named(base: str, generation: int) -> str:
    return base if generation == 0 else f"{base}~w{generation}"


def identity(data_dir, base: str, *, checked: bool = True) -> str:
    """The vector namespace of an OpenAI-compatible embedding model, or OllamaUnavailable while it is
    changed (CHANGED) or not checked yet in a process that must check first (UNCHECKED).

    data_dir is the installation's data directory, also for a client built for an evaluation copy.
    """
    record = _read(data_dir).get(base)
    if record and record.get("changed"):
        raise OllamaUnavailable(CHANGED_TEXT, endpoint="/embeddings", error_code=CHANGED)
    if not checked:
        raise OllamaUnavailable(UNCHECKED_TEXT, endpoint="/embeddings", error_code=UNCHECKED)
    return _named(base, record["generation"] if record else 0)


def similarity(left: list, right: list) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def client_of(models):
    """The embedding client that needs a fingerprint (an OpenAI-compatible server), or None."""
    from .memory_models import MemoryModels
    from .memory_openai import OpenAICompatible
    # Only known client types are inspected: a stand-in for the models is never touched.
    client = models.embedding_client if isinstance(models, MemoryModels) else models
    return client if isinstance(client, OpenAICompatible) and client.role == "embedding" else None


async def check(client, *, accept: bool = False) -> dict:
    """Send the fixed request and compare its answer with the stored one; accept replaces a changed one.

    Raises OllamaUnavailable when the server or the model cannot answer; then nothing is stored.
    """
    base = await client.listed_identity(client.settings.embedding_model)
    vector = [round(float(value), 7) for value in (await client.embed([FINGERPRINT_TEXT]))[0]]
    path = Path(client.fingerprint_dir)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with (path / LOCK).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _cache.pop(path / FILE, None)
        records = dict(_read(path))
        record = records.get(base)
        if record is None:
            record, status, score = {"vector": vector, "generation": 0, "recorded_at": now}, "recorded", None
        else:
            score = similarity(record["vector"], vector)
            record = dict(record)
            if score >= SIMILARITY:
                status = "same"
                record.pop("changed", None)
                record.pop("changed_at", None)
            elif accept:
                status = "accepted"
                record = {"vector": vector, "generation": record["generation"] + 1, "recorded_at": now}
            else:
                status = "changed"
                record.setdefault("changed_at", now)
                record["changed"] = True
        record["checked_at"] = now
        records[base] = record
        _write(path, records)
    client.fingerprint_checked = True
    result = {"status": status, "generation": record["generation"],
              "identity": None if record.get("changed") else _named(base, record["generation"])}
    if score is not None:
        result["similarity"] = round(score, 6)
    return result


def doctor_block(data_dir, base: str) -> tuple[str | None, dict]:
    """Read-only, for doctor: the identity in use (None while changed) and what the stored check says."""
    record = _read(data_dir).get(base)
    if record is None:
        return base, {"recorded": False, "changed": False,
                      "checked_when": "when the service starts and before rebuild-vectors"}
    block: dict[str, Any] = {"recorded": True, "changed": bool(record.get("changed")),
                             "generation": record["generation"], "checked_at": record.get("checked_at")}
    if block["changed"]:
        block.update(changed_at=record.get("changed_at"), error=CHANGED_TEXT, next=NEXT_STEP)
        return None, block
    return _named(base, record["generation"]), block
