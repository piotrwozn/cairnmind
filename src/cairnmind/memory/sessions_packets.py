"""Resume packet formatting, preparation, acknowledgment, and hook event recording for sessions."""
from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from cairnmind.memory.confirmation import confirmed_by
from cairnmind.memory.integrations import DATA_NOT_INSTRUCTIONS
from cairnmind.memory.memory_egress import HOSTED_PACKET_NOTE, Gate, ordered
from cairnmind.memory.packet_origin import omitted_by_origin, preview_key, record_origins
from cairnmind.storage.domain_types import ProjectRecord, ResumePacketRecord, SessionRecord
from cairnmind.storage.sql import clean_row, json_text, utc_now
from cairnmind.storage.storage_query import (
    db_find,
    db_find_one,
    db_group_counts,
    db_insert_document,
    db_update_where,
    desc,
    eq,
    ne,
)
from cairnmind.store import ORIGIN_METADATA

log = logging.getLogger(__name__)

MAX_PACKET_BYTES = 24576
MAX_RECORDS = 20
# 2: each record carries its origin, confirmed records come first, omitted_by_origin (#178). A packet of an
# earlier schema is never current, so an ACK given for it goes stale instead of counting for what it never showed.
PACKET_SCHEMA_VERSION = 2
RESUME_REASONS = {"startup", "resume", "compact", "clear", "fork", "refresh"}
AGENT_CLIENTS = {"api", "copilot"}
# Any MCP client whose conversation Cairnmind cannot read (Cursor, Cline, Zed and others).
EXTERNAL_CLIENT = "external"
# The answer of resume_context while the session's ACK of its packet still holds (#325).
UNCHANGED_NOTE = ("Unchanged: this session acknowledged this packet and nothing it promises has changed, so the "
                  "acknowledgement still holds. This answer is not an acknowledgement and extends none. A client "
                  "that no longer has the packet asks for the full packet again.")
EXTERNAL_WARNING = (
    "External client: Cairnmind cannot read or archive this client's own conversation (history: unarchived), "
    "so nothing said in it can be verified (evidence: degraded). What this session records is kept as reported "
    "by an external session and is never shown or counted as verified.")


def _hash(value: Any) -> str:
    return hashlib.sha256(json_text(value).encode("utf-8")).hexdigest()


def _scope_contains(root: str, cwd: Any) -> bool:
    if not isinstance(cwd, str) or not cwd:
        return False
    try:
        return Path(cwd).expanduser().resolve().is_relative_to(Path(root))
    except (OSError, ValueError):
        return False


def _readback(packet: ResumePacketRecord | Mapping[str, Any]) -> dict[str, dict[str, Any] | None]:
    return {name: ({"id": packet[key]["id"], "version": packet[key]["version"]}
                   if packet.get(key) else None)
            for name, key in (("plan", "active_plan"), ("task", "active_task"))}


def format_packet(packet: ResumePacketRecord | Mapping[str, Any]) -> str:
    """Small hook/launcher bootstrap; the complete bounded packet is an MCP read."""
    data = cast(Mapping[str, Any], packet)
    plan, task = data.get("active_plan"), data.get("active_task")
    external = data.get("history") == "unarchived"
    # A plan or task an external session wrote is never presented as verified, not even to a native client.
    unverified_plan = bool(plan) and plan.get("origin_evidence") == "degraded"
    unverified_task = bool(task) and task.get("origin_evidence") == "degraded"
    # Written by the built-in agent after a third-party MCP tool result (origin_client mcp:<server>) has its own
    # wording; an external client's wording stays for what an external client wrote.

    def third_party(item: Mapping[str, Any]) -> bool:
        return str(item.get("origin_client", "")).startswith("mcp:")

    def note(item: Mapping[str, Any]) -> str:
        return (" [written after a third-party tool result in a built-in agent session; not verified]"
                if third_party(item) else " [written by an external client's session; not verified]")

    only_third_party = all(
        third_party(item)
        for item, unverified in ((plan, unverified_plan), (task, unverified_task))
        if unverified and item is not None
    )
    result = [
        ("Cairnmind external-client resume bootstrap: history unarchived, evidence degraded "
         "(receipt is not proof of understanding)." if external else
         ("Cairnmind resume bootstrap: the active plan or task was written after a third-party tool result in a "
          "built-in agent session and is not verified (receipt is not proof of understanding)."
          if only_third_party else
          "Cairnmind resume bootstrap: the active plan or task was written by an external client's session "
          "and is not verified (receipt is not proof of understanding).")
         if unverified_plan or unverified_task else
         "Cairnmind verified resume bootstrap (receipt is not proof of understanding)."),
        f"Logical session: {data['session_id']}; epoch: {data['epoch']}.",
        f"Packet: {data['id']}; SHA-256: {data['hash']}.",
        f"Project: {data['project_id']}; worktree: {data['worktree']}.",
        "Before normal shared-memory writes, call the Cairnmind MCP tool resume_context, read the complete "
        "packet, then call ack_resume with its exact id/hash and this readback shape:",
        json_text(_readback(packet)),
        "Do not invent missing state or mark tasks done from a summary. "
        "Native CLI permissions still apply to all native actions.",
        DATA_NOT_INSTRUCTIONS,
    ]
    if plan:
        result.extend([f"Active plan v{plan['version']}{note(plan) if unverified_plan else ''}: "
                       f"{plan.get('title', '')[:300]}",
                       f"Goal: {plan.get('goal', '')[:1200]}"])
        for constraint in plan.get("constraints", [])[:8]:
            result.append("Constraint: " + constraint[:240])
    if task:
        result.append(f"Active task {task['id']} v{task['version']} [{task.get('state', 'todo')}]"
                      f"{note(task) if unverified_task else ''}: " + task.get("content", "")[:1000])
    else:
        result.append("Active task: none; use the explicit reason in the complete packet.")
    if packet.get("no_history"):
        result.append("No prior conversation history is known for this project scope.")
    if external:
        result.append("This client's own conversation is not archived; what this session records is kept "
                      "as reported by an external session and never counts as verified.")
    if packet.get("baseline_unverified"):
        result.append("New empty project: the code baseline is unverified. Initialize Git and commit "
                      "before relying on code-bound test completion.")
    result.append("Full constraints, source references, blockers, archive/index/extraction "
                  "status and omitted records are in the complete resume packet.")
    return "\n".join(result)[:6500]


class _SessionPacketMixin:
    """Resume packet assembly, preparation, readback/ACK handling, and hook event ingestion."""

    if TYPE_CHECKING:
        store: Any
        settings: Any

        def _session(self, session_id: str) -> dict[str, Any]: ...
        def _packet(self, session: SessionRecord | Mapping[str, Any]) -> dict[str, Any] | None: ...
        def _own_continuity(self, session: SessionRecord | Mapping[str, Any]) -> list[str]: ...
        def _root(self, project_id: str, worktree: str | None) -> tuple[ProjectRecord, str]: ...
        def _require_scope(self, tx: Any, project_id: str, root: str) -> None: ...
        def _code(self, session: SessionRecord | Mapping[str, Any]) -> dict[str, Any]: ...
        def _state(
            self,
            session: SessionRecord | Mapping[str, Any],
            code: dict[str, Any] | None = None,
        ) -> dict[str, Any]: ...
        def _state_locked(
            self,
            session: SessionRecord | Mapping[str, Any],
            code: dict[str, Any],
        ) -> dict[str, Any]: ...
        def _write_refusal(
            self,
            session: SessionRecord | Mapping[str, Any],
            packet_id: str | None,
        ) -> str | None: ...
        @staticmethod
        def _with_newer(
            reply: dict[str, Any],
            state: dict[str, Any],
            packet: ResumePacketRecord | Mapping[str, Any],
        ) -> dict[str, Any]: ...
        @staticmethod
        def _newer_count(
            state: dict[str, Any],
            packet: ResumePacketRecord | Mapping[str, Any],
        ) -> int: ...
        @staticmethod
        def _encode_watermark(watermark: Any) -> Any: ...
        @staticmethod
        def _target(path: str, client: str, watermark: dict[str, Any] | None = None) -> dict[str, Any]: ...
        @classmethod
        def _merge_targets(cls, result: list[dict[str, Any]]) -> list[dict[str, Any]]: ...
        def _targets(self, session: SessionRecord | Mapping[str, Any], deadline: float) -> list[dict[str, Any]]: ...
        def _legacy_action_targets(
            self,
            session: SessionRecord | Mapping[str, Any],
            packet: ResumePacketRecord | Mapping[str, Any],
        ) -> list[dict[str, Any]] | None: ...
        def _archive(
            self,
            session: SessionRecord | Mapping[str, Any],
            target: dict[str, Any],
            deadline: float,
        ) -> dict[str, Any]: ...

    def _packet_byte_budget(self) -> int:
        return MAX_PACKET_BYTES

    def _max_records(self) -> int:
        return MAX_RECORDS

    def _schema_version(self) -> int:
        return PACKET_SCHEMA_VERSION

    def _is_current_schema(self, packet: ResumePacketRecord | Mapping[str, Any]) -> bool:
        return packet.get("schema_version") == self._schema_version()

    def _pending_status(self, project_id: str, kind: str) -> dict:
        rows = db_group_counts(
            self.store, "jobs", ("state",), {"project_id": project_id, "kind": kind}, count_alias="total"
        )
        counts = {row["state"]: int(row["total"]) for row in rows}
        return {"status": "pending" if any(counts.get(key, 0) for key in ("pending", "running"))
                else "failed" if counts.get("failed") else "complete", "counts": counts}

    def _build(self, session: dict, state: dict, archives: list[dict], unknowns: list[str],
               created_at: str | None = None) -> dict:
        max_bytes = self._packet_byte_budget()
        max_records = self._max_records()
        plan, task = state["plan"], state["task"]
        evidence = state.get("evidence") or {}
        plan_view = ({key: plan.get(key) for key in
                      ("id", "version", "title", "goal", "constraints", "source_ids")} if plan else None)
        task_view = ({"id": task["id"], "version": task["version"], "content": task["content"],
                      "state": task.get("metadata", {}).get("state", "todo"),
                      "source_ids": task.get("source_ids", []),
                      "depends_on": task.get("metadata", {}).get("depends_on", []),
                      "acceptance_criteria": task.get("metadata", {}).get("acceptance_criteria", []),
                      "required_profiles": task.get("metadata", {}).get("required_profiles", []),
                      "requires_test": task.get("metadata", {}).get("requires_test", False),
                      "requires_review": task.get("metadata", {}).get("requires_review", False)} if task else None)
        # The mark of an external session's plan or task, only where there is one: native packets are unchanged.
        if plan_view:
            plan_view.update({key: plan[key] for key in ORIGIN_METADATA if key in plan})
        if task_view:
            task_view.update({key: task["metadata"][key] for key in ORIGIN_METADATA if key in task.get("metadata", {})})
        if task_view and task and isinstance(task.get("metadata", {}).get("scope"), list) and task["metadata"]["scope"]:
            # Only for a task that declares one, so every other packet stays byte for byte what it was.
            task_view["scope"] = [str(pattern)[:500] for pattern in task["metadata"]["scope"][:64]]
        gate = None
        if session.get("held_classes"):
            # A hosted built-in session (#183): previews of held classes are left out and counted. The state
            # hash is not changed by it, so the ACK rules are the same as for every other session.
            gate = Gate(self.store, frozenset(session["held_classes"]))
            gate.prime(state["records"])
            state = {**state, "records": [row for row in state["records"] if not gate.record_held(row)]}
        origins = record_origins(self.store, state["records"])
        previews = sorted(state["records"], key=lambda row: preview_key(row, origins[row["id"]]))[:max_records]
        archived_agent_history = db_find(
            self.store,
            "sources",
            [
                eq("project_id", session["project_id"]),
                eq("kind", "agent_transcript"),
                ne("metadata.agent_session_id", session["id"]),
            ],
            columns=("id",),
            limit=1,
        )
        packet_archives = [{key: value for key, value in row.items() if key != "source_ids"}
                           if row.get("kind") == "agent_journal" else row for row in archives]
        packet = {"id": str(uuid4()), "schema_version": self._schema_version(), "project_id": session["project_id"],
                  "session_id": session["id"], "epoch": session["epoch"], "client": session["client"],
                  "worktree": session["worktree"], "created_at": created_at or utc_now(),
                  "active_plan": plan_view, "active_task": task_view,
                  "next_action": state["reason"], "blockers": state["blocked"],
                  "records": [{"id": row["id"], "version": row["version"], "kind": row["kind"],
                               "status": row["status"], "origin": origins[row["id"]],
                               "content": row["content"][:600],
                               "content_truncated": len(row["content"]) > 600,
                               "source_ids": row.get("source_ids", [])[:8],
                               # On a confirmed record only (#177): person (here), import (confirmed elsewhere,
                               # never verified here), before_rule (nobody recorded) or seed.
                               **({"confirmed_by": confirmed_by(row)} if confirmed_by(row) else {}),
                               **({"evidence_state": evidence[row["id"]]} if row["id"] in evidence else {})}
                              for row in previews],
                  "omitted_records": max(0, len(state["records"]) - len(previews)),
                  "omitted_by_origin": omitted_by_origin(state["records"], previews, origins),
                  "code": state["code"], "state_hash": state["state_hash"],
                  "archive_watermarks": packet_archives, "no_history": not archives and not archived_agent_history,
                  "archive": {"status": "complete" if all(row["status"] == "complete" for row in archives)
                              else "partial", "scope": "captured EOFs only"},
                  "indexing": self._pending_status(session["project_id"], "embed"),
                  "extraction": self._pending_status(session["project_id"], "extract"),
                  "unknowns": [value[:500] for value in list(dict.fromkeys(unknowns))[:20]],
                  "omitted_unknowns": max(0, len(set(unknowns)) - 20)}
        if gate and gate.records:
            packet["held_back"] = {"classes": ordered(gate.held), "records": len(gate.records),
                                   "note": HOSTED_PACKET_NOTE}
        orphaned = sum(value == "orphaned" for value in evidence.values())
        changed = sum(value == "evidence_replaced" for value in evidence.values())
        evidence_warning = (
            f"As of the last indexing of the project's files, {orphaned + changed} memory records in this scope "
            f"have no supporting source that still stands ({orphaned} orphaned, {changed} with a changed file). "
            f"Treat them as unverified; list them with `cairnmind memory orphans {session['project_id']}`.")
        if orphaned or changed:
            packet["warnings"] = [evidence_warning]
        baseline = state.get("initial_baseline_eligible", False) and not archives
        if baseline:
            packet["baseline_unverified"] = True
            packet["warnings"] = ["New empty project: no code/history baseline is verified. "
                                  "Initialize Git and commit before relying on code-bound check completion.",
                                  *state["code"].get("unknowns", []), *packet.get("warnings", [])]
        if session["client"] == EXTERNAL_CLIENT:
            # Only for an external client, so every other packet stays byte for byte what it was. The
            # history exists but cannot be read: it is unarchived, not absent, and the archive status
            # describes only the other clients' transcripts captured in this worktree.
            packet.update(history="unarchived", evidence="degraded", no_history=False)
            packet["archive"]["scope"] = "captured EOFs only; this client's own conversation is not archived"
            packet["warnings"] = [EXTERNAL_WARNING, *packet.get("warnings", [])]
        if evidence and len(json_text(packet).encode("utf-8")) > max_bytes:
            # Advisory evidence data yields first: it must never be what makes a packet unready.
            # A record without a state reads as anchored, which an external session's record must never do:
            # one that needs "degraded" leaves the preview list and is counted as omitted instead.
            shown = [row for row in packet["records"] if row.get("evidence_state") != "degraded"]
            packet["omitted_records"] += len(packet["records"]) - len(shown)
            packet["records"] = shown
            packet["omitted_by_origin"] = omitted_by_origin(state["records"], shown, origins)
            for row in shown:
                row.pop("evidence_state", None)
            if len(json_text(packet).encode("utf-8")) > max_bytes and evidence_warning in packet.get("warnings", []):
                packet["warnings"].remove(evidence_warning)
                if not packet["warnings"]:
                    del packet["warnings"]
        if task_view and "scope" in task_view and len(json_text(packet).encode("utf-8")) > max_bytes:
            # Advice, like the evidence data: it must never be what makes a packet unready.
            task_view["scope_omitted"] = len(task_view.pop("scope"))
        while packet["records"] and len(json_text(packet).encode("utf-8")) > max_bytes:
            # The preview is not essential: the last, least trusted records yield one by one, counted (#178).
            packet["records"].pop()
            packet["omitted_records"] += 1
            packet["omitted_by_origin"] = omitted_by_origin(state["records"], packet["records"], origins)
        if len(json_text(packet).encode("utf-8")) > max_bytes:
            # Essential goal/constraints are never silently truncated to a ready packet.
            packet["unknowns"].append("Essential resume packet exceeds the bounded context budget")
            packet["records"] = []
            packet["omitted_records"] = len(state["records"])
            packet["omitted_by_origin"] = omitted_by_origin(state["records"], [], origins)
            for key in ("active_plan", "active_task"):
                if packet[key]:
                    packet[key] = {field: packet[key][field] for field in ("id", "version", *ORIGIN_METADATA)
                                   if field in packet[key]}
            packet["blockers"] = []
            packet["archive_watermarks"] = [{key: row.get(key) for key in
                                              ("path", "size", "archived_offset", "status", "sha256")}
                                             for row in archives[:30]]
            packet["omitted_archive_watermarks"] = max(0, len(archives) - 30)
            while (packet["archive_watermarks"]
                   and len(json_text(packet).encode("utf-8")) > max_bytes - 512):
                packet["archive_watermarks"].pop()
                packet["omitted_archive_watermarks"] += 1
        packet["status"] = ("unavailable" if state["code"].get("status") != "fresh" and not baseline else
                            "partial" if packet["unknowns"] or packet["archive"]["status"] != "complete"
                            else "ready")
        packet["hash"] = _hash(packet)
        return packet

    @staticmethod
    def _result(session: dict, packet: dict | None) -> dict:
        return {"session_id": session["id"], "epoch": session["epoch"],
                "status": packet.get("status", "partial") if packet else "partial", "packet": packet,
                "unknowns": packet.get("unknowns", []) if packet else ["Resume packet requires preparation"]}

    def prepare(self, project_id: str, client: str, worktree: str | None = None,
                session_id: str | None = None, reason: str = "startup", timeout: float = 30,
                held_classes: list[str] | None = None) -> dict:
        if client not in {"codex", "claude", EXTERNAL_CLIENT, *AGENT_CLIENTS}:
            raise ValueError("Session client must be codex, claude, external, api or copilot")
        if client in AGENT_CLIENTS and session_id is not None:
            from cairnmind.agents.agent_memory import _journal_path
            _journal_path(self.settings, session_id)
        if reason not in RESUME_REASONS:
            raise ValueError("Unknown resume reason")
        if (isinstance(timeout, bool) or not isinstance(timeout, (float, int))
                or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Resume timeout must be nonnegative")
        # timeout=0 performs a single non-waiting check, useful for scanner/UI probes.
        started, limit = time.monotonic(), min(float(timeout), 30.0)
        deadline = started + (limit if limit else 30.0)
        project, root = self._root(project_id, worktree)
        with self.store.transaction() as tx:
            self._require_scope(tx, project["id"], root)
            session = db_find_one(tx, "sessions", {"id": session_id}) if session_id else None
            if session:
                if (session["project_id"], session["client"], session["worktree"]) != (project["id"], client, root):
                    raise ValueError("A logical session cannot change project, client or worktree")
                # A refresh changes the state packet, not the captured history
                # horizon. The current model already has its ongoing turn.
                previous_packet = self._packet(session)
                if "resume_targets" not in session and previous_packet:
                    session["resume_targets"] = self._legacy_action_targets(session, previous_packet)
                # Retrying partial preparation is the same logical resume action.
                if session.get("status") == "ready" and reason != "refresh":
                    session["epoch"] += 1
                    session.update(resume_targets=None, pending_start=True)
                if reason == "refresh" and session.get("native_session_id"):
                    session["pending_start"] = False
                session.update(reason=reason, ack_id=None, packet_id=None)
            else:
                session = {"id": session_id or str(uuid4()), "project_id": project["id"], "client": client,
                           "worktree": root, "epoch": 1, "reason": reason, "status": "partial",
                           "native_session_id": None, "packet_id": None, "ack_id": None,
                           "pending_start": reason != "refresh", "resume_targets": None,
                           "transcript_targets": [], "event_ids": [], "created_at": utc_now()}
                db_insert_document(tx, "sessions", session)
            if held_classes is not None:  # a refresh without it keeps what the session was prepared with
                session["held_classes"] = list(held_classes)
            session["updated_at"] = utc_now()
            db_update_where(tx, "sessions", set_fields=session, where={"id": session["id"]})
        archives, unknowns, targets = [], [], []
        capture_complete = session.get("resume_targets") is not None
        try:
            saved_targets = session.get("resume_targets")
            targets = saved_targets if saved_targets is not None else []
            if saved_targets is None:
                captured = self._targets(session, deadline)
                # Persist before waiting: a retry must never chase a growing
                # native transcript. Hooks retain later horizons separately.
                with self.store.transaction() as tx:
                    current = db_find_one(tx, "sessions", {"id": session["id"]}, required=True)
                    if current is None:
                        raise LookupError(f"Unknown session: {session['id']}")
                    if current["epoch"] != session["epoch"]:
                        return {"session_id": current["id"], "epoch": current["epoch"], "status": "partial",
                                "packet": None, "unknowns": ["Session resumed during horizon capture; retry"]}
                    if current.get("resume_targets") is None:
                        current["resume_targets"] = captured
                        db_update_where(tx, "sessions", set_fields=current, where={"id": current["id"]})
                    targets = current["resume_targets"]
                    capture_complete = True
            while True:
                archives = [self._archive(session, target, deadline) for target in targets]
                if all(row["status"] == "complete" for row in archives) or not limit or time.monotonic() >= deadline:
                    break
                # Only independent scanner/import work can advance a cursor here.
                if any(row.get("archived_offset", 0) < row.get("size", 0) and
                       "Import cursor has not archived the captured EOF" in row.get("unknowns", [])
                       for row in archives):
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                else:
                    break
            for archive in archives:
                unknowns.extend(f"{reason} ({archive['path']})" for reason in archive.get("unknowns", []))
        except TimeoutError as exc:
            unknowns.append(str(exc))
        # Taken before the state is read: a proposal that lands while the packet is built is then counted as newer.
        state_read_at = utc_now()
        state = self._state(session)
        # Advisory only, and only when a packet is prepared: it is not an unknown and not part
        # of the state identity, so a deleted or edited file neither blocks a resume nor
        # invalidates an acknowledged one. Computed outside the store's writer lock.
        from cairnmind.memory.evidence import evidence_states
        state["evidence"] = {record_id: value["evidence_state"] for record_id, value in evidence_states(
            self.store, session["project_id"], state["records"], quotes=False).items()
            if value["evidence_state"] != "anchored"}
        unknowns.extend(state.get("unknowns", []))
        unknowns.extend(self._own_continuity(session))
        if not (state.get("initial_baseline_eligible") and not archives and not targets):
            unknowns.extend(state["code"].get("unknowns", []))
        if limit and time.monotonic() > deadline:
            unknowns.append("Resume preparation exceeded its deadline")
        packet = self._build(session, state, archives, unknowns, state_read_at)
        # Keep raw proofs out of the bounded packet; proofs survive source-file loss.
        proved = []
        for index, target in enumerate(targets):
            archive = archives[index] if index < len(archives) else None
            if archive and archive["status"] == "complete":
                target = {**target, "proof": {key: archive.get(key) for key in
                                             ("size", "status", "sha256", "archived_offset", "source_count", "source_ids_hash")}}
                if archive.get("kind") == "agent_journal":
                    target["proof"].update({key: archive.get(key) for key in
                                           ("public_sources_hash", "sequence", "chain_hash")})
            proved.append(target)
        with self.store.transaction() as tx:
            self._require_scope(tx, project["id"], root)
            current = db_find_one(tx, "sessions", {"id": session["id"]}, required=True)
            if current is None:
                raise LookupError(f"Unknown session: {session['id']}")
            if current["epoch"] != session["epoch"]:
                return {"session_id": current["id"], "epoch": current["epoch"], "status": "partial", "packet": None,
                        "unknowns": ["Session resumed again during packet preparation; retry"]}
            db_insert_document(tx, "resume_packets", {"id": packet["id"], "project_id": project["id"],
                                                      "session_id": session["id"], "epoch": session["epoch"],
                                                      "hash": packet["hash"], "payload": packet,
                                                      "created_at": packet["created_at"]})
            # A concurrent Stop may capture another horizon without changing epoch.
            # Keep that durable requirement for the next resume preparation.
            extra_targets = [target for target in current.get("transcript_targets", []) if target not in targets]
            current.update(packet_id=packet["id"], ack_id=None, status=packet["status"],
                           resume_targets=proved if capture_complete else None,
                           transcript_targets=self._merge_targets([*proved, *extra_targets]), updated_at=utc_now())
            db_update_where(tx, "sessions", set_fields=current, where={"id": current["id"]})
        return self._result(current, packet)

    def exact_context(self, session_id: str, packet_id: str) -> dict:
        """Read an exact current packet without refreshing horizons or epochs."""
        code = self._code(self._session(session_id))  # before the transaction (#255)
        with self.store.transaction():
            session = self._session(session_id)
            packet = self._packet(session)
            if (not packet or packet.get("status") != "ready" or packet.get("id") != packet_id
                    or packet.get("epoch") != session["epoch"] or not self._is_current_schema(packet)):
                raise ValueError("resume_required: requested startup packet is no longer the ready current packet")
            continuity = self._own_continuity(session)
            if continuity:
                raise ValueError("resume_required: " + continuity[0])
            try:
                current = self._state_locked(session, code)
            except (OSError, ValueError) as exc:
                raise ValueError(f"resume_required: current project scope cannot be restored ({exc})") from None
            if current["state_hash"] != packet["state_hash"]:
                raise ValueError("resume_required: relevant project state or code changed before exact packet read")
            return self._result(session, packet)

    def resume_context(self, session_id: str, short: bool = False) -> dict:
        session = self._session(session_id)
        packet = self._packet(session)
        if packet and packet.get("status") == "ready":
            kept = None
            try:
                if not self._is_current_schema(packet):
                    raise ValueError("A packet made by an earlier version is never current")
                current = self._state(session)
                if current["state_hash"] == packet["state_hash"] and not self._own_continuity(session):
                    kept = current
            except (OSError, ValueError) as exc:
                log.debug("Resume context state check failed for %s: %s", session_id, exc)
            if kept is not None:
                return (self._unchanged(session_id, kept, packet) if short else None) or self._result(session, packet)
            with self.store.transaction() as tx:
                current_session = db_find_one(tx, "sessions", {"id": session_id}, required=True)
                if current_session is None:
                    raise LookupError(f"Unknown session: {session_id}")
                if current_session["epoch"] == session["epoch"]:
                    if "resume_targets" not in current_session:
                        current_session["resume_targets"] = self._legacy_action_targets(current_session, packet)
                    current_session.update(epoch=session["epoch"] + 1, packet_id=None, ack_id=None,
                                           status="partial", updated_at=utc_now())
                    db_update_where(tx, "sessions", set_fields=current_session, where={"id": session_id})
        return self.prepare(session["project_id"], session["client"], session["worktree"], session_id,
                            reason="refresh", timeout=0)

    def _unchanged(self, session_id: str, state: dict, packet: dict) -> dict | None:
        """The short answer when this session's ACK of ``packet`` still holds; None otherwise (#325).

        Read-only: it writes nothing, is no acknowledgement and extends none. The write gate stays require_ack.
        """
        session = self._session(session_id)  # read again: a hook may have moved the epoch since
        if session.get("packet_id") != packet["id"] or self._write_refusal(session, packet["id"]):
            return None
        reply = {"session_id": session["id"], "epoch": session["epoch"], "status": "ready", "unchanged": True,
                 "acknowledged": True, "packet_id": packet["id"], "packet_hash": packet["hash"],
                 "newer_automatic_proposals": self._newer_count(state, packet), "note": UNCHANGED_NOTE}
        if session["client"] == EXTERNAL_CLIENT:
            reply.update(history="unarchived", evidence="degraded")  # an acknowledged external session stays degraded
        return reply

    @staticmethod
    def _refuse_ack(session: dict, packet: dict | None, packet_id: str, packet_hash: str, readback: dict) -> None:
        """Raise unless ``packet`` is the session's ready current packet and ``readback`` reads it exactly."""
        if (packet and packet["status"] != "ready" and packet["id"] == packet_id
                and packet["hash"] == packet_hash and packet["epoch"] == session["epoch"]):
            # The exact current packet: say why it cannot be acknowledged instead of "does not match".
            status = "unavailable" if packet["status"] == "unavailable" else "partial"
            unknowns = [str(value) for value in packet.get("unknowns") or []]
            reason = (unknowns[0][:500] + (f" (and {len(unknowns) - 1} more)" if len(unknowns) > 1 else "")
                      if unknowns else "its code state cannot be checked" if status == "unavailable"
                      else "its archive is incomplete")
            raise ValueError(f"resume_required: the current packet is {status}, not ready, and cannot be acknowledged: {reason}")
        if (not packet or packet["status"] != "ready" or packet["id"] != packet_id
                or packet["hash"] != packet_hash or packet["epoch"] != session["epoch"]
                or packet.get("schema_version") != PACKET_SCHEMA_VERSION):
            raise ValueError("resume_required: ACK does not match a ready current packet")
        valid_types = (isinstance(readback, dict) and set(readback) == {"plan", "task"}
                       and all(value is None or (isinstance(value, dict)
                               and set(value) == {"id", "version"} and isinstance(value["id"], str)
                               and type(value["version"]) is int and value["version"] > 0)
                               for value in readback.values()))
        if not valid_types or readback != _readback(packet):
            raise ValueError("resume_required: readback must match active plan/task IDs and versions")

    def ack(self, session_id: str, packet_id: str, packet_hash: str, readback: dict) -> dict:
        # Refused early without hashing the worktree; everything is checked again in the transaction.
        session = self._session(session_id)
        self._refuse_ack(session, self._packet(session), packet_id, packet_hash, readback)
        # The code state is taken before the writer transaction, which then holds only database requests:
        # hashing a slow worktree inside it let ArcadeDB expire it (#255).
        code = self._code(session)
        with self.store.transaction() as tx:
            session = db_find_one(tx, "sessions", {"id": session_id}, required=True)
            if session is None:
                raise LookupError(f"Unknown session: {session_id}")
            packet = self._packet(session)
            self._refuse_ack(session, packet, packet_id, packet_hash, readback)
            if packet is None:
                raise ValueError("resume_required: ACK does not match a ready current packet")
            continuity = self._own_continuity(session)
            if continuity:
                raise ValueError("resume_required: " + continuity[0])
            state = self._state_locked(session, code)
            if state["state_hash"] != packet["state_hash"]:
                raise ValueError("resume_required: relevant project state or code changed before ACK")
            previous = db_find_one(tx, "resume_acks", {"id": session.get("ack_id")})
            if previous and previous.get("packet_id") == packet_id:
                return self._with_newer(previous, state, packet)
            acknowledgment = {"id": str(uuid4()), "project_id": session["project_id"], "session_id": session_id,
                              "epoch": session["epoch"], "packet_id": packet_id, "packet_hash": packet_hash,
                              "readback": readback, "status": "acknowledged", "created_at": utc_now(),
                              "meaning": "Exact packet receipt/readback; not proof of understanding"}
            db_insert_document(tx, "resume_acks", acknowledgment)
            db_update_where(tx, "sessions", set_fields={"ack_id": acknowledgment["id"], "updated_at": utc_now()},
                            where={"id": session_id})
            return self._with_newer(acknowledgment, state, packet)

    def note_event(self, payload: dict, client: str) -> None:
        """Called after spool import by the service; native hook performs no DB I/O."""
        if client not in {"codex", "claude"} or not isinstance(payload, dict):
            raise ValueError("Invalid client hook event")
        event = payload.get("hook_event_name")
        if event not in {"SessionStart", "PreCompact", "Stop", "SessionEnd"}:
            return
        # An event spooled by a hook of an earlier version carries the earlier key.
        native_id = payload.get("session_id")
        logical_id = payload.get("cairnmind_session_id") or payload.get("devbrain_session_id")
        cwd = payload.get("cwd")
        project = self.store.resolve_project(cwd, recorded=True)
        # Most-specific registered root, not an inherited project environment hint.
        roots = db_find(self.store, "project_roots", {"project_id": project["id"]}, columns=("path",))
        matching = [str(Path(row["path"]).resolve()) for row in roots if _scope_contains(str(Path(row["path"]).resolve()), cwd)]
        if not matching:
            raise ValueError("Hook working directory is not a registered project scope")
        root = max(matching, key=len)
        event_id = payload.get("event_id") or _hash(payload)
        with self.store.transaction() as tx:
            self._require_scope(tx, project["id"], root)
            receipt = db_find_one(tx, "session_events", {"id": event_id})
            if receipt:
                old_session = db_find_one(tx, "sessions", {"id": receipt["session_id"]}, required=True)
                if old_session is None:
                    raise LookupError(f"Unknown session: {receipt['session_id']}")
                if ((old_session["project_id"], old_session["client"], old_session["worktree"])
                        != (project["id"], client, root) or (logical_id and logical_id != old_session["id"])):
                    raise ValueError("Hook event ID is already bound to another logical session")
                return
            session = db_find_one(tx, "sessions", {"id": logical_id}) if logical_id else None
            if logical_id and not session:
                raise ValueError("Hook refers to an unknown logical session")
            if not session and native_id:
                rows = db_find(
                    tx,
                    "sessions",
                    {"project_id": project["id"], "client": client, "native_session_id": native_id},
                    order_by=(desc("updated_at"),),
                    limit=1,
                )
                session = clean_row(rows[0]) if rows else None
            if session and (session["project_id"], session["client"], session["worktree"]) != (project["id"], client, root):
                raise ValueError("Hook session does not match client/project/worktree")
            if not session:
                session = {"id": str(uuid4()), "project_id": project["id"], "client": client, "worktree": root,
                           "native_session_id": native_id, "epoch": 1, "status": "partial", "packet_id": None,
                           "ack_id": None, "transcript_targets": [], "event_ids": [], "created_at": utc_now()}
                db_insert_document(tx, "sessions", session)
            if native_id:
                if session.get("native_session_id") and native_id != session["native_session_id"]:
                    raise ValueError("Logical session is already bound to a different native session")
                session["native_session_id"] = native_id
            reason = payload.get("resume_reason") or payload.get("source") or "startup"
            if event == "PreCompact":
                session.update(epoch=session["epoch"] + 1, packet_id=None, ack_id=None,
                               status="partial", precompact_pending=True, resume_targets=None, reason="compact")
            elif event == "SessionStart":
                prepared_start = bool(session.get("pending_start", False)) and reason != "compact"
                precompacted = reason == "compact" and bool(session.get("precompact_pending", False))
                # db_update_where updates supplied fields; popping would leave persisted
                # true flags behind and suppress every later native boundary.
                session["pending_start"] = False
                session["precompact_pending"] = False
                if reason in {"resume", "clear", "fork", "compact"} and not prepared_start and not precompacted:
                    session.update(epoch=session["epoch"] + 1, packet_id=None, ack_id=None,
                                   status="partial", resume_targets=None)
                session["reason"] = reason
            if payload.get("transcript_path"):
                target = self._target(payload["transcript_path"], client, payload.get("transcript_watermark"))
                targets = session.get("transcript_targets", [])
                if target not in targets:
                    targets.append(target)
                session["transcript_targets"] = targets
            db_insert_document(tx, "session_events", {"id": event_id, "project_id": project["id"],
                                                      "session_id": session["id"], "native_session_id": native_id,
                                                      "client": client, "hook_event_name": event,
                                                      "resume_reason": reason, "transcript_path": payload.get("transcript_path"),
                                                      "transcript_watermark": self._encode_watermark(payload.get("transcript_watermark")),
                                                      "applied_epoch": session["epoch"], "created_at": utc_now()})
            session.update(updated_at=utc_now())
            db_update_where(tx, "sessions", set_fields=session, where={"id": session["id"]})
