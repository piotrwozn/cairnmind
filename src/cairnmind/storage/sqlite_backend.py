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

import hashlib
import json
import logging
import math
import random
import re
import shutil
import sqlite3
import struct
import threading
import time
from array import array
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cairnmind.storage.sql import clean_row, json_text
from cairnmind.storage.storage_backend import ALL_COLLECTIONS, META_COLLECTION

SQLITE_SCHEMA_VERSION = 4
BUSY_TIMEOUT_MS = 5000
MAX_BUSY_RETRIES = 6
log = logging.getLogger(__name__)

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


def pack_vector(vector: list[float]) -> bytes:
    """Pack a float vector as canonical little-endian float32 BLOB (ADR-03)."""
    return struct.pack(f"<{len(vector)}f", *(float(v) for v in vector))


def unpack_vector(blob: bytes) -> array:
    arr = array("f")
    arr.frombytes(blob)
    return arr


def cosine_distance(left: list[float] | tuple[float, ...] | array, right: list[float] | tuple[float, ...] | array) -> float:
    if len(left) != len(right):
        return 2.0
    dot = math.sumprod(left, right)
    norm_l = math.sumprod(left, left)
    norm_r = math.sumprod(right, right)
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


def _table_column_names(table: str) -> set[str]:
    if table == META_COLLECTION:
        return {"name"}
    cols = {"id"}
    for col, _ in _COLLECTION_COLUMNS.get(table, ()):
        cols.add(col)
    return cols


_RAW_SQL_DISABLED = (
    "Raw ArcadeDB SQL is disabled on SQLiteBackend; "
    "use declarative StorageBackend / storage_query methods"
)


class SQLiteTransaction:
    """Native SQLite transaction handle compatible with JournalTransaction & StorageTransaction."""

    def __init__(self, backend: SQLiteBackend, conn: sqlite3.Connection, *, read_only: bool = False):
        self._backend = backend
        self._conn = conn
        self._read_only = read_only

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError(_RAW_SQL_DISABLED)

    def command(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self._read_only:
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        raise NotImplementedError(_RAW_SQL_DISABLED)

    def script(self, statements: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self._read_only:
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        raise NotImplementedError(_RAW_SQL_DISABLED)

    def server_time_ms(self) -> int:
        return int(time.time() * 1000)

    def list_collections(self) -> set[str]:
        return self._backend.list_collections()

    def iter_backup_pages(
        self,
        collection: str,
        *,
        page_rows: int = 1000,
        page_bytes: int = 2 * 1024 * 1024,
    ) -> Iterator[str]:
        return self._backend._iter_backup_pages_conn(
            self._conn, collection, page_rows=page_rows, page_bytes=page_bytes
        )

    def find(
        self,
        collection: str,
        where: Any = (),
        *,
        columns: Any = None,
        order_by: Any = (),
        limit: int | None = None,
        distinct: bool = False,
    ) -> list[dict[str, Any]]:
        return self._backend._find_rows(
            self._conn, collection, where, columns=columns, order_by=order_by, limit=limit, distinct=distinct
        )

    def find_one(
        self,
        collection: str,
        where: Any = (),
        *,
        columns: Any = None,
        order_by: Any = (),
        required: bool = False,
    ) -> dict[str, Any] | None:
        return self._backend._find_one_row(
            self._conn, collection, where, columns=columns, order_by=order_by, required=required
        )

    def count(self, collection: str, where: Any = ()) -> int:
        return self._backend._count_rows(self._conn, collection, where)

    def group_counts(
        self,
        collection: str,
        group_by: Any,
        where: Any = (),
        *,
        count_alias: str = "total",
    ) -> list[dict[str, Any]]:
        return self._backend._group_count_rows(self._conn, collection, group_by, where, count_alias=count_alias)

    def update_where(
        self,
        collection: str,
        *,
        set_fields: dict[str, Any] | None = None,
        remove_fields: Any = (),
        where: Any = (),
    ) -> list[dict[str, Any]]:
        if self._read_only:
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        return self._backend._update_where_rows(
            self._conn, collection, set_fields=set_fields, remove_fields=remove_fields, where=where
        )

    def delete_where(
        self,
        collection: str,
        where: Any = (),
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if self._read_only:
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        return self._backend._delete_where_rows(self._conn, collection, where, limit=limit)

    def insert_document(self, collection: str, data: dict[str, Any]) -> dict[str, Any]:
        if self._read_only:
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        self._backend._insert_row(self._conn, collection, data)
        return data

    def insert_documents(self, collection: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self._read_only:
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
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
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        raise NotImplementedError(_RAW_SQL_DISABLED)

    def upsert_document(
        self,
        collection: str,
        data: dict[str, Any],
        key_field: str = "id",
    ) -> dict[str, Any]:
        if self._read_only:
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
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
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        raise NotImplementedError(_RAW_SQL_DISABLED)

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
            from cairnmind.storage.sqlite_telemetry import record_sqlite_metrics

            record_sqlite_metrics(data_dir, self.metrics_snapshot())
        except (OSError, ValueError, TypeError) as exc:
            log.debug("Failed to persist SQLite metrics: %s", exc)

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
        except (ImportError, OSError, sqlite3.Error, AttributeError) as exc:
            log.debug("Optional sqlite-vec extension unavailable: %s", exc)
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
            except (sqlite3.Error, OSError) as exc:
                log.debug("Failed to close SQLite connection: %s", exc)
        self._memory_conn = None
        if hasattr(self._local, "conn"):
            delattr(self._local, "conn")

    def ensure_database(self) -> None:
        if self._read_only:
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
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

    def init_schema(self, store: Any = None) -> None:
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
                        delay = (0.015 * (2 ** attempt)) + random.uniform(0.0, 0.025)  # noqa: S311 - retry jitter, non-crypto
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
        raise NotImplementedError(_RAW_SQL_DISABLED)

    def command(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self._read_only:
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        raise NotImplementedError(_RAW_SQL_DISABLED)

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
        if self._read_only:
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        raise NotImplementedError(_RAW_SQL_DISABLED)

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
        if self._read_only:
            from cairnmind.storage.store_readonly import ReadOnlyStoreError
            raise ReadOnlyStoreError("A store opened for reading cannot run a database command")
        raise NotImplementedError(_RAW_SQL_DISABLED)

    def get_by_id(self, reader: Any, collection: str, row_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        row = conn.execute(f'SELECT doc FROM "{collection}" WHERE id=?', (row_id,)).fetchone()
        return clean_row(json.loads(row[0])) if row else None

    # --- Declarative Backend-Neutral Queries & Mutations (Native SQLite, zero SQL transpilation) ---

    def _find_rows(
        self,
        conn: sqlite3.Connection,
        collection: str,
        where: Any = (),
        *,
        columns: Any = None,
        order_by: Any = (),
        limit: int | None = None,
        distinct: bool = False,
    ) -> list[dict[str, Any]]:
        from cairnmind.storage.storage_query import (
            compile_sqlite_where,
            extract_field_path,
            normalize_columns,
            normalize_order_by,
            sqlite_field_expr,
        )
        table_cols = _table_column_names(collection)
        where_sql, params = compile_sqlite_where(conn, table_cols, where)
        order_specs = normalize_order_by(order_by)
        order_sql = ""
        if order_specs:
            order_parts = [
                f"{sqlite_field_expr(table_cols, spec.field)} {'DESC' if spec.desc else 'ASC'}"
                for spec in order_specs
            ]
            order_sql = " ORDER BY " + ", ".join(order_parts)
        limit_sql = f" LIMIT {max(0, int(limit))}" if limit is not None else ""
        sql = f'SELECT doc FROM "{collection}"' + (f" WHERE {where_sql}" if where_sql else "") + order_sql + limit_sql
        rows = conn.execute(sql, params).fetchall()
        docs = [clean_row(json.loads(r[0])) for r in rows]
        col_specs = normalize_columns(columns)
        if not col_specs:
            return docs
        projected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for doc in docs:
            item: dict[str, Any] = {}
            for spec in col_specs:
                key = spec.alias or spec.field.split(".")[-1]
                item[key] = extract_field_path(doc, spec.field)
            if distinct:
                marker = json_text(item)
                if marker in seen:
                    continue
                seen.add(marker)
            projected.append(item)
        return projected

    def _find_one_row(
        self,
        conn: sqlite3.Connection,
        collection: str,
        where: Any = (),
        *,
        columns: Any = None,
        order_by: Any = (),
        required: bool = False,
    ) -> dict[str, Any] | None:
        from cairnmind.storage.storage_query import missing_row_message
        rows = self._find_rows(conn, collection, where, columns=columns, order_by=order_by, limit=1)
        if not rows:
            if required:
                raise ValueError(missing_row_message(collection))
            return None
        return rows[0]

    def _count_rows(self, conn: sqlite3.Connection, collection: str, where: Any = ()) -> int:
        from cairnmind.storage.storage_query import compile_sqlite_where
        table_cols = _table_column_names(collection)
        where_sql, params = compile_sqlite_where(conn, table_cols, where)
        sql = f'SELECT count(*) FROM "{collection}"' + (f" WHERE {where_sql}" if where_sql else "")
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def _group_count_rows(
        self,
        conn: sqlite3.Connection,
        collection: str,
        group_by: Any,
        where: Any = (),
        *,
        count_alias: str = "total",
    ) -> list[dict[str, Any]]:
        from cairnmind.storage.storage_query import (
            compile_sqlite_where,
            sqlite_field_expr,
            validate_field_path,
        )
        table_cols = _table_column_names(collection)
        where_sql, params = compile_sqlite_where(conn, table_cols, where)
        fields = [validate_field_path(f) for f in group_by]
        exprs = [sqlite_field_expr(table_cols, f) for f in fields]
        out_names = [f.split(".")[-1] for f in fields]
        select_expr = ", ".join(f'{expr} AS "{name}"' for expr, name in zip(exprs, out_names, strict=True))
        group_expr = ", ".join(exprs)
        sql = (
            f'SELECT {select_expr}, count(*) AS "{count_alias}" FROM "{collection}"'
            + (f" WHERE {where_sql}" if where_sql else "")
            + f" GROUP BY {group_expr}"
        )
        rows = conn.execute(sql, params).fetchall()
        return [
            {**{out_names[i]: row[i] for i in range(len(out_names))}, count_alias: int(row[len(out_names)])}
            for row in rows
        ]

    def _update_where_rows(
        self,
        conn: sqlite3.Connection,
        collection: str,
        *,
        set_fields: dict[str, Any] | None = None,
        remove_fields: Any = (),
        where: Any = (),
    ) -> list[dict[str, Any]]:
        from cairnmind.storage.storage_query import apply_nested_updates, compile_sqlite_where
        pk = "name" if collection == META_COLLECTION else "id"
        table_cols = _table_column_names(collection)
        where_sql, params = compile_sqlite_where(conn, table_cols, where)
        sql = f'SELECT "{pk}", doc FROM "{collection}"' + (f" WHERE {where_sql}" if where_sql else "")
        rows = conn.execute(sql, params).fetchall()
        updated_docs: list[dict[str, Any]] = []
        for row in rows:
            doc = apply_nested_updates(json.loads(row["doc"]), set_fields=set_fields, remove_fields=remove_fields)
            self._replace_row_by_id(conn, collection, row[pk], doc)
            updated_docs.append(doc)
        return updated_docs

    def _delete_where_rows(
        self,
        conn: sqlite3.Connection,
        collection: str,
        where: Any = (),
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        from cairnmind.storage.storage_query import compile_sqlite_where, sqlite_id_filter_sql
        pk = "name" if collection == META_COLLECTION else "id"
        table_cols = _table_column_names(collection)
        where_sql, params = compile_sqlite_where(conn, table_cols, where)
        if limit is not None:
            lim = max(0, int(limit))
            if lim == 0:
                return [{"count": 0}]
            sql = f'SELECT "{pk}" FROM "{collection}"' + (f" WHERE {where_sql}" if where_sql else "") + f" LIMIT {lim}"
            ids = [row[0] for row in conn.execute(sql, params).fetchall()]
            if ids:
                clause, id_params = sqlite_id_filter_sql(conn, f'"{pk}"', ids, slot=7)
                conn.execute(f'DELETE FROM "{collection}" WHERE {clause}', id_params)
        else:
            sql = f'SELECT "{pk}" FROM "{collection}"' + (f" WHERE {where_sql}" if where_sql else "")
            ids = [row[0] for row in conn.execute(sql, params).fetchall()]
            if where_sql:
                conn.execute(f'DELETE FROM "{collection}" WHERE {where_sql}', params)
            else:
                conn.execute(f'DELETE FROM "{collection}"')
        if collection in {"chunks", "records"} and ids:
            self._sync_fts_delete(conn, collection, [str(x) for x in ids])
        return [{"count": len(ids)}]

    def find(
        self,
        collection: str,
        where: Any = (),
        *,
        columns: Any = None,
        order_by: Any = (),
        limit: int | None = None,
        distinct: bool = False,
        reader: Any = None,
    ) -> list[dict[str, Any]]:
        return self._find_rows(
            self._conn(), collection, where, columns=columns, order_by=order_by, limit=limit, distinct=distinct
        )

    def find_one(
        self,
        collection: str,
        where: Any = (),
        *,
        columns: Any = None,
        order_by: Any = (),
        required: bool = False,
        reader: Any = None,
    ) -> dict[str, Any] | None:
        return self._find_one_row(
            self._conn(), collection, where, columns=columns, order_by=order_by, required=required
        )

    def count(self, collection: str, where: Any = (), *, reader: Any = None) -> int:
        return self._count_rows(self._conn(), collection, where)

    def group_counts(
        self,
        collection: str,
        group_by: Any,
        where: Any = (),
        *,
        count_alias: str = "total",
        reader: Any = None,
    ) -> list[dict[str, Any]]:
        return self._group_count_rows(self._conn(), collection, group_by, where, count_alias=count_alias)

    def update_where(
        self,
        tx: Any,
        collection: str,
        *,
        set_fields: dict[str, Any] | None = None,
        remove_fields: Any = (),
        where: Any = (),
    ) -> list[dict[str, Any]]:
        if hasattr(tx, "update_where") and tx is not self:
            return tx.update_where(
                collection, set_fields=set_fields, remove_fields=remove_fields, where=where
            )
        return self._update_where_rows(
            self._conn(), collection, set_fields=set_fields, remove_fields=remove_fields, where=where
        )

    def delete_where(
        self,
        tx: Any,
        collection: str,
        where: Any = (),
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if hasattr(tx, "delete_where") and tx is not self:
            return tx.delete_where(collection, where, limit=limit)
        return self._delete_where_rows(self._conn(), collection, where, limit=limit)

    # --- Full-Text Search (FTS5 + BM25, TEMP TABLE safe for large eligible lists) ---

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
        from cairnmind.storage.storage_query import sqlite_id_filter_sql
        t0 = time.monotonic()
        conn = self._conn()
        src_clause, src_params = sqlite_id_filter_sql(conn, "c.source_id", eligible_sources, slot=0)
        rows = conn.execute(
            f"SELECT c.doc, -bm25(chunks_fts) AS score "
            f"FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.id "
            f"WHERE chunks_fts MATCH ? AND c.project_id = ? AND c.is_current = 1 "
            f"AND {src_clause} "
            f"ORDER BY bm25(chunks_fts) ASC LIMIT ?",
            [fts_query, project_id, *src_params, int(limit)],
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
        from cairnmind.storage.storage_query import sqlite_id_filter_sql
        t0 = time.monotonic()
        conn = self._conn()
        rec_clause, rec_params = sqlite_id_filter_sql(conn, "r.id", eligible_records, slot=0)
        rows = conn.execute(
            f"SELECT r.doc, -bm25(records_fts) AS score "
            f"FROM records_fts JOIN records r ON r.id = records_fts.id "
            f"WHERE records_fts MATCH ? AND r.project_id = ? AND r.status <> 'superseded' "
            f"AND {rec_clause} "
            f"ORDER BY bm25(records_fts) ASC LIMIT ?",
            [fts_query, project_id, *rec_params, int(limit)],
        ).fetchall()
        self._fts_latencies_ms.append((time.monotonic() - t0) * 1000.0)
        return [{**json.loads(row[0]), "search_score": float(row[1])} for row in rows]

    # --- Vectors (ADR-03: float32 BLOB + C-level math.sumprod exact cosine / optional sqlite-vec) ---

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
            if not self._find_one_row(conn, "projects", {"id": project_id}, columns=("id",)):
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
        from cairnmind.storage.storage_query import sqlite_id_filter_sql
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
        src_clause, src_params = sqlite_id_filter_sql(conn, "source_id", eligible_sources, slot=0)
        rows = conn.execute(
            f"SELECT chunk_id, vector_blob, doc FROM embeddings "
            f"WHERE project_id=? AND namespace=? AND dimension=? AND {src_clause}",
            [project_id, namespace, dim, *src_params],
        ).fetchall()
        q_arr = array("f", query_vector)
        q_norm = math.sqrt(math.sumprod(q_arr, q_arr))
        scored: list[tuple[float, str]] = []
        for row in rows:
            blob = row[1]
            if blob is not None:
                vec = unpack_vector(blob)
            else:
                doc = json.loads(row[2])
                vec = array("f", doc.get("vector") or ())
            if len(vec) != dim:
                continue
            if q_norm <= 0.0:
                dist = 1.0
            else:
                v_norm = math.sqrt(math.sumprod(vec, vec))
                if v_norm <= 0.0:
                    dist = 1.0
                else:
                    sim = max(-1.0, min(1.0, math.sumprod(q_arr, vec) / (q_norm * v_norm)))
                    dist = 1.0 - sim
            scored.append((dist, str(row[0])))
        scored.sort(key=lambda item: (item[0], item[1]))
        neighbors = [{"chunk_id": cid, "distance": dist} for dist, cid in scored[: int(limit)]]
        return True, neighbors, None

    def delete_vectors_for_sources(self, tx: Any, source_ids: list[str]) -> None:
        if not source_ids:
            return
        from cairnmind.storage.storage_query import is_in
        self.delete_where(tx, "embeddings", is_in("source_id", list(source_ids)))

    def delete_vectors_for_project(self, tx: Any, project_id: str) -> None:
        self.delete_where(tx, "embeddings", {"project_id": project_id})

    # --- Graph (Vertices, Edges, Cycle-Guarded WITH RECURSIVE Bounded BFS) ---

    def upsert_vertex(self, tx: Any, node: dict[str, Any], *, existing: bool = False) -> dict[str, Any]:
        if existing:
            self.update_where(tx, "nodes", set_fields=node, where={"id": node["id"]})
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
            if any(existing.get(k) != edge[k] for k in ("project_id", "from_id", "to_id")):
                self.delete_where(tx, "edges", {"id": edge["id"]})
            else:
                self.update_where(tx, "edges", set_fields=edge, where={"id": edge["id"]})
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

    def delete_graph_nodes(
        self,
        tx: Any,
        project_id: str,
        node_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not node_ids:
            return [{"count": 0}]
        from cairnmind.storage.storage_query import eq, is_in
        return self.delete_where(tx, "nodes", [eq("project_id", project_id), is_in("id", list(node_ids))])

    def delete_graph_edges(
        self,
        tx: Any,
        project_id: str,
        edge_ids: list[str],
        *,
        kinds: Any = None,
    ) -> list[dict[str, Any]]:
        if not edge_ids:
            return [{"count": 0}]
        from cairnmind.storage.storage_query import eq, is_in
        conds: list[Any] = [is_in("id", list(edge_ids))]
        if kinds is not None:
            conds = [eq("project_id", project_id), is_in("kind", list(kinds)), *conds]
        return self.delete_where(tx, "edges", conds)

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
        from cairnmind.storage.storage_query import sqlite_id_filter_sql
        conn = self._conn()
        elig_clause, elig_params = sqlite_id_filter_sql(conn, "id", eligible_nodes, slot=0)
        conds = ["id = ?"]
        sql_params: list[Any] = [project_id, *elig_params, exact_query]
        if seed_sources:
            src_clause, src_params = sqlite_id_filter_sql(conn, "source_id", seed_sources, slot=1)
            conds.append(src_clause)
            sql_params.extend(src_params)
        for word in words:
            conds.append("LOWER(label) LIKE ?")
            sql_params.append(f"%{word.casefold()}%")
        sql_params.append(int(limit))
        rows = conn.execute(
            f"SELECT id FROM nodes WHERE project_id = ? AND is_current = 1 "
            f"AND {elig_clause} "
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
        """Cycle-safe bounded BFS via SQLite WITH RECURSIVE CTE (TEMP TABLE safe for large graphs)."""
        if not seed_ids or not eligible_nodes:
            return []
        from cairnmind.storage.storage_query import sqlite_id_filter_sql
        t0 = time.monotonic()
        conn = self._conn()
        seed_clause, seed_params = sqlite_id_filter_sql(conn, "id", seed_ids, slot=0)
        node_clause, node_params = sqlite_id_filter_sql(conn, "id", eligible_nodes, slot=1)
        if not eligible_edges:
            rows = conn.execute(
                f"SELECT doc, 0 AS distance FROM nodes "
                f"WHERE project_id = ? AND is_current = 1 "
                f"AND {seed_clause} AND {node_clause} "
                f"AND (json_extract(doc, '$.metadata.status') IS NULL "
                f"     OR json_extract(doc, '$.metadata.status') <> 'superseded') "
                f"ORDER BY id LIMIT ?",
                [project_id, *seed_params, *node_params, int(limit)],
            ).fetchall()
            self._graph_latencies_ms.append((time.monotonic() - t0) * 1000.0)
            return [{**json.loads(row[0]), "distance": int(row[1])} for row in rows]

        edge_clause, edge_params = sqlite_id_filter_sql(conn, "id", eligible_edges, slot=2)
        sql = f"""
        WITH RECURSIVE
        valid_nodes(id) AS (
            SELECT id FROM nodes
            WHERE project_id = ? AND is_current = 1
              AND {node_clause}
              AND (json_extract(doc, '$.metadata.status') IS NULL
                   OR json_extract(doc, '$.metadata.status') <> 'superseded')
        ),
        valid_edges(src, dst) AS (
            SELECT from_id, to_id FROM edges
            WHERE project_id = ? AND is_current = 1 AND status <> 'superseded'
              AND {edge_clause}
            UNION
            SELECT to_id, from_id FROM edges
            WHERE project_id = ? AND is_current = 1 AND status <> 'superseded'
              AND {edge_clause}
        ),
        walk(node_id, depth, path) AS (
            SELECT id, 0, '|' || id || '|'
            FROM valid_nodes
            WHERE {seed_clause}
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
            *node_params,
            project_id,
            *edge_params,
            project_id,
            *edge_params,
            *seed_params,
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
        from cairnmind.storage.storage_query import sqlite_id_filter_sql
        conn = self._conn()
        src_clause, src_params = sqlite_id_filter_sql(conn, "source_id", eligible_sources, slot=0)
        rows = conn.execute(
            f"SELECT doc FROM nodes WHERE project_id = ? AND is_current = 1 AND kind = 'symbol' "
            f"AND {src_clause} "
            f"ORDER BY label, json_extract(doc, '$.metadata.path'), json_extract(doc, '$.metadata.line_start'), id",
            [project_id, *src_params],
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

    # --- Backup Paging, Migration, Restore DDL & Native Sizing ---

    def _iter_backup_pages_conn(
        self,
        conn: sqlite3.Connection,
        collection: str,
        *,
        page_rows: int = 1000,
        page_bytes: int = 2 * 1024 * 1024,
    ) -> Iterator[str]:
        limit, last_rowid = page_rows, 0
        while True:
            rows = conn.execute(
                f'SELECT rowid, doc FROM "{collection}" WHERE rowid > ? ORDER BY rowid ASC LIMIT ?',
                (last_rowid, limit),
            ).fetchall()
            size = 0
            for row in rows:
                text = json_text(clean_row(json.loads(row[1])))
                size += len(text)
                yield text
            if len(rows) < limit:
                return
            last_rowid = int(rows[-1][0])
            limit = (
                max(1, limit // 2)
                if size > page_bytes
                else min(page_rows, limit * 2)
                if size < page_bytes // 4
                else limit
            )

    def ensure_restored_vector_types(self, vector_indexes: list[dict[str, Any]]) -> None:
        for registry in vector_indexes:
            name, dimension = registry.get("type_name"), registry.get("dimension")
            if not isinstance(name, str) or not name.startswith("vector_") or type(dimension) is not int or dimension <= 0:
                raise ValueError("Invalid vector namespace in backup")

    def wipe_all_collections(self, collections: tuple[str, ...]) -> None:
        wipe_order = list(reversed(collections))
        with self.raw_transaction() as tx:
            for col in wipe_order:
                tx._conn.execute(f'DELETE FROM "{col}"')
            tx._conn.execute("DELETE FROM chunks_fts")
            tx._conn.execute("DELETE FROM records_fts")

    def import_migration_batch(self, collection: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self.raw_transaction() as tx:
            if collection == "journal_state":
                tx._conn.execute("DELETE FROM journal_state")
            for row in rows:
                self._upsert_row(tx._conn, collection, row, key_field="id")

    def upsert_meta_row(self, row: dict[str, Any]) -> None:
        with self.raw_transaction() as tx:
            self._upsert_row(tx._conn, META_COLLECTION, clean_row(row), key_field="name")

    def measure_collection_exact_bytes(
        self,
        collection: str,
        *,
        project_id: str | None = None,
        project_key: str = "project_id",
    ) -> int:
        conn = self._conn()
        if project_id is not None:
            row = conn.execute(
                f'SELECT COALESCE(SUM(length(doc)), 0) FROM "{collection}" WHERE "{project_key}" = ?',
                (project_id,),
            ).fetchone()
        else:
            row = conn.execute(f'SELECT COALESCE(SUM(length(doc)), 0) FROM "{collection}"').fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def measure_collection_sample(
        self,
        collection: str,
        rows: int,
        *,
        project_id: str | None = None,
        project_key: str = "project_id",
        fields: str = "",
        sample_points: int = 32,
        sample_window: int = 8,
        seed: int = 184,
    ) -> list[dict[str, Any]]:
        conn = self._conn()
        from cairnmind.storage.storage_query import extract_field_path
        extra_fields = [f.strip().strip("`") for f in fields.split(",") if f.strip()]
        generator, seen, sample = random.Random(seed), set(), []  # noqa: S311 - deterministic non-crypto sampling
        part = rows / sample_points
        where = f' WHERE "{project_key}" = ?' if project_id is not None else ""
        base_params = [project_id] if project_id is not None else []
        for point in range(sample_points):
            offset = int(point * part + generator.random() * max(0.0, part - sample_window))
            fetched = conn.execute(
                f'SELECT rowid, doc, length(doc) FROM "{collection}"{where} LIMIT ? OFFSET ?',
                [*base_params, int(sample_window), int(offset)],
            ).fetchall()
            for r in fetched:
                rid = f"#{r[0]}"
                if rid in seen:
                    continue
                seen.add(rid)
                doc = json.loads(r[1])
                item: dict[str, Any] = {"rid": rid, "b": int(r[2] or 0)}
                for field in extra_fields:
                    item[field.split(".")[-1]] = extract_field_path(doc, field)
                sample.append(item)
        return sample

    def measure_collection_grouped(
        self,
        collection: str,
        group_fields: tuple[str, ...],
        *,
        project_id: str | None = None,
        project_key: str = "project_id",
        include_bytes: bool = True,
    ) -> list[dict[str, Any]]:
        from cairnmind.storage.storage_query import sqlite_field_expr, validate_field_path
        conn = self._conn()
        table_cols = _table_column_names(collection)
        fields = [validate_field_path(f) for f in group_fields]
        exprs = [sqlite_field_expr(table_cols, f) for f in fields]
        out_names = [f.split(".")[-1] for f in fields]
        select_expr = ", ".join(f'{expr} AS "{name}"' for expr, name in zip(exprs, out_names, strict=True))
        group_expr = ", ".join(exprs)
        bytes_col = ", COALESCE(SUM(length(doc)), 0) AS b" if include_bytes else ""
        where = f' WHERE "{project_key}" = ?' if project_id is not None else ""
        params = [project_id] if project_id is not None else []
        rows = conn.execute(
            f'SELECT {select_expr}, count(*) AS n{bytes_col} FROM "{collection}"{where} GROUP BY {group_expr}',
            params,
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item: dict[str, Any] = {}
            for i, name in enumerate(out_names):
                val = row[i]
                if name == "is_current" and val in (0, 1):
                    val = bool(val)
                item[name] = val
            item["n"] = int(row[len(out_names)] or 0)
            if include_bytes:
                item["b"] = int(row[len(out_names) + 1] or 0)
            out.append(item)
        return out

    def _compile_package_only(
        self, conn: sqlite3.Connection, table: str, project_id: str, only: Any
    ) -> tuple[str, list[Any]]:
        from cairnmind.storage.storage_query import compile_sqlite_where, eq, is_in, not_in
        table_cols = _table_column_names(table)
        conds: list[Any] = [eq("project_id", project_id)]
        if only is not None:
            if isinstance(only, tuple) and len(only) == 2 and isinstance(only[0], str):
                # Support legacy tuple specs e.g. ("kind IN :kept_nodes", {"kept_nodes": [...]})
                expr_str, expr_params = only
                m_not = re.fullmatch(r"\s*`?([A-Za-z_][A-Za-z0-9_.]*)`?\s+NOT\s+IN\s+:([A-Za-z_][A-Za-z0-9_]*)\s*", expr_str, re.I)
                m_in = re.fullmatch(r"\s*`?([A-Za-z_][A-Za-z0-9_.]*)`?\s+IN\s+:([A-Za-z_][A-Za-z0-9_]*)\s*", expr_str, re.I)
                if m_not:
                    conds.append(not_in(m_not.group(1), expr_params.get(m_not.group(2), ())))
                elif m_in:
                    conds.append(is_in(m_in.group(1), expr_params.get(m_in.group(2), ())))
                else:
                    raise ValueError(f"Unsupported SQLite package filter: {expr_str!r}")
            else:
                from cairnmind.storage.storage_query import normalize_where
                conds.extend(normalize_where(only))
        return compile_sqlite_where(conn, table_cols, conds)

    def measure_package_table(
        self,
        table: str,
        project_id: str,
        *,
        only: Any = None,
        group_sources: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        conn = self._conn()
        where_sql, params = self._compile_package_only(conn, table, project_id, only)
        if group_sources:
            rows = conn.execute(
                f'SELECT "kind", "is_current", count(*) AS n, '
                f'COALESCE(SUM(length(CAST(doc AS BLOB))), 0) AS b, '
                f'COALESCE(MAX(length(CAST(doc AS BLOB))), 0) AS m '
                f'FROM "{table}" WHERE {where_sql} GROUP BY "kind", "is_current"',
                params,
            ).fetchall()
            out = [
                {
                    "kind": r[0],
                    "is_current": bool(r[1]) if r[1] in (0, 1) else r[1],
                    "n": int(r[2] or 0),
                    "b": int(r[3] or 0),
                    "m": int(r[4] or 0),
                }
                for r in rows
            ]
        else:
            rows = conn.execute(
                f'SELECT count(*) AS n, '
                f'COALESCE(SUM(length(CAST(doc AS BLOB))), 0) AS b, '
                f'COALESCE(MAX(length(CAST(doc AS BLOB))), 0) AS m '
                f'FROM "{table}" WHERE {where_sql}',
                params,
            ).fetchall()
            out = [
                {
                    "kind": None,
                    "is_current": None,
                    "n": int(r[0] or 0),
                    "b": int(r[1] or 0),
                    "m": int(r[2] or 0),
                }
                for r in rows
            ]
        return out, True

    def measure_package_max_bytes(
        self,
        table: str,
        project_id: str,
        *,
        floor_chars: int,
        only: Any = None,
    ) -> int | None:
        conn = self._conn()
        where_sql, params = self._compile_package_only(conn, table, project_id, only)
        row = conn.execute(
            f'SELECT MAX(length(CAST(doc AS BLOB))) FROM "{table}" '
            f'WHERE {where_sql} AND length(doc) > ?',
            [*params, int(floor_chars)],
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

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

    def list_collections(self) -> set[str]:
        return self._backend.list_collections()

    def server_time_ms(self) -> int:
        return self._backend.server_time_ms()

    def insert_document(self, collection: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._backend.raw_transaction() as tx:
            return tx.insert_document(collection, data)

    def insert_documents(self, collection: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._backend.raw_transaction() as tx:
            return tx.insert_documents(collection, rows)

    def upsert_document(self, collection: str, data: dict[str, Any], key_field: str = "id") -> dict[str, Any]:
        with self._backend.raw_transaction() as tx:
            return tx.upsert_document(collection, data, key_field=key_field)

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self._backend.query(sql, params)

    def command(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self._backend.command(sql, params)

    def find(
        self,
        collection: str,
        where: Any = (),
        *,
        columns: Any = None,
        order_by: Any = (),
        limit: int | None = None,
        distinct: bool = False,
    ) -> list[dict[str, Any]]:
        return self._backend.find(
            collection, where, columns=columns, order_by=order_by, limit=limit, distinct=distinct
        )

    def find_one(
        self,
        collection: str,
        where: Any = (),
        *,
        columns: Any = None,
        order_by: Any = (),
        required: bool = False,
    ) -> dict[str, Any] | None:
        return self._backend.find_one(
            collection, where, columns=columns, order_by=order_by, required=required
        )

    def count(self, collection: str, where: Any = ()) -> int:
        return self._backend.count(collection, where)

    def group_counts(
        self,
        collection: str,
        group_by: Any,
        where: Any = (),
        *,
        count_alias: str = "total",
    ) -> list[dict[str, Any]]:
        return self._backend.group_counts(collection, group_by, where, count_alias=count_alias)

    def update_where(
        self,
        collection: str,
        *,
        set_fields: dict[str, Any] | None = None,
        remove_fields: Any = (),
        where: Any = (),
    ) -> list[dict[str, Any]]:
        return self._backend.update_where(
            self._backend, collection, set_fields=set_fields, remove_fields=remove_fields, where=where
        )

    def delete_where(
        self,
        collection: str,
        where: Any = (),
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._backend.delete_where(self._backend, collection, where, limit=limit)

    def transaction(self, commit_timeout: float = 30.0, *, commit: bool = True):
        return self._backend.raw_transaction(commit_timeout=commit_timeout)

    def close(self) -> None:
        self._backend.close()
