"""Embedded zero-dependency SQLite StorageBackend (PR #7 / Faza 4b, ADR-01/03/05).

Provides full behavioral parity with ArcadeBackend across all 28 collections:
- WAL mode, synchronous=NORMAL, foreign_keys=ON, busy_timeout=5000
- BEGIN IMMEDIATE write transactions with jittered SQLITE_BUSY retry + telemetry counter
- Read-only mode via URI ``?mode=ro`` + ``PRAGMA query_only=ON``
- Versioned schema via ``PRAGMA user_version`` with automatic ``pre-vN.bak`` backup
- FTS5 (``chunks_fts``, ``records_fts``) with ``unicode61 tokenchars '_-.:/'`` and safe token quoting
- Cycle-guarded ``WITH RECURSIVE`` bounded graph BFS (``instr(path, '|' || dst || '|') = 0``)
- Canonical ``float32`` BLOB embeddings + native ``sqlite-vec`` ``vec0`` ANN / exact cosine fallback
"""
from __future__ import annotations

import logging
import math
import random
import shutil
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cairnmind.storage import sqlite_vectors as _vec
from cairnmind.storage.sqlite_crud import _SQLiteCrudMixin
from cairnmind.storage.sqlite_graph import _SQLiteGraphMixin
from cairnmind.storage.sqlite_measure import _SQLiteMeasureMixin
from cairnmind.storage.sqlite_schema import (
    _COLLECTION_COLUMNS as _COLLECTION_COLUMNS,
)
from cairnmind.storage.sqlite_schema import (
    _LOOKUP_INDEXES as _LOOKUP_INDEXES,
)
from cairnmind.storage.sqlite_schema import (
    _RAW_SQL_DISABLED as _RAW_SQL_DISABLED,
)
from cairnmind.storage.sqlite_schema import (
    BUSY_TIMEOUT_MS as BUSY_TIMEOUT_MS,
)
from cairnmind.storage.sqlite_schema import (
    MAX_BUSY_RETRIES as MAX_BUSY_RETRIES,
)
from cairnmind.storage.sqlite_schema import (
    SQLITE_SCHEMA_VERSION as SQLITE_SCHEMA_VERSION,
)
from cairnmind.storage.sqlite_schema import (
    SQLiteTransaction as SQLiteTransaction,
)
from cairnmind.storage.sqlite_schema import (
    _is_busy_error as _is_busy_error,
)
from cairnmind.storage.sqlite_schema import (
    _sql_col_value as _sql_col_value,
)
from cairnmind.storage.sqlite_schema import (
    _SQLiteClientProxy as _SQLiteClientProxy,
)
from cairnmind.storage.sqlite_schema import (
    _table_column_names as _table_column_names,
)
from cairnmind.storage.sqlite_schema import (
    init_sqlite_schema,
)
from cairnmind.storage.sqlite_schema import (
    quote_fts5_query as quote_fts5_query,
)
from cairnmind.storage.sqlite_vectors import (
    cosine_distance as cosine_distance,
)
from cairnmind.storage.sqlite_vectors import (
    pack_vector as pack_vector,
)
from cairnmind.storage.sqlite_vectors import (
    unpack_vector as unpack_vector,
)

log = logging.getLogger(__name__)


class SQLiteBackend(_SQLiteCrudMixin, _SQLiteGraphMixin, _SQLiteMeasureMixin):
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
        self._vec0_synced: set[str] = set()
        self._vec_ann_queries: int = 0
        self._vec_fallback_queries: int = 0
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
            "vec_ann_queries": self._vec_ann_queries,
            "vec_fallback_queries": self._vec_fallback_queries,
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
        self._vec0_synced.clear()
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
        init_sqlite_schema(self, self._conn())

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

    # --- Vectors (ADR-03: native sqlite-vec vec0 KNN + float32 BLOB exact cosine fallback) ---

    def ensure_vector_index(
        self,
        project_id: str,
        namespace: str,
        dimension: int,
        model: str,
    ) -> dict[str, Any] | None:
        if not 1 <= dimension <= _vec.VEC0_MAX_DIM:
            raise ValueError("Unsupported embedding dimension")
        conn = self._conn()
        if not conn.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone():
            return None
        type_name = _vec.vector_type_name(project_id, namespace, dimension)
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
            if self._sqlite_vec_available and not self._read_only:
                vec_table, _ = _vec.backfill_vec0_table(conn, project_id, namespace, dimension, type_name)
                self._vec0_synced.add(vec_table)
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
        present, neighbors, warning, used_vec0 = _vec.search_vectors(
            conn,
            project_id,
            namespace,
            query_vector,
            eligible_sources,
            limit,
            sqlite_vec_available=self._sqlite_vec_available,
            read_only=self._read_only,
            synced_tables=self._vec0_synced,
            raw_tx_factory=self.raw_transaction,
        )
        if present and eligible_sources and int(limit) > 0:
            if used_vec0:
                self._vec_ann_queries += 1
            else:
                self._vec_fallback_queries += 1
        return present, neighbors, warning

    def delete_vectors_for_sources(self, tx: Any, source_ids: list[str]) -> None:
        if not source_ids:
            return
        from cairnmind.storage.storage_query import is_in
        self.delete_where(tx, "embeddings", is_in("source_id", list(source_ids)))

    def delete_vectors_for_project(self, tx: Any, project_id: str) -> None:
        self.delete_where(tx, "embeddings", {"project_id": project_id})

    # --- Read-only client proxy compatibility for Store ---

    def as_client_proxy(self) -> _SQLiteClientProxy:
        return _SQLiteClientProxy(self)
