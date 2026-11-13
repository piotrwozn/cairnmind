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

from .stages import AGENT_CLIENTS, CLIENT_STAGES, CONNECTION_PROBLEMS, LAUNCHABLE_CLIENTS, STAGES, describe, validate_stage_map

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
CHECK_MOVED = ("Project changed while the check ran. Its result was recorded for the earlier project "
               "and is not shown here.")
ALREADY_RUNNING = ("This assistant is already running here. No process was restarted and it received no new "
                   "resume packet. For the next stage, end that session or map another assistant to the stage.")


CHECK_CONTINUES = "The check keeps running; its result will appear here when it ends."
AGENT_STARTED = ("Stage started. Nothing is sent to the model until you write to the assistant in its panel; "
                 "it must read and acknowledge its resume packet before it changes anything.")


async def _shown(app, scope) -> bool:
    current = await asyncio.to_thread(app.workspace.read)
    return (current.get("project_id"), current.get("worktree")) == scope


def _late_result(app, scope, profile):
    """After the stage worker was cancelled: the outcome as a notice, never a dialog over other work."""
    def done(future):
        if future.cancelled():
            return
        try:
            current = app.workspace.read()
        except (OSError, ValueError):
            return
        if (current.get("project_id"), current.get("worktree")) != scope:
            return  # Recorded for that project; it is not this one's result.
        error = future.exception()
        if error is not None:
            app.notice(app.t("The check could not run") + ": " + profile + " · " + str(error))
            return
        result = future.result()
        verdict = "Check passed." if result.get("passed") is True else "Check did not pass."
        app.notice(app.t("Check finished") + ": " + profile + " · " + app.t(verdict)
                   + (" " + app.t("Outcome") + ": " + str(result["outcome"]) if result.get("outcome") else "")
                   + (" · " + str(result["error"]) if result.get("error") else ""))
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
    if scope[0] != project["id"] or not scope[1]:
        raise ValueError(app.t("Select a visible project first."))
    decision = await app.request("GET", "/api/stage", params={"project": scope[0], "worktree": scope[1]})
    if not await _shown(app, scope):
        app.notice(PROJECT_CHANGED)
        return
    text = describe(decision, app.t)
    if decision.get("action") == "start_slot":
        await app.load_state()
        if not await _shown(app, scope):
            app.notice(PROJECT_CHANGED)
            return
        # The launcher returns a live process of this slot unchanged: no new session, no packet.
        # The built-in agent's launcher (start_agent) reuses a live run by the same identity.
        if reusable_native_run(app.data.get("runs", []), scope[0], scope[1], decision["client"], decision["slot_id"]):
            app.notice(app.t(ALREADY_RUNNING) + "\n" + text)
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
            app.notice(PROJECT_CHANGED)
            return
        failed = [row.get("code") for row in current.get("launch_errors", [])
                  if row.get("slot_id") == decision["slot_id"]]
        if failed:
            app.notice(app.t("The assistant did not start") + ": " + ", ".join(map(str, failed)) + "\n" + text)
        elif current.get("last_error"):
            app.notice(app.launch_notice(current))
        elif decision["client"] in AGENT_CLIENTS:
            app.notice(app.t(AGENT_STARTED) + "\n" + text)
        else:
            app.notice(app.t("Stage started. The assistant must acknowledge its resume packet.") + "\n" + text)
    elif decision.get("action") == "run_check":
        profile = decision["check_profile"]
        running = getattr(app, "_stage_check", None)
        if running is not None and not running[0].done():
            # One check at a time from here; an assistant stage is never held back by it.
            app.notice(app.t("A check is still running") + ": " + running[1])
            return
        app.notice(app.t("Running check") + ": " + profile)
        # The check process is not stopped when the worker is cancelled; it finishes and is recorded.
        future = asyncio.ensure_future(asyncio.to_thread(run_stage_check, app.settings, decision))
        app._stage_check = (future, profile + " · " + str(app.workspace_data.get("project_name") or project["name"])
                            + " · " + str(decision["task"]["id"]))
        try:
            result = await asyncio.shield(future)
        except asyncio.CancelledError:
            # Another click in the same group cancelled the worker: say "project changed" only
            # when it did, and let the outcome reach this project once the check ends.
            future.add_done_callback(_late_result(app, scope, profile))
            current = app.workspace.read()
            same = (current.get("project_id"), current.get("worktree")) == scope
            app.notice(app.t(CHECK_CONTINUES) + " " + profile if same else CHECK_MOVED)
            raise
        if not await _shown(app, scope):
            app.notice(CHECK_MOVED)
            return
        app.notice("")
        await app.push_screen_wait(MessageModal("Check result", text + "\n\n" + check_summary(result, app.t)))
    elif decision.get("problem") in CONNECTION_PROBLEMS:
        # The mapped assistant stays mapped; its connection is what needs the person.
        await app.push_screen_wait(MessageModal("Run next stage", text))
    else:
        await app.push_screen_wait(MessageModal("Run next stage", text + "\n\n"
                                                + app.t("Map stages in More → Plan stages.")))


async def edit_stages(app):
    """Map implement and review to an assistant of this project and test to a check profile."""
    from .terminal_ui import ChoiceModal, Field, FormModal
    project_id = app.workspace_data.get("project_id")
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
        slot = next((s for s in slots if s["id"] == target), None)
        name = profiles.get(slot["profile_id"], {}).get("label") if slot else None
        return app.t(stage) + " · " + (name or app.t("Unknown assistant")) + " (" + target + ")"

    stage = await app.push_screen_wait(ChoiceModal("Plan stages", [(shown(s), s) for s in STAGES]))
    if stage is None:
        return
    if stage in CLIENT_STAGES:
        choices = [(profiles[s["profile_id"]]["label"] + " (" + s["id"] + ")", s["id"]) for s in slots
                   if profiles.get(s["profile_id"], {}).get("client") in LAUNCHABLE_CLIENTS]
        value = await app.push_screen_wait(ChoiceModal(app.t(stage), choices + [("Not set", _UNSET)]))
        if value is None:
            return
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
    app.notice("Plan stages saved. Nothing was started.")
