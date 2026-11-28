"""A person's act that answers after a project switch still tells its outcome (#354), and the last findings of #347.

Every act of the workspace with a request after its consent dialog is driven to the dialog; the project changes
while the request is in flight; the outcome (done, refused, unclear) must come as a notice addressed to the
project the act was for (app.notice_for), never silence. Headless Textual with fake brokers: no tmux, engine or
model, so the same on macOS; fast tier.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from consent_steps import consent_drawn
from test_person_acts_consent import ACTS
from test_project_forget_workspace import LOG, OPEN, choose, consent, notice_of
from test_project_forget_workspace import ui as forget_ui
from test_workspace_stage_scope import wait_until

from cairnmind.terminal import (
    terminal_legacy_groups,
    terminal_plan_confirm,
    terminal_published_import,
    terminal_release,
)
from cairnmind.terminal import terminal_project_forget as view
from cairnmind.terminal.terminal_client import BrokerError, BrokerUnreachable
from cairnmind.terminal.terminal_i18n import CATALOG

OWN = {"review-confirm", "review-dispute", "conflict", "plan", "release", "verdict", "adopt", "forget-project",
       "team-memory"}
SWITCHED = [act for act in ACTS if act.id in OWN]
REFUSAL, LOST = "refusal-detail-4xx", "answer-lost-detail"
VERDICT_UNCLEAR = ("No clear answer from the memory service. The verdict may have been saved. Saving the same "
                   "verdict again is safe and stores nothing twice.")
# act: (what a refusal says, what an unclear outcome says). A refusal is not done; an unclear outcome may be done.
SAYS = {
    "review-confirm": ("The review was not saved.", "The service did not confirm the review, so it may have been saved."),
    "review-dispute": ("The review was not saved.", "The service did not confirm the review, so it may have been saved."),
    "conflict": ("The decision was not saved.", "The service did not confirm the decision, so it may have been saved."),
    "plan": ("The plan was not confirmed.", terminal_plan_confirm.PLAN_UNCLEAR),
    "release": ("The record was not released.", terminal_release.RELEASE_UNCLEAR),
    "verdict": ("The verdict was not saved.", VERDICT_UNCLEAR),
    "adopt": (terminal_legacy_groups.NOT_ADOPTED, terminal_legacy_groups.ADOPT_UNCLEAR),
    "team-memory": (terminal_published_import.NOT_IMPORTED, terminal_published_import.IMPORT_UNCLEAR),
}


def spy(app, path, how):
    """The project changes as the act's request goes out; the request ends as `how`. Returns what notice_for got
    after that moment: (about, text) pairs."""
    told = []
    state = {"posted": False}
    notice_for = app.notice_for

    def fail(sent):
        state["posted"] = True
        app._scope_version += 1  # the project changes while the request is in flight
        if how == "refused":
            raise BrokerError(403, REFUSAL)
        if how == "unclear":
            if sent is not None:
                sent.append(True)  # the verdict's own client has found the service: the request went out
            raise BrokerUnreachable(503, LOST)

    original, impact = app.request, getattr(app, "impact_request", None)

    async def request(method, route, *, params=None, json=None):
        if method == "POST" and route == path and not (json or {}).get("dry_run") and how != "done":
            fail(None)
        if method == "POST" and route == path and how == "done" and not (json or {}).get("dry_run"):
            state["posted"] = True
            app._scope_version += 1
        return await original(method, route, params=params, json=json)

    def impact_request(method, route, *, sent=None, **kwargs):
        if method == "POST" and route == path:
            if how == "done":
                state["posted"] = True
                app._scope_version += 1
            else:
                fail(sent)
        return impact(method, route, sent=sent, **kwargs)

    def notice_for_spy(about, value, kind="info"):
        if state["posted"]:
            told.append((about, str(getattr(value, "plain", value)), kind))
        return notice_for(about, value, kind)

    app.request, app.notice_for = request, notice_for_spy
    if impact:
        app.impact_request = impact_request
    return told


@pytest.mark.asyncio
@pytest.mark.parametrize("act", SWITCHED, ids=[act.id for act in SWITCHED])
@pytest.mark.parametrize("how", ["done", "refused", "unclear"])
async def test_an_outcome_after_a_project_switch_is_told_for_its_project(tmp_path, monkeypatch, act, how):
    app, workspace, sent = act.make(tmp_path, monkeypatch, "en")
    path = act.expected(workspace)[0][0]
    told = spy(app, path, how)
    async with app.run_test(size=(120, 40)) as pilot:
        await act.reach(app, pilot, act.text)
        await consent_drawn(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: told)
        about, text, kind = told[0]
        assert about["project_id"] == act.expected(workspace)[0][1]["project"]
        assert "Nothing was saved" not in text  # a request was sent: the act is not "nothing saved"
        if act.id == "verdict":
            assert about["project_name"] is None  # the worktree is not the project's name
        if how == "refused":
            assert REFUSAL in text and kind == "error"
        if how == "unclear":
            assert LOST in text
        if act.id in SAYS and how != "done":
            said, unclear = SAYS[act.id][0], SAYS[act.id][1]
            assert (unclear if how == "unclear" else said) in text
            assert (said if how == "unclear" else unclear) not in text
        if how == "done":
            assert REFUSAL not in text and LOST not in text and kind == "info"


@pytest.mark.asyncio
async def test_the_outcome_is_told_for_the_deleted_project_not_the_open_one(tmp_path, monkeypatch):
    app, broker, workspace = forget_ui(tmp_path, monkeypatch)
    told = []
    notice_for = app.notice_for
    app.notice_for = lambda about, value, kind="info": (told.append((about, str(value))), notice_for(about, value,
                                                                                                    kind))[1]
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(app, pilot, downs=1)  # "Other"; the open project is another one
        await consent(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: broker.sent and told)
    assert told[-1][0]["project_id"] == "other" and "Other" in told[-1][1]


@pytest.mark.asyncio
async def test_a_500_after_the_commit_is_not_called_not_deleted(tmp_path, monkeypatch):
    app, broker, workspace = forget_ui(tmp_path, monkeypatch)
    broker.no_answer = "500"  # the service deleted, then an unexpected exception answered 500
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(app, pilot, downs=1)
        await consent(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: broker.sent and "Deleted the memory of Other" in notice_of(app))
        assert view.NOT_DELETED not in notice_of(app)


@pytest.mark.asyncio
async def test_a_lost_answer_with_the_project_still_listed_keeps_the_reason(tmp_path, monkeypatch):
    app, broker, workspace = forget_ui(tmp_path, monkeypatch)
    broker.no_answer = "not"
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(app, pilot)
        await consent(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: broker.sent and view.UNKNOWN.split(",")[0][:20] in notice_of(app))
        assert "The connection to the local broker was interrupted." in notice_of(app)


@pytest.mark.asyncio
async def test_a_deletion_confirmed_only_by_the_list_says_when_the_answer_log_is_still_there(tmp_path, monkeypatch):
    monkeypatch.setattr(view, "Path", lambda value: SimpleNamespace(exists=lambda: value == LOG))
    app, broker, workspace = forget_ui(tmp_path, monkeypatch)
    broker.no_answer = "committed"
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(app, pilot, downs=1)
        await consent(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: broker.sent and "Deleted the memory of Other" in notice_of(app))
        assert LOG in notice_of(app) and view.LOST_ANSWER in notice_of(app)


@pytest.mark.asyncio
async def test_a_deletion_confirmed_only_by_the_list_with_the_log_gone_says_nothing_of_it(tmp_path, monkeypatch):
    app, broker, workspace = forget_ui(tmp_path, monkeypatch)  # LOG is a path that does not exist here
    broker.no_answer = "committed"
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(app, pilot, downs=1)
        await consent(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: broker.sent and "Deleted the memory of Other" in notice_of(app))
        assert "could not be deleted" not in notice_of(app)


@pytest.mark.asyncio
async def test_a_listing_that_is_not_a_dict_is_not_a_deletion(tmp_path, monkeypatch):
    app, broker, workspace = forget_ui(tmp_path, monkeypatch)
    calls = {"n": 0}
    plain = broker.request

    def request(method, path, **kwargs):
        if path == "/api/inspector/projects/deletable":
            calls["n"] += 1
            if calls["n"] > 1:  # the re-read after the lost answer
                return ["not", "a", "dict"]
        return plain(method, path, **kwargs)
    broker.request = request
    broker.no_answer = "committed"
    async with app.run_test(size=(120, 40)) as pilot:
        await choose(app, pilot)
        await consent(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: broker.sent and "not known whether" in notice_of(app))
    assert workspace.read()["project_id"] == OPEN


@pytest.mark.asyncio
async def test_a_snapshot_saved_after_a_project_switch_is_told(tmp_path, monkeypatch):
    from test_terminal_panels import settle, setup

    from cairnmind.terminal.terminal_inspector import InspectorApp
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal.terminal_ui.language_for", lambda settings: "en")
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    plain = broker.request

    def request(method, path, **kwargs):
        if path == "/api/memory/snapshot":
            app._scope_version += 1  # the project changes while the snapshot is written
            return {}
        return plain(method, path, **kwargs)
    broker.request = request
    told = []
    notice_for = app.notice_for
    app.notice_for = lambda about, value, kind="info": (told.append((about, str(value))), notice_for(about, value,
                                                                                                    kind))[1]
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.preview_scope = (workspace.value["project_id"], workspace.value["worktree"])
        app.memory_operation("save")
        await wait_until(pilot, lambda: told)
    assert told[0][0]["project_id"] == workspace.value["project_id"] and told[0][1] == "Snapshot saved."


@pytest.mark.asyncio
async def test_a_failed_snapshot_save_after_a_project_switch_is_told_for_its_project(tmp_path, monkeypatch):
    from test_terminal_panels import settle, setup

    from cairnmind.terminal.terminal_inspector import InspectorApp
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal.terminal_ui.language_for", lambda settings: "en")
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    plain = broker.request

    def request(method, path, **kwargs):
        if path == "/api/memory/snapshot":
            app._scope_version += 1  # the project changes while the snapshot is written
            raise BrokerError(500, "snapshot-failed-detail")
        return plain(method, path, **kwargs)
    broker.request = request
    told = []
    notice_for = app.notice_for
    app.notice_for = lambda about, value, kind="info": (told.append((about, str(value), kind)),
                                                         notice_for(about, value, kind))[1]
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        app.preview_scope = (workspace.value["project_id"], workspace.value["worktree"])
        app.memory_operation("save")
        await wait_until(pilot, lambda: told)
    assert told[0][0]["project_id"] == workspace.value["project_id"] and told[0][2] == "error"
    assert "snapshot-failed-detail" in told[0][1]


def test_the_new_sentences_have_polish():
    from cairnmind.terminal import terminal_legacy_groups, terminal_plan_confirm, terminal_published_import
    for sentence in (terminal_legacy_groups.NOT_ADOPTED, terminal_legacy_groups.ADOPT_UNCLEAR,
                     terminal_published_import.NOT_IMPORTED, terminal_published_import.IMPORT_UNCLEAR,
                     view.LOST_ANSWER, "The plan was not confirmed.", "The decision was not saved.",
                     "The review was not saved.", terminal_plan_confirm.PLAN_UNCLEAR, terminal_release.RELEASE_UNCLEAR,
                     "The service did not confirm the review, so it may have been saved.",
                     "The service did not confirm the decision, so it may have been saved.",
                     "The current list was reloaded.", "The current version was reloaded.",
                     "No clear answer from the memory service. The verdict may have been saved. Saving the same "
                     "verdict again is safe and stores nothing twice."):
        assert CATALOG[sentence] != sentence, sentence
    assert terminal_plan_confirm.MESSAGES["The plan was not confirmed."] == CATALOG["The plan was not confirmed."]
