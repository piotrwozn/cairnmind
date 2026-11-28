"""CLI commands for tasks, plans, checks, and stage routing."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Annotated, Any

import typer

from cairnmind.cli_cmds import io as _io
from cairnmind.cli_cmds.cli_arguments import (
    GroupArg,
    PlanIdArg,
    ProfileArg,
    ProjectArg,
    StateArg,
    TaskIdArg,
    TitleArg,
)
from cairnmind.cli_cmds.io import initialized
from cairnmind.config import Settings


def register(apps: dict[str, typer.Typer]) -> None:
    tasks_app = apps["tasks_app"]
    plans_app = apps["plans_app"]
    checks_app = apps["checks_app"]
    stage_app = apps["stage_app"]

    @tasks_app.command("list")
    def task_list(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None):
        from cairnmind.memory.verification import verification_status
        store = _io.store_for_read(ctx.obj)

        def read():
            pid = _io.resolve_project(store, project)["id"]
            scope = _io.actor_scope(store, pid, worktree)
            tasks = [r for r in store.list_records(pid) if r["kind"] == "task"]
            return pid, [{**task, "verification": verification_status(store, task, scope)} for task in tasks]

        _io.output(store.read_consistently(read, _io.READ_KINDS["task list"]))

    @tasks_app.command("add")
    @initialized
    def task_add(ctx: typer.Context, project: ProjectArg, title: TitleArg, plan: str | None = None, step: int = 1,
                 criterion: Annotated[list[str] | None, typer.Option("--criterion")] = None,
                 depends_on: Annotated[list[str] | None, typer.Option("--depends-on")] = None,
                 requires_test: bool = False, requires_review: bool = False):
        store = _io.store_for(ctx.obj)
        pid = _io.resolve_project(store, project)["id"]
        metadata = {"state": "todo", "plan_id": plan, "step_index": step,
                    "acceptance_criteria": criterion or [], "depends_on": depends_on or [],
                    "requires_test": requires_test, "requires_review": requires_review}
        text = json.dumps({"title": title, **metadata}, ensure_ascii=False)
        from cairnmind.memory import confirmation
        # The command line confirms nothing (#214): the task is stored as proposed, and the command says so.
        with _io.actor_write(store, ctx.obj, pid):
            from cairnmind.memory.secret_mask import (
                note_metadata,  # indexed with its known secrets replaced (#182)
            )
            src = store.snapshot(pid, f"manual://task/{uuid.uuid4()}", text, "manual",
                                 note_metadata(store.settings, text, {"role": "user"}))
            result = store.remember(pid, {"kind": "task", "content": title, "status": "proposed", "metadata": metadata},
                                    [src["id"]])
        typer.echo(confirmation.CLI_TASK_PROPOSED, err=True)
        _io.output(result)

    @tasks_app.command("update")
    @initialized
    def task_update(ctx: typer.Context, project: ProjectArg, task_id: TaskIdArg, state: StateArg,
                    evidence: Path | None = None, test_passed: bool = False,
                    review_passed: bool = False, acceptance_passed: bool = False,
                    version: int | None = None, worktree: Path | None = None,
                    plan_version: int | None = None,
                    check_run: Annotated[list[str] | None, typer.Option("--check-run")] = None):
        store = _io.store_for(ctx.obj)
        pid = _io.resolve_project(store, project)["id"]
        task = next((r for r in store.list_records(pid) if r["id"] == task_id and r["kind"] == "task"), None)
        if task is None:
            raise ValueError("Task does not exist in this project")  # as the store says a missing row (#244)
        sources = task["source_ids"]
        if isinstance(sources, str):
            sources = json.loads(sources)
        metadata: dict[str, Any] = {"state": state}
        if check_run:
            metadata["check_run_ids"] = check_run
        evidence_text = None
        if test_passed or review_passed or acceptance_passed:
            if evidence is None:
                raise typer.BadParameter("Verification requires --evidence FILE with the check result")
            evidence_text = evidence.read_text(encoding="utf-8")
            if not evidence_text.strip():
                _io.refuse("The evidence file must contain a non-empty check result")
        with _io.actor_write(store, ctx.obj, pid, worktree) as scope:
            if evidence_text is not None and evidence is not None:
                src = store.snapshot(pid, f"verification://{uuid.uuid4()}", evidence_text, "verification",
                                     {"path": str(evidence.resolve()), "role": "user", "reported": True})
                sources = [*sources, src["id"]]
                for key, passed in (("test", test_passed), ("review", review_passed), ("acceptance", acceptance_passed)):
                    if passed:
                        metadata[f"{key}_passed"] = True
                        metadata[f"{key}_source_ids"] = [src["id"]]
            record = {"id": task_id, "kind": "task", "content": task["content"], "status": task["status"],
                      "metadata": metadata, "worktree": scope,
                      "expected_version": version if version is not None else task["version"]}
            plan_ref = task.get("metadata", {}).get("plan_ref")
            if plan_ref:
                from cairnmind.memory.plans import PlanManager
                current_plan = PlanManager(store).get(pid, plan_ref)["plan"]
                record["expected_plan_version"] = plan_version if plan_version is not None else current_plan["version"]
            result = store.remember(pid, record, sources)
        _io.output(result)

    @plans_app.command("save")
    @initialized
    def plan_save(ctx: typer.Context,
                  project: Annotated[str, typer.Argument(metavar="PROJECT", help="The project's name, id or folder.")],
                  input_json: Annotated[Path, typer.Argument(
                      metavar="INPUT_JSON", help="A JSON file with plan and steps; the format is in docs/WORKFLOW.md.")],
                  idempotency_key: Annotated[str, typer.Option(
                      "--idempotency-key", help="Any text naming this save. The same key with the same file returns "
                                                "the earlier result instead of saving twice; with another file it is "
                                                "refused.")],
                  expected_version: Annotated[int | None, typer.Option(
                      help="The plan version you edited; the save is refused if it changed since.")] = None,
                  task_versions: Annotated[Path | None, typer.Option(
                      help="A JSON file mapping task ids to the versions you saw.")] = None,
                  worktree: Annotated[Path | None, typer.Option(help="The registered worktree the plan is for.")] = None,
                  activate: Annotated[bool, typer.Option(help="Make the saved plan the active one.")] = False):
        """Save a new version of a plan from a JSON file; --activate makes it the active plan."""
        from cairnmind.memory.plans import PlanManager
        document = _io._json_file(input_json)
        if not isinstance(document, dict):
            _io.refuse("The file must contain an object with the fields plan and steps")
        versions = (_io._json_file(task_versions) if task_versions
                    else document.get("task_versions"))
        store = _io.store_for(ctx.obj)
        pid = _io.resolve_project(store, project)["id"]
        # A save is never a person's act (#214): the store refuses one that changes or removes a step a person
        # confirmed, with the step named.
        with _io.actor_write(store, ctx.obj, pid, worktree) as scope:
            result = PlanManager(store).save(pid, document.get("plan", {}), document.get("steps", []),
                idempotency_key, expected_version if expected_version is not None else document.get("expected_version"),
                versions, scope, activate)
        _io.output(result)

    @plans_app.command("list")
    def plan_list(ctx: typer.Context, project: ProjectArg):
        from cairnmind.memory.plans import PlanManager
        store = _io.store_for_read(ctx.obj)

        def read():
            pid = _io.resolve_project(store, project)["id"]
            return pid, PlanManager(store).list(pid)

        _io.output(store.read_consistently(read, _io.READ_KINDS["plan list"]))

    @plans_app.command("show")
    def plan_show(ctx: typer.Context, project: ProjectArg, plan_id: PlanIdArg):
        from cairnmind.memory.plans import PlanManager
        store = _io.store_for_read(ctx.obj)

        def read():
            pid = _io.resolve_project(store, project)["id"]
            return pid, PlanManager(store).get(pid, plan_id)

        _io.output(store.read_consistently(read, _io.READ_KINDS["plan show"]))

    @plans_app.command("next")
    def plan_next(ctx: typer.Context, project: ProjectArg, plan_id: str | None = None, worktree: Path | None = None):
        from cairnmind.memory.plans import PlanManager
        store = _io.store_for_read(ctx.obj)

        def read():
            pid = _io.resolve_project(store, project)["id"]
            scope = _io.actor_scope(store, pid, worktree)
            result = PlanManager(store).next_task(pid, plan_id, scope)
            if result.get("task"):
                from cairnmind.memory.scope import drift_warning
                from cairnmind.memory.verification import verification_status
                result["verification"] = verification_status(store, result["task"], scope)
                warning = drift_warning(result["verification"].get("scope_drift"))
                if warning:
                    result["warnings"] = [warning]
            return pid, result

        _io.output(store.read_consistently(read, _io.READ_KINDS["plan next"]))

    @plans_app.command("activate")
    @initialized
    def plan_activate(ctx: typer.Context, project: ProjectArg, plan_id: PlanIdArg, worktree: Path | None = None):
        from cairnmind.memory.plans import PlanManager
        store = _io.store_for(ctx.obj)
        pid = _io.resolve_project(store, project)["id"]
        with _io.actor_write(store, ctx.obj, pid, worktree) as scope:
            result = PlanManager(store).activate(pid, plan_id, scope)
        _io.output(result)

    @plans_app.command("adopt")
    @initialized
    def plan_adopt(ctx: typer.Context, project: ProjectArg, group: GroupArg,
                   idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
                   title: str | None = None, worktree: Path | None = None, activate: bool = False):
        from cairnmind.memory.plans import PlanManager
        store = _io.store_for(ctx.obj)
        pid = _io.resolve_project(store, project)["id"]
        # Adoption gives each task a plan key: a task a person confirmed is refused here; the person adopts that group
        # in the workspace (#214, #305).
        with _io.actor_write(store, ctx.obj, pid, worktree) as scope:
            result = PlanManager(store).adopt_legacy(pid, group, plan={"title": title} if title else None,
                idempotency_key=idempotency_key, worktree=scope, activate=activate)
        _io.output(result)

    @plans_app.command("contract")
    def plan_contract(ctx: typer.Context, project: ProjectArg,
                      plan_id: Annotated[str | None, typer.Option("--plan-id", help="The plan; the active one if omitted.")] = None,
                      worktree: Path | None = None,
                      write: Annotated[str | None, typer.Option(
                          "--write", metavar="CODE",
                          help="Add the proposed check profiles; CODE is printed with the diff they were shown in.")] = None,
                      as_json: Annotated[bool, typer.Option("--json", help="Print the contract as JSON.")] = False):
        """Show each acceptance criterion of a plan with the check that proves it. Runs no command."""
        from cairnmind.memory.contract import build_contract, write_contract
        if write:
            # Writing the profile file prepares the store as every writing command does.
            ctx.obj = Settings.load(ctx.obj.data_dir, create=True)
        store = _io.store_for(ctx.obj) if write else _io.store_for_read(ctx.obj)

        def run_contract():
            pid = _io.resolve_project(store, project)["id"]
            scope = _io.actor_scope(store, pid, worktree)
            return pid, (write_contract(store, pid, write, plan_id, scope) if write
                         else build_contract(store, pid, plan_id, scope))

        # Repeated only for a read-only store, never for --write: Store(path) runs it once.
        result = store.read_consistently(run_contract, _io.READ_KINDS["plan contract"])
        if as_json:
            _io.output(result)
        else:
            typer.echo(result["text"])

    @checks_app.command("profiles")
    def check_profiles(ctx: typer.Context, project: ProjectArg, worktree: Path | None = None):
        from cairnmind.memory.verification import load_profiles
        store = _io.store_for_read(ctx.obj)

        def read():
            pid = _io.resolve_project(store, project)["id"]
            return pid, load_profiles(store, pid, _io.actor_scope(store, pid, worktree))

        _io.output(store.read_consistently(read, _io.READ_KINDS["check profiles"]))

    @checks_app.command("run")
    @initialized
    def check_run(ctx: typer.Context, project: ProjectArg, profile: ProfileArg,
                  task: Annotated[str, typer.Option("--task")], worktree: Path | None = None):
        from cairnmind.memory.verification import run_check
        store = _io.store_for(ctx.obj)
        pid = _io.resolve_project(store, project)["id"]
        with _io.actor_write(store, ctx.obj, pid, worktree) as scope:
            pass  # Validate receipt first; do not hold the database lock while executing a program.
        result = run_check(store, ctx.obj, pid, profile, task, scope)
        _io.output(result)
        if not result.get("passed"):
            raise typer.Exit(1)

    @stage_app.command("next")
    @initialized
    def stage_next(ctx: typer.Context, project: ProjectArg, plan_id: str | None = None, worktree: Path | None = None,
                   as_json: Annotated[bool, typer.Option("--json", help="Print the decision as JSON.")] = False):
        """Show the next plan step's stage and what is mapped to it. Changes no task and starts nothing."""
        from cairnmind.agents.api_runtime import cli_stage_readiness
        from cairnmind.memory.plans import PlanManager
        from cairnmind.memory.verification import declared_check_names
        from cairnmind.pal.terminal_safe import safe_text
        from cairnmind.terminal.cockpit_state import CockpitState
        from cairnmind.terminal.stages import (
            FROM_WORKSPACE,
            check_declared,
            describe,
            next_command,
            project_routing,
            resolve_stage,
        )
        store = _io.store_for(ctx.obj)
        selected = _io.resolve_project(store, project)
        scope = _io.actor_scope(store, selected["id"], worktree)
        plans = PlanManager(store)
        stages, slots, profiles = project_routing(CockpitState(ctx.obj).load(), selected["id"])
        decision = resolve_stage(plans.next_task(selected["id"], plan_id, scope), stages, slots, profiles,
                                 readiness=cli_stage_readiness(ctx.obj))
        decision = check_declared(decision, declared_check_names(scope or selected["root"]))
        active = decision["plan"] if plan_id is None else plans.next_task(selected["id"], None, scope)["plan"]
        decision["command"], decision["command_reason"] = next_command(
            decision, _io._project_handle(store, selected), scope or str(Path(selected["root"]).resolve()),
            slots=slots, profiles=profiles, active_plan_id=(active or {}).get("id"))
        if decision["command"]:
            # The printed command acts on this installation, not on the default one (#244).
            decision["command"][1:1] = _io._data_dir_arguments(ctx.obj.data_dir)
        if as_json:
            _io.output(decision)
            return
        typer.echo(safe_text(describe(decision)))
        if decision["command"]:
            from cairnmind.pal.process_control import format_command
            typer.echo("Next: " + safe_text(format_command(decision["command"])))
        elif decision["command_reason"]:
            typer.echo(FROM_WORKSPACE)
        from cairnmind.terminal.workspace_stage import MAP_HINT, MAP_PROBLEMS
        if decision.get("problem") in MAP_PROBLEMS:
            typer.echo(MAP_HINT)  # the remedy the workspace gives for the same decision

    @checks_app.command("list")
    @initialized
    def check_list(ctx: typer.Context, project: ProjectArg,
                   task: Annotated[str | None, typer.Option("--task")] = None,
                   worktree: Path | None = None):
        from cairnmind.memory.verification import recover_interrupted_runs, verification_status
        store = _io.store_for(ctx.obj)
        pid = _io.resolve_project(store, project)["id"]
        scope = _io.actor_scope(store, pid, worktree)
        recover_interrupted_runs(store, ctx.obj)
        from cairnmind.storage.storage_query import asc, db_find, db_find_one, desc
        check_where = {"project_id": pid}
        if task:
            check_where["task_id"] = task
        if scope:
            check_where["worktree"] = scope
        rows = db_find(
            store,
            "check_runs",
            check_where,
            order_by=[desc("ended_at"), asc("id")],
            limit=100,
        )
        result: dict[str, Any] = {"checks": [{key: value for key, value in row.items() if not key.startswith("@")} for row in rows]}
        if task:
            record = db_find_one(store, "records", {"id": task, "project_id": pid, "kind": "task"})
            if not record:
                raise ValueError("Task does not exist in this project")  # as the store says a missing row (#244)
            result["verification"] = verification_status(store, record, scope)
        _io.output(result)
