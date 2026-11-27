"""Engineer Cockpit contracts, provenance helpers, diffs, and Mock/Live cockpit mixins."""
from __future__ import annotations

from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field

from cairnmind.storage_query import (
    asc,
    db_find,
    db_find_one,
    db_insert_document,
    db_update_where,
    desc,
)

OriginCategory = Literal["ENGINE_VERIFIED", "HUMAN_REPO", "MCP_DEGRADED"]
OriginEvidence = Literal["verified", "human", "repo_ci", "degraded"]

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("generic_api_token", re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,})\b")),
    ("bearer_secret", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{24,}")),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_origin(
    category: OriginCategory,
    origin_evidence: OriginEvidence,
    *,
    origin_client: str | None = None,
    source_label: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    default_details: dict[OriginCategory, str] = {
        "ENGINE_VERIFIED": "Directly verified by Cairnmind engine against immutable file hashes and quotes.",
        "HUMAN_REPO": "Authored directly by an engineer in UI or detected from repository files, PRs, or CI.",
        "MCP_DEGRADED": (
            "origin_evidence=degraded: written by an external MCP agent whose conversation history is "
            "unarchived and not directly verified by the engine."
        ),
    }
    return {
        "category": category,
        "origin_evidence": origin_evidence,
        "origin_client": origin_client,
        "source_label": source_label or origin_client or category.lower(),
        "detail": detail or default_details[category],
    }


def classify_record_origin(
    record: dict[str, Any],
    *,
    provenance_status: str = "VERIFIED",
) -> dict[str, Any]:
    """Derive unified `OriginMetadata` from a Cairnmind record or plan row."""
    meta = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    origin_evidence = str(
        record.get("origin_evidence") or meta.get("origin_evidence") or ""
    ).lower()
    origin_client = record.get("origin_client") or meta.get("origin_client")
    origin_source = str(meta.get("origin") or meta.get("source_type") or "").lower()

    if (
        origin_evidence == "degraded"
        or str(origin_client or "").startswith("mcp:")
        or origin_client == "external"
    ):
        return make_origin(
            "MCP_DEGRADED",
            "degraded",
            origin_client=str(origin_client or "external"),
            source_label=f"{origin_client or 'external'} (degraded)",
        )

    if origin_source in {
        "workspace_form",
        "ui_human",
        "human",
        "code_comment",
        "adr",
        "git_trailer",
        "github_pr",
        "ci_scan",
        "local_user_review",
    } or meta.get("confirmation"):
        source_label = origin_source or "human-ui"
        ev: OriginEvidence = (
            "human"
            if origin_source in {"workspace_form", "ui_human", "human", "local_user_review"}
            else "repo_ci"
        )
        return make_origin(
            "HUMAN_REPO",
            ev,
            origin_client=str(origin_client or "human"),
            source_label=source_label,
        )

    if provenance_status == "VERIFIED" or record.get("status") == "confirmed":
        return make_origin(
            "ENGINE_VERIFIED",
            "verified",
            origin_client=str(origin_client or "cairnmind-engine"),
            source_label="cairnmind-engine",
        )

    return make_origin(
        "HUMAN_REPO",
        "repo_ci",
        origin_client=str(origin_client or "repo"),
        source_label=str(origin_client or "repo"),
    )


def classify_audit_origin(entry: dict[str, Any]) -> dict[str, Any]:
    client = str(entry.get("client") or "").lower()
    if client in {"external", "cursor", "cline"} or "degraded" in str(entry.get("message", "")).lower():
        return make_origin(
            "MCP_DEGRADED",
            "degraded",
            origin_client=entry.get("client") or "external",
            source_label=f"{entry.get('client') or 'external'} (degraded)",
        )
    if client in {"cairnmind-ui", "human", "ui-human", "github", "ci"}:
        return make_origin(
            "HUMAN_REPO",
            "human" if "ui" in client or "human" in client else "repo_ci",
            origin_client=entry.get("client") or "cairnmind-ui",
            source_label=entry.get("client") or "cairnmind-ui",
        )
    return make_origin(
        "ENGINE_VERIFIED",
        "verified",
        origin_client=entry.get("client") or "cairnmind-engine",
        source_label=entry.get("client") or "cairnmind-engine",
    )


def compute_snapshot_diff(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Compute a deterministic field-level diff between two version snapshots."""
    left = before or {}
    right = after or {}
    keys = sorted(set(left) | set(right))
    ignore = {"updated_at", "created_at", "file_fingerprints"}
    changes: list[dict[str, Any]] = []
    for key in keys:
        if key in ignore:
            continue
        in_left = key in left
        in_right = key in right
        if not in_left and in_right:
            changes.append({
                "field": key,
                "change_type": "added",
                "before": None,
                "after": deepcopy(right[key]),
            })
        elif in_left and not in_right:
            changes.append({
                "field": key,
                "change_type": "removed",
                "before": deepcopy(left[key]),
                "after": None,
            })
        elif left[key] != right[key]:
            changes.append({
                "field": key,
                "change_type": "modified",
                "before": deepcopy(left[key]),
                "after": deepcopy(right[key]),
            })
    return changes


def scan_and_redact_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run deterministic secret scanning and redaction gate over an export payload."""
    findings: list[dict[str, Any]] = []

    def scrub(value: Any, path: str) -> Any:
        if isinstance(value, str):
            redacted = value
            for rule_name, pattern in SECRET_PATTERNS:
                if pattern.search(redacted):
                    findings.append({
                        "rule": rule_name,
                        "location": path,
                        "action": "REDACTED",
                    })
                    redacted = pattern.sub(f"[REDACTED:{rule_name}]", redacted)
            return redacted
        if isinstance(value, list):
            return [scrub(item, f"{path}[{idx}]") for idx, item in enumerate(value)]
        if isinstance(value, dict):
            return {k: scrub(v, f"{path}.{k}" if path else str(k)) for k, v in value.items()}
        return value

    clean = scrub(deepcopy(payload), "")
    status = "REDACTED" if findings else "PASS"
    gate = {
        "status": status,
        "secrets_scanned": len(SECRET_PATTERNS),
        "secrets_redacted": len(findings),
        "findings": findings,
    }
    return clean, gate


def safe_file_fingerprint(repo_root: Path, relative_path: str) -> tuple[bool, str | None]:
    """Check whether a relative repo path exists safely inside repo_root and return its SHA-256."""
    normalized = relative_path.replace("\\", "/").strip()
    if not normalized or "\0" in normalized:
        return False, None
    rel = Path(normalized)
    if rel.is_absolute() or ".." in rel.parts or any(p.casefold() == ".git" for p in rel.parts):
        return False, None
    root = repo_root.expanduser().resolve()
    target = (root / rel).resolve()
    try:
        inside = target.is_relative_to(root)
    except ValueError:
        inside = False
    if not inside or not target.is_file():
        return False, None
    try:
        return True, hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return False, None


# ---------------------------------------------------------------------------
# Request Schemas for Engineer Cockpit Endpoints
# ---------------------------------------------------------------------------

class UiAskRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    scope: str | None = Field(default=None, max_length=1024)
    verifier_mode: Literal["auto", "strict", "fts_fallback"] = "auto"


class PlanStepInput(BaseModel):
    key: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    depends_on: list[str] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)


class CreatePlanRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    goal: str = Field(default="", max_length=4000)
    status: Literal["draft", "active"] = "draft"
    external_ref: str | None = Field(default=None, max_length=256)
    constraints: list[str] = Field(default_factory=list)
    linked_files: list[str] = Field(default_factory=list)
    steps: list[PlanStepInput] = Field(default_factory=list)
    expected_version: int = Field(default=0, ge=0)


class UpdatePlanRequest(BaseModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    goal: str | None = Field(default=None, max_length=4000)
    external_ref: str | None = Field(default=None, max_length=256)
    constraints: list[str] | None = None
    linked_files: list[str] | None = None
    steps: list[PlanStepInput] | None = None
    status: Literal["draft", "active", "archived"] | None = None


class PromotePlanRequest(BaseModel):
    expected_version: int = Field(ge=1)
    force: bool = False


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    step_key: str | None = Field(default=None, max_length=120)
    plan_id: str | None = Field(default=None, max_length=200)
    external_ref: str | None = Field(default=None, max_length=256)
    scope: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    status: Literal["draft", "proposed", "confirmed"] = "draft"
    expected_version: int = Field(default=0, ge=0)


class CreateFactRequest(BaseModel):
    kind: Literal["fact", "decision", "constraint"] = "fact"
    key: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=4000)
    source_path: str | None = Field(default=None, max_length=1024)
    line_start: int = Field(default=1, ge=1)
    line_end: int = Field(default=20, ge=1)
    verbatim_quote: str = Field(default="", max_length=4000)
    expected_version: int = Field(default=0, ge=0)


class DecisionActionRequest(BaseModel):
    action: Literal["accept", "ignore"]


class CloneVersionRequest(BaseModel):
    source_version: int = Field(ge=1)
    title_suffix: str | None = Field(default=None, max_length=120)


class ExportSnapshotRequest(BaseModel):
    mode: Literal["team"] = "team"
    preview: bool = False


class SimulateConflictRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=200)
    client: str = Field(default="codex-cli", max_length=120)


class SimulateFileEditRequest(BaseModel):
    file_path: str = Field(default="src/cairnmind/sessions.py", max_length=1024)


# ---------------------------------------------------------------------------
# Mock Cockpit Mixin (extends `MockUiState` for full `?mock=1` backend tests)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Live Cockpit Mixin (extends `LiveUiAdapter` against ArcadeDB Store)
# ---------------------------------------------------------------------------

class LiveCockpitMixin:
    """Adds Phases 1-4 Engineer Cockpit endpoints onto `LiveUiAdapter`."""

    store: Any
    settings: Any
    bus: Any
    read_only: bool = False

    def _resolve_project(self, project_arg: str | None) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def _state_rev(self, project_id: str) -> int:  # pragma: no cover
        raise NotImplementedError

    def snapshot(self, project_arg: str | None = None) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def _writable_status(self) -> tuple[bool, str | None]:
        if self.read_only:
            return False, "Launched with --read-only safe preview mode."
        if getattr(self.store, "read_only", False):
            return False, "Store is opened in read-only mode."
        not_writer = getattr(self.store, "not_writer", None)
        if not_writer:
            return False, str(not_writer)
        return True, None

    def _require_writable(self) -> None:
        writable, reason = self._writable_status()
        if not writable:
            raise HTTPException(403, reason or "Read-only mode enabled.")

    def _llm_configured(self) -> bool:
        if self.settings is None:
            return False
        try:
            from cairnmind import knowledge_mode
            return bool(knowledge_mode.enabled(self.settings))
        except Exception:  # noqa: BLE001
            return False

    def capabilities(self) -> dict[str, Any]:
        writable, reason = self._writable_status()
        return {
            "writable": writable,
            "read_only_reason": reason,
            "ask_available": True,
            "llm_configured": self._llm_configured(),
            "multi_project": True,
            "supported_export_modes": ["team"],
        }

    def _project_plans(self, pid: str, root_path: Path, tasks_out: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = db_find(
            self.store,
            "plans",
            {"project_id": pid},
            order_by=[desc("updated_at")],
            limit=50,
        )
        active_plan_id: str | None = None
        with suppress(Exception):
            wt_rows = db_find(
                self.store,
                "worktree_states",
                {"project_id": pid},
                columns=["active_plan_id"],
                limit=5,
            )
            if wt_rows:
                active_plan_id = wt_rows[0].get("active_plan_id")

        tasks_by_plan: dict[str, list[dict[str, Any]]] = {}
        for t in tasks_out:
            tasks_by_plan.setdefault(t["plan_id"], []).append(t)

        out: list[dict[str, Any]] = []
        for row in rows:
            plan_id = row["id"]
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            raw_status = row.get("status") or meta.get("status")
            if raw_status in {"draft", "active", "archived"}:
                plan_status = raw_status
            elif active_plan_id and plan_id == active_plan_id:
                plan_status = "active"
            else:
                plan_status = "active" if not active_plan_id and not out else "draft"

            plan_tasks = tasks_by_plan.get(plan_id, [])
            steps = meta.get("steps") or [
                {
                    "key": t["step_key"],
                    "title": t["title"],
                    "depends_on": list(t["depends_on"]),
                    "scope": list(t["source_paths"]),
                    "acceptance_criteria": list(t["acceptance_criteria"]),
                }
                for t in plan_tasks
            ]
            linked = list(dict.fromkeys(
                list(meta.get("linked_files") or [])
                + [p for t in plan_tasks for p in t.get("source_paths") or []]
            ))
            fps = dict(meta.get("file_fingerprints") or {})
            for path in linked:
                if path not in fps:
                    ok, sha = safe_file_fingerprint(root_path, path)
                    if ok and sha:
                        fps[path] = sha
            out.append({
                "id": plan_id,
                "title": str(row.get("title") or plan_id),
                "goal": str(row.get("goal") or ""),
                "status": plan_status,
                "version": int(row.get("version") or 1),
                "external_ref": meta.get("external_ref") or row.get("external_ref"),
                "constraints": list(row.get("constraints") or []),
                "linked_files": linked,
                "file_fingerprints": fps,
                "steps": steps,
                "origin": classify_record_origin(row, provenance_status="VERIFIED"),
                "created_at": str(row.get("created_at") or utc_now()),
                "updated_at": str(row.get("updated_at") or row.get("created_at") or utc_now()),
            })
        return out

    def ask(self, req: UiAskRequest, project_arg: str | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        root_path = Path(proj["root"]).expanduser().resolve()
        now = utc_now()
        snap = self.snapshot(pid)
        llm_ok = self._llm_configured()
        fallback_mode = req.verifier_mode == "fts_fallback" or not llm_ok

        q_lower = req.query.strip().lower()
        terms = [t for t in re.findall(r"[a-z0-9_]{3,}", q_lower)]
        scored: list[tuple[int, dict[str, Any]]] = []
        for claim in snap["claims"]:
            hay = " ".join([
                claim["key"],
                claim["title"],
                claim["content"],
                claim["source_path"],
                claim["verbatim_quote"],
            ]).lower()
            score = sum(1 for term in terms if term in hay)
            if req.scope and req.scope.lower() in claim["source_path"].lower():
                score += 2
            if score > 0:
                scored.append((score, claim))
        scored.sort(key=lambda item: item[0], reverse=True)

        citations: list[dict[str, Any]] = []
        for _, claim in scored[:5]:
            fp = claim["current_disk_sha256"]
            if claim["source_path"] and not fp:
                _, disk_sha = safe_file_fingerprint(root_path, claim["source_path"])
                fp = disk_sha or claim["recorded_sha256"]
            citations.append({
                "id": f"cit-{claim['id']}",
                "claim_id": claim["id"],
                "file_path": claim["source_path"],
                "line_start": claim["line_start"],
                "line_end": claim["line_end"],
                "fingerprint": fp,
                "verbatim_quote": claim["verbatim_quote"] or claim["content"],
                "origin": claim.get("origin"),
                "stale_now": claim["provenance_status"] != "VERIFIED",
            })

        latency_ms = max(1, int((time.perf_counter() - started) * 1000))
        if not citations:
            return {
                "id": f"ask-{uuid4().hex[:8]}",
                "query": req.query,
                "status": "UNKNOWN",
                "answer": "UNKNOWN — No verified evidence in Cairnmind store matches this query.",
                "citations": [],
                "tier_used": "tier_0_fts_evidence",
                "latency_ms": latency_ms,
                "cached": False,
                "fallback_mode": fallback_mode,
                "fallback_reason": (
                    "LLM not configured — searched Tier 0 FTS evidence only." if fallback_mode else None
                ),
                "unknowns": ["No indexed claims or verbatim quotes matched the query terms."],
                "timestamp": now,
            }

        summary_items = "; ".join(f"{c['key']} ({c['source_path'] or 'memory'})" for _, c in scored[:3])
        prefix = "[Tier 0 FTS Evidence Mode] " if fallback_mode else ""
        return {
            "id": f"ask-{uuid4().hex[:8]}",
            "query": req.query,
            "status": "PARTIAL" if fallback_mode else "VERIFIED",
            "answer": f"{prefix}Matched {len(citations)} evidence-backed record(s): {summary_items}.",
            "citations": citations,
            "tier_used": "tier_0_fts_evidence" if fallback_mode else "tier_1_evidence_synthesis",
            "latency_ms": latency_ms,
            "cached": False,
            "fallback_mode": fallback_mode,
            "fallback_reason": (
                "No LLM configured / offline — showing direct verbatim evidence matches (Tier 0 FTS)."
                if fallback_mode
                else None
            ),
            "unknowns": [],
            "timestamp": now,
        }

    def drift_monitor(
        self,
        *,
        project_arg: str | None = None,
        source: str = "all",
        directory: str | None = None,
        status: str = "all",
        page: int = 1,
        page_size: int = 5,
    ) -> dict[str, Any]:
        snap = self.snapshot(project_arg)
        plans = snap.get("plans") or []
        approved_plan = next((p for p in plans if p["status"] == "active"), plans[0] if plans else None)

        drift_items: list[dict[str, Any]] = []
        for claim in snap["claims"]:
            if claim["provenance_status"] == "VERIFIED":
                continue
            origin = claim.get("origin") or make_origin("HUMAN_REPO", "human")
            drift_items.append({
                "id": f"drift-{claim['id']}",
                "file_path": claim["source_path"] or "unscoped",
                "drift_type": "QUOTE_MUTATED" if claim["drift_kind"] == "QUOTE_MUTATED" else "FILE_MODIFIED",
                "actor": "agent" if origin["category"] == "MCP_DEGRADED" else "human",
                "origin": origin,
                "recorded_sha256": claim["recorded_sha256"],
                "current_sha256": claim["current_disk_sha256"],
                "summary": f"{claim['key']}: {claim['drift_kind']}",
                "impacted_task_ids": list(claim["task_ids"]),
                "impacted_claim_ids": [claim["id"]],
                "detected_at": claim["updated_at"],
            })

        test_freshness: list[dict[str, Any]] = []
        for task in snap["tasks"]:
            gate = task["test_gate"]
            if gate["status"] == "FAIL":
                freshness, reason = "REJECTED", "Test gate failed"
            elif gate["status"] == "PASS" and task["evidence_status"] == "VERIFIED":
                freshness, reason = "FRESH", "Fingerprint matches passing test run"
            else:
                freshness, reason = "STALE", f"{gate['status']} — re-run verification"
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
                "origin": task.get("origin"),
            })

        # Proposed claims detected from code comments / ADRs / Git trailers / CI
        detected: list[dict[str, Any]] = []
        for claim in snap["claims"]:
            key_lower = claim["key"].lower()
            is_detected = (
                claim["status"] == "proposed"
                or key_lower.startswith(("adr:", "comment:", "trailer:", "ci:", "github:"))
            )
            if not is_detected:
                continue
            if key_lower.startswith("github:"):
                src_type = "github"
            elif key_lower.startswith("ci:"):
                src_type = "ci"
            else:
                src_type = "local_file"
            directory_name = (
                str(Path(claim["source_path"]).parent.as_posix()) + "/"
                if claim["source_path"] and "/" in claim["source_path"]
                else "./"
            )
            detected.append({
                "id": claim["id"],
                "title": claim["title"],
                "summary": claim["content"],
                "kind": claim["kind"],
                "source_type": src_type,
                "source_ref": claim["source_path"] or claim["key"],
                "directory": directory_name,
                "file_path": claim["source_path"],
                "line_start": claim["line_start"],
                "line_end": claim["line_end"],
                "verbatim_quote": claim["verbatim_quote"],
                "status": "pending" if claim["status"] == "proposed" else "accepted",
                "detected_at": claim["updated_at"],
                "origin": claim.get("origin") or make_origin("HUMAN_REPO", "repo_ci"),
            })

        directories = sorted({d["directory"] for d in detected})
        source_counts = {
            "local_file": sum(1 for d in detected if d["source_type"] == "local_file"),
            "github": sum(1 for d in detected if d["source_type"] == "github"),
            "ci": sum(1 for d in detected if d["source_type"] == "ci"),
        }
        filtered = detected
        if source and source != "all":
            filtered = [d for d in filtered if d["source_type"] == source]
        if directory:
            filtered = [d for d in filtered if d["directory"].startswith(directory)]
        if status and status != "all":
            filtered = [d for d in filtered if d["status"] == status]

        safe_size = max(1, min(50, int(page_size)))
        total = len(filtered)
        total_pages = max(1, (total + safe_size - 1) // safe_size)
        safe_page = max(1, min(int(page), total_pages))
        start = (safe_page - 1) * safe_size

        return {
            "approved_plan": approved_plan,
            "repo_state": {
                "branch": snap["project"]["branch"],
                "head": snap["project"]["head"],
                "fingerprint": snap["project"]["fingerprint"],
                "checked_at": snap["updated_at"],
                "dirty_files_count": len({d["file_path"] for d in drift_items}),
            },
            "drift_items": drift_items,
            "test_freshness": test_freshness,
            "detected_decisions": {
                "items": filtered[start : start + safe_size],
                "total": total,
                "page": safe_page,
                "page_size": safe_size,
                "total_pages": total_pages,
                "directories": directories,
                "source_counts": source_counts,
            },
        }

    def decide_detected_decision(
        self,
        decision_id: str,
        req: DecisionActionRequest,
        project_arg: str | None = None,
    ) -> dict[str, Any]:
        from cairnmind import person_proof

        self._require_writable()
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        current = db_find_one(self.store, "records", {"id": decision_id, "project_id": pid})
        if not current:
            raise HTTPException(404, f"Detected decision not found: {decision_id}")
        cur_status = str(current.get("status") or "proposed")
        proven = person_proof.present()
        if cur_status in {"confirmed", "disputed"} and not proven:
            raise HTTPException(403, person_proof.PROOF_MISSING)
        now = utc_now()
        meta = dict(current.get("metadata") or {}) if isinstance(current.get("metadata"), dict) else {}
        if req.action == "accept":
            # Only a workspace person_proof may transition a record to confirmed (STABLE_CONTRACT §5).
            next_status = "confirmed" if proven else "proposed"
            meta["ui_triaged"] = "confirmed" if proven else "accepted_proposed"
        else:
            next_status = "superseded"
            meta["ui_triaged"] = "ignored"
        with self.store.transaction() as tx:
            updated = {
                **{k: v for k, v in current.items() if not k.startswith("@")},
                "status": next_status,
                "metadata": meta,
                "version": int(current.get("version") or 1) + 1,
                "updated_at": now,
            }
            db_update_where(tx, "records", set_fields=updated, where={"id": decision_id})
        rev = self._state_rev(pid)
        self.bus.publish("state.updated", {"state_rev": rev, "reason": f"detected_decision_{req.action}", "decision_id": decision_id})
        return {
            "ok": True,
            "decision": {
                "id": decision_id,
                "status": "accepted" if req.action == "accept" else "ignored",
                "record_status": next_status,
                "requires_person_confirmation": req.action == "accept" and not proven,
            },
            "created_claim_id": decision_id if req.action == "accept" else None,
            "state_rev": rev,
        }

    def create_plan(self, req: CreatePlanRequest, project_arg: str | None = None) -> dict[str, Any]:
        self._require_writable()
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        root_path = Path(proj["root"]).expanduser().resolve()
        now = utc_now()
        plan_id = f"plan-{req.status}-{uuid4().hex[:8]}"
        linked = list(dict.fromkeys([f.strip() for f in req.linked_files if f.strip()]))
        fps: dict[str, str] = {}
        for path in linked:
            ok, sha = safe_file_fingerprint(root_path, path)
            if ok and sha:
                fps[path] = sha
        steps = [s.model_dump() for s in req.steps]
        doc = {
            "id": plan_id,
            "project_id": pid,
            "title": req.title.strip(),
            "goal": req.goal.strip(),
            "status": req.status,
            "version": 1,
            "constraints": [c.strip() for c in req.constraints if c.strip()],
            "step_ids": [],
            "source_ids": [],
            "metadata": {
                "origin": "ui_human",
                "status": req.status,
                "external_ref": req.external_ref.strip() if req.external_ref else None,
                "linked_files": linked,
                "file_fingerprints": fps,
                "steps": steps,
            },
            "created_at": now,
            "updated_at": now,
        }
        with self.store.transaction() as tx:
            db_insert_document(tx, "plans", doc)
            with suppress(Exception):
                db_insert_document(tx, "plan_revisions", {
                    "id": f"{plan_id}:1",
                    "project_id": pid,
                    "plan_id": plan_id,
                    "version": 1,
                    "reason": "draft_created" if req.status == "draft" else "plan_created",
                    "snapshot": {"plan": doc, "tasks": []},
                    "recorded_at": now,
                })
        rev = self._state_rev(pid)
        self.bus.publish("state.updated", {"state_rev": rev, "reason": "plan_created", "plan_id": plan_id})
        plan_out = {
            "id": plan_id,
            "title": doc["title"],
            "goal": doc["goal"],
            "status": req.status,
            "version": 1,
            "external_ref": doc["metadata"]["external_ref"],
            "constraints": doc["constraints"],
            "linked_files": linked,
            "file_fingerprints": fps,
            "steps": steps,
            "origin": make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui"),
            "created_at": now,
            "updated_at": now,
        }
        return {"ok": True, "plan": plan_out, "state_rev": rev}

    def update_plan(self, plan_id: str, req: UpdatePlanRequest, project_arg: str | None = None) -> dict[str, Any]:
        self._require_writable()
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        root_path = Path(proj["root"]).expanduser().resolve()
        current = db_find_one(self.store, "plans", {"id": plan_id, "project_id": pid})
        if not current:
            raise HTTPException(404, f"Plan not found: {plan_id}")
        cur_ver = int(current.get("version") or 1)
        meta = dict(current.get("metadata") or {}) if isinstance(current.get("metadata"), dict) else {}

        def project_plan(row: dict[str, Any], row_meta: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": row["id"],
                "title": row.get("title", ""),
                "goal": row.get("goal", ""),
                "status": row.get("status") or row_meta.get("status") or "draft",
                "version": int(row.get("version") or 1),
                "external_ref": row_meta.get("external_ref"),
                "constraints": list(row.get("constraints") or []),
                "linked_files": list(row_meta.get("linked_files") or []),
                "steps": list(row_meta.get("steps") or []),
                "origin": classify_record_origin(row, provenance_status="VERIFIED"),
            }

        if req.expected_version != cur_ver:
            server_view = project_plan(current, meta)
            local_view = deepcopy(server_view)
            if req.title is not None:
                local_view["title"] = req.title.strip()
            if req.goal is not None:
                local_view["goal"] = req.goal.strip()
            if req.steps is not None:
                local_view["steps"] = [s.model_dump() for s in req.steps]
            raise HTTPException(
                409,
                detail={
                    "code": "stale_plan_version",
                    "detail": f"Plan {plan_id} version conflict: expected v{req.expected_version}, current is v{cur_ver}.",
                    "entity_kind": "plan",
                    "entity_id": plan_id,
                    "expected_version": req.expected_version,
                    "current_version": cur_ver,
                    "modified_by": server_view["origin"],
                    "server_snapshot": server_view,
                    "diff": compute_snapshot_diff(local_view, server_view),
                },
            )

        now = utc_now()
        updated = deepcopy(current)
        if req.title is not None:
            updated["title"] = req.title.strip()
        if req.goal is not None:
            updated["goal"] = req.goal.strip()
        if req.constraints is not None:
            updated["constraints"] = [c.strip() for c in req.constraints if c.strip()]
        if req.external_ref is not None:
            meta["external_ref"] = req.external_ref.strip() or None
        if req.linked_files is not None:
            linked = list(dict.fromkeys([f.strip() for f in req.linked_files if f.strip()]))
            meta["linked_files"] = linked
            fps: dict[str, str] = {}
            for path in linked:
                ok, sha = safe_file_fingerprint(root_path, path)
                if ok and sha:
                    fps[path] = sha
            meta["file_fingerprints"] = fps
        if req.steps is not None:
            meta["steps"] = [s.model_dump() for s in req.steps]
        if req.status is not None:
            updated["status"] = req.status
            meta["status"] = req.status
        meta["origin"] = "ui_human"
        updated["metadata"] = meta
        updated["version"] = cur_ver + 1
        updated["updated_at"] = now

        with self.store.transaction() as tx:
            db_update_where(
                tx,
                "plans",
                set_fields={k: v for k, v in updated.items() if not k.startswith("@")},
                where={"id": plan_id},
            )
            with suppress(Exception):
                db_insert_document(tx, "plan_revisions", {
                    "id": f"{plan_id}:{updated['version']}",
                    "project_id": pid,
                    "plan_id": plan_id,
                    "version": updated["version"],
                    "reason": "draft_updated",
                    "snapshot": {"plan": updated, "tasks": []},
                    "recorded_at": now,
                })
        rev = self._state_rev(pid)
        self.bus.publish("state.updated", {"state_rev": rev, "reason": "plan_updated", "plan_id": plan_id})
        return {"ok": True, "plan": project_plan(updated, meta), "state_rev": rev}

    def promote_plan(self, plan_id: str, req: PromotePlanRequest, project_arg: str | None = None) -> dict[str, Any]:
        self._require_writable()
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        root_path = Path(proj["root"]).expanduser().resolve()
        current = db_find_one(self.store, "plans", {"id": plan_id, "project_id": pid})
        if not current:
            raise HTTPException(404, f"Plan not found: {plan_id}")
        cur_ver = int(current.get("version") or 1)
        meta = dict(current.get("metadata") or {}) if isinstance(current.get("metadata"), dict) else {}
        if req.expected_version != cur_ver:
            raise HTTPException(
                409,
                detail={
                    "code": "stale_plan_version",
                    "detail": f"Plan {plan_id} version conflict: expected v{req.expected_version}, current is v{cur_ver}.",
                    "entity_kind": "plan",
                    "entity_id": plan_id,
                    "expected_version": req.expected_version,
                    "current_version": cur_ver,
                    "modified_by": classify_record_origin(current),
                    "server_snapshot": current,
                    "diff": [],
                },
            )
        missing_files: list[str] = []
        drifted_files: list[dict[str, str]] = []
        verified_files: list[str] = []
        rec_fps = meta.get("file_fingerprints") or {}
        for path in meta.get("linked_files") or []:
            ok, sha = safe_file_fingerprint(root_path, path)
            if not ok or not sha:
                missing_files.append(path)
            elif rec_fps.get(path) and rec_fps[path] != sha:
                drifted_files.append({"path": path, "recorded_sha256": rec_fps[path], "current_sha256": sha})
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
                "plan": {"id": plan_id, "version": cur_ver, "status": "draft", "title": current.get("title", "")},
                "state_rev": self._state_rev(pid),
            }
        res = self.update_plan(plan_id, UpdatePlanRequest(expected_version=req.expected_version, status="active"), project_arg)
        return {"ok": True, "requires_confirmation": False, "precheck": precheck, "plan": res["plan"], "state_rev": res["state_rev"]}

    def create_task(self, req: CreateTaskRequest, project_arg: str | None = None) -> dict[str, Any]:
        self._require_writable()
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        now = utc_now()
        task_id = f"task-{uuid4().hex[:8]}"
        step_key = (req.step_key or re.sub(r"[^a-z0-9]+", "-", req.title.lower()).strip("-") or task_id)[:64]
        doc = {
            "id": task_id,
            "project_id": pid,
            "kind": "task",
            "key": step_key,
            "content": req.title.strip(),
            "status": "proposed" if req.status == "draft" else req.status,
            "version": 1,
            "source_ids": [],
            "metadata": {
                "origin": "ui_human",
                "plan_ref": req.plan_id or "",
                "step_key": step_key,
                "state": "draft" if req.status == "draft" else "todo",
                "scope": [p.strip() for p in req.scope if p.strip()],
                "acceptance_criteria": [c.strip() for c in req.acceptance_criteria if c.strip()],
                "external_ref": req.external_ref.strip() if req.external_ref else None,
            },
            "created_at": now,
            "updated_at": now,
        }
        with self.store.transaction() as tx:
            db_insert_document(tx, "records", doc)
        rev = self._state_rev(pid)
        self.bus.publish("state.updated", {"state_rev": rev, "reason": "task_created", "task_id": task_id})
        return {
            "ok": True,
            "task": {
                "id": task_id,
                "step_key": step_key,
                "title": doc["content"],
                "plan_id": req.plan_id or "standalone-drafts",
                "plan_title": "Standalone Quick Tasks",
                "status": req.status,
                "state": doc["metadata"]["state"],
                "version": 1,
                "evidence_verified": 0,
                "evidence_total": max(1, len(req.scope)),
                "evidence_status": "UNVERIFIED",
                "test_gate": {"profile_name": "default", "status": "MISSING", "sha256_short": None, "run_id": None, "ran_at": None},
                "updated_at": now,
                "claim_ids": [],
                "source_paths": doc["metadata"]["scope"],
                "depends_on": [],
                "acceptance_criteria": doc["metadata"]["acceptance_criteria"],
                "origin": make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui"),
                "external_ref": doc["metadata"]["external_ref"],
            },
            "state_rev": rev,
        }

    def create_fact(self, req: CreateFactRequest, project_arg: str | None = None) -> dict[str, Any]:
        from cairnmind import person_proof

        self._require_writable()
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        root_path = Path(proj["root"]).expanduser().resolve()
        now = utc_now()
        claim_id = f"fact-{uuid4().hex[:8]}"
        path = (req.source_path or "").strip()
        disk_sha = ""
        if path:
            ok, sha = safe_file_fingerprint(root_path, path)
            if ok and sha:
                disk_sha = sha
        proven = person_proof.present()
        record_status = "confirmed" if proven else "proposed"
        doc = {
            "id": claim_id,
            "project_id": pid,
            "kind": req.kind,
            "key": req.key.strip(),
            "content": req.content.strip(),
            "status": record_status,
            "version": 1,
            "source_ids": [],
            "metadata": {
                "origin": "ui_human",
                "title": req.title.strip(),
                "source_path": path,
                "line_start": req.line_start,
                "line_end": max(req.line_start, req.line_end),
                "evidence": req.verbatim_quote.strip(),
                "recorded_sha256": disk_sha,
                "requires_person_confirmation": not proven,
            },
            "created_at": now,
            "updated_at": now,
        }
        with self.store.transaction() as tx:
            db_insert_document(tx, "records", doc)
        rev = self._state_rev(pid)
        self.bus.publish("state.updated", {"state_rev": rev, "reason": "fact_created", "claim_id": claim_id})
        return {
            "ok": True,
            "claim": {
                "id": claim_id,
                "kind": req.kind,
                "key": doc["key"],
                "title": req.title.strip(),
                "content": doc["content"],
                "status": record_status,
                "provenance_status": "VERIFIED" if disk_sha else "UNVERIFIED",
                "source_path": path,
                "line_start": req.line_start,
                "line_end": max(req.line_start, req.line_end),
                "verbatim_quote": req.verbatim_quote.strip(),
                "recorded_sha256": disk_sha,
                "current_disk_sha256": disk_sha,
                "quote_survives": True if req.verbatim_quote.strip() else None,
                "drift_kind": "MATCH",
                "updated_at": now,
                "task_ids": [],
                "version": 1,
                "origin": make_origin("HUMAN_REPO", "human", origin_client="ui-human", source_label="human-ui"),
            },
            "state_rev": rev,
        }

    def get_versions(
        self,
        entity_kind: str,
        entity_id: str,
        *,
        from_version: int | None = None,
        to_version: int | None = None,
        project_arg: str | None = None,
    ) -> dict[str, Any]:
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        versions: list[dict[str, Any]] = []
        if entity_kind == "plan":
            rev_rows = db_find(
                self.store,
                "plan_revisions",
                {"project_id": pid, "plan_id": entity_id},
                order_by=[asc("version")],
            )
            for r in rev_rows:
                snap = (r.get("snapshot") or {}).get("plan") or r.get("snapshot") or {}
                origin = classify_record_origin(snap)
                versions.append({
                    "version": int(r.get("version") or 1),
                    "timestamp": str(r.get("recorded_at") or utc_now()),
                    "reason": str(r.get("reason") or "save"),
                    "actor_type": "human" if origin["category"] == "HUMAN_REPO" else "agent",
                    "actor_label": origin["source_label"],
                    "origin": origin,
                    "audit_entry_id": None,
                    "summary": f"Plan revision v{r.get('version', 1)} ({r.get('reason', 'save')}).",
                    "snapshot": snap,
                })
        else:
            rev_rows = db_find(
                self.store,
                "record_revisions",
                {"project_id": pid, "record_id": entity_id},
                order_by=[asc("version")],
            )
            for r in rev_rows:
                snap = r.get("snapshot") or {}
                origin = classify_record_origin(snap)
                versions.append({
                    "version": int(r.get("version") or 1),
                    "timestamp": str(r.get("recorded_at") or snap.get("updated_at") or utc_now()),
                    "reason": "record_revision",
                    "actor_type": "human" if origin["category"] == "HUMAN_REPO" else "agent",
                    "actor_label": origin["source_label"],
                    "origin": origin,
                    "audit_entry_id": None,
                    "summary": f"Revision v{r.get('version', 1)}",
                    "snapshot": snap,
                })

        if not versions:
            snap = self.snapshot(pid)
            pool = snap.get("plans", []) if entity_kind == "plan" else snap["tasks"] if entity_kind == "task" else snap["claims"]
            found = next((item for item in pool if item["id"] == entity_id), None)
            if found is None:
                raise HTTPException(404, f"{entity_kind} not found: {entity_id}")
            origin = found.get("origin") or make_origin("ENGINE_VERIFIED", "verified")
            versions = [{
                "version": int(found.get("version") or 1),
                "timestamp": str(found.get("updated_at") or utc_now()),
                "reason": "current_state",
                "actor_type": "human" if origin["category"] == "HUMAN_REPO" else "agent",
                "actor_label": origin["source_label"],
                "origin": origin,
                "audit_entry_id": None,
                "summary": "Current state.",
                "snapshot": found,
            }]

        versions.sort(key=lambda v: v["version"])
        latest = versions[-1]["version"]
        to_v = to_version or latest
        from_v = from_version or (versions[-2]["version"] if len(versions) >= 2 else latest)
        from_entry = next((v for v in versions if v["version"] == from_v), versions[0])
        to_entry = next((v for v in versions if v["version"] == to_v), versions[-1])
        return {
            "entity_kind": entity_kind,
            "entity_id": entity_id,
            "current_version": latest,
            "from_version": from_entry["version"],
            "to_version": to_entry["version"],
            "versions": versions,
            "diff": compute_snapshot_diff(from_entry["snapshot"], to_entry["snapshot"]),
        }

    def clone_version_as_draft(
        self,
        entity_kind: str,
        entity_id: str,
        req: CloneVersionRequest,
        project_arg: str | None = None,
    ) -> dict[str, Any]:
        self._require_writable()
        history = self.get_versions(entity_kind, entity_id, project_arg=project_arg)
        chosen = next((v for v in history["versions"] if v["version"] == req.source_version), None)
        if chosen is None:
            raise HTTPException(404, f"Version v{req.source_version} not found")
        snap = chosen["snapshot"]
        suffix = req.title_suffix or f"(restored from v{req.source_version})"
        if entity_kind == "plan":
            meta = snap.get("metadata") if isinstance(snap.get("metadata"), dict) else {}
            raw_steps = snap.get("steps") or meta.get("steps") or []
            steps = [
                PlanStepInput(
                    key=str(s.get("key") or f"step-{i}"),
                    title=str(s.get("title") or s.get("content") or f"Step {i}"),
                    depends_on=list(s.get("depends_on") or []),
                    scope=list(s.get("scope") or []),
                    acceptance_criteria=list(s.get("acceptance_criteria") or []),
                )
                for i, s in enumerate(raw_steps, start=1)
                if isinstance(s, dict)
            ]
            created = self.create_plan(
                CreatePlanRequest(
                    title=f"{snap.get('title', entity_id)} {suffix}",
                    goal=str(snap.get("goal") or ""),
                    status="draft",
                    external_ref=snap.get("external_ref") or meta.get("external_ref"),
                    constraints=list(snap.get("constraints") or []),
                    linked_files=list(snap.get("linked_files") or meta.get("linked_files") or []),
                    steps=steps,
                ),
                project_arg=project_arg,
            )
            return {"ok": True, "source_version": req.source_version, "draft_plan": created["plan"], "state_rev": created["state_rev"]}
        if entity_kind == "claim":
            created = self.create_fact(
                CreateFactRequest(
                    kind="decision" if snap.get("kind") == "decision" else "constraint" if snap.get("kind") == "constraint" else "fact",
                    key=f"{snap.get('key', entity_id)}.v{req.source_version}-draft",
                    title=f"{snap.get('title', entity_id)} {suffix}",
                    content=str(snap.get("content") or ""),
                    source_path=snap.get("source_path") or None,
                    verbatim_quote=str(snap.get("verbatim_quote") or ""),
                ),
                project_arg=project_arg,
            )
            return {"ok": True, "source_version": req.source_version, "draft_claim": created["claim"], "state_rev": created["state_rev"]}
        created = self.create_task(
            CreateTaskRequest(
                title=f"{snap.get('title', entity_id)} {suffix}",
                scope=list(snap.get("source_paths") or []),
                acceptance_criteria=list(snap.get("acceptance_criteria") or []),
                status="draft",
            ),
            project_arg=project_arg,
        )
        return {"ok": True, "source_version": req.source_version, "draft_task": created["task"], "state_rev": created["state_rev"]}

    def reindex(self, project_arg: str | None = None) -> dict[str, Any]:
        from cairnmind.ingest import index_project_if_changed

        self._require_writable()
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        job_id = f"reidx-{uuid4().hex[:6]}"
        started = time.perf_counter()
        self.bus.publish("reindex.progress", {"job_id": job_id, "percent": 20, "stage": "scanning", "message": "Scanning repository tree"})
        changed = 0
        with suppress(Exception):
            result = index_project_if_changed(self.store, pid)
            changed = int((result or {}).get("changed", 0))
        self.bus.publish("reindex.progress", {"job_id": job_id, "percent": 75, "stage": "indexing", "message": "Refreshing claims and fingerprints"})
        snap = self.snapshot(pid)
        self.bus.publish("reindex.progress", {"job_id": job_id, "percent": 100, "stage": "complete", "message": "Repository re-index completed"})
        self.bus.publish("state.updated", {"state_rev": snap["state_rev"], "reason": "reindex_complete", "changed_files": changed})
        return {
            "ok": True,
            "job_id": job_id,
            "files_scanned": len({c["source_path"] for c in snap["claims"] if c["source_path"]}),
            "changed_files": changed,
            "state_rev": snap["state_rev"],
            "duration_ms": max(1, int((time.perf_counter() - started) * 1000)),
            "timestamp": utc_now(),
        }

    def doctor(self, project_arg: str | None = None) -> dict[str, Any]:
        snap = self.snapshot(project_arg)
        writable, reason = self._writable_status()
        llm_ok = self._llm_configured()
        stale_claims = sum(1 for c in snap["claims"] if c["provenance_status"] != "VERIFIED")
        backend_mode = "server" if getattr(self.settings, "arcade_connect_url", None) else "arcadedb"
        warnings: list[str] = []
        if not writable and reason:
            warnings.append(reason)
        if not llm_ok:
            warnings.append("LLM disabled/unconfigured — Ask uses Tier 0 FTS verbatim evidence fallback.")
        if stale_claims:
            warnings.append(f"{stale_claims} claim(s) require re-verification.")
        return {
            "status": "WARNING" if warnings else "HEALTHY",
            "backend_mode": backend_mode,
            "writable": writable,
            "read_only_reason": reason,
            "llm_status": {
                "configured": llm_ok,
                "mode": "enabled" if llm_ok else "disabled",
                "chat_model": getattr(self.settings, "chat_model", None) if llm_ok else None,
                "embedding_model": getattr(self.settings, "embedding_model", None) if llm_ok else None,
                "fallback": "tier_0_fts_evidence",
            },
            "index_freshness": {
                "status": "STALE" if stale_claims else "FRESH",
                "files_indexed": len({c["source_path"] for c in snap["claims"] if c["source_path"]}),
                "stale_claims": stale_claims,
                "last_indexed_at": snap["updated_at"],
                "worktree_fingerprint": (snap["project"]["fingerprint"] or "")[:16] or None,
            },
            "scaling_thresholds": [
                {"metric": "Active Records", "current": len(snap["claims"]) + len(snap["tasks"]), "threshold": 10000, "status": "OK", "unit": "rows"},
                {"metric": "Graph Nodes", "current": len(snap["graph"]["nodes"]), "threshold": 50000, "status": "OK", "unit": "nodes"},
                {"metric": "Active Sessions", "current": len(snap["sessions"]), "threshold": 50, "status": "OK", "unit": "sessions"},
            ],
            "warnings": warnings,
            "checked_at": utc_now(),
        }

    def export_snapshot(self, req: ExportSnapshotRequest, project_arg: str | None = None) -> dict[str, Any]:
        if not req.preview:
            self._require_writable()
        snap = self.snapshot(project_arg)
        confirmed = [
            c for c in snap["claims"]
            if c["status"] == "confirmed" and (c.get("origin") or {}).get("origin_evidence") != "degraded"
        ]
        raw_payload = {
            "schema_version": 2,
            "mode": req.mode,
            "project": snap["project"]["name"],
            "exported_at": utc_now(),
            "plans": [p for p in snap.get("plans", []) if p["status"] == "active"],
            "records": [
                {
                    "id": c["id"],
                    "kind": c["kind"],
                    "key": c["key"],
                    "content": c["content"],
                    "source_path": c["source_path"],
                    "recorded_sha256": c["recorded_sha256"],
                }
                for c in confirmed
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
                "confirmed_records": len(confirmed),
                "plans_included": len(raw_payload["plans"]),
                "held_proposed": sum(1 for c in snap["claims"] if c["status"] != "confirmed"),
                "held_degraded": sum(1 for c in snap["claims"] if (c.get("origin") or {}).get("origin_evidence") == "degraded"),
                "excluded_transcripts": len(snap["sessions"]),
            },
            "artifact_filename": f"{snap['project']['name']}-team-snapshot-{digest[:8]}.json",
            "snapshot_sha256": digest,
            "exported_at": raw_payload["exported_at"],
            "payload_preview": clean_payload,
        }
