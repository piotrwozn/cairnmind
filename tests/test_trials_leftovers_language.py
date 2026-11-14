"""What the 0.10.0 trial fixes left in the source language (#161): English is written, Polish is translated."""
import ast
import re
from pathlib import Path

import pytest

from cairnmind.terminal_i18n import CATALOG, LEGACY, tr

SOURCE = Path(__file__).resolve().parents[1] / "src" / "cairnmind"
POLISH = re.compile("[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
PANELS = ("terminal_panels.py", "terminal_inspector.py", "terminal_impact.py")


def literals(name):
    tree = ast.parse((SOURCE / name).read_text(encoding="utf-8"))
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


@pytest.mark.parametrize("name", PANELS)
def test_panel_text_is_written_in_english(name):
    # About 140 labels and sentences were written in Polish and turned into English through LEGACY.
    shown = [text for text in literals(name) if POLISH.search(text)]
    aliases = [text for text in literals(name) if text in LEGACY and LEGACY[text] != text and text != "model"]
    assert shown == [] and aliases == []


@pytest.mark.parametrize("language", ["en", "pl"])
def test_an_alias_and_its_english_read_the_same(language):
    # What made writing the English in place of a Polish alias safe: both are shown alike, in either language.
    differ = {alias: english for alias, english in LEGACY.items() if tr(alias, language) != tr(english, language)}
    assert differ == {}


@pytest.mark.parametrize(("english", "polish"), [
    ("A historical version cannot be reviewed.", "Wersja historyczna nie podlega ocenie."),
    ("Raw record", "Surowy zapis"),
])
def test_two_inspector_texts_that_were_polish_in_english_too(english, polish):
    # They were not in the catalog, so an English inspector showed them in Polish.
    assert english in literals("terminal_inspector.py") and polish not in literals("terminal_inspector.py")
    assert tr(english, "en") == english and tr(english, "pl") == polish


def test_the_stale_revision_message_is_english_and_reads_as_before():
    now = "stale_workspace_revision: read the current state and try again."
    earlier = "stale_workspace_revision: odczytaj bieżący stan i ponów operację."
    assert now in literals("workspace_state.py") and earlier not in literals("workspace_state.py")
    # The code stays first (callers look for it); the panel shows what it showed before, in both languages.
    shown = ["Workspace changed. Refresh its current state and try again.", earlier]
    assert [tr(now, language) for language in ("en", "pl")] == shown
    assert [tr(earlier, language) for language in ("en", "pl")] == shown


def test_the_preview_answers_in_english_and_is_translated():
    from cairnmind import terminal_preview
    from cairnmind.terminal_i18n_messages import broker_error_text
    from cairnmind.terminal_client import BrokerError
    fixed = [terminal_preview.NOTICE, "The synthetic preview server does not answer.", "Synthetic connection failure."]
    assert all(text in literals("terminal_preview.py") and not POLISH.search(text) for text in fixed)
    assert all(CATALOG.get(text) and POLISH.search(CATALOG[text]) for text in fixed)
    error = BrokerError(503, "Synthetic connection failure.")
    assert broker_error_text(error, lambda text: tr(text, "pl")) == "Broker 503: Syntetyczna awaria połączenia."
    assert broker_error_text(error, lambda text: tr(text, "en")) == "Broker 503: Synthetic connection failure."
