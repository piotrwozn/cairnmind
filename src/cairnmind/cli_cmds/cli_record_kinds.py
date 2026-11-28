"""`cairnmind record-kind`: the transcript record kinds a person accepted as technical for a project (#210)."""
from __future__ import annotations

from typing import Annotated

import typer

from cairnmind.cli_cmds.cli_arguments import KindArg, KindClientArg, ProjectArg

record_kind_app = typer.Typer(help="Unknown transcript record kinds that block resume packets, and what a "
                                   "person accepted as technical for a project.")


def register(app, initialized):
    app.add_typer(record_kind_app, name="record-kind")

    @record_kind_app.command("list")
    def record_kind_list(ctx: typer.Context, project: ProjectArg):
        """The unknown kinds in the project's transcripts, each with one example, and what was accepted."""
        from cairnmind.cli_cmds import io as _io
        from cairnmind.memory import record_kinds
        store = _io.store_for_read(ctx.obj)
        _io.output(record_kinds.overview(store, _io.resolve_project(store, project)["id"]))

    def act():
        from cairnmind.cli_cmds import io as _io
        from cairnmind.memory import record_kinds
        # The command line confirms nothing (#214). The arguments stay so that a script gets this sentence.
        _io.refuse(record_kinds.CLI_REFUSED)

    @record_kind_app.command("accept")
    @initialized
    def record_kind_accept(ctx: typer.Context, project: ProjectArg, client: KindClientArg, kind: KindArg,
                           note: Annotated[str | None, typer.Option(
                               "--note", help="Why this kind is technical; kept with the date.")] = None):
        """Refused since 0.13.0: a person does it in the inspector of the workspace."""
        act()

    @record_kind_app.command("withdraw")
    @initialized
    def record_kind_withdraw(ctx: typer.Context, project: ProjectArg, client: KindClientArg, kind: KindArg,
                             note: Annotated[str | None, typer.Option("--note", help="Why, kept with the date.")]
                             = None):
        """Refused since 0.13.0: a person does it in the inspector of the workspace."""
        act()
