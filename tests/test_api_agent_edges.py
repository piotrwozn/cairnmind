"""Edge-case tests for api_agent (AgentEngine streaming, redaction, MCP retry, copilot, close)."""
from __future__ import annotations

import asyncio
import json

import pytest

from cairnmind.api_agent import AgentEngine, TOOL_CONTEXT_CHARS
from cairnmind.api_providers import ProviderError
from cairnmind.config import Settings


class _MiniJournal:
    def __init__(self):
        self._events = []
        self._secrets = {"TOPSECRET99"}

    def append(self, event_type, data):
        ev = {"id": f"ev-{len(self._events) + 1}", "seq": len(self._events) + 1, "type": event_type, "data": data}
        self._events.append(ev)
        return ev

    def events(self):
        return list(self._events)

    def pending_actions(self):
        return []


class _MiniMemory:
    def __init__(self, *, archive_ok=True, prepare_ok=True):
        self.archive_ok = archive_ok
        self.prepare_ok = prepare_ok
        self.closed = False

    async def archive(self, journal):
        if self.archive_ok is None:
            raise RuntimeError("archive down")
        if not self.archive_ok:
            return {"status": "partial", "unknowns": ["archive gap"]}
        return {"status": "complete", "source_ids": [f"src-{i}" for i in range(1, 50)]}

    async def prepare(self, **kwargs):
        if not self.prepare_ok:
            return {"status": "partial", "unknowns": ["prepare gap"]}
        return {"status": "ready", "packet": {"id": "pkt-1", "hash": "h1"}}

    async def fresh(self, **kwargs):
        return {"status": "fresh"}

    async def close(self):
        self.closed = True


class _MiniTools:
    def __init__(self, *, fail_interrupt=False, fail_close=False):
        self.fail_interrupt = fail_interrupt
        self.fail_close = fail_close

    def interrupt(self):
        if self.fail_interrupt:
            raise RuntimeError("tool interrupt err")

    def close(self):
        if self.fail_close:
            raise RuntimeError("tool close err")


class _StubProvider:
    def __init__(self, fn=None, *, fail_interrupt=False, fail_close=False):
        self.fn = fn
        self.fail_interrupt = fail_interrupt
        self.fail_close = fail_close
        self.calls = []

    async def complete(self, messages, tools, on_delta=None):
        self.calls.append((messages, tools))
        return await self.fn(messages, tools, on_delta)

    async def run_turn(self, text, tools, handler, *, system, on_delta, on_event):
        return await self.fn(text, tools, handler, on_delta, on_event)

    async def interrupt(self):
        if self.fail_interrupt:
            raise RuntimeError("prov interrupt err")

    async def aclose(self):
        if self.fail_close:
            raise RuntimeError("prov close err")


def _make_engine(tmp_path, *, client="api", provider=None, memory=None, tools=None, on_event=None, on_delta=None):
    settings = Settings(data_dir=tmp_path / "data")
    run = {
        "id": "run-1", "generation": 1, "project_id": "proj-1",
        "worktree": str(tmp_path), "session_id": "sess-1", "client": client,
    }
    profile = {
        "id": "prof-1", "label": "P", "client": client, "provider": "openai",
        "model": "gpt-4o", "base_url": "https://api.openai.com/v1",
        "limits": {"max_model_calls": 5, "max_output_tokens": 512, "max_tool_calls": 2, "max_turn_seconds": 1},
    }
    return AgentEngine(
        settings,
        run,
        profile,
        key="APIKEY1234",
        journal=_MiniJournal(),
        memory=memory or _MiniMemory(),
        tools=tools or _MiniTools(),
        provider=provider or _StubProvider(),
        on_event=on_event,
        on_delta=on_delta,
    )


@pytest.mark.asyncio
async def test_agent_engine_stream_projection_packets_and_helpers(tmp_path):
    def bad_cb(*args):
        raise RuntimeError("ui crash")

    eng = _make_engine(tmp_path, on_event=bad_cb, on_delta=bad_cb)
    # _redact without journal.redact returns unchanged
    assert eng._redact({"a": 1}) == {"a": 1}
    # _notify swallows UI exceptions
    await eng._notify("status", {"state": "ok"})
    # _delta ignores non-str, flushes >= 2048 chars, holds partial secret prefix, _finish_stream redacts it
    await eng._delta(123)
    await eng._delta("a" * 2100 + "TOPSE")
    assert eng._display_pending == "TOPSE"
    await eng._finish_stream()
    assert eng._display_pending == ""

    # _archive_stopped swallows archive failure
    eng.memory.archive_ok = None
    await eng._archive_stopped()
    eng.memory.archive_ok = True

    # _message with huge non-JSON tool result exercises _bounded_projection
    delivered = await eng._message({"role": "tool", "name": "run_command", "content": "Z" * (TOOL_CONTEXT_CHARS + 500)})
    parsed = json.loads(delivered["content"])
    assert parsed["host_context_truncated"] is True
    assert parsed["full_result_source_id"].startswith("src-")

    # _bounded_projection deep nesting, list truncation, oversized dict result_excerpt
    deep = {"a": {"b": {"c": {"d": {"e": {"f": "leaf"}}}}}}
    assert AgentEngine._bounded_projection(deep)["a"]["b"]["c"]["d"]["e"] == "[nested result available in archived source]"
    assert len(AgentEngine._bounded_projection(list(range(20)))) == 8
    huge_dict = {"ok": True, **{f"k{i}": "x" * 2000 for i in range(25)}}
    projected = AgentEngine._bounded_projection(huge_dict)
    assert projected["ok"] is True and "result_excerpt" in projected

    # _ready_packet invalid JSON and _replace_packets with invalid JSON
    assert AgentEngine._ready_packet("{bad") is False
    eng._context.append({"role": "tool", "name": "resume_context", "content": "{bad"})
    eng._replace_packets()
    assert json.loads(eng._context[-1]["content"])["replaced_by_a_later_read"] is True

    # messages() fallback without journal.messages()
    assert eng.messages()


@pytest.mark.asyncio
async def test_agent_engine_mcp_retry_error_codes_timeout_and_close(tmp_path):
    # Case 1: MCP tools withdrawn on retry success
    calls = 0

    async def provider_retry_ok(messages, tools, on_delta):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError("tools_rejected")
        return {"text": "done", "tool_calls": [], "usage": {}}

    eng = _make_engine(tmp_path, provider=_StubProvider(provider_retry_ok))
    eng._schemas["mcp_demo_echo"] = {"name": "mcp_demo_echo", "description": "echo", "parameters": {}}
    eng.mcp.catalogue["mcp_demo_echo"] = {"server_id": "demo", "tool": "echo"}
    eng._acknowledged = True
    res = await eng._complete()
    assert res["text"] == "done" and "mcp_demo_echo" not in eng._schemas

    # Case 2: MCP tools kept when retry also fails
    async def provider_retry_fail(messages, tools, on_delta):
        raise ProviderError("provider_error")

    eng2 = _make_engine(tmp_path, provider=_StubProvider(provider_retry_fail))
    eng2._schemas["mcp_demo_echo"] = {"name": "mcp_demo_echo", "description": "echo", "parameters": {}}
    eng2.mcp.catalogue["mcp_demo_echo"] = {"server_id": "demo", "tool": "echo"}
    eng2._acknowledged = True
    with pytest.raises(ProviderError) as exc_info:
        await eng2._complete()
    assert exc_info.value.code == "provider_error" and "mcp_demo_echo" in eng2._schemas

    # Turn validation: invalid_input, context_limit error, closed
    assert (await eng2.turn("   "))["status"] == "invalid_input"

    async def context_overflow(messages, tools, on_delta):
        raise ProviderError("context_limit")

    eng3 = _make_engine(tmp_path, provider=_StubProvider(context_overflow))
    res_ctx = await eng3.turn("hello")
    assert res_ctx["status"] == "context_limit"

    # Copilot turn edge cases: non-dict result, non-str text, timeout
    async def bad_copilot_dict(text, tools, handler, on_delta, on_event):
        return "not-a-dict"

    eng_cp = _make_engine(tmp_path, client="copilot", provider=_StubProvider(bad_copilot_dict))
    res_cp = await eng_cp.turn("hi")
    assert res_cp["status"] == "error"

    async def bad_copilot_text(text, tools, handler, on_delta, on_event):
        return {"text": 12345}

    eng_cp2 = _make_engine(tmp_path, client="copilot", provider=_StubProvider(bad_copilot_text))
    assert (await eng_cp2.turn("hi"))["status"] == "error"

    async def slow_copilot(text, tools, handler, on_delta, on_event):
        await asyncio.sleep(5)
        return {"text": "late"}

    eng_cp3 = _make_engine(tmp_path, client="copilot", provider=_StubProvider(slow_copilot))
    eng_cp3.max_turn_seconds = 0.05
    assert (await eng_cp3.turn("hi"))["status"] == "limit_reached"

    # close() swallows failures from tools/provider/mcp and marks closed
    eng_fail_close = _make_engine(
        tmp_path,
        provider=_StubProvider(fail_interrupt=True, fail_close=True),
        tools=_MiniTools(fail_interrupt=True, fail_close=True),
    )
    await eng_fail_close.close()
    assert (await eng_fail_close.turn("after close"))["status"] == "closed"
