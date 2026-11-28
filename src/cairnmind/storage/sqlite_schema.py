"""SQLite schema constants, DDL initialization, transaction handle, and client proxy."""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

from cairnmind.storage.sql import json_text
from cairnmind.storage.storage_backend import ALL_COLLECTIONS, META_COLLECTION

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

_RAW_SQL_DISABLED = (
    "Raw ArcadeDB SQL is disabled on SQLiteBackend; "
    "use declarative StorageBackend / storage_query methods"
)


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


def init_sqlite_schema(backend: Any, conn: sqlite3.Connection) -> None:
    """Create or migrate all 28 collections, FTS5 tables, and indexes."""
    ver_row = conn.execute("PRAGMA user_version").fetchone()
    current_version = int(ver_row[0]) if ver_row is not None else 0
    if 0 < current_version < SQLITE_SCHEMA_VERSION:
        backend._backup_before_schema_upgrade(current_version)
    with backend.raw_transaction():
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
        if not conn.execute("SELECT id FROM journal_state WHERE id='current'").fetchone():
            backend._insert_row(
                conn,
                "journal_state",
                {"id": "current", "epoch": str(uuid4()), "seq": 0, "first_seq": 1},
            )
        if not conn.execute(f"SELECT name FROM {META_COLLECTION} WHERE name='schema_version'").fetchone():
            backend._insert_row(conn, META_COLLECTION, {"name": "schema_version", "value": SQLITE_SCHEMA_VERSION})
        else:
            backend._upsert_row(
                conn, META_COLLECTION, {"name": "schema_version", "value": SQLITE_SCHEMA_VERSION}, key_field="name"
            )
        conn.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")


class SQLiteTransaction:
    """Native SQLite transaction handle compatible with JournalTransaction & StorageTransaction."""

    def __init__(self, backend: Any, conn: sqlite3.Connection, *, read_only: bool = False):
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


class _SQLiteClientProxy:
    """Adapter exposing ArcadeClient-shaped lifecycle methods over SQLiteBackend for Store."""

    def __init__(self, backend: Any):
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
