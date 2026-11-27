"""Deterministic fixed store, normalizer, and assertion helpers for #249 frozen contract examples."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from typer.testing import CliRunner
import uvicorn

from cairnmind import __version__, cli
from cairnmind.config import Settings
from cairnmind.importers import add_importer
from cairnmind.memory import MemoryManager
from cairnmind.memory_published import Publisher
from cairnmind.plans import PlanManager
from cairnmind.service import create_app
from cairnmind.sessions import SessionManager
from cairnmind.store import Store

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_HEX32_RE = re.compile(r"\b[0-9a-f]{32}\b")
_ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
_SHA256_RE = re.compile(r"\b[0-9a-f]{64}\b")
_GIT_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")


class DisabledOllama:
    """Deterministic stub used only to host the local FastAPI service for contract capture."""

    async def models(self):
        return {"models": []}

    async def embed(self, *args, **kwargs):
        raise AssertionError("Contract store fixture uses deterministic mode and must not embed.")

    async def chat(self, *args, **kwargs):
        raise AssertionError("Contract store fixture uses deterministic mode and must not invoke chat.")


@contextmanager
def run_contract_service(settings: Settings):
    """Run the real FastAPI service on an ephemeral loopback socket for contract example checks."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        settings.port = listener.getsockname()[1]
        settings.save()
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings, start_worker=False, ollama=DisabledOllama()),
                log_config=None,
                access_log=False,
                log_level="warning",
            )
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10.0
            while thread.is_alive() and not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "Local contract service did not start"
            yield settings
        finally:
            server.should_exit = True
            thread.join(timeout=10.0)


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-11-26T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-11-26T00:00:00Z",
    }
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True)
    return res.stdout.strip()


def build_contract_environment(tmp_path: Path) -> dict[str, Any]:
    """Create a reproducible fixed project, store, plan, session, memory packages, and converter."""
    data_dir = tmp_path / "data"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(
        "def compute_total(a: int, b: int) -> int:\n"
        "    \"\"\"Return deterministic sum.\"\"\"\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# Contract Fixture Repo\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "checkout", "-B", "main")
    _git(repo, "config", "user.email", "contract@example.invalid")
    _git(repo, "config", "user.name", "Contract Builder")
    _git(repo, "add", "src/app.py", "README.md")
    _git(repo, "commit", "-m", "initial fixture commit")

    settings = Settings.load(data_dir, create=True)
    settings.knowledge_agent = "disabled"
    settings.save()
    store = Store(data_dir, settings=settings)

    project = store.add_project("contract-app", repo)
    pid = project["id"]

    from cairnmind.ingest import index_project

    index_project(store, pid)
    code_source = next(
        row
        for row in store.query(
            "SELECT FROM sources WHERE project_id=:pid AND is_current=true AND kind='code'",
            {"pid": pid},
        )
        if "app.py" in row.get("uri", "")
    )
    note = store.snapshot(
        pid,
        "workspace://finding/contract-baseline",
        "Keep compute_total synchronous and deterministic.",
        kind="manual",
        metadata={"origin": "workspace_finding", "root": str(repo)},
    )
    store.remember(
        pid,
        {
            "kind": "decision",
            "key": "sync-compute",
            "content": "Keep compute_total synchronous and deterministic.",
            "status": "proposed",
            "metadata": {"root": str(repo), "evidence": "def compute_total"},
        },
        [note["id"], code_source["id"]],
    )

    plan_res = PlanManager(store).save(
        pid,
        {
            "title": "Contract stabilization",
            "goal": "Freeze JSON/MCP shapes for 1.0.0",
            "constraints": ["Preserve field names across minor releases"],
        },
        [
            {
                "key": "step-one",
                "content": "Verify frozen CLI and MCP responses.",
                "depends_on": [],
                "metadata": {"acceptance_criteria": ["All frozen JSON fields match"]},
            }
        ],
        idempotency_key="contract-plan-init-1",
        worktree=str(repo),
        activate=True,
    )
    plan_id = plan_res["plan"]["id"]

    sessions = SessionManager(store, settings)
    prepared = sessions.prepare(pid, "codex", repo, reason="startup")
    session_id = prepared["session_id"]
    packet = prepared["packet"]
    sessions.ack(
        session_id,
        packet["id"],
        packet["hash"],
        readback={
            "plan": {"id": packet["active_plan"]["id"], "version": packet["active_plan"]["version"]}
            if packet.get("active_plan")
            else None,
            "task": {"id": packet["active_task"]["id"], "version": packet["active_task"]["version"]}
            if packet.get("active_task")
            else None,
        },
    )

    # Portable snapshot & published team memory
    MemoryManager(store).snapshot(pid, repo)
    Publisher(store).publish(pid, repo)

    # External converter script (#127)
    conv_dir = tmp_path / "converters"
    conv_dir.mkdir()
    converter = conv_dir / "conv.py"
    converter.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "src = Path(sys.argv[1]).read_text(encoding='utf-8')\n"
        "for line in src.splitlines():\n"
        "    role, cwd, text = line.split('|', 2)\n"
        "    print(json.dumps({'version': 1, 'role': role, 'content': text, "
        "'session_id': 'ext-1', 'timestamp': '2026-11-26T00:00:00Z', 'cwd': cwd}), flush=True)\n",
        encoding="utf-8",
    )
    foreign_log = conv_dir / "sample.foreign"
    foreign_log.write_text(f"user|{repo}|Contract importer note about compute_total\n", encoding="utf-8")

    # Seed one importer before CLI capture or let CLI commands capture add/list/import/remove cleanly
    add_importer(data_dir, "seed-conv", [sys.executable, str(converter)])

    return {
        "tmp_path": tmp_path,
        "data_dir": data_dir,
        "repo": repo,
        "settings": settings,
        "store": store,
        "project": project,
        "project_id": pid,
        "plan_id": plan_id,
        "session_id": session_id,
        "code_source_id": code_source["id"],
        "converter": converter,
        "foreign_log": foreign_log,
        "base_revision": "HEAD",
        "head_sha": _git(repo, "rev-parse", "HEAD"),
    }


def _normalize_scalar(value: str, replacements: list[tuple[str, str]], maps: dict[str, dict[str, str]]) -> str:
    text = value.replace("\\", "/")
    for src, dst in replacements:
        if src:
            text = text.replace(src, dst)
    # Collapse Windows drive-prefixed file URIs to POSIX form after root replacement
    text = re.sub(r"file:///[A-Za-z]:/", "file:///", text)

    def map_token(pattern: re.Pattern[str], bucket: str, prefix: str) -> str:
        nonlocal text

        def repl(match: re.Match[str]) -> str:
            raw = match.group(0)
            table = maps[bucket]
            if raw not in table:
                table[raw] = f"<{prefix}_{len(table) + 1}>"
            return table[raw]

        text = pattern.sub(repl, text)
        return text

    map_token(_UUID_RE, "uuid", "UUID")
    map_token(_SHA256_RE, "sha256", "SHA256")
    map_token(_GIT_SHA_RE, "git_sha", "GIT_SHA")
    map_token(_HEX32_RE, "hex32", "ID32")
    text = _ISO_TS_RE.sub("<TIMESTAMP>", text)
    if __version__ and text == __version__:
        text = "<VERSION>"
    return text


def _normalize_doctor(normalized: dict[str, Any]) -> None:
    """Normalize host-probed capabilities in `doctor` so every OS/runner produces an identical contract shape."""
    for os_only in ("wsl", "workspace", "service_autostart", "cockpit"):
        normalized.pop(os_only, None)
    for host_key in ("python", "openssl", "environment_notes", "missing_required", "attention"):
        if host_key in normalized:
            normalized[host_key] = (
                "<HOST_PROBED>"
                if isinstance(normalized[host_key], str)
                else ["<HOST_PROBED>" for _ in normalized[host_key]]
                if isinstance(normalized[host_key], list)
                else normalized[host_key]
            )
    if isinstance(normalized.get("python_executable"), str):
        normalized["python_executable"] = "<PYTHON_EXE>"
    if isinstance(normalized.get("java"), dict):
        for opt_key in ("error", "managed_executable", "configured_executable"):
            normalized["java"].pop(opt_key, None)
        normalized["java"]["compatible"] = True
        normalized["java"]["on_path"] = True
        normalized["java"]["executable"] = "<HOST_PROBED>"
        normalized["java"]["version"] = "<HOST_PROBED>"
        normalized["java"]["major"] = 21
    if isinstance(normalized.get("arcadedb"), dict):
        for k in ("version", "version_matches", "installed"):
            if k in normalized["arcadedb"]:
                normalized["arcadedb"][k] = "<HOST_PROBED>" if isinstance(normalized["arcadedb"][k], str) else bool(normalized["arcadedb"][k])
    if isinstance(normalized.get("tmux"), dict):
        normalized["tmux"] = {"available": False, "code": "<HOST_PROBED>"}
    if isinstance(normalized.get("gpu"), dict):
        normalized["gpu"] = {"available": False}
    if isinstance(normalized.get("ollama"), dict):
        normalized["ollama"] = {
            "available": False,
            "chat_model_installed": False,
            "embedding_model_installed": False,
            "models": [],
            "running": [],
            "url": normalized["ollama"].get("url", "http://127.0.0.1:11434"),
        }
    if isinstance(normalized.get("memory_models"), dict):
        normalized["memory_models"]["evaluated"] = False
        normalized["memory_models"]["evaluations_passed"] = []
        for slot in ("chat", "embedding"):
            entry = normalized["memory_models"].get(slot)
            if isinstance(entry, dict):
                entry["identity"] = "<MODEL_ID>"
                entry["model_available"] = False
                entry["reachable"] = False
                entry.pop("error", None)
                entry.pop("structured_output", None)
    if isinstance(normalized.get("scip"), dict):
        normalized["scip"]["mode"] = "auto"
        normalized["scip"]["docker_available"] = False
        for idx_info in (normalized["scip"].get("indexers") or {}).values():
            if isinstance(idx_info, dict):
                idx_info["mode"] = "<HOST_MODE>"
                idx_info["executable"] = None
                idx_info["installed"] = False
                idx_info["missing_tools"] = []
    if isinstance(normalized.get("clients"), dict):
        for client_info in normalized["clients"].values():
            if isinstance(client_info, dict):
                for k, v in list(client_info.items()):
                    if isinstance(v, bool):
                        client_info[k] = False
                if "path" in client_info:
                    client_info["path"] = None
                if "version" in client_info:
                    client_info["version"] = None
                if "checks" in client_info:
                    client_info["checks"] = {}
                if "notes" in client_info:
                    client_info["notes"] = ["<HOST_PROBED>"]
    if isinstance(normalized.get("topology"), dict):
        topo = normalized["topology"]
        if isinstance(topo.get("lines"), list):
            topo["lines"] = [re.sub(r"Java \d+", "Java <MAJOR>", line) for line in topo["lines"]]
        if isinstance(topo.get("storage"), dict) and "java_major" in topo["storage"]:
            topo["storage"]["java_major"] = 21
        if isinstance(topo.get("this_machine"), list):
            for item in topo["this_machine"]:
                if isinstance(item, dict) and "java_major" in item:
                    item["java_major"] = 21
    if isinstance(normalized.get("store_writer"), dict):
        for k in ("host", "machine"):
            if k in normalized["store_writer"]:
                normalized["store_writer"][k] = "<HOST>"


def normalize_contract_payload(value: Any, env: dict[str, Any], *, command_name: str = "") -> Any:
    """Normalize machine/run-specific values while keeping EVERY object key, type, and shape exact."""
    repo_posix = str(env["repo"]).replace("\\", "/")
    data_posix = str(env["data_dir"]).replace("\\", "/")
    tmp_posix = str(env["tmp_path"]).replace("\\", "/")
    conv_posix = str(env["converter"]).replace("\\", "/")
    foreign_posix = str(env["foreign_log"]).replace("\\", "/")
    py_posix = str(sys.executable).replace("\\", "/")

    replacements = sorted(
        [
            (str(env["converter"]), "<CONVERTER_SCRIPT>"),
            (conv_posix, "<CONVERTER_SCRIPT>"),
            (str(env["foreign_log"]), "<FOREIGN_LOG>"),
            (foreign_posix, "<FOREIGN_LOG>"),
            (str(env["repo"]), "<WORKTREE>"),
            (repo_posix, "<WORKTREE>"),
            (str(env["data_dir"]), "<DATA_DIR>"),
            (data_posix, "<DATA_DIR>"),
            (str(env["tmp_path"]), "<TMP_ROOT>"),
            (tmp_posix, "<TMP_ROOT>"),
            (str(sys.executable), "<PYTHON_EXE>"),
            (py_posix, "<PYTHON_EXE>"),
            (str(env["settings"].url), "http://127.0.0.1:<PORT>"),
            (str(env["settings"].arcade_url), "http://127.0.0.1:<ARCADE_PORT>"),
            (str(env["settings"].arcade_database), "<ARCADE_DB>"),
            *(([(env["head_sha"][:12], "<GIT_SHA_SHORT>")] if env.get("head_sha") else [])),
        ],
        key=lambda item: len(item[0]),
        reverse=True,
    )
    maps: dict[str, dict[str, str]] = {"uuid": {}, "sha256": {}, "git_sha": {}, "hex32": {}}

    def walk(obj: Any, path: tuple[str, ...] = ()) -> Any:
        if isinstance(obj, dict):
            normalized = {}
            for key in sorted(obj):
                normalized[key] = walk(obj[key], (*path, key))
            if command_name == "doctor" and path == ():
                _normalize_doctor(normalized)
            return normalized
        if isinstance(obj, list):
            items = obj
            if path == ("held", "items") and all(isinstance(it, dict) for it in obj):
                items = sorted(obj, key=lambda it: (str(it.get("kind", "")), str(it.get("excerpt", "")), str(it.get("reason", ""))))
            return [walk(item, path) for item in items]
        if isinstance(obj, str):
            out = _normalize_scalar(obj, replacements, maps)
            if path and path[-1] in {"host", "machine", "platform", "size_label"}:
                return "<HOST>"
            if path and path[-1] == "instance":
                return "<INSTANCE_ID>"
            if command_name == "store_size" and path == ("text",):
                out = re.sub(r" +[0-9]+(?:\.[0-9]+)? (?:B|KiB|MiB|GiB|TiB)\b", " <SIZE>", out)
                out = re.sub(r"Measured in [0-9.]+ s\.", "Measured in <SEC> s.", out)
            return out
        if isinstance(obj, (int, float)) and not isinstance(obj, bool) and path:
            if path[-1] in {
                "port", "pid", "elapsed_ms", "elapsed_seconds", "duration_ms", "total_bytes", "package_size", "bytes",
            } or (
                "package_size" in path
                and path[-1] in {"used", "fraction", "current", "replaced", "transcripts", "age_seconds"}
            ):
                return 0.0 if isinstance(obj, float) else 0
        return obj

    return walk(value)


def _diff_json(expected: Any, actual: Any, path: str = "$") -> list[str]:
    diffs: list[str] = []
    if type(expected) is not type(actual):
        diffs.append(f"{path}: type mismatch expected {type(expected).__name__}, got {type(actual).__name__}")
        return diffs
    if isinstance(expected, dict):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        if missing:
            diffs.append(f"{path}: removed keys {missing}")
        if extra:
            diffs.append(f"{path}: unexpected added keys {extra}")
        for key in sorted(set(expected) & set(actual)):
            diffs.extend(_diff_json(expected[key], actual[key], f"{path}.{key}"))
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            diffs.append(f"{path}: list length expected {len(expected)}, got {len(actual)}")
        for idx, (e_item, a_item) in enumerate(zip(expected, actual)):
            diffs.extend(_diff_json(e_item, a_item, f"{path}[{idx}]"))
    elif expected != actual:
        diffs.append(f"{path}: expected {expected!r}, got {actual!r}")
    return diffs


def assert_matches_frozen_example(
    actual_payload: Any,
    golden_path: Path,
    env: dict[str, Any],
    *,
    command_name: str = "",
) -> None:
    """Assert normalized CLI/MCP output matches its frozen JSON contract example; fails cleanly on any drift."""
    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    actual = normalize_contract_payload(actual_payload, env, command_name=command_name)
    diffs = _diff_json(expected, actual)
    if diffs:
        preview = "\n".join(diffs[:15])
        raise AssertionError(
            f"Contract output drifted from frozen example {golden_path}:\n{preview}\n"
            "If an intentional contract change was made, update examples with scripts/update_contract_examples.py."
        )


def capture_cli_json_contracts(env: dict[str, Any]) -> dict[str, Any]:
    """Run the core CLI --json commands on the fixed contract store and return raw decoded JSON objects."""
    runner = CliRunner()
    data_dir = str(env["data_dir"])
    repo = str(env["repo"])
    plan_id = env["plan_id"]
    session_id = env["session_id"]

    # Ensure no coding session leaks into CLI calls
    saved = {k: os.environ.pop(k, None) for k in ("CAIRNMIND_SESSION_ID", "CAIRNMIND_PACKET_ID",
                                                  "DEVBRAIN_SESSION_ID", "DEVBRAIN_PACKET_ID")}
    try:
        commands: list[tuple[str, list[str]]] = [
            ("status", ["--data-dir", data_dir, "status"]),
            ("doctor", ["--data-dir", data_dir, "doctor", "--json"]),
            ("project_list", ["--data-dir", data_dir, "project", "list", "--json"]),
            ("store_writer", ["--data-dir", data_dir, "store", "writer"]),
            ("store_size", ["--data-dir", data_dir, "store", "size", "contract-app", "--json"]),
            ("ask", ["--data-dir", data_dir, "ask", "contract-app", "compute_total", "--json"]),
            ("brief", ["--data-dir", data_dir, "brief", "contract-app", "--json"]),
            ("records", ["--data-dir", data_dir, "records", "contract-app"]),
            ("remember", ["--data-dir", data_dir, "remember", "contract-app", "CLI note contract check",
                          "--kind", "fact", "--key", "cli-fact", "--json"]),
            ("plan_list", ["--data-dir", data_dir, "plan", "list", "contract-app"]),
            ("plan_show", ["--data-dir", data_dir, "plan", "show", "contract-app", plan_id]),
            ("plan_next", ["--data-dir", data_dir, "plan", "next", "contract-app"]),
            ("task_list", ["--data-dir", data_dir, "task", "list", "contract-app"]),
            ("stage_next", ["--data-dir", data_dir, "stage", "next", "contract-app", "--json"]),
            ("check_list", ["--data-dir", data_dir, "check", "list", "contract-app"]),
            ("check_profiles", ["--data-dir", data_dir, "check", "profiles", "contract-app"]),
            ("jobs_list", ["--data-dir", data_dir, "jobs", "list", "contract-app"]),
            ("session_show", ["--data-dir", data_dir, "session", "show", session_id]),
            ("scan_diff", ["--data-dir", data_dir, "scan", "contract-app", "--diff", "HEAD", "--worktree", repo, "--json"]),
            ("impact", ["--data-dir", data_dir, "impact", "contract-app", "--since", "HEAD", "--json"]),
            ("usage", ["--data-dir", data_dir, "usage", "contract-app", "--json"]),
            ("memory_status", ["--data-dir", data_dir, "memory", "status", "contract-app"]),
            ("memory_inspect", ["--data-dir", data_dir, "memory", "inspect",
                                str(Path(repo) / ".memory" / "cairnmind")]),
            ("memory_orphans", ["--data-dir", data_dir, "memory", "orphans", "contract-app", "--json"]),
            ("memory_decisions", ["--data-dir", data_dir, "memory", "decisions", "contract-app", "--json"]),
            ("memory_extraction_stats", ["--data-dir", data_dir, "memory", "extraction-stats", "contract-app", "--json"]),
            ("memory_publish_preview", ["--data-dir", data_dir, "memory", "publish", "contract-app", "--preview"]),
            ("importer_add", ["--data-dir", data_dir, "importer", "add", "cli-conv",
                              sys.executable, str(env["converter"]), "--json"]),
            ("importer_list", ["--data-dir", data_dir, "importer", "list", "--json"]),
            ("import_via", ["--data-dir", data_dir, "import", str(env["foreign_log"]),
                            "--via", "cli-conv", "--project", "contract-app"]),
            ("importer_remove", ["--data-dir", data_dir, "importer", "remove", "cli-conv", "--json"]),
        ]
        results: dict[str, Any] = {}
        for name, argv in commands:
            res = runner.invoke(cli.app, argv)
            if res.exit_code != 0:
                raise AssertionError(f"CLI command {name} ({argv}) exited {res.exit_code}:\n{res.output}")
            try:
                results[name] = json.loads(res.output)
            except ValueError as exc:
                raise AssertionError(f"CLI command {name} ({argv}) did not emit JSON ({exc}):\n{res.output}") from None
        return results
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


async def capture_mcp_tool_contracts(env: dict[str, Any]) -> dict[str, Any]:
    """Call all 8 MCP tools through StrictFastMCP on the fixed contract store."""
    from cairnmind.mcp_server import ServiceClient, create_server

    settings = env["settings"]
    repo = env["repo"]

    # Prepare an external MCP client session on the same contract project and exercise all 8 tools.
    client = ServiceClient(settings, external={"project": "contract-app", "worktree": str(repo)})
    server = create_server(settings, service=client)
    results: dict[str, Any] = {}
    try:
        _, first_ctx = await server.call_tool("resume_context", {"full_packet": True})
        results["resume_context_full"] = first_ctx
        packet = first_ctx["packet"]
        readback = {
            "plan": {"id": packet["active_plan"]["id"], "version": packet["active_plan"]["version"]}
            if packet.get("active_plan")
            else None,
            "task": {"id": packet["active_task"]["id"], "version": packet["active_task"]["version"]}
            if packet.get("active_task")
            else None,
        }
        _, acked = await server.call_tool(
            "ack_resume",
            {"packet_id": packet["id"], "packet_hash": packet["hash"], "readback": readback},
        )
        results["ack_resume"] = acked

        _, short_ctx = await server.call_tool("resume_context", {"full_packet": False})
        results["resume_context_short"] = short_ctx

        _, ask_res = await server.call_tool("ask", {"project": "contract-app", "question": "compute_total"})
        results["ask"] = ask_res

        _, rem_res = await server.call_tool(
            "remember",
            {
                "project": "contract-app",
                "record": {"kind": "fact", "key": "mcp-fact", "content": "MCP recorded contract fact."},
                "sources": [],
            },
        )
        results["remember"] = rem_res

        _, chk_res = await server.call_tool(
            "checkpoint",
            {"project": "contract-app", "state": {"summary": "Contract checkpoint verified", "next": "done"}},
        )
        results["checkpoint"] = chk_res

        # Saving a checkpoint moves state_hash; refresh and re-acknowledge before mutating plans.
        _, refreshed_ctx = await server.call_tool("resume_context", {"full_packet": True})
        r_pkt = refreshed_ctx["packet"]
        r_readback = {
            "plan": {"id": r_pkt["active_plan"]["id"], "version": r_pkt["active_plan"]["version"]}
            if r_pkt.get("active_plan")
            else None,
            "task": {"id": r_pkt["active_task"]["id"], "version": r_pkt["active_task"]["version"]}
            if r_pkt.get("active_task")
            else None,
        }
        await server.call_tool(
            "ack_resume",
            {"packet_id": r_pkt["id"], "packet_hash": r_pkt["hash"], "readback": r_readback},
        )

        _, plan_res = await server.call_tool(
            "save_plan",
            {
                "project": "contract-app",
                "plan": {"key": "mcp-plan", "title": "MCP Plan", "goal": "Verify save_plan output"},
                "steps": [{"key": "mcp-step-1", "content": "Run contract check", "depends_on": []}],
                "idempotency_key": "contract-mcp-plan-1",
                "worktree": str(repo),
            },
        )
        results["save_plan"] = plan_res

        _, nxt_res = await server.call_tool("next_task", {"project": "contract-app", "worktree": str(repo)})
        results["next_task"] = nxt_res

        _, cst_res = await server.call_tool("check_status", {"project": "contract-app", "worktree": str(repo)})
        results["check_status"] = cst_res
    finally:
        await client.aclose()
    return results
