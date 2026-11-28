"""CLI commands for sessions, client installation/running, hooks, and MCP server/presets/probe."""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer

from cairnmind.cli_cmds import io as _io
from cairnmind.cli_cmds.cli_arguments import (
    ClientArg,
    PacketHashArg,
    PacketIdArg,
    ProjectArg,
    ReadbackArg,
    SessionClientArg,
    SessionIdArg,
)
from cairnmind.cli_cmds.io import JSON_FLAG, initialized
from cairnmind.config import Settings

_log = logging.getLogger(__name__)


def register(apps: dict[str, typer.Typer]) -> None:
    app = apps["app"]
    sessions_app = apps["sessions_app"]
    mcp_app = apps["mcp_app"]

    @sessions_app.command("prepare")
    @initialized
    def session_prepare(ctx: typer.Context, project: ProjectArg, client: SessionClientArg, worktree: Path | None = None):
        result = _io.request(ctx.obj, "POST", "/sessions/prepare", json={"project": project, "client": client,
                         "worktree": str(worktree.resolve()) if worktree else None})
        _io.output(result)
        if result["status"] != "ready":
            raise typer.Exit(2)

    @sessions_app.command("show")
    @initialized
    def session_show(ctx: typer.Context, session_id: SessionIdArg):
        _io.output(_io.request(ctx.obj, "GET", f"/sessions/{session_id}"))

    @sessions_app.command("context")
    @initialized
    def session_context(ctx: typer.Context, session_id: SessionIdArg):
        _io.output(_io.request(ctx.obj, "POST", "/sessions/context", json={"session_id": session_id}))

    @sessions_app.command("ack")
    @initialized
    def session_ack(ctx: typer.Context, session_id: SessionIdArg, packet_id: PacketIdArg, packet_hash: PacketHashArg, readback_json: ReadbackArg):
        actor = _io.session_value("SESSION_ID")
        if actor and actor != session_id:
            raise typer.BadParameter("Another session cannot be acknowledged")
        _io.output(_io.request(ctx.obj, "POST", "/sessions/ack", json={"session_id": session_id,
            "packet_id": packet_id, "packet_hash": packet_hash,
            "readback": _io._json_file(readback_json)}))

    @app.command()
    @initialized
    def install(ctx: typer.Context, project: ProjectArg, as_json: JSON_FLAG = False,
                recorder: Annotated[bool, typer.Option("--recorder", help="Turn the Claude Code cairnmind-recorder "
                                                       "subagent on again for the project and write it.")] = False,
                no_recorder: Annotated[bool, typer.Option("--no-recorder", help="Remove the cairnmind-recorder subagent "
                                                          "file Cairnmind wrote and keep it off for the project.")] = False,
                pre_commit: Annotated[bool, typer.Option("--pre-commit", help="Add the cairnmind-memory-audit hook to "
                                                         ".pre-commit-config.yaml in the project root.")] = False,
                mcp_client: Annotated[list[str] | None, typer.Option(
                    "--mcp-client",
                    help="Also generate and safely merge an MCP preset for this client (repeatable, e.g. cursor, vscode, zed).",
                )] = None):
        """Add the project's MCP server, hooks and instructions, keeping the existing configuration."""
        from cairnmind.memory.integrations import install_integrations
        if recorder and no_recorder:
            raise typer.BadParameter("Choose one of --recorder and --no-recorder.")
        store = _io.store_for(ctx.obj)
        selected = _io.resolve_project(store, project)
        result = install_integrations(ctx.obj, selected, recorder=True if recorder else False if no_recorder else None,
                                      pre_commit=pre_commit)
        import shlex

        from cairnmind.cli_cmds import first_start
        from cairnmind.pal.terminal_safe import safe_text
        root = Path(selected["root"]).resolve()
        if mcp_client:
            from cairnmind.mcp.mcp_presets import generate_preset
            presets_out = []
            for client_name in mcp_client:
                preset = generate_preset(client_name, ctx.obj, root, write=True)
                presets_out.append(preset)
                if preset.get("written") and preset.get("path"):
                    result["changed"].append(preset["path"])
                result["warnings"].extend(preset.get("warnings") or [])
            result["mcp_presets"] = presets_out
        files = ", ".join(safe_text(Path(os.path.relpath(path, root)).as_posix()) for path in result["changed"]) or first_start.NOTHING_WRITTEN
        from cairnmind.memory.integrations import missing_parts
        installed = {client: info for client, info in result["clients"].items() if info.get("installed")}
        # Connected is the rule a verified launch uses; an installed client that fails it is named with what it lacks.
        connected = [client for client, info in installed.items() if not missing_parts(info)]
        unconnected = ", ".join(f"{first_start.CLIENT_NAMES.get(client, client)} (not in place: "
                                f"{', '.join(missing_parts(info))})" for client, info in installed.items()
                                if missing_parts(info))
        name = shlex.quote(safe_text(selected["name"]))
        if connected:
            lines = [first_start.INSTALL_DONE.format(
                clients=", ".join(first_start.CLIENT_NAMES.get(str(client), str(client)) for client in connected), files=files,
                project=name, command=connected[0])]
            if unconnected:
                lines.append(first_start.INSTALL_ALSO.format(clients=unconnected))
        elif unconnected:
            lines = [first_start.INSTALL_NOT_CONNECTED.format(files=files, clients=unconnected)]
        else:
            lines = [first_start.INSTALL_NO_CLIENT.format(project=name)]
        # With no client installed the warnings are the two "not installed" lines, which INSTALL_NO_CLIENT says once.
        skipped = {first_start.CLIENT_SKIPPED.format(client=client) for client in result["clients"]}
        notes = [warning for warning in result["warnings"] if installed or warning not in skipped]
        _io._human_or_json(result, as_json, lambda: lines + notes)

    @app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
    @initialized
    def run(ctx: typer.Context, project: ProjectArg, client: ClientArg, worktree: Path | None = None):
        """Start Codex or Claude Code in the project, bound to its memory. Arguments after -- go to the client."""
        from cairnmind.memory.integrations import run_client
        _io._refuse_during_upgrade()
        _io._ensure_service_or_refuse(ctx.obj)
        selected = _io.resolve_project(_io.store_for(ctx.obj), project)
        if worktree:
            store = _io.store_for(ctx.obj)
            root = str(worktree.expanduser().resolve())
            from cairnmind.storage.storage_query import db_find_one
            if not db_find_one(store, "project_roots", {"project_id": selected["id"], "path": root}, columns=["id"]):
                from cairnmind.storage.sql import unregistered_root
                _io.refuse(unregistered_root(root, selected))
            selected = {**selected, "root": root}
        args = list(ctx.args)
        if args[:1] == ["--"]:
            args = args[1:]
        try:
            code = run_client(ctx.obj, selected, client, args)
        except ValueError as exc:
            from cairnmind.memory.integrations import CLIENTS
            if client not in CLIENTS or shutil.which(client) is not None:
                raise
            # The client is not here: where to get it, and what works without one (the workspace words this its own way).
            from cairnmind.cli_cmds.first_start import RUN_NO_CLIENT
            urls = {"codex": "https://developers.openai.com/codex/cli", "claude": "https://code.claude.com/docs/en/setup"}
            raise ValueError(f"{str(exc).rstrip('.')}. {RUN_NO_CLIENT.format(url=urls.get(client, 'its own instructions'))}") from None
        raise typer.Exit(code)

    def _resolve_mcp_project_root(settings: Settings, project: str | None) -> Path:
        """Resolve `--project` to an absolute folder path without starting any engine or service."""
        if project is None:
            return Path.cwd().resolve()
        candidate = Path(project).expanduser()
        if candidate.exists() and candidate.is_dir():
            return candidate.resolve()
        # Try resolving a registered project name/id read-only when the store is available.
        try:
            selected = _io.resolve_project(_io.store_for_read(settings), project)
            return Path(selected["root"]).expanduser().resolve()
        except Exception as exc:  # noqa: BLE001 - best-effort read-only lookup before path fallback
            _log.debug("Read-only project lookup failed for MCP root %r: %s", project, exc)
            if candidate.is_absolute() or "/" in project or "\\" in project or project.startswith((".", "~")):
                return candidate.resolve()
            raise ValueError(
                f"Could not resolve project {project!r} as an existing directory or registered project."
            ) from None

    @mcp_app.callback(invoke_without_command=True)
    def mcp(ctx: typer.Context,
            client: Annotated[str, typer.Option("--client", help=(
                "native: started by the Cairnmind launcher (Codex, Claude Code). external: any other MCP client "
                "(Cursor, Cline, Zed, ...); its session is evidence-degraded and never verified."))] = "native",
            project: Annotated[str | None, typer.Option("--project", help=(
                "With --client external: the project id, name or a path inside it (default: the working directory)."))] = None,
            worktree: Annotated[Path | None, typer.Option("--worktree", help=(
                "With --client external: the registered worktree (default: the one containing the project path)."))] = None):
        """MCP over stdio; prints no logs or CLI messages to stdout. Also hosts `generate` and `test-connection`."""
        if ctx.invoked_subcommand is not None:
            return
        if client not in {"native", "external"}:
            raise typer.BadParameter("--client must be native or external")
        if client == "native" and (project is not None or worktree is not None):
            raise typer.BadParameter("--project and --worktree are only for --client external")
        ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
        if ctx.obj._origin.get("connect_saved"):
            from cairnmind.storage.database_mode import saved_note
            typer.echo(f"Cairnmind: {saved_note(ctx.obj)}", err=True)
        from cairnmind.mcp.mcp_server import run
        if client == "native":
            run(ctx.obj)
            return
        run(ctx.obj, external={"project": project or str(Path.cwd()),
                               "worktree": str(worktree.expanduser().resolve()) if worktree is not None else None})

    @mcp_app.command("generate")
    def mcp_generate(
        ctx: typer.Context,
        client: Annotated[
            str | None,
            typer.Option(
                "--client",
                help="Target MCP client: claude-code, claude-desktop, cursor, windsurf, vscode, cline, roo-code, codex, gemini-cli, zed or generic.",
            ),
        ] = None,
        auto: Annotated[
            bool,
            typer.Option("--auto", help="Detect installed MCP clients in the system or repository."),
        ] = False,
        project: Annotated[
            str | None,
            typer.Option("--project", help="Project name, id or folder (defaults to the current directory)."),
        ] = None,
        worktree: Annotated[
            Path | None,
            typer.Option("--worktree", help="Optional worktree path passed to external MCP presets."),
        ] = None,
        scope: Annotated[
            str | None,
            typer.Option("--scope", help="Configuration scope: project or user."),
        ] = None,
        name: Annotated[
            str,
            typer.Option("--name", help="Server key name in the client config (default: cairnmind)."),
        ] = "cairnmind",
        external: Annotated[
            bool,
            typer.Option("--external", help="Generate an external (--client external) preset even for claude-code or codex project configs."),
        ] = False,
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", help="Preview the generated configuration and diff without writing files."),
        ] = False,
        stdout_only: Annotated[
            bool,
            typer.Option("--stdout", help="Print the generated configuration block to stdout without touching files."),
        ] = False,
        write: Annotated[
            bool,
            typer.Option("--write", help="Safely deep-merge the preset into the client config file with a .cairnmind.bak backup."),
        ] = False,
        force: Annotated[
            bool,
            typer.Option("--force", help="Allow replacing a user-scoped preset that currently points to another project."),
        ] = False,
        yes: Annotated[
            bool,
            typer.Option("--yes", help="Confirm writing all detected clients in --auto --write mode without prompting."),
        ] = False,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Print the full generation result as JSON."),
        ] = False,
    ):
        """Generate or safely deep-merge universal MCP client presets without ever adding auto-approval flags.

        Supports JSON, JSONC (VS Code, Zed) and TOML (Codex) with atomic replacement and `<file>.cairnmind.bak` backups. Without `--write`, previews the generated preset or diff without touching any files.
        """
        from cairnmind.mcp.mcp_presets import (
            SUPPORTED_CLIENTS,
            detect_installed_clients,
            generate_preset,
        )

        if write and (dry_run or stdout_only):
            raise typer.BadParameter("Choose either --write or --dry-run/--stdout, not both.")
        if not client and not auto:
            raise typer.BadParameter(
                f"Specify --client <name> ({', '.join(SUPPORTED_CLIENTS)}) or --auto."
            )
        if client and auto:
            raise typer.BadParameter("Choose one of --client and --auto.")
        if scope is not None and scope not in ("project", "user"):
            raise typer.BadParameter("--scope must be 'project' or 'user'.")

        project_root = _resolve_mcp_project_root(ctx.obj, project)
        do_write = bool(write and not dry_run and not stdout_only)

        if auto:
            detected = detect_installed_clients(project_root)
            if not detected:
                payload = {"project": str(project_root), "detected": [], "presets": []}
                if as_json:
                    _io.output(payload)
                else:
                    typer.echo("No supported MCP clients detected. Pass `--client <name>` explicitly (or `--client generic`).")
                return

            chosen_clients: list[str] = []
            if do_write and _io._interactive_terminal() and not yes and not as_json:
                for item in detected:
                    if typer.confirm(f"Write Cairnmind MCP preset for {item['title']} ({item['client']})?", default=True):
                        chosen_clients.append(item["client"])
            else:
                chosen_clients = [item["client"] for item in detected]

            presets = []
            for client_name in chosen_clients:
                item_scope = scope or next(d["default_scope"] for d in detected if d["client"] == client_name)
                presets.append(
                    generate_preset(
                        client_name,
                        ctx.obj,
                        project_root,
                        scope=item_scope,
                        server_name=name,
                        worktree=worktree,
                        external=external,
                        write=do_write,
                        force=force,
                    )
                )
            payload = {"project": str(project_root), "detected": detected, "presets": presets}
            if as_json:
                _io.output(payload)
            else:
                for preset in presets:
                    for warning in preset.get("warnings") or []:
                        typer.echo(f"Warning: {warning}", err=True)
                    action = "Wrote" if preset["written"] else ("Unchanged" if do_write else "Preview")
                    typer.echo(f"[{action}] {preset['client']} ({preset['scope']}): {preset['path']}")
                    if not do_write and preset.get("diff"):
                        typer.echo(preset["diff"].rstrip("\n"))
                    elif not do_write:
                        typer.echo(preset["content"].rstrip("\n"))
            return

        if client is None:
            raise typer.BadParameter(
                f"Specify --client <name> ({', '.join(SUPPORTED_CLIENTS)}) or --auto."
            )
        preset = generate_preset(
            client,
            ctx.obj,
            project_root,
            scope=scope,
            server_name=name,
            worktree=worktree,
            external=external,
            write=do_write,
            force=force,
        )
        for warning in preset.get("warnings") or []:
            typer.echo(f"Warning: {warning}", err=True)
        if as_json:
            _io.output(preset)
            return
        if stdout_only or client == "generic":
            typer.echo(preset["content"].rstrip("\n"))
            return
        if do_write:
            status_word = "Updated" if preset["written"] else "Already up to date"
            backup_note = f" (backup: {preset['backup']})" if preset.get("backup") else ""
            typer.echo(f"{status_word} {preset['client']} preset at {preset['path']}{backup_note}")
            return
        # Dry-run preview: print diff when file exists and differs, otherwise print full generated config.
        if preset.get("path") and Path(preset["path"]).exists() and preset.get("diff"):
            typer.echo(preset["diff"].rstrip("\n"))
        else:
            typer.echo(preset["content"].rstrip("\n"))

    @mcp_app.command("test-connection")
    def mcp_test_connection(
        ctx: typer.Context,
        client: Annotated[
            str | None,
            typer.Option(
                "--client",
                help="Optional client preset whose command and env should be probed (e.g. cursor, vscode, codex).",
            ),
        ] = None,
        scope: Annotated[
            str | None,
            typer.Option("--scope", help="Configuration scope when testing a specific --client preset: project or user."),
        ] = None,
        project: Annotated[
            str | None,
            typer.Option("--project", help="Project name, id or folder (defaults to the current directory)."),
        ] = None,
        worktree: Annotated[
            Path | None,
            typer.Option("--worktree", help="Optional worktree path for the external MCP server."),
        ] = None,
        name: Annotated[
            str,
            typer.Option("--name", help="Server key name when reading an existing client preset file (default: cairnmind)."),
        ] = "cairnmind",
        timeout: Annotated[
            float,
            typer.Option("--timeout", min=0.5, max=30.0, help="Hard timeout in seconds for the JSON-RPC handshake."),
        ] = 5.0,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Print the full handshake diagnostic as JSON."),
        ] = False,
    ):
        """Run an isolated stdio JSON-RPC `initialize` and `tools/list` handshake against Cairnmind's MCP server.

        Fails hard if any non-JSON-RPC byte is emitted on stdout or if the handshake exceeds `--timeout`.
        """
        import tomllib

        from cairnmind.mcp.mcp_presets import (
            CLIENT_SPECS,
            build_server_entry,
            config_path_for_client,
            parse_json_or_jsonc,
        )
        from cairnmind.mcp.mcp_probe import probe_mcp_stdio

        project_root = _resolve_mcp_project_root(ctx.obj, project)
        command = ""
        args: list[str] = []
        env: dict[str, str] | None = None
        source_label = "generated"

        if client is not None:
            if client not in CLIENT_SPECS:
                raise typer.BadParameter(f"Unsupported MCP client: {client!r}")
            spec = CLIENT_SPECS[client]
            resolved_scope = scope or spec.default_scope
            path = config_path_for_client(client, resolved_scope, project_root)
            loaded_entry = None
            if path is not None and path.is_file():
                raw = path.read_text(encoding="utf-8")
                doc = tomllib.loads(raw) if spec.file_format == "toml" else parse_json_or_jsonc(
                    raw, path, allow_jsonc=(spec.file_format == "jsonc")
                )
                container = doc.get(spec.root_key)
                if isinstance(container, dict) and isinstance(container.get(name), dict):
                    loaded_entry = container[name]
                    source_label = str(path)
            if loaded_entry is None:
                loaded_entry, _ = build_server_entry(
                    client, ctx.obj, project_root, scope=resolved_scope, worktree=worktree
                )
            cmd_field = loaded_entry.get("command")
            if isinstance(cmd_field, dict):
                command = str(cmd_field.get("path", ""))
                args = [str(a) for a in (cmd_field.get("args") or [])]
                env = cmd_field.get("env") if isinstance(cmd_field.get("env"), dict) else None
            else:
                command = str(cmd_field or "")
                args = [str(a) for a in (loaded_entry.get("args") or [])]
                env = loaded_entry.get("env") if isinstance(loaded_entry.get("env"), dict) else None
        else:
            entry, _ = build_server_entry(
                "generic", ctx.obj, project_root, scope="stdout", worktree=worktree, external=True
            )
            command = entry["command"]
            args = entry["args"]
            env = entry.get("env")

        result = probe_mcp_stdio(
            command,
            args,
            env=env,
            cwd=project_root,
            timeout=timeout,
            settings=ctx.obj,
            project_root=project_root,
        )
        result["preset_source"] = source_label
        if as_json:
            _io.output(result)
        else:
            if result["ok"]:
                typer.echo(
                    f"MCP Handshake: OK ({result['latency_ms']} ms, stdio clean, "
                    f"{result['tools_count']} tools, backend={result['backend']}:{result['backend_mode']})"
                )
            else:
                typer.echo(f"MCP Handshake: FAILED ({result['latency_ms']} ms): {result['error']}")
        if not result["ok"]:
            raise typer.Exit(1)

    @app.command()
    @initialized
    def hook(ctx: typer.Context, client: Annotated[str, typer.Option("--client")]):
        """Handle one hook event of a coding client, read from standard input. Run by the client, not by hand."""
        from cairnmind.memory.integrations import hook as handle_hook
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            result = handle_hook(ctx.obj, payload, client)
            if result:
                typer.echo(json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            # Never corrupt the coding client's protocol or block a turn on memory failures.
            typer.echo(f"Cairnmind hook: {exc}", err=True)
