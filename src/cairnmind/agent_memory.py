"""Own-agent memory transport and finite, source-verified transcript archives.

Operational journals stay local. Only public conversation/tool events become
portable evidence; provider continuations and authentication material do not.
Archiving is ingestion, never an acknowledgement or permission grant.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .sql import clean_row, json_text, one_row

AGENT_CLIENTS = {"api", "copilot"}
_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")
_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_PROVIDER_FIELDS = {"continuation", "reasoning", "thinking", "encrypted_content", "signature"}


def _hash(value: Any) -> str:
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def _journal_path(settings, session_id: str) -> Path:
    if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
        raise ValueError("Invalid own-agent logical session identity")
    return settings.data_dir / "runtime" / "agents" / session_id / "events.jsonl"


def _public(event):
    visible = deepcopy(event)
    data = visible.get("data")
    if visible.get("type") == "message" and isinstance(data, dict) and data.get("role") == "assistant":
        # These are local adapter-envelope continuations. Fields with the same
        # names inside actual tool arguments/results remain literal evidence.
        for key in list(data):
            if str(key).casefold() in _PROVIDER_FIELDS:
                data.pop(key)
    return visible


MCP_PREFIX = "mcp_"
_MCP_EVENTS = {"mcp_server_started", "mcp_server_failed", "mcp_server_stopped"}
_MCP_SERVER = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


def _mcp_server(event) -> str | None:
    """The server of an event that carries a third-party MCP server's text or names its tool, else None.

    Decided from the host-written event type and tool name only. The name of a tool the engine offers is
    ``mcp_<server>_<tool>``; a built-in tool never starts with the prefix.
    """
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        return None
    if event.get("type") in _MCP_EVENTS:
        server = data.get("server_id")
    elif isinstance(data.get("name"), str) and data["name"].startswith(MCP_PREFIX):
        server = data["name"][len(MCP_PREFIX):].partition("_")[0]
    else:
        return None
    return server if isinstance(server, str) and _MCP_SERVER.fullmatch(server) else "unknown"


def _third_party_result(event) -> bool:
    """Whether an event carries text that came from a server: a completed call whose result holds the
    server's content (a result or a tool error it reported). A call refused before it reached a server, a
    fixed failure code, a result too large to deliver or a made-up ``mcp_`` name carries none."""
    data = event.get("data") if isinstance(event, dict) else None
    result = data.get("result") if isinstance(data, dict) else None
    return (event.get("type") == "action_completed" and _mcp_server(event) is not None
            and isinstance(result, dict) and isinstance(result.get("content"), list))


def _persons(event) -> bool:
    """The person's own words: their messages and their resolution notes."""
    data = event.get("data") if isinstance(event, dict) else None
    return ((event.get("type") == "message" and isinstance(data, dict) and data.get("role") == "user")
            or event.get("type") == "action_resolved")


def extracted_message(event) -> bool:
    """Whether an archived event without third-party text goes to memory extraction: a message of the
    person or the agent with nonblank text. Tool results and host events never do."""
    data = event.get("data") if isinstance(event, dict) else None
    return (event.get("type") == "message" and isinstance(data, dict) and data.get("role") in {"user", "assistant"}
            and isinstance(data.get("content"), str) and bool(data["content"].strip()))


def third_party_origin(store, project_id, session_id) -> dict | None:
    """The mark of a write made by an own-agent session after it received a third-party tool result.

    The engine archives at once after such a result, so the archive holds it before any later write.
    Derived from archived host metadata, never from a payload (store.ORIGIN_METADATA).
    """
    rows = store.query(
        "SELECT metadata.origin_client AS client FROM sources WHERE project_id=:project AND kind='agent_transcript' "
        "AND metadata.agent_session_id=:session AND metadata.third_party_result=true LIMIT 1",
        {"project": project_id, "session": session_id})
    if not rows or not str(rows[0].get("client", "")).startswith("mcp:"):
        return None
    return {"origin_client": rows[0]["client"], "origin_session_id": session_id,
            "origin_history": "archived", "origin_evidence": "degraded"}


def is_mcp_source(row) -> bool:
    """Whether an archived agent event holds third-party MCP text: kept and readable by id, never chunked."""
    metadata = row.get("metadata")
    if isinstance(metadata, (str, bytes)):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return False
    return (row.get("kind") == "agent_transcript" and isinstance(metadata, dict)
            and str(metadata.get("origin_client", "")).startswith("mcp:")
            and metadata.get("origin_evidence") == "degraded")


def _journal_events(settings, session_id):
    from .agent_journal import AgentJournal
    events = AgentJournal(settings, session_id).events()
    if not isinstance(events, list):
        raise ValueError("Invalid own-agent journal")
    return events


def capture_agent_target(settings, owner: dict) -> dict | None:
    """Capture a validated finite prefix. Later appends never move this horizon."""
    sid = owner["id"]
    path = _journal_path(settings, sid)
    target = {"kind": "agent_journal", "path": str(path), "client": owner["client"],
              "agent_session_id": sid}
    if not path.exists() and not path.is_symlink():
        # Existing proved targets remain on the session and survive log loss.
        # A never-created journal is an empty source history, not an error.
        return None
    try:
        events = _journal_events(settings, sid)
        if not events:
            return None
        raw = b"".join((json_text(event) + "\n").encode() for event in events)
        return {**target, "sequence": len(events), "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(), "chain_hash": events[-1]["hash"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**target, "error": f"Own-agent journal cannot be captured ({type(exc).__name__})"}


def agent_continuity_unknowns(store, settings, session: dict) -> list[str]:
    """An own logical session cannot restart its private sequence at one.

    Other/new sessions may use the immutable public archive after its origin
    disappears. Continuing the originating logical session additionally needs
    the local provider journal, including any nonportable continuation state.
    """
    if session.get("client") not in AGENT_CLIENTS:
        return []
    rows = store.query(
        "SELECT metadata.agent_sequence AS sequence,metadata.agent_event_hash AS event_hash "
        "FROM sources WHERE project_id=:project AND kind='agent_transcript' "
        "AND metadata.agent_session_id=:session ORDER BY metadata.agent_sequence DESC LIMIT 1",
        {"project": session["project_id"], "session": session["id"]})
    if not rows:
        return []
    reason = ("Own-agent private journal is missing, truncated or rewritten; start a new agent session. "
              "Its archived project memory remains available.")
    try:
        path = _journal_path(settings, session["id"])
        if not path.exists():
            return [reason]
        sequence, expected = rows[0].get("sequence"), rows[0].get("event_hash")
        events = _journal_events(settings, session["id"])
        if (type(sequence) is not int or sequence < 1 or len(events) < sequence
                or events[sequence - 1].get("hash") != expected):
            return [reason]
    except (OSError, ValueError, KeyError, TypeError):
        return [reason]
    return []


def _source_proof(rows: list[dict]) -> str:
    return _hash([[row["id"], row["sha256"], row.get("metadata", {}).get("agent_event_hash")]
                  for row in sorted(rows, key=lambda row: row.get("metadata", {}).get("agent_sequence", 0))])


def _verify_archived(rows: list[dict], target: dict, proof: dict, deadline: float) -> bool:
    if (proof.get("status") != "complete" or proof.get("sequence") != target.get("sequence")
            or proof.get("size") != target.get("size") or proof.get("sha256") != target.get("sha256")
            or len(rows) != target.get("sequence") or len(rows) != proof.get("source_count")
            or _hash(sorted(row["id"] for row in rows)) != proof.get("source_ids_hash")
            or _source_proof(rows) != proof.get("public_sources_hash")):
        return False
    ordered = sorted(rows, key=lambda row: row.get("metadata", {}).get("agent_sequence", 0))
    for seq, row in enumerate(ordered, 1):
        if time.monotonic() > deadline:
            raise TimeoutError("Own-agent archive proof exceeded its deadline")
        metadata = row.get("metadata") or {}
        content = row.get("content")
        if (metadata.get("agent_sequence") != seq or row.get("kind") != "agent_transcript"
                or not isinstance(content, str) or hashlib.sha256(content.encode()).hexdigest() != row.get("sha256")):
            return False
        try:
            event = json.loads(content)
        except (ValueError, TypeError):
            return False
        if (not isinstance(event, dict) or event.get("seq") != seq or event.get("session_id") != target["agent_session_id"]
                or event.get("hash") != metadata.get("agent_event_hash")):
            return False
    return not ordered or ordered[-1].get("metadata", {}).get("agent_event_hash") == proof.get("chain_hash")


def archive_agent_target(store, settings, session: dict, target: dict, deadline: float) -> dict:
    """Verify/ingest a captured journal prefix without trusting client-sent text."""
    result = {**target, "status": "partial", "archived_offset": 0, "unknowns": []}
    if target.get("error"):
        return {**result, "unknowns": [target["error"]]}
    sid = target.get("agent_session_id")
    try:
        expected_path = _journal_path(settings, sid)
        if target.get("path") != str(expected_path):
            raise ValueError("Own-agent archive path does not match its bound logical session")
        owner = one_row(store, "SELECT FROM sessions WHERE id=:id", {"id": sid})
        if (owner.get("client") not in AGENT_CLIENTS or target.get("client") != owner["client"]
                or (owner.get("project_id"), owner.get("worktree")) !=
                (session["project_id"], session["worktree"])):
            raise ValueError("Own-agent archive belongs to another project or worktree")
        sequence, size = target.get("sequence"), target.get("size")
        if (type(sequence) is not int or not 1 <= sequence <= 1_000_000
                or type(size) is not int or not 0 < size <= 64 * 1024 * 1024):
            raise ValueError("Invalid own-agent archive horizon")
        rows = [clean_row(row) for row in store.query(
            "SELECT FROM sources WHERE project_id=:project AND metadata.agent_session_id=:session "
            "AND metadata.agent_sequence<=:sequence",
            {"project": session["project_id"], "session": sid, "sequence": sequence})]
        if len({row.get("metadata", {}).get("agent_sequence") for row in rows}) != len(rows):
            raise ValueError("Conflicting durable own-agent event sequences")
        proof = target.get("proof") or {}
        # First verify the immutable archive. Its existence does not depend on
        # the originating computer retaining its operational private journal.
        if _verify_archived(rows, target, proof, deadline):
            ordered = sorted(rows, key=lambda row: row["metadata"]["agent_sequence"])
            return {**result, **proof, "source_ids": [row["id"] for row in ordered], "unknowns": []}
        if proof.get("status") == "complete":
            raise ValueError("Previously proved own-agent archive no longer matches its durable sources")
        prefix = target.get("known_prefix")
        if isinstance(prefix, dict):
            prior = [row for row in rows if row.get("metadata", {}).get("agent_sequence", 0) <= prefix.get("sequence", 0)]
            if not _verify_archived(prior, prefix, prefix.get("proof") or {}, deadline):
                raise ValueError("Previously proved own-agent archive prefix no longer matches its durable sources")
        events = _journal_events(settings, sid)
        if len(events) < sequence:
            raise ValueError("Own-agent journal was truncated before the captured horizon")
        events = events[:sequence]
        raw = b"".join((json_text(event) + "\n").encode() for event in events)
        if (len(raw) != size or hashlib.sha256(raw).hexdigest() != target.get("sha256")
                or events[-1].get("hash") != target.get("chain_hash")):
            raise ValueError("Own-agent journal captured prefix changed")
        by_sequence = {row.get("metadata", {}).get("agent_sequence"): row for row in rows}
        source_rows = []
        tainted = None  # the server of the first third-party result in this session, once there is one
        from .ingest import _write_chunks
        for event in events:
            if time.monotonic() > deadline:
                raise TimeoutError("Own-agent archive preparation exceeded its deadline")
            seq = event["seq"]
            visible = _public(event)
            content = json_text(visible) + "\n"
            uri = f"agent-transcript://{owner['client']}/{sid}/{seq}/{event['hash']}"
            existing = by_sequence.get(seq)
            digest = hashlib.sha256(content.encode()).hexdigest()
            if existing and (existing.get("uri") != uri or existing.get("sha256") != digest
                             or existing.get("content") != content):
                raise ValueError(f"Conflicting durable own-agent source at event {seq}")
            data = visible.get("data") or {}
            role = data.get("role", "metadata") if isinstance(data, dict) else "metadata"
            normalized = data.get("content") if isinstance(data, dict) else None
            if not isinstance(normalized, str) or not normalized.strip():
                normalized = json_text(data)
            metadata = {"client": owner["client"], "role": role, "session_id": sid,
                        "agent_session_id": sid, "agent_sequence": seq,
                        "agent_event_hash": event["hash"],
                        "raw_sha256": hashlib.sha256((json_text(event) + "\n").encode()).hexdigest(),
                        "worktree": owner["worktree"], "cwd": owner["worktree"],
                        "origin": "agent_runtime", "archive_parser_revision": 1,
                        "archive_view": "public-agent-event-v1",
                        "normalized_text": normalized, "source_timestamp": event.get("created_at")}
            server = _mcp_server(event)
            if server is None and tainted and not _persons(event):
                # After a third-party result anything the agent says or does may restate it: its messages,
                # tool requests and their arguments, tool results and approvals are archived like the result
                # itself (degraded, not chunked, not extracted), from then on. The person's words are not.
                server = tainted
            if _third_party_result(event):
                metadata["third_party_result"] = True
                tainted = tainted or server
            if server is not None:
                # Third-party text (store.ORIGIN_METADATA, as for an external session): it never makes a
                # record anchored, and is not chunked, so search and resume never surface it later.
                metadata.update(origin_client="mcp:" + server, origin_session_id=sid,
                                origin_history="archived", origin_evidence="degraded")
            with store.transaction():
                # Forgetting or rebinding the project during ingestion cannot
                # publish a source into an unregistered scope.
                store._require_project(store, session["project_id"])
                roots = store.query("SELECT path FROM project_roots WHERE project_id=:project",
                                    {"project": session["project_id"]})
                if owner["worktree"] not in {row["path"] for row in roots}:
                    raise ValueError("Own-agent worktree is no longer registered")
                source = store.snapshot(session["project_id"], uri, content, "agent_transcript", metadata)
            # Match scanner lock ordering: ingestion lock precedes DB writer.
            # The exact source is already durable if interruption happens here.
            if server is None:
                _write_chunks(store, session["project_id"], source["id"], normalized, {"role": role})
            if server is None and extracted_message(visible):
                store.enqueue("extract", session["project_id"], {"source_id": source["id"]},
                              f"extract:{source['id']}")
            source_rows.append(source)
        result.update(status="complete", archived_offset=size, source_count=len(source_rows),
                      source_ids=[row["id"] for row in source_rows],
                      source_ids_hash=_hash(sorted(row["id"] for row in source_rows)),
                      public_sources_hash=_source_proof(source_rows), unknowns=[])
        return result
    except (OSError, ValueError, KeyError, TypeError, TimeoutError) as exc:
        result["unknowns"] = [str(exc)[:500]]
        return result


def _tool(name, description, properties=None, required=None):
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": required or [],
        "additionalProperties": False}}


MEMORY_TOOLS = [
    _tool("resume_context", "Read this session's complete current resume packet before its exact ACK."),
    _tool("ack_resume", "Acknowledge the exact packet actually read. readback.plan copies active_plan's "
          "id/version, or null if absent; readback.task copies active_task's id/version, or null if absent. "
          "Both readback keys are required. Never invent IDs or versions.", {
        "packet_id": {"type": "string"}, "packet_hash": {"type": "string"},
        "readback": {"type": "object", "additionalProperties": False, "required": ["plan", "task"],
                     "properties": {name: {
                         "type": ["object", "null"], "additionalProperties": False,
                         "description": f"Copy id and version from packet.active_{name}; use null when absent.",
                         "required": ["id", "version"],
                         "properties": {"id": {"type": "string", "minLength": 1},
                                        "version": {"type": "integer", "minimum": 1}},
                     } for name in ("plan", "task")}}}, ["packet_id", "packet_hash", "readback"]),
    _tool("ask", "Ask this project's local source-grounded knowledge agent.", {
        "question": {"type": "string", "minLength": 1, "maxLength": 16000}}, ["question"]),
    _tool("remember", "Save source-backed knowledge using expected versions when updating.", {
        "record": {"type": "object"}, "sources": {"type": "array", "items": {"type": "string"}}},
          ["record", "sources"]),
    _tool("checkpoint", "Save reported state and next steps; not machine proof or human approval.",
          {"state": {"type": "object"}}, ["state"]),
    _tool("save_plan", "Save a first-class plan and its versioned DAG of tasks atomically.", {
        "plan": {"type": "object"}, "steps": {"type": "array", "items": {"type": "object"}},
        "idempotency_key": {"type": "string"}, "expected_version": {"type": ["integer", "null"]},
        "task_versions": {"type": ["object", "null"]}, "activate": {"type": "boolean"}},
          ["plan", "steps", "idempotency_key"]),
    _tool("next_task", "Read the current plan's next eligible task and blockers.",
          {"plan_id": {"type": ["string", "null"]}}),
    _tool("check_status", "Read current machine-check freshness; never executes a command.",
          {"task_id": {"type": ["string", "null"]}}),
    _tool("read_source", "Read exact archived evidence belonging to this project, with pagination.", {
        "source_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 65536},
        "view": {"type": "string", "enum": ["raw", "normalized", "metadata"]}}, ["source_id"]),
    _tool("memory_records", "Read project memory records and their source references.", {
        "kind": {"type": ["string", "null"]}, "cursor": {"type": ["string", "null"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
]
READ_MEMORY_TOOLS = {"resume_context", "session_status", "ask", "next_task", "check_status", "read_source", "memory_records"}


class AgentMemoryClient:
    """Run-bound service transport; the model alone supplies ACK and readback."""
    def __init__(self, settings, project_id, worktree, session_id, *, client="api", transport=None):
        if client not in AGENT_CLIENTS:
            raise ValueError("Own-agent memory client must be api or copilot")
        _journal_path(settings, session_id)
        self.settings, self.project_id = settings, project_id
        self.worktree = str(Path(worktree).expanduser().resolve())
        self.session_id, self.client = session_id, client
        self.packet_id = None
        self._seen_packet = None
        self.transport = transport

    async def _request(self, method, path, *, body=None, params=None):
        timeout = self.settings.ask_timeout + 15 if path == "/ask" else 45
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=timeout, trust_env=False) as client:
                response = await client.request(method, self.settings.url + path,
                    json=body if method == "POST" else None,
                    params={k: v for k, v in (params or {}).items() if v is not None},
                    headers={"Authorization": f"Bearer {self.settings.token}"})
            if response.is_error:
                try:
                    detail = str(response.json().get("detail", "Request rejected"))[:500]
                except ValueError:
                    detail = "Request rejected"
                raise RuntimeError(f"Cairnmind service rejected the request ({response.status_code}): {detail}")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Expected object service response")
            return result
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Cairnmind service unavailable ({type(exc).__name__})") from None

    async def prepare(self, reason="startup"):
        result = await self._request("POST", "/sessions/prepare", body={
            "project": self.project_id, "client": self.client, "worktree": self.worktree,
            "session_id": self.session_id, "reason": reason})
        if result.get("session_id") != self.session_id:
            raise RuntimeError("Session mismatch in prepared own-agent context")
        # Preparation/delivery is not an actual model read and cannot authorize ACK.
        self.packet_id, self._seen_packet = None, None
        return result

    async def status(self):
        result = await self._request("GET", "/sessions/" + quote(self.session_id, safe=""))
        if (result.get("session_id") != self.session_id or result.get("project_id") != self.project_id
                or result.get("client") != self.client or result.get("worktree") != self.worktree):
            raise RuntimeError("Own-agent session scope changed")
        return result

    async def archive(self, journal=None):
        if journal is not None and getattr(journal, "session_id", self.session_id) != self.session_id:
            raise RuntimeError("Cannot archive a different agent session")
        return await self._request("POST", "/sessions/agent/archive", body={"session_id": self.session_id})

    async def execute(self, name, args):
        if not isinstance(args, dict):
            raise ValueError("Memory tool arguments must be an object")
        if any(field in args for field in ("project", "project_id", "session_id", "worktree", "packet_id")):
            # packet_id is the model's exact ACK identity, not a scope override.
            allowed = {"packet_id"} if name == "ack_resume" else set()
            if set(args).intersection({"project", "project_id", "session_id", "worktree", "packet_id"}) - allowed:
                raise ValueError("Memory tool cannot override its bound project/session/worktree")
        if name == "session_status":
            return await self.status()
        if name == "resume_context":
            result = await self._request("POST", "/sessions/context", body={"session_id": self.session_id})
            if result.get("session_id") != self.session_id:
                raise RuntimeError("Session mismatch in returned own-agent packet")
            packet = result.get("packet") or {}
            if packet and (packet.get("project_id") != self.project_id or packet.get("worktree") != self.worktree):
                raise RuntimeError("Resume packet scope changed")
            self.packet_id = packet.get("id")
            self._seen_packet = packet if packet else None
            return result
        if name == "ack_resume":
            if not self._seen_packet or not self.packet_id or args.get("packet_id") != self.packet_id:
                raise RuntimeError("resume_required: read resume_context before acknowledging its exact packet")
            return await self._request("POST", "/sessions/ack", body={**args, "session_id": self.session_id})
        common = {"project": self.project_id, "worktree": self.worktree, "session_id": self.session_id}
        if name == "ask":
            return await self._request("POST", "/ask", body={**common, **args})
        if name == "next_task":
            return await self._request("POST", "/plans/next", body={**common, **args})
        if name == "check_status":
            return await self._request("GET", "/checks", params={**common, **args})
        if name == "read_source":
            sid = args.get("source_id")
            if not isinstance(sid, str) or not _SOURCE.fullmatch(sid):
                raise ValueError("A valid source ID is required")
            return await self._request("GET", "/inspector/sources/" + quote(sid, safe=""), params={
                "project": self.project_id, "worktree": self.worktree,
                "offset": args.get("offset", 0), "limit": args.get("limit", 16384),
                "view": args.get("view", "raw")})
        if name == "memory_records":
            return await self._request("GET", "/inspector/records", params={
                "project": self.project_id, "worktree": self.worktree, **args})
        endpoints = {"remember": "/remember", "checkpoint": "/checkpoint", "save_plan": "/plans/save"}
        if name in endpoints:
            if not self.packet_id:
                raise RuntimeError("resume_required: fetch and acknowledge the current packet")
            body = {"project": self.project_id, "session_id": self.session_id,
                    "packet_id": self.packet_id, **args}
            if name == "save_plan":
                body["worktree"] = self.worktree
            return await self._request("POST", endpoints[name], body=body)
        raise ValueError("Unknown project-memory tool")
