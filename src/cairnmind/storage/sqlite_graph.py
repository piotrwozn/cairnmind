"""SQLite FTS5 search, graph operations, bounded recursive BFS, and symbol/URI search mixin."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from cairnmind.storage.sqlite_schema import quote_fts5_query


class _SQLiteGraphMixin:
    """Mixin implementing FTS5, graph mutations/BFS, and symbol/URI queries for SQLiteBackend."""

    if TYPE_CHECKING:
        _fts_latencies_ms: deque[float]
        _graph_latencies_ms: deque[float]

        def _conn(self) -> sqlite3.Connection: ...

        def insert_document(self, tx: Any, collection: str, data: dict[str, Any]) -> dict[str, Any]: ...

        def update_where(
            self,
            tx: Any,
            collection: str,
            *,
            set_fields: dict[str, Any] | None = None,
            remove_fields: Any = (),
            where: Any = (),
        ) -> list[dict[str, Any]]: ...

        def delete_where(
            self,
            tx: Any,
            collection: str,
            where: Any = (),
            *,
            limit: int | None = None,
        ) -> list[dict[str, Any]]: ...

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
