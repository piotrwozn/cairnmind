"""Tool dispatch, compaction, usage accounting, and provider turn loops for AgentEngine."""
from __future__ import annotations

import asyncio
import json
import math
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from jsonschema import Draft202012Validator

from cairnmind.agents.api_agent_stream import (
    _BOOTSTRAP_TOOLS,
    MAX_REPLY_CHARS,
    MAX_TOOLS_PER_REPLY,
    NEXT_STEP,
    SYSTEM,
    _ArchiveIncomplete,
    _await,
    _json,
)
from cairnmind.memory.memory_egress import coding_read, coding_written


class _AgentTurnMixin:
    """Memory/tool execution, context compaction, usage tracking, and API/Copilot turn loops."""

    if TYPE_CHECKING:
        run: dict[str, Any]
        profile: dict[str, Any]
        memory: Any
        tools: Any
        provider: Any
        mcp: Any
        max_model_calls: int
        max_tool_calls: int
        _held: Any
        _memory_tools: dict[str, Any]
        _read_memory: set[str]
        _schemas: dict[str, Any]
        _prepared: bool
        _acknowledged: bool
        _last_packet: Any
        _acked_packet: Any
        _context: list[dict[str, Any]]
        _reads: Any
        _closed: bool
        _turn_task: Any
        _compaction: dict[str, Any] | None
        _needs_compaction: bool
        _compaction_notice: bool
        _renewed_context: bool
        _last_user: str
        _turn_usage: dict[str, Any]
        _model_calls: int
        _tool_calls: int
        _seen_calls: set[str]
        _memory_partial: dict[str, Any] | None
        _stream_buffer: str
        _display_pending: str
        _request_id: str | None
        _execution_epoch: int
        _copilot_token: Any
        _provider_context: Any

        @staticmethod
        def _error(code: Any, **extra: Any) -> dict[str, Any]: ...
        def _redact(self, value: Any) -> Any: ...
        def _record(self, event_type: Any, data: Any) -> Any: ...
        async def _notify(self, event_type: Any, data: Any) -> None: ...
        async def _delta(self, text: Any) -> None: ...
        async def _finish_stream(self) -> None: ...
        async def _checked_archive(self) -> Any: ...
        async def _message(self, message: Any) -> Any: ...
        def _over_budget(self) -> bool: ...
        async def _prepare(self, *, reason: str = "startup", archive: bool = True) -> Any: ...
        async def _fresh(self) -> bool: ...
        async def _complete(self) -> Any: ...

    async def _memory_tool(self, name, arguments):
        if name == "ack_resume":
            packet = self._last_packet
            if (not packet or packet.get("status") != "ready"
                    or arguments.get("packet_id") != packet.get("id")
                    or arguments.get("packet_hash") != packet.get("hash")):
                return self._error("resume_required", unknowns=[
                    "Read the complete ready packet using resume_context before its exact ACK."])
        result = await self.memory.execute(name, arguments)
        if self._held and name in ("read_source", "memory_records"):
            return coding_read(name, result, self._held, self.run["session_id"])
        if self._held and name in ("remember", "checkpoint"):
            return coding_written(result)
        if name == "resume_context":
            packet = result.get("packet") if isinstance(result, dict) else None
            # The service hands back the same packet while nothing changed, and its ACK stays valid; reading
            # it again must not leave the run asking for an ACK the workspace shows as given (#157).
            self._acknowledged = (self._acknowledged and isinstance(packet, dict) and result.get("status") == "ready"
                                  and self._acked_packet == (packet.get("id"), packet.get("hash")))
            if packet and self._redact(packet) != packet:
                # Never let the model acknowledge a hash after silently
                # changing the authoritative bytes it was supposed to read.
                self._last_packet = None
                self._acknowledged, self._acked_packet = False, None  # a kept ACK never survives this refusal
                self._memory_partial = {"status": "unavailable", "unknowns": [
                    "The authoritative packet contains credential material; it was not delivered or acknowledged."]}
                return self._error("credential_in_packet", **self._memory_partial)
            self._last_packet = packet if packet and result.get("status") == "ready" else None
            self._memory_partial = result if isinstance(result, dict) and result.get("status") != "ready" else None
            if self._last_packet and not self._acknowledged:
                # A small model skipped the ACK and then found no file tool (#240).
                result = {**result, "next_step": NEXT_STEP}
        elif name == "ack_resume":
            self._acknowledged = (isinstance(result, dict) and result.get("status") == "acknowledged"
                                  and result.get("packet_id") == arguments.get("packet_id")
                                  and result.get("packet_hash") == arguments.get("packet_hash"))
            if not self._acknowledged:
                return self._error("resume_required", unknowns=["The service did not accept the exact ACK."])
            self._acked_packet = (result["packet_id"], result["packet_hash"])
            await self._notify("status", {"memory": "acknowledged", "packet_id": result["packet_id"]})
        return result

    async def _compact(self, arguments):
        if not await self._fresh():
            return self._error("resume_required")
        archive = await self._checked_archive()
        state = {**arguments["state"], "worktree": self.run["worktree"],
                 "origin": "agent_context_compaction"}
        checkpoint = await self.memory.execute("checkpoint", {"state": state})
        if self._held:  # the result carries metadata merged in from earlier checkpoints of other sessions
            checkpoint = coding_written(checkpoint)
        self._compaction = {"state": state, "checkpoint": checkpoint,
                            "archive": archive, "current_request": self._last_user}
        self._acknowledged = False
        self._last_packet = None
        return {"ok": True, "checkpoint": checkpoint, "context_reset_pending": True,
                "next_step": "Finish this tool batch; the host will renew context and require a new model ACK."}

    async def _dispatch(self, name, arguments, call_id):
        schema = self._schemas.get(name)
        if not schema or not isinstance(arguments, dict):
            return self._error("invalid_tool_call")
        # A third-party schema is checked by McpTools.call, off the event loop and with a time bound.
        if name not in self.mcp.catalogue and not Draft202012Validator(schema["parameters"]).is_valid(arguments):
            from cairnmind.agents.agent_arguments import argument_problem
            problem = argument_problem(schema["parameters"], arguments, name) or {}
            return self._error("invalid_tool_arguments", **problem)
        if self._redact(arguments) != arguments:
            return self._error("credential_in_tool_arguments")
        if self._compaction is not None:
            return self._error("context_reset_required")
        if name not in self._read_memory and not await self._fresh():
            return self._error("resume_required", unknowns=["Read and ACK the current ready packet before effects."])
        if self._needs_compaction and name not in self._read_memory | {"compact_context"}:
            return self._error("context_compaction_required")
        try:
            if name == "compact_context":
                return await self._compact(arguments)
            if name in self._memory_tools:
                return await self._memory_tool(name, arguments)
            if name in self.mcp.catalogue:
                called = self.mcp.results
                result = await self.mcp.call(name, arguments, call_id, self.tools.command_timeout_seconds)
                if name in self.mcp.withdrawn and self._schemas.pop(name, None) is not None:
                    await self._notify("mcp_tool_withdrawn", {"name": name})
                if self.mcp.results != called:
                    # Archived at once: the service then marks this session's later memory writes degraded.
                    await self._checked_archive()
                return result
            if name in {"write_file", "delete_file"}:
                refused = self._reads.refusal(arguments.get("path"), arguments.get("expected_sha256"), name)
                if refused is not None:
                    return refused
            self.tools.current_tool_call_id = call_id
            try:
                return await self.tools.execute(name, arguments)
            finally:
                self.tools.current_tool_call_id = None
        except asyncio.CancelledError:
            raise
        except _ArchiveIncomplete:
            unknowns = self._memory_partial.get("unknowns", []) if isinstance(self._memory_partial, dict) else []
            return self._error("archive_incomplete", unknowns=unknowns)
        except Exception as exc:
            from cairnmind.agents.agent_memory import MemoryRefusal
            if isinstance(exc, MemoryRefusal):
                # The service's refusal of a confirmation, or a fixed hint: what to do instead (#240).
                return self._error(exc.code, message=exc.message)
            # Native/service/provider messages can contain document or HTTP
            # text. Only a fixed classification crosses the tool boundary.
            text = str(exc).lower()
            if "resume_required" in text:
                self._acknowledged = False
                return self._error("resume_required")
            if "version conflict" in text or "changed" in text or "conflict" in text:
                return self._error("state_conflict", unknowns=["Read the current state before another proposal."])
            return self._error("tool_failed")

    async def _execute_call(self, call, *, journal_assistant=False):
        self._tool_calls += 1
        if call["name"] not in _BOOTSTRAP_TOOLS:
            self._renewed_context = False
        if self.run["client"] == "copilot" and self._tool_calls > self.max_tool_calls:
            return self._error("tool_limit_reached")
        if journal_assistant:
            await self._message({"role": "assistant", "content": "", "tool_calls": [call]})
        self._record("tool_requested", {"tool_call_id": call["id"], "name": call["name"],
                                         "arguments": call["arguments"]})
        await self._notify("tool_start", {"tool_call_id": call["id"], "name": call["name"],
                                           "arguments": call["arguments"]})
        raw = await self._dispatch(call["name"], call["arguments"], call["id"])
        result = self._redact(raw)
        delivered = await self._message({"role": "tool", "tool_call_id": call["id"],
                                         "name": call["name"], "content": _json(result)})
        if call["name"] not in self.mcp.catalogue:
            # Masking is told apart from file text that merely contains the mask's words (#157).
            self._reads.observe(call["name"], json.loads(delivered["content"]),
                                masked=result != raw or delivered["content"] != _json(result))
        await self._notify("tool_result", {"tool_call_id": call["id"], "name": call["name"],
                                            "result": result})
        return json.loads(delivered["content"]) if self.run["client"] == "copilot" else result

    def _usage(self, usage):
        if not isinstance(usage, dict):
            usage = {}
        observed_both = all(type(usage.get(key)) is int and 0 <= usage[key] <= 10**12
                            for key in ("input_tokens", "output_tokens"))
        self._turn_usage["usage_complete"] = self._turn_usage.get("usage_complete", True) and observed_both
        for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens"):
            value = usage.get(key)
            if type(value) is int and 0 <= value <= 10**12:
                self._turn_usage[key] = self._turn_usage.get(key, 0) + value
        # SDK-specific observed counters can be shown as observations without
        # translating subscription usage into an invented dollar amount.
        if isinstance(usage.get("copilot"), dict):
            self._turn_usage["copilot"] = deepcopy(usage["copilot"])
        for key in ("cost", "cache_read_tokens", "cache_write_tokens", "premium_requests", "credits"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 10**15:
                observed = self._turn_usage.setdefault("provider_observations", {})
                observed[key] = observed.get(key, 0) + value
        pricing = self.profile.get("pricing")
        self._turn_usage["estimated_cost"] = None
        if isinstance(pricing, dict) and self._turn_usage["usage_complete"]:
            incoming, outgoing = pricing.get("input_per_million"), pricing.get("output_per_million")
            if (isinstance(incoming, (int, float)) and isinstance(outgoing, (int, float))
                    and type(incoming) in (float, int) and type(outgoing) in (float, int)
                    and math.isfinite(incoming) and math.isfinite(outgoing) and incoming >= 0 and outgoing >= 0):
                self._turn_usage["estimated_cost"] = (
                    self._turn_usage.get("input_tokens", 0) * incoming
                    + self._turn_usage.get("output_tokens", 0) * outgoing) / 1_000_000
                self._turn_usage["cost_currency"] = "USD"
        self._record("usage", deepcopy(self._turn_usage))

    def _reply(self, reply):
        if not isinstance(reply, dict) or not isinstance(reply.get("text", ""), str):
            raise ValueError("Invalid provider response")
        text = reply.get("text", "")
        calls = reply.get("tool_calls") or []
        if len(text) > MAX_REPLY_CHARS or not isinstance(calls, list) or len(calls) > MAX_TOOLS_PER_REPLY:
            raise ValueError("Provider response exceeds bounds")
        identifiers = set()
        for call in calls:
            if (not isinstance(call, dict) or not isinstance(call.get("id"), str)
                    or not 1 <= len(call["id"]) <= 256 or call["id"] in identifiers
                    or call["id"] in self._seen_calls or not isinstance(call.get("name"), str)
                    or not isinstance(call.get("arguments"), dict)):
                raise ValueError("Invalid or repeated provider tool call")
            _json(call["arguments"])
            identifiers.add(call["id"])
        self._seen_calls.update(identifiers)
        self._usage(reply.get("usage"))
        message = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = deepcopy(calls)
        if reply.get("continuation") is not None:
            message["continuation"] = deepcopy(reply["continuation"])
        return message, calls

    def _result(self, status, text="", **extra):
        return {"status": status, "text": self._redact(text), "usage": deepcopy(self._turn_usage),
                "model_calls": None if self.run["client"] == "copilot" else self._model_calls,
                "tool_calls": self._tool_calls, **extra}

    async def _finish_compaction(self):
        summary = self._compaction
        if summary is None:
            return None
        await self._checked_archive()
        prepared = await self._prepare(reason="refresh", archive=False)
        if not self._prepared:
            return prepared
        # A checkpoint is inspected, explicitly labelled prior state; the
        # current packet still has to be read and acknowledged by the model.
        self._reads.clear()
        self._context = [{"role": "user", "content": _json({
            "host": "Context renewed from durable evidence; read resume_context and ACK before continuing.",
            "checkpoint": summary["checkpoint"], "state": summary["state"],
            "current_request": summary["current_request"],
        })}]
        self._record("context_compacted", {"checkpoint": summary["checkpoint"], "state": summary["state"]})
        self._compaction = None
        self._needs_compaction = self._compaction_notice = False
        self._renewed_context = True
        await self._notify("context_compacted", {"checkpoint": summary["checkpoint"]})
        return prepared

    async def _provider_context_check(self, current=None):
        """A provider that started a new conversation (Copilot's session renewed on a catalog change, on
        compaction or by any other path) has lost what the model read and acknowledged. The ledger and the
        ACK describe the old conversation, so both go: otherwise a model that saw only a slice in the new
        conversation could replace or delete a whole file (#157)."""
        if current is None:
            current = getattr(self.provider, "session_id", None)
        if not isinstance(current, str) or current == self._provider_context:
            return
        previous, self._provider_context = self._provider_context, current
        if previous is None:
            return  # the first identity this engine learns; the ledger is cleared when a run is prepared
        self._reads.clear()
        self._acknowledged, self._acked_packet, self._last_packet = False, None, None
        self._record("provider_context_reset", {"run_id": self.run["id"], "generation": self.run["generation"]})
        await self._notify("status", {"memory": "requires_model_ack"})

    async def _context_notice(self):
        if not self._over_budget():
            return
        self._needs_compaction = True
        if not self._compaction_notice:
            notice = {"role": "user", "content": (
                "HOST CONTEXT BUDGET: preserve an evidence-based checkpoint through compact_context "
                "before further effects. Refresh resume_context and exact ACK first if needed. "
                "Full prior messages and results remain in the durable archive.")}
            self._context.append(notice)
            self._record("context_notice", {"reason": "context_budget"})
            self._compaction_notice = True

    async def _api_turn(self):
        while self._model_calls < self.max_model_calls:
            await self._context_notice()
            self._model_calls += 1
            self._request_id = str(uuid4())
            self._stream_buffer = ""
            self._display_pending = ""
            self._record("provider_started", {"request_id": self._request_id, "call": self._model_calls,
                                               "provider": self.profile.get("provider", "api")})
            reply = await self._complete()
            await self._finish_stream()
            message, calls = self._reply(reply)
            await self._message(message)
            self._stream_buffer = ""  # Complete assistant message is now durable.
            self._record("provider_completed", {"request_id": self._request_id})
            await self._notify("usage", deepcopy(self._turn_usage))
            if not calls:
                if not self._acknowledged:
                    return self._result("resume_required", message["content"], unknowns=[
                        "The model did not complete the current packet's read/ACK protocol."])
                if self._needs_compaction:
                    return self._result("context_limit", message["content"], unknowns=[
                        "A checkpoint is required before context can be shortened."])
                return self._result("completed", message["content"])
            for call in calls:
                await self._execute_call(call)
            if self._memory_partial is not None:
                return self._result("partial", unknowns=self._memory_partial.get("unknowns", []))
            if self._compaction is not None:
                prepared = await self._finish_compaction()
                if prepared and not self._prepared:
                    return self._result("partial", unknowns=prepared.get("unknowns", []))
        return self._result("limit_reached", unknowns=["Model call limit reached; continue explicitly to renew the budget."])

    async def _copilot_turn(self, text):
        tool_lock = asyncio.Lock()
        epoch = self._execution_epoch

        def handler_for(token):
            async def handler(name, arguments, tool_call_id=None):
                # SDKs may issue custom tools in parallel. Serialize one
                # session's envelopes, ACK transitions and human approvals;
                # retired/queued callbacks cannot gain a later turn's lease.
                async with tool_lock:
                    if (self._copilot_token is not token or epoch != self._execution_epoch
                            or self._turn_task is None or self._closed):
                        return self._error("turn_interrupted")
                    call_id = tool_call_id or "copilot_" + uuid4().hex
                    if call_id in self._seen_calls:
                        return self._error("repeated_tool_call")
                    self._seen_calls.add(call_id)
                    # run_turn may have renewed the session before this call; check before any effect.
                    await self._provider_context_check()
                    call = {"id": call_id, "name": name, "arguments": arguments}
                    return await self._execute_call(call, journal_assistant=True)
            return handler

        async def event(value):
            # SDK events are incremental observations, while run_turn returns
            # their aggregate. Count that aggregate once on completion.
            await self._notify("provider_event", value if isinstance(value, dict) else {})

        # SDK internal request counts aren't advertised as observable.  Bound
        # host tools and elapsed time instead; reset never grants a new budget.
        for _ in range(3):
            self._request_id = str(uuid4())
            self._stream_buffer = ""
            self._display_pending = ""
            self._record("provider_started", {"request_id": self._request_id, "provider": "copilot"})
            token = object()
            self._copilot_token = token
            try:
                reply = await self.provider.run_turn(
                    text, list(self._schemas.values()), handler_for(token), system=SYSTEM,
                    on_delta=self._delta, on_event=event)
            finally:
                self._copilot_token = None
            await self._finish_stream()
            if not isinstance(reply, dict):
                raise ValueError("Invalid Copilot result")
            await self._provider_context_check(reply.get("copilot_session_id"))
            await self._provider_context_check()
            self._usage(reply.get("usage"))
            response = reply.get("text", "")
            if not isinstance(response, str) or len(response) > MAX_REPLY_CHARS:
                raise ValueError("Invalid Copilot text")
            await self._message({"role": "assistant", "content": response})
            self._stream_buffer = ""
            self._record("provider_completed", {"request_id": self._request_id})
            if self._memory_partial is not None:
                return self._result("partial", response, unknowns=self._memory_partial.get("unknowns", []))
            if self._compaction is not None:
                reset = getattr(self.provider, "reset_context", None)
                if reset is None:
                    return self._result("context_limit", unknowns=["Copilot context reset unavailable."])
                await _await(reset())
                await self._provider_context_check()
                prepared = await self._finish_compaction()
                if prepared and not self._prepared:
                    return self._result("partial", unknowns=prepared.get("unknowns", []))
                text = self._context[-1]["content"]
                continue
            if self._tool_calls >= self.max_tool_calls:
                return self._result("limit_reached", response, unknowns=["Host tool limit reached."])
            if not self._acknowledged:
                return self._result("resume_required", response, unknowns=[
                    "Copilot did not complete the exact model read/ACK protocol."])
            return self._result("completed", response)
        return self._result("context_limit", unknowns=["Context renewal limit reached."])
