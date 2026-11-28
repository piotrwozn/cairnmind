"""Embedded zero-dependency SQLite StorageBackend (PR #7 / Faza 4b, ADR-01/03/05).

Provides full behavioral parity with ArcadeBackend across all 28 collections:
- WAL mode, synchronous=NORMAL, foreign_keys=ON, busy_timeout=5000
- BEGIN IMMEDIATE write transactions with jittered SQLITE_BUSY retry + telemetry counter
- Read-only mode via URI ``?mode=ro`` + ``PRAGMA query_only=ON``
- Versioned schema via ``PRAGMA user_version`` with automatic ``pre-vN.bak`` backup
- FTS5 (``chunks_fts``, ``records_fts``) with ``unicode61 tokenchars '_-.:/'`` and safe token quoting
- Cycle-guarded ``WITH RECURSIVE`` bounded graph BFS (``instr(path, '|' || dst || '|') = 0``)
- Canonical ``float32`` BLOB embeddings + exact cosine similarity scan (optional ``sqlite-vec``)
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import random
import re
import shutil
import sqlite3
import struct
import threading
import time
from typing import Any, Iterator

from .sql import clean_row, json_text
from .storage_backend import ALL_COLLECTIONS, META_COLLECTION

SQLITE_SCHEMA_VERSION = 4
BUSY_TIMEOUT_MS = 5000
MAX_BUSY_RETRIES = 6

# Indexed SQL columns per collection (all rows also keep full canonical JSON in ``doc``).
_COLLECTION_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "projects": (
        ("name", "TEXT UNIQUE"),
        ("root", "TEXT UNIQUE"),
        ("removed_at", "TEXT"),
    ),
    "project_roots": (
        ("project_id", "TEXT"),
        ("path", "TEXT UNIQUE"),
    ),
    "sources": (
        ("project_id", "TEXT"),
        ("uri", "TEXT"),
        ("kind", "TEXT"),
        ("is_current", "INTEGER"),
        ("current_key", "TEXT UNIQUE"),
        ("created_at", "TEXT"),
    ),
    "chunks": (
        ("project_id", "TEXT"),
        ("source_id", "TEXT"),
        ("is_current", "INTEGER"),
        ("text", "TEXT"),
    ),
    "records": (
        ("project_id", "TEXT"),
        ("kind", "TEXT"),
        ("key", "TEXT"),
        ("status", "TEXT"),
        ("active_key", "TEXT UNIQUE"),
        ("content", "TEXT"),
        ("updated_at", "TEXT"),
    ),
    "record_revisions": (
        ("project_id", "TEXT"),
        ("record_id", "TEXT"),
        ("version", "INTEGER"),
    ),
    "jobs": (
        ("project_id", "TEXT"),
        ("kind", "TEXT"),
        ("state", "TEXT"),
        ("job_key", "TEXT UNIQUE"),
        ("lease_until", "REAL"),
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
    ),
    "embeddings": (
        ("project_id", "TEXT"),
        ("source_id", "TEXT"),
        ("chunk_id", "TEXT"),
        ("namespace", "TEXT"),
        ("dimension", "INTEGER"),
        ("is_current", "INTEGER"),
    ),
    "import_cursors": (
        ("project_id", "TEXT"),
        ("path", "TEXT UNIQUE"),
    ),
    "ingestion_inbox": (("project_id", "TEXT"),),
    "vector_indexes": (
        ("project_id", "TEXT"),
        ("namespace", "TEXT"),
        ("dimension", "INTEGER"),
        ("type_name", "TEXT"),
    ),
    "plans": (("project_id", "TEXT"),),
    "plan_revisions": (
        ("project_id", "TEXT"),
        ("plan_id", "TEXT"),
    ),
    "plan_requests": (("project_id", "TEXT"),),
    "worktree_states": (
        ("project_id", "TEXT"),
        ("worktree", "TEXT"),
    ),
    "sessions": (("project_id", "TEXT"),),
    "resume_packets": (
        ("project_id", "TEXT"),
        ("session_id", "TEXT"),
        ("created_at", "TEXT"),
    ),
    "resume_acks": (
        ("project_id", "TEXT"),
        ("session_id", "TEXT"),
    ),
    "session_events": (
        ("project_id", "TEXT"),
        ("session_id", "TEXT"),
    ),
    "check_runs": (
        ("project_id", "TEXT"),
        ("task_id", "TEXT"),
    ),
    "memory_settings": (("project_id", "TEXT"),),
    "memory_snapshots": (("project_id", "TEXT"),),
    "memory_imports": (("project_id", "TEXT"),),
    "memory_bindings": (("project_id", "TEXT"),),
    "journal_state": (("project_id", "TEXT"),),
    "journal_events": (
        ("project_id", "TEXT"),
        ("epoch", "TEXT"),
        ("seq", "INTEGER"),
        ("entity_type", "TEXT"),
    ),
    "nodes": (
        ("project_id", "TEXT"),
        ("kind", "TEXT"),
        ("label", "TEXT"),
        ("source_id", "TEXT"),
        ("is_current", "INTEGER"),
    ),
    "edges": (
        ("project_id", "TEXT"),
        ("from_id", "TEXT"),
        ("to_id", "TEXT"),
        ("kind", "TEXT"),
        ("source_id", "TEXT"),
        ("is_current", "INTEGER"),
        ("status", "TEXT"),
    ),
}

_LOOKUP_INDEXES: tuple[tuple[str, str], ...] = (
    ("chunks", "source_id"),
    ("nodes", "source_id"),
    ("edges", "source_id"),
    ("edges", "from_id"),
    ("edges", "to_id"),
    ("embeddings", "source_id"),
    ("embeddings", "chunk_id"),
    ("embeddings", "namespace"),
    ("sources", "uri"),
    ("record_revisions", "record_id"),
    ("check_runs", "task_id"),
    ("session_events", "session_id"),
    ("resume_packets", "session_id"),
)

_SQL_KEYWORDS = frozenset({
    "AND", "OR", "NOT", "IN", "IS", "NULL", "TRUE", "FALSE", "LIKE", "ASC", "DESC",
    "ORDER", "BY", "LIMIT", "GROUP", "WHERE", "SELECT", "FROM", "AS", "DISTINCT",
    "COUNT", "SUM", "MIN", "MAX", "AVG", "CASE", "WHEN", "THEN", "ELSE", "END",
})

_SELECT_RE = re.compile(
    r"^\s*SELECT\s+(?P<distinct>DISTINCT\s+)?(?P<cols>.*?)\s*FROM\s+`?(?P<table>[A-Za-z_][A-Za-z0-9_:]*)`?"
    r"(?P<rest>.*)$",
    re.I | re.S,
)
_DELETE_RE = re.compile(
    r"^\s*DELETE\s+FROM\s+`?(?P<table>[A-Za-z_][A-Za-z0-9_]*)`?(?P<rest>.*)$",
    re.I | re.S,
)
_INSERT_CONTENT_RE = re.compile(
    r"^\s*(?:INSERT\s+INTO|CREATE\s+VERTEX)\s+`?(?P<table>[A-Za-z_][A-Za-z0-9_]*)`?\s+CONTENT\s+(?P<body>.*)$",
    re.I | re.S,
)
_INSERT_SET_RE = re.compile(
    r"^\s*(?:INSERT\s+INTO|CREATE\s+VERTEX)\s+`?(?P<table>[A-Za-z_][A-Za-z0-9_]*)`?\s+SET\s+(?P<set>.*)$",
    re.I | re.S,
)
_UPDATE_REMOVE_RE = re.compile(
    r"^\s*UPDATE\s+`?(?P<table>[A-Za-z_][A-Za-z0-9_]*)`?\s+REMOVE\s+(?P<fields>.*?)(?:\s+WHERE\s+(?P<where>.*))?$",
    re.I | re.S,
)
_UPDATE_RE = re.compile(
    r"^\s*UPDATE\s+`?(?P<table>[A-Za-z_][A-Za-z0-9_]*)`?\s+SET\s+(?P<set>.*?)(?:\s+(?P<upsert>UPSERT))?"
    r"(?:\s+WHERE\s+(?P<where>.*))?$",
    re.I | re.S,
)


def pack_vector(vector: list[float]) -> bytes:
    """Pack a float vector as canonical little-endian float32 BLOB (ADR-03)."""
    return struct.pack(f"<{len(vector)}f", *(float(v) for v in vector))


def unpack_vector(blob: bytes) -> tuple[float, ...]:
    count = len(blob) // 4
    return struct.unpack(f"<{count}f", blob)


def cosine_distance(left: list[float] | tuple[float, ...], right: list[float] | tuple[float, ...]) -> float:
    if len(left) != len(right):
        return 2.0
    dot = 0.0
    norm_l = 0.0
    norm_r = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_l += a * a
        norm_r += b * b
    denom = math.sqrt(norm_l) * math.sqrt(norm_r)
    if denom <= 0.0:
        return 1.0
    sim = max(-1.0, min(1.0, dot / denom))
    return 1.0 - sim


def quote_fts5_query(words: list[str]) -> str:
    """Safely quote tokens for SQLite FTS5 MATCH (prevents operator/punctuation injection)."""
    quoted = []
    for raw in words:
        token = str(raw).strip()
        if not token:
            continue
        escaped = token.replace('"', '""')
        quoted.append(f'"{escaped}"')
    return " OR ".join(quoted)


def _is_busy_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _sql_col_value(value: Any) -> Any:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (dict, list)):
        return json_text(value)
    return value


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    parts, buf = [], []
    quote, depth = None, 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                quote = None
        elif ch in "'\"`":
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif depth == 0 and text[i:i + len(sep)] == sep:
            parts.append("".join(buf).strip())
            buf = []
            i += len(sep)
            continue
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _split_clauses(rest: str) -> dict[str, str]:
    """Extract WHERE / GROUP BY / ORDER BY / LIMIT clauses at parenthesis depth 0."""
    clauses = {"where": "", "group_by": "", "order_by": "", "limit": ""}
    keywords = (
        ("WHERE", "where"),
        ("GROUP BY", "group_by"),
        ("ORDER BY", "order_by"),
        ("LIMIT", "limit"),
    )
    positions: list[tuple[int, int, str]] = []
    quote, depth, i = None, 0, 0
    upper = rest.upper()
    while i < len(rest):
        ch = rest[i]
        if quote:
            if ch == "\\" and i + 1 < len(rest):
                i += 2
                continue
            if ch == quote:
                if i + 1 < len(rest) and rest[i + 1] == quote:
                    i += 2
                    continue
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and (i == 0 or not (rest[i - 1].isalnum() or rest[i - 1] == "_")):
            for kw, key in keywords:
                if upper.startswith(kw, i):
                    end = i + len(kw)
                    if end == len(rest) or not (rest[end].isalnum() or rest[end] == "_"):
                        positions.append((i, end, key))
                        i = end
                        break
            else:
                i += 1
            continue
        i += 1
    for idx, (start, end, key) in enumerate(positions):
        next_start = positions[idx + 1][0] if idx + 1 < len(positions) else len(rest)
        clauses[key] = rest[end:next_start].strip()
    return clauses


def _table_column_names(table: str) -> set[str]:
    if table == META_COLLECTION:
        return {"name"}
    cols = {"id"}
    for col, _ in _COLLECTION_COLUMNS.get(table, ()):
        cols.add(col)
    return cols


def _field_expr(table: str, raw_ident: str) -> str:
    cleaned = raw_ident.strip("`")
    if "." in cleaned:
        return f"json_extract(doc, '$.{cleaned}')"
    if cleaned in _table_column_names(table):
        return f'"{cleaned}"'
    return f"json_extract(doc, '$.{cleaned}')"


def _translate_expr(table: str, expr: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Translate an ArcadeDB WHERE/ORDER/GROUP expression into SQLite SQL + expanded params."""
    out_params: dict[str, Any] = {}
    for k, v in (params or {}).items():
        if isinstance(v, bool):
            out_params[k] = 1 if v else 0
        elif isinstance(v, (dict, list)):
            out_params[k] = v  # may be expanded if used with IN :param
        else:
            out_params[k] = v

    # Replace inline list literals IN ['a', 'b'] -> IN ('a', 'b') outside strings.
    def _replace_brackets(text: str) -> str:
        res = []
        quote = None
        i = 0
        while i < len(text):
            ch = text[i]
            if quote:
                res.append(ch)
                if ch == quote:
                    quote = None
                i += 1
                continue
            if ch in "'\"":
                quote = ch
                res.append(ch)
                i += 1
                continue
            if ch == "[":
                res.append("(")
            elif ch == "]":
                res.append(")")
            else:
                res.append(ch)
            i += 1
        return "".join(res)

    text = _replace_brackets(expr)

    # Expand `NOT IN :param` / `IN :param` where param is a list/tuple/set.
    expand_counter = [0]

    def _expand_in(match: re.Match) -> str:
        neg = match.group("neg") or ""
        pname = match.group("param")
        val = (params or {}).get(pname)
        if not isinstance(val, (list, tuple, set)):
            return match.group(0)
        items = list(val)
        if not items:
            return "1=1" if neg else "0=1"
        placeholders = []
        for item in items:
            key = f"__in_{expand_counter[0]}"
            expand_counter[0] += 1
            out_params[key] = _sql_col_value(item)
            placeholders.append(f":{key}")
        prefix = "NOT IN" if neg else "IN"
        return f"{prefix} ({', '.join(placeholders)})"

    text = re.sub(
        r"(?P<neg>\bNOT\s+)?\bIN\s+:(?P<param>[A-Za-z_][A-Za-z0-9_]*)",
        _expand_in,
        text,
        flags=re.I,
    )

    # Fix up any `col 0=1` / `col 1=1` created when `col IN :empty` was expanded.
    text = re.sub(
        r"(?:`?[A-Za-z_][A-Za-z0-9_.]*`?)\s+(0=1|1=1)\b",
        lambda m: f"({m.group(1)})",
        text,
    )

    # Token-by-token rewrite of identifiers, booleans, and dotted paths outside string literals.
    result = []
    prev_word = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"":
            q = ch
            start = i
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text[i] == q:
                    if i + 1 < n and text[i + 1] == q:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            result.append(text[start:i])
            prev_word = ""
            continue
        if ch == "(" and re.match(r"\(\s*SELECT\b", text[i:], re.I):
            depth = 0
            q_sub = None
            j = i
            while j < n:
                c_sub = text[j]
                if q_sub:
                    if c_sub == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if c_sub == q_sub:
                        if j + 1 < n and text[j + 1] == q_sub:
                            j += 2
                            continue
                        q_sub = None
                elif c_sub in "'\"":
                    q_sub = c_sub
                elif c_sub == "(":
                    depth += 1
                elif c_sub == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j < n and depth == 0:
                inner_sql = text[i + 1:j].strip()
                sub_m = _SELECT_RE.match(inner_sql)
                if sub_m:
                    sub_table = sub_m.group("table")
                    sub_cols_raw = (sub_m.group("cols") or "").strip()
                    sub_cols = ", ".join(
                        _field_expr(sub_table, c.strip()) if c.strip() != "*" else "*"
                        for c in _split_top_level(sub_cols_raw, ",")
                    ) if sub_cols_raw else "*"
                    sub_clauses = _split_clauses((sub_m.group("rest") or "").strip())
                    sub_where = ""
                    if sub_clauses["where"]:
                        sub_where, sub_params = _translate_expr(sub_table, sub_clauses["where"], params)
                        out_params.update(sub_params)
                    rebuilt = f'(SELECT {sub_cols} FROM "{sub_table}"'
                    if sub_where:
                        rebuilt += f" WHERE {sub_where}"
                    rebuilt += ")"
                    result.append(rebuilt)
                    i = j + 1
                    continue
        if ch == ":":
            m = re.match(r":[A-Za-z_][A-Za-z0-9_]*", text[i:])
            if m:
                result.append(m.group(0))
                i += len(m.group(0))
                continue
        if ch == "`":
            end = text.find("`", i + 1)
            if end != -1:
                ident = text[i + 1:end]
                result.append(_field_expr(table, ident))
                i = end + 1
                continue
        if ch.isalpha() or ch == "_":
            m = re.match(r"[A-Za-z_][A-Za-z0-9_.]*", text[i:])
            assert m is not None
            word = m.group(0)
            after_pos = i + len(word)
            next_ch = text[after_pos] if after_pos < n else ""
            upper = word.upper()
            if prev_word == "AS":
                result.append(f'"{word}"')
            elif upper == "TRUE":
                result.append("1")
            elif upper == "FALSE":
                result.append("0")
            elif upper in _SQL_KEYWORDS or next_ch == "(":
                result.append(word)
            else:
                result.append(_field_expr(table, word))
            prev_word = upper
            i = after_pos
            continue
        if not ch.isspace():
            prev_word = ""
        result.append(ch)
        i += 1

    # Remove non-scalar params that were expanded for IN clauses so sqlite3 doesn't choke.
    final_params = {
        k: (_sql_col_value(v) if isinstance(v, (dict, list)) else v)
        for k, v in out_params.items()
        if f":{k}" in "".join(result) or k in out_params
    }
    sql_out = "".join(result)
    bound_names = set(re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", sql_out))
    final_params = {k: v for k, v in final_params.items() if k in bound_names}
    return sql_out, final_params


def _extract_dotted(row: dict[str, Any], path: str) -> Any:
    current: Any = row
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _project_rows(docs: list[dict[str, Any]], cols_spec: str, distinct: bool = False) -> list[dict[str, Any]]:
    spec = cols_spec.strip()
    if not spec or spec == "*":
        rows = [clean_row(doc) for doc in docs]
    else:
        col_specs = _split_top_level(spec, ",")
        parsed: list[tuple[str, str]] = []
        for item in col_specs:
            m = re.match(r"^(.+?)\s+AS\s+`?([A-Za-z_][A-Za-z0-9_]*)`?$", item, re.I)
            if m:
                src = m.group(1).strip().strip("`")
                alias = m.group(2).strip()
            else:
                src = item.strip().strip("`")
                alias = src.split(".")[-1]
            parsed.append((src, alias))
        rows = []
        for doc in docs:
            projected = {}
            for src, alias in parsed:
                if src == "*":
                    projected.update(clean_row(doc))
                else:
                    projected[alias] = _extract_dotted(doc, src)
            rows.append(projected)
    if distinct:
        seen = set()
        unique = []
        for row in rows:
            key = json_text(row)
            if key not in seen:
                seen.add(key)
                unique.append(row)
        return unique
    return rows


def _apply_doc_updates(doc: dict[str, Any], updates: dict[str, Any]) -> None:
    """Apply top-level or dotted-path (e.g. metadata.analysis) updates into a document dict."""
    for key, value in updates.items():
        if "." not in key:
            doc[key] = value
            continue
        parts = key.split(".")
        target: Any = doc
        for part in parts[:-1]:
            cur = target.get(part) if isinstance(target, dict) else None
            if not isinstance(cur, dict):
                cur = {}
                target[part] = cur
            target = cur
        target[parts[-1]] = value


def _remove_doc_fields(doc: dict[str, Any], fields: list[str]) -> None:
    """Remove top-level or dotted-path fields from a document dict."""
    for field in fields:
        if "." not in field:
            doc.pop(field, None)
            continue
        parts = field.split(".")
        target: Any = doc
        for part in parts[:-1]:
            if not isinstance(target, dict) or part not in target:
                target = None
                break
            target = target[part]
        if isinstance(target, dict):
            target.pop(parts[-1], None)


def _parse_set_assignments(set_clause: str, params: dict[str, Any]) -> dict[str, Any]:
    """Parse ArcadeDB `col=:param, col2=true, ...` assignment list into a Python dict."""
    values: dict[str, Any] = {}
    for assign in _split_top_level(set_clause, ","):
        left, _, right = assign.partition("=")
        col = left.strip().strip("`")
        val_raw = right.strip()
        if val_raw.startswith(":"):
            values[col] = params.get(val_raw[1:])
        elif val_raw.lower() == "true":
            values[col] = True
        elif val_raw.lower() == "false":
            values[col] = False
        elif val_raw.lower() == "null":
            values[col] = None
        elif val_raw.startswith("'") and val_raw.endswith("'"):
            values[col] = val_raw[1:-1]
        else:
            try:
                values[col] = json.loads(val_raw)
            except ValueError:
                values[col] = val_raw
    return values


def _is_aggregate_select(cols_spec: str, group_by: str) -> bool:
    if group_by:
        return True
    return bool(re.search(r"\b(count|sum|min|max|avg)\s*\(", cols_spec, re.I))


class SQLiteTransaction:
    """Native SQLite transaction handle compatible with JournalTransaction & StorageTransaction."""

    def __init__(self, backend: SQLiteBackend, conn: sqlite3.Connection, *, read_only: bool = False):
        self._backend = backend
        self._conn = conn
        self._read_only = read_only

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self._backend._exec_query(self._conn, sql, params or {})

    def command(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self._read_only:
            from .store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        return self._backend._exec_command(self._conn, sql, params or {})

    def script(self, statements: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self._read_only:
            from .store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        last: list[dict[str, Any]] = []
        for stmt in statements.split(";\n"):
            stmt = stmt.strip()
            if not stmt:
                continue
            if stmt.upper().startswith("SELECT "):
                last = self.query(stmt, params)
            else:
                last = self.command(stmt, params)
        return last

    def server_time_ms(self) -> int:
        return int(time.time() * 1000)

    def insert_document(self, collection: str, data: dict[str, Any]) -> dict[str, Any]:
        if self._read_only:
            from .store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        self._backend._insert_row(self._conn, collection, data)
        return data

    def insert_documents(self, collection: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self._read_only:
            from .store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        for row in rows:
            self._backend._insert_row(self._conn, collection, row)
        return list(rows)

    def update_document(
        self,
        collection: str,
        data: dict[str, Any],
        where: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if self._read_only:
            from .store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        return self._backend._update_rows(self._conn, collection, data, where, params)

    def upsert_document(
        self,
        collection: str,
        data: dict[str, Any],
        key_field: str = "id",
    ) -> dict[str, Any]:
        if self._read_only:
            from .store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        self._backend._upsert_row(self._conn, collection, data, key_field=key_field)
        return data

    def delete_documents(
        self,
        collection: str,
        where: str = "",
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if self._read_only:
            from .store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        return self._backend._delete_rows(self._conn, collection, where, params or {})

    def create_vertex(self, collection: str, node: dict[str, Any]) -> dict[str, Any]:
        return self.insert_document(collection, node)

    def create_edge(self, collection: str, edge: dict[str, Any]) -> dict[str, Any]:
        return self.insert_document(collection, edge)


class SQLiteBackend:
    """Embedded SQLite implementation of ``StorageBackend`` (WAL + FTS5 + CTE + float32 vectors)."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        read_only: bool = False,
        store: Any = None,
    ):
        self._store = store
        self._read_only = bool(read_only)
        self._is_memory = str(db_path) == ":memory:"
        self.db_path = Path(":memory:") if self._is_memory else Path(db_path).expanduser().resolve()
        self._local = threading.local()
        self._all_conns_lock = threading.Lock()
        self._all_conns: list[sqlite3.Connection] = []
        self._memory_conn: sqlite3.Connection | None = None
        self._memory_lock = threading.RLock()
        self._sqlite_vec_available = False
        # Telemetry counters (consumed by PR #8 doctor scale detection)
        self.busy_retries: int = 0
        self._busy_timestamps: deque[float] = deque(maxlen=1000)
        self._fts_latencies_ms: deque[float] = deque(maxlen=500)
        self._graph_latencies_ms: deque[float] = deque(maxlen=500)
        self._last_metrics_persisted: float = 0.0

    def _metrics_data_dir(self) -> Path | None:
        if self._is_memory or self._read_only:
            return None
        if self._store is not None and getattr(self._store, "data_dir", None):
            return Path(self._store.data_dir)
        return self.db_path.parent

    def persist_metrics(self, *, force: bool = False) -> None:
        """Persist rolling latency and busy-retry counters to ``data_dir/runtime/sqlite-metrics.json``."""
        data_dir = self._metrics_data_dir()
        if data_dir is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_metrics_persisted) < 30.0:
            return
        self._last_metrics_persisted = now
        try:
            from .sqlite_telemetry import record_sqlite_metrics

            record_sqlite_metrics(data_dir, self.metrics_snapshot())
        except Exception:  # noqa: BLE001
            pass

    @property
    def backend_kind(self) -> str:
        return "sqlite"

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def sqlite_vec_loaded(self) -> bool:
        return self._sqlite_vec_available

    def metrics_snapshot(self) -> dict[str, Any]:
        """Return live SQLite telemetry for doctor/scale checks (PR #8)."""
        now = time.time()
        recent_busy = sum(1 for ts in self._busy_timestamps if now - ts <= 600.0)

        def _p95(samples: deque[float]) -> float:
            if not samples:
                return 0.0
            ordered = sorted(samples)
            idx = min(len(ordered) - 1, max(0, int(math.ceil(0.95 * len(ordered))) - 1))
            return round(ordered[idx], 2)

        db_bytes = 0
        if not self._is_memory and self.db_path.exists():
            try:
                db_bytes = self.db_path.stat().st_size
                wal = Path(str(self.db_path) + "-wal")
                if wal.exists():
                    db_bytes += wal.stat().st_size
            except OSError:
                pass
        return {
            "backend": "sqlite",
            "db_path": ":memory:" if self._is_memory else str(self.db_path),
            "db_bytes": db_bytes,
            "busy_retries_total": self.busy_retries,
            "busy_retries_10m": recent_busy,
            "fts_p95_ms": _p95(self._fts_latencies_ms),
            "graph_p95_ms": _p95(self._graph_latencies_ms),
            "sqlite_vec_loaded": self._sqlite_vec_available,
        }

    def _open_connection(self) -> sqlite3.Connection:
        if self._is_memory:
            conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        elif self._read_only:
            uri = self.db_path.as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            conn = sqlite3.connect(str(self.db_path), isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        if self._read_only:
            conn.execute("PRAGMA query_only=ON")
        else:
            if not self._is_memory:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        # Optional sqlite-vec acceleration (ADR-03): load cleanly if installed, never fail if absent.
        try:
            import sqlite_vec  # type: ignore[import-not-found]
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._sqlite_vec_available = True
        except Exception:  # noqa: BLE001
            pass
        with self._all_conns_lock:
            self._all_conns.append(conn)
        return conn

    def _conn(self) -> sqlite3.Connection:
        if self._is_memory:
            with self._memory_lock:
                if self._memory_conn is None:
                    self._memory_conn = self._open_connection()
                return self._memory_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open_connection()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        if not self._read_only and not self._is_memory and (
            self.busy_retries or self._fts_latencies_ms or self._graph_latencies_ms
        ):
            self.persist_metrics(force=True)
        with self._all_conns_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._memory_conn = None
        if hasattr(self._local, "conn"):
            delattr(self._local, "conn")

    def ensure_database(self) -> None:
        if self._read_only:
            from .store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot create the database")
        if not self._is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._conn()

    def database_exists(self) -> bool:
        if self._is_memory:
            return self._memory_conn is not None
        return self.db_path.is_file()

    def list_collections(self, reader: Any = None) -> set[str]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {str(row[0]) for row in rows}

    def server_time_ms(self, reader: Any = None) -> int:
        return int(time.time() * 1000)

    def _backup_before_schema_upgrade(self, old_version: int) -> Path | None:
        if self._is_memory or not self.db_path.exists():
            return None
        backup = self.db_path.with_name(f"{self.db_path.name}.pre-v{old_version}.bak")
        if not backup.exists():
            shutil.copy2(self.db_path, backup)
        return backup

    def init_schema(self) -> None:
        """Create or migrate all 28 collections, FTS5 tables, and indexes."""
        if self._read_only:
            return
        conn = self._conn()
        current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if 0 < current_version < SQLITE_SCHEMA_VERSION:
            self._backup_before_schema_upgrade(current_version)
        with self.raw_transaction():
            conn.execute(f"CREATE TABLE IF NOT EXISTS {META_COLLECTION} (name TEXT PRIMARY KEY, doc TEXT NOT NULL)")
            for collection in ALL_COLLECTIONS:
                extra_cols = _COLLECTION_COLUMNS.get(collection, ())
                col_defs = ["id TEXT PRIMARY KEY"]
                for col, col_type in extra_cols:
                    col_defs.append(f'"{col}" {col_type}')
                if collection == "embeddings":
                    col_defs.append("vector_blob BLOB")
                col_defs.append("doc TEXT NOT NULL")
                conn.execute(f'CREATE TABLE IF NOT EXISTS "{collection}" ({", ".join(col_defs)})')
                if collection != "projects":
                    conn.execute(
                        f'CREATE INDEX IF NOT EXISTS "idx_{collection}_project_id" ON "{collection}"("project_id")'
                    )
            conn.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS "idx_journal_events_epoch_seq" '
                'ON "journal_events"("epoch", "seq")'
            )
            for collection, field in _LOOKUP_INDEXES:
                conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "idx_{collection}_{field}" ON "{collection}"("{field}")'
                )
            # FTS5 tables with code-aware unicode61 tokenchars (PR #7 spec)
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                "id UNINDEXED, project_id UNINDEXED, text, "
                "tokenize=\"unicode61 tokenchars '_-.:/'\")"
            )
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5("
                "id UNINDEXED, project_id UNINDEXED, content, "
                "tokenize=\"unicode61 tokenchars '_-.:/'\")"
            )
            # Ensure initial journal_state & schema_version meta rows
            from uuid import uuid4
            if not conn.execute("SELECT id FROM journal_state WHERE id='current'").fetchone():
                self._insert_row(
                    conn,
                    "journal_state",
                    {"id": "current", "epoch": str(uuid4()), "seq": 0, "first_seq": 1},
                )
            if not conn.execute(f"SELECT name FROM {META_COLLECTION} WHERE name='schema_version'").fetchone():
                self._insert_row(conn, META_COLLECTION, {"name": "schema_version", "value": SQLITE_SCHEMA_VERSION})
            else:
                self._upsert_row(
                    conn, META_COLLECTION, {"name": "schema_version", "value": SQLITE_SCHEMA_VERSION}, key_field="name"
                )
            conn.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")

    @contextmanager
    def raw_transaction(self, commit_timeout: float = 30.0):
        """Low-level SQLite transaction with BEGIN IMMEDIATE + jittered SQLITE_BUSY retry."""
        conn = self._conn()
        lock = self._memory_lock if self._is_memory else None
        if lock is not None:
            lock.acquire()
        try:
            if conn.in_transaction:
                yield SQLiteTransaction(self, conn, read_only=self._read_only)
                return
            begin_sql = "BEGIN DEFERRED" if self._read_only else "BEGIN IMMEDIATE"
            for attempt in range(MAX_BUSY_RETRIES):
                try:
                    conn.execute(begin_sql)
                    break
                except sqlite3.OperationalError as exc:
                    if _is_busy_error(exc) and attempt < MAX_BUSY_RETRIES - 1:
                        self.busy_retries += 1
                        self._busy_timestamps.append(time.time())
                        delay = (0.015 * (2 ** attempt)) + random.uniform(0.0, 0.025)
                        time.sleep(delay)
                        continue
                    raise
            tx = SQLiteTransaction(self, conn, read_only=self._read_only)
            try:
                yield tx
                if self._read_only:
                    conn.execute("ROLLBACK")
                else:
                    conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            if lock is not None:
                lock.release()

    def transaction(self, commit_timeout: float = 30.0):
        if self._store is not None:
            return self._store.transaction(commit_timeout=commit_timeout)
        return self.raw_transaction(commit_timeout=commit_timeout)

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self._exec_query(self._conn(), sql, params or {})

    def command(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self._read_only:
            from .store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        with self.raw_transaction():
            return self._exec_command(self._conn(), sql, params or {})

    # --- Internal Row Operations + FTS5 Sync ---

    def _sync_fts_upsert(self, conn: sqlite3.Connection, collection: str, row: dict[str, Any]) -> None:
        if collection == "chunks":
            row_id = row.get("id")
            if isinstance(row_id, str):
                conn.execute("DELETE FROM chunks_fts WHERE id = ?", (row_id,))
                if row.get("is_current", True) and isinstance(row.get("text"), str):
                    conn.execute(
                        "INSERT INTO chunks_fts(id, project_id, text) VALUES (?, ?, ?)",
                        (row_id, row.get("project_id") or "", row["text"]),
                    )
        elif collection == "records":
            row_id = row.get("id")
            if isinstance(row_id, str):
                conn.execute("DELETE FROM records_fts WHERE id = ?", (row_id,))
                if row.get("status") != "superseded" and isinstance(row.get("content"), str):
                    conn.execute(
                        "INSERT INTO records_fts(id, project_id, content) VALUES (?, ?, ?)",
                        (row_id, row.get("project_id") or "", row["content"]),
                    )

    def _sync_fts_delete(self, conn: sqlite3.Connection, collection: str, row_ids: list[str]) -> None:
        if not row_ids:
            return
        if collection == "chunks":
            conn.executemany("DELETE FROM chunks_fts WHERE id = ?", [(rid,) for rid in row_ids])
        elif collection == "records":
            conn.executemany("DELETE FROM records_fts WHERE id = ?", [(rid,) for rid in row_ids])

    def _insert_row(self, conn: sqlite3.Connection, collection: str, data: dict[str, Any]) -> None:
        row = clean_row(data)
        doc_str = json_text(row)
        if collection == META_COLLECTION:
            try:
                conn.execute(
                    f"INSERT INTO {META_COLLECTION}(name, doc) VALUES (?, ?)",
                    (row["name"], doc_str),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Duplicate key in {collection}: {exc}") from exc
            return
        cols = ["id"] + [c for c, _ in _COLLECTION_COLUMNS.get(collection, ())]
        vals: list[Any] = [row.get("id")]
        for c, _ in _COLLECTION_COLUMNS.get(collection, ()):
            vals.append(_sql_col_value(row.get(c)))
        if collection == "embeddings":
            cols.append("vector_blob")
            vec = row.get("vector")
            vals.append(pack_vector(vec) if isinstance(vec, list) and vec else None)
        cols.append("doc")
        vals.append(doc_str)
        quoted_cols = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join("?" for _ in cols)
        try:
            conn.execute(f'INSERT INTO "{collection}" ({quoted_cols}) VALUES ({placeholders})', vals)
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Duplicate key in {collection}: {exc}") from exc
        self._sync_fts_upsert(conn, collection, row)

    def _upsert_row(
        self,
        conn: sqlite3.Connection,
        collection: str,
        data: dict[str, Any],
        *,
        key_field: str = "id",
    ) -> None:
        row = clean_row(data)
        key_val = row.get(key_field)
        if collection == META_COLLECTION:
            doc_str = json_text(row)
            conn.execute(
                f"INSERT INTO {META_COLLECTION}(name, doc) VALUES (?, ?) "
                f"ON CONFLICT(name) DO UPDATE SET doc=excluded.doc",
                (row["name"], doc_str),
            )
            return
        # Check if existing row matches key_field and merge/replace cleanly.
        existing = conn.execute(
            f'SELECT id, doc FROM "{collection}" WHERE "{key_field}" = ?',
            (_sql_col_value(key_val),),
        ).fetchone()
        if existing is not None:
            merged = {**json.loads(existing["doc"]), **row}
            merged["id"] = row.get("id") or existing["id"]
            self._replace_row_by_id(conn, collection, existing["id"], merged)
            return
        self._insert_row(conn, collection, row)

    def _replace_row_by_id(
        self,
        conn: sqlite3.Connection,
        collection: str,
        old_id: str,
        row: dict[str, Any],
    ) -> None:
        row = clean_row(row)
        doc_str = json_text(row)
        if collection == META_COLLECTION:
            conn.execute(
                f"UPDATE {META_COLLECTION} SET name=?, doc=? WHERE name=?",
                (row["name"], doc_str, old_id),
            )
            return
        cols = ["id"] + [c for c, _ in _COLLECTION_COLUMNS.get(collection, ())]
        vals: list[Any] = [row.get("id")]
        for c, _ in _COLLECTION_COLUMNS.get(collection, ()):
            vals.append(_sql_col_value(row.get(c)))
        if collection == "embeddings":
            cols.append("vector_blob")
            vec = row.get("vector")
            vals.append(pack_vector(vec) if isinstance(vec, list) and vec else None)
        cols.append("doc")
        vals.append(doc_str)
        vals.append(old_id)
        setters = ", ".join(f'"{c}"=?' for c in cols)
        try:
            conn.execute(f'UPDATE "{collection}" SET {setters} WHERE id=?', vals)
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Duplicate key in {collection}: {exc}") from exc
        if old_id != row.get("id"):
            self._sync_fts_delete(conn, collection, [old_id])
        self._sync_fts_upsert(conn, collection, row)

    def _update_rows(
        self,
        conn: sqlite3.Connection,
        collection: str,
        updates: dict[str, Any],
        where: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        pk = "name" if collection == META_COLLECTION else "id"
        if where:
            sql_where, sql_params = _translate_expr(collection, where, params)
            rows = conn.execute(f'SELECT "{pk}", doc FROM "{collection}" WHERE {sql_where}', sql_params).fetchall()
        else:
            rows = conn.execute(f'SELECT "{pk}", doc FROM "{collection}"').fetchall()
        updated_docs: list[dict[str, Any]] = []
        for row in rows:
            doc = json.loads(row["doc"])
            _apply_doc_updates(doc, updates)
            self._replace_row_by_id(conn, collection, row[pk], doc)
            updated_docs.append(doc)
        return updated_docs

    def _remove_fields_rows(
        self,
        conn: sqlite3.Connection,
        collection: str,
        fields: list[str],
        where: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        pk = "name" if collection == META_COLLECTION else "id"
        if where:
            sql_where, sql_params = _translate_expr(collection, where, params)
            rows = conn.execute(f'SELECT "{pk}", doc FROM "{collection}" WHERE {sql_where}', sql_params).fetchall()
        else:
            rows = conn.execute(f'SELECT "{pk}", doc FROM "{collection}"').fetchall()
        updated_docs: list[dict[str, Any]] = []
        for row in rows:
            doc = json.loads(row["doc"])
            _remove_doc_fields(doc, fields)
            self._replace_row_by_id(conn, collection, row[pk], doc)
            updated_docs.append(doc)
        return updated_docs

    def _delete_rows(
        self,
        conn: sqlite3.Connection,
        collection: str,
        where: str,
        params: dict[str, Any],
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        pk = "name" if collection == META_COLLECTION else "id"
        if limit is not None:
            lim = max(0, int(limit))
            if lim == 0:
                return [{"count": 0}]
            if where:
                sql_where, sql_params = _translate_expr(collection, where, params)
                rows = conn.execute(
                    f'SELECT "{pk}" FROM "{collection}" WHERE {sql_where} LIMIT {lim}',
                    sql_params,
                ).fetchall()
            else:
                rows = conn.execute(f'SELECT "{pk}" FROM "{collection}" LIMIT {lim}').fetchall()
            ids = [row[0] for row in rows]
            if ids:
                ph = ", ".join("?" for _ in ids)
                conn.execute(f'DELETE FROM "{collection}" WHERE "{pk}" IN ({ph})', ids)
        elif where:
            sql_where, sql_params = _translate_expr(collection, where, params)
            rows = conn.execute(f'SELECT "{pk}" FROM "{collection}" WHERE {sql_where}', sql_params).fetchall()
            ids = [row[0] for row in rows]
            conn.execute(f'DELETE FROM "{collection}" WHERE {sql_where}', sql_params)
        else:
            rows = conn.execute(f'SELECT "{pk}" FROM "{collection}"').fetchall()
            ids = [row[0] for row in rows]
            conn.execute(f'DELETE FROM "{collection}"')
        if collection in {"chunks", "records"}:
            self._sync_fts_delete(conn, collection, [str(x) for x in ids])
        return [{"count": len(ids)}]

    # --- ArcadeSQL Compatibility Translator (for read-only projections & shared domain modules) ---

    def _exec_query(self, conn: sqlite3.Connection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        stripped = sql.strip()
        if "sysdate()" in stripped:
            return [{"milliseconds": int(time.time() * 1000)}]
        m = _SELECT_RE.match(stripped)
        if not m:
            raise ValueError(f"Unsupported SQLite query: {stripped[:160]}")
        distinct = bool(m.group("distinct"))
        cols = (m.group("cols") or "").strip()
        table = m.group("table")
        rest = (m.group("rest") or "").strip()

        if table == "schema:types":
            names = sorted(self.list_collections())
            return [{"name": n} for n in names]

        clauses = _split_clauses(rest)
        sql_params: dict[str, Any] = {}
        where_sql = ""
        if clauses["where"]:
            where_sql, w_params = _translate_expr(table, clauses["where"], params)
            sql_params.update(w_params)

        order_sql = ""
        if clauses["order_by"]:
            order_sql, o_params = _translate_expr(table, clauses["order_by"], params)
            sql_params.update(o_params)

        limit_sql = ""
        if clauses["limit"]:
            lim_raw = clauses["limit"].strip()
            if lim_raw.startswith(":"):
                pname = lim_raw[1:]
                limit_sql = f"LIMIT :{pname}"
                sql_params[pname] = int(params[pname])
            else:
                limit_sql = f"LIMIT {int(lim_raw)}"

        if _is_aggregate_select(cols, clauses["group_by"]):
            # Translate column list & GROUP BY directly for aggregates like count(*).
            agg_cols, a_params = _translate_expr(table, cols, params)
            sql_params.update(a_params)
            group_sql = ""
            if clauses["group_by"]:
                g_expr, g_params = _translate_expr(table, clauses["group_by"], params)
                sql_params.update(g_params)
                group_sql = f"GROUP BY {g_expr}"
            query_sql = (
                f'SELECT {agg_cols} FROM "{table}"'
                + (f" WHERE {where_sql}" if where_sql else "")
                + (f" {group_sql}" if group_sql else "")
                + (f" ORDER BY {order_sql}" if order_sql else "")
                + (f" {limit_sql}" if limit_sql else "")
            )
            cur = conn.execute(query_sql, sql_params)
            col_names = [desc[0] for desc in cur.description or ()]
            return [{col_names[i]: row[i] for i in range(len(col_names))} for row in cur.fetchall()]

        # Non-aggregate SELECT: fetch canonical `doc` rows via SQLite indexes, then project exact Python types!
        query_sql = (
            f'SELECT doc FROM "{table}"'
            + (f" WHERE {where_sql}" if where_sql else "")
            + (f" ORDER BY {order_sql}" if order_sql else "")
            + (f" {limit_sql}" if limit_sql else "")
        )
        rows = conn.execute(query_sql, sql_params).fetchall()
        docs = [json.loads(row[0]) for row in rows]
        return _project_rows(docs, cols, distinct=distinct)

    def _exec_command(self, conn: sqlite3.Connection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        stripped = sql.strip()
        upper = stripped.upper()
        if upper.startswith(("CREATE DOCUMENT TYPE", "CREATE VERTEX TYPE", "CREATE EDGE TYPE",
                             "CREATE PROPERTY", "CREATE INDEX")):
            return []
        m_del = _DELETE_RE.match(stripped)
        if m_del:
            clauses = _split_clauses((m_del.group("rest") or "").strip())
            limit_val: int | None = None
            if clauses["limit"]:
                lim_raw = clauses["limit"].strip()
                limit_val = int(params[lim_raw[1:]]) if lim_raw.startswith(":") else int(lim_raw)
            return self._delete_rows(conn, m_del.group("table"), clauses["where"], params, limit=limit_val)
        m_ins = _INSERT_CONTENT_RE.match(stripped)
        if m_ins:
            table = m_ins.group("table")
            body = m_ins.group("body").strip()
            if body.startswith(":"):
                payload = params.get(body[1:])
                if isinstance(payload, list):
                    for item in payload:
                        self._insert_row(conn, table, item)
                    return list(payload)
                if isinstance(payload, dict):
                    self._insert_row(conn, table, payload)
                    return [payload]
            row = json.loads(body)
            self._insert_row(conn, table, row)
            return [row]
        m_ins_set = _INSERT_SET_RE.match(stripped)
        if m_ins_set:
            table = m_ins_set.group("table")
            row = _parse_set_assignments(m_ins_set.group("set").strip(), params)
            self._insert_row(conn, table, row)
            return [row]
        m_rem = _UPDATE_REMOVE_RE.match(stripped)
        if m_rem:
            table = m_rem.group("table")
            fields = [f.strip().strip("`") for f in _split_top_level(m_rem.group("fields"), ",") if f.strip()]
            where_clause = (m_rem.group("where") or "").strip()
            return self._remove_fields_rows(conn, table, fields, where_clause, params)
        m_upd = _UPDATE_RE.match(stripped)
        if m_upd:
            table = m_upd.group("table")
            set_clause = m_upd.group("set").strip()
            is_upsert = bool(m_upd.group("upsert"))
            where_clause = (m_upd.group("where") or "").strip()
            updates = _parse_set_assignments(set_clause, params)
            if is_upsert:
                # Extract key_field from simple `field=:param` WHERE clause.
                m_key = re.match(r"^`?([A-Za-z_][A-Za-z0-9_]*)`?\s*=\s*:([A-Za-z_][A-Za-z0-9_]*)$", where_clause)
                key_field = m_key.group(1) if m_key else "id"
                if m_key and key_field not in updates:
                    updates[key_field] = params.get(m_key.group(2))
                self._upsert_row(conn, table, updates, key_field=key_field)
                return [updates]
            return self._update_rows(conn, table, updates, where_clause, params)
        raise ValueError(f"Unsupported SQLite command: {stripped[:160]}")

    # --- StorageBackend Protocol Methods ---

    def insert_document(self, tx: Any, collection: str, data: dict[str, Any]) -> dict[str, Any]:
        if hasattr(tx, "insert_document"):
            return tx.insert_document(collection, data)
        self._insert_row(self._conn(), collection, data)
        return data

    def insert_documents(self, tx: Any, collection: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if hasattr(tx, "insert_documents"):
            return tx.insert_documents(collection, rows)
        for row in rows:
            self._insert_row(self._conn(), collection, row)
        return list(rows)

    def update_document(
        self,
        tx: Any,
        collection: str,
        data: dict[str, Any],
        where: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if hasattr(tx, "update_document"):
            return tx.update_document(collection, data, where, params)
        return self._update_rows(self._conn(), collection, data, where, params)

    def upsert_document(
        self,
        tx: Any,
        collection: str,
        data: dict[str, Any],
        key_field: str = "id",
    ) -> dict[str, Any]:
        if hasattr(tx, "upsert_document"):
            return tx.upsert_document(collection, data, key_field=key_field)
        self._upsert_row(self._conn(), collection, data, key_field=key_field)
        return data

    def delete_documents(
        self,
        tx: Any,
        collection: str,
        where: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if hasattr(tx, "delete_documents"):
            return tx.delete_documents(collection, where, params or {})
        return self._delete_rows(self._conn(), collection, where, params or {})

    def get_by_id(self, reader: Any, collection: str, row_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        row = conn.execute(f'SELECT doc FROM "{collection}" WHERE id=?', (row_id,)).fetchone()
        return clean_row(json.loads(row[0])) if row else None

    # --- Full-Text Search (FTS5 + BM25) ---

    def search_chunks_fts(
        self,
        project_id: str,
        words: list[str],
        eligible_sources: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        if not words or not eligible_sources:
            return []
        fts_query = quote_fts5_query(words)
        if not fts_query:
            return []
        t0 = time.monotonic()
        conn = self._conn()
        placeholders = ", ".join("?" for _ in eligible_sources)
        rows = conn.execute(
            f"SELECT c.doc, -bm25(chunks_fts) AS score "
            f"FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.id "
            f"WHERE chunks_fts MATCH ? AND c.project_id = ? AND c.is_current = 1 "
            f"AND c.source_id IN ({placeholders}) "
            f"ORDER BY bm25(chunks_fts) ASC LIMIT ?",
            [fts_query, project_id, *eligible_sources, int(limit)],
        ).fetchall()
        self._fts_latencies_ms.append((time.monotonic() - t0) * 1000.0)
        return [{**json.loads(row[0]), "search_score": float(row[1])} for row in rows]

    def search_records_fts(
        self,
        project_id: str,
        words: list[str],
        eligible_records: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        if not words or not eligible_records:
            return []
        fts_query = quote_fts5_query(words)
        if not fts_query:
            return []
        t0 = time.monotonic()
        conn = self._conn()
        placeholders = ", ".join("?" for _ in eligible_records)
        rows = conn.execute(
            f"SELECT r.doc, -bm25(records_fts) AS score "
            f"FROM records_fts JOIN records r ON r.id = records_fts.id "
            f"WHERE records_fts MATCH ? AND r.project_id = ? AND r.status <> 'superseded' "
            f"AND r.id IN ({placeholders}) "
            f"ORDER BY bm25(records_fts) ASC LIMIT ?",
            [fts_query, project_id, *eligible_records, int(limit)],
        ).fetchall()
        self._fts_latencies_ms.append((time.monotonic() - t0) * 1000.0)
        return [{**json.loads(row[0]), "search_score": float(row[1])} for row in rows]

    # --- Vectors (ADR-03: float32 BLOB + exact cosine similarity) ---

    def ensure_vector_index(
        self,
        project_id: str,
        namespace: str,
        dimension: int,
        model: str,
    ) -> dict[str, Any] | None:
        if not 1 <= dimension <= 8192:
            raise ValueError("Unsupported embedding dimension")
        conn = self._conn()
        if not conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone():
            return None
        identity = json.dumps([project_id, namespace, dimension], separators=(",", ":"))
        type_name = "vector_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        values = {
            "id": type_name,
            "project_id": project_id,
            "model": model,
            "namespace": namespace,
            "dimension": dimension,
            "type_name": type_name,
            "index_name": f"{type_name}[vector]",
        }
        with self.transaction() as tx:
            if not tx.query("SELECT id FROM projects WHERE id=:id", {"id": project_id}):
                return None
            self.upsert_document(tx, "vector_indexes", values, key_field="id")
        return values

    def has_vector_embedding(
        self,
        project_id: str,
        namespace: str,
        chunk_id: str,
        dimension: int,
        identifier: str,
    ) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT id FROM embeddings WHERE id=? AND project_id=? AND namespace=? AND dimension=? "
            "AND vector_blob IS NOT NULL",
            (identifier, project_id, namespace, int(dimension)),
        ).fetchone()
        return row is not None

    def upsert_embedding(
        self,
        tx: Any,
        values: dict[str, Any],
        index_info: dict[str, Any] | None = None,
    ) -> None:
        self.upsert_document(tx, "embeddings", {**values, "is_current": True}, key_field="id")

    def search_vectors(
        self,
        project_id: str,
        namespace: str,
        query_vector: list[float],
        eligible_sources: list[str],
        limit: int,
    ) -> tuple[bool, list[dict[str, Any]], str | None]:
        conn = self._conn()
        dim = len(query_vector)
        has_idx = conn.execute(
            "SELECT id FROM vector_indexes WHERE project_id=? AND namespace=? AND dimension=?",
            (project_id, namespace, dim),
        ).fetchone()
        if not has_idx:
            return False, [], None
        if not eligible_sources:
            return True, [], None
        placeholders = ", ".join("?" for _ in eligible_sources)
        rows = conn.execute(
            f"SELECT chunk_id, vector_blob, doc FROM embeddings "
            f"WHERE project_id=? AND namespace=? AND dimension=? AND source_id IN ({placeholders})",
            [project_id, namespace, dim, *eligible_sources],
        ).fetchall()
        scored: list[tuple[float, str]] = []
        for row in rows:
            blob = row[1]
            if blob is not None:
                vec = unpack_vector(blob)
            else:
                doc = json.loads(row[2])
                vec = doc.get("vector") or ()
            if len(vec) != dim:
                continue
            dist = cosine_distance(query_vector, vec)
            scored.append((dist, str(row[0])))
        scored.sort(key=lambda item: (item[0], item[1]))
        neighbors = [{"chunk_id": cid, "distance": dist} for dist, cid in scored[: int(limit)]]
        return True, neighbors, None

    def delete_vectors_for_sources(self, tx: Any, source_ids: list[str]) -> None:
        if not source_ids:
            return
        tx.command("DELETE FROM embeddings WHERE source_id IN :ids", {"ids": list(source_ids)})

    def delete_vectors_for_project(self, tx: Any, project_id: str) -> None:
        tx.command("DELETE FROM embeddings WHERE project_id=:project", {"project": project_id})

    # --- Graph (Vertices, Edges, Cycle-Guarded WITH RECURSIVE Bounded BFS) ---

    def upsert_vertex(self, tx: Any, node: dict[str, Any], *, existing: bool = False) -> dict[str, Any]:
        if existing:
            self.update_document(tx, "nodes", node, "id=:id", {"id": node["id"], "project_id": node["project_id"]})
        elif hasattr(tx, "create_vertex"):
            tx.create_vertex("nodes", node)
        else:
            self.insert_document(tx, "nodes", node)
        return node

    def upsert_edge(
        self,
        tx: Any,
        edge: dict[str, Any],
        *,
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if existing:
            mine = {"id": edge["id"], "project_id": edge["project_id"]}
            if any(existing.get(k) != edge[k] for k in ("project_id", "from_id", "to_id")):
                tx.command("DELETE FROM edges WHERE id=:id", mine)
            else:
                self.update_document(tx, "edges", edge, "id=:id", mine)
                return edge
        if hasattr(tx, "create_edge"):
            tx.create_edge("edges", edge)
        else:
            self.insert_document(tx, "edges", edge)
        return edge

    def write_graph_batch(
        self,
        tx: Any,
        project_id: str,
        created_nodes: list[dict[str, Any]],
        updated_nodes: list[dict[str, Any]],
        created_edges: list[dict[str, Any]],
    ) -> None:
        for node in created_nodes:
            self.upsert_vertex(tx, node, existing=False)
        for node in updated_nodes:
            self.upsert_vertex(tx, node, existing=True)
        for edge in created_edges:
            self.upsert_edge(tx, edge, existing=None)

    def search_graph_seeds(
        self,
        project_id: str,
        exact_query: str,
        seed_sources: list[str],
        words: list[str],
        eligible_nodes: list[str],
        limit: int = 7,
    ) -> list[dict[str, Any]]:
        if not eligible_nodes:
            return []
        conn = self._conn()
        elig_ph = ", ".join("?" for _ in eligible_nodes)
        conds = ["id = ?"]
        sql_params: list[Any] = [project_id, *eligible_nodes, exact_query]
        if seed_sources:
            src_ph = ", ".join("?" for _ in seed_sources)
            conds.append(f"source_id IN ({src_ph})")
            sql_params.extend(seed_sources)
        for word in words:
            conds.append("LOWER(label) LIKE ?")
            sql_params.append(f"%{word.casefold()}%")
        sql_params.append(int(limit))
        rows = conn.execute(
            f"SELECT id FROM nodes WHERE project_id = ? AND is_current = 1 "
            f"AND id IN ({elig_ph}) "
            f"AND (json_extract(doc, '$.metadata.status') IS NULL "
            f"     OR json_extract(doc, '$.metadata.status') <> 'superseded') "
            f"AND ({' OR '.join(conds)}) LIMIT ?",
            sql_params,
        ).fetchall()
        return [{"id": row[0]} for row in rows]

    def traverse_graph(
        self,
        project_id: str,
        seed_ids: list[str],
        eligible_nodes: list[str],
        eligible_edges: list[str],
        *,
        max_depth: int = 4,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Cycle-safe bounded BFS via SQLite WITH RECURSIVE CTE."""
        if not seed_ids or not eligible_nodes:
            return []
        t0 = time.monotonic()
        conn = self._conn()
        seed_ph = ", ".join("?" for _ in seed_ids)
        node_ph = ", ".join("?" for _ in eligible_nodes)
        if not eligible_edges:
            # Depth 0 seeds only when no edges are eligible.
            rows = conn.execute(
                f"SELECT doc, 0 AS distance FROM nodes "
                f"WHERE project_id = ? AND is_current = 1 "
                f"AND id IN ({seed_ph}) AND id IN ({node_ph}) "
                f"AND (json_extract(doc, '$.metadata.status') IS NULL "
                f"     OR json_extract(doc, '$.metadata.status') <> 'superseded') "
                f"ORDER BY id LIMIT ?",
                [project_id, *seed_ids, *eligible_nodes, int(limit)],
            ).fetchall()
            self._graph_latencies_ms.append((time.monotonic() - t0) * 1000.0)
            return [{**json.loads(row[0]), "distance": int(row[1])} for row in rows]

        edge_ph = ", ".join("?" for _ in eligible_edges)
        sql = f"""
        WITH RECURSIVE
        valid_nodes(id) AS (
            SELECT id FROM nodes
            WHERE project_id = ? AND is_current = 1
              AND id IN ({node_ph})
              AND (json_extract(doc, '$.metadata.status') IS NULL
                   OR json_extract(doc, '$.metadata.status') <> 'superseded')
        ),
        valid_edges(src, dst) AS (
            SELECT from_id, to_id FROM edges
            WHERE project_id = ? AND is_current = 1 AND status <> 'superseded'
              AND id IN ({edge_ph})
            UNION
            SELECT to_id, from_id FROM edges
            WHERE project_id = ? AND is_current = 1 AND status <> 'superseded'
              AND id IN ({edge_ph})
        ),
        walk(node_id, depth, path) AS (
            SELECT id, 0, '|' || id || '|'
            FROM valid_nodes
            WHERE id IN ({seed_ph})
            UNION ALL
            SELECT e.dst, w.depth + 1, w.path || e.dst || '|'
            FROM walk w
            JOIN valid_edges e ON e.src = w.node_id
            JOIN valid_nodes vn ON vn.id = e.dst
            WHERE w.depth < ?
              AND instr(w.path, '|' || e.dst || '|') = 0
        ),
        ranked(node_id, distance) AS (
            SELECT node_id, MIN(depth) AS distance
            FROM walk
            GROUP BY node_id
            ORDER BY distance ASC, node_id ASC
            LIMIT ?
        )
        SELECT n.doc, r.distance
        FROM ranked r
        JOIN nodes n ON n.id = r.node_id
        ORDER BY r.distance ASC, r.node_id ASC
        """
        params: list[Any] = [
            project_id,
            *eligible_nodes,
            project_id,
            *eligible_edges,
            project_id,
            *eligible_edges,
            *seed_ids,
            int(max_depth),
            int(limit),
        ]
        rows = conn.execute(sql, params).fetchall()
        self._graph_latencies_ms.append((time.monotonic() - t0) * 1000.0)
        return [{**json.loads(row[0]), "distance": int(row[1])} for row in rows]

    # --- Symbol & URI Search ---

    def search_definition_nodes(
        self,
        project_id: str,
        eligible_sources: list[str],
        *,
        mode: str,
        pattern: str | None = None,
        names: list[str] | None = None,
        owner: str | None = None,
        file_pattern: str | None = None,
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        if not eligible_sources:
            return []
        conn = self._conn()
        src_ph = ", ".join("?" for _ in eligible_sources)
        rows = conn.execute(
            f"SELECT doc FROM nodes WHERE project_id = ? AND is_current = 1 AND kind = 'symbol' "
            f"AND source_id IN ({src_ph}) "
            f"ORDER BY label, json_extract(doc, '$.metadata.path'), json_extract(doc, '$.metadata.line_start'), id",
            [project_id, *eligible_sources],
        ).fetchall()
        docs = [json.loads(row[0]) for row in rows]
        matched: list[dict[str, Any]] = []
        if mode == "code":
            if not names:
                return []
            target_set = set(names)
            for doc in docs:
                meta = doc.get("metadata") or {}
                if doc.get("label") in target_set or meta.get("member") in target_set:
                    matched.append(doc)
        elif mode == "plain":
            if not names:
                return []
            target_set = set(names)
            for doc in docs:
                meta = doc.get("metadata") or {}
                if meta.get("kind") in {"constant", "field"} and (
                    doc.get("label") in target_set or meta.get("member") in target_set
                ):
                    matched.append(doc)
        elif mode == "pattern":
            if not pattern:
                return []
            rx = re.compile(pattern)
            for doc in docs:
                meta = doc.get("metadata") or {}
                mkind = meta.get("kind")
                member = str(meta.get("member") or "").lower()
                label = str(doc.get("label") or "").lower()
                if (mkind in {"field", "parameter"} and rx.fullmatch(member)) or (
                    mkind == "constant" and rx.fullmatch(label)
                ):
                    matched.append(doc)
        elif mode == "context":
            if not pattern or not owner or not file_pattern:
                return []
            rx_name = re.compile(pattern)
            rx_owner = re.compile(owner)
            rx_file = re.compile(file_pattern)
            for doc in docs:
                meta = doc.get("metadata") or {}
                mkind = meta.get("kind")
                member = str(meta.get("member") or "").lower()
                label = str(doc.get("label") or "").lower()
                mowner = str(meta.get("owner") or "").lower()
                mpath = str(meta.get("path") or "").lower()
                if (
                    mkind == "field"
                    and rx_name.fullmatch(member)
                    and (rx_owner.fullmatch(mowner) or rx_file.fullmatch(mpath))
                ) or (mkind == "constant" and rx_name.fullmatch(label) and rx_file.fullmatch(mpath)):
                    matched.append(doc)
        else:
            raise ValueError(f"Unknown definition search mode: {mode!r}")

        return [
            {k: doc.get(k) for k in ("id", "label", "source_id", "metadata")}
            for doc in matched[: int(limit)]
        ]

    def search_sources_by_uri_substring(
        self,
        project_id: str,
        substring: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conn = self._conn()
        needle = f"%{substring.casefold()}%"
        rows = conn.execute(
            "SELECT uri FROM sources WHERE project_id = ? AND is_current = 1 "
            "AND LOWER(uri) LIKE ? LIMIT ?",
            (project_id, needle, int(limit)),
        ).fetchall()
        return [{"uri": row[0]} for row in rows]

    # --- Streaming Export & Counts ---

    def iter_collection(
        self,
        reader: Any,
        collection: str,
        *,
        project_id: str | None = None,
        batch_size: int = 1000,
    ) -> Iterator[list[dict[str, Any]]]:
        conn = self._conn()
        batch_size = max(1, int(batch_size))
        after: str | None = None
        while True:
            clauses = []
            sql_params: list[Any] = []
            if project_id is not None:
                field = "id" if collection == "projects" else "project_id"
                clauses.append(f'"{field}" = ?')
                sql_params.append(project_id)
            if after is not None:
                clauses.append("id > ?")
                sql_params.append(after)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            sql_params.append(batch_size)
            rows = conn.execute(
                f'SELECT doc FROM "{collection}"{where} ORDER BY id ASC LIMIT ?',
                sql_params,
            ).fetchall()
            if not rows:
                break
            batch = [clean_row(json.loads(r[0])) for r in rows]
            yield batch
            if len(batch) < batch_size:
                break
            after = batch[-1].get("id")
            if not isinstance(after, str):
                break

    def collection_counts(self, reader: Any = None) -> dict[str, int]:
        conn = self._conn()
        existing = self.list_collections()
        counts: dict[str, int] = {}
        for collection in ALL_COLLECTIONS:
            if collection not in existing:
                counts[collection] = 0
                continue
            row = conn.execute(f'SELECT count(*) FROM "{collection}"').fetchone()
            counts[collection] = int(row[0]) if row else 0
        return counts

    # --- Read-only client proxy compatibility for Store ---

    def as_client_proxy(self) -> _SQLiteClientProxy:
        return _SQLiteClientProxy(self)


class _SQLiteClientProxy:
    """Adapter exposing ArcadeClient-shaped lifecycle methods over SQLiteBackend for Store."""

    def __init__(self, backend: SQLiteBackend):
        self._backend = backend

    @property
    def settings(self) -> Any:
        return getattr(self._backend._store, "settings", None)

    @property
    def database(self) -> str:
        return str(self._backend.db_path)

    def ensure_database(self) -> None:
        self._backend.ensure_database()

    def database_exists(self) -> bool:
        return self._backend.database_exists()

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self._backend.query(sql, params)

    def command(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self._backend.command(sql, params)

    def transaction(self, commit_timeout: float = 30.0, *, commit: bool = True):
        return self._backend.raw_transaction(commit_timeout=commit_timeout)

    def close(self) -> None:
        self._backend.close()
