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
    assert setup_mark.unchecked(data_dir) is None  # nothing was copied: nothing is unchecked


def test_a_kept_copy_blocks_every_start_with_or_without_a_stale_mark(data_dir, children):
    child = children(*HELPER)
    child.kill()
    child.wait()
    copy = data_dir / setup_mark.BEFORE
    copy.write_text("{}")
    assert setup_mark.state(data_dir) == ("none", None)  # a change that ended and left the copy: no mark
    for expected in (setup_mark.NOTE_COPY.format(copy=copy), None):
        reason = setup_mark.refusal(data_dir)
        assert str(reason) == (expected or setup_mark.NOTE_STALE_COPY.format(
            pid=child.pid, path=setup_mark.mark_path(data_dir), copy=copy))
        assert str(setup_mark.unchecked(data_dir)) == str(reason) and str(setup_mark.note(data_dir)) == str(reason)
        setup_mark.mark_path(data_dir).write_text(f"{child.pid}\n")  # and now the same with the dead holder's mark
    own = setup_mark.mark_path(data_dir)
    own.write_text(f"{os.getpid()}\n")
    assert setup_mark.refusal(data_dir) is None  # the process that made the copy may start the service
    copy.unlink()
    own.unlink()
    assert setup_mark.refusal(data_dir) is None and setup_mark.unchecked(data_dir) is None


def test_a_running_change_is_not_unchecked_for_doctor_and_its_own_copy_does_not_block_it(data_dir, children):
    (data_dir / setup_mark.BEFORE).write_text("{}")
    child = children(*HELPER)
    setup_mark.mark_path(data_dir).write_text(f"{child.pid}\n")
    assert setup_mark.unchecked(data_dir) is None and "is in progress" in str(setup_mark.refusal(data_dir))


def test_the_holder_may_start_the_service_in_a_process_that_the_mark_identifies_as_the_change(data_dir, children):
    """A real helper-like process: its command line names the module, so its own mark reads as running, and
    only its own pid is exempt: the process that is not the holder is refused."""
    code = ("import sys; from pathlib import Path; from cairnmind import setup_mark\n"
            "d = Path(sys.argv[1])\n"
            "with setup_mark.held(d):\n"
            "    print(setup_mark.state(d)[0], setup_mark.refusal(d), flush=True)\n"
            "    sys.stdin.readline()\n")
    child = subprocess.Popen([sys.executable, "-c", code, str(data_dir), "cairnmind.setup_change"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "running None"  # the holder is not refused by its own mark
        assert setup_mark.state(data_dir) == ("running", child.pid)
        assert "is in progress" in str(setup_mark.refusal(data_dir))  # this process is
    finally:
        child.stdin.close()
        child.wait(timeout=20)
    assert not setup_mark.mark_path(data_dir).exists()


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


def test_a_framework_python_with_an_unreadable_command_line_counts_as_the_change(data_dir, monkeypatch):
    from cairnmind import process_identity  # macOS runs it as .../MacOS/Python
    setup_mark.mark_path(data_dir).write_text("4242\n")
    monkeypatch.setattr(process_identity, "command_line", lambda pid: None)
    monkeypatch.setattr(process_identity, "executable", lambda pid: "/x/Python.app/Contents/MacOS/Python")
    monkeypatch.setattr(process_identity, "alive", lambda pid: True)
    assert setup_mark.state(data_dir) == ("running", 4242)


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


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setenv("CAIRNMIND_PORT", "1")  # nothing here may reach the person's own service
    for name in ("CAIRNMIND_SESSION_ID", "DEVBRAIN_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def running(data_dir, children):
    """A change that runs in this data directory: a live process whose command line names the helper."""
    child = children(*HELPER)
    setup_mark.mark_path(data_dir).write_text(f"{child.pid}\n")
    return child


def forbidden(*args, **kwargs):
    raise AssertionError("a service was started")


def test_a_running_change_blocks_an_automatic_start_and_names_it(data_dir, running, isolated, monkeypatch):
    from cairnmind import cli
    from cairnmind.config import Settings
    settings = Settings.load(data_dir)
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    with pytest.raises(cli.SetupInProgress) as refused:
        cli.ensure_service(settings)
    assert isinstance(refused.value, cli.ServiceDidNotStart)
    assert str(refused.value) == setup_mark.NOTE_RUNNING.format(pid=running.pid, path=setup_mark.mark_path(data_dir))
    assert not (data_dir / "service.log").exists()


def test_a_stale_mark_does_not_stop_a_start(data_dir, children, isolated, monkeypatch):
    from types import SimpleNamespace

    from cairnmind import cli
    from cairnmind.config import Settings
    child = children(*HELPER)
    child.kill()
    child.wait()
    setup_mark.mark_path(data_dir).write_text(f"{child.pid}\n")
    settings = Settings.load(data_dir)
    alive = iter([False, True])
    monkeypatch.setattr(cli, "service_alive", lambda settings: next(alive))
    started = []
    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda *args, **kwargs: started.append(args) or SimpleNamespace(poll=lambda: None))
    assert cli.ensure_service(settings) is True
    assert len(started) == 1


def test_a_running_service_is_still_used_during_a_change_so_the_change_can_stop_it(data_dir, running, isolated,
                                                                                    monkeypatch):
    from cairnmind import cli
    from cairnmind.config import Settings
    monkeypatch.setattr(cli, "service_alive", lambda settings: True)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    monkeypatch.setattr("cairnmind.service_version.refusal", lambda settings: None)
    assert cli.ensure_service(Settings.load(data_dir)) is False


def test_commands_during_a_change_say_so(data_dir, running, isolated, monkeypatch):
    from typer.testing import CliRunner

    from cairnmind import cli
    from conftest import plain_words
    from cairnmind.config import Settings
    Settings.load(data_dir, create=True)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    sentence = setup_mark.NOTE_RUNNING.format(pid=running.pid, path=setup_mark.mark_path(data_dir))
    runner = CliRunner()
    status = runner.invoke(cli.app, ["--data-dir", str(data_dir), "status"])
    assert status.exit_code == 2, status.output
    assert sentence in plain_words(status.output) and "cairnmind serve" not in status.output
    profile = runner.invoke(cli.app, ["--data-dir", str(data_dir), "setup", "local", "--yes"])
    assert profile.exit_code == 2, profile.output
    assert sentence in plain_words(profile.output)
    from cairnmind.config import Settings
    import typer
    with pytest.raises(typer.Exit):  # what `cairnmind restore` holds before it touches anything
        with cli._service_stopped(Settings.load(data_dir), "held", "running"):
            pytest.fail("a restore started during a change")


def test_doctor_names_a_change_that_did_not_finish(data_dir, children, isolated, monkeypatch):
    import json

    from typer.testing import CliRunner

    from cairnmind import cli, integrations
    child = children(*HELPER)
    child.kill()
    child.wait()
    setup_mark.mark_path(data_dir).write_text(f"{child.pid}\n")
    monkeypatch.setattr(integrations, "doctor", lambda settings: {})
    report = json.loads(CliRunner().invoke(cli.app, ["--data-dir", str(data_dir), "doctor"]).output)
    notes = [" ".join(line.split()) for line in report["environment_notes"]]
    assert " ".join(setup_mark.NOTE_STALE.format(pid=child.pid, path=setup_mark.mark_path(data_dir)).split()) in notes


def test_a_kept_copy_stops_a_start_and_doctor_counts_it_as_missing(data_dir, isolated, monkeypatch):
    import json

    from typer.testing import CliRunner

    from cairnmind import cli, integrations
    from cairnmind.config import Settings
    from cairnmind.terminal_i18n import tr
    (data_dir / setup_mark.BEFORE).write_text("{}")  # a change that ended, and config.json is nobody's checked
    sentence = setup_mark.NOTE_COPY.format(copy=data_dir / setup_mark.BEFORE)
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    with pytest.raises(cli.SetupInProgress) as refused:
        cli.ensure_service(Settings.load(data_dir, create=True))  # what every panel and the broker do when it is gone
    assert str(refused.value) == sentence
    monkeypatch.setattr(integrations, "doctor", lambda settings: {})
    report = json.loads(CliRunner().invoke(cli.app, ["--data-dir", str(data_dir), "doctor"]).output)
    flat = lambda lines: [" ".join(line.split()) for line in lines]  # noqa: E731
    assert flat(report["environment_blockers"]).count(" ".join(sentence.split())) == 1
    assert flat(report["environment_notes"]).count(" ".join(sentence.split())) == 1  # once, not twice
    from cairnmind.terminal_i18n_messages import setup_mark_text
    assert setup_mark_text(sentence, lambda text: tr(text, "pl")) == setup_mark.MESSAGES[setup_mark.NOTE_COPY].format(
        copy=data_dir / setup_mark.BEFORE)


def test_an_interrupted_start_ends_the_service_it_spawned(data_dir, isolated, monkeypatch):
    """ensure_service leaves nothing behind when a signal reaches it while it waits for the service it started."""
    from types import SimpleNamespace

    from cairnmind import cli
    from cairnmind.config import Settings
    ended = []
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(
        poll=lambda: None, terminate=lambda: ended.append("terminate")))

    def interrupt(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli.ensure_service(Settings.load(data_dir, create=True))
    assert ended == ["terminate"]


def test_the_broker_says_the_change_when_the_service_is_gone(data_dir, running, isolated):
    import httpx
    from fastapi.testclient import TestClient

    from cairnmind.cockpit import create_cockpit_app
    from cairnmind.config import Settings
    from cairnmind.terminal_client import BrokerError
    from cairnmind.terminal_i18n import tr
    from cairnmind.terminal_i18n_messages import broker_error_text

    def gone(request):
        raise httpx.ConnectError("refused", request=request)

    origin = "http://127.0.0.1:8766"
    app = create_cockpit_app(Settings.load(data_dir, create=True), proxy_transport=httpx.MockTransport(gone))
    sentence = setup_mark.NOTE_RUNNING.format(pid=running.pid, path=setup_mark.mark_path(data_dir))
    with TestClient(app, base_url=origin) as client:
        ticket = app.state.browser_sessions.ticket()
        assert client.post("/auth/exchange", json={"ticket": ticket}, headers={"Origin": origin}).status_code == 200
        response = client.get("/api/status")
        assert response.status_code == 503 and response.json()["detail"] == sentence
        setup_mark.mark_path(data_dir).unlink()  # no change in progress: the earlier sentence
        assert client.get("/api/status").json()["detail"] == "The memory service is unavailable. Restart cairnmind."
    error = BrokerError(503, sentence)
    assert broker_error_text(error, lambda text: tr(text, "en")) == f"Broker 503: {sentence}"
    assert broker_error_text(error, lambda text: tr(text, "pl")) == "Broker 503: " + setup_mark.MESSAGES[
        setup_mark.NOTE_RUNNING].format(pid=running.pid, path=setup_mark.mark_path(data_dir))


def test_an_mcp_tool_call_during_a_change_names_it(data_dir, running, isolated, monkeypatch):
    from cairnmind import cli, mcp_server
    from cairnmind.config import Settings
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    monkeypatch.setattr("cairnmind.store_writer.inspect", lambda settings: {"role": "writer"})
    with pytest.raises(RuntimeError) as refused:
        mcp_server.start_service(Settings.load(data_dir))
    reason = setup_mark.NOTE_RUNNING.format(pid=running.pid, path=setup_mark.mark_path(data_dir))
    assert str(refused.value) == setup_mark.BRIDGE_SETUP.format(reason=reason)
    assert "cairnmind serve" not in str(refused.value)


async def test_the_activity_panel_shows_the_change_instead_of_unavailable(tmp_path, monkeypatch):
    from cairnmind.terminal_client import BrokerError
    from test_terminal_activity import Broker, flat, make, until

    sentence = setup_mark.NOTE_RUNNING.format(pid=4242, path="/data/setup-in-progress")

    class Down(Broker):
        def request(self, method, path, **kwargs):
            raise BrokerError(503, sentence)

    for language, expected in (("en", sentence), ("pl", setup_mark.MESSAGES[setup_mark.NOTE_RUNNING].format(
            pid=4242, path="/data/setup-in-progress"))):
        app, *_ = make(tmp_path, monkeypatch, language=language, broker=Down())
        async with app.run_test(size=(60, 43)) as pilot:
            await until(pilot, lambda: expected in flat(app))
            assert "Memory service unavailable" not in flat(app)


def test_every_mark_sentence_has_polish_with_the_same_placeholders():
    import string
    assert set(setup_mark.MESSAGES) == {setup_mark.NOTE_RUNNING, setup_mark.NOTE_STALE, setup_mark.NOTE_STALE_COPY,
                                        setup_mark.NOTE_COPY, setup_mark.NOTE_UNREADABLE, setup_mark.BUSY,
                                        setup_mark.BRIDGE_SETUP}
    for english, polish in setup_mark.MESSAGES.items():
        names = lambda text: {field for _, field, _, _ in string.Formatter().parse(text) if field}  # noqa: E731
        assert names(english) == names(polish)
        assert CATALOG[english] == polish
