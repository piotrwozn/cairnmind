"""Deterministic-only mode (#274): the knowledge agent's stored choice, what init, `models pull` and `models set` do
with it, and what doctor, the evaluations and `ask` say without models.

No test asks a model server that is not a stand-in: servers are httpx.MockTransport, and the store is a FakeStore (the
fast tier has no database). The conftest fixture models_found_at_init replaces the probe of init; the tests of the real
probe put it back.
"""
import json

import pytest

from cairnmind import first_start, knowledge_mode
from cairnmind.config import Settings
from cairnmind.terminal_i18n import tr

NEW_SENTENCES = [first_start.KNOWLEDGE_DISABLED, first_start.MODELS_NOT_CONFIGURED, first_start.GET_MODELS,
                 first_start.SERVER_DOWN, first_start.MODEL_MISSING, first_start.CHOSE_ENABLED,
                 first_start.CHOSE_DISABLED, first_start.TURNED_ON, first_start.ASK_OFF, first_start.NEEDS_MODELS,
                 first_start.KNOWLEDGE_INVALID]


def test_a_config_from_before_0_13_counts_as_enabled_and_a_new_one_waits_for_init(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    (old / "config.json").write_text(json.dumps({"port": 1, "chat_model": "m"}))  # no knowledge_agent: before 0.13.0
    assert Settings.load(old).knowledge_agent == "enabled"
    new = Settings.load(tmp_path / "new", create=True)
    assert new.knowledge_agent == ""  # not chosen yet: init chooses
    assert Settings.load(tmp_path / "new").knowledge_agent == ""  # saved as it is, not turned into enabled
    assert Settings.load(tmp_path / "nothing").knowledge_agent == ""  # no config.json at all
    new.knowledge_agent = "disabled"
    assert [knowledge_mode.enabled(Settings.load(old)), knowledge_mode.enabled(Settings.load(tmp_path / "new")),
            knowledge_mode.enabled(new)] == [True, True, False]
    new.knowledge_agent = "on"
    with pytest.raises(ValueError, match="knowledge_agent in config.json is 'on'; it must be enabled or disabled"):
        knowledge_mode.enabled(new)


def test_the_unavailable_answer_and_the_status_fields():
    answer = knowledge_mode.unavailable_answer()
    assert answer["status"] == "unavailable" and answer["sources"] == [] and answer["answer"] == first_start.ASK_OFF
    assert answer["unknowns"] == [first_start.GET_MODELS]
    assert answer["diagnostics"]["code"] == "models_not_configured"
    pipeline = {"archive": {"pending": 7, "running": 0, "completed": 0, "failed": 0},
                "indexing": {"pending": 3, "running": 1, "completed": 5, "failed": 2},
                "extraction": {"pending": 2, "running": 0, "completed": 1, "failed": 0},
                "other": {"pending": 9, "running": 9, "completed": 0, "failed": 0}}
    assert knowledge_mode.status_fields(pipeline) == {
        "models": "not configured (deterministic-only mode)", "jobs_held": 6, "get_models": first_start.GET_MODELS}


def test_the_ask_panel_shows_the_answer_in_polish_and_every_new_sentence_has_polish():
    from cairnmind.terminal_commands import ask_summary
    shown = ask_summary(knowledge_mode.unavailable_answer(), lambda text: tr(text, "pl"))
    assert first_start.MESSAGES[first_start.ASK_OFF] in shown
    assert first_start.MESSAGES[first_start.GET_MODELS] in shown
    for english in NEW_SENTENCES:
        assert first_start.MESSAGES[english] != english
        assert tr(english, "pl") == first_start.MESSAGES[english]
