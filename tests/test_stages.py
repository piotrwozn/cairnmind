import pytest

from cairnmind.plans import PlanManager
from cairnmind.stages import describe, resolve_stage, validate_project_stages, validate_stage_map
from cairnmind.store import Store

SLOTS = [{"id": "codex", "profile_id": "codex"}, {"id": "claude", "profile_id": "claude"},
         {"id": "helper", "profile_id": "api-helper"}, {"id": "orphan", "profile_id": "gone"}]
PROFILES = [{"id": "codex", "client": "codex", "label": "Codex"},
            {"id": "claude", "client": "claude", "label": "Claude"},
            {"id": "api-helper", "client": "api", "label": "API helper"}]
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
                                          ("helper", "unavailable_assistant")])
def test_unknown_or_unlaunchable_assistant_is_shown_and_never_replaced(slot, problem):
    decision = resolve_stage(step("todo"), {"implement": slot, "review": "claude"}, SLOTS, PROFILES)
    assert (decision["action"], decision["problem"], decision["slot_id"]) == ("none", problem, slot)
    assert decision["client"] in {None, "api"}


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
