"""`cairnmind importer`: manage external transcript converter commands outside coding sessions (#127)."""
from __future__ import annotations

from typing import Annotated

import typer

from cairnmind.cli_arguments import (
    ImporterArgsArg,
    ImporterCommandArg,
    ImporterNameArg,
)

importer_app = typer.Typer(
    help="Register external transcript converter commands (person-only, outside coding sessions).",
)


def register(app: typer.Typer) -> None:
    app.add_typer(importer_app, name="importer")

    @importer_app.command("add")
    def importer_add(
        ctx: typer.Context,
        name: ImporterNameArg,
        command: ImporterCommandArg,
        args: ImporterArgsArg = None,
        timeout_seconds: Annotated[
            int,
            typer.Option("--timeout", min=1, max=3600, help="Subprocess timeout in seconds."),
        ] = 120,
        output_limit_bytes: Annotated[
            int,
            typer.Option("--output-limit", min=1, max=64 * 1024 * 1024, help="Combined stdout/stderr limit in bytes."),
        ] = 10 * 1024 * 1024,
        replace: Annotated[
            bool,
            typer.Option("--replace", help="Overwrite an existing converter under this name."),
        ] = False,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Print the saved converter entry as JSON."),
        ] = False,
    ):
        """Register an out-of-process transcript converter command on this machine."""
        from cairnmind.cli import _human_or_json, output, refuse
        from cairnmind.config import Settings
        from cairnmind.importers import ImporterRefused, add_importer, require_outside_session

        try:
            require_outside_session()
            ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
            entry = add_importer(
                ctx.obj.data_dir,
                name,
                [command, *(args or [])],
                timeout_seconds=timeout_seconds,
                output_limit_bytes=output_limit_bytes,
                replace=replace,
            )
        except ImporterRefused as exc:
            refuse(str(exc))
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None

        if as_json:
            output(entry)
        else:
            _human_or_json(
                entry,
                False,
                lambda: [f"Registered importer '{entry['name']}': {' '.join(entry['argv'])}"],
            )

    @importer_app.command("remove")
    def importer_remove(
        ctx: typer.Context,
        name: ImporterNameArg,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Print the removal summary as JSON."),
        ] = False,
    ):
        """Remove a registered converter command; converted history already stored stays as degraded sources."""
        from cairnmind.cli import _human_or_json, output, refuse
        from cairnmind.config import Settings
        from cairnmind.importers import ImporterRefused, remove_importer, require_outside_session

        try:
            require_outside_session()
            ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
            result = remove_importer(ctx.obj.data_dir, name)
        except ImporterRefused as exc:
            refuse(str(exc))
        except ValueError as exc:
            refuse(str(exc))

        if as_json:
            output(result)
        else:
            _human_or_json(result, False, lambda: [f"Removed importer '{result['name']}'."])

    @importer_app.command("list")
    def importer_list(
        ctx: typer.Context,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Print registered converters as JSON."),
        ] = False,
    ):
        """List registered external transcript converters on this machine."""
        from cairnmind.cli import _human_or_json, output, refuse
        from cairnmind.config import Settings
        from cairnmind.importers import ImporterRefused, list_importers, require_outside_session

        try:
            require_outside_session()
            ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
            items = list_importers(ctx.obj.data_dir)
        except ImporterRefused as exc:
            refuse(str(exc))
        except ValueError as exc:
            refuse(str(exc))

        payload = {"importers": items}
        if as_json:
            output(payload)
        else:
            def sentences():
                if not items:
                    return ["No external transcript importers registered."]
                return [
                    f"{row['name']}: {' '.join(row['argv'])} "
                    f"(timeout={row['timeout_seconds']}s, limit={row['output_limit_bytes']}B)"
                    for row in items
                ]

            _human_or_json(payload, False, sentences)
