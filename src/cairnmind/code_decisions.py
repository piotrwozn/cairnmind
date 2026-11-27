"""Per-project opt-in ingestion of decisions from code comments, ADRs, or Git trailers (#412).

Each project picks at most one decision source (`off` by default; `comments`, `adr`, or
`trailers`). Every source follows the same invariants:
- runs only when enabled for that project (off by default);
- creates only `proposed` records, never `confirmed`;
- passes through every write guard (single-writer store, read-only/not-writer refusal,
  unfinished restore, active upgrade mark and setup mark);
- is idempotent (reading the same text twice creates one record and never alters a
  record a person confirmed, disputed or superseded);
- is non-destructive (removing a tag, comment, ADR file, or symbol never deletes
  records; evidence states report `tag_removed` / `symbol_gone` / `orphaned`).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from .sql import utc_now
from .storage_query import db_find, db_find_one, db_insert_document, db_update_where
from .symbol_anchor import (
    COMMENT_TAGS,
    find_symbol_in_analysis,
    make_symbol_anchor,
    posix_path,
)

DECISION_SOURCES = ("off", "comments", "adr", "trailers")
DEFAULT_DECISION_SOURCE = "off"
SETTING = "decision_source"
TRAILER_COMMIT_LIMIT = 500

_ADR_DIR_PREFIXES = ("docs/adr/", "docs/adrs/", "doc/adr/", "doc/adrs/", "adr/", "adrs/")
_ADR_IGNORED_NAMES = frozenset({"readme.md", "index.md", "template.md", "_template.md"})
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_ADR_HEADING_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
_ADR_SECTION_RE = re.compile(r"^\s*#{2,3}\s+(Decision|Invariant|Rule|Summary)\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_SECTION_RE = re.compile(r"^\s*#{1,3}\s+\S", re.MULTILINE)
_TRAILER_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*:\s*(.+?)\s*$")
_TRAILER_TAGS: dict[str, str] = {
    "decision": "decision",
    "invariant": "constraint",
    "why": "decision",
}
_ADR_REF_RE = re.compile(r"\bADR[-_ ]?(\d+[A-Za-z0-9_-]*)\b", re.IGNORECASE)


def get_decision_source(reader, project_id: str) -> str:
    """Return the configured per-project decision source (`off` by default)."""
    row = db_find_one(reader, "memory_settings", {"id": project_id}) or {}
    value = row.get(SETTING)
    return value if isinstance(value, str) and value in DECISION_SOURCES else DEFAULT_DECISION_SOURCE


def _write_refusal(store) -> str | None:
    """Return why this installation may not write right now, or None when writes are allowed."""
    if getattr(store, "read_only", False):
        return getattr(store, "not_writer", None) or "store is read-only"
    if getattr(store, "_unfinished_restore", False):
        return "an unfinished restore holds this store"
    from . import setup_mark, upgrade_mark
    reason = upgrade_mark.refusal()
    if reason:
        return str(reason)
    reason = setup_mark.refusal(store.data_dir)
    if reason:
        return str(reason)
    return None


def set_decision_source(store, project_id: str, source: str) -> dict[str, Any]:
    """Configure the single per-project decision source (`off`, `comments`, `adr`, or `trailers`)."""
    choice = source.strip().lower() if isinstance(source, str) else ""
    if choice not in DECISION_SOURCES:
        raise ValueError(f"Unknown decision source {source!r}; choose one of: {', '.join(DECISION_SOURCES)}.")
    if getattr(store, "not_writer", None):
        from .store_writer import StoreNotWriter
        raise StoreNotWriter(store.not_writer)
    if getattr(store, "read_only", False):
        from .store_readonly import ReadOnlyStoreError
        raise ReadOnlyStoreError("A store opened for reading cannot change project decision settings")
    from . import setup_mark, upgrade_mark
    for mark_reason in (upgrade_mark.refusal(), setup_mark.refusal(store.data_dir)):
        if mark_reason:
            raise ValueError(str(mark_reason))

    project = store.resolve_project(project_id)
    pid = project["id"]
    previous = get_decision_source(store, pid)
    if choice != previous:
        with store.transaction() as tx:
            store._require_project(tx, pid)
            if db_find(tx, "memory_settings", {"id": pid}, columns=("id",)):
                db_update_where(tx, "memory_settings", set_fields={SETTING: choice, "updated_at": utc_now()},
                                where={"id": pid})
            else:
                db_insert_document(tx, "memory_settings", {
                    "id": pid,
                    "project_id": pid,
                    "enabled": False,
                    "interval_minutes": 15,
                    "worktree": str(project["root"]),
                    SETTING: choice,
                    "updated_at": utc_now(),
                })
        from .ingest import forget_index_signature
        forget_index_signature(store, pid)
    return {
        "project_id": pid,
        "project": project["name"],
        "decision_source": choice,
        "previous": previous,
        "status": "configured" if choice != previous else "unchanged",
    }


def _norm_text(text: str) -> str:
    return " ".join(text.replace("\r\n", "\n").replace("\r", "\n").split()).strip()


def _text_sha256(text: str) -> str:
    return hashlib.sha256(_norm_text(text).encode("utf-8")).hexdigest()


def comment_decision_key(relative_path: str | Path, symbol: str | None, tag: str, text_sha256: str) -> str:
    """Deterministic key for a tagged comment so re-reading the same text creates one record."""
    rel = posix_path(relative_path)
    digest = hashlib.sha256(f"{rel}\0{symbol or ''}\0{tag}\0{text_sha256}".encode("utf-8")).hexdigest()[:24]
    return f"comment:{tag.lower()}:{digest}"


def adr_decision_key(relative_path: str | Path, text_sha256: str) -> str:
    """Deterministic key for an ADR decision so re-reading the same text creates one record."""
    rel = posix_path(relative_path)
    digest = hashlib.sha256(f"{rel}\0{text_sha256}".encode("utf-8")).hexdigest()[:24]
    return f"adr:{digest}"


def trailer_decision_key(tag: str, text_sha256: str) -> str:
    """Deterministic key for a Git trailer decision so reading the same text twice makes one record."""
    digest = hashlib.sha256(f"{tag.lower()}\0{text_sha256}".encode("utf-8")).hexdigest()[:24]
    return f"trailer:{tag.lower()}:{digest}"


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return posix_path(path.relative_to(root))
    except ValueError:
        return posix_path(path.name)


def is_adr_path(relative_path: str | Path) -> bool:
    """Whether a POSIX relative path sits in a recognised ADR folder and is a Markdown ADR file."""
    rel = posix_path(relative_path)
    low = rel.lower()
    if not low.endswith((".md", ".mdx")):
        return False
    name = low.rsplit("/", 1)[-1]
    if name in _ADR_IGNORED_NAMES:
        return False
    return any(low.startswith(prefix) for prefix in _ADR_DIR_PREFIXES)


def _strip_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].strip()
    return text


def parse_adr_document(relative_path: str | Path, content: str) -> dict[str, Any] | None:
    """Parse an ADR Markdown file (optional YAML front matter + title / Decision section)."""
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    front: dict[str, Any] = {}
    body = text
    fm_match = _FRONT_MATTER_RE.match(text)
    if fm_match:
        body = text[fm_match.end():]
        current_list_key: str | None = None
        for raw_line in fm_match.group(1).split("\n"):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- ") and current_list_key is not None:
                front.setdefault(current_list_key, []).append(_strip_quotes(stripped[2:]))
                continue
            current_list_key = None
            if ":" not in stripped:
                continue
            key, raw_val = stripped.split(":", 1)
            key = key.strip().lower()
            val = raw_val.strip()
            if not key:
                continue
            if not val:
                front[key] = []
                current_list_key = key
            elif val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                front[key] = [_strip_quotes(part) for part in inner.split(",") if _strip_quotes(part)] if inner else []
            else:
                front[key] = _strip_quotes(val)

    title = front.get("title") if isinstance(front.get("title"), str) and front["title"].strip() else None
    if not title:
        heading = _ADR_HEADING_RE.search(body)
        if heading:
            title = heading.group(1).strip()

    # Prefer explicit front matter `decision`, else the `## Decision` section, else first body paragraph.
    decision_text = front.get("decision") if isinstance(front.get("decision"), str) and front["decision"].strip() else None
    raw_quote: str | None = decision_text
    if not decision_text:
        sec_match = _ADR_SECTION_RE.search(body)
        if sec_match:
            after = body[sec_match.end():].lstrip("\n")
            nxt = _NEXT_SECTION_RE.search(after)
            section_raw = (after[:nxt.start()] if nxt else after).strip()
            if section_raw:
                decision_text = section_raw
                raw_quote = section_raw
    if not decision_text:
        paragraphs = [
            para.strip()
            for para in re.split(r"\n\s*\n", body.strip())
            if para.strip() and not para.strip().startswith("#")
        ]
        if paragraphs:
            decision_text = paragraphs[0]
            raw_quote = paragraphs[0]
        elif title:
            decision_text = title
            raw_quote = title

    if not decision_text or not decision_text.strip():
        return None

    rel = posix_path(relative_path)
    adr_id = front.get("id") if isinstance(front.get("id"), str) and front["id"].strip() else Path(rel).stem
    raw_kind = str(front.get("kind") or "").strip().lower()
    kind = "constraint" if raw_kind in ("constraint", "invariant") else "decision"

    summary = _norm_text(decision_text)
    if title and _norm_text(title).lower() not in summary.lower():
        content_text = f"{_norm_text(title)}: {summary}"
    else:
        content_text = summary

    # Optional code symbol references in front matter: `path` + `symbol`, or `symbols: ["src/a.py:fn"]`.
    symbol_refs: list[tuple[str, str]] = []
    fm_path = front.get("path") or front.get("file")
    fm_symbol = front.get("symbol")
    if isinstance(fm_path, str) and fm_path.strip() and isinstance(fm_symbol, str) and fm_symbol.strip():
        symbol_refs.append((posix_path(fm_path), fm_symbol.strip()))
    fm_symbols = front.get("symbols")
    if isinstance(fm_symbols, list):
        for ref in fm_symbols:
            if isinstance(ref, str) and ":" in ref:
                rpath, rsym = ref.rsplit(":", 1)
                if rpath.strip() and rsym.strip():
                    symbol_refs.append((posix_path(rpath), rsym.strip()))
            elif isinstance(ref, str) and ref.strip() and isinstance(fm_path, str) and fm_path.strip():
                symbol_refs.append((posix_path(fm_path), ref.strip()))

    evidence = (raw_quote or decision_text).strip()
    if len(evidence) > 1200:
        evidence = evidence[:1200]
    return {
        "adr_id": adr_id,
        "title": title or adr_id,
        "adr_status": front.get("status") if isinstance(front.get("status"), str) else None,
        "kind": kind,
        "path": rel,
        "text": content_text,
        "text_sha256": _text_sha256(content_text),
        "evidence": evidence,
        "symbol_refs": symbol_refs,
    }


def parse_commit_trailers(commit_sha: str, committed_at: str, subject: str, body: str) -> list[dict[str, Any]]:
    """Extract `Decision:`, `Invariant:`, `Why:`, and `Refs: ADR-...` trailers from a Git commit message."""
    normalized = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    # Trailers sit in the final non-empty paragraph (standard Git trailer convention),
    # or any standalone trailing block of `Key: Value` lines.
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    trailer_lines: list[tuple[str, str, str]] = []
    for para in reversed(paragraphs):
        lines = para.split("\n")
        parsed: list[tuple[str, str, str]] = []
        valid_para = True
        for line in lines:
            match = _TRAILER_LINE_RE.match(line)
            if match:
                parsed.append((match.group(1), match.group(2).strip(), line.strip()))
            elif parsed and (line.startswith(" ") or line.startswith("\t")) and line.strip():
                prev_key, prev_val, prev_raw = parsed[-1]
                parsed[-1] = (prev_key, f"{prev_val} {line.strip()}", f"{prev_raw}\n{line}")
            else:
                valid_para = False
                break
        if valid_para and parsed:
            trailer_lines = parsed
            break
    if not trailer_lines:
        return []

    refs: list[str] = []
    entries: list[dict[str, Any]] = []
    for key, value, raw_line in trailer_lines:
        low = key.lower()
        if low in ("refs", "ref", "adr"):
            for m in _ADR_REF_RE.finditer(value):
                ref_label = f"ADR-{m.group(1)}"
                if ref_label not in refs:
                    refs.append(ref_label)
            if not refs and value.strip():
                refs.append(value.strip())
    for key, value, raw_line in trailer_lines:
        low = key.lower()
        if low in _TRAILER_TAGS:
            clean = _norm_text(value)
            if not clean:
                continue
            entries.append({
                "tag": key.upper(),
                "kind": _TRAILER_TAGS[low],
                "text": clean,
                "text_sha256": _text_sha256(clean),
                "raw": raw_line,
                "commit": commit_sha,
                "committed_at": committed_at,
                "subject": _norm_text(subject),
                "refs": list(refs),
            })
    # If the commit carries `Refs: ADR-...` without an explicit `Decision:`/`Invariant:`/`Why:` trailer,
    # propose a decision linked to the referenced ADR using the commit subject.
    if not entries and refs and _norm_text(subject):
        adr_refs = [ref for ref in refs if ref.upper().startswith("ADR-")]
        if adr_refs:
            text = f"{_norm_text(subject)} ({', '.join(adr_refs)})"
            entries.append({
                "tag": "REFS",
                "kind": "decision",
                "text": text,
                "text_sha256": _text_sha256(text),
                "raw": f"Refs: {', '.join(adr_refs)}",
                "commit": commit_sha,
                "committed_at": committed_at,
                "subject": _norm_text(subject),
                "refs": adr_refs,
            })
    return entries


def _git_trailer_entries(root: Path, limit: int = TRAILER_COMMIT_LIMIT) -> list[dict[str, Any]]:
    """Read recent non-merge commits from `root` without taking Git locks and extract trailers."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                "--no-merges",
                f"--max-count={limit}",
                "--format=%H%x1f%aI%x1f%s%x1f%b%x1e",
            ],
            capture_output=True,
            timeout=15,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []
    decoded = proc.stdout.decode("utf-8", errors="replace")
    found: list[dict[str, Any]] = []
    for chunk in decoded.split("\x1e"):
        chunk = chunk.strip("\n\r")
        if not chunk:
            continue
        parts = chunk.split("\x1f", 3)
        if len(parts) != 4:
            continue
        sha, committed_at, subject, body = parts
        sha = sha.strip()
        if not sha:
            continue
        found.extend(parse_commit_trailers(sha, committed_at.strip(), subject, body))
    return found


def _record_exists(store, project_id: str, kind: str, key: str) -> bool:
    return bool(
        db_find(
            store,
            "records",
            {"project_id": project_id, "kind": kind, "key": key},
            columns=("id",),
            limit=1,
        )
    )


def _ingest_comments(store, project_id: str, indexed: dict[Path, dict[str, Any]]) -> dict[str, Any]:
    created = existing = skipped = 0
    entries = sorted(
        indexed.items(),
        key=lambda pair: _relative_posix(pair[0], Path(pair[1]["root"])),
    )
    for path, entry in entries:
        source_id = (entry.get("source") or {}).get("id")
        analysis = entry.get("analysis") or {}
        comments = analysis.get("tagged_comments") or []
        if not source_id or not comments:
            continue
        root = Path(entry["root"])
        rel = _relative_posix(path, root)
        for item in comments:
            tag = item.get("tag")
            text = item.get("text")
            text_sha256 = item.get("text_sha256")
            if tag not in COMMENT_TAGS or not isinstance(text, str) or not text.strip() or not isinstance(text_sha256, str):
                skipped += 1
                continue
            kind = COMMENT_TAGS[tag]
            symbol = item.get("symbol") if isinstance(item.get("symbol"), str) and item.get("symbol") else None
            symbol_kind = item.get("symbol_kind")
            symbol_hash = item.get("symbol_body_hash")
            key = comment_decision_key(rel, symbol, tag, text_sha256)
            if _record_exists(store, project_id, kind, key):
                existing += 1
                continue
            anchor = (
                make_symbol_anchor(rel, symbol, symbol_kind, symbol_hash)
                if symbol and symbol_kind and symbol_hash
                else None
            )
            comment_anchor = {
                "tag": tag,
                "path": rel,
                "symbol": symbol,
                "text_sha256": text_sha256,
                "line_start": item.get("line_start"),
                "line_end": item.get("line_end"),
            }
            raw_quote = item.get("raw") if isinstance(item.get("raw"), str) and item["raw"].strip() else f"{tag}: {text}"
            metadata: dict[str, Any] = {
                "origin": "code_comment",
                "evidence": raw_quote,
                "path": str(path),
                "root": str(root),
            }
            if symbol:
                metadata["symbol"] = symbol
            store.remember(
                project_id,
                {"kind": kind, "status": "proposed", "key": key, "content": text, "metadata": metadata},
                [source_id],
                symbol_anchor=anchor,
                comment_anchor=comment_anchor,
            )
            created += 1
    return {"source": "comments", "created": created, "existing": existing, "skipped": skipped}


def _ingest_adrs(store, project_id: str, indexed: dict[Path, dict[str, Any]]) -> dict[str, Any]:
    created = existing = skipped = 0
    by_rel: dict[str, dict[str, Any]] = {}
    entries = sorted(
        indexed.items(),
        key=lambda pair: _relative_posix(pair[0], Path(pair[1]["root"])),
    )
    for path, entry in entries:
        rel = _relative_posix(path, Path(entry["root"]))
        by_rel[rel] = entry

    for path, entry in entries:
        source_id = (entry.get("source") or {}).get("id")
        if not source_id:
            continue
        root = Path(entry["root"])
        rel = _relative_posix(path, root)
        if not is_adr_path(rel):
            continue
        source_row = store.get_source(source_id)
        parsed = parse_adr_document(rel, str(source_row.get("content") or ""))
        if parsed is None:
            skipped += 1
            continue
        key = adr_decision_key(rel, parsed["text_sha256"])
        if _record_exists(store, project_id, parsed["kind"], key):
            existing += 1
            continue
        # Optionally resolve a symbol anchor if the ADR front matter explicitly names a code file and symbol.
        anchor = None
        for ref_path, ref_symbol in parsed["symbol_refs"]:
            target = by_rel.get(ref_path)
            if not target:
                continue
            sym = find_symbol_in_analysis(target.get("analysis"), ref_symbol)
            if sym and isinstance(sym.get("body_hash"), str):
                anchor = make_symbol_anchor(
                    ref_path,
                    sym.get("qualified_name") or sym["name"],
                    sym["kind"],
                    sym["body_hash"],
                )
                break
        adr_anchor = {
            "adr_id": parsed["adr_id"],
            "title": parsed["title"],
            "adr_status": parsed["adr_status"],
            "path": rel,
            "text_sha256": parsed["text_sha256"],
        }
        metadata: dict[str, Any] = {
            "origin": "adr_file",
            "evidence": parsed["evidence"],
            "path": str(path),
            "root": str(root),
        }
        if anchor is not None:
            metadata["symbol"] = anchor["symbol"]
        store.remember(
            project_id,
            {"kind": parsed["kind"], "status": "proposed", "key": key, "content": parsed["text"], "metadata": metadata},
            [source_id],
            symbol_anchor=anchor,
            adr_anchor=adr_anchor,
        )
        created += 1
    return {"source": "adr", "created": created, "existing": existing, "skipped": skipped}


def _ingest_trailers(store, project_id: str, roots: list[Path]) -> dict[str, Any]:
    created = existing = skipped = 0
    for root in sorted({Path(r).resolve() for r in roots}):
        if not root.is_dir():
            continue
        entries = _git_trailer_entries(root)
        for item in entries:
            key = trailer_decision_key(item["tag"], item["text_sha256"])
            if _record_exists(store, project_id, item["kind"], key):
                existing += 1
                continue
            commit_text = f"commit {item['commit']}\n{item['subject']}\n\n{item['raw']}\n"
            src = store.snapshot(
                project_id,
                f"git://commit/{item['commit']}",
                commit_text,
                "manual",
                {
                    "origin": "git_trailer",
                    "role": "git",
                    "git_commit": item["commit"],
                    "source_timestamp": item["committed_at"],
                    "root": str(root),
                },
            )
            trailer_anchor = {
                "tag": item["tag"],
                "commit": item["commit"],
                "committed_at": item["committed_at"],
                "refs": item["refs"],
                "text_sha256": item["text_sha256"],
            }
            metadata: dict[str, Any] = {
                "origin": "git_trailer",
                "evidence": item["raw"],
                "root": str(root),
            }
            store.remember(
                project_id,
                {"kind": item["kind"], "status": "proposed", "key": key, "content": item["text"], "metadata": metadata},
                [src["id"]],
                trailer_anchor=trailer_anchor,
            )
            created += 1
    return {"source": "trailers", "created": created, "existing": existing, "skipped": skipped}


def ingest_project_decisions(
    store,
    project_id: str,
    indexed: dict[Path, dict[str, Any]],
    *,
    roots: list[Path] | None = None,
) -> dict[str, Any]:
    """Create proposed records from the single configured decision source of `project_id`."""
    mode = get_decision_source(store, project_id)
    if mode == "off":
        return {"source": "off", "created": 0, "existing": 0, "skipped": 0}
    refused = _write_refusal(store)
    if refused:
        return {"source": mode, "created": 0, "existing": 0, "skipped": 0, "refused": refused}

    if mode == "comments":
        return _ingest_comments(store, project_id, indexed)
    if mode == "adr":
        return _ingest_adrs(store, project_id, indexed)
    if mode == "trailers":
        target_roots = list(roots) if roots is not None else sorted({Path(entry["root"]) for entry in indexed.values()})
        return _ingest_trailers(store, project_id, target_roots)
    return {"source": mode, "created": 0, "existing": 0, "skipped": 0}
