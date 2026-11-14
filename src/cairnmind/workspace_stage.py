"""Run next stage and the plan stage map in the terminal workspace.

Run next stage asks the cockpit which stage the next plan step is in, then does exactly what
Start work or `cairnmind check run` would do for the mapped assistant or check. It never edits
a task, a plan or a session: the launched assistant still receives its resume packet and must
acknowledge it, and the task moves to its next state only when someone updates it.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from .terminal_notices import about
from .stages import (AGENT_CLIENTS, CLIENT_STAGES, CONFIRM_PROBLEMS, CONNECTION_PROBLEMS, LAUNCHABLE_CLIENTS, STAGES,
                     changed_since_confirmed, confirmed_choice, describe, stage_slot, validate_stage_map)

_UNSET = ":unset"


def check_argv(settings, decision: dict) -> list[str]:
    """The `cairnmind check run` invocation for the mapped profile: the one existing check path."""
    return [sys.executable, "-I", "-m", "cairnmind", "--data-dir", str(settings.data_dir), "check", "run",
            decision["project_id"], decision["check_profile"], "--task", decision["task"]["id"],
            "--worktree", decision["worktree"]]


def run_stage_check(settings, decision: dict, runner=subprocess.run) -> dict:
    """Run the check as the CLI does, in this process's own identity (its environment is inherited).

    The command checks a session's ACK when the caller is a session, records the run as local
    evidence and changes no task state. Its JSON output is the stored run.
    """
    completed = runner(check_argv(settings, decision), capture_output=True, text=True, stdin=subprocess.DEVNULL)
    try:
        run = json.loads(completed.stdout)
    except ValueError:
        run = None
    if not isinstance(run, dict):
        message = (completed.stderr or completed.stdout or "").strip()[-500:]
        return {"passed": False, "outcome": None, "error": message or "The check did not run."}
    return {key: run.get(key) for key in ("id", "passed", "outcome", "returncode", "reasons", "error")}


def check_summary(result: dict, translate) -> str:
    lines = [translate("Check passed." if result.get("passed") is True else "Check did not pass.")]
    if result.get("outcome"):
        lines.append(translate("Outcome") + ": " + str(result["outcome"]))
    for reason in (result.get("reasons") or [])[:10]:
        lines.append("- " + str(reason))
    if result.get("error"):
        lines.append(translate("Error") + ": " + str(result["error"]))
    if result.get("id"):
        lines.append(translate("Check run") + ": " + str(result["id"]))
    lines.append(translate("The task state is unchanged. Update it after you review the result."))
    return "\n".join(lines)


PROJECT_CHANGED = "Project changed. Run next stage was not carried out; press it again for this project."
CHECK_MOVED = ("Project changed while the check ran. Its result belongs to the earlier project and is listed "
               "under that project in Notices.")
ALREADY_RUNNING = ("This assistant is already running here. No process was restarted and it received no new "
                   "resume packet. For the next stage, end that session or map another assistant to the stage.")


CHECK_KEEPS_RUNNING = "The check keeps running"
CHECK_CONTINUES = "Its result will appear in Notices when it ends."
AGENT_STARTED = ("Stage started. Nothing is sent to the model until you write to the assistant in its panel; "
                 "it must read and acknowledge its resume packet before it changes anything.")
MAP_HINT = "Map stages in More → Plan stages."
PLAN_HINT = ("Activate a plan for this folder with cairnmind plan activate, or save a new one with "
             "cairnmind plan save --activate.")
MAP_PROBLEMS = ("not_mapped", "unknown_assistant", "unavailable_assistant")


def _hint(decision: dict) -> str | None:
    """What the person can do next. Mapping helps only when a stage lacks a usable mapping."""
    if decision.get("problem") == "no_task":
        return PLAN_HINT if decision.get("reason") == "no_active_plan" else None
    return MAP_HINT if decision.get("problem") in MAP_PROBLEMS else None


async def _shown(app, scope) -> bool:
    current = await asyncio.to_thread(app.workspace.read)
    return (current.get("project_id"), current.get("worktree")) == scope


def _outcome(app, profile, result=None, error=None) -> str:
    if error is not None:
        return app.t("The check could not run") + ": " + profile + " · " + str(error)
    verdict = "Check passed." if result.get("passed") is True else "Check did not pass."
    return (app.t("Check finished") + ": " + profile + " · " + app.t(verdict)
            + (" " + app.t("Outcome") + ": " + str(result["outcome"]) if result.get("outcome") else "")
            + (" · " + str(result["error"]) if result.get("error") else ""))


def _deliver(app, scope, subject, text, current=None) -> None:
    """On the notice line when the project is still shown; otherwise only in Notices, under its project."""
    if current is None:
        try:
            current = app.workspace.read()
        except (OSError, ValueError):
            current = {}
    if (current.get("project_id"), current.get("worktree")) == scope:
        app.notice_for(subject, text)
    else:
        app.notice_elsewhere(subject, text)


def _late_result(app, scope, profile, subject=None):
    """After the stage worker was cancelled: the outcome as a notice, never a dialog over other work.

    The check was recorded for the project it ran for. When another project is shown by then, the
    outcome still reaches Notices, tagged with its own project, and leaves the notice line alone.
    """
    def done(future):
        if future.cancelled():
            return
        error = future.exception()
        _deliver(app, scope, subject, _outcome(app, profile, None if error else future.result(), error))
    return done


async def run_next_stage(app):
    """One action: start the mapped assistant or run the mapped check for the next plan step.

    It runs in the same exclusive worker group as Start work, so choosing another project
    cancels it, and after every wait it drops its result when the workspace no longer shows
    the project and folder it was pressed for.
    """
    from .terminal_ui import MessageModal
    from .workspace import reusable_native_run
    project = app.chosen_project()
    scope = (app.workspace_data.get("project_id"), app.workspace_data.get("worktree"))
    subject = about(scope[0], project.get("name"), scope[1])  # what these notices are about, whatever is shown later
    if scope[0] != project["id"] or not scope[1]:
        raise ValueError(app.t("Select a visible project first."))
    decision = await app.request("GET", "/api/stage", params={"project": scope[0], "worktree": scope[1]})
    if not await _shown(app, scope):
        app.notice_for(subject, PROJECT_CHANGED)
        return
    text = describe(decision, app.t)
    if decision.get("action") == "start_slot":
        await app.load_state()
        if not await _shown(app, scope):
            app.notice_for(subject, PROJECT_CHANGED)
            return
        # The launcher returns a live process of this slot unchanged: no new session, no packet.
        # The built-in agent's launcher (start_agent) reuses a live run by the same identity.
        if reusable_native_run(app.data.get("runs", []), scope[0], scope[1], decision["client"], decision["slot_id"]):
            app.notice_for(subject, app.t(ALREADY_RUNNING) + "\n" + text)
            return
        # Exactly Start work for one assistant: the supervisor posts /api/client-runs once
        # and the launcher prepares a fresh resume packet that the assistant must ACK. For
        # the built-in agent and Copilot that launcher is start_agent, with the profile's
        # limits and credential, in a panel of this workspace.
        await asyncio.to_thread(app.workspace.select_project, project, scope[1], intent="work",
                                requested_slot_ids=[decision["slot_id"]])
        await asyncio.to_thread(app.workspace.update, {"focus": "client1"})
        await app.reconcile()
        current = await asyncio.to_thread(app.workspace.read)
        if (current.get("project_id"), current.get("worktree")) != scope:
            app.notice_for(subject, PROJECT_CHANGED)
            return
        failed = [row.get("code") for row in current.get("launch_errors", [])
                  if row.get("slot_id") == decision["slot_id"]]
        if failed:
            app.notice_for(subject, app.t("The assistant did not start") + ": " + ", ".join(map(str, failed)) + "\n" + text)
        elif current.get("last_error"):
            app.notice_for(subject, app.launch_notice(current))
        elif decision["client"] in AGENT_CLIENTS:
            app.notice_for(subject, app.t(AGENT_STARTED) + "\n" + text)
        else:
            app.notice_for(subject, app.t("Stage started. The assistant must acknowledge its resume packet.") + "\n" + text)
    elif decision.get("action") == "run_check":
        profile = decision["check_profile"]
        running = getattr(app, "_stage_check", None)
        if running is not None and not running[0].done():
            # One check at a time from here; an assistant stage is never held back by it.
            app.notice_for(subject, app.t("A check is still running") + ": " + running[1])
            return
        app.notice_for(subject, app.t("Running check") + ": " + profile)
        # The check process is not stopped when the worker is cancelled; it finishes and is recorded.
        future = asyncio.ensure_future(asyncio.to_thread(run_stage_check, app.settings, decision))
        app._stage_check = (future, profile + " · " + str(app.workspace_data.get("project_name") or project["name"])
                            + " · " + str(decision["task"]["id"]))
        try:
            result = await asyncio.shield(future)
        except asyncio.CancelledError:
            # Another click in the same group cancelled the worker. Opening another project from the
            # sidebar cancels it too, before the workspace switches, so the project may still look
            # the same here: the outcome reaches Notices under this project either way.
            future.add_done_callback(_late_result(app, scope, profile, subject))
            current = app.workspace.read()
            same = (current.get("project_id"), current.get("worktree")) == scope
            app.notice_for(subject, app.t(CHECK_KEEPS_RUNNING) + ": " + profile + " · " + app.t(CHECK_CONTINUES)
                           if same else CHECK_MOVED)
            raise
        current = await asyncio.to_thread(app.workspace.read)
        if (current.get("project_id"), current.get("worktree")) != scope:
            app.notice_for(subject, CHECK_MOVED)
            _deliver(app, scope, subject, _outcome(app, profile, result), current)
            return
        app.notice_for(subject, "")
        await app.push_screen_wait(MessageModal("Check result", text + "\n\n" + check_summary(result, app.t)))
    elif decision.get("problem") in CONNECTION_PROBLEMS + CONFIRM_PROBLEMS:
        # The text says what to do: fix the connection, or confirm the stage in Plan stages.
        await app.push_screen_wait(MessageModal("Run next stage", text))
    else:
        hint = _hint(decision)
        await app.push_screen_wait(MessageModal("Run next stage", text + ("\n\n" + app.t(hint) if hint else "")))


async def edit_stages(app):
    """Map implement and review to an assistant of this project and test to a check profile."""
    from .terminal_ui import ChoiceModal, Field, FormModal
    project_id = app.workspace_data.get("project_id")
    subject = about(project_id, app.workspace_data.get("project_name"))
    if not project_id:
        raise ValueError(app.t("Select a visible project first."))
    stored = app.data.get("settings", {}).get("project_stages", {})
    current = dict(stored.get(project_id, {}))
    profiles = {p["id"]: p for p in app.data.get("profiles", [])}
    slots = app.configured_slots(project_id)

    def shown(stage):
        target = current.get(stage)
        if not target:
            return app.t(stage) + " · " + app.t("Not set")
        if stage == "test":
            return app.t(stage) + " · " + target
        slot_id = stage_slot(target)
        slot = next((s for s in slots if s["id"] == slot_id), None)
        name = profiles.get(slot["profile_id"], {}).get("label") if slot else None
        text = app.t(stage) + " · " + (name or app.t("Unknown assistant")) + " (" + slot_id + ")"
        profile = profiles.get(slot["profile_id"]) if slot else None
        # The same comparison as Run next stage: profile, client and, for an agent, its provider,
        # model and address.
        if profile and changed_since_confirmed(target, slot, profile) != []:
            text += " · " + app.t("not confirmed; save it to confirm")
        return text

    def choice(slot):
        # What a stage stores: the slot and what it names at this moment.
        return confirmed_choice(slot, profiles[slot["profile_id"]])

    stage = await app.push_screen_wait(ChoiceModal("Plan stages", [(shown(s), s) for s in STAGES]))
    if stage is None:
        return
    if stage in CLIENT_STAGES:
        choices = [(profiles[s["profile_id"]]["label"] + " (" + s["id"] + ")", s["id"]) for s in slots
                   if profiles.get(s["profile_id"], {}).get("client") in LAUNCHABLE_CLIENTS]
        value = await app.push_screen_wait(ChoiceModal(app.t(stage), choices + [("Not set", _UNSET)]))
        if value is None:
            return
        if value != _UNSET:
            # Saving binds the stage to the assistant chosen now; a later change of the slot's
            # connection is shown, never started, until the stage is saved again.
            value = choice(next(s for s in slots if s["id"] == value))
    else:
        def valid(text):
            try:
                validate_stage_map({"test": text}) if text else None
            except ValueError as exc:
                return str(exc)
            return None
        form = await app.push_screen_wait(FormModal("Plan stages", [
            Field("profile", "Check profile for the test stage (empty: not set)", current.get("test", ""),
                  validator=valid)], note="The profile is read from this folder's checks.toml when it runs.",
            submit="Save"))
        if form is None:
            return
        value = form["profile"] or _UNSET
    if value == _UNSET:
        current.pop(stage, None)
    else:
        current[stage] = value
    validate_stage_map(current)
    mapping = {key: row for key, row in stored.items() if key != project_id}
    if current:
        mapping[project_id] = current
    await app.request("PUT", "/api/settings", json={"project_stages": mapping})
    await app.load_state()
    app.notice_for(subject, "Plan stages saved. Nothing was started.")
