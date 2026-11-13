"""Stage router: which assistant or check the next plan step goes to.

The router reads `next_task` and the owner's stage map and suggests one action. It never
changes a task, a plan or a session: launching still goes through the normal launcher with its
resume packet and ACK, a check still goes through `cairnmind check run`, and moving a task to
its next state stays an explicit action of the person or the assistant.
"""
from __future__ import annotations

import re

STAGES = ("implement", "review", "test")
# A task state names the work it waits for. Only the state decides the stage.
STATE_STAGES = {"todo": "implement", "in_progress": "implement", "needs_review": "review", "needs_test": "test"}
CLIENT_STAGES = ("implement", "review")
# The assistants the workspace launches as native clients, and its own agent run in a panel.
NATIVE_CLIENTS = ("codex", "claude")
AGENT_CLIENTS = ("api", "copilot")
# Only the assistants the workspace can launch for a plan step.
LAUNCHABLE_CLIENTS = (*NATIVE_CLIENTS, *AGENT_CLIENTS)
_SLOT = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _valid_choice(target) -> bool:
    """A slot id (maps saved before #125), or the slot with the profile and client it named when saved."""
    if isinstance(target, str):
        return bool(_SLOT.fullmatch(target))
    return (isinstance(target, dict) and set(target) == {"slot", "profile_id", "client"}
            and all(isinstance(target[key], str) and _SLOT.fullmatch(target[key]) for key in ("slot", "profile_id"))
            and target["client"] in LAUNCHABLE_CLIENTS)


def stage_slot(target) -> str:
    """The slot id a client stage names, in either shape."""
    return target["slot"] if isinstance(target, dict) else target


def validate_stage_map(value) -> dict:
    """The stage map of one project: implement/review name an assistant slot, test a check profile.

    Only the shape is checked here. Whether a slot or a profile exists is decided when the router
    runs, so that removing an assistant never makes the preferences unreadable, and a map that
    names a missing one is shown as such instead of being repaired or replaced. Plan stages saves
    a client stage as the slot with the profile and client it named then; a plain slot id is an
    older map, still read.
    """
    if not isinstance(value, dict) or set(value) - set(STAGES):
        raise ValueError("Unknown plan stage; use implement, review or test.")
    result = {}
    for stage, target in value.items():
        if stage in CLIENT_STAGES:
            if not _valid_choice(target):
                raise ValueError("A stage must name an assistant slot.")
            target = dict(target) if isinstance(target, dict) else target
        elif (not isinstance(target, str) or not 1 <= len(target) <= 160 or target.startswith("-")
                or _CONTROL.search(target)):
            raise ValueError("The test stage must name a check profile.")
        result[stage] = target
    return result


def validate_project_stages(value) -> dict:
    if not isinstance(value, dict) or len(value) > 1000:
        raise ValueError("Invalid plan stages.")
    result = {}
    for project_id, mapping in value.items():
        if not isinstance(project_id, str) or not 1 <= len(project_id) <= 160 or "\0" in project_id:
            raise ValueError("Invalid project identifier.")
        result[project_id] = validate_stage_map(mapping)
    return result


def project_routing(preferences: dict, project_id: str) -> tuple[dict, list, list]:
    """The stage map, the effective assistant slots and the profiles of one project."""
    stages = preferences.get("project_stages", {}).get(project_id, {})
    slots = preferences.get("project_slots", {}).get(project_id, preferences.get("default_slots", []))
    return stages, slots, preferences.get("profiles", [])


def agent_problem(profile: dict, readiness: dict | None) -> str | None:
    """Pure: what the launcher would refuse before it starts the built-in agent or Copilot.

    `readiness` is what was observed without a network call, login or download: `credential`
    is the credential store's status code for the profile (None when it could not be read,
    "unknown" when nobody could look), `runtime` the Copilot runtime's status code.
    """
    readiness = readiness or {}
    credential = readiness.get("credential")
    if credential is None:
        return "credential_unavailable"
    if credential == "credential_missing":
        # A Copilot profile without a credential reference runs as the signed-in GitHub account;
        # one that names a credential is not started without it.
        return "copilot_credential_unavailable" if profile.get("client") == "copilot" else "credential_missing"
    if profile.get("client") == "copilot" and readiness.get("runtime") not in {"ready", "unknown"}:
        return "runtime_missing"
    return None


def resolve_stage(next_result: dict, stage_map: dict, slots: list, profiles: list, readiness=None) -> dict:
    """Pure: the next step's stage and the single action the owner mapped to it.

    `readiness(profile)` is asked only for the mapped built-in agent or Copilot profile; see
    `agent_problem`. Without it, its credential and runtime are "unknown": the launcher still
    refuses a missing credential or runtime when the stage is started.
    """
    task, plan = next_result.get("task"), next_result.get("plan")
    decision = {"stage": None, "action": "none", "problem": None, "reason": next_result.get("reason"),
                "plan": {"id": plan.get("id"), "version": plan.get("version"), "title": plan.get("title")} if plan else None,
                "task": None, "slot_id": None, "profile_id": None, "client": None, "label": None,
                "provider": None, "model": None, "credential": None, "runtime": None,
                "check_profile": None, "required_profiles": []}
    if not task:
        decision["problem"] = "no_task"
        return decision
    metadata = task.get("metadata", {})
    state = metadata.get("state", "todo")
    required = metadata.get("required_profiles", [])
    decision["task"] = {"id": task.get("id"), "version": task.get("version"), "state": state,
                        "content": str(task.get("content", ""))[:300]}
    decision["required_profiles"] = [p for p in required if isinstance(p, str)] if isinstance(required, list) else []
    stage = STATE_STAGES.get(state)
    decision["stage"] = stage
    if stage is None:
        decision["problem"] = "no_stage"
        return decision
    target = (stage_map or {}).get(stage)
    if not target:
        decision["problem"] = "not_mapped"
        return decision
    if stage == "test":
        decision.update(action="run_check", check_profile=target)
        return decision
    slot_id = stage_slot(target)
    decision["slot_id"] = slot_id
    slot = next((row for row in slots if isinstance(row, dict) and row.get("id") == slot_id), None)
    profile = next((row for row in profiles if slot and isinstance(row, dict) and row.get("id") == slot.get("profile_id")), None)
    if slot is None or profile is None:
        # Never fall back to another assistant: the owner chose this one.
        decision["problem"] = "unknown_assistant"
        return decision
    decision.update(profile_id=profile["id"], client=profile.get("client"), label=profile.get("label"))
    if profile.get("client") in AGENT_CLIENTS:
        decision.update(provider=profile.get("provider"), model=profile.get("model"))
    # A slot keeps its id when its connection is replaced or the default slots change: the stage
    # starts only the profile that was chosen for it, never what the slot points to now.
    if isinstance(target, dict) and (target["profile_id"], target["client"]) != (profile["id"], profile.get("client")):
        decision["problem"] = "assistant_changed"
        return decision
    if not isinstance(target, dict) and profile.get("client") in AGENT_CLIENTS:
        # A map saved before #125 named only the slot, when it could hold Codex or Claude Code only.
        decision["problem"] = "assistant_unconfirmed"
        return decision
    if profile.get("client") not in LAUNCHABLE_CLIENTS:
        decision["problem"] = "unavailable_assistant"
        return decision
    if profile["client"] in AGENT_CLIENTS:
        observed = readiness(profile) if readiness else {"credential": "unknown", "runtime": "unknown"}
        decision.update(credential=observed.get("credential"),
                        runtime=observed.get("runtime") if profile["client"] == "copilot" else None)
        problem = agent_problem(profile, observed)
        if problem:
            # Shown as it is; another assistant, native or paid, is never started instead.
            decision["problem"] = problem
            return decision
    decision["action"] = "start_slot"
    return decision


FROM_WORKSPACE = "Start this stage from the workspace with Run next stage."


def next_command(decision: dict, project_id: str, worktree: str, *, slots: list, profiles: list,
                 active_plan_id: str | None) -> tuple[list[str] | None, str | None]:
    """The existing command that does exactly what the stage map says, or why there is none.

    `cairnmind run` launches a client by name for the worktree's active plan: it carries no
    slot, profile or model. It is offered only when that is all the mapped assistant is.
    """
    if decision.get("action") == "run_check":
        return ["cairnmind", "check", "run", project_id, decision["check_profile"],
                "--task", decision["task"]["id"], "--worktree", worktree], None
    if decision.get("action") != "start_slot":
        return None, None
    if decision.get("client") in AGENT_CLIENTS:
        # `cairnmind run` launches only Codex and Claude Code; the agent runs in a workspace panel.
        return None, "agent_client"
    by_id = {row.get("id"): row for row in profiles if isinstance(row, dict)}
    same_client = [row for row in slots if by_id.get(row.get("profile_id"), {}).get("client") == decision["client"]]
    if by_id.get(decision.get("profile_id"), {}).get("model"):
        return None, "profile_model"
    if len(same_client) != 1:
        return None, "shared_client"
    if not decision.get("plan") or decision["plan"].get("id") != active_plan_id:
        return None, "inactive_plan"
    return ["cairnmind", "run", project_id, decision["client"], "--worktree", worktree], None


_PROBLEMS = {
    "no_task": "There is no next plan step to route.",
    "no_stage": "This step's state has no stage.",
    "not_mapped": "No assistant or check is mapped to this stage.",
    "unknown_assistant": "The mapped assistant is not configured for this project.",
    "unavailable_assistant": "The mapped assistant cannot be launched for a plan step.",
    "credential_missing": "The mapped assistant has no credential. Add its key in Connections; nothing was started.",
    "credential_unavailable": ("The mapped assistant's credential cannot be read. Check it in Connections; "
                               "nothing was started."),
    "runtime_missing": "GitHub Copilot is not prepared on this computer. Prepare it in Connections; nothing was started.",
    "copilot_credential_unavailable": (
        "This Copilot profile names a credential that is not available. Provide it, or clear the credential "
        "reference in the profile so the stage uses the signed-in GitHub account; nothing was started."),
    "assistant_changed": ("The assistant of this stage changed since it was mapped. Confirm it in More → Plan stages; "
                          "nothing was started."),
    "assistant_unconfirmed": ("This stage was mapped before it could name the built-in agent or Copilot. Confirm it "
                              "in More → Plan stages; nothing was started."),
}
# Problems whose text already says what to do: the connection, or confirming the stage again.
CONNECTION_PROBLEMS = ("credential_missing", "credential_unavailable", "runtime_missing",
                       "copilot_credential_unavailable")
CONFIRM_PROBLEMS = ("assistant_changed", "assistant_unconfirmed")
_REASONS = {"no_active_plan": "No active plan in this folder.", "completed": "Every step of the plan is done.",
            "blocked": "Every remaining step is blocked."}


def describe(decision: dict, translate=lambda text: text) -> str:
    """Readable lines. Fixed text is translated; stored names are appended, never formatted in."""
    lines = []
    if decision.get("plan"):
        plan = decision["plan"]
        lines.append(translate("Plan") + ": " + str(plan.get("title") or plan.get("id"))
                     + " (v" + str(plan.get("version")) + ")")
    task = decision.get("task")
    if task:
        lines.append(translate("Step") + ": " + task["content"] + " [" + str(task["state"]) + ", v"
                     + str(task.get("version")) + "]")
    if decision.get("stage"):
        lines.append(translate("Stage") + ": " + translate(decision["stage"]))
    if decision.get("slot_id"):
        lines.append(translate("Assistant") + ": " + str(decision.get("label") or decision["slot_id"])
                     + " (" + str(decision["slot_id"]) + ")")
    if decision.get("client") in AGENT_CLIENTS:
        named = " · ".join(str(value) for value in (decision.get("provider"), decision.get("model")) if value)
        lines.append(translate("Model") + ": " + (named or translate("default model")))
    if decision.get("check_profile"):
        lines.append(translate("Check profile") + ": " + decision["check_profile"])
    if decision.get("required_profiles"):
        lines.append(translate("Required checks") + ": " + ", ".join(decision["required_profiles"]))
    problem = decision.get("problem")
    if problem == "no_task":
        lines.append(translate(_REASONS.get(decision.get("reason"), _PROBLEMS["no_task"])))
    elif problem:
        lines.append(translate(_PROBLEMS[problem]))
    return "\n".join(lines)
