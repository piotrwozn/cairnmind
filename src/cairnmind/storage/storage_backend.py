"""Backend-neutral StorageBackend protocol covering all 27 Cairnmind collections (PR #6 / Faza 4a).

Every contract in docs/CONTRACTS.md, docs/MEMORY_CONTRACTS.md and docs/ARCADE_CONTRACTS.md
(optimistic versioning, read-before-write, fingerprints, origin_evidence, bounded BFS,
hybrid FTS+vector+graph retrieval, journal outbox) runs identically regardless of whether
the underlying StorageBackend is ArcadeBackend (PR #6) or SQLiteBackend (PR #7).
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Iterator, Protocol, runtime_checkable

# Canonical collections matching Store.EXPORT_TYPES (26 document types + nodes + edges).
ALL_COLLECTIONS: tuple[str, ...] = (
    # Legacy core (11)
    "projects",
    "project_roots",
    "sources",
    "chunks",
    "records",
    "record_revisions",
    "jobs",
    "embeddings",
    "import_cursors",
    "ingestion_inbox",
    "vector_indexes",
    # v0.3 workflow, sessions & verification (9)
    "plans",
    "plan_revisions",
    "plan_requests",
    "worktree_states",
    "sessions",
    "resume_packets",
    "resume_acks",
    "session_events",
    "check_runs",
    # v0.4 portable memory (4)
    "memory_settings",
    "memory_snapshots",
    "memory_imports",
    "memory_bindings",
    # v0.5 journal outbox (2)
    "journal_state",
    "journal_events",
    # Native graph (2)
    "nodes",
    "edges",
)

META_COLLECTION = "devbrain_meta"
VERDICT_CACHE_COLLECTION = "verdict_cache"


@runtime_checkable
class StorageTransaction(Protocol):
    """Transactional handle exposed inside ``StorageBackend.transaction()``."""

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def command(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def server_time_ms(self) -> int: ...

    def insert_document(self, collection: str, data: dict[str, Any]) -> dict[str, Any]: ...

    def insert_documents(self, collection: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def update_document(
        self,
        collection: str,
        data: dict[str, Any],
        where: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]: ...

    def upsert_document(
        self,
        collection: str,
        data: dict[str, Any],
        key_field: str = "id",
    ) -> dict[str, Any]: ...

    def delete_documents(
        self,
        collection: str,
        where: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    def find(
        self,
        collection: str,
        where: Any = (),
        *,
        columns: Any = None,
        order_by: Any = (),
        limit: int | None = None,
        distinct: bool = False,
    ) -> list[dict[str, Any]]: ...

    def find_one(
        self,
        collection: str,
        where: Any = (),
        *,
        columns: Any = None,
        order_by: Any = (),
        required: bool = False,
    ) -> dict[str, Any] | None: ...

    def count(self, collection: str, where: Any = ()) -> int: ...

    def group_counts(
        self,
        collection: str,
        group_by: Any,
        where: Any = (),
        *,
        count_alias: str = "total",
    ) -> list[dict[str, Any]]: ...

    def update_where(
        self,
        collection: str,
        *,
        set_fields: dict[str, Any] | None = None,
        remove_fields: Any = (),
        where: Any = (),
    ) -> list[dict[str, Any]]: ...

    def delete_where(
        self,
        collection: str,
        where: Any = (),
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class StorageBackend(Protocol):
    """Unified persistence contract implemented by ArcadeBackend and SQLiteBackend."""

    @property
    def backend_kind(self) -> str:
        """Identifier of the active backend: ``'arcade'`` or ``'sqlite'``."""
        ...

    @property
    def read_only(self) -> bool:
        """Whether this backend refuses all mutating operations."""
        ...

    def ensure_database(self) -> None: ...

    def database_exists(self) -> bool: ...

    def list_collections(self) -> set[str]: ...

    def server_time_ms(self, reader: Any = None) -> int: ...

    def close(self) -> None: ...

    def transaction(self, commit_timeout: float = 30.0) -> AbstractContextManager[Any]: ...

    # --- Low-level SQL compatibility for read-only projections & gradual DAL callers ---
    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    def command(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...

    # --- Structured Document CRUD ---
    def insert_document(self, tx: Any, collection: str, data: dict[str, Any]) -> dict[str, Any]: ...

    def insert_documents(self, tx: Any, collection: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

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

    def delete_documents(
        self,
        tx: Any,
        collection: str,
        where: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_by_id(self, reader: Any, collection: str, row_id: str) -> dict[str, Any] | None: ...

    # --- Declarative Backend-Neutral Queries & Mutations (DAL) ---
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
    ) -> list[dict[str, Any]]: ...

    def find_one(
        self,
        collection: str,
        where: Any = (),
        *,
        columns: Any = None,
        order_by: Any = (),
        required: bool = False,
        reader: Any = None,
    ) -> dict[str, Any] | None: ...

    def count(self, collection: str, where: Any = (), *, reader: Any = None) -> int: ...

    def group_counts(
        self,
        collection: str,
        group_by: Any,
        where: Any = (),
        *,
        count_alias: str = "total",
        reader: Any = None,
    ) -> list[dict[str, Any]]: ...

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

    # --- Full-Text Search ---
    def search_chunks_fts(
        self,
        project_id: str,
        words: list[str],
        eligible_sources: list[str],
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def search_records_fts(
        self,
        project_id: str,
        words: list[str],
        eligible_records: list[str],
        limit: int,
    ) -> list[dict[str, Any]]: ...

    # --- Vectors (ADR-03 parity) ---
    def ensure_vector_index(
        self,
        project_id: str,
        namespace: str,
        dimension: int,
        model: str,
    ) -> dict[str, Any] | None: ...

    def has_vector_embedding(
        self,
        project_id: str,
        namespace: str,
        chunk_id: str,
        dimension: int,
        identifier: str,
    ) -> bool: ...

    def upsert_embedding(
        self,
        tx: Any,
        values: dict[str, Any],
        index_info: dict[str, Any] | None = None,
    ) -> None: ...

    def search_vectors(
        self,
        project_id: str,
        namespace: str,
        query_vector: list[float],
        eligible_sources: list[str],
        limit: int,
    ) -> tuple[bool, list[dict[str, Any]], str | None]:
        """Search vector embeddings for ``(project_id, namespace, len(query_vector))``.

        Returns ``(index_present, neighbors, warning_or_none)`` where each neighbor
        has ``chunk_id`` and ``distance`` (cosine distance, smaller is closer).
        """
        ...

    def delete_vectors_for_sources(self, tx: Any, source_ids: list[str]) -> None: ...

    def delete_vectors_for_project(self, tx: Any, project_id: str) -> None: ...

    # --- Graph (Vertices, Edges, Bounded BFS) ---
    def upsert_vertex(self, tx: Any, node: dict[str, Any], *, existing: bool = False) -> dict[str, Any]: ...

    def upsert_edge(
        self,
        tx: Any,
        edge: dict[str, Any],
        *,
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def write_graph_batch(
        self,
        tx: Any,
        project_id: str,
        created_nodes: list[dict[str, Any]],
        updated_nodes: list[dict[str, Any]],
        created_edges: list[dict[str, Any]],
    ) -> None: ...

    def delete_graph_nodes(
        self,
        tx: Any,
        project_id: str,
        node_ids: list[str],
    ) -> list[dict[str, Any]]: ...

    def delete_graph_edges(
        self,
        tx: Any,
        project_id: str,
        edge_ids: list[str],
        *,
        kinds: Any = None,
    ) -> list[dict[str, Any]]: ...

    def search_graph_seeds(
        self,
        project_id: str,
        exact_query: str,
        seed_sources: list[str],
        words: list[str],
        eligible_nodes: list[str],
        limit: int = 7,
    ) -> list[dict[str, Any]]: ...

    def traverse_graph(
        self,
        project_id: str,
        seed_ids: list[str],
        eligible_nodes: list[str],
        eligible_edges: list[str],
        *,
        max_depth: int = 4,
        limit: int = 30,
    ) -> list[dict[str, Any]]: ...

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
    ) -> list[dict[str, Any]]: ...

    def search_sources_by_uri_substring(
        self,
        project_id: str,
        substring: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...

    # --- Streaming Export / Migration Introspection ---
    def iter_collection(
        self,
        reader: Any,
        collection: str,
        *,
        project_id: str | None = None,
        batch_size: int = 1000,
    ) -> Iterator[list[dict[str, Any]]]: ...

    def collection_counts(self, reader: Any = None) -> dict[str, int]: ...

    # --- Schema Init, Migration & Sizing Primitives ---
    def init_schema(self, store: Any = None) -> None: ...

    def ensure_restored_vector_types(self, vector_indexes: list[dict[str, Any]]) -> None: ...

    def wipe_all_collections(self, collections: tuple[str, ...]) -> None: ...

    def import_migration_batch(self, collection: str, rows: list[dict[str, Any]]) -> None: ...

    def upsert_meta_row(self, row: dict[str, Any]) -> None: ...

    def measure_collection_exact_bytes(
        self,
        collection: str,
        *,
        project_id: str | None = None,
        project_key: str = "project_id",
    ) -> int: ...

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
    ) -> list[dict[str, Any]]: ...

    def measure_collection_grouped(
        self,
        collection: str,
        group_fields: tuple[str, ...],
        *,
        project_id: str | None = None,
        project_key: str = "project_id",
        include_bytes: bool = True,
    ) -> list[dict[str, Any]]: ...

    def measure_package_table(
        self,
        table: str,
        project_id: str,
        *,
        only: Any = None,
        group_sources: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]: ...

    def measure_package_max_bytes(
        self,
        table: str,
        project_id: str,
        *,
        floor_chars: int,
        only: Any = None,
    ) -> int | None: ...
