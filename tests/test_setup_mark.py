"""The mark of a change of where storage and models run: what it blocks, what it names, what it does not block (#339).

Nothing here depends on the platform beyond process_identity, which reads procfs on Linux and sysctl on macOS.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from cairnmind import setup_mark
from cairnmind.service_version import Sentence
from cairnmind.terminal_i18n import CATALOG

HELPER = ["-c", "import time; time.sleep(30)", "cairnmind.setup_change"]


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def children():
    started = []

    def start(*arguments):
        child = subprocess.Popen([sys.executable, *arguments])
        started.append(child)
        return child

    yield start
    for child in started:
        child.kill()
        child.wait()


def test_no_mark_blocks_nothing(data_dir):
    assert setup_mark.state(data_dir) == ("none", None)
    assert setup_mark.refusal(data_dir) is None
    assert setup_mark.note(data_dir) is None


def test_a_running_change_blocks_and_names_its_process(data_dir, children):
    child = children(*HELPER)
    setup_mark.mark_path(data_dir).write_text(f"{child.pid}\n")
    assert setup_mark.state(data_dir) == ("running", child.pid)
    reason = setup_mark.refusal(data_dir)
    assert isinstance(reason, Sentence)
    assert str(reason) == setup_mark.NOTE_RUNNING.format(pid=child.pid, path=setup_mark.mark_path(data_dir))


def test_a_stale_mark_blocks_nothing_and_doctor_names_it(data_dir, children):
    child = children(*HELPER)
    child.kill()
    child.wait()
    setup_mark.mark_path(data_dir).write_text(f"{child.pid}\n")
    assert setup_mark.state(data_dir) == ("stale", child.pid)
    assert setup_mark.refusal(data_dir) is None
    assert str(setup_mark.note(data_dir)) == setup_mark.NOTE_STALE.format(
        pid=child.pid, path=setup_mark.mark_path(data_dir))
    (data_dir / setup_mark.BEFORE).write_text("{}")
    assert setup_mark.BEFORE in str(setup_mark.note(data_dir))


def test_a_mark_held_by_another_program_is_stale(data_dir, children):
    child = children("-c", "import time; time.sleep(30)")  # alive, but not a change
    setup_mark.mark_path(data_dir).write_text(f"{child.pid}\n")
    assert setup_mark.state(data_dir) == ("stale", child.pid)
    assert setup_mark.refusal(data_dir) is None


@pytest.mark.parametrize("content", [b"garbage", b"0\n", b"99999999999\n", b""])
def test_an_unreadable_mark_blocks(data_dir, content):
    setup_mark.mark_path(data_dir).write_bytes(content)
    assert setup_mark.state(data_dir) == ("unreadable", None)
    assert str(setup_mark.refusal(data_dir)) == setup_mark.NOTE_UNREADABLE.format(path=setup_mark.mark_path(data_dir))


def test_a_mark_that_is_a_directory_blocks(data_dir):
    setup_mark.mark_path(data_dir).mkdir()
    assert setup_mark.state(data_dir) == ("unreadable", None)
    assert setup_mark.refusal(data_dir) is not None


def test_the_holder_may_start_the_service(data_dir):
    setup_mark.mark_path(data_dir).write_text(f"{os.getpid()}\n")
    assert setup_mark.refusal(data_dir) is None


def test_a_process_that_cannot_be_identified_counts_as_the_change(data_dir, monkeypatch):
    from cairnmind import process_identity
    setup_mark.mark_path(data_dir).write_text("4242\n")
    monkeypatch.setattr(process_identity, "command_line", lambda pid: None)
    monkeypatch.setattr(process_identity, "executable", lambda pid: None)
    monkeypatch.setattr(process_identity, "alive", lambda pid: True)
    assert setup_mark.state(data_dir) == ("running", 4242)
    assert setup_mark.refusal(data_dir) is not None


def test_the_mark_is_per_data_directory(tmp_path, children):
    one, other = tmp_path / "one", tmp_path / "other"
    one.mkdir()
    other.mkdir()
    child = children(*HELPER)
    setup_mark.mark_path(one).write_text(f"{child.pid}\n")
    assert setup_mark.refusal(one) is not None
    assert setup_mark.refusal(other) is None


def test_held_writes_the_mark_and_removes_only_its_own(data_dir):
    with setup_mark.held(data_dir):
        assert setup_mark.mark_path(data_dir).read_text() == f"{os.getpid()}\n"
        assert oct(setup_mark.mark_path(data_dir).stat().st_mode & 0o777) == "0o600"
    assert not setup_mark.mark_path(data_dir).exists()
    with setup_mark.held(data_dir):
        setup_mark.mark_path(data_dir).write_text("7\n")  # another holder's, put there meanwhile
    assert setup_mark.mark_path(data_dir).read_text() == "7\n"


def test_held_replaces_a_stale_mark(data_dir, children):
    child = children(*HELPER)
    child.kill()
    child.wait()
    setup_mark.mark_path(data_dir).write_text(f"{child.pid}\n")
    with setup_mark.held(data_dir):
        assert setup_mark.mark_path(data_dir).read_text() == f"{os.getpid()}\n"


def test_a_second_change_is_refused_while_the_first_holds_the_lock(data_dir):
    import fcntl
    descriptor = os.open(data_dir / setup_mark.LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # flock is per open file: a second open one refuses
        with pytest.raises(setup_mark.ChangeBusy) as refused:
            with setup_mark.held(data_dir):
                pytest.fail("a second change started")
        assert str(refused.value) == setup_mark.BUSY
        assert not setup_mark.mark_path(data_dir).exists()
    finally:
        os.close(descriptor)


def test_every_mark_sentence_has_polish_with_the_same_placeholders():
    import string
    assert set(setup_mark.MESSAGES) == {setup_mark.NOTE_RUNNING, setup_mark.NOTE_STALE, setup_mark.NOTE_STALE_COPY,
                                        setup_mark.NOTE_UNREADABLE, setup_mark.BUSY, setup_mark.BRIDGE_SETUP}
    for english, polish in setup_mark.MESSAGES.items():
        names = lambda text: {field for _, field, _, _ in string.Formatter().parse(text) if field}  # noqa: E731
        assert names(english) == names(polish)
        assert CATALOG[english] == polish
