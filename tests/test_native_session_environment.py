"""Exercise native Codex MCP stdio environment filtering without a model turn."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tomllib

import pytest

from cairnmind.config import Settings
from cairnmind import integrations


@pytest.mark.parametrize("forward_session", [False, True])
def test_native_codex_mcp_stdio_receives_only_explicit_launch_environment(tmp_path, forward_session):
    binary = shutil.which("codex")
    if not binary:
        pytest.skip("Native Codex is not installed")
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)", version.stdout)
    if not match or tuple(map(int, match.groups())) < (0, 166, 0):
        pytest.skip("Native transport regression requires Codex >=0.166.0")
    settings = Settings.load(tmp_path / "memory", create=True)
    config_path = tmp_path / ".codex/config.toml"
    integrations._merge_toml_mcp(config_path, settings)
    server = tomllib.loads(config_path.read_text())["mcp_servers"]["cairnmind"]
    capture = tmp_path / "capture.json"
    bridge = tmp_path / "capture_mcp.py"
    bridge.write_text('''import json, os, sys
from pathlib import Path
names = ("DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID", "DEVBRAIN_CONTEXT_FILE")
Path(sys.argv[1]).write_text(json.dumps({name: os.environ.get(name) for name in names}))
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request.get("method") == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "environment-fixture", "version": "1"}}
    else:
        result = {"tools": []}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
''')
    # Use the generated allowlist with a disposable discovery-only MCP server.
    # initialize + status/list causes real stdio spawn but sends no model turn,
    # performs no model inference and has no access to the user's auth/config.
    allowlist = server["env_vars"] if forward_session else []
    configuration = ('mcp_servers={cairnmind={command=' + json.dumps(sys.executable)
                     + ',args=' + json.dumps([str(bridge), str(capture)])
                     + ',env_vars=' + json.dumps(allowlist) + ',startup_timeout_sec=4}}')
    environment = dict(os.environ)
    codex_home = tmp_path / "isolated-codex-home"
    codex_home.mkdir()
    environment.update(CODEX_HOME=str(codex_home),
                       DEVBRAIN_SESSION_ID="native-fixture-launch-session",
                       DEVBRAIN_PACKET_ID="native-fixture-packet",
                       DEVBRAIN_CONTEXT_FILE="must-not-leak-through-allowlist")
    output: queue.Queue = queue.Queue()
    with (tmp_path / "codex-stderr.txt").open("w+") as stderr:
        process = subprocess.Popen([binary, "--no-daemon", "app-server", "--stdio", "-c", configuration],
                                   cwd=tmp_path, env=environment, text=True,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)

        def read_output():
            for line in process.stdout:
                output.put(json.loads(line))
            output.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()

        def send(message):
            process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
            process.stdin.flush()

        def response(identifier):
            while True:
                message = output.get(timeout=15)
                assert message is not None, "Native app-server exited before replying"
                if message.get("id") == identifier:
                    assert "error" not in message, message
                    return message["result"]

        try:
            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "devbrain-environment-test", "version": "1"},
                "capabilities": {"experimentalApi": True}}})
            response(1)
            send({"method": "initialized"})
            send({"id": 2, "method": "mcpServerStatus/list", "params": {
                "serverName": "cairnmind", "detail": "toolsAndAuthOnly"}})
            result = response(2)
            assert len(result["data"]) == 1
            assert result["data"][0]["serverInfo"]["name"] == "environment-fixture"
            observed = json.loads(capture.read_text())
            assert observed == {
                "DEVBRAIN_SESSION_ID": environment["DEVBRAIN_SESSION_ID"] if forward_session else None,
                "DEVBRAIN_PACKET_ID": environment["DEVBRAIN_PACKET_ID"] if forward_session else None,
                "DEVBRAIN_CONTEXT_FILE": None,
            }
        finally:
            process.stdin.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=2)
            process.stdout.close()
