"""Language findings of the 0.10.0 trial (#153): English source text, Polish translation, nothing mixed."""
import ast
import re
from pathlib import Path

import pytest

from cairnmind.terminal_commands import ask_summary
from cairnmind.terminal_i18n import tr
from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_ui import ChoiceModal
from test_terminal_panels import setup, settle

SOURCE = Path(__file__).resolve().parents[1] / "src" / "cairnmind"
POLISH = re.compile("[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")


def polish_literals(name):
    tree = ast.parse((SOURCE / name).read_text(encoding="utf-8"))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and POLISH.search(node.value)]


@pytest.mark.parametrize("name", ["agent.py", "service.py"])
def test_backend_messages_are_written_in_english(name):
    assert polish_literals(name) == []


# What `cairnmind ask` returned on the trial installation for an English question, now in English.
UNVERIFIED = {
    "status": "partial", "answer": "The model's answer could not be verified in the sources.",
    "sources": [], "diagnostics": {"code": "answer_unsupported"},
    "unknowns": ["The quoted passage does not occur in the checked version of the source.",
                 "The quotes do not support the answer: the quote names another key [bold]",
                 "Nothing in memory says when the trial ran."],
}


def test_ask_answer_and_gaps_read_in_english_in_an_english_panel():
    text = ask_summary(UNVERIFIED, lambda value: tr(value, "en"))
    assert POLISH.search(text) is None
    assert "Status: Partial" in text and "could not be verified in the sources." in text


def test_ask_answer_and_gaps_are_translated_in_a_polish_panel_but_model_text_is_kept():
    text = ask_summary(UNVERIFIED, lambda value: tr(value, "pl"))
    assert "Stan: Częściowa" in text
    assert "Nie udało się zweryfikować odpowiedzi modelu w źródłach." in text
    assert "- Cytowany fragment nie występuje w sprawdzonej wersji źródła." in text
    # The fixed prefix is translated; the judging model's reason after it stays exactly as it came.
    assert "- Cytaty nie potwierdzają odpowiedzi: the quote names another key [bold]" in text
    assert "- Nothing in memory says when the trial ran." in text


def test_a_complete_answer_is_never_swapped_for_a_catalog_sentence():
    # A model may answer with any words, even a word the catalog knows; a verified answer is shown as it is.
    result = {"status": "complete", "answer": "Open", "sources": [], "unknowns": ["Open"]}
    text = ask_summary(result, lambda value: tr(value, "pl"))
    assert "Stan: Pełna" in text and "\nOpen" in text and "- Open" in text and "Otwórz" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(("choice", "title"), [
    ("language", "Language"), ("view", "Layout"), ("reduced_motion", "Reduce motion")])
async def test_the_value_dialog_is_titled_after_the_setting_it_changes(tmp_path, monkeypatch, choice, title):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    seen = []

    async def choose(screen):
        seen.append(screen)
        return choice if len(seen) == 1 else None
    monkeypatch.setattr(app, "push_screen_wait", choose)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.action_settings()
        await settle(app, pilot)
    assert [screen.heading for screen in seen if isinstance(screen, ChoiceModal)] == ["Settings", title]
