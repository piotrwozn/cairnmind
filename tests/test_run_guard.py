"""A hanging test fails after its time limit, and what a test leaves running is stopped after it (#236).

Most cases run pytest itself on a small test module, with this directory's conftest and guard, as a
real run would: a hang or a leak there must end inside that run, and this test checks the result.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest

import run_guard
from cairnmind.arcade import ArcadeError

HERE = Path(__file__).resolve().parent
# Longer than any inner run needs once the guard works; without it the inner run hangs until this ends.
INNER_RUN_SECONDS = 90


def inner_run(tmp_path: Path, source: str, *options: str, limit: str = "2", env: dict | None = None):
    """Run pytest on `source` as tests/test_shards.py (a module the conftest runs without the engine).

    Returns the completed process and how long it took. A run that does not end in INNER_RUN_SECONDS
    is killed with its whole group and fails this test.
    """
    tests = tmp_path / "inner" / "tests"
    (tests / "fixtures").mkdir(parents=True)
    for name in ("conftest.py", "run_guard.py"):
        if (HERE / name).exists():
            shutil.copyfile(HERE / name, tests / name)
    shutil.copyfile(HERE / "fixtures" / "mcp_fixture_server.py", tests / "fixtures" / "mcp_fixture_server.py")
    (tests / "test_shards.py").write_text(textwrap.dedent(source))
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("CAIRNMIND_TEST_TIME_LIMIT", "DEVBRAIN_TEST_TIME_LIMIT", "TMUX", "TMUX_PANE")}
    environment.update({"CAIRNMIND_TEST_TIME_LIMIT": limit, **(env or {})})
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rA",
                                "--basetemp", str(tmp_path / "base"), "tests/test_shards.py", *options],
                               cwd=tests.parent, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               start_new_session=True)
    try:
        output, _ = process.communicate(timeout=INNER_RUN_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        pytest.fail(f"The inner run did not end in {INNER_RUN_SECONDS} s:\n{output[-3000:]}")
    return subprocess.CompletedProcess(process.args, process.returncode, output, ""), time.monotonic() - started


def ended(probe, pid: int) -> bool:
    """Whether nothing answers `probe` (os.kill or os.killpg) for `pid` within a few seconds: an
    orphan that was killed stays a zombie until init reaps it."""
    deadline = time.monotonic() + 5
    while True:
        try:
            probe(pid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(.05)


def gone(pid: int) -> bool:
    return ended(os.kill, pid)


def group_gone(pgid: int) -> bool:
    return ended(os.killpg, pgid)


def test_a_hanging_test_fails_after_its_time_limit_and_the_run_goes_on(tmp_path):
    result, took = inner_run(tmp_path, """
        import time

        def test_hangs():
            time.sleep(3600)

        def test_after_it():
            pass
    """)
    assert result.returncode == 1, result.stdout
    assert "1 failed, 1 passed" in result.stdout
    assert "FAILED tests/test_shards.py::test_hangs" in result.stdout
    assert "took longer than 2 s" in result.stdout
    assert took < 60


def test_a_test_sets_its_own_limit(tmp_path):
    result, took = inner_run(tmp_path, """
        import time
        import pytest

        @pytest.mark.time_limit(1)
        def test_shorter_than_the_default():
            time.sleep(3600)

        @pytest.mark.time_limit(30)
        def test_longer_than_the_default():
            time.sleep(4)
    """)
    assert "1 failed, 1 passed" in result.stdout, result.stdout
    assert "FAILED tests/test_shards.py::test_shorter_than_the_default" in result.stdout
    assert "took longer than 1 s" in result.stdout
    assert took < 60


def test_under_xdist_a_hang_neither_blocks_the_exclusive_gate_nor_the_run(tmp_path):
    """The run that deadlocked on the Mac: a test hangs in its shared slot while an `exclusive` test
    waits for every slot. The hang fails, the exclusive test then runs, and the run ends. A test whose
    time-limit signal cannot be delivered (here: blocked) is ended by the backstop with its worker."""
    result, took = inner_run(tmp_path, """
        import signal
        import time
        import pytest

        def test_hangs_in_its_slot():
            time.sleep(3600)

        def test_cannot_be_interrupted():
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
            time.sleep(3600)

        @pytest.mark.exclusive
        def test_exclusive_waits_for_the_others():
            pass

        def test_ordinary():
            pass
    """, "-n", "2", limit="3")
    assert result.returncode == 1, result.stdout
    assert "PASSED tests/test_shards.py::test_exclusive_waits_for_the_others" in result.stdout
    assert "PASSED tests/test_shards.py::test_ordinary" in result.stdout
    assert "FAILED tests/test_shards.py::test_hangs_in_its_slot" in result.stdout
    assert "FAILED tests/test_shards.py::test_cannot_be_interrupted" in result.stdout
    assert took < 75


def test_a_test_waiting_at_the_gate_fails_in_time(tmp_path):
    if not run_guard.CLOCK.signals:
        pytest.skip("this run has no time limit (CAIRNMIND_TEST_TIME_LIMIT=0 or not the main thread)")
    gate = tmp_path / "test-slot.gate"
    with gate.open("a") as holder, gate.open("a") as waiter:
        fcntl.flock(holder, fcntl.LOCK_EX)  # another worker's test that never ends
        started = time.monotonic()
        with pytest.raises(pytest.fail.Exception, match="waiting for the test slot took longer than 1 s"):
            with run_guard.waiting(1, "waiting for the test slot"):
                fcntl.flock(waiter, fcntl.LOCK_EX)
        assert time.monotonic() - started < 10


def test_what_a_failed_test_left_running_is_stopped_after_it(tmp_path):
    """A server that ignores SIGTERM with a child in its group, and a plain child that ignores it too:
    both are gone after the run. A process of a module-scoped fixture lives on to the next test."""
    pids = tmp_path / "pids"
    result, _ = inner_run(tmp_path, """
        import os
        from pathlib import Path
        import subprocess
        import sys
        import time
        import pytest
        import conftest

        DEAF = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(3600)"

        @pytest.fixture(scope="module")
        def shared_sleeper():
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
            yield process
            process.kill()
            process.wait()

        def test_leaves_processes_and_fails(shared_sleeper):
            server = subprocess.Popen([sys.executable, conftest.MCP_FIXTURE, "deaf"], stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, start_new_session=True)
            plain = subprocess.Popen([sys.executable, "-c", DEAF])
            Path(os.environ["GUARD_PIDS"]).write_text(f"{server.pid} {plain.pid} {shared_sleeper.pid}")
            time.sleep(1)  # the server starts its child
            assert False, "this test fails and leaves both running"

        def test_a_module_fixture_outlives_one_test(shared_sleeper):
            assert shared_sleeper.poll() is None
    """, env={"GUARD_PIDS": str(pids)})
    server, plain, shared = map(int, pids.read_text().split())
    try:
        assert "1 failed, 1 passed" in result.stdout, result.stdout
        assert "PASSED tests/test_shards.py::test_a_module_fixture_outlives_one_test" in result.stdout
        assert group_gone(server) and gone(plain) and gone(shared), result.stdout
        assert "left running" in result.stdout and str(server) in result.stdout and str(plain) in result.stdout
        assert str(shared) not in result.stdout.split("left running", 1)[1]
    finally:
        for pid, kill in ((server, os.killpg), (plain, os.kill), (shared, os.kill)):
            with contextlib.suppress(OSError):
                kill(pid, signal.SIGKILL)


def test_a_limit_is_a_number_of_seconds():
    assert run_guard.read_limit("2.5") == 2.5 and run_guard.read_limit("0") == 0 and run_guard.read_limit(7) == 7
    for wrong in ("", "abc", "-1", "nan", "inf", None):
        with pytest.raises(pytest.UsageError):
            run_guard.read_limit(wrong)


def test_only_a_socket_named_with_dash_s_is_taken_from_a_tmux_command(tmp_path):
    socket_of = run_guard.tmux_socket
    assert socket_of(["/opt/tmux-3.4/tmux", "-S", "/x/s", "new-session"], None) == ("/opt/tmux-3.4/tmux", "/x/s")
    assert socket_of(("tmux", "-f", "/dev/null", "-S", "rel/s", "ls"), tmp_path) == ("tmux", str(tmp_path / "rel/s"))
    assert socket_of(["tmux", "capture-pane", "-p", "-S", "-"], None) is None  # an option of the command
    assert socket_of(["tmux", "-L", "name", "list-sessions"], None) is None  # tmux's own directory
    assert socket_of(["tmuxinator", "-S", "/x/s"], None) is None
    assert socket_of("tmux -S /x/s ls", None) is None  # a shell line is not parsed
    assert run_guard.default_tmux_socket(f"/tmp/tmux-{os.getuid()}/default")
    assert not run_guard.default_tmux_socket("/tmp/cairnmind-ui-1000-abc/tmux.sock")


def test_a_tmux_server_that_was_running_before_the_test_is_not_the_tests(tmp_path):
    """A socket the test was not first to use (the installation's, or a module fixture's) is never stopped."""
    fake = tmp_path / "tmux"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    before, fresh = tmp_path / "before.sock", tmp_path / "fresh.sock"
    before.write_text("")
    for socket in (before, fresh):
        subprocess.run([str(fake), "-S", str(socket), "list-sessions"], check=True)
    assert str(fresh) in run_guard.SPAWNED.sockets and str(before) not in run_guard.SPAWNED.sockets


def test_an_engine_left_in_the_tests_directory_is_stopped_through_its_identity_check(tmp_path, monkeypatch):
    import cairnmind.engine as engine
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        for name, pid in (("left", running.pid), ("own", os.getpid()), ("ended", ended.pid)):
            (tmp_path / name / "arcade").mkdir(parents=True)
            (tmp_path / name / "arcade" / "process.json").write_text(json.dumps({"pid": pid}))
        asked = []
        monkeypatch.setattr(engine, "stop_engine", lambda settings: asked.append(settings.data_dir))
        assert run_guard.stop_engines({"tmp_path": tmp_path}) == [f"engine {running.pid} of {tmp_path / 'left'}"]
        assert asked == [(tmp_path / "left").resolve()]  # not this process, not a process that ended

        def refuse(settings):
            raise ArcadeError("Managed ArcadeDB PID no longer belongs to this environment")
        monkeypatch.setattr(engine, "stop_engine", refuse)
        assert run_guard.stop_engines({"tmp_path": tmp_path}) == [
            f"engine {running.pid} of {tmp_path / 'left'}, NOT stopped: ArcadeError: "
            "Managed ArcadeDB PID no longer belongs to this environment"]
        assert running.poll() is None  # refused: nothing was killed
        assert run_guard.stop_engines({}) == []  # a test without tmp_path has no directory of its own
    finally:
        running.kill()
        running.wait()


@pytest.mark.tmux
def test_a_tmux_server_a_failed_test_started_is_stopped_after_it(tmp_path):
    tmux = shutil.which("tmux")
    if not tmux:
        pytest.skip("tmux is not installed")
    folder = Path(tempfile.mkdtemp(prefix="cm236-"))  # short: a socket path has a length limit
    socket = folder / "s"
    try:
        result, _ = inner_run(tmp_path, """
            import os
            import subprocess

            def test_leaves_a_tmux_server():
                subprocess.run([os.environ["GUARD_TMUX"], "-S", os.environ["GUARD_SOCKET"], "-f", "/dev/null",
                                "new-session", "-d", "sleep 3600"], check=True)
                assert False, "this test fails and leaves its tmux server"
        """, env={"GUARD_TMUX": tmux, "GUARD_SOCKET": str(socket)})
        assert "1 failed" in result.stdout, result.stdout
        assert "left running" in result.stdout and str(socket) in result.stdout
        answer = subprocess.run([tmux, "-S", str(socket), "list-sessions"], capture_output=True, timeout=10)
        assert answer.returncode != 0  # no server answers on the socket any more
    finally:
        subprocess.run([tmux, "-S", str(socket), "kill-server"], capture_output=True, timeout=10)
        shutil.rmtree(folder, ignore_errors=True)
