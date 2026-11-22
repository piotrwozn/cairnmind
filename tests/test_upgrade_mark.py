"""The upgrade mark the installer leaves and the package reads (#298)."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from cairnmind import process_identity, upgrade_mark
from cairnmind.terminal_i18n import CATALOG


@pytest.fixture
def mark(tmp_path):
    path = tmp_path / "state" / "upgrade-in-progress"
    path.parent.mkdir()
    original = upgrade_mark.mark_path
    upgrade_mark.mark_path = lambda: path
    yield path
    upgrade_mark.mark_path = original


def installer_like_child():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "/x/scripts/install-wsl.sh"])


def test_without_a_mark_nothing_is_blocked_or_noted(mark):
    assert upgrade_mark.state() == ("none", None)
    assert upgrade_mark.refusal() is None
    assert upgrade_mark.note() is None


def test_a_mark_of_a_running_installer_blocks_and_names_its_process(mark):
    child = installer_like_child()
    try:
        mark.write_text(f"{child.pid}\n")
        assert upgrade_mark.state() == ("running", child.pid)
        assert upgrade_mark.refusal() == upgrade_mark.NOTE_RUNNING.format(pid=child.pid)
    finally:
        child.kill()
        child.wait()


def test_a_mark_whose_installer_is_gone_blocks_nothing_and_doctor_says_the_upgrade_did_not_finish(mark):
    child = installer_like_child()
    child.kill()
    child.wait()
    mark.write_text(f"{child.pid}\n")
    assert upgrade_mark.state() == ("stale", child.pid)
    assert upgrade_mark.refusal() is None
    assert upgrade_mark.note() == upgrade_mark.NOTE_STALE.format(pid=child.pid)


def test_a_mark_whose_process_id_now_names_another_program_blocks_nothing(mark):
    import os
    mark.write_text(f"{os.getpid()}\n")
    assert upgrade_mark.state() == ("stale", os.getpid())
    assert upgrade_mark.refusal() is None


def test_a_process_that_runs_but_cannot_be_identified_counts_as_the_installer(mark, monkeypatch):
    mark.write_text("4242\n")
    monkeypatch.setattr(process_identity, "command_line", lambda pid: None)
    monkeypatch.setattr(process_identity, "alive", lambda pid: True)
    assert upgrade_mark.state() == ("running", 4242)


@pytest.mark.parametrize("content", [b"", b"abc\n", b"12 34\n", b"1" * 40, "directory", "no home"])
def test_a_mark_that_cannot_be_read_blocks(mark, monkeypatch, content):
    if content == "directory":
        mark.mkdir()
    elif content == "no home":
        monkeypatch.setattr(upgrade_mark, "mark_path", lambda: Path.home())

        def no_home():
            raise RuntimeError("Could not determine home directory.")
        monkeypatch.setattr(Path, "home", staticmethod(no_home))
    else:
        mark.write_bytes(content)
    assert upgrade_mark.state() == ("unreadable", None)
    assert upgrade_mark.refusal() == upgrade_mark.NOTE_UNREADABLE


def test_every_upgrade_sentence_has_its_polish_in_the_terminal_catalog():
    placeholders = lambda text: sorted(re.findall(r"\{(\w+)\}", text))  # noqa: E731
    assert upgrade_mark.MESSAGES
    for english, polish in upgrade_mark.MESSAGES.items():
        assert CATALOG[english] == polish != english
        assert placeholders(english) == placeholders(polish)
