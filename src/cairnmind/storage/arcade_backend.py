"""Reference ArcadeDB implementation of the StorageBackend protocol (PR #6 / Faza 4a).

Wraps ArcadeClient / ReadOnlyClient and Store, isolating every ArcadeDB-specific SQL
dialect construct (SEARCH_INDEX, vector.neighbors, LSM_VECTOR, TRAVERSE, CREATE VERTEX/EDGE,
MATCHES, toLowerCase(), sysdate(), schema:types, UPSERT) behind clean domain methods.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

from cairnmind.storage.arcade import REQUEST_TIMEOUT, ArcadeError
from cairnmind.storage.sql import IDENTIFIER, clean_row, json_text, one_row, quote_name
from cairnmind.storage.storage_backend import ALL_COLLECTIONS

GRAPH_ROWS_PER_REQUEST = 250


def vector_type_name(project_id: str, namespace: str, dimension: int) -> str:
    identity = json.dumps([project_id, namespace, dimension], separators=(",", ":"))
    return "vector_" + hashlib.sha256(identity.encode()).hexdigest()[:24]


class ArcadeBackend:
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

    # --- Full-Text Search ---

    def search_chunks_fts(
        self,
        project_id: str,
        words: list[str],
        eligible_sources: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        if not words or not eligible_sources:
            return []
        expression = " OR ".join(words)
        return self._store.query(
            "SELECT *, $score AS search_score FROM chunks "
            "WHERE SEARCH_INDEX('chunks[text]', :query)=true AND project_id=:project AND is_current=true "
            "AND source_id IN :eligible_sources "
            f"ORDER BY $score DESC LIMIT {int(limit)}",
            {"query": expression, "project": project_id, "eligible_sources": eligible_sources},
        )

    def search_records_fts(
        self,
        project_id: str,
        words: list[str],
        eligible_records: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        if not words or not eligible_records:
            return []
        return self._store.query(
            "SELECT *, $score AS search_score FROM records "
            "WHERE SEARCH_INDEX('records[content]', :query)=true AND project_id=:project AND status<>'superseded' "
            "AND id IN :eligible_records "
            f"ORDER BY $score DESC LIMIT {int(limit)}",
            {"query": " OR ".join(words), "project": project_id, "eligible_records": eligible_records},
        )

    # --- Vectors ---

    def ensure_vector_index(
        self,
        project_id: str,
        namespace: str,
        dimension: int,
        model: str,
    ) -> dict[str, Any] | None:
        if not 1 <= dimension <= 8192:
            raise ValueError("Unsupported embedding dimension")
        if not self._store.query("SELECT id FROM projects WHERE id=:id", {"id": project_id}):
            return None
        type_name = vector_type_name(project_id, namespace, dimension)
        index_name = f"{type_name}[vector]"
        if type_name in self._vector_indexes:
            if self._store.query("SELECT id FROM vector_indexes WHERE id=:id", {"id": type_name}):
                return self._vector_indexes[type_name]
            self._vector_indexes.pop(type_name, None)
        self._store.command(f"CREATE DOCUMENT TYPE {type_name} IF NOT EXISTS")
        self._store.command(f"CREATE PROPERTY {type_name}.id IF NOT EXISTS STRING")
        self._store.command(f"CREATE PROPERTY {type_name}.vector IF NOT EXISTS ARRAY_OF_FLOATS")
        self._store.command(f"CREATE INDEX IF NOT EXISTS ON {type_name}(id) UNIQUE")
        self._store.command(
            f"CREATE INDEX IF NOT EXISTS ON {type_name}(vector) LSM_VECTOR "
            f"METADATA {{dimensions:{dimension}, similarity:'COSINE', idPropertyName:'id'}}"
        )
        values = {
            "id": type_name,
            "project_id": project_id,
            "model": model,
            "namespace": namespace,
            "dimension": dimension,
            "type_name": type_name,
            "index_name": index_name,
        }
        with self._store.transaction() as tx:
            try:
                self._store._require_project(tx, project_id)
            except ValueError:
                return None
            self.upsert_document(tx, "vector_indexes", values, key_field="id")
        self._vector_indexes[type_name] = values
        return values

    def has_vector_embedding(
        self,
        project_id: str,
        namespace: str,
        chunk_id: str,
        dimension: int,
        identifier: str,
    ) -> bool:
        type_name = vector_type_name(project_id, namespace, dimension)
        return bool(
            self._store.query(
                f"SELECT id FROM {quote_name(type_name)} WHERE id=:id AND is_current=true",
                {"id": identifier},
            )
        )

    def upsert_embedding(
        self,
        tx: Any,
        values: dict[str, Any],
        index_info: dict[str, Any] | None = None,
    ) -> None:
        self.upsert_document(tx, "embeddings", values, key_field="id")
        type_name = (index_info or {}).get("type_name") or vector_type_name(
            values["project_id"], values["namespace"], int(values["dimension"])
        )
        self.upsert_document(tx, type_name, {**values, "is_current": True}, key_field="id")

    def search_vectors(
        self,
        project_id: str,
        namespace: str,
        query_vector: list[float],
        eligible_sources: list[str],
        limit: int,
    ) -> tuple[bool, list[dict[str, Any]], str | None]:
        indexes = self._store.query(
            "SELECT FROM vector_indexes WHERE project_id=:project AND namespace=:namespace AND dimension=:dimension",
            {"project": project_id, "namespace": namespace, "dimension": len(query_vector)},
        )
        if not indexes:
            return False, [], None
        expected = vector_type_name(project_id, namespace, len(query_vector))
        index_name = f"{expected}[vector]"
        try:
            neighbors = self._store.query(
                "SELECT expand(`vector.neighbors`(:index_name, :query_vector, :count, "
                "{efSearch: :ef_search,where: :scope,filter:(SELECT @rid FROM "
                + expected
                + " WHERE project_id=:project AND namespace=:namespace AND is_current=true "
                "AND source_id IN :eligible_sources)}))",
                {
                    "index_name": index_name,
                    "query_vector": query_vector,
                    "count": int(limit),
                    "ef_search": max(200, int(limit)),
                    "scope": {
                        "project_id": project_id,
                        "namespace": namespace,
                        "is_current": True,
                    },
                    "project": project_id,
                    "namespace": namespace,
                    "eligible_sources": eligible_sources,
                },
            )
            return True, neighbors, None
        except ArcadeError as exc:
            return True, [], f"Native vector search unavailable: {type(exc).__name__}"

    def delete_vectors_for_sources(self, tx: Any, source_ids: list[str]) -> None:
        if not source_ids:
            return
        tx.command("DELETE FROM embeddings WHERE source_id IN :ids", {"ids": source_ids})
        for row in tx.query("SELECT FROM vector_indexes"):
            name = row.get("type_name")
            if name and IDENTIFIER.fullmatch(name) and name.startswith("vector_"):
                tx.command(f"DELETE FROM {quote_name(name)} WHERE source_id IN :ids", {"ids": source_ids})

    def delete_vectors_for_project(self, tx: Any, project_id: str) -> None:
        source_ids = [r["id"] for r in tx.query("SELECT id FROM sources WHERE project_id=:project", {"project": project_id})]
        self.delete_vectors_for_sources(tx, source_ids)
        tx.command("DELETE FROM embeddings WHERE project_id=:project", {"project": project_id})

    # --- Graph (Vertices, Edges, Bounded BFS) ---

    def upsert_vertex(self, tx: Any, node: dict[str, Any], *, existing: bool = False) -> dict[str, Any]:
        if existing:
            self.update_document(tx, "nodes", node, "id=:id", {"id": node["id"], "project_id": node["project_id"]})
        elif hasattr(tx, "create_vertex"):
            tx.create_vertex("nodes", node)
        else:
            tx.command(f"CREATE VERTEX nodes CONTENT {json_text(node)}")
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
            tx.command(
                "CREATE EDGE edges FROM (SELECT FROM nodes WHERE id=:from_id) "
                f"TO (SELECT FROM nodes WHERE id=:to_id) CONTENT {json_text(edge)}",
                {"from_id": edge["from_id"], "to_id": edge["to_id"]},
            )
        return edge

    def write_graph_batch(
        self,
        tx: Any,
        project_id: str,
        created_nodes: list[dict[str, Any]],
        updated_nodes: list[dict[str, Any]],
        created_edges: list[dict[str, Any]],
    ) -> None:
        for offset in range(0, len(created_nodes), GRAPH_ROWS_PER_REQUEST):
            part = created_nodes[offset:offset + GRAPH_ROWS_PER_REQUEST]
            stored = tx.command("CREATE VERTEX nodes CONTENT :rows", {"rows": part, "project_id": project_id})
            if len(stored) != len(part):
                raise RuntimeError("The database did not create every graph vertex of a group")
        for offset in range(0, len(updated_nodes), GRAPH_ROWS_PER_REQUEST):
            statements, params = [], {}
            for index, node in enumerate(updated_nodes[offset:offset + GRAPH_ROWS_PER_REQUEST]):
                statements.append(
                    f"UPDATE nodes SET {','.join(f'{quote_name(key)}=:u{index}_{key}' for key in node)} "
                    f"WHERE id=:w{index}"
                )
                params.update({f"u{index}_{key}": value for key, value in node.items()})
                params[f"w{index}"] = node["id"]
            tx.script(statements, params, project_id)
        for offset in range(0, len(created_edges), GRAPH_ROWS_PER_REQUEST):
            part = created_edges[offset:offset + GRAPH_ROWS_PER_REQUEST]
            statements: list[str] = []
            params: dict[str, Any] = {"ids": [edge["id"] for edge in part]}
            for index, edge in enumerate(part):
                statements.append(
                    f"CREATE EDGE edges FROM (SELECT FROM nodes WHERE id=:f{index}) "
                    f"TO (SELECT FROM nodes WHERE id=:t{index}) CONTENT :e{index}"
                )
                params.update({f"f{index}": edge["from_id"], f"t{index}": edge["to_id"], f"e{index}": edge})
            statements.append("SELECT count(*) AS n FROM edges WHERE id IN :ids")
            found = tx.script(statements, params, project_id)
            if not found or found[0].get("n") != len(part):
                raise RuntimeError("The database did not create every graph edge of a group")

    def delete_graph_nodes(
        self,
        tx: Any,
        project_id: str,
        node_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not node_ids:
            return [{"count": 0}]
        return list(
            tx.command(
                "DELETE FROM nodes WHERE project_id=:project AND id IN :ids",
                {"project": project_id, "ids": list(node_ids)},
            )
        )

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
        if kinds is None:
            return list(
                tx.command(
                    "DELETE FROM edges WHERE id IN :ids",
                    {"project": project_id, "ids": list(edge_ids)},
                )
            )
        return list(
            tx.command(
                "DELETE FROM edges WHERE project_id=:project AND kind IN :kinds AND id IN :ids",
                {"project": project_id, "kinds": list(kinds), "ids": list(edge_ids)},
            )
        )

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
        conditions = ["id=:exact", "source_id IN :sources"]
        params: dict[str, Any] = {
            "project": project_id,
            "exact": exact_query,
            "sources": seed_sources,
            "eligible_nodes": eligible_nodes,
        }
        for index, word in enumerate(words):
            conditions.append(f"label.toLowerCase() LIKE :term{index}")
            params[f"term{index}"] = f"%{word}%"
        return self._store.query(
            "SELECT id FROM nodes WHERE project_id=:project AND is_current=true "
            "AND id IN :eligible_nodes "
            "AND (metadata.status IS NULL OR metadata.status<>'superseded') "
            "AND (" + " OR ".join(conditions) + f") LIMIT {int(limit)}",
            params,
        )

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
        if not seed_ids or not eligible_nodes:
            return []
        return self._store.query(
            "SELECT *, $depth AS distance FROM ("
            "TRAVERSE bothE('edges')[project_id=:project AND is_current=true AND status<>'superseded' "
            "AND id IN :eligible_edges].bothV() "
            "FROM (SELECT FROM nodes WHERE id IN :ids AND project_id=:project AND id IN :eligible_nodes) "
            f"WHILE $depth<={int(max_depth)} AND project_id=:project AND is_current=true "
            "AND id IN :eligible_nodes "
            "AND (metadata.status IS NULL OR metadata.status<>'superseded') "
            f"LIMIT {int(limit)} STRATEGY BREADTH_FIRST)",
            {
                "project": project_id,
                "ids": seed_ids,
                "eligible_nodes": eligible_nodes,
                "eligible_edges": eligible_edges,
            },
        )

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
        select = (
            "SELECT id,label,source_id,metadata FROM nodes WHERE project_id=:project AND is_current=true "
            "AND kind='symbol' AND source_id IN :eligible AND "
        )
        order = "ORDER BY label, metadata.path, metadata.line_start, id"
        base_params: dict[str, Any] = {"project": project_id, "eligible": eligible_sources}
        if mode == "code":
            if not names:
                return []
            return self._store.query(
                select + f"(label IN :names OR metadata.member IN :names) {order} LIMIT {int(limit)}",
                {**base_params, "names": names},
            )
        if mode == "plain":
            if not names:
                return []
            return self._store.query(
                select
                + "(label IN :names OR metadata.member IN :names) AND metadata.kind IN ['constant', 'field'] "
                + f"{order} LIMIT {int(limit)}",
                {**base_params, "names": names},
            )
        if mode == "pattern":
            if not pattern:
                return []
            return self._store.query(
                select
                + "((metadata.kind IN ['field', 'parameter'] AND metadata.member.toLowerCase() MATCHES :pattern) "
                + "OR (metadata.kind = 'constant' AND label.toLowerCase() MATCHES :pattern)) "
                + f"{order} LIMIT {int(limit)}",
                {**base_params, "pattern": pattern},
            )
        if mode == "context":
            if not pattern or not owner or not file_pattern:
                return []
            return self._store.query(
                select
                + "((metadata.kind = 'field' AND metadata.member.toLowerCase() MATCHES :name "
                + "AND (metadata.owner.toLowerCase() MATCHES :owner OR metadata.path.toLowerCase() MATCHES :file)) "
                + "OR (metadata.kind = 'constant' AND label.toLowerCase() MATCHES :name "
                + "AND metadata.path.toLowerCase() MATCHES :file)) "
                + f"{order} LIMIT {int(limit)}",
                {**base_params, "name": pattern, "file": file_pattern, "owner": owner},
            )
        raise ValueError(f"Unknown definition search mode: {mode!r}")

    def search_sources_by_uri_substring(
        self,
        project_id: str,
        substring: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self._store.query(
            "SELECT uri FROM sources WHERE project_id=:project AND is_current=true "
            f"AND uri.toLowerCase() LIKE :part LIMIT {int(limit)}",
            {"project": project_id, "part": f"%{substring.casefold()}%"},
        )

    # --- Streaming Export & Counts ---

    def iter_collection(
        self,
        reader: Any,
        collection: str,
        *,
        project_id: str | None = None,
        batch_size: int = 1000,
    ) -> Iterator[list[dict[str, Any]]]:
        target = reader if reader is not None else self._store
        batch_size = max(1, int(batch_size))
        after = None
        while True:
            clauses = []
            params: dict[str, Any] = {}
            if project_id is not None:
                field = "id" if collection == "projects" else "project_id"
                clauses.append(f"{quote_name(field)}=:project")
                params["project"] = project_id
            if after is not None:
                clauses.append("id>:cursor_id")
                params["cursor_id"] = after
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = [
                clean_row(row)
                for row in target.query(
                    f"SELECT FROM {quote_name(collection)}{where} ORDER BY id LIMIT {batch_size}",
                    params,
                )
            ]
            if not rows:
                break
            yield rows
            if len(rows) < batch_size:
                break
            after = rows[-1].get("id")
            if not isinstance(after, str):
                break

    def collection_counts(self, reader: Any = None) -> dict[str, int]:
        target = reader if reader is not None else self._store
        counts: dict[str, int] = {}
        existing = self.list_collections(target)
        for collection in ALL_COLLECTIONS:
            if collection not in existing:
                counts[collection] = 0
                continue
            rows = target.query(f"SELECT count(*) AS total FROM {quote_name(collection)}")
            counts[collection] = int(rows[0]["total"]) if rows else 0
        return counts

    # --- Schema Init, Migration & Sizing Primitives ---

    def init_schema(self, store: Any = None) -> None:
        """Create or upgrade ArcadeDB types, properties, and indexes under writer lock."""
        from uuid import uuid4

        from cairnmind.config import BACKUP_SUFFIXES
        from cairnmind.storage.sql import insert_document, update_document
        from cairnmind.storage.store_backup import streamed_export
        from cairnmind.store import (
            DOCUMENT_TYPES,
            EXPORT_TYPES,
            LEGACY_EXPORT_TYPES,
            LOOKUP_INDEX_SET,
            SCHEMA_VERSION,
            V03_DOCUMENT_TYPES,
            V03_EXPORT_TYPES,
            V04_DOCUMENT_TYPES,
            V04_EXPORT_TYPES,
            V05_DOCUMENT_TYPES,
        )

        owner = store if store is not None else self._store
        client = owner.client
        client.command("CREATE DOCUMENT TYPE devbrain_meta IF NOT EXISTS")
        meta = {
            row.get("name"): row.get("value")
            for row in client.query(
                "SELECT FROM devbrain_meta WHERE name IN ['schema_version','lookup_indexes']"
            )
        }
        version, indexed = meta.get("schema_version"), meta.get("lookup_indexes")
        if version is not None:
            if version not in {1, 2, 3, SCHEMA_VERSION}:
                raise ValueError("Database schema version is incompatible")
            if version == SCHEMA_VERSION:
                if indexed != LOOKUP_INDEX_SET:
                    self._lookup_indexes(owner, indexed)
                return
            destination = (
                owner.data_dir
                / "backups"
                / f"before-schema-{version}-to-{SCHEMA_VERSION}-{uuid4().hex}{BACKUP_SUFFIXES[0]}"
            )
            with client.transaction() as tx:
                owner._write_backup(
                    destination,
                    streamed_export(
                        tx,
                        LEGACY_EXPORT_TYPES
                        if version == 1
                        else V03_EXPORT_TYPES
                        if version == 2
                        else V04_EXPORT_TYPES,
                        version + 1,
                    ),
                )
        collections = (
            (*V03_DOCUMENT_TYPES, *V04_DOCUMENT_TYPES, *V05_DOCUMENT_TYPES)
            if version == 1
            else (*V04_DOCUMENT_TYPES, *V05_DOCUMENT_TYPES)
            if version == 2
            else V05_DOCUMENT_TYPES
            if version == 3
            else DOCUMENT_TYPES
        )
        exports = collections if version is not None else EXPORT_TYPES
        statements: list[str] = []
        run_ddl = statements.append if version is None else client.command
        for collection in collections:
            run_ddl(f"CREATE DOCUMENT TYPE {collection} IF NOT EXISTS")
        if version is None:
            run_ddl("CREATE VERTEX TYPE nodes IF NOT EXISTS")
            run_ddl("CREATE EDGE TYPE edges IF NOT EXISTS")
        for collection in exports:
            run_ddl(f"CREATE PROPERTY {collection}.id IF NOT EXISTS STRING")
            run_ddl(f"CREATE INDEX IF NOT EXISTS ON {collection}(id) UNIQUE NULL_STRATEGY SKIP")
            if collection != "projects":
                run_ddl(f"CREATE PROPERTY {collection}.project_id IF NOT EXISTS STRING")
                run_ddl(f"CREATE INDEX IF NOT EXISTS ON {collection}(project_id) NOTUNIQUE NULL_STRATEGY SKIP")
        if version is None:
            for collection, field in (
                ("projects", "name"),
                ("projects", "root"),
                ("project_roots", "path"),
                ("sources", "current_key"),
                ("records", "active_key"),
                ("jobs", "job_key"),
                ("import_cursors", "path"),
            ):
                run_ddl(f"CREATE PROPERTY {collection}.{field} IF NOT EXISTS STRING")
                run_ddl(f"CREATE INDEX IF NOT EXISTS ON {collection}({field}) UNIQUE NULL_STRATEGY SKIP")
            for collection in ("sources", "chunks", "nodes", "edges"):
                run_ddl(f"CREATE PROPERTY {collection}.is_current IF NOT EXISTS BOOLEAN")
            for collection, field in (("chunks", "text"), ("records", "content")):
                run_ddl(f"CREATE PROPERTY {collection}.{field} IF NOT EXISTS STRING")
                run_ddl(
                    f"CREATE INDEX IF NOT EXISTS ON {collection}({field}) FULL_TEXT METADATA {{similarity:'BM25'}}"
                )
        run_ddl("CREATE PROPERTY journal_events.epoch IF NOT EXISTS STRING")
        run_ddl("CREATE PROPERTY journal_events.seq IF NOT EXISTS LONG")
        run_ddl("CREATE INDEX IF NOT EXISTS ON journal_events(epoch,seq) UNIQUE")
        if statements:
            client.script(";\n".join(statements))
        with client.transaction() as tx:
            if not tx.query("SELECT id FROM journal_state WHERE id='current'"):
                insert_document(
                    tx, "journal_state", {"id": "current", "epoch": str(uuid4()), "seq": 0, "first_seq": 1}
                )
            if not tx.query("SELECT name FROM devbrain_meta WHERE name='schema_version'"):
                insert_document(tx, "devbrain_meta", {"name": "schema_version", "value": SCHEMA_VERSION})
            else:
                update_document(tx, "devbrain_meta", {"value": SCHEMA_VERSION}, "name='schema_version'", {})
        if indexed != LOOKUP_INDEX_SET:
            self._lookup_indexes(owner, indexed)

    def _lookup_indexes(self, store: Any, recorded: Any) -> None:
        from cairnmind.storage.sql import insert_document, update_document
        from cairnmind.store import LOOKUP_INDEX_SET, LOOKUP_INDEXES

        client = store.client
        try:
            for collection, field in LOOKUP_INDEXES:
                client.command(f"CREATE PROPERTY {collection}.{field} IF NOT EXISTS STRING")
                client.command(
                    f"CREATE INDEX IF NOT EXISTS ON {collection}({field}) NOTUNIQUE NULL_STRATEGY SKIP"
                )
        except ArcadeError:
            return
        with client.transaction() as tx:
            if not tx.query("SELECT name FROM devbrain_meta WHERE name='lookup_indexes'"):
                insert_document(tx, "devbrain_meta", {"name": "lookup_indexes", "value": LOOKUP_INDEX_SET})
            else:
                update_document(tx, "devbrain_meta", {"value": LOOKUP_INDEX_SET}, "name='lookup_indexes'", {})

    def ensure_restored_vector_types(self, vector_indexes: list[dict[str, Any]]) -> None:
        for registry in vector_indexes:
            name, dimension = registry.get("type_name"), registry.get("dimension")
            if not isinstance(name, str) or not name.startswith("vector_") or type(dimension) is not int or dimension <= 0:
                raise ValueError("Invalid vector namespace in backup")
            self._store.command(f"CREATE DOCUMENT TYPE {quote_name(name)} IF NOT EXISTS")
            self._store.command(f"CREATE PROPERTY {quote_name(name)}.id IF NOT EXISTS STRING")
            self._store.command(f"CREATE INDEX IF NOT EXISTS ON {quote_name(name)}(id) UNIQUE")
            self._store.command(f"CREATE PROPERTY {quote_name(name)}.vector IF NOT EXISTS ARRAY_OF_FLOATS")
            self._store.command(
                f"CREATE INDEX IF NOT EXISTS ON {quote_name(name)}(vector) LSM_VECTOR "
                f"METADATA {{dimensions:{dimension},similarity:'COSINE',idPropertyName:'id'}}"
            )

    def wipe_all_collections(self, collections: tuple[str, ...]) -> None:
        wipe_order = list(reversed(collections))
        existing_types = self.list_collections(self._client)
        with self._client.transaction() as tx:
            for tname in sorted(existing_types):
                if tname.startswith("vector_"):
                    tx.command(f"DELETE FROM {quote_name(tname)}")
            for col in wipe_order:
                if col in existing_types:
                    tx.command(f"DELETE FROM {quote_name(col)}")

    def import_migration_batch(self, collection: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        ids = [r["id"] for r in rows if isinstance(r.get("id"), str)]
        with self._client.transaction() as tx:
            if collection == "journal_state":
                tx.command("DELETE FROM journal_state")
                existing_ids: set[str] = set()
            elif ids:
                found = tx.query(f"SELECT id FROM {quote_name(collection)} WHERE id IN :ids", {"ids": ids})
                existing_ids = {str(r["id"]) for r in found if r.get("id") is not None}
            else:
                existing_ids = set()

            to_insert = [clean_row(r) for r in rows if r.get("id") not in existing_ids]
            if to_insert:
                if collection == "nodes":
                    for offset in range(0, len(to_insert), 250):
                        tx.command("CREATE VERTEX nodes CONTENT :rows", {"rows": to_insert[offset:offset + 250]})
                elif collection == "edges":
                    for edge in to_insert:
                        tx.command(
                            "CREATE EDGE edges FROM (SELECT FROM nodes WHERE id=:from_id) "
                            f"TO (SELECT FROM nodes WHERE id=:to_id) CONTENT {json_text(edge)}",
                            {"from_id": edge["from_id"], "to_id": edge["to_id"]},
                        )
                else:
                    for offset in range(0, len(to_insert), 250):
                        tx.command(
                            f"INSERT INTO {quote_name(collection)} CONTENT :rows",
                            {"rows": to_insert[offset:offset + 250]},
                        )

        if collection == "vector_indexes":
            for row in rows:
                pid = row.get("project_id")
                ns = row.get("namespace")
                dim = row.get("dimension")
                model = row.get("model") or ""
                if pid and ns and isinstance(dim, int) and dim > 0:
                    self.ensure_vector_index(pid, ns, dim, model)
        elif collection == "embeddings":
            existing_types = self.list_collections(self._client)
            with self._client.transaction() as tx:
                for row in rows:
                    vec = row.get("vector")
                    pid = row.get("project_id")
                    ns = row.get("namespace")
                    dim = row.get("dimension")
                    if isinstance(vec, list) and vec and pid and ns and isinstance(dim, int):
                        tname = vector_type_name(pid, ns, dim)
                        if tname in existing_types:
                            found = tx.query(f"SELECT id FROM {quote_name(tname)} WHERE id=:id", {"id": row["id"]})
                            if not found:
                                tx.command(
                                    f"INSERT INTO {quote_name(tname)} "
                                    f"CONTENT {json_text({**clean_row(row), 'is_current': True})}"
                                )

    def upsert_meta_row(self, row: dict[str, Any]) -> None:
        cleaned = clean_row(row)
        name = cleaned["name"]
        with self._client.transaction() as tx:
            tx.command("DELETE FROM devbrain_meta WHERE name=:name", {"name": name})
            tx.command(f"INSERT INTO devbrain_meta CONTENT {json_text(cleaned)}")

    def measure_collection_exact_bytes(
        self,
        collection: str,
        *,
        project_id: str | None = None,
        project_key: str = "project_id",
    ) -> int:
        where = f" WHERE {quote_name(project_key)}=:project" if project_id else ""
        params = {"project": project_id} if project_id else {}
        found = self._store.query(f"SELECT sum(@this.toJSON().length()) AS b FROM {quote_name(collection)}{where}", params)
        return int(found[0].get("b") or 0) if found else 0

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
        import random
        where = f" WHERE {quote_name(project_key)}=:project" if project_id else ""
        params = {"project": project_id} if project_id else {}
        generator, seen, sample = random.Random(seed), set(), []  # noqa: S311 - deterministic non-crypto sampling
        part = rows / sample_points
        for point in range(sample_points):
            offset = int(point * part + generator.random() * max(0.0, part - sample_window))
            for row in self._store.query(
                f"SELECT @rid AS rid, {fields}@this.toJSON().length() AS b "
                f"FROM {quote_name(collection)}{where} SKIP {offset} LIMIT {sample_window}",
                params,
            ):
                if row.get("rid") not in seen:
                    seen.add(row.get("rid"))
                    sample.append(row)
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
        where = f" WHERE {quote_name(project_key)}=:project" if project_id else ""
        params = {"project": project_id} if project_id else {}
        grp = ", ".join(quote_name(f) for f in group_fields)
        select = f"{grp}, count(*) AS n" + (", sum(@this.toJSON().length()) AS b" if include_bytes else "")
        return list(
            self._store.query(
                f"SELECT {select} FROM {quote_name(collection)}{where} GROUP BY {grp}",
                params,
            )
        )

    def measure_package_table(
        self,
        table: str,
        project_id: str,
        *,
        only: Any = None,
        group_sources: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Measure row count, exact UTF-8 bytes (via width regexes), and max chars for one package table."""
        from cairnmind.storage.storage_query import compile_arcade_where

        length_expr = "@this.toJSON().length()"
        beyond_ascii = "\\u0080-\\uD7FF\\uE000-\\uFFFF\\ud800\\udc00-\\udbff\\udfff"
        widths = (
            beyond_ascii,
            "\\u0800-\\uD7FF\\uE000-\\uFFFF\\ud800\\udc00-\\udbff\\udfff",
            "\\ud800\\udc00-\\udbff\\udfff",
        )
        digits = "0123456789abcdef"

        base_where = "project_id=:project"
        base_params: dict[str, Any] = {"project": project_id}
        if only is not None:
            if isinstance(only, tuple) and len(only) == 2 and isinstance(only[0], str):
                base_where += f" AND {only[0]}"
                base_params.update(only[1])
            else:
                w_sql, w_params = compile_arcade_where(only, param_prefix="only_")
                if w_sql:
                    base_where += f" AND {w_sql}"
                    base_params.update(w_params)

        def run_q(select_expr: str, extra_where: str = "", extra_params: dict[str, Any] | None = None, tail: str = ""):
            cond = " AND ".join(p for p in (extra_where, base_where) if p)
            return self._store.query(
                f"SELECT {select_expr} FROM {quote_name(table)} WHERE {cond}{tail}",
                {**base_params, **(extra_params or {})},
            )

        def kept(pattern: str) -> str:
            return f"@this.toJSON().normalize('NFC','[^{pattern}]').length()"

        def parts(low, high, prefix):
            edges = [low] + [prefix + d for d in digits[1:]] + [high]
            return list(zip(edges, edges[1:], strict=False))

        def in_range(low, high):
            w, p = [], {}
            if low is not None:
                w.append("id >= :low")
                p["low"] = low
            if high is not None:
                w.append("id < :high")
                p["high"] = high
            return " AND ".join(w), p

        group = " GROUP BY kind, is_current" if group_sources else ""
        label = "kind, is_current, " if group_sources else ""
        found = run_q(f"{label}count(*) AS n, sum({length_expr}) AS b, max({length_expr}) AS m", tail=group)

        def sum_of(pattern: str):
            expr = f"{label}sum({kept(pattern)}) AS w"

            def run_part(low=None, high=None):
                ew, ep = in_range(low, high)
                return run_q(expr, extra_where=ew, extra_params=ep, tail=group)

            def add(rows_in, into):
                for r in rows_in:
                    k = (r.get("kind"), r.get("is_current"))
                    into[k] = into.get(k, 0) + int(r.get("w") or 0)

            total: dict[Any, int] = {}
            try:
                add(run_part(), total)
                return total
            except RuntimeError:
                total = {}
            for digit, (first_low, first_high) in zip(digits, parts(None, None, ""), strict=True):
                try:
                    add(run_part(first_low, first_high), total)
                    continue
                except RuntimeError:
                    pass
                for low, high in parts(first_low, first_high, digit):
                    try:
                        add(run_part(low, high), total)
                    except RuntimeError:
                        return None
            return total

        exact = True
        extra: dict[Any, int] = {}
        for pattern in widths:
            wmap = sum_of(pattern)
            if wmap is None:
                exact, extra = False, {}
                break
            for k, val in wmap.items():
                extra[k] = extra.get(k, 0) + val
            if not any(wmap.values()):
                break

        result_rows: list[dict[str, Any]] = []
        for r in found:
            key = (r.get("kind"), r.get("is_current"))
            result_rows.append({
                "kind": r.get("kind"),
                "is_current": r.get("is_current"),
                "n": int(r.get("n") or 0),
                "b": int(r.get("b") or 0) + extra.get(key, 0),
                "m": int(r.get("m") or 0),
            })
        return result_rows, exact

    def measure_package_max_bytes(
        self,
        table: str,
        project_id: str,
        *,
        floor_chars: int,
        only: Any = None,
    ) -> int | None:
        from cairnmind.storage.storage_query import compile_arcade_where

        length_expr = "@this.toJSON().length()"
        beyond_ascii = "\\u0080-\\uD7FF\\uE000-\\uFFFF\\ud800\\udc00-\\udbff\\udfff"
        widths = (
            beyond_ascii,
            "\\u0800-\\uD7FF\\uE000-\\uFFFF\\ud800\\udc00-\\udbff\\udfff",
            "\\ud800\\udc00-\\udbff\\udfff",
        )
        bytes_expr = "(" + length_expr + "".join(
            f" + @this.toJSON().normalize('NFC','[^{pattern}]').length()" for pattern in widths
        ) + ")"
        conds = [f"{length_expr} > :floor", "project_id=:project"]
        params: dict[str, Any] = {"floor": floor_chars, "project": project_id}
        if only is not None:
            if isinstance(only, tuple) and len(only) == 2 and isinstance(only[0], str):
                conds.append(only[0])
                params.update(only[1])
            else:
                w_sql, w_params = compile_arcade_where(only, param_prefix="only_")
                if w_sql:
                    conds.append(w_sql)
                    params.update(w_params)
        found = self._store.query(
            f"SELECT max({bytes_expr}) AS m FROM {quote_name(table)} WHERE {' AND '.join(conds)}",
            params,
        )
        if found and found[0].get("m") is not None:
            return int(found[0]["m"])
        return None
