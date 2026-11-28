"""CLI commands for database engine, DB users, store management, backup/restore, and service autostart."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from cairnmind.cli_cmds import io as _io
from cairnmind.cli_cmds.cli_arguments import (
    ExportArg,
    ProjectArg,
    QuestionArg,
)
from cairnmind.cli_cmds.io import JSON_FLAG, initialized

TAKEOVER_NEXT = {
    "writer": "This installation is already the writer; nothing to do.",
    "unknown": ("The role could not be read. Start the engine with `cairnmind engine start` and run this again, "
                "or pass --yes, which starts it and makes this installation the writer."),
    None: "Run `cairnmind store takeover --yes` to make this installation the writer.",
}


def register(apps: dict[str, typer.Typer]) -> None:
    app = apps["app"]
    engine_app = apps["engine_app"]
    store_app = apps["store_app"]
    db_app = apps["db_app"]
    autostart_app = apps["autostart_app"]

    @engine_app.command("install")
    @initialized
    def engine_install(ctx: typer.Context, distribution: Path | None = None):
        """Download the pinned ArcadeDB engine and verify its SHA-256 (about 87 MB)."""
        from cairnmind.storage.engine import install_engine
        with _io._engine_command():
            path = install_engine(ctx.obj, distribution)
        _io.output({"installed": str(path), "version": ctx.obj.arcade_version})

    @engine_app.command("start")
    @initialized
    def engine_start(
            ctx: typer.Context,
            docker: Annotated[bool, typer.Option("--docker", help="Run the engine in Docker; saved as engine_mode.")] = False,
            native: Annotated[bool, typer.Option(
                "--native", help="Run the engine with Java 21 or newer on this machine; saved as engine_mode.")] = False,
            build: Annotated[bool, typer.Option(
                "--build", help="With --docker: build the image on this machine from the files shipped with "
                                "Cairnmind, instead of downloading it.")] = False):
        """Start the database engine in its saved mode, or in the one --docker or --native names."""
        if docker and native:
            raise typer.BadParameter("Choose one of --docker and --native.")
        if build and not docker:
            raise typer.BadParameter("--build goes with --docker.")
        from cairnmind.storage.engine import start_command
        with _io._engine_command():
            _io.output(start_command(ctx.obj, docker=docker, native=native, build=build))

    @engine_app.command("status")
    @initialized
    def engine_inspect(ctx: typer.Context):
        """Whether the database server answers, where it is and which version it runs. Starts nothing."""
        from cairnmind.storage.engine import engine_status
        result = engine_status(ctx.obj)
        _io.output(result)
        if result.get("problem") or result.get("available") is None:
            # Connect-only (#68): a server of a version this release does not support is refused, not only shown,
            # and so is a configuration that breaks a rule of #72, with which the server was not asked (#242).
            raise typer.Exit(1)

    @engine_app.command("stop")
    @initialized
    def engine_stop(ctx: typer.Context, as_json: JSON_FLAG = False):
        """Stop the database engine; a Docker engine's container is removed, the data stays, and `engine start` runs it again."""
        from cairnmind.storage.database_mode import connect_only
        from cairnmind.storage.engine import local_process_recorded, stop_engine
        from cairnmind.storage.engine_docker import container_recorded
        # A connect-only installation without an engine of its own has nothing to stop, whether or not its
        # service runs: it says so, instead of asking to stop the service first (#68).
        nothing_here = (connect_only(ctx.obj) and not local_process_recorded(ctx.obj)
                        and not container_recorded(ctx.obj))
        if not nothing_here and _io.service_alive(ctx.obj):
            _io.refuse("Run cairnmind stop first; the knowledge service is using ArcadeDB")
        with _io._engine_command():
            result = stop_engine(ctx.obj)

        def sentences():
            from cairnmind.cli_cmds import first_start
            if not result.get("stopping"):
                return [first_start.ENGINE_NOT_STOPPED.format(reason=str(result.get("reason") or "unknown").rstrip("."))]
            if connect_only(ctx.obj):
                return [first_start.ENGINE_STOPPED_CONNECT]
            return [first_start.ENGINE_STOPPED.format(data_dir=ctx.obj.data_dir)]
        _io._human_or_json(result, as_json, sentences)

    @db_app.command("create-user")
    @initialized
    def db_create_user(ctx: typer.Context, admin_password_file: Annotated[Path, typer.Option(
            "--admin-password-file", help="A private file (mode 0600) with the root password of the server.")]):
        """Create the database and the user arcade_user with rights on it only, signing in once as root.

        The new user's password is the one configured for it: CAIRNMIND_ARCADE_PASSWORD or arcade_password_file.
        """
        from cairnmind.storage.database_credentials import admin_password, create_user
        admin = admin_password(str(admin_password_file.expanduser().absolute()))
        _io.output(create_user(ctx.obj, admin))

    @store_app.command("size")
    def store_size(ctx: typer.Context, project: Annotated[str | None, typer.Argument(
                       metavar="PROJECT",
                       help="Measure only this project's rows; the disk use is always the whole data directory.")] = None,
                   as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False):
        """Rows and approximate bytes per collection, kind and namespace, and the disk use of the data directory.

        Read-only: it never starts the engine or the service and changes nothing. Prints no record or source text."""
        from cairnmind.storage.store_size import measure
        store = _io.store_for_read(ctx.obj)
        report = measure(store, _io.resolve_project(store, project)["id"] if project is not None else None)
        if as_json:
            _io.output(report)
        else:
            typer.echo(report["text"])

    @store_app.command("writer")
    @initialized
    def store_writer_show(ctx: typer.Context):
        """Which installation writes this store, and whether this one is it. Starts no engine; writes no row."""
        from cairnmind.storage.store_writer import inspect
        _io.output(inspect(ctx.obj))

    @store_app.command("takeover")
    @initialized
    def store_takeover(ctx: typer.Context,
                       yes: Annotated[bool, typer.Option("--yes", help="Take over writing; without it nothing changes.")] = False,
                       note: Annotated[str | None, typer.Option(help="Why, kept with the change (one line).")] = None):
        """Make this installation the store's writer.

        The previous writer stops at its next write; its running jobs go back to the queue. The change, with its time, host and note, is kept in the store.
        """
        from cairnmind.storage.store_writer import inspect, take_over_at, valid_note
        # Moving the writer is a person's decision, as a verdict is: a coding session cannot make it.
        if _io.session_value("SESSION_ID"):
            _io.refuse("A coding session cannot take over writing the store; only a person can, outside the session.")
        try:
            valid_note(note)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--note") from None
        if not yes:
            current = inspect(ctx.obj)
            _io.output({"taken_over": False, **current, "next": TAKEOVER_NEXT.get(current.get("role"), TAKEOVER_NEXT[None])})
            raise typer.Exit(1)
        _io.output(take_over_at(ctx.obj.data_dir, note))

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
        from cairnmind.storage.replaced_chunks import drop_replaced_chunks
        _io.output(drop_replaced_chunks(_io.store_for(ctx.obj), batch=batch, max_batches=max_batches))

    @store_app.command("prune-backups")
    def store_prune_backups(
            ctx: typer.Context,
            keep: Annotated[int, typer.Option(min=0, max=1000, help="Safety backups of each kind to keep, newest first.")],
            yes: Annotated[bool, typer.Option("--yes", help="Delete without asking again.")] = False):
        """Delete old safety backups (before-schema-*, before-restore-*), keeping the newest K of each kind.

        Shows what it would delete and asks first. Only a person in their own terminal can run it; scheduled backups and backups you named are never touched. Starts no engine (#190): a before-restore-* copy is deleted only while the engine answers (`cairnmind engine start`), because the mark of an unfinished restore is read from it (#362).
        """
        from cairnmind.memory.confirmation import person_at_terminal
        from cairnmind.storage.store_prune import delete_backups, prune_plan
        directory = ctx.obj.data_dir / "backups"
        plan = prune_plan(directory, keep)
        if not person_at_terminal():
            _io.refuse("Only a person can delete safety backups: run this command in your own terminal, outside a "
                   "coding session, with input and output not piped or redirected. Nothing was deleted.")
        said = {}
        if any(entry["kind"] == "before_restore" for entry in plan["delete"]):
            # A copy that the mark of an unfinished restore names is the only way back, however old it is (#362).
            # shortcut: no writer lock, so with --keep 0 a restore that has just written its copy but not yet its mark can
            # lose it here (it then fails closed as NO_UNDO); take database-writer.lock here if that ever matters.
            from cairnmind.pal.terminal_safe import safe_text
            from cairnmind.storage.store_prune import (
                KEPT_FOR_A_RESTORE,
                KEPT_FOR_AN_UNKNOWN_MARK,
                keep_what_a_restore_names,
                named_by_a_restore,
            )
            from cairnmind.terminal.terminal_i18n import tr
            named = named_by_a_restore(ctx.obj)
            plan, held = keep_what_a_restore_names(plan, named)
            if held:
                text = KEPT_FOR_AN_UNKNOWN_MARK if isinstance(named, str) else KEPT_FOR_A_RESTORE
                values = {"names": ", ".join(safe_text(name) for name in held), "reason": safe_text(str(named))}
                said = {"kept_for_an_unfinished_restore": held, "note": tr(text, "en", **values),
                        "note_pl": tr(text, "pl", **values)}
        shown = {"directory": plan["directory"], "keep": keep, "delete_bytes": plan["delete_bytes"],
                 "delete": [{key: entry[key] for key in ("name", "kind", "bytes", "modified")} for entry in plan["delete"]],
                 "kept": [entry["name"] for entry in plan["kept"]], **said}
        if not plan["delete"]:
            _io.output({**shown, "deleted": []})
            return
        _io.output(shown)
        if not yes and not typer.confirm(f"Delete these {len(plan['delete'])} safety backup(s)?", default=False):
            _io.output({"deleted": [], "note": "Nothing was deleted."})
            raise typer.Exit(1)
        _io.output(delete_backups(directory, plan))

    @app.command("rebuild-vectors")
    @initialized
    def rebuild_vectors(ctx: typer.Context, project: ProjectArg):
        """Rebuild the project's derived vectors, keeping sources and history."""
        import asyncio

        from cairnmind.memory.memory_models import from_settings
        from cairnmind.service import schedule_embeddings
        settings = ctx.obj
        from cairnmind.agents.knowledge_mode import needs_models
        why = needs_models(settings, "cairnmind rebuild-vectors")
        if why:
            _io.refuse(why)  # no embedding model is configured; nothing is deleted or asked (#274)
        store = _io.store_for(settings)
        pid = _io.resolve_project(store, project)["id"]
        from cairnmind.memory import memory_fingerprint

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
        _io.output(result)

    @app.command()
    @initialized
    def export(ctx: typer.Context, destination: ExportArg, project: str | None = None, markdown: bool = False):
        """Write the records and sources, of one project or all, as JSON or Markdown for reading. Not a backup."""
        store = _io.store_for(ctx.obj, notice=False)  # it only reads, so a reader is not shown the refusal-like notice
        pid = _io.resolve_project(store, project)["id"] if project else None
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
        _io.output({"export": str(destination.resolve())})

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
        _io.output({"backup": str(_io.store_for(ctx.obj, notice=False).backup(target))})

    @app.command()
    @initialized
    def restore(ctx: typer.Context, source: Annotated[Path, typer.Argument(
            metavar="SOURCE", exists=True, dir_okay=False, readable=True,
            help="A backup file written by `cairnmind backup`.")]):
        """Replace the store with a backup, after a safety backup of the current one. The service must be stopped.

        A person's confirmations in the backup are kept only while the workspace is open; otherwise each becomes the backup's claim and accepted record kinds are withdrawn."""
        # A missing path or a folder is refused while the arguments are read, before the store or engine is touched.
        with _io._service_stopped(ctx.obj, "Run cairnmind stop first; the service still holds the store lock",
                              "Run cairnmind stop first; restoring requires a stopped service"):
            from cairnmind.storage.restore_claims import checked_restore
            # The proof is read once, before anything changes; only "key" keeps a person's acts (#214).
            outcome = checked_restore(ctx.obj, source, lambda: _io.store_for(ctx.obj, for_restore=True))
        for key in ("notice", "vectors_notice"):
            notice = outcome.pop(key, None)
            if notice:
                typer.echo(f"Cairnmind: {notice[0].format(**notice[1])}", err=True)
        # A restored folder keeps the machine that registered it: on another machine each is named, with its command.
        from cairnmind.memory import project_folders
        away = project_folders.unavailable(_io.store_for(ctx.obj, notice=False))
        for item in away:
            if item["state"] == "elsewhere":
                typer.echo(f"Cairnmind: {item['message']}", err=True)
        _io.output({"restored": True, **outcome, **({"unavailable_folders": away} if away else {})})

    @app.command()
    def forget(ctx: typer.Context, project: ProjectArg,
               delete_project: Annotated[bool, typer.Option(
                   "--delete-project", help="Kept so that a script gets the refusal; nothing is deleted.")] = False):
        """Refused since 0.13.0: a person deletes a project's memory in the workspace (More, then Delete project memory)."""
        from cairnmind.memory.project_forget import CLI_REFUSED
        # A person's act needs the open workspace's key and its consent dialog (#347): no settings, store or engine here.
        _io.refuse(CLI_REFUSED)

    @app.command()
    @initialized
    def benchmark(ctx: typer.Context, project: ProjectArg, question: QuestionArg):
        """Time the first answer and the cached repeat; this does not replace a quality evaluation."""
        runs = []
        for label in ("first", "cached"):
            started = _io.time.perf_counter()
            result = _io.request(ctx.obj, "POST", "/ask", json={"project": project, "question": question})
            runs.append({"run": label, "seconds": round(_io.time.perf_counter() - started, 3),
                         "status": result["status"], "cached": result.get("cached"),
                         "sources": len(result.get("sources", []))})
        try:
            gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=5, check=False).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            gpu = "nvidia-smi unavailable"
        _io.output({"runs": runs, "gpu": gpu})

    @autostart_app.command("status")
    def service_autostart_status(ctx: typer.Context):
        """Show whether per-user logon autostart is registered for this data directory."""
        from cairnmind.pal.service_autostart import status as autostart_status
        _io.output(autostart_status(ctx.obj))

    @autostart_app.command("install")
    def service_autostart_install(ctx: typer.Context):
        """Register per-user Windows Task Scheduler autostart (LogonTrigger + RestartOnFailure)."""
        from cairnmind.pal.service_autostart import install as autostart_install
        try:
            _io.output(autostart_install(ctx.obj))
        except ValueError as exc:
            _io.refuse(str(exc))

    @autostart_app.command("remove")
    def service_autostart_remove(ctx: typer.Context):
        """Remove per-user Windows Task Scheduler autostart for this data directory."""
        from cairnmind.pal.service_autostart import remove as autostart_remove
        try:
            _io.output(autostart_remove(ctx.obj))
        except ValueError as exc:
            _io.refuse(str(exc))

    engine_app.command("up", help="The same as `cairnmind engine start`.")(engine_start)
