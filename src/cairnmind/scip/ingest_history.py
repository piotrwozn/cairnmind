"""Codex and Claude CLI transcript schema parsing, reparsing, import, and cursor polling."""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import UUID

from cairnmind.memory.secret_mask import ingest_masker, masked_metadata
from cairnmind.scip.ingest_batch import _ingestion_lock, _write_chunks
from cairnmind.scip.ingest_walk import MAX_FILE_BYTES, _chunks, _identifier, _json, _metadata, _now, _roots
from cairnmind.storage.storage_query import (
    asc,
    db_delete_where,
    db_find,
    db_find_one,
    db_update_where,
    db_upsert_document,
    eq,
)

TRANSCRIPT_PARSER_REVISION = 3
UNKNOWN_TRANSCRIPT_WARNING = "Unknown transcript schema; raw record retained"
CODEX_TECHNICAL_RECORDS = {"world_state", "token_usage_record"}
_CLAUDE_TECHNICAL_FIELDS = {
    "mode": {"mode": str},
    "permission-mode": {"permissionMode": str},
    "session-owner": {"accountUuid": str, "organizationUuid": str},
    "atis-latch": {"atis": str},
    "attachment": {"parentUuid": (type(None), str), "isSidechain": bool, "attachment": dict,
                   "uuid": str, "timestamp": str, "userType": str, "entrypoint": str,
                   "cwd": str, "version": str, "gitBranch": str},
    "api-request-shape": {"shapeHash": str, "timestamp": str, "version": str, "shape": dict},
    "api-request": {"id": str, "timestamp": str, "version": str, "querySource": str,
                    "shapeHash": str, "params": dict, "keep": int, "tail": list,
                    "cacheControl": list, "requestDigest": str},
    "last-prompt": {"leafUuid": str},
    "cost-state": {"totalCostUSD": (int, float), "totalAPIDuration": int,
                   "totalAPIDurationWithoutRetries": int, "totalToolDuration": int,
                   "totalLinesAdded": int, "totalLinesRemoved": int, "totalDuration": int,
                   "startTime": int, "modelUsage": dict, "hasUnknownModelCost": bool},
    "ai-title": {"aiTitle": str},
    "tools-used": {"tools": list},
}
# Fields some client versions omit: absent is accepted, present must still have the type.
# Claude Code 2.1.325 writes last-prompt records with only leafUuid (#155).
_CLAUDE_OPTIONAL_FIELDS = {"last-prompt": {"lastPrompt": str}}
CLAUDE_TECHNICAL_RECORDS = set(_CLAUDE_TECHNICAL_FIELDS)


def _save_cursor(store, path: Path, project_id: str | None, client: str, offset: int) -> None:
    with store.transaction() as tx:
        if project_id and not db_find_one(tx, "projects", {"id": project_id}, columns=("id",)):
            raise ValueError("Project no longer exists; history cursor was not recreated")
        db_upsert_document(
            tx,
            "import_cursors",
            {"id": _identifier(path), "path": str(path), "project_id": project_id, "client": client,
             "offset": offset, "fingerprint": _fingerprint(path, offset), "updated_at": _now()},
            key_field="path",
        )


def _event_cwd(event: dict) -> str | None:
    value = event.get("cwd")
    if not value and isinstance(event.get("payload"), dict):
        value = event["payload"].get("cwd")
    return value if isinstance(value, str) else None


def _codex_technical_metadata(event: dict) -> bool:
    """Validate the two Codex 0.166 technical rollout payloads we understand.

    These payloads carry accounting/state metadata, never user decisions. Keep
    the original JSONL record rather than turning arbitrary state into text.
    Unknown fields follow serde's forward-compatible handling; required fields
    and their actual wire types must still match the official schema.
    """
    kind, payload = event.get("type"), event.get("payload")
    if not isinstance(payload, dict):
        return False
    if kind == "world_state":
        return type(payload.get("full")) is bool and isinstance(payload.get("state"), dict)
    if kind != "token_usage_record":
        return False
    for key in ("thread_id", "session_id"):
        if not isinstance(payload.get(key), str):
            return False
        try:
            UUID(payload[key])
        except ValueError:
            return False
    if not all(isinstance(payload.get(key), str) for key in ("turn_id", "root_turn_id", "response_id")):
        return False
    required = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    for name in ("usage", "turn_token_usage", "thread_token_usage"):
        usage = payload.get(name)
        if not isinstance(usage, dict):
            return False
        for key in (*required, *(("cache_write_input_tokens",) if "cache_write_input_tokens" in usage else ())):
            value = usage.get(key)
            if type(value) is not int or not -(2**63) <= value < 2**63:
                return False
        budget = usage.get("codex_rollout_budget_units")
        if budget is not None and (type(budget) not in {int, float}
                                   or (isinstance(budget, float) and not math.isfinite(budget))):
            return False
    return True


def _claude_technical_metadata(event: dict) -> bool:
    """Validate the observed Claude bookkeeping envelopes, retaining opaque raw JSON.

    These records are searchable source evidence, not independently authored
    user decisions. Required wire fields and their types are checked; extra
    fields and nested attachment/request/accounting payloads remain untouched.
    """
    kind = event.get("type")
    if not isinstance(kind, str):
        return False
    fields = _CLAUDE_TECHNICAL_FIELDS.get(kind)
    if fields is None or not isinstance(event.get("sessionId"), str):
        return False
    optional = _CLAUDE_OPTIONAL_FIELDS.get(kind, {})
    for key, expected in (*fields.items(), *((k, v) for k, v in optional.items() if k in event)):
        allowed = expected if isinstance(expected, tuple) else (expected,)
        if key not in event or type(event[key]) not in allowed:
            return False
        if type(event[key]) is float and not math.isfinite(event[key]):
            return False
    return True


def _technical_metadata_client(event: dict) -> str | None:
    if _codex_technical_metadata(event):
        return "codex"
    if _claude_technical_metadata(event):
        return "claude"
    return None


def _event_text(event: dict, client: str) -> tuple[str, str, str, bool]:
    """Return normalized text, role, detected client, and schema recognition."""
    kind = event.get("type", "")
    if not isinstance(kind, str):
        return "", "unknown", client, False
    payload = event.get("payload")
    if kind in CODEX_TECHNICAL_RECORDS:
        return ("", "metadata", "codex", True) if _codex_technical_metadata(event) else ("", "unknown", client, False)
    if kind in CLAUDE_TECHNICAL_RECORDS:
        return ("", "metadata", "claude", True) if _claude_technical_metadata(event) else ("", "unknown", client, False)
    if isinstance(payload, dict) and kind in {"session_meta", "turn_context"}:
        return "", "metadata", "codex", True
    if kind == "event_msg" and isinstance(payload, dict):
        subkind = payload.get("type")
        if subkind in {"user_message", "agent_message"}:
            return str(payload.get("message", "")), "user" if subkind == "user_message" else "assistant", "codex", True
        return "", "metadata", "codex", True
    message = event.get("message")
    if kind == "response_item" and isinstance(payload, dict):
        client = "codex"
        message = payload
    elif isinstance(message, dict) and (kind in {"user", "assistant"} or "sessionId" in event):
        client = "claude"
    elif "role" in event and "content" in event:
        message = event
    elif kind in {"progress", "system", "summary", "file-history-snapshot", "queue-operation"}:
        return str(event.get("summary", "")), "metadata", "claude" if client == "auto" else client, True
    if isinstance(message, dict):
        role = message.get("role", "tool" if message.get("type") in {"function_call", "function_call_output"} else "unknown")
        content = message.get("content", message.get("output", ""))
        if message.get("type") == "function_call":
            content = f"Tool: {message.get('name', '')}\n{message.get('arguments', '')}"
        if isinstance(content, list):
            blocks = []
            if content and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                # Claude represents tool responses inside role=user messages.
                # They are not decisions or instructions authored by the user.
                role = "tool"
            for block in content:
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("text"), str):
                    blocks.append(block["text"])
                elif block.get("type") == "tool_result":
                    result = block.get("content", "")
                    blocks.append(result if isinstance(result, str) else _json(result))
                elif block.get("type") == "tool_use":
                    blocks.append(f"Tool: {block.get('name', '')}\n{_json(block.get('input', {}))}")
            content = "\n".join(blocks)
        return str(content) if content is not None else "", str(role), client, True
    return "", "unknown", client, False


def _fingerprint(path: Path, offset: int = 0) -> str:
    stat = path.stat()
    # A prefix and the last committed bytes detect in-place rewrites as well as
    # replacement/truncation, while remaining stable under ordinary appends.
    with path.open("rb") as handle:
        head = handle.readline(4096)
        handle.seek(max(0, offset - 256))
        tail = handle.read(min(256, offset))
    return f"{stat.st_dev}:{stat.st_ino}:{hashlib.sha256(head).hexdigest()}:{hashlib.sha256(tail).hexdigest()}"


def _source_project(store, requested: str | None, cwd: str | None) -> tuple[str | None, str | None]:
    inferred = None
    if cwd:
        with suppress(ValueError):
            inferred = store.resolve_project(cwd, recorded=True)["id"]
    if requested and cwd and requested != inferred:
        return None, (
            "Declared working directory belongs to another project" if inferred
            else "Declared working directory is outside the requested project's registered roots"
        )
    return requested or inferred, None


def _validate_reparse_options(client: str, limit: int, deadline: float | None) -> None:
    if client not in {"auto", "codex", "claude"}:
        raise ValueError("client must be auto, codex, or claude")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Transcript reparse limit must be between 1 and 1000")
    if deadline is not None and (type(deadline) not in {int, float} or not math.isfinite(deadline)):
        raise ValueError("Transcript reparse deadline must be a finite monotonic timestamp")


def reparse_history_sources(
    store,
    path: Path,
    project_id: str,
    client: str = "auto",
    limit: int = 1000,
    *,
    deadline: float | None = None,
    ingestion_lock_fn=None,
    reparse_locked_fn=None,
) -> dict:
    """Repair only previously unknown, now understood technical client records.

    The stored raw snapshot is the authority, including when its original file
    has gone away. This does not replay cursors, replace evidence, extract
    decisions, or change project attribution. Normal archive proof still has to
    independently establish the captured transcript horizon.
    """
    lock_fn = _ingestion_lock if ingestion_lock_fn is None else ingestion_lock_fn
    reparse_locked = _reparse_history_sources_locked if reparse_locked_fn is None else reparse_locked_fn
    path = Path(path).expanduser().resolve()
    project_id = store.resolve_project(project_id)["id"]
    _validate_reparse_options(client, limit, deadline)
    with lock_fn(store, "history", str(path), deadline=deadline):
        return reparse_locked(store, path, project_id, client, limit, deadline=deadline)


def _reparse_history_sources_locked(
    store,
    path: Path,
    project_id: str | None,
    client: str = "auto",
    limit: int = 1000,
    *,
    deadline: float | None = None,
    technical_metadata_client_fn=None,
    source_project_fn=None,
    chunks_fn=None,
    max_file_bytes: int = MAX_FILE_BYTES,
    time_mod=None,
) -> dict:
    _validate_reparse_options(client, limit, deadline)
    detect_client = _technical_metadata_client if technical_metadata_client_fn is None else technical_metadata_client_fn
    resolve_src_project = _source_project if source_project_fn is None else source_project_fn
    split_chunks = _chunks if chunks_fn is None else chunks_fn
    clock = time if time_mod is None else time_mod
    counts = {"repaired": 0, "unchanged": 0, "errors": []}
    where = [
        eq("is_current", True),
        eq("kind", "transcript_unknown"),
        eq("metadata.import_path", str(path)),
        eq("metadata.schema_warning", UNKNOWN_TRANSCRIPT_WARNING),
    ]
    if project_id:
        where.append(eq("project_id", project_id))
    sources = db_find(
        store,
        "sources",
        where,
        order_by=(asc("metadata.byte_offset"), asc("id")),
        limit=limit,
    )
    for source in sources:
        if deadline is not None and clock.monotonic() >= deadline:
            raise TimeoutError("Transcript reparse deadline exceeded")
        counts["unchanged"] += 1
        metadata = _metadata(source.get("metadata"))
        content = source.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > max_file_bytes:
            counts["errors"].append(f"Transcript source {source['id']}: raw record exceeds the reparse budget")
            continue
        try:
            event = json.loads(content)
        except ValueError:
            continue
        detected = detect_client(event) if isinstance(event, dict) else None
        if detected is None:
            continue
        offset, session_id, original_client = (metadata.get("byte_offset"), metadata.get("session_id"),
                                                metadata.get("client"))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if (source.get("sha256") != digest or not content.endswith("\n")
                or type(offset) is not int or offset < 0 or not isinstance(session_id, str) or not session_id
                or original_client not in {"auto", detected}
                or client not in {"auto", detected}
                or (detected == "claude" and event.get("sessionId") != session_id)
                or source.get("uri") != f"transcript://{original_client}/{_identifier(path, session_id)}/{offset}/{digest}"
                or metadata.get("role") != "unknown" or metadata.get("normalized_text") != ""):
            counts["errors"].append(f"Transcript source {source['id']}: original provenance or raw hash does not match")
            continue
        cwd, event_cwd = metadata.get("cwd"), _event_cwd(event)
        if ((cwd is not None and not isinstance(cwd, str)) or (event_cwd and event_cwd != cwd)
                or resolve_src_project(store, source["project_id"], cwd)[0] != source["project_id"]):
            counts["errors"].append(f"Transcript source {source['id']}: working directory conflicts with its project")
            continue
        # Old unknown records were indexed as their raw JSON. Understood
        # metadata schemas have the same raw text view, so retain every chunk
        # identity and embedding. Only their derived role changes. A raw view
        # whose known secrets were replaced (#182) was chunked from the masked
        # view kept beside it.
        view = metadata.get("indexed_text") if metadata.get("masked") else content
        if not isinstance(view, str):
            counts["errors"].append(f"Transcript source {source['id']}: raw chunk projection does not match")
            continue
        expected = {
            _identifier(source["project_id"], source["id"], number, first, last, text): (text, first, last)
            for number, (text, first, last) in enumerate(split_chunks(view))
        }
        with store.transaction() as tx:
            store._require_project(tx, source["project_id"])
            current = db_find(
                tx,
                "sources",
                {"id": source["id"], "project_id": source["project_id"], "is_current": True},
            )
            if (not current or current[0].get("kind") != "transcript_unknown"
                    or current[0].get("sha256") != digest or current[0].get("content") != content
                    or _metadata(current[0].get("metadata")) != metadata):
                counts["errors"].append(f"Transcript source {source['id']}: source changed during reparse")
                continue
            chunks = db_find(tx, "chunks", {"source_id": source["id"]})
            if (set(expected) != {chunk["id"] for chunk in chunks}
                    or any(chunk.get("project_id") != source["project_id"]
                           or not chunk.get("is_current")
                           or (chunk.get("text"), chunk.get("line_start"), chunk.get("line_end")) != expected[chunk["id"]]
                           for chunk in chunks)):
                counts["errors"].append(f"Transcript source {source['id']}: raw chunk projection does not match")
                continue
            updated = {**metadata, "role": "metadata", "normalized_text": "", "schema_warning": None,
                       "transcript_parser_revision": TRANSCRIPT_PARSER_REVISION,
                       "transcript_reparse": {"previous_kind": "transcript_unknown",
                                              "previous_schema_warning": metadata["schema_warning"],
                                              "previous_parser_revision": metadata.get("transcript_parser_revision")}}
            db_update_where(
                tx,
                "sources",
                set_fields={"kind": "transcript", "metadata": updated},
                where={"id": source["id"]},
            )
            for chunk in chunks:
                db_update_where(
                    tx,
                    "chunks",
                    set_fields={"metadata": {**_metadata(chunk.get("metadata")), "role": "metadata"}},
                    where={"id": chunk["id"]},
                )
        counts["repaired"] += 1
        counts["unchanged"] -= 1
    return counts


def import_history(
    store,
    path: Path,
    project_id: str | None,
    client: str = "auto",
    *,
    ingestion_lock_fn=None,
    import_locked_fn=None,
) -> dict:
    """Durably import complete JSONL records; unfinished tails are retried later."""
    lock_fn = _ingestion_lock if ingestion_lock_fn is None else ingestion_lock_fn
    import_locked = _import_history_locked if import_locked_fn is None else import_locked_fn
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        # Directories delegate to per-file locks below, allowing unrelated
        # transcripts to be imported concurrently without nested lock cycles.
        return import_locked(store, path, project_id, client)
    with lock_fn(store, "history", str(path)):
        return import_locked(store, path, project_id, client)


def _import_history_locked(
    store,
    path: Path,
    project_id: str | None,
    client: str = "auto",
    *,
    import_history_fn=None,
    ingest_masker_fn=None,
    reparse_locked_fn=None,
    source_project_fn=None,
    event_text_fn=None,
    write_chunks_fn=None,
    save_cursor_fn=None,
) -> dict:
    import_hist = import_history if import_history_fn is None else import_history_fn
    get_masker = ingest_masker if ingest_masker_fn is None else ingest_masker_fn
    reparse_locked = _reparse_history_sources_locked if reparse_locked_fn is None else reparse_locked_fn
    resolve_src_project = _source_project if source_project_fn is None else source_project_fn
    parse_event = _event_text if event_text_fn is None else event_text_fn
    write_chunks = _write_chunks if write_chunks_fn is None else write_chunks_fn
    save_cursor = _save_cursor if save_cursor_fn is None else save_cursor_fn

    path = Path(path).expanduser().resolve()
    if project_id:
        project_id = store.resolve_project(project_id)["id"]
    if client not in {"auto", "codex", "claude"}:
        raise ValueError("client must be auto, codex, or claude")
    counts: dict[str, Any] = {"files": 0, "archived": 0, "chunks": 0, "inbox": 0, "unknown": 0, "repaired": 0,
                              "errors": []}
    if path.is_dir():
        for candidate in sorted(path.rglob("*.jsonl")):
            if candidate.is_symlink():
                continue
            result = import_hist(store, candidate, project_id, client)
            for key in counts:
                if key == "errors":
                    counts[key].extend(result[key])
                else:
                    counts[key] += result[key]
        return counts
    if not path.is_file():
        raise ValueError(f"History does not exist: {path}")
    # Secrets that cannot be read cannot be kept out: nothing is imported then, and the cursor stays.
    try:
        masker = get_masker(store.settings)
    except ValueError as exc:
        raise ValueError(f"Transcript import refused: {exc}") from None
    # The cursor may already be at EOF, and snapshot() intentionally returns
    # the same immutable source for identical raw bytes. Repair the narrow
    # known metadata compatibility gap independently of append ingestion.
    reparsed = reparse_locked(store, path, project_id, client)
    counts["errors"].extend(reparsed["errors"])
    counts["repaired"] = reparsed["repaired"]
    row = db_find_one(store, "import_cursors", {"path": str(path)})
    offset = int(row["offset"]) if row else 0
    if row and row["fingerprint"] != _fingerprint(path, offset):
        offset = 0
    # Explicit reassignment replays sources from the inbox without losing originals.
    if row and project_id and row["project_id"] != project_id:
        offset = 0
    if path.stat().st_size < offset:
        offset = 0
    cwd = None
    session_id = path.stem
    # Discover session metadata even when resuming beyond its first record.
    with path.open("rb") as probe:
        for _ in range(30):
            line = probe.readline(MAX_FILE_BYTES)
            if not line:
                break
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                cwd = _event_cwd(event) or cwd
                session_id = str(event.get("sessionId") or (event.get("payload") or {}).get("id", session_id)) if isinstance(event.get("payload", {}), dict) else session_id
                if cwd:
                    break
    counts["files"] = 1
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            event_offset = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                break
            end = handle.tell()
            text = raw.decode("utf-8", "replace")
            reason = None
            event: dict = {}
            try:
                decoded = json.loads(text)
                if not isinstance(decoded, dict):
                    reason = "JSONL event is not an object"
                else:
                    event = decoded
            except ValueError:
                reason = "Invalid complete JSONL record"
            cwd = _event_cwd(event) or cwd
            if isinstance(event.get("sessionId"), str):
                session_id = event["sessionId"]
            if event.get("type") == "session_meta" and isinstance(event.get("payload"), dict):
                session_id = str(event["payload"].get("id", session_id))
            resolved, conflict = resolve_src_project(store, project_id, cwd)
            reason = reason or conflict
            normalized, role, detected, recognized = parse_event(event, client)
            if not recognized:
                counts["unknown"] += 1
                reason = reason or UNKNOWN_TRANSCRIPT_WARNING
            if not resolved:
                with store.transaction() as inbox_tx:
                    db_upsert_document(
                        inbox_tx,
                        "ingestion_inbox",
                        {"id": _identifier(path, event_offset, text), "path": str(path), "source_offset": event_offset,
                         "sha256": hashlib.sha256(raw).hexdigest(), "raw": text, "client": detected, "cwd": cwd,
                         "reason": reason or "No registered project matches working directory", "created_at": _now()},
                        key_field="id",
                    )
                counts["inbox"] += 1
            else:
                # Every complete source event is archived, including unrecognized records.
                # Immutable event URIs keep old evidence valid when a client
                # rotates/replaces a file and reuses its byte offsets.
                event_hash = hashlib.sha256(raw).hexdigest()
                # Known secrets are replaced in the readable view and in the chunks written from it (#182).
                # A line without a normalized view is chunked from its raw text, so that text is masked too.
                # The raw line is archived as received: its hash, URI and identity do not change. That masked
                # raw view is kept as metadata.indexed_text, which every reader of the readable view uses
                # (extraction_input.source_text); normalized_text stays empty, so the line is still not extracted.
                normalized, kinds = masker.mask(normalized)
                indexed, raw_kinds = (normalized, []) if normalized else masker.mask(text)
                event_metadata = {"client": detected, "role": role, "session_id": session_id, "cwd": cwd,
                                  "import_path": str(path), "byte_offset": event_offset, "normalized_text": normalized,
                                  "schema_warning": reason, "source_timestamp": event.get("timestamp"),
                                  "transcript_parser_revision": TRANSCRIPT_PARSER_REVISION}
                if raw_kinds:
                    event_metadata["indexed_text"] = indexed
                source = store.snapshot(
                    resolved, f"transcript://{detected}/{_identifier(path, session_id)}/{event_offset}/{event_hash}", text,
                    kind="transcript" if recognized else "transcript_unknown",
                    metadata=masked_metadata(event_metadata, kinds or raw_kinds),
                )
                counts["archived"] += 1
                counts["chunks"] += write_chunks(store, resolved, source["id"], indexed, {"role": role})
                if normalized and role in {"user", "assistant"}:
                    store.enqueue("extract", resolved, {"source_id": source["id"]}, f"extract:{source['id']}")
                # Assignment moves this exact event into the project's archive.
                # Only remove its inbox copy after source/chunks/jobs are durable;
                # a crash before this point safely replays without losing bytes.
                db_delete_where(
                    store,
                    "ingestion_inbox",
                    {"path": str(path), "source_offset": event_offset, "sha256": event_hash},
                )
            if reason:
                counts["errors"].append(f"{path}:{event_offset}: {reason}")
            # A crash before cursor commit is safe: sources and jobs are idempotent.
            save_cursor(store, path, project_id, detected, end)
            offset = end
    # Register even empty or currently partial files so polling notices appends.
    if not db_find_one(store, "import_cursors", {"path": str(path)}, columns=("path",)):
        save_cursor(store, path, project_id, client, offset)
    return counts


# Per store and path, the transcript files whose last import in this process read no line, repaired no record
# and reported no error (#363). While the cursor in the database stands at the end of the file and the file is
# the one the cursor describes, importing it again would do nothing, so the poll skips it. A file that read,
# repaired or reported anything is imported again by every poll, as before.
_POLLED: set[tuple[str, str]] = set()


def _at_end(path: Path, cursor: dict) -> bool:
    """Whether the cursor stands at the end of this very file: nothing new can be read from it."""
    try:
        offset = int(cursor.get("offset") or 0)
        return path.is_file() and path.stat().st_size == offset and _fingerprint(path, offset) == cursor.get("fingerprint")
    except (OSError, ValueError, TypeError):
        return False  # an identity that cannot be established is imported in full


def poll_imports(
    store,
    *,
    polled: set[tuple[str, str]] | None = None,
    ingest_masker_fn=None,
    import_history_fn=None,
) -> dict:
    from cairnmind.memory.project_removal import removed_ids

    polled_set = _POLLED if polled is None else polled
    get_masker = ingest_masker if ingest_masker_fn is None else ingest_masker_fn
    import_hist = import_history if import_history_fn is None else import_history_fn
    rows = db_find(store, "import_cursors", columns=("path", "project_id", "client", "offset", "fingerprint"))
    result = {"files": 0, "archived": 0, "chunks": 0, "inbox": 0, "unknown": 0, "errors": []}
    removed = removed_ids(store)  # a removed project's conversations are not archived until it is back (#327)
    try:  # local files only; when the secrets cannot be read no file is skipped, so each import refuses and says so
        get_masker(store.settings)
        secrets_readable = True
    except (OSError, ValueError):  # an OSError too: each import then raises it and the loop reports it per file, as on main
        secrets_readable = False
    for row in rows:
        if row["project_id"] in removed:
            continue
        key = (str(store.data_dir), row["path"])
        if secrets_readable and key in polled_set and _at_end(Path(row["path"]), row):
            continue
        try:
            item = import_hist(store, Path(row["path"]), row["project_id"], row["client"])
            for name, value in item.items():
                if name == "errors":
                    result[name].extend(value)
                elif name in result:
                    result[name] += value
            # Remembered only when the import changed and reported nothing; a line appended after the look
            # makes _at_end false at the next skip check.
            if not (item["archived"] or item["inbox"] or item["repaired"] or item["errors"]):
                polled_set.add(key)
            else:
                polled_set.discard(key)
        except (OSError, ValueError) as exc:
            result["errors"].append(f"{row['path']}: {exc}")
            polled_set.discard(key)
    return result


def register_default_histories(store, project_id: str, root: Path, *, import_history_fn=None) -> dict:
    import_hist = import_history if import_history_fn is None else import_history_fn
    project = store.resolve_project(project_id)
    roots = _roots(store, project)
    root = Path(root).resolve()
    if root not in roots:
        from cairnmind.storage.sql import unregistered_root
        raise ValueError(unregistered_root(root, project))
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    locations = [(codex_home / "sessions", "codex"), (codex_home / "archived_sessions", "codex"), (claude_home / "projects", "claude")]
    result = {"discovered": 0, "registered": 0, "archived": 0, "errors": []}
    for base, client in locations:
        try:
            if not base.is_dir():
                continue
            candidates = sorted(base.rglob("*.jsonl"))
        except OSError:
            continue
        for path in candidates:
            if path.is_symlink():
                continue
            result["discovered"] += 1
            declared = None
            try:
                with path.open("rb") as handle:
                    for _ in range(30):
                        line = handle.readline(MAX_FILE_BYTES)
                        if not line:
                            break
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict):
                            declared = _event_cwd(event)
                            if declared:
                                break
                if not declared:
                    continue
                try:
                    belongs = store.resolve_project(declared, recorded=True)["id"] == project["id"]
                except ValueError:
                    belongs = False
                if belongs:
                    counts = import_hist(store, path, project["id"], client)
                    result["registered"] += 1
                    result["archived"] += counts["archived"]
                    result["errors"].extend(counts["errors"])
            except OSError as exc:
                result["errors"].append(f"{path}: {exc}")
    return result
