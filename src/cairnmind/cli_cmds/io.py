"""Shared CLI runtime, HTTP service, store-opening, and output helpers."""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Annotated

import httpx
import typer

from cairnmind.config import SERVICE_NAMES, Settings, drop_env, session_value, tls_verify
from cairnmind.pal.file_locks import lock_exclusive, unlock
from cairnmind.pal.process_control import detached_popen_kwargs
from cairnmind.pal.service_token import (
    PORT_ADVICE as _PORT_ADVICE,
    foreign_service_text,
    refused_token,
)
from cairnmind.storage.arcade import ArcadeError


def output(value) -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    use_ascii = encoding.lower().replace("-", "") not in {"utf8", "utf8sig"}
    typer.echo(json.dumps(value, ensure_ascii=use_ascii, indent=2, default=str))


def _stdout_is_terminal() -> bool:
    return sys.stdout.isatty()


def _stderr_is_terminal() -> bool:
    return sys.stderr.isatty()


def _format_cli_stderr(prefix: str, message: str) -> str:
    """Format a stderr line, applying human remediation in interactive terminals and NO_COLOR hygiene."""
    from cairnmind.cli_cmds.cli_human import format_human_error, strip_ansi
    shown = message
    if not _INVOCATION.get("as_json") and _stderr_is_terminal():
        human = format_human_error(message)
        if human is not None:
            shown = human
    line = _pasteable(f"{prefix}{shown}")
    if os.environ.get("NO_COLOR"):
        line = strip_ansi(line)
    return line


JSON_FLAG = Annotated[bool, typer.Option(
    "--json", help="Print the full result as JSON; the default when the output is not a terminal.")]


def _human_or_json(result, as_json: bool, sentences) -> None:
    """doctor's rule for the commands a person reads (#287, #313): in a terminal the sentences, each made safe to
    print; with --json, or in a pipe, a file or a script, the full JSON exactly as before. `sentences` is called only
    for a terminal."""
    if as_json or not _stdout_is_terminal():
        output(result)
    else:
        from cairnmind.pal.terminal_safe import safe_text
        typer.echo("\n".join(safe_text(line) for line in sentences()))


def initialized(command):
    """Initialize only when an operational leaf runs, after argument/help parsing."""
    @wraps(command)
    def invoke(ctx, *args, **kwargs):
        ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
        if _INVOCATION.get("backend"):
            ctx.obj.storage_backend = _INVOCATION["backend"]
        if _INVOCATION.get("db_path"):
            ctx.obj.db_path = _INVOCATION["db_path"]
        if ctx.obj._origin.get("connect_saved"):
            from cairnmind.storage.database_mode import saved_note
            typer.echo(f"Cairnmind: {saved_note(ctx.obj)}", err=True)
        return command(ctx, *args, **kwargs)
    return invoke


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
    if not arguments:
        return message
    from cairnmind.pal.process_control import format_command
    from cairnmind.pal.terminal_safe import safe_text
    # `cairnmind restore` (the refusal of an unfinished restore, #362) and `cairnmind store takeover` replace or take
    # over a whole store: pasted bare they would act on the default installation.
    for command in ("project root ", "restore ", "store takeover "):
        message = message.replace(f"`cairnmind {command}", f"`cairnmind {safe_text(format_command(arguments))} {command}")
    return message


def _json_file(path: Path):
    """A JSON file named on the command line. One that is not JSON is refused in one line that names it, as one of
    another shape is, not as the parser's bare message (#244)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:  # JSONDecodeError, or bytes that are not UTF-8
        refuse(f"The file {_shortened(str(path))} is not valid JSON: {_shortened(str(exc), 300)}")


def _shortened(text: str, limit: int = 200) -> str:
    from cairnmind.pal.terminal_safe import safe_text
    text = safe_text(text)
    return text if len(text) <= limit else text[:limit // 2 - 1] + "…" + text[-(limit // 2):]


def _os_error_text(exc: OSError) -> str:
    """An operating-system error about a path as a sentence, with a long path shortened (#244)."""
    import errno
    if exc.errno is None or exc.filename is None:
        return str(exc)
    import os
    path = _shortened(os.fsdecode(exc.filename) if isinstance(exc.filename, bytes) else str(exc.filename))
    if (exc.errno == errno.ENAMETOOLONG
            or getattr(exc, "winerror", None) == 206
            or (getattr(exc, "winerror", None) == 123
                and any(len(part) > 255 for part in Path(os.fsdecode(exc.filename)).parts))):
        return f"The path is too long for this system: {path}"
    if exc.errno == errno.ENOENT:
        return f"{path} does not exist."
    return f"{exc.strerror or 'The path cannot be used'}: {path}"


_NOTICES_SHOWN: set[str] = set()


def store_for(settings: Settings, *, notice: bool = True, for_restore: bool = False):
    """The full store. notice=False for a command that only reads it on a reader, as backup and export do.
    for_restore=True opens a store that carries the mark of an unfinished restore, as only `restore` does (#362)."""
    from cairnmind.store import Store
    store = Store(settings.data_dir, settings=settings, for_restore=for_restore)
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
    "memory decisions": _RESOLVE | {"memory"},
    "impact": _RESOLVE | {"record", "source", "memory", "node", "edge", "graph"},
    "scan": _RESOLVE | {"record", "source", "memory", "node", "edge", "graph"},
    "usage": _RESOLVE,
    "proposed": _RESOLVE | {"record", "source", "memory"},
    "pulse": _RESOLVE | {"record", "plan", "check", "source", "memory", "session"},
}


def actor_scope(store, project_id: str, worktree: Path | str | None = None):
    """Read the launcher's worktree without requiring an ACK merely to inspect state."""
    scope = str(Path(worktree).expanduser().resolve()) if worktree else None
    session_id = session_value("SESSION_ID")
    if session_id:
        from cairnmind.storage.storage_query import db_find_one
        session = db_find_one(store, "sessions", {"id": session_id}, columns=["project_id", "worktree"])
        if not session or session["project_id"] != project_id:
            raise ValueError("resume_required: command project differs from the session")
        if scope and scope != session["worktree"]:
            raise ValueError("resume_required: command worktree differs from the session")
        scope = session["worktree"]
    return scope


@contextmanager
def actor_write(store, settings: Settings, project_id: str, worktree: Path | str | None = None):
    """Native CLI commands inherit the launcher's logical session; manual repair stays possible."""
    session_id = session_value("SESSION_ID")
    code = None
    if session_id:
        from cairnmind.memory.integrations import drain_hook_spool
        from cairnmind.memory.sessions import SessionManager
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
    from cairnmind.cli_cmds import service_version  # not cairnmind.service: that imports the web framework (#405)
    try:
        with httpx.Client(trust_env=False, timeout=0.7, verify=tls_verify(settings.url)) as client:
            result = client.get(settings.url + "/health")
            data = result.json()
            alive = (result.is_success and data.get("service") in SERVICE_NAMES
                     and data.get("instance") == service_version.instance_id(settings))
    except (httpx.HTTPError, ValueError):
        return False
    if alive:
        service_version.remember(settings, data)  # the version check that follows reads this answer, not a new one
    return alive


@contextmanager
def _service_stopped(settings: Settings, held: str, running: str):
    """Hold service.lock, so no service of this data directory starts meanwhile; refuse while one runs."""
    from cairnmind.terminal import setup_mark
    reason = setup_mark.refusal(settings.data_dir)
    if reason:
        refuse(str(reason))  # a change of setup holds this data directory (#339)
    with (settings.data_dir / "service.lock").open("a", encoding="utf-8") as lock:
        try:
            lock_exclusive(lock, blocking=False)
        except BlockingIOError:
            refuse(held)
        try:
            if service_alive(settings):
                refuse(running)
            yield
        finally:
            try:
                unlock(lock)
            except OSError:
                pass


def last_log_line(path: Path) -> str:
    """The last line of a log, bounded and with control characters replaced: what a stopped process said."""
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 8192))
            lines = [line for line in handle.read().decode("utf-8", "replace").splitlines() if line.strip()]
    except OSError:
        return f"see {path}"
    text = lines[-1] if lines else f"see {path}"
    # Long enough for the refusal of an unfinished restore (#362), whose last words are the way out.
    return "".join(char if char.isprintable() else "?" for char in text)[:2000]


def _bind_failed(log: Path, since: int) -> bool:
    """Whether what this start wrote to the log (from byte ``since``, not an earlier start's) says the port is taken."""
    try:
        with log.open("rb") as handle:
            handle.seek(since)
            return b"address already in use" in handle.read(65536).lower()
    except OSError:
        return False


def ensure_service(settings: Settings, *, background_inference: bool = True) -> bool:
    """True when this call started the service process, False when one already answered."""
    if service_alive(settings):
        from cairnmind.cli_cmds.service_version import refusal as other_version
        reason = other_version(settings)
        if reason:
            # It holds the rules of another version: nothing is sent to it, read or write (#319). Raised before
            # anything starts and with started never True, so the caller announces no start.
            raise ServiceOfOtherVersion(reason)
        return False  # used, and stoppable, during an upgrade too: the mark only stops a start
    from cairnmind.cli_cmds import upgrade_mark
    reason = upgrade_mark.refusal()
    if reason:
        # The installer is replacing this code: a start now would run the version being replaced (#298).
        # Raised before anything starts, so the caller announces no start.
        raise UpgradeInProgress(reason)
    from cairnmind.terminal import setup_mark
    reason = setup_mark.refusal(settings.data_dir)
    if reason:
        # A change of setup stopped this service on purpose (#339); raised before anything starts, as above.
        raise SetupInProgress(reason)
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
                                   stdin=subprocess.DEVNULL, stdout=handle, stderr=handle,
                                   env=env, **detached_popen_kwargs())
    try:
        return _wait_for_start(settings, process, log, started_at)
    except KeyboardInterrupt:
        # An interrupted start does not leave behind the service it spawned: nobody would know it runs (#339).
        try:
            process.terminate()
        except OSError:
            pass
        raise


def _wait_for_start(settings: Settings, process, log: Path, started_at: int) -> bool:
    """ensure_service's wait for the process it spawned: True when the service answers, else it raises."""
    from cairnmind.terminal import setup_mark
    from cairnmind.cli_cmds import upgrade_mark
    seen = None  # an installer that began at any moment of this start, even one that has ended by the time it answers
    for _ in range(300):
        seen = seen or upgrade_mark.refusal()
        if service_alive(settings):
            if seen:
                _stop_started(settings)  # raises with the sentence that says the start was ended: never announced
            return True
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
    seen = seen or upgrade_mark.refusal()
    if seen:
        # Still starting when the installer began: its stop found nothing to stop. This start's own process goes.
        if process is not None:
            try:
                process.terminate()
            except OSError:
                pass
        raise UpgradeInProgress(upgrade_mark.refusal() or upgrade_mark.UPGRADE_ENDED)
    reason = setup_mark.refusal(settings.data_dir)
    if reason:
        raise SetupInProgress(reason)  # a start that met the change's lock ends with the change's sentence
    raise ServiceDidNotStart(f"The service did not start. Check {log}; port {settings.port} may be in use.")


def _stop_started(settings: Settings) -> None:
    """An installer that began after the check in ensure_service may have found no service to stop yet: the service
    this start brought up is stopped here, so none runs the version being replaced (#298). A start whose starter
    ends first cannot be closed from here. The text says what is true now: an installer that has ended is not
    "in progress"."""
    from cairnmind.cli_cmds import upgrade_mark
    try:
        with httpx.Client(trust_env=False, timeout=5, verify=tls_verify(settings.url)) as client:
            client.post(settings.url + "/shutdown",
                        headers={"Authorization": f"Bearer {settings.token}"}).raise_for_status()
    except httpx.HTTPError:
        from cairnmind.pal.process_control import format_command
        command = format_command(["cairnmind", *_data_dir_arguments(settings.data_dir), "stop"])
        raise UpgradeInProgress(upgrade_mark.UPGRADE_STOP_FAILED.format(command=command)) from None
    raise UpgradeInProgress(upgrade_mark.refusal() or upgrade_mark.UPGRADE_ENDED)


class ServiceDidNotStart(ValueError):
    """The knowledge service did not start. A command shows it as a refusal, not as a wrong argument (#244);
    the workspace and the cockpit, which call ensure_service too, get an ordinary error with the reason."""


class UpgradeInProgress(ServiceDidNotStart):
    """An installer is replacing Cairnmind (upgrade_mark.py): nothing starts the service until it has finished (#298)."""


class SetupInProgress(ServiceDidNotStart):
    """A change of where storage and models run stopped this service on purpose (setup_mark.py): nothing starts it
    until the change has finished (#339)."""


class ServiceOfOtherVersion(ServiceDidNotStart):
    """This data directory's service runs another version (service_version.py): nothing is sent to it (#319)."""


def ensure_no_upgrade() -> None:
    """A coding session starts nothing of the version being replaced, even beside a running service, whose stop the
    installer may be about to make (#319). ensure_service itself reads the mark only when it has to start."""
    from cairnmind.cli_cmds import upgrade_mark
    reason = upgrade_mark.refusal()
    if reason:
        raise UpgradeInProgress(reason)


def _refuse_during_upgrade() -> None:
    try:
        ensure_no_upgrade()
    except UpgradeInProgress as exc:
        refuse(str(exc))


def _ensure_service_or_refuse(settings: Settings) -> None:
    try:
        started = ensure_service(settings)
    except ServiceDidNotStart as exc:
        refuse(str(exc))
    if started:
        say_service_started(settings)


def _refuse_unknown_project(settings: Settings, project: str) -> None:
    """An unknown project is said before a service is started for it (#313). Only when the store can be read without
    starting anything; otherwise the service resolves the project and says the same sentence."""
    from cairnmind.storage.store_readonly import StoreNotReady
    try:
        store_for_read(settings).resolve_project(project)
    except StoreNotReady:
        return  # shortcut: an engine that is not running yet is started by the service, which then refuses
    except ValueError as exc:
        if str(exc).startswith("Unknown project: "):
            raise
    except ArcadeError as exc:
        if exc.status_code == 401:
            raise  # an engine that refuses this data directory: said in full, and no service is started for it
        return
    except (OSError, httpx.HTTPError):
        return


def say_service_started(settings: Settings) -> None:
    """Nothing starts in the background unannounced (#287): said whether or not this is a terminal."""
    from cairnmind.cli_cmds.first_start import SERVICE_STARTED
    typer.echo(f"Cairnmind: {SERVICE_STARTED.format(url=settings.url, data_dir=settings.data_dir)}", err=True)


def _refuse_other_version(settings: Settings) -> None:
    from cairnmind.cli_cmds.service_version import refusal
    reason = refusal(settings)
    if reason:
        refuse(reason)


def refuse(message: str, code: int = 2):
    """A refusal that is not about the command line: no usage line and no "Invalid value" (#154)."""
    typer.echo(_format_cli_stderr("Error: ", message), err=True)
    raise typer.Exit(code)


def _service_unavailable(settings: Settings) -> str:
    """Why a diagnostic that asks the running service got no answer, and what to do. A reader never runs the
    service (#70), so it is not told to start one (#244)."""
    from cairnmind.storage.store_writer import inspect
    try:
        role = inspect(settings)
    except Exception:  # noqa: BLE001 - the role only chooses the advice; the refusal is given either way
        role = {}
    if role.get("role") in {"reader", "lost"}:
        from cairnmind.pal.terminal_safe import safe_text
        return (f"{safe_text(role.get('reason') or '')} This command asks the running service, which only the "
                "writer runs. Here `cairnmind store writer` and `cairnmind doctor` show this installation's role, "
                "and the commands that only read the store work.").strip()
    from cairnmind.cli_cmds import upgrade_mark
    reason = upgrade_mark.refusal()
    if reason:
        return reason  # stopped for an upgrade: `cairnmind serve` would start the version being replaced (#298)
    from cairnmind.terminal import setup_mark
    reason = setup_mark.refusal(settings.data_dir)
    if reason:
        return str(reason)  # stopped for a change of setup (#339)
    from cairnmind.cli_cmds.first_start import STATUS_NOT_RUNNING
    return STATUS_NOT_RUNNING


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
            raise_refusal: bool = False, matched: bool = False, **kwargs):
    # session=False sends no session of the launched agent (a replay asks for the logged scope, #15);
    # raise_refusal=True raises ServiceRefusal instead of ending the command; matched=True: the caller matched the
    # service by instance id and it is reached whatever its version (stop, #319).
    if start_service:
        if not matched:
            _ensure_service_or_refuse(settings)
    elif not (settings.data_dir / "config.json").is_file() or not settings.token:
        refuse("No existing Cairnmind configuration; run `cairnmind init` explicitly.")
    else:
        _refuse_other_version(settings)  # a diagnostic starts nothing and sends nothing to another version (#319)
    retry_job = path.startswith("/jobs/") and path.endswith("/retry")
    if not session:
        pass
    elif (method.upper() == "POST" and (retry_job or path in {
            "/remember", "/findings", "/checkpoint", "/plans/save", "/plans/activate",
            "/memory/snapshot", "/memory/import", "/memory/heal", "/memory/publish", "/memory/import-published",
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
    elif method.upper() == "GET" and (path in {"/context", "/checks", "/jobs", "/memory/status", "/memory/heal-audit"}
                                      or path.startswith("/jobs/")):
        session_id = session_value("SESSION_ID")
        if session_id:
            kwargs["params"] = {**dict(kwargs.get("params") or {}), "session_id": session_id}
    with httpx.Client(trust_env=False, timeout=settings.ask_timeout + 90, verify=tls_verify(settings.url),
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
            if not start_service and refused_token(response.status_code, detail):
                detail = foreign_service_text(settings)
            # The service's answer, not a wrong argument; shown as it is, without control characters.
            from cairnmind.pal.terminal_safe import safe_text
            if raise_refusal:
                raise ServiceRefusal(safe_text(str(detail)))
            from cairnmind.storage.sql import missing_row_text
            if response.status_code == 404 or missing_row_text(str(detail)):
                # An unknown id or project is said as the commands that read the store say it (#244).
                typer.echo(_pasteable(f"Cairnmind: {safe_text(str(detail))}"), err=True)
                raise typer.Exit(1)
            refuse(safe_text(str(detail)))
        return response.json()


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _launch_terminal_workspace(ctx: typer.Context, *, takeover: bool = False) -> None:
    # Check before initialization: redirected CLI diagnostics must never create
    # configuration, launch a browser or silently start interactive processes.
    if not _interactive_terminal():
        refuse(
            "Run `cairnmind` in an interactive terminal. "
            "Available commands: `cairnmind --help`; status: `cairnmind workspace status`.")
    from cairnmind.terminal.workspace import launch_workspace
    ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
    code = launch_workspace(ctx.obj, takeover=True) if takeover else launch_workspace(ctx.obj)
    if code:
        raise typer.Exit(code=code)


@contextmanager
def _engine_command():
    """An engine command on a connect-only installation ends with one plain sentence, not a failure (#68)."""
    from cairnmind.storage.engine import ConnectOnly
    try:
        yield
    except ConnectOnly as exc:
        refuse(str(exc))


def _managed_only(settings: Settings, what: str) -> None:
    from cairnmind.storage.database_mode import connect_only, refusal
    if connect_only(settings):
        refuse(f"{what} creates and drops temporary databases. " + refusal(settings, "create or drop a database")
               + " Run it on an installation with the engine on this machine.")


def _project_handle(store, project: dict) -> str:
    """How a printed command names the project: its name when that names exactly this project, else its id."""
    name = project.get("name")
    if not isinstance(name, str) or not name or name.startswith("-") or name != name.strip():
        return project["id"]
    try:
        return name if store.resolve_project(name)["id"] == project["id"] else project["id"]
    except ValueError:
        return project["id"]


def _not_run_without_models(settings, command: str) -> None:
    """An evaluation of the models in deterministic-only mode (#274): not run, exit 3, and nothing is asked."""
    from cairnmind.agents.knowledge_mode import needs_models
    why = needs_models(settings, command)
    if why:
        output({"status": "not_run", "reason": why})
        raise typer.Exit(3)


def _record_evaluation(settings, kind: str, result: dict) -> None:
    """A passing evaluation is recorded for the pair of models it ran with (doctor and answers read it)."""
    if result.get("status") == "passed" and not result.get("inference") == "injected_adapter":
        from cairnmind.memory.memory_models import record_passed
        result["models_evaluation_recorded"] = record_passed(settings, kind, result.get("model_identities"))


SNAPSHOT_WAIT_SECONDS = 1800


def _retry_jobs(store):
    return store.retry_jobs()
