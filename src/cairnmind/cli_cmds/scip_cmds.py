"""CLI commands for multi-language SCIP code indexing."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from cairnmind.cli_cmds.cli_arguments import (
    ProjectArg,
)
from cairnmind.cli_cmds import io as _io
from cairnmind.cli_cmds.io import initialized


def register(apps: dict[str, typer.Typer]) -> None:
    scip_app = apps["scip_app"]

    @scip_app.command("status")
    def scip_status_cmd(
        ctx: typer.Context,
        project: Annotated[
            str | None,
            typer.Argument(metavar="[PROJECT]", help="Optional project name or id to detect languages in."),
        ] = None,
        root: Annotated[Path | None, typer.Option("--root", help="Optional folder path to detect SCIP languages in.")] = None,
    ):
        """Show installed SCIP indexers, host toolchains, and detected languages. Read-only."""
        from cairnmind.scip.scip_manager import scip_status
        target_root = root
        if project and target_root is None:
            store = _io.store_for_read(ctx.obj)
            target_root = Path(store.resolve_project(project)["root"])
        _io.output(scip_status(ctx.obj, target_root))

    @scip_app.command("install")
    @initialized
    def scip_install_cmd(
        ctx: typer.Context,
        targets: Annotated[
            list[str] | None,
            typer.Argument(
                metavar="[TARGETS]...",
                help="Languages (go, rust, java, kotlin, c, cpp, csharp, php, ruby) or indexer IDs. Omit for all.",
            ),
        ] = None,
    ):
        """Download and verify pinned SCIP indexers into Cairnmind's private runtime directory."""
        from cairnmind.scip.scip_manager import ScipError, ensure_indexer
        from cairnmind.scip.scip_registry import INDEXERS, LANGUAGE_TO_INDEXER

        if not targets:
            indexer_ids = list(INDEXERS.keys())
        else:
            indexer_ids = []
            for item in targets:
                key = item.strip().lower()
                mapped = key if key in INDEXERS else LANGUAGE_TO_INDEXER.get(key)
                if not mapped:
                    _io.refuse(f"Unknown SCIP language or indexer: {item!r}")
                if mapped not in indexer_ids:
                    indexer_ids.append(mapped)

        results = {}
        failed = False
        for indexer_id in indexer_ids:
            try:
                installed = ensure_indexer(ctx.obj, indexer_id, allow_download=True)
                results[indexer_id] = installed.to_dict()
            except (ScipError, ValueError, OSError) as exc:
                failed = True
                results[indexer_id] = {"indexer_id": indexer_id, "status": "install_failed", "error": str(exc)}
        _io.output({"indexers": results})
        if failed:
            raise typer.Exit(1)

    @scip_app.command("index")
    @initialized
    def scip_index_cmd(
        ctx: typer.Context,
        project: ProjectArg,
        lang: Annotated[
            list[str] | None,
            typer.Option("--lang", help="Limit indexing to specific language(s). Repeatable."),
        ] = None,
        worktree: Annotated[Path | None, typer.Option("--worktree", help="Registered project folder to index.")] = None,
        no_download: Annotated[
            bool, typer.Option("--no-download", help="Use only already-installed indexers; do not download.")
        ] = False,
    ):
        """Run SCIP indexer(s) for a project and update its ArcadeDB code graph immediately."""
        from cairnmind.scip.scip_ingest import index_project_scip
        store = _io.store_for(ctx.obj)
        result = index_project_scip(
            store,
            ctx.obj,
            project,
            languages=lang or None,
            worktree=worktree,
            allow_download=not no_download,
        )
        _io.output(result)
        if result.get("errors"):
            raise typer.Exit(1)

    @scip_app.command("import")
    @initialized
    def scip_import_cmd(
        ctx: typer.Context,
        project: ProjectArg,
        index_file: Annotated[
            Path,
            typer.Argument(
                metavar="INDEX_FILE",
                exists=True,
                dir_okay=False,
                readable=True,
                help="Path to an externally or CI-generated binary SCIP index (index.scip).",
            ),
        ],
        root: Annotated[Path | None, typer.Option("--root", help="Registered project folder the SCIP paths are relative to.")] = None,
    ):
        """Import an externally generated index.scip into a project's ArcadeDB code graph."""
        from cairnmind.scip.scip_ingest import apply_scip_index
        store = _io.store_for(ctx.obj)
        proj = store.resolve_project(project)
        target_root = root if root is not None else Path(proj["root"])
        result = apply_scip_index(store, proj["id"], target_root, index_file.expanduser().resolve())
        _io.output(result)
