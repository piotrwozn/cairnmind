#!/usr/bin/env python3
"""Capture real native clients and real DevBrain evidence in a disposable project.

The MCP gateway intentionally exposes only this demo's project. It delegates
resume/ACK, records, plans and verification to the shipped DevBrain classes.
It does not use Ollama, replace provider replies, or implement a fake database.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from devbrain.config import Settings
from devbrain.evaluation import _native_output
from devbrain.agent import parse_object
from devbrain.plans import PlanManager
from devbrain.sessions import SessionManager
from devbrain.store import Store
from devbrain.verification import capture_code_state, run_check, verification_status

HERE = Path(__file__).resolve().parent
TOOLS = ("resume_context", "ack_resume", "memory", "read_source", "remember_decision")


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          text=True, capture_output=True).stdout.strip()


def setup(output: Path, base: Settings) -> tuple[Settings, Store, dict, dict]:
    """Never reset a supplied directory or reuse another project's database."""
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory must be new or empty; existing captures are never overwritten")
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    root = output / "checkout"
    shutil.copytree(HERE / "sample", root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (root / ".devbrain").mkdir()
    (root / ".devbrain/checks.toml").write_text(
        "[checks.unit]\nargv = " + json.dumps([sys.executable, "-m", "unittest", "-v", "test_checkout"])
        + '\ncwd = "."\ntimeout_seconds = 15\n', encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.name", "DevBrain demo")
    git(root, "config", "user.email", "demo@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-qm", "Checkout sample before hand-off")
    settings = replace(base, data_dir=output / "memory", arcade_database="devbrain_demo_" + uuid4().hex,
                       token=secrets.token_urlsafe(32))
    settings.save()
    store = Store(settings.data_dir, settings=settings)
    project = store.add_project("Checkout demo", root)
    source = store.snapshot(project["id"], "sample://DESIGN.md", (root / "DESIGN.md").read_text(),
                            "document", {"origin": "checked_in_sample", "path": str(root / "DESIGN.md")})
    (output / "evidence").mkdir()
    write_json(output / "binding.json", {"project_id": project["id"], "source_id": source["id"]})
    return settings, store, project, source


class DemoGateway:
    """Session-bound operations; every successful tool call is retained locally."""

    def __init__(self, output: Path, session_id: str):
        self.output = output.resolve()
        self.settings = Settings.load(self.output / "memory")
        if not self.settings.arcade_database.startswith("devbrain_demo_"):
            raise ValueError("Demo gateway requires its isolated database namespace")
        self.store = Store(self.settings.data_dir, settings=self.settings)
        self.sessions = SessionManager(self.store, self.settings)
        self.binding = json.loads((self.output / "binding.json").read_text())
        self.session_id = session_id
        self.session = self.sessions.status(session_id)
        if self.session["project_id"] != self.binding["project_id"]:
            raise ValueError("Session is outside this demo project")

    def record(self, tool: str, arguments: dict, result: dict) -> dict:
        event = {"session_id": self.session_id, "client": self.session["client"], "tool": tool,
                 "arguments": arguments, "result": result, "time": datetime.now(timezone.utc).isoformat()}
        with (self.output / "evidence/tool-calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return result

    def resume_context(self) -> dict:
        return self.record("resume_context", {}, self.sessions.resume_context(self.session_id))

    def ack_resume(self, packet_id: str, packet_hash: str, readback: dict) -> dict:
        arguments = {"packet_id": packet_id, "packet_hash": packet_hash, "readback": readback}
        return self.record("ack_resume", arguments, self.sessions.ack(self.session_id, **arguments))

    def memory(self) -> dict:
        self.sessions.require_ack(self.session_id)
        return self.record("memory", {}, {"project_id": self.binding["project_id"],
            "requirements_source_id": self.binding["source_id"],
            "records": self.store.list_records(self.binding["project_id"])})

    def read_source(self, source_id: str) -> dict:
        self.sessions.require_ack(self.session_id)
        source = self.store.get_source(source_id)
        if source["project_id"] != self.binding["project_id"]:
            raise ValueError("Source is outside this demo project")
        return self.record("read_source", {"source_id": source_id},
                           {key: source[key] for key in ("id", "uri", "content", "sha256")})

    def remember_decision(self, decision: str, source_id: str, quote: str, task_title: str) -> dict:
        if self.session["client"] != "codex":
            raise ValueError("The recall phase is read-only")
        if not 1 <= len(decision) <= 160 or not 1 <= len(task_title) <= 100:
            raise ValueError("Use a short decision (160 characters) and task title (100 characters)")
        if source_id != self.binding["source_id"]:
            raise ValueError("Cite the sample requirements source")
        source = self.store.get_source(source_id)
        if not quote.strip() or quote not in source["content"]:
            raise ValueError("The quote must occur verbatim in the archived source")
        with self.store.transaction():
            self.sessions.require_ack(self.session_id)
            if self.store.list_records(self.binding["project_id"]):
                raise ValueError("This demo decision has already been saved; read memory instead")
            decision_record = self.store.remember(self.binding["project_id"], {
                "kind": "decision", "key": "money-representation", "content": decision,
                "status": "proposed", "metadata": {"origin": "native_codex_demo", "quote": quote}}, [source_id])
            saved = PlanManager(self.store).save(self.binding["project_id"], {
                "title": "Checkout correctness", "goal": task_title, "constraints": [decision]},
                [{"key": "verify-checkout", "content": task_title,
                  "metadata": {"requires_test": True, "required_profiles": ["unit"],
                               "acceptance_criteria": [quote]}}],
                "native-demo-decision", worktree=self.session["worktree"], activate=True)
        return self.record("remember_decision", {"decision": decision, "source_id": source_id,
            "quote": quote, "task_title": task_title},
            {"decision": decision_record, "task": saved["tasks"][0], "source_id": source_id, "quote": quote})


def mcp(output: Path, session_id: str) -> None:
    from mcp.server.fastmcp import FastMCP

    gateway = DemoGateway(output, session_id)
    server = FastMCP("devbrain_demo", instructions="Call resume_context, read it, then ACK exact IDs and versions.")
    for name in TOOLS:
        server.tool()(getattr(gateway, name))
    server.run(transport="stdio")


def native(client: str, output: Path, session: dict, prompt: str) -> dict:
    if session.get("status") != "ready":
        raise RuntimeError(f"{client} resume packet is not ready; no provider process was started")
    executable = shutil.which(client)
    if not executable:
        raise RuntimeError(f"Install and log in to {client} before recording")
    environment = dict(os.environ)
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "DEVBRAIN_ARCADE_DATABASE", "DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID", "DEVBRAIN_PROJECT"):
        environment.pop(key, None)
    auth = subprocess.run([executable, *( ["login", "status"] if client == "codex" else ["auth", "status", "--json"])],
                          capture_output=True, text=True, timeout=20, env=environment)
    if client == "codex":
        authenticated = "chatgpt" in (auth.stdout + auth.stderr).lower()
    else:
        auth_info = json.loads(auth.stdout or "{}")
        authenticated = auth_info.get("loggedIn") is True and auth_info.get("authMethod") in {"claude.ai", "oauth"}
    if auth.returncode or not authenticated:
        raise RuntimeError(f"Log in to {client} in this Linux/WSL environment first")
    server = {"command": sys.executable, "args": [str(Path(__file__).resolve()), "mcp", "--output", str(output),
              "--session-id", session["session_id"]], "env": {"PYTHONPATH": str(HERE.parent / "src")}}
    if client == "codex":
        policies = "{" + ",".join(json.dumps(name) + '={approval_mode="approve"}' for name in TOOLS) + "}"
        configuration = ("mcp_servers={devbrain_demo={command=" + json.dumps(server["command"])
            + ",args=" + json.dumps(server["args"]) + ",env={PYTHONPATH=" + json.dumps(server["env"]["PYTHONPATH"])
            + "},enabled_tools=" + json.dumps(list(TOOLS)) + ",tools=" + policies + "}}")
        argv = [executable, "--no-daemon", "exec", "--ignore-user-config", "--strict-config", "--sandbox",
                "read-only", "--ephemeral", "--json", "--cd", str(output / "checkout"), "-c",
                'approval_policy="never"', "-c", configuration, prompt]
    else:
        config = output / "memory/native-mcp.json"
        write_json(config, {"mcpServers": {"devbrain_demo": server}})
        argv = [executable, "--strict-mcp-config", "--mcp-config", str(config), "--tools", "", "--allowedTools",
                *["mcp__devbrain_demo__" + name for name in TOOLS if name != "remember_decision"],
                "--permission-mode", "dontAsk", "--no-session-persistence", "--output-format", "json", "-p", prompt]
    print(f"Running actual {client} session...", flush=True)
    started = time.monotonic()
    completed = subprocess.run(argv, cwd=output / "checkout", env=environment, capture_output=True,
                               text=True, timeout=360)
    evidence = output / "evidence"
    (evidence / f"{client}.stdout").write_text(completed.stdout, encoding="utf-8")
    (evidence / f"{client}.stderr").write_text(completed.stderr, encoding="utf-8")
    write_json(evidence / f"{client}.invocation.json", {"argv": argv, "returncode": completed.returncode,
              "elapsed_seconds": round(time.monotonic() - started, 3)})
    if completed.returncode:
        raise RuntimeError(f"{client} exited {completed.returncode}; inspect the private evidence directory")
    answer, usage = _native_output(client, completed.stdout)
    return {"answer": parse_object(answer), "usage": usage, "session_id": session["session_id"],
            "elapsed_seconds": round(time.monotonic() - started, 3)}


def validate_native(output: Path, client: str, result: dict, decision: dict, task: dict, source: dict) -> None:
    """Require actual gateway calls and exact DB-backed recall, not fluent final text."""
    events = [json.loads(line) for line in (output / "evidence/tool-calls.jsonl").read_text().splitlines()]
    calls = [row for row in events if row["session_id"] == result["session_id"] and row["client"] == client]
    required = {"resume_context", "ack_resume", "memory", "read_source"}
    if client == "codex":
        required.add("remember_decision")
    if not required <= {row["tool"] for row in calls}:
        raise ValueError(f"{client} did not complete the required real tool calls")
    if not any(row["tool"] == "read_source" and row["result"]["id"] == source["id"] for row in calls):
        raise ValueError(f"{client} did not read the requirements source")
    answer = result["answer"]
    for field, expected in (("decision", decision["content"]), ("task_id", task["id"]), ("source_id", source["id"])):
        if answer.get(field) != expected:
            raise ValueError(f"{client} returned the wrong {field}")
    if not answer.get("quote") or answer["quote"] not in source["content"]:
        raise ValueError(f"{client} did not return a literal source quote")


def check_and_change(settings: Settings, store: Store, project: dict, task: dict) -> dict:
    root = Path(project["root"])
    run = run_check(store, settings, project["id"], "unit", task["id"], root)
    fresh = verification_status(store, task, root)
    if not run.get("passed") or fresh["status"] != "fresh":
        raise ValueError("The actual unit check did not produce fresh evidence")
    code = root / "checkout.py"
    before = code.read_text()
    if before.count("return sum(prices)") != 1:
        raise ValueError("Unexpected sample code; refusing to edit")
    code.write_text(before.replace("return sum(prices)", "return sum(prices) + 1"))
    stale = verification_status(store, task, root)
    if stale["status"] != "stale":
        raise ValueError("Code mutation did not invalidate the earlier evidence")
    return {"run": run, "fresh": fresh, "stale": stale,
            "current_code": capture_code_state(store, project["id"], root),
            "diff": git(root, "diff", "--", "checkout.py"),
            "stdout": store.get_source(run["source_ids"]["stdout"])["content"],
            "stderr": store.get_source(run["source_ids"]["stderr"])["content"]}


def capture_document(codex: dict, claude: dict, decision: dict, task: dict, source: dict, checks: dict) -> dict:
    def scene(key, title, subtitle, command, lines, duration, agent=None):
        chapters = {"intro": "01 / OVERVIEW", "codex": "02 / DECIDE", "handoff": "03 / HAND OFF",
                    "recall": "04 / RECALL", "fresh": "05 / FRESH", "change": "06 / CHANGE", "stale": "07 / STALE"}
        return {"id": key, "title": title, "subtitle": subtitle, "command": command,
                "lines": lines, "duration": duration, "agent": agent, "chapter": chapters[key],
                "focus_line": {"recall": 2, "stale": 0}.get(key, max(0, len(lines) - 1))}

    return {"schema_version": 1, "title": "DevBrain", "duration": 38, "scenes": [
        scene("intro", "Requirements", "", "cat DESIGN.md", source["content"].splitlines(), 3),
        scene("codex", "Make the decision once.", "Actual Codex session · source-backed memory", "Codex / remember_decision",
              [decision["content"], {"text": "Saved in DevBrain", "tone": "green"},
               "Task: " + task["content"], "Source: " + source["uri"]], 6, "Codex"),
        scene("handoff", "End the session. Keep the context.", "Codex process exited · a new Claude Code process starts", "Claude Code / resume_context → ack_resume",
              ["Active task: " + task["content"], "Task version: " + str(task["version"]),
               {"text": "Exact packet and task version acknowledged", "tone": "green"}], 5, "Claude Code"),
        scene("recall", "The next agent can cite it.", "Actual Claude Code reply · no answer in the prompt", "Claude Code / memory → read_source",
              [claude["answer"]["decision"], '"' + claude["answer"]["quote"] + '"',
               "Source: " + source["uri"], "Task: " + claude["answer"]["task_id"]], 7, "Claude Code"),
        scene("fresh", "Passing, and current.", "DevBrain ran the unit profile against this code", "DevBrain / run_check(profile='unit')",
              [line for line in (checks["stdout"] + checks["stderr"]).splitlines() if line.strip()]
              + [{"text": "verification: " + checks["fresh"]["status"], "tone": "green"}], 6),
        scene("change", "One line changes.", "An intentional regression in the sample checkout", "git diff -- checkout.py",
              [{"text": line, "tone": "amber" if line.startswith("+") else "plain"}
               for line in checks["diff"].splitlines() if line.startswith(("-    ", "+    "))], 4),
        scene("stale", "Yesterday's green is not today's proof.", "The old passing run remains in history", "DevBrain / verification_status(task)",
              [{"text": "verification: " + checks["stale"]["status"], "tone": "amber"},
               *checks["stale"]["reasons"], "Run the check again before trusting this revision."], 7),
    ], "provenance": {"mode": "real_native_clients", "captured_at": datetime.now(timezone.utc).isoformat(),
        "codex": {"status": "passed", "elapsed_seconds": codex["elapsed_seconds"]},
        "claude": {"status": "passed", "elapsed_seconds": claude["elapsed_seconds"]},
        "gateway": "Demo-scoped MCP adapter over shipped DevBrain Store, SessionManager and PlanManager.",
        "sample_source_sha256": source["sha256"],
        "check_run_id": checks["run"]["id"], "check_returncode": checks["run"]["returncode"],
        "fresh_fingerprint": checks["run"]["code_before"]["fingerprint"],
        "changed_fingerprint": checks["current_code"]["fingerprint"]}}


def write_cast(path: Path, capture: dict) -> None:
    """Write the terminal demo in standard asciinema v2 format."""
    header = {"version": 2, "width": 110, "height": 22, "title": "DevBrain terminal demo",
              "env": {"TERM": "xterm-256color", "SHELL": "/bin/sh"}}
    lines, elapsed = [json.dumps(header, ensure_ascii=False)], 0.0
    for scene in capture["scenes"]:
        text = "\x1b[0m\x1b[38;2;51;255;102;48;2;0;0;0m\x1b[2J\x1b[H"
        if scene["command"]:
            text += "> " + scene["command"] + "\r\n\r\n"
        text += "\r\n".join(row if isinstance(row, str) else row["text"] for row in scene["lines"]) + "\r\n"
        lines.append(json.dumps([elapsed, "o", text], ensure_ascii=False))
        elapsed += scene["duration"]
    lines.append(json.dumps([elapsed, "o", "\r\n"]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def live(output: Path, base: Settings) -> dict:
    from devbrain.engine import ensure_engine

    ensure_engine(base)
    settings, store, project, source = setup(output, base)
    output = output.resolve()
    sessions = SessionManager(store, settings)
    common = ("Use ONLY the devbrain_demo MCP tools. Do not use shell, browse, or read files. "
              "Call resume_context, read the full packet, then ack_resume with exact packet ID/hash and "
              "readback {plan:{id,version}|null,task:{id,version}|null}. Then call memory and read_source. ")
    codex = native("codex", output, sessions.prepare(project["id"], "codex"), common +
        "Read the sample requirements. Decide how checkout money should be represented and save it with "
        "remember_decision. Keep the decision under 160 characters and task_title under 100 characters. "
        "Use a literal requirements quote and make the task about verifying checkout correctness. "
        "After saving, return ONLY JSON with decision (exact saved text), task_id, source_id, quote.")
    records = store.list_records(project["id"])
    decision = next(row for row in records if row["kind"] == "decision")
    task = next(row for row in records if row["kind"] == "task")
    validate_native(output, "codex", codex, decision, task, source)
    claude = native("claude", output, sessions.prepare(project["id"], "claude"), common +
        "Recover the decision and active task left by the previous agent. Read the decision's source. "
        "Do not change memory. Return ONLY JSON with decision (exact saved content), task_id, source_id, "
        "quote (a verbatim excerpt supporting the decision). The answer must come from DevBrain.")
    validate_native(output, "claude", claude, decision, task, source)
    checks = check_and_change(settings, store, project, task)
    write_json(output / "evidence/results.json", {"codex": codex, "claude": claude, "decision": decision,
               "task": task, "source": source, "checks": checks})
    capture = capture_document(codex, claude, decision, task, source, checks)
    write_json(output / "capture.json", capture)
    write_cast(output / "demo.cast", capture)
    return {"status": "passed", "capture": str(output / "capture.json"),
            "cast": str(output / "demo.cast"), "fresh": checks["fresh"]["status"], "stale": checks["stale"]["status"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["live", "mcp"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, help="Existing DevBrain engine settings; default: DEVBRAIN_HOME")
    parser.add_argument("--session-id")
    args = parser.parse_args()
    if args.mode == "mcp":
        if not args.session_id:
            parser.error("mcp requires --session-id")
        mcp(args.output, args.session_id)
    else:
        if os.name != "posix":
            parser.error("Run inside Linux or WSL2, like DevBrain itself")
        result = live(args.output, Settings.load(args.data_dir))
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
