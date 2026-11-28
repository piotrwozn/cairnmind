"""Bounded programming-agent orchestration over the real Cairnmind memory protocol.

The engine never turns delivery of a packet into an ACK.  Only a model tool call
which reads the current packet, followed by its exact accepted readback, opens
the side-effect gate.  The local append-only journal precedes tool execution and
is also the recovery source; restarting this class never replays a tool.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from copy import deepcopy
from typing import Any

from cairnmind.agents.agent_reads import (
    ReadLedger,
)
from cairnmind.agents.agent_reads import (
    projected_read as projected_read,
)
from cairnmind.agents.api_agent_stream import (
    _BOOTSTRAP_TOOLS,
    COMPACT_TOOL,
    CONTEXT_BUDGET_CHARS,
    MAX_INPUT_CHARS,
    SYSTEM,
    _AgentStreamMixin,
    _await,
)
from cairnmind.agents.api_agent_stream import (
    COMPACT_SHARE as COMPACT_SHARE,
)
from cairnmind.agents.api_agent_stream import (
    MAX_REPLY_CHARS as MAX_REPLY_CHARS,
)
from cairnmind.agents.api_agent_stream import (
    MAX_TOOLS_PER_REPLY as MAX_TOOLS_PER_REPLY,
)
from cairnmind.agents.api_agent_stream import (
    NEXT_STEP as NEXT_STEP,
)
from cairnmind.agents.api_agent_stream import (
    TOOL_CONTEXT_CHARS as TOOL_CONTEXT_CHARS,
)
from cairnmind.agents.api_agent_stream import (
    _ArchiveIncomplete as _ArchiveIncomplete,
)
from cairnmind.agents.api_agent_stream import (
    _json as _json,
)
from cairnmind.agents.api_agent_turns import _AgentTurnMixin
from cairnmind.memory.memory_egress import coding_held_classes
from cairnmind.memory.memory_egress import coding_read as coding_read
from cairnmind.memory.memory_egress import coding_written as coding_written

log = logging.getLogger(__name__)


class AgentEngine(_AgentStreamMixin, _AgentTurnMixin):
    """One explicit user turn at a time; process/UI restarts grant no authority."""

    def __init__(self, settings, run, profile, *, key=None, provider=None, memory=None,
                 journal=None, approve=None, on_delta=None, on_event=None, tools=None, mcp_servers=None):
        from cairnmind.agents.agent_journal import AgentJournal
        from cairnmind.agents.agent_memory import MEMORY_TOOLS, READ_MEMORY_TOOLS, AgentMemoryClient
        from cairnmind.agents.agent_tools import FILE_TOOLS, ToolExecutor

        required = {"id", "generation", "project_id", "worktree", "session_id", "client"}
        if (not isinstance(run, dict) or not required <= set(run)
                or run["client"] not in {"api", "copilot"}
                or type(run["generation"]) is not int or run["generation"] < 1):
            raise ValueError("Invalid API agent run binding")
        self.settings, self.run, self.profile = settings, dict(run), deepcopy(profile)
        self.on_delta, self.on_event = on_delta, on_event
        self.journal = journal if journal is not None else AgentJournal(settings, run["session_id"])
        if key and hasattr(self.journal, "add_secrets"):
            self.journal.add_secrets(key)
        # What a hosted model must not read of memory (#183): read_source and memory_records are held below,
        # ask and the resume packet by the service, which the memory client tells.
        self._held = coding_held_classes(settings, run["client"], self.profile)
        self.memory = memory if memory is not None else AgentMemoryClient(
            settings, run["project_id"], run["worktree"], run["session_id"], client=run["client"],
            held=self._held)
        self._approve_callback = approve
        self.tools = tools if tools is not None else ToolExecutor(
            settings, run["worktree"], session_id=run["session_id"], run_id=run["id"],
            generation=run["generation"], journal=self.journal,
            approve=self._approve_command if approve is not None else None)
        limits = self.profile.get("limits") or {}
        self.max_model_calls = self._limit(limits.get("max_model_calls"), 20, 1, 1000)
        self.max_tool_calls = self._limit(limits.get("max_tool_calls"), 20, 1, 10000)
        self.max_turn_seconds = self._limit(limits.get("max_turn_seconds"), 900, 1, 86400)
        self.tools.command_timeout_seconds = self._limit(
            limits.get("command_timeout_seconds"), 120, 1, 3600)
        self._ollama_window = None  # (context window, maximum output) of an API Ollama profile
        if run["client"] == "api" and self.profile.get("provider") == "ollama":
            from cairnmind.agents.api_profiles import normalize_profile
            try:
                normal = normalize_profile(self.profile)["limits"]
                self._ollama_window = (normal["context_window_tokens"], normal["max_output_tokens"])
            except (ValueError, KeyError, TypeError):
                pass  # the provider refuses an invalid profile itself
        self.provider: Any = provider
        if self.provider is None:
            if run["client"] == "copilot":
                from cairnmind.agents.copilot_adapter import CopilotAdapter
                self.provider = CopilotAdapter(profile, key, settings=settings,
                                               session_id=run["session_id"])
            else:
                from cairnmind.agents.api_providers import make_provider
                self.provider = make_provider(profile, key)
        self._memory_tools = {row["name"]: row for row in MEMORY_TOOLS}
        self._read_memory = set(READ_MEMORY_TOOLS) | _BOOTSTRAP_TOOLS
        self._schemas = {row["name"]: deepcopy(row)
                         for row in [*MEMORY_TOOLS, *FILE_TOOLS, COMPACT_TOOL]}
        from cairnmind.mcp.mcp_agent import McpTools
        from cairnmind.mcp.mcp_host import PREFIX
        if any(name.startswith(PREFIX) for name in self._schemas):
            raise ValueError("The mcp_ prefix is reserved for third-party tools")
        # Third-party MCP tools (#126): API runs only. Their names are never read-memory names, so the
        # ACK gate below offers and admits them like every other effect.
        self.mcp = McpTools(mcp_servers if run["client"] == "api" else [], run=self.run, journal=self.journal,
                            worktree=run["worktree"], reserved=set(self._schemas),
                            approve=self._approve_command if approve is not None else None,
                            changes=getattr(self.tools, "changes", None))
        self._mcp_started = False
        self._emitted = False
        self._prepared = False
        self._acknowledged = False
        self._last_packet = None
        self._acked_packet = None
        self._context: list[dict[str, Any]] = []
        # What file content the current model context has seen (#157). Only an API run renews its context
        # at CONTEXT_BUDGET_CHARS; Copilot keeps its own.
        self._reads = ReadLedger(CONTEXT_BUDGET_CHARS if run["client"] == "api" else None)
        self._closed = False
        self._turn_task = None
        self._lock = asyncio.Lock()
        self._compaction = None
        self._needs_compaction = False
        self._compaction_notice = False
        # True from a context renewal until the model calls a tool other than the bootstrap ones: until then
        # the context holds only what a compaction keeps, so another notice could only loop (#240).
        self._renewed_context = False
        self._context_floor = 0  # estimated tokens of that renewed context, learned at the last renewal
        self._last_user = ""
        self._turn_usage = {}
        self._model_calls = 0
        self._tool_calls = 0
        self._seen_calls = set()
        self._memory_partial: dict[str, Any] | None = None
        self._stream_buffer = ""
        self._display_pending = ""
        self._stream_secrets = {value for value in (key, getattr(settings, "token", None),
                                                    getattr(settings, "arcade_password", None))
                                if isinstance(value, str) and value}
        self._request_id = None
        self._execution_epoch = 0
        self._copilot_token = None
        # The provider-side conversation this engine's ledger and ACK describe (#157).
        self._provider_context = getattr(self.provider, "session_id", None)
        self._recover_incomplete_calls()

    @staticmethod
    def _context_budget_chars() -> int:
        return CONTEXT_BUDGET_CHARS

    @staticmethod
    def _tool_context_chars() -> int:
        return TOOL_CONTEXT_CHARS

    @staticmethod
    def _limit(value, default, low, high):
        return value if type(value) is int and low <= value <= high else default

    @staticmethod
    def _error(code, **extra):
        return {"ok": False, "error_code": code, **extra}

    @staticmethod
    def _provider_diagnostic(error):
        """Use locally defined diagnostics, never an upstream response body.

        Reconstruct the fixed hint from the typed provider code and bounded
        numeric context observations. An arbitrary exception's ``hint`` or
        unknown detail fields are not an approved diagnostic channel.
        """
        from cairnmind.agents.api_providers import ProviderError

        if not isinstance(error, ProviderError):
            return {}
        raw = getattr(error, "context_details", None)
        details = {key: raw[key] for key in ("configured_context_window", "model_context_window")
                   if isinstance(raw, dict) and type(raw.get(key)) is int
                   and 0 < raw[key] <= 2**31 - 1}
        safe = ProviderError(error.code, context_details=details)
        return {"hint": safe.hint, "context_details": dict(safe.context_details)}

    async def _prepare(self, *, reason="startup", archive=True):
        if archive and self.journal.events():
            await self._checked_archive()
        result = await self.memory.prepare(reason=reason)
        if not isinstance(result, dict):
            raise ValueError("Invalid memory preparation result")
        self._prepared = result.get("status") == "ready"
        self._acknowledged = False
        self._last_packet = None
        self._memory_partial = None if self._prepared else result
        if not self._prepared:
            await self._notify("memory_required", {"status": result.get("status", "partial"),
                                                   "unknowns": result.get("unknowns") or
                                                   (result.get("packet") or {}).get("unknowns", [])})
        return result

    async def _fresh(self):
        if not self._acknowledged:
            return False
        try:
            status = await self.memory.status()
        except Exception as exc:
            log.debug("Memory status check failed (%s)", type(exc).__name__)
            self._acknowledged = False
            self._prepared = False
            self._memory_partial = {"status": "unavailable", "unknowns": [
                "The local memory service is unavailable; effects are paused."]}
            return False
        valid = (isinstance(status, dict) and status.get("acknowledged") is True
                 and not status.get("stale") and status.get("status") == "ready")
        if not valid:
            self._acknowledged = False
            if isinstance(status, dict) and status.get("status") != "ready":
                self._memory_partial = status
        return valid

    async def _approve_command(self, request):
        if self._approve_callback is None:
            return False
        decision = await _await(self._approve_callback(deepcopy(request)))
        if decision is not True:
            return False
        # A person can leave the confirmation open while another client edits
        # the project. Approval of the exact command doesn't refresh memory.
        if not await self._fresh():
            self._record("approval_stale", {"action_id": request.get("action_id"),
                                             "run_id": self.run["id"], "generation": self.run["generation"]})
            raise RuntimeError("resume_required: project state changed during command approval")
        return True

    def _available_tools(self):
        if self._compaction is not None:
            return []
        if self._needs_compaction:
            names = self._read_memory | {"compact_context"}
        elif not self._acknowledged:
            names = self._read_memory
        else:
            names = set(self._schemas)
        return [deepcopy(schema) for name, schema in self._schemas.items() if name in names]

    async def turn(self, text):
        if self._closed:
            return self._result("closed")
        if self._lock.locked():
            return self._result("busy")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT_CHARS:
            return self._result("invalid_input")
        async with self._lock:
            self._execution_epoch += 1
            self._turn_task = asyncio.current_task()
            self._turn_usage, self._model_calls, self._tool_calls = {}, 0, 0
            self._last_user = self._redact(text)
            result = None
            if self.pending_actions():
                self._turn_task = None
                return self._result("uncertain", unknowns=[
                    "An earlier action has no confirmed outcome. Resolve it explicitly before continuing."],
                    pending_actions=self.pending_actions())
            try:
                await self._start_mcp()
                if not self._prepared:
                    previous = bool(self.journal.events())
                    prepared = await self._prepare(reason="resume" if previous else "startup", archive=previous)
                    if not self._prepared:
                        return self._result("partial", unknowns=prepared.get("unknowns", []))
                    # New process == new model context. Persisted history is
                    # restored through the archived memory packet, not a blind
                    # replay of all old messages and unbounded tool output.
                    self._context = []
                    self._reads.clear()
                elif self._acknowledged:
                    await self._fresh()
                    if self._memory_partial is not None:
                        return self._result("partial", unknowns=self._memory_partial.get("unknowns", []))
                await self._message({"role": "user", "content": self._last_user})
                self._record("turn_started", {"run_id": self.run["id"],
                                               "generation": self.run["generation"]})
                await self._notify("status", {"state": "running", "memory": "requires_model_ack"
                                               if not self._acknowledged else "acknowledged"})
                if self.run["client"] == "copilot":
                    async with asyncio.timeout(self.max_turn_seconds):
                        result = await self._copilot_turn(self._last_user)
                else:
                    result = await self._api_turn()
                self._record("turn_finished", {"status": result["status"], "model_calls": result["model_calls"],
                                                "tool_calls": self._tool_calls})
                await self._checked_archive()
                await self._notify("status", {"state": result["status"]})
                return result
            except asyncio.CancelledError:
                self._record("turn_interrupted", {"run_id": self.run["id"], "generation": self.run["generation"]})
                await self._stop_tools()
                self._acknowledged = False
                self._prepared = False
                self._last_packet = None
                await self._archive_stopped()
                return self._result("interrupted", pending_actions=self.pending_actions())
            except TimeoutError:
                self._record("turn_interrupted", {"reason": "time_limit"})
                await self._stop_tools()
                self._prepared = self._acknowledged = False
                await self._archive_stopped()
                return self._result("limit_reached", unknowns=["Turn time limit reached."],
                                    pending_actions=self.pending_actions())
            except Exception as exc:
                code = getattr(exc, "code", None)
                if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 80:
                    code = "agent_error"
                diagnostic = self._provider_diagnostic(exc)
                self._record("turn_error", {"code": code, **diagnostic})
                self._prepared = self._acknowledged = False
                await self._notify("error", {"code": code, **diagnostic})
                if code == "archive_incomplete":
                    unknowns = self._memory_partial.get("unknowns", []) if isinstance(self._memory_partial, dict) else []
                    return self._result("partial", result.get("text", "") if result else "",
                                        effects_status=result.get("status") if result else None,
                                        unknowns=unknowns,
                                        pending_actions=self.pending_actions())
                await self._archive_stopped()
                if code == "context_limit":
                    hint = diagnostic.get("hint") or (
                        "The request exceeds the configured model context. Increase the profile's explicit "
                        "context window or continue with a shorter request restored from archived memory. "
                        "The request was not automatically retried.")
                    return self._result("context_limit", error_code=code, **diagnostic,
                                        unknowns=[hint], pending_actions=self.pending_actions())
                return self._result("error", error_code=code, **diagnostic, unknowns=[
                    diagnostic.get("hint") or "The turn stopped. No request or action was automatically repeated."],
                    pending_actions=self.pending_actions())
            finally:
                self._turn_task = None

    async def _start_mcp(self):
        """Start the run's MCP servers once, before the first model call; failures are reported, not fatal."""
        if self._mcp_started:
            return
        # An interrupted start has stopped what it started; it is tried again on the next turn.
        failures = await self.mcp.start(self._record)
        self._mcp_started = True
        for schema in self.mcp.schemas():
            self._schemas[schema["name"]] = deepcopy(schema)
        for server_id, code in failures:
            await self._notify("mcp_server_failed", {"server_id": server_id, "code": code})

    async def _complete(self):
        """One model call (#126). When the provider refuses a request that offered third-party tools, either
        naming the tool definitions (``tools_rejected``) or with no usable cause (``provider_error``,
        ``unsupported_request``), and nothing was shown yet, the call is made once more without them. If that
        succeeds they are withdrawn for the run and reported; if it fails, they stay and the original error
        stands. Any other refusal, a context overflow included, is not retried."""
        from cairnmind.agents.api_providers import ProviderError
        tools = self._available_tools()
        messages = [{"role": "system", "content": SYSTEM}, *deepcopy(self._context)]
        self._emitted = False
        try:
            return await self.provider.complete(messages, tools, on_delta=self._delta)
        except ProviderError as error:
            offered = sorted({self.mcp.catalogue[t["name"]]["server_id"] for t in tools if t["name"] in self.mcp.catalogue})
            if (not offered or self._emitted
                    or error.code not in {"tools_rejected", "provider_error", "unsupported_request"}):
                raise
            first = error
        self._stream_buffer = self._display_pending = ""
        without = [tool for tool in tools if tool["name"] not in self.mcp.catalogue]
        try:
            reply = await self.provider.complete(messages, without, on_delta=self._delta)
        except ProviderError:
            # Not the third-party tools: they stay offered, and the person sees the original refusal.
            self._record("mcp_tools_kept", {"code": first.code, "servers": offered})
            await self._notify("mcp_tools_kept", {"code": first.code, "servers": offered})
            raise first from None
        for name in list(self.mcp.catalogue):
            self._schemas.pop(name, None)
        data = {"code": first.code, "servers": offered, "named_by_provider": first.code == "tools_rejected"}
        self._record("mcp_tools_withdrawn", data)
        await self._notify("mcp_tools_withdrawn", data)
        return reply

    async def _stop_tools(self):
        stop = getattr(self.tools, "interrupt", None)
        if stop is not None:
            try:
                await _await(stop())
            except Exception as exc:
                log.debug("Tool interrupt failed (%s)", type(exc).__name__)

    async def interrupt(self):
        self._execution_epoch += 1
        self._copilot_token = None
        task = self._turn_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        await self._stop_tools()
        stop = getattr(self.provider, "interrupt", None)
        if stop is not None:
            try:
                await _await(stop())
            except Exception as exc:
                log.debug("Provider interrupt failed (%s)", type(exc).__name__)
        return {"interrupted": task is not None}

    cancel = interrupt

    async def close(self):
        self._closed = True
        task = self._turn_task
        await self.interrupt()
        if task is not None and task is not asyncio.current_task() and not task.done():
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
        # No server process and no command outlives the run.
        for closer in (lambda: self.mcp.close(self._record), getattr(self.tools, "close", None)):
            try:
                if closer is not None:
                    await _await(closer())
            except Exception as exc:
                log.debug("Agent resource close failed (%s)", type(exc).__name__)
        for target in (self.provider, self.memory):
            closer = getattr(target, "aclose", None) or getattr(target, "close", None)
            if closer is not None:
                try:
                    await _await(closer())
                except Exception as exc:
                    log.debug("Agent backend close failed (%s)", type(exc).__name__)
