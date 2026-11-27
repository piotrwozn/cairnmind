"""Local read-mostly State, Provenance & Concurrency Inspector API and static host.

Exposes the `/api/ui/*` endpoints specified in `ui/SPEC.md` and serves the compiled
`ui/dist` bundle on loopback localhost (`cairnmind ui --port 4310`).

All file reads are strictly bounded to the resolved project repository root and never
follow symlinks outside the repository or inspect `.git`.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from cairnmind.storage_query import (
    asc,
    db_count,
    db_find,
    db_find_one,
    db_insert_document,
    db_update_where,
    desc,
    eq,
    is_in,
    ne,
)
from cairnmind.ui_cockpit import (
    CloneVersionRequest,
    CreateFactRequest,
    CreatePlanRequest,
    CreateTaskRequest,
    DecisionActionRequest,
    ExportSnapshotRequest,
    LiveCockpitMixin,
    MockCockpitMixin,
    PromotePlanRequest,
    SimulateConflictRequest,
    SimulateFileEditRequest,
    UiAskRequest,
    UpdatePlanRequest,
    classify_audit_origin,
    classify_record_origin,
)

MAX_CONTEXT_FILE_BYTES = 1_048_576
MAX_CONTEXT_LINES = 400
MAX_BUS_EVENTS = 250

UiEventType = Literal[
    "state.updated",
    "session.ack",
    "evidence.stale",
    "mcp.write.accepted",
    "mcp.write.rejected",
    "repo_fingerprint_changed",
    "reindex.progress",
    "plan.conflict",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_ui_dist(override: Path | None = None) -> Path | None:
    """Locate the compiled Vite bundle (`ui/dist`) beside the repo or installed package."""
    candidates: list[Path] = []
    if override is not None:
        candidates.append(Path(override))
    env_dir = os.environ.get("CAIRNMIND_UI_DIST")
    if env_dir:
        candidates.append(Path(env_dir))
    module_dir = Path(__file__).resolve().parent
    candidates.extend([
        module_dir / "ui_dist",
        module_dir.parent.parent / "ui" / "dist",
        Path.cwd() / "ui" / "dist",
    ])
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if (resolved / "index.html").is_file():
            return resolved
    return None


def _is_git_segment(parts: tuple[str, ...]) -> bool:
    return any(part.casefold() == ".git" for part in parts)


def safe_read_file_context(
    repo_root: str | Path,
    relative_path: str,
    line_start: int = 1,
    line_end: int = 60,
) -> dict[str, Any]:
    """Read a bounded, path-sanitized line window inside `repo_root` with live disk SHA-256."""
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise HTTPException(400, "path is required")
    if "\0" in relative_path:
        raise HTTPException(400, "Invalid path")
    normalized = relative_path.replace("\\", "/").strip()
    candidate_rel = Path(normalized)
    if candidate_rel.is_absolute() or ".." in candidate_rel.parts or _is_git_segment(candidate_rel.parts):
        raise HTTPException(403, "Path must stay inside repository root")
    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(404, "Repository root does not exist")
    target = (root / candidate_rel).resolve()
    try:
        inside = target.is_relative_to(root)
    except ValueError:
        inside = False
    if not inside or _is_git_segment(target.relative_to(root).parts):
        raise HTTPException(403, "Path escapes repository root")
    if not target.is_file():
        raise HTTPException(404, f"File not found: {normalized}")
    try:
        size = target.stat().st_size
    except OSError:
        raise HTTPException(404, "Cannot inspect file") from None
    if size > MAX_CONTEXT_FILE_BYTES:
        raise HTTPException(413, "File exceeds 1 MiB inspection limit")
    try:
        raw = target.read_bytes()
    except OSError:
        raise HTTPException(500, "Cannot read file") from None
    if b"\x00" in raw[:8192]:
        raise HTTPException(415, "Binary files cannot be displayed as quote context")
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    all_lines = text.split("\n")
    total_lines = len(all_lines)
    start = max(1, min(int(line_start), total_lines))
    end = max(start, min(int(line_end), total_lines, start + MAX_CONTEXT_LINES - 1))
    lines = [{"number": idx, "text": all_lines[idx - 1]} for idx in range(start, end + 1)]
    return {
        "path": target.relative_to(root).as_posix(),
        "line_start": start,
        "line_end": end,
        "total_lines": total_lines,
        "current_disk_sha256": _sha256_bytes(raw),
        "lines": lines,
    }


class UiEventBus:
    """In-memory typed SSE fan-out ring buffer for UI events and live MCP audit signals."""

    def __init__(self, maxlen: int = MAX_BUS_EVENTS) -> None:
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._audit: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()

    def publish(self, event_type: UiEventType, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            envelope = {
                "id": f"ui-{self._seq}",
                "seq": self._seq,
                "type": event_type,
                "timestamp": _utc_now(),
                "payload": deepcopy(payload),
            }
            self._events.append(envelope)
            subs = list(self._subscribers)
        for loop, queue in subs:
            if not loop.is_closed():
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._enqueue, queue, envelope)
        return envelope

    @staticmethod
    def _enqueue(queue: asyncio.Queue, envelope: dict[str, Any]) -> None:
        if queue.full():
            with suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with suppress(asyncio.QueueFull):
            queue.put_nowait(envelope)

    def record_audit(self, entry: dict[str, Any]) -> dict[str, Any]:
        item = {
            "id": entry.get("id") or f"aud-{uuid4().hex[:8]}",
            "timestamp": entry.get("timestamp") or _utc_now(),
            **entry,
        }
        with self._lock:
            self._audit.appendleft(item)
        event_type: UiEventType = (
            "mcp.write.accepted" if item.get("verdict") == "ACCEPTED" else "mcp.write.rejected"
        )
        self.publish(event_type, {
            "audit_id": item["id"],
            "session_id": item.get("session_id"),
            "client": item.get("client"),
            "mcp_tool": item.get("mcp_tool"),
            "verdict": item.get("verdict"),
            "reason_code": item.get("reason_code"),
            "message": item.get("message"),
            "target_id": item.get("target_id"),
            "entry": item,
        })
        return item

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._audit)[:limit]

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:]

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        with self._lock:
            self._subscribers.add((loop, queue))
        return loop, queue

    def unsubscribe(self, token: tuple[asyncio.AbstractEventLoop, asyncio.Queue]) -> None:
        with self._lock:
            self._subscribers.discard(token)


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


# ---------------------------------------------------------------------------
# Live Cairnmind Store Adapter
# ---------------------------------------------------------------------------

def _classify_reason_code(message: str) -> str:
    lower = message.lower()
    if any(tok in lower for tok in ("resume_required", "ack", "packet", "conflict", "stale")):
        return "OCC_STALE_RESUME_PACKET"
    if "quote" in lower or "evidence" in lower:
        return "VERBATIM_QUOTE_NOT_FOUND"
    if "fingerprint" in lower or "hash" in lower:
        return "FILE_FINGERPRINT_MISMATCH"
    if "test" in lower or "check" in lower or "verification" in lower:
        return "TEST_GATE_UNVERIFIED"
    return "OCC_STALE_RESUME_PACKET"


def _find_quote_lines(file_path: Path, quote: str) -> tuple[int, int]:
    if not quote or not file_path.is_file():
        return 1, 25
    try:
        if file_path.stat().st_size > MAX_CONTEXT_FILE_BYTES:
            return 1, 25
        text = file_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    except OSError:
        return 1, 25
    idx = text.find(quote.replace("\r\n", "\n"))
    if idx < 0:
        return 1, min(30, max(1, text.count("\n") + 1))
    start = text[:idx].count("\n") + 1
    end = start + quote.replace("\r\n", "\n").count("\n")
    return start, max(start, end)


class LiveUiAdapter(LiveCockpitMixin):
    """Projects Cairnmind's ArcadeDB Store into Engineer Cockpit contracts."""

    def __init__(
        self,
        store: Any,
        settings: Any,
        bus: UiEventBus,
        default_project: str | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        self.store = store
        self.settings = settings
        self.bus = bus
        self.default_project = default_project
        self.read_only = read_only

    def _resolve_project(self, project_arg: str | None) -> dict[str, Any]:
        selected = project_arg or self.default_project
        if selected:
            try:
                return self.store.resolve_project(selected)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from None
        row = db_find_one(self.store, "projects", order_by=[desc("updated_at"), asc("id")])
        if not row:
            raise HTTPException(404, "No projects registered in Cairnmind store. Run `cairnmind ui --mock` to preview.")
        return row

    def _state_rev(self, project_id: str) -> int:
        from cairnmind.journal import Journal
        try:
            cursor = Journal(self.store).cursor()
            seq = int(cursor.get("seq", 0))
            if seq > 0:
                return seq
        except Exception:  # noqa: BLE001
            pass
        return db_count(self.store, "records", {"project_id": project_id})

    def snapshot(self, project_arg: str | None = None) -> dict[str, Any]:
        from cairnmind.evidence import evidence_states
        from cairnmind.verification import capture_code_state, verification_status

        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        root_path = Path(proj["root"]).expanduser().resolve()
        state_rev = self._state_rev(pid)
        now = _utc_now()

        code = {"branch": None, "head": None, "fingerprint": None}
        with suppress(Exception):
            code = capture_code_state(self.store, pid, str(root_path))

        # 1. Sessions & OCC sync status
        session_rows = db_find(
            self.store,
            "sessions",
            {"project_id": pid},
            order_by=[desc("updated_at")],
            limit=50,
        )
        ack_rows = db_find(
            self.store,
            "resume_acks",
            {"project_id": pid},
            order_by=[desc("recorded_at")],
            limit=200,
        )
        latest_ack_by_session: dict[str, dict[str, Any]] = {}
        for row in ack_rows:
            sid = row.get("session_id")
            if isinstance(sid, str) and sid not in latest_ack_by_session:
                latest_ack_by_session[sid] = row

        sessions_out: list[dict[str, Any]] = []
        for s in session_rows:
            if s.get("status") == "dropped":
                continue
            sid = s["id"]
            ack = latest_ack_by_session.get(sid)
            active_pkt = s.get("active_packet_id")
            acked_pkt = s.get("acknowledged_packet_id") or (ack.get("packet_id") if ack else None)
            synced = bool(active_pkt and acked_pkt and active_pkt == acked_pkt)
            ack_rev = state_rev if synced else max(0, state_rev - 1)
            sessions_out.append({
                "id": sid,
                "client": str(s.get("client") or "external"),
                "worktree": str(s.get("worktree") or proj["root"]),
                "acknowledged_rev": ack_rev,
                "current_state_rev": state_rev,
                "sync_status": "SYNCED" if synced else "OUTDATED_ACK",
                "packet_id": acked_pkt or active_pkt or "",
                "packet_hash": str((ack or {}).get("packet_hash") or "")[:16],
                "last_seen_at": str(s.get("updated_at") or s.get("created_at") or now),
                "degraded": s.get("client") == "external",
                "dropped": False,
            })

        # 2. Records, Sources & Verbatim Quote Provenance
        all_records = db_find(
            self.store,
            "records",
            [eq("project_id", pid), ne("status", "superseded")],
            order_by=[desc("updated_at")],
            limit=300,
        )
        task_records = [r for r in all_records if r.get("kind") == "task"]
        claim_records = [r for r in all_records if r.get("kind") != "task"]
        states = evidence_states(self.store, pid, all_records, quotes=True) if all_records else {}

        source_ids = sorted({
            sid for r in all_records for sid in (r.get("source_ids") or []) if isinstance(sid, str)
        })
        source_rows = db_find(
            self.store,
            "sources",
            [is_in("id", source_ids)],
            columns=["id", "uri", "kind", "sha256", "is_current", "metadata"],
        ) if source_ids else []
        sources_by_id = {row["id"]: row for row in source_rows}

        plans = db_find(
            self.store,
            "plans",
            {"project_id": pid},
            order_by=[desc("updated_at")],
            limit=20,
        )
        plans_by_id = {p["id"]: p for p in plans}

        check_runs = db_find(
            self.store,
            "check_runs",
            {"project_id": pid},
            order_by=[desc("ended_at")],
            limit=200,
        )
        checks_by_task: dict[str, list[dict[str, Any]]] = {}
        for chk in check_runs:
            tid = chk.get("task_id")
            if isinstance(tid, str):
                checks_by_task.setdefault(tid, []).append(chk)

        claims_out: list[dict[str, Any]] = []
        claims_by_source_path: dict[str, list[str]] = {}
        for rec in claim_records:
            st = states.get(rec["id"], {})
            ev_state = st.get("evidence_state", "unverified")
            meta = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
            quote = str(meta.get("evidence") or "")
            file_src = next(
                (sources_by_id[sid] for sid in (rec.get("source_ids") or [])
                 if sid in sources_by_id and sources_by_id[sid].get("kind") in {"code", "document", "file"}),
                None,
            )
            rel_path = ""
            recorded_sha = ""
            disk_sha = ""
            line_start, line_end = 1, 20
            if file_src:
                smeta = file_src.get("metadata") if isinstance(file_src.get("metadata"), dict) else {}
                raw_path = smeta.get("path") or file_src.get("uri") or ""
                try:
                    p = Path(raw_path)
                    rel_path = p.resolve().relative_to(root_path).as_posix() if p.is_absolute() else Path(raw_path).as_posix()
                except (OSError, ValueError):
                    rel_path = str(raw_path)
                recorded_sha = str(file_src.get("sha256") or "")
                disk_file = root_path / rel_path
                if disk_file.is_file():
                    with suppress(OSError):
                        disk_sha = _sha256_bytes(disk_file.read_bytes())
                    line_start, line_end = _find_quote_lines(disk_file, quote)

            quote_survives = st.get("quote_survives")
            if ev_state == "anchored" and (not disk_sha or not recorded_sha or disk_sha == recorded_sha):
                prov_status = "VERIFIED"
                drift_kind = "MATCH"
            elif not disk_sha and rel_path:
                prov_status = "STALE"
                drift_kind = "FILE_MISSING"
            elif quote_survives is False:
                prov_status = "STALE"
                drift_kind = "QUOTE_MUTATED"
            elif ev_state in {"evidence_replaced", "orphaned"} or (disk_sha and recorded_sha and disk_sha != recorded_sha):
                prov_status = "STALE"
                drift_kind = "FILE_DRIFT_QUOTE_INTACT" if quote_survives is not False else "QUOTE_MUTATED"
            else:
                prov_status = "UNVERIFIED"
                drift_kind = "MATCH"

            claims_out.append({
                "id": rec["id"],
                "kind": str(rec.get("kind") or "fact"),
                "key": str(rec.get("key") or rec["id"]),
                "title": str(rec.get("content") or "")[:120],
                "content": str(rec.get("content") or ""),
                "status": str(rec.get("status") or "proposed"),
                "provenance_status": prov_status,
                "source_path": rel_path,
                "line_start": line_start,
                "line_end": line_end,
                "verbatim_quote": quote,
                "recorded_sha256": recorded_sha,
                "current_disk_sha256": disk_sha,
                "quote_survives": quote_survives,
                "drift_kind": drift_kind,
                "updated_at": str(rec.get("updated_at") or rec.get("created_at") or now),
                "task_ids": [],
                "version": int(rec.get("version") or 1),
                "origin": classify_record_origin(rec, provenance_status=prov_status),
            })
            if rel_path:
                claims_by_source_path.setdefault(rel_path, []).append(rec["id"])

        # 3. Tasks & Test Gates
        tasks_out: list[dict[str, Any]] = []
        for task in task_records:
            meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
            plan_id = meta.get("plan_ref") or ""
            plan = plans_by_id.get(plan_id)
            task_checks = checks_by_task.get(task["id"], [])
            latest_check = task_checks[0] if task_checks else None
            ver = None
            with suppress(Exception):
                ver = verification_status(self.store, task, str(root_path))

            gate_status = "MISSING"
            profile_name = (meta.get("required_profiles") or ["default"])[0]
            sha_short = None
            run_id = None
            ran_at = None
            if latest_check:
                profile_name = str(latest_check.get("profile_id") or profile_name)
                run_id = latest_check.get("id")
                ran_at = latest_check.get("ended_at")
                fp_val = str(latest_check.get("profile_hash") or latest_check.get("task_definition_hash") or "")
                sha_short = fp_val[:7] if fp_val else None
                if ver and ver.get("stale"):
                    gate_status = "STALE_CODE"
                elif latest_check.get("passed") is True:
                    gate_status = "PASS"
                else:
                    gate_status = "FAIL"

            # Link claims touching overlapping scope/source paths
            scope_paths = [str(p) for p in (meta.get("scope") or []) if isinstance(p, str)]
            linked_claims: list[str] = []
            for path_key, cids in claims_by_source_path.items():
                if any(path_key == sp or path_key.startswith(sp.rstrip("/") + "/") for sp in scope_paths):
                    linked_claims.extend(cids)
            if not linked_claims:
                st = states.get(task["id"], {})
                ev_total = max(1, len(task.get("source_ids") or []))
                ev_verified = ev_total if st.get("evidence_state") == "anchored" else 0
            else:
                ev_total = len(linked_claims)
                claim_map = {c["id"]: c for c in claims_out}
                ev_verified = sum(1 for cid in linked_claims if claim_map[cid]["provenance_status"] == "VERIFIED")
                for cid in linked_claims:
                    claim_map[cid]["task_ids"].append(task["id"])

            ev_status = "VERIFIED" if ev_total > 0 and ev_verified == ev_total else "STALE"
            tasks_out.append({
                "id": task["id"],
                "step_key": str(meta.get("step_key") or task.get("key") or task["id"]),
                "title": str(task.get("content") or "")[:160],
                "plan_id": str(plan_id),
                "plan_title": str(plan.get("title") if plan else "Standalone Tasks"),
                "status": str(task.get("status") or "proposed"),
                "state": str(meta.get("state") or "todo"),
                "version": int(task.get("version") or 1),
                "evidence_verified": ev_verified,
                "evidence_total": ev_total,
                "evidence_status": ev_status,
                "test_gate": {
                    "profile_name": profile_name,
                    "status": gate_status,
                    "sha256_short": sha_short,
                    "run_id": run_id,
                    "ran_at": ran_at,
                },
                "updated_at": str(task.get("updated_at") or task.get("created_at") or now),
                "claim_ids": linked_claims,
                "source_paths": scope_paths,
                "depends_on": list(meta.get("depends_on") or []),
                "acceptance_criteria": list(meta.get("acceptance_criteria") or []),
                "origin": classify_record_origin(task, provenance_status=ev_status),
                "external_ref": meta.get("external_ref"),
            })

        # 4. Graph (4 deterministic columns)
        graph = _build_graph_from_entities(claims_out, tasks_out)

        # 5. Audit log (in-memory MCP bus + stored session_events)
        audit_entries = list(self.bus.recent_audit(100))
        if len(audit_entries) < 50:
            sess_events = db_find(
                self.store,
                "session_events",
                {"project_id": pid},
                order_by=[desc("recorded_at")],
                limit=50,
            )
            seen_ids = {e["id"] for e in audit_entries}
            for ev in sess_events:
                eid = str(ev.get("id") or f"sev-{uuid4().hex[:8]}")
                if eid in seen_ids:
                    continue
                kind = str(ev.get("kind") or "session_event")
                rejected = "refus" in kind or "reject" in kind or "stale" in kind
                msg = str(ev.get("detail") or ev.get("message") or kind)
                audit_entries.append({
                    "id": eid,
                    "timestamp": str(ev.get("recorded_at") or now),
                    "session_id": str(ev.get("session_id") or "local"),
                    "client": str(ev.get("client") or "mcp"),
                    "mcp_tool": kind,
                    "verdict": "REJECTED" if rejected else "ACCEPTED",
                    "reason_code": _classify_reason_code(msg) if rejected else "OK",
                    "message": msg,
                    "target_id": ev.get("session_id"),
                    "arguments": ev.get("payload") if isinstance(ev.get("payload"), dict) else {"kind": kind},
                    "violation_detail": {"rule": _classify_reason_code(msg) if rejected else "OK", "raw": ev},
                    "origin": classify_audit_origin({"client": ev.get("client") or "mcp", "message": msg}),
                })
        for entry in audit_entries:
            entry.setdefault("origin", classify_audit_origin(entry))
        audit_entries.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)

        plans_out = self._project_plans(pid, root_path, tasks_out)
        active_plan = next((p for p in plans_out if p["status"] == "active"), None)
        drifted_files = {c["source_path"] for c in claims_out if c["provenance_status"] != "VERIFIED" and c["source_path"]}
        stale_tests = sum(1 for t in tasks_out if t["test_gate"]["status"] != "PASS")
        pending_decisions = sum(1 for c in claims_out if c["status"] == "proposed")

        return {
            "project": {
                "id": pid,
                "name": str(proj.get("name") or pid),
                "root": str(root_path),
                "branch": code.get("branch"),
                "head": (code.get("head") or "")[:12] or None,
                "fingerprint": code.get("fingerprint"),
                "mode": "live",
            },
            "capabilities": self.capabilities(),
            "state_rev": state_rev,
            "updated_at": now,
            "sessions": sessions_out,
            "plans": plans_out,
            "tasks": tasks_out,
            "claims": claims_out,
            "graph": graph,
            "audit_log": audit_entries[:100],
            "drift_summary": {
                "drifted_files_count": len(drifted_files),
                "stale_tests_count": stale_tests,
                "pending_decisions_count": pending_decisions,
                "last_approved_plan_id": active_plan["id"] if active_plan else None,
                "last_approved_plan_version": active_plan["version"] if active_plan else None,
            },
        }

    def file_context(self, path: str, line_start: int, line_end: int, project_arg: str | None = None) -> dict[str, Any]:
        proj = self._resolve_project(project_arg)
        return safe_read_file_context(proj["root"], path, line_start=line_start, line_end=line_end)

    def reverify(self, project_arg: str | None = None) -> dict[str, Any]:
        from cairnmind.ingest import index_project_if_changed

        self._require_writable()
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        with suppress(Exception):
            index_project_if_changed(self.store, pid)
        snap = self.snapshot(pid)
        stale = [c["id"] for c in snap["claims"] if c["provenance_status"] == "STALE"]
        verified = [c["id"] for c in snap["claims"] if c["provenance_status"] == "VERIFIED"]
        now = _utc_now()
        if stale:
            self.bus.publish("evidence.stale", {
                "state_rev": snap["state_rev"],
                "claim_ids": stale,
                "message": f"{len(stale)} stale claims detected during fingerprint re-verification.",
            })
        self.bus.publish("state.updated", {
            "state_rev": snap["state_rev"],
            "reason": "reverify",
            "stale_claims": len(stale),
        })
        return {
            "ok": True,
            "state_rev": snap["state_rev"],
            "checked_files": len({c["source_path"] for c in snap["claims"] if c["source_path"]}),
            "stale_claims": len(stale),
            "verified_claims": len(verified),
            "timestamp": now,
        }

    def drop_session(self, session_id: str, project_arg: str | None = None) -> dict[str, Any]:
        self._require_writable()
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        row = db_find_one(self.store, "sessions", {"id": session_id, "project_id": pid})
        if not row:
            raise HTTPException(404, f"Active session not found: {session_id}")
        now = _utc_now()
        with self.store.transaction() as tx:
            updated = {
                **{k: v for k, v in row.items() if not k.startswith("@")},
                "status": "dropped",
                "active_packet_id": None,
                "updated_at": now,
            }
            db_update_where(tx, "sessions", set_fields=updated, where={"id": session_id})
            db_insert_document(tx, "session_events", {
                "id": str(uuid4()),
                "project_id": pid,
                "session_id": session_id,
                "kind": "session_dropped_ui",
                "detail": "Emergency session lock drop from Cairnmind UI",
                "recorded_at": now,
            })
        rev = self._state_rev(pid)
        self.bus.record_audit({
            "session_id": session_id,
            "client": str(row.get("client") or "mcp"),
            "mcp_tool": "session.drop",
            "verdict": "ACCEPTED",
            "reason_code": "OK",
            "message": f"Dropped session lock {session_id} via UI.",
            "target_id": session_id,
            "arguments": {"session_id": session_id},
            "violation_detail": {"rule": "OK", "state_rev": rev},
        })
        self.bus.publish("state.updated", {"state_rev": rev, "reason": "session_dropped", "session_id": session_id})
        return {"ok": True, "session_id": session_id, "dropped": True, "state_rev": rev, "timestamp": now}


def _build_graph_from_entities(claims: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> dict[str, Any]:
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
            edges.append({"id": eid, "source": source, "target": target, "kind": kind, "status": status})

    for claim in claims:
        path = claim.get("source_path")
        ev_status = "REJECTED" if claim.get("drift_kind") == "QUOTE_MUTATED" else claim.get("provenance_status", "VERIFIED")
        if path:
            add_node({
                "id": f"file:{path}",
                "column": 0,
                "category": "source_file",
                "label": Path(path).name,
                "subtitle": path,
                "status": ev_status,
                "ref_id": path,
                "meta": {"sha256_short": (claim.get("current_disk_sha256") or "")[:7]},
            })
        ev_id = f"ev:quote:{claim['id']}"
        add_node({
            "id": ev_id,
            "column": 1,
            "category": "evidence",
            "label": f"Quote L{claim['line_start']}-{claim['line_end']}",
            "subtitle": claim.get("drift_kind", "MATCH"),
            "status": ev_status,
            "ref_id": claim["id"],
            "meta": {"evidence_type": "quote", "source_path": path or ""},
        })
        if path:
            add_edge(f"file:{path}", ev_id, "anchors_quote", ev_status)
        claim_nid = f"claim:{claim['id']}"
        claim_origin = claim.get("origin") or {}
        add_node({
            "id": claim_nid,
            "column": 2,
            "category": "claim",
            "label": claim["key"],
            "subtitle": claim["title"],
            "status": claim.get("provenance_status", "VERIFIED"),
            "ref_id": claim["id"],
            "meta": {
                "kind": claim["kind"],
                "version": claim["version"],
                "origin_category": claim_origin.get("category", "ENGINE_VERIFIED"),
                "origin_evidence": claim_origin.get("origin_evidence", "verified"),
                "origin_client": claim_origin.get("origin_client"),
            },
        })
        add_edge(ev_id, claim_nid, "supports_claim", ev_status)

    for task in tasks:
        gate = task["test_gate"]
        task_status = (
            "REJECTED" if gate["status"] == "FAIL"
            else "STALE" if task["evidence_status"] == "STALE" or gate["status"] in {"STALE_CODE", "MISSING"}
            else "VERIFIED"
        )
        tnid = f"task:{task['id']}"
        task_origin = task.get("origin") or {}
        add_node({
            "id": tnid,
            "column": 3,
            "category": "task",
            "label": f"{task['id']} · {task['state']}",
            "subtitle": task["title"],
            "status": task_status,
            "ref_id": task["id"],
            "meta": {
                "profile": gate["profile_name"],
                "gate_status": gate["status"],
                "origin_category": task_origin.get("category", "ENGINE_VERIFIED"),
                "origin_evidence": task_origin.get("origin_evidence", "verified"),
                "origin_client": task_origin.get("origin_client"),
            },
        })
        for cid in task.get("claim_ids") or []:
            if f"claim:{cid}" in seen_nodes:
                add_edge(f"claim:{cid}", tnid, "informs_task", task_status)

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# FastAPI Router & App Factory
# ---------------------------------------------------------------------------

def _bind_cockpit_routes(
    router: APIRouter,
    *,
    event_bus: UiEventBus,
    mock_state: MockUiState,
    live_adapter: LiveUiAdapter | None,
    default_mock: bool,
    project_path_param: bool = False,
) -> None:
    """Register all Engineer Cockpit REST + SSE routes on `router` (with optional `{project_id}` prefix)."""

    def _use_mock(mock: int | bool | None) -> bool:
        if mock is not None:
            return bool(mock)
        return default_mock or live_adapter is None

    def _pick_project(project_id: str | None, project: str | None) -> str | None:
        return project_id or project

    p = "/projects/{project_id}" if project_path_param else ""

    @router.get(f"{p}/snapshot")
    async def ui_snapshot(
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.snapshot()
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.snapshot, _pick_project(project_id, project))

    @router.get(f"{p}/file-context")
    async def ui_file_context(
        path: str = Query(min_length=1, max_length=2048),
        lineStart: int = Query(default=1, ge=1),
        lineEnd: int = Query(default=60, ge=1),
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.file_context(path, lineStart, lineEnd)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.file_context, path, lineStart, lineEnd, _pick_project(project_id, project)
        )

    @router.get(f"{p}/events")
    async def ui_events(
        request: Request,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
        once: bool = Query(default=False),
    ):
        use_mock = _use_mock(mock)
        selected_project = _pick_project(project_id, project)

        async def stream():
            token = event_bus.subscribe()
            _, queue = token
            try:
                snap = (
                    mock_state.snapshot()
                    if use_mock
                    else await asyncio.to_thread(live_adapter.snapshot, selected_project)
                )
                init_payload = {
                    "id": "ui-init",
                    "type": "state.updated",
                    "timestamp": _utc_now(),
                    "payload": {
                        "state_rev": snap["state_rev"],
                        "mode": snap["project"]["mode"],
                        "reason": "connected",
                    },
                }
                yield f"event: state.updated\nid: ui-init\ndata: {json.dumps(init_payload)}\n\n"
                if once:
                    return
                last_heartbeat = time.monotonic()
                while not await request.is_disconnected():
                    try:
                        envelope = await asyncio.wait_for(queue.get(), timeout=1.0)
                        yield (
                            f"event: {envelope['type']}\n"
                            f"id: {envelope['id']}\n"
                            f"data: {json.dumps(envelope)}\n\n"
                        )
                    except TimeoutError:
                        if time.monotonic() - last_heartbeat >= 15.0:
                            yield ": heartbeat\n\n"
                            last_heartbeat = time.monotonic()
            finally:
                event_bus.unsubscribe(token)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post(f"{p}/ask")
    async def ui_ask(
        body: UiAskRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.ask(body)
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.ask, body, _pick_project(project_id, project))

    @router.get(f"{p}/drift")
    async def ui_drift(
        source: str = Query(default="all"),
        directory: str | None = Query(default=None),
        status: str = Query(default="all"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=5, ge=1, le=50),
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.drift_monitor(
                source=source,
                directory=directory,
                status=status,
                page=page,
                page_size=page_size,
            )
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.drift_monitor,
            project_arg=_pick_project(project_id, project),
            source=source,
            directory=directory,
            status=status,
            page=page,
            page_size=page_size,
        )

    @router.post(f"{p}/detected-decisions/{{decision_id}}/decide")
    async def ui_decide_detected(
        decision_id: str,
        body: DecisionActionRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.decide_detected_decision(decision_id, body)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.decide_detected_decision,
            decision_id,
            body,
            _pick_project(project_id, project),
        )

    @router.post(f"{p}/plans")
    async def ui_create_plan(
        body: CreatePlanRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.create_plan(body)
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.create_plan, body, _pick_project(project_id, project))

    @router.patch(f"{p}/plans/{{plan_id}}")
    async def ui_update_plan(
        plan_id: str,
        body: UpdatePlanRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.update_plan(plan_id, body)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.update_plan, plan_id, body, _pick_project(project_id, project)
        )

    @router.post(f"{p}/plans/{{plan_id}}/promote")
    async def ui_promote_plan(
        plan_id: str,
        body: PromotePlanRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.promote_plan(plan_id, body)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.promote_plan, plan_id, body, _pick_project(project_id, project)
        )

    @router.post(f"{p}/tasks")
    async def ui_create_task(
        body: CreateTaskRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.create_task(body)
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.create_task, body, _pick_project(project_id, project))

    @router.post(f"{p}/facts")
    async def ui_create_fact(
        body: CreateFactRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.create_fact(body)
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.create_fact, body, _pick_project(project_id, project))

    @router.get(f"{p}/versions/{{entity_kind}}/{{entity_id}}")
    async def ui_get_versions(
        entity_kind: Literal["plan", "task", "claim"],
        entity_id: str,
        from_version: int | None = Query(default=None, ge=1),
        to_version: int | None = Query(default=None, ge=1),
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.get_versions(entity_kind, entity_id, from_version=from_version, to_version=to_version)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.get_versions,
            entity_kind,
            entity_id,
            from_version=from_version,
            to_version=to_version,
            project_arg=_pick_project(project_id, project),
        )

    @router.post(f"{p}/versions/{{entity_kind}}/{{entity_id}}/clone-draft")
    async def ui_clone_version(
        entity_kind: Literal["plan", "task", "claim"],
        entity_id: str,
        body: CloneVersionRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.clone_version_as_draft(entity_kind, entity_id, body)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.clone_version_as_draft,
            entity_kind,
            entity_id,
            body,
            _pick_project(project_id, project),
        )

    @router.post(f"{p}/actions/reverify")
    async def ui_reverify(
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.reverify()
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.reverify, _pick_project(project_id, project))

    @router.post(f"{p}/actions/reindex")
    async def ui_reindex(
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.reindex()
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.reindex, _pick_project(project_id, project))

    @router.get(f"{p}/doctor")
    async def ui_doctor(
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.doctor()
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.doctor, _pick_project(project_id, project))

    @router.post(f"{p}/actions/export-snapshot")
    async def ui_export_snapshot(
        body: ExportSnapshotRequest,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.export_snapshot(body)
        assert live_adapter is not None
        return await asyncio.to_thread(live_adapter.export_snapshot, body, _pick_project(project_id, project))

    @router.post(f"{p}/actions/sessions/{{session_id}}/drop")
    async def ui_drop_session(
        session_id: str,
        project_id: str | None = None,
        project: str | None = Query(default=None),
        mock: int | None = Query(default=None),
    ):
        if _use_mock(mock):
            return mock_state.drop_session(session_id)
        assert live_adapter is not None
        return await asyncio.to_thread(
            live_adapter.drop_session, session_id, _pick_project(project_id, project)
        )

    @router.post(f"{p}/mock/simulate-conflict")
    async def ui_mock_simulate_conflict(
        body: SimulateConflictRequest,
        mock: int | None = Query(default=None),
    ):
        if not _use_mock(mock):
            raise HTTPException(400, "Simulation endpoints are only available in mock mode.")
        return mock_state.simulate_conflict(body)

    @router.post(f"{p}/mock/simulate-file-edit")
    async def ui_mock_simulate_file_edit(
        body: SimulateFileEditRequest,
        mock: int | None = Query(default=None),
    ):
        if not _use_mock(mock):
            raise HTTPException(400, "Simulation endpoints are only available in mock mode.")
        return mock_state.simulate_file_edit(body)


def create_ui_router(
    *,
    store: Any = None,
    settings: Any = None,
    default_project: str | None = None,
    default_mock: bool = False,
    read_only: bool = False,
    bus: UiEventBus | None = None,
    dependencies: list[Any] | None = None,
    prefix: str = "/api/ui",
    mock_state: MockUiState | None = None,
    live_adapter: LiveUiAdapter | None = None,
) -> APIRouter:
    event_bus = bus or UiEventBus()
    state = mock_state or MockUiState(event_bus, read_only=read_only)
    adapter = live_adapter or (
        LiveUiAdapter(store, settings, event_bus, default_project, read_only=read_only)
        if store is not None
        else None
    )
    router = APIRouter(prefix=prefix, tags=["ui"], dependencies=dependencies or [])
    _bind_cockpit_routes(
        router,
        event_bus=event_bus,
        mock_state=state,
        live_adapter=adapter,
        default_mock=default_mock,
        project_path_param=False,
    )
    _bind_cockpit_routes(
        router,
        event_bus=event_bus,
        mock_state=state,
        live_adapter=adapter,
        default_mock=default_mock,
        project_path_param=True,
    )
    router.event_bus = event_bus  # type: ignore[attr-defined]
    router.mock_state = state  # type: ignore[attr-defined]
    router.live_adapter = adapter  # type: ignore[attr-defined]
    return router


def attach_ui_routes(
    app: FastAPI,
    store: Any,
    settings: Any,
    *,
    bus: UiEventBus | None = None,
    dependencies: list[Any] | None = None,
    read_only: bool = False,
) -> UiEventBus:
    """Attach `/api/ui/*` (and `/api/ui/projects/{project_id}/*`) routes to the main Cairnmind service."""
    router = create_ui_router(
        store=store,
        settings=settings,
        default_mock=False,
        read_only=read_only,
        bus=bus,
        dependencies=dependencies,
    )
    app.include_router(router)
    event_bus: UiEventBus = router.event_bus  # type: ignore[attr-defined]
    app.state.ui_event_bus = event_bus
    return event_bus


def create_ui_app(
    *,
    store: Any = None,
    settings: Any = None,
    default_project: str | None = None,
    default_mock: bool = False,
    read_only: bool = False,
    static_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 4310,
    auth: Any = None,
) -> FastAPI:
    """Standalone loopback FastAPI app for `cairnmind ui --port 4310`."""
    from cairnmind import __version__
    from cairnmind.ui_auth import UiSessionAuth, install_ui_security

    app = FastAPI(title="Cairnmind Engineer Cockpit & Inspector", version=__version__)
    ui_auth = auth if isinstance(auth, UiSessionAuth) else UiSessionAuth.create(host=host, port=port)
    install_ui_security(app, ui_auth)
    app.state.ui_auth = ui_auth

    ui_router = create_ui_router(
        store=store,
        settings=settings,
        default_project=default_project,
        default_mock=default_mock,
        read_only=read_only,
        prefix="/api/ui",
    )
    app.include_router(ui_router)
    app.state.ui_event_bus = ui_router.event_bus  # type: ignore[attr-defined]

    dist = resolve_ui_dist(static_dir)
    if dist is not None:
        assets_dir = dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="ui-assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str):
            if full_path.startswith("api/") or full_path == "api":
                return JSONResponse({"detail": "Not found"}, status_code=404)
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_relative_to(dist) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        async def missing_bundle():
            has_source_ui = (Path(__file__).resolve().parent.parent.parent / "ui" / "package.json").is_file()
            hint = (
                "Run <code>cd ui &amp;&amp; npm ci &amp;&amp; npm run build</code>, then restart <code>cairnmind ui</code>."
                if has_source_ui
                else "Reinstall Cairnmind from a release wheel built with bundled UI assets (or set <code>CAIRNMIND_UI_DIST</code>)."
            )
            return HTMLResponse(
                "<!doctype html><html><body style='background:#060709;color:#e4e4e7;font-family:monospace;padding:24px'>"
                "<h3>Cairnmind UI bundle not built</h3>"
                f"<p>{hint}</p>"
                "<p>Authenticated API endpoints are live under <code>/api/ui/snapshot</code>.</p>"
                "</body></html>",
                status_code=503,
            )

    return app
