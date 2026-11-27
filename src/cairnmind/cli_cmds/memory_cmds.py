"""CLI commands for portable project memory snapshots, decisions, orphans, re-extraction, and healing."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import httpx
import typer

from cairnmind.cli_cmds.cli_arguments import (
    ProjectArg,
    SnapshotArg,
)
from cairnmind.cli_cmds import io as _io
from cairnmind.cli_cmds.io import initialized
from cairnmind.config import Settings


def register(apps: dict[str, typer.Typer]) -> None:
    memory_app = apps["memory_app"]

    @memory_app.command("snapshot")
    @initialized
    def memory_snapshot(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None, preview: bool = False,
                        profile: Annotated[str, typer.Option(
                            "--profile", help="Snapshot profile: team (default, no transcripts, quote-by-reference) or full.")] = "team",
                        include_transcripts: Annotated[bool, typer.Option(
                            "--include-transcripts", help="Required with --profile full to export raw transcripts and full source contents.")] = False,
                        skip_uncommitted: Annotated[bool, typer.Option(
                            "--skip-uncommitted", help="In team profile, skip uncommitted or untracked file evidence instead of blocking.")] = False,
                        redact: Annotated[bool, typer.Option(
                            "--redact", help="Mask detected secrets with [REDACTED:...] markers instead of aborting export.")] = False):
        """Save the project's portable memory snapshot; defaults to --profile team (fail-closed privacy).

        Use --preview to inspect scope and size without writing. Use --profile full --include-transcripts only for private local backups that will not be pushed to a remote Git repository.
        """
        from cairnmind.memory.snapshot_privacy import PROFILE_TEAM, validate_profile_options
        try:
            validate_profile_options(profile, include_transcripts)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        payload: dict[str, object] = {"project": project, "worktree": str(worktree.expanduser().resolve()) if worktree else None}
        if profile != PROFILE_TEAM:
            payload["profile"] = profile
        if include_transcripts:
            payload["include_transcripts"] = True
        if skip_uncommitted:
            payload["skip_uncommitted"] = True
        if redact:
            payload["redact"] = True
        if preview:
            result = _io.request(ctx.obj, "POST", "/memory/preview", json=payload)
            _io.output(result)
            return
        try:
            # A large project writes tens of thousands of objects: far longer than an ordinary request (#158).
            result = _io.request(ctx.obj, "POST", "/memory/snapshot", json=payload,
                             timeout=httpx.Timeout(60, read=_io.SNAPSHOT_WAIT_SECONDS))
        except httpx.TimeoutException:
            raise typer.BadParameter(
                f"No answer within {_io.SNAPSHOT_WAIT_SECONDS // 60} minutes. The snapshot is still being written by "
                f"the service; leaving did not stop it. It is done when `cairnmind memory status {project}` shows "
                "the new snapshot; until then another snapshot waits for this one.") from None
        if isinstance(result, dict):
            if result.get("contains_transcripts"):
                typer.echo(
                    "Warning: this snapshot includes raw transcripts (contains_transcripts=true). "
                    "Do not commit or push it to a remote Git repository.",
                    err=True,
                )
            if result.get("redacted") and result.get("redaction_counts"):
                summary = ", ".join(f"{tag}={count}" for tag, count in sorted(result["redaction_counts"].items()))
                typer.echo(f"Redacted secrets before writing snapshot: {summary}", err=True)
        _io.output(result)

    @memory_app.command("inspect")
    def memory_inspect(ctx: typer.Context, path: SnapshotArg):
        """Check a snapshot's files without initializing the store or starting services."""
        from cairnmind.memory.memory import inspect_snapshot
        # Preserve symlink components for the package validator to reject them.
        selected = str(path.expanduser().absolute())
        if _io.session_value("SESSION_ID"):
            _io.output(_io.request(ctx.obj, "POST", "/memory/inspect", start_service=False, json={"path": selected}))
        else:
            _io.output(inspect_snapshot(selected))

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
        from cairnmind.memory.memory_view import show_snapshot
        # Preserve symlink components for the package validator to reject them.
        selected = str(path.expanduser().absolute())
        expected = None
        if _io.session_value("SESSION_ID"):
            # A coding session may read only its own project's memory; the service decides.
            expected = _io.request(ctx.obj, "POST", "/memory/inspect", start_service=False,
                               json={"path": selected})["snapshot_id"]
        typer.echo(show_snapshot(selected, kind=kind, status=status, grep=grep, sources_text=sources,
                                 expected_snapshot_id=expected))

    @memory_app.command("decisions")
    def memory_decisions(
        ctx: typer.Context,
        project: ProjectArg,
        source: Annotated[
            str | None,
            typer.Argument(
                metavar="[SOURCE]",
                help="Single per-project decision source: off, comments, adr, or trailers. Omit to inspect.",
            ),
        ] = None,
        as_json: Annotated[bool, typer.Option("--json", help="Print the setting as JSON.")] = False,
    ):
        """Show or set the single per-project code decision source (off by default: off | comments | adr | trailers)."""
        from cairnmind.memory.code_decisions import DECISION_SOURCES, get_decision_source, set_decision_source

        if source is None:
            store = _io.store_for_read(ctx.obj)

            def read():
                selected = store.resolve_project(project)
                current = get_decision_source(store, selected["id"])
                return selected["id"], {
                    "project_id": selected["id"],
                    "project": selected["name"],
                    "decision_source": current,
                    "allowed_sources": list(DECISION_SOURCES),
                }

            result = store.read_consistently(read, _io.READ_KINDS["memory decisions"])
        else:
            ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
            store = _io.store_for(ctx.obj)
            result = {**set_decision_source(store, project, source), "allowed_sources": list(DECISION_SOURCES)}
        if as_json:
            _io.output(result)
        else:
            typer.echo(
                f"Decision source for {result['project']}: {result['decision_source']} "
                f"(allowed: {', '.join(DECISION_SOURCES)})"
            )

    @memory_app.command("orphans")
    def memory_orphans(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None,
                       as_json: Annotated[bool, typer.Option("--json", help="Print the records as JSON.")] = False):
        """List records whose evidence is no longer current: orphaned, or resting on a changed source. Read-only."""
        from cairnmind.memory.brief import _scoped_records
        from cairnmind.memory.evidence import orphans_report
        from cairnmind.memory.verification import _root
        store = _io.store_for_read(ctx.obj)

        def read():
            selected = store.resolve_project(project)
            root = _root(store, selected["id"], _io.actor_scope(store, selected["id"], worktree))
            return selected["id"], orphans_report(store, selected, root, _scoped_records(store, selected["id"], root))

        report = store.read_consistently(read, _io.READ_KINDS["memory orphans"])
        if as_json:
            _io.output(report)
        else:
            typer.echo(report["text"])

    @memory_app.command("extraction-stats")
    def memory_extraction_stats(ctx: typer.Context, project: ProjectArg,
                                as_json: Annotated[bool, typer.Option("--json", help="Print the numbers as JSON.")] = False):
        """Per extractor version: how many extracted records are proposed, confirmed, disputed or superseded. Read-only."""
        from cairnmind.agents.extraction_stats import extraction_stats, render
        store = _io.store_for_read(ctx.obj)

        def read():
            selected = store.resolve_project(project)
            return selected["id"], extraction_stats(store, selected["id"])

        stats = store.read_consistently(read, _io.READ_KINDS["memory extraction-stats"])
        if as_json:
            _io.output(stats)
        else:
            typer.echo(render(stats))

    @memory_app.command("reextract")
    @initialized
    def memory_reextract(ctx: typer.Context, project: ProjectArg,
                         below_version: Annotated[int | None, typer.Option(
                             "--below-version", min=1, help="Extractor version; default is this build's.")] = None,
                         dry_run: Annotated[bool, typer.Option(
                             "--dry-run", help="Print the counts per extractor version and queue nothing.")] = False):
        """Extract again the sources whose untouched proposals come from an older extractor. Queues one extract job
        per source; when it commits, that source's untouched proposals of an older extractor version are superseded
        in the same transaction. Confirmed, disputed and edited records are never changed.
        """
        from cairnmind.agents.extraction_input import EXTRACTOR_VERSION
        from cairnmind.memory.reextract import plan, queue
        below = EXTRACTOR_VERSION if below_version is None else below_version
        if below > EXTRACTOR_VERSION:
            raise typer.BadParameter(f"This build's extractor is version {EXTRACTOR_VERSION}.", param_hint="--below-version")
        if not dry_run:
            if _io.session_value("SESSION_ID"):
                raise typer.BadParameter("A model session cannot queue extraction; a person runs this command.")
            from cairnmind.agents.knowledge_mode import needs_models
            why = needs_models(ctx.obj, "cairnmind memory reextract")
            if why:
                _io.refuse(why)  # as rebuild-vectors: the jobs loop does not run without models, nothing is queued (#274)
        store = _io.store_for(ctx.obj)
        if not dry_run and getattr(store, "not_writer", None):
            from cairnmind.storage.store_writer import StoreNotWriter
            raise StoreNotWriter(store.not_writer)
        pid = store.resolve_project(project)["id"]
        _io.output((plan if dry_run else queue)(store, pid, below, EXTRACTOR_VERSION))

    @memory_app.command("import")
    def memory_import(ctx: typer.Context, path: SnapshotArg,
                      root: Annotated[Path, typer.Option("--root")], name: str | None = None,
                      dry_run: bool = False,
                      replace_confirmed: Annotated[bool, typer.Option(
                          "--replace-confirmed", help="Ask to replace records that were confirmed or disputed on this "
                          "machine; refused outside the workspace, where Import in the memory view does it. The dry "
                          "run lists them.")] = False,
                      no_heal: Annotated[bool, typer.Option(
                          "--no-heal", help="Skip automatic Git-diff and AST quote self-healing during import.")] = False):
        """Import the memory of this project only; --dry-run needs a running service and writes nothing."""
        from cairnmind.memory.memory import inspect_snapshot
        selected = str(path.expanduser().absolute())
        inspect_snapshot(selected)  # Validate the transfer before initializing anything.
        if not dry_run:
            ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
        payload = {"path": selected, "root": str(root.expanduser().resolve()), "dry_run": dry_run}
        if replace_confirmed:
            payload["replace_confirmed"] = True
        if no_heal:
            payload["heal_quotes"] = False
        if name is not None:
            payload["name"] = name
        _io.output(_io.request(ctx.obj, "POST", "/memory/import", start_service=not dry_run, json=payload))

    @memory_app.command("heal")
    @initialized
    def memory_heal(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None,
                    commit: Annotated[str, typer.Option("--commit", help="Target Git revision to heal quotes against.")] = "HEAD",
                    dry_run: bool = False):
        """Self-heal moved file quotes via Git diffs, AST context, SHA-256 verification, CAS, and audit logging."""
        payload = {
            "project": project,
            "worktree": str(worktree.expanduser().resolve()) if worktree else None,
            "target_commit": commit,
            "dry_run": dry_run,
        }
        _io.output(_io.request(ctx.obj, "POST", "/memory/heal", start_service=not dry_run, json=payload))

    @memory_app.command("heal-audit")
    def memory_heal_audit(ctx: typer.Context, project: ProjectArg,
                          record: Annotated[str | None, typer.Option("--record", help="Filter audit entries by record ID.")] = None,
                          limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100):
        """Read the immutable quote self-healing audit log for a project."""
        params = {"project": project, "limit": limit}
        if record is not None:
            params["record_id"] = record
        _io.output(_io.request(ctx.obj, "GET", "/memory/heal-audit", start_service=False, params=params))

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
        _io.output(_io.request(ctx.obj, "PATCH", "/memory/settings", json=payload))

    @memory_app.command("status")
    def memory_status(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None):
        """Read the last snapshot and the schedule; start no service and create no configuration."""
        params = {"project": project}
        if worktree is not None:
            params["worktree"] = str(worktree.expanduser().resolve())
        _io.output(_io.request(ctx.obj, "GET", "/memory/status", start_service=False, params=params))
