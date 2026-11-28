"""SQLite streaming export, backup paging, migration, restore DDL, and sizing mixin."""
from __future__ import annotations

import json
import random
import re
import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

from cairnmind.storage import sqlite_vectors as _vec
from cairnmind.storage.sql import clean_row, json_text
from cairnmind.storage.sqlite_schema import _table_column_names
from cairnmind.storage.storage_backend import ALL_COLLECTIONS, META_COLLECTION


class _SQLiteMeasureMixin:
    """Mixin implementing streaming export, backup paging, migration, and sizing for SQLiteBackend."""

    if TYPE_CHECKING:
        _read_only: bool
        _sqlite_vec_available: bool
        _vec0_synced: set[str]

        def _conn(self) -> sqlite3.Connection: ...

        def list_collections(self, reader: Any = None) -> set[str]: ...

        def raw_transaction(self, commit_timeout: float = 30.0) -> AbstractContextManager[Any]: ...

        def _upsert_row(
            self,
            conn: sqlite3.Connection,
            collection: str,
            data: dict[str, Any],
            *,
            key_field: str = "id",
        ) -> None: ...

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
        conn = self._conn()
        for registry in vector_indexes:
            name, dimension = registry.get("type_name"), registry.get("dimension")
            if not isinstance(name, str) or not name.startswith("vector_") or type(dimension) is not int or dimension <= 0:
                raise ValueError("Invalid vector namespace in backup")
            if self._sqlite_vec_available and not self._read_only:
                with self.raw_transaction():
                    _vec.ensure_vec0_table(conn, name, dimension)

    def wipe_all_collections(self, collections: tuple[str, ...]) -> None:
        wipe_order = list(reversed(collections))
        with self.raw_transaction() as tx:
            for col in wipe_order:
                tx._conn.execute(f'DELETE FROM "{col}"')
            tx._conn.execute("DELETE FROM chunks_fts")
            tx._conn.execute("DELETE FROM records_fts")
            _vec.drop_all_vec0_tables(tx._conn)
        self._vec0_synced.clear()

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
                m_not = re.fullmatch(
                    r"\s*`?([A-Za-z_][A-Za-z0-9_.]*)`?\s+NOT\s+IN\s+:([A-Za-z_][A-Za-z0-9_]*)\s*",
                    expr_str,
                    re.I,
                )
                m_in = re.fullmatch(
                    r"\s*`?([A-Za-z_][A-Za-z0-9_.]*)`?\s+IN\s+:([A-Za-z_][A-Za-z0-9_]*)\s*",
                    expr_str,
                    re.I,
                )
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
