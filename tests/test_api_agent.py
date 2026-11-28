"""Own-agent orchestration: actual readback, durable effects and no blind replay."""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from cairnmind.agents.agent_journal import AgentJournal
from cairnmind.agents.agent_memory import AgentMemoryClient
from cairnmind.agents.api_agent import AgentEngine
from cairnmind.agents.api_providers import ProviderError
from cairnmind.config import Settings
from cairnmind.service import create_app
from cairnmind.store import Store


def call(name, arguments=None, identifier=None):
    return {"id": identifier or uuid4().hex, "name": name, "arguments": arguments or {}}


def reply(*calls, text="", usage=None):
    return {"text": text, "tool_calls": list(calls), "usage": usage or {}}


class ScriptedProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    async def complete(self, messages, tools, *, on_delta=None):
        self.requests.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        assert self.responses, "Unexpected provider generation"
        value = self.responses.pop(0)
        if callable(value):
            value = value(messages, tools)
        if asyncio.iscoroutine(value):
            value = await value
        if isinstance(value, Exception):
            raise value
        if on_delta and value.get("text"):
            await on_delta(value["text"])
        return value

    async def close(self):
        self.closed = True


class Memory:
    def __init__(self, *, status="ready"):
        self.ready = status
        self.acked = False
        self.read = False
        self.stale = False
        self.calls = []
        self.archives = 0
        self.prepares = 0
        self.packet = {"id": "packet-1", "hash": "a" * 64, "status": status,
                       "active_plan": None, "active_task": None}

    async def prepare(self, reason="startup"):
        self.prepares += 1
        self.acked = self.read = False
        self.stale = False
        return {"status": self.ready, "packet": deepcopy(self.packet),
                "unknowns": ["Transcript gap"] if self.ready != "ready" else []}

    async def status(self):
        return {"status": self.ready, "acknowledged": self.acked and not self.stale, "stale": self.stale}

    async def archive(self, journal):
        self.archives += 1
        return {"status": "complete", "sequence": len(journal.events()),
                "source_ids": [f"source-{n}" for n in range(1, len(journal.events()) + 1)]}

    def ack_arguments(self):
        return {"packet_id": self.packet["id"], "packet_hash": self.packet["hash"],
                "readback": {"plan": None, "task": None}}

    async def execute(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        if name == "resume_context":
            self.read = True
            self.stale = False
            return {"status": self.ready, "packet": deepcopy(self.packet),
                    "unknowns": ["Transcript gap"] if self.ready != "ready" else []}
        if name == "ack_resume":
            if not self.read or arguments != self.ack_arguments():
                raise RuntimeError("resume_required: wrong actual packet readback")
            self.acked = True
            return {"status": "acknowledged", "packet_id": self.packet["id"],
                    "packet_hash": self.packet["hash"], "id": "ack-1"}
        if name in {"checkpoint", "remember", "save_plan"}:
            if not self.acked or self.stale:
                raise RuntimeError("resume_required")
            self.stale = True
            return {"id": "saved-" + name, "source_id": "source-checkpoint"}
        return {"records": [], "unknowns": []}


@pytest.fixture
def setup(tmp_path):
    worktree = tmp_path / "project"
    worktree.mkdir()
    settings = Settings(tmp_path / "private-data")
    run = {"id": "run-one", "generation": 1, "project_id": "project-one",
           "worktree": str(worktree), "session_id": "session-one", "client": "api"}
    profile = {"id": "api", "client": "api", "label": "API", "provider": "ollama",
               "base_url": "http://127.0.0.1:11434", "model": "tool-model", "limits": {}}
    return settings, run, profile, worktree


def engine(setup, provider, memory=None, **kwargs):
    settings, run, profile, _ = setup
    return AgentEngine(settings, run, profile, provider=provider, memory=memory or Memory(), **kwargs)


def tools_results(agent):
    return [json.loads(message["content"]) for message in agent.messages() if message["role"] == "tool"]


@pytest.mark.asyncio
async def test_bootstrap_is_model_read_then_exact_ack_before_a_real_file_change(setup):
    memory = Memory()
    provider = ScriptedProvider(
        reply(call("write_file", {"path": "app.py", "content": "wrong", "expected_sha256": None})),
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        reply(call("write_file", {"path": "app.py", "content": "VALUE=7\n", "expected_sha256": None})),
        reply(text="Zapisano implementację; testy pozostają do wykonania."))
    agent = engine(setup, provider, memory)
    result = await agent.turn("Utwórz app.py")
    assert result["status"] == "completed"
    assert setup[3].joinpath("app.py").read_text() == "VALUE=7\n"
    assert tools_results(agent)[0]["error_code"] == "resume_required"
    assert [name for name, _ in memory.calls] == ["resume_context", "ack_resume"]
    first_tools = {row["name"] for row in provider.requests[0]["tools"]}
    assert "write_file" not in first_tools and "ack_resume" in first_tools
    assert "write_file" in {row["name"] for row in provider.requests[3]["tools"]}
    assert agent.pending_actions() == []
    events = agent.journal.events()
    start = next(row for row in events if row["type"] == "action_started")
    done = next(row for row in events if row["type"] == "action_completed")
    assert start["seq"] < done["seq"]
    assert start["data"]["tool_call_id"] == done["data"]["tool_call_id"]
    await agent.close()


@pytest.mark.asyncio
async def test_prepared_packet_delivery_cannot_authorize_ack_or_effects(setup):
    memory = Memory()
    provider = ScriptedProvider(reply(call("ack_resume", memory.ack_arguments())), reply(text="Gotowe"))
    agent = engine(setup, provider, memory)
    result = await agent.turn("Kontynuuj")
    assert result["status"] == "resume_required"
    assert not memory.acked
    assert not memory.calls  # ACK refused before touching even the injected service.
    assert tools_results(agent)[0]["error_code"] == "resume_required"
    await agent.close()


@pytest.mark.asyncio
async def test_partial_stops_before_generation_and_does_not_synthesize_ack(setup):
    memory = Memory(status="partial")
    provider = ScriptedProvider()
    agent = engine(setup, provider, memory)
    result = await agent.turn("Pracuj")
    assert result["status"] == "partial" and result["unknowns"] == ["Transcript gap"]
    assert provider.requests == [] and memory.calls == []
    assert not agent.messages()
    await agent.close()


@pytest.mark.asyncio
async def test_partial_archive_on_resume_stops_before_any_generation_even_if_prepare_would_be_ready(setup):
    settings, run, _, _ = setup
    journal = AgentJournal(settings, run["session_id"])
    journal.append("message", {"role": "user", "content": "Old conversation"})

    class Incomplete(Memory):
        async def archive(self, journal):
            return {"status": "partial", "unknowns": ["Archived prefix conflicts with private log"]}

    memory, provider = Incomplete(), ScriptedProvider()
    agent = engine(setup, provider, memory, journal=journal)
    result = await agent.turn("Kontynuuj")
    assert result["status"] == "partial" and "conflicts" in result["unknowns"][0]
    assert not provider.requests and memory.prepares == 0
    await agent.close()


@pytest.mark.asyncio
async def test_partial_archive_after_effect_preserves_real_result_but_never_reports_completed(setup):
    class Incomplete(Memory):
        async def archive(self, journal):
            return {"status": "partial", "unknowns": ["Own history not completely archived"]}

    memory = Incomplete()
    provider = ScriptedProvider(reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
                                reply(call("write_file", {"path": "actual.txt", "content": "real effect",
                                                          "expected_sha256": None})), reply(text="Zapisano plik."))
    agent = engine(setup, provider, memory)
    result = await agent.turn("Utwórz plik")
    assert result["status"] == "partial" and result["effects_status"] == "completed"
    assert setup[3].joinpath("actual.txt").read_text() == "real effect"
    assert result["text"] == "Zapisano plik."
    calls = len(provider.requests)
    next_result = await agent.turn("Kontynuuj")
    assert next_result["status"] == "partial" and len(provider.requests) == calls
    assert len([row for row in agent.journal.events() if row["type"] == "action_completed"]) == 1
    await agent.close()


@pytest.mark.asyncio
async def test_partial_archive_prevents_checkpoint_compaction(setup):
    class Incomplete(Memory):
        async def archive(self, journal):
            return {"status": "partial", "unknowns": ["Missing history prefix"]}

    memory = Incomplete()
    provider = ScriptedProvider(reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
                                reply(call("compact_context", {"state": {
                                    "goal": "Continue", "results": [], "next_step": "Inspect"}})))
    agent = engine(setup, provider, memory)
    result = await agent.turn("Kontynuuj")
    assert result["status"] == "partial"
    assert not [name for name, _ in memory.calls if name == "checkpoint"]
    assert not [row for row in agent.journal.events() if row["type"] == "context_compacted"]
    await agent.close()


@pytest.mark.asyncio
async def test_relevant_state_change_closes_effect_gate_until_new_read_ack(setup):
    memory = Memory()

    def changed(messages, tools):
        memory.stale = True
        return reply(call("write_file", {"path": "stale.py", "content": "bad", "expected_sha256": None}))

    provider = ScriptedProvider(reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
                                changed, reply(text="Potrzebuję odświeżyć pamięć."))
    agent = engine(setup, provider, memory)
    result = await agent.turn("Wykonaj")
    assert result["status"] == "resume_required"
    assert not setup[3].joinpath("stale.py").exists()
    assert tools_results(agent)[-1]["error_code"] == "resume_required"
    assert "write_file" not in {row["name"] for row in provider.requests[-1]["tools"]}
    await agent.close()


@pytest.mark.asyncio
async def test_bad_readback_never_authorizes_mutation(setup):
    memory = Memory()
    bad = {**memory.ack_arguments(), "readback": {"plan": {"id": "invented", "version": 1}, "task": None}}
    provider = ScriptedProvider(reply(call("resume_context")), reply(call("ack_resume", bad)), reply(text="Brak ACK"))
    agent = engine(setup, provider, memory)
    result = await agent.turn("Start")
    assert result["status"] == "resume_required"
    assert memory.acked is False
    assert tools_results(agent)[-1]["error_code"] == "resume_required"
    await agent.close()


@pytest.mark.asyncio
async def test_model_budget_is_bounded_and_price_is_unknown_without_rates(setup):
    setup[2]["limits"]["max_model_calls"] = 3
    provider = ScriptedProvider(*[reply(call("resume_context"), usage={"input_tokens": 11, "output_tokens": 2})
                                  for _ in range(3)])
    agent = engine(setup, provider)
    result = await agent.turn("Przypomnij stan")
    assert result["status"] == "limit_reached" and len(provider.requests) == 3
    assert result["usage"]["input_tokens"] == 33
    assert result["usage"]["estimated_cost"] is None
    await agent.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [{}, {"input_tokens": 10}, {"output_tokens": 10}])
async def test_configured_price_never_turns_missing_counters_into_zero_cost(setup, usage):
    setup[2]["pricing"] = {"input_per_million": 10, "output_per_million": 30}
    provider = ScriptedProvider(reply(text="No proof", usage=usage))
    agent = engine(setup, provider)
    result = await agent.turn("Stan")
    assert result["usage"]["estimated_cost"] is None
    assert result["usage"]["usage_complete"] is False
    await agent.close()


@pytest.mark.asyncio
async def test_known_token_counts_with_rates_have_an_explicit_estimate(setup):
    setup[2]["pricing"] = {"input_per_million": 10, "output_per_million": 30}
    provider = ScriptedProvider(reply(text="No proof", usage={"input_tokens": 1000, "output_tokens": 100}))
    agent = engine(setup, provider)
    result = await agent.turn("Stan")
    assert result["usage"]["estimated_cost"] == pytest.approx(0.013)
    assert result["usage"]["cost_currency"] == "USD"
    await agent.close()


@pytest.mark.asyncio
async def test_large_tool_result_is_complete_in_archive_but_explicitly_bounded_for_model(setup):
    content = "A source line that must remain fully recoverable.\n" * 1000
    setup[3].joinpath("large.txt").write_text(content, encoding="utf-8", newline="\n")
    memory = Memory()
    provider = ScriptedProvider(reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
                                reply(call("read_file", {"path": "large.txt"})), reply(text="Odczytano fragment."))
    agent = engine(setup, provider, memory)
    result = await agent.turn("Przeczytaj duży plik")
    assert result["status"] == "completed"
    full = tools_results(agent)[-1]
    assert full["content"] == content
    model_view = json.loads(provider.requests[-1]["messages"][-1]["content"])
    assert model_view["host_context_truncated"] is True
    assert model_view["full_result_archived"] is True
    assert model_view["full_result_source_id"].startswith("source-")
    assert model_view["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert len(model_view["content"]) < len(content)
    assert memory.archives >= 2  # full output archived before the next request, then completed turn.
    await agent.close()


@pytest.mark.asyncio
async def test_authoritative_packet_with_credential_material_is_not_silently_redacted_and_acked(setup):
    memory = Memory()
    memory.packet["records"] = [{"content": "Leaked sk-sensitive-packet"}]
    provider = ScriptedProvider(reply(call("resume_context")))
    agent = engine(setup, provider, memory, key="sk-sensitive-packet")
    result = await agent.turn("Odtwórz")
    assert result["status"] == "partial"
    assert not memory.acked
    observed = tools_results(agent)[0]
    assert observed["error_code"] == "credential_in_packet" and "packet" not in observed
    assert "sk-sensitive-packet" not in agent.journal.path.read_text()
    assert not agent._last_packet
    await agent.close()


@pytest.mark.asyncio
async def test_provider_failure_is_not_retried_and_actual_secret_never_enters_journal(setup):
    class Rejected(ValueError):
        code = "network_unavailable"

    provider = ScriptedProvider(Rejected("HTTP auth sk-private-key-exact"))
    agent = engine(setup, provider, key="sk-private-key-exact")
    result = await agent.turn("Use sk-private-key-exact but don't disclose it")
    assert result["status"] == "error" and result["error_code"] == "network_unavailable"
    assert len(provider.requests) == 1
    assert "sk-private-key-exact" not in agent.journal.path.read_text()
    assert "sk-private-key-exact" not in json.dumps(provider.requests)
    await agent.close()


@pytest.mark.asyncio
async def test_context_rejection_has_an_actionable_hint_and_is_not_retried(setup):
    class ContextLimit(ValueError):
        code = "context_limit"

    provider = ScriptedProvider(ContextLimit("provider-specific body is not a user diagnostic"))
    agent = engine(setup, provider)
    result = await agent.turn("Przywróć")
    assert result["status"] == "context_limit" and result["error_code"] == "context_limit"
    assert "Increase" in result["unknowns"][0] and len(provider.requests) == 1
    assert "provider-specific" not in agent.journal.path.read_text()
    await agent.close()


@pytest.mark.asyncio
async def test_ollama_trained_context_rejection_says_to_decrease_window_and_exposes_only_safe_counts(setup):
    error = ProviderError("context_limit", context_details={
        "configured_context_window": 65536, "model_context_window": 32768})
    # The fixed provider diagnostic remains trustworthy even if future code
    # accidentally attaches an upstream body to an exception attribute.
    error.hint = "Upstream Authorization: sk-should-never-be-shown"
    error.context_details["authorization"] = "sk-should-never-be-shown"
    provider = ScriptedProvider(error)
    agent = engine(setup, provider)
    result = await agent.turn("Uruchom model")
    assert result["status"] == "context_limit" and result["error_code"] == "context_limit"
    assert "Lower it" in result["hint"] and "Increase" not in result["hint"]
    assert result["unknowns"] == [result["hint"]]
    assert result["context_details"] == {"configured_context_window": 65536, "model_context_window": 32768}
    assert "sk-should-never-be-shown" not in json.dumps(result)
    assert "sk-should-never-be-shown" not in agent.journal.path.read_text()
    assert len(provider.requests) == 1
    await agent.close()


@pytest.mark.asyncio
async def test_ollama_unknown_context_reports_metadata_problem_without_invented_window_or_retry(setup):
    provider = ScriptedProvider(ProviderError("context_unknown"))
    agent = engine(setup, provider)
    result = await agent.turn("Uruchom model")
    assert result["status"] == "error" and result["error_code"] == "context_unknown"
    assert "gave no reliable" in result["hint"] and "limit" in result["hint"]
    assert result["context_details"] == {}
    assert result["unknowns"] == [result["hint"]]
    assert len(provider.requests) == 1
    await agent.close()


@pytest.mark.asyncio
async def test_visible_partial_stream_is_durable_on_disconnect_without_a_false_complete_message(setup):
    class Disconnected:
        calls = 0

        async def complete(self, messages, tools, *, on_delta=None):
            self.calls += 1
            await on_delta("Visible answer before connection loss.")
            raise ConnectionError("Disconnected after server accepted the request")

    provider = Disconnected()
    memory = Memory()
    agent = engine(setup, provider, memory)
    result = await agent.turn("Odpowiedz")
    assert result["status"] == "error" and provider.calls == 1
    partial = [event for event in agent.journal.events() if event["type"] == "provider_stream"]
    assert len(partial) == 1
    assert partial[0]["data"]["text"] == "Visible answer before connection loss."
    assert partial[0]["data"]["complete"] is False
    assert not [message for message in agent.messages() if message["role"] == "assistant"]
    assert memory.archives == 1
    await agent.close()


@pytest.mark.asyncio
async def test_one_character_network_deltas_cannot_reconstruct_registered_credentials(setup):
    secret = "sk-stream-secret-123456789"
    emitted = []

    class Fragmented:
        async def complete(self, messages, tools, *, on_delta=None):
            for character in "before " + secret + " after":
                await on_delta(character)
                assert secret not in "".join(emitted)
                assert not "".join(emitted).endswith("sk-stream-secret-")
            return reply(text="before " + secret + " after")

    agent = engine(setup, Fragmented(), key=secret, on_delta=emitted.append)
    result = await agent.turn("Odpowiedz")
    assert result["status"] == "resume_required"
    assert "".join(emitted) == "before [REDACTED] after"
    assert secret not in agent.journal.path.read_text()
    assert result["text"] == "before [REDACTED] after"
    await agent.close()


@pytest.mark.asyncio
async def test_recovery_uses_fsynced_completed_result_without_reexecuting(setup):
    settings, run, _, root = setup
    journal = AgentJournal(settings, run["session_id"])
    tool = call("write_file", {"path": "one.py", "content": "one", "expected_sha256": None}, "completed-call")
    journal.append("message", {"role": "assistant", "content": "", "tool_calls": [tool]})
    journal.append("action_started", {"action_id": "effect-1", "tool_call_id": tool["id"],
                                      "name": "write_file", "arguments": tool["arguments"]})
    root.joinpath("one.py").write_text("one")
    journal.append("action_completed", {"action_id": "effect-1", "tool_call_id": tool["id"],
                                        "name": "write_file", "result": {"ok": True, "path": "one.py"}})
    before = root.joinpath("one.py").stat().st_mtime_ns
    provider = ScriptedProvider()
    agent = engine(setup, provider, journal=journal)
    assert agent.pending_actions() == []
    assert tools_results(agent) == [{"ok": True, "path": "one.py"}]
    assert root.joinpath("one.py").stat().st_mtime_ns == before
    assert not provider.requests
    await agent.close()


@pytest.mark.asyncio
async def test_uncertain_action_blocks_model_work_until_explicit_human_resolution(setup):
    settings, run, _, _ = setup
    journal = AgentJournal(settings, run["session_id"])
    journal.append("action_started", {"action_id": "uncertain", "name": "run_command", "command": "deploy"})
    provider = ScriptedProvider()
    agent = engine(setup, provider, journal=journal)
    result = await agent.turn("Kontynuuj")
    assert result["status"] == "uncertain" and not provider.requests
    assert agent.pending_actions()[0]["action_id"] == "uncertain"
    with pytest.raises(ValueError, match="nonempty"):
        agent.resolve_uncertain("uncertain", " ")
    resolved = agent.resolve_uncertain("uncertain", "Sprawdziłem wynik ręcznie; nie uruchamiaj ponownie.")
    assert resolved == {"resolved": True, "action_id": "uncertain", "replayed": False}
    assert not agent.pending_actions() and not provider.requests
    await agent.close()


@pytest.mark.asyncio
async def test_duplicate_call_id_does_not_reexecute_an_effect(setup):
    memory = Memory()
    create = call("write_file", {"path": "once.txt", "content": "one", "expected_sha256": None}, "one-call")
    provider = ScriptedProvider(reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
                                reply(create), reply(create))
    agent = engine(setup, provider, memory)
    result = await agent.turn("Utwórz raz")
    assert result["status"] == "error"
    assert setup[3].joinpath("once.txt").read_text() == "one"
    completed = [row for row in agent.journal.events() if row["type"] == "action_completed"]
    assert len(completed) == 1
    await agent.close()


@pytest.mark.asyncio
async def test_explicit_interrupt_cancels_current_generation_without_retry(setup):
    entered = asyncio.Event()

    async def waiting(messages, tools):
        entered.set()
        await asyncio.Event().wait()

    provider = ScriptedProvider(waiting)
    agent = engine(setup, provider)
    task = asyncio.create_task(agent.turn("Poczekaj"))
    await entered.wait()
    assert (await agent.turn("Drugie"))["status"] == "busy"
    await agent.interrupt()
    result = await task
    assert result["status"] == "interrupted"
    assert len(provider.requests) == 1
    await agent.close()
    assert provider.closed


@pytest.mark.asyncio
async def test_context_compaction_requires_checkpoint_then_new_model_ack(setup, monkeypatch):
    monkeypatch.setattr("cairnmind.agents.api_agent.CONTEXT_BUDGET_CHARS", 200)
    memory = Memory()
    compact = {"state": {"goal": "Continue", "results": ["Read saved sources"], "next_step": "Review"}}
    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        reply(call("compact_context", compact)), reply(call("resume_context")),
        reply(call("ack_resume", memory.ack_arguments())), reply(text="Odtworzono kontekst."))
    agent = engine(setup, provider, memory)
    result = await agent.turn("A fairly long current request " * 20)
    # A tiny artificial threshold remains exceeded even by the checkpoint;
    # its final response must honestly report context_limit, not discard it.
    assert result["status"] == "context_limit"
    assert sum(name == "checkpoint" for name, _ in memory.calls) == 1
    assert sum(name == "ack_resume" for name, _ in memory.calls) == 2
    assert any(row["type"] == "context_compacted" for row in agent.journal.events())
    post = provider.requests[3]["messages"]
    assert "Context renewed" in post[1]["content"]
    assert not any("tool_calls" in message for message in post[1:])
    await agent.close()


@pytest.mark.asyncio
async def test_copilot_uses_same_model_ack_gate_and_visible_tool_history(setup):
    settings, run, profile, _ = setup
    run["client"] = profile["client"] = "copilot"
    memory = Memory()


    class Copilot:
        async def run_turn(self, prompt, tools, handler, *, system="", on_delta=None, on_event=None):
            blocked = await handler("write_file", {"path": "cp.txt", "content": "bad", "expected_sha256": None})
            assert blocked["error_code"] == "resume_required"
            await handler("resume_context", {})
            await handler("ack_resume", memory.ack_arguments())
            written = await handler("write_file", {"path": "cp.txt", "content": "ok", "expected_sha256": None})
            assert written["ok"]
            return {"text": "Zapisano", "usage": {"input_tokens": 5, "output_tokens": 2}}

    agent = AgentEngine(settings, run, profile, provider=Copilot(), memory=memory)
    result = await agent.turn("Wykonaj w Copilocie")
    assert result["status"] == "completed"
    assert result["model_calls"] is None  # SDK internal request count is not observable.
    assert setup[3].joinpath("cp.txt").read_text() == "ok"
    assert len(tools_results(agent)) == 4
    assert [name for name, _ in memory.calls] == ["resume_context", "ack_resume"]
    await agent.close()


@pytest.mark.asyncio
async def test_copilot_context_reset_preserves_checkpoint_and_demands_new_actual_ack(setup):
    settings, run, profile, root = setup
    run["client"] = profile["client"] = "copilot"
    memory = Memory()


    class Copilot:
        turns = 0
        resets = 0

        async def run_turn(self, prompt, tools, handler, *, system="", on_delta=None, on_event=None):
            self.turns += 1
            await handler("resume_context", {})
            await handler("ack_resume", memory.ack_arguments())
            if self.turns == 1:
                saved = await handler("compact_context", {"state": {
                    "goal": "Finish", "results": ["Checkpoint saved"], "next_step": "Create file"}})
                assert saved["context_reset_pending"]
                blocked = await handler("write_file", {"path": "cp-reset.txt", "content": "wrong",
                                                       "expected_sha256": None})
                assert blocked["error_code"] == "context_reset_required"
            else:
                assert self.resets == 1
                await handler("write_file", {"path": "cp-reset.txt", "content": "right",
                                             "expected_sha256": None})
            return {"text": "Checkpoint" if self.turns == 1 else "Gotowe", "usage": {}}

        async def reset_context(self):
            self.resets += 1

    provider = Copilot()
    agent = AgentEngine(settings, run, profile, provider=provider, memory=memory)
    result = await agent.turn("Wykonaj")
    assert result["status"] == "completed"
    assert root.joinpath("cp-reset.txt").read_text() == "right"
    assert sum(name == "ack_resume" for name, _ in memory.calls) == 2
    assert provider.resets == 1 and provider.turns == 2
    assert any(event["type"] == "context_compacted" for event in agent.journal.events())
    await agent.close()


@pytest.mark.asyncio
async def test_parallel_copilot_tools_have_serial_approval_and_retired_callbacks_are_rejected(setup):
    settings, run, profile, _ = setup
    run["client"] = profile["client"] = "copilot"
    memory = Memory()
    approval_active = False
    approved = []

    async def approve(request):
        nonlocal approval_active
        assert not approval_active, "Two approvals must never overlap"
        approval_active = True
        await asyncio.sleep(.01)
        approved.append(request["command"])
        approval_active = False
        return True

    import sys

    class Copilot:
        saved_handler = None

        async def run_turn(self, prompt, tools, handler, *, system="", on_delta=None, on_event=None):
            self.saved_handler = handler
            await handler("resume_context", {})
            await handler("ack_resume", memory.ack_arguments())
            cmd_first = "[Console]::Write('first')" if sys.platform == "win32" else "printf first"
            cmd_second = "[Console]::Write('second')" if sys.platform == "win32" else "printf second"
            first, second = await asyncio.gather(handler("run_command", {"command": cmd_first}),
                                                 handler("run_command", {"command": cmd_second}))
            assert first["stdout"] == "first" and second["stdout"] == "second"
            return {"text": "Oba wyniki zapisane.", "usage": {}}

    provider = Copilot()
    agent = AgentEngine(settings, run, profile, provider=provider, memory=memory, approve=approve)
    result = await agent.turn("Wykonaj dwa polecenia")
    assert result["status"] == "completed"
    expected_cmds = (["[Console]::Write('first')", "[Console]::Write('second')"]
                     if sys.platform == "win32" else ["printf first", "printf second"])
    assert approved == expected_cmds
    stale = await provider.saved_handler("run_command", {"command": "printf forbidden"})
    assert stale["error_code"] == "turn_interrupted" and len(approved) == 2
    await agent.close()


@pytest.mark.asyncio
async def test_state_change_while_confirmation_is_open_requires_new_ack_before_command(setup):
    memory = Memory()

    async def approve(request):
        memory.stale = True
        return True

    provider = ScriptedProvider(reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
                                reply(call("run_command", {"command": "printf bad > not-run.txt"})),
                                reply(text="Stan zmienił się podczas potwierdzenia."))
    agent = engine(setup, provider, memory, approve=approve)
    result = await agent.turn("Uruchom")
    assert result["status"] == "resume_required"
    assert not setup[3].joinpath("not-run.txt").exists()
    assert any(row["type"] == "approval_stale" for row in agent.journal.events())
    assert not [row for row in agent.journal.events() if row["type"] == "action_started"]
    await agent.close()


@pytest.mark.asyncio
async def test_copilot_incremental_usage_and_final_aggregate_are_not_double_counted(setup):
    settings, run, profile, _ = setup
    run["client"] = profile["client"] = "copilot"
    memory = Memory()


    class Copilot:
        async def run_turn(self, prompt, tools, handler, *, system="", on_delta=None, on_event=None):
            await handler("resume_context", {})
            await handler("ack_resume", memory.ack_arguments())
            await on_event({"type": "usage", "usage": {"input_tokens": 20, "output_tokens": 4, "cost": .025}})
            await on_event({"type": "usage", "usage": {"input_tokens": 30, "output_tokens": 6, "cost": .1}})
            return {"text": "Odtworzono", "usage": {"input_tokens": 50, "output_tokens": 10, "cost": .125}}

    agent = AgentEngine(settings, run, profile, provider=Copilot(), memory=memory)
    result = await agent.turn("Stan")
    assert result["status"] == "completed"
    assert result["usage"]["input_tokens"] == 50 and result["usage"]["output_tokens"] == 10
    assert result["usage"]["provider_observations"]["cost"] == .125
    assert result["usage"]["estimated_cost"] is None and "cost_currency" not in result["usage"]
    assert len([event for event in agent.journal.events() if event["type"] == "usage"]) == 1
    await agent.close()


@pytest.mark.asyncio
async def test_production_memory_transport_and_model_orchestration_on_real_arcade(tmp_path):
    root = tmp_path / "git-project"
    root.mkdir()
    for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                 ("config", "user.email", "fixture@example.invalid")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    root.joinpath("app.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
    store = Store(tmp_path / "data")
    project = store.add_project("real-api-agent", root)
    sid = uuid4().hex
    run = {"id": uuid4().hex, "generation": 1, "project_id": project["id"],
           "worktree": str(root), "session_id": sid, "client": "api"}
    memory = AgentMemoryClient(store.settings, project["id"], root, sid,
                               transport=httpx.ASGITransport(app=create_app(store.settings, start_worker=False)))

    def ack_from_actual_tool_result(messages, tools):
        assert not store.query("SELECT FROM resume_acks")
        packet = json.loads(messages[-1]["content"])["packet"]
        assert packet["status"] == "ready"
        return reply(call("ack_resume", {"packet_id": packet["id"], "packet_hash": packet["hash"],
                                           "readback": {"plan": None, "task": None}}))

    digest = hashlib.sha256(b"VALUE = 1\n").hexdigest()
    provider = ScriptedProvider(reply(call("resume_context")), ack_from_actual_tool_result,
                                reply(call("apply_patch", {"path": "app.py", "expected_sha256": digest,
                                                            "old_text": "VALUE = 1", "new_text": "VALUE = 2"})),
                                reply(text="Zmieniono kod. Świeże testy pozostają do wykonania."))
    profile = {"id": "local", "client": "api", "provider": "ollama", "model": "fixture", "limits": {}}
    agent = AgentEngine(store.settings, run, profile, provider=provider, memory=memory)
    result = await agent.turn("Zmień wartość na dwa")
    assert result["status"] == "completed", result
    assert root.joinpath("app.py").read_text() == "VALUE = 2\n"
    acks = store.query("SELECT FROM resume_acks")
    assert len(acks) == 1 and acks[0]["session_id"] == sid
    sources = store.query("SELECT FROM sources WHERE kind='agent_transcript'")
    assert any("Zmień wartość na dwa" in row["content"] for row in sources)
    assert any("VALUE = 2" in row["content"] for row in sources)
    assert not store.query("SELECT FROM check_runs")
    await agent.close()


@pytest.mark.asyncio
async def test_reading_the_same_packet_again_keeps_its_ack(setup):
    # #157: the pane said resume_required while the service, the header and Sessions still had the ACK.
    memory = Memory()
    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        reply(call("resume_context")), reply(text="Nothing more to do."))
    agent = engine(setup, provider, memory)
    result = await agent.turn("Check the plan")
    assert result["status"] == "completed" and memory.acked
    assert "write_file" in {row["name"] for row in provider.requests[-1]["tools"]}


@pytest.mark.asyncio
async def test_a_changed_packet_still_needs_a_new_ack(setup):
    memory = Memory()

    def changed(messages, tools):
        memory.packet = {**memory.packet, "id": "packet-2", "hash": "b" * 64}
        return reply(call("resume_context"))

    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        changed, reply(text="Stopping here."))
    agent = engine(setup, provider, memory)
    result = await agent.turn("Check the plan")
    assert result["status"] == "resume_required"
    assert "write_file" not in {row["name"] for row in provider.requests[-1]["tools"]}


@pytest.mark.asyncio
async def test_a_kept_ack_ends_when_the_same_packet_is_refused_for_credential_material(setup):
    # #157 review: the kept ACK outlived credential_in_packet, and the next turn stopped as partial at once.
    memory = Memory()

    def leaked(messages, tools):
        memory.packet = {**memory.packet, "records": [{"content": "Leaked sk-sensitive-packet"}]}
        return reply(call("resume_context"))

    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        leaked, reply(text="Read the packet again."))
    agent = engine(setup, provider, memory, key="sk-sensitive-packet")
    first = await agent.turn("Check the plan")
    assert first["status"] == "partial" and tools_results(agent)[-1]["error_code"] == "credential_in_packet"
    assert agent._acknowledged is False
    asked = len(provider.requests)
    second = await agent.turn("Continue")
    assert len(provider.requests) == asked + 1  # the model is asked again instead of a stop before it
    assert "write_file" not in {row["name"] for row in provider.requests[-1]["tools"]}
    assert second["status"] == "resume_required"


@pytest.mark.asyncio
async def test_a_refused_confirmation_reaches_the_model_with_what_to_do_on_real_arcade(tmp_path):
    # #240: the store's "only a person confirms" refusals reached the model as a bare tool_failed, or as a
    # state_conflict that sends it to read and retry; a key that exists came back as tool_failed.
    from conftest import as_person

    from cairnmind.memory.confirmation import REFUSED
    root = tmp_path / "project"
    root.mkdir()
    for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                 ("config", "user.email", "fixture@example.invalid")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    root.joinpath("app.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
    store = Store(tmp_path / "data")
    project = store.add_project("refusals", root)
    source = store.snapshot(project["id"], "doc://notes", "The engine is ArcadeDB.")["id"]
    confirmed = as_person(store, project["id"], {"kind": "fact", "key": "engine", "content": "ArcadeDB"}, [source])
    store.remember(project["id"], {"kind": "fact", "key": "proposed-one", "content": "One"}, [source])
    sid = uuid4().hex
    run = {"id": uuid4().hex, "generation": 1, "project_id": project["id"],
           "worktree": str(root), "session_id": sid, "client": "api"}
    memory = AgentMemoryClient(store.settings, project["id"], root, sid,
                               transport=httpx.ASGITransport(app=create_app(store.settings, start_worker=False)))

    def ack(messages, tools):
        packet = json.loads(messages[-1]["content"])["packet"]
        return reply(call("ack_resume", {"packet_id": packet["id"], "packet_hash": packet["hash"],
                                           "readback": {"plan": None, "task": None}}))

    remember = [
        {"record": {"kind": "fact", "key": "new-one", "content": "Two", "status": "confirmed"}, "sources": [source]},
        {"record": {"id": confirmed["id"], "expected_version": confirmed["version"], "kind": "fact",
                    "key": "engine", "content": "PostgreSQL"}, "sources": [source]},
        {"record": {"kind": "fact", "key": "proposed-one", "content": "Changed"}, "sources": [source]},
    ]
    provider = ScriptedProvider(reply(call("resume_context")), ack,
                                reply(*(call("remember", arguments) for arguments in remember)),
                                reply(text="Stored nothing."))
    profile = {"id": "local", "client": "api", "provider": "ollama", "model": "fixture", "limits": {}}
    agent = AgentEngine(store.settings, run, profile, provider=provider, memory=memory)
    try:
        result = await agent.turn("Remember these")
        assert len(tools_results(agent)) == 5, result
        refused, kept, exists = tools_results(agent)[-3:]
        assert refused["error_code"] == "confirmation_refused" and refused["message"] == REFUSED
        assert kept["error_code"] == "confirmation_refused"
        assert kept["message"].startswith(f"A person confirmed record {confirmed['id']} (fact)")
        assert "Nothing was saved" in kept["message"] and "possible_conflicts" in kept["message"]
        assert exists["error_code"] == "key_exists"
        assert "record.id and record.expected_version" in exists["message"]
        assert "inside the record argument" in exists["message"] and "Changed" not in exists["message"]
        assert not store.query("SELECT FROM records WHERE `key`='new-one'")
        assert store.query("SELECT FROM records WHERE id=:id", {"id": confirmed["id"]})[0]["content"] == "ArcadeDB"
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_invalid_tool_arguments_say_which_argument_and_why_without_its_value(setup):
    # #240: write_file with expected_sha256 "" came back as a bare invalid_tool_arguments.
    memory = Memory()
    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        reply(call("write_file", {"path": "app.py", "content": "VALUE=1\n", "expected_sha256": "not-a-hash"}),
              call("write_file", {"path": "app.py", "content": "VALUE=1\n"})),
        reply(text="Done."))
    agent = engine(setup, provider, memory)
    try:
        await agent.turn("Create app.py")
        bad_hash, missing = tools_results(agent)[-2:]
        assert bad_hash["error_code"] == missing["error_code"] == "invalid_tool_arguments"
        assert bad_hash["argument"] == "expected_sha256" and "not-a-hash" not in json.dumps(bad_hash)
        assert "null" in bad_hash["hint"] and "read_file" in bad_hash["hint"]
        assert missing["keyword"] == "required" and "expected_sha256" in missing["hint"]
        assert not setup[3].joinpath("app.py").exists()
    finally:
        await agent.close()


def test_the_instructions_say_effect_tools_appear_only_after_the_ack():
    # #240: a small model asked to write a file said it had no write tool and never acknowledged.
    from cairnmind.agents.api_agent import SYSTEM
    assert "offered only after" in SYSTEM and "ack_resume" in SYSTEM


@pytest.mark.asyncio
async def test_a_ready_packet_before_its_ack_names_the_next_step(setup):
    memory = Memory()
    provider = ScriptedProvider(reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
                                reply(call("resume_context")), reply(text="Done."))
    agent = engine(setup, provider, memory)
    try:
        await agent.turn("Check")
        first, _, again = tools_results(agent)
        assert "ack_resume" in first["next_step"] and "next_step" not in again
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_a_wrong_record_field_and_an_unknown_source_reach_the_model_with_what_to_do_on_real_arcade(tmp_path):
    # #240, second trial: remember with record.text and read_source with a made-up id, a session id or an id
    # of a bad shape reached the model as a bare tool_failed.
    root = tmp_path / "project"
    root.mkdir()
    for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                 ("config", "user.email", "fixture@example.invalid")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    root.joinpath("app.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
    store = Store(tmp_path / "data")
    project = store.add_project("unknowns", root)
    source = store.snapshot(project["id"], "doc://notes", "The engine is ArcadeDB.")["id"]
    sid = uuid4().hex
    run = {"id": uuid4().hex, "generation": 1, "project_id": project["id"],
           "worktree": str(root), "session_id": sid, "client": "api"}
    memory = AgentMemoryClient(store.settings, project["id"], root, sid,
                               transport=httpx.ASGITransport(app=create_app(store.settings, start_worker=False)))

    def ack(messages, tools):
        packet = json.loads(messages[-1]["content"])["packet"]
        return reply(call("ack_resume", {"packet_id": packet["id"], "packet_hash": packet["hash"],
                                           "readback": {"plan": None, "task": None}}))

    provider = ScriptedProvider(
        reply(call("resume_context")), ack,
        reply(call("remember", {"record": {"kind": "fact", "text": "Two"}, "sources": [source]}),
              call("read_source", {"source_id": "made-up-source"}), call("read_source", {"source_id": sid}),
              call("read_source", {"source_id": "../etc/passwd"}), call("read_source", {"source_id": source})),
        reply(text="Done."))
    profile = {"id": "local", "client": "api", "provider": "ollama", "model": "fixture", "limits": {}}
    agent = AgentEngine(store.settings, run, profile, provider=provider, memory=memory)
    try:
        result = await agent.turn("Remember and read")
        assert len(tools_results(agent)) == 7, result
        record, made_up, session, bad_shape, found = tools_results(agent)[-5:]
        assert record["error_code"] == "invalid_record"
        assert "'text' is not a record field" in record["message"] and "Nothing was saved" in record["message"]
        for refused in (made_up, session, bad_shape):
            assert refused["error_code"] == "unknown_source", refused
            assert "memory_records" in refused["message"] and "passwd" not in refused["message"]
        assert "error_code" not in found and "ArcadeDB" in json.dumps(found)
        assert not store.query("SELECT FROM records WHERE content='Two'")
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_a_reread_packet_replaces_the_earlier_ones_in_the_model_context(setup):
    # #240, second trial: every resume_context read appended another full packet, until an Ollama
    # request no longer fitted its window.
    memory = Memory()
    memory.packet["records"] = [{"content": "R" * 12_000}]
    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        reply(call("resume_context")), reply(call("resume_context")), reply(text="Done."))
    agent = engine(setup, provider, memory)
    try:
        assert (await agent.turn("Check"))["status"] == "completed"
        packets = [json.loads(row["content"]) for row in provider.requests[-1]["messages"]
                   if row.get("role") == "tool" and row.get("name") == "resume_context"]
        assert len(packets) == 3
        assert [("R" * 12_000) in json.dumps(packet) for packet in packets] == [False, False, True]
        assert packets[0]["packet_id"] == "packet-1" and packets[0]["replaced_by_a_later_read"] is True
        # The journal and the archive keep every read whole.
        assert sum(("R" * 12_000) in row.get("content", "") for row in agent.messages()) == 3
    finally:
        await agent.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("window,notice", [(32768, True), (131072, False)])
async def test_the_compaction_notice_comes_before_the_ollama_window_refuses(setup, window, notice):
    # #240, second trial: on the default 32k Ollama window a turn ended in context_limit, because the
    # notice waited for 80,000 characters while the provider's own check refused at about 50,000.
    from cairnmind.agents.api_providers import _ollama_messages, ollama_prompt_estimate
    settings, run, profile, _ = setup
    profile = {**profile, "limits": {"context_window_tokens": window, "max_output_tokens": 4096}}
    provider = ScriptedProvider(reply(text="Need the packet first."))
    agent = AgentEngine(settings, run, profile, provider=provider, memory=Memory())
    try:
        await agent.turn("x" * 40_000)
        request = provider.requests[0]
        names = {tool["name"] for tool in request["tools"]}
        assert ("compact_context" in names) is notice
        assert any("HOST CONTEXT BUDGET" in str(row.get("content")) for row in request["messages"]) is notice
        if notice:
            # The notice still fits: the provider's own pre-check would send this request.
            tools = [{"type": "function", "function": tool} for tool in request["tools"]]
            assert ollama_prompt_estimate(_ollama_messages(request["messages"]), tools) + 4096 <= window
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_a_record_the_store_does_not_save_reaches_the_model_with_what_to_do_on_real_arcade(tmp_path):
    # #240, review: an unknown kind, a missing content or metadata that is not an object reached the model as
    # a bare tool_failed; only unknown field names were refused with a reason.
    root = tmp_path / "project"
    root.mkdir()
    for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                 ("config", "user.email", "fixture@example.invalid")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    root.joinpath("app.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
    store = Store(tmp_path / "data")
    project = store.add_project("shapes", root)
    source = store.snapshot(project["id"], "doc://notes", "The engine is ArcadeDB.")["id"]
    sid = uuid4().hex
    run = {"id": uuid4().hex, "generation": 1, "project_id": project["id"],
           "worktree": str(root), "session_id": sid, "client": "api"}
    memory = AgentMemoryClient(store.settings, project["id"], root, sid,
                               transport=httpx.ASGITransport(app=create_app(store.settings, start_worker=False)))

    def ack(messages, tools):
        packet = json.loads(messages[-1]["content"])["packet"]
        return reply(call("ack_resume", {"packet_id": packet["id"], "packet_hash": packet["hash"],
                                           "readback": {"plan": None, "task": None}}))

    provider = ScriptedProvider(
        reply(call("resume_context")), ack,
        reply(call("remember", {"record": {"kind": "note", "content": "Kind"}, "sources": [source]}),
              call("remember", {"record": {"kind": "fact"}, "sources": [source]}),
              call("remember", {"record": {"kind": "fact", "content": "Meta", "metadata": "x"},
                                "sources": [source]})),
        reply(text="Done."))
    profile = {"id": "local", "client": "api", "provider": "ollama", "model": "fixture", "limits": {}}
    agent = AgentEngine(store.settings, run, profile, provider=provider, memory=memory)
    try:
        await agent.turn("Remember")
        kind, content, metadata = tools_results(agent)[-3:]
        for refused, sentence in ((kind, "Unknown record kind or status"),
                                  (content, "Record content must be nonempty text"),
                                  (metadata, "Record metadata must be an object")):
            assert refused["error_code"] == "invalid_record", refused
            assert refused["message"].startswith(sentence) and "Nothing was saved" in refused["message"]
        assert not store.query("SELECT FROM records WHERE content='Kind' OR content='Meta'")
    finally:
        await agent.close()


def _pad_catalogue(agent, target):
    """Add tool schemas until the system prompt, the whole catalogue and the output room alone are
    estimated at about `target` tokens, as a large MCP catalogue would be."""
    from cairnmind.agents.api_agent import SYSTEM
    from cairnmind.agents.api_providers import _ollama_messages, ollama_prompt_estimate

    def fixed():
        tools = [{"type": "function", "function": tool} for tool in agent._schemas.values()]
        return ollama_prompt_estimate(_ollama_messages([{"role": "system", "content": SYSTEM}]), tools) + 4096

    number = 0
    while fixed() < target:
        number += 1
        agent._schemas[f"pad_{number}"] = {"name": f"pad_{number}", "description": "d" * 400,
                                           "parameters": {"type": "object", "properties": {}}}
    return fixed()


def _fits_the_window(request, window=32768):
    from cairnmind.agents.api_providers import _ollama_messages, ollama_prompt_estimate
    tools = [{"type": "function", "function": tool} for tool in request["tools"]]
    return ollama_prompt_estimate(_ollama_messages(request["messages"]), tools) + 4096 <= window


@pytest.mark.asyncio
async def test_a_large_tool_catalogue_alone_never_withdraws_the_effect_tools(setup):
    # #240, review: the 80% trigger counted the tool catalogue, which a compaction cannot shrink. A catalogue
    # past the share set the notice on every call after the ACK, though the provider would send the request.
    memory = Memory()
    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())), reply(text="Done."))
    agent = engine(setup, provider, memory)
    try:
        _pad_catalogue(agent, int(32768 * 0.85))
        result = await agent.turn("Check")
        assert result["status"] == "completed", result
        last = provider.requests[-1]
        assert "write_file" in {tool["name"] for tool in last["tools"]}
        assert not any("HOST CONTEXT BUDGET" in str(row.get("content")) for row in last["messages"])
        assert _fits_the_window(last)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_a_renewed_context_that_a_compaction_cannot_shorten_gets_no_second_notice(setup):
    # #240, review: with the catalogue just under the share, the context a compaction keeps (request, packet,
    # ACK) put the request past it again, so each compaction bought one tool batch at most.
    memory = Memory()
    compact = {"state": {"goal": "Continue", "results": ["Read"], "next_step": "Write"}}
    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        reply(call("compact_context", compact)), reply(call("resume_context")),
        reply(call("ack_resume", memory.ack_arguments())),
        reply(call("write_file", {"path": "app.py", "content": "VALUE=7\n", "expected_sha256": None})),
        reply(text="Done."))
    agent = engine(setup, provider, memory)
    try:
        _pad_catalogue(agent, int(32768 * 0.8) - 1500)
        result = await agent.turn("r" * 12_000)
        assert result["status"] == "completed", result
        assert sum(name == "checkpoint" for name, _ in memory.calls) == 1
        assert (setup[3] / "app.py").read_text() == "VALUE=7\n"
        assert sum(row["type"] == "context_notice" for row in agent.journal.events()) == 1
        # Every request still fitted the provider's own check of the whole window.
        assert all(_fits_the_window(request) for request in provider.requests)
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_a_failed_reread_keeps_the_acknowledged_packet_in_the_model_context(setup):
    # #240, review: a reread that failed stubbed out the only copy of the packet the run had acknowledged.
    class FlakyMemory(Memory):
        async def execute(self, name, arguments):
            if name == "resume_context" and any(called == "resume_context" for called, _ in self.calls):
                self.calls.append((name, deepcopy(arguments)))
                raise RuntimeError("service unavailable")
            return await super().execute(name, arguments)

    memory = FlakyMemory()
    memory.packet["records"] = [{"content": "R" * 2_000}]
    provider = ScriptedProvider(
        reply(call("resume_context")), reply(call("ack_resume", memory.ack_arguments())),
        reply(call("resume_context")), reply(text="Done."))
    agent = engine(setup, provider, memory)
    try:
        assert (await agent.turn("Check"))["status"] == "completed"
        rows = [json.loads(row["content"]) for row in provider.requests[-1]["messages"]
                if row.get("role") == "tool" and row.get("name") == "resume_context"]
        assert rows[1]["error_code"] == "tool_failed"
        assert rows[0]["packet"]["hash"] == "a" * 64 and "R" * 2_000 in json.dumps(rows[0])
    finally:
        await agent.close()
