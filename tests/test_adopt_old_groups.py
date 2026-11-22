"""A person adopts a group of old tasks into a plan in one act (#305).

`plan adopt` on the command line makes no person's act, so it cannot adopt a group that holds a task a person
confirmed or one confirmed before the rule. The workspace can: `adopt_group` runs the adoption inside a proven
person's act and writes one audit note. Real HTTP service, ArcadeDB and Git; no model is asked.
"""
from __future__ import annotations

import pytest

from cairnmind import person_proof
from cairnmind.confirmation import GROUP_CHANGED, ConfirmationRefused, confirmation_of, confirmed_by
from cairnmind.memory_acts import adopt_group
from cairnmind.plans import PlanManager, legacy_groups

from conftest import confirmed_before_rule
from test_confirmed_by_person import fixture  # noqa: F401


def old_task(fixture, key, content, group="old-group", index=1, **metadata):  # noqa: F811
    return fixture.store.remember(fixture.pid, {"kind": "task", "key": key, "content": content, "metadata": {
        "plan_id": group, "step_index": index, "state": "todo", **metadata}}, [fixture.note["id"]])


def audits(fixture):  # noqa: F811
    return fixture.store.query("SELECT FROM sources WHERE project_id=:p AND uri LIKE 'review://%'",
                               {"p": fixture.pid})


def shown(fixture, group="old-group"):  # noqa: F811
    [found] = [item for item in legacy_groups(fixture.store, fixture.pid)["groups"] if item["group"] == group]
    return {task["id"]: task["version"] for task in found["tasks"]}


def old_group(fixture):  # noqa: F811
    first = old_task(fixture, "b", "Second step", index=2)
    second = old_task(fixture, "a", "First step", index=1)
    third = old_task(fixture, "c", "Third step", index=3, depends_on=[])
    confirmed_before_rule(fixture.store, first["id"])
    return first, second, third


def test_old_groups_are_listed_in_plan_order_and_managed_ones_are_not(fixture):  # noqa: F811
    first, second, third = old_group(fixture)
    other = old_task(fixture, "o", "Other", group="other")
    old_task(fixture, "n", "No group", group=None)
    managed = old_task(fixture, "m", "Managed", group="managed")
    PlanManager(fixture.store).adopt_legacy(fixture.pid, "managed", idempotency_key="k")
    groups = legacy_groups(fixture.store, fixture.pid)["groups"]
    assert [group["group"] for group in groups] == ["old-group", "other"]
    [old, _] = groups
    assert [task["id"] for task in old["tasks"]] == [second["id"], first["id"], third["id"]]
    assert old["confirmed"] == 1
    assert [task["confirmed_by"] for task in old["tasks"]] == [
        confirmed_by(fixture.record(task["id"])) for task in old["tasks"]]
    assert old["tasks"][1]["status"] == "confirmed" and old["tasks"][0]["version"] == second["version"]
    assert other["id"] not in {task["id"] for group in groups[:1] for task in group["tasks"]}
    assert managed["id"] not in {task["id"] for group in groups for task in group["tasks"]}


def test_a_person_adopts_a_confirmed_old_group_in_one_act(fixture):  # noqa: F811
    first, second, third = old_group(fixture)
    source = fixture.store.snapshot(fixture.pid, "legacy://done", "Reported")
    fixture.store.remember(fixture.pid, {"id": third["id"], "expected_version": third["version"], "metadata": {
        "state": "done", "test_passed": True, "test_source_ids": [source["id"]]}}, [source["id"]])
    before = len(audits(fixture))
    result = adopt_group(fixture.store, fixture.pid, "old-group", shown(fixture), title="Login")
    assert result["plan"]["title"] == "Login" and result["replayed"] is False
    steps = {step["id"]: step for step in result["tasks"]}
    assert [step["id"] for step in result["tasks"]] == [second["id"], first["id"], third["id"]]
    for step in steps.values():
        assert step["status"] == "proposed" and confirmation_of(step) is None, step
        assert step["key"].startswith(f"plan:{result['plan']['id']}:")
    assert steps[first["id"]]["key"].endswith(":b")
    assert steps[third["id"]]["metadata"]["state"] == "done" and steps[third["id"]]["metadata"]["test_passed"] is True
    new = audits(fixture)
    assert len(new) == before + 1
    [note] = [row for row in new if row["id"] == result["audit_source_id"]]
    assert '"action": "adopt_plan_group"' in note["content"]
    assert PlanManager(fixture.store).get(fixture.pid, result["plan"]["id"])["plan"]["title"] == "Login"
    revisions = fixture.store.query("SELECT reason FROM plan_revisions WHERE plan_id=:id",
                                    {"id": result["plan"]["id"]})
    assert "legacy_adoption" in {row["reason"] for row in revisions}


@pytest.mark.unproven
def test_adopting_needs_the_proof(fixture):  # noqa: F811
    old_group(fixture)
    versions, notes, records = shown(fixture), len(audits(fixture)), fixture.count()
    with pytest.raises(ConfirmationRefused) as refused:
        adopt_group(fixture.store, fixture.pid, "old-group", versions)
    assert str(refused.value) == person_proof.PROOF_MISSING
    assert PlanManager(fixture.store).list(fixture.pid) == []
    assert len(audits(fixture)) == notes and fixture.count() == records


def test_the_routes_list_with_the_token_and_adopt_only_with_the_key(fixture):  # noqa: F811
    first, second, third = old_group(fixture)
    listed = fixture.client.get("/inspector/legacy-groups", params={"project": fixture.pid})
    assert listed.status_code == 200 and [group["group"] for group in listed.json()["groups"]] == ["old-group"]
    body = {"project": fixture.pid, "group": "old-group", "task_versions": shown(fixture)}
    notes, records = len(audits(fixture)), fixture.count()
    refused = fixture.client.post("/inspector/legacy-groups/adopt", json=body)
    assert refused.status_code == 403 and refused.json()["detail"] == person_proof.PROOF_MISSING
    assert len(audits(fixture)) == notes and fixture.count() == records
    for bad in ({**body, "task_versions": {}}, {**body, "extra": 1}, {**body, "title": ""}):
        assert fixture.client.post("/inspector/legacy-groups/adopt", json=bad, headers=fixture.person
                                   ).status_code == 422
    done = fixture.client.post("/inspector/legacy-groups/adopt", json=body, headers=fixture.person)
    assert done.status_code == 200, done.text
    assert {task["status"] for task in done.json()["tasks"]} == {"proposed"}
    again = fixture.client.post("/inspector/legacy-groups/adopt", json=body, headers=fixture.person)
    assert again.status_code == 400 and again.json()["detail"] == GROUP_CHANGED


def test_a_group_that_changed_since_it_was_shown_adopts_nothing(fixture):  # noqa: F811
    first, second, third = old_group(fixture)
    notes = len(audits(fixture))

    def refused(versions):
        with pytest.raises(ValueError, match="changed since they were shown"):
            adopt_group(fixture.store, fixture.pid, "old-group", versions)
        assert PlanManager(fixture.store).list(fixture.pid) == [] and len(audits(fixture)) == notes

    stale = shown(fixture)
    fixture.store.remember(fixture.pid, {"id": second["id"], "expected_version": second["version"],
                                         "metadata": {"state": "in_progress"}}, [fixture.note["id"]])
    refused(stale)  # a moved version
    before_added = shown(fixture)
    old_task(fixture, "d", "Added later", index=4)
    refused(before_added)  # a task added to the group
    refused({key: value for key, value in shown(fixture).items() if key != third["id"]})  # a task left out
