"""Persistent local cache for verification verdicts (`verdict_cache.sqlite3`).

Keys strictly bind every verification input so any file edit, quote change, context-window
change, prompt/schema revision, verifier upgrade, or model switch triggers an automatic cache miss:
  - verifier_version
  - stage ("support" | "names")
  - verification_mode ("strict_llm" | "nli")
  - question_norm
  - claim_norm
  - citations: sorted [{path, secondary_file, quote_sha256, file_fingerprint, context_window_sha256}]
  - model_id
  - prompt_hash

Transient failures, timeouts and unparsed outputs are NEVER cached.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from .private_files import make_private
from .retrieval import json_value, secondary_source

VERIFIER_VERSION = 1
CACHE_FILENAME = "verdict_cache.sqlite3"
CONTEXT_WINDOW_RADIUS = 4


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def context_window_text(full_text: str, line_start: int | None, line_end: int | None,
                        radius: int = CONTEXT_WINDOW_RADIUS) -> str:
    """Extract the ±radius surrounding lines around `[line_start, line_end]` (1-indexed)."""
    if not isinstance(full_text, str) or not full_text:
        return ""
    lines = full_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not isinstance(line_start, int) or not isinstance(line_end, int) or line_start < 1:
        return full_text[:2000]
    first = max(0, line_start - 1 - radius)
    last = min(len(lines), max(line_start, line_end) + radius)
    return "\n".join(lines[first:last])


def prompt_fingerprint(system_prompt: str, schema: dict[str, Any] | None = None) -> str:
    payload = json.dumps({"system": system_prompt, "schema": schema}, ensure_ascii=False, sort_keys=True)
    return _sha256(payload)


def citation_fingerprint_entries(sources: list[dict[str, Any]], evidence: dict[str, dict[str, Any]] | None = None,
                                 *, source_text_fn=None) -> list[dict[str, Any]]:
    """Build deterministic per-citation fingerprint entries for the cache key."""
    entries: list[dict[str, Any]] = []
    for source in sources:
        metadata = json_value(source.get("metadata"), {})
        relative = metadata.get("relative_path") if isinstance(metadata, dict) else None
        path = relative if isinstance(relative, str) and relative else str(source.get("uri", ""))
        quote = str(source.get("quote", ""))
        file_fp = str(source.get("sha256", ""))
        full_text = ""
        if evidence and source.get("id") in evidence:
            ev = evidence[source["id"]]
            full_text = source_text_fn(ev) if source_text_fn else str(ev.get("content", ""))
        elif isinstance(source.get("content"), str):
            full_text = source["content"]
        window = context_window_text(full_text, source.get("line_start"), source.get("line_end")) if full_text else quote
        entries.append({
            "path": path[:240],
            "secondary_file": bool(secondary_source(path)),
            "quote_sha256": _sha256(quote),
            "file_fingerprint": file_fp,
            "context_window_sha256": _sha256(window),
        })
    entries.sort(key=lambda item: (item["path"], item["file_fingerprint"], item["quote_sha256"],
                                   item["context_window_sha256"], item["secondary_file"]))
    return entries


def verdict_cache_key(
    *,
    stage: str,
    question: str,
    claim: str,
    sources: list[dict[str, Any]],
    model_id: str,
    prompt_hash: str,
    verification_mode: str = "strict_llm",
    evidence: dict[str, dict[str, Any]] | None = None,
    source_text_fn=None,
    verifier_version: int = VERIFIER_VERSION,
) -> str:
    """Compute the strict SHA-256 verdict cache key."""
    material = {
        "verifier_version": int(verifier_version),
        "stage": stage,
        "verification_mode": verification_mode,
        "question_norm": _normalize_text(question),
        "claim_norm": _normalize_text(claim),
        "citations": citation_fingerprint_entries(sources, evidence, source_text_fn=source_text_fn),
        "model_id": str(model_id or ""),
        "prompt_hash": str(prompt_hash or ""),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(encoded)


def cache_enabled() -> bool:
    raw = os.environ.get("CAIRNMIND_VERDICT_CACHE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


class VerdictCache:
    """Persistent SQLite-backed verdict cache in `<data_dir>/verdict_cache.sqlite3`."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser()
        self.path = self.data_dir / CACHE_FILENAME

    def _connect(self, *, create: bool = True) -> sqlite3.Connection | None:
        if not create and not self.path.is_file():
            return None
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            existed = self.path.exists()
            conn = sqlite3.connect(str(self.path), timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS verdicts ("
                "  cache_key TEXT PRIMARY KEY,"
                "  stage TEXT NOT NULL,"
                "  verdict_json TEXT NOT NULL,"
                "  created_at REAL NOT NULL,"
                "  hits INTEGER NOT NULL DEFAULT 0"
                ");"
            )
            if not existed:
                try:
                    make_private(self.path)
                except OSError:
                    pass
            return conn
        except (OSError, sqlite3.Error):
            return None

    def get(self, key: str) -> dict[str, Any] | None:
        if not cache_enabled():
            return None
        conn = self._connect(create=False)
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT verdict_json FROM verdicts WHERE cache_key = ?", (key,)).fetchone()
            if row is None:
                return None
            try:
                value = json.loads(row[0])
            except (ValueError, TypeError):
                return None
            if not isinstance(value, dict):
                return None
            try:
                conn.execute("UPDATE verdicts SET hits = hits + 1 WHERE cache_key = ?", (key,))
                conn.commit()
            except sqlite3.Error:
                pass
            return value
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    def put(self, key: str, stage: str, verdict: dict[str, Any]) -> bool:
        if not cache_enabled():
            return False
        conn = self._connect(create=True)
        if conn is None:
            return False
        try:
            encoded = json.dumps(verdict, ensure_ascii=False, sort_keys=True)
            conn.execute(
                "INSERT INTO verdicts (cache_key, stage, verdict_json, created_at, hits) "
                "VALUES (?, ?, ?, ?, 0) "
                "ON CONFLICT(cache_key) DO UPDATE SET verdict_json=excluded.verdict_json, created_at=excluded.created_at",
                (key, stage, encoded, time.time()),
            )
            conn.commit()
            return True
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def clear(self) -> dict[str, Any]:
        """Remove all cached verification verdicts without starting any service or database engine."""
        if not self.path.is_file():
            return {"status": "cleared", "deleted_entries": 0, "path": str(self.path)}
        conn = self._connect(create=False)
        deleted = 0
        if conn is not None:
            try:
                row = conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()
                deleted = int(row[0]) if row else 0
                conn.execute("DELETE FROM verdicts")
                conn.commit()
            finally:
                conn.close()
        return {"status": "cleared", "deleted_entries": deleted, "path": str(self.path)}

    def stats(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"exists": False, "entries": 0, "total_hits": 0, "bytes": 0, "path": str(self.path)}
        conn = self._connect(create=False)
        if conn is None:
            return {"exists": True, "entries": 0, "total_hits": 0, "bytes": self.path.stat().st_size, "path": str(self.path)}
        try:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(hits), 0) FROM verdicts").fetchone()
            entries = int(row[0]) if row else 0
            hits = int(row[1]) if row else 0
            return {
                "exists": True,
                "entries": entries,
                "total_hits": hits,
                "bytes": self.path.stat().st_size,
                "path": str(self.path),
            }
        finally:
            conn.close()
