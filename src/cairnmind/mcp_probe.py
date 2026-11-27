"""Stdio JSON-RPC handshake probe for `cairnmind mcp test-connection` and `cairnmind doctor --check-mcp`.

Spawns the exact MCP command & environment from a preset in an isolated subprocess,
sends `initialize` + `notifications/initialized` + `tools/list`, enforces a strict
clean-stdout check (failing hard on any non-JSON-RPC byte), measures latency, and
cleanly terminates the child process on completion or timeout.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
from typing import Any

from . import __version__
from .config import Settings, env_names
from .database_mode import describe as describe_storage

EXPECTED_CORE_TOOLS = frozenset({
    "ask",
    "remember",
    "checkpoint",
    "save_plan",
    "next_task",
    "resume_context",
    "ack_resume",
    "check_status",
})

_LAUNCHER_VARS = tuple(
    key for name in ("SESSION_ID", "PACKET_ID", "PROJECT", "CLIENT", "CONTEXT_FILE") for key in env_names(name)
)


def _clean_env(extra: dict[str, str] | None = None, *, strip_launcher: bool = True) -> dict[str, str]:
    env = dict(os.environ)
    if strip_launcher:
        for key in _LAUNCHER_VARS:
            env.pop(key, None)
    if extra:
        for key, value in extra.items():
            # Expand `${VAR:-}` shell-style placeholders used by Claude Code project presets.
            if isinstance(value, str) and value.startswith("${") and value.endswith(":-}"):
                var_name = value[2:-3]
                env[key] = os.environ.get(var_name, "")
            elif isinstance(value, str):
                env[key] = value
    return env


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except OSError:
        pass
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def _reader_thread(stream, out_queue: queue.Queue[bytes | None]) -> None:
    try:
        while True:
            line = stream.readline()
            if not line:
                out_queue.put(None)
                return
            out_queue.put(line)
    except OSError:
        out_queue.put(None)


def _read_jsonrpc_message(
    out_queue: queue.Queue[bytes | None],
    deadline: float,
    expected_id: int,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Read lines from stdout until message with `expected_id` arrives.

    Returns (message, error_text, stdout_clean). Fails immediately if any non-JSON-RPC
    byte/line appears on stdout.
    """
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "Timed out waiting for JSON-RPC response from MCP server.", True
        try:
            raw = out_queue.get(timeout=remaining)
        except queue.Empty:
            return None, "Timed out waiting for JSON-RPC response from MCP server.", True
        if raw is None:
            return None, "MCP server closed stdout before completing JSON-RPC handshake.", True
        stripped = raw.strip()
        if not stripped:
            # Bare empty lines before/outside JSON-RPC frames corrupt strict stdio framing.
            return None, "Dirty stdout: unexpected empty line outside JSON-RPC frame.", False
        try:
            text = stripped.decode("utf-8")
            frame = json.loads(text)
        except (UnicodeDecodeError, ValueError):
            preview = stripped[:160].decode("utf-8", errors="replace")
            return None, f"Dirty stdout: non-JSON-RPC output detected before/during handshake: {preview!r}", False
        if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
            preview = stripped[:160].decode("utf-8", errors="replace")
            return None, f"Dirty stdout: non-JSON-RPC frame on stdout: {preview!r}", False
        if frame.get("id") == expected_id:
            return frame, None, True
        # Valid JSON-RPC notification from server (e.g. logging/progress) — keep reading.


def probe_mcp_stdio(
    command: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
    timeout: float = 5.0,
    settings: Settings | None = None,
    project_root: Path | None = None,
    strip_launcher: bool = True,
) -> dict[str, Any]:
    """Run a live stdio MCP JSON-RPC `initialize` + `tools/list` probe with strict stdout validation."""
    started = time.perf_counter()
    deadline = time.monotonic() + max(0.5, float(timeout))
    storage_info = describe_storage(settings) if settings is not None else {}
    backend = "arcadedb"
    backend_mode = (
        f"managed_{storage_info.get('engine', 'native')}"
        if storage_info.get("mode") == "managed"
        else storage_info.get("mode", "unknown")
    )

    result: dict[str, Any] = {
        "ok": False,
        "latency_ms": 0,
        "stdout_clean": True,
        "command": command,
        "args": list(args),
        "project": str(project_root.expanduser().resolve()) if project_root else None,
        "backend": backend,
        "backend_mode": backend_mode,
        "protocol_version": None,
        "server_info": None,
        "tools": [],
        "tools_count": 0,
        "stderr_preview": "",
        "error": None,
    }

    proc: subprocess.Popen[bytes] | None = None
    stderr_lines: list[bytes] = []

    def _collect_stderr(stream) -> None:
        try:
            total = 0
            while total < 8192:
                chunk = stream.readline(1024)
                if not chunk:
                    break
                stderr_lines.append(chunk)
                total += len(chunk)
        except OSError:
            pass

    try:
        proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            env=_clean_env(env, strip_launcher=strip_launcher),
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        result["error"] = f"Failed to start MCP server process: {exc}"
        result["latency_ms"] = max(1, int(round((time.perf_counter() - started) * 1000)))
        return result

    out_queue: queue.Queue[bytes | None] = queue.Queue()
    t_out = threading.Thread(target=_reader_thread, args=(proc.stdout, out_queue), daemon=True)
    t_err = threading.Thread(target=_collect_stderr, args=(proc.stderr,), daemon=True)
    t_out.start()
    t_err.start()

    try:
        assert proc.stdin is not None
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cairnmind-mcp-probe", "version": __version__},
            },
        }
        proc.stdin.write((json.dumps(init_req, separators=(",", ":")) + "\n").encode("utf-8"))
        proc.stdin.flush()

        init_resp, err, clean = _read_jsonrpc_message(out_queue, deadline, expected_id=1)
        result["stdout_clean"] = clean
        if err or init_resp is None:
            result["error"] = err
            return result
        if "error" in init_resp:
            result["error"] = f"MCP initialize failed: {init_resp['error']}"
            return result

        init_result = init_resp.get("result") or {}
        result["protocol_version"] = init_result.get("protocolVersion")
        result["server_info"] = init_result.get("serverInfo")

        # Send initialized notification and request tools/list.
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        proc.stdin.write((json.dumps(notif, separators=(",", ":")) + "\n").encode("utf-8"))
        proc.stdin.write((json.dumps(list_req, separators=(",", ":")) + "\n").encode("utf-8"))
        proc.stdin.flush()

        tools_resp, err, clean = _read_jsonrpc_message(out_queue, deadline, expected_id=2)
        result["stdout_clean"] = result["stdout_clean"] and clean
        if err or tools_resp is None:
            result["error"] = err
            return result
        if "error" in tools_resp:
            result["error"] = f"MCP tools/list failed: {tools_resp['error']}"
            return result

        tools_raw = (tools_resp.get("result") or {}).get("tools") or []
        tool_names = sorted(t["name"] for t in tools_raw if isinstance(t, dict) and isinstance(t.get("name"), str))
        result["tools"] = tool_names
        result["tools_count"] = len(tool_names)
        missing_core = sorted(EXPECTED_CORE_TOOLS - set(tool_names))
        if missing_core:
            result["error"] = f"MCP server is missing core Cairnmind tools: {', '.join(missing_core)}"
            return result
        result["ok"] = True
        return result
    except BrokenPipeError:
        result["error"] = "MCP server terminated unexpectedly during handshake."
        return result
    finally:
        _terminate_process(proc)
        t_err.join(timeout=0.3)
        t_out.join(timeout=0.3)
        if proc.stdout:
            try:
                proc.stdout.close()
            except OSError:
                pass
        if proc.stderr:
            try:
                proc.stderr.close()
            except OSError:
                pass
        result["stderr_preview"] = b"".join(stderr_lines).decode("utf-8", errors="replace").strip()[:500]
        if not result["ok"] and result["stderr_preview"] and result["error"] and "Dirty stdout" not in result["error"]:
            result["error"] = f"{result['error']} (stderr: {result['stderr_preview'][:200]})"
        result["latency_ms"] = max(1, int(round((time.perf_counter() - started) * 1000)))
