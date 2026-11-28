"""Operational cockpit endpoints (ask, drift, decisions, reindex, doctor, export)."""
from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import HTTPException

from cairnmind.storage.arcade import ArcadeError
from cairnmind.storage.storage_query import db_find_one, db_update_where
from cairnmind.ui.schemas import (
    DecisionActionRequest,
    ExportSnapshotRequest,
    UiAskRequest,
    make_origin,
    safe_file_fingerprint,
    scan_and_redact_payload,
    sha256_text,
    utc_now,
)

log = logging.getLogger(__name__)


class _LiveOpsMixin:
    """Operational Engineer Cockpit endpoints mixed into `LiveCockpitMixin`."""

    if TYPE_CHECKING:
        store: Any
        settings: Any
        bus: Any
        read_only: bool

        def _resolve_project(self, project_arg: str | None) -> dict[str, Any]: ...
        def _state_rev(self, project_id: str) -> int: ...
        def snapshot(self, project_arg: str | None = None) -> dict[str, Any]: ...
        def _writable_status(self) -> tuple[bool, str | None]: ...
        def _require_writable(self) -> None: ...
        def _llm_configured(self) -> bool: ...

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
        from cairnmind.terminal import person_proof

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
        self.bus.publish(
            "state.updated",
            {"state_rev": rev, "reason": f"detected_decision_{req.action}", "decision_id": decision_id},
        )
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

    def reindex(self, project_arg: str | None = None) -> dict[str, Any]:
        from cairnmind.scip.ingest import index_project_if_changed

        self._require_writable()
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        job_id = f"reidx-{uuid4().hex[:6]}"
        started = time.perf_counter()
        self.bus.publish(
            "reindex.progress",
            {"job_id": job_id, "percent": 20, "stage": "scanning", "message": "Scanning repository tree"},
        )
        changed = 0
        try:
            result = index_project_if_changed(self.store, pid)
            changed = int((result or {}).get("changed", 0))
        except (
            ArcadeError,
            OSError,
            subprocess.SubprocessError,
            ValueError,
            TypeError,
            RuntimeError,
            KeyError,
            AttributeError,
        ) as exc:
            log.debug("Repository re-index failed for %s: %s", pid, exc)
        self.bus.publish(
            "reindex.progress",
            {"job_id": job_id, "percent": 75, "stage": "indexing", "message": "Refreshing claims and fingerprints"},
        )
        snap = self.snapshot(pid)
        self.bus.publish(
            "reindex.progress",
            {"job_id": job_id, "percent": 100, "stage": "complete", "message": "Repository re-index completed"},
        )
        self.bus.publish(
            "state.updated",
            {"state_rev": snap["state_rev"], "reason": "reindex_complete", "changed_files": changed},
        )
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
                {
                    "metric": "Active Records",
                    "current": len(snap["claims"]) + len(snap["tasks"]),
                    "threshold": 10000,
                    "status": "OK",
                    "unit": "rows",
                },
                {
                    "metric": "Graph Nodes",
                    "current": len(snap["graph"]["nodes"]),
                    "threshold": 50000,
                    "status": "OK",
                    "unit": "nodes",
                },
                {
                    "metric": "Active Sessions",
                    "current": len(snap["sessions"]),
                    "threshold": 50,
                    "status": "OK",
                    "unit": "sessions",
                },
            ],
            "warnings": warnings,
            "checked_at": utc_now(),
        }

    def export_snapshot(self, req: ExportSnapshotRequest, project_arg: str | None = None) -> dict[str, Any]:
        if not req.preview:
            self._require_writable()
        snap = self.snapshot(project_arg)
        confirmed = [
            c
            for c in snap["claims"]
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
                "held_degraded": sum(
                    1 for c in snap["claims"] if (c.get("origin") or {}).get("origin_evidence") == "degraded"
                ),
                "excluded_transcripts": len(snap["sessions"]),
            },
            "artifact_filename": f"{snap['project']['name']}-team-snapshot-{digest[:8]}.json",
            "snapshot_sha256": digest,
            "exported_at": raw_payload["exported_at"],
            "payload_preview": clean_payload,
        }
