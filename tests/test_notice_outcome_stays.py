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
import threading

import pytest
from consent_steps import consent_drawn
from test_person_acts_consent import ACTS
from test_terminal_panels import setup
from test_workspace_stage_scope import wait_until

from textual.widgets import Button

from cairnmind import terminal_ui
from cairnmind.terminal_client import BrokerError
from cairnmind.terminal_i18n import tr
from cairnmind.terminal_i18n_messages import broker_error_text
from cairnmind.terminal_inspector import InspectorApp
from cairnmind.terminal_panels import SidebarApp
from cairnmind.terminal_ui import BACKGROUND, OUTCOME_HOLD

DOWN = "refresh-is-down-marker"
# act, language; the inspector acts get their refresh error from the journal, the first one also from a reload
# The verdict is not here: it is sent by impact_request on a thread of its own, so it has its own test below.
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
        await outcome_then_errors(app, pilot, now, how, order, told, told[-1])


async def outcome_then_errors(app, pilot, now, how, order, told, outcome):
    """The act's outcome is on the line (or about to be); the refresh error comes after it, in the order given."""
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
@pytest.mark.parametrize("order", ["refresh-error-first", "refresh-error-last"])
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_the_outcome_of_a_verdict_stays_too(tmp_path, monkeypatch, language, order):
    # The verdict is sent by impact_request on a thread of its own (a worker of group impact-verdict), not by
    # app.request: its harness is the impact fake's own gate, a threading.Event the request thread waits on.
    act = next(a for a in ACTS if a.id == "verdict")
    app, workspace, sent = act.make(tmp_path, monkeypatch, language)
    impact = app.broker.request.__self__
    release, now, told = threading.Event(), [0.0], Told()
    impact.post_gates.append(release)
    monkeypatch.setattr(terminal_ui, "_clock", lambda: now[0])
    notice = app.notice

    def spy(value, kind="info"):
        if impact.posted and not BACKGROUND.get():
            told.append(str(getattr(value, "plain", value)))
        return notice(value, kind)

    app.notice = spy
    async with app.run_test(size=(120, 40)) as pilot:
        await act.reach(app, pilot, act.text)
        await consent_drawn(app, pilot)
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: impact.posted)  # the request thread is waiting on the gate
        if order == "refresh-error-first":
            refresh(app, "")
            await wait_until(pilot, lambda: line(app) == refresh_text(app, ""))
            assert not told
        release.set()
        saved = tr("Verdict saved: {verdict}. No record, status or task was changed.", language,
                   verdict=tr("still valid", language))
        await wait_until(pilot, lambda: saved in told)
        await app.workers.wait_for_complete()
        assert told[-1] == saved  # the last thing the act told, after its own reload of the list
        await outcome_then_errors(app, pilot, now, "", order, told, saved)


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
        background("another refresh error")
        assert line(app) == "another refresh error"  # a refresh error is no outcome: it holds nothing
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
        # an outcome that took the line since: the waiting notice stays until the last hold ends, then shows
        app.notice("outcome two")
        background(app, "waits again")
        app.notice("outcome three")
        assert line(app) == "outcome three"
        now[0] += OUTCOME_HOLD + 1
        app._hold_over()
        assert line(app) == "waits again"
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
@pytest.mark.parametrize("order", ["state-first", "outcome-first"])
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_the_workspaces_own_unavailable_state_yields_to_the_outcome(tmp_path, monkeypatch, language, order):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    down = {"broker_available": False, "last_error": "The client service is unavailable; running sessions are kept."}
    async with app.run_test(size=(120, 40)):
        await app.workers.wait_for_complete()
        if order == "state-first":
            workspace.update(down)
            await app._poll_workspace()
            assert line(app) != "outcome"  # nothing stands on the line: the state is shown as before
            app.notice("outcome")
        else:
            app.notice("outcome")
            workspace.update(down)
            await app._poll_workspace()
        assert line(app) == "outcome"
        assert app._outcome is not None and app._outcome[0] == app.notice_history.on_line().seq  # the state started no hold
        assert any(e.text == tr(app.launch_notice(workspace.read()), language) for e in app.notice_history.entries)


@pytest.mark.asyncio
async def test_a_background_inspector_reload_that_dies_yields_to_the_outcome(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(120, 40)) as pilot:
        await app.workers.wait_for_complete()

        async def dies(*args, **kwargs):
            raise KeyError("reload-died")

        monkeypatch.setattr(app, "load_page", dies)
        app.notice("outcome")
        app.refresh_if_list()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert line(app) == "outcome"
        assert any("reload-died" in e.text for e in app.notice_history.entries)
        await pilot.press("shift")  # the person moved on: a foreground read that dies is shown at once
        app.refresh_page()
        await wait_until(pilot, lambda: "reload-died" in line(app))


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


@pytest.mark.asyncio
async def test_a_hint_that_replaced_the_outcome_holds_nothing_and_a_held_notice_is_published(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    now = [0.0]
    monkeypatch.setattr(terminal_ui, "_clock", lambda: now[0])
    async with app.run_test(size=(120, 40)):
        await app.workers.wait_for_complete()
        published = []
        monkeypatch.setattr(app, "publish_notices", lambda **kwargs: published.append(kwargs))
        app.notice("outcome")
        background(app, "held error")
        assert published[-1:] == [{"later": True}] and len(published) == 2  # the held one goes to the activity copy too
        app.leave_hint("a key hint")  # something else than a notice took the line from the outcome
        background(app, "refresh error")
        assert line(app) == "refresh error"
        app.notice("outcome two")
        background(app, "held again")
        app.leave_hint("another hint")
        now[0] += OUTCOME_HOLD + 1
        app._hold_over()
        assert line(app) == "another hint"  # the waiting notice does not replace what stands there now


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_the_sidebars_mcp_start_failure_is_told_once_and_shows_when_the_hold_ends(tmp_path, monkeypatch,
                                                                                     language):
    from test_mcp_failure_notice import api_run, sidebar_with
    runs = []
    app, project = await sidebar_with(tmp_path, monkeypatch, lambda: runs, language)
    now = [0.0]
    monkeypatch.setattr(terminal_ui, "_clock", lambda: now[0])
    async with app.run_test(size=(120, 35)):
        await app.workers.wait_for_complete()
        app.notice("outcome")
        runs[:] = [api_run(project, [{"server_id": "gone", "code": "start_failed"}])]
        for _ in range(3):  # the sidebar's tick: the source tells it once, not once per tick
            app.refresh_sidebar()
            await app.workers.wait_for_complete()
        assert line(app) == "outcome"
        entries = [e for e in app.notice_history.entries if "gone" in e.text]
        assert len(entries) == 1 and entries[0].count == 1 and entries[0].kind == "warn"
        assert entries[0].project_id == project["id"]
        now[0] += OUTCOME_HOLD + 1
        app._hold_over()
        assert line(app) == entries[0].text  # told once, so nothing else would ever show it
        assert app.query_one("#panel-notice").has_class("-warn")
        assert app.notice_history.on_line() is entries[0]


@pytest.mark.asyncio
async def test_the_graphs_interrupted_connection_is_told_once_and_shows_when_the_hold_ends(tmp_path, monkeypatch):
    from cairnmind.config import Settings
    from cairnmind.terminal_graph import GraphApp
    from cairnmind.workspace_state import WorkspaceState
    gate, down = asyncio.Event(), []

    class Broker:
        def ensure(self):
            return {"available": True}

        def request(self, method, path, **kwargs):
            if down:
                raise OSError("graph-read-down")
            return {"nodes": [], "edges": [], "groups": []}

        async def events(self, **kwargs):
            await gate.wait()
            yield {"event": "unavailable", "data": {}}
            await asyncio.Queue().get()

    settings = Settings(tmp_path / "data")
    root = tmp_path / "project"
    root.mkdir()
    workspace = WorkspaceState(settings, "outcome-graph")
    workspace.select_project({"id": "graph-project", "name": "Graph", "root": str(root)}, intent="inspect")
    workspace.update({"focus": "graph"})
    monkeypatch.setattr("cairnmind.terminal_graph.tmux_visible", lambda *args: True)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: "en")
    now = [0.0]
    monkeypatch.setattr(terminal_ui, "_clock", lambda: now[0])
    app = GraphApp(settings, broker=Broker(), workspace=workspace, motion=False)
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause(0.2)
        app.notice("outcome")
        gate.set()
        sentence = "The connection to memory was interrupted; the view and camera were kept."
        await wait_until(pilot, lambda: any(e.text == sentence for e in app.notice_history.entries))
        assert line(app) == "outcome"
        assert [e.count for e in app.notice_history.entries if e.text == sentence] == [1]
        # the event-driven reload that fails is a background notice too: it waits in the history
        down.append(True)
        app._dirty = True
        await wait_until(pilot, lambda: any("graph-read-down" in e.text for e in app.notice_history.entries))
        assert line(app) == "outcome"
        now[0] += OUTCOME_HOLD + 1
        app._hold_over()
        assert "graph-read-down" in line(app)  # the newest one that waited


@pytest.mark.asyncio
@pytest.mark.parametrize("panel", ["sidebar", "inspector"])
async def test_a_refresh_that_recovered_leaves_nothing_to_show_when_the_hold_ends(tmp_path, monkeypatch, panel):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = (SidebarApp if panel == "sidebar" else InspectorApp)(settings, broker=broker, workspace=workspace)
    now = [0.0]
    monkeypatch.setattr(terminal_ui, "_clock", lambda: now[0])
    async with app.run_test(size=(120, 40)):
        await app.workers.wait_for_complete()
        healthy, down = app.request, [True]

        async def request(*args, **kwargs):
            if down[0]:
                raise BrokerError(503, "probe-down")
            return await healthy(*args, **kwargs)

        app.request = request

        async def refresh_once():
            app.refresh_sidebar() if panel == "sidebar" else app.refresh_page(background=True)
            await app.workers.wait_for_complete()

        app.notice("outcome")
        await refresh_once()  # fails: waits for the end of the hold
        assert line(app) == "outcome" and app._held is not None
        down[0] = False
        await refresh_once()  # the service is back
        now[0] += OUTCOME_HOLD + 1
        app._hold_over()
        assert "probe-down" not in line(app)


@pytest.mark.asyncio
async def test_a_notice_that_waited_before_the_line_was_cleared_is_not_shown_after_a_later_outcome(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    now = [0.0]
    monkeypatch.setattr(terminal_ui, "_clock", lambda: now[0])
    async with app.run_test(size=(120, 40)):
        await app.workers.wait_for_complete()
        app.notice("outcome")
        background(app, "old error")
        app.notice("")  # the line was cleared
        app.notice("outcome two")
        now[0] += OUTCOME_HOLD + 1
        app._hold_over()
        assert line(app) == "outcome two"


@pytest.mark.asyncio
async def test_an_older_waiting_notice_is_not_shown_after_a_newer_background_notice_reached_the_line(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    now = [0.0]
    monkeypatch.setattr(terminal_ui, "_clock", lambda: now[0])
    async with app.run_test(size=(120, 40)):
        await app.workers.wait_for_complete()
        app.notice("outcome")
        background(app, "E1")  # waits
        app.leave_hint("a hint")  # takes the line from the outcome
        background(app, "E2")  # nothing holds: shown at once
        assert line(app) == "E2"
        app.notice("outcome two")
        now[0] += OUTCOME_HOLD + 1
        app._hold_over()
        assert line(app) == "outcome two"


@pytest.mark.asyncio
async def test_the_record_kinds_check_run_by_the_poll_yields_to_the_outcome(tmp_path, monkeypatch):
    from cairnmind.terminal_record_kinds import _announce
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    now = [0.0]
    monkeypatch.setattr(terminal_ui, "_clock", lambda: now[0])
    async with app.run_test(size=(120, 40)):
        await app.workers.wait_for_complete()

        async def request(*args, **kwargs):
            return {"blocking": [{"client": "probe_client", "kind": "probe_kind"}]}

        app.request = request
        app.notice("outcome")
        app.run_worker(_announce(app, "p1", {"project_name": "P"}), exit_on_error=False)
        await app.workers.wait_for_complete()
        assert line(app) == "outcome"
        now[0] += OUTCOME_HOLD + 1
        app._hold_over()
        assert "probe_kind" in line(app)  # it waited, as a refresh error does
