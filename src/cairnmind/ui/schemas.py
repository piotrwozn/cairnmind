"""Engineer Cockpit request schemas, provenance origin helpers, diffs, and redaction."""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

OriginCategory = Literal["ENGINE_VERIFIED", "HUMAN_REPO", "MCP_DEGRADED"]
OriginEvidence = Literal["verified", "human", "repo_ci", "degraded"]

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("generic_api_token", re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,})\b")),
    ("bearer_secret", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{24,}")),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
    raw_meta = record.get("metadata")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
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
