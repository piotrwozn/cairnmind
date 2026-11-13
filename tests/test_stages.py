import pytest

from cairnmind.plans import PlanManager
from cairnmind.stages import describe, project_routing, resolve_stage, validate_project_stages, validate_stage_map
from cairnmind.store import Store

SLOTS = [{"id": "codex", "profile_id": "codex"}, {"id": "claude", "profile_id": "claude"},
         {"id": "helper", "profile_id": "api-helper"}, {"id": "orphan", "profile_id": "gone"},
         {"id": "pilot", "profile_id": "copilot"}, {"id": "odd", "profile_id": "odd"}]
PROFILES = [{"id": "codex", "client": "codex", "label": "Codex"},
            {"id": "claude", "client": "claude", "label": "Claude"},
            {"id": "api-helper", "client": "api", "label": "API helper", "provider": "openai", "model": "gpt-x",
             "credential_ref": "profile:api-helper"},
            {"id": "copilot", "client": "copilot", "label": "GitHub Copilot"},
            {"id": "odd", "client": "cursor", "label": "Not launchable"}]
MAP = {"implement": "codex", "review": "claude", "test": "unit"}


def step(state, **metadata):
    return {"plan": {"id": "plan-1", "version": 3, "title": "API"},
            "task": {"id": "task-1", "version": 7, "content": "Build it",
                     "metadata": {"state": state, **metadata}},
            "reason": state if state in {"in_progress", "needs_test", "needs_review"} else "ready", "blocked": []}


@pytest.mark.parametrize("state,stage,action,target", [
    ("todo", "implement", "start_slot", "codex"), ("in_progress", "implement", "start_slot", "codex"),
    ("needs_review", "review", "start_slot", "claude"), ("needs_test", "test", "run_check", "unit")])
def test_each_task_state_routes_to_its_mapped_stage(state, stage, action, target):
    decision = resolve_stage(step(state), MAP, SLOTS, PROFILES)
    assert (decision["stage"], decision["action"], decision["problem"]) == (stage, action, None)
    assert target == (decision["check_profile"] if action == "run_check" else decision["slot_id"])
    assert decision["task"] == {"id": "task-1", "version": 7, "state": state, "content": "Build it"}
    assert decision["plan"] == {"id": "plan-1", "version": 3, "title": "API"}
    if action == "start_slot":
        assert decision["client"] == target and decision["profile_id"] == target


@pytest.mark.parametrize("reason", ["no_active_plan", "completed", "blocked"])
def test_no_next_step_does_nothing(reason):
    decision = resolve_stage({"plan": None, "task": None, "reason": reason, "blocked": []}, MAP, SLOTS, PROFILES)
    assert (decision["action"], decision["problem"], decision["stage"]) == ("none", "no_task", None)
    assert describe(decision)


def test_missing_mapping_does_nothing_and_says_so():
    decision = resolve_stage(step("needs_review"), {"implement": "codex"}, SLOTS, PROFILES)
    assert (decision["stage"], decision["action"], decision["problem"]) == ("review", "none", "not_mapped")
    assert "No assistant or check is mapped to this stage." in describe(decision)


@pytest.mark.parametrize("slot,problem", [("missing", "unknown_assistant"), ("orphan", "unknown_assistant"),
                                          ("odd", "unavailable_assistant")])
def test_unknown_or_unlaunchable_assistant_is_shown_and_never_replaced(slot, problem):
    decision = resolve_stage(step("todo"), {"implement": slot, "review": "claude"}, SLOTS, PROFILES)
    assert (decision["action"], decision["problem"], decision["slot_id"]) == ("none", problem, slot)
    assert decision["client"] in {None, "cursor"}


def chosen(slot):
    """A client stage as Plan stages saves it: the slot and the profile and client it names now."""
    profile_id = next(s["profile_id"] for s in SLOTS if s["id"] == slot)
    client = next(p["client"] for p in PROFILES if p["id"] == profile_id)
    return {"slot": slot, "profile_id": profile_id, "client": client}


def test_a_repointed_project_slot_never_starts_the_profile_it_points_to_now():
    stage_map = {"implement": chosen("claude")}
    repointed = [{"id": "claude", "profile_id": "api-helper"}]  # "Replace connection" keeps the slot id.
    decision = resolve_stage(step("todo"), stage_map, repointed, PROFILES, readiness=observed())
    assert (decision["action"], decision["problem"], decision["client"]) == ("none", "assistant_changed", "api")
    assert describe(decision).endswith("Confirm it in More → Plan stages; nothing was started.")
    assert resolve_stage(step("todo"), stage_map, SLOTS, PROFILES)["action"] == "start_slot"


def test_a_repointed_default_slot_never_starts_the_profile_it_points_to_now():
    preferences = {"project_stages": {"p": {"review": chosen("claude")}}, "project_slots": {},
                   "default_slots": [{"id": "claude", "profile_id": "copilot"}], "profiles": PROFILES}
    decision = resolve_stage(step("needs_review"), *project_routing(preferences, "p"), readiness=observed())
    assert (decision["action"], decision["problem"], decision["client"]) == ("none", "assistant_changed", "copilot")


def test_an_old_map_follows_a_native_slot_as_before():
    # #122 followed the slot for a Codex/Claude Code swap; a plain slot id still does.
    swapped = [{"id": "codex", "profile_id": "claude"}]
    decision = resolve_stage(step("todo"), {"implement": "codex"}, swapped, PROFILES)
    assert (decision["action"], decision["client"], decision["problem"]) == ("start_slot", "claude", None)


@pytest.mark.parametrize("slot", ["helper", "pilot"])
def test_an_old_map_with_an_agent_slot_must_be_confirmed(slot):
    decision = resolve_stage(step("todo"), {"implement": slot}, SLOTS, PROFILES, readiness=observed())
    assert (decision["action"], decision["problem"], decision["slot_id"]) == ("none", "assistant_unconfirmed", slot)
    decision = resolve_stage(step("todo"), {"implement": chosen(slot)}, SLOTS, PROFILES, readiness=observed())
    assert (decision["action"], decision["problem"]) == ("start_slot", None)


def test_both_shapes_of_a_client_stage_are_valid_and_old_preferences_load(tmp_path):
    from cairnmind.cockpit_state import CockpitState
    from cairnmind.config import Settings
    both = {"implement": "codex", "review": chosen("helper"), "test": "unit"}
    assert validate_stage_map(both) == both
    for bad in ({"implement": {"slot": "codex"}}, {"implement": {**chosen("codex"), "client": "cursor"}},
                {"implement": {**chosen("codex"), "extra": 1}}, {"implement": {**chosen("codex"), "slot": "bad slot"}}):
        with pytest.raises(ValueError):
            validate_stage_map(bad)
    settings = Settings.load(tmp_path / "data", create=True)
    state = CockpitState(settings)
    state.update({"project_stages": {"p": {"implement": "codex"}}})  # A file saved before #125.
    assert CockpitState(settings).load()["project_stages"] == {"p": {"implement": "codex"}}
    state.update({"project_stages": {"p": both}})
    assert state.load()["project_stages"] == {"p": both}


def observed(credential="configured_unchecked", runtime="ready", seen=None):
    def readiness(profile):
        if seen is not None:
            seen.append(profile["id"])
        return {"credential": credential, "runtime": runtime if profile["client"] == "copilot" else None}
    return readiness


@pytest.mark.parametrize("slot,client", [("helper", "api"), ("pilot", "copilot")])
def test_an_api_or_copilot_slot_starts_with_the_facts_the_workspace_needs(slot, client):
    seen = []
    decision = resolve_stage(step("needs_review"), {"review": chosen(slot)}, SLOTS, PROFILES,
                             readiness=observed(seen=seen))
    assert (decision["action"], decision["problem"], decision["slot_id"], decision["client"]) == (
        "start_slot", None, slot, client)
    profile = next(p for p in PROFILES if p["client"] == client)
    assert (decision["profile_id"], decision["provider"], decision["model"]) == (
        profile["id"], profile.get("provider"), profile.get("model"))
    assert decision["credential"] == "configured_unchecked"
    assert decision["runtime"] == ("ready" if client == "copilot" else None)
    assert seen == [profile["id"]]  # Only the mapped profile is looked at.


def test_a_native_stage_never_looks_at_agent_credentials():
    decision = resolve_stage(step("todo"), MAP, SLOTS, PROFILES,
                             readiness=lambda profile: pytest.fail("read a credential for a native client"))
    assert (decision["action"], decision["credential"], decision["provider"]) == ("start_slot", None, None)


@pytest.mark.parametrize("slot,credential,runtime,problem", [
    ("helper", "credential_missing", None, "credential_missing"),
    ("helper", None, None, "credential_unavailable"),
    ("pilot", "credential_missing", "ready", "credential_missing"),
    ("pilot", "not_required", "copilot_sdk_missing", "runtime_missing"),
    ("pilot", "not_required", "copilot_runtime_invalid", "runtime_missing")])
def test_an_agent_that_cannot_start_is_shown_as_such_and_never_replaced(slot, credential, runtime, problem):
    decision = resolve_stage(step("todo"), {"implement": chosen(slot), "review": "claude"}, SLOTS, PROFILES,
                             readiness=observed(credential, runtime))
    assert (decision["action"], decision["problem"], decision["slot_id"]) == ("none", problem, slot)
    assert decision["client"] in {"api", "copilot"}
    text = describe(decision)
    assert text.endswith(describe_problem[problem])


describe_problem = {
    "credential_missing": "The mapped assistant has no credential. Add its key in Connections; nothing was started.",
    "credential_unavailable": "credential cannot be read. Check it in Connections; nothing was started.",
    "runtime_missing": "GitHub Copilot is not prepared on this computer. Prepare it in Connections; nothing was started."}


def test_without_an_observation_the_launcher_decides():
    decision = resolve_stage(step("todo"), {"implement": chosen("helper")}, SLOTS, PROFILES)
    assert (decision["action"], decision["credential"]) == ("start_slot", "unknown")


def test_no_stage_is_mapped_by_default_even_to_a_configured_agent():
    decision = resolve_stage(step("todo"), {}, SLOTS, PROFILES, readiness=observed())
    assert (decision["action"], decision["problem"], decision["client"]) == ("none", "not_mapped", None)


def test_test_stage_names_only_the_mapped_profile_and_shows_required_ones():
    decision = resolve_stage(step("needs_test", required_profiles=["unit", "lint"]), MAP, SLOTS, PROFILES)
    assert decision["check_profile"] == "unit" and decision["required_profiles"] == ["unit", "lint"]
    unmapped = resolve_stage(step("needs_test", required_profiles=["unit"]), {"implement": "codex"}, SLOTS, PROFILES)
    assert (unmapped["action"], unmapped["problem"], unmapped["check_profile"]) == ("none", "not_mapped", None)


def test_describe_appends_stored_text_and_never_formats_it():
    decision = resolve_stage(step("todo"), MAP, SLOTS, PROFILES)
    decision["task"]["content"] = "{boom} %s"
    text = describe(decision, lambda value: value.upper())
    assert "STEP: {boom} %s" in text and "STAGE: IMPLEMENT" in text


def test_stage_map_shape_is_strict():
    assert validate_stage_map(MAP) == MAP
    assert validate_project_stages({"p": {"review": "claude"}}) == {"p": {"review": "claude"}}
    for bad in ({"deploy": "codex"}, {"implement": "bad slot"}, {"implement": 3}, {"test": "-x"},
                {"test": ""}, {"test": "a\nb"}, ["implement"]):
        with pytest.raises(ValueError):
            validate_stage_map(bad)
    for bad in ({"": {}}, {"p" * 161: {}}, {"p": None}, []):
        with pytest.raises(ValueError):
            validate_project_stages(bad)


def test_routing_reads_next_task_without_changing_the_plan(tmp_path):
    store = Store(tmp_path / "memory")
    project = store.add_project("example", tmp_path / "repo")
    plans = PlanManager(store)
    saved = plans.save(project["id"], {"title": "Two steps"},
                       [{"key": "build", "content": "Build"}, {"key": "check", "content": "Check", "depends_on": ["build"]}],
                       "initial", activate=True)
    before = plans.get(project["id"], saved["plan"]["id"])
    decision = resolve_stage(plans.next_task(project["id"]), MAP, SLOTS, PROFILES)
    assert (decision["stage"], decision["slot_id"]) == ("implement", "codex")
    assert plans.get(project["id"], saved["plan"]["id"]) == before
