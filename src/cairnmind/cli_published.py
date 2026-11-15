"""The `memory` commands of published team memory (#170, #171, #172), registered on the CLI's memory group."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer


def register(memory_app, initialized):
    @memory_app.command("publish")
    @initialized
    def memory_publish(ctx: typer.Context, project: str, worktree: Path | None = None,
                       preview: Annotated[bool, typer.Option(
                           "--preview", help="Show what would be written, and the size, without writing.")] = False,
                       new_package: Annotated[bool, typer.Option(
                           "--new-package", help="In a checkout that holds other packages, start this project's own.")]
                       = False):
        """Write the project's confirmed knowledge as one file per record where its carrier says.

        By default under .memory/cairnmind-published/; see `memory carrier`. Never stages, commits or pushes.
        Held records are listed with the reason. Exit 1 when refused."""
        from cairnmind.cli import output, request
        payload = {"project": project, "worktree": str(worktree.expanduser().resolve()) if worktree else None,
                   "preview": preview}
        if new_package:
            payload["new_package"] = True
        output(request(ctx.obj, "POST", "/memory/publish", json=payload))

    @memory_app.command("carrier")
    @initialized
    def memory_carrier(ctx: typer.Context, project: str,
                       repository: Annotated[bool, typer.Option(
                           "--repository", help="Publish into the project's repository (the default).")] = False,
                       checkout: Annotated[Path | None, typer.Option(
                           "--checkout", help="Publish into a checkout of a separate memory repository.")] = None,
                       worktree: Path | None = None):
        """Choose where the project's published memory goes. Nothing is moved: the command says where the
        earlier package is and what to do with it. Committing and pushing stay with you."""
        from cairnmind.cli import output, refuse, request
        from cairnmind.config import session_value
        if repository == (checkout is not None):
            raise typer.BadParameter("Give exactly one of --repository or --checkout PATH")
        if session_value("SESSION_ID"):
            refuse("Where published memory goes is chosen only by a person, not inside a coding session.")
        output(request(ctx.obj, "POST", "/memory/carrier", json={
            "project": project, "kind": "repository" if repository else "checkout",
            "path": str(checkout.expanduser().absolute()) if checkout is not None else None,
            **({"worktree": str(worktree.expanduser().resolve())} if worktree else {})}))

    @memory_app.command("check-published")
    def memory_check_published(ctx: typer.Context, path: Path,
                               config: Annotated[Path | None, typer.Option(
                                   "--config", help="Personal-data patterns; default: .cairnmind/publication.toml "
                                                    "of the checkout holding PATH.")] = None):
        """Check a published memory directory without a store, service or model. Exit 0 valid, 1 refused.

        Includes the secrets scan (private keys, key and token shapes, passwords in connection strings,
        random strings, and personal data when the project turns it on); no secret is printed."""
        from cairnmind.memory_published_check import check_directory, releases_in
        from cairnmind.memory_published_release import checkout_rules
        from cairnmind.memory_published_secrets import SecretRulesError
        from cairnmind.terminal_safe import safe_text
        selected = path.expanduser().absolute()
        try:
            rules = checkout_rules(selected, config.expanduser().absolute() if config else None)
        except (SecretRulesError, OSError) as exc:
            raise typer.BadParameter(str(exc), param_hint="--config") from None
        tree, errors = check_directory(selected, rules)
        for location, reason in errors:
            typer.echo(safe_text(f"{location or '(tree)'}: {reason}"))
        if errors:
            raise typer.Exit(1)
        typer.echo(f"valid: {len(tree['records'])} records and {len(tree['plans'])} plans of {tree['project_id']}")
        # A release is a claim in the file, like an audit: listed so that a reviewer sees every one.
        for line in releases_in(tree):
            typer.echo(safe_text(line))

    @memory_app.command("import-published")
    def memory_import_published(ctx: typer.Context, path: Path,
                                project: Annotated[str, typer.Option("--project")],
                                dry_run: bool = False,
                                bind: Annotated[bool, typer.Option(
                                    "--bind", help="Replace the project's publication identity with the directory's.")] = False,
                                take_published: Annotated[list[str] | None, typer.Option(
                                    "--take-published",
                                    help="A diverged record ID: append the published version locally; nothing is removed.")] = None):
        """Import a merged published memory directory; nothing imported is shown as verified."""
        from cairnmind.cli import output, refuse, request
        from cairnmind.config import Settings
        from cairnmind.memory_published import _listing
        from cairnmind.memory_published_check import check_directory
        selected = path.expanduser().absolute()
        _, errors = check_directory(selected)  # Refuse a broken tree before starting anything.
        if errors:
            refuse("The published memory was refused: " + _listing(errors), 1)
        if not dry_run:
            ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
        output(request(ctx.obj, "POST", "/memory/import-published", start_service=not dry_run,
                       json={"path": str(selected), "project": project, "dry_run": dry_run, "bind": bind,
                             "take_published": list(take_published or [])}))

    @memory_app.command("import-commit")
    def memory_import_commit(ctx: typer.Context, revision: str,
                             project: Annotated[str, typer.Option("--project")],
                             worktree: Path | None = None, dry_run: bool = False,
                             bind: Annotated[bool, typer.Option(
                                 "--bind", help="Replace the project's publication identity with the commit's.")] = False):
        """Import the published memory of a commit (the main branch after a merge) and re-check every quoted
        passage against that commit's code: still there anchors a record, gone leaves it orphaned. Nothing
        imported is verified. Reads Git only; writes nothing to the repository."""
        from cairnmind.cli import output, request
        from cairnmind.config import Settings
        if revision.startswith("-"):
            raise typer.BadParameter("REVISION must be a commit name such as HEAD, a branch or a commit ID")
        if not dry_run:
            ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
        output(request(ctx.obj, "POST", "/memory/import-commit", start_service=not dry_run,
                       json={"revision": revision, "project": project, "dry_run": dry_run, "bind": bind,
                             "worktree": str(worktree.expanduser().resolve()) if worktree else None}))

    @memory_app.command("show-published")
    def memory_show_published(ctx: typer.Context, path: Path,
                              kind: Annotated[str | None, typer.Option("--kind", help="Record kind, or 'plan'.")] = None,
                              status: Annotated[str | None, typer.Option("--status", help="Record status.")] = None):
        """Read a published memory directory as text. Writes nothing and needs no store or service."""
        from cairnmind.memory_published_check import check_directory
        from cairnmind.memory_view import render_snapshot
        from cairnmind.cli import refuse
        selected = path.expanduser().absolute()
        tree, errors = check_directory(selected)
        if errors:
            from cairnmind.memory_published import _listing
            refuse("The published memory was refused: " + _listing(errors), 1)
        payload = tree["payload"]
        manifest = {"project": {"id": tree["project_id"], "name": "published memory"},
                    "counts": {table: len(rows) for table, rows in payload.items()},
                    "snapshot_id": "none (a published directory has no snapshot)", "created_at": None,
                    "origin_root": f"published directory {selected}"}
        typer.echo(render_snapshot(manifest, payload, kind=kind, status=status))
        typer.echo("Published elsewhere and not verified here: reviewed_by names are claims made by the files.")

    @memory_app.command("release")
    @initialized
    def memory_release(ctx: typer.Context, project: str, record: str,
                       reason: Annotated[str, typer.Option("--reason", help="Why the hits are not secrets; published.")],
                       worktree: Path | None = None):
        """Let a record's false alarms of the secrets scan be published, after reading the record.

        The release names each field and kind of hit and is published in the record's file with the reason.
        A registered secret is never released."""
        from cairnmind.cli import output, refuse, request
        from cairnmind.config import session_value
        if session_value("SESSION_ID"):
            refuse("A coding session cannot release a record; a person releases it outside the session.")
        from cairnmind.memory_published_format import canonical_uuid
        if not canonical_uuid(record):
            raise typer.BadParameter("RECORD must be a record ID")
        output(request(ctx.obj, "POST", f"/inspector/records/{record}/release",
                       json={"project": project, "reason": reason,
                             "worktree": str(worktree.expanduser().resolve()) if worktree else None}))

    @memory_app.command("diff")
    def memory_diff(ctx: typer.Context, base: str, head: str,
                    project_root: Annotated[Path, typer.Option(
                        "--project-root", help="The project's directory in the Git repository (default: here).")] = Path("."),
                    markdown: Annotated[bool, typer.Option(
                        "--markdown", help="A bounded Markdown comment for a pull request.")] = False,
                    max_chars: Annotated[int, typer.Option("--max-chars", min=2000, max=60000)] = 60000,
                    as_json: Annotated[bool, typer.Option("--json", help="The comparison as JSON.")] = False):
        """Compare the published memory of two commits, without a store, service or model.

        Exit 0 when the head's published memory would import, 1 when it would not, 2 on a usage error.
        The head is held to the secrets scan of `check-published`, with the head commit's
        .cairnmind/publication.toml; no secret is printed."""
        from cairnmind.cli import output, refuse
        from cairnmind.memory_published_diff import compare, render_markdown, render_text
        from cairnmind.memory_published_git import published_files
        from cairnmind.memory_published_release import commit_config_text
        from cairnmind.memory_published_secrets import SecretRulesError, load_rules
        root = project_root.expanduser().absolute()
        try:
            base_commit, base_files, base_errors = published_files(root, base)
            head_commit, head_files, head_errors = published_files(root, head)
            rules = load_rules(commit_config_text(root, head_commit))
        except (ValueError, SecretRulesError) as exc:
            refuse(str(exc), 2)
        # The base is only compared with: what of it can be read is used, and each entry left out is counted.
        report = (compare(base_files, head_files, rules) if not head_errors
                  else {"status": "refused", "errors": head_errors, "entries": [], "counts": [], "skipped": 0})
        if base_errors and not head_errors:
            if base_files:
                report["skipped"] += len(base_errors)
            else:
                report["base_unreadable"] = True
        if as_json:
            output({"base": base_commit, "head": head_commit, **report})
        elif markdown:
            typer.echo(render_markdown(report, base_commit, head_commit, max_chars), nl=False)
        else:
            typer.echo(render_text(report, base_commit, head_commit), nl=False)
        if report["status"] == "refused":
            raise typer.Exit(1)

    @memory_app.command("confirm-plan")
    @initialized
    def memory_confirm_plan(ctx: typer.Context, project: str, plan: str,
                            expected_version: Annotated[int, typer.Option("--expected-version", min=1)],
                            reason: str | None = None):
        """Confirm a plan and all its active steps as a person, so that they can be published."""
        from cairnmind.cli import output, refuse, request
        from cairnmind.config import session_value
        if session_value("SESSION_ID"):
            # A coding session's agent runs commands too; only a person confirms (the inspector works for both).
            refuse("A coding session cannot confirm a plan; a person confirms it outside the session or in the inspector.")
        from cairnmind.memory_published_format import canonical_uuid
        if not canonical_uuid(plan):
            raise typer.BadParameter("PLAN must be a plan ID")
        output(request(ctx.obj, "POST", f"/inspector/plans/{plan}/confirm",
                       json={"project": project, "expected_plan_version": expected_version, "reason": reason}))
