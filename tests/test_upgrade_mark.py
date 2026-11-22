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
    monkeypatch.setattr(process_identity, "executable", lambda pid: None)
    assert upgrade_mark.state() == ("running", 4242)
    # It blocks, and the text does not claim an installer for a process nobody could identify (a reused id, #298).
    assert "cannot be identified" in upgrade_mark.refusal() and "upgrade-in-progress" in upgrade_mark.refusal()
    assert "nie daje się rozpoznać" in CATALOG[upgrade_mark.NOTE_RUNNING]


@pytest.mark.parametrize(("program", "kind"), [("/sbin/launchd", "stale"), ("/bin/bash", "running")])
def test_a_process_without_a_readable_command_line_is_told_by_its_program(mark, monkeypatch, program, kind):
    """A reused id of a root daemon or a kernel thread must not block for ever; a shell may still be the installer."""
    mark.write_text("4242\n")
    monkeypatch.setattr(process_identity, "command_line", lambda pid: None)
    monkeypatch.setattr(process_identity, "alive", lambda pid: True)
    monkeypatch.setattr(process_identity, "executable", lambda pid: program)
    assert upgrade_mark.state() == (kind, 4242)


@pytest.mark.parametrize("content", [b"", b"abc\n", b"12 34\n", b"1" * 40, b"0\n", b"4294967296\n", "directory", "no home"])
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


def test_the_installer_and_the_package_agree_on_the_mark(tmp_path):
    """The path, the format and the identity the installer writes are the ones the package reads: a real bash,
    named install-wsl.sh, writes the mark and a Python reads it, while it runs and after it removed the mark."""
    scripts = Path(__file__).resolve().parents[1] / "scripts" / "install-wsl.sh"
    text = scripts.read_text()
    start = text.index("# --- what an installed version left running")
    functions = text[start:text.index("# --- end ---", start)]
    reader = ("import sys; from cairnmind import upgrade_mark as u; "
              "u.mark_path = lambda: __import__('pathlib').Path(sys.argv[1]); print(u.state())")
    mark = tmp_path / ".local" / "state" / "cairnmind" / "upgrade-in-progress"
    installer = tmp_path / "install-wsl.sh"
    installer.write_text("set -euo pipefail\n" + functions + f"\nbegin_upgrade\n"
                         f"\"{sys.executable}\" -I -c \"{reader}\" \"{mark}\"\nend_upgrade\ntest ! -e \"$upgrade_mark\"\n")
    ran = subprocess.run(["bash", str(installer)], env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
                         capture_output=True, text=True, timeout=30)
    assert ran.returncode == 0, ran.stderr
    kind, pid = eval(ran.stdout)  # noqa: S307 - the tuple printed just above
    assert kind == "running" and pid > 1
    assert not mark.exists()


def test_every_upgrade_sentence_has_its_polish_in_the_terminal_catalog():
    placeholders = lambda text: sorted(re.findall(r"\{(\w+)\}", text))  # noqa: E731
    assert upgrade_mark.MESSAGES
    for english, polish in upgrade_mark.MESSAGES.items():
        assert CATALOG[english] == polish != english
        assert placeholders(english) == placeholders(polish)
