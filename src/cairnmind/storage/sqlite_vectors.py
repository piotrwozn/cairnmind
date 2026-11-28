"""SQLite vector indexing & KNN search with native ``sqlite-vec`` (``vec0``) and exact cosine fallback.

When the optional ``cairnmind[vec]`` extra (``sqlite-vec``) is installed and loaded on the
connection, each vector namespace/dimension pair gets a synchronized ``vec0`` virtual table:

    CREATE VIRTUAL TABLE IF NOT EXISTS "vec0_<hash24>" USING vec0(
        embedding_id TEXT PRIMARY KEY,
        vector float[<dim>] distance_metric=cosine,
        source_id TEXT,
        +chunk_id TEXT
    )

All writes, updates, deletions, migrations, and wipes stay synchronized with the canonical
``embeddings`` table. If ``sqlite-vec`` is not installed, or if a query hits a ``vec0`` edge
case (zero-norm query vector, ``limit > 4096``, or a read-only database populated without
``sqlite-vec``), search transparently falls back to exact C-level ``math.sumprod`` cosine
ranking over ``embeddings.vector_blob`` with identical result ordering.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import struct
from array import array
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from cairnmind.storage.domain_types import EmbeddingRecord, VectorNeighbor

log = logging.getLogger(__name__)

# Verified against sqlite-vec 0.1.9: k > 4096 raises OperationalError.
VEC0_MAX_K = 4096
VEC0_MAX_DIM = 8192
_VEC0_TABLE_RX = re.compile(r"^vec0_[0-9a-f]{24}$")
_VECTOR_TYPE_RX = re.compile(r"^vector_[0-9a-f]{24}$")


def pack_vector(vec: list[float] | tuple[float, ...]) -> bytes:
    """Pack a float vector into canonical little-endian float32 bytes."""
    return struct.pack(f"<{len(vec)}f", *(float(v) for v in vec))


def unpack_vector(blob: bytes) -> array:
    """Unpack little-endian float32 bytes into an ``array('f')``."""
    arr = array("f")
    arr.frombytes(blob)
    return arr


def cosine_distance(left: list[float] | tuple[float, ...] | array, right: list[float] | tuple[float, ...] | array) -> float:
    """Exact cosine distance in ``[0.0, 2.0]`` using C-level ``math.sumprod``."""
    if len(left) != len(right):
        return 2.0
    if not left:
        return 1.0
    na = math.sqrt(math.sumprod(left, left))
    nb = math.sqrt(math.sumprod(right, right))
    if na <= 0.0 or nb <= 0.0:
        return 1.0
    sim = math.sumprod(left, right) / (na * nb)
    sim = max(-1.0, min(1.0, sim))
    return 1.0 - sim


def vector_type_name(project_id: str, namespace: str, dimension: int) -> str:
    """Return the canonical ``vector_<24-hex>`` identifier for a namespace/dimension."""
    identity = json.dumps([project_id, namespace, int(dimension)], separators=(",", ":"))
    return "vector_" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def vec0_table_name(type_name: str) -> str:
    """Derive a validated ``vec0_<24-hex>`` virtual table name from ``vector_<24-hex>``."""
    if not isinstance(type_name, str) or not _VECTOR_TYPE_RX.fullmatch(type_name):
        raise ValueError(f"Invalid vector type name: {type_name!r}")
    table = "vec0_" + type_name.removeprefix("vector_")
    if not _VEC0_TABLE_RX.fullmatch(table):
        raise ValueError(f"Invalid vec0 table name: {table!r}")
    return table


def vec0_table_exists(conn: sqlite3.Connection, vec_table: str) -> bool:
    """Return True if the ``vec0`` virtual table exists in ``sqlite_master``."""
    if not _VEC0_TABLE_RX.fullmatch(vec_table):
        return False
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (vec_table,),
    ).fetchone()
    return row is not None


def ensure_vec0_table(conn: sqlite3.Connection, type_name: str, dimension: int) -> str:
    """Create the ``vec0`` virtual table for ``(type_name, dimension)`` if absent."""
    dim = int(dimension)
    if not 1 <= dim <= VEC0_MAX_DIM:
        raise ValueError("Unsupported embedding dimension")
    vec_table = vec0_table_name(type_name)
    conn.execute(
        f'CREATE VIRTUAL TABLE IF NOT EXISTS "{vec_table}" USING vec0('
        f"embedding_id TEXT PRIMARY KEY, "
        f"vector float[{dim}] distance_metric=cosine, "
        f"source_id TEXT, "
        f"+chunk_id TEXT)"
    )
    return vec_table


def _extract_embedding_blob(blob: bytes | None, doc_text: str | None, dimension: int) -> bytes | None:
    if blob is not None and len(blob) == dimension * 4:
        return bytes(blob)
    if doc_text:
        doc = json.loads(doc_text)
        vec = doc.get("vector")
        if isinstance(vec, list) and len(vec) == dimension:
            return pack_vector(vec)
    return None


def backfill_vec0_table(
    conn: sqlite3.Connection,
    project_id: str,
    namespace: str,
    dimension: int,
    type_name: str,
    *,
    force: bool = False,
) -> tuple[str, int]:
    """Ensure ``vec0`` exists and holds every canonical row from ``embeddings``."""
    dim = int(dimension)
    vec_table = ensure_vec0_table(conn, type_name, dim)
    emb_count = int(
        conn.execute(
            "SELECT count(*) FROM embeddings "
            "WHERE project_id=? AND namespace=? AND dimension=? AND vector_blob IS NOT NULL",
            (project_id, namespace, dim),
        ).fetchone()[0]
    )
    vec_count = int(conn.execute(f'SELECT count(*) FROM "{vec_table}"').fetchone()[0])
    if not force and vec_count == emb_count:
        return vec_table, 0

    conn.execute(f'DELETE FROM "{vec_table}"')
    rows = conn.execute(
        "SELECT id, source_id, chunk_id, vector_blob, doc FROM embeddings "
        "WHERE project_id=? AND namespace=? AND dimension=?",
        (project_id, namespace, dim),
    ).fetchall()
    inserted = 0
    for row in rows:
        blob = _extract_embedding_blob(row[3], row[4], dim)
        if blob is None:
            continue
        conn.execute(
            f'INSERT INTO "{vec_table}" (embedding_id, vector, source_id, chunk_id) VALUES (?, ?, ?, ?)',
            (str(row[0]), blob, str(row[1] or ""), str(row[2] or "")),
        )
        inserted += 1
    return vec_table, inserted


def sync_vec_upsert(
    conn: sqlite3.Connection,
    row: EmbeddingRecord | Mapping[str, Any],
    *,
    sqlite_vec_available: bool,
    read_only: bool,
) -> None:
    """Synchronize an inserted or updated ``embeddings`` row into its ``vec0`` table."""
    if not sqlite_vec_available or read_only:
        return
    emb_id = row.get("id")
    project_id = row.get("project_id")
    namespace = row.get("namespace")
    dimension = row.get("dimension")
    if not (isinstance(emb_id, str) and isinstance(project_id, str) and isinstance(namespace, str)):
        return
    if not isinstance(dimension, int) or isinstance(dimension, bool) or not 1 <= dimension <= VEC0_MAX_DIM:
        return
    vec = row.get("vector")
    type_name = vector_type_name(project_id, namespace, dimension)
    vec_table = vec0_table_name(type_name)
    if not isinstance(vec, list) or len(vec) != dimension:
        if vec0_table_exists(conn, vec_table):
            conn.execute(f'DELETE FROM "{vec_table}" WHERE embedding_id = ?', (emb_id,))
        return
    ensure_vec0_table(conn, type_name, dimension)
    conn.execute(f'DELETE FROM "{vec_table}" WHERE embedding_id = ?', (emb_id,))
    conn.execute(
        f'INSERT INTO "{vec_table}" (embedding_id, vector, source_id, chunk_id) VALUES (?, ?, ?, ?)',
        (emb_id, pack_vector(vec), str(row.get("source_id") or ""), str(row.get("chunk_id") or "")),
    )


def sync_vec_delete_rows(
    conn: sqlite3.Connection,
    deleted_rows: list[tuple[str, str | None, str | None, int | None]],
    *,
    sqlite_vec_available: bool,
    read_only: bool,
) -> None:
    """Remove deleted ``embeddings`` rows from their corresponding ``vec0`` virtual tables."""
    if not sqlite_vec_available or read_only or not deleted_rows:
        return
    grouped: dict[tuple[str, str, int], list[str]] = defaultdict(list)
    for emb_id, project_id, namespace, dimension in deleted_rows:
        if not (isinstance(emb_id, str) and isinstance(project_id, str) and isinstance(namespace, str)):
            continue
        if not isinstance(dimension, int) or isinstance(dimension, bool) or not 1 <= dimension <= VEC0_MAX_DIM:
            continue
        grouped[(project_id, namespace, dimension)].append(emb_id)
    for (project_id, namespace, dimension), emb_ids in grouped.items():
        vec_table = vec0_table_name(vector_type_name(project_id, namespace, dimension))
        if not vec0_table_exists(conn, vec_table):
            continue
        conn.executemany(
            f'DELETE FROM "{vec_table}" WHERE embedding_id = ?',
            [(eid,) for eid in emb_ids],
        )


def drop_all_vec0_tables(conn: sqlite3.Connection) -> None:
    """Drop all ``vec0_*`` virtual tables (SQLite automatically drops their shadow tables)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name LIKE 'vec0_%' AND sql LIKE 'CREATE VIRTUAL TABLE%USING vec0%'"
    ).fetchall()
    for row in rows:
        name = str(row[0])
        if _VEC0_TABLE_RX.fullmatch(name):
            conn.execute(f'DROP TABLE IF EXISTS "{name}"')


def _exact_cosine_scan(
    conn: sqlite3.Connection,
    project_id: str,
    namespace: str,
    dim: int,
    query_vector: list[float],
    eligible_sources: list[str],
    limit: int,
) -> list[VectorNeighbor]:
    from cairnmind.storage.storage_query import sqlite_id_filter_sql

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
    return [{"chunk_id": cid, "distance": dist} for dist, cid in scored[: int(limit)]]


def _vec0_is_synchronized(
    conn: sqlite3.Connection,
    project_id: str,
    namespace: str,
    dim: int,
    vec_table: str,
) -> bool:
    if not vec0_table_exists(conn, vec_table):
        return False
    emb_count = int(
        conn.execute(
            "SELECT count(*) FROM embeddings "
            "WHERE project_id=? AND namespace=? AND dimension=? AND vector_blob IS NOT NULL",
            (project_id, namespace, dim),
        ).fetchone()[0]
    )
    vec_count = int(conn.execute(f'SELECT count(*) FROM "{vec_table}"').fetchone()[0])
    return vec_count == emb_count


def search_vectors(
    conn: sqlite3.Connection,
    project_id: str,
    namespace: str,
    query_vector: list[float],
    eligible_sources: list[str],
    limit: int,
    *,
    sqlite_vec_available: bool,
    read_only: bool,
    synced_tables: set[str],
    raw_tx_factory: Any = None,
) -> tuple[bool, list[VectorNeighbor], str | None, bool]:
    """Execute vector KNN search via ``vec0`` when available, falling back to exact cosine scan.

    Returns ``(index_present, neighbors, warning, used_vec0)``.
    """
    dim = len(query_vector)
    idx_row = conn.execute(
        "SELECT id, type_name FROM vector_indexes WHERE project_id=? AND namespace=? AND dimension=?",
        (project_id, namespace, dim),
    ).fetchone()
    if not idx_row:
        return False, [], None, False
    lim = int(limit)
    if not eligible_sources or lim <= 0:
        return True, [], None, False

    type_name = str(idx_row[1] or idx_row[0])
    q_arr = array("f", query_vector)
    q_norm = math.sqrt(math.sumprod(q_arr, q_arr))

    if sqlite_vec_available and q_norm > 0.0 and 1 <= lim <= VEC0_MAX_K and _VECTOR_TYPE_RX.fullmatch(type_name):
        vec_table = vec0_table_name(type_name)
        ready = vec_table in synced_tables and vec0_table_exists(conn, vec_table)
        if not ready:
            if _vec0_is_synchronized(conn, project_id, namespace, dim, vec_table):
                synced_tables.add(vec_table)
                ready = True
            elif not read_only:
                if raw_tx_factory is not None and not conn.in_transaction:
                    with raw_tx_factory():
                        backfill_vec0_table(conn, project_id, namespace, dim, type_name)
                else:
                    backfill_vec0_table(conn, project_id, namespace, dim, type_name)
                synced_tables.add(vec_table)
                ready = True

        if ready:
            from cairnmind.storage.storage_query import sqlite_id_filter_sql

            src_clause, src_params = sqlite_id_filter_sql(conn, "source_id", eligible_sources, slot=0)
            q_blob = pack_vector(query_vector)
            rows = conn.execute(
                f'SELECT chunk_id, distance FROM "{vec_table}" '
                f"WHERE vector MATCH ? AND k = ? AND {src_clause}",
                [q_blob, lim, *src_params],
            ).fetchall()
            scored = [
                (
                    1.0 if row[1] is None or math.isnan(float(row[1])) else float(row[1]),
                    str(row[0]),
                )
                for row in rows
            ]
            scored.sort(key=lambda item: (item[0], item[1]))
            neighbors: list[VectorNeighbor] = [{"chunk_id": cid, "distance": dist} for dist, cid in scored[:lim]]
            return True, neighbors, None, True

    neighbors = _exact_cosine_scan(conn, project_id, namespace, dim, query_vector, eligible_sources, lim)
    return True, neighbors, None, False
