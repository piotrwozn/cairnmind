"""Deterministic, versioned resume packets and actual client acknowledgments.

The launcher can refuse an unready packet.  An ACK proves receipt/readback of a
particular packet, not comprehension, and never changes native CLI permissions.
Preparing a packet only waits for durable import cursors, never model inference.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import time
import tomllib
from pathlib import Path
from typing import Any
from uuid import uuid4

from .store import _clean, _insert, _json, _now, _one, _set

MAX_PACKET_BYTES = 24576
MAX_RECORDS = 20
RESUME_REASONS = {"startup", "resume", "compact", "clear", "fork", "refresh"}
AGENT_CLIENTS = {"api", "copilot"}


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _scope_contains(root: str, cwd: Any) -> bool:
    if not isinstance(cwd, str) or not cwd:
        return False
    try:
        return Path(cwd).expanduser().resolve().is_relative_to(Path(root))
    except (OSError, ValueError):
        return False


def _code_identity(code: dict) -> dict:
    return {key: code.get(key) for key in
            ("status", "worktree", "head", "branch", "fingerprint", "manifest_hash",
             "unverified_scope_fingerprint")}


def _managed_empty_file(relative: str, raw: bytes, settings, project: dict) -> bool:
    """Recognize our complete generated files; a filename/substring is not enough."""
    from .integrations import BEGIN, END, TOML_BEGIN, TOML_END, SESSION_ENV_VARS, _command, _instructions

    try:
        text = raw.decode("utf-8")
        if relative in {"AGENTS.md", "CLAUDE.md"}:
            # Existing user instructions surrounding our block are real prior state.
            return (text.strip() == _instructions(project, settings).strip()
                    and text.count(BEGIN) == 1 and text.count(END) == 1)
        command = _command(settings, "mcp")
        server = {"command": command[0], "args": command[1:]}
        if relative == ".mcp.json":
            return json.loads(text) == {"mcpServers": {"devbrain": {
                **server, "env": {name: "${" + name + ":-}" for name in SESSION_ENV_VARS}}}}
        if relative == ".codex/config.toml":
            expected = {"mcp_servers": {"devbrain": {**server, "env_vars": list(SESSION_ENV_VARS),
                                                    "startup_timeout_sec": 15, "tool_timeout_sec": 75}}}
            return (tomllib.loads(text) == expected and text.strip().startswith(TOML_BEGIN)
                    and text.strip().endswith(TOML_END)
                    and text.count(TOML_BEGIN) == text.count(TOML_END) == 1)
        client = "codex" if relative == ".codex/hooks.json" else "claude"
        hooks = {}
        for event in ("SessionStart", "PreCompact", "Stop", "SessionEnd"):
            handler = {"type": "command", "command": shlex.join(_command(settings, "hook", "--client", client)),
                       "timeout": 10 if event == "SessionStart" else 3}
            if client == "codex" and event == "SessionStart":
                handler["additionalContextLimit"] = 0
            group = {"hooks": [handler]}
            if event == "SessionStart":
                group["matcher"] = ("startup|resume|clear|compact|fork" if client == "codex"
                                    else "startup|resume|clear|compact")
            hooks[event] = [group]
        return json.loads(text) == {"hooks": hooks}
    except (UnicodeDecodeError, ValueError, TypeError):
        return False


def _empty_scope(root: str, settings, project: dict) -> tuple[bool, str, set[str]]:
    """Only a new empty directory and identifiable managed integration files."""
    allowed = {"AGENTS.md", "CLAUDE.md", ".mcp.json", ".codex/config.toml", ".codex/hooks.json",
               ".claude/settings.json", ".claude/settings.local.json"}
    paths, inventory, eligible = set(), [], True
    try:
        for child in sorted(Path(root).iterdir()):
            if child.name == ".git":
                continue
            candidates = list(child.iterdir()) if child.is_dir() and child.name in {".codex", ".claude"} else [child]
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


def _readback(packet: dict) -> dict:
    return {name: ({"id": packet[key]["id"], "version": packet[key]["version"]}
                   if packet.get(key) else None)
            for name, key in (("plan", "active_plan"), ("task", "active_task"))}


def format_packet(packet: dict) -> str:
    """Small hook/launcher bootstrap; the complete bounded packet is an MCP read."""
    plan, task = packet.get("active_plan"), packet.get("active_task")
    result = [
        "Cairnmind verified resume bootstrap (receipt is not proof of understanding).",
        f"Logical session: {packet['session_id']}; epoch: {packet['epoch']}.",
        f"Packet: {packet['id']}; SHA-256: {packet['hash']}.",
        f"Project: {packet['project_id']}; worktree: {packet['worktree']}.",
        "Before normal shared-memory writes, call devbrain.resume_context, read the complete "
        "packet, then call devbrain.ack_resume with its exact id/hash and this readback shape:",
        _json(_readback(packet)),
        "Do not invent missing state or mark tasks done from a summary. "
        "Native CLI permissions still apply to all native actions.",
    ]
    if plan:
        result.extend([f"Active plan v{plan['version']}: {plan.get('title', '')[:300]}",
                       f"Goal: {plan.get('goal', '')[:1200]}"])
        for constraint in plan.get("constraints", [])[:8]:
            result.append("Constraint: " + constraint[:240])
    if task:
        result.append(f"Active task {task['id']} v{task['version']} [{task.get('state', 'todo')}]: "
                      + task.get("content", "")[:1000])
    else:
        result.append("Active task: none; use the explicit reason in the complete packet.")
    if packet.get("no_history"):
        result.append("No prior conversation history is known for this project scope.")
    if packet.get("baseline_unverified"):
        result.append("New empty project: the code baseline is unverified. Initialize Git and commit "
                      "before relying on code-bound test completion.")
    result.append("Full constraints, source references, blockers, archive/index/extraction "
                  "status and omitted records are in the complete resume packet.")
    return "\n".join(result)[:6500]


class SessionManager:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    def _session(self, session_id: str) -> dict:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("A logical session ID is required")
        return _one(self.store, "SELECT FROM sessions WHERE id=:id", {"id": session_id})

    def _packet(self, session: dict) -> dict | None:
        if not session.get("packet_id"):
            return None
        row = _one(self.store, "SELECT FROM resume_packets WHERE id=:id",
                   {"id": session["packet_id"]}, False)
        return row.get("payload") if row else None

    def _own_continuity(self, session: dict) -> list[str]:
        if session.get("client") not in AGENT_CLIENTS:
            return []
        from .agent_memory import agent_continuity_unknowns
        return agent_continuity_unknowns(self.store, self.settings, session)

    def _root(self, project_id: str, worktree: str | None) -> tuple[dict, str]:
        project = self.store.resolve_project(project_id)
        root = str(Path(worktree or project["root"]).expanduser().resolve())
        roots = self.store.query("SELECT path FROM project_roots WHERE project_id=:project",
                                 {"project": project["id"]})
        if root not in {str(Path(row["path"]).resolve()) for row in roots}:
            raise ValueError("Resume worktree must be an exact registered project root")
        if not Path(root).is_dir():
            raise ValueError("Resume worktree does not exist")
        return project, root

    def _require_scope(self, tx, project_id: str, root: str) -> None:
        """Writer-transaction recheck; prior path resolution may race forgetting."""
        self.store._require_project(tx, project_id)
        roots = tx.query("SELECT path FROM project_roots WHERE project_id=:project", {"project": project_id})
        if root not in {str(Path(row["path"]).expanduser().resolve()) for row in roots}:
            raise ValueError("Resume worktree is no longer a registered project root")

    def _state(self, session: dict) -> dict:
        """Relevant committed state only; transcript technical tails are excluded."""
        with self.store.transaction():
            return self._state_locked(session)

    def _state_locked(self, session: dict) -> dict:
        from .plans import PlanManager
        from .verification import capture_code_state
        from .memory_paths import bound_metadata, memory_bindings

        project_id, root = session["project_id"], session["worktree"]
        project, _ = self._root(project_id, root)
        following = PlanManager(self.store).next_task(project_id, worktree=root)
        plan, task = following.get("plan"), following.get("task")
        state_rows = self.store.query(
            "SELECT FROM worktree_states WHERE project_id=:project AND worktree=:root",
            {"project": project_id, "root": root})
        worktree_state = _clean(state_rows[0]) if state_rows else None
        records = self.store.list_records(project_id)
        bindings = memory_bindings(self.store, project_id)
        relevant = []
        for record in records:
            metadata = bound_metadata(self.store, project_id, record.get("metadata"), bindings=bindings)
            kind = record["kind"]
            record_root = metadata.get("worktree") or metadata.get("worktree_root")
            if record_root and str(Path(record_root).expanduser().resolve()) != root:
                continue
            if kind in {"decision", "constraint", "preference", "checkpoint"}:
                relevant.append(record)
            elif kind == "task" and ((plan and metadata.get("plan_ref") == plan["id"])
                                     or not metadata.get("plan_ref")):
                relevant.append(record)
        relevant.sort(key=lambda row: (row["kind"], row["id"]))
        source_ids = sorted({source_id for row in [*relevant, *([plan] if plan else []), *([task] if task else [])]
                             for source_id in row.get("source_ids", [])})
        source_rows = self.store.query("SELECT id,project_id,sha256 FROM sources WHERE id IN :ids",
                                       {"ids": source_ids}) if source_ids else []
        sources_by_id = {row["id"]: row for row in source_rows}
        missing_sources = [source_id for source_id in source_ids
                           if sources_by_id.get(source_id, {}).get("project_id") != project_id]
        state_unknowns = [f"A current plan/task/memory reference has no scoped durable source: {value}"
                          for value in missing_sources]
        code = capture_code_state(self.store, project_id, worktree=root)
        initial_baseline = False
        if code.get("status") != "fresh":
            empty_scope, inventory_hash, managed_paths = _empty_scope(root, self.settings, project)
            code["unverified_scope_fingerprint"] = inventory_hash
            sources = self.store.query("SELECT uri,kind FROM sources WHERE project_id=:project", {"project": project_id})
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
                    "records": [[row["id"], row["version"], row["status"]] for row in relevant],
                    "sources": [[source_id, sources_by_id.get(source_id, {}).get("project_id"),
                                 sources_by_id.get(source_id, {}).get("sha256")] for source_id in source_ids],
                    "code": _code_identity(code)}
        return {"plan": plan, "task": task, "reason": following.get("reason"),
                "blocked": following.get("blocked", []), "records": relevant,
                "code": code, "state_hash": _hash(identity), "unknowns": state_unknowns,
                "initial_baseline_eligible": initial_baseline}

    @staticmethod
    def _target(path: str, client: str, watermark: dict | None = None) -> dict:
        result = {"path": str(Path(path).expanduser().resolve()), "client": client}
        if isinstance(watermark, dict):
            for key in ("size", "device", "inode", "mtime_ns", "head_sha256", "head_bytes",
                        "observed_size", "pending_tail_bytes", "pending_tail_sha256", "stable"):
                if key in watermark:
                    result[key] = watermark[key]
        if "size" not in result:
            try:
                from .integrations import _transcript_watermark
                result.update(_transcript_watermark(Path(result["path"])))
            except (OSError, ValueError) as exc:
                result["error"] = f"Transcript cannot be captured: {type(exc).__name__}"
        if type(result.get("size")) is not int or result.get("size", -1) < 0:
            result.setdefault("error", "Transcript EOF is unknown")
        return result

    @staticmethod
    def _same_generation(left: dict, right: dict) -> bool:
        """Short captured prefixes may grow on append without changing generation."""
        if (left.get("path"), left.get("device"), left.get("inode")) != (
                right.get("path"), right.get("device"), right.get("inode")):
            return False
        left_size = max(int(left.get("size", -1)), int(left.get("observed_size", -1)))
        right_size = max(int(right.get("size", -1)), int(right.get("observed_size", -1)))
        if left_size != right_size:
            try:
                actual = Path(left["path"]).stat()
                if (actual.st_size < max(left_size, right_size)
                        or (actual.st_dev, actual.st_ino) != (left.get("device"), left.get("inode"))):
                    return False
            except OSError:
                return False
        # A complete-prefix hash excludes a captured unfinished record. Never
        # merge away that separately bound requirement after its bytes change.
        tails = [row for row in (left, right) if row.get("pending_tail_sha256")]
        if tails:
            try:
                with Path(left["path"]).open("rb") as handle:
                    actual = os.fstat(handle.fileno())
                    if (actual.st_dev, actual.st_ino) != (left.get("device"), left.get("inode")):
                        return False
                    for row in tails:
                        start = row.get("captured_tail_start", row.get("size"))
                        end = row.get("observed_size")
                        if (type(start) is not int or type(end) is not int
                                or not 0 < end - start <= 2 * 1024 * 1024):
                            return False
                        handle.seek(start)
                        raw = handle.read(end - start)
                        if (len(raw) != end - start
                                or hashlib.sha256(raw).hexdigest() != row["pending_tail_sha256"]):
                            return False
            except OSError:
                return False
        if (left.get("head_bytes"), left.get("head_sha256")) == (
                right.get("head_bytes"), right.get("head_sha256")):
            return True
        if left.get("device") is None or left.get("inode") is None:
            return False
        try:
            with Path(left["path"]).open("rb") as handle:
                actual = os.fstat(handle.fileno())
                if (actual.st_dev, actual.st_ino) != (left["device"], left["inode"]):
                    return False
                head = handle.read(4096)
            return all(type(row.get("head_bytes")) is int and 0 <= row["head_bytes"] <= len(head)
                       and hashlib.sha256(head[:row["head_bytes"]]).hexdigest() == row.get("head_sha256")
                       for row in (left, right))
        except OSError:
            return False

    @classmethod
    def _merge_targets(cls, result: list[dict]) -> list[dict]:
        targets: list[dict] = []
        for target in result:
            for index, previous in enumerate(targets):
                if target.get("kind") == "agent_journal" or previous.get("kind") == "agent_journal":
                    if (target.get("kind") != previous.get("kind")
                            or target.get("agent_session_id") != previous.get("agent_session_id")
                            or target.get("path") != previous.get("path")
                            or target.get("error") or previous.get("error")):
                        continue
                    # A validated append extends one local journal generation.
                    # Equal-length conflicting digests must stay separately visible.
                    if target.get("sequence") == previous.get("sequence") and target.get("sha256") != previous.get("sha256"):
                        continue
                    if target.get("sequence") != previous.get("sequence"):
                        smaller = min((target, previous), key=lambda row: row.get("sequence", -1))
                        try:
                            amount = smaller.get("size")
                            if type(amount) is not int or not 0 <= amount <= 64 * 1024 * 1024:
                                continue
                            fd = os.open(smaller["path"], os.O_RDONLY | os.O_NOFOLLOW)
                            with os.fdopen(fd, "rb") as handle:
                                prefix = handle.read(amount)
                            if len(prefix) != amount or hashlib.sha256(prefix).hexdigest() != smaller.get("sha256"):
                                continue
                        except OSError:
                            continue
                    chosen = dict(max((target, previous), key=lambda row: row.get("sequence", -1)))
                    known = [row for row in (target, previous)
                             if row.get("proof", {}).get("status") == "complete"]
                    known.extend(row["known_prefix"] for row in (target, previous)
                                 if row.get("known_prefix", {}).get("proof", {}).get("status") == "complete")
                    full = next((row for row in known if row.get("sequence") == chosen.get("sequence")
                                 and row.get("sha256") == chosen.get("sha256")), None)
                    if full:
                        chosen["proof"] = full["proof"]
                        chosen.pop("known_prefix", None)
                    elif known:
                        prefix = max(known, key=lambda row: row.get("sequence", -1))
                        chosen["known_prefix"] = {key: prefix[key] for key in
                            ("kind", "path", "client", "agent_session_id", "sequence", "size", "sha256", "chain_hash", "proof")
                            if key in prefix}
                    targets[index] = chosen
                    break
                if cls._same_generation(previous, target):
                    required = max(int(target.get("size", -1)), int(target.get("observed_size", -1)))
                    previous_required = max(int(previous.get("size", -1)), int(previous.get("observed_size", -1)))
                    if required > previous_required or (required == previous_required
                                                        and target.get("proof") and not previous.get("proof")):
                        targets[index] = target
                    break
            else:
                targets.append(target)
        return sorted(targets, key=lambda row: (row["path"], str(row.get("device")),
                                                str(row.get("inode")), str(row.get("head_sha256"))))

    def _targets(self, session: dict, deadline: float) -> list[dict]:
        project, root = session["project_id"], session["worktree"]
        result = []
        rows = self.store.query("SELECT FROM sessions WHERE project_id=:project AND worktree=:root",
                                {"project": project, "root": root})
        for row in rows:
            result.extend(row.get("transcript_targets", []))
            if row.get("client") in AGENT_CLIENTS:
                from .agent_memory import capture_agent_target
                captured = capture_agent_target(self.settings, row)
                if captured is not None:
                    result.append(captured)
        cursors = self.store.query("SELECT FROM import_cursors WHERE project_id=:project", {"project": project})
        for cursor in cursors:
            if time.monotonic() > deadline:
                raise TimeoutError("Resume preparation deadline exceeded")
            metadata_rows = self.store.query(
                "SELECT metadata FROM sources WHERE project_id=:project AND metadata.import_path=:path LIMIT 1",
                {"project": project, "path": cursor["path"]})
            cwd = (metadata_rows[0].get("metadata") or {}).get("cwd") if metadata_rows else None
            if cwd and not _scope_contains(root, cwd):
                continue
            captured = self._target(cursor["path"], cursor.get("client", "auto"))
            if captured.get("error") and any(
                    row.get("path") == cursor["path"] and row.get("proof", {}).get("status") == "complete"
                    and row.get("size") == int(cursor.get("offset", 0)) for row in result):
                continue
            result.append(captured)
        # A durable hook must count even before the independent scanner drains it.
        spool = self.settings.data_dir / "hook-events"
        for path in sorted(spool.glob("*.json")):
            if time.monotonic() > deadline:
                raise TimeoutError("Resume preparation deadline exceeded")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not _scope_contains(root, payload.get("cwd")):
                    continue
                if payload.get("transcript_path"):
                    result.append(self._target(payload["transcript_path"], payload.get("client", "auto"),
                                               payload.get("transcript_watermark")))
            except (OSError, ValueError, TypeError):
                # Unreadable spool metadata cannot prove that its horizon was archived.
                result.append({"path": str(path), "client": "unknown",
                               "error": "Unreadable pending hook event; archive horizon is unknown"})
        # Preserve different generations and the largest captured horizon of each.
        return self._merge_targets(result)

    @staticmethod
    def _finish_captured_tail(target: dict) -> None:
        """Wait only for the record containing the original finite observed tail."""
        if not target.get("pending_tail_bytes"):
            return
        size, observed = target["size"], target.get("observed_size")
        if (type(observed) is not int or observed <= size
                or observed - size != target["pending_tail_bytes"]
                or observed - size > 2 * 1024 * 1024):
            raise ValueError("Captured unfinished JSONL tail has invalid bounds")
        with Path(target["path"]).open("rb") as handle:
            actual = os.fstat(handle.fileno())
            if ((target.get("device") is not None and target["device"] != actual.st_dev)
                    or (target.get("inode") is not None and target["inode"] != actual.st_ino)):
                raise ValueError("Captured transcript generation was rotated or truncated")
            head = handle.read(int(target.get("head_bytes", min(4096, size))))
            if target.get("head_sha256") and hashlib.sha256(head).hexdigest() != target["head_sha256"]:
                raise ValueError("Captured transcript prefix changed")
            handle.seek(size)
            original = handle.read(observed - size)
            if (len(original) != observed - size
                    or hashlib.sha256(original).hexdigest() != target.get("pending_tail_sha256")):
                raise ValueError("Captured unfinished JSONL tail changed")
            completion = handle.readline(2 * 1024 * 1024 + 1)
            if not completion.endswith(b"\n") or len(completion) > 2 * 1024 * 1024:
                raise ValueError("Captured EOF contains an incomplete JSONL range")
            completed = observed + len(completion)
            handle.seek(0)
            head = handle.read(min(4096, completed))
        target.update(size=completed, captured_tail_start=size, pending_tail_bytes=0, head_bytes=len(head),
                      head_sha256=hashlib.sha256(head).hexdigest(), tail_completed_at=completed)

    @staticmethod
    def _packet_targets(packet: dict) -> list[dict]:
        """Recover a pre-upgrade action's original horizons, including empty scope."""
        targets = []
        for archive in packet.get("archive_watermarks", []):
            target = dict(archive)
            if archive.get("status") == "complete":
                target["proof"] = {key: archive.get(key) for key in
                                   ("size", "status", "sha256", "archived_offset", "source_count", "source_ids_hash")}
                if archive.get("kind") == "agent_journal":
                    target["proof"].update({key: archive.get(key) for key in
                                           ("public_sources_hash", "sequence", "chain_hash")})
            targets.append(target)
        return targets

    def _legacy_action_targets(self, session: dict, packet: dict) -> list[dict] | None:
        """Recover an old launch boundary only when its native receipt proves it.

        Releases before fixed horizons could replace a ready launch packet with
        a partial refresh that chased its own newly created rollout. The first
        durable SessionStart identifies the already prepared action. A later
        lifecycle boundary prevents this inference; its requirements are kept.
        """
        events = self.store.query(
            "SELECT FROM session_events WHERE session_id=:session "
            "AND hook_event_name IN ['SessionStart','PreCompact'] ORDER BY created_at",
            {"session": session["id"]})
        if len(events) == 1:
            event = events[0]
            if (event.get("hook_event_name") == "SessionStart"
                    and event.get("resume_reason") in {"startup", "resume"}
                    and event.get("native_session_id")
                    and event.get("native_session_id") == session.get("native_session_id")):
                candidates = self.store.query(
                    "SELECT FROM resume_packets WHERE session_id=:session AND epoch=:epoch ORDER BY created_at DESC",
                    {"session": session["id"], "epoch": event.get("applied_epoch")})
                for row in candidates:
                    prepared = row.get("payload") or {}
                    if (row.get("created_at", "") <= event.get("created_at", "")
                            and prepared.get("status") == "ready"
                            and not prepared.get("omitted_archive_watermarks")):
                        return self._packet_targets(prepared)
        if (not packet.get("omitted_archive_watermarks")
                and (packet.get("status") == "ready" or packet.get("archive_watermarks"))):
            return self._packet_targets(packet)
        return None

    def _recover_capture_target(self, session: dict, target: dict, deadline: float) -> dict | None:
        """Resolve a never-captured startup request from a later durable receipt.

        No physical EOF is recaptured here. A bounded target, partial tail or
        generation conflict is never replaced by this compatibility repair.
        The caller still has to prove every byte of the fixed later watermark.
        """
        allowed = {"path", "client", "error", "status", "archived_offset", "unknowns"}
        if (set(target) - allowed or target.get("error") not in {
                "Transcript EOF is unknown", "Transcript cannot be captured: FileNotFoundError"}
                or target.get("client") not in {"codex", "claude"}
                or not isinstance(target.get("path"), str)
                or type(target.get("archived_offset", 0)) is not int or target.get("archived_offset", 0) != 0
                or ("status" in target and target["status"] != "partial")
                or ("unknowns" in target and target["unknowns"] != [target["error"]])):
            return None
        path = str(Path(target["path"]).expanduser().resolve())
        if path != target["path"]:
            return None
        events = self.store.query(
            "SELECT FROM session_events WHERE project_id=:project AND client=:client "
            "AND transcript_path=:path ORDER BY created_at,id",
            {"project": session["project_id"], "client": target["client"], "path": path})
        bound = []
        for event in events:
            if time.monotonic() >= deadline:
                raise TimeoutError("Resume preparation deadline exceeded")
            native = event.get("native_session_id")
            owner = _one(self.store, "SELECT FROM sessions WHERE id=:id",
                         {"id": event.get("session_id")}, False)
            if (not isinstance(native, str) or not native or not owner
                    or (owner.get("project_id"), owner.get("client"), owner.get("worktree"),
                        owner.get("native_session_id")) !=
                    (session["project_id"], target["client"], session["worktree"], native)):
                continue
            bound.append(event)
        # Without an event ID in the legacy marker, multiple failed captures
        # cannot safely be attributed to one finite later receipt.
        missing = [event for event in bound if event.get("transcript_watermark") is None]
        if len(missing) != 1 or missing[0].get("hook_event_name") != "SessionStart":
            return None
        origin = missing[0]
        captured = next((event for event in bound
                         if event.get("session_id") == origin["session_id"]
                         and event.get("native_session_id") == origin["native_session_id"]
                         and isinstance(event.get("created_at"), str)
                         and isinstance(origin.get("created_at"), str)
                         and event["created_at"] > origin["created_at"]
                         and event.get("transcript_watermark") is not None), None)
        if captured is None:
            return None
        watermark = captured["transcript_watermark"]
        if not isinstance(watermark, dict):
            return None
        size, device, inode = (watermark.get("size"), watermark.get("device"), watermark.get("inode"))
        head_bytes, head_hash = watermark.get("head_bytes"), watermark.get("head_sha256")
        observed, pending = watermark.get("observed_size", size), watermark.get("pending_tail_bytes", 0)
        if (type(size) is not int or size <= 0
                or type(device) is not int or device < 0 or type(inode) is not int or inode < 0
                or type(head_bytes) is not int or head_bytes != min(4096, size)
                or not isinstance(head_hash, str) or len(head_hash) != 64
                or any(char not in "0123456789abcdef" for char in head_hash)
                or type(observed) is not int or observed < size
                or type(pending) is not int or pending < 0 or observed - size != pending):
            return None
        candidate = self._target(path, target["client"], watermark)
        if candidate.get("error"):
            return None
        event_fields = ("id", "session_id", "native_session_id", "hook_event_name", "created_at")
        candidate["capture_recovery"] = {
            "original_target": dict(target), "project_id": session["project_id"], "worktree": session["worktree"],
            "native_session_id": origin["native_session_id"],
            "origin_event": {key: origin.get(key) for key in event_fields},
            "resolution_event": {key: captured.get(key) for key in event_fields},
        }
        return candidate

    def _archive(self, session: dict, target: dict, deadline: float) -> dict:
        """Prove every captured line against durable source hashes, not cursor alone."""
        if target.get("kind") == "agent_journal":
            from .agent_memory import archive_agent_target
            return archive_agent_target(self.store, self.settings, session, target, deadline)
        result = {**target, "status": "partial", "archived_offset": 0, "unknowns": []}
        if target.get("error"):
            candidate = self._recover_capture_target(session, target, deadline)
            if candidate is not None:
                checked = self._archive(session, candidate, deadline)
                if checked["status"] == "complete":
                    target.clear()
                    target.update(candidate)
                    return checked
                result["unknowns"].extend(checked.get("unknowns", []))
            result["unknowns"].append(target["error"])
            return result
        try:
            self._finish_captured_tail(target)
            result.update(target)
        except (OSError, ValueError) as exc:
            result["unknowns"].append(str(exc))
            return result
        path, size = Path(target["path"]), target["size"]
        try:
            stat = path.stat()
            if ((target.get("device") is not None and target["device"] != stat.st_dev)
                    or (target.get("inode") is not None and target["inode"] != stat.st_ino)
                    or stat.st_size < size):
                raise ValueError("Captured transcript generation was rotated or truncated")
            if target.get("head_sha256"):
                with path.open("rb") as handle:
                    head = handle.read(int(target.get("head_bytes", min(4096, size))))
                if hashlib.sha256(head).hexdigest() != target["head_sha256"]:
                    raise ValueError("Captured transcript prefix changed")
        except (OSError, ValueError) as exc:
            if not target.get("proof"):
                result["unknowns"].append(str(exc))
                return result
        cursor = _one(self.store, "SELECT FROM import_cursors WHERE path=:path AND project_id=:project",
                      {"path": str(path), "project": session["project_id"]}, False)
        if not cursor or int(cursor.get("offset", 0)) < size:
            result["archived_offset"] = int(cursor.get("offset", 0)) if cursor else 0
            result["unknowns"].append("Import cursor has not archived the captured EOF")
            return result
        # Compatibility repairs only reinterpret already archived, hash-checked
        # technical records. Run before proof so the first launch after an
        # upgrade does not race the independent scanner's next import pass.
        from .ingest import _identifier, reparse_history_sources
        try:
            reparsed = reparse_history_sources(self.store, path, session["project_id"],
                                               target.get("client", "auto"), deadline=deadline)
        except (OSError, ValueError) as exc:
            result["unknowns"].append(f"Transcript compatibility repair failed: {exc}")
            return result
        sources = self.store.query(
            "SELECT id,uri,sha256,kind,metadata FROM sources WHERE project_id=:project "
            "AND metadata.import_path=:path AND metadata.byte_offset<:horizon",
            {"project": session["project_id"], "path": str(path), "horizon": size})
        by_offset = {}
        for row in sources:
            metadata = row.get("metadata") or {}
            by_offset.setdefault(metadata.get("byte_offset"), []).append(row)
        source_ids, unknowns, digest, offset = [], list(reparsed.get("errors", [])), hashlib.sha256(), 0
        try:
            stat = path.stat()
            if ((target.get("device") is not None and target["device"] != stat.st_dev)
                    or (target.get("inode") is not None and target["inode"] != stat.st_ino)
                    or stat.st_size < size):
                raise ValueError("Captured transcript generation was rotated or truncated")
            with path.open("rb") as handle:
                head_bytes = int(target.get("head_bytes", min(4096, size)))
                head = handle.read(head_bytes)
                if target.get("head_sha256") and hashlib.sha256(head).hexdigest() != target["head_sha256"]:
                    raise ValueError("Captured transcript prefix changed")
                handle.seek(0)
                while offset < size:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Resume preparation deadline exceeded")
                    raw = handle.readline(size - offset)
                    if not raw or not raw.endswith(b"\n"):
                        raise ValueError("Captured EOF contains an incomplete JSONL range")
                    sha = hashlib.sha256(raw.decode("utf-8", "replace").encode("utf-8")).hexdigest()
                    candidates = [row for row in by_offset.get(offset, []) if row.get("sha256") == sha]
                    if not candidates:
                        raise ValueError(f"Missing durable source at transcript byte {offset}")
                    source = sorted(candidates, key=lambda row: row["id"])[0]
                    if source["kind"] != "transcript" or (source.get("metadata") or {}).get("schema_warning"):
                        unknowns.append(f"Unparsed or conflicting transcript record at byte {offset}")
                    recovery = target.get("capture_recovery")
                    if recovery:
                        metadata = source.get("metadata") or {}
                        native = recovery["native_session_id"]
                        client = metadata.get("client")
                        cwd = metadata.get("cwd")
                        if (metadata.get("session_id") != native or client not in {"auto", target["client"]}
                                or source.get("uri") != f"transcript://{client}/{_identifier(path, native)}/{offset}/{sha}"
                                or (cwd is not None and not _scope_contains(session["worktree"], cwd))):
                            unknowns.append(f"Capture recovery source provenance conflicts at byte {offset}")
                    source_ids.append(source["id"])
                    digest.update(raw)
                    offset += len(raw)
            result.update(archived_offset=offset, sha256=digest.hexdigest(), source_count=len(source_ids),
                          source_ids_hash=_hash(sorted(source_ids)), unknowns=unknowns,
                          status="partial" if unknowns else "complete")
        except (OSError, ValueError) as exc:
            # An already-proved immutable horizon survives deletion of the original
            # transcript, provided the complete durable source set still exists.
            proof = target.get("proof")
            if proof and proof.get("size") == size and proof.get("status") == "complete":
                ids = sorted(row["id"] for row in sources)
                if len(ids) == proof.get("source_count") and _hash(ids) == proof.get("source_ids_hash"):
                    return {**result, **proof, "path": str(path), "original_unavailable": True}
            result["unknowns"].append(str(exc))
        return result

    def _pending_status(self, project_id: str, kind: str) -> dict:
        rows = self.store.query(
            "SELECT state,count(*) AS total FROM jobs WHERE project_id=:project AND kind=:kind GROUP BY state",
            {"project": project_id, "kind": kind})
        counts = {row["state"]: int(row["total"]) for row in rows}
        return {"status": "pending" if any(counts.get(key, 0) for key in ("pending", "running"))
                else "failed" if counts.get("failed") else "complete", "counts": counts}

    def _build(self, session: dict, state: dict, archives: list[dict], unknowns: list[str]) -> dict:
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
        previews = sorted(state["records"], key=lambda row:
                          (0 if row["kind"] == "checkpoint" else 1, row["kind"], row["id"]))[:MAX_RECORDS]
        archived_agent_history = self.store.query(
            "SELECT id FROM sources WHERE project_id=:project AND kind='agent_transcript' "
            "AND metadata.agent_session_id!=:session LIMIT 1",
            {"project": session["project_id"], "session": session["id"]})
        packet_archives = [{key: value for key, value in row.items() if key != "source_ids"}
                           if row.get("kind") == "agent_journal" else row for row in archives]
        packet = {"id": str(uuid4()), "schema_version": 1, "project_id": session["project_id"],
                  "session_id": session["id"], "epoch": session["epoch"], "client": session["client"],
                  "worktree": session["worktree"], "created_at": _now(),
                  "active_plan": plan_view, "active_task": task_view,
                  "next_action": state["reason"], "blockers": state["blocked"],
                  "records": [{"id": row["id"], "version": row["version"], "kind": row["kind"],
                               "status": row["status"], "content": row["content"][:600],
                               "content_truncated": len(row["content"]) > 600,
                               "source_ids": row.get("source_ids", [])[:8],
                               **({"evidence_state": evidence[row["id"]]} if row["id"] in evidence else {})}
                              for row in previews],
                  "omitted_records": max(0, len(state["records"]) - len(previews)),
                  "code": state["code"], "state_hash": state["state_hash"],
                  "archive_watermarks": packet_archives, "no_history": not archives and not archived_agent_history,
                  "archive": {"status": "complete" if all(row["status"] == "complete" for row in archives)
                              else "partial", "scope": "captured EOFs only"},
                  "indexing": self._pending_status(session["project_id"], "embed"),
                  "extraction": self._pending_status(session["project_id"], "extract"),
                  "unknowns": [value[:500] for value in list(dict.fromkeys(unknowns))[:20]],
                  "omitted_unknowns": max(0, len(set(unknowns)) - 20)}
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
        if evidence and len(_json(packet).encode("utf-8")) > MAX_PACKET_BYTES:
            # Advisory evidence data yields first: it must never be what makes a packet unready.
            for row in packet["records"]:
                row.pop("evidence_state", None)
            if len(_json(packet).encode("utf-8")) > MAX_PACKET_BYTES and evidence_warning in packet.get("warnings", []):
                packet["warnings"].remove(evidence_warning)
                if not packet["warnings"]:
                    del packet["warnings"]
        if len(_json(packet).encode("utf-8")) > MAX_PACKET_BYTES:
            # Essential goal/constraints are never silently truncated to a ready packet.
            packet["unknowns"].append("Essential resume packet exceeds the bounded context budget")
            packet["records"] = []
            packet["omitted_records"] = len(state["records"])
            for key in ("active_plan", "active_task"):
                if packet[key]:
                    packet[key] = {field: packet[key][field] for field in ("id", "version")}
            packet["blockers"] = []
            packet["archive_watermarks"] = [{key: row.get(key) for key in
                                              ("path", "size", "archived_offset", "status", "sha256")}
                                             for row in archives[:30]]
            packet["omitted_archive_watermarks"] = max(0, len(archives) - 30)
            while (packet["archive_watermarks"]
                   and len(_json(packet).encode("utf-8")) > MAX_PACKET_BYTES - 512):
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
                session_id: str | None = None, reason: str = "startup", timeout: float = 30) -> dict:
        if client not in {"codex", "claude", *AGENT_CLIENTS}:
            raise ValueError("Session client must be codex, claude, api or copilot")
        if client in AGENT_CLIENTS and session_id is not None:
            from .agent_memory import _journal_path
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
            session = _one(tx, "SELECT FROM sessions WHERE id=:id", {"id": session_id}, False) if session_id else None
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
                           "transcript_targets": [], "event_ids": [], "created_at": _now()}
                _insert(tx, "sessions", session)
            session["updated_at"] = _now()
            _set(tx, "sessions", session, "id=:id", {"id": session["id"]})
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
                    current = _one(tx, "SELECT FROM sessions WHERE id=:id", {"id": session["id"]})
                    if current["epoch"] != session["epoch"]:
                        return {"session_id": current["id"], "epoch": current["epoch"], "status": "partial",
                                "packet": None, "unknowns": ["Session resumed during horizon capture; retry"]}
                    if current.get("resume_targets") is None:
                        current["resume_targets"] = captured
                        _set(tx, "sessions", current, "id=:id", {"id": current["id"]})
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
                unknowns.extend(f"{archive['path']}: {reason}" for reason in archive.get("unknowns", []))
        except TimeoutError as exc:
            unknowns.append(str(exc))
        state = self._state(session)
        # Advisory only, and only when a packet is prepared: it is not an unknown and not part
        # of the state identity, so a deleted or edited file neither blocks a resume nor
        # invalidates an acknowledged one. Computed outside the store's writer lock.
        from .evidence import evidence_states
        state["evidence"] = {record_id: value["evidence_state"] for record_id, value in evidence_states(
            self.store, session["project_id"], state["records"], quotes=False).items()
            if value["evidence_state"] != "anchored"}
        unknowns.extend(state.get("unknowns", []))
        unknowns.extend(self._own_continuity(session))
        if not (state.get("initial_baseline_eligible") and not archives and not targets):
            unknowns.extend(state["code"].get("unknowns", []))
        if limit and time.monotonic() > deadline:
            unknowns.append("Resume preparation exceeded its deadline")
        packet = self._build(session, state, archives, unknowns)
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
            current = _one(tx, "SELECT FROM sessions WHERE id=:id", {"id": session["id"]})
            if current["epoch"] != session["epoch"]:
                return {"session_id": current["id"], "epoch": current["epoch"], "status": "partial", "packet": None,
                        "unknowns": ["Session resumed again during packet preparation; retry"]}
            _insert(tx, "resume_packets", {"id": packet["id"], "project_id": project["id"],
                                           "session_id": session["id"], "epoch": session["epoch"],
                                           "hash": packet["hash"], "payload": packet, "created_at": packet["created_at"]})
            # A concurrent Stop may capture another horizon without changing epoch.
            # Keep that durable requirement for the next resume preparation.
            extra_targets = [target for target in current.get("transcript_targets", []) if target not in targets]
            current.update(packet_id=packet["id"], ack_id=None, status=packet["status"],
                           resume_targets=proved if capture_complete else None,
                           transcript_targets=self._merge_targets([*proved, *extra_targets]), updated_at=_now())
            _set(tx, "sessions", current, "id=:id", {"id": current["id"]})
        return self._result(current, packet)

    def archive_agent_journal(self, session_id: str) -> dict:
        """Archive a finite own-runtime journal without granting model authority."""
        from .agent_memory import capture_agent_target
        session = self._session(session_id)
        if session.get("client") not in AGENT_CLIENTS:
            raise ValueError("Only an API or Copilot session owns an agent journal")
        continuity = self._own_continuity(session)
        if continuity:
            return {"session_id": session_id, "status": "partial", "source_ids": [], "unknowns": continuity}
        target = capture_agent_target(self.settings, session)
        if target is None:
            return {"session_id": session_id, "status": "complete", "sequence": 0,
                    "source_ids": [], "unknowns": []}
        rows = self.store.query("SELECT transcript_targets FROM sessions WHERE project_id=:project AND worktree=:root",
                                {"project": session["project_id"], "root": session["worktree"]})
        known = [value for row in rows for value in row.get("transcript_targets", [])
                 if value.get("kind") == "agent_journal" and value.get("agent_session_id") == session_id]
        merged = self._merge_targets([*known, target])
        if len(merged) != 1:
            return {"session_id": session_id, "status": "partial", "source_ids": [],
                    "unknowns": ["Conflicting own-agent captured journal horizons; start a new agent session."]}
        target = merged[0]
        archive = self._archive(session, target, time.monotonic() + 30)
        if archive.get("status") == "complete":
            proved = {**target, "proof": {key: archive.get(key) for key in
                       ("size", "status", "sha256", "archived_offset", "source_count", "source_ids_hash",
                        "public_sources_hash", "sequence", "chain_hash")}}
            with self.store.transaction() as tx:
                self._require_scope(tx, session["project_id"], session["worktree"])
                current = _one(tx, "SELECT FROM sessions WHERE id=:id", {"id": session_id})
                targets = self._merge_targets([*current.get("transcript_targets", []), proved])
                _set(tx, "sessions", {"transcript_targets": targets, "updated_at": _now()},
                     "id=:id", {"id": session_id})
        return {"session_id": session_id, **archive}

    def exact_context(self, session_id: str, packet_id: str) -> dict:
        """Read an exact current packet without refreshing horizons or epochs."""
        with self.store.transaction():
            session = self._session(session_id)
            packet = self._packet(session)
            if (not packet or packet.get("status") != "ready" or packet.get("id") != packet_id
                    or packet.get("epoch") != session["epoch"]):
                raise ValueError("resume_required: requested startup packet is no longer the ready current packet")
            continuity = self._own_continuity(session)
            if continuity:
                raise ValueError("resume_required: " + continuity[0])
            try:
                current = self._state_locked(session)
            except (OSError, ValueError) as exc:
                raise ValueError(f"resume_required: current project scope cannot be restored ({exc})") from None
            if current["state_hash"] != packet["state_hash"]:
                raise ValueError("resume_required: relevant project state or code changed before exact packet read")
            return self._result(session, packet)

    def resume_context(self, session_id: str) -> dict:
        session = self._session(session_id)
        packet = self._packet(session)
        if packet and packet.get("status") == "ready":
            try:
                current = self._state(session)
                if current["state_hash"] == packet["state_hash"] and not self._own_continuity(session):
                    return self._result(session, packet)
            except (OSError, ValueError):
                pass
            with self.store.transaction() as tx:
                current_session = _one(tx, "SELECT FROM sessions WHERE id=:id", {"id": session_id})
                if current_session["epoch"] == session["epoch"]:
                    if "resume_targets" not in current_session:
                        current_session["resume_targets"] = self._legacy_action_targets(current_session, packet)
                    current_session.update(epoch=session["epoch"] + 1, packet_id=None, ack_id=None,
                                           status="partial", updated_at=_now())
                    _set(tx, "sessions", current_session, "id=:id", {"id": session_id})
        return self.prepare(session["project_id"], session["client"], session["worktree"], session_id,
                            reason="refresh", timeout=0)

    def ack(self, session_id: str, packet_id: str, packet_hash: str, readback: dict) -> dict:
        with self.store.transaction() as tx:
            session = _one(tx, "SELECT FROM sessions WHERE id=:id", {"id": session_id})
            packet = self._packet(session)
            if (not packet or packet["status"] != "ready" or packet["id"] != packet_id
                    or packet["hash"] != packet_hash or packet["epoch"] != session["epoch"]):
                raise ValueError("resume_required: ACK does not match a ready current packet")
            valid_types = (isinstance(readback, dict) and set(readback) == {"plan", "task"}
                           and all(value is None or (isinstance(value, dict)
                                   and set(value) == {"id", "version"} and isinstance(value["id"], str)
                                   and type(value["version"]) is int and value["version"] > 0)
                                   for value in readback.values()))
            if not valid_types or readback != _readback(packet):
                raise ValueError("resume_required: readback must match active plan/task IDs and versions")
            continuity = self._own_continuity(session)
            if continuity:
                raise ValueError("resume_required: " + continuity[0])
            if self._state(session)["state_hash"] != packet["state_hash"]:
                raise ValueError("resume_required: relevant project state or code changed before ACK")
            previous = _one(tx, "SELECT FROM resume_acks WHERE id=:id", {"id": session.get("ack_id")}, False)
            if previous and previous.get("packet_id") == packet_id:
                return previous
            acknowledgment = {"id": str(uuid4()), "project_id": session["project_id"], "session_id": session_id,
                              "epoch": session["epoch"], "packet_id": packet_id, "packet_hash": packet_hash,
                              "readback": readback, "status": "acknowledged", "created_at": _now(),
                              "meaning": "Exact packet receipt/readback; not proof of understanding"}
            _insert(tx, "resume_acks", acknowledgment)
            _set(tx, "sessions", {"ack_id": acknowledgment["id"], "updated_at": _now()},
                 "id=:id", {"id": session_id})
            return acknowledgment

    def require_ack(self, session_id: str, packet_id: str | None = None) -> None:
        """Read-only; safe inside the caller's writer transaction; no import locks."""
        session = self._session(session_id)
        packet = self._packet(session)
        if (not packet or packet.get("status") != "ready" or packet["epoch"] != session["epoch"]
                or (packet_id is not None and packet_id != packet["id"]) or not session.get("ack_id")):
            raise ValueError("resume_required: fetch and acknowledge the current ready resume packet")
        acknowledgment = _one(self.store, "SELECT FROM resume_acks WHERE id=:id", {"id": session["ack_id"]}, False)
        if (not acknowledgment or acknowledgment.get("packet_id") != packet["id"]
                or acknowledgment.get("packet_hash") != packet["hash"]
                or acknowledgment.get("epoch") != session["epoch"]):
            raise ValueError("resume_required: current packet has no actual model ACK")
        continuity = self._own_continuity(session)
        if continuity:
            raise ValueError("resume_required: " + continuity[0])
        try:
            current = self._state(session)
        except (OSError, ValueError) as exc:
            raise ValueError(f"resume_required: current project scope cannot be restored ({exc})") from None
        if current["state_hash"] != packet["state_hash"]:
            raise ValueError("resume_required: plan, task, decision, scope or code changed")

    def note_event(self, payload: dict, client: str) -> None:
        """Called after spool import by the service; native hook performs no DB I/O."""
        if client not in {"codex", "claude"} or not isinstance(payload, dict):
            raise ValueError("Invalid client hook event")
        event = payload.get("hook_event_name")
        if event not in {"SessionStart", "PreCompact", "Stop", "SessionEnd"}:
            return
        native_id, logical_id = payload.get("session_id"), payload.get("devbrain_session_id")
        cwd = payload.get("cwd")
        project = self.store.resolve_project(cwd)
        # Most-specific registered root, not an inherited project environment hint.
        roots = self.store.query("SELECT path FROM project_roots WHERE project_id=:project", {"project": project["id"]})
        matching = [str(Path(row["path"]).resolve()) for row in roots if _scope_contains(str(Path(row["path"]).resolve()), cwd)]
        if not matching:
            raise ValueError("Hook working directory is not a registered project scope")
        root = max(matching, key=len)
        event_id = payload.get("event_id") or _hash(payload)
        with self.store.transaction() as tx:
            self._require_scope(tx, project["id"], root)
            receipt = _one(tx, "SELECT FROM session_events WHERE id=:id", {"id": event_id}, False)
            if receipt:
                old_session = _one(tx, "SELECT FROM sessions WHERE id=:id", {"id": receipt["session_id"]})
                if ((old_session["project_id"], old_session["client"], old_session["worktree"])
                        != (project["id"], client, root) or (logical_id and logical_id != old_session["id"])):
                    raise ValueError("Hook event ID is already bound to another logical session")
                return
            session = _one(tx, "SELECT FROM sessions WHERE id=:id", {"id": logical_id}, False) if logical_id else None
            if logical_id and not session:
                raise ValueError("Hook refers to an unknown logical session")
            if not session and native_id:
                rows = tx.query("SELECT FROM sessions WHERE project_id=:project AND client=:client "
                                "AND native_session_id=:native ORDER BY updated_at DESC LIMIT 1",
                                {"project": project["id"], "client": client, "native": native_id})
                session = _clean(rows[0]) if rows else None
            if session and (session["project_id"], session["client"], session["worktree"]) != (project["id"], client, root):
                raise ValueError("Hook session does not match client/project/worktree")
            if not session:
                session = {"id": str(uuid4()), "project_id": project["id"], "client": client, "worktree": root,
                           "native_session_id": native_id, "epoch": 1, "status": "partial", "packet_id": None,
                           "ack_id": None, "transcript_targets": [], "event_ids": [], "created_at": _now()}
                _insert(tx, "sessions", session)
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
                # _set updates supplied fields; popping would leave persisted
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
            _insert(tx, "session_events", {"id": event_id, "project_id": project["id"],
                                           "session_id": session["id"], "native_session_id": native_id,
                                           "client": client, "hook_event_name": event,
                                           "resume_reason": reason, "transcript_path": payload.get("transcript_path"),
                                           "transcript_watermark": payload.get("transcript_watermark"),
                                           "applied_epoch": session["epoch"], "created_at": _now()})
            session.update(updated_at=_now())
            _set(tx, "sessions", session, "id=:id", {"id": session["id"]})

    def status(self, session_id: str) -> dict:
        session = self._session(session_id)
        packet = self._packet(session)
        result = {**self._result(session, packet), "client": session["client"],
                  "worktree": session["worktree"], "project_id": session["project_id"],
                  "native_session_id": session.get("native_session_id"), "acknowledged": False, "stale": False}
        try:
            self.require_ack(session_id)
            result["acknowledged"] = True
        except ValueError as exc:
            result["ack_reason"] = str(exc)
            result["stale"] = bool(session.get("ack_id"))
        return result
