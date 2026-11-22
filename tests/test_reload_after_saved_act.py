"""A reload that fails after a saved act never makes the act read as not saved (#359).

Each act of the workspace that reloads its view after the request is driven to its consent dialog and approved.
The request answers 2xx; every read after it fails (a lost connection, a broker 503). The notice must say the act
was done and that the view could not be reloaded, in English and Polish, never "not saved" or "may have been saved",
and exactly one request was sent. A refusal (4xx) followed by a failed reload still says not done, and does not
claim a reload. Headless Textual with fake brokers: no tmux, engine or model, so the same on macOS; fast tier.
"""
from __future__ import annotations

import pytest
from consent_steps import consent_drawn
from test_acts_after_project_switch import REFUSAL, SAYS
from test_person_acts_consent import ACTS
from test_workspace_stage_scope import wait_until

from cairnmind.terminal_client import BrokerError, BrokerUnreachable
from cairnmind.terminal_i18n import tr
from cairnmind.terminal_person_act import NOT_RELOADED
from cairnmind.terminal_record_kinds import ACCEPT_UNCLEAR, NOT_ACCEPTED, NOT_WITHDRAWN, WITHDRAW_UNCLEAR

TEAM = ("Team memory imported: {written} records written, {replaced} of them in place of a verdict given on this "
        "computer. Nothing imported is verified here.")
# act: the sentence that tells it done, and its fields
DONE = {
    "review-confirm": ("Review saved with an audit note.", {}),
    "review-dispute": ("Review saved with an audit note.", {}),
    "conflict": ("Decision saved with an audit note.", {}),
    "plan": ("Plan confirmed with an audit note.", {}),
    "team-memory": (TEAM, {"written": 0, "replaced": 0}),
}
ACT_IDS = list(DONE)
CLAIMS = ("The current version was reloaded.", "The current list was reloaded.")
LOST = "answer-lost-detail"
READ_FAILS = {"unreachable": BrokerUnreachable(503, LOST), "503": BrokerError(503, "broker-503-detail")}


class Told(list):
    posts = 0  # a refused request never reaches the fake broker's record: it is counted here


def after_the_request(app, path, how):
    """Every read after the act's request fails (`how` is a key of READ_FAILS) or, for "refused", the request itself
    is refused and then the reads fail. Returns the notices told after the request."""
    told, state, original, notice = Told(), {"posted": False}, app.request, app.notice

    async def request(method, route, *, params=None, json=None, **kwargs):
        if state["posted"]:
            raise READ_FAILS.get(how, BrokerUnreachable(503, LOST))
        if method == "POST" and route == path and not (json or {}).get("dry_run"):
            state["posted"] = True
            told.posts += 1
            if how == "refused":
                raise BrokerError(403, REFUSAL)
            if how == "lost":
                raise BrokerUnreachable(503, LOST)
        return await original(method, route, params=params, json=json, **kwargs)

    def spy(value, kind="info"):
        if state["posted"]:
            told.append(str(getattr(value, "plain", value)))
        return notice(value, kind)

    app.request, app.notice = request, spy
    return told


def test_the_new_sentence_has_polish():
    assert tr(NOT_RELOADED, "pl") != NOT_RELOADED


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("how", list(READ_FAILS))
@pytest.mark.parametrize("act", [a for a in ACTS if a.id in DONE], ids=ACT_IDS)
async def test_a_failed_reload_after_a_saved_act_is_told_as_saved(tmp_path, monkeypatch, act, how, language):
    app, workspace, sent = act.make(tmp_path, monkeypatch, language)
    told = after_the_request(app, act.expected(workspace)[0][0], how)
    async with app.run_test(size=(120, 40)) as pilot:
        await act.reach(app, pilot, act.text)
        await consent_drawn(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: told)
        sentence, fields = DONE[act.id]
        text = told[-1]
        assert tr(sentence, language, **fields) in text and tr(NOT_RELOADED, language) in text
        for wrong in SAYS.get(act.id, ()) + CLAIMS:
            assert tr(wrong, language) not in text
        assert len(sent()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("act", [a for a in ACTS if a.id in SAYS and a.id in DONE], ids=[
    a.id for a in ACTS if a.id in SAYS and a.id in DONE])
async def test_a_refusal_followed_by_a_failed_reload_is_still_not_done(tmp_path, monkeypatch, act):
    app, workspace, sent = act.make(tmp_path, monkeypatch, "en")
    told = after_the_request(app, act.expected(workspace)[0][0], "refused")
    async with app.run_test(size=(120, 40)) as pilot:
        await act.reach(app, pilot, act.text)
        await consent_drawn(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: told)
        text = told[-1]
        assert SAYS[act.id][0] in text and REFUSAL in text
        assert not any(claim in text for claim in CLAIMS) and tr(NOT_RELOADED) not in text
        assert told.posts == 1


SIDEBAR_SAVED = "Memory imported, but the view could not be refreshed."
SIDEBAR = {"refused": ("Memory was not imported.", REFUSAL),
           "lost": ("The service did not confirm the import, so the memory may have been imported.", None)}
# act: the sentence of a request that was refused, and of one whose answer was lost
KINDS = {"kind-accept": (NOT_ACCEPTED, ACCEPT_UNCLEAR), "kind-withdraw": (NOT_WITHDRAWN, WITHDRAW_UNCLEAR)}


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("how", list(READ_FAILS))
async def test_a_sidebar_import_whose_refresh_fails_is_told_as_imported(tmp_path, monkeypatch, how, language):
    act = next(a for a in ACTS if a.id == "sidebar-import")
    app, workspace, sent = act.make(tmp_path, monkeypatch, language)
    told = after_the_request(app, act.expected(workspace)[0][0], how)
    async with app.run_test(size=(120, 40)) as pilot:
        await act.reach(app, pilot, act.text)
        await consent_drawn(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: told)
        assert tr(SIDEBAR_SAVED, language) in told[-1] and "Broker" not in told[-1]
        assert len(sent()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("how", ["refused", "lost"])
@pytest.mark.parametrize("act_id", ["sidebar-import", "kind-accept", "kind-withdraw"])
async def test_a_refused_or_lost_request_of_these_acts_says_not_done_or_may_have_been(
        tmp_path, monkeypatch, act_id, how, language):
    act = next(a for a in ACTS if a.id == act_id)
    app, workspace, sent = act.make(tmp_path, monkeypatch, language)
    told = after_the_request(app, act.expected(workspace)[0][0], how)
    async with app.run_test(size=(120, 40)) as pilot:
        await act.reach(app, pilot, act.text)
        await consent_drawn(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: told)
        sentence = (SIDEBAR[how] if act_id == "sidebar-import" else
                    (KINDS[act_id][0], REFUSAL) if how == "refused" else (KINDS[act_id][1], None))[0]
        assert tr(sentence, language) in told[-1]
        assert told.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_an_unclear_verdict_does_not_say_the_list_was_reloaded_when_the_reload_failed(
        tmp_path, monkeypatch, language):
    act = next(a for a in ACTS if a.id == "verdict")
    app, workspace, sent = act.make(tmp_path, monkeypatch, language)
    told, notice = [], app.notice

    def spy(value, kind="info"):
        told.append(str(getattr(value, "plain", value)))
        return notice(value, kind)

    def lost(method, path, *, sent=None, **kwargs):
        if sent is not None:
            sent.append(True)
        raise BrokerUnreachable(503, LOST)

    app.notice = spy
    async with app.run_test(size=(120, 40)) as pilot:
        await act.reach(app, pilot, act.text)
        await consent_drawn(app, pilot)
        app.impact_request = lost
        await pilot.click("#confirm-yes")
        claim = ("No clear answer from the memory service. The verdict may have been saved; the list was reloaded and "
                 "shows it if so. Saving the same verdict again is safe and stores nothing twice.")
        plain = ("No clear answer from the memory service. The verdict may have been saved. Saving the same verdict "
                 "again is safe and stores nothing twice.")
        assert tr(claim, language) != tr(plain, language)
        await wait_until(pilot, lambda: any(tr(plain, language) in line for line in told))
        assert not any(tr(claim, language) in line for line in told)
