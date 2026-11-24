"""The outcome of a person's act stays on the notice line against the error of a background refresh (#430).

The sidebar refreshes every 3 s, the inspector reloads on the memory journal, the graph on its events; when one fails
its error used to replace the sentence that told how the act just made ended. Now the outcome holds for
OUTCOME_HOLD seconds or until a key or a click; the refresh error goes to the notice history meanwhile (one entry with
a count) and shows on the line after the hold if it still fails. Both orders are forced for every act of the two
panels that have an act and a refresh. Headless Textual with fake brokers: no tmux, engine or model; the clock the
hold reads is replaced, no sleep. Fast tier.
"""
from __future__ import annotations

import asyncio

import pytest
from consent_steps import consent_drawn
from test_person_acts_consent import ACTS
from test_terminal_panels import setup
from test_workspace_stage_scope import wait_until

from textual.widgets import Button

from cairnmind import terminal_ui
from cairnmind.terminal_client import BrokerError
from cairnmind.terminal_i18n_messages import broker_error_text
from cairnmind.terminal_inspector import InspectorApp
from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_ui import BACKGROUND, OUTCOME_HOLD

DOWN = "refresh-is-down-marker"
# act, language; the inspector acts get their refresh error from the journal, the first one also from a reload
# The verdict is left out: it is sent by impact_request on a thread of its own and its view is not reloaded by the
# journal; it tells through the same notice().
LANGUAGES = [(act.id, "en", "") for act in ACTS if act.id != "verdict"] + [("kind-accept", "pl", ""), ("review-confirm", "pl", ""),
                                                    ("review-confirm", "en", "reload")]


class Told(list):
    posts = 0


def spy_on_the_line(app, path, gate):
    """After the act's request every read fails with DOWN (`gate` holds the act's answer until it is set). Returns
    the outcome sentences: what was told after the request by anything but a background worker."""
    told, state, original, notice = Told(), {"posted": False}, app.request, app.notice

    async def request(method, route, *, params=None, json=None, **kwargs):
        if state["posted"]:
            raise BrokerError(503, DOWN)
        if method == "POST" and route == path and not (json or {}).get("dry_run"):
            state["posted"] = True
            told.posts += 1
            await gate.wait()
        return await original(method, route, params=params, json=json, **kwargs)

    def spy(value, kind="info"):
        if state["posted"] and not BACKGROUND.get():
            told.append(str(getattr(value, "plain", value)))
        return notice(value, kind)

    app.request, app.notice = request, spy
    return told


def line(app):
    return str(app.query_one("#panel-notice").render())


async def journal_down(*args, **kwargs):
    raise OSError(DOWN)
    yield


def refresh(app, how):
    if isinstance(app, SidebarApp):
        app.refresh_sidebar()
    elif how == "reload":
        app.refresh_page(background=True)
    else:
        app.broker.events = journal_down
        app.watch_journal()


def seen_by(app, told):
    return list(told), [(e.text, e.kind, e.count) for e in app.notice_history.entries]


def refresh_text(app, how):
    """What the background refresh tells: the sidebar's and a reload's Broker error, the journal's own sentence."""
    if isinstance(app, SidebarApp) or how == "reload":
        return broker_error_text(BrokerError(503, DOWN), app.t)
    return app.t("Memory updates") + ": " + DOWN


def errors_in_history(app, how):
    return [entry for entry in app.notice_history.entries if entry.text == refresh_text(app, how)]  # an act may tell it too


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["refresh-error-first", "refresh-error-last"])
@pytest.mark.parametrize("act_id, language, how", LANGUAGES)
async def test_the_outcome_stays_and_the_refresh_error_waits_in_the_history(tmp_path, monkeypatch, act_id, language,
                                                                          how, order):
    act = next(a for a in ACTS if a.id == act_id)
    app, workspace, sent = act.make(tmp_path, monkeypatch, language)
    now = [0.0]
    monkeypatch.setattr(terminal_ui, "_clock", lambda: now[0])
    gate = asyncio.Event()
    told = spy_on_the_line(app, act.expected(workspace)[0][0], gate)
    async with app.run_test(size=(120, 40)) as pilot:
        await act.reach(app, pilot, act.text)
        await consent_drawn(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: told.posts)
        if order == "refresh-error-first":
            refresh(app, how)
            # nothing stands on the line yet: the error is shown as before
            await wait_until(pilot, lambda: line(app) == refresh_text(app, how))
            assert not told
        gate.set()
        await wait_until(pilot, lambda: told)
        outcome = told[-1]
        assert outcome != refresh_text(app, how)  # the act's sentence, not its own raw broker error
        if order == "refresh-error-first":
            assert line(app) == outcome  # an outcome replaces the refresh error at once
            refresh(app, how)
        else:
            seen = len(errors_in_history(app, how))
            refresh(app, how)
            await wait_until(pilot, lambda: len(errors_in_history(app, how)) > seen)
        await wait_until(pilot, lambda: errors_in_history(app, how))
        await app.workers.wait_for_complete()
        assert line(app) == outcome
        # a repeated refresh error is one entry with its count, not one entry per tick
        def held():
            return len(errors_in_history(app, how)), sum(e.count for e in errors_in_history(app, how))

        for _ in range(2):
            refresh(app, how)
            await app.workers.wait_for_complete()
        entries, counted = held()
        for _ in range(3):
            refresh(app, how)
            await app.workers.wait_for_complete()
        assert held() == (entries, counted + 3), seen_by(app, told)
        assert line(app) == outcome
        # after the hold the error that still holds shows on the line
        now[0] += OUTCOME_HOLD + 1
        refresh(app, how)
        await wait_until(pilot, lambda: line(app) == refresh_text(app, how))
        assert held() == (entries, counted + 4)


@pytest.mark.asyncio
@pytest.mark.parametrize("panel", ["sidebar", "inspector"])
async def test_a_key_releases_the_hold_and_a_second_outcome_replaces_the_first(tmp_path, monkeypatch, panel):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: "en")
    app = (SidebarApp if panel == "sidebar" else InspectorApp)(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()

        def background(text):
            token = BACKGROUND.set(True)
            try:
                app.notice(text, "error")
            finally:
                BACKGROUND.reset(token)

        background("first refresh error")
        assert line(app) == "first refresh error"  # no outcome on the line: shown as before
        app.notice("outcome one")
        background("second refresh error")
        assert line(app) == "outcome one"
        app.notice("outcome two")
        assert line(app) == "outcome two"  # an outcome replaces an outcome at once
        background("second refresh error")
        assert line(app) == "outcome two"
        await pilot.press("shift")
        background("second refresh error")
        assert line(app) == "second refresh error"  # the person moved on: nothing holds
        app.notice("outcome three")
        await pilot.click("#panel-notice")
        background("third refresh error")
        assert line(app) == "third refresh error"


def background(app, text, kind="error"):
    app.notice_background(text, kind)


@pytest.mark.asyncio
async def test_a_reload_that_succeeds_does_not_replace_the_outcome_with_its_count(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        count = line(app)
        app.notice("outcome of the act")
        app.refresh_if_list()  # the journal's reload, healthy
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert line(app) == "outcome of the act"
        await pilot.press("shift")
        app.refresh_if_list()
        await app.workers.wait_for_complete()
        assert line(app) == count  # nothing holds: the count is read as before


@pytest.mark.asyncio
@pytest.mark.parametrize("panel", ["sidebar", "inspector"])
async def test_a_notice_that_waited_shows_when_the_hold_ends(tmp_path, monkeypatch, panel):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = (SidebarApp if panel == "sidebar" else InspectorApp)(settings, broker=broker, workspace=workspace)
    now = [0.0]
    monkeypatch.setattr(terminal_ui, "_clock", lambda: now[0])
    async with app.run_test(size=(120, 40)):
        await app.workers.wait_for_complete()
        app.notice("outcome")
        background(app, "told once")
        background(app, "newest, told once")
        assert line(app) == "outcome"
        app._hold_over()  # the timer, too early: the clock has not reached the deadline
        assert line(app) == "outcome"
        now[0] += OUTCOME_HOLD + 1
        app._hold_over()
        assert line(app) == "newest, told once"
        entries = {e.text: (e.count, e.seq) for e in app.notice_history.entries}
        assert entries["newest, told once"][0] == 1 and entries["told once"][0] == 1  # told once: not counted twice
        assert app.notice_history.on_line().text == "newest, told once"
        assert app.query_one("#panel-notice").has_class("-error")
        # an outcome that took the line since: the waiting notice does not replace it
        app.notice("outcome two")
        background(app, "waits again")
        app.notice("outcome three")
        now[0] += OUTCOME_HOLD + 1
        app._hold_over()
        assert line(app) == "outcome three"
        # a background notice that clears the failure cancels the waiting one
        app.notice("outcome four")
        background(app, "failing")
        background(app, "")
        app._release_outcome()
        assert line(app) == "outcome four"


@pytest.mark.asyncio
async def test_the_hold_ends_on_its_own_timer(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    monkeypatch.setattr(terminal_ui, "OUTCOME_HOLD", 0.3)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        app.notice("outcome")
        background(app, "the journal ended")
        assert line(app) == "outcome"
        await wait_until(pilot, lambda: line(app) == "the journal ended")


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["mouse", "typing", "button"])
async def test_what_a_person_does_in_a_field_or_on_a_button_ends_the_hold(tmp_path, monkeypatch, how):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        app.notice("outcome")
        if how == "mouse":
            await pilot.click("#project-search")
        elif how == "typing":
            app.query_one("#project-search").focus()
            await pilot.press("x")
        else:
            app.post_message(Button.Pressed(Button("x")))
            await pilot.pause()
        await pilot.pause()
        background(app, "refresh error")
        assert line(app) == "refresh error"


@pytest.mark.asyncio
async def test_the_workspace_poll_and_a_dying_refresh_worker_yield_too(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()
        app.notice("outcome")

        def unreadable():
            raise OSError("state-unreadable")

        monkeypatch.setattr(app.workspace, "read", unreadable)
        await app._poll_workspace()
        assert line(app) == "outcome"
        assert [e.text for e in app.notice_history.entries][-1] == "state-unreadable"
        before = app._outcome
        app.notice("outcome two")
        assert app._outcome != before  # the poll's error started no hold of its own; the outcome did

        async def dies():
            raise KeyError("worker-died")

        app.run_worker(dies(), group="sidebar-refresh", exit_on_error=False)
        await pilot.pause(0.3)
        assert line(app) == "outcome two"
        app.run_worker(dies(), group="an-act", exit_on_error=False)
        await wait_until(pilot, lambda: "worker-died" in line(app))  # an act's own failure is shown at once


@pytest.mark.asyncio
async def test_an_outcome_that_left_the_line_holds_nothing(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)):
        await app.workers.wait_for_complete()
        app.notice("outcome")
        app.notice("")  # the line was cleared
        token = BACKGROUND.set(True)
        try:
            app.notice("refresh error", "error")
        finally:
            BACKGROUND.reset(token)
        assert line(app) == "refresh error"
