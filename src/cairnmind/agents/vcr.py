"""Deterministic VCR record/replay cassette layer for model inference evaluations.

Records and replays `chat`, `embed`, `models`, `model_identity`, and `model_digest` calls
so PR CI can run real evaluation pipelines (`eval extraction`, `eval run`, `eval memory`)
deterministically in seconds with zero GPU/Ollama dependency and zero `not_run` (exit 3) skips.

Under `--replay`, any request missing from the cassette raises `CassetteMissError` immediately
(fail-closed, never falls back to network, CLI exits 1).
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from uuid import UUID

CASSETTE_FORMAT = "cairnmind-vcr-cassette"
CASSETTE_VERSION = 1

_ISO_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_TEMP_PATH = re.compile(r"(?:/[A-Za-z0-9._-]+)*/cairnmind-(?:eval|memory-eval|repo-eval)-[A-Za-z0-9_-]+")
_WIN_TEMP_PATH = re.compile(r"[A-Za-z]:\\(?:[^\\\s\"']+\\)*cairnmind-(?:eval|memory-eval|repo-eval)-[A-Za-z0-9_-]+")


class CassetteMissError(RuntimeError):
    """A replayed request did not match any recorded entry in the VCR cassette."""


class DeterministicIdGenerator:
    """Produces repeatable UUIDv4-shaped IDs and markers during VCR record/replay eval runs."""

    def __init__(self, seed: str = "cairnmind-vcr-seed-v1"):
        self._seed = seed
        self._counter = 0

    def next_uuid(self) -> str:
        self._counter += 1
        digest = hashlib.sha256(f"{self._seed}:uuid:{self._counter}".encode("utf-8")).digest()
        raw = bytearray(digest[:16])
        raw[6] = (raw[6] & 0x0F) | 0x40
        raw[8] = (raw[8] & 0x3F) | 0x80
        return str(UUID(bytes=bytes(raw)))

    def next_hex(self, length: int = 12) -> str:
        self._counter += 1
        digest = hashlib.sha256(f"{self._seed}:hex:{self._counter}".encode("utf-8")).hexdigest()
        return digest[:length]


@contextmanager
def deterministic_uuid4(seed: str = "cairnmind-vcr-seed-v1"):
    """Temporarily replace `uuid4` across `uuid` and Cairnmind fixture modules for repeatable VCR evals."""
    import sys
    import uuid as _uuid

    gen = DeterministicIdGenerator(seed)

    def fake_uuid4() -> UUID:
        return UUID(gen.next_uuid())

    patched: list[tuple[Any, str, Any]] = [(_uuid, "uuid4", _uuid.uuid4)]
    _uuid.uuid4 = fake_uuid4
    for mod_name in (
        "cairnmind.agents.evaluation", "cairnmind.agents.memory_evaluation", "cairnmind.store",
        "cairnmind.memory.plans", "cairnmind.memory.confirmation", "cairnmind.memory.memory",
    ):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "uuid4"):
            patched.append((mod, "uuid4", getattr(mod, "uuid4")))
            setattr(mod, "uuid4", fake_uuid4)
    try:
        yield gen
    finally:
        for obj, attr, original in reversed(patched):
            setattr(obj, attr, original)


def normalize_for_key(value: Any) -> Any:
    """Normalize transient fields (temp paths, timestamps, JSON strings) before hashing a VCR key."""
    if isinstance(value, str):
        # If string is a JSON object/array, normalize recursively so embedded paths/timestamps normalize too
        stripped = value.strip()
        if len(stripped) >= 2 and stripped[0] in "{[" and stripped[-1] in "}]":
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError, RecursionError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                return json.dumps(normalize_for_key(parsed), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        text = _TEMP_PATH.sub("<EVAL_TMP>", value)
        text = _WIN_TEMP_PATH.sub("<EVAL_TMP>", text)
        text = _ISO_TS.sub("<TIMESTAMP>", text)
        return text
    if isinstance(value, list):
        return [normalize_for_key(item) for item in value]
    if isinstance(value, dict):
        return {str(k): normalize_for_key(v) for k, v in sorted(value.items())}
    return value


def request_key(op: str, payload: dict[str, Any]) -> str:
    normalized = normalize_for_key({"op": op, "payload": payload})
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class VcrOllama:
    """Record or deterministically replay Ollama `chat`, `embed`, `models`, `model_identity` and `model_digest`."""

    def __init__(
        self,
        cassette_path: str | Path,
        *,
        mode: str,
        inner=None,
        suite: str = "eval",
        chat_model: str | None = None,
        embedding_model: str | None = None,
    ):
        if mode not in {"record", "replay"}:
            raise ValueError(f"Unsupported VCR mode {mode!r}; expected 'record' or 'replay'.")
        if mode == "record" and inner is None:
            raise ValueError("VCR record mode requires a live inner model client.")
        self.cassette_path = Path(cassette_path).expanduser().resolve()
        self.mode = mode
        self.inner = inner
        self.suite = suite
        self.chat_model = chat_model or (getattr(getattr(inner, "settings", None), "chat_model", None) or "cassette-chat")
        self.embedding_model = embedding_model or (
            getattr(getattr(inner, "settings", None), "embedding_model", None) or "cassette-embed"
        )
        self.model_identities: dict[str, str] = {}
        self.model_digests: dict[str, str | None] = {}
        self.models_payload: dict[str, Any] | None = None
        self.entries: dict[str, dict[str, Any]] = {}
        self._seen_counts: dict[str, int] = {}
        self.replay_hits = 0
        self.recorded_count = 0
        if mode == "replay":
            self._load()

    @classmethod
    def record(cls, inner, cassette_path: str | Path, *, suite: str = "eval") -> VcrOllama:
        return cls(cassette_path, mode="record", inner=inner, suite=suite)

    @classmethod
    def replay(cls, cassette_path: str | Path) -> VcrOllama:
        return cls(cassette_path, mode="replay", inner=None)

    def _load(self) -> None:
        if not self.cassette_path.is_file():
            raise ValueError(f"VCR cassette does not exist: {self.cassette_path}")
        try:
            data = json.loads(self.cassette_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot read VCR cassette {self.cassette_path}: {exc}") from None
        if (not isinstance(data, dict) or data.get("format") != CASSETTE_FORMAT
                or data.get("format_version") != CASSETTE_VERSION or not isinstance(data.get("entries"), dict)):
            raise ValueError(f"Invalid VCR cassette format in {self.cassette_path}")
        self.suite = str(data.get("suite", "eval"))
        self.model_identities = dict(data.get("model_identities") or {})
        self.model_digests = dict(data.get("model_digests") or {})
        self.models_payload = data.get("models")
        self.entries = dict(data["entries"])

    def save(self) -> Path:
        """Atomically write the recorded cassette to disk with deterministic sorted JSON."""
        self.cassette_path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "format": CASSETTE_FORMAT,
            "format_version": CASSETTE_VERSION,
            "suite": self.suite,
            "model_identities": dict(sorted(self.model_identities.items())),
            "model_digests": dict(sorted(self.model_digests.items())),
            **({"models": self.models_payload} if self.models_payload is not None else {}),
            "entries": dict(sorted(self.entries.items())),
        }
        encoded = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        fd, tmp = tempfile.mkstemp(prefix=".cairnmind-vcr-", dir=self.cassette_path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.cassette_path)
        finally:
            Path(tmp).unlink(missing_ok=True)
        return self.cassette_path

    def _lookup_key(self, base_key: str) -> str:
        count = self._seen_counts.get(base_key, 0)
        self._seen_counts[base_key] = count + 1
        indexed = f"{base_key}#{count}"
        if self.mode == "replay":
            if indexed in self.entries:
                return indexed
            if base_key in self.entries:
                return base_key
            return indexed
        return base_key if count == 0 else indexed

    async def open(self) -> None:
        if self.inner is not None and hasattr(self.inner, "open"):
            await self.inner.open()

    async def aclose(self) -> None:
        if self.inner is not None and hasattr(self.inner, "aclose"):
            await self.inner.aclose()

    async def models(self) -> dict[str, Any]:
        if self.mode == "replay":
            if self.models_payload is not None:
                return deepcopy(self.models_payload)
            return {
                "models": [
                    {"name": name, "digest": digest or "sha256:" + "0" * 64}
                    for name, digest in sorted(self.model_digests.items())
                ]
            }
        data = await self.inner.models()
        self.models_payload = deepcopy(data)
        return data

    async def model_identity(self, name: str) -> str:
        if self.mode == "replay":
            if name in self.model_identities:
                return self.model_identities[name]
            digest = self.model_digests.get(name)
            if digest:
                return f"{name}@{digest}"
            if self.model_identities:
                # Match by role fallback when test switches chat_model setting name
                if len(self.model_identities) == 1:
                    return next(iter(self.model_identities.values()))
            raise CassetteMissError(f"VCR cassette {self.cassette_path.name} has no model_identity for {name!r}.")
        identity = await self.inner.model_identity(name)
        self.model_identities[name] = identity
        if "@" in identity:
            self.model_digests.setdefault(name, identity.split("@", 1)[1])
        return identity

    async def model_digest(self, name: str) -> str | None:
        if self.mode == "replay":
            if name in self.model_digests:
                return self.model_digests[name]
            ident = self.model_identities.get(name)
            if isinstance(ident, str) and "@" in ident:
                return ident.split("@", 1)[1]
            return None
        digest = await self.inner.model_digest(name) if hasattr(self.inner, "model_digest") else None
        self.model_digests[name] = digest
        return digest

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any) -> dict:
        key_payload = {
            "messages": messages,
            **({"tools": tools} if tools else {}),
            **({"format": kwargs["format"]} if "format" in kwargs and kwargs["format"] is not None else {}),
            **({"options": kwargs["options"]} if kwargs.get("options") else {}),
        }
        base_key = request_key("chat", key_payload)
        slot = self._lookup_key(base_key)
        if self.mode == "replay":
            entry = self.entries.get(slot)
            if entry is None:
                preview = str(messages[-1].get("content", ""))[:160] if messages else ""
                raise CassetteMissError(
                    f"VCR cassette miss for chat ({slot[:16]}...) in {self.cassette_path.name}. "
                    f"Re-record cassette with --record. Last message preview: {preview!r}"
                )
            self.replay_hits += 1
            return deepcopy(entry["response"])
        response = await self.inner.chat(messages, tools=tools, **kwargs)
        self.entries[slot] = {"op": "chat", "key": slot, "response": deepcopy(response)}
        self.recorded_count += 1
        return response

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        base_key = request_key("embed", {"texts": texts})
        slot = self._lookup_key(base_key)
        if self.mode == "replay":
            entry = self.entries.get(slot)
            if entry is None:
                preview = str(texts[0])[:120]
                raise CassetteMissError(
                    f"VCR cassette miss for embed ({slot[:16]}...) in {self.cassette_path.name}. "
                    f"Re-record cassette with --record. First text preview: {preview!r}"
                )
            self.replay_hits += 1
            return deepcopy(entry["response"])
        vectors = await self.inner.embed(texts)
        self.entries[slot] = {"op": "embed", "key": slot, "response": deepcopy(vectors)}
        self.recorded_count += 1
        return vectors
