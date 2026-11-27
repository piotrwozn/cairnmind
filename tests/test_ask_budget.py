"""The time budget of a question on a slow model server (issue #231, umbrella #105).

On an Apple M3 Pro with an MLX build of the default model one discovery call took about 43 seconds;
a complete answer needs that call, the support check and, when citations fail, a repair. The model
server here is a fake that takes the time each call took there on a simulated clock: the agent's
module clock and the event loop's clock (which ends the budget) are the same, advanced only by the
fake, so the speed of the machine running the tests does not matter. This file names default budgets,
so `cairnmind eval repository` never indexes it (NOT_INDEXED in repository_evaluation.py).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from test_knowledge import FakeOllama, calls, document, final, workspace  # noqa: F401  (workspace is a fixture)
from test_second_look import ANSWER, QUESTION, UNKNOWN, long_chunk

from cairnmind.agents import agent as agent_module
from cairnmind import config
from cairnmind.agents.agent import KnowledgeAgent
from cairnmind.config import Settings
from cairnmind.agents.support import NAMES_SYSTEM, SUPPORT_SYSTEM

# Discovery measured on the Mac (10,677 prompt tokens); repair and support are smaller prompts,
# estimated from their size, not measured.
MAC_SECONDS = {"discovery": 43.0, "repair": 20.0, "support": 10.0}
KILN = "The pottery kiln gets 384 g of glaze per firing."


class Clock:
    """Simulated seconds. Only ``pass_time`` moves it: retrieval and the store take none."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def pass_time(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)  # lets the loop see a budget that ran out


@pytest.fixture
async def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(monotonic=clock))  # the module's own clock
    monkeypatch.setattr(asyncio.get_running_loop(), "time", clock)  # asyncio.timeout, the budget, reads this
    return clock


class SlowModel(FakeOllama):
    """FakeOllama that takes ``seconds[phase]`` of simulated time for each call, as a slow local server does."""

    def __init__(self, replies, seconds, clock):
        super().__init__(replies)
        self.seconds, self.clock = seconds, clock

    async def chat(self, messages, tools=None, **kwargs):
        phase = ("support" if messages and messages[0].get("content") in (SUPPORT_SYSTEM, NAMES_SYSTEM)
                 else "repair" if kwargs.get("format") is not None else "discovery")
        await self.clock.pass_time(self.seconds[phase])
        return await super().chat(messages, tools, **kwargs)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
async def test_a_repaired_answer_at_the_speed_of_the_mac_fits_the_default_budget_of_macos(
        workspace, tmp_path, monkeypatch, clock, platform):  # noqa: F811
    store, project, settings = workspace
    with monkeypatch.context() as patch:  # only for loading: sys.platform is the whole process's
        patch.setattr(config.sys, "platform", platform)
        default = Settings.load(tmp_path / "fresh").ask_timeout  # a new installation: no config.json yet
    assert default == {"darwin": 120.0, "linux": 60.0}[platform]
    settings.ask_timeout = default
    source, _ = document(store, project, KILN, uri="notes://kiln")
    model = SlowModel([final(source["id"], "an invented quote", "384 g."), final(source["id"], KILN, "384 g.")],
                      MAC_SECONDS, clock)
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], QUESTION)
    diagnostics = result["diagnostics"]
    if platform == "linux":  # the budget of Linux, unchanged, is too short at this speed: the reason for #231
        assert result["status"] == "partial" and diagnostics["repair_outcome"] == "budget_exhausted"
        return
    assert result["status"] == "complete", json.dumps(diagnostics)
    assert diagnostics["repair_outcome"] == "succeeded" and diagnostics["support_check"] == "supported"
    assert result["sources"][0]["quote"] == KILN  # citations are verified as on any other answer


def test_a_budget_in_config_json_is_kept_on_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "darwin")
    (tmp_path / "config.json").write_text(json.dumps({"ask_timeout": 45.0}))
    assert Settings.load(tmp_path).ask_timeout == 45.0


async def test_no_second_look_starts_with_less_time_left_than_the_first_pass_took(
        workspace, monkeypatch, clock):  # noqa: F811
    store, project, settings = workspace
    document(store, project, long_chunk(), uri="notes://kiln")
    monkeypatch.setattr(agent_module, "SECOND_LOOK_MIN_SECONDS", 0.0)
    settings.ask_timeout = 2.0
    # The first pass (an answer and its repair) takes 70% of the budget: a second pass of the same calls cannot finish.
    model = SlowModel([UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN], {"discovery": 0.7, "repair": 0.7, "support": 0.0}, clock)
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], QUESTION)
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "missing_evidence"
    assert "second_look" not in result["diagnostics"] and len(model.calls) == 2
    # simulated: 0.7 s + 0.7 s of a 2.0 s budget leave 0.6 s, less than the 1.4 s
    # the first pass took from its last discovery call (the answer and its repair)
    assert result["diagnostics"]["budget"]["elapsed_seconds"] == pytest.approx(1.4), f"clock at {clock.now} s"


class StepModel(FakeOllama):
    """FakeOllama whose n-th chat call takes ``delays[n]`` simulated seconds."""

    def __init__(self, replies, delays, clock):
        super().__init__(replies)
        self.delays, self.clock = list(delays), clock

    async def chat(self, messages, tools=None, **kwargs):
        await self.clock.pass_time(self.delays.pop(0) if self.delays else 0.0)
        return await super().chat(messages, tools, **kwargs)


async def test_slow_tool_steps_of_the_first_pass_do_not_hold_back_a_second_look(
        workspace, monkeypatch, clock):  # noqa: F811
    store, project, settings = workspace
    source, _ = document(store, project, long_chunk(), uri="notes://kiln")
    monkeypatch.setattr(agent_module, "SECOND_LOOK_MIN_SECONDS", 0.0)
    settings.ask_timeout = 2.0
    # A long search step (55% of the budget), then an answer and its repair (10%): what is left is less than
    # the first pass took, but more than the calls a second look repeats (the second look makes no tool steps).
    model = StepModel([calls(("search", {"query": "kiln glaze"})), UNKNOWN, UNKNOWN,
                       final(source["id"], ANSWER, "384 g.")], [1.1, 0.1, 0.1, 0.1], clock)
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], QUESTION)
    # simulated: search 1.1 s, answer 0.1 s, repair 0.1 s of a 2.0 s budget leave 0.7 s; the second look takes 0.1 s
    assert result["status"] == "complete", f"clock at {clock.now} s of 2.0: " + json.dumps(result["diagnostics"])
    assert result["diagnostics"]["second_look"] == "answered" and "_last_call_at" not in result["diagnostics"]


class HangingSecondLook(StepModel):
    """A StepModel whose third call (the second look's first) runs on past the end of the budget it was given."""

    async def chat(self, messages, tools=None, **kwargs):
        if len(self.calls) < 2:
            return await super().chat(messages, tools, **kwargs)
        self.calls.append(messages)
        # Lands exactly on the budget's end, passes no time once more (the loop fires the timeout one step of
        # this task late), then would run 100 s longer: only a budget given to the call stops it.
        await self.clock.pass_time(2.0 - self.clock.now)
        await self.clock.pass_time(0.0)
        await self.clock.pass_time(100.0)
        return await super().chat(messages, tools, **kwargs)


async def test_a_second_look_that_outlasts_the_time_left_is_ended_inside_the_question_budget(
        workspace, monkeypatch, clock):  # noqa: F811
    store, project, settings = workspace
    document(store, project, long_chunk(), uri="notes://kiln")
    monkeypatch.setattr(agent_module, "SECOND_LOOK_MIN_SECONDS", 0.0)
    settings.ask_timeout = 2.0
    # simulated: an answer 0.25 s and its repair 0.25 s leave 1.5 s of 2.0, more than the 0.5 s they took, so a
    # second look starts; its call would take 100 s and is ended with the question's budget, at 2.0 s.
    model = HangingSecondLook([UNKNOWN, UNKNOWN, UNKNOWN], [0.25, 0.25], clock)
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], QUESTION)
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "missing_evidence"
    assert result["diagnostics"]["second_look"] == "no_answer" and len(model.calls) == 3
    assert clock.now <= settings.ask_timeout, f"clock at {clock.now} s of 2.0"
