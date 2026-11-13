"""Cairnmind CLI; native coding clients retain their own terminal and permissions."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
from contextlib import contextmanager
from functools import wraps
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated

import httpx
import typer

from cairnmind.config import SERVICE_NAMES, Settings, drop_env, session_value
from cairnmind.ollama import OllamaUnavailable
from cairnmind.arcade import ArcadeError

app = typer.Typer(no_args_is_help=False, pretty_exceptions_enable=False,
                  help="A terminal workspace with memory: Codex, Claude, Ollama, API and GitHub Copilot.")
projects_app = typer.Typer(help="The registry of projects and their folders.")
tasks_app = typer.Typer(help="Shared plan steps, their states and verification evidence.")
models_app = typer.Typer(help="Local Ollama models.")
engine_app = typer.Typer(help="The native ArcadeDB engine (Java 21+).")
plans_app = typer.Typer(help="Versioned plans and a deterministic next step.")
checks_app = typer.Typer(help="Named project checks and how current their evidence is.")
sessions_app = typer.Typer(help="Resume packets, the scope of history and acknowledgements.")
eval_app = typer.Typer(help="Isolated evaluation of the real memory.")
jobs_app = typer.Typer(help="Diagnostics and explicit retry of one project's jobs.")
memory_app = typer.Typer(help="Portable memory of one project and the local snapshot schedule.")
cockpit_app = typer.Typer(help="The local cockpit, persistent terminals and the memory view.")
workspace_app = typer.Typer(help="The terminal workspace: status, detaching and stopping.")
app.add_typer(projects_app, name="project")
app.add_typer(tasks_app, name="task")
app.add_typer(models_app, name="models")
app.add_typer(engine_app, name="engine")
app.add_typer(plans_app, name="plan")
app.add_typer(checks_app, name="check")
app.add_typer(sessions_app, name="session")
app.add_typer(eval_app, name="eval")
app.add_typer(jobs_app, name="jobs")
app.add_typer(memory_app, name="memory")
app.add_typer(cockpit_app, name="cockpit")
app.add_typer(workspace_app, name="workspace")


def output(value) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def initialized(command):
    """Initialize only when an operational leaf runs, after argument/help parsing."""
    @wraps(command)
    def invoke(ctx, *args, **kwargs):
        ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
        return command(ctx, *args, **kwargs)
    return invoke


def entrypoint() -> None:
    try:
        app()
    except (ValueError, OSError, ArcadeError, httpx.HTTPError, OllamaUnavailable) as exc:
        typer.echo(f"Cairnmind: {exc}", err=True)
        raise SystemExit(1) from None


def store_for(settings: Settings):
    from cairnmind.store import Store
    return Store(settings.data_dir)


def store_for_read(settings: Settings):
    """The existing store for a command that only reads. It starts no engine and creates nothing: when the
    configuration, engine, database or schema is not ready it fails at once, saying what to run."""
    from cairnmind.store import Store
    return Store.open_readonly(settings)


def actor_scope(store, project_id: str, worktree: Path | str | None = None):
    """Read the launcher's worktree without requiring an ACK merely to inspect state."""
    scope = str(Path(worktree).expanduser().resolve()) if worktree else None
    session_id = session_value("SESSION_ID")
    if session_id:
        rows = store.query("SELECT project_id,worktree FROM sessions WHERE id=:id", {"id": session_id})
        if not rows or rows[0]["project_id"] != project_id:
            raise ValueError("resume_required: command project differs from the session")
        if scope and scope != rows[0]["worktree"]:
            raise ValueError("resume_required: command worktree differs from the session")
        scope = rows[0]["worktree"]
    return scope


@contextmanager
def actor_write(store, settings: Settings, project_id: str, worktree: Path | str | None = None):
    """Native CLI commands inherit the launcher's logical session; manual repair stays possible."""
    session_id = session_value("SESSION_ID")
    if session_id:
        from cairnmind.integrations import drain_hook_spool
        drain_hook_spool(settings, store)
    with store.transaction():
        scope = actor_scope(store, project_id, worktree)
        if session_id:
            from cairnmind.sessions import SessionManager
            manager = SessionManager(store, settings)
            manager.require_ack(session_id)
        yield scope


def service_alive(settings: Settings) -> bool:
    from cairnmind.service import instance_id
    try:
        with httpx.Client(trust_env=False, timeout=0.7) as client:
            result = client.get(settings.url + "/health")
            data = result.json()
            return result.is_success and data.get("service") in SERVICE_NAMES and data.get("instance") == instance_id(settings)
    except (httpx.HTTPError, ValueError):
        return False


def ensure_service(settings: Settings, *, background_inference: bool = True) -> None:
    if service_alive(settings):
        return
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log = settings.data_dir / "service.log"
    with log.open("ab") as handle:
        env = dict(os.environ)
        # The service is a shared system worker, not a child actor of one coding session.
        drop_env(env, "SESSION_ID", "PACKET_ID")
        command = [sys.executable, "-m", "cairnmind", "--data-dir", str(settings.data_dir), "serve"]
        if not background_inference:
            command.append("--defer-background-inference")
        subprocess.Popen(command,
                         stdin=subprocess.DEVNULL, stdout=handle, stderr=handle, start_new_session=True, env=env)
    for _ in range(300):
        if service_alive(settings):
            return
        time.sleep(0.2)
    raise typer.BadParameter(f"The service did not start. Check {log}; port {settings.port} may be in use.")


def request(settings: Settings, method: str, path: str, *, start_service: bool = True, **kwargs):
    if start_service:
        ensure_service(settings)
    elif not (settings.data_dir / "config.json").is_file() or not settings.token:
        raise typer.BadParameter("No existing Cairnmind configuration; run `cairnmind init` explicitly.")
    retry_job = path.startswith("/jobs/") and path.endswith("/retry")
    if (method.upper() == "POST" and (retry_job or path in {
            "/remember", "/findings", "/checkpoint", "/plans/save", "/plans/activate",
            "/memory/snapshot", "/memory/import"})
            or method.upper() == "PATCH" and path == "/memory/settings"):
        session_id = session_value("SESSION_ID")
        if session_id:
            payload = dict(kwargs.get("json") or {})
            if payload.get("session_id") not in (None, session_id):
                raise typer.BadParameter("resume_required: another session cannot be impersonated")
            payload["session_id"] = session_id
            kwargs["json"] = payload
    elif method.upper() == "POST" and path in {"/ask", "/plans/next", "/memory/preview", "/memory/inspect"}:
        session_id = session_value("SESSION_ID")
        if session_id:
            payload = dict(kwargs.get("json") or {})
            payload["session_id"] = session_id
            kwargs["json"] = payload
    elif method.upper() == "GET" and (path in {"/context", "/checks", "/jobs", "/memory/status"}
                                      or path.startswith("/jobs/")):
        session_id = session_value("SESSION_ID")
        if session_id:
            kwargs["params"] = {**dict(kwargs.get("params") or {}), "session_id": session_id}
    with httpx.Client(trust_env=False, timeout=settings.ask_timeout + 90,
                      headers={"Authorization": f"Bearer {settings.token}"}) as client:
        try:
            response = client.request(method, settings.url + path, **kwargs)
        except httpx.HTTPError:
            if not start_service:
                raise typer.BadParameter(
                    "The existing Cairnmind service is unavailable; diagnostics do not start it. "
                    "Run `cairnmind serve` explicitly.") from None
            raise
        if not response.is_success:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise typer.BadParameter(str(detail))
        return response.json()


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context, data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None):
    ctx.obj = Settings.load(data_dir, create=False)
    if ctx.invoked_subcommand is None:
        _launch_terminal_workspace(ctx)


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _launch_terminal_workspace(ctx: typer.Context, *, takeover: bool = False) -> None:
    # Check before initialization: redirected CLI diagnostics must never create
    # configuration, launch a browser or silently start interactive processes.
    if not _interactive_terminal():
        raise typer.BadParameter(
            "Run `cairnmind` in an interactive terminal. "
            "Available commands: `cairnmind --help`; status: `cairnmind workspace status`.")
    from cairnmind.workspace import launch_workspace
    ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
    code = launch_workspace(ctx.obj, takeover=True) if takeover else launch_workspace(ctx.obj)
    if code:
        raise typer.Exit(code=code)


@app.command()
def tui(ctx: typer.Context):
    """Open the Cairnmind terminal workspace (the same as the command without arguments)."""
    _launch_terminal_workspace(ctx)


@app.command("version")
def version():
    """Show the version without initializing configuration or services."""
    from cairnmind import __version__
    output({"version": __version__})


@workspace_app.command("status")
def terminal_workspace_status(ctx: typer.Context):
    """Read the status without starting services or initializing configuration."""
    from cairnmind.workspace import workspace_status
    output(workspace_status(ctx.obj))


@workspace_app.command("detach")
def terminal_workspace_detach(ctx: typer.Context):
    """Detach the terminal view and keep the client sessions."""
    from cairnmind.workspace import detach_workspace
    output(detach_workspace(ctx.obj))


@workspace_app.command("takeover")
def terminal_workspace_takeover(ctx: typer.Context):
    """Take control in this terminal; the previous view becomes read-only."""
    _launch_terminal_workspace(ctx, takeover=True)


@workspace_app.command("stop")
def terminal_workspace_stop(ctx: typer.Context, clients: bool = False):
    """Close the workspace; --clients also stops the sessions that belong to it."""
    from cairnmind.workspace import stop_workspace
    output(stop_workspace(ctx.obj, stop_clients=clients))


@cockpit_app.command("serve")
@initialized
def cockpit_serve(ctx: typer.Context, port: int | None = None):
    """Run the cockpit server; normally the cairnmind command manages it."""
    from cairnmind.cockpit import serve_cockpit
    serve_cockpit(ctx.obj, port=port)


@cockpit_app.command("status")
def cockpit_status(ctx: typer.Context):
    """Read the cockpit status without starting services or initializing configuration."""
    from cairnmind.cockpit import cockpit_status as inspect
    output(inspect(ctx.obj))


@cockpit_app.command("stop")
def cockpit_stop(ctx: typer.Context, clients: bool = False):
    """Stop the cockpit; --clients explicitly stops the managed terminals too."""
    from cairnmind.cockpit import stop_cockpit
    output(stop_cockpit(ctx.obj, stop_clients=clients))


@app.command()
@initialized
def init(ctx: typer.Context):
    """Create the local store. Downloads no models and changes no client configuration."""
    store_for(ctx.obj)
    output({"data_dir": str(ctx.obj.data_dir), "next": "cairnmind doctor; cairnmind models pull"})


@engine_app.command("install")
@initialized
def engine_install(ctx: typer.Context, distribution: Path | None = None):
    """Download the pinned ArcadeDB engine and verify its SHA-256 (about 87 MB)."""
    from cairnmind.engine import install_engine
    path = install_engine(ctx.obj, distribution)
    output({"installed": str(path), "version": ctx.obj.arcade_version})


@engine_app.command("start")
@initialized
def engine_start(ctx: typer.Context):
    from cairnmind.engine import start_engine
    output(start_engine(ctx.obj))


@engine_app.command("status")
@initialized
def engine_inspect(ctx: typer.Context):
    from cairnmind.engine import engine_status
    output(engine_status(ctx.obj))


@engine_app.command("stop")
@initialized
def engine_stop(ctx: typer.Context):
    from cairnmind.engine import stop_engine
    if service_alive(ctx.obj):
        raise typer.BadParameter("Run cairnmind stop first; the knowledge service is using ArcadeDB")
    output(stop_engine(ctx.obj))


@app.command()
@initialized
def doctor(ctx: typer.Context, contract: bool = typer.Option(
        False, "--contract", help="Show only the client contract: every check with its evidence line."),
           output_format: Annotated[str, typer.Option(
               "--format", help="With --contract: json (default) or text, one line per client and check.")] = "json"):
    if output_format not in ("json", "text"):
        raise typer.BadParameter("--format must be json or text.")
    if output_format == "text" and not contract:
        raise typer.BadParameter("--format text is available only with --contract.")
    if contract:
        from cairnmind.integrations import CLIENTS, _probe_cli, contract_text
        probes = {name: _probe_cli(name) for name in CLIENTS}
        if output_format == "text":
            typer.echo(contract_text(probes))
        else:
            output({name: {key: probe[key] for key in ("installed", "version", "verified_resume", "checks", "notes")}
                    for name, probe in probes.items()})
        if any(probe["installed"] and not probe["verified_resume"] for probe in probes.values()):
            raise typer.Exit(1)
        return
    from cairnmind.integrations import doctor as inspect
    result = inspect(ctx.obj)
    result["service_running"] = service_alive(ctx.obj)
    result["data_dir"] = str(ctx.obj.data_dir)
    from cairnmind.config import ENV_PREFIXES, data_dir_note, env_conflicts, session_conflicts
    # Shown here and nowhere else: a hook or an MCP server must not print notes of its own.
    result["environment_notes"] = [
        f"{ENV_PREFIXES[0]}{name} and {ENV_PREFIXES[1]}{name} are both set and differ; {ENV_PREFIXES[0]}{name} is used."
        for name in env_conflicts()] + [
        f"{ENV_PREFIXES[0]}{name} and {ENV_PREFIXES[1]}{name} name different sessions; neither is used: commands in "
        "this shell are refused and hooks and the MCP server stay unbound. Start the client again through the launcher."
        for name in session_conflicts()] + [note for note in [data_dir_note()] if note]
    result["models_lock_present"] = (ctx.obj.data_dir / "models.lock.json").exists()
    from cairnmind.api_runtime import coding_profiles_status
    result["coding_profiles"] = coding_profiles_status(ctx.obj)
    output(result)


@app.command()
@initialized
def serve(ctx: typer.Context, defer_background_inference: bool = False):
    """Run the local service (the single owner of the GPU and the queue)."""
    import uvicorn
    from cairnmind.service import create_app
    settings = ctx.obj
    with (settings.data_dir / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise typer.BadParameter("The service is already running for this store.")
        api = create_app(settings, background_inference=not defer_background_inference)
        server = uvicorn.Server(uvicorn.Config(api, host=settings.host, port=settings.port, log_level="info"))
        api.state.shutdown = lambda: setattr(server, "should_exit", True)
        server.run()


@app.command()
@initialized
def stop(ctx: typer.Context):
    """Stop only this store's service, without deleting data."""
    if not service_alive(ctx.obj):
        output({"stopping": False, "reason": "The service is not running"})
        return
    output(request(ctx.obj, "POST", "/shutdown"))


@projects_app.command("add")
@initialized
def project_add(ctx: typer.Context, name: str, root: Path):
    store = store_for(ctx.obj)
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise typer.BadParameter("The path must be a folder, not a file")
    project = store.add_project(name, root)
    from cairnmind.ingest import index_project, register_default_histories
    result = index_project(store, project["id"])
    discovery = register_default_histories(store, project["id"], Path(project["root"]))
    output({"project": project, "indexed": result, "history": discovery})


@projects_app.command("list")
def project_list(ctx: typer.Context):
    output(store_for_read(ctx.obj).list_projects())


@projects_app.command("root")
@initialized
def project_root(ctx: typer.Context, project: str, path: Path):
    """Explicitly assign an additional folder or worktree to the project."""
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    root = path.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise typer.BadParameter("The path must be a folder, not a file")
    store.add_project_root(pid, root)
    output({"project": pid, "root": str(root)})


@app.command()
@initialized
def index(ctx: typer.Context, project: str):
    output(request(ctx.obj, "POST", "/ingest", json={"project": project}))


@app.command("import")
@initialized
def import_command(ctx: typer.Context, path: Path, project: str | None = None, client: str = "auto"):
    """Import a client's JSONL; without a project, keep the history in the import inbox."""
    output(request(ctx.obj, "POST", "/import", json={"path": str(path.expanduser().resolve()),
                                                    "project": project, "client": client}))


@app.command()
@initialized
def ask(ctx: typer.Context, project: str, question: str, worktree: Path | None = None,
        answer_schema: Path | None = None):
    payload = {"project": project, "question": question,
               "worktree": str(worktree.resolve()) if worktree else None}
    if answer_schema is not None:
        from cairnmind.answer_contract import prepare_answer_schema
        payload["answer_schema"] = prepare_answer_schema(json.loads(answer_schema.read_text(encoding="utf-8")))
    output(request(ctx.obj, "POST", "/ask", json=payload))


@app.command()
@initialized
def context(ctx: typer.Context, project: str, worktree: Path | None = None):
    output(request(ctx.obj, "GET", "/context", params={"project": project,
                   **({"worktree": str(worktree.resolve())} if worktree else {})}))


@app.command()
@initialized
def brief(ctx: typer.Context, project: str, worktree: Path | None = None,
          as_json: Annotated[bool, typer.Option("--json", help="Print the marked lines as JSON.")] = False):
    """Show where work stopped: checkpoint, next task, check evidence, commits and proposals. Asks no model."""
    from cairnmind.brief import build_brief
    # Read the store directly, as `tasks list` does: starting the service could start model work.
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    result = build_brief(store, pid, actor_scope(store, pid, worktree))
    if as_json:
        output(result)
    else:
        typer.echo(result["text"])


@app.command()
def impact(ctx: typer.Context, project: str,
           since: Annotated[str, typer.Option("--since", help="Commit, tag or branch to compare the worktree with.")],
           worktree: Path | None = None,
           as_json: Annotated[bool, typer.Option("--json", help="Print the linked records as JSON.")] = False):
    """List records linked to the files and symbols changed since a commit. Read-only; asks no model."""
    from cairnmind.impact import build_impact
    store = store_for_read(ctx.obj)

    def read():
        pid = store.resolve_project(project)["id"]
        return build_impact(store, pid, since, actor_scope(store, pid, worktree))

    result = store.read_consistently(read)
    if as_json:
        output(result)
    else:
        typer.echo(result["text"])


@app.command()
def usage(ctx: typer.Context, project: str,
          since: Annotated[str, typer.Option("--since", help="How far back to look: Nd (days) or Nh (hours).")] = "7d",
          as_json: Annotated[bool, typer.Option("--json", help="Print the usage as JSON.")] = False):
    """Sum the token usage of the built-in API agent per day, profile and model. Asks no model, starts no service."""
    from cairnmind.usage import build_usage
    # Resolving the project name reads the store, which needs the running engine but never starts it;
    # the usage itself is read from the session journals and nothing is written to them.
    store = store_for_read(ctx.obj)
    found = store.read_consistently(lambda: store.resolve_project(project))
    try:
        result = build_usage(ctx.obj.data_dir, found, since)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--since")
    if as_json:
        output(result)
    else:
        typer.echo(result["text"])


@app.command()
def status(ctx: typer.Context, project: str | None = None):
    output(request(ctx.obj, "GET", "/status", start_service=False,
                   params={"project": project} if project else {}))


@jobs_app.command("list")
def jobs_list(ctx: typer.Context, project: str,
              state: Annotated[str | None, typer.Option("--state", "--status")] = None,
              kind: str | None = None,
              limit: Annotated[int, typer.Option(min=1, max=100)] = 50):
    """Read a bounded list of jobs; start neither the service nor the engine."""
    params = {"project": project, "limit": limit}
    if state is not None:
        params["state"] = state
    if kind is not None:
        params["kind"] = kind
    output(request(ctx.obj, "GET", "/jobs", start_service=False, params=params))


@jobs_app.command("show")
def jobs_show(ctx: typer.Context, project: str, job_id: str):
    """Read a job that belongs to the given project."""
    output(request(ctx.obj, "GET", f"/jobs/{job_id}", start_service=False, params={"project": project}))


@jobs_app.command("retry")
@initialized
def jobs_retry(ctx: typer.Context, project: str, job_id: str,
               expected_attempts: Annotated[int, typer.Option("--expected-attempts", min=0)],
               include_omissions: Annotated[bool, typer.Option(
                   "--include-omissions", help="Include a finished extraction in which every record was rejected.")
               ] = False):
    """Explicitly retry the chosen job if its attempt counter still matches."""
    payload = {"project": project, "expected_attempts": expected_attempts}
    if include_omissions:
        payload["include_omissions"] = True
    output(request(ctx.obj, "POST", f"/jobs/{job_id}/retry", json=payload))


@app.command()
def records(ctx: typer.Context, project: str, history: bool = False):
    store = store_for_read(ctx.obj)
    output(store.read_consistently(
        lambda: store.list_records(store.resolve_project(project)["id"], include_history=history)))


@app.command()
def source(ctx: typer.Context, source_id: str):
    """Read the stored snapshot of a source, even when the original no longer exists."""
    output(store_for_read(ctx.obj).get_source(source_id))


@app.command()
@initialized
def remember(ctx: typer.Context, project: str, content: str, kind: str = "fact",
             source_id: Annotated[list[str] | None, typer.Option("--source")] = None,
             confirmed: bool = False, key: str | None = None):
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    with actor_write(store, ctx.obj, pid):
        sources = source_id or [store.snapshot(pid, f"manual://{uuid.uuid4()}", content, "manual",
                                              {"role": "user", "origin": "cli"})["id"]]
        record = {"kind": kind, "content": content, "status": "confirmed" if confirmed else "proposed"}
        if key is not None:
            record["key"] = key
        result = store.remember(pid, record, sources)
    output(result)


@app.command()
@initialized
def checkpoint(ctx: typer.Context, project: str, state_file: Path):
    output(request(ctx.obj, "POST", "/checkpoint", json={"project": project,
                   "state": json.loads(state_file.read_text(encoding="utf-8"))}))


@tasks_app.command("list")
def task_list(ctx: typer.Context, project: str, worktree: Path | None = None):
    from cairnmind.verification import verification_status
    store = store_for_read(ctx.obj)

    def read():
        pid = store.resolve_project(project)["id"]
        scope = actor_scope(store, pid, worktree)
        tasks = [r for r in store.list_records(pid) if r["kind"] == "task"]
        return [{**task, "verification": verification_status(store, task, scope)} for task in tasks]

    output(store.read_consistently(read))


@tasks_app.command("add")
@initialized
def task_add(ctx: typer.Context, project: str, title: str, plan: str | None = None, step: int = 1,
             criterion: Annotated[list[str] | None, typer.Option("--criterion")] = None,
             depends_on: Annotated[list[str] | None, typer.Option("--depends-on")] = None,
             requires_test: bool = False, requires_review: bool = False):
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    metadata = {"state": "todo", "plan_id": plan, "step_index": step,
                "acceptance_criteria": criterion or [], "depends_on": depends_on or [],
                "requires_test": requires_test, "requires_review": requires_review}
    text = json.dumps({"title": title, **metadata}, ensure_ascii=False)
    with actor_write(store, ctx.obj, pid):
        src = store.snapshot(pid, f"manual://task/{uuid.uuid4()}", text, "manual", {"role": "user"})
        result = store.remember(pid, {"kind": "task", "content": title, "status": "confirmed",
                                     "metadata": metadata}, [src["id"]])
    output(result)


@tasks_app.command("update")
@initialized
def task_update(ctx: typer.Context, project: str, task_id: str, state: str,
                evidence: Path | None = None, test_passed: bool = False,
                review_passed: bool = False, acceptance_passed: bool = False,
                version: int | None = None, worktree: Path | None = None,
                plan_version: int | None = None,
                check_run: Annotated[list[str] | None, typer.Option("--check-run")] = None):
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    task = next((r for r in store.list_records(pid) if r["id"] == task_id and r["kind"] == "task"), None)
    if task is None:
        raise typer.BadParameter("No such task in this project")
    sources = task["source_ids"]
    if isinstance(sources, str):
        sources = json.loads(sources)
    metadata = {"state": state}
    if check_run:
        metadata["check_run_ids"] = check_run
    evidence_text = None
    if test_passed or review_passed or acceptance_passed:
        if evidence is None:
            raise typer.BadParameter("Verification requires --evidence FILE with the check result")
        evidence_text = evidence.read_text(encoding="utf-8")
        if not evidence_text.strip():
            raise typer.BadParameter("The evidence file must contain a non-empty check result")
    with actor_write(store, ctx.obj, pid, worktree) as scope:
        if evidence_text is not None:
            src = store.snapshot(pid, f"verification://{uuid.uuid4()}", evidence_text, "verification",
                                 {"path": str(evidence.resolve()), "role": "user", "reported": True})
            sources = [*sources, src["id"]]
            for key, passed in (("test", test_passed), ("review", review_passed), ("acceptance", acceptance_passed)):
                if passed:
                    metadata[f"{key}_passed"] = True
                    metadata[f"{key}_source_ids"] = [src["id"]]
        record = {"id": task_id, "kind": "task", "content": task["content"], "status": task["status"],
                  "metadata": metadata, "worktree": scope,
                  "expected_version": version if version is not None else task["version"]}
        plan_ref = task.get("metadata", {}).get("plan_ref")
        if plan_ref:
            from cairnmind.plans import PlanManager
            current_plan = PlanManager(store).get(pid, plan_ref)["plan"]
            record["expected_plan_version"] = plan_version if plan_version is not None else current_plan["version"]
        result = store.remember(pid, record, sources)
    output(result)


@plans_app.command("save")
@initialized
def plan_save(ctx: typer.Context, project: str, input_json: Path,
              idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
              expected_version: int | None = None, task_versions: Path | None = None,
              worktree: Path | None = None, activate: bool = False):
    from cairnmind.plans import PlanManager
    document = json.loads(input_json.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise typer.BadParameter("The file must contain an object with the fields plan and steps")
    versions = (json.loads(task_versions.read_text(encoding="utf-8")) if task_versions
                else document.get("task_versions"))
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    with actor_write(store, ctx.obj, pid, worktree) as scope:
        result = PlanManager(store).save(pid, document.get("plan", {}), document.get("steps", []),
            idempotency_key, expected_version if expected_version is not None else document.get("expected_version"),
            versions, scope, activate)
    output(result)


@plans_app.command("list")
def plan_list(ctx: typer.Context, project: str):
    from cairnmind.plans import PlanManager
    store = store_for_read(ctx.obj)
    output(store.read_consistently(lambda: PlanManager(store).list(store.resolve_project(project)["id"])))


@plans_app.command("show")
def plan_show(ctx: typer.Context, project: str, plan_id: str):
    from cairnmind.plans import PlanManager
    store = store_for_read(ctx.obj)
    output(store.read_consistently(lambda: PlanManager(store).get(store.resolve_project(project)["id"], plan_id)))


@plans_app.command("next")
def plan_next(ctx: typer.Context, project: str, plan_id: str | None = None, worktree: Path | None = None):
    from cairnmind.plans import PlanManager
    store = store_for_read(ctx.obj)

    def read():
        pid = store.resolve_project(project)["id"]
        scope = actor_scope(store, pid, worktree)
        result = PlanManager(store).next_task(pid, plan_id, scope)
        if result.get("task"):
            from cairnmind.scope import drift_warning
            from cairnmind.verification import verification_status
            result["verification"] = verification_status(store, result["task"], scope)
            warning = drift_warning(result["verification"].get("scope_drift"))
            if warning:
                result["warnings"] = [warning]
        return result

    output(store.read_consistently(read))


@plans_app.command("activate")
@initialized
def plan_activate(ctx: typer.Context, project: str, plan_id: str, worktree: Path | None = None):
    from cairnmind.plans import PlanManager
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    with actor_write(store, ctx.obj, pid, worktree) as scope:
        result = PlanManager(store).activate(pid, plan_id, scope)
    output(result)


@plans_app.command("adopt")
@initialized
def plan_adopt(ctx: typer.Context, project: str, group: str,
               idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
               title: str | None = None, worktree: Path | None = None, activate: bool = False):
    from cairnmind.plans import PlanManager
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    with actor_write(store, ctx.obj, pid, worktree) as scope:
        result = PlanManager(store).adopt_legacy(pid, group, plan={"title": title} if title else None,
            idempotency_key=idempotency_key, worktree=scope, activate=activate)
    output(result)


@plans_app.command("contract")
def plan_contract(ctx: typer.Context, project: str,
                  plan_id: Annotated[str | None, typer.Option("--plan-id", help="The plan; the active one if omitted.")] = None,
                  worktree: Path | None = None,
                  write: Annotated[str | None, typer.Option(
                      "--write", metavar="CODE",
                      help="Add the proposed check profiles; CODE is printed with the diff they were shown in.")] = None,
                  as_json: Annotated[bool, typer.Option("--json", help="Print the contract as JSON.")] = False):
    """Show each acceptance criterion of a plan with the check that proves it. Runs no command."""
    from cairnmind.contract import build_contract, write_contract
    if write:
        # Writing the profile file prepares the store as every writing command does.
        ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
    store = store_for(ctx.obj) if write else store_for_read(ctx.obj)

    def run_contract():
        pid = store.resolve_project(project)["id"]
        scope = actor_scope(store, pid, worktree)
        return write_contract(store, pid, write, plan_id, scope) if write else build_contract(store, pid, plan_id, scope)

    # Repeated only for a read-only store, never for --write: Store(path) runs it once.
    result = store.read_consistently(run_contract)
    if as_json:
        output(result)
    else:
        typer.echo(result["text"])


@checks_app.command("profiles")
def check_profiles(ctx: typer.Context, project: str, worktree: Path | None = None):
    from cairnmind.verification import load_profiles
    store = store_for_read(ctx.obj)

    def read():
        pid = store.resolve_project(project)["id"]
        return load_profiles(store, pid, actor_scope(store, pid, worktree))

    output(store.read_consistently(read))


@checks_app.command("run")
@initialized
def check_run(ctx: typer.Context, project: str, profile: str,
              task: Annotated[str, typer.Option("--task")], worktree: Path | None = None):
    from cairnmind.verification import run_check
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    with actor_write(store, ctx.obj, pid, worktree) as scope:
        pass  # Validate receipt first; do not hold the database lock while executing a program.
    result = run_check(store, ctx.obj, pid, profile, task, scope)
    output(result)
    if not result.get("passed"):
        raise typer.Exit(1)


@checks_app.command("list")
@initialized
def check_list(ctx: typer.Context, project: str,
               task: Annotated[str | None, typer.Option("--task")] = None,
               worktree: Path | None = None):
    from cairnmind.verification import recover_interrupted_runs, verification_status
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    scope = actor_scope(store, pid, worktree)
    recover_interrupted_runs(store, ctx.obj)
    rows = store.query("SELECT FROM check_runs WHERE project_id=:project "
                       + ("AND task_id=:task " if task else "")
                       + ("AND worktree=:worktree " if scope else "") + "ORDER BY ended_at DESC,id LIMIT 100",
                       {"project": pid, "task": task, "worktree": scope})
    result = {"checks": [{key: value for key, value in row.items() if not key.startswith("@")} for row in rows]}
    if task:
        records = store.query("SELECT FROM records WHERE id=:id AND project_id=:project AND kind='task'",
                              {"id": task, "project": pid})
        if not records:
            raise typer.BadParameter("No such task in this project")
        result["verification"] = verification_status(store, records[0], scope)
    output(result)


@sessions_app.command("prepare")
@initialized
def session_prepare(ctx: typer.Context, project: str, client: str, worktree: Path | None = None):
    result = request(ctx.obj, "POST", "/sessions/prepare", json={"project": project, "client": client,
                     "worktree": str(worktree.resolve()) if worktree else None})
    output(result)
    if result["status"] != "ready":
        raise typer.Exit(2)


@sessions_app.command("show")
@initialized
def session_show(ctx: typer.Context, session_id: str):
    output(request(ctx.obj, "GET", f"/sessions/{session_id}"))


@sessions_app.command("context")
@initialized
def session_context(ctx: typer.Context, session_id: str):
    output(request(ctx.obj, "POST", "/sessions/context", json={"session_id": session_id}))


@sessions_app.command("ack")
@initialized
def session_ack(ctx: typer.Context, session_id: str, packet_id: str, packet_hash: str, readback_json: Path):
    actor = session_value("SESSION_ID")
    if actor and actor != session_id:
        raise typer.BadParameter("Another session cannot be acknowledged")
    output(request(ctx.obj, "POST", "/sessions/ack", json={"session_id": session_id,
        "packet_id": packet_id, "packet_hash": packet_hash,
        "readback": json.loads(readback_json.read_text(encoding="utf-8"))}))


@eval_app.command("run")
@initialized
def eval_run(ctx: typer.Context, clients: bool = False, report_dir: Path | None = None,
             skip_claude: bool = False):
    from cairnmind.evaluation import run_evaluation
    if skip_claude and not clients:
        raise typer.BadParameter("--skip-claude requires --clients")
    result = run_evaluation(ctx.obj, clients=clients, report_dir=report_dir, skip_claude=skip_claude)
    output(result)
    if result.get("status") == "failed":
        raise typer.Exit(1)
    if result.get("status") == "not_run":
        raise typer.Exit(3)


@eval_app.command("memory")
@initialized
def eval_memory(ctx: typer.Context, report_dir: Path | None = None):
    """Explicitly check extraction, transfer and restoration of memory with the local Ollama."""
    from cairnmind.memory_evaluation import run_memory_evaluation
    result = run_memory_evaluation(ctx.obj, report_dir=report_dir)
    output(result)
    if result.get("status") != "passed":
        raise typer.Exit(3 if result.get("status") == "not_run" else 1)


@memory_app.command("snapshot")
@initialized
def memory_snapshot(ctx: typer.Context, project: str, worktree: Path | None = None, preview: bool = False):
    """Save the project's full memory; --preview shows the scope without writing a snapshot."""
    payload = {"project": project, "worktree": str(worktree.expanduser().resolve()) if worktree else None}
    output(request(ctx.obj, "POST", "/memory/preview" if preview else "/memory/snapshot", json=payload))


@memory_app.command("inspect")
def memory_inspect(ctx: typer.Context, path: Path):
    """Check a snapshot's files without initializing the store or starting services."""
    from cairnmind.memory import inspect_snapshot
    # Preserve symlink components for the package validator to reject them.
    selected = str(path.expanduser().absolute())
    if session_value("SESSION_ID"):
        output(request(ctx.obj, "POST", "/memory/inspect", start_service=False, json={"path": selected}))
    else:
        output(inspect_snapshot(selected))


@memory_app.command("show")
def memory_show(ctx: typer.Context, path: Path,
                kind: Annotated[str | None, typer.Option("--kind", help="Record kind, or 'plan'.")] = None,
                status: Annotated[str | None, typer.Option("--status", help="Record status.")] = None,
                grep: Annotated[str | None, typer.Option("--grep", help="Case-insensitive text to look for.")] = None):
    """Read a snapshot's records and plans as text. Writes nothing; outside a coding session needs no engine or service."""
    from cairnmind.memory_view import show_snapshot
    # Preserve symlink components for the package validator to reject them.
    selected = str(path.expanduser().absolute())
    expected = None
    if session_value("SESSION_ID"):
        # A coding session may read only its own project's memory; the service decides.
        expected = request(ctx.obj, "POST", "/memory/inspect", start_service=False,
                           json={"path": selected})["snapshot_id"]
    typer.echo(show_snapshot(selected, kind=kind, status=status, grep=grep, expected_snapshot_id=expected))


@memory_app.command("orphans")
def memory_orphans(ctx: typer.Context, project: str, worktree: Path | None = None,
                   as_json: Annotated[bool, typer.Option("--json", help="Print the records as JSON.")] = False):
    """List records whose evidence is no longer current: orphaned, or resting on a changed source. Read-only."""
    from cairnmind.brief import _scoped_records
    from cairnmind.evidence import orphans_report
    from cairnmind.verification import _root
    store = store_for_read(ctx.obj)

    def read():
        selected = store.resolve_project(project)
        root = _root(store, selected["id"], actor_scope(store, selected["id"], worktree))
        return orphans_report(store, selected, root, _scoped_records(store, selected["id"], root))

    report = store.read_consistently(read)
    if as_json:
        output(report)
    else:
        typer.echo(report["text"])


@memory_app.command("import")
def memory_import(ctx: typer.Context, path: Path,
                  root: Annotated[Path, typer.Option("--root")], name: str | None = None,
                  dry_run: bool = False):
    """Import the memory of this project only; --dry-run needs a running service and writes nothing."""
    from cairnmind.memory import inspect_snapshot
    selected = str(path.expanduser().absolute())
    inspect_snapshot(selected)  # Validate the transfer before initializing anything.
    if not dry_run:
        ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
    payload = {"path": selected, "root": str(root.expanduser().resolve()), "dry_run": dry_run}
    if name is not None:
        payload["name"] = name
    output(request(ctx.obj, "POST", "/memory/import", start_service=not dry_run, json=payload))


@memory_app.command("auto")
def memory_auto(ctx: typer.Context, project: str, enable: bool = False, disable: bool = False,
                interval_minutes: Annotated[int, typer.Option(min=1, max=10080)] = 15,
                worktree: Path | None = None):
    """Enable or disable the local schedule of one project; no commits and nothing is sent to GitHub."""
    if enable == disable:
        raise typer.BadParameter("Choose exactly one: --enable or --disable.")
    ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
    payload = {"project": project, "enabled": enable, "interval_minutes": interval_minutes,
               "worktree": str(worktree.expanduser().resolve()) if worktree else None}
    output(request(ctx.obj, "PATCH", "/memory/settings", json=payload))


@memory_app.command("status")
def memory_status(ctx: typer.Context, project: str, worktree: Path | None = None):
    """Read the last snapshot and the schedule; start no service and create no configuration."""
    params = {"project": project}
    if worktree is not None:
        params["worktree"] = str(worktree.expanduser().resolve())
    output(request(ctx.obj, "GET", "/memory/status", start_service=False, params=params))


@app.command()
@initialized
def install(ctx: typer.Context, project: str):
    """Add the project's MCP server, hooks and instructions, keeping the existing configuration."""
    from cairnmind.integrations import install_integrations
    store = store_for(ctx.obj)
    output(install_integrations(ctx.obj, store.resolve_project(project)))


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
@initialized
def run(ctx: typer.Context, project: str, client: str, worktree: Path | None = None):
    from cairnmind.integrations import run_client
    ensure_service(ctx.obj)
    selected = store_for(ctx.obj).resolve_project(project)
    if worktree:
        store = store_for(ctx.obj)
        root = str(worktree.expanduser().resolve())
        if not store.query("SELECT id FROM project_roots WHERE project_id=:project AND path=:path",
                            {"project": selected["id"], "path": root}):
            raise typer.BadParameter("The worktree must be a registered folder of the project")
        selected = {**selected, "root": root}
    args = list(ctx.args)
    if args[:1] == ["--"]:
        args = args[1:]
    raise typer.Exit(run_client(ctx.obj, selected, client, args))


@app.command()
@initialized
def mcp(ctx: typer.Context):
    """MCP over stdio; prints no logs or CLI messages to stdout."""
    from cairnmind.mcp_server import run
    run(ctx.obj)


@app.command()
@initialized
def hook(ctx: typer.Context, client: Annotated[str, typer.Option("--client")]):
    from cairnmind.integrations import hook as handle_hook
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = handle_hook(ctx.obj, payload, client)
        if result:
            typer.echo(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        # Never corrupt the coding client's protocol or block a turn on memory failures.
        typer.echo(f"Cairnmind hook: {exc}", err=True)


@models_app.command("list")
@initialized
def model_list(ctx: typer.Context):
    from cairnmind.ollama import Ollama
    output(asyncio.run(Ollama(ctx.obj).models()))


@models_app.command("pull")
@initialized
def model_pull(ctx: typer.Context):
    """Explicitly download the models (several GB) and record their digests."""
    settings = ctx.obj
    with httpx.Client(trust_env=False, timeout=httpx.Timeout(60, read=900)) as client:
        for name in (settings.chat_model, settings.embedding_model):
            typer.echo(f"Downloading {name}…", err=True)
            with client.stream("POST", settings.ollama_url + "/api/pull", json={"name": name, "stream": True}) as res:
                res.raise_for_status()
                previous = None
                for line in res.iter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise typer.BadParameter(data["error"])
                    status = data.get("status")
                    if status != previous:
                        typer.echo(str(status), err=True)
                        previous = status
        result = client.get(settings.ollama_url + "/api/tags")
        result.raise_for_status()
        models = result.json()
    lock = {"chat_model": settings.chat_model, "embedding_model": settings.embedding_model,
            "models": models.get("models", []), "locked_at": time.time()}
    (settings.data_dir / "models.lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
    store = store_for(settings)
    from cairnmind.service import schedule_embeddings
    for item in models.get("models", []):
        if item.get("name") == settings.embedding_model and item.get("digest"):
            schedule_embeddings(store, f"{settings.embedding_model}@{item['digest']}")
    output({"models": [settings.chat_model, settings.embedding_model], "digests_saved": True})


def _retry_jobs(store):
    store.retry_jobs()


@app.command("retry-jobs")
@initialized
def retry_jobs(ctx: typer.Context):
    if session_value("SESSION_ID"):
        raise typer.BadParameter(
            "A model session cannot retry the jobs of all projects. "
            "Use `cairnmind jobs retry PROJECT JOB_ID --expected-attempts N` with a current ACK.")
    _retry_jobs(store_for(ctx.obj))
    output({"retried": True})


@app.command("rebuild-vectors")
@initialized
def rebuild_vectors(ctx: typer.Context, project: str):
    """Rebuild the project's derived vectors, keeping sources and history."""
    from cairnmind.ollama import Ollama
    from cairnmind.service import schedule_embeddings
    settings = ctx.obj
    store = store_for(settings)
    pid = store.resolve_project(project)["id"]
    namespace = asyncio.run(Ollama(settings).model_identity(settings.embedding_model))
    store.delete_embeddings(pid)
    count = schedule_embeddings(store, namespace)
    output({"project": pid, "missing_vectors_queued": count, "namespace": namespace,
            "sources_preserved": True})


@app.command()
@initialized
def export(ctx: typer.Context, destination: Path, project: str | None = None, markdown: bool = False):
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"] if project else None
    data = store.export(pid)
    if markdown:
        parts = ["# Cairnmind — memory export\n"]
        for record in data.get("records", []):
            parts.append(f"## {record['kind']}: {record.get('key') or record['id']}\n\n{record['content']}\n")
            parts.append("```json\n" + json.dumps(record, ensure_ascii=False, indent=2) + "\n```\n")
        for src in data.get("sources", []):
            parts.append(f"## Source {src['id']}\n\nURI: {src['uri']}\n\n````text\n{src['content']}\n````\n")
        text = "\n".join(parts)
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    destination.write_text(text, encoding="utf-8")
    output({"export": str(destination.resolve())})


@app.command()
@initialized
def backup(ctx: typer.Context, destination: Path):
    output({"backup": str(store_for(ctx.obj).backup(destination))})


@app.command()
@initialized
def restore(ctx: typer.Context, source: Path):
    with (ctx.obj.data_dir / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise typer.BadParameter("Run cairnmind stop first; the service still holds the store lock")
        if service_alive(ctx.obj):
            raise typer.BadParameter("Run cairnmind stop first; restoring requires a stopped service")
        store_for(ctx.obj).restore(source)
    output({"restored": True})


@app.command()
@initialized
def forget(ctx: typer.Context, project: str,
           delete_project: Annotated[bool, typer.Option("--delete-project", help="Explicitly delete the project's data")] = False):
    if not delete_project:
        raise typer.BadParameter("Deleting requires --delete-project. Existing backups stay unchanged.")
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    store.forget_project(pid)
    output({"deleted": pid, "backups_unchanged": True})


@app.command()
@initialized
def benchmark(ctx: typer.Context, project: str, question: str):
    """Time the first answer and the cached repeat; this does not replace a quality evaluation."""
    runs = []
    for label in ("first", "cached"):
        started = time.perf_counter()
        result = request(ctx.obj, "POST", "/ask", json={"project": project, "question": question})
        runs.append({"run": label, "seconds": round(time.perf_counter() - started, 3),
                     "status": result["status"], "cached": result.get("cached"),
                     "sources": len(result.get("sources", []))})
    try:
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        gpu = "nvidia-smi unavailable"
    output({"runs": runs, "gpu": gpu})
