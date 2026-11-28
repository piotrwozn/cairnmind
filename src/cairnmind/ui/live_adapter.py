"""Live Cairnmind store adapter and Engineer Cockpit endpoints."""
from __future__ import annotations

import logging
from contextlib import suppress
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
from cairnmind.ui.live_cockpit_ops import _LiveOpsMixin
from cairnmind.ui.live_cockpit_plans import _LivePlansMixin
from cairnmind.ui.schemas import (
    classify_audit_origin,
    classify_record_origin,
)

log = logging.getLogger(__name__)

__all__ = ["LiveCockpitMixin", "LiveUiAdapter"]


class LiveCockpitMixin(_LivePlansMixin, _LiveOpsMixin):
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
        source_rows = (
            db_find(
                self.store,
                "sources",
                [is_in("id", source_ids)],
                columns=["id", "uri", "kind", "sha256", "is_current", "metadata"],
            )
            if source_ids
            else []
        )
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
                (
                    sources_by_id[sid]
                    for sid in (rec.get("source_ids") or [])
                    if sid in sources_by_id and sources_by_id[sid].get("kind") in {"code", "document", "file"}
                ),
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
