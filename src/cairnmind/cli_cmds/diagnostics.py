"""CLI entry callback, workspace/cockpit/TUI launchers, init, doctor, setup, migrate, serve, stop, ui."""
from __future__ import annotations

import logging
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any

import typer

from cairnmind.cli_cmds import io as _io
from cairnmind.cli_cmds.io import JSON_FLAG, initialized
from cairnmind.config import Settings
from cairnmind.pal.file_locks import lock_exclusive, unlock

_log = logging.getLogger(__name__)


def register(apps: dict[str, typer.Typer]) -> None:
    app = apps["app"]
    cockpit_app = apps["cockpit_app"]
    workspace_app = apps["workspace_app"]

    @app.callback(invoke_without_command=True)
    def main(
        ctx: typer.Context,
        data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
        backend: Annotated[str | None, typer.Option("--backend", help="Storage backend: sqlite or arcade.")] = None,
        db_path: Annotated[str | None, typer.Option("--db-path", help="Override SQLite database path (or :memory:).")] = None,
    ):
        from cairnmind.cli_cmds.cli_human import color_enabled
        ctx.color = color_enabled(sys.stdout)
        _io._INVOCATION.clear()
        ctx.obj = Settings.load(data_dir, create=False)
        if backend is not None:
            chosen = backend.strip().lower()
            if chosen not in {"sqlite", "arcade"}:
                _io.refuse("Storage backend must be 'sqlite' or 'arcade'.")
            ctx.obj.storage_backend = chosen
            _io._INVOCATION["backend"] = chosen
        if db_path is not None:
            ctx.obj.db_path = db_path
            _io._INVOCATION["db_path"] = db_path
        _io._INVOCATION["data_dir"] = ctx.obj.data_dir
        _io._INVOCATION["as_json"] = "--json" in sys.argv[1:]
        if ctx.invoked_subcommand is None:
            _io._launch_terminal_workspace(ctx)

    @app.command()
    def tui(ctx: typer.Context):
        """Open the Cairnmind terminal workspace (the same as the command without arguments)."""
        _io._launch_terminal_workspace(ctx)

    @app.command("version")
    def version():
        """Show the version without initializing configuration or services."""
        from cairnmind import __version__
        _io.output({"version": __version__})

    @workspace_app.command("status")
    def terminal_workspace_status(ctx: typer.Context):
        """Read the status without starting services or initializing configuration."""
        from cairnmind.terminal.workspace import workspace_status
        _io.output(workspace_status(ctx.obj))

    @workspace_app.command("detach")
    def terminal_workspace_detach(ctx: typer.Context):
        """Detach the terminal view and keep the client sessions."""
        from cairnmind.terminal.workspace import detach_workspace
        _io.output(detach_workspace(ctx.obj))

    @workspace_app.command("takeover")
    def terminal_workspace_takeover(ctx: typer.Context):
        """Take control in this terminal; the previous view becomes read-only."""
        _io._launch_terminal_workspace(ctx, takeover=True)

    @workspace_app.command("stop")
    def terminal_workspace_stop(ctx: typer.Context, clients: bool = False):
        """Close the workspace; --clients also stops the sessions that belong to it."""
        from cairnmind.terminal.workspace import stop_workspace
        _io.output(stop_workspace(ctx.obj, stop_clients=clients))

    @cockpit_app.command("serve")
    @initialized
    def cockpit_serve(ctx: typer.Context, port: int | None = None):
        """Run the cockpit server; normally the cairnmind command manages it."""
        from cairnmind.terminal.cockpit import serve_cockpit
        serve_cockpit(ctx.obj, port=port)

    @cockpit_app.command("status")
    def cockpit_status(ctx: typer.Context):
        """Read the cockpit status without starting services or initializing configuration."""
        from cairnmind.terminal.cockpit import cockpit_status as inspect
        _io.output(inspect(ctx.obj))

    @cockpit_app.command("stop")
    def cockpit_stop(ctx: typer.Context, clients: bool = False):
        """Stop the cockpit; --clients explicitly stops the managed terminals too."""
        from cairnmind.terminal.cockpit import stop_cockpit
        _io.output(stop_cockpit(ctx.obj, stop_clients=clients))

    @app.command("try")
    def try_command(
        non_interactive: Annotated[
            bool, typer.Option("--non-interactive", help="Run all walkthrough steps without waiting for Enter.")
        ] = False,
        as_json: JSON_FLAG = False,
    ):
        """Try Cairnmind in an ephemeral in-memory sandbox without touching your current directory.

        Runs a 3-step proof on the bundled sample project using embedded SQLite in memory: unknown on missing evidence, blocked done after a code edit, and verified done after a fresh check run."""
        from cairnmind.cli_cmds.try_demo import run_try_sandbox

        emit_json = as_json or not _io._stdout_is_terminal()
        interactive = (not non_interactive) and (not emit_json) and _io._interactive_terminal()
        try:
            result = run_try_sandbox(
                interactive=interactive,
                echo=None if emit_json else typer.echo,
                prompt=input if interactive else None,
            )
        except KeyboardInterrupt:
            typer.echo("Cairnmind: try sandbox interrupted and cleaned up.", err=True)
            raise typer.Exit(130) from None
        if emit_json:
            _io.output(result)

    @app.command()
    @initialized
    def init(
        ctx: typer.Context,
        backend: Annotated[str | None, typer.Option("--backend", help="Storage backend to initialize: sqlite or arcade.")] = None,
        as_json: JSON_FLAG = False,
    ):
        """Create the store, or open the one a connect-only installation was given.

        Downloads no models and changes no client configuration."""
        from cairnmind.config import resolved_backend
        from cairnmind.storage.database_mode import connect_only, engine_mode
        from cairnmind.storage.engine_docker import choose_engine_mode, start_docker_engine

        if backend is not None:
            chosen_backend = backend.strip().lower()
            if chosen_backend not in {"sqlite", "arcade"}:
                _io.refuse("Storage backend must be 'sqlite' or 'arcade'.")
            ctx.obj.storage_backend = chosen_backend
            ctx.obj.save()

        active_backend = resolved_backend(ctx.obj)
        if active_backend == "arcade":
            chosen = choose_engine_mode(ctx.obj)  # the first run chooses where the engine runs, and says which (#272)
            if chosen:
                typer.echo(f"Cairnmind: {chosen}", err=True)
            if not connect_only(ctx.obj) and engine_mode(ctx.obj) == "docker":
                start_docker_engine(ctx.obj, pull=True)  # the first start downloads the image; ensure_engine never does
        store = _io.store_for(ctx.obj)
        from cairnmind.agents.knowledge_mode import choose, enabled
        said = choose(ctx.obj)  # the first run chooses whether the knowledge agent has models, and says which (#274)
        if said:
            typer.echo(f"Cairnmind: {said}", err=True)
        result: dict[str, Any] = {
            "data_dir": str(ctx.obj.data_dir),
            "storage_backend": active_backend,
            "next": "cairnmind doctor, then cairnmind project add NAME FOLDER",
        }
        result["knowledge_agent"] = "enabled" if enabled(ctx.obj) else "disabled"
        if active_backend == "arcade" and not connect_only(ctx.obj):
            result["engine_mode"] = engine_mode(ctx.obj)
        if getattr(store, "not_writer", None):
            result["writer"] = store.writer_state()
        from cairnmind.memory.confirmation_upgrade import report
        marks = report(getattr(store, "confirmations_before_rule", None))
        if marks:
            result["confirmations_before_rule"] = marks

        def sentences():
            from cairnmind.cli_cmds import first_start
            lines = [first_start.INIT_READY.format(data_dir=ctx.obj.data_dir)]
            if marks:
                lines.append(first_start.INIT_MARKED.format(marked=marks["marked_as_a_persons"],
                                                            left=marks["still_before_the_rule"]))
            return lines
        _io._human_or_json(result, as_json, sentences)

    @app.command()
    @initialized
    def doctor(ctx: typer.Context, contract: bool = typer.Option(
            False, "--contract", help="Show only the client contract: every check with its evidence line."),
               output_format: Annotated[str, typer.Option(
                   "--format", help="With --contract: json (default) or text, one line per client and check.")] = "json",
               check_mcp: Annotated[bool, typer.Option(
                   "--check-mcp", help="Run a bounded 5-second stdio JSON-RPC handshake probe against Cairnmind's MCP server.")] = False,
               project: Annotated[str | None, typer.Option(
                   "--project", help="Project name, id or folder for Memory Pulse and project-scoped MCP preset detection.")] = None,
               as_json: bool = typer.Option(
                   False, "--json", help="Print the full report as JSON; the default when the output is not a terminal.")):
        """Report where storage and the models run and what Cairnmind needs and what is optional: a short summary in a
        terminal, the full report as JSON otherwise. Read-only. Exits 1 when something required is missing."""
        if output_format not in ("json", "text"):
            raise typer.BadParameter("--format must be json or text.")
        if output_format == "text" and not contract:
            raise typer.BadParameter("--format text is available only with --contract.")
        if contract:
            from cairnmind.memory.integrations import CLIENTS, _probe_cli, contract_text
            probes = {name: _probe_cli(name) for name in CLIENTS}
            if output_format == "text":
                typer.echo(contract_text(probes))
            else:
                _io.output({name: {key: probe[key] for key in ("installed", "version", "verified_resume", "checks", "notes")}
                        for name, probe in probes.items()})
            if any(probe["installed"] and not probe["verified_resume"] for probe in probes.values()):
                raise typer.Exit(1)
            return
        from cairnmind.memory.integrations import doctor as inspect
        result = inspect(ctx.obj)
        from cairnmind.agents.knowledge_mode import doctor_fields
        result.update(doctor_fields(ctx.obj))  # without models: information, not a fault (#274)
        result["service_running"] = _io.service_alive(ctx.obj)
        result["data_dir"] = str(ctx.obj.data_dir)
        result["service_url"] = ctx.obj.url  # the address every command here talks to, so a collision is visible
        from cairnmind.cli_cmds.service_version import refusal as other_version
        from cairnmind.cli_cmds.upgrade_mark import note as upgrade_note
        from cairnmind.config import ENV_PREFIXES, data_dir_note, env_conflicts, env_names, session_conflicts
        from cairnmind.terminal.setup_mark import note as setup_note
        from cairnmind.terminal.setup_mark import unchecked as setup_unchecked
        # Shown here and nowhere else: a hook or an MCP server must not print notes of its own.
        # The notes of a shell whose commands are refused are also listed as blockers: the summary counts them as missing.
        blockers = [
            f"{ENV_PREFIXES[0]}{name} and {ENV_PREFIXES[1]}{name} name different sessions; neither is used: commands in "
            "this shell are refused and hooks and the MCP server stay unbound. Start the client again through the launcher."
            for name in session_conflicts()]
        # A service of another version answers, but every command that needs it is refused: required and not working,
        # so it is a blocker too, not only a note (#319). Asked only of this data directory's service.
        blockers += [note for note in [other_version(ctx.obj) if result["service_running"] else None] if note]
        # A change of setup that ended with settings nobody checked: nothing starts the service until they are (#339).
        unchecked = setup_unchecked(ctx.obj.data_dir)
        blockers += [str(unchecked)] if unchecked else []
        result["environment_notes"] = [
            f"{ENV_PREFIXES[0]}{name} and {ENV_PREFIXES[1]}{name} are both set and differ; {ENV_PREFIXES[0]}{name} is used."
            for name in env_conflicts()] + blockers + [
                str(note) for note in [data_dir_note(), upgrade_note(),
                                       None if unchecked else setup_note(ctx.obj.data_dir)] if note]  # not twice
        result["environment_blockers"] = blockers
        for name, variable in (("port", "PORT"), ("cockpit_port", "COCKPIT_PORT"), ("ollama_url", "OLLAMA_URL")):
            if name in ctx.obj._origin.get("kept", {}):
                stored, used = ctx.obj._origin["kept"][name]
                spelling = next(key for key in env_names(variable) if os.environ.get(key))
                stored = stored if stored not in (None, "") else "not set"
                result["environment_notes"].append(
                    f"{spelling} is used by this process: {name} is {used} here, and {stored} in config.json.")
        from cairnmind.storage.engine import log_settings_problems
        # The same lines `engine start` prints, so a mistyped log setting is not seen only at start (#238).
        result["environment_notes"] += [line.removeprefix("cairnmind: ") for line in log_settings_problems(ctx.obj)]
        result["models_lock_present"] = (ctx.obj.data_dir / "models.lock.json").exists()
        from cairnmind.storage.store_writer import inspect as writer_role
        result["store_writer"] = writer_role(ctx.obj)
        from cairnmind.agents.api_runtime import coding_profiles_status
        result["coding_profiles"] = coding_profiles_status(ctx.obj)
        from cairnmind.memory.record_kinds import doctor_view
        result["projects"] = None  # how many are registered; None when the store cannot be read
        result["restore_unfinished"] = None  # the mark of a restore that did not finish (#362); never None when unknown
        from cairnmind.storage.restore_staged import RestoreUnfinished
        result["project_folders"] = None  # the folders this machine cannot read, by project (#371); None likewise
        store = None
        try:
            # What a person accepted as technical, per project, and how to withdraw it (#210).
            store = _io.store_for_read(ctx.obj)
            result["accepted_record_kinds"] = doctor_view(store)
            result["projects"] = len(store.list_projects())
            from cairnmind.memory.project_folders import unavailable
            result["project_folders"] = unavailable(store)
        except RestoreUnfinished as exc:
            # A required thing is missing: the store holds part of a backup. The sentence names the way out.
            result["restore_unfinished"] = exc.mark or {"unreadable": True}
            blockers.append(str(exc))
            result.setdefault("accepted_record_kinds", {"unavailable": "RestoreUnfinished"})
        except Exception as exc:  # noqa: BLE001 - doctor reports a store it cannot read, it does not stop
            _log.debug("Doctor read-only store inspection failed: %s", exc)
            result["restore_unfinished"] = {"unavailable": type(exc).__name__}
            result.setdefault("accepted_record_kinds", {"unavailable": type(exc).__name__})
        from cairnmind.cli_cmds.doctor_preflight import (
            doctor_mcp_handshake,
            doctor_mcp_presets,
            doctor_pulse_block,
            security_guards_status,
            storage_preflight,
        )
        try:
            pulse_report, project_root = doctor_pulse_block(ctx.obj, store, project=project)
        finally:
            if store is not None and hasattr(store, "client") and hasattr(store.client, "close"):
                try:
                    store.client.close()
                except Exception as exc:  # noqa: BLE001 - best-effort close of diagnostic client
                    _log.debug("Failed to close doctor read-only store client: %s", exc)
        result["pulse"] = pulse_report
        result["mcp_presets"] = doctor_mcp_presets(project_root=project_root or Path.cwd())
        result["storage_preflight"] = storage_preflight(ctx.obj, project_root=project_root)
        result["security_guards"] = security_guards_status(ctx.obj, project_root=project_root)
        if check_mcp:
            result["mcp_handshake"] = doctor_mcp_handshake(ctx.obj, project_root=project_root, timeout=5.0)
        from cairnmind.cli_cmds.topology import topology
        # From what doctor found above, so it asks nothing more (#74): first, so the JSON reads top-down.
        java = result.get("java", {})  # a Java below 21 is not named as the engine's: the attention line says what was found
        result = {"topology": topology(ctx.obj, models=result.get("memory_models"), writer=result["store_writer"],
                                         java_major=java.get("major") if java.get("compatible") else None), **result}
        from cairnmind.cli_cmds.doctor_attention import required_missing, summary
        _io._human_or_json(result, as_json, lambda: summary(result))
        if required_missing(result):
            raise typer.Exit(1)  # something required is missing; models, tmux and clients never change this (#313)

    @app.command()
    def pulse(
        ctx: typer.Context,
        project: Annotated[
            str | None,
            typer.Argument(
                metavar="PROJECT",
                help="The project's name, id or folder. Defaults to the current repository or working directory.",
            ),
        ] = None,
        worktree: Annotated[
            Path | None,
            typer.Option("--worktree", help="Registered worktree to audit instead of the current directory."),
        ] = None,
        strict: Annotated[
            bool,
            typer.Option("--strict", help="Exit 1 when drift, stale tests or degraded records are detected (ideal for CI and pre-push hooks)."),
        ] = False,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Print a machine-readable JSON pulse report on stdout."),
        ] = False,
    ):
        """Fast read-only consistency micro-audit of code drift, test freshness, degraded records and snapshot integrity.

        Starts no engine or service and creates no lock or temporary files. Exit codes: 0 for HEALTHY (or DRIFT without --strict), 1 for DRIFT with --strict, 2 for BROKEN integrity or unavailable store.
        """
        from cairnmind.memory.pulse import exit_code_for_pulse, render_pulse, run_pulse

        report = run_pulse(ctx.obj, project=project, worktree=worktree)
        if as_json:
            _io.output(report)
        else:
            typer.echo(render_pulse(report))
        code = exit_code_for_pulse(report, strict=strict)
        if code:
            raise typer.Exit(code)

    @app.command()
    @initialized
    def setup(ctx: typer.Context,
              profile: Annotated[str | None, typer.Argument(metavar="[PROFILE]", help=(
                  "local, remote-db or remote. Without it, setup only shows where storage and the models run."))] = None,
              as_json: Annotated[bool, typer.Option("--json", help="Print the topology, or a profile's plan, as JSON.")] = False,
              url: Annotated[str | None, typer.Option("--url", help="remote-db, remote: the ArcadeDB this installation only connects to.")] = None,
              user: Annotated[str | None, typer.Option("--user", help="remote-db, remote: the database user; never root on another host.")] = None,
              database: Annotated[str | None, typer.Option("--database", help="remote-db, remote: the database name.")] = None,
              password_file: Annotated[str | None, typer.Option("--password-file", help=(
                  "remote-db, remote: a private file (mode 0600) you created with the user's password; only its path is saved."))] = None,
              ca_file: Annotated[str | None, typer.Option("--ca-file", help="remote-db, remote: the PEM file of a private certificate authority.")] = None,
              admin_password_file: Annotated[str | None, typer.Option("--admin-password-file", help=(
                  "remote-db, remote: a private file with the server's root password; with --yes the database and its user "
                  "are created first, as `cairnmind db create-user` does. Never saved."))] = None,
              models_url: Annotated[str | None, typer.Option("--models-url", help="remote: the model server on another computer (HTTPS).")] = None,
              provider: Annotated[str | None, typer.Option("--provider", help="remote: ollama or openai_compatible.")] = None,
              chat_model: Annotated[str | None, typer.Option("--chat-model", help="remote: the chat model's name on that server.")] = None,
              embedding_model: Annotated[str | None, typer.Option("--embedding-model", help="remote: the embedding model's name on that server.")] = None,
              credential_ref: Annotated[str | None, typer.Option("--credential-ref", help="remote: env:NAME or profile:ID naming the server's key; never the key.")] = None,
              allow_remote: Annotated[bool, typer.Option("--allow-remote", help="remote: consent to send memory content to that server.")] = False,
              backup: Annotated[str | None, typer.Option("--backup", help=(
                  "local, from a store this installation only connects to: back it up into this new file and restore that here."))] = None,
              empty: Annotated[bool, typer.Option("--empty", help=(
                  "local, from a store this installation only connects to: use the store on this machine without its data."))] = False,
              yes: Annotated[bool, typer.Option("--yes", help="Make the change; without it nothing changes.")] = False):
        """Show where storage and the memory models run; with PROFILE (local, remote-db, remote), move them.

        A profile is tested before anything is saved; without --yes it is only tested and shown. Refused while the knowledge service runs and in a coding session. Passwords and keys are never asked for, printed or saved: --password-file names a file you created. Without a profile, no setting of an initialized installation changes, except that a database address given for the first time in CAIRNMIND_ARCADE_CONNECT_URL is saved, as every command saves it."""
        from cairnmind.terminal import setup_profiles
        fields = {"url": url, "user": user, "database": database, "password_file": password_file, "ca_file": ca_file,
                  "admin_password_file": admin_password_file, "models_url": models_url, "provider": provider,
                  "chat_model": chat_model, "embedding_model": embedding_model, "credential_ref": credential_ref,
                  "allow_remote": allow_remote, "backup": backup, "empty": empty}
        if profile is None:
            given = setup_profiles.given_options(fields)
            if given:
                raise typer.BadParameter(str(setup_profiles.Said(setup_profiles.WITHOUT_PROFILE, option=given[0])))
            from cairnmind.cli_cmds.topology import CHANGE_HINT, probe
            place = probe(ctx.obj)
            if as_json:
                _io.output(place)
            else:
                typer.echo("\n".join([*place["lines"], "", CHANGE_HINT]))
            return
        request = setup_profiles.Request(profile, **fields)
        problem = setup_profiles.option_problem(request)
        if problem:
            raise typer.BadParameter(str(problem))
        # Every refusal says "nothing was changed"; the generic rule may already have saved the address of a variable.
        tail = (" " + str(setup_profiles.Said(setup_profiles.ADDRESS_SAVED))) if ctx.obj._origin.get("connect_saved") else ""
        if _io.session_value("SESSION_ID"):
            _io.refuse(str(setup_profiles.Said(setup_profiles.IN_SESSION)) + tail)
        running = str(setup_profiles.Said(setup_profiles.SERVICE_RUNS)) + tail
        with _io._service_stopped(ctx.obj, running, running):
            try:
                result = setup_profiles.run(ctx.obj, request, yes=yes,
                                            say=lambda text: typer.echo(f"Cairnmind: {text}", err=True))
            except setup_profiles.SetupRefused as exc:
                _io.refuse(str(exc) + tail)
        if as_json:
            _io.output(setup_profiles.as_json(result))
        else:
            typer.echo("\n".join(setup_profiles.render(result)))
        if result["changes"] and not result["applied"]:
            raise typer.Exit(1)

    @app.command("migrate")
    @initialized
    def migrate_command(
        ctx: typer.Context,
        to: Annotated[str | None, typer.Option("--to", help="Target backend: server (ArcadeDB) or sqlite.")] = None,
        resume: Annotated[bool, typer.Option("--resume", help="Resume an interrupted migration from its last checkpoint.")] = False,
        abort: Annotated[bool, typer.Option("--abort", help="Abort an interrupted migration and clean partial target data.")] = False,
        overwrite: Annotated[bool, typer.Option("--overwrite", help="Replace existing data in a non-empty target database.")] = False,
        target_namespace: Annotated[str | None, typer.Option("--target-namespace", help="ArcadeDB database name to create or migrate into.")] = None,
        include_cache: Annotated[bool, typer.Option("--include-cache", help="Verify and report verdict cache continuity alongside the migration.")] = False,
        force: Annotated[bool, typer.Option("--force", help="Allow reverse migration to SQLite even when size thresholds are exceeded.")] = False,
        batch_size: Annotated[int, typer.Option("--batch-size", help="Number of rows transferred per checkpointed batch.")] = 1000,
        as_json: JSON_FLAG = False,
    ):
        """Migrate all Cairnmind data losslessly between embedded SQLite and ArcadeDB server.

        Verifies exact per-collection counts and canonical SHA-256 parity before atomically switching config.json. Refused inside coding sessions."""
        from cairnmind.storage.migration import MigrationError, abort_migration
        from cairnmind.storage.migration import migrate as run_migration

        if abort and (resume or to is not None):
            raise typer.BadParameter("Use --abort by itself, without --to or --resume.")
        if resume and to is not None:
            raise typer.BadParameter("Use --resume without --to; the direction is read from the saved checkpoint.")
        if not abort and not resume and to not in {"server", "sqlite"}:
            raise typer.BadParameter("Specify --to server, --to sqlite, --resume, or --abort.")

        try:
            if abort:
                result = abort_migration(ctx.obj)

                def sentences():
                    return [f"Aborted migration {result.get('migration_id') or ''}; source store and config.json were left untouched."]

                _io._human_or_json(result, as_json, sentences)
                return

            result = run_migration(
                ctx.obj,
                to=to,
                resume=resume,
                overwrite=overwrite,
                target_namespace=target_namespace,
                include_cache=include_cache,
                force=force,
                batch_size=batch_size,
            )
        except MigrationError as exc:
            _io.refuse(str(exc))

        def sentences():
            lines = [
                f"Migrated {result['total_rows']} rows across {result['collections']} collections "
                f"({result['source_backend']} -> {result['target_backend']}), SHA-256 {result['sha256'][:16]}."
            ]
            if result.get("archived_sqlite"):
                lines.append(f"Archived previous SQLite database to {result['archived_sqlite']}.")
            if result.get("person_confirmations") != "kept":
                lines.append(
                    f"Source role was reader: {result.get('confirmations_turned_into_claims', 0)} confirmation(s) "
                    "were downgraded to migration claims."
                )
            return lines

        _io._human_or_json(result, as_json, sentences)

    @app.command()
    def ui(
        ctx: typer.Context,
        port: Annotated[int, typer.Option("--port", help="Loopback TCP port for the Cairnmind UI inspector.")] = 4310,
        host: Annotated[str, typer.Option("--host", help="Loopback bind host (127.0.0.1, localhost, or ::1).")] = "127.0.0.1",
        project: Annotated[str | None, typer.Option("--project", help="Default project id, name, or folder.")] = None,
        mock: Annotated[bool, typer.Option("--mock", help="Serve the deterministic mock dataset without ArcadeDB.")] = False,
        read_only: Annotated[bool, typer.Option("--read-only", help="Serve the cockpit in safe read-only preview mode (disables mutations).")] = False,
        no_open: Annotated[bool, typer.Option("--no-open", help="Do not open a browser tab automatically.")] = False,
    ):
        """Serve the local Engineer Cockpit & Provenance Inspector web UI on localhost."""
        import threading
        import webbrowser

        import uvicorn

        from cairnmind.ui.ui_auth import UiSessionAuth
        from cairnmind.ui.ui_server import create_ui_app

        if host not in {"127.0.0.1", "localhost", "::1"}:
            _io.refuse("Cairnmind UI must bind to a loopback host (127.0.0.1, localhost, or ::1).")
        if not 1 <= port <= 65535:
            _io.refuse("Port must be between 1 and 65535.")

        store = None
        settings = ctx.obj
        if not mock:
            if settings is None or not settings.token:
                _io.refuse("Initialize Cairnmind first (`cairnmind init`), or pass `--mock` to preview with sample data.")
            store = _io.store_for_read(settings) if read_only else _io.store_for(settings)

        ui_auth = UiSessionAuth.create(host=host, port=port)
        api = create_ui_app(
            store=store,
            settings=settings,
            default_project=project,
            default_mock=mock,
            read_only=read_only,
            host=host,
            port=port,
            auth=ui_auth,
        )
        url = ui_auth.bootstrap_url(host=host, mock=mock, project=project)
        mode_tag = " [read-only]" if read_only else ""
        typer.echo(f"Cairnmind UI serving on {url}{mode_tag}")
        if not no_open:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        server = uvicorn.Server(uvicorn.Config(api, host=host, port=port, log_level="info"))
        server.run()

    @app.command()
    @initialized
    def serve(ctx: typer.Context, defer_background_inference: bool = False):
        """Run the local service (the single owner of the GPU and the queue)."""
        import uvicorn

        from cairnmind.service import create_app
        settings = ctx.obj
        with (settings.data_dir / "service.lock").open("a", encoding="utf-8") as lock:
            try:
                lock_exclusive(lock, blocking=False)
            except BlockingIOError:
                _io.refuse("The service is already running for this store.")
            try:
                api = create_app(settings, background_inference=not defer_background_inference)
                server = uvicorn.Server(uvicorn.Config(api, host=settings.host, port=settings.port, log_level="info"))
                api.state.shutdown = lambda: setattr(server, "should_exit", True)
                server.run()
            finally:
                with suppress(OSError):
                    unlock(lock)

    @app.command()
    @initialized
    def stop(ctx: typer.Context, as_json: JSON_FLAG = False):
        """Stop only this store's service, without deleting data."""
        from cairnmind.cli_cmds import first_start
        from cairnmind.storage.engine import local_process_recorded
        from cairnmind.storage.engine_docker import container_recorded
        if not _io.service_alive(ctx.obj):
            result = {"stopping": False, "reason": "The service is not running"}
            lines = [first_start.STOP_NOT_RUNNING.format(url=ctx.obj.url)]
        else:
            # Reached whatever version it runs: the way out of a refusal (#319).
            result = _io.request(ctx.obj, "POST", "/shutdown", matched=True)
            lines = [(first_start.STOP_STOPPING if result.get("stopping") else first_start.STOP_REFUSED)
                     .format(url=ctx.obj.url)]
        if local_process_recorded(ctx.obj) or container_recorded(ctx.obj):
            lines.append(first_start.ENGINE_LEFT_RUNNING)
        from cairnmind.terminal.cockpit import cockpit_status
        if cockpit_status(ctx.obj)["available"]:  # the interface broker is a process of its own (#169)
            lines.append(first_start.BROKER_LEFT_RUNNING)
        _io._human_or_json(result, as_json, lambda: lines)
