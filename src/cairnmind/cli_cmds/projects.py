"""CLI commands for projects, indexing, asking, brief, impact, scan, usage, records, remember, jobs."""
from __future__ import annotations

import sys
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from cairnmind.cli_cmds import io as _io
from cairnmind.cli_cmds.cli_arguments import (
    FolderArg,
    JobIdArg,
    OptionalProjectNameArg,
    OptionalProjectRootArg,
    ProjectArg,
    QuestionArg,
    RecordIdArg,
    SourceIdArg,
    StateFileArg,
    TranscriptArg,
)
from cairnmind.cli_cmds.io import JSON_FLAG, initialized
from cairnmind.config import Settings
from cairnmind.storage.arcade import ArcadeError

_CLONE_OPT = Annotated[str | None, typer.Option(
    "--clone", help="Clone a Git repository (owner/repo, HTTPS or SSH URL) before registering.")]


_VIA_OPT = Annotated[str, typer.Option(
    "--via", help="Clone tool used with --clone: auto (default), gh or git.")]


_BRANCH_OPT = Annotated[str | None, typer.Option(
    "--branch", "-b", help="Branch or tag to check out when cloning.")]


_FRESH_OPT = Annotated[bool, typer.Option(
    "--fresh", help="When cloning a repository that contains .memory/cairnmind/, register a new project identity anyway.")]


_NO_SCIP_OPT = Annotated[
    bool,
    typer.Option("--no-scip", help="Skip enqueueing automatic background SCIP indexing for detected languages."),
]


class UsageBy(StrEnum):
    day = "day"
    week = "week"


def register(apps: dict[str, typer.Typer]) -> None:
    app = apps["app"]
    projects_app = apps["projects_app"]
    jobs_app = apps["jobs_app"]

    def _run_project_add(ctx: typer.Context, name: str | None, root: str | None, clone: str | None,
                         via: str, branch: str | None, fresh: bool, as_json: bool, no_scip: bool = False) -> None:
        import shlex

        from cairnmind.cli_cmds import first_start
        from cairnmind.memory import project_clone
        from cairnmind.memory.brief import _git_out
        from cairnmind.pal.process_control import quote_arg
        from cairnmind.pal.terminal_safe import safe_text

        clone_spec = clone
        dest_arg: str | None = root
        chosen_name: str | None = name
        if clone_spec is None:
            if project_clone.is_explicit_remote_url(name):
                clone_spec = name
                chosen_name = None
                dest_arg = root
            elif project_clone.is_explicit_remote_url(root):
                clone_spec = root
                dest_arg = None

        clone_info = None
        found_memory = {"memory_snapshot": None, "published_memory": None}
        if clone_spec is None:
            if (via or "auto").strip().lower() != "auto":
                raise typer.BadParameter(project_clone.CLONE_ONLY_OPTION.format(option="--via"), param_hint="--via")
            if branch is not None:
                raise typer.BadParameter(project_clone.CLONE_ONLY_OPTION.format(option="--branch"), param_hint="--branch")
            if fresh:
                raise typer.BadParameter(project_clone.CLONE_ONLY_OPTION.format(option="--fresh"), param_hint="--fresh")
            if chosen_name is None:
                raise typer.BadParameter("Missing argument 'NAME'.", param_hint="NAME")
            if dest_arg is None:
                raise typer.BadParameter("Missing argument 'ROOT'.", param_hint="ROOT")
            root_path = Path(dest_arg)
            try:
                resolved_root = root_path.expanduser().resolve(strict=True)  # the folder is checked before the store is opened
            except FileNotFoundError as exc:
                if getattr(exc, "winerror", None) == 206:
                    raise
                given = safe_text(str(root_path.expanduser().absolute()))
                raise ValueError(first_start.PROJECT_FOLDER_MISSING.format(root=given, quoted=quote_arg(given))) from None
            if not resolved_root.is_dir():
                raise typer.BadParameter("The path must be a folder, not a file")
            store = _io.store_for(ctx.obj)
        else:
            source = project_clone.parse_clone_source(clone_spec)
            chosen_branch = project_clone.validate_branch(branch) if branch is not None else None
            chosen_name = source.default_name if chosen_name is None else chosen_name
            raw_dest = Path(dest_arg) if dest_arg is not None else Path.cwd() / source.default_name
            dest = project_clone.prepare_destination(raw_dest, ctx.obj.data_dir)
            project_clone.choose_tool(source, via)
            store = _io.store_for(ctx.obj)
            chosen_name = project_clone.check_store_preflight(store, chosen_name, dest)
            interactive = not as_json and _io._stdout_is_terminal() and sys.stdin.isatty() and sys.stderr.isatty()
            clone_info = project_clone.clone_repository(
                source, dest, via=via, branch=chosen_branch, interactive=interactive)
            resolved_root = dest.resolve(strict=True)
            found_memory = project_clone.inspect_cloned_memory(resolved_root)
            if found_memory["memory_snapshot"] and not fresh:
                snap = found_memory["memory_snapshot"]
                shown_root = safe_text(str(resolved_root))
                shown_snap = safe_text(snap)
                next_step = first_start.CLONE_SNAPSHOT_NEXT.format(
                    snapshot=quote_arg(shown_snap), root=quote_arg(shown_root),
                    project=quote_arg(safe_text(chosen_name)))
                payload = {"status": "snapshot_found", "cloned": clone_info, "memory_snapshot": snap,
                           "registered": False, "next": next_step}
                _io._human_or_json(payload, as_json, lambda: [
                    first_start.PROJECT_CLONED.format(source=safe_text(source.raw), root=shown_root,
                                                      tool=clone_info["tool"]),
                    first_start.CLONE_SNAPSHOT_FOUND.format(snapshot=shown_snap),
                    next_step,
                ])
                raise typer.Exit(1)

        from cairnmind.memory import project_removal as removal
        known = {row["id"] for row in store.list_projects()}
        removed = removal.removed_rows(store)
        project = store.add_project(chosen_name, resolved_root, restore=False) if fresh else store.add_project(
            chosen_name, resolved_root)
        from cairnmind.scip.ingest import index_project, register_default_histories
        result = index_project(store, project["id"])
        discovery = register_default_histories(store, project["id"], Path(project["root"]))
        if no_scip:
            scip_info = {"mode": "skipped", "detected_languages": [], "queued": False, "job_id": None}
        else:
            from cairnmind.scip.scip_ingest import enqueue_project_scip
            scip_info = enqueue_project_scip(store, ctx.obj, project["id"], resolved_root)
        # Said, never silent (#327): whether a removed project came back, or one that keeps its memory did not.
        restored, kept, details = removal.add_outcome(removed, known, project)
        quoted = shlex.quote(safe_text(chosen_name))
        jobs, waiting_line = _jobs_after_add(ctx.obj, store, project["id"], quoted)
        published = found_memory.get("published_memory")

        def sentences():
            shown = safe_text(str(resolved_root))
            existed = project["id"] in known
            lines = []
            if clone_info is not None:
                lines.append(first_start.PROJECT_CLONED.format(
                    source=safe_text(clone_info["source"]), root=shown, tool=clone_info["tool"]))
            lines.extend(
                removal.restored_lines(project, restored, result.get("files", 0)) if restored else
                [folder["message"] for folder in result["unavailable"]] if result.get("unavailable") else
                [(first_start.PROJECT_EXISTED if existed else first_start.PROJECT_ADDED).format(
                    project=quoted, root=shown, files=result.get("files", 0))])
            if _git_out(resolved_root, "rev-parse", "--is-inside-work-tree") is None:
                lines.append(first_start.NOT_GIT.format(root=shown))
            elif _git_out(resolved_root, "rev-parse", "--verify", "-q", "HEAD") is None:
                lines.append(first_start.NO_COMMIT.format(root=shown))
            if scip_info.get("queued"):
                langs = ", ".join(scip_info["detected_languages"])
                lines.append(f"Queued background SCIP indexing for: {langs}.")
            if published:
                shown_pub = safe_text(published)
                lines.append(first_start.CLONE_PUBLISHED_FOUND.format(
                    published=shown_pub, quoted_published=quote_arg(shown_pub), project=quoted))
            lines.extend(removal.not_brought_back(row) for row in kept)
            lines.extend([waiting_line] if waiting_line else [])
            # The warning comes before the next step, which stays last (#326).
            lines.append((first_start.PROJECT_EXISTED_NEXT if existed or restored else first_start.PROJECT_ADDED_NEXT)
                         .format(project=quoted))
            return lines

        scip_extra = {"scip": scip_info} if (scip_info.get("queued") or scip_info.get("detected_languages")) else {}
        payload = {"project": project, "indexed": result, "history": discovery, "jobs": jobs, **scip_extra, **details}
        if clone_info is not None:
            payload["cloned"] = {**clone_info, **({"published_memory": published} if published else {})}
        _io._human_or_json(payload, as_json, sentences)
        if result.get("unavailable"):  # a folder of another machine was not indexed: said above, and the exit code says so
            raise typer.Exit(1)

    @app.command("add")
    @initialized
    def add_project_shortcut(ctx: typer.Context, name: OptionalProjectNameArg = None, root: OptionalProjectRootArg = None,
                             clone: _CLONE_OPT = None, via: _VIA_OPT = "auto", branch: _BRANCH_OPT = None,
                             fresh: _FRESH_OPT = False, as_json: JSON_FLAG = False, no_scip: _NO_SCIP_OPT = False):
        """Register a project folder, or clone a Git repository and register it (shortcut for `project add`)."""
        _run_project_add(ctx, name, root, clone, via, branch, fresh, as_json, no_scip=no_scip)

    @projects_app.command("add")
    @initialized
    def project_add(ctx: typer.Context, name: OptionalProjectNameArg = None, root: OptionalProjectRootArg = None,
                    clone: _CLONE_OPT = None, via: _VIA_OPT = "auto", branch: _BRANCH_OPT = None,
                    fresh: _FRESH_OPT = False, as_json: JSON_FLAG = False, no_scip: _NO_SCIP_OPT = False):
        """Register a project: its name and the folder of its files, which are indexed. With --clone (or an HTTPS/SSH Git URL) clones the repository first with gh or git."""
        _run_project_add(ctx, name, root, clone, via, branch, fresh, as_json, no_scip=no_scip)

    def _jobs_after_add(settings: Settings, store, project_id: str, quoted: str) -> tuple[dict, str | None]:
        """The extraction and embedding jobs of a project add are run by the service. project add indexes in its own
        process, as remember does, and starts nothing: only `index` goes through the service (#366). So it says how many
        jobs are queued, whether the service runs, and why they wait: no models (#274), an upgrade mark that refuses a
        start (#298, #339), or a service that was stopped. waiting_because None means not known to wait: a running service
        started by the workspace, or before models were turned on, can still hold them."""
        from cairnmind.agents import knowledge_mode
        from cairnmind.cli_cmds import first_start, upgrade_mark
        summary = store.job_summary(project_id)
        count = sum(summary[group][state] for group in ("indexing", "extraction") for state in ("pending", "running"))
        running = _io.service_alive(settings)
        jobs = {"queued": count, "service_running": running, "waiting_because": None}
        if not count:
            return jobs, None
        try:
            models_on = knowledge_mode.enabled(settings)
        except ValueError as invalid:  # a hand-edited config: the add is done, so it still reports, with the reason
            jobs["waiting_because"] = "models_not_configured"
            return jobs, str(invalid)
        if not models_on:
            jobs["waiting_because"] = "models_not_configured"  # held even beside a running service
            return jobs, first_start.JOBS_HELD_NO_MODELS.format(count=count)
        if running:
            return jobs, None
        from cairnmind.terminal import setup_mark
        reason, cause = upgrade_mark.refusal(), "upgrade_in_progress"
        if not reason:  # a change of setup from the workspace holds the data directory's start the same way (#339)
            reason, cause = setup_mark.refusal(settings.data_dir), "setup_in_progress"
        if reason:
            jobs["waiting_because"] = cause
            return jobs, first_start.JOBS_HELD_UPGRADE.format(count=count, reason=reason)
        jobs["waiting_because"] = "service_stopped"
        return jobs, first_start.JOBS_HELD_STOPPED.format(count=count, project=quoted)

    @projects_app.command("list")
    def project_list(ctx: typer.Context, as_json: JSON_FLAG = False,
                     removed: Annotated[bool, typer.Option(
                         "--removed", help="List the removed projects, whose memory is kept.")] = False):
        """List the registered projects with their folders."""
        from cairnmind.memory import project_removal as removal
        store = _io.store_for_read(ctx.obj)
        if removed:
            rows = removal.removed_view(store)
            _io._human_or_json(rows, as_json, lambda: removal.removed_sentences(rows))
            return
        projects = store.list_projects()

        def sentences():
            from cairnmind.cli_cmds import first_start
            hint = removal.removed_rows(store)
            if not projects:
                return [*([removal.REMOVED_HINT.format(count=len(hint))] if hint else []), first_start.PROJECTS_NONE]
            return [first_start.PROJECTS_HEADING, *(f"  - {row['name']}: {row['root']}" for row in projects),
                    *([removal.REMOVED_HINT.format(count=len(hint))] if hint else []), first_start.PROJECTS_NEXT]
        _io._human_or_json(projects, as_json, sentences)

    @projects_app.command("remove")
    @initialized
    def project_remove(ctx: typer.Context, project: ProjectArg,
                       root: Annotated[Path | None, typer.Option(
                           "--root", help="Remove only this extra folder of the project.")] = None,
                       yes: Annotated[bool, typer.Option(
                           "--yes", help="Make the change; without it nothing changes.")] = False,
                       as_json: JSON_FLAG = False):
        """Remove a project, or one extra folder of it, from the registry. Its memory is kept and nothing is deleted.

        Without --yes it only says what would stop, what stays and where, and how to bring the project back.
        """
        from cairnmind.memory import project_removal as removal
        from cairnmind.storage.store_writer import require_writer
        # A session must not unregister projects: a person does, as with takeover and setup.
        if _io.session_value("SESSION_ID"):
            _io.refuse(removal.IN_SESSION)
        store = _io.store_for(ctx.obj)
        require_writer(store)
        row = _io.resolve_project(store, project)
        try:
            removal.require_idle(ctx.obj, store, row)
            result = removal.plan(ctx.obj, store, row, root.expanduser().resolve() if root else None)
            if yes:
                result = removal.apply(store, result)
        except removal.Refused as exc:
            _io.refuse(str(exc))
        _io._human_or_json(result, as_json, lambda: removal.sentences(result))
        if not yes:
            raise typer.Exit(1)

    @projects_app.command("root")
    @initialized
    def project_root(ctx: typer.Context, project: ProjectArg, path: FolderArg):
        """Explicitly assign an additional folder or worktree to the project."""
        store = _io.store_for(ctx.obj)
        pid = _io.resolve_project(store, project)["id"]
        root = path.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise typer.BadParameter("The path must be a folder, not a file")
        moved = store.add_project_root(pid, root)  # a folder another machine had is now this machine's (#371)
        _io.output({"project": pid, "root": str(root), **moved})

    @app.command()
    @initialized
    def index(ctx: typer.Context, project: ProjectArg):
        """Index the project's files again through the service, which it starts when needed."""
        result = _io.request(ctx.obj, "POST", "/ingest", json={"project": project})
        _io.output(result)
        # A folder that is not on this machine is not an index that worked: named, and the exit code says so (#371).
        if isinstance(result, dict) and result.get("unavailable"):
            for folder in result["unavailable"]:
                typer.echo(f"Cairnmind: {folder['message']}", err=True)
            raise typer.Exit(1)

    @app.command("import")
    @initialized
    def import_command(
        ctx: typer.Context,
        path: TranscriptArg,
        project: str | None = None,
        client: str = "auto",
        via: Annotated[
            str | None,
            typer.Option("--via", help="Registered external transcript converter name; runs outside any coding session."),
        ] = None,
    ):
        """Import a client's JSONL; with --via, run a registered converter and archive as degraded history."""
        if via is not None:
            if client != "auto":
                _io.refuse("--via cannot be combined with --client.")
            from cairnmind.memory.importers import (
                ImporterExecutionError,
                ImporterRefused,
                require_outside_session,
                run_converter_import,
            )
            try:
                require_outside_session()
                store = _io.store_for(ctx.obj)
                _io.output(run_converter_import(store, ctx.obj, path, via, project=project))
            except (ImporterRefused, ImporterExecutionError) as exc:
                _io.refuse(str(exc))
            return
        _io.output(_io.request(ctx.obj, "POST", "/import", json={"path": str(path.expanduser().resolve()),
                                                        "project": project, "client": client}))

    @app.command()
    @initialized
    def ask(ctx: typer.Context, project: ProjectArg, question: QuestionArg, worktree: Path | None = None,
            answer_schema: Path | None = None,
            log: Annotated[bool, typer.Option("--log", help="Keep this answer and its citations in the project's "
                                              "answer log, for `cairnmind eval replay`.")] = False,
            fast: Annotated[bool, typer.Option("--fast", help="Enable Tier-1 CPU NLI verification before escalating to Tier-2 LLM.")] = False,
            verifier: Annotated[str | None, typer.Option("--verifier", help="Verification tier: llm (default, strict) or nli (opt-in Tier-1 NLI triage).")] = None,
            strict_llm: Annotated[bool, typer.Option("--strict-llm", help="Force Tier-2 LLM verification (default).")] = False,
            as_json: JSON_FLAG = False):
        """Answer a question from the project's memory, with the sources it cites. Starts the service when needed."""
        if verifier is not None and verifier not in ("llm", "nli"):
            raise typer.BadParameter("Choose 'llm' or 'nli'.", param_hint="--verifier")
        if strict_llm and (fast or verifier == "nli"):
            raise typer.BadParameter("--strict-llm cannot be combined with --fast or --verifier nli.")
        chosen_verifier = "nli" if (fast or verifier == "nli") else "llm"
        payload: dict[str, object] = {"project": project, "question": question,
                                      "worktree": str(worktree.resolve()) if worktree else None}
        if chosen_verifier != "llm":
            payload["verifier"] = chosen_verifier
        if log:
            payload["log"] = True
        if answer_schema is not None:
            from cairnmind.agents.answer_contract import prepare_answer_schema
            payload["answer_schema"] = prepare_answer_schema(_io._json_file(answer_schema))
        alive = _io.service_alive(ctx.obj)
        if alive:
            _io._refuse_other_version(ctx.obj)  # reads the answer service_alive just took: one /health for the command (#405)
        else:
            _io._refuse_unknown_project(ctx.obj, project)
        result = _io.request(ctx.obj, "POST", "/ask", json=payload, matched=alive)  # matched: ensure_service would ask again
        from cairnmind.cli_cmds.cli_human import enrich_ask_json, has_degraded_origin
        enriched = enrich_ask_json(result)
        if (as_json or not _io._stdout_is_terminal()) and has_degraded_origin(enriched):
            from cairnmind.cli_cmds import first_start
            typer.echo(f"Cairnmind: {first_start.DEGRADED_ORIGIN_NOTE}", err=True)

        def sentences():
            from cairnmind.terminal.terminal_commands import ask_summary  # the workspace's view of an answer
            return ask_summary(enriched, str).split("\n")
        _io._human_or_json(enriched, as_json, sentences)

    @app.command()
    @initialized
    def context(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None):
        """Show the context a coding session starts with: checkpoint, tasks and records. Starts the service when needed."""
        _io.output(_io.request(ctx.obj, "GET", "/context", params={"project": project,
                       **({"worktree": str(worktree.resolve())} if worktree else {})}))

    @app.command()
    @initialized
    def brief(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None,
              as_json: Annotated[bool, typer.Option("--json", help="Print the marked lines as JSON.")] = False):
        """Show where work stopped: checkpoint, next task, check evidence, commits and proposals. Asks no model."""
        from cairnmind.memory.brief import build_brief
        # Read the store directly, as `tasks list` does: starting the service could start model work.
        store = _io.store_for(ctx.obj)
        pid = _io.resolve_project(store, project)["id"]
        result = build_brief(store, pid, _io.actor_scope(store, pid, worktree))
        if as_json:
            _io.output(result)
        else:
            typer.echo(result["text"])
            if result.get("next"):
                typer.echo(result["next"])

    @app.command()
    def impact(ctx: typer.Context, project: ProjectArg,
               since: Annotated[str, typer.Option("--since", help="Commit, tag or branch to compare the worktree with.")],
               worktree: Path | None = None,
               as_json: Annotated[bool, typer.Option("--json", help="Print the linked records as JSON.")] = False,
               pending: Annotated[bool, typer.Option(
                   "--pending", help="List only records without a current verdict.")] = False):
        """List records linked to the files and symbols changed since a commit. Read-only; asks no model."""
        from cairnmind.memory.impact import build_impact
        store = _io.store_for_read(ctx.obj)

        def read():
            pid = _io.resolve_project(store, project)["id"]
            return pid, build_impact(store, pid, since, _io.actor_scope(store, pid, worktree), pending=pending)

        result = store.read_consistently(read, _io.READ_KINDS["impact"])
        if as_json:
            _io.output(result)
        else:
            typer.echo(result["text"])

    @app.command()
    def scan(
        ctx: typer.Context,
        project: Annotated[
            str | None,
            typer.Argument(metavar="[PROJECT]", help="Project name, id or folder (default: current working directory)."),
        ] = None,
        diff: str = typer.Option(
            ...,
            "--diff",
            help="Git commit, branch, tag or range (e.g. origin/main or origin/main...HEAD).",
        ),
        worktree: Annotated[
            Path | None,
            typer.Option("--worktree", help="Registered worktree path to compare."),
        ] = None,
        as_json: Annotated[bool, typer.Option("--json", help="Print the scan report as JSON.")] = False,
    ):
        """Check anchored records against a Git diff without a running service. Read-only; writes nothing.

        Exit codes: 0 when clean, 1 when any anchored symbol changed or is gone, 2 on error or unready store.
        """
        from cairnmind.memory.scan import scan_diff

        target = project if project is not None else str((worktree or Path.cwd()).expanduser().resolve())
        try:
            store = _io.store_for_read(ctx.obj)

            def read():
                pid = _io.resolve_project(store, target)["id"]
                return pid, scan_diff(store, pid, diff, _io.actor_scope(store, pid, worktree))

            result = store.read_consistently(read, _io.READ_KINDS["scan"])
        except (ValueError, RuntimeError, OSError, ArcadeError) as exc:
            typer.echo(_io._pasteable(f"Cairnmind: {_io._os_error_text(exc) if isinstance(exc, OSError) else exc}"), err=True)
            raise typer.Exit(2) from None
        if as_json:
            _io.output(result)
        else:
            typer.echo(result["text"])
        if result["exit_code"]:
            raise typer.Exit(result["exit_code"])

    @app.command("impact-verdict")
    @initialized
    def impact_verdict(ctx: typer.Context, project: ProjectArg, record_id: RecordIdArg,
                       fingerprint: Annotated[str, typer.Option(help="The entry's fingerprint, from --json.")],
                       verdict: Annotated[str, typer.Option(help="still_valid, needs_update or not_related.")],
                       since: Annotated[str, typer.Option(help="The commit the list was made against.")],
                       note: Annotated[str | None, typer.Option(help="Required for needs_update.")] = None,
                       worktree: Path | None = None):
        """Refused since 0.13.0: a person gives a verdict in the Impact view of the inspector."""
        from cairnmind.memory.impact_verdicts import CLI_REFUSED
        # A person's act needs the open workspace's key (#306); the options stay so that a script gets this sentence.
        _io.refuse(CLI_REFUSED)

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
        from cairnmind.cli_cmds.codex_usage import stored_records, waiting_in_inbox
        from cairnmind.cli_cmds.usage import build_usage
        from cairnmind.memory.verification import usage_warn_threshold
        # The store is read, never prepared: it needs the running engine and never starts it. The usage itself is
        # read from the session journals and, for Codex, from the store; nothing is written anywhere.
        store = _io.store_for_read(ctx.obj)

        def read():
            selected = _io.resolve_project(store, project)
            return selected["id"], selected

        found = store.read_consistently(read, _io.READ_KINDS["usage"])
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
            raise typer.BadParameter(str(exc), param_hint="--since") from None
        if as_json:
            _io.output(result)
        else:
            typer.echo(result["text"])

    @app.command()
    def status(ctx: typer.Context, project: str | None = None):
        """Ask the running service for its queue, store and model state. Starts nothing."""
        result = _io.request(ctx.obj, "GET", "/status", start_service=False, params={"project": project} if project else {})
        # The address that was asked, so a report from another directory's service is recognised as such.
        _io.output({"service_url": ctx.obj.url, **result} if isinstance(result, dict) else result)

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
        _io.output(_io.request(ctx.obj, "GET", "/jobs", start_service=False, params=params))

    @jobs_app.command("show")
    def jobs_show(ctx: typer.Context, project: ProjectArg, job_id: JobIdArg):
        """Read a job that belongs to the given project."""
        _io.output(_io.request(ctx.obj, "GET", f"/jobs/{job_id}", start_service=False, params={"project": project}))

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
        _io.output(_io.request(ctx.obj, "POST", f"/jobs/{job_id}/retry", json=payload))

    @app.command("retry-jobs")
    @initialized
    def retry_jobs(ctx: typer.Context):
        """Queue every failed job of every project again, keeping its history. A person's command."""
        if _io.session_value("SESSION_ID"):
            raise typer.BadParameter(
                "A model session cannot retry the jobs of all projects. "
                "Use `cairnmind jobs retry PROJECT JOB_ID --expected-attempts N` with a current ACK.")
        store = _io.store_for(ctx.obj)
        if getattr(store, "not_writer", None):
            # A reader retries nothing, even when nothing failed: the notice already said why, so the refusal
            # is that one line and exit 1, not a success beside it (#244).
            from cairnmind.storage.store_writer import StoreNotWriter
            raise StoreNotWriter(store.not_writer)
        # What really happened is printed, not a constant.
        job_ids = list((_io._retry_jobs(store) or {}).get("job_ids", []))
        _io.output({"retried": bool(job_ids), "count": len(job_ids), "job_ids": job_ids})

    @app.command()
    def records(ctx: typer.Context, project: ProjectArg, history: bool = False):
        """List the project's records as JSON; --history adds the replaced ones. Read-only; starts nothing."""
        store = _io.store_for_read(ctx.obj)

        def read():
            pid = _io.resolve_project(store, project)["id"]
            return pid, store.list_records(pid, include_history=history)

        _io.output(store.read_consistently(read, _io.READ_KINDS["records"]))

    @app.command()
    def proposed(ctx: typer.Context, project: ProjectArg,
                 days: Annotated[int, typer.Option("--days", min=0, max=3650,
                                                   help="List the ones not updated for this many days or more.")] = 30,
                 worktree: Path | None = None,
                 as_json: Annotated[bool, typer.Option("--json", help="Print the list as JSON.")] = False):
        """List proposed findings not updated for a number of days, oldest first. Read-only; asks no model.

        Each one shows its id, kind, source count and newest evidence time. Confirm or dispute them in the memory inspector, which records your review with its version check and audit note (#189).
        """
        from cairnmind.memory.stale_proposed import stale_proposed, stale_text
        from cairnmind.terminal.inspector import Inspector
        store = _io.store_for_read(ctx.obj)

        def read():
            selected = _io.resolve_project(store, project)
            inspector = Inspector(store)
            scope = inspector._scope(selected["id"], _io.actor_scope(store, selected["id"], worktree))
            result = stale_proposed(store, selected["id"], days=days, matches=lambda row: inspector._matches(scope, row))
            return selected["id"], {**result, "text": stale_text(result, selected["name"])}

        result = store.read_consistently(read, _io.READ_KINDS["proposed"])
        if as_json:
            _io.output(result)
        else:
            typer.echo(result["text"])

    @app.command()
    def source(ctx: typer.Context, source_id: SourceIdArg):
        """Read the stored snapshot of a source, even when the original no longer exists."""
        _io.output(_io.store_for_read(ctx.obj).get_source(source_id))

    @app.command()
    @initialized
    def remember(ctx: typer.Context, project: ProjectArg,
                 content: Annotated[str, typer.Argument(
                     metavar="CONTENT", help="The text to keep. Text that begins with - goes last, after --, with the "
                                             "options before it: `cairnmind remember PROJECT --kind decision -- '-text'`.")],
                 kind: str = "fact",
                 source_id: Annotated[list[str] | None, typer.Option("--source")] = None,
                 confirmed: Annotated[bool, typer.Option(
                     help="Refused since 0.13.0; a person confirms in the inspector.")] = False,
                 key: str | None = None, as_json: JSON_FLAG = False):
        """Store a finding as proposed. --confirmed is refused: a person confirms in the inspector of the workspace."""
        from cairnmind.memory import confirmation
        if confirmed:
            # The command line confirms nothing (#214): refused before anything is written, not even a proposed record,
            # because the person asked for a confirmed one and storing another quietly is not allowed.
            _io.refuse(confirmation.CLI_CONFIRM_REFUSED)
        store = _io.store_for(ctx.obj)
        pid = _io.resolve_project(store, project)["id"]
        from cairnmind.store import KeyTaken
        try:
            with _io.actor_write(store, ctx.obj, pid):
                record = {"kind": kind, "content": content, "status": "proposed"}
                if key is not None:
                    record["key"] = key
                # Known secrets are replaced in the note's indexed view (#182); the note itself is kept as typed.
                from cairnmind.memory.secret_mask import note_metadata
                sources = source_id or [store.snapshot(pid, f"manual://{uuid.uuid4()}", content, "manual",
                                                      note_metadata(store.settings, content,
                                                                    {"role": "user", "origin": "cli"}))["id"]]
                result = store.remember(pid, record, sources)
        except (KeyTaken, confirmation.VerdictKept) as exc:
            if getattr(exc, "change", "key") != "key":
                raise
            # The key is taken and this command cannot update a record by id: what the person can do (#238).
            _io.refuse(f"Record {exc.record_id} already holds the key {key!r} for a {kind}. Nothing was saved. Use "
                   "another key, or dispute or decide that record in the inspector.")

        from cairnmind.cli_cmds.cli_human import has_degraded_origin
        if (as_json or not _io._stdout_is_terminal()) and has_degraded_origin(result):
            from cairnmind.cli_cmds import first_start
            typer.echo(f"Cairnmind: {first_start.DEGRADED_ORIGIN_NOTE}", err=True)

        def sentences():
            import shlex

            from cairnmind.cli_cmds import first_start
            from cairnmind.pal.terminal_safe import safe_text
            quoted = shlex.quote(safe_text(project))
            extra = [first_start.DEGRADED_ORIGIN_NOTE] if has_degraded_origin(result) else []
            # brief lists a proposed record for review, except a task or a checkpoint, which it leaves out.
            if result.get("status") == "proposed" and kind not in {"task", "checkpoint"}:
                return [first_start.REMEMBER_PROPOSED.format(kind=safe_text(kind), project=quoted), *extra]
            return [first_start.REMEMBER_STORED.format(status=result.get("status"), kind=safe_text(kind),
                                                       id=result.get("id"), project=quoted), *extra]
        _io._human_or_json(result, as_json, sentences)

    @app.command()
    @initialized
    def checkpoint(ctx: typer.Context, project: ProjectArg, state_file: StateFileArg):
        """Store where work stands, from a JSON file, as the project's checkpoint. Starts the service when needed."""
        _io.output(_io.request(ctx.obj, "POST", "/checkpoint", json={"project": project,
                       "state": _io._json_file(state_file)}))
