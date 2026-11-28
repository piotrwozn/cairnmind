"""SQLite row operations, FTS5/vec0 write synchronization, and declarative CRUD mixin."""
from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from cairnmind.storage import sqlite_vectors as _vec
from cairnmind.storage.sql import clean_row, json_text
from cairnmind.storage.sqlite_schema import (
    _COLLECTION_COLUMNS,
    _RAW_SQL_DISABLED,
    _sql_col_value,
    _table_column_names,
)
from cairnmind.storage.sqlite_vectors import pack_vector
from cairnmind.storage.storage_backend import META_COLLECTION


class _SQLiteCrudMixin:
    """Mixin implementing row persistence, FTS5/vec0 sync, and declarative CRUD for SQLiteBackend."""

    if TYPE_CHECKING:
        _read_only: bool
        _sqlite_vec_available: bool

        def _conn(self) -> sqlite3.Connection: ...

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
        if collection == "embeddings":
            _vec.sync_vec_upsert(
                conn,
                row,
                sqlite_vec_available=self._sqlite_vec_available,
                read_only=self._read_only,
            )

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
            if collection == "embeddings":
                _vec.sync_vec_delete_rows(
                    conn,
                    [(old_id, row.get("project_id"), row.get("namespace"), row.get("dimension"))],
                    sqlite_vec_available=self._sqlite_vec_available,
                    read_only=self._read_only,
                )
        self._sync_fts_upsert(conn, collection, row)
        if collection == "embeddings":
            _vec.sync_vec_upsert(
                conn,
                row,
                sqlite_vec_available=self._sqlite_vec_available,
                read_only=self._read_only,
            )

    # --- StorageBackend Protocol Document CRUD ---

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
        deleted_embeddings: list[tuple[str, str | None, str | None, int | None]] = []
        if limit is not None:
            lim = max(0, int(limit))
            if lim == 0:
                return [{"count": 0}]
            if collection == "embeddings":
                sql = (
                    'SELECT id, project_id, namespace, dimension FROM "embeddings"'
                    + (f" WHERE {where_sql}" if where_sql else "")
                    + f" LIMIT {lim}"
                )
                emb_rows = conn.execute(sql, params).fetchall()
                deleted_embeddings = [(str(r[0]), r[1], r[2], r[3]) for r in emb_rows]
                ids = [r[0] for r in deleted_embeddings]
            else:
                sql = f'SELECT "{pk}" FROM "{collection}"' + (f" WHERE {where_sql}" if where_sql else "") + f" LIMIT {lim}"
                ids = [row[0] for row in conn.execute(sql, params).fetchall()]
            if ids:
                clause, id_params = sqlite_id_filter_sql(conn, f'"{pk}"', ids, slot=7)
                conn.execute(f'DELETE FROM "{collection}" WHERE {clause}', id_params)
        else:
            if collection == "embeddings":
                sql = (
                    'SELECT id, project_id, namespace, dimension FROM "embeddings"'
                    + (f" WHERE {where_sql}" if where_sql else "")
                )
                emb_rows = conn.execute(sql, params).fetchall()
                deleted_embeddings = [(str(r[0]), r[1], r[2], r[3]) for r in emb_rows]
                ids = [r[0] for r in deleted_embeddings]
            else:
                sql = f'SELECT "{pk}" FROM "{collection}"' + (f" WHERE {where_sql}" if where_sql else "")
                ids = [row[0] for row in conn.execute(sql, params).fetchall()]
            if where_sql:
                conn.execute(f'DELETE FROM "{collection}" WHERE {where_sql}', params)
            else:
                conn.execute(f'DELETE FROM "{collection}"')
        if collection in {"chunks", "records"} and ids:
            self._sync_fts_delete(conn, collection, [str(x) for x in ids])
        elif collection == "embeddings" and deleted_embeddings:
            _vec.sync_vec_delete_rows(
                conn,
                deleted_embeddings,
                sqlite_vec_available=self._sqlite_vec_available,
                read_only=self._read_only,
            )
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
