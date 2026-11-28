"""Reference ArcadeDB implementation of the StorageBackend protocol (PR #6 / Faza 4a).

Wraps ArcadeClient / ReadOnlyClient and Store, isolating every ArcadeDB-specific SQL
dialect construct (SEARCH_INDEX, vector.neighbors, LSM_VECTOR, TRAVERSE, CREATE VERTEX/EDGE,
MATCHES, toLowerCase(), sysdate(), schema:types, UPSERT) behind clean domain methods.
"""
from __future__ import annotations

from typing import Any

from cairnmind.storage.arcade import REQUEST_TIMEOUT
from cairnmind.storage.arcade_graph import (
    GRAPH_ROWS_PER_REQUEST as GRAPH_ROWS_PER_REQUEST,
)
from cairnmind.storage.arcade_graph import (
    _ArcadeGraphMixin,
)
from cairnmind.storage.arcade_graph import (
    vector_type_name as vector_type_name,
)
from cairnmind.storage.arcade_measure import _ArcadeMeasureMixin
from cairnmind.storage.sql import clean_row, json_text, one_row, quote_name


class ArcadeBackend(_ArcadeGraphMixin, _ArcadeMeasureMixin):
    """StorageBackend implementation backed by ArcadeDB HTTP API."""

    def __init__(self, store: Any):
        self._store = store
        self._vector_indexes: dict[str, dict[str, Any]] = {}

    @property
    def backend_kind(self) -> str:
        return "arcade"

    @property
    def read_only(self) -> bool:
        return bool(getattr(self._store, "read_only", False))

    @property
    def _client(self) -> Any:
        return self._store.client

    def ensure_database(self) -> None:
        self._client.ensure_database()

    def database_exists(self) -> bool:
        return bool(self._client.database_exists())

    def list_collections(self, reader: Any = None) -> set[str]:
        target = reader if reader is not None else self._client
        return {str(row.get("name")) for row in target.query("SELECT name FROM schema:types") if row.get("name")}

    def server_time_ms(self, reader: Any = None) -> int:
        target = reader if reader is not None else self._store
        rows = target.query("SELECT sysdate().asLong() AS milliseconds")
        milliseconds = rows[0]["milliseconds"] if rows else None
        if not isinstance(milliseconds, int) or isinstance(milliseconds, bool):
            raise ValueError("The database server gave no time; the writer row was not changed.")
        return milliseconds

    def close(self) -> None:
        self._client.close()

    def transaction(self, commit_timeout: float = REQUEST_TIMEOUT):
        return self._store.transaction(commit_timeout=commit_timeout)

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self._store.query(sql, params)

    def command(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self._store.command(sql, params)

    # --- Structured Document CRUD ---

    def insert_document(self, tx: Any, collection: str, data: dict[str, Any]) -> dict[str, Any]:
        if hasattr(tx, "insert_document"):
            return tx.insert_document(collection, data)
        tx.command(f"INSERT INTO {quote_name(collection)} CONTENT {json_text(data)}")
        return data

    def insert_documents(self, tx: Any, collection: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if hasattr(tx, "insert_documents"):
            return tx.insert_documents(collection, rows)
        stored = tx.command(f"INSERT INTO {quote_name(collection)} CONTENT :rows", {"rows": rows})
        if not isinstance(stored, list) or len(stored) != len(rows):
            raise RuntimeError(f"The database did not store every row of a group in {collection}")
        return stored

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
        assignments = ",".join(f"{quote_name(key)}=:value_{key}" for key in data)
        return tx.command(
            f"UPDATE {quote_name(collection)} SET {assignments} WHERE {where}",
            {**params, **{f"value_{key}": value for key, value in data.items()}},
        )

    def upsert_document(
        self,
        tx: Any,
        collection: str,
        data: dict[str, Any],
        key_field: str = "id",
    ) -> dict[str, Any]:
        if hasattr(tx, "upsert_document"):
            return tx.upsert_document(collection, data, key_field=key_field)
        setters = ",".join(f"{quote_name(key)}=:value_{key}" for key in data)
        params = {f"value_{key}": value for key, value in data.items()}
        params["upsert_key"] = data[key_field]
        tx.command(
            f"UPDATE {quote_name(collection)} SET {setters} UPSERT WHERE {quote_name(key_field)}=:upsert_key",
            params,
        )
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
        clause = f" WHERE {where}" if where else ""
        return tx.command(f"DELETE FROM {quote_name(collection)}{clause}", params or {})

    def get_by_id(self, reader: Any, collection: str, row_id: str) -> dict[str, Any] | None:
        return one_row(reader, f"SELECT FROM {quote_name(collection)} WHERE id=:id", {"id": row_id}, False)

    # --- Declarative Backend-Neutral Queries & Mutations ---

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
        from cairnmind.storage.storage_query import _build_arcade_select
        target = reader if reader is not None else self._store
        sql, params = _build_arcade_select(
            collection, where, columns=columns, order_by=order_by, limit=limit, distinct=distinct
        )
        return [clean_row(row) for row in target.query(sql, params)]

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
        from cairnmind.storage.storage_query import missing_row_message
        rows = self.find(collection, where, columns=columns, order_by=order_by, limit=1, reader=reader)
        if not rows:
            if required:
                raise ValueError(missing_row_message(collection))
            return None
        return rows[0]

    def count(self, collection: str, where: Any = (), *, reader: Any = None) -> int:
        from cairnmind.storage.storage_query import compile_arcade_where
        target = reader if reader is not None else self._store
        where_sql, params = compile_arcade_where(where)
        clause = f" WHERE {where_sql}" if where_sql else ""
        rows = target.query(f"SELECT count(*) AS total FROM {quote_name(collection)}{clause}", params)
        return int(rows[0]["total"]) if rows and rows[0].get("total") is not None else 0

    def group_counts(
        self,
        collection: str,
        group_by: Any,
        where: Any = (),
        *,
        count_alias: str = "total",
        reader: Any = None,
    ) -> list[dict[str, Any]]:
        from cairnmind.storage.storage_query import compile_arcade_where, quote_arcade_field
        target = reader if reader is not None else self._store
        fields = [quote_arcade_field(f) for f in group_by]
        where_sql, params = compile_arcade_where(where)
        clause = f" WHERE {where_sql}" if where_sql else ""
        group_expr = ", ".join(fields)
        rows = target.query(
            f"SELECT {group_expr}, count(*) AS {quote_name(count_alias)} "
            f"FROM {quote_name(collection)}{clause} GROUP BY {group_expr}",
            params,
        )
        return [clean_row(row) for row in rows]

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
        from cairnmind.storage.storage_query import compile_arcade_where, quote_arcade_field
        where_sql, params = compile_arcade_where(where)
        clause = f" WHERE {where_sql}" if where_sql else ""
        out: list[dict[str, Any]] = []
        if set_fields:
            assignments = []
            set_params = dict(params)
            for idx, (field, value) in enumerate(set_fields.items()):
                pname = f"set_{idx}"
                assignments.append(f"{quote_arcade_field(field)}=:{pname}")
                set_params[pname] = value
            out = list(
                tx.command(
                    f"UPDATE {quote_name(collection)} SET {', '.join(assignments)}{clause}",
                    set_params,
                )
            )
        if remove_fields:
            rem = ", ".join(quote_arcade_field(f) for f in remove_fields)
            out = list(tx.command(f"UPDATE {quote_name(collection)} REMOVE {rem}{clause}", params))
        return out

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
        from cairnmind.storage.storage_query import compile_arcade_where
        where_sql, params = compile_arcade_where(where)
        clause = f" WHERE {where_sql}" if where_sql else ""
        lim = f" LIMIT {max(0, int(limit))}" if limit is not None else ""
        return list(tx.command(f"DELETE FROM {quote_name(collection)}{clause}{lim}", params))
