"""Plan, task, fact, and version-history endpoints for `LiveCockpitMixin`."""
from __future__ import annotations

import logging
import re
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import HTTPException

from cairnmind.storage.arcade import ArcadeError
from cairnmind.storage.storage_query import (
    asc,
    db_find,
    db_find_one,
    db_insert_document,
    db_update_where,
    desc,
)
from cairnmind.ui.schemas import (
    CloneVersionRequest,
    CreateFactRequest,
    CreatePlanRequest,
    CreateTaskRequest,
    PlanStepInput,
    PromotePlanRequest,
    UpdatePlanRequest,
    classify_record_origin,
    compute_snapshot_diff,
    make_origin,
    safe_file_fingerprint,
    utc_now,
)

log = logging.getLogger(__name__)


class _LivePlansMixin:
    """Plan, task, fact, and version endpoints mixed into `LiveCockpitMixin`."""

    if TYPE_CHECKING:
        store: Any
        settings: Any
        bus: Any
        read_only: bool

        def _resolve_project(self, project_arg: str | None) -> dict[str, Any]: ...
        def _state_rev(self, project_id: str) -> int: ...
        def snapshot(self, project_arg: str | None = None) -> dict[str, Any]: ...
        def _require_writable(self) -> None: ...

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
        except (ArcadeError, OSError, ValueError, TypeError, RuntimeError, KeyError, AttributeError) as exc:
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
            linked = list(
                dict.fromkeys(
                    list(meta.get("linked_files") or [])
                    + [p for t in plan_tasks for p in t.get("source_paths") or []]
                )
            )
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
            except (ArcadeError, OSError, ValueError, TypeError, RuntimeError, KeyError, AttributeError) as exc:
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
            except (ArcadeError, OSError, ValueError, TypeError, RuntimeError, KeyError, AttributeError) as exc:
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
        return {
            "ok": True,
            "requires_confirmation": False,
            "precheck": precheck,
            "plan": res["plan"],
            "state_rev": res["state_rev"],
        }

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
            return {
                "ok": True,
                "source_version": req.source_version,
                "draft_plan": created["plan"],
                "state_rev": created["state_rev"],
            }
        if entity_kind == "claim":
            created = self.create_fact(
                CreateFactRequest(
                    kind=(
                        "decision"
                        if snap.get("kind") == "decision"
                        else "constraint"
                        if snap.get("kind") == "constraint"
                        else "fact"
                    ),
                    key=f"{snap.get('key', entity_id)}.v{req.source_version}-draft",
                    title=f"{snap.get('title', entity_id)} {suffix}",
                    content=str(snap.get("content") or ""),
                    source_path=snap.get("source_path") or None,
                    verbatim_quote=str(snap.get("verbatim_quote") or ""),
                ),
                project_arg=project_arg,
            )
            return {
                "ok": True,
                "source_version": req.source_version,
                "draft_claim": created["claim"],
                "state_rev": created["state_rev"],
            }
        created = self.create_task(
            CreateTaskRequest(
                title=f"{snap.get('title', entity_id)} {suffix}",
                scope=list(snap.get("source_paths") or []),
                acceptance_criteria=list(snap.get("acceptance_criteria") or []),
                status="draft",
            ),
            project_arg=project_arg,
        )
        return {
            "ok": True,
            "source_version": req.source_version,
            "draft_task": created["task"],
            "state_rev": created["state_rev"],
        }
