"""Deterministic mock fixtures and stateful preview backend for `cairnmind ui --mock`."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import threading
import time
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from cairnmind.ui.event_bus import UiEventBus
from cairnmind.ui.graph import (
    MAX_CONTEXT_LINES,
    _is_git_segment,
    _sha256_text,
    _utc_now,
)
from cairnmind.ui.schemas import (
    CloneVersionRequest,
    CreateFactRequest,
    CreatePlanRequest,
    CreateTaskRequest,
    DecisionActionRequest,
    ExportSnapshotRequest,
    PromotePlanRequest,
    SimulateConflictRequest,
    SimulateFileEditRequest,
    UiAskRequest,
    UpdatePlanRequest,
    classify_audit_origin,
    compute_snapshot_diff,
    make_origin,
    scan_and_redact_payload,
    sha256_text,
    utc_now,
)

# ---------------------------------------------------------------------------
# Deterministic Mock Dataset (`?mock=1` or `cairnmind ui --mock`)
# ---------------------------------------------------------------------------

_MOCK_FILES: dict[str, str] = {
    "src/cairnmind/sessions.py": """\"\"\"Deterministic, versioned resume packets and OCC write gates.\"\"\"
from __future__ import annotations

import hashlib
from typing import Any

PACKET_SCHEMA_VERSION = 2


class SessionManager:
    def require_ack(self, session_id: str, packet_id: str | None = None) -> None:
        \"\"\"Reject writes unless the session acknowledged the latest state_rev.\"\"\"
        session = self._session(session_id)
        current_rev = self.current_state_rev(session["project_id"])
        acknowledged_rev = session.get("acknowledged_rev", 0)
        if acknowledged_rev < current_rev:
            raise ValueError(
                f"OCC_STALE_RESUME_PACKET: session {session_id} ack_rev={acknowledged_rev} "
                f"< state_rev={current_rev}; fetch resume_context and ack_resume first."
            )
        if packet_id and session.get("active_packet_id") != packet_id:
            raise ValueError("OCC_STALE_RESUME_PACKET: packet_id no longer active.")
""",
    "src/cairnmind/evidence.py": """\"\"\"Derived verbatim quote provenance and file fingerprint checks.\"\"\"
from __future__ import annotations

import hashlib
from pathlib import Path


def verify_verbatim_quote(file_path: Path, recorded_sha256: str, verbatim_quote: str) -> dict:
    \"\"\"Compare on-disk SHA-256 and exact verbatim passage against recorded provenance.\"\"\"
    raw = file_path.read_bytes()
    disk_sha256 = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace").replace("\\r\\n", "\\n")
    quote_intact = verbatim_quote in text
    if disk_sha256 == recorded_sha256 and quote_intact:
        return {"status": "VERIFIED", "drift_kind": "MATCH", "disk_sha256": disk_sha256}
    if quote_intact:
        return {"status": "STALE", "drift_kind": "FILE_DRIFT_QUOTE_INTACT", "disk_sha256": disk_sha256}
    return {"status": "STALE", "drift_kind": "QUOTE_MUTATED", "disk_sha256": disk_sha256}
""",
    "src/cairnmind/verification.py": """\"\"\"CLI-only test gate execution bound to the Git working tree fingerprint.\"\"\"
from __future__ import annotations

import hashlib
from pathlib import Path


def evaluate_test_gate(profile_name: str, expected_fingerprint: str, current_fingerprint: str) -> dict:
    \"\"\"A passing test run stays valid only while the worktree fingerprint is unchanged.\"\"\"
    if expected_fingerprint != current_fingerprint:
        return {
            "profile_name": profile_name,
            "status": "STALE_CODE",
            "reason": "FILE_FINGERPRINT_MISMATCH",
        }
    return {
        "profile_name": profile_name,
        "status": "PASS",
        "sha256_short": current_fingerprint[:7],
    }
""",
    "src/cairnmind/plans.py": """\"\"\"Atomic, versioned project plans shared by independent coding clients.\"\"\"
from __future__ import annotations


def compare_and_swap_task(task: dict, expected_version: int, expected_plan_version: int) -> dict:
    \"\"\"Reject concurrent progress writes if either task or plan version diverged.\"\"\"
    if task["version"] != expected_version:
        raise ValueError("Task version conflict; read the current plan and retry.")
    if task["plan_version"] != expected_plan_version:
        raise ValueError("Plan version conflict; read the current plan and retry.")
    return {**task, "version": task["version"] + 1}
""",
    "src/cairnmind/mcp_server.py": """\"\"\"Stdio MCP guardrail firewall forwarding validated tool writes to local service.\"\"\"
from __future__ import annotations


def enforce_write_contract(tool_name: str, args: dict, session_state: dict) -> None:
    \"\"\"Block hallucinated quotes, stale OCC packets, or missing test proofs.\"\"\"
    if not session_state.get("synced"):
        raise PermissionError("OCC_STALE_RESUME_PACKET")
    if tool_name == "remember" and not args.get("verbatim_quote"):
        raise ValueError("VERBATIM_QUOTE_NOT_FOUND")
    if tool_name == "task_done" and not session_state.get("test_gate_passed"):
        raise ValueError("TEST_GATE_UNVERIFIED")
""",
}


class MockCockpitMixin:
    """Adds Phases 1-4 Engineer Cockpit state and mutations onto `MockUiState`."""

    read_only: bool = False
    llm_configured: bool = True
    bus: Any
    _lock: Any
    files: dict[str, str]
    state_rev: int
    updated_at: str
    sessions: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    audit_log: list[dict[str, Any]]
    plans: list[dict[str, Any]]
    detected_decisions: list[dict[str, Any]]
    version_history: dict[str, list[dict[str, Any]]]

    def _file_sha(self, path: str) -> str:  # pragma: no cover - provided by MockUiState
        raise NotImplementedError

    def _init_cockpit_data(self) -> None:
        # Enrich existing claims/tasks/audit entries with unified `origin` metadata.
        origin_by_claim = {
            "clm-occ-gate": make_origin("ENGINE_VERIFIED", "verified", origin_client="cairnmind-engine"),
            "clm-quote-verify": make_origin("ENGINE_VERIFIED", "verified", origin_client="claude-code"),
            "clm-test-gate-fp": make_origin(
                "MCP_DEGRADED",
                "degraded",
                origin_client="external",
                source_label="cursor (external)",
            ),
            "clm-plan-cas": make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui"),
            "clm-mcp-firewall": make_origin(
                "MCP_DEGRADED",
                "degraded",
                origin_client="external",
                source_label="cursor (external)",
            ),
        }
        for claim in self.claims:
            claim["origin"] = origin_by_claim.get(
                claim["id"],
                make_origin("ENGINE_VERIFIED", "verified", origin_client="cairnmind-engine"),
            )

        origin_by_task = {
            "TSK-101": make_origin("ENGINE_VERIFIED", "verified", origin_client="claude-code"),
            "TSK-102": make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui"),
            "TSK-103": make_origin("ENGINE_VERIFIED", "verified", origin_client="cairnmind-engine"),
            "TSK-104": make_origin(
                "MCP_DEGRADED",
                "degraded",
                origin_client="external",
                source_label="cursor (external)",
            ),
            "TSK-105": make_origin("HUMAN_REPO", "repo_ci", origin_client="github", source_label="github-pr#464"),
        }
        for task in self.tasks:
            task["origin"] = origin_by_task.get(
                task["id"],
                make_origin("ENGINE_VERIFIED", "verified", origin_client="cairnmind-engine"),
            )
            task.setdefault("external_ref", None)

        for entry in self.audit_log:
            entry["origin"] = classify_audit_origin(entry)

        sha_sessions = self._file_sha("src/cairnmind/sessions.py")
        sha_evidence = self._file_sha("src/cairnmind/evidence.py")
        sha_verification = self._file_sha("src/cairnmind/verification.py")
        sha_plans = self._file_sha("src/cairnmind/plans.py")
        sha_mcp = self._file_sha("src/cairnmind/mcp_server.py")

        self.plans = [
            {
                "id": "plan-release-012",
                "title": "v0.12 Concurrency & Provenance Hardening",
                "goal": "Guarantee deterministic OCC resume gates, verbatim quote provenance, and worktree-bound test gates.",
                "status": "active",
                "version": 3,
                "external_ref": "REL-012",
                "constraints": [
                    "Every shared-memory write requires acknowledged_rev == state_rev",
                    "Never mutate confirmed verdicts from external degraded MCP sessions",
                ],
                "linked_files": [
                    "src/cairnmind/sessions.py",
                    "src/cairnmind/evidence.py",
                    "src/cairnmind/verification.py",
                    "src/cairnmind/plans.py",
                    "src/cairnmind/mcp_server.py",
                ],
                "file_fingerprints": {
                    "src/cairnmind/sessions.py": sha_sessions,
                    "src/cairnmind/evidence.py": sha_evidence,
                    "src/cairnmind/verification.py": sha_verification,
                    "src/cairnmind/plans.py": sha_plans,
                    "src/cairnmind/mcp_server.py": sha_mcp,
                },
                "steps": [
                    {"key": t["step_key"], "title": t["title"], "depends_on": list(t["depends_on"]), "scope": list(t["source_paths"]), "acceptance_criteria": list(t["acceptance_criteria"])}
                    for t in self.tasks
                ],
                "origin": make_origin("ENGINE_VERIFIED", "verified", origin_client="cairnmind-engine"),
                "created_at": "2026-11-25T09:10:00.000Z",
                "updated_at": "2026-11-26T12:28:51.004Z",
            },
            {
                "id": "plan-draft-jira-1234",
                "title": "Multi-project Rust Adapter & CAS Conflict Recovery",
                "goal": "Prepare project_id-scoped API routes and interactive 409 conflict recovery without breaking existing Inspector workflows.",
                "status": "draft",
                "version": 2,
                "external_ref": "JIRA-1234",
                "constraints": [
                    "Zero visual regression in Obsidian Precision Glass aesthetic",
                    "All mutations require expected_version CAS token",
                ],
                "linked_files": [
                    "src/cairnmind/plans.py",
                    "src/cairnmind/sessions.py",
                ],
                "file_fingerprints": {
                    "src/cairnmind/plans.py": sha_plans,
                    "src/cairnmind/sessions.py": sha_sessions,
                },
                "steps": [
                    {
                        "key": "adapter-project-prefix",
                        "title": "Introduce optional project_id prefix in UI HTTP adapter",
                        "depends_on": [],
                        "scope": ["src/cairnmind/plans.py"],
                        "acceptance_criteria": ["Routes accept /api/projects/{project_id}/*"],
                    },
                    {
                        "key": "cas-409-recovery",
                        "title": "Render glass 409 conflict diff banner with refresh or fork draft actions",
                        "depends_on": ["adapter-project-prefix"],
                        "scope": ["src/cairnmind/sessions.py"],
                        "acceptance_criteria": ["Never overwrite remote edits silently"],
                    },
                ],
                "origin": make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui"),
                "created_at": "2026-11-26T10:00:00.000Z",
                "updated_at": "2026-11-26T12:20:00.000Z",
            },
            {
                "id": "plan-draft-stale-paths",
                "title": "Legacy Verifier Cleanup (Pre-check Demo)",
                "goal": "Demonstrate fingerprint pre-check warning when promoting a draft referencing moved or modified files.",
                "status": "draft",
                "version": 1,
                "external_ref": "JIRA-1299",
                "constraints": ["Confirm all referenced files exist before activating"],
                "linked_files": [
                    "src/cairnmind/verification.py",
                    "src/cairnmind/legacy_verifier.py",
                ],
                "file_fingerprints": {
                    "src/cairnmind/verification.py": sha256_text("stale-baseline-verification"),
                    "src/cairnmind/legacy_verifier.py": sha256_text("deleted-file"),
                },
                "steps": [
                    {
                        "key": "remove-legacy-verifier",
                        "title": "Retire obsolete verification fallback helper",
                        "depends_on": [],
                        "scope": ["src/cairnmind/verification.py"],
                        "acceptance_criteria": ["Fingerprint pre-check catches deleted file"],
                    },
                ],
                "origin": make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui"),
                "created_at": "2026-11-26T11:15:00.000Z",
                "updated_at": "2026-11-26T11:15:00.000Z",
            },
        ]

        self.detected_decisions = [
            {
                "id": "det-adr-001",
                "title": "ADR-014: Symbol-anchored evidence survives line shifts",
                "summary": "Bind extracted decisions to AST symbol body hashes in addition to raw file SHA-256 so formatting edits avoid false staleness.",
                "kind": "decision",
                "source_type": "local_file",
                "source_ref": "docs/adr/014-symbol-anchors.md",
                "directory": "docs/adr/",
                "file_path": "src/cairnmind/evidence.py",
                "line_start": 8,
                "line_end": 18,
                "verbatim_quote": "def verify_verbatim_quote(file_path: Path, recorded_sha256: str, verbatim_quote: str) -> dict:",
                "status": "pending",
                "detected_at": "2026-11-26T12:25:00.000Z",
                "origin": make_origin("HUMAN_REPO", "repo_ci", origin_client="adr-scanner", source_label="local_file"),
            },
            {
                "id": "det-pr-465",
                "title": "PR #465: Opt-in code decision ingestion defaults to off",
                "summary": "Code comments, ADRs, and Git trailers remain disabled per project until explicitly enabled; ingested items start as proposed.",
                "kind": "constraint",
                "source_type": "github",
                "source_ref": "GitHub PR #465",
                "directory": "src/cairnmind/",
                "file_path": "src/cairnmind/mcp_server.py",
                "line_start": 5,
                "line_end": 13,
                "verbatim_quote": "def enforce_write_contract(tool_name: str, args: dict, session_state: dict) -> None:",
                "status": "pending",
                "detected_at": "2026-11-26T12:18:10.000Z",
                "origin": make_origin("HUMAN_REPO", "repo_ci", origin_client="github", source_label="github-pr#465"),
            },
            {
                "id": "det-ci-918",
                "title": "CI Scan #918: Windows native file-lock verification required",
                "summary": "CI workflow mandates fast-tier Windows tests alongside Linux/macOS coverage merges (>=95% threshold).",
                "kind": "constraint",
                "source_type": "ci",
                "source_ref": "CI Workflow #918",
                "directory": ".github/workflows/",
                "file_path": "src/cairnmind/verification.py",
                "line_start": 8,
                "line_end": 19,
                "verbatim_quote": "def evaluate_test_gate(profile_name: str, expected_fingerprint: str, current_fingerprint: str) -> dict:",
                "status": "pending",
                "detected_at": "2026-11-26T12:10:45.000Z",
                "origin": make_origin("HUMAN_REPO", "repo_ci", origin_client="ci-scan", source_label="ci-workflow#918"),
            },
            {
                "id": "det-pr-468",
                "title": "PR #468: External history imports always carry origin_evidence=degraded",
                "summary": "Imported third-party transcripts remain searchable but never anchor verified memory or bypass human review.",
                "kind": "decision",
                "source_type": "github",
                "source_ref": "GitHub PR #468",
                "directory": "src/cairnmind/",
                "file_path": "src/cairnmind/sessions.py",
                "line_start": 10,
                "line_end": 22,
                "verbatim_quote": "class SessionManager:",
                "status": "pending",
                "detected_at": "2026-11-26T11:55:20.000Z",
                "origin": make_origin("HUMAN_REPO", "repo_ci", origin_client="github", source_label="github-pr#468"),
            },
            {
                "id": "det-local-plan",
                "title": "RFC-022: Read-before-write CAS enforced for human UI edits",
                "summary": "UI mutations submit expected_version tokens identically to MCP agents so concurrent saves trigger controlled 409 diff resolution.",
                "kind": "plan",
                "source_type": "local_file",
                "source_ref": "docs/adr/022-ui-cas.md",
                "directory": "docs/adr/",
                "file_path": "src/cairnmind/plans.py",
                "line_start": 5,
                "line_end": 12,
                "verbatim_quote": "def compare_and_swap_task(task: dict, expected_version: int, expected_plan_version: int) -> dict:",
                "status": "pending",
                "detected_at": "2026-11-26T11:42:00.000Z",
                "origin": make_origin("HUMAN_REPO", "repo_ci", origin_client="adr-scanner", source_label="local_file"),
            },
            {
                "id": "det-ci-905",
                "title": "CI Scan #905: Redaction gate blocks unredacted tokens in team exports",
                "summary": "Every team memory publication must pass secret scanning before creating shareable snapshots.",
                "kind": "constraint",
                "source_type": "ci",
                "source_ref": "CI Workflow #905",
                "directory": ".github/workflows/",
                "file_path": "src/cairnmind/evidence.py",
                "line_start": 1,
                "line_end": 10,
                "verbatim_quote": '"""Derived verbatim quote provenance and file fingerprint checks."""',
                "status": "accepted",
                "detected_at": "2026-11-26T10:30:00.000Z",
                "origin": make_origin("HUMAN_REPO", "repo_ci", origin_client="ci-scan", source_label="ci-workflow#905"),
            },
            {
                "id": "det-ui-001",
                "title": "UI Spec: Thin adapter boundary ready for Rust multi-project backend",
                "summary": "Keep all REST/SSE calls behind ui/src/api/ with optional project_id path prefixes.",
                "kind": "decision",
                "source_type": "local_file",
                "source_ref": "ui/src/api/adapter.ts",
                "directory": "ui/src/",
                "file_path": "src/cairnmind/plans.py",
                "line_start": 1,
                "line_end": 8,
                "verbatim_quote": '"""Atomic, versioned project plans shared by independent coding clients."""',
                "status": "pending",
                "detected_at": "2026-11-26T09:50:00.000Z",
                "origin": make_origin("HUMAN_REPO", "repo_ci", origin_client="code-comment", source_label="local_file"),
            },
        ]

        # Seed rich version history for Time-Travel Diff & Clone as Draft
        plan_v1 = {
            "id": "plan-release-012",
            "title": "v0.12 Concurrency Hardening",
            "goal": "Introduce OCC session checks.",
            "status": "draft",
            "version": 1,
            "external_ref": "REL-012",
            "constraints": ["Every shared-memory write requires acknowledged_rev == state_rev"],
            "linked_files": ["src/cairnmind/sessions.py", "src/cairnmind/plans.py"],
            "steps": self.plans[0]["steps"][:2],
        }
        plan_v2 = {
            **plan_v1,
            "title": "v0.12 Concurrency & Provenance Hardening",
            "goal": "Guarantee OCC resume gates and verbatim quote provenance.",
            "status": "active",
            "version": 2,
            "linked_files": ["src/cairnmind/sessions.py", "src/cairnmind/evidence.py", "src/cairnmind/plans.py"],
            "steps": self.plans[0]["steps"][:4],
        }
        plan_v3 = deepcopy(self.plans[0])

        draft_v1 = {
            "id": "plan-draft-jira-1234",
            "title": "Multi-project Rust Adapter",
            "goal": "Prepare project_id-scoped API routes.",
            "status": "draft",
            "version": 1,
            "external_ref": "JIRA-1234",
            "constraints": ["Zero visual regression in Obsidian Precision Glass aesthetic"],
            "linked_files": ["src/cairnmind/plans.py"],
            "steps": self.plans[1]["steps"][:1],
        }
        draft_v2 = deepcopy(self.plans[1])

        self.version_history = {
            "plan:plan-release-012": [
                {
                    "version": 1,
                    "timestamp": "2026-11-25T09:10:00.000Z",
                    "reason": "draft_created",
                    "actor_type": "human",
                    "actor_label": "piotr (UI)",
                    "origin": make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui"),
                    "audit_entry_id": None,
                    "summary": "Initial release hardening draft with OCC session & CAS steps.",
                    "snapshot": plan_v1,
                },
                {
                    "version": 2,
                    "timestamp": "2026-11-26T10:15:00.000Z",
                    "reason": "promoted_active",
                    "actor_type": "agent",
                    "actor_label": "claude-code",
                    "origin": make_origin("ENGINE_VERIFIED", "verified", origin_client="claude-code"),
                    "audit_entry_id": "aud-9005",
                    "summary": "Promoted to active and expanded provenance quote & MCP firewall gates.",
                    "snapshot": plan_v2,
                },
                {
                    "version": 3,
                    "timestamp": "2026-11-26T12:28:51.004Z",
                    "reason": "task_progress",
                    "actor_type": "agent",
                    "actor_label": "claude-code",
                    "origin": make_origin("ENGINE_VERIFIED", "verified", origin_client="claude-code"),
                    "audit_entry_id": "aud-9007",
                    "summary": "Added Blast-Radius Inspector task and finalized non-degraded verdict constraint.",
                    "snapshot": plan_v3,
                },
            ],
            "plan:plan-draft-jira-1234": [
                {
                    "version": 1,
                    "timestamp": "2026-11-26T10:00:00.000Z",
                    "reason": "draft_created",
                    "actor_type": "human",
                    "actor_label": "piotr (UI)",
                    "origin": make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui"),
                    "audit_entry_id": None,
                    "summary": "Created Jira draft JIRA-1234 for Rust adapter layer.",
                    "snapshot": draft_v1,
                },
                {
                    "version": 2,
                    "timestamp": "2026-11-26T12:20:00.000Z",
                    "reason": "draft_updated",
                    "actor_type": "human",
                    "actor_label": "piotr (UI)",
                    "origin": make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui"),
                    "audit_entry_id": None,
                    "summary": "Added CAS 409 conflict diff recovery step and sessions.py linkage.",
                    "snapshot": draft_v2,
                },
            ],
            "claim:clm-occ-gate": [
                {
                    "version": 1,
                    "timestamp": "2026-11-25T11:00:00.000Z",
                    "reason": "created",
                    "actor_type": "agent",
                    "actor_label": "cairnmind-engine",
                    "origin": make_origin("ENGINE_VERIFIED", "verified", origin_client="cairnmind-engine"),
                    "audit_entry_id": None,
                    "summary": "Initial extraction of SessionManager.require_ack guard.",
                    "snapshot": {**deepcopy(self.claims[0]), "version": 1, "status": "proposed", "provenance_status": "VERIFIED"},
                },
                {
                    "version": 2,
                    "timestamp": "2026-11-26T09:00:00.000Z",
                    "reason": "human_confirmed",
                    "actor_type": "human",
                    "actor_label": "piotr (UI)",
                    "origin": make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui"),
                    "audit_entry_id": "aud-9005",
                    "summary": "Engineer confirmed OCC constraint against sessions.py.",
                    "snapshot": {**deepcopy(self.claims[0]), "version": 2, "status": "confirmed", "provenance_status": "VERIFIED"},
                },
                {
                    "version": 3,
                    "timestamp": "2026-11-26T12:24:10.000Z",
                    "reason": "fingerprint_drift",
                    "actor_type": "agent",
                    "actor_label": "codex-cli",
                    "origin": make_origin("ENGINE_VERIFIED", "verified", origin_client="codex-cli"),
                    "audit_entry_id": "aud-9007",
                    "summary": "Recorded SHA-256 drifted after sessions.py edit; quote remains intact.",
                    "snapshot": deepcopy(self.claims[0]),
                },
            ],
        }

    def _require_writable(self) -> None:
        if self.read_only:
            raise HTTPException(
                403,
                "Cockpit is running in --read-only preview mode; write actions are disabled.",
            )

    def capabilities(self) -> dict[str, Any]:
        return {
            "writable": not self.read_only,
            "read_only_reason": "Launched with --read-only safe preview mode." if self.read_only else None,
            "ask_available": True,
            "llm_configured": self.llm_configured,
            "multi_project": True,
            "supported_export_modes": ["team"],
        }

    def drift_summary(self) -> dict[str, Any]:
        drifted_files = {c["source_path"] for c in self.claims if c["provenance_status"] != "VERIFIED" and c["source_path"]}
        stale_tests = sum(1 for t in self.tasks if t["test_gate"]["status"] != "PASS")
        pending_decisions = sum(1 for d in self.detected_decisions if d["status"] == "pending")
        active = next((p for p in self.plans if p["status"] == "active"), None)
        return {
            "drifted_files_count": len(drifted_files),
            "stale_tests_count": stale_tests,
            "pending_decisions_count": pending_decisions,
            "last_approved_plan_id": active["id"] if active else None,
            "last_approved_plan_version": active["version"] if active else None,
        }

    def ask(self, req: UiAskRequest) -> dict[str, Any]:
        started = time.perf_counter()
        query = req.query.strip()
        q_lower = query.lower()
        now = utc_now()

        with self._lock:
            fallback_mode = req.verifier_mode == "fts_fallback" or not self.llm_configured
            citations: list[dict[str, Any]] = []

            def build_citation_from_claim(claim: dict[str, Any]) -> dict[str, Any]:
                path = claim["source_path"]
                fp = self._file_sha(path) if path in self.files else claim["current_disk_sha256"]
                return {
                    "id": f"cit-{claim['id']}",
                    "claim_id": claim["id"],
                    "file_path": path,
                    "line_start": claim["line_start"],
                    "line_end": claim["line_end"],
                    "fingerprint": fp,
                    "verbatim_quote": claim["verbatim_quote"],
                    "origin": deepcopy(claim.get("origin")),
                    "stale_now": False,
                }

            # Match known onboarding / domain queries deterministically
            if any(tok in q_lower for tok in ("unknown", "billing", "stripe", "kubernetes", "k8s", "graphql", "payment")):
                latency_ms = max(12, int((time.perf_counter() - started) * 1000) + 18)
                return {
                    "id": f"ask-{uuid4().hex[:8]}",
                    "query": query,
                    "status": "UNKNOWN",
                    "answer": (
                        "UNKNOWN — No verified evidence or indexed source files in this project support an answer "
                        "about that topic. Cairnmind refuses to speculate without verbatim citations."
                    ),
                    "citations": [],
                    "tier_used": "tier_0_fts_evidence" if fallback_mode else "tier_2_verified_llm",
                    "latency_ms": latency_ms,
                    "cached": False,
                    "fallback_mode": fallback_mode,
                    "fallback_reason": "LLM not configured — searched Tier 0 FTS evidence only." if fallback_mode else None,
                    "unknowns": ["No indexed code or memory claim matched the requested subject."],
                    "timestamp": now,
                }

            is_polish = bool(re.search(r"[ąćęłńóśźż]", q_lower)) or any(
                tok in q_lower
                for tok in ("jakie", "ktore", "komponent", "przeplyw", "decyzj", "wykryto", "nieaktualn", "blokuj", "zadania", "dowody")
            )

            if any(tok in q_lower for tok in ("komponent", "przepływ", "architecture", "component", "flow", "overview")):
                matched_claims = [c for c in self.claims if c["id"] in {"clm-occ-gate", "clm-quote-verify", "clm-plan-cas", "clm-mcp-firewall"}]
                citations = [build_citation_from_claim(c) for c in matched_claims]
                if is_polish:
                    answer = (
                        "Główne komponenty Cairnminda tworzą czterostopniowy potok weryfikacji: "
                        "(1) `SessionManager.require_ack` wymusza zgodność `acknowledged_rev` z `state_rev` (OCC gate), "
                        "(2) `verify_verbatim_quote` porównuje SHA-256 i dosłowny cytat w kodzie, "
                        "(3) `compare_and_swap_task` chroni atomowość planów i wersji zadań, oraz "
                        "(4) `enforce_write_contract` blokuje zapisy MCP bez cytatów i zaliczonych testów."
                    )
                else:
                    answer = (
                        "Cairnmind's core components form a four-stage verification pipeline: "
                        "(1) `SessionManager.require_ack` enforces `acknowledged_rev` matching `state_rev` (OCC gate), "
                        "(2) `verify_verbatim_quote` checks the SHA-256 hash and verbatim quote in source code, "
                        "(3) `compare_and_swap_task` protects atomic plan and task versions, and "
                        "(4) `enforce_write_contract` blocks MCP writes lacking citations or passing checks."
                    )
            elif any(tok in q_lower for tok in ("decyzj", "decision", "adr", "wykryto", "recent")):
                matched_claims = [c for c in self.claims if c["kind"] in {"decision", "constraint"}][:3]
                citations = [build_citation_from_claim(c) for c in matched_claims]
                pending = [d["title"] for d in self.detected_decisions if d["status"] == "pending"][:3]
                if is_polish:
                    answer = (
                        "Zatwierdzone decyzje i wykryte propozycje obejmują: rozróżnianie `FILE_DRIFT_QUOTE_INTACT` od "
                        "`QUOTE_MUTATED` w `evidence.py`, podwójny CAS (`task` + `plan`) w `plans.py`, oraz oczekujące "
                        f"wykryte decyzje z repo/PR/CI: {'; '.join(pending)}."
                    )
                else:
                    answer = (
                        "Confirmed decisions and detected proposals include: distinguishing `FILE_DRIFT_QUOTE_INTACT` "
                        "from `QUOTE_MUTATED` in `evidence.py`, dual CAS (`task` + `plan`) in `plans.py`, and pending "
                        f"detected decisions from repo/PR/CI: {'; '.join(pending)}."
                    )
            elif any(tok in q_lower for tok in ("nieaktualn", "stale", "drift", "blokuj", "block")):
                matched_claims = [c for c in self.claims if c["provenance_status"] != "VERIFIED"]
                citations = [build_citation_from_claim(c) for c in matched_claims]
                stale_tasks = [f"{t['id']} ({t['test_gate']['status']})" for t in self.tasks if t["test_gate"]["status"] != "PASS"]
                if is_polish:
                    answer = (
                        f"Wykryto {len(matched_claims)} nieaktualne dowody (m.in. `clm-test-gate-fp` ze stanem `QUOTE_MUTATED` "
                        f"w `verification.py` oraz `clm-occ-gate` z `FILE_DRIFT_QUOTE_INTACT` w `sessions.py`). "
                        f"Zadania wymagające akcji: {', '.join(stale_tasks)}."
                    )
                else:
                    answer = (
                        f"Detected {len(matched_claims)} stale evidence items (including `clm-test-gate-fp` with "
                        f"`QUOTE_MUTATED` in `verification.py` and `clm-occ-gate` with `FILE_DRIFT_QUOTE_INTACT` in "
                        f"`sessions.py`). Tasks requiring action: {', '.join(stale_tasks)}."
                    )
            else:
                # General lexical search across claims and mock files
                terms = [t for t in re.findall(r"[a-z0-9_]{3,}", q_lower)]
                scored: list[tuple[int, dict[str, Any]]] = []
                for claim in self.claims:
                    haystack = " ".join([
                        claim["key"],
                        claim["title"],
                        claim["content"],
                        claim["source_path"],
                        claim["verbatim_quote"],
                    ]).lower()
                    score = sum(1 for term in terms if term in haystack)
                    if score > 0:
                        scored.append((score, claim))
                scored.sort(key=lambda item: item[0], reverse=True)
                matched_claims = [c for _, c in scored[:3]]
                citations = [build_citation_from_claim(c) for c in matched_claims]
                if not citations:
                    latency_ms = max(10, int((time.perf_counter() - started) * 1000) + 14)
                    unknown_answer = (
                        "UNKNOWN — Brak dowodów w pamięci projektu ani w zindeksowanych plikach dla tego pytania."
                        if is_polish
                        else "UNKNOWN — No evidence found in project memory or indexed files for this question."
                    )
                    return {
                        "id": f"ask-{uuid4().hex[:8]}",
                        "query": query,
                        "status": "UNKNOWN",
                        "answer": unknown_answer,
                        "citations": [],
                        "tier_used": "tier_0_fts_evidence" if fallback_mode else "tier_2_verified_llm",
                        "latency_ms": latency_ms,
                        "cached": False,
                        "fallback_mode": fallback_mode,
                        "fallback_reason": "LLM not configured — Tier 0 FTS search returned 0 citations." if fallback_mode else None,
                        "unknowns": ["Try refining the query with a module, symbol, or task identifier."],
                        "timestamp": now,
                    }
                snippets = "; ".join(f"{c['key']} ({c['source_path']})" for c in matched_claims)
                answer = (
                    f"Znaleziono {len(matched_claims)} zweryfikowane dowody powiązane z zapytaniem: {snippets}."
                    if is_polish
                    else f"Found {len(matched_claims)} verified evidence items related to the query: {snippets}."
                )

            if fallback_mode:
                prefix = (
                    f"[Tier 0 FTS Evidence Mode] Bez LLM — znaleziono {len(citations)} dopasowane cytaty źródłowe. "
                    if is_polish
                    else f"[Tier 0 FTS Evidence Mode] No LLM configured — found {len(citations)} matching source citations. "
                )
                answer = prefix + answer

            status = "PARTIAL" if fallback_mode else "VERIFIED"
            latency_ms = max(18, int((time.perf_counter() - started) * 1000) + (22 if fallback_mode else 64))
            return {
                "id": f"ask-{uuid4().hex[:8]}",
                "query": query,
                "status": status,
                "answer": answer,
                "citations": citations,
                "tier_used": "tier_0_fts_evidence" if fallback_mode else "tier_2_verified_llm",
                "latency_ms": latency_ms,
                "cached": False,
                "fallback_mode": fallback_mode,
                "fallback_reason": (
                    "No LLM configured / offline mode — serving pure verbatim citations via Tier 0 FTS."
                    if fallback_mode
                    else None
                ),
                "unknowns": [],
                "timestamp": now,
            }

    def drift_monitor(
        self,
        *,
        source: str = "all",
        directory: str | None = None,
        status: str = "all",
        page: int = 1,
        page_size: int = 5,
    ) -> dict[str, Any]:
        with self._lock:
            approved_plan = next((deepcopy(p) for p in self.plans if p["status"] == "active"), None)
            fp = sha256_text("|".join(f"{k}:{self._file_sha(k)}" for k in sorted(self.files)))
            drift_items: list[dict[str, Any]] = []
            for claim in self.claims:
                if claim["provenance_status"] == "VERIFIED":
                    continue
                drift_type = (
                    "QUOTE_MUTATED"
                    if claim["drift_kind"] == "QUOTE_MUTATED"
                    else "FILE_MODIFIED"
                )
                origin = deepcopy(claim.get("origin") or make_origin("HUMAN_REPO", "human"))
                actor = "agent" if origin["category"] == "MCP_DEGRADED" else "human"
                drift_items.append({
                    "id": f"drift-{claim['id']}",
                    "file_path": claim["source_path"],
                    "drift_type": drift_type,
                    "actor": actor,
                    "origin": origin,
                    "recorded_sha256": claim["recorded_sha256"],
                    "current_sha256": claim["current_disk_sha256"],
                    "summary": f"{claim['key']}: {claim['drift_kind']} impacting {len(claim['task_ids'])} task(s).",
                    "impacted_task_ids": list(claim["task_ids"]),
                    "impacted_claim_ids": [claim["id"]],
                    "detected_at": claim["updated_at"],
                })

            test_freshness: list[dict[str, Any]] = []
            for task in self.tasks:
                gate = task["test_gate"]
                rejected_audit = next(
                    (a for a in self.audit_log if a.get("target_id") == task["id"] and a.get("verdict") == "REJECTED"),
                    None,
                )
                if gate["status"] == "FAIL" or (rejected_audit and gate["status"] != "PASS" and task["state"] == "blocked"):
                    freshness = "REJECTED"
                    reason = rejected_audit["reason_code"] if rejected_audit else "TEST_GATE_FAIL"
                elif gate["status"] == "PASS" and task["evidence_status"] == "VERIFIED":
                    freshness = "FRESH"
                    reason = "Fingerprint verified"
                else:
                    freshness = "STALE"
                    reason = f"{gate['status']} — code changed after last verified run"
                test_freshness.append({
                    "task_id": task["id"],
                    "task_title": task["title"],
                    "plan_id": task["plan_id"],
                    "profile_name": gate["profile_name"],
                    "freshness": freshness,
                    "gate_status": gate["status"],
                    "run_id": gate["run_id"],
                    "sha256_short": gate["sha256_short"],
                    "ran_at": gate["ran_at"],
                    "reason": reason,
                    "origin": deepcopy(task.get("origin")),
                })

            # Filtered & paginated detected decisions (anti-noise for monorepos)
            all_decisions = list(self.detected_decisions)
            directories = sorted({d["directory"] for d in all_decisions if d.get("directory")})
            source_counts = {
                "local_file": sum(1 for d in all_decisions if d["source_type"] == "local_file"),
                "github": sum(1 for d in all_decisions if d["source_type"] == "github"),
                "ci": sum(1 for d in all_decisions if d["source_type"] == "ci"),
            }
            filtered = all_decisions
            if source and source != "all":
                filtered = [d for d in filtered if d["source_type"] == source]
            if directory:
                dir_norm = directory.strip().rstrip("/") + "/"
                filtered = [d for d in filtered if d["directory"].startswith(dir_norm) or d["directory"] == directory]
            if status and status != "all":
                filtered = [d for d in filtered if d["status"] == status]

            safe_page_size = max(1, min(50, int(page_size)))
            total = len(filtered)
            total_pages = max(1, (total + safe_page_size - 1) // safe_page_size)
            safe_page = max(1, min(int(page), total_pages))
            start = (safe_page - 1) * safe_page_size
            page_items = deepcopy(filtered[start : start + safe_page_size])

            return {
                "approved_plan": approved_plan,
                "repo_state": {
                    "branch": "main",
                    "head": "86a303f9c4e1",
                    "fingerprint": fp,
                    "checked_at": self.updated_at,
                    "dirty_files_count": len({d["file_path"] for d in drift_items}),
                },
                "drift_items": drift_items,
                "test_freshness": test_freshness,
                "detected_decisions": {
                    "items": page_items,
                    "total": total,
                    "page": safe_page,
                    "page_size": safe_page_size,
                    "total_pages": total_pages,
                    "directories": directories,
                    "source_counts": source_counts,
                },
            }

    def decide_detected_decision(self, decision_id: str, req: DecisionActionRequest) -> dict[str, Any]:
        self._require_writable()
        now = utc_now()
        created_claim_id: str | None = None
        with self._lock:
            target = next((d for d in self.detected_decisions if d["id"] == decision_id), None)
            if target is None:
                raise HTTPException(404, f"Detected decision not found: {decision_id}")
            target["status"] = "accepted" if req.action == "accept" else "ignored"
            self.state_rev += 1
            self.updated_at = now
            if req.action == "accept":
                created_claim_id = f"clm-{decision_id}"
                if not any(c["id"] == created_claim_id for c in self.claims):
                    file_path = target.get("file_path") or ""
                    sha = self._file_sha(file_path) if file_path in self.files else sha256_text(target["summary"])
                    new_claim = {
                        "id": created_claim_id,
                        "kind": target["kind"] if target["kind"] in {"decision", "constraint", "fact"} else "decision",
                        "key": f"detected.{decision_id}",
                        "title": target["title"],
                        "content": target["summary"],
                        "status": "confirmed",
                        "provenance_status": "VERIFIED",
                        "source_path": file_path,
                        "line_start": target.get("line_start", 1),
                        "line_end": target.get("line_end", 10),
                        "verbatim_quote": target.get("verbatim_quote", ""),
                        "recorded_sha256": sha,
                        "current_disk_sha256": sha,
                        "quote_survives": True,
                        "drift_kind": "MATCH",
                        "updated_at": now,
                        "task_ids": [],
                        "version": 1,
                        "origin": deepcopy(target["origin"]),
                    }
                    self.claims.insert(0, new_claim)
                    self.version_history[f"claim:{created_claim_id}"] = [{
                        "version": 1,
                        "timestamp": now,
                        "reason": "accepted_detected_decision",
                        "actor_type": "human",
                        "actor_label": "engineer (UI)",
                        "origin": deepcopy(target["origin"]),
                        "audit_entry_id": None,
                        "summary": f"Accepted detected decision from {target['source_ref']}.",
                        "snapshot": deepcopy(new_claim),
                    }]
            rev = self.state_rev
            decision_copy = deepcopy(target)

        self.bus.publish("state.updated", {
            "state_rev": rev,
            "reason": f"detected_decision_{req.action}",
            "decision_id": decision_id,
        })
        return {
            "ok": True,
            "decision": decision_copy,
            "created_claim_id": created_claim_id,
            "state_rev": rev,
        }

    def create_plan(self, req: CreatePlanRequest) -> dict[str, Any]:
        self._require_writable()
        now = utc_now()
        with self._lock:
            plan_id = f"plan-{req.status}-{uuid4().hex[:6]}"
            steps = [s.model_dump() for s in req.steps]
            linked = list(dict.fromkeys([f.strip() for f in req.linked_files if f.strip()]))
            file_fps = {
                path: self._file_sha(path) if path in self.files else sha256_text(f"missing:{path}")
                for path in linked
            }
            if req.status == "active":
                for p in self.plans:
                    if p["status"] == "active":
                        p["status"] = "archived"
            origin = make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui")
            plan = {
                "id": plan_id,
                "title": req.title.strip(),
                "goal": req.goal.strip(),
                "status": req.status,
                "version": 1,
                "external_ref": req.external_ref.strip() if req.external_ref else None,
                "constraints": [c.strip() for c in req.constraints if c.strip()],
                "linked_files": linked,
                "file_fingerprints": file_fps,
                "steps": steps,
                "origin": origin,
                "created_at": now,
                "updated_at": now,
            }
            self.plans.insert(0, plan)
            self.version_history[f"plan:{plan_id}"] = [{
                "version": 1,
                "timestamp": now,
                "reason": "draft_created" if req.status == "draft" else "plan_created",
                "actor_type": "human",
                "actor_label": "engineer (UI)",
                "origin": deepcopy(origin),
                "audit_entry_id": None,
                "summary": f"Created {req.status} plan '{plan['title']}'.",
                "snapshot": deepcopy(plan),
            }]
            self.state_rev += 1
            self.updated_at = now
            rev = self.state_rev
            result_plan = deepcopy(plan)

        self.bus.publish("state.updated", {
            "state_rev": rev,
            "reason": "plan_created",
            "plan_id": plan_id,
            "status": req.status,
        })
        return {"ok": True, "plan": result_plan, "state_rev": rev}

    def update_plan(self, plan_id: str, req: UpdatePlanRequest) -> dict[str, Any]:
        self._require_writable()
        now = utc_now()
        with self._lock:
            target = next((p for p in self.plans if p["id"] == plan_id), None)
            if target is None:
                raise HTTPException(404, f"Plan not found: {plan_id}")
            if req.expected_version != target["version"]:
                proposed = deepcopy(target)
                if req.title is not None:
                    proposed["title"] = req.title.strip()
                if req.goal is not None:
                    proposed["goal"] = req.goal.strip()
                if req.external_ref is not None:
                    proposed["external_ref"] = req.external_ref.strip() or None
                if req.constraints is not None:
                    proposed["constraints"] = [c.strip() for c in req.constraints if c.strip()]
                if req.linked_files is not None:
                    proposed["linked_files"] = list(dict.fromkeys([f.strip() for f in req.linked_files if f.strip()]))
                if req.steps is not None:
                    proposed["steps"] = [s.model_dump() for s in req.steps]
                diff = compute_snapshot_diff(proposed, target)
                raise HTTPException(
                    409,
                    detail={
                        "code": "stale_plan_version",
                        "detail": (
                            f"Plan {plan_id} version conflict: expected v{req.expected_version}, "
                            f"current version is v{target['version']}."
                        ),
                        "entity_kind": "plan",
                        "entity_id": plan_id,
                        "expected_version": req.expected_version,
                        "current_version": target["version"],
                        "modified_by": deepcopy(target.get("origin")),
                        "server_snapshot": deepcopy(target),
                        "diff": diff,
                    },
                )

            if req.title is not None:
                target["title"] = req.title.strip()
            if req.goal is not None:
                target["goal"] = req.goal.strip()
            if req.external_ref is not None:
                target["external_ref"] = req.external_ref.strip() or None
            if req.constraints is not None:
                target["constraints"] = [c.strip() for c in req.constraints if c.strip()]
            if req.linked_files is not None:
                linked = list(dict.fromkeys([f.strip() for f in req.linked_files if f.strip()]))
                target["linked_files"] = linked
                target["file_fingerprints"] = {
                    path: self._file_sha(path) if path in self.files else sha256_text(f"missing:{path}")
                    for path in linked
                }
            if req.steps is not None:
                target["steps"] = [s.model_dump() for s in req.steps]
            if req.status is not None:
                target["status"] = req.status
            target["version"] += 1
            target["updated_at"] = now
            origin = make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui")
            target["origin"] = origin

            self.version_history.setdefault(f"plan:{plan_id}", []).append({
                "version": target["version"],
                "timestamp": now,
                "reason": "draft_updated",
                "actor_type": "human",
                "actor_label": "engineer (UI)",
                "origin": deepcopy(origin),
                "audit_entry_id": None,
                "summary": f"Updated plan '{target['title']}' to v{target['version']}.",
                "snapshot": deepcopy(target),
            })
            self.state_rev += 1
            self.updated_at = now
            rev = self.state_rev
            result_plan = deepcopy(target)

        self.bus.publish("state.updated", {
            "state_rev": rev,
            "reason": "plan_updated",
            "plan_id": plan_id,
            "version": result_plan["version"],
        })
        return {"ok": True, "plan": result_plan, "state_rev": rev}

    def promote_plan(self, plan_id: str, req: PromotePlanRequest) -> dict[str, Any]:
        self._require_writable()
        now = utc_now()
        with self._lock:
            target = next((p for p in self.plans if p["id"] == plan_id), None)
            if target is None:
                raise HTTPException(404, f"Plan not found: {plan_id}")
            if req.expected_version != target["version"]:
                raise HTTPException(
                    409,
                    detail={
                        "code": "stale_plan_version",
                        "detail": (
                            f"Plan {plan_id} version conflict during promotion: expected v{req.expected_version}, "
                            f"current is v{target['version']}."
                        ),
                        "entity_kind": "plan",
                        "entity_id": plan_id,
                        "expected_version": req.expected_version,
                        "current_version": target["version"],
                        "modified_by": deepcopy(target.get("origin")),
                        "server_snapshot": deepcopy(target),
                        "diff": [],
                    },
                )

            missing_files: list[str] = []
            drifted_files: list[dict[str, str]] = []
            verified_files: list[str] = []
            recorded_fps = target.get("file_fingerprints") or {}
            for path in target.get("linked_files") or []:
                if path not in self.files:
                    missing_files.append(path)
                else:
                    current_sha = self._file_sha(path)
                    rec_sha = recorded_fps.get(path)
                    if rec_sha and rec_sha != current_sha:
                        drifted_files.append({
                            "path": path,
                            "recorded_sha256": rec_sha,
                            "current_sha256": current_sha,
                        })
                    else:
                        verified_files.append(path)

            precheck = {
                "missing_files": missing_files,
                "drifted_files": drifted_files,
                "verified_files": verified_files,
            }
            if (missing_files or drifted_files) and not req.force:
                return {
                    "ok": False,
                    "requires_confirmation": True,
                    "precheck": precheck,
                    "plan": deepcopy(target),
                    "state_rev": self.state_rev,
                }

            for p in self.plans:
                if p["id"] != plan_id and p["status"] == "active":
                    p["status"] = "archived"
            target["status"] = "active"
            target["version"] += 1
            target["updated_at"] = now
            target["file_fingerprints"] = {
                path: self._file_sha(path)
                for path in target.get("linked_files") or []
                if path in self.files
            }
            origin = make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui")
            target["origin"] = origin

            # Materialize steps into tasks table if not already present
            for idx, step in enumerate(target.get("steps") or [], start=1):
                step_key = step["key"]
                if not any(t["plan_id"] == plan_id and t["step_key"] == step_key for t in self.tasks):
                    self.tasks.insert(0, {
                        "id": f"TSK-{200 + len(self.tasks) + idx}",
                        "step_key": step_key,
                        "title": step["title"],
                        "plan_id": plan_id,
                        "plan_title": target["title"],
                        "status": "confirmed",
                        "state": "todo",
                        "version": 1,
                        "evidence_verified": 0,
                        "evidence_total": max(1, len(step.get("scope") or [])),
                        "evidence_status": "UNVERIFIED",
                        "test_gate": {
                            "profile_name": "default",
                            "status": "MISSING",
                            "sha256_short": None,
                            "run_id": None,
                            "ran_at": None,
                        },
                        "updated_at": now,
                        "claim_ids": [],
                        "source_paths": list(step.get("scope") or []),
                        "depends_on": list(step.get("depends_on") or []),
                        "acceptance_criteria": list(step.get("acceptance_criteria") or []),
                        "origin": deepcopy(origin),
                        "external_ref": target.get("external_ref"),
                    })

            self.version_history.setdefault(f"plan:{plan_id}", []).append({
                "version": target["version"],
                "timestamp": now,
                "reason": "promoted_active",
                "actor_type": "human",
                "actor_label": "engineer (UI)",
                "origin": deepcopy(origin),
                "audit_entry_id": None,
                "summary": f"Promoted draft '{target['title']}' to active plan v{target['version']}.",
                "snapshot": deepcopy(target),
            })
            self.state_rev += 1
            self.updated_at = now
            rev = self.state_rev
            result_plan = deepcopy(target)

        self.bus.publish("state.updated", {
            "state_rev": rev,
            "reason": "plan_promoted",
            "plan_id": plan_id,
            "version": result_plan["version"],
        })
        return {
            "ok": True,
            "requires_confirmation": False,
            "precheck": precheck,
            "plan": result_plan,
            "state_rev": rev,
        }

    def create_task(self, req: CreateTaskRequest) -> dict[str, Any]:
        self._require_writable()
        now = utc_now()
        with self._lock:
            target_plan = next((p for p in self.plans if p["id"] == req.plan_id), None) if req.plan_id else None
            if req.plan_id and target_plan is None:
                raise HTTPException(404, f"Plan not found: {req.plan_id}")
            if target_plan is not None and req.expected_version != target_plan["version"]:
                raise HTTPException(
                    409,
                    detail={
                        "code": "stale_plan_version",
                        "detail": (
                            f"Plan {target_plan['id']} version conflict: expected v{req.expected_version}, "
                            f"current is v{target_plan['version']}."
                        ),
                        "entity_kind": "plan",
                        "entity_id": target_plan["id"],
                        "expected_version": req.expected_version,
                        "current_version": target_plan["version"],
                        "modified_by": deepcopy(target_plan.get("origin")),
                        "server_snapshot": deepcopy(target_plan),
                        "diff": [],
                    },
                )
            task_id = f"TSK-{len(self.tasks) + 101}"
            step_key = (req.step_key or re.sub(r"[^a-z0-9]+", "-", req.title.lower()).strip("-") or task_id.lower())[:64]
            origin = make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui")
            new_task = {
                "id": task_id,
                "step_key": step_key,
                "title": req.title.strip(),
                "plan_id": target_plan["id"] if target_plan else "standalone-drafts",
                "plan_title": target_plan["title"] if target_plan else "Standalone Quick Tasks",
                "status": req.status,
                "state": "draft" if req.status == "draft" else "todo",
                "version": 1,
                "evidence_verified": 0,
                "evidence_total": max(1, len(req.scope)),
                "evidence_status": "UNVERIFIED",
                "test_gate": {
                    "profile_name": "default",
                    "status": "MISSING",
                    "sha256_short": None,
                    "run_id": None,
                    "ran_at": None,
                },
                "updated_at": now,
                "claim_ids": [],
                "source_paths": [p.strip() for p in req.scope if p.strip()],
                "depends_on": [],
                "acceptance_criteria": [c.strip() for c in req.acceptance_criteria if c.strip()],
                "origin": origin,
                "external_ref": req.external_ref.strip() if req.external_ref else None,
            }
            self.tasks.insert(0, new_task)
            if target_plan is not None:
                target_plan["version"] += 1
                target_plan["steps"].append({
                    "key": step_key,
                    "title": new_task["title"],
                    "depends_on": [],
                    "scope": list(new_task["source_paths"]),
                    "acceptance_criteria": list(new_task["acceptance_criteria"]),
                })
                target_plan["updated_at"] = now
            self.version_history[f"task:{task_id}"] = [{
                "version": 1,
                "timestamp": now,
                "reason": "quick_task_created",
                "actor_type": "human",
                "actor_label": "engineer (UI)",
                "origin": deepcopy(origin),
                "audit_entry_id": None,
                "summary": f"Added quick task '{new_task['title']}'.",
                "snapshot": deepcopy(new_task),
            }]
            self.state_rev += 1
            self.updated_at = now
            rev = self.state_rev
            result_task = deepcopy(new_task)

        self.bus.publish("state.updated", {
            "state_rev": rev,
            "reason": "task_created",
            "task_id": task_id,
        })
        return {"ok": True, "task": result_task, "state_rev": rev}

    def create_fact(self, req: CreateFactRequest) -> dict[str, Any]:
        self._require_writable()
        now = utc_now()
        with self._lock:
            claim_id = f"clm-{uuid4().hex[:8]}"
            path = (req.source_path or "").strip()
            sha = self._file_sha(path) if path and path in self.files else sha256_text(req.content)
            quote = req.verbatim_quote.strip()
            quote_survives = (quote in self.files[path]) if (path and path in self.files and quote) else None
            prov_status = "VERIFIED" if (not path or quote_survives is not False) else "STALE"
            drift_kind = "QUOTE_MUTATED" if quote_survives is False else "MATCH"
            origin = make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui")
            new_claim = {
                "id": claim_id,
                "kind": req.kind,
                "key": req.key.strip(),
                "title": req.title.strip(),
                "content": req.content.strip(),
                "status": "confirmed",
                "provenance_status": prov_status,
                "source_path": path,
                "line_start": req.line_start,
                "line_end": max(req.line_start, req.line_end),
                "verbatim_quote": quote,
                "recorded_sha256": sha,
                "current_disk_sha256": sha,
                "quote_survives": quote_survives,
                "drift_kind": drift_kind,
                "updated_at": now,
                "task_ids": [],
                "version": 1,
                "origin": origin,
            }
            self.claims.insert(0, new_claim)
            self.version_history[f"claim:{claim_id}"] = [{
                "version": 1,
                "timestamp": now,
                "reason": "fact_created",
                "actor_type": "human",
                "actor_label": "engineer (UI)",
                "origin": deepcopy(origin),
                "audit_entry_id": None,
                "summary": f"Recorded {req.kind} '{req.key}'.",
                "snapshot": deepcopy(new_claim),
            }]
            self.state_rev += 1
            self.updated_at = now
            rev = self.state_rev
            result_claim = deepcopy(new_claim)

        self.bus.publish("state.updated", {
            "state_rev": rev,
            "reason": "fact_created",
            "claim_id": claim_id,
        })
        return {"ok": True, "claim": result_claim, "state_rev": rev}

    def get_versions(
        self,
        entity_kind: str,
        entity_id: str,
        from_version: int | None = None,
        to_version: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            key = f"{entity_kind}:{entity_id}"
            versions = deepcopy(self.version_history.get(key) or [])
            if not versions:
                entity = None
                if entity_kind == "plan":
                    entity = next((p for p in self.plans if p["id"] == entity_id), None)
                elif entity_kind == "task":
                    entity = next((t for t in self.tasks if t["id"] == entity_id), None)
                elif entity_kind == "claim":
                    entity = next((c for c in self.claims if c["id"] == entity_id), None)
                if entity is None:
                    raise HTTPException(404, f"{entity_kind} not found: {entity_id}")
                versions = [{
                    "version": int(entity.get("version") or 1),
                    "timestamp": entity.get("updated_at") or self.updated_at,
                    "reason": "current_state",
                    "actor_type": "human" if (entity.get("origin") or {}).get("category") == "HUMAN_REPO" else "agent",
                    "actor_label": (entity.get("origin") or {}).get("source_label") or "cairnmind",
                    "origin": deepcopy(entity.get("origin") or make_origin("ENGINE_VERIFIED", "verified")),
                    "audit_entry_id": None,
                    "summary": "Current recorded state.",
                    "snapshot": deepcopy(entity),
                }]

            versions.sort(key=lambda item: item["version"])
            latest_ver = versions[-1]["version"]
            to_v = to_version or latest_ver
            from_v = from_version or (versions[-2]["version"] if len(versions) >= 2 else latest_ver)
            from_entry = next((v for v in versions if v["version"] == from_v), versions[0])
            to_entry = next((v for v in versions if v["version"] == to_v), versions[-1])
            diff = compute_snapshot_diff(from_entry["snapshot"], to_entry["snapshot"])
            return {
                "entity_kind": entity_kind,
                "entity_id": entity_id,
                "current_version": latest_ver,
                "from_version": from_entry["version"],
                "to_version": to_entry["version"],
                "versions": versions,
                "diff": diff,
            }

    def clone_version_as_draft(
        self,
        entity_kind: str,
        entity_id: str,
        req: CloneVersionRequest,
    ) -> dict[str, Any]:
        self._require_writable()
        history = self.get_versions(entity_kind, entity_id)
        chosen = next((v for v in history["versions"] if v["version"] == req.source_version), None)
        if chosen is None:
            raise HTTPException(404, f"Version v{req.source_version} not found for {entity_kind}:{entity_id}")

        snap = deepcopy(chosen["snapshot"])
        now = utc_now()
        suffix = req.title_suffix or f"(restored from v{req.source_version})"
        origin = make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui")

        if entity_kind == "plan":
            with self._lock:
                new_id = f"plan-draft-clone-{uuid4().hex[:6]}"
                draft_plan = {
                    **snap,
                    "id": new_id,
                    "title": f"{snap.get('title', entity_id)} {suffix}",
                    "status": "draft",
                    "version": 1,
                    "origin": origin,
                    "created_at": now,
                    "updated_at": now,
                }
                self.plans.insert(0, draft_plan)
                self.version_history[f"plan:{new_id}"] = [{
                    "version": 1,
                    "timestamp": now,
                    "reason": f"cloned_from_v{req.source_version}",
                    "actor_type": "human",
                    "actor_label": "engineer (UI)",
                    "origin": deepcopy(origin),
                    "audit_entry_id": None,
                    "summary": f"Safe rollback draft cloned from {entity_id} v{req.source_version}.",
                    "snapshot": deepcopy(draft_plan),
                }]
                self.state_rev += 1
                self.updated_at = now
                rev = self.state_rev
            self.bus.publish("state.updated", {"state_rev": rev, "reason": "version_cloned_draft", "plan_id": new_id})
            return {"ok": True, "source_version": req.source_version, "draft_plan": draft_plan, "state_rev": rev}

        if entity_kind == "claim":
            with self._lock:
                new_id = f"clm-draft-{uuid4().hex[:6]}"
                draft_claim = {
                    **snap,
                    "id": new_id,
                    "key": f"{snap.get('key', entity_id)}.v{req.source_version}-draft",
                    "title": f"{snap.get('title', entity_id)} {suffix}",
                    "status": "proposed",
                    "version": 1,
                    "origin": origin,
                    "updated_at": now,
                }
                self.claims.insert(0, draft_claim)
                self.state_rev += 1
                self.updated_at = now
                rev = self.state_rev
            self.bus.publish("state.updated", {"state_rev": rev, "reason": "version_cloned_draft", "claim_id": new_id})
            return {"ok": True, "source_version": req.source_version, "draft_claim": draft_claim, "state_rev": rev}

        with self._lock:
            new_id = f"TSK-D{len(self.tasks) + 101}"
            draft_task = {
                **snap,
                "id": new_id,
                "title": f"{snap.get('title', entity_id)} {suffix}",
                "status": "draft",
                "state": "draft",
                "version": 1,
                "origin": origin,
                "updated_at": now,
            }
            self.tasks.insert(0, draft_task)
            self.state_rev += 1
            self.updated_at = now
            rev = self.state_rev
        self.bus.publish("state.updated", {"state_rev": rev, "reason": "version_cloned_draft", "task_id": new_id})
        return {"ok": True, "source_version": req.source_version, "draft_task": draft_task, "state_rev": rev}

    def reindex(self) -> dict[str, Any]:
        self._require_writable()
        job_id = f"reidx-{uuid4().hex[:6]}"
        started = time.perf_counter()
        stages = [
            (20, "scanning", "Scanning repository worktree files"),
            (55, "hashing", "Computing SHA-256 fingerprints and checking quote anchors"),
            (85, "indexing", "Updating symbol index and dependency graph"),
            (100, "complete", "Repository re-index completed"),
        ]
        for pct, stage, message in stages:
            self.bus.publish("reindex.progress", {
                "job_id": job_id,
                "percent": pct,
                "stage": stage,
                "message": message,
            })
        now = utc_now()
        with self._lock:
            self.state_rev += 1
            self.updated_at = now
            rev = self.state_rev
            files_count = len(self.files)
        self.bus.publish("state.updated", {
            "state_rev": rev,
            "reason": "reindex_complete",
            "files_scanned": files_count,
        })
        return {
            "ok": True,
            "job_id": job_id,
            "files_scanned": files_count,
            "changed_files": 0,
            "state_rev": rev,
            "duration_ms": max(15, int((time.perf_counter() - started) * 1000) + 120),
            "timestamp": now,
        }

    def doctor(self) -> dict[str, Any]:
        with self._lock:
            stale_claims = sum(1 for c in self.claims if c["provenance_status"] != "VERIFIED")
            fp = sha256_text("|".join(f"{k}:{self._file_sha(k)}" for k in sorted(self.files)))
            warnings: list[str] = []
            if stale_claims:
                warnings.append(f"{stale_claims} claim(s) have stale file fingerprints or mutated quotes.")
            if not self.llm_configured:
                warnings.append("LLM offline/disabled — Ask runs in Tier 0 FTS verbatim evidence mode.")
            if self.read_only:
                warnings.append("Running in --read-only mode; mutations are disabled.")
            return {
                "status": "WARNING" if warnings else "HEALTHY",
                "backend_mode": "mock",
                "writable": not self.read_only,
                "read_only_reason": "Launched with --read-only safe preview mode." if self.read_only else None,
                "llm_status": {
                    "configured": self.llm_configured,
                    "mode": "enabled" if self.llm_configured else "disabled",
                    "chat_model": "qwen2.5-coder:14b" if self.llm_configured else None,
                    "embedding_model": "nomic-embed-text" if self.llm_configured else None,
                    "fallback": "tier_0_fts_evidence",
                },
                "index_freshness": {
                    "status": "STALE" if stale_claims else "FRESH",
                    "files_indexed": len(self.files),
                    "stale_claims": stale_claims,
                    "last_indexed_at": self.updated_at,
                    "worktree_fingerprint": fp[:16],
                },
                "scaling_thresholds": [
                    {"metric": "Active Records", "current": len(self.claims) + len(self.tasks), "threshold": 10000, "status": "OK", "unit": "rows"},
                    {"metric": "Indexed Files", "current": len(self.files), "threshold": 25000, "status": "OK", "unit": "files"},
                    {"metric": "Graph Nodes", "current": len(self.files) + len(self.claims) * 2 + len(self.tasks), "threshold": 50000, "status": "OK", "unit": "nodes"},
                    {"metric": "Stale Claims Ratio", "current": round(stale_claims / max(1, len(self.claims)) * 100), "threshold": 25, "status": "WARN" if stale_claims else "OK", "unit": "%"},
                ],
                "warnings": warnings,
                "checked_at": utc_now(),
            }

    def export_snapshot(self, req: ExportSnapshotRequest) -> dict[str, Any]:
        if not req.preview:
            self._require_writable()
        now = utc_now()
        with self._lock:
            confirmed_claims = [
                c for c in self.claims
                if c["status"] == "confirmed" and (c.get("origin") or {}).get("origin_evidence") != "degraded"
            ]
            held_proposed = sum(1 for c in self.claims if c["status"] != "confirmed")
            held_degraded = sum(1 for c in self.claims if (c.get("origin") or {}).get("origin_evidence") == "degraded")
            active_plans = [p for p in self.plans if p["status"] == "active"]

            raw_payload = {
                "schema_version": 2,
                "mode": req.mode,
                "project": "cairnmind",
                "exported_at": now,
                "plans": [
                    {"id": p["id"], "title": p["title"], "version": p["version"], "goal": p["goal"]}
                    for p in active_plans
                ],
                "records": [
                    {
                        "id": c["id"],
                        "kind": c["kind"],
                        "key": c["key"],
                        "content": c["content"],
                        "source_path": c["source_path"],
                        "recorded_sha256": c["recorded_sha256"],
                    }
                    for c in confirmed_claims
                ],
            }
            clean_payload, gate = scan_and_redact_payload(raw_payload)
            digest = sha256_text(str(clean_payload))
            return {
                "ok": True,
                "mode": req.mode,
                "preview": req.preview,
                "redaction_gate": gate,
                "summary": {
                    "confirmed_records": len(confirmed_claims),
                    "plans_included": len(active_plans),
                    "held_proposed": held_proposed,
                    "held_degraded": held_degraded,
                    "excluded_transcripts": len(self.sessions),
                },
                "artifact_filename": f"cairnmind-team-snapshot-{digest[:8]}.json",
                "snapshot_sha256": digest,
                "exported_at": now,
                "payload_preview": clean_payload,
            }

    def simulate_conflict(self, req: SimulateConflictRequest) -> dict[str, Any]:
        now = utc_now()
        with self._lock:
            target = next((p for p in self.plans if p["id"] == req.plan_id), None)
            if target is None:
                raise HTTPException(404, f"Plan not found: {req.plan_id}")
            target["version"] += 1
            target["updated_at"] = now
            target["goal"] = target["goal"].rstrip(".") + f" [updated concurrently by {req.client} via MCP]."
            agent_origin = make_origin(
                "MCP_DEGRADED" if req.client == "external" else "ENGINE_VERIFIED",
                "degraded" if req.client == "external" else "verified",
                origin_client=req.client,
                source_label=f"{req.client} (MCP)",
            )
            target["origin"] = agent_origin
            self.version_history.setdefault(f"plan:{req.plan_id}", []).append({
                "version": target["version"],
                "timestamp": now,
                "reason": "mcp_concurrent_edit",
                "actor_type": "agent",
                "actor_label": req.client,
                "origin": deepcopy(agent_origin),
                "audit_entry_id": "aud-9007",
                "summary": f"Concurrent MCP edit by {req.client} bumped plan to v{target['version']}.",
                "snapshot": deepcopy(target),
            })
            self.state_rev += 1
            self.updated_at = now
            rev = self.state_rev
            ver = target["version"]

        self.bus.publish("plan.conflict", {
            "plan_id": req.plan_id,
            "current_version": ver,
            "client": req.client,
            "message": f"{req.client} modified {req.plan_id} to v{ver} via MCP.",
        })
        self.bus.publish("state.updated", {"state_rev": rev, "reason": "mcp_concurrent_edit", "plan_id": req.plan_id})
        return {"ok": True, "plan_id": req.plan_id, "current_version": ver, "state_rev": rev}

    def simulate_file_edit(self, req: SimulateFileEditRequest) -> dict[str, Any]:
        now = utc_now()
        path = req.file_path.replace("\\", "/").strip()
        with self._lock:
            if path not in self.files:
                raise HTTPException(404, f"Mock file not found: {path}")
            old_sha = self._file_sha(path)
            self.files[path] = self.files[path] + f"\n# live edit at {now}\n"
            new_sha = self._file_sha(path)
            impacted_claims: list[str] = []
            for claim in self.claims:
                if claim["source_path"] == path:
                    claim["current_disk_sha256"] = new_sha
                    claim["provenance_status"] = "STALE"
                    if claim["drift_kind"] == "MATCH":
                        claim["drift_kind"] = "FILE_DRIFT_QUOTE_INTACT"
                    claim["updated_at"] = now
                    impacted_claims.append(claim["id"])
            self.state_rev += 1
            self.updated_at = now
            rev = self.state_rev

        self.bus.publish("repo_fingerprint_changed", {
            "file_path": path,
            "old_sha256": old_sha,
            "new_sha256": new_sha,
            "claim_ids": impacted_claims,
            "state_rev": rev,
            "message": f"File {path} changed on disk ({new_sha[:7]}).",
        })
        if impacted_claims:
            self.bus.publish("evidence.stale", {
                "state_rev": rev,
                "claim_ids": impacted_claims,
                "file_path": path,
                "message": f"{len(impacted_claims)} claim(s) marked stale after edit to {path}.",
            })
        return {
            "ok": True,
            "file_path": path,
            "old_sha256": old_sha,
            "new_sha256": new_sha,
            "claim_ids": impacted_claims,
            "state_rev": rev,
        }


class MockUiState(MockCockpitMixin):
    """Stateful deterministic fixture for `?mock=1` so every UI state & action works end-to-end."""

    def __init__(self, bus: UiEventBus, *, read_only: bool = False) -> None:
        self.bus = bus
        self.read_only = read_only
        self.llm_configured = True
        self._lock = threading.Lock()
        self.files = deepcopy(_MOCK_FILES)
        self.state_rev = 42
        self.updated_at = "2026-11-26T12:30:14.412Z"
        self.reverify_count = 0
        self._init_data()
        self._init_cockpit_data()

    def _file_sha(self, path: str) -> str:
        return _sha256_text(self.files[path])

    def _init_data(self) -> None:
        sha_sessions = self._file_sha("src/cairnmind/sessions.py")
        sha_evidence = self._file_sha("src/cairnmind/evidence.py")
        sha_verification = self._file_sha("src/cairnmind/verification.py")
        sha_plans = self._file_sha("src/cairnmind/plans.py")
        sha_mcp = self._file_sha("src/cairnmind/mcp_server.py")

        # Simulate prior hashes for stale files so hash comparisons show realistic SHA-256 drift.
        old_sha_sessions = _sha256_text(self.files["src/cairnmind/sessions.py"] + "\n# rev-39")
        old_sha_verification = _sha256_text(self.files["src/cairnmind/verification.py"] + "\n# rev-37")
        old_sha_mcp = _sha256_text(self.files["src/cairnmind/mcp_server.py"] + "\n# rev-40")

        self.sessions: list[dict[str, Any]] = [
            {
                "id": "sess-claude-9f2a",
                "client": "claude-code",
                "worktree": "/workspace/cairnmind",
                "acknowledged_rev": 42,
                "current_state_rev": 42,
                "sync_status": "SYNCED",
                "packet_id": "pkt-0042-a91f",
                "packet_hash": sha_sessions[:16],
                "last_seen_at": "2026-11-26T12:30:11.204Z",
                "degraded": False,
                "dropped": False,
            },
            {
                "id": "sess-codex-3c81",
                "client": "codex-cli",
                "worktree": "/workspace/cairnmind",
                "acknowledged_rev": 39,
                "current_state_rev": 42,
                "sync_status": "OUTDATED_ACK",
                "packet_id": "pkt-0039-d402",
                "packet_hash": old_sha_sessions[:16],
                "last_seen_at": "2026-11-26T12:26:48.910Z",
                "degraded": False,
                "dropped": False,
            },
            {
                "id": "sess-cursor-7d04",
                "client": "external",
                "worktree": "/workspace/cairnmind",
                "acknowledged_rev": 38,
                "current_state_rev": 42,
                "sync_status": "OUTDATED_ACK",
                "packet_id": "pkt-0038-11be",
                "packet_hash": old_sha_verification[:16],
                "last_seen_at": "2026-11-26T12:19:03.044Z",
                "degraded": True,
                "dropped": False,
            },
        ]

        self.claims: list[dict[str, Any]] = [
            {
                "id": "clm-occ-gate",
                "kind": "constraint",
                "key": "sessions.require_ack.occ_guard",
                "title": "OCC write gate rejects sessions with acknowledged_rev < state_rev",
                "content": "Every shared-memory write must pass SessionManager.require_ack; outdated resume packets raise OCC_STALE_RESUME_PACKET.",
                "status": "confirmed",
                "provenance_status": "STALE",
                "source_path": "src/cairnmind/sessions.py",
                "line_start": 11,
                "line_end": 22,
                "verbatim_quote": "if acknowledged_rev < current_rev:\n            raise ValueError(",
                "recorded_sha256": old_sha_sessions,
                "current_disk_sha256": sha_sessions,
                "quote_survives": True,
                "drift_kind": "FILE_DRIFT_QUOTE_INTACT",
                "updated_at": "2026-11-26T12:24:10.000Z",
                "task_ids": ["TSK-101", "TSK-102"],
                "version": 3,
            },
            {
                "id": "clm-quote-verify",
                "kind": "decision",
                "key": "evidence.verbatim_quote_check",
                "title": "Provenance verification distinguishes hash drift from quote mutation",
                "content": "verify_verbatim_quote separates FILE_DRIFT_QUOTE_INTACT from QUOTE_MUTATED using raw UTF-8 substring matching.",
                "status": "confirmed",
                "provenance_status": "VERIFIED",
                "source_path": "src/cairnmind/evidence.py",
                "line_start": 8,
                "line_end": 18,
                "verbatim_quote": 'if disk_sha256 == recorded_sha256 and quote_intact:\n        return {"status": "VERIFIED", "drift_kind": "MATCH", "disk_sha256": disk_sha256}',
                "recorded_sha256": sha_evidence,
                "current_disk_sha256": sha_evidence,
                "quote_survives": True,
                "drift_kind": "MATCH",
                "updated_at": "2026-11-26T12:29:02.115Z",
                "task_ids": ["TSK-103"],
                "version": 2,
            },
            {
                "id": "clm-test-gate-fp",
                "kind": "constraint",
                "key": "verification.fingerprint_binding",
                "title": "Test gate passes invalidate immediately on worktree fingerprint mismatch",
                "content": "evaluate_test_gate binds passing runs to the exact Git manifest fingerprint and emits STALE_CODE on drift.",
                "status": "confirmed",
                "provenance_status": "STALE",
                "source_path": "src/cairnmind/verification.py",
                "line_start": 8,
                "line_end": 19,
                "verbatim_quote": 'return {"profile": profile_name, "status": "LEGACY_OK"}',
                "recorded_sha256": old_sha_verification,
                "current_disk_sha256": sha_verification,
                "quote_survives": False,
                "drift_kind": "QUOTE_MUTATED",
                "updated_at": "2026-11-26T12:15:40.700Z",
                "task_ids": ["TSK-102", "TSK-104"],
                "version": 4,
            },
            {
                "id": "clm-plan-cas",
                "kind": "decision",
                "key": "plans.compare_and_swap",
                "title": "Plan progress writes require both task and plan CAS versions",
                "content": "compare_and_swap_task rejects stale task versions and plan versions atomically before incrementing.",
                "status": "confirmed",
                "provenance_status": "VERIFIED",
                "source_path": "src/cairnmind/plans.py",
                "line_start": 5,
                "line_end": 12,
                "verbatim_quote": 'if task["plan_version"] != expected_plan_version:\n        raise ValueError("Plan version conflict; read the current plan and retry.")',
                "recorded_sha256": sha_plans,
                "current_disk_sha256": sha_plans,
                "quote_survives": True,
                "drift_kind": "MATCH",
                "updated_at": "2026-11-26T12:28:51.004Z",
                "task_ids": ["TSK-101"],
                "version": 1,
            },
            {
                "id": "clm-mcp-firewall",
                "kind": "fact",
                "key": "mcp_server.write_firewall",
                "title": "MCP write firewall requires verbatim quote and passing test gate",
                "content": "enforce_write_contract rejects unverified remember and task_done calls before store mutation.",
                "status": "proposed",
                "provenance_status": "STALE",
                "source_path": "src/cairnmind/mcp_server.py",
                "line_start": 5,
                "line_end": 13,
                "verbatim_quote": 'if tool_name == "task_done" and not session_state.get("test_gate_passed"):\n        raise ValueError("TEST_GATE_UNVERIFIED")',
                "recorded_sha256": old_sha_mcp,
                "current_disk_sha256": sha_mcp,
                "quote_survives": True,
                "drift_kind": "FILE_DRIFT_QUOTE_INTACT",
                "updated_at": "2026-11-26T12:21:17.390Z",
                "task_ids": ["TSK-104", "TSK-105"],
                "version": 2,
            },
        ]

        self.tasks: list[dict[str, Any]] = [
            {
                "id": "TSK-101",
                "step_key": "occ-session-gate",
                "title": "Enforce OCC resume_packet check across concurrent MCP writers",
                "plan_id": "plan-release-012",
                "plan_title": "v0.12 Concurrency & Provenance Hardening",
                "status": "confirmed",
                "state": "in_progress",
                "version": 5,
                "evidence_verified": 1,
                "evidence_total": 2,
                "evidence_status": "STALE",
                "test_gate": {
                    "profile_name": "occ-concurrency",
                    "status": "STALE_CODE",
                    "sha256_short": old_sha_sessions[:7],
                    "run_id": "chk-8812",
                    "ran_at": "2026-11-26T12:18:04.112Z",
                },
                "updated_at": "2026-11-26T12:28:51.004Z",
                "claim_ids": ["clm-occ-gate", "clm-plan-cas"],
                "source_paths": ["src/cairnmind/sessions.py", "src/cairnmind/plans.py"],
                "depends_on": [],
                "acceptance_criteria": [
                    "Reject writes when acknowledged_rev < state_rev",
                    "Preserve atomic rollback on plan CAS conflict",
                ],
            },
            {
                "id": "TSK-102",
                "step_key": "fingerprint-reverify",
                "title": "Invalidate test gates when tracked worktree SHA-256 drifts",
                "plan_id": "plan-release-012",
                "plan_title": "v0.12 Concurrency & Provenance Hardening",
                "status": "confirmed",
                "state": "needs_test",
                "version": 4,
                "evidence_verified": 0,
                "evidence_total": 2,
                "evidence_status": "STALE",
                "test_gate": {
                    "profile_name": "verification-gate",
                    "status": "STALE_CODE",
                    "sha256_short": old_sha_verification[:7],
                    "run_id": "chk-8809",
                    "ran_at": "2026-11-26T12:14:22.880Z",
                },
                "updated_at": "2026-11-26T12:24:10.000Z",
                "claim_ids": ["clm-occ-gate", "clm-test-gate-fp"],
                "source_paths": ["src/cairnmind/sessions.py", "src/cairnmind/verification.py"],
                "depends_on": ["TSK-101"],
                "acceptance_criteria": [
                    "Mark test gate STALE_CODE on any manifest hash drift",
                    "Detect mutated verbatim quotes in replaced files",
                ],
            },
            {
                "id": "TSK-103",
                "step_key": "quote-provenance-viewer",
                "title": "Render highlighted verbatim quote provenance with SHA-256 diff",
                "plan_id": "plan-release-012",
                "plan_title": "v0.12 Concurrency & Provenance Hardening",
                "status": "confirmed",
                "state": "done",
                "version": 3,
                "evidence_verified": 1,
                "evidence_total": 1,
                "evidence_status": "VERIFIED",
                "test_gate": {
                    "profile_name": "provenance-unit",
                    "status": "PASS",
                    "sha256_short": sha_evidence[:7],
                    "run_id": "chk-8840",
                    "ran_at": "2026-11-26T12:29:18.501Z",
                },
                "updated_at": "2026-11-26T12:29:18.501Z",
                "claim_ids": ["clm-quote-verify"],
                "source_paths": ["src/cairnmind/evidence.py"],
                "depends_on": [],
                "acceptance_criteria": [
                    "Compare recorded_sha256 against current_disk_sha256",
                    "Highlight exact verbatim_quote substring in file snippet",
                ],
            },
            {
                "id": "TSK-104",
                "step_key": "mcp-guardrail-firewall",
                "title": "Block unverified MCP task completions and hallucinated quotes",
                "plan_id": "plan-release-012",
                "plan_title": "v0.12 Concurrency & Provenance Hardening",
                "status": "confirmed",
                "state": "blocked",
                "version": 2,
                "evidence_verified": 0,
                "evidence_total": 2,
                "evidence_status": "STALE",
                "test_gate": {
                    "profile_name": "mcp-guardrails",
                    "status": "FAIL",
                    "sha256_short": sha_mcp[:7],
                    "run_id": "chk-8835",
                    "ran_at": "2026-11-26T12:22:01.003Z",
                },
                "updated_at": "2026-11-26T12:22:01.003Z",
                "claim_ids": ["clm-test-gate-fp", "clm-mcp-firewall"],
                "source_paths": ["src/cairnmind/verification.py", "src/cairnmind/mcp_server.py"],
                "depends_on": ["TSK-102"],
                "acceptance_criteria": [
                    "Reject remember without exact verbatim quote in cited file",
                    "Reject task_done when required test gate is not PASS",
                ],
            },
            {
                "id": "TSK-105",
                "step_key": "blast-radius-inspector",
                "title": "Illuminate downstream blast radius from edited source files",
                "plan_id": "plan-release-012",
                "plan_title": "v0.12 Concurrency & Provenance Hardening",
                "status": "proposed",
                "state": "todo",
                "version": 1,
                "evidence_verified": 0,
                "evidence_total": 1,
                "evidence_status": "STALE",
                "test_gate": {
                    "profile_name": "ui-smoke",
                    "status": "MISSING",
                    "sha256_short": None,
                    "run_id": None,
                    "ran_at": None,
                },
                "updated_at": "2026-11-26T12:21:17.390Z",
                "claim_ids": ["clm-mcp-firewall"],
                "source_paths": ["src/cairnmind/mcp_server.py"],
                "depends_on": ["TSK-103", "TSK-104"],
                "acceptance_criteria": [
                    "Render deterministic 4-column dagre graph",
                    "Highlight downstream evidence, claims, and tasks on click",
                ],
            },
        ]

        self.audit_log: list[dict[str, Any]] = [
            {
                "id": "aud-9007",
                "timestamp": "2026-11-26T12:30:14.412Z",
                "session_id": "sess-codex-3c81",
                "client": "codex-cli",
                "mcp_tool": "plans.save",
                "verdict": "REJECTED",
                "reason_code": "OCC_STALE_RESUME_PACKET",
                "message": "Session acknowledged_rev #39 is behind current state_rev #42.",
                "target_id": "TSK-101",
                "arguments": {
                    "project": "cairnmind-core",
                    "session_id": "sess-codex-3c81",
                    "packet_id": "pkt-0039-d402",
                    "task_id": "TSK-101",
                    "expected_version": 4,
                    "state": "done",
                },
                "violation_detail": {
                    "rule": "OCC_STALE_RESUME_PACKET",
                    "acknowledged_rev": 39,
                    "current_state_rev": 42,
                    "active_packet_id": "pkt-0042-a91f",
                    "remediation": "Call resume_context, inspect state_rev #42 changes, then call ack_resume before retrying.",
                },
            },
            {
                "id": "aud-9006",
                "timestamp": "2026-11-26T12:29:41.087Z",
                "session_id": "sess-cursor-7d04",
                "client": "external",
                "mcp_tool": "memory.remember",
                "verdict": "REJECTED",
                "reason_code": "VERBATIM_QUOTE_NOT_FOUND",
                "message": "Quoted snippet does not exist verbatim in src/cairnmind/verification.py.",
                "target_id": "clm-test-gate-fp",
                "arguments": {
                    "project": "cairnmind-core",
                    "session_id": "sess-cursor-7d04",
                    "kind": "constraint",
                    "key": "verification.fingerprint_binding",
                    "source_path": "src/cairnmind/verification.py",
                    "verbatim_quote": 'return {"profile": profile_name, "status": "LEGACY_OK"}',
                },
                "violation_detail": {
                    "rule": "VERBATIM_QUOTE_NOT_FOUND",
                    "source_path": "src/cairnmind/verification.py",
                    "disk_sha256": sha_verification,
                    "reason": "Verbatim passage missing from current file revision; hallucinated or deleted quote blocked.",
                },
            },
            {
                "id": "aud-9005",
                "timestamp": "2026-11-26T12:29:18.501Z",
                "session_id": "sess-claude-9f2a",
                "client": "claude-code",
                "mcp_tool": "memory.remember",
                "verdict": "ACCEPTED",
                "reason_code": "OK",
                "message": "Verified verbatim quote and matching SHA-256 anchored clm-quote-verify.",
                "target_id": "clm-quote-verify",
                "arguments": {
                    "project": "cairnmind-core",
                    "session_id": "sess-claude-9f2a",
                    "packet_id": "pkt-0042-a91f",
                    "kind": "decision",
                    "key": "evidence.verbatim_quote_check",
                    "source_path": "src/cairnmind/evidence.py",
                },
                "violation_detail": {
                    "rule": "OK",
                    "recorded_sha256": sha_evidence,
                    "state_rev": 42,
                },
            },
            {
                "id": "aud-9004",
                "timestamp": "2026-11-26T12:27:05.622Z",
                "session_id": "sess-claude-9f2a",
                "client": "claude-code",
                "mcp_tool": "task.complete",
                "verdict": "REJECTED",
                "reason_code": "TEST_GATE_UNVERIFIED",
                "message": "Task TSK-104 requires passing check profile 'mcp-guardrails' on current fingerprint.",
                "target_id": "TSK-104",
                "arguments": {
                    "project": "cairnmind-core",
                    "session_id": "sess-claude-9f2a",
                    "packet_id": "pkt-0042-a91f",
                    "task_id": "TSK-104",
                    "state": "done",
                },
                "violation_detail": {
                    "rule": "TEST_GATE_UNVERIFIED",
                    "required_profile": "mcp-guardrails",
                    "last_run_id": "chk-8835",
                    "last_outcome": "FAIL",
                },
            },
            {
                "id": "aud-9003",
                "timestamp": "2026-11-26T12:25:19.104Z",
                "session_id": "sess-codex-3c81",
                "client": "codex-cli",
                "mcp_tool": "checks.attach",
                "verdict": "REJECTED",
                "reason_code": "FILE_FINGERPRINT_MISMATCH",
                "message": "Check run chk-8809 fingerprint differs from current worktree manifest.",
                "target_id": "TSK-102",
                "arguments": {
                    "project": "cairnmind-core",
                    "session_id": "sess-codex-3c81",
                    "task_id": "TSK-102",
                    "check_run_id": "chk-8809",
                },
                "violation_detail": {
                    "rule": "FILE_FINGERPRINT_MISMATCH",
                    "check_fingerprint": old_sha_verification[:16],
                    "current_fingerprint": sha_verification[:16],
                    "drifted_files": ["src/cairnmind/verification.py", "src/cairnmind/sessions.py"],
                },
            },
            {
                "id": "aud-9002",
                "timestamp": "2026-11-26T12:23:44.918Z",
                "session_id": "sess-claude-9f2a",
                "client": "claude-code",
                "mcp_tool": "session.ack_resume",
                "verdict": "ACCEPTED",
                "reason_code": "OK",
                "message": "Session acknowledged resume packet pkt-0042-a91f at state_rev #42.",
                "target_id": "sess-claude-9f2a",
                "arguments": {
                    "session_id": "sess-claude-9f2a",
                    "packet_id": "pkt-0042-a91f",
                    "packet_hash": sha_sessions[:16],
                },
                "violation_detail": {
                    "rule": "OK",
                    "acknowledged_rev": 42,
                },
            },
        ]

    def _build_graph(self) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        seen_nodes: set[str] = set()
        seen_edges: set[str] = set()

        def add_node(node: dict[str, Any]) -> None:
            if node["id"] not in seen_nodes:
                seen_nodes.add(node["id"])
                nodes.append(node)

        def add_edge(source: str, target: str, kind: str, status: str) -> None:
            eid = f"{source}->{target}"
            if eid not in seen_edges:
                seen_edges.add(eid)
                edges.append({
                    "id": eid,
                    "source": source,
                    "target": target,
                    "kind": kind,
                    "status": status,
                })

        # Column 0: Source Files
        file_status: dict[str, str] = {}
        for claim in self.claims:
            path = claim["source_path"]
            cur = file_status.get(path, "VERIFIED")
            if claim["provenance_status"] == "CONFLICT" or claim["drift_kind"] == "QUOTE_MUTATED":
                file_status[path] = "REJECTED"
            elif claim["provenance_status"] == "STALE" and cur != "REJECTED":
                file_status[path] = "STALE"
            else:
                file_status.setdefault(path, "VERIFIED")

        for path in sorted(self.files):
            status = file_status.get(path, "VERIFIED")
            add_node({
                "id": f"file:{path}",
                "column": 0,
                "category": "source_file",
                "label": Path(path).name,
                "subtitle": path,
                "status": status,
                "ref_id": path,
                "meta": {"sha256_short": self._file_sha(path)[:7]},
            })

        # Column 1: Evidence (Quotes & Test Runs) + Column 2: Memory Claims
        for claim in self.claims:
            ev_id = f"ev:quote:{claim['id']}"
            ev_status = "REJECTED" if claim["drift_kind"] == "QUOTE_MUTATED" else claim["provenance_status"]
            add_node({
                "id": ev_id,
                "column": 1,
                "category": "evidence",
                "label": f"Quote L{claim['line_start']}-{claim['line_end']}",
                "subtitle": claim["drift_kind"],
                "status": ev_status,
                "ref_id": claim["id"],
                "meta": {
                    "evidence_type": "quote",
                    "source_path": claim["source_path"],
                    "sha256_short": claim["recorded_sha256"][:7],
                },
            })
            add_edge(f"file:{claim['source_path']}", ev_id, "anchors_quote", ev_status)

            claim_node_id = f"claim:{claim['id']}"
            claim_origin = claim.get("origin") or {}
            add_node({
                "id": claim_node_id,
                "column": 2,
                "category": "claim",
                "label": claim["key"],
                "subtitle": claim["title"],
                "status": claim["provenance_status"],
                "ref_id": claim["id"],
                "meta": {
                    "kind": claim["kind"],
                    "version": claim["version"],
                    "origin_category": claim_origin.get("category", "ENGINE_VERIFIED"),
                    "origin_evidence": claim_origin.get("origin_evidence", "verified"),
                    "origin_client": claim_origin.get("origin_client"),
                },
            })
            add_edge(ev_id, claim_node_id, "supports_claim", ev_status)

            for tid in claim["task_ids"]:
                add_edge(claim_node_id, f"task:{tid}", "informs_task", claim["provenance_status"])

        # Column 1 (Test Runs) + Column 3: Plan Tasks
        for task in self.tasks:
            gate = task["test_gate"]
            gate_status = (
                "VERIFIED" if gate["status"] == "PASS"
                else "STALE" if gate["status"] == "STALE_CODE"
                else "REJECTED" if gate["status"] == "FAIL"
                else "STALE"
            )
            task_status = (
                "REJECTED" if gate["status"] == "FAIL"
                else "STALE" if task["evidence_status"] == "STALE" or gate["status"] in {"STALE_CODE", "MISSING"}
                else "VERIFIED"
            )
            task_node_id = f"task:{task['id']}"
            task_origin = task.get("origin") or {}
            add_node({
                "id": task_node_id,
                "column": 3,
                "category": "task",
                "label": f"{task['id']} · {task['state']}",
                "subtitle": task["title"],
                "status": task_status,
                "ref_id": task["id"],
                "meta": {
                    "profile": gate["profile_name"],
                    "gate_status": gate["status"],
                    "evidence": f"{task['evidence_verified']}/{task['evidence_total']}",
                    "origin_category": task_origin.get("category", "ENGINE_VERIFIED"),
                    "origin_evidence": task_origin.get("origin_evidence", "verified"),
                    "origin_client": task_origin.get("origin_client"),
                },
            })
            if gate.get("run_id"):
                run_ev_id = f"ev:test:{gate['run_id']}"
                add_node({
                    "id": run_ev_id,
                    "column": 1,
                    "category": "evidence",
                    "label": f"Test · {gate['profile_name']}",
                    "subtitle": f"{gate['status']} @ {gate['sha256_short'] or 'none'}",
                    "status": gate_status,
                    "ref_id": task["id"],
                    "meta": {"evidence_type": "test_run", "run_id": gate["run_id"]},
                })
                for src_path in task["source_paths"]:
                    if f"file:{src_path}" in seen_nodes:
                        add_edge(f"file:{src_path}", run_ev_id, "hashed_by_test", gate_status)
                for cid in task["claim_ids"]:
                    add_edge(run_ev_id, f"claim:{cid}", "verifies_claim", gate_status)

        return {"nodes": nodes, "edges": edges}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            fp = _sha256_text("|".join(f"{k}:{self._file_sha(k)}" for k in sorted(self.files)))
            return {
                "project": {
                    "id": "proj-cairnmind-core",
                    "name": "cairnmind",
                    "root": "/workspace/cairnmind",
                    "branch": "main",
                    "head": "86a303f9c4e1",
                    "fingerprint": fp,
                    "mode": "mock",
                },
                "capabilities": self.capabilities(),
                "state_rev": self.state_rev,
                "updated_at": self.updated_at,
                "sessions": deepcopy([s for s in self.sessions if not s["dropped"]]),
                "plans": deepcopy(self.plans),
                "tasks": deepcopy(self.tasks),
                "claims": deepcopy(self.claims),
                "graph": self._build_graph(),
                "audit_log": deepcopy(self.audit_log),
                "drift_summary": self.drift_summary(),
            }

    def file_context(self, path: str, line_start: int = 1, line_end: int = 60) -> dict[str, Any]:
        normalized = path.replace("\\", "/").strip()
        if ".." in Path(normalized).parts or Path(normalized).is_absolute() or _is_git_segment(Path(normalized).parts):
            raise HTTPException(403, "Path must stay inside repository root")
        with self._lock:
            content = self.files.get(normalized)
        if content is None:
            raise HTTPException(404, f"Mock file not found: {normalized}")
        all_lines = content.split("\n")
        total = len(all_lines)
        start = max(1, min(int(line_start), total))
        end = max(start, min(int(line_end), total, start + MAX_CONTEXT_LINES - 1))
        return {
            "path": normalized,
            "line_start": start,
            "line_end": end,
            "total_lines": total,
            "current_disk_sha256": _sha256_text(content),
            "lines": [{"number": idx, "text": all_lines[idx - 1]} for idx in range(start, end + 1)],
        }

    def reverify(self) -> dict[str, Any]:
        self._require_writable()
        now = _utc_now()
        with self._lock:
            self.reverify_count += 1
            self.state_rev += 1
            self.updated_at = now
            # Update session sync statuses against the bumped state_rev.
            for sess in self.sessions:
                if sess["dropped"]:
                    continue
                sess["current_state_rev"] = self.state_rev
                if sess["id"] == "sess-claude-9f2a":
                    sess["acknowledged_rev"] = self.state_rev
                    sess["sync_status"] = "SYNCED"
                    sess["last_seen_at"] = now
                else:
                    sess["sync_status"] = "OUTDATED_ACK"
            stale_ids = [c["id"] for c in self.claims if c["provenance_status"] == "STALE"]
            verified_ids = [c["id"] for c in self.claims if c["provenance_status"] == "VERIFIED"]
            audit_entry = {
                "id": f"aud-{9007 + self.reverify_count}",
                "timestamp": now,
                "session_id": "ui-inspector",
                "client": "cairnmind-ui",
                "mcp_tool": "fingerprints.reverify",
                "verdict": "ACCEPTED",
                "reason_code": "OK",
                "message": (
                    f"Re-verified {len(self.files)} source files: "
                    f"{len(verified_ids)} verified, {len(stale_ids)} stale claims."
                ),
                "target_id": stale_ids[0] if stale_ids else None,
                "arguments": {"mode": "mock", "files_scanned": len(self.files)},
                "violation_detail": {
                    "rule": "OK",
                    "state_rev": self.state_rev,
                    "stale_claims": stale_ids,
                },
                "origin": classify_audit_origin({"client": "cairnmind-ui", "message": "Re-verified source files"}),
            }
            self.audit_log.insert(0, audit_entry)
            rev = self.state_rev

        if stale_ids:
            self.bus.publish("evidence.stale", {
                "state_rev": rev,
                "claim_ids": stale_ids,
                "message": f"{len(stale_ids)} claims remain stale after fingerprint scan.",
            })
        self.bus.publish("state.updated", {
            "state_rev": rev,
            "reason": "reverify",
            "checked_files": len(self.files),
            "stale_claims": len(stale_ids),
        })
        return {
            "ok": True,
            "state_rev": rev,
            "checked_files": len(self.files),
            "stale_claims": len(stale_ids),
            "verified_claims": len(verified_ids),
            "timestamp": now,
        }

    def drop_session(self, session_id: str) -> dict[str, Any]:
        self._require_writable()
        now = _utc_now()
        with self._lock:
            target = next((s for s in self.sessions if s["id"] == session_id and not s["dropped"]), None)
            if target is None:
                raise HTTPException(404, f"Active session not found: {session_id}")
            target["dropped"] = True
            self.state_rev += 1
            self.updated_at = now
            for sess in self.sessions:
                if not sess["dropped"]:
                    sess["current_state_rev"] = self.state_rev
                    if sess["sync_status"] == "SYNCED":
                        sess["acknowledged_rev"] = self.state_rev
            audit_entry = {
                "id": f"aud-drop-{uuid4().hex[:6]}",
                "timestamp": now,
                "session_id": session_id,
                "client": target["client"],
                "mcp_tool": "session.drop",
                "verdict": "ACCEPTED",
                "reason_code": "OK",
                "message": f"Dropped session lock {session_id} (last ack_rev #{target['acknowledged_rev']}).",
                "target_id": session_id,
                "arguments": {"session_id": session_id, "reason": "emergency_ui_drop"},
                "violation_detail": {"rule": "OK", "state_rev": self.state_rev},
                "origin": classify_audit_origin({"client": target["client"], "message": "Dropped session lock"}),
            }
            self.audit_log.insert(0, audit_entry)
            rev = self.state_rev

        self.bus.publish("state.updated", {
            "state_rev": rev,
            "reason": "session_dropped",
            "session_id": session_id,
        })
        return {
            "ok": True,
            "session_id": session_id,
            "dropped": True,
            "state_rev": rev,
            "timestamp": now,
        }
