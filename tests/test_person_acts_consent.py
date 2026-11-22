"""A person's act is sent only from a consent dialog (#332).

Headless Textual with fake brokers: no tmux, engine, Docker or model, so the same on macOS; fast tier.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from cairnmind import terminal_person_act
from cairnmind.terminal_i18n import CATALOG, tr
from cairnmind.terminal_panels import ConfirmModal
from cairnmind.terminal_person_act import NOT_GIVEN, agreed, record_line, typed


@pytest.mark.asyncio
async def test_agreed_is_true_only_for_an_explicit_yes():
    answers = [True, False, None, {"note": ""}, 1]
    seen = []

    async def push_screen_wait(screen):
        seen.append(screen)
        return answers.pop(0)

    app = SimpleNamespace(push_screen_wait=push_screen_wait)
    got = [await agreed(app, "Confirm this record?", "text", "Confirm review") for _ in range(5)]
    assert got == [True, False, False, False, False]
    assert all(isinstance(screen, ConfirmModal) and screen.consent for screen in seen)
    assert seen[0].heading == "Confirm this record?" and seen[0].approve == "Confirm review"


def test_every_new_sentence_has_polish():
    for key, polish in terminal_person_act.MESSAGES.items():
        assert CATALOG[key] == polish != key
    assert tr(NOT_GIVEN, "pl", label=tr("Reason", "pl")) == "Uzasadnienie: nie podano."


def test_typed_text_is_shown_whole_and_escaped():
    shown = typed(lambda text, **values: tr(text, "en", **values), "Reason", "a\x1b[31mb\n" + "x" * 3000)
    assert "\\x1b[31m" in shown and "\x1b" not in shown and "x" * 3000 in shown
    assert typed(lambda text, **values: tr(text, "en", **values), "Reason", "  ") == "Reason: none given."
    line = record_line({"content": "first line\nsecond " + "y" * 500})
    assert "\n" not in line and len(line) <= 201
