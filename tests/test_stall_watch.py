"""A request of the test engine that has no answer for a while is recorded while it is still pending (#221)."""
import re
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import stall_watch

from cairnmind.config import Settings
from cairnmind.storage.arcade import ArcadeClient, ArcadeError


def make_client(tmp_path, port=2480, **options):
    settings = Settings(data_dir=tmp_path, arcade_url=f"http://127.0.0.1:{port}", arcade_database="stall_test",
                        arcade_password="synthetic-local-password")
    return settings, ArcadeClient(settings, **options)


def test_a_request_the_server_never_answers_is_captured_with_its_sockets(tmp_path, capsys):
    with socket.socket() as server:  # accepts connections (the kernel does) and never reads or answers
        server.bind(("127.0.0.1", 0))
        server.listen()
        settings, client = make_client(tmp_path, server.getsockname()[1], session_id="TX-42")
        request = stall_watch.watch(settings, ArcadeClient._request, after=0.5)
        started = time.monotonic()
        with pytest.raises(ArcadeError, match="unavailable"):
            request(client, "POST", "/api/v1/query/stall_test", timeout=2.5,
                    json={"language": "sql", "command": "SELECT FROM t WHERE a = :a", "params": {"a": "SECRET-VALUE"}})
        assert time.monotonic() - started > 2  # the capture did not end the request early
    err = capsys.readouterr().err
    assert "ENGINE REQUEST PENDING" in err
    assert "POST /api/v1/query/stall_test" in err and "SELECT FROM t WHERE a = :a" in err
    assert "SECRET-VALUE" not in err
    assert "HTTP transaction session TX-42" in err
    assert "thread dump: not captured" in err  # no engine here: said, not silent
    assert "engine.log: none" in err
    if stall_watch.shutil.which("ss"):  # the server's side holds the unread request
        assert any(int(queue) > 0 for queue in re.findall(r"^ESTAB\s+(\d+)\s", err, re.M))
    client.close()


def test_statements_are_printed_without_values():
    user = 'create user {"name": "cm", "password": "SECRET-PASSWORD", "databases": {}}'
    assert stall_watch.statement(user, "/api/v1/server") == "create user"
    insert = "INSERT INTO notes CONTENT {\"text\": \"SECRET-TEXT\"}"
    assert stall_watch.statement(insert, "/api/v1/command/db") == "INSERT INTO notes"
    assert stall_watch.statement("SELECT FROM t WHERE a = 'SECRET-TEXT' AND b = :b", "/api/v1/query/db") == \
        "SELECT FROM t WHERE a = ? AND b = :b"


@pytest.mark.skipif(not Path("/proc/net/tcp").exists(), reason="the kernel's socket table is a Linux file")
def test_without_ss_the_proc_table_is_printed(monkeypatch, tmp_path):
    monkeypatch.setattr(stall_watch.shutil, "which", lambda name: None)
    monkeypatch.setattr(stall_watch.sys, "platform", "linux")
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        text = stall_watch.sockets(port)
    assert "/proc/net/tcp" in text and f":{port:04X} " in text


def test_macos_says_that_sockets_are_not_captured(monkeypatch):
    monkeypatch.setattr(stall_watch.sys, "platform", "darwin")
    assert "not captured" in stall_watch.sockets(2480)


def test_a_fast_request_records_nothing_and_leaves_no_thread(tmp_path, capsys, recwarn):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"result": []}))
    settings, client = make_client(tmp_path, transport=transport)
    request = stall_watch.watch(settings, ArcadeClient._request, after=0.5)
    threads = threading.active_count()
    assert request(client, "GET", "/api/v1/databases").status_code == 200
    assert threading.active_count() == threads
    time.sleep(0.8)  # past the stall time: a cancelled watch must not fire
    assert capsys.readouterr().err == ""
    assert not recwarn.list


def test_a_late_answer_is_marked_as_a_slow_request(tmp_path, capsys):
    def slow(request):
        time.sleep(1)
        return httpx.Response(200, json={"result": []})

    settings, client = make_client(tmp_path, transport=httpx.MockTransport(slow))
    request = stall_watch.watch(settings, ArcadeClient._request, after=0.3)
    with pytest.warns(UserWarning, match="SLOW REQUEST, answered after") as seen:
        assert request(client, "GET", "/api/v1/databases").status_code == 200
    assert "GET /api/v1/databases" in str(seen[0].message)
    assert "ENGINE REQUEST PENDING" in capsys.readouterr().err


def test_one_thread_dump_for_requests_that_stall_together(tmp_path, monkeypatch):
    taken = []
    monkeypatch.setattr(stall_watch, "engine_threads", lambda log: taken.append(log) or "THREADS")
    log = tmp_path / "engine.log"
    assert stall_watch.thread_dump(log) == "THREADS"
    assert "not taken" in stall_watch.thread_dump(log)  # a second worker, a moment later
    assert len(taken) == 1


def test_late_answer_inside_clirunner_does_not_corrupt_json_output(tmp_path):
    import json

    import typer
    from typer.testing import CliRunner

    def slow(request):
        time.sleep(0.35)
        return httpx.Response(200, json={"result": []})

    settings, client = make_client(tmp_path, transport=httpx.MockTransport(slow))
    request = stall_watch.watch(settings, ArcadeClient._request, after=0.1)
    app = typer.Typer()

    @app.command()
    def cmd():
        request(client, "GET", "/api/v1/databases")
        print(json.dumps({"ok": True}))

    with pytest.warns(UserWarning, match="SLOW REQUEST"):
        result = CliRunner().invoke(app)
    assert result.exit_code == 0
    assert json.loads(result.output) == {"ok": True}
