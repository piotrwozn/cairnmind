"""The `memory` commands of published team memory (#170, #171, #172), registered on the CLI's memory group."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from cairnmind.cli_cmds import io as _io
from cairnmind.cli_cmds.cli_arguments import (
    AuditPathArg,
    BaseArg,
    HeadArg,
    OursSnapshotArg,
    PlanArg,
    ProjectArg,
    PublishedArg,
    RecordArg,
    RevisionArg,
    TheirsSnapshotArg,
)


def register(memory_app, initialized):
    @memory_app.command("publish")
    @initialized
    def memory_publish(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None,
                       preview: Annotated[bool, typer.Option(
                           "--preview", help="Show what would be written, and the size, without writing.")] = False,
                       new_package: Annotated[bool, typer.Option(
                           "--new-package", help="In a checkout that holds other packages, start this project's own.")]
                       = False):
        """Write the project's confirmed knowledge as one file per record where its carrier says.

        By default under .memory/cairnmind-published/; see `memory carrier`. Never stages, commits or pushes. Held records are listed with the reason. Exit 1 when refused.
        """
        payload = {"project": project, "worktree": str(worktree.expanduser().resolve()) if worktree else None,
                   "preview": preview}
        if new_package:
            payload["new_package"] = True
        _io.output(_io.request(ctx.obj, "POST", "/memory/publish", json=payload))

    @memory_app.command("carrier")
    @initialized
    def memory_carrier(ctx: typer.Context, project: ProjectArg,
                       repository: Annotated[bool, typer.Option(
                           "--repository", help="Publish into the project's repository (the default).")] = False,
                       checkout: Annotated[Path | None, typer.Option(
                           "--checkout", help="Publish into a checkout of a separate memory repository.")] = None,
                       worktree: Path | None = None):
        """Choose where the project's published memory goes. Nothing is moved: the command says where the
        earlier package is and what to do with it. Committing and pushing stay with you."""
        from cairnmind.config import session_value
        if repository == (checkout is not None):
            raise typer.BadParameter("Give exactly one of --repository or --checkout PATH")
        if session_value("SESSION_ID"):
            _io.refuse("Where published memory goes is chosen only by a person, not inside a coding session.")
        _io.output(_io.request(ctx.obj, "POST", "/memory/carrier", json={
            "project": project, "kind": "repository" if repository else "checkout",
            "path": str(checkout.expanduser().absolute()) if checkout is not None else None,
            **({"worktree": str(worktree.expanduser().resolve())} if worktree else {})}))

    @memory_app.command("check-published")
    def memory_check_published(ctx: typer.Context, path: PublishedArg,
                               config: Annotated[Path | None, typer.Option(
                                   "--config", help="Personal-data patterns; default: .cairnmind/publication.toml "
                                                    "of the checkout holding PATH.")] = None):
        """Check a published memory directory without a store, service or model. Exit 0 valid, 1 refused, 2 when
        PATH is not a directory.

        Includes the secrets scan (private keys, key and token shapes, passwords in connection strings, random strings, and personal data when the project turns it on); no secret is printed.
        """
        from cairnmind.memory.memory_published_check import check_directory, releases_in
        from cairnmind.memory.memory_published_release import checkout_rules
        from cairnmind.memory.memory_published_secrets import SecretRulesError
        from cairnmind.pal.terminal_safe import safe_text
        selected = path.expanduser().absolute()
        if not selected.is_dir():
            # A wrong argument, not a refused tree (#239): exit 2, as for any other bad parameter.
            raise typer.BadParameter(safe_text(f"{selected} is not a directory"), param_hint="PATH")
        try:
            rules = checkout_rules(selected, config.expanduser().absolute() if config else None)
        except (SecretRulesError, OSError) as exc:
            raise typer.BadParameter(str(exc), param_hint="--config") from None
        tree, errors = check_directory(selected, rules)
        for location, reason in errors:
            typer.echo(safe_text(f"{location or '(tree)'}: {reason}"))
        if errors or tree is None:
            raise typer.Exit(1)
        typer.echo(f"valid: {len(tree['records'])} records and {len(tree['plans'])} plans of {tree['project_id']}")
        # A release is a claim in the file, like an audit: listed so that a reviewer sees every one.
        for line in releases_in(tree):
            typer.echo(safe_text(line))

    @memory_app.command("import-published")
    def memory_import_published(ctx: typer.Context, path: PublishedArg,
                                project: Annotated[str, typer.Option("--project")],
                                dry_run: bool = False,
                                bind: Annotated[bool, typer.Option(
                                    "--bind", help="Replace the project's publication identity with the directory's.")] = False,
                                take_published: Annotated[list[str] | None, typer.Option(
                                    "--take-published",
                                    help="A diverged record ID: append the published version locally; nothing is removed. "
                                    "Over a record confirmed or disputed here it is refused outside the workspace "
                                    "(Team memory in the memory view).")] = None,
                                replace_confirmed: Annotated[bool, typer.Option(
                                    "--replace-confirmed", help="Ask to replace records that were confirmed or disputed "
                                    "on this machine; refused outside the workspace, where Team memory in the memory "
                                    "view does it. The dry run lists them.")] = False):
        """Import a merged published memory directory; nothing imported is shown as verified."""
        from cairnmind.config import Settings
        from cairnmind.memory.memory_published import _listing
        from cairnmind.memory.memory_published_check import check_directory
        selected = path.expanduser().absolute()
        _, errors = check_directory(selected)  # Refuse a broken tree before starting anything.
        if errors:
            _io.refuse("The published memory was refused: " + _listing(errors), 1)
        if not dry_run:
            ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
        _io.output(_io.request(ctx.obj, "POST", "/memory/import-published", start_service=not dry_run,
                               json={"path": str(selected), "project": project, "dry_run": dry_run, "bind": bind,
                                     "take_published": list(take_published or []),
                                     **({"replace_confirmed": True} if replace_confirmed else {})}))

    @memory_app.command("import-commit")
    def memory_import_commit(ctx: typer.Context, revision: RevisionArg,
                             project: Annotated[str, typer.Option("--project")],
                             worktree: Path | None = None, dry_run: bool = False,
                             bind: Annotated[bool, typer.Option(
                                 "--bind", help="Replace the project's publication identity with the commit's.")] = False,
                             replace_confirmed: Annotated[bool, typer.Option(
                                 "--replace-confirmed", help="Ask to replace records that were confirmed or disputed "
                                 "on this machine; refused outside the workspace, where Team memory in the memory "
                                 "view does it. The dry run lists them.")] = False):
        """Import the published memory of a commit (the main branch after a merge) and re-check every quoted
        passage against that commit's code: still there anchors a record, gone leaves it orphaned. Nothing
        imported is verified. Reads Git only; writes nothing to the repository."""
        from cairnmind.config import Settings
        if revision.startswith("-"):
            raise typer.BadParameter("REVISION must be a commit name such as HEAD, a branch or a commit ID")
        if not dry_run:
            ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
        _io.output(_io.request(ctx.obj, "POST", "/memory/import-commit", start_service=not dry_run,
                               json={"revision": revision, "project": project, "dry_run": dry_run, "bind": bind,
                                     **({"replace_confirmed": True} if replace_confirmed else {}),
                                     "worktree": str(worktree.expanduser().resolve()) if worktree else None}))

    @memory_app.command("show-published")
    def memory_show_published(ctx: typer.Context, path: PublishedArg,
                              kind: Annotated[str | None, typer.Option("--kind", help="Record kind, or 'plan'.")] = None,
                              status: Annotated[str | None, typer.Option("--status", help="Record status.")] = None):
        """Read a published memory directory as text. Writes nothing and needs no store or service."""
        from cairnmind.memory.memory_published_check import check_directory
        from cairnmind.memory.memory_view import render_snapshot
        selected = path.expanduser().absolute()
        tree, errors = check_directory(selected)
        if errors or tree is None:
            from cairnmind.memory.memory_published import _listing
            _io.refuse("The published memory was refused: " + _listing(errors), 1)
        payload = tree["payload"]
        manifest = {"project": {"id": tree["project_id"], "name": "published memory"},
                    "counts": {table: len(rows) for table, rows in payload.items()},
                    "snapshot_id": "none (a published directory has no snapshot)", "created_at": None,
                    "origin_root": f"published directory {selected}"}
        typer.echo(render_snapshot(manifest, payload, kind=kind, status=status))
        typer.echo("Published elsewhere and not verified here: reviewed_by names are claims made by the files.")

    @memory_app.command("release")
    @initialized
    def memory_release(ctx: typer.Context, project: ProjectArg, record: RecordArg,
                       reason: Annotated[str, typer.Option("--reason", help="Why the hits are not secrets; published.")],
                       worktree: Path | None = None):
        """Refused since 0.13.0: a person releases a record in the inspector of the workspace."""
        from cairnmind.memory.memory_published_release import CLI_REFUSED
        # A person's act needs the open workspace's key (#306); the options stay so that a script gets this sentence.
        _io.refuse(CLI_REFUSED)

    @memory_app.command("diff")
    def memory_diff(ctx: typer.Context, base: BaseArg, head: HeadArg,
                    project_root: Annotated[Path, typer.Option(
                        "--project-root", help="The project's directory in the Git repository (default: here).")] = Path("."),
                    markdown: Annotated[bool, typer.Option(
                        "--markdown", help="A bounded Markdown comment for a pull request.")] = False,
                    max_chars: Annotated[int, typer.Option("--max-chars", min=2000, max=60000)] = 60000,
                    as_json: Annotated[bool, typer.Option("--json", help="The comparison as JSON.")] = False,
                    package: Annotated[str | None, typer.Option(
                        "--package", help="In a separate memory repository (the checkout carrier): the publication "
                                          "identity whose directory to compare, instead of .memory/cairnmind-published.")]
                    = None):
        """Compare the published memory of two commits, without a store, service or model.

        Exit 0 when the head's published memory would import, 1 when it would not, 2 on a usage error. The head is held to the secrets scan of `check-published`, with the head commit's .cairnmind/publication.toml; no secret is printed.
        """
        from cairnmind.memory.memory_published_diff import compare, render_markdown, render_text
        from cairnmind.memory.memory_published_git import published_files
        from cairnmind.memory.memory_published_release import commit_config_text
        from cairnmind.memory.memory_published_secrets import SecretRulesError, load_rules
        root = project_root.expanduser().absolute()
        try:
            base_commit, base_files, base_errors = published_files(root, base, package)
            head_commit, head_files, head_errors = published_files(root, head, package)
            rules = load_rules(commit_config_text(root, head_commit))
        except (ValueError, SecretRulesError) as exc:
            _io.refuse(str(exc), 2)
        # The base is only compared with: what of it can be read is used, and each entry left out is counted.
        report = (compare(base_files, head_files, rules) if not head_errors
                  else {"status": "refused", "errors": head_errors, "entries": [], "counts": [], "skipped": 0})
        if base_errors and not head_errors:
            if base_files:
                report["skipped"] += len(base_errors)
            else:
                report["base_unreadable"] = True
        if as_json:
            _io.output({"base": base_commit, "head": head_commit, **report})
        elif markdown:
            typer.echo(render_markdown(report, base_commit, head_commit, max_chars), nl=False)
        else:
            typer.echo(render_text(report, base_commit, head_commit), nl=False)
        if report["status"] == "refused":
            raise typer.Exit(1)

    @memory_app.command("confirm-plan")
    @initialized
    def memory_confirm_plan(ctx: typer.Context, project: ProjectArg, plan: PlanArg,
                            expected_version: Annotated[int, typer.Option("--expected-version", min=1)],
                            reason: str | None = None, worktree: Path | None = None):
        """Refused since 0.13.0: a person confirms a plan in the inspector of the workspace."""
        from cairnmind.memory import confirmation
        # The command line confirms nothing (#214). The options stay so that a script gets this sentence.
        _io.refuse(confirmation.CLI_PLAN_REFUSED)

    @memory_app.command("audit")
    def memory_audit(ctx: typer.Context, path: AuditPathArg = Path("."),
                     config: Annotated[Path | None, typer.Option(
                         "--config", help="Secrets and allowlist rules; default: .cairnmind/publication.toml.")] = None,
                     no_history: Annotated[bool, typer.Option(
                         "--no-history", help="Skip scanning Git history for earlier contains_transcripts commits.")] = False,
                     as_json: Annotated[bool, typer.Option("--json", help="Print the audit report as JSON.")] = False):
        """Audit .memory/cairnmind/ and .memory/cairnmind-published/ without a store, service or model.

        Exits 0 when clean, 1 when transcripts, secrets, uncommitted working-tree refs or .cairnmindignore violations are found, and 2 on invalid path or configuration. Also warns if Git history contains an earlier snapshot with contains_transcripts=true.
        """
        from cairnmind.memory.memory_audit import audit_memory, render_audit_text
        from cairnmind.memory.memory_published_secrets import SecretRulesError
        from cairnmind.pal.terminal_safe import safe_text
        try:
            report = audit_memory(path, config=config, check_history=not no_history)
        except SecretRulesError as exc:
            raise typer.BadParameter(str(exc), param_hint="--config") from None
        except ValueError as exc:
            raise typer.BadParameter(safe_text(str(exc)), param_hint="PATH") from None
        if as_json:
            for warning in report.get("history_warnings", []):
                typer.echo(safe_text(warning["message"]), err=True)
            _io.output(report)
        else:
            typer.echo(render_audit_text(report))
        if report["status"] != "passed":
            raise typer.Exit(1)

    @memory_app.command("merge")
    def memory_merge(ctx: typer.Context, ours: OursSnapshotArg, theirs: TheirsSnapshotArg,
                     output_dir: Annotated[Path, typer.Option(
                         "--output", help="Directory where the merged snapshot package will be written.")]):
        """Merge two portable memory snapshots deterministically by entity version and fingerprint.

        Resolves branch conflicts in .memory/cairnmind/ without a store, service or model. Raises a conflict if both sides edited the same entity at the same version with different content.
        """
        from cairnmind.memory.snapshot_privacy import merge_snapshots
        _io.output(merge_snapshots(ours, theirs, output_dir))
