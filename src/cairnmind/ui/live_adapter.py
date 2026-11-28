"""Live Cairnmind store adapter and Engineer Cockpit endpoints."""
from __future__ import annotations

import logging
import re
import time
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from cairnmind.storage.storage_query import (
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
from cairnmind.ui.event_bus import UiEventBus
from cairnmind.ui.graph import (
    _build_graph_from_entities,
    _classify_reason_code,
    _find_quote_lines,
    _sha256_bytes,
    _utc_now,
    safe_read_file_context,
)
from cairnmind.ui.schemas import (
    CloneVersionRequest,
    CreateFactRequest,
    CreatePlanRequest,
    CreateTaskRequest,
    DecisionActionRequest,
    ExportSnapshotRequest,
    PlanStepInput,
    PromotePlanRequest,
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

log = logging.getLogger(__name__)


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
            from cairnmind.agents import knowledge_mode
            return bool(knowledge_mode.enabled(self.settings))
        except Exception as exc:
            log.debug("Unable to determine LLM knowledge mode status: %s", exc)
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
        try:
            wt_rows = db_find(
                self.store,
                "worktree_states",
                {"project_id": pid},
                columns=["active_plan_id"],
                limit=5,
            )
            if wt_rows:
                active_plan_id = wt_rows[0].get("active_plan_id")
        except Exception as exc:
            log.debug("Failed to read active plan from worktree_states for %s: %s", pid, exc)

        tasks_by_plan: dict[str, list[dict[str, Any]]] = {}
        for t in tasks_out:
            tasks_by_plan.setdefault(t["plan_id"], []).append(t)

        out: list[dict[str, Any]] = []
        for row in rows:
            plan_id = row["id"]
            raw_meta = row.get("metadata")
            meta = raw_meta if isinstance(raw_meta, dict) else {}
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
            try:
                db_insert_document(tx, "plan_revisions", {
                    "id": f"{plan_id}:1",
                    "project_id": pid,
                    "plan_id": plan_id,
                    "version": 1,
                    "reason": "draft_created" if req.status == "draft" else "plan_created",
                    "snapshot": {"plan": doc, "tasks": []},
                    "recorded_at": now,
                })
            except Exception as exc:
                log.debug("Optional plan_revisions insert skipped for %s: %s", plan_id, exc)
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
            try:
                db_insert_document(tx, "plan_revisions", {
                    "id": f"{plan_id}:{updated['version']}",
                    "project_id": pid,
                    "plan_id": plan_id,
                    "version": updated["version"],
                    "reason": "draft_updated",
                    "snapshot": {"plan": updated, "tasks": []},
                    "recorded_at": now,
                })
            except Exception as exc:
                log.debug("Optional plan_revisions update skipped for %s: %s", plan_id, exc)
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
        from cairnmind.terminal import person_proof

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
        from cairnmind.scip.ingest import index_project_if_changed

        self._require_writable()
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        job_id = f"reidx-{uuid4().hex[:6]}"
        started = time.perf_counter()
        self.bus.publish("reindex.progress", {"job_id": job_id, "percent": 20, "stage": "scanning", "message": "Scanning repository tree"})
        changed = 0
        try:
            result = index_project_if_changed(self.store, pid)
            changed = int((result or {}).get("changed", 0))
        except Exception as exc:
            log.debug("Repository re-index failed for %s: %s", pid, exc)
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


class LiveUiAdapter(LiveCockpitMixin):
    """Projects Cairnmind's Store into Engineer Cockpit contracts."""

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
        from cairnmind.storage.journal import Journal
        try:
            cursor = Journal(self.store).cursor()
            seq = int(cursor.get("seq", 0))
            if seq > 0:
                return seq
        except Exception as exc:
            log.debug("Journal cursor unavailable; falling back to record count: %s", exc)
        return db_count(self.store, "records", {"project_id": project_id})

    def snapshot(self, project_arg: str | None = None) -> dict[str, Any]:
        from cairnmind.memory.evidence import evidence_states
        from cairnmind.memory.verification import capture_code_state, verification_status

        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        root_path = Path(proj["root"]).expanduser().resolve()
        state_rev = self._state_rev(pid)
        now = _utc_now()

        code = {"branch": None, "head": None, "fingerprint": None}
        try:
            code = capture_code_state(self.store, pid, str(root_path))
        except Exception as exc:
            log.debug("Code state capture unavailable for %s: %s", pid, exc)

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
            raw_meta = rec.get("metadata")
            meta = raw_meta if isinstance(raw_meta, dict) else {}
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
                raw_smeta = file_src.get("metadata")
                smeta = raw_smeta if isinstance(raw_smeta, dict) else {}
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
            raw_meta = task.get("metadata")
            meta = raw_meta if isinstance(raw_meta, dict) else {}
            plan_id = meta.get("plan_ref") or ""
            plan = plans_by_id.get(plan_id)
            task_checks = checks_by_task.get(task["id"], [])
            latest_check = task_checks[0] if task_checks else None
            ver = None
            try:
                ver = verification_status(self.store, task, str(root_path))
            except Exception as exc:
                log.debug("Verification status unavailable for task %s: %s", task["id"], exc)

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
        from cairnmind.scip.ingest import index_project_if_changed

        self._require_writable()
        proj = self._resolve_project(project_arg)
        pid = proj["id"]
        try:
            index_project_if_changed(self.store, pid)
        except Exception as exc:
            log.debug("Re-index during reverify failed for %s: %s", pid, exc)
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
