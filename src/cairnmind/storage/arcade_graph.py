"""ArcadeDB FTS, vector, native graph, and symbol/URI search mixin for ArcadeBackend."""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from cairnmind.storage.arcade import ArcadeError
from cairnmind.storage.sql import IDENTIFIER, json_text, quote_name

GRAPH_ROWS_PER_REQUEST = 250


def vector_type_name(project_id: str, namespace: str, dimension: int) -> str:
    identity = json.dumps([project_id, namespace, dimension], separators=(",", ":"))
    return "vector_" + hashlib.sha256(identity.encode()).hexdigest()[:24]


class _ArcadeGraphMixin:
    """Mixin providing FTS, vector, graph, and symbol/URI queries for ArcadeBackend."""

    if TYPE_CHECKING:
        _store: Any
        _vector_indexes: dict[str, dict[str, Any]]

        def update_document(
            self,
            tx: Any,
            collection: str,
            data: dict[str, Any],
            where: str,
            params: dict[str, Any],
        ) -> list[dict[str, Any]]: ...

        def upsert_document(
            self,
            tx: Any,
            collection: str,
            data: dict[str, Any],
            key_field: str = "id",
        ) -> dict[str, Any]: ...

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
            statements: list[str] = []
            params: dict[str, Any] = {}
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
            edge_statements: list[str] = []
            edge_params: dict[str, Any] = {"ids": [edge["id"] for edge in part]}
            for index, edge in enumerate(part):
                edge_statements.append(
                    f"CREATE EDGE edges FROM (SELECT FROM nodes WHERE id=:f{index}) "
                    f"TO (SELECT FROM nodes WHERE id=:t{index}) CONTENT :e{index}"
                )
                edge_params.update({f"f{index}": edge["from_id"], f"t{index}": edge["to_id"], f"e{index}": edge})
            edge_statements.append("SELECT count(*) AS n FROM edges WHERE id IN :ids")
            found = tx.script(edge_statements, edge_params, project_id)
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
