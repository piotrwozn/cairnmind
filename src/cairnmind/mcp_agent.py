"""The built-in agent's side of third-party MCP tools (#126): one-use approval and the journal.

The engine offers these tools only after the model's exact resume ACK and refuses them without a fresh
one, as it does every other tool that is not a read of memory. Each call is shown to the person with its
server, tool and exact arguments and runs only after their one-use approval, also for a tool its server
calls read-only; there is no always-allow and nothing is remembered per server. The approval itself goes
through the engine's command approval, which re-checks that the project state did not change meanwhile.
The journal records the request, the decision, ``action_started`` before the call and its outcome; an
interrupted call stays an uncertain action that only a person resolves, and it is never repeated.
"""
from __future__ import annotations

from copy import deepcopy
import inspect
import json
from uuid import uuid4

from .mcp_host import MAX_ARGUMENT_BYTES, McpHost, _canonical
from .mcp_schema import affordable, valid_arguments
from .terminal_safe import safe_text

# The approval dialog prints the whole request; a request whose text would be longer is refused, so what
# runs is always exactly what was shown, never a cut view of it.
MAX_SHOWN_CHARS = 60_000
PROJECTION_TEXT_CHARS = 10_000
_HOST_KEYS = ("ok", "server_id", "tool", "third_party_untrusted", "is_error", "error_code", "notice")


def approval_text(request) -> str:
    """The request exactly as the approval dialog prints it."""
    return safe_text(json.dumps(request, ensure_ascii=False, indent=2))


def projection(result: dict) -> dict:
    """A large third-party result for the model's context: the host's marking always kept, server text
    only inside ``content``, cut to a bound. The complete result is in the journal and the archive."""
    value = {key: result[key] for key in _HOST_KEYS if key in result}
    value["third_party_untrusted"] = True
    budget, content = PROJECTION_TEXT_CHARS, []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            content.append({key: block[key] for key in ("type", "omitted", "bytes") if key in block})
            continue
        piece = block["text"][:budget]
        while piece and len(json.dumps(piece, ensure_ascii=False)) > budget:
            piece = piece[:len(piece) // 2]
        budget -= len(json.dumps(piece, ensure_ascii=False))
        content.append({"type": "text", "text": piece, "truncated": piece != block["text"]})
    value.update(content=content, content_truncated=True, structured_omitted="structured" in result)
    return value


class McpTools:
    def __init__(self, servers, *, run, journal, worktree, reserved, approve=None):
        self.run, self.journal, self.approve = run, journal, approve
        self.results = 0  # third-party results journaled in this run
        self.withdrawn = set()  # tools whose argument check ran out of time
        redact = getattr(journal, "redact", None)
        self.host = McpHost(servers, worktree=worktree, reserved=reserved, redact=redact)

    @property
    def catalogue(self):
        return self.host.catalogue

    def schemas(self):
        return self.host.schemas()

    async def start(self, record):
        """Start the servers once; returns the failures as (server_id, code) for the status line.

        A server whose start cannot be journaled is stopped and reported, the others keep their tools.
        """
        failures = []
        for event_type, data in await self.host.start():
            try:
                record(event_type, data)
            except Exception:
                if event_type != "mcp_server_started":
                    raise
                await self.host.withdraw(data["server_id"], "catalogue_invalid")
                event_type, data = "mcp_server_failed", {"server_id": data["server_id"], "code": "catalogue_invalid"}
                record(event_type, data)
            if event_type == "mcp_server_failed":
                failures.append((data["server_id"], data["code"]))
        return failures

    def _binding(self, call_id):
        return {"session_id": self.run["session_id"], "run_id": self.run["id"],
                "generation": self.run["generation"], "tool_call_id": call_id}

    async def call(self, name, arguments, call_id, timeout):
        entry = self.catalogue[name]
        base = {"server_id": entry["server_id"], "tool": entry["tool"], "third_party_untrusted": True}
        try:
            size = len(_canonical(arguments))
        except (ValueError, TypeError, RecursionError):
            return {"ok": False, "error_code": "invalid_tool_arguments", **base}
        if size > MAX_ARGUMENT_BYTES:
            return {"ok": False, "error_code": "arguments_too_large", "bytes": size, **base}
        schema = entry["schema"]["parameters"]
        if not affordable(schema, arguments):
            return {"ok": False, "error_code": "arguments_too_costly", **base}
        # Evaluated on a dedicated thread with a time limit (mcp_schema). A tool whose check ran out of time
        # once is withdrawn for the run: its schema is not evaluated again.
        valid = await valid_arguments(schema, arguments)
        if valid is None:
            self.withdrawn.add(name)
            self.journal.append("mcp_tool_withdrawn", {"name": name, "server_id": entry["server_id"],
                                                       "tool": entry["tool"], "reason": "validation_timeout"})
            return {"ok": False, "error_code": "tool_withdrawn", **base}
        if not valid:
            return {"ok": False, "error_code": "invalid_tool_arguments", **base}
        action_id = uuid4().hex
        request = {"kind": "mcp_tool", "action_id": action_id, "server_id": entry["server_id"],
                   "tool": entry["tool"], "name": name, "arguments": deepcopy(arguments),
                   "timeout_seconds": timeout, **self._binding(call_id)}
        shown = len(approval_text(request))
        if shown > MAX_SHOWN_CHARS:
            return {"ok": False, "error_code": "arguments_too_long_to_show", "shown_chars": shown, **base}
        self.journal.append("approval_requested", request)
        if self.approve is None:
            return {"ok": False, "error_code": "approval_required", **base,
                    "message": "This third-party tool call requires human approval"}
        decision = self.approve(deepcopy(request))
        if inspect.isawaitable(decision):
            decision = await decision
        approved = decision is True
        self.journal.append("approval_decided", {**request, "approved": approved})
        if not approved:
            return {"ok": False, "error_code": "approval_denied", **base,
                    "message": "The person did not approve this call"}
        action = {"action_id": action_id, "kind": "mcp_tool", "name": name, "server_id": entry["server_id"],
                  "tool": entry["tool"], "arguments": deepcopy(arguments), **self._binding(call_id)}
        self.journal.append("action_started", action)
        try:
            result = await self.host.call(name, arguments, timeout)
        except BaseException:
            # The server may have acted; the outcome is unknown and only a person resolves it.
            self.journal.append("action_interrupted", action)
            raise
        result = self.journal.redact(result) if hasattr(self.journal, "redact") else result
        self.journal.append("action_completed", {**action, "result": result})
        self.results += 1
        return result

    async def close(self, record):
        for server_id in await self.host.close():
            record("mcp_server_stopped", {"server_id": server_id, "reason": "run_ended"})
