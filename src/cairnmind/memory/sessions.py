"""Deterministic, versioned resume packets and actual client acknowledgments.

The launcher can refuse an unready packet.  An ACK proves receipt/readback of a
particular packet, not comprehension, and never changes native CLI permissions.
Preparing a packet only waits for durable import cursors, never model inference.
"""
from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from cairnmind.agents.recorder_agent import RECORDER_PATH, agent_file
from cairnmind.memory.sessions_archive import _SessionArchiveMixin
from cairnmind.memory.sessions_packets import (
    AGENT_CLIENTS as AGENT_CLIENTS,
)
from cairnmind.memory.sessions_packets import (
    EXTERNAL_CLIENT as EXTERNAL_CLIENT,
)
from cairnmind.memory.sessions_packets import (
    EXTERNAL_WARNING as EXTERNAL_WARNING,
)
from cairnmind.memory.sessions_packets import (
    MAX_PACKET_BYTES as MAX_PACKET_BYTES,
)
from cairnmind.memory.sessions_packets import (
    MAX_RECORDS as MAX_RECORDS,
)
from cairnmind.memory.sessions_packets import (
    PACKET_SCHEMA_VERSION as PACKET_SCHEMA_VERSION,
)
from cairnmind.memory.sessions_packets import (
    RESUME_REASONS as RESUME_REASONS,
)
from cairnmind.memory.sessions_packets import (
    UNCHANGED_NOTE as UNCHANGED_NOTE,
)
from cairnmind.memory.sessions_packets import (
    _hash as _hash,
)
from cairnmind.memory.sessions_packets import (
    _readback as _readback,
)
from cairnmind.memory.sessions_packets import (
    _scope_contains as _scope_contains,
)
from cairnmind.memory.sessions_packets import (
    _SessionPacketMixin,
)
from cairnmind.memory.sessions_packets import (
    format_packet as format_packet,
)
from cairnmind.storage.domain_types import (
    MemoryRecord,
    ProjectRecord,
    ResumePacketRecord,
    SessionRecord,
    SourceRecord,
)
from cairnmind.storage.sql import clean_row
from cairnmind.storage.sql import json_text as json_text
from cairnmind.storage.storage_query import (
    db_find,
    db_find_one,
    eq,
    is_in,
    is_not_null,
)
from cairnmind.store import EXTRACTION_MARK


def origin_mark(session: SessionRecord | Mapping[str, Any]) -> dict[str, str] | None:
    """The mark a write made in this session carries, derived from the session row alone."""
    if session.get("client") != EXTERNAL_CLIENT:
        return None
    return {"origin_client": EXTERNAL_CLIENT, "origin_session_id": session["id"],
            "origin_history": "unarchived", "origin_evidence": "degraded"}


STALE_REASONS = ("state_changed", "packet_changed", "ack_mismatch", "scope_unavailable", "continuity")


def stale_reason(message: str) -> str:
    """Why a kept ACK no longer holds, as a fixed code from the sentence require_ack raised (#161)."""
    if "changed since the acknowledged packet" in message:
        return "state_changed"  # a record, plan, task or code changed, a tool result's record included
    if "fetch and acknowledge the current ready resume packet" in message:
        return "packet_changed"
    if "no actual model ACK" in message:
        return "ack_mismatch"
    if "scope cannot be restored" in message:
        return "scope_unavailable"
    return "continuity"


def _current_schema(packet: ResumePacketRecord | Mapping[str, Any]) -> bool:
    return packet.get("schema_version") == PACKET_SCHEMA_VERSION


def _registered_roots(reader: Any) -> list[str]:
    """Every registered project root as stored: the rows that decide what a code manifest leaves out (#255)."""
    return sorted({row["path"] for row in db_find(reader, "project_roots", columns=("path",))
                   if isinstance(row.get("path"), str)})


def _automatic_proposal(
    record: MemoryRecord | Mapping[str, Any],
    sources_by_id: Mapping[str, SourceRecord | Mapping[str, Any]],
) -> bool:
    """Whether ``record`` is a proposal the extraction worker wrote and nobody has touched since (#256).

    Such a proposal is not part of the state an ACK is given for: the packet shows it, labelled, but does
    not promise it. Decided only from what the store wrote itself: the extraction mark, which only the
    worker's own write sets, a client can neither set nor keep, any later write drops and a memory package
    never carries in. The origin label is a client's to write and plays no part. A confirmed, disputed,
    edited, superseded or imported record, and every write of a person or a session, is not one.
    """
    metadata = record.get("metadata")
    mark = metadata.get(EXTRACTION_MARK) if isinstance(metadata, dict) else None
    return bool(isinstance(mark, str) and record.get("status") == "proposed" and record.get("version") == 1
                and record.get("source_ids") == [mark]
                and sources_by_id.get(mark, {}).get("project_id") == record.get("project_id"))


def _code_identity(code: Mapping[str, Any]) -> dict[str, Any]:
    return {key: code.get(key) for key in
            ("status", "worktree", "head", "branch", "fingerprint", "manifest_hash",
             "unverified_scope_fingerprint")}


def _managed_empty_file(
    relative: str,
    raw: bytes,
    settings: Any,
    project: ProjectRecord | Mapping[str, Any],
) -> bool:
    """Recognize our complete generated files; a filename/substring is not enough."""
    from cairnmind.memory.integrations import BEGIN, END, _command, _earlier_command, _instructions

    try:
        text = raw.decode("utf-8").replace("\r\n", "\n")
        project_dict = cast(dict[str, Any], project)
        if relative == RECORDER_PATH:
            return text == agent_file(settings, project_dict)
        if relative in {"AGENTS.md", "CLAUDE.md"}:
            # Existing user instructions surrounding our block are real prior state.
            return (text.strip() == _instructions(project_dict, settings).strip()
                    and text.count(BEGIN) == 1 and text.count(END) == 1)
        # A file generated before the module was renamed is as much ours as a new one.
        return any(_managed_command_file(relative, text, settings, builder)
                   for builder in (_command, _earlier_command))
    except (UnicodeDecodeError, ValueError, TypeError):
        return False


def _managed_command_file(
    relative: str,
    text: str,
    settings: Any,
    _command: Callable[..., list[str]],
) -> bool:
    """Whether a generated MCP or hook file is exactly what `_command` would produce."""
    from cairnmind.memory.integrations import (
        EARLIER_SERVER,
        EARLIER_SESSION_ENV_VARS,
        EARLIER_TOML_BEGIN,
        EARLIER_TOML_END,
        SERVER,
        SESSION_ENV_VARS,
        TOML_BEGIN,
        TOML_END,
        codex_tool_timeout,
    )

    command = _command(settings, "mcp")
    server = {"command": command[0], "args": command[1:]}
    # What this version generates, and what versions up to 0.9.0 generated under the earlier names.
    shapes = ((SERVER, SESSION_ENV_VARS, TOML_BEGIN, TOML_END),
              (EARLIER_SERVER, EARLIER_SESSION_ENV_VARS, EARLIER_TOML_BEGIN, EARLIER_TOML_END))
    if relative == ".mcp.json":
        content = json.loads(text)
        return any(content == {"mcpServers": {key: {**server, "env": {name: "${" + name + ":-}" for name in forwarded}}}}
                   for key, forwarded, _, _ in shapes)
    if relative == ".codex/config.toml":
        content = tomllib.loads(text)
        listed = (content.get("mcp_servers") or {}).get(SERVER, {}).get("env_vars")
        if isinstance(listed, list) and sorted(map(str, listed)) == sorted(SESSION_ENV_VARS):
            # A block of an earlier version converted by install lists the same names in another order.
            content["mcp_servers"][SERVER]["env_vars"] = list(SESSION_ENV_VARS)
        # Versions up to 0.11 wrote 75 whatever the question budget was.
        waits = {75, codex_tool_timeout(settings)}
        return any(content == {"mcp_servers": {key: {**server, "env_vars": list(forwarded),
                                                     "startup_timeout_sec": 15, "tool_timeout_sec": wait}}}
                   and text.strip().startswith(begin) and text.strip().endswith(end)
                   and text.count(begin) == text.count(end) == 1
                   for key, forwarded, begin, end in shapes for wait in waits)
    client = "codex" if relative == ".codex/hooks.json" else "claude"
    hooks = {}
    from cairnmind.pal.process_control import format_command
    for event in ("SessionStart", "PreCompact", "Stop", "SessionEnd"):
        handler = {"type": "command", "command": format_command(_command(settings, "hook", "--client", client)),
                   "timeout": 10 if event == "SessionStart" else 3}
        if client == "codex" and event == "SessionStart":
            handler["additionalContextLimit"] = 0
        group: dict[str, Any] = {"hooks": [handler]}
        if event == "SessionStart":
            group["matcher"] = ("startup|resume|clear|compact|fork" if client == "codex"
                                else "startup|resume|clear|compact")
        hooks[event] = [group]
    return json.loads(text) == {"hooks": hooks}


def _empty_scope(
    root: str,
    settings: Any,
    project: ProjectRecord | Mapping[str, Any],
) -> tuple[bool, str, set[str]]:
    """Only a new empty directory and identifiable managed integration files."""
    allowed = {"AGENTS.md", "CLAUDE.md", ".mcp.json", ".codex/config.toml", ".codex/hooks.json",
               ".claude/settings.json", ".claude/settings.local.json", RECORDER_PATH}
    paths, inventory, eligible = set(), [], True
    try:
        for child in sorted(Path(root).iterdir()):
            if child.name == ".git":
                continue
            candidates = list(child.iterdir()) if child.is_dir() and child.name in {".codex", ".claude"} else [child]
            # The recorder subagent's folder (#376): only its own file in it is allowed.
            agents = child / "agents"
            if child.name == ".claude" and agents.is_dir() and not agents.is_symlink():
                candidates = [path for path in candidates if path != agents] + list(agents.iterdir())
            for path in sorted(candidates):
                relative = path.relative_to(root).as_posix()
                if path.is_symlink() or not path.is_file() or relative not in allowed or path.stat().st_size > 1048576:
                    eligible = False
                    inventory.append([relative, "not-an-empty-managed-scope"])
                    continue
                raw = path.read_bytes()
                if not _managed_empty_file(relative, raw, settings, project):
                    eligible = False
                inventory.append([relative, hashlib.sha256(raw).hexdigest()])
                paths.add(str(path))
        return eligible, _hash(inventory), paths
    except OSError:
        return False, _hash("unreadable-scope"), paths


class SessionManager(_SessionArchiveMixin, _SessionPacketMixin):
    def __init__(self, store: Any, settings: Any) -> None:
        self.store, self.settings = store, settings

    def _packet_byte_budget(self) -> int:
        return MAX_PACKET_BYTES

    def _max_records(self) -> int:
        return MAX_RECORDS

    def _schema_version(self) -> int:
        return PACKET_SCHEMA_VERSION

    def _is_current_schema(self, packet: ResumePacketRecord | Mapping[str, Any]) -> bool:
        return _current_schema(packet)

    def _session(self, session_id: str) -> dict[str, Any]:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("A logical session ID is required")
        session = db_find_one(self.store, "sessions", {"id": session_id}, required=True)
        if session is None:
            raise LookupError(f"Unknown session: {session_id}")
        removed = db_find_one(
            self.store, "projects", [eq("id", session.get("project_id")), is_not_null("removed_at")],
        )
        if removed:  # every call of a session of a removed project names the removal, the way back included (#327)
            from cairnmind.memory.project_removal import removed_text
            raise ValueError(f"Unknown project: {removed['id']}. " + removed_text(removed, self.store.data_dir))
        return session

    def _packet(self, session: SessionRecord | Mapping[str, Any]) -> dict[str, Any] | None:
        packet_id = session.get("packet_id")
        if not packet_id:
            return None
        row = db_find_one(self.store, "resume_packets", {"id": packet_id})
        return row.get("payload") if row else None

    def _own_continuity(self, session: SessionRecord | Mapping[str, Any]) -> list[str]:
        if session.get("client") not in AGENT_CLIENTS:
            return []
        from cairnmind.agents.agent_memory import agent_continuity_unknowns
        return agent_continuity_unknowns(self.store, self.settings, cast(dict[str, Any], session))

    def _root(self, project_id: str, worktree: str | None) -> tuple[ProjectRecord, str]:
        project = cast(ProjectRecord, self.store.resolve_project(project_id))
        root = str(Path(worktree or project["root"]).expanduser().resolve())
        roots = db_find(self.store, "project_roots", {"project_id": project["id"]}, columns=("path",))
        if root not in {str(Path(row["path"]).resolve()) for row in roots}:
            raise ValueError("Resume worktree must be an exact registered project root")
        if not Path(root).is_dir():
            raise ValueError("Resume worktree does not exist")
        return project, root

    def _require_scope(self, tx: Any, project_id: str, root: str) -> None:
        """Writer-transaction recheck; prior path resolution may race forgetting."""
        self.store._require_project(tx, project_id)
        roots = db_find(tx, "project_roots", {"project_id": project_id}, columns=("path",))
        if root not in {str(Path(row["path"]).expanduser().resolve()) for row in roots}:
            raise ValueError("Resume worktree is no longer a registered project root")

    def _code(self, session: dict) -> dict:
        """The code state of a session's worktree: Git and every file hashed, twice (#255).

        Slow where the worktree is (seconds on a /mnt/c checkout under WSL, more while indexing reads the
        same files), and it reads no row that a transaction would protect. So it runs before the writer
        transaction that compares it, never inside one: ArcadeDB ends a server transaction that sees no
        request for about five seconds ("Remote transaction ... not found or expired"). A failure is kept
        and raised where the state is computed, so callers see the error they saw before.

        The state is the worktree as it was just before the transaction began, including any wait for the
        writer lock; the transaction never held the file system still either. Which registered roots below
        the worktree the manifest leaves out is a database read made here, outside the transaction, so
        the registered roots are kept with it and compared again inside (_state_locked).
        """
        from cairnmind.memory.verification import capture_code_state

        project_id, root = session["project_id"], session["worktree"]
        result: dict[str, Any] = {"scope": (project_id, root)}
        try:
            # Read before the capture, which reads them again: a registration in between makes the
            # comparison in the transaction refuse rather than miss it.
            result["roots"] = _registered_roots(self.store)
            project, _ = self._root(project_id, root)
            code = capture_code_state(self.store, project_id, worktree=root)
            result["code"] = code
            if code.get("status") != "fresh":
                result["empty_scope"] = _empty_scope(root, self.settings, project)
        except (OSError, ValueError) as exc:
            result["error"] = exc
        return result

    def code_scope(self, session_id: str, packet_id: str | None = None) -> dict:
        """The code state for ``require_ack(..., code=)``, taken before the caller opens its transaction.

        A session that require_ack refuses before it compares any code state (unknown, not acknowledged, or
        another packet named) is not hashed: hashing a slow worktree only to refuse would cost seconds. If
        such a session became acknowledged before the caller's transaction, require_ack refuses this one
        write with the kept error instead of hashing inside that transaction; the client retries it.
        """
        try:
            session = self._session(session_id)
            refusal = self._write_refusal(session, packet_id)
        except ValueError as exc:
            session, refusal = None, str(exc)
        if refusal or session is None:
            scope = (session["project_id"], session["worktree"]) if session else None
            return {"scope": scope, "error": ValueError(
                "the session was not acknowledged when this write began; retry it")}
        return self._code(session)

    def _state(self, session: dict, code: dict | None = None) -> dict:
        """Relevant committed state only; transcript technical tails are excluded."""
        if code is None:
            code = self._code(session)
        with self.store.transaction():
            return self._state_locked(session, code)

    def _state_locked(self, session: dict, code: dict) -> dict:
        """The state of the rows, read in the caller's transaction, with ``code`` taken before it (_code)."""
        from cairnmind.memory.memory_paths import bound_metadata, memory_bindings
        from cairnmind.memory.plans import PlanManager

        project_id, root = session["project_id"], session["worktree"]
        if code.get("scope") != (project_id, root):
            raise ValueError("resume_required: the code state was taken for another session scope")
        if "error" in code:
            raise code["error"]
        if _registered_roots(self.store) != code["roots"]:
            raise ValueError("resume_required: registered project roots changed while the code state was taken")
        self._root(project_id, root)
        following = PlanManager(self.store).next_task(project_id, worktree=root)
        plan, task = following.get("plan"), following.get("task")
        state_rows = db_find(self.store, "worktree_states", {"project_id": project_id, "worktree": root})
        worktree_state = clean_row(state_rows[0]) if state_rows else None
        records = self.store.list_records(project_id)
        bindings = memory_bindings(self.store, project_id)
        relevant, resolved = [], {}
        for record in records:
            metadata = bound_metadata(self.store, project_id, record.get("metadata"), bindings=bindings)
            kind = record["kind"]
            record_root = metadata.get("worktree") or metadata.get("worktree_root")
            # Each distinct root is resolved once: resolving reads the file system, and in a transaction
            # time spent between two requests counts towards its expiry (#255).
            if record_root and record_root not in resolved:
                resolved[record_root] = str(Path(record_root).expanduser().resolve())
            if record_root and resolved[record_root] != root:
                continue
            if kind in {"decision", "constraint", "preference", "checkpoint"} or kind == "task" and ((plan and metadata.get("plan_ref") == plan["id"])
                                     or not metadata.get("plan_ref")):
                relevant.append(record)
        relevant.sort(key=lambda row: (row["kind"], row["id"]))
        current_items = [*([plan] if plan else []), *([task] if task else [])]
        all_source_ids = sorted({source_id for row in [*relevant, *current_items]
                                 for source_id in row.get("source_ids", [])})
        source_rows = db_find(
            self.store, "sources", [is_in("id", all_source_ids)],
            columns=("id", "project_id", "sha256"),
        ) if all_source_ids else []
        sources_by_id = {row["id"]: row for row in source_rows}
        missing_sources = [source_id for source_id in all_source_ids
                           if sources_by_id.get(source_id, {}).get("project_id") != project_id]
        # A proposal the extraction worker wrote and nobody touched since stays in the packet but not in the
        # state an ACK is given for (#256): only its arrival is not a change; any write to it is.
        acknowledged = [row for row in relevant if not _automatic_proposal(row, sources_by_id)]
        source_ids = sorted({source_id for row in [*acknowledged, *current_items]
                             for source_id in row.get("source_ids", [])})
        state_unknowns = [f"A current plan/task/memory reference has no scoped durable source: {value}"
                          for value in missing_sources]
        code, scope = dict(code["code"]), code
        initial_baseline = False
        if code.get("status") != "fresh":
            empty_scope, inventory_hash, managed_paths = scope["empty_scope"]
            code["unverified_scope_fingerprint"] = inventory_hash
            sources = db_find(self.store, "sources", {"project_id": project_id}, columns=("uri", "kind"))
            initial_baseline = (empty_scope and not plan and not records and
                                all(row.get("kind") in {"code", "document"} and row.get("uri") in managed_paths
                                    for row in sources))
        identity = {"project_id": project_id, "worktree": root,
                    "worktree_state": ({key: worktree_state.get(key)
                                       for key in ("id", "version", "active_plan_id")}
                                      if worktree_state else None),
                    "plan": ({"id": plan["id"], "version": plan["version"],
                              "context_hash": plan.get("context_hash")} if plan else None),
                    "task": ({"id": task["id"], "version": task["version"]} if task else None),
                    "records": [[row["id"], row["version"], row["status"]] for row in acknowledged],
                    "sources": [[source_id, sources_by_id.get(source_id, {}).get("project_id"),
                                 sources_by_id.get(source_id, {}).get("sha256")] for source_id in source_ids],
                    "code": _code_identity(code)}
        return {"plan": plan, "task": task, "reason": following.get("reason"),
                "blocked": following.get("blocked", []), "records": relevant,
                "code": code, "state_hash": _hash(identity), "unknowns": state_unknowns,
                "initial_baseline_eligible": initial_baseline,
                # Not part of the state hash: only for telling a client what its packet does not show (#299).
                "automatic_created": [row["created_at"] for row in relevant
                                      if _automatic_proposal(row, sources_by_id)]}

    @staticmethod
    def _with_newer(reply: dict, state: dict, packet: dict) -> dict:
        """``reply`` plus how many automatic proposals are newer than ``packet``; unchanged when none (#299).

        Information only: read after the state hash was compared, it decides nothing. The key is never stored.
        """
        newer = SessionManager._newer_count(state, packet)
        return {**reply, "newer_automatic_proposals": newer} if newer else reply

    @staticmethod
    def _newer_count(state: dict, packet: dict) -> int:
        return sum(created > packet["created_at"] for created in state["automatic_created"])

    def _write_refusal(self, session: dict, packet_id: str | None) -> str | None:
        """require_ack's refusals that need no code state, from database reads only; None when none applies."""
        packet = self._packet(session)
        if (not packet or packet.get("status") != "ready" or packet["epoch"] != session["epoch"]
                or (packet_id is not None and packet_id != packet["id"]) or not session.get("ack_id")):
            return "resume_required: fetch and acknowledge the current ready resume packet"
        acknowledgment = db_find_one(self.store, "resume_acks", {"id": session["ack_id"]})
        if (not acknowledgment or acknowledgment.get("packet_id") != packet["id"]
                or acknowledgment.get("packet_hash") != packet["hash"]
                or acknowledgment.get("epoch") != session["epoch"]):
            return "resume_required: current packet has no actual model ACK"
        if not _current_schema(packet):
            return ("resume_required: the acknowledged packet was made by an earlier version of Cairnmind; "
                    "fetch and acknowledge the current ready resume packet")
        return None

    def require_ack(self, session_id: str, packet_id: str | None = None, code: dict | None = None) -> None:
        """Read-only; safe inside the caller's writer transaction; no import locks.

        Inside a transaction pass ``code``, from code_scope taken before it began: without it the worktree is
        hashed here, and a slow worktree let ArcadeDB expire the caller's transaction (#255).
        """
        self._acknowledged_state(session_id, packet_id, code)

    def _acknowledged_state(self, session_id: str, packet_id: str | None, code: dict | None) -> tuple[dict, dict]:
        """require_ack's checks; the current state and the packet when they all hold."""
        session = self._session(session_id)
        refusal = self._write_refusal(session, packet_id)
        if refusal:
            raise ValueError(refusal)
        packet = self._packet(session)
        if packet is None:
            raise ValueError("resume_required: fetch and acknowledge the current ready resume packet")
        continuity = self._own_continuity(session)
        if continuity:
            raise ValueError("resume_required: " + continuity[0])
        try:
            current = self._state(session, code)
        except (OSError, ValueError) as exc:
            raise ValueError(f"resume_required: current project scope cannot be restored ({exc})") from None
        if current["state_hash"] != packet["state_hash"]:
            raise ValueError("resume_required: plan, task, decision, scope or code changed since the acknowledged "
                             "packet (a checkpoint or decision this session saved counts too); call resume_context "
                             "and ack_resume its new packet")
        return current, packet

    def status(self, session_id: str) -> dict:
        session = self._session(session_id)
        packet = self._packet(session)
        result = {**self._result(session, packet), "client": session["client"],
                  "worktree": session["worktree"], "project_id": session["project_id"],
                  "native_session_id": session.get("native_session_id"), "acknowledged": False, "stale": False}
        if session["client"] == EXTERNAL_CLIENT:
            # An acknowledged external session is still not a verified one.
            result.update(history="unarchived", evidence="degraded")
        try:
            current, current_packet = self._acknowledged_state(session_id, None, None)
            result["acknowledged"] = True
            result = self._with_newer(result, current, current_packet)
        except ValueError as exc:
            result["ack_reason"] = str(exc)
            result["stale"] = bool(session.get("ack_id"))
            if result["stale"]:
                result["stale_reason"] = stale_reason(str(exc))
        return result
