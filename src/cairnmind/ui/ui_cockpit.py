"""Compatibility facade re-exporting cockpit schemas, helpers, and live mixin."""
from __future__ import annotations

from typing import Any

from cairnmind.ui.live_adapter import LiveCockpitMixin
from cairnmind.ui.schemas import (
    SECRET_PATTERNS,
    CloneVersionRequest,
    CreateFactRequest,
    CreatePlanRequest,
    CreateTaskRequest,
    DecisionActionRequest,
    ExportSnapshotRequest,
    OriginCategory,
    OriginEvidence,
    PlanStepInput,
    PromotePlanRequest,
    SimulateConflictRequest,
    SimulateFileEditRequest,
    UiAskRequest,
    UpdatePlanRequest,
    classify_audit_origin,
    classify_record_origin,
    compute_snapshot_diff,
    make_origin,
    safe_file_fingerprint,
    scan_and_redact_payload,
    sha256_text,
    utc_now,
)

__all__ = [
    "SECRET_PATTERNS", "CloneVersionRequest", "CreateFactRequest", "CreatePlanRequest",
    "CreateTaskRequest", "DecisionActionRequest", "ExportSnapshotRequest", "LiveCockpitMixin",
    "OriginCategory", "OriginEvidence", "PlanStepInput", "PromotePlanRequest",
    "SimulateConflictRequest", "SimulateFileEditRequest", "UiAskRequest", "UpdatePlanRequest",
    "classify_audit_origin", "classify_record_origin", "compute_snapshot_diff", "make_origin",
    "safe_file_fingerprint", "scan_and_redact_payload", "sha256_text", "utc_now",
]


def __getattr__(name: str) -> Any:
    if name == "MockCockpitMixin":
        from cairnmind.ui.mock_state import MockCockpitMixin

        return MockCockpitMixin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
