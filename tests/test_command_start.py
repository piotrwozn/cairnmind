"""A command that only talks to the running service starts fast: it imports neither the service nor the web
framework, and asks /health once before its request (#405)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cairnmind import __version__, cli
from cairnmind.config import Settings
from cairnmind.service_version import instance_id
from test_other_version import OLD, forbidden, invoke, isolated, old_service  # noqa: F401 - isolated: autouse

HEAVY = ("cairnmind.service", "fastapi", "uvicorn", "starlette")
RUN = """
import json, sys
from cairnmind import cli
sys.argv = ["cairnmind", "--data-dir", sys.argv[1], *sys.argv[2:]]
try:
    cli.entrypoint()
except SystemExit:
    pass
print(json.dumps([name for name in %r if name in sys.modules]), file=sys.stderr)
""" % (HEAVY,)


class StubService:
    """The knowledge service as far as a command sees it: /health, /projects and one answer to everything else."""

    def __init__(self, tmp_path):
        self.settings = Settings.load(tmp_path / "data", create=True)
        self.seen: list[str] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                stub.seen.append(f"GET {self.path.split('?')[0]}")
                self.reply({"service": "cairnmind", "version": __version__, "instance": instance_id(stub.settings)}
                           if self.path == "/health" else {"status": "ok", "running": True})

            def do_POST(self):
                stub.seen.append(f"POST {self.path}")
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.reply({"status": "unavailable", "answer": "", "matches": [], "sources": []})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.settings.port = self.server.server_address[1]
        self.settings.save()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def run(self, *arguments):
        env = {key: value for key, value in os.environ.items() if not key.startswith(("CAIRNMIND_", "DEVBRAIN_"))}
        done = subprocess.run([sys.executable, "-c", RUN, str(self.settings.data_dir), *arguments],
                              capture_output=True, text=True, env=env, timeout=60)
        return done, json.loads(done.stderr.strip().splitlines()[-1])

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub(tmp_path):
    service = StubService(tmp_path)
    yield service
    service.close()


@pytest.mark.parametrize("arguments", [["ask", "demo", "a question", "--json"], ["status"], ["context", "demo"]])
def test_a_command_that_asks_the_service_does_not_import_it_or_the_web_framework(stub, arguments):
    done, loaded = stub.run(*arguments)
    assert done.returncode == 0, done.stderr
    assert loaded == [], f"the command imported {loaded}"
    assert stub.seen, "the command never reached the service"


def test_the_bridge_module_does_not_import_the_service_or_the_web_framework():
    # starlette and uvicorn are the MCP library's own imports, not ours
    code = f"import sys, cairnmind.mcp_server; print([n for n in {HEAVY[:2]!r} if n in sys.modules])"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "[]", done.stdout + done.stderr


def test_one_ask_is_one_health_request_and_the_request(stub):
    done, _ = stub.run("ask", "demo", "a question", "--json")
    assert done.returncode == 0, done.stderr
    assert stub.seen == ["GET /health", "POST /ask"]


def test_an_ask_of_a_service_of_another_version_still_sends_nothing_after_one_health(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    seen = old_service(monkeypatch, settings, version=OLD)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    result = invoke(settings, "ask", "demo", "a question")
    assert result.exit_code == 2, result.output
    assert [path for _, path, _ in seen] == ["/health"]


def test_a_service_that_answers_is_asked_once_by_a_command_in_this_process(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    seen = old_service(monkeypatch, settings, version=__version__)
    invoke(settings, "ask", "demo", "a question")
    assert [path for _, path, _ in seen] == ["/health", "/ask"]


def test_a_second_command_asks_again_and_a_stale_answer_is_not_reused(tmp_path, monkeypatch):
    """The answer service_alive took is used once, by the version check right after it, and not by a later one."""
    settings = Settings.load(tmp_path / "data", create=True)
    seen = old_service(monkeypatch, settings, version=__version__)
    assert cli.service_alive(settings) is True
    from cairnmind import service_version
    assert service_version.refusal(settings) is None and len(seen) == 1
    assert service_version.refusal(settings) is None and len(seen) == 2  # used once: the next one asks
    cli.service_alive(settings)
    monkeypatch.setattr(service_version.time, "monotonic", lambda: 10 ** 9)  # a second later
    assert service_version.refusal(settings) is None and len(seen) == 4


def test_an_unknown_project_is_still_refused_before_a_service_is_started(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    monkeypatch.setattr(cli, "service_alive", lambda settings: False)
    monkeypatch.setattr(cli, "store_for_read", lambda settings: type("S", (), {
        "resolve_project": staticmethod(lambda name: (_ for _ in ()).throw(ValueError("Unknown project: nope")))})())
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    result = invoke(settings, "ask", "nope", "a question")
    assert result.exit_code == 1 and "Unknown project: nope" in str(result.exception)


def test_a_service_that_is_up_is_not_asked_about_the_project_first(tmp_path, monkeypatch):
    settings = Settings.load(tmp_path / "data", create=True)
    seen = old_service(monkeypatch, settings, version=__version__)
    monkeypatch.setattr(cli, "store_for_read", forbidden)
    invoke(settings, "ask", "demo", "a question")
    assert [path for _, path, _ in seen] == ["/health", "/ask"]
