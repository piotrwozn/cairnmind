"""Cairnmind CLI facade; native coding clients retain their own terminal and permissions."""
from __future__ import annotations

import json as json
import os as os
import shutil as shutil
import subprocess as subprocess
import sys
import types

import httpx as httpx
import typer
from typer.core import TyperArgument, TyperGroup

from cairnmind.agents.ollama import OllamaUnavailable
from cairnmind.cli_cmds import (
    cli_importers as _cli_importers,
    cli_published as _cli_published,
    cli_record_kinds as _cli_record_kinds,
    diagnostics as _diagnostics,
    engine_cmds as _engine_cmds,
    eval_cmds as _eval_cmds,
    io as _io,
    memory_cmds as _memory_cmds,
    models_cmds as _models_cmds,
    plans as _plans,
    projects as _projects,
    scip_cmds as _scip_cmds,
    sessions as _sessions,
)
from cairnmind.cli_cmds.io import (
    JSON_FLAG as JSON_FLAG,
    READ_KINDS as READ_KINDS,
    SNAPSHOT_WAIT_SECONDS as SNAPSHOT_WAIT_SECONDS,
    ServiceDidNotStart as ServiceDidNotStart,
    ServiceOfOtherVersion as ServiceOfOtherVersion,
    ServiceRefusal as ServiceRefusal,
    SetupInProgress as SetupInProgress,
    UpgradeInProgress as UpgradeInProgress,
    _INVOCATION as _INVOCATION,
    _NOTICES_SHOWN as _NOTICES_SHOWN,
    _RESOLVE as _RESOLVE,
    _bind_failed as _bind_failed,
    _data_dir_arguments as _data_dir_arguments,
    _engine_command as _engine_command,
    _ensure_service_or_refuse as _ensure_service_or_refuse,
    _format_cli_stderr as _format_cli_stderr,
    _human_or_json as _human_or_json,
    _interactive_terminal as _interactive_terminal,
    _json_file as _json_file,
    _launch_terminal_workspace as _launch_terminal_workspace,
    _managed_only as _managed_only,
    _not_run_without_models as _not_run_without_models,
    _os_error_text as _os_error_text,
    _pasteable as _pasteable,
    _project_handle as _project_handle,
    _record_evaluation as _record_evaluation,
    _refuse_during_upgrade as _refuse_during_upgrade,
    _refuse_other_version as _refuse_other_version,
    _refuse_unknown_project as _refuse_unknown_project,
    _retry_jobs as _retry_jobs,
    _service_stopped as _service_stopped,
    _service_unavailable as _service_unavailable,
    _shortened as _shortened,
    _stderr_is_terminal as _stderr_is_terminal,
    _stdout_is_terminal as _stdout_is_terminal,
    _stop_started as _stop_started,
    _wait_for_start as _wait_for_start,
    actor_scope as actor_scope,
    actor_write as actor_write,
    caller_path as caller_path,
    ensure_no_upgrade as ensure_no_upgrade,
    ensure_service as ensure_service,
    initialized as initialized,
    last_log_line as last_log_line,
    output as output,
    refuse as refuse,
    registered_projects as registered_projects,
    request as request,
    say_service_started as say_service_started,
    service_alive as service_alive,
    session_value as session_value,
    store_for as store_for,
    store_for_read as store_for_read,
    time as time,
)
from cairnmind.storage.arcade import ArcadeError


class _ProjectFirstGroup(TyperGroup):
    """A command whose first argument is the project and that is given only one: the error says the project comes
    first (#326). A question cannot be told from a project name, so nothing is looked up and nothing is started."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except Exception as exc:
            # By name: newer Typer carries its own copy of Click, so click.MissingParameter would not match.
            if type(exc).__name__ != "MissingParameter":
                raise
            arguments = [p for p in exc.ctx.command.params if isinstance(p, TyperArgument)] if exc.ctx else []
            if (len(arguments) > 1 and arguments[0].metavar == "PROJECT" and isinstance(exc.param, TyperArgument)
                    and exc.param is not arguments[0]):
                from cairnmind.cli_cmds.first_start import PROJECT_FIRST
                exc.message = PROJECT_FIRST.format(
                    missing=" ".join(a.metavar or a.name for a in arguments[1:]))
            raise


app = typer.Typer(cls=_ProjectFirstGroup, no_args_is_help=False, pretty_exceptions_enable=False,
                  help="A terminal workspace with memory: Codex, Claude, Ollama, API and GitHub Copilot. Start: "
                       "cairnmind doctor, then cairnmind project add NAME FOLDER, cairnmind remember NAME TEXT and "
                       "cairnmind brief NAME.")
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
cache_app = typer.Typer(help="Inspect or clear the local verification verdict cache.")
service_app = typer.Typer(help="Background knowledge service autostart management.")
autostart_app = typer.Typer(help="Per-user service autostart at logon (Windows Task Scheduler).")
scip_app = typer.Typer(help="Multi-language SCIP code indexing (Go, Rust, Java, Kotlin, C/C++, C#, PHP, Ruby).")
mcp_app = typer.Typer(no_args_is_help=False, help="MCP over stdio, universal client presets, and handshake probe.")

service_app.add_typer(autostart_app, name="autostart")
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
app.add_typer(cache_app, name="cache")
app.add_typer(service_app, name="service")
app.add_typer(scip_app, name="scip")
app.add_typer(mcp_app, name="mcp")

_APPS = {
    "app": app,
    "projects_app": projects_app,
    "tasks_app": tasks_app,
    "models_app": models_app,
    "engine_app": engine_app,
    "plans_app": plans_app,
    "checks_app": checks_app,
    "stage_app": stage_app,
    "sessions_app": sessions_app,
    "eval_app": eval_app,
    "jobs_app": jobs_app,
    "memory_app": memory_app,
    "cockpit_app": cockpit_app,
    "workspace_app": workspace_app,
    "store_app": store_app,
    "db_app": db_app,
    "answers_app": answers_app,
    "cache_app": cache_app,
    "service_app": service_app,
    "autostart_app": autostart_app,
    "scip_app": scip_app,
    "mcp_app": mcp_app,
}

for _mod in (
    _diagnostics,
    _engine_cmds,
    _projects,
    _plans,
    _sessions,
    _eval_cmds,
    _memory_cmds,
    _models_cmds,
    _scip_cmds,
):
    _mod.register(_APPS)

_cli_published.register(memory_app, initialized)
_cli_record_kinds.register(app, initialized)
_cli_importers.register(app)


def entrypoint() -> None:
    _io._NOTICES_SHOWN.clear()
    _io._INVOCATION["as_json"] = "--json" in sys.argv[1:]
    try:
        app()
    except (ValueError, OSError, ArcadeError, httpx.HTTPError, OllamaUnavailable) as exc:
        from cairnmind.storage.store_writer import StoreNotWriter
        if not (isinstance(exc, StoreNotWriter) and str(exc) in _io._NOTICES_SHOWN):
            raw = _io._os_error_text(exc) if isinstance(exc, OSError) else str(exc)
            typer.echo(_io._format_cli_stderr("Cairnmind: ", raw), err=True)
        raise SystemExit(1) from None


_IO_FORWARDED = frozenset(name for name in dir(_io) if not name.startswith("__"))
_ORIG_GLOBALS = globals()


class _CliFacade(types.ModuleType):
    """Module facade forwarding attribute patches on `cairnmind.cli` to `cairnmind.cli_cmds.io`."""

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        _ORIG_GLOBALS[name] = value
        if name in _IO_FORWARDED:
            setattr(_io, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        _ORIG_GLOBALS.pop(name, None)
        if name in _IO_FORWARDED and hasattr(_io, name):
            delattr(_io, name)


_self_mod = sys.modules.get(__name__)
if _self_mod is not None and not isinstance(_self_mod, _CliFacade):
    _facade = _CliFacade(__name__, __doc__)
    _facade.__dict__.update(_self_mod.__dict__)
    sys.modules[__name__] = _facade
