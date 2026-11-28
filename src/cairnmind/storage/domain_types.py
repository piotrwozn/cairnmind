"""Typed domain contracts for Cairnmind core entities.

These ``TypedDict`` definitions replace raw ``dict[str, Any]`` across ``storage/``,
``pal/``, ``mcp/``, ``store.py``, and ``memory/`` while keeping runtime JSON documents
100% unchanged and backward-compatible with both ArcadeDB and SQLite backends.
"""
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

RecordKind = Literal[
    "decision",
    "task",
    "fact",
    "constraint",
    "question",
    "preference",
    "checkpoint",
]
RecordStatus = Literal["proposed", "confirmed", "disputed", "superseded"]
TaskState = Literal["todo", "in_progress", "needs_test", "needs_review", "done", "blocked"]
BackendKind = Literal["arcade", "sqlite"]
JobState = Literal["pending", "running", "completed", "failed", "abandoned"]


class ProjectRecord(TypedDict):
    """Canonical project row stored in the ``projects`` collection."""

    id: str
    name: str
    root: str
    created_at: NotRequired[str]
    updated_at: NotRequired[str]
    removed_at: NotRequired[str | None]
    graph_fingerprint: NotRequired[str | None]
    metadata: NotRequired[dict[str, Any]]


class ProjectRootRecord(TypedDict):
    """Registered worktree root row in ``project_roots``."""

    id: str
    project_id: str
    path: str
    created_at: NotRequired[str]


class SourceRecord(TypedDict):
    """Canonical source snapshot stored in the ``sources`` collection."""

    id: str
    project_id: str
    uri: str
    kind: str
    content: NotRequired[str]
    sha256: NotRequired[str]
    is_current: NotRequired[bool]
    current_key: NotRequired[str | None]
    created_at: NotRequired[str]
    updated_at: NotRequired[str]
    metadata: NotRequired[dict[str, Any]]


class ChunkRecord(TypedDict):
    """Indexed text chunk in the ``chunks`` collection."""

    id: str
    project_id: str
    source_id: str
    text: str
    line_start: NotRequired[int]
    line_end: NotRequired[int]
    is_current: NotRequired[bool]
    metadata: NotRequired[dict[str, Any]]
    search_score: NotRequired[float]


class MemoryRecord(TypedDict):
    """Canonical decision, fact, task, constraint, or checkpoint in ``records``."""

    id: str
    project_id: str
    kind: str
    key: str
    content: str
    status: str
    version: int
    source_ids: list[str]
    active_key: NotRequired[str | None]
    metadata: NotRequired[dict[str, Any]]
    created_at: NotRequired[str]
    updated_at: NotRequired[str]
    search_score: NotRequired[float]


class RecordRevision(TypedDict):
    """Immutable revision snapshot in ``record_revisions``."""

    id: str
    project_id: str
    record_id: str
    version: int
    snapshot: dict[str, Any]
    recorded_at: NotRequired[str]


class PlanRecord(TypedDict):
    """Structured work plan stored in the ``plans`` collection."""

    id: str
    project_id: str
    title: str
    goal: str
    status: str
    version: int
    constraints: NotRequired[list[str]]
    step_ids: NotRequired[list[str]]
    source_ids: NotRequired[list[str]]
    metadata: NotRequired[dict[str, Any]]
    created_at: NotRequired[str]
    updated_at: NotRequired[str]


class PlanRevision(TypedDict):
    """Immutable plan revision snapshot in ``plan_revisions``."""

    id: str
    project_id: str
    plan_id: str
    version: int
    reason: NotRequired[str]
    snapshot: dict[str, Any]
    recorded_at: NotRequired[str]


class TaskRecord(MemoryRecord):
    """Specialized task record stored in ``records`` with ``kind == 'task'``."""

    pass


class SessionRecord(TypedDict):
    """Tracked coding-agent session in the ``sessions`` collection."""

    id: str
    project_id: str
    client: NotRequired[str]
    worktree: NotRequired[str | None]
    status: NotRequired[str]
    active_packet_id: NotRequired[str | None]
    acknowledged_packet_id: NotRequired[str | None]
    metadata: NotRequired[dict[str, Any]]
    created_at: NotRequired[str]
    updated_at: NotRequired[str]


class ResumePacketRecord(TypedDict):
    """Prepared continuity resume packet in ``resume_packets``."""

    id: str
    project_id: str
    session_id: str
    hash: str
    schema_version: NotRequired[int]
    payload: dict[str, Any]
    created_at: NotRequired[str]


class ResumeAckRecord(TypedDict):
    """Recorded resume acknowledgment in ``resume_acks``."""

    id: str
    project_id: str
    session_id: str
    packet_id: str
    packet_hash: NotRequired[str]
    recorded_at: NotRequired[str]


class CheckRunRecord(TypedDict):
    """Recorded verification check execution in ``check_runs``."""

    id: str
    project_id: str
    task_id: NotRequired[str | None]
    profile_id: NotRequired[str]
    passed: NotRequired[bool]
    exit_code: NotRequired[int | None]
    profile_hash: NotRequired[str | None]
    task_definition_hash: NotRequired[str | None]
    code_fingerprint: NotRequired[str | None]
    started_at: NotRequired[str]
    ended_at: NotRequired[str]
    metadata: NotRequired[dict[str, Any]]


class GraphNodeRecord(TypedDict):
    """Code or knowledge vertex in the ``nodes`` collection."""

    id: str
    project_id: str
    kind: str
    label: str
    source_id: NotRequired[str | None]
    is_current: NotRequired[bool]
    metadata: NotRequired[dict[str, Any]]
    distance: NotRequired[int]


class GraphEdgeRecord(TypedDict):
    """Directed graph edge in the ``edges`` collection."""

    id: str
    project_id: str
    from_id: str
    to_id: str
    kind: str
    source_id: NotRequired[str | None]
    is_current: NotRequired[bool]
    status: NotRequired[str]
    metadata: NotRequired[dict[str, Any]]


class JobRecord(TypedDict):
    """Background worker queue entry in the ``jobs`` collection."""

    id: str
    project_id: str
    kind: str
    state: str
    payload: NotRequired[dict[str, Any]]
    job_key: NotRequired[str | None]
    attempts: NotRequired[int]
    lease_owner: NotRequired[str | None]
    lease_until: NotRequired[float | None]
    last_error: NotRequired[str | None]
    diagnostics: NotRequired[dict[str, Any]]
    created_at: NotRequired[str]
    updated_at: NotRequired[str]


class VectorIndexRecord(TypedDict):
    """Registered vector namespace/dimension descriptor in ``vector_indexes``."""

    id: str
    project_id: str
    model: str
    namespace: str
    dimension: int
    type_name: str
    index_name: str


class EmbeddingRecord(TypedDict):
    """Chunk embedding row stored in ``embeddings``."""

    id: str
    project_id: str
    source_id: str
    chunk_id: str
    namespace: str
    dimension: int
    vector: NotRequired[list[float]]
    is_current: NotRequired[bool]


class VectorNeighbor(TypedDict):
    """Single nearest-neighbor hit returned by ``StorageBackend.search_vectors``."""

    chunk_id: str
    distance: float


class JournalCursor(TypedDict):
    """Monotonic change-journal cursor returned by ``Journal.cursor``."""

    epoch: str
    seq: int
    first_seq: NotRequired[int]


class JournalEventRecord(TypedDict):
    """Single change-journal entry stored in ``journal_events``."""

    id: str
    epoch: str
    seq: int
    project_id: NotRequired[str | None]
    entity_type: str
    entity_id: NotRequired[str | None]
    action: str
    recorded_at: NotRequired[str]
    payload: NotRequired[dict[str, Any]]


class SQLiteMetricsSnapshot(TypedDict):
    """Live telemetry snapshot produced by ``SQLiteBackend.metrics_snapshot``."""

    backend: Literal["sqlite"]
    db_path: str
    db_bytes: int
    busy_retries_total: int
    busy_retries_10m: int
    fts_p95_ms: float
    graph_p95_ms: float
    sqlite_vec_loaded: bool
    vec_ann_queries: int
    vec_fallback_queries: int


__all__ = [
    "BackendKind",
    "CheckRunRecord",
    "ChunkRecord",
    "EmbeddingRecord",
    "GraphEdgeRecord",
    "GraphNodeRecord",
    "JobRecord",
    "JobState",
    "JournalCursor",
    "JournalEventRecord",
    "MemoryRecord",
    "PlanRecord",
    "PlanRevision",
    "ProjectRecord",
    "ProjectRootRecord",
    "RecordKind",
    "RecordRevision",
    "RecordStatus",
    "ResumeAckRecord",
    "ResumePacketRecord",
    "SQLiteMetricsSnapshot",
    "SessionRecord",
    "SourceRecord",
    "TaskRecord",
    "TaskState",
    "VectorIndexRecord",
    "VectorNeighbor",
]
