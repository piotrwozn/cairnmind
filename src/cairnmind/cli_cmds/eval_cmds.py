"""CLI commands for evaluations, answer logs, and verdict cache."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from cairnmind.cli_cmds import io as _io
from cairnmind.cli_cmds.cli_arguments import (
    AnswerIdArg,
    ProjectArg,
    RepositoryArg,
)
from cairnmind.cli_cmds.io import initialized


def register(apps: dict[str, typer.Typer]) -> None:
    eval_app = apps["eval_app"]
    answers_app = apps["answers_app"]
    cache_app = apps["cache_app"]

    def _eval_vcr_client(settings, *, record: Path | None, replay: Path | None, suite: str):
        if record is not None and replay is not None:
            raise typer.BadParameter("Choose at most one of --record or --replay.")
        if replay is not None:
            from cairnmind.agents.vcr import VcrOllama
            try:
                return VcrOllama.replay(replay), None
            except ValueError as exc:
                raise typer.BadParameter(str(exc), param_hint="--replay") from None
        if record is not None:
            from cairnmind.agents.vcr import VcrOllama
            from cairnmind.memory.memory_models import from_settings
            client = VcrOllama.record(from_settings(settings), record, suite=suite)
            return client, client
        return None, None

    @eval_app.command("run")
    @initialized
    def eval_run(ctx: typer.Context, clients: bool = False, report_dir: Path | None = None,
                 skip_claude: bool = False,
                 record: Annotated[Path | None, typer.Option("--record", help="Record model responses to a VCR cassette JSON file.")] = None,
                 replay: Annotated[Path | None, typer.Option("--replay", help="Replay model responses offline from a VCR cassette JSON file.")] = None):
        """Run twenty isolated recall cases against local inference or a deterministic VCR cassette."""
        from cairnmind.agents.evaluation import run_evaluation
        from cairnmind.agents.vcr import CassetteMissError
        if skip_claude and not clients:
            raise typer.BadParameter("--skip-claude requires --clients")
        vcr_client, recorder = _eval_vcr_client(ctx.obj, record=record, replay=replay, suite="eval_run")
        if replay is None:
            _io._not_run_without_models(ctx.obj, "cairnmind eval run")
        _io._managed_only(ctx.obj, "`cairnmind eval run`")
        try:
            kwargs = {"ollama": vcr_client} if vcr_client is not None else {}
            result = run_evaluation(ctx.obj, clients=clients, report_dir=report_dir, skip_claude=skip_claude, **kwargs)
        except CassetteMissError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from None
        if recorder is not None:
            recorder.save()
        if replay is None:
            _io._record_evaluation(ctx.obj, "recall", result)
        _io.output(result)
        if result.get("status") == "failed":
            raise typer.Exit(1)
        if result.get("status") == "not_run":
            raise typer.Exit(3)

    @eval_app.command("memory")
    @initialized
    def eval_memory(ctx: typer.Context, report_dir: Path | None = None,
                    record: Annotated[Path | None, typer.Option("--record", help="Record model responses to a VCR cassette JSON file.")] = None,
                    replay: Annotated[Path | None, typer.Option("--replay", help="Replay model responses offline from a VCR cassette JSON file.")] = None):
        """Explicitly check extraction, transfer and restoration of memory with local Ollama or a VCR cassette."""
        from cairnmind.agents.memory_evaluation import run_memory_evaluation
        from cairnmind.agents.vcr import CassetteMissError
        vcr_client, recorder = _eval_vcr_client(ctx.obj, record=record, replay=replay, suite="eval_memory")
        if replay is None:
            _io._not_run_without_models(ctx.obj, "cairnmind eval memory")
        _io._managed_only(ctx.obj, "`cairnmind eval memory`")
        try:
            kwargs = {"ollama": vcr_client} if vcr_client is not None else {}
            result = run_memory_evaluation(ctx.obj, report_dir=report_dir, **kwargs)
        except CassetteMissError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from None
        if recorder is not None:
            recorder.save()
        if replay is None:
            _io._record_evaluation(ctx.obj, "memory", result)
        _io.output(result)
        if result.get("status") != "passed":
            raise typer.Exit(3 if result.get("status") == "not_run" else 1)

    @eval_app.command("extraction")
    @initialized
    def eval_extraction(ctx: typer.Context, transcripts: Path | None = None, runs: int = 3,
                        report_dir: Path | None = None,
                        record: Annotated[Path | None, typer.Option("--record", help="Record model responses to a VCR cassette JSON file.")] = None,
                        replay: Annotated[Path | None, typer.Option("--replay", help="Replay model responses offline from a VCR cassette JSON file.")] = None):
        """Measure what extraction proposes from conversations against the records a person would keep.

        TRANSCRIPTS defaults to docs/evaluation/extraction-transcripts.json of the current directory. Needs Ollama with the configured chat model (or --replay CASSETTE), no engine; nothing is stored. Exits 3 without the model, or 1 on cassette miss or failure.
        """
        from cairnmind.agents.extraction_evaluation import run_extraction_evaluation
        from cairnmind.agents.vcr import CassetteMissError
        vcr_client, recorder = _eval_vcr_client(ctx.obj, record=record, replay=replay, suite="eval_extraction")
        if replay is None:
            _io._not_run_without_models(ctx.obj, "cairnmind eval extraction")
        path = transcripts or Path("docs") / "evaluation" / "extraction-transcripts.json"
        try:
            kwargs = {"ollama": vcr_client} if vcr_client is not None else {}
            result = run_extraction_evaluation(ctx.obj, path, runs=runs, report_dir=report_dir, **kwargs)
        except CassetteMissError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from None
        except ValueError as exc:
            _io.refuse(_io._shortened(str(exc), 1000))  # the file or the log, not the command line (#244)
        if recorder is not None:
            recorder.save()
        _io.output({key: value for key, value in result.items() if key != "runs"}
               | {"runs": [{"run": run["run"], **run["summary"], "failures": run["failures"]} for run in result["runs"]]})
        if result.get("status") != "passed":
            raise typer.Exit(3 if result.get("status") == "not_run" else 1)

    @eval_app.command("repository")
    @initialized
    def eval_repository(ctx: typer.Context, root: RepositoryArg,
                        questions: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
                        report_dir: Path | None = None,
                        record: Annotated[Path | None, typer.Option("--record", help="Record model responses to a VCR cassette JSON file.")] = None,
                        replay: Annotated[Path | None, typer.Option("--replay", help="Replay model responses offline from a VCR cassette JSON file.")] = None):
        """Ask questions with known answers about a real repository, in a throwaway copy and database.

        QUESTIONS defaults to ROOT/docs/evaluation/repository-questions.json. Needs Ollama with the configured models (or --replay CASSETTE) and a running engine; exits 3 when the models are not available, or 1 on cassette miss or failure.
        """
        from cairnmind.agents.repository_evaluation import run_repository_evaluation
        from cairnmind.agents.vcr import CassetteMissError
        vcr_client, recorder = _eval_vcr_client(ctx.obj, record=record, replay=replay, suite="eval_repository")
        if replay is None:
            _io._not_run_without_models(ctx.obj, "cairnmind eval repository")
        _io._managed_only(ctx.obj, "`cairnmind eval repository`")
        path = questions or Path(root) / "docs" / "evaluation" / "repository-questions.json"
        try:
            kwargs = {"ollama": vcr_client} if vcr_client is not None else {}
            result = run_repository_evaluation(ctx.obj, root, path, report_dir=report_dir, **kwargs)
        except CassetteMissError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from None
        except ValueError as exc:
            _io.refuse(_io._shortened(str(exc), 1000))  # the file or the log, not the command line (#244)
        if recorder is not None:
            recorder.save()
        if replay is None:
            _io._record_evaluation(ctx.obj, "repository", result)
        _io.output(result)
        if result.get("status") != "passed":
            raise typer.Exit(3 if result.get("status") == "not_run" else 1)

    @eval_app.command("injection")
    @initialized
    def eval_injection(ctx: typer.Context, fixture: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
                       runs: int = 3, report_dir: Path | None = None):
        """Measure how often planted instructions in indexed sources are obeyed, in a throwaway database.

        FIXTURE defaults to docs/evaluation/injection-fixture.json of the current directory. Needs Ollama with the configured models and a running engine. It only measures: exit 0 when measured, 3 when not run, 2 when the fixture or an option is refused, 1 when it failed (an invariant broke, a path could not be measured, an error occurred).
        """
        from cairnmind.agents.injection_evaluation import run_injection_evaluation
        _io._not_run_without_models(ctx.obj, "cairnmind eval injection")
        _io._managed_only(ctx.obj, "`cairnmind eval injection`")
        path = fixture or Path("docs") / "evaluation" / "injection-fixture.json"
        try:
            result = run_injection_evaluation(ctx.obj, path, runs=runs, report_dir=report_dir)
        except ValueError as exc:
            _io.refuse(_io._shortened(str(exc), 1000))  # the file or the log, not the command line (#244)
        _io.output({key: value for key, value in result.items() if key != "details"})
        if result.get("status") != "measured":
            raise typer.Exit(3 if result.get("status") == "not_run" else 1)

    @eval_app.command("replay")
    @initialized
    def eval_replay(ctx: typer.Context, project: ProjectArg,
                    limit: Annotated[int, typer.Option("--limit", help="How many distinct logged questions, newest first.")] = 20,
                    report_dir: Path | None = None):
        """Ask the logged questions of a project again and report answers that changed status or lost sources.

        Compares the status and the set of cited source addresses, not the wording. Needs the service and its model; exits 1 when an answer regressed or lost sources, 3 when nothing was logged.
        """
        from cairnmind.agents.answer_log import replay
        _io._not_run_without_models(ctx.obj, "cairnmind eval replay")
        pid = _io.resolve_project(_io.store_for_read(ctx.obj), project)["id"]
        try:
            # Asked for the logged scope, not the session's, and a refusal is kept as its reason in the report.
            result = replay(ctx.obj, pid, lambda payload: _io.request(ctx.obj, "POST", "/ask", json=payload, session=False,
                                                                  raise_refusal=True),
                            limit=limit, report_dir=report_dir)
        except ValueError as exc:
            _io.refuse(_io._shortened(str(exc), 1000))  # the file or the log, not the command line (#244)
        _io.output(result)
        if result["status"] != "passed":
            raise typer.Exit(3 if result["status"] == "not_run" else 1)

    @answers_app.command("list")
    def answers_list(ctx: typer.Context, project: ProjectArg,
                     limit: Annotated[int, typer.Option("--limit", help="How many answers, newest first.")] = 20):
        """List the logged answers of a project, newest first. Asks no model and writes nothing."""
        from cairnmind.agents.answer_log import listing
        pid = _io.resolve_project(_io.store_for_read(ctx.obj), project)["id"]
        try:
            _io.output(listing(ctx.obj, pid, limit))
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None

    @answers_app.command("show")
    def answers_show(ctx: typer.Context, project: ProjectArg, answer_id: AnswerIdArg):
        """Show one logged answer with its question, citations, code state, model and timings."""
        from cairnmind.agents.answer_log import find
        pid = _io.resolve_project(_io.store_for_read(ctx.obj), project)["id"]
        try:
            _io.output(find(ctx.obj, pid, answer_id))
        except LookupError as exc:
            # An unknown id is said as an unknown task or plan is: "Cairnmind: ...", exit 1 (#244).
            raise ValueError(str(exc)) from None
        except ValueError as exc:  # an id of the wrong form
            _io.refuse(str(exc))

    @answers_app.command("prune")
    def answers_prune(ctx: typer.Context, project: ProjectArg,
                      keep: Annotated[int, typer.Option("--keep", min=0, help="How many of the newest answers to keep; "
                                                        "0 deletes the log.")]):
        """Keep only the newest logged answers of a project."""
        from cairnmind.agents.answer_log import prune
        pid = _io.resolve_project(_io.store_for_read(ctx.obj), project)["id"]
        _io.output(prune(ctx.obj, pid, keep))

    @cache_app.command("clear")
    def cache_clear(ctx: typer.Context):
        """Clear all cached verification verdicts without starting the engine or service."""
        from cairnmind.agents.verdict_cache import VerdictCache
        _io.output(VerdictCache(ctx.obj.data_dir).clear())

    @cache_app.command("status")
    def cache_status(ctx: typer.Context):
        """Show the size and entry count of the local verification verdict cache. Read-only."""
        from cairnmind.agents.verdict_cache import VerdictCache
        _io.output(VerdictCache(ctx.obj.data_dir).stats())
