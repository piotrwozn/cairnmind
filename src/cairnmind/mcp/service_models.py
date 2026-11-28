"""Pydantic request body models for the local Cairnmind HTTP service."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AskBody(BaseModel):
    project: str
    question: str = Field(min_length=1, max_length=16000)
    worktree: str | None = None
    session_id: str | None = None
    answer_schema: dict | None = None
    # Keep this answer in the project's answer log (#15); None follows the answer_log setting.
    log: bool | None = None
    # Classes a hosted built-in run must not receive (#183); they can only shrink what the answer reads.
    held_classes: list[Literal["project", "conversation", "third_party"]] | None = None
    verifier: Literal["llm", "nli"] = "llm"


class RememberBody(BaseModel):
    project: str
    record: dict
    sources: list[str] = Field(default_factory=list)
    session_id: str | None = None
    packet_id: str | None = None
    # Only the MCP bridge sends it (#325): the answer then names the metadata keys this write removed. The
    # command line and the built-in agent send nothing and get the stored record exactly as before.
    name_dropped: bool = Field(default=False, strict=True)


class FindingBody(BaseModel):
    project: str
    content: str
    kind: str
    key: str
    status: str = "proposed"
    worktree: str | None = None
    session_id: str | None = None
    packet_id: str | None = None


class CheckpointBody(BaseModel):
    project: str
    state: dict
    session_id: str | None = None
    packet_id: str | None = None
    name_dropped: bool = Field(default=False, strict=True)  # as in RememberBody (#325)


class RegisterProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    root: str = Field(min_length=1, max_length=4096)
    fresh: bool = False  # a separate identity: a removed project of that name and folder is not brought back
    no_scip: bool = False  # skip automatic background SCIP enqueue for this registration


class PlanSaveBody(BaseModel):
    project: str
    plan: dict
    steps: list[dict]
    idempotency_key: str = Field(min_length=1, max_length=256)
    expected_version: int | None = None
    task_versions: dict[str, int] | None = None
    worktree: str | None = None
    activate: bool = False
    session_id: str | None = None
    packet_id: str | None = None


class PlanNextBody(BaseModel):
    project: str
    plan_id: str | None = None
    worktree: str | None = None
    session_id: str | None = None


class PlanActivateBody(BaseModel):
    project: str
    plan_id: str
    worktree: str | None = None
    session_id: str | None = None
    packet_id: str | None = None


class SessionPrepareBody(BaseModel):
    project: str
    client: str
    worktree: str | None = None
    session_id: str | None = None
    reason: str = "startup"
    # Classes whose record previews a hosted built-in session's packet leaves out (#183).
    held_classes: list[Literal["project", "conversation", "third_party"]] | None = None


class SessionContextBody(BaseModel):
    session_id: str
    packet_id: str | None = None
    # Compatibility with startup hooks from 0.3.4 and earlier. Both spellings
    # request an exact read; they must never silently initiate a refresh.
    expected_packet_id: str | None = None
    # The short answer of #325. Only the MCP bridge sends it; the command line, the built-in agent and the
    # start hook send nothing and get the packet as before.
    short: bool = Field(default=False, strict=True)


class SessionAckBody(BaseModel):
    session_id: str
    packet_id: str
    packet_hash: str
    readback: dict


class AgentArchiveBody(BaseModel):
    model_config = {"extra": "forbid"}
    session_id: str = Field(min_length=1, max_length=200)


class JobRetryBody(BaseModel):
    project: str
    expected_attempts: int = Field(ge=0, strict=True)
    include_omissions: bool = Field(default=False, strict=True)
    session_id: str | None = None
    packet_id: str | None = None


class ProjectBody(BaseModel):
    project: str


class ImportBody(BaseModel):
    path: str
    project: str | None = None
    client: str = "auto"


class MemoryScopeBody(BaseModel):
    project: str
    worktree: str | None = None
    session_id: str | None = None


class MemorySnapshotBody(MemoryScopeBody):
    packet_id: str | None = None


class MemoryPreviewBody(MemoryScopeBody):
    profile: str = Field(default="team")
    include_transcripts: bool = Field(default=False, strict=True)
    skip_uncommitted: bool = Field(default=False, strict=True)
    redact: bool = Field(default=False, strict=True)


class MemorySnapshotExportBody(MemorySnapshotBody):
    profile: str = Field(default="team")
    include_transcripts: bool = Field(default=False, strict=True)
    skip_uncommitted: bool = Field(default=False, strict=True)
    redact: bool = Field(default=False, strict=True)


class BackgroundInferenceBody(MemorySnapshotBody):
    model_config = {"extra": "forbid"}


class MemoryInspectBody(BaseModel):
    path: str = Field(min_length=1)
    session_id: str | None = None


class MemoryImportBody(MemoryInspectBody):
    root: str = Field(min_length=1)
    name: str | None = None
    dry_run: bool = Field(default=False, strict=True)
    replace_confirmed: bool = Field(default=False, strict=True)
    expected_replaced: list[str] | None = Field(default=None, max_length=100_000)  # the list a person was shown (#333)
    heal_quotes: bool = Field(default=True, strict=True)
    packet_id: str | None = None


class MemoryHealBody(BaseModel):
    model_config = {"extra": "forbid"}
    project: str = Field(min_length=1)
    worktree: str | None = Field(default=None, max_length=4096)
    target_commit: str = Field(default="HEAD", min_length=1, max_length=256)
    dry_run: bool = Field(default=False, strict=True)
    expected_versions: dict[str, int] | None = None
    expected_snapshot_id: str | None = Field(default=None, min_length=64, max_length=64)
    record_ids: list[str] | None = Field(default=None, max_length=1000)
    session_id: str | None = None
    packet_id: str | None = None


class MemoryPublishBody(MemorySnapshotBody):
    model_config = {"extra": "forbid"}
    preview: bool = Field(default=False, strict=True)
    new_package: bool = Field(default=False, strict=True)


class MemoryImportCommitBody(BaseModel):
    model_config = {"extra": "forbid"}
    revision: str = Field(min_length=1, max_length=256)
    project: str = Field(min_length=1)
    worktree: str | None = Field(default=None, max_length=4096)
    dry_run: bool = Field(default=False, strict=True)
    bind: bool = Field(default=False, strict=True)
    replace_confirmed: bool = Field(default=False, strict=True)
    expected_replaced: list[str] | None = Field(default=None, max_length=100_000)  # the list a person was shown (#304)
    session_id: str | None = None
    packet_id: str | None = None


class MemoryImportPublishedBody(BaseModel):
    model_config = {"extra": "forbid"}
    path: str = Field(min_length=1, max_length=4096)
    project: str = Field(min_length=1)
    dry_run: bool = Field(default=False, strict=True)
    bind: bool = Field(default=False, strict=True)
    take_published: list[str] = Field(default_factory=list, max_length=1000)
    replace_confirmed: bool = Field(default=False, strict=True)
    expected_replaced: list[str] | None = Field(default=None, max_length=100_000)  # the list a person was shown (#304)
    session_id: str | None = None
    packet_id: str | None = None


class MemoryCarrierBody(BaseModel):
    model_config = {"extra": "forbid"}
    project: str = Field(min_length=1)
    kind: str = Field(min_length=1, max_length=32)
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    worktree: str | None = None
    session_id: str | None = None
    packet_id: str | None = None


class MemorySettingsBody(MemorySnapshotBody):
    enabled: bool = Field(strict=True)
    interval_minutes: int = Field(default=15, ge=1, le=10080, strict=True)


__all__ = [
    "AgentArchiveBody",
    "AskBody",
    "BackgroundInferenceBody",
    "CheckpointBody",
    "FindingBody",
    "ImportBody",
    "JobRetryBody",
    "MemoryCarrierBody",
    "MemoryHealBody",
    "MemoryImportBody",
    "MemoryImportCommitBody",
    "MemoryImportPublishedBody",
    "MemoryInspectBody",
    "MemoryPreviewBody",
    "MemoryPublishBody",
    "MemoryScopeBody",
    "MemorySettingsBody",
    "MemorySnapshotBody",
    "MemorySnapshotExportBody",
    "PlanActivateBody",
    "PlanNextBody",
    "PlanSaveBody",
    "ProjectBody",
    "RegisterProjectBody",
    "RememberBody",
    "SessionAckBody",
    "SessionContextBody",
    "SessionPrepareBody",
]
