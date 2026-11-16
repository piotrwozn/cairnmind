"""`cairnmind record-kind`: the transcript record kinds a person accepted as technical for a project (#210)."""
from __future__ import annotations

from typing import Annotated

import typer

from cairnmind.cli_arguments import KindArg, KindClientArg, ProjectArg

record_kind_app = typer.Typer(help="Unknown transcript record kinds that block resume packets, and what a "
                                   "person accepted as technical for a project.")


def register(app, initialized):
    app.add_typer(record_kind_app, name="record-kind")

    @record_kind_app.command("list")
    def record_kind_list(ctx: typer.Context, project: ProjectArg):
        """The unknown kinds in the project's transcripts, each with one example, and what was accepted."""
        from cairnmind import record_kinds
        from cairnmind.cli import output, store_for_read
        store = store_for_read(ctx.obj)
        output(record_kinds.overview(store, store.resolve_project(project)["id"]))

    def act(ctx, project, client, kind, note, withdraw):
        from cairnmind import confirmation, record_kinds
        from cairnmind.cli import output, refuse, store_for
        if not confirmation.person_at_terminal():
            # A coding session's shell, the built-in agent's command or a script: not a person (#177).
            refuse(record_kinds.CLI_REFUSED)
        try:
            record_kinds.check(client, kind, note, withdraw=withdraw)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        store = store_for(ctx.obj)
        output(record_kinds.act_as_person(store, store.resolve_project(project)["id"], client, kind, note,
                                          route="cli", withdraw=withdraw))

    @record_kind_app.command("accept")
    @initialized
    def record_kind_accept(ctx: typer.Context, project: ProjectArg, client: KindClientArg, kind: KindArg,
                           note: Annotated[str | None, typer.Option(
                               "--note", help="Why this kind is technical; kept with the date.")] = None):
        """Treat KIND from CLIENT (claude, codex or unknown) as technical in this project, as a person in
        their own terminal. A malformed record of a kind the reader knows, and a transcript that fails its
        integrity checks, still block."""
        act(ctx, project, client, kind, note, False)

    @record_kind_app.command("withdraw")
    @initialized
    def record_kind_withdraw(ctx: typer.Context, project: ProjectArg, client: KindClientArg, kind: KindArg,
                             note: Annotated[str | None, typer.Option("--note", help="Why, kept with the date.")]
                             = None):
        """Withdraw an acceptance: packets of the project are blocked by that kind again."""
        act(ctx, project, client, kind, note, True)
