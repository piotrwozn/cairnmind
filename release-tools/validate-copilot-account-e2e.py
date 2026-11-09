#!/usr/bin/env python3
"""Explicit live account test on synthetic data; never run in the regular suite."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from uuid import uuid4

import httpx

from devbrain.agent_memory import AgentMemoryClient
from devbrain.api_agent import AgentEngine
from devbrain.config import Settings
from devbrain.copilot_adapter import CopilotAdapter
from devbrain.service import create_app
from devbrain.store import Store


def inventory(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and ".git" not in path.relative_to(root).parts}


async def validate(args):
    started = time.monotonic()
    identity = uuid4().hex
    work = args.work_root.expanduser().resolve() / identity
    auth_home, engine_home = args.auth_home.expanduser().resolve(), args.engine_home.expanduser().resolve()
    if work.is_relative_to(auth_home) or auth_home == engine_home:
        raise ValueError("Use an isolated test engine and a work root outside the account data directory")
    work.mkdir(parents=True, mode=0o700)
    project_root = work / "project"
    project_root.mkdir()
    marker = "COPILOT_SYNTHETIC_" + secrets.token_hex(12)
    marker_bytes = (marker + "\n").encode()
    (project_root / "marker.txt").write_bytes(marker_bytes)
    for argv in (("init", "-q"), ("config", "user.name", "DevBrain live validation"),
                 ("config", "user.email", "validation@example.invalid"), ("add", "marker.txt"),
                 ("commit", "-qm", "Synthetic read-only Copilot validation")):
        subprocess.run(["git", "-C", str(project_root), *argv], check=True, capture_output=True)
    before = inventory(project_root)
    # No OAuth token is copied. Only the adapter uses the existing account runtime.
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "GITHUB_COPILOT_API_TOKEN"):
        os.environ.pop(name, None)
    account = Settings.load(auth_home)
    shared_engine = Settings.load(engine_home)
    database = "devbrain_copilot_e2e_" + identity
    settings = replace(shared_engine, data_dir=work / "data", arcade_database=database,
                       token=secrets.token_urlsafe(32), ollama_url="http://127.0.0.1:1")
    settings.save()
    store = None
    agent = None
    report = {"status": "running", "created_at": datetime.now(timezone.utc).isoformat(),
              "model": "auto", "authentication": "existing_github_account", "api_key_provided": False,
              "ambient_github_tokens_removed": True, "synthetic_project": str(project_root),
              "isolated_database": database, "local_model_inference": False,
              "allowed_tools": ["resume_context", "ack_resume", "session_status", "read_file"],
              "limits": {"max_tool_calls": 8, "max_turn_seconds": 120}}
    try:
        store = Store(settings.data_dir, settings=settings)
        project = store.add_project("synthetic-copilot-account-e2e", project_root)
        session_id = "copilot_e2e_" + identity
        run = {"id": uuid4().hex, "generation": 1, "project_id": project["id"],
               "worktree": str(project_root), "session_id": session_id, "client": "copilot"}
        profile = {"id": "copilot-e2e", "client": "copilot", "label": "Synthetic Copilot validation",
                   "model": "auto", "limits": report["limits"]}
        provider = CopilotAdapter(profile, None, settings=account, session_id=session_id)
        memory = AgentMemoryClient(settings, project["id"], project_root, session_id, client="copilot",
            transport=httpx.ASGITransport(app=create_app(settings, start_worker=False)))
        agent = AgentEngine(settings, run, profile, provider=provider, memory=memory)
        # The harness deliberately offers no file mutation, shell or memory-write tools.
        agent._schemas = {name: row for name, row in agent._schemas.items() if name in report["allowed_tools"]}
        print(json.dumps({"stage": "live_turn", "model": "auto", "tools": report["allowed_tools"]}), flush=True)
        result = await agent.turn(
            "This is a synthetic read-only connection test. First call resume_context, then acknowledge "
            "the actual ready packet with ack_resume and its exact IDs/hash/readback. Then call read_file "
            "for marker.txt. Reply with COPILOT_E2E_OK followed by the exact marker from that file. "
            "Do not run commands, change files, or create memory records.")
        events = agent.journal.events()
        requests = [event["data"] for event in events if event["type"] == "tool_requested"]
        tool_results = [event["data"] for event in events
                        if event["type"] == "message" and event.get("data", {}).get("role") == "tool"]
        decoded = [(row["name"], json.loads(row["content"])) for row in tool_results]
        reads = [value for name, value in decoded if name == "read_file"]
        contexts = [value for name, value in decoded if name == "resume_context"]
        acks = store.query("SELECT id,session_id,packet_id,packet_hash,status,readback FROM resume_acks "
                           "WHERE session_id=:session", {"session": session_id})
        actual_ack = any(ack.get("status") == "acknowledged"
                         and ack.get("packet_id") == (context.get("packet") or {}).get("id")
                         and ack.get("packet_hash") == (context.get("packet") or {}).get("hash")
                         for ack in acks for context in contexts)
        read_verified = any(value.get("ok") is True and value.get("content") == marker_bytes.decode()
                            and value.get("sha256") == hashlib.sha256(marker_bytes).hexdigest() for value in reads)
        names = [row["name"] for row in requests]
        ordered = all(name in names for name in ("resume_context", "ack_resume", "read_file"))
        ordered = ordered and names.index("resume_context") < names.index("ack_resume") < names.index("read_file")
        report.update(agent_status=result.get("status"), error_code=result.get("error_code"),
                      tool_order=names, actual_model_ack_verified=actual_ack, file_read_verified=read_verified,
                      protocol_order_verified=ordered, project_unchanged=inventory(project_root) == before,
                      response_contains_marker=marker in result.get("text", ""), ack_count=len(acks),
                      action_events=sum(event["type"].startswith("action_") for event in events),
                      pending_actions=len(agent.pending_actions()),
                      archived_transcript_sources=store.query(
                          "SELECT count(*) AS total FROM sources WHERE kind='agent_transcript'")[0]["total"])
        checks = (result.get("status") == "completed", actual_ack, read_verified, ordered,
                  report["project_unchanged"], report["response_contains_marker"],
                  report["action_events"] == 0, report["pending_actions"] == 0,
                  set(names) <= set(report["allowed_tools"]))
        report["status"] = "passed" if all(checks) else "failed"
    except Exception as error:
        report.update(status="failed", exception_type=type(error).__name__)
        # Provider exception bodies never enter the report.
    finally:
        if agent is not None:
            await agent.close()
        if store is not None:
            assert store.settings.arcade_database == database and database.startswith("devbrain_copilot_e2e_")
            try:
                store.client.server_command("drop database " + database)
                report["only_owned_database_removed"] = True
                store.client.server_info()
                report["shared_engine_kept_running"] = True
            except Exception as error:
                report.update(status="failed", cleanup_exception_type=type(error).__name__)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x") as output:
            json.dump(report, output, indent=2)
            output.write("\n")
        print(json.dumps(report), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-home", required=True, type=Path)
    parser.add_argument("--engine-home", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    raise SystemExit(asyncio.run(validate(parser.parse_args())))
