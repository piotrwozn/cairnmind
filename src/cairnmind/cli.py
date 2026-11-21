"""Cairnmind CLI; native coding clients retain their own terminal and permissions."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
from contextlib import contextmanager
from enum import Enum
from functools import wraps
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated

import httpx
import typer

from cairnmind.cli_arguments import (
    AnswerIdArg, ClientArg, ExportArg, FolderArg, GroupArg, JobIdArg, PacketHashArg, PacketIdArg, PlanIdArg,
    ProfileArg, ProjectArg, ProjectNameArg, ProjectRootArg, QuestionArg, ReadbackArg, RecordIdArg, RepositoryArg,
    SessionClientArg, SessionIdArg, SnapshotArg, SourceIdArg, StateArg, StateFileArg, TaskIdArg, TitleArg,
    TranscriptArg)
from cairnmind.config import SERVICE_NAMES, Settings, drop_env, session_value
from cairnmind.ollama import OllamaUnavailable
from cairnmind.arcade import ArcadeError

app = typer.Typer(no_args_is_help=False, pretty_exceptions_enable=False,
                  help="A terminal workspace with memory: Codex, Claude, Ollama, API and GitHub Copilot.")
projects_app = typer.Typer(help="The registry of projects and their folders.")
tasks_app = typer.Typer(help="Shared plan steps, their states and verification evidence.")
models_app = typer.Typer(help="The knowledge agent's models: the local Ollama, or a server you choose.")
engine_app = typer.Typer(help="The ArcadeDB engine: native with Java 21+, or in Docker.")
plans_app = typer.Typer(help="Versioned plans and a deterministic next step.")
checks_app = typer.Typer(help="Named project checks and how current their evidence is.")
stage_app = typer.Typer(help="Which assistant or check the next plan step goes to. Starts nothing.")
sessions_app = typer.Typer(help="Resume packets, the scope of history and acknowledgements.")
eval_app = typer.Typer(help="Isolated evaluation of the real memory.")
jobs_app = typer.Typer(help="Diagnostics and explicit retry of one project's jobs.")
memory_app = typer.Typer(help="Portable memory of one project and the local snapshot schedule.")
cockpit_app = typer.Typer(help="The local cockpit, persistent terminals and the memory view.")
workspace_app = typer.Typer(help="The terminal workspace: status, detaching and stopping.")
store_app = typer.Typer(help="How large the store is, which installation writes it, and removing rows that nothing reads.")
db_app = typer.Typer(help="Access to a database on another host: its own user (#72).")
answers_app = typer.Typer(help="The opt-in log of ask answers and the evidence they cited.")
app.add_typer(projects_app, name="project")
app.add_typer(tasks_app, name="task")
app.add_typer(models_app, name="models")
app.add_typer(engine_app, name="engine")
app.add_typer(plans_app, name="plan")
app.add_typer(checks_app, name="check")
app.add_typer(stage_app, name="stage")
app.add_typer(sessions_app, name="session")
app.add_typer(eval_app, name="eval")
app.add_typer(jobs_app, name="jobs")
app.add_typer(memory_app, name="memory")
app.add_typer(cockpit_app, name="cockpit")
app.add_typer(workspace_app, name="workspace")
app.add_typer(store_app, name="store")
app.add_typer(db_app, name="db")
app.add_typer(answers_app, name="answers")


def output(value) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def initialized(command):
    """Initialize only when an operational leaf runs, after argument/help parsing."""
    @wraps(command)
    def invoke(ctx, *args, **kwargs):
        ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
        if ctx.obj._origin.get("connect_saved"):
            from cairnmind.database_mode import saved_note
            typer.echo(f"Cairnmind: {saved_note(ctx.obj)}", err=True)
        return command(ctx, *args, **kwargs)
    return invoke


def entrypoint() -> None:
    _NOTICES_SHOWN.clear()
    try:
        app()
    except (ValueError, OSError, ArcadeError, httpx.HTTPError, OllamaUnavailable) as exc:
        from cairnmind.store_writer import StoreNotWriter
        if not (isinstance(exc, StoreNotWriter) and str(exc) in _NOTICES_SHOWN):
            # The reader's notice was printed when the store opened: the refusal is not said twice (#244).
            typer.echo(_pasteable(f"Cairnmind: {_os_error_text(exc) if isinstance(exc, OSError) else exc}"),
                       err=True)
        raise SystemExit(1) from None


# The data directory of the running command, set by the app callback, for the commands a refusal prints.
_INVOCATION: dict = {}


def _data_dir_arguments(data_dir) -> list[str]:
    """`--data-dir DIR` when a plain shell would use another directory: CAIRNMIND_HOME, set by a wrapper, is not
    set in the shell a printed command is pasted into (#244)."""
    from cairnmind.config import default_data_dir
    return [] if data_dir is None or data_dir == default_data_dir(environ={}) else ["--data-dir", str(data_dir)]


def _pasteable(message: str) -> str:
    """A refusal whose register command (`cairnmind project root ...`, from sql.unregistered_root, built where no
    settings are known) acts on this installation when pasted, not on the default one. Only that command: it
    writes to the store, so pasting it bare would register the folder in another installation. Other commands a
    refusal names (`cairnmind init`, `cairnmind serve`) are printed as they are."""
    arguments = _data_dir_arguments(_INVOCATION.get("data_dir"))
    if not arguments or "`cairnmind project root " not in message:
        return message
    import shlex
    from cairnmind.terminal_safe import safe_text
    return message.replace("`cairnmind project root ", f"`cairnmind {safe_text(shlex.join(arguments))} project root ")


def _json_file(path: Path):
    """A JSON file named on the command line. One that is not JSON is refused in one line that names it, as one of
    another shape is, not as the parser's bare message (#244)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:  # JSONDecodeError, or bytes that are not UTF-8
        refuse(f"The file {_shortened(str(path))} is not valid JSON: {_shortened(str(exc), 300)}")


def _shortened(text: str, limit: int = 200) -> str:
    from cairnmind.terminal_safe import safe_text
    text = safe_text(text)
    return text if len(text) <= limit else text[:limit // 2 - 1] + "…" + text[-(limit // 2):]


def _os_error_text(exc: OSError) -> str:
    """An operating-system error about a path as a sentence, with a long path shortened (#244)."""
    import errno
    if exc.errno is None or exc.filename is None:
        return str(exc)
    import os
    path = _shortened(os.fsdecode(exc.filename) if isinstance(exc.filename, bytes) else str(exc.filename))
    if exc.errno == errno.ENOENT:
        return f"{path} does not exist."
    if exc.errno == errno.ENAMETOOLONG:
        return f"The path is too long for this system: {path}"
    return f"{exc.strerror or 'The path cannot be used'}: {path}"


_NOTICES_SHOWN: set[str] = set()


def store_for(settings: Settings, *, notice: bool = True):
    """The full store. notice=False for a command that only reads it on a reader, as backup and export do."""
    from cairnmind.store import Store
    store = Store(settings.data_dir)
    if store.not_writer and notice:
        # Another installation writes this database (#70): reads work, every write is refused with this reason.
        typer.echo(f"Cairnmind: {store.not_writer}", err=True)
        _NOTICES_SHOWN.add(store.not_writer)
    return store


def store_for_read(settings: Settings):
    """The existing store for a command that only reads. It starts no engine and creates nothing: when the
    configuration, engine, database or schema is not ready it fails at once, saying what to run."""
    from cairnmind.store import Store
    return Store.open_readonly(settings)


# The journal entity types (journal.TABLE_TYPES) each read-only command reads; a commit of one of
# them in the command's project during the read makes it read again. Resolving the project and its
# worktree reads `project` and `scope` (projects, project_roots, worktree_states). Sessions, read only
# for the bound session's own project and worktree, which do not change, are left out on purpose:
# session events are written all the time. tests/test_store_readonly.py checks each set against the
# tables the command really queries.
_RESOLVE = frozenset({"project", "scope"})
READ_KINDS = {
    "records": _RESOLVE | {"record"},
    "task list": _RESOLVE | {"record", "check"},
    "plan list": _RESOLVE | {"plan"},
    "plan show": _RESOLVE | {"plan", "record"},
    "plan next": _RESOLVE | {"plan", "record", "check"},
    "plan contract": _RESOLVE | {"plan", "record", "check"},
    "check profiles": _RESOLVE,
    "memory orphans": _RESOLVE | {"record", "source", "memory"},
    "memory extraction-stats": _RESOLVE | {"record"},
    "impact": _RESOLVE | {"record", "source", "memory", "node", "edge", "graph"},
    "usage": _RESOLVE,
    "proposed": _RESOLVE | {"record", "source", "memory"},
}


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
    code = None
    if session_id:
        from cairnmind.integrations import drain_hook_spool
        from cairnmind.sessions import SessionManager
        drain_hook_spool(settings, store)
        manager = SessionManager(store, settings)
        # Hashed before the transaction: inside it, a slow worktree let ArcadeDB expire it (#255).
        code = manager.code_scope(session_id)
    with store.transaction():
        scope = actor_scope(store, project_id, worktree)
        if session_id:
            manager.require_ack(session_id, code=code)
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


def last_log_line(path: Path) -> str:
    """The last line of a log, bounded and with control characters replaced: what a stopped process said."""
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 4096))
            lines = [line for line in handle.read().decode("utf-8", "replace").splitlines() if line.strip()]
    except OSError:
        return f"see {path}"
    text = lines[-1] if lines else f"see {path}"
    return "".join(char if char.isprintable() else "?" for char in text)[:500]


_PORT_ADVICE = "Give this data directory its own port: `port` in {}/config.json, or CAIRNMIND_PORT for one command."


def _bind_failed(log: Path, since: int) -> bool:
    """Whether what this start wrote to the log (from byte ``since``, not an earlier start's) says the port is taken."""
    try:
        with log.open("rb") as handle:
            handle.seek(since)
            return b"address already in use" in handle.read(65536).lower()
    except OSError:
        return False


def ensure_service(settings: Settings, *, background_inference: bool = True) -> None:
    if service_alive(settings):
        return
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log = settings.data_dir / "service.log"
    with log.open("ab") as handle:
        started_at = handle.tell()
        env = dict(os.environ)
        # The service is a shared system worker, not a child actor of one coding session.
        drop_env(env, "SESSION_ID", "PACKET_ID")
        command = [sys.executable, "-m", "cairnmind", "--data-dir", str(settings.data_dir), "serve"]
        if not background_inference:
            command.append("--defer-background-inference")
        process = subprocess.Popen(command,
                                   stdin=subprocess.DEVNULL, stdout=handle, stderr=handle, start_new_session=True, env=env)
    for _ in range(300):
        if service_alive(settings):
            return
        if getattr(process, "poll", lambda: None)() is not None:
            # It stopped instead of starting, for example on an installation that only reads the store (#70).
            # One that met a service starting at the same moment is waited for, as before.
            said = last_log_line(log)
            if _bind_failed(log, started_at):  # uvicorn's own last line is only "Application shutdown complete" (#267)
                raise ServiceDidNotStart(f"The service stopped at start: port {settings.port} is already in use, by "
                                         f"another program or by the service of another data directory. "
                                         f"{_PORT_ADVICE.format(settings.data_dir)}")
            if "already running" not in said:
                # The service's own prefix is not repeated (#244).
                raise ServiceDidNotStart(f"The service stopped at start: {said.removeprefix('Cairnmind: ')}")
            process = None
        time.sleep(0.2)
    raise ServiceDidNotStart(f"The service did not start. Check {log}; port {settings.port} may be in use.")


class ServiceDidNotStart(ValueError):
    """The knowledge service did not start. A command shows it as a refusal, not as a wrong argument (#244);
    the workspace and the cockpit, which call ensure_service too, get an ordinary error with the reason."""


def _ensure_service_or_refuse(settings: Settings) -> None:
    try:
        ensure_service(settings)
    except ServiceDidNotStart as exc:
        refuse(str(exc))


def refuse(message: str, code: int = 2):
    """A refusal that is not about the command line: no usage line and no "Invalid value" (#154)."""
    typer.echo(_pasteable(f"Error: {message}"), err=True)
    raise typer.Exit(code)


def _service_unavailable(settings: Settings) -> str:
    """Why a diagnostic that asks the running service got no answer, and what to do. A reader never runs the
    service (#70), so it is not told to start one (#244)."""
    from cairnmind.store_writer import inspect
    try:
        role = inspect(settings)
    except Exception:  # noqa: BLE001 - the role only chooses the advice; the refusal is given either way
        role = {}
    if role.get("role") in {"reader", "lost"}:
        from cairnmind.terminal_safe import safe_text
        return (f"{safe_text(role.get('reason') or '')} This command asks the running service, which only the "
                "writer runs. Here `cairnmind store writer` and `cairnmind doctor` show this installation's role, "
                "and the commands that only read the store work.").strip()
    return ("The existing Cairnmind service is unavailable; diagnostics do not start it. "
            "Run `cairnmind serve` explicitly.")


class ServiceRefusal(Exception):
    """The service refused a request made with ``raise_refusal=True``; the message is its sanitized reason."""


def caller_path(project: str, registered=lambda: frozenset()) -> str:
    """A project written as a path that exists here, made absolute: the service reads a relative path from
    its own working directory and refuses it (#238). A name, or a path that does not exist, is sent as given.

    ``registered`` returns the ids and names the service knows, or None when it cannot tell. A registered
    id or name wins over a local folder of the same spelling (a project called ``web/api``), as it does in
    Store.resolve_project and the MCP server; when the service cannot tell, the value is sent as given and
    the service decides (it refuses a relative path rather than guessing)."""
    if project.startswith((".", "~")) or "/" in project:
        local = Path(project).expanduser()
        if local.exists():
            known = registered()
            if known is not None and project not in known:
                return str(local.resolve())
    return project


def registered_projects(client, settings: Settings) -> frozenset[str] | None:
    """The ids and names of the registered projects, or None when the service does not list them."""
    try:
        response = client.request("GET", settings.url + "/projects")
        rows = response.json() if response.is_success else None
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    return frozenset(value for row in rows if isinstance(row, dict)
                     for value in (row.get("id"), row.get("name")) if isinstance(value, str))


def request(settings: Settings, method: str, path: str, *, start_service: bool = True, session: bool = True,
            raise_refusal: bool = False, **kwargs):
    # session=False sends no session of the launched agent (a replay asks for the logged scope, #15);
    # raise_refusal=True raises ServiceRefusal instead of ending the command.
    if start_service:
        _ensure_service_or_refuse(settings)
    elif not (settings.data_dir / "config.json").is_file() or not settings.token:
        refuse("No existing Cairnmind configuration; run `cairnmind init` explicitly.")
    retry_job = path.startswith("/jobs/") and path.endswith("/retry")
    if not session:
        pass
    elif (method.upper() == "POST" and (retry_job or path in {
            "/remember", "/findings", "/checkpoint", "/plans/save", "/plans/activate",
            "/memory/snapshot", "/memory/import", "/memory/publish", "/memory/import-published",
            "/memory/import-commit", "/memory/carrier"})
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
        listed: list = []

        def registered():
            if not listed:
                listed.append(registered_projects(client, settings))
            return listed[0]

        for part in ("json", "params"):
            values = kwargs.get(part)
            if isinstance(values, dict) and isinstance(values.get("project"), str):
                kwargs[part] = {**values, "project": caller_path(values["project"], registered)}
        try:
            response = client.request(method, settings.url + path, **kwargs)
        except httpx.HTTPError:
            if not start_service:
                refuse(_service_unavailable(settings))
            raise
        if not response.is_success:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            # Only where the service was not started or matched by instance id here: after that match a 401 is this
            # directory's own service with a changed token, not another directory's.
            if not start_service and response.status_code == 401 and "Invalid local service token" in str(detail):
                detail = (f"The service at {settings.url} does not accept the token of {settings.data_dir}, so it "
                          f"most likely belongs to another data directory. {_PORT_ADVICE.format(settings.data_dir)}")
            # The service's answer, not a wrong argument; shown as it is, without control characters.
            from cairnmind.terminal_safe import safe_text
            if raise_refusal:
                raise ServiceRefusal(safe_text(str(detail)))
            from cairnmind.sql import missing_row_text
            if response.status_code == 404 or missing_row_text(str(detail)):
                # An unknown id or project is said as the commands that read the store say it (#244).
                typer.echo(_pasteable(f"Cairnmind: {safe_text(str(detail))}"), err=True)
                raise typer.Exit(1)
            refuse(safe_text(str(detail)))
        return response.json()


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context, data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None):
    _INVOCATION.clear()
    ctx.obj = Settings.load(data_dir, create=False)
    _INVOCATION["data_dir"] = ctx.obj.data_dir
    if ctx.invoked_subcommand is None:
        _launch_terminal_workspace(ctx)


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _launch_terminal_workspace(ctx: typer.Context, *, takeover: bool = False) -> None:
    # Check before initialization: redirected CLI diagnostics must never create
    # configuration, launch a browser or silently start interactive processes.
    if not _interactive_terminal():
        refuse(
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
    """Create the store, or open the one a connect-only installation was given.

    Downloads no models and changes no client configuration."""
    from cairnmind.database_mode import connect_only, engine_mode
    from cairnmind.engine_docker import choose_engine_mode, start_docker_engine
    chosen = choose_engine_mode(ctx.obj)  # the first run chooses where the engine runs, and says which (#272)
    if chosen:
        typer.echo(f"Cairnmind: {chosen}", err=True)
    if not connect_only(ctx.obj) and engine_mode(ctx.obj) == "docker":
        start_docker_engine(ctx.obj, pull=True)  # the first start downloads the image; ensure_engine never does
    store = store_for(ctx.obj)
    from cairnmind.knowledge_mode import choose, enabled
    said = choose(ctx.obj)  # the first run chooses whether the knowledge agent has models, and says which (#274)
    if said:
        typer.echo(f"Cairnmind: {said}", err=True)
    result = {"data_dir": str(ctx.obj.data_dir), "next": "cairnmind doctor; cairnmind models pull"}
    result["knowledge_agent"] = "enabled" if enabled(ctx.obj) else "disabled"
    if not connect_only(ctx.obj):
        result["engine_mode"] = engine_mode(ctx.obj)
    if getattr(store, "not_writer", None):
        result["writer"] = store.writer_state()
    from cairnmind.confirmation_upgrade import report
    marks = report(getattr(store, "confirmations_before_rule", None))
    if marks:
        result["confirmations_before_rule"] = marks
    output(result)


@contextmanager
def _engine_command():
    """An engine command on a connect-only installation ends with one plain sentence, not a failure (#68)."""
    from cairnmind.engine import ConnectOnly
    try:
        yield
    except ConnectOnly as exc:
        refuse(str(exc))


def _managed_only(settings: Settings, what: str) -> None:
    from cairnmind.database_mode import connect_only, refusal
    if connect_only(settings):
        refuse(f"{what} creates and drops temporary databases. " + refusal(settings, "create or drop a database")
               + " Run it on an installation with the engine on this machine.")


@engine_app.command("install")
@initialized
def engine_install(ctx: typer.Context, distribution: Path | None = None):
    """Download the pinned ArcadeDB engine and verify its SHA-256 (about 87 MB)."""
    from cairnmind.engine import install_engine
    with _engine_command():
        path = install_engine(ctx.obj, distribution)
    output({"installed": str(path), "version": ctx.obj.arcade_version})


@engine_app.command("start")
@initialized
def engine_start(
        ctx: typer.Context,
        docker: Annotated[bool, typer.Option("--docker", help="Run the engine in Docker; saved as engine_mode.")] = False,
        native: Annotated[bool, typer.Option(
            "--native", help="Run the engine with Java 21 on this machine; saved as engine_mode.")] = False,
        build: Annotated[bool, typer.Option(
            "--build", help="With --docker: build the image on this machine from the files shipped with "
                            "Cairnmind, instead of downloading it.")] = False):
    """Start the database engine in its saved mode, or in the one --docker or --native names."""
    if docker and native:
        raise typer.BadParameter("Choose one of --docker and --native.")
    if build and not docker:
        raise typer.BadParameter("--build goes with --docker.")
    from cairnmind.engine import start_command
    with _engine_command():
        output(start_command(ctx.obj, docker=docker, native=native, build=build))


engine_app.command("up", help="The same as `cairnmind engine start`.")(engine_start)


@engine_app.command("status")
@initialized
def engine_inspect(ctx: typer.Context):
    """Whether the database server answers, where it is and which version it runs. Starts nothing."""
    from cairnmind.engine import engine_status
    result = engine_status(ctx.obj)
    output(result)
    if result.get("problem") or result.get("available") is None:
        # Connect-only (#68): a server of a version this release does not support is refused, not only shown,
        # and so is a configuration that breaks a rule of #72, with which the server was not asked (#242).
        raise typer.Exit(1)


@engine_app.command("stop")
@initialized
def engine_stop(ctx: typer.Context):
    from cairnmind.engine import local_process_recorded, stop_engine
    from cairnmind.engine_docker import container_recorded
    from cairnmind.database_mode import connect_only
    # A connect-only installation without an engine of its own has nothing to stop, whether or not its
    # service runs: it says so, instead of asking to stop the service first (#68).
    nothing_here = (connect_only(ctx.obj) and not local_process_recorded(ctx.obj)
                    and not container_recorded(ctx.obj))
    if not nothing_here and service_alive(ctx.obj):
        refuse("Run cairnmind stop first; the knowledge service is using ArcadeDB")
    with _engine_command():
        output(stop_engine(ctx.obj))


@db_app.command("create-user")
@initialized
def db_create_user(ctx: typer.Context, admin_password_file: Annotated[Path, typer.Option(
        "--admin-password-file", help="A private file (mode 0600) with the root password of the server.")]):
    """Create the database and the user arcade_user with rights on it only, signing in once as root.

    The new user's password is the one configured for it: CAIRNMIND_ARCADE_PASSWORD or arcade_password_file.
    """
    from cairnmind.database_credentials import admin_password, create_user
    admin = admin_password(str(admin_password_file.expanduser().absolute()))
    output(create_user(ctx.obj, admin))


@app.command()
@initialized
def doctor(ctx: typer.Context, contract: bool = typer.Option(
        False, "--contract", help="Show only the client contract: every check with its evidence line."),
           output_format: Annotated[str, typer.Option(
               "--format", help="With --contract: json (default) or text, one line per client and check.")] = "json"):
    """Report where storage and the models run, Python, Java, tmux, the engine, Ollama and the coding clients. Read-only."""
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
    from cairnmind.knowledge_mode import doctor_fields
    result.update(doctor_fields(ctx.obj))  # without models: information, not a fault (#274)
    result["service_running"] = service_alive(ctx.obj)
    result["data_dir"] = str(ctx.obj.data_dir)
    result["service_url"] = ctx.obj.url  # the address every command here talks to, so a collision is visible
    from cairnmind.config import ENV_PREFIXES, data_dir_note, env_conflicts, env_names, session_conflicts
    # Shown here and nowhere else: a hook or an MCP server must not print notes of its own.
    result["environment_notes"] = [
        f"{ENV_PREFIXES[0]}{name} and {ENV_PREFIXES[1]}{name} are both set and differ; {ENV_PREFIXES[0]}{name} is used."
        for name in env_conflicts()] + [
        f"{ENV_PREFIXES[0]}{name} and {ENV_PREFIXES[1]}{name} name different sessions; neither is used: commands in "
        "this shell are refused and hooks and the MCP server stay unbound. Start the client again through the launcher."
        for name in session_conflicts()] + [note for note in [data_dir_note()] if note]
    for name, variable in (("port", "PORT"), ("cockpit_port", "COCKPIT_PORT"), ("ollama_url", "OLLAMA_URL")):
        if name in ctx.obj._origin.get("kept", {}):
            stored, used = ctx.obj._origin["kept"][name]
            spelling = next(key for key in env_names(variable) if os.environ.get(key))
            result["environment_notes"].append(
                f"{spelling} is used by this process: {name} is {used} here, and {stored} in config.json.")
    from cairnmind.engine import log_settings_problems
    # The same lines `engine start` prints, so a mistyped log setting is not seen only at start (#238).
    result["environment_notes"] += [line.removeprefix("cairnmind: ") for line in log_settings_problems(ctx.obj)]
    result["models_lock_present"] = (ctx.obj.data_dir / "models.lock.json").exists()
    from cairnmind.store_writer import inspect as writer_role
    result["store_writer"] = writer_role(ctx.obj)
    from cairnmind.api_runtime import coding_profiles_status
    result["coding_profiles"] = coding_profiles_status(ctx.obj)
    from cairnmind.record_kinds import doctor_view
    try:
        # What a person accepted as technical, per project, and how to withdraw it (#210).
        result["accepted_record_kinds"] = doctor_view(store_for_read(ctx.obj))
    except Exception as exc:  # noqa: BLE001 - doctor reports a store it cannot read, it does not stop
        result["accepted_record_kinds"] = {"unavailable": type(exc).__name__}
    from cairnmind.topology import topology
    # From what doctor found above, so it asks nothing more (#74): first, so the JSON reads top-down.
    result = {"topology": topology(ctx.obj, models=result.get("memory_models"), writer=result["store_writer"]), **result}
    output(result)


@app.command()
@initialized
def setup(ctx: typer.Context, as_json: Annotated[bool, typer.Option("--json", help="Print the topology as JSON.")] = False):
    """Show where storage and the memory models run, and what runs on this machine. Changes no setting of an initialized installation."""
    from cairnmind.topology import CHANGE_HINT, probe
    place = probe(ctx.obj)
    if as_json:
        output(place)
    else:
        typer.echo("\n".join([*place["lines"], "", CHANGE_HINT]))


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
            refuse("The service is already running for this store.")
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
def project_add(ctx: typer.Context, name: ProjectNameArg, root: ProjectRootArg):
    root = root.expanduser().resolve(strict=True)  # the folder is checked before the store is opened
    if not root.is_dir():
        raise typer.BadParameter("The path must be a folder, not a file")
    store = store_for(ctx.obj)
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
def project_root(ctx: typer.Context, project: ProjectArg, path: FolderArg):
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
def index(ctx: typer.Context, project: ProjectArg):
    """Index the project's files again through the service, which it starts when needed."""
    output(request(ctx.obj, "POST", "/ingest", json={"project": project}))


@app.command("import")
@initialized
def import_command(ctx: typer.Context, path: TranscriptArg, project: str | None = None, client: str = "auto"):
    """Import a client's JSONL; without a project, keep the history in the import inbox."""
    output(request(ctx.obj, "POST", "/import", json={"path": str(path.expanduser().resolve()),
                                                    "project": project, "client": client}))


@app.command()
@initialized
def ask(ctx: typer.Context, project: ProjectArg, question: QuestionArg, worktree: Path | None = None,
        answer_schema: Path | None = None,
        log: Annotated[bool, typer.Option("--log", help="Keep this answer and its citations in the project's "
                                          "answer log, for `cairnmind eval replay`.")] = False):
    """Answer a question from the project's memory, with the sources it cites. Starts the service when needed."""
    payload = {"project": project, "question": question,
               "worktree": str(worktree.resolve()) if worktree else None}
    if log:
        payload["log"] = True
    if answer_schema is not None:
        from cairnmind.answer_contract import prepare_answer_schema
        payload["answer_schema"] = prepare_answer_schema(_json_file(answer_schema))
    output(request(ctx.obj, "POST", "/ask", json=payload))


@app.command()
@initialized
def context(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None):
    """Show the context a coding session starts with: checkpoint, tasks and records. Starts the service when needed."""
    output(request(ctx.obj, "GET", "/context", params={"project": project,
                   **({"worktree": str(worktree.resolve())} if worktree else {})}))


@app.command()
@initialized
def brief(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None,
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
def impact(ctx: typer.Context, project: ProjectArg,
           since: Annotated[str, typer.Option("--since", help="Commit, tag or branch to compare the worktree with.")],
           worktree: Path | None = None,
           as_json: Annotated[bool, typer.Option("--json", help="Print the linked records as JSON.")] = False,
           pending: Annotated[bool, typer.Option(
               "--pending", help="List only records without a current verdict.")] = False):
    """List records linked to the files and symbols changed since a commit. Read-only; asks no model."""
    from cairnmind.impact import build_impact
    store = store_for_read(ctx.obj)

    def read():
        pid = store.resolve_project(project)["id"]
        return pid, build_impact(store, pid, since, actor_scope(store, pid, worktree), pending=pending)

    result = store.read_consistently(read, READ_KINDS["impact"])
    if as_json:
        output(result)
    else:
        typer.echo(result["text"])


@app.command("impact-verdict")
@initialized
def impact_verdict(ctx: typer.Context, project: ProjectArg, record_id: RecordIdArg,
                   fingerprint: Annotated[str, typer.Option(help="The entry's fingerprint, from --json.")],
                   verdict: Annotated[str, typer.Option(help="still_valid, needs_update or not_related.")],
                   since: Annotated[str, typer.Option(help="The commit the list was made against.")],
                   note: Annotated[str | None, typer.Option(help="Required for needs_update.")] = None,
                   worktree: Path | None = None):
    """Record your verdict on one record listed by `impact`. Changes no record and no task."""
    from cairnmind.impact_verdicts import record_verdict
    # A verdict is a person's judgement, as `retry-jobs` is a person's act: a coding session cannot give one.
    if session_value("SESSION_ID"):
        raise typer.BadParameter("A coding session cannot record an impact verdict; only a person can, "
                                 "outside the session.")
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    try:
        result = record_verdict(store, pid, record_id, fingerprint, verdict, since,
                                str(worktree.expanduser().resolve()) if worktree else None, note)
    except LookupError as exc:
        # The record is not in that list (it may exist): a refusal of the state, not a usage error (#244).
        refuse(str(exc))
    output(result)


class UsageBy(str, Enum):
    day = "day"
    week = "week"


@app.command()
def usage(ctx: typer.Context, project: ProjectArg,
          since: Annotated[str, typer.Option("--since", help="How far back to look: Nd (days) or Nh (hours).")] = "7d",
          by: Annotated[UsageBy, typer.Option("--by", help="Group by UTC day or by ISO week.")] = UsageBy.day,
          warn_over: Annotated[int | None, typer.Option(
              "--warn-over", min=1, help="Warn when input plus output tokens of the period exceed this; blocks "
              # Help is rendered through Rich: an unescaped [usage] would be read as a style tag and dropped.
              "nothing. Default: warn_over_tokens under \\[usage] in .cairnmind/checks.toml.")] = None,
          as_json: Annotated[bool, typer.Option("--json", help="Print the usage as JSON.")] = False):
    """Sum the token usage of the built-in API agent per day or week, profile and model. Asks no model."""
    from cairnmind.codex_usage import stored_records, waiting_in_inbox
    from cairnmind.usage import build_usage
    from cairnmind.verification import usage_warn_threshold
    # The store is read, never prepared: it needs the running engine and never starts it. The usage itself is
    # read from the session journals and, for Codex, from the store; nothing is written anywhere.
    store = store_for_read(ctx.obj)

    def read():
        selected = store.resolve_project(project)
        return selected["id"], selected

    found = store.read_consistently(read, READ_KINDS["usage"])
    # Codex usage is read from the transcript lines that history import stored for this project, not from files.
    # It is counted while import may still be writing, so not repeated: the pages of stored lines and of the
    # inbox go by key (codex_usage._pages), and a row present during the whole read is counted exactly once.
    unavailable = None
    if warn_over is None:
        try:
            warn_over = usage_warn_threshold(store, found["id"])
        except (ValueError, OSError, RecursionError) as exc:
            # A report must not depend on the checks file: say why there is no default threshold and go on.
            # (tomllib raises RecursionError, not a TOML error, on a deeply nested array.)
            unavailable = ("the checks file is nested too deeply to read" if isinstance(exc, RecursionError)
                           else str(exc))
    try:
        result = build_usage(ctx.obj.data_dir, found, since, by=by.value, warn_over=warn_over,
                             codex_records=lambda start: stored_records(store, found["id"], start),
                             codex_waiting=lambda start: waiting_in_inbox(store, found["id"], start),
                             warn_unavailable=unavailable)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--since")
    if as_json:
        output(result)
    else:
        typer.echo(result["text"])


@app.command()
def status(ctx: typer.Context, project: str | None = None):
    """Ask the running service for its queue, store and model state. Starts nothing."""
    output(request(ctx.obj, "GET", "/status", start_service=False,
                   params={"project": project} if project else {}))


@store_app.command("size")
def store_size(ctx: typer.Context, project: Annotated[str | None, typer.Argument(
                   metavar="PROJECT",
                   help="Measure only this project's rows; the disk use is always the whole data directory.")] = None,
               as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False):
    """Rows and approximate bytes per collection, kind and namespace, and the disk use of the data directory.

    Read-only: it never starts the engine or the service and changes nothing. Prints no record or source text."""
    from cairnmind.store_size import measure
    store = store_for_read(ctx.obj)
    report = measure(store, store.resolve_project(project)["id"] if project is not None else None)
    if as_json:
        output(report)
    else:
        typer.echo(report["text"])


@store_app.command("writer")
@initialized
def store_writer_show(ctx: typer.Context):
    """Which installation writes this store, and whether this one is it. Starts no engine; writes no row."""
    from cairnmind.store_writer import inspect
    output(inspect(ctx.obj))


# What `store takeover` without --yes suggests, by the role it read (#244).
TAKEOVER_NEXT = {
    "writer": "This installation is already the writer; nothing to do.",
    "unknown": ("The role could not be read. Start the engine with `cairnmind engine start` and run this again, "
                "or pass --yes, which starts it and makes this installation the writer."),
    None: "Run `cairnmind store takeover --yes` to make this installation the writer.",
}


@store_app.command("takeover")
@initialized
def store_takeover(ctx: typer.Context,
                   yes: Annotated[bool, typer.Option("--yes", help="Take over writing; without it nothing changes.")] = False,
                   note: Annotated[str | None, typer.Option(help="Why, kept with the change (one line).")] = None):
    """Make this installation the store's writer.

    The previous writer stops at its next write; its running jobs go back to the queue. The change, with its time, host and note, is kept in the store.
    """
    from cairnmind.store_writer import inspect, take_over_at, valid_note
    # Moving the writer is a person's decision, as a verdict is: a coding session cannot make it.
    if session_value("SESSION_ID"):
        refuse("A coding session cannot take over writing the store; only a person can, outside the session.")
    try:
        valid_note(note)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--note") from None
    if not yes:
        current = inspect(ctx.obj)
        output({"taken_over": False, **current, "next": TAKEOVER_NEXT.get(current.get("role"), TAKEOVER_NEXT[None])})
        raise typer.Exit(1)
    output(take_over_at(ctx.obj.data_dir, note))


@store_app.command("drop-replaced-chunks")
@initialized
def store_drop_replaced_chunks(
        ctx: typer.Context,
        batch: Annotated[int, typer.Option(min=1, max=1000, help="Rows (chunks or vectors) deleted per transaction.")] = 200,
        max_batches: Annotated[int | None, typer.Option(
            min=1, help="Stop after this many transactions; run again to go on.")] = None):
    """Delete the chunks that earlier releases kept for replaced sources, in every project; prints how many rows went.

    Nothing searches them. Every source stays with its full text and hash, so evidence citing an old version is still readable and checkable. Short transactions, safe to stop and run again (#186).
    """
    from cairnmind.replaced_chunks import drop_replaced_chunks
    output(drop_replaced_chunks(store_for(ctx.obj), batch=batch, max_batches=max_batches))


@store_app.command("prune-backups")
def store_prune_backups(
        ctx: typer.Context,
        keep: Annotated[int, typer.Option(min=0, max=1000, help="Safety backups of each kind to keep, newest first.")],
        yes: Annotated[bool, typer.Option("--yes", help="Delete without asking again.")] = False):
    """Delete old safety backups (before-schema-*, before-restore-*), keeping the newest K of each kind.

    Shows what it would delete and asks first. Only a person in their own terminal can run it; scheduled backups and backups you named are never touched. Starts no engine (#190).
    """
    from cairnmind.confirmation import person_at_terminal
    from cairnmind.store_prune import delete_backups, prune_plan
    directory = ctx.obj.data_dir / "backups"
    plan = prune_plan(directory, keep)
    if not person_at_terminal():
        refuse("Only a person can delete safety backups: run this command in your own terminal, outside a "
               "coding session, with input and output not piped or redirected. Nothing was deleted.")
    shown = {"directory": plan["directory"], "keep": keep, "delete_bytes": plan["delete_bytes"],
             "delete": [{key: entry[key] for key in ("name", "kind", "bytes", "modified")} for entry in plan["delete"]],
             "kept": [entry["name"] for entry in plan["kept"]]}
    if not plan["delete"]:
        output({**shown, "deleted": []})
        return
    output(shown)
    if not yes and not typer.confirm(f"Delete these {len(plan['delete'])} safety backup(s)?", default=False):
        output({"deleted": [], "note": "Nothing was deleted."})
        raise typer.Exit(1)
    output(delete_backups(directory, plan))


@jobs_app.command("list")
def jobs_list(ctx: typer.Context, project: ProjectArg,
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
def jobs_show(ctx: typer.Context, project: ProjectArg, job_id: JobIdArg):
    """Read a job that belongs to the given project."""
    output(request(ctx.obj, "GET", f"/jobs/{job_id}", start_service=False, params={"project": project}))


@jobs_app.command("retry")
@initialized
def jobs_retry(ctx: typer.Context, project: ProjectArg, job_id: JobIdArg,
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
def records(ctx: typer.Context, project: ProjectArg, history: bool = False):
    """List the project's records as JSON; --history adds the replaced ones. Read-only; starts nothing."""
    store = store_for_read(ctx.obj)

    def read():
        pid = store.resolve_project(project)["id"]
        return pid, store.list_records(pid, include_history=history)

    output(store.read_consistently(read, READ_KINDS["records"]))


@app.command()
def proposed(ctx: typer.Context, project: ProjectArg,
             days: Annotated[int, typer.Option("--days", min=0, max=3650,
                                               help="List the ones not updated for this many days or more.")] = 30,
             worktree: Path | None = None,
             as_json: Annotated[bool, typer.Option("--json", help="Print the list as JSON.")] = False):
    """List proposed findings not updated for a number of days, oldest first. Read-only; asks no model.

    Each one shows its id, kind, source count and newest evidence time. Confirm or dispute them in the memory inspector, which records your review with its version check and audit note (#189).
    """
    from cairnmind.inspector import Inspector
    from cairnmind.stale_proposed import stale_proposed, stale_text
    store = store_for_read(ctx.obj)

    def read():
        selected = store.resolve_project(project)
        inspector = Inspector(store)
        scope = inspector._scope(selected["id"], actor_scope(store, selected["id"], worktree))
        result = stale_proposed(store, selected["id"], days=days, matches=lambda row: inspector._matches(scope, row))
        return selected["id"], {**result, "text": stale_text(result, selected["name"])}

    result = store.read_consistently(read, READ_KINDS["proposed"])
    if as_json:
        output(result)
    else:
        typer.echo(result["text"])


@app.command()
def source(ctx: typer.Context, source_id: SourceIdArg):
    """Read the stored snapshot of a source, even when the original no longer exists."""
    output(store_for_read(ctx.obj).get_source(source_id))


@app.command()
@initialized
def remember(ctx: typer.Context, project: ProjectArg,
             content: Annotated[str, typer.Argument(
                 metavar="CONTENT", help="The text to keep. Text that begins with - goes last, after --, with the "
                                         "options before it: `cairnmind remember PROJECT --kind decision -- '-text'`.")],
             kind: str = "fact",
             source_id: Annotated[list[str] | None, typer.Option("--source")] = None,
             confirmed: bool = False, key: str | None = None):
    """Store a finding as proposed. --confirmed stores it as confirmed, for a person in their own terminal."""
    from cairnmind import confirmation
    if confirmed and not confirmation.person_at_terminal():
        # Only a person confirms (#177). A coding session's shell and an agent's command are not one:
        # refused before anything is written, with what to do instead.
        refuse(confirmation.CLI_REFUSED)
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    from cairnmind.store import KeyTaken
    try:
        with actor_write(store, ctx.obj, pid):
            record = {"kind": kind, "content": content, "status": "confirmed" if confirmed else "proposed"}
            if key is not None:
                record["key"] = key
            # Known secrets are replaced in the note's indexed view (#182); the note itself is kept as typed.
            from cairnmind.secret_mask import note_metadata
            sources = source_id or [store.snapshot(pid, f"manual://{uuid.uuid4()}", content, "manual",
                                                  note_metadata(store.settings, content,
                                                                {"role": "user", "origin": "cli"}))["id"]]
            if confirmed:
                result = confirmation.remember_as_person(store, pid, record, sources)
            else:
                result = store.remember(pid, record, sources)
    except (KeyTaken, confirmation.VerdictKept) as exc:
        if getattr(exc, "change", "key") != "key":
            raise
        # The key is taken and this command cannot update a record by id: what the person can do (#238).
        refuse(f"Record {exc.record_id} already holds the key {key!r} for a {kind}. Nothing was saved. Use "
               "another key, or dispute or decide that record in the inspector.")
    output(result)


@app.command()
@initialized
def checkpoint(ctx: typer.Context, project: ProjectArg, state_file: StateFileArg):
    """Store where work stands, from a JSON file, as the project's checkpoint. Starts the service when needed."""
    output(request(ctx.obj, "POST", "/checkpoint", json={"project": project,
                   "state": _json_file(state_file)}))


@tasks_app.command("list")
def task_list(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None):
    from cairnmind.verification import verification_status
    store = store_for_read(ctx.obj)

    def read():
        pid = store.resolve_project(project)["id"]
        scope = actor_scope(store, pid, worktree)
        tasks = [r for r in store.list_records(pid) if r["kind"] == "task"]
        return pid, [{**task, "verification": verification_status(store, task, scope)} for task in tasks]

    output(store.read_consistently(read, READ_KINDS["task list"]))


@tasks_app.command("add")
@initialized
def task_add(ctx: typer.Context, project: ProjectArg, title: TitleArg, plan: str | None = None, step: int = 1,
             criterion: Annotated[list[str] | None, typer.Option("--criterion")] = None,
             depends_on: Annotated[list[str] | None, typer.Option("--depends-on")] = None,
             requires_test: bool = False, requires_review: bool = False):
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    metadata = {"state": "todo", "plan_id": plan, "step_index": step,
                "acceptance_criteria": criterion or [], "depends_on": depends_on or [],
                "requires_test": requires_test, "requires_review": requires_review}
    text = json.dumps({"title": title, **metadata}, ensure_ascii=False)
    from cairnmind import confirmation
    # A task a person adds in their own terminal is confirmed, as before. Inside a coding session, from an
    # agent's command or a script there is no person (#177): the task is stored as proposed, and says so.
    person = confirmation.person_at_terminal()
    with actor_write(store, ctx.obj, pid):
        from cairnmind.secret_mask import note_metadata  # indexed with its known secrets replaced (#182)
        src = store.snapshot(pid, f"manual://task/{uuid.uuid4()}", text, "manual",
                             note_metadata(store.settings, text, {"role": "user"}))
        record = {"kind": "task", "content": title, "status": "confirmed" if person else "proposed",
                  "metadata": metadata}
        if person:
            result = confirmation.remember_as_person(store, pid, record, [src["id"]])
        else:
            result = store.remember(pid, record, [src["id"]])
    if not person:
        typer.echo("Stored as proposed: only a person in their own terminal adds a confirmed task. This command "
                   "runs inside a coding session, or its input or output is not a terminal (piped or redirected). "
                   "The person confirms it in the inspector.", err=True)
    output(result)


@tasks_app.command("update")
@initialized
def task_update(ctx: typer.Context, project: ProjectArg, task_id: TaskIdArg, state: StateArg,
                evidence: Path | None = None, test_passed: bool = False,
                review_passed: bool = False, acceptance_passed: bool = False,
                version: int | None = None, worktree: Path | None = None,
                plan_version: int | None = None,
                check_run: Annotated[list[str] | None, typer.Option("--check-run")] = None):
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    task = next((r for r in store.list_records(pid) if r["id"] == task_id and r["kind"] == "task"), None)
    if task is None:
        raise ValueError("Task does not exist in this project")  # as the store says a missing row (#244)
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
            refuse("The evidence file must contain a non-empty check result")
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
def plan_save(ctx: typer.Context,
              project: Annotated[str, typer.Argument(metavar="PROJECT", help="The project's name, id or folder.")],
              input_json: Annotated[Path, typer.Argument(
                  metavar="INPUT_JSON", help="A JSON file with plan and steps; the format is in docs/WORKFLOW.md.")],
              idempotency_key: Annotated[str, typer.Option(
                  "--idempotency-key", help="Any text naming this save. The same key with the same file returns "
                                            "the earlier result instead of saving twice; with another file it is "
                                            "refused.")],
              expected_version: Annotated[int | None, typer.Option(
                  help="The plan version you edited; the save is refused if it changed since.")] = None,
              task_versions: Annotated[Path | None, typer.Option(
                  help="A JSON file mapping task ids to the versions you saw.")] = None,
              worktree: Annotated[Path | None, typer.Option(help="The registered worktree the plan is for.")] = None,
              activate: Annotated[bool, typer.Option(help="Make the saved plan the active one.")] = False):
    """Save a new version of a plan from a JSON file; --activate makes it the active plan."""
    from cairnmind.plans import PlanManager
    document = _json_file(input_json)
    if not isinstance(document, dict):
        refuse("The file must contain an object with the fields plan and steps")
    versions = (_json_file(task_versions) if task_versions
                else document.get("task_versions"))
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    from cairnmind import confirmation
    # Typed by a person in their own terminal, a save may change or remove a step they confirmed (#177);
    # from a coding session's shell or an agent's command the store refuses that, with the step named.
    with actor_write(store, ctx.obj, pid, worktree) as scope, \
            confirmation.person_command(store, pid, "command_line_plan_save", scope):
        result = PlanManager(store).save(pid, document.get("plan", {}), document.get("steps", []),
            idempotency_key, expected_version if expected_version is not None else document.get("expected_version"),
            versions, scope, activate)
    output(result)


@plans_app.command("list")
def plan_list(ctx: typer.Context, project: ProjectArg):
    from cairnmind.plans import PlanManager
    store = store_for_read(ctx.obj)

    def read():
        pid = store.resolve_project(project)["id"]
        return pid, PlanManager(store).list(pid)

    output(store.read_consistently(read, READ_KINDS["plan list"]))


@plans_app.command("show")
def plan_show(ctx: typer.Context, project: ProjectArg, plan_id: PlanIdArg):
    from cairnmind.plans import PlanManager
    store = store_for_read(ctx.obj)

    def read():
        pid = store.resolve_project(project)["id"]
        return pid, PlanManager(store).get(pid, plan_id)

    output(store.read_consistently(read, READ_KINDS["plan show"]))


@plans_app.command("next")
def plan_next(ctx: typer.Context, project: ProjectArg, plan_id: str | None = None, worktree: Path | None = None):
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
        return pid, result

    output(store.read_consistently(read, READ_KINDS["plan next"]))


@plans_app.command("activate")
@initialized
def plan_activate(ctx: typer.Context, project: ProjectArg, plan_id: PlanIdArg, worktree: Path | None = None):
    from cairnmind.plans import PlanManager
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    with actor_write(store, ctx.obj, pid, worktree) as scope:
        result = PlanManager(store).activate(pid, plan_id, scope)
    output(result)


@plans_app.command("adopt")
@initialized
def plan_adopt(ctx: typer.Context, project: ProjectArg, group: GroupArg,
               idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
               title: str | None = None, worktree: Path | None = None, activate: bool = False):
    from cairnmind.plans import PlanManager
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    from cairnmind import confirmation
    # Adoption gives each task a plan key: on a task a person confirmed, only a person's command does (#177).
    with actor_write(store, ctx.obj, pid, worktree) as scope, \
            confirmation.person_command(store, pid, "command_line_plan_adopt", scope):
        result = PlanManager(store).adopt_legacy(pid, group, plan={"title": title} if title else None,
            idempotency_key=idempotency_key, worktree=scope, activate=activate)
    output(result)


@plans_app.command("contract")
def plan_contract(ctx: typer.Context, project: ProjectArg,
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
        return pid, (write_contract(store, pid, write, plan_id, scope) if write
                     else build_contract(store, pid, plan_id, scope))

    # Repeated only for a read-only store, never for --write: Store(path) runs it once.
    result = store.read_consistently(run_contract, READ_KINDS["plan contract"])
    if as_json:
        output(result)
    else:
        typer.echo(result["text"])


@checks_app.command("profiles")
def check_profiles(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None):
    from cairnmind.verification import load_profiles
    store = store_for_read(ctx.obj)

    def read():
        pid = store.resolve_project(project)["id"]
        return pid, load_profiles(store, pid, actor_scope(store, pid, worktree))

    output(store.read_consistently(read, READ_KINDS["check profiles"]))


@checks_app.command("run")
@initialized
def check_run(ctx: typer.Context, project: ProjectArg, profile: ProfileArg,
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


def _project_handle(store, project: dict) -> str:
    """How a printed command names the project: its name when that names exactly this project, else its id."""
    name = project.get("name")
    if not isinstance(name, str) or not name or name.startswith("-") or name != name.strip():
        return project["id"]
    try:
        return name if store.resolve_project(name)["id"] == project["id"] else project["id"]
    except ValueError:
        return project["id"]


@stage_app.command("next")
@initialized
def stage_next(ctx: typer.Context, project: ProjectArg, plan_id: str | None = None, worktree: Path | None = None,
               as_json: Annotated[bool, typer.Option("--json", help="Print the decision as JSON.")] = False):
    """Show the next plan step's stage and what is mapped to it. Changes no task and starts nothing."""
    import shlex
    from cairnmind.api_runtime import cli_stage_readiness
    from cairnmind.cockpit_state import CockpitState
    from cairnmind.plans import PlanManager
    from cairnmind.stages import (FROM_WORKSPACE, check_declared, describe, next_command, project_routing,
                                  resolve_stage)
    from cairnmind.terminal_safe import safe_text
    from cairnmind.verification import declared_check_names
    store = store_for(ctx.obj)
    selected = store.resolve_project(project)
    scope = actor_scope(store, selected["id"], worktree)
    plans = PlanManager(store)
    stages, slots, profiles = project_routing(CockpitState(ctx.obj).load(), selected["id"])
    decision = resolve_stage(plans.next_task(selected["id"], plan_id, scope), stages, slots, profiles,
                             readiness=cli_stage_readiness(ctx.obj))
    decision = check_declared(decision, declared_check_names(scope or selected["root"]))
    active = decision["plan"] if plan_id is None else plans.next_task(selected["id"], None, scope)["plan"]
    decision["command"], decision["command_reason"] = next_command(
        decision, _project_handle(store, selected), scope or str(Path(selected["root"]).resolve()),
        slots=slots, profiles=profiles, active_plan_id=(active or {}).get("id"))
    if decision["command"]:
        # The printed command acts on this installation, not on the default one (#244).
        decision["command"][1:1] = _data_dir_arguments(ctx.obj.data_dir)
    if as_json:
        output(decision)
        return
    typer.echo(safe_text(describe(decision)))
    if decision["command"]:
        typer.echo("Next: " + safe_text(shlex.join(decision["command"])))
    elif decision["command_reason"]:
        typer.echo(FROM_WORKSPACE)
    from cairnmind.workspace_stage import MAP_HINT, MAP_PROBLEMS
    if decision.get("problem") in MAP_PROBLEMS:
        typer.echo(MAP_HINT)  # the remedy the workspace gives for the same decision


@checks_app.command("list")
@initialized
def check_list(ctx: typer.Context, project: ProjectArg,
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
            raise ValueError("Task does not exist in this project")  # as the store says a missing row (#244)
        result["verification"] = verification_status(store, records[0], scope)
    output(result)


@sessions_app.command("prepare")
@initialized
def session_prepare(ctx: typer.Context, project: ProjectArg, client: SessionClientArg, worktree: Path | None = None):
    result = request(ctx.obj, "POST", "/sessions/prepare", json={"project": project, "client": client,
                     "worktree": str(worktree.resolve()) if worktree else None})
    output(result)
    if result["status"] != "ready":
        raise typer.Exit(2)


@sessions_app.command("show")
@initialized
def session_show(ctx: typer.Context, session_id: SessionIdArg):
    output(request(ctx.obj, "GET", f"/sessions/{session_id}"))


@sessions_app.command("context")
@initialized
def session_context(ctx: typer.Context, session_id: SessionIdArg):
    output(request(ctx.obj, "POST", "/sessions/context", json={"session_id": session_id}))


@sessions_app.command("ack")
@initialized
def session_ack(ctx: typer.Context, session_id: SessionIdArg, packet_id: PacketIdArg, packet_hash: PacketHashArg, readback_json: ReadbackArg):
    actor = session_value("SESSION_ID")
    if actor and actor != session_id:
        raise typer.BadParameter("Another session cannot be acknowledged")
    output(request(ctx.obj, "POST", "/sessions/ack", json={"session_id": session_id,
        "packet_id": packet_id, "packet_hash": packet_hash,
        "readback": _json_file(readback_json)}))


@eval_app.command("run")
@initialized
def eval_run(ctx: typer.Context, clients: bool = False, report_dir: Path | None = None,
             skip_claude: bool = False):
    from cairnmind.evaluation import run_evaluation
    if skip_claude and not clients:
        raise typer.BadParameter("--skip-claude requires --clients")
    _not_run_without_models(ctx.obj, "cairnmind eval run")
    _managed_only(ctx.obj, "`cairnmind eval run`")
    result = run_evaluation(ctx.obj, clients=clients, report_dir=report_dir, skip_claude=skip_claude)
    _record_evaluation(ctx.obj, "recall", result)
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
    _not_run_without_models(ctx.obj, "cairnmind eval memory")
    _managed_only(ctx.obj, "`cairnmind eval memory`")
    result = run_memory_evaluation(ctx.obj, report_dir=report_dir)
    _record_evaluation(ctx.obj, "memory", result)
    output(result)
    if result.get("status") != "passed":
        raise typer.Exit(3 if result.get("status") == "not_run" else 1)


@eval_app.command("extraction")
@initialized
def eval_extraction(ctx: typer.Context, transcripts: Path | None = None, runs: int = 3,
                    report_dir: Path | None = None):
    """Measure what extraction proposes from conversations against the records a person would keep.

    TRANSCRIPTS defaults to docs/evaluation/extraction-transcripts.json of the current directory. Needs Ollama with the configured chat model, no engine; nothing is stored. Exits 3 without the model.
    """
    from cairnmind.extraction_evaluation import run_extraction_evaluation
    _not_run_without_models(ctx.obj, "cairnmind eval extraction")
    path = transcripts or Path("docs") / "evaluation" / "extraction-transcripts.json"
    try:
        result = run_extraction_evaluation(ctx.obj, path, runs=runs, report_dir=report_dir)
    except ValueError as exc:
        refuse(_shortened(str(exc), 1000))  # the file or the log, not the command line (#244)
    output({key: value for key, value in result.items() if key != "runs"}
           | {"runs": [{"run": run["run"], **run["summary"], "failures": run["failures"]} for run in result["runs"]]})
    if result.get("status") != "passed":
        raise typer.Exit(3 if result.get("status") == "not_run" else 1)


@eval_app.command("repository")
@initialized
def eval_repository(ctx: typer.Context, root: RepositoryArg,
                    questions: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
                    report_dir: Path | None = None):
    """Ask questions with known answers about a real repository, in a throwaway copy and database.

    QUESTIONS defaults to ROOT/docs/evaluation/repository-questions.json. Needs Ollama with the configured models and a running engine; exits 3 when the models are not available.
    """
    from cairnmind.repository_evaluation import run_repository_evaluation
    _not_run_without_models(ctx.obj, "cairnmind eval repository")
    _managed_only(ctx.obj, "`cairnmind eval repository`")
    path = questions or Path(root) / "docs" / "evaluation" / "repository-questions.json"
    try:
        result = run_repository_evaluation(ctx.obj, root, path, report_dir=report_dir)
    except ValueError as exc:
        refuse(_shortened(str(exc), 1000))  # the file or the log, not the command line (#244)
    _record_evaluation(ctx.obj, "repository", result)
    output(result)
    if result.get("status") != "passed":
        raise typer.Exit(3 if result.get("status") == "not_run" else 1)


def _not_run_without_models(settings, command: str) -> None:
    """An evaluation of the models in deterministic-only mode (#274): not run, exit 3, and nothing is asked."""
    from cairnmind.knowledge_mode import needs_models
    why = needs_models(settings, command)
    if why:
        output({"status": "not_run", "reason": why})
        raise typer.Exit(3)


def _record_evaluation(settings, kind: str, result: dict) -> None:
    """A passing evaluation is recorded for the pair of models it ran with (doctor and answers read it)."""
    if result.get("status") == "passed" and not result.get("inference") == "injected_adapter":
        from cairnmind.memory_models import record_passed
        result["models_evaluation_recorded"] = record_passed(settings, kind, result.get("model_identities"))


@eval_app.command("replay")
@initialized
def eval_replay(ctx: typer.Context, project: ProjectArg,
                limit: Annotated[int, typer.Option("--limit", help="How many distinct logged questions, newest first.")] = 20,
                report_dir: Path | None = None):
    """Ask the logged questions of a project again and report answers that changed status or lost sources.

    Compares the status and the set of cited source addresses, not the wording. Needs the service and its model; exits 1 when an answer regressed or lost sources, 3 when nothing was logged.
    """
    from cairnmind.answer_log import replay
    _not_run_without_models(ctx.obj, "cairnmind eval replay")
    pid = store_for_read(ctx.obj).resolve_project(project)["id"]
    try:
        # Asked for the logged scope, not the session's, and a refusal is kept as its reason in the report.
        result = replay(ctx.obj, pid, lambda payload: request(ctx.obj, "POST", "/ask", json=payload, session=False,
                                                              raise_refusal=True),
                        limit=limit, report_dir=report_dir)
    except ValueError as exc:
        refuse(_shortened(str(exc), 1000))  # the file or the log, not the command line (#244)
    output(result)
    if result["status"] != "passed":
        raise typer.Exit(3 if result["status"] == "not_run" else 1)


@answers_app.command("list")
def answers_list(ctx: typer.Context, project: ProjectArg,
                 limit: Annotated[int, typer.Option("--limit", help="How many answers, newest first.")] = 20):
    """List the logged answers of a project, newest first. Asks no model and writes nothing."""
    from cairnmind.answer_log import listing
    pid = store_for_read(ctx.obj).resolve_project(project)["id"]
    try:
        output(listing(ctx.obj, pid, limit))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


@answers_app.command("show")
def answers_show(ctx: typer.Context, project: ProjectArg, answer_id: AnswerIdArg):
    """Show one logged answer with its question, citations, code state, model and timings."""
    from cairnmind.answer_log import find
    pid = store_for_read(ctx.obj).resolve_project(project)["id"]
    try:
        output(find(ctx.obj, pid, answer_id))
    except LookupError as exc:
        # An unknown id is said as an unknown task or plan is: "Cairnmind: ...", exit 1 (#244).
        raise ValueError(str(exc)) from None
    except ValueError as exc:  # an id of the wrong form
        refuse(str(exc))


@answers_app.command("prune")
def answers_prune(ctx: typer.Context, project: ProjectArg,
                  keep: Annotated[int, typer.Option("--keep", min=0, help="How many of the newest answers to keep; "
                                                    "0 deletes the log.")]):
    """Keep only the newest logged answers of a project."""
    from cairnmind.answer_log import prune
    pid = store_for_read(ctx.obj).resolve_project(project)["id"]
    output(prune(ctx.obj, pid, keep))


SNAPSHOT_WAIT_SECONDS = 1800


@memory_app.command("snapshot")
@initialized
def memory_snapshot(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None, preview: bool = False):
    """Save the project's full memory; --preview shows the scope without writing a snapshot."""
    payload = {"project": project, "worktree": str(worktree.expanduser().resolve()) if worktree else None}
    if preview:
        output(request(ctx.obj, "POST", "/memory/preview", json=payload))
        return
    try:
        # A large project writes tens of thousands of objects: far longer than an ordinary request (#158).
        result = request(ctx.obj, "POST", "/memory/snapshot", json=payload,
                         timeout=httpx.Timeout(60, read=SNAPSHOT_WAIT_SECONDS))
    except httpx.TimeoutException:
        raise typer.BadParameter(
            f"No answer within {SNAPSHOT_WAIT_SECONDS // 60} minutes. The snapshot is still being written by "
            f"the service; leaving did not stop it. It is done when `cairnmind memory status {project}` shows "
            "the new snapshot; until then another snapshot waits for this one.") from None
    output(result)


@memory_app.command("inspect")
def memory_inspect(ctx: typer.Context, path: SnapshotArg):
    """Check a snapshot's files without initializing the store or starting services."""
    from cairnmind.memory import inspect_snapshot
    # Preserve symlink components for the package validator to reject them.
    selected = str(path.expanduser().absolute())
    if session_value("SESSION_ID"):
        output(request(ctx.obj, "POST", "/memory/inspect", start_service=False, json={"path": selected}))
    else:
        output(inspect_snapshot(selected))


@memory_app.command("show")
def memory_show(ctx: typer.Context, path: SnapshotArg,
                kind: Annotated[str | None, typer.Option("--kind", help="Record kind, or 'plan'.")] = None,
                status: Annotated[str | None, typer.Option("--status", help="Record status.")] = None,
                grep: Annotated[str | None, typer.Option(
                    "--grep", help="Case-insensitive text to look for, also in the acceptance criteria and check names "
                                     "shown for tasks; with --sources also in the source texts shown.")] = None,
                sources: Annotated[bool, typer.Option(
                    "--sources", help="Print the text of each record's sources, up to 1,200 characters each and 60,000 "
                                      "in all. A source that supports several records is printed under each.")] = False):
    """Read a snapshot's records and plans as text. Writes nothing; outside a coding session needs no engine or service.

    Tasks show their acceptance criteria and the check each is linked to; a record that replaced another, or was replaced, names it."""
    from cairnmind.memory_view import show_snapshot
    # Preserve symlink components for the package validator to reject them.
    selected = str(path.expanduser().absolute())
    expected = None
    if session_value("SESSION_ID"):
        # A coding session may read only its own project's memory; the service decides.
        expected = request(ctx.obj, "POST", "/memory/inspect", start_service=False,
                           json={"path": selected})["snapshot_id"]
    typer.echo(show_snapshot(selected, kind=kind, status=status, grep=grep, sources_text=sources,
                             expected_snapshot_id=expected))


@memory_app.command("orphans")
def memory_orphans(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None,
                   as_json: Annotated[bool, typer.Option("--json", help="Print the records as JSON.")] = False):
    """List records whose evidence is no longer current: orphaned, or resting on a changed source. Read-only."""
    from cairnmind.brief import _scoped_records
    from cairnmind.evidence import orphans_report
    from cairnmind.verification import _root
    store = store_for_read(ctx.obj)

    def read():
        selected = store.resolve_project(project)
        root = _root(store, selected["id"], actor_scope(store, selected["id"], worktree))
        return selected["id"], orphans_report(store, selected, root, _scoped_records(store, selected["id"], root))

    report = store.read_consistently(read, READ_KINDS["memory orphans"])
    if as_json:
        output(report)
    else:
        typer.echo(report["text"])


@memory_app.command("extraction-stats")
def memory_extraction_stats(ctx: typer.Context, project: ProjectArg,
                            as_json: Annotated[bool, typer.Option("--json", help="Print the numbers as JSON.")] = False):
    """Per extractor version: how many extracted records are proposed, confirmed, disputed or superseded. Read-only."""
    from cairnmind.extraction_stats import extraction_stats, render
    store = store_for_read(ctx.obj)

    def read():
        selected = store.resolve_project(project)
        return selected["id"], extraction_stats(store, selected["id"])

    stats = store.read_consistently(read, READ_KINDS["memory extraction-stats"])
    if as_json:
        output(stats)
    else:
        typer.echo(render(stats))


@memory_app.command("import")
def memory_import(ctx: typer.Context, path: SnapshotArg,
                  root: Annotated[Path, typer.Option("--root")], name: str | None = None,
                  dry_run: bool = False,
                  replace_confirmed: Annotated[bool, typer.Option(
                      "--replace-confirmed", help="Let the package replace records that were confirmed or disputed "
                      "on this machine; the dry run lists them.")] = False):
    """Import the memory of this project only; --dry-run needs a running service and writes nothing."""
    from cairnmind.memory import inspect_snapshot
    selected = str(path.expanduser().absolute())
    inspect_snapshot(selected)  # Validate the transfer before initializing anything.
    if not dry_run:
        ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
    payload = {"path": selected, "root": str(root.expanduser().resolve()), "dry_run": dry_run}
    if replace_confirmed:
        payload["replace_confirmed"] = True
    if name is not None:
        payload["name"] = name
    output(request(ctx.obj, "POST", "/memory/import", start_service=not dry_run, json=payload))


@memory_app.command("auto")
def memory_auto(ctx: typer.Context, project: ProjectArg, enable: bool = False, disable: bool = False,
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
def memory_status(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None):
    """Read the last snapshot and the schedule; start no service and create no configuration."""
    params = {"project": project}
    if worktree is not None:
        params["worktree"] = str(worktree.expanduser().resolve())
    output(request(ctx.obj, "GET", "/memory/status", start_service=False, params=params))


# Published team memory (#170): publish, check-published, import-published, show-published, confirm-plan.
from cairnmind.cli_published import register as _register_published  # noqa: E402

_register_published(memory_app, initialized)

# Unknown transcript record kinds a person accepted as technical for a project (#210).
from cairnmind.cli_record_kinds import register as _register_record_kinds  # noqa: E402

_register_record_kinds(app, initialized)


@app.command()
@initialized
def install(ctx: typer.Context, project: ProjectArg):
    """Add the project's MCP server, hooks and instructions, keeping the existing configuration."""
    from cairnmind.integrations import install_integrations
    store = store_for(ctx.obj)
    output(install_integrations(ctx.obj, store.resolve_project(project)))


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
@initialized
def run(ctx: typer.Context, project: ProjectArg, client: ClientArg, worktree: Path | None = None):
    """Start Codex or Claude Code in the project, bound to its memory. Arguments after -- go to the client."""
    from cairnmind.integrations import run_client
    _ensure_service_or_refuse(ctx.obj)
    selected = store_for(ctx.obj).resolve_project(project)
    if worktree:
        store = store_for(ctx.obj)
        root = str(worktree.expanduser().resolve())
        if not store.query("SELECT id FROM project_roots WHERE project_id=:project AND path=:path",
                            {"project": selected["id"], "path": root}):
            from cairnmind.sql import unregistered_root
            refuse(unregistered_root(root, selected))
        selected = {**selected, "root": root}
    args = list(ctx.args)
    if args[:1] == ["--"]:
        args = args[1:]
    raise typer.Exit(run_client(ctx.obj, selected, client, args))


@app.command()
@initialized
def mcp(ctx: typer.Context,
        client: Annotated[str, typer.Option("--client", help=(
            "native: started by the Cairnmind launcher (Codex, Claude Code). external: any other MCP client "
            "(Cursor, Cline, Zed, ...); its session is evidence-degraded and never verified."))] = "native",
        project: Annotated[str | None, typer.Option("--project", help=(
            "With --client external: the project id, name or a path inside it (default: the working directory)."))] = None,
        worktree: Annotated[Path | None, typer.Option("--worktree", help=(
            "With --client external: the registered worktree (default: the one containing the project path)."))] = None):
    """MCP over stdio; prints no logs or CLI messages to stdout."""
    if client not in {"native", "external"}:
        raise typer.BadParameter("--client must be native or external")
    if client == "native" and (project is not None or worktree is not None):
        raise typer.BadParameter("--project and --worktree are only for --client external")
    from cairnmind.mcp_server import run
    if client == "native":
        run(ctx.obj)
        return
    run(ctx.obj, external={"project": project or str(Path.cwd()),
                           "worktree": str(worktree.expanduser().resolve()) if worktree is not None else None})


@app.command()
@initialized
def hook(ctx: typer.Context, client: Annotated[str, typer.Option("--client")]):
    """Handle one hook event of a coding client, read from standard input. Run by the client, not by hand."""
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
    from cairnmind.memory_models import from_settings
    output(asyncio.run(from_settings(ctx.obj).models()))


@models_app.command("set")
@initialized
def model_set(ctx: typer.Context,
              role: Annotated[str, typer.Option("--role", help="chat (answers, extraction) or embedding.")],
              provider: Annotated[str, typer.Option("--provider", help="ollama or openai_compatible.")],
              model: Annotated[str, typer.Option("--model", help="The model's name on that server.")],
              base_url: Annotated[str | None, typer.Option(
                  "--base-url", help="The server's address; HTTPS unless it is this computer.")] = None,
              credential_ref: Annotated[str | None, typer.Option(
                  "--credential-ref", help="env:NAME or profile:ID naming the key; never the key itself.")] = None,
              context_window: Annotated[int | None, typer.Option(
                  "--context-window", help="Chat only: the model's context in tokens.")] = None,
              reasoning_effort: Annotated[str | None, typer.Option(
                  "--reasoning-effort", help="Chat on an OpenAI-compatible server only: none, low, medium or high; "
                                             "none turns thinking off where the server supports it.")] = None,
              allow_remote: Annotated[bool, typer.Option(
                  "--allow-remote", help="Consent to send memory content to a server on another computer.")] = False):
    """Choose the knowledge agent's chat or embedding model and server, after a test call to it."""
    from cairnmind.memory_models import configure_role
    try:
        result = asyncio.run(configure_role(
            ctx.obj, role, provider=provider, model=model, base_url=base_url, credential_ref=credential_ref,
            context_window=context_window, reasoning_effort=reasoning_effort, allow_remote=allow_remote))
    except (ValueError, OllamaUnavailable) as exc:
        raise typer.BadParameter(str(exc)) from None
    from cairnmind.knowledge_mode import turn_on
    said = turn_on(ctx.obj)  # its check passed: the knowledge agent is on from the service's next start (#274)
    if said:
        result["knowledge_agent"] = said
    output(result)


@models_app.command("pull")
@initialized
def model_pull(ctx: typer.Context):
    """Explicitly download the models (several GB) and record their digests."""
    from cairnmind.memory_models import profile
    settings = ctx.obj
    local = [name for role, name in (("chat", settings.chat_model), ("embedding", settings.embedding_model))
             if profile(settings, role) is None]
    if not local:
        raise typer.BadParameter("Neither memory model runs on the local Ollama; install them on their own server.")
    with httpx.Client(trust_env=False, timeout=httpx.Timeout(60, read=900)) as client:
        for name in local:
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
    from cairnmind.knowledge_mode import turn_on
    said = turn_on(settings)  # the models are here: the knowledge agent is on from the service's next start (#274)
    if said:
        typer.echo(f"Cairnmind: {said}", err=True)  # said now: the store steps below may still fail
    store = store_for(settings)
    from cairnmind.service import schedule_embeddings
    for item in models.get("models", []):
        if item.get("name") == settings.embedding_model and item.get("digest") and settings.embedding_model in local:
            schedule_embeddings(store, f"{settings.embedding_model}@{item['digest']}")
    output({"models": local, "digests_saved": True, **({"knowledge_agent": said} if said else {})})


def _retry_jobs(store):
    return store.retry_jobs()


@app.command("retry-jobs")
@initialized
def retry_jobs(ctx: typer.Context):
    """Queue every failed job of every project again, keeping its history. A person's command."""
    if session_value("SESSION_ID"):
        raise typer.BadParameter(
            "A model session cannot retry the jobs of all projects. "
            "Use `cairnmind jobs retry PROJECT JOB_ID --expected-attempts N` with a current ACK.")
    store = store_for(ctx.obj)
    if getattr(store, "not_writer", None):
        # A reader retries nothing, even when nothing failed: the notice already said why, so the refusal
        # is that one line and exit 1, not a success beside it (#244).
        from cairnmind.store_writer import StoreNotWriter
        raise StoreNotWriter(store.not_writer)
    # What really happened is printed, not a constant.
    job_ids = list((_retry_jobs(store) or {}).get("job_ids", []))
    output({"retried": bool(job_ids), "count": len(job_ids), "job_ids": job_ids})


@app.command("rebuild-vectors")
@initialized
def rebuild_vectors(ctx: typer.Context, project: ProjectArg):
    """Rebuild the project's derived vectors, keeping sources and history."""
    from cairnmind.memory_models import from_settings
    from cairnmind.service import schedule_embeddings
    settings = ctx.obj
    from cairnmind.knowledge_mode import needs_models
    why = needs_models(settings, "cairnmind rebuild-vectors")
    if why:
        refuse(why)  # no embedding model is configured; nothing is deleted or asked (#274)
    store = store_for(settings)
    pid = store.resolve_project(project)["id"]
    from cairnmind import memory_fingerprint

    async def identity():
        models = from_settings(settings)
        try:
            # An OpenAI-compatible embedding role: the fixed request first; changed weights are accepted
            # here and get a new namespace, apart from every earlier vector (#212).
            client = memory_fingerprint.client_of(models)
            checked = await memory_fingerprint.check(client, accept=True) if client is not None else None
            return await models.model_identity(settings.embedding_model), checked
        finally:
            await models.aclose()

    namespace, fingerprint = asyncio.run(identity())
    store.delete_embeddings(pid)
    held: dict = {}
    count = schedule_embeddings(store, namespace, settings=settings, report=held)
    result = {"project": pid, "missing_vectors_queued": count, "namespace": namespace, "sources_preserved": True}
    if fingerprint is not None:
        result["embedding_fingerprint"] = fingerprint
    if held.get("embedding_held_back"):
        # Chunks of a class the remote embedding server may not receive (memory_remote_classes, #181).
        result["vectors_held_back"] = held["embedding_held_back"]
    output(result)


@app.command()
@initialized
def export(ctx: typer.Context, destination: ExportArg, project: str | None = None, markdown: bool = False):
    """Write the records and sources, of one project or all, as JSON or Markdown for reading. Not a backup."""
    store = store_for(ctx.obj, notice=False)  # it only reads, so a reader is not shown the refusal-like notice
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
def backup(ctx: typer.Context, destination: Annotated[str, typer.Argument(
        metavar="DESTINATION", help="A file, or a folder for a file with a dated name; a path ending in / is a "
                                    "folder and is created when missing.")]):
    """Write a compressed, checksummed backup of the whole store. Reads only: also works on a reader."""
    # Taken as typed: a Path would drop the trailing slash that says the destination is a folder (#244).
    target = Path(destination).expanduser()
    if destination.endswith(("/", os.sep)):
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
    output({"backup": str(store_for(ctx.obj, notice=False).backup(target))})


@app.command()
@initialized
def restore(ctx: typer.Context, source: Annotated[Path, typer.Argument(
        metavar="SOURCE", exists=True, dir_okay=False, readable=True,
        help="A backup file written by `cairnmind backup`.")]):
    """Replace the store with a backup, after a safety backup of the current one. The service must be stopped."""
    # A missing path or a folder is refused while the arguments are read, before the store or engine is touched.
    with (ctx.obj.data_dir / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            refuse("Run cairnmind stop first; the service still holds the store lock")
        if service_alive(ctx.obj):
            refuse("Run cairnmind stop first; restoring requires a stopped service")
        store = store_for(ctx.obj)
        store.restore(source)
    from cairnmind.confirmation_upgrade import report
    marks = report(getattr(store, "confirmations_before_rule", None))
    output({"restored": True, **({"confirmations_before_rule": marks} if marks else {})})


@app.command()
@initialized
def forget(ctx: typer.Context, project: ProjectArg,
           delete_project: Annotated[bool, typer.Option("--delete-project", help="Explicitly delete the project's data")] = False):
    """Delete a project and all its memory from the store. Backups are not changed; only a restore brings it back."""
    if not delete_project:
        raise typer.BadParameter("Deleting requires --delete-project. Existing backups stay unchanged.")
    store = store_for(ctx.obj)
    pid = store.resolve_project(project)["id"]
    store.forget_project(pid)
    from cairnmind.answer_log import forget as forget_answers
    forget_answers(ctx.obj, pid)
    output({"deleted": pid, "backups_unchanged": True})


@app.command()
@initialized
def benchmark(ctx: typer.Context, project: ProjectArg, question: QuestionArg):
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
