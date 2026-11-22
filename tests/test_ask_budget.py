"""The time budget of a question on a slow model server (issue #231, umbrella #105).

On an Apple M3 Pro with an MLX build of the default model one discovery call took about 43 seconds;
a complete answer needs that call, the support check and, when citations fail, a repair. The model
server here is a fake that sleeps for the time each call took there, scaled down by SCALE, and the
budget of the question is scaled the same way. This file names default budgets, so `cairnmind eval
repository` never indexes it (NOT_INDEXED in repository_evaluation.py).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from test_knowledge import FakeOllama, calls, document, final, workspace  # noqa: F401  (workspace is a fixture)
from test_second_look import ANSWER, QUESTION, UNKNOWN, long_chunk

from cairnmind import agent as agent_module
from cairnmind import config
from cairnmind.agent import KnowledgeAgent
from cairnmind.config import Settings
from cairnmind.support import NAMES_SYSTEM, SUPPORT_SYSTEM

SCALE = 0.05
# Discovery measured on the Mac (10,677 prompt tokens); repair and support are smaller prompts,
# estimated from their size, not measured.
MAC_SECONDS = {"discovery": 43.0, "repair": 20.0, "support": 10.0}
KILN = "The pottery kiln gets 384 g of glaze per firing."
# The two second-look tests below keep their proportions at this many times their first values, so the
# fixed overhead of retrieval and the store (about 0.35 s under coverage on CI) cannot tip them.
STRETCH = 3.0


class SlowModel(FakeOllama):
    """FakeOllama that takes ``seconds[phase]`` for each call, as a slow local server does."""

    def __init__(self, replies, seconds):
        super().__init__(replies)
        self.seconds = seconds

    async def chat(self, messages, tools=None, **kwargs):
        phase = ("support" if messages and messages[0].get("content") in (SUPPORT_SYSTEM, NAMES_SYSTEM)
                 else "repair" if kwargs.get("format") is not None else "discovery")
        await asyncio.sleep(self.seconds[phase])
        return await super().chat(messages, tools, **kwargs)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
async def test_a_repaired_answer_at_the_speed_of_the_mac_fits_the_default_budget_of_macos(
        workspace, tmp_path, monkeypatch, platform):  # noqa: F811
    store, project, settings = workspace
    with monkeypatch.context() as patch:  # only for loading: sys.platform is the whole process's
        patch.setattr(config.sys, "platform", platform)
        default = Settings.load(tmp_path / "fresh").ask_timeout  # a new installation: no config.json yet
    assert default == {"darwin": 120.0, "linux": 60.0}[platform]
    settings.ask_timeout = default * SCALE  # at the scale of the model below
    source, _ = document(store, project, KILN, uri="notes://kiln")
    model = SlowModel([final(source["id"], "an invented quote", "384 g."), final(source["id"], KILN, "384 g.")],
                      {phase: seconds * SCALE for phase, seconds in MAC_SECONDS.items()})
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


async def test_no_second_look_starts_with_less_time_left_than_the_first_pass_took(workspace, monkeypatch):  # noqa: F811
    store, project, settings = workspace
    document(store, project, long_chunk(), uri="notes://kiln")
    monkeypatch.setattr(agent_module, "SECOND_LOOK_MIN_SECONDS", 0.0)
    settings.ask_timeout = 2.0 * STRETCH
    # The first pass (an answer and its repair) takes 70% of the budget: a second pass of the same calls cannot finish.
    model = SlowModel([UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN],
                      {"discovery": 0.7 * STRETCH, "repair": 0.7 * STRETCH, "support": 0.0})
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], QUESTION)
    assert result["status"] == "partial" and result["diagnostics"]["code"] == "missing_evidence"
    assert "second_look" not in result["diagnostics"] and len(model.calls) == 2
    assert result["diagnostics"]["budget"]["elapsed_seconds"] < settings.ask_timeout - 0.3 * STRETCH


class StepModel(FakeOllama):
    """FakeOllama whose n-th chat call takes ``delays[n]`` seconds."""

    def __init__(self, replies, delays):
        super().__init__(replies)
        self.delays = list(delays)

    async def chat(self, messages, tools=None, **kwargs):
        await asyncio.sleep(self.delays.pop(0) if self.delays else 0.0)
        return await super().chat(messages, tools, **kwargs)


async def test_slow_tool_steps_of_the_first_pass_do_not_hold_back_a_second_look(workspace, monkeypatch):  # noqa: F811
    store, project, settings = workspace
    source, _ = document(store, project, long_chunk(), uri="notes://kiln")
    monkeypatch.setattr(agent_module, "SECOND_LOOK_MIN_SECONDS", 0.0)
    settings.ask_timeout = 2.0 * STRETCH
    # A long search step (55% of the budget), then an answer and its repair (10%): what is left is less than
    # the first pass took, but more than the calls a second look repeats (the second look makes no tool steps).
    model = StepModel([calls(("search", {"query": "kiln glaze"})), UNKNOWN, UNKNOWN,
                       final(source["id"], ANSWER, "384 g.")], [1.1 * STRETCH, 0.1 * STRETCH, 0.1 * STRETCH, 0.1 * STRETCH])
    result = await KnowledgeAgent(store, settings, model).ask(project["id"], QUESTION)
    assert result["status"] == "complete", json.dumps(result["diagnostics"])
    assert result["diagnostics"]["second_look"] == "answered" and "_last_call_at" not in result["diagnostics"]
