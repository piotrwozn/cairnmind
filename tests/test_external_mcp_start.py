"""An external MCP session starts the knowledge service when nothing listens (#258).

The data directory sets port 1, so a connection a test forgot to fake is refused at once and never reaches the
owner's service. No test starts a real process: ensure_service is replaced, or Popen is.
"""
import asyncio
import errno
import json
import shlex
import subprocess
import sys

import httpx
import pytest

from conftest import own_time
from cairnmind import cli, mcp_server
from cairnmind.config import Settings

UNAVAILABLE = "Cairnmind service is unavailable (ConnectError); start `cairnmind serve` or use the launcher."


@pytest.fixture
def data(tmp_path, monkeypatch):
    for name in ("CAIRNMIND_SESSION_ID", "CAIRNMIND_PACKET_ID", "DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID"):
        monkeypatch.delenv(name, raising=False)
    directory = tmp_path / "memory"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps({"port": 1, "arcade_url": "http://127.0.0.1:1"}))
    return directory


@pytest.fixture
def settings(data):
    return Settings.load(data, create=True)


class Network:
    """A service that is reachable only once `up` is set; every request it was sent is kept."""

    def __init__(self, monkeypatch, status=200, fail=None):
        self.up, self.seen, self.status, self.fail = False, [], status, fail
        real = httpx.AsyncClient
        monkeypatch.setattr(mcp_server.httpx, "AsyncClient", lambda **kw: real(
            transport=httpx.MockTransport(self.handle), **kw))

    def handle(self, request):
        self.seen.append(request.url.path)
        if self.fail:
            raise self.fail(" ", request=request)
        if not self.up:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(self.status, json={"answer": "yes"})


def external(settings, tmp_path):
    return mcp_server.ServiceClient(settings, external={"project": str(tmp_path), "worktree": None})


def ask(bridge):
    return asyncio.run(bridge.post("/ask", {"project": "p", "question": "q"}))


def test_an_external_bridge_starts_the_service_when_nothing_listens(monkeypatch, settings, tmp_path):
    network, started = Network(monkeypatch), []
    monkeypatch.setattr(cli, "ensure_service", lambda s: (started.append(s), setattr(network, "up", True)))
    assert ask(external(settings, tmp_path)) == {"answer": "yes"}
    assert started == [settings]
    assert network.seen == ["/ask", "/ask"]  # the first never reached a service; the second did


def test_a_service_that_cannot_start_is_named_with_the_command(monkeypatch, settings, tmp_path, data):
    Network(monkeypatch)
    command = f"cairnmind --data-dir {shlex.quote(str(data))} serve"
    for failure, reason in ((cli.ServiceDidNotStart("The service stopped at start: port 1 is in use"),
                             "The service stopped at start: port 1 is in use."),
                            (OSError(errno.EACCES, "Permission denied", str(data / "service.log")), "Permission denied")):
        def refuse(s, failure=failure):
            raise failure
        monkeypatch.setattr(cli, "ensure_service", refuse)
        with pytest.raises(RuntimeError) as caught:
            ask(external(settings, tmp_path))
        text = str(caught.value)
        assert text.startswith("The Cairnmind service is not running and could not be started: ")
        assert text.endswith(f" Start it with `{command}`, then call the tool again.")
        assert reason in text


def test_a_native_bridge_never_starts_the_service(monkeypatch, settings):
    network = Network(monkeypatch)

    def never(s):
        raise AssertionError("a native bridge must not start the service")
    monkeypatch.setattr(cli, "ensure_service", never)
    with pytest.raises(RuntimeError) as caught:
        ask(mcp_server.ServiceClient(settings))
    assert str(caught.value) == UNAVAILABLE and network.seen == ["/ask"]


@pytest.mark.parametrize("status, fail, message", [
    (503, None, "Cairnmind service rejected the request (503): "),
    (200, httpx.ReadTimeout, "Cairnmind service is unavailable (ReadTimeout); ")])
def test_a_refusal_or_a_slow_answer_starts_nothing(monkeypatch, settings, tmp_path, status, fail, message):
    network = Network(monkeypatch, status=status, fail=fail)
    network.up = True

    def never(s):
        raise AssertionError("the service answered or may have received the request: nothing to start")
    monkeypatch.setattr(cli, "ensure_service", never)
    with pytest.raises(RuntimeError) as caught:
        ask(external(settings, tmp_path))
    assert str(caught.value).startswith(message) and network.seen == ["/ask"]


def test_a_second_refused_connection_ends_with_the_unavailable_message(monkeypatch, settings, tmp_path):
    network, started = Network(monkeypatch), []
    monkeypatch.setattr(cli, "ensure_service", lambda s: started.append(s))  # "started", yet nothing listens
    with pytest.raises(RuntimeError) as caught:
        ask(external(settings, tmp_path))
    assert str(caught.value) == UNAVAILABLE
    assert len(started) == 1 and network.seen == ["/ask", "/ask"]


def test_the_service_is_started_as_ask_starts_it(monkeypatch, settings, data):
    monkeypatch.setenv("CAIRNMIND_SESSION_ID", "outer-session")
    answers = iter([False, True])
    monkeypatch.setattr(cli, "service_alive", lambda s: next(answers))
    own_time(monkeypatch, cli, sleep=lambda seconds: None)
    spawned = []

    class Process:
        def poll(self):
            return None

    def popen(command, **kwargs):
        spawned.append((command, kwargs))
        return Process()
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    mcp_server.start_service(settings)
    (command, kwargs), = spawned
    assert command == [sys.executable, "-m", "cairnmind", "--data-dir", str(settings.data_dir), "serve"]
    assert kwargs["start_new_session"] is True and kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"].name == str(data / "service.log")
    assert "CAIRNMIND_SESSION_ID" not in kwargs["env"]


@pytest.mark.parametrize("role", ["reader", "lost"])
def test_a_reader_is_not_asked_to_start_the_service_and_nothing_is_spawned(monkeypatch, settings, tmp_path, role):
    from cairnmind import store_writer
    Network(monkeypatch)
    monkeypatch.setattr(store_writer, "inspect", lambda s: {"role": role, "reason": "This installation only reads."})

    def never(s):
        raise AssertionError("a reader never runs the service")
    monkeypatch.setattr(cli, "ensure_service", never)
    with pytest.raises(RuntimeError) as caught:
        ask(external(settings, tmp_path))
    text = str(caught.value)
    assert text.startswith("This installation only reads.") and "Run `cairnmind serve`" not in text
    assert "Start it with" not in text


def test_during_an_upgrade_the_bridge_starts_nothing_and_says_why(monkeypatch, settings, tmp_path):
    from cairnmind import upgrade_mark
    network = Network(monkeypatch)
    monkeypatch.setattr(cli, "service_alive", lambda s: False)
    monkeypatch.setattr(upgrade_mark, "state", lambda: ("running", 4242))

    def never(*args, **kwargs):
        raise AssertionError("a service was started during an upgrade")
    monkeypatch.setattr(cli.subprocess, "Popen", never)
    with pytest.raises(RuntimeError) as caught:
        ask(external(settings, tmp_path))
    assert str(caught.value) == upgrade_mark.BRIDGE_UPGRADE.format(reason=upgrade_mark.NOTE_RUNNING.format(pid=4242))
    assert "cairnmind serve" not in str(caught.value)
    assert network.seen == ["/ask"]


def test_once_the_mark_is_gone_the_same_bridge_starts_the_service_again(monkeypatch, settings, tmp_path):
    from cairnmind import upgrade_mark
    network, marks = Network(monkeypatch), [("running", 4242), ("none", None)]
    monkeypatch.setattr(cli, "service_alive", lambda s: False)
    monkeypatch.setattr(upgrade_mark, "state", lambda: marks[0])

    bridge = external(settings, tmp_path)
    with pytest.raises(RuntimeError, match="upgrade of Cairnmind is in progress"):
        ask(bridge)
    marks.pop(0)  # the installer has finished
    monkeypatch.setattr(cli, "ensure_service", lambda s: setattr(network, "up", True))
    assert ask(bridge) == {"answer": "yes"}


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("loaded", ["other", None])
def test_a_bridge_whose_installed_code_changed_refuses_every_call(monkeypatch, settings, tmp_path, native, loaded):
    from cairnmind import __version__
    from cairnmind.upgrade_mark import CODE_REPLACED, CODE_UNREADABLE
    network = Network(monkeypatch)
    network.up = True
    if loaded is None:
        monkeypatch.setattr(mcp_server, "LOADED_CODE", None)
    else:
        monkeypatch.setattr(mcp_server, "code_identity", lambda: (1, 2, 3, 4))
    monkeypatch.setattr(cli, "ensure_service", lambda s: pytest.fail("a changed bridge must start nothing"))
    bridge = mcp_server.ServiceClient(settings) if native else external(settings, tmp_path)
    with pytest.raises(RuntimeError) as caught:
        ask(bridge)
    # Files that could not be read at start are not "changed": the message says what is true.
    assert str(caught.value) == (CODE_UNREADABLE if loaded is None else CODE_REPLACED.format(version=__version__))
    assert network.seen == []


def test_a_native_bridge_during_an_upgrade_does_not_advise_serve(monkeypatch, settings):
    from cairnmind import upgrade_mark
    Network(monkeypatch)  # nothing listens
    monkeypatch.setattr(upgrade_mark, "state", lambda: ("running", 4242))
    with pytest.raises(RuntimeError) as caught:
        ask(mcp_server.ServiceClient(settings))
    assert str(caught.value) == upgrade_mark.BRIDGE_UPGRADE.format(reason=upgrade_mark.NOTE_RUNNING.format(pid=4242))
    assert "cairnmind serve" not in str(caught.value)


def test_a_native_bridge_during_a_change_of_setup_names_it_and_does_not_advise_serve(monkeypatch, settings):
    from cairnmind import setup_mark
    Network(monkeypatch)  # nothing listens
    monkeypatch.setattr(setup_mark, "state", lambda data_dir: ("running", 4242))
    with pytest.raises(RuntimeError) as caught:
        ask(mcp_server.ServiceClient(settings))
    reason = setup_mark.NOTE_RUNNING.format(pid=4242, path=setup_mark.mark_path(settings.data_dir))
    assert str(caught.value) == setup_mark.BRIDGE_SETUP.format(reason=reason)
    assert "cairnmind serve" not in str(caught.value)


def test_a_tool_call_while_an_index_is_built_says_the_store_is_prepared_not_unavailable(monkeypatch, settings, tmp_path):
    from cairnmind import index_mark, process_identity, upgrade_mark
    monkeypatch.setattr(upgrade_mark, "state", lambda: ("none", None))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "cairnmind"])
    try:
        index_mark.mark_path(settings.data_dir).write_text(f"{child.pid}\n{process_identity.start(child.pid)}\n")
        sentence = index_mark.BRIDGE_INDEX.format(reason=index_mark.NOTE_RUNNING.format(pid=child.pid))
        network = Network(monkeypatch)  # nothing listens
        monkeypatch.setattr(cli, "ensure_service", lambda s: pytest.fail("a second start while the store is prepared"))
        for bridge in (mcp_server.ServiceClient(settings), external(settings, tmp_path)):  # native, external
            with pytest.raises(RuntimeError) as caught:
                ask(bridge)
            text = str(caught.value)
            assert text == sentence and "being prepared" in text and "again shortly" in text
            assert "unavailable" not in text and "resume_required" not in text and "cairnmind serve" not in text
            assert bridge.session_id is None  # the session is untouched
        assert network.seen[:1] == ["/ask"]
    finally:
        child.kill()
        child.wait()
    # the mark is gone with its builder: the ordinary answer again
    index_mark.mark_path(settings.data_dir).unlink()
    with pytest.raises(RuntimeError) as caught:
        ask(mcp_server.ServiceClient(settings))
    assert str(caught.value) == UNAVAILABLE


def test_ask_with_a_schema_imports_nothing_of_the_new_version_in_a_replaced_bridge(monkeypatch, settings):
    from cairnmind import __version__
    from cairnmind.upgrade_mark import CODE_REPLACED
    monkeypatch.setattr(mcp_server, "code_identity", lambda: (1, 2, 3, 4))
    monkeypatch.delitem(sys.modules, "cairnmind.answer_contract", raising=False)
    server = mcp_server.create_server(settings, mcp_server.ServiceClient(settings))
    arguments = {"project": "p", "question": "q", "answer_schema": {"type": "object"}}
    with pytest.raises(Exception, match="changed after this MCP server started") as caught:
        asyncio.run(server.call_tool("ask", arguments))
    assert CODE_REPLACED.format(version=__version__) in str(caught.value)
    assert "cairnmind.answer_contract" not in sys.modules


def test_parallel_calls_wait_for_one_start(monkeypatch, settings, tmp_path):
    import time
    network, active, worst, calls = Network(monkeypatch), [0], [0], []

    def slow_start(s):
        active[0] += 1
        worst[0] = max(worst[0], active[0])
        calls.append(s)
        time.sleep(0.05)
        network.up = True
        active[0] -= 1
    monkeypatch.setattr(cli, "ensure_service", slow_start)
    bridge = external(settings, tmp_path)

    async def both():
        return await asyncio.gather(*(bridge.post("/ask", {"project": "p", "question": "q"}) for _ in range(2)))
    assert asyncio.run(both()) == [{"answer": "yes"}] * 2
    assert worst[0] == 1
