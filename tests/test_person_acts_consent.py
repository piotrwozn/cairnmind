"""A person's act is sent only from a consent dialog (#332).

One table of every act a workspace panel sends to a path of person_proof.PERSON_BROKER_PATH: the form or choice
before the dialog sends nothing, a key made before the dialog was drawn answers nothing, the approval button sends
exactly the expected request, Cancel and Esc send nothing, and the dialog fits and scrolls at 80 x 24.

Headless Textual with fake brokers: no tmux, engine, Docker or model, so the same on macOS; fast tier. Never call
settle() while a dialog is open: the act's worker is parked on it (consent_steps.consent_drawn waits instead).
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable

import pytest
from conftest import plain_words
from consent_steps import consent_drawn, early_enter_sends_nothing
from test_inspector_plan_confirm import plan_app
from test_inspector_published_import import published_app
from test_inspector_release import RELEASE, release_app
from test_legacy_groups_workspace import ui as legacy_ui
from test_record_kinds_workspace import ACCEPTED, BLOCKING
from test_record_kinds_workspace import ui as kinds_ui
from test_terminal_impact import VERDICT, Impact, open_impact
from test_terminal_panels import setup, settle
from test_terminal_ux080 import CONFLICT, screen_text, setup_ui
from test_workspace_stage_scope import wait_until
from textual.widgets import Button, Select, Static, TextArea

from cairnmind import (terminal_person_act, terminal_plan_confirm, terminal_record_kinds, terminal_release)
from cairnmind.terminal_i18n import CATALOG, tr
from cairnmind.terminal_inspector import InspectorApp
from cairnmind.terminal_panels import ConfirmModal, SidebarApp
from cairnmind.terminal_person_act import NOT_GIVEN, agreed, record_line, typed
from cairnmind.terminal_ui import ChoiceModal, FormModal

REASON = "Read the source."
LAST = "LAST-LINE-OF-THE-TEXT"
LONG = "\n".join([f"text line {number}" for number in range(1, 40)] + [LAST])


def words(app):
    return plain_words(screen_text(app))


async def form_ready(app, pilot):
    await wait_until(pilot, lambda: isinstance(app.screen, FormModal) and app.screen.is_mounted)


async def choice_ready(app, pilot):
    await wait_until(pilot, lambda: isinstance(app.screen, ChoiceModal) and app.screen.is_mounted)


async def type_into(app, pilot, field, text):
    widget = app.screen.query_one("#field-" + field)
    if isinstance(widget, TextArea):
        widget.text = text
    else:
        widget.value = text


# --- one factory per act: (tmp_path, monkeypatch, language) -> (app, workspace, sent) ------------------------

def posts_of(broker, suffix):
    return lambda: [(path, body) for method, path, _, body in broker.calls
                    if method == "POST" and path.endswith(suffix) and body.get("dry_run") is not True]


def make_review(tmp_path, monkeypatch, language):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    return InspectorApp(settings, broker=broker, workspace=workspace), workspace, posts_of(broker, "/review")


def reach_review(action):
    async def reach(app, pilot, text):
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        app.review(action)
        await form_ready(app, pilot)
        await type_into(app, pilot, "reason", text)
        await pilot.press("ctrl+s")
    return reach


def make_conflict(tmp_path, monkeypatch, language):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    original, posted = broker.request, []

    def request(method, path, *, params=None, json=None, timeout=20):
        if path == "/api/inspector/conflicts":
            return {"items": [dict(CONFLICT)], "total": 1, "next_cursor": None}
        if path == "/api/inspector/conflicts/resolve":
            posted.append((path, dict(json)))
            return {"decision": json["action"]}
        return original(method, path, params=params, json=json, timeout=timeout)

    broker.request = request
    return InspectorApp(settings, broker=broker, workspace=workspace), workspace, lambda: list(posted)


async def reach_conflict(app, pilot, text):
    await settle(app, pilot)
    app.set_view("conflicts")
    await app.load_page()
    await app.open_entity("new-1:old-1", "conflicts")
    app.resolve_conflict()
    await choice_ready(app, pilot)
    await pilot.press("enter")  # Keep A
    await form_ready(app, pilot)
    await type_into(app, pilot, "reason", text)
    await pilot.press("ctrl+s")


def make_apply(tmp_path, monkeypatch, language):
    settings, broker, workspace = setup(tmp_path, monkeypatch, project=False)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    return InspectorApp(settings, broker=broker, workspace=workspace), workspace, posts_of(broker, "/api/memory/import")


async def reach_apply(app, pilot, text):
    await settle(app, pilot)
    app.action_snapshot()
    await settle(app, pilot)
    app.memory_operation("import")
    await form_ready(app, pilot)
    await type_into(app, pilot, "path", "/projects/a/.memory/devbrain")
    await type_into(app, pilot, "root", "/projects/a")
    await pilot.press("ctrl+s")
    await wait_until(pilot, lambda: app.pending_import)
    await settle(app, pilot)
    app.memory_operation("apply")


def make_plan(tmp_path, monkeypatch, language):
    app, broker, workspace = plan_app(tmp_path, monkeypatch, language)
    return app, workspace, posts_of(broker, "/confirm")


async def reach_plan(app, pilot, text):
    await settle(app, pilot)
    app.query_one("#inspector-view", Select).value = "plans"
    await settle(app, pilot)
    app.open_selected("plan-a", "plans")
    await settle(app, pilot)
    app.review("confirm")
    await form_ready(app, pilot)
    await type_into(app, pilot, "reason", text)
    await pilot.press("ctrl+s")


def make_release(tmp_path, monkeypatch, language):
    app, broker, workspace = release_app(tmp_path, monkeypatch, language)
    return app, workspace, posts_of(broker, "/release")


async def reach_release(app, pilot, text):
    await settle(app, pilot)
    app.open_selected("record-a")
    await settle(app, pilot)
    app.review("release")
    await form_ready(app, pilot)
    await type_into(app, pilot, "reason", text)
    await pilot.press("ctrl+s")


def make_verdict(tmp_path, monkeypatch, language):
    settings, broker, workspace = setup_ui(tmp_path, monkeypatch, language)
    impact = Impact(broker, workspace)
    return (InspectorApp(settings, broker=broker, workspace=workspace), workspace,
            lambda: [(VERDICT, body) for body in impact.posted])


async def reach_verdict(app, pilot, text):
    await open_impact(app, pilot)
    app.record_impact_verdict()
    await choice_ready(app, pilot)
    await pilot.press("enter")  # still valid
    await form_ready(app, pilot)
    await type_into(app, pilot, "note", text)
    await pilot.press("ctrl+s")


def make_kinds(view):
    def make(tmp_path, monkeypatch, language):
        app, broker, workspace = kinds_ui(tmp_path, monkeypatch, view, language)
        return app, workspace, lambda: [(path, body) for _, path, body in broker.sent]
    return make


async def reach_kind_accept(app, pilot, text):
    await settle(app, pilot)
    app.record_kinds()
    await choice_ready(app, pilot)
    await pilot.press("enter")
    await form_ready(app, pilot)
    await type_into(app, pilot, "note", text)
    await pilot.press("ctrl+s")


async def reach_kind_withdraw(app, pilot, text):
    await settle(app, pilot)
    app.record_kinds()
    await choice_ready(app, pilot)
    await pilot.press("enter")


def make_adopt(tmp_path, monkeypatch, language):
    app, broker, workspace = legacy_ui(tmp_path, monkeypatch, language=language)
    return app, workspace, lambda: [(path, body) for _, path, body in broker.sent]


async def reach_adopt(app, pilot, text):
    await settle(app, pilot)
    app.legacy_groups()
    await choice_ready(app, pilot)
    await pilot.press("enter")
    await form_ready(app, pilot)
    await pilot.press("ctrl+s")


def make_sidebar_import(tmp_path, monkeypatch, language):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("cairnmind.terminal_ui.language_for", lambda settings: language)
    return SidebarApp(settings, broker=broker, workspace=workspace), workspace, posts_of(broker, "/api/memory/import")


async def reach_sidebar_import(app, pilot, text):
    await settle(app, pilot)
    app.import_memory()
    await form_ready(app, pilot)
    await type_into(app, pilot, "path", "/projects/a/.memory/devbrain")
    await type_into(app, pilot, "root", "/projects/a")
    await pilot.press("ctrl+s")


def make_team(tmp_path, monkeypatch, language):
    app, broker, workspace = published_app(tmp_path, monkeypatch, language)
    return app, workspace, lambda: [(path, body) for path, body in broker.writes() if body["dry_run"] is False]


async def reach_team(app, pilot, text):
    await settle(app, pilot)
    app.action_snapshot()
    await settle(app, pilot)
    app.memory_operation("published")
    await choice_ready(app, pilot)
    await pilot.press("enter")
    await form_ready(app, pilot)
    await pilot.press("ctrl+s")


@dataclass
class Act:
    id: str
    make: Callable
    reach: Callable
    title: str  # the dialog's heading, in English
    shown: tuple  # what the dialog's body holds, whatever the language
    expected: Callable  # workspace -> the requests the approval button sends
    text: str | None = None  # what is typed in the form's text field, where there is one

    def __repr__(self):
        return self.id


IMPORT = "/api/memory/import"
ACTS = [
    Act("review-confirm", make_review, reach_review("confirm"), "Confirm this record?",
        ("Validate token safely.",), lambda w: [("/api/inspector/records/record-a/review", {
            "project": "project-a", "action": "confirm", "expected_version": 3, "reason": REASON,
            "worktree": w.value["worktree"]})], REASON),
    Act("review-dispute", make_review, reach_review("dispute"), "Dispute this record?",
        ("Validate token safely.",), lambda w: [("/api/inspector/records/record-a/review", {
            "project": "project-a", "action": "dispute", "expected_version": 3, "reason": REASON,
            "worktree": w.value["worktree"]})], REASON),
    Act("conflict", make_conflict, reach_conflict, "Save this decision?",
        ("Sessions last 24 hours.", "Sessions expire after one hour."),
        lambda w: [("/api/inspector/conflicts/resolve", {
            "project": "project-keyboard", "record_id": "new-1", "other_id": "old-1", "action": "supersede",
            "keep": "new-1", "expected_version": 3, "other_expected_version": 5, "reason": REASON,
            "worktree": w.value["worktree"]})], REASON),
    Act("apply", make_apply, reach_apply, "Import this project memory?", (),
        lambda w: [(IMPORT, {"path": "/projects/a/.memory/devbrain", "root": "/projects/a", "dry_run": False,
                             "replace_confirmed": False})]),
    Act("plan", make_plan, reach_plan, "Confirm this plan?", ("Managed plan",),
        lambda w: [("/api/inspector/plans/plan-a/confirm", {
            "project": "project-a", "expected_plan_version": 5, "reason": REASON, "worktree": w.value["worktree"]})],
        REASON),
    Act("release", make_release, reach_release, "Release this record?", ("Validate token safely.",),
        lambda w: [(RELEASE, {"project": "project-a", "reason": REASON, "worktree": w.value["worktree"]})], REASON),
    Act("verdict", make_verdict, reach_verdict, "Save this verdict?", ("Sessions last 30 minutes.",),
        lambda w: [(VERDICT, {"project": "project-keyboard", "worktree": w.value["worktree"], "since": "HEAD",
                              "record_id": "rec-rule", "fingerprint": "a" * 64, "verdict": "still_valid",
                              "note": REASON})], REASON),
    Act("kind-accept", make_kinds({"blocking": [BLOCKING], "accepted": []}), reach_kind_accept,
        "Treat this kind as technical?", ("future-claude-record",),
        lambda w: [("/api/inspector/record-kinds/accept", {
            "project": "project-keyboard", "client": "claude", "kind": "future-claude-record", "note": REASON})],
        REASON),
    Act("kind-withdraw", make_kinds({"blocking": [], "accepted": [ACCEPTED]}), reach_kind_withdraw,
        "Withdraw an acceptance", ("Ada Nowak",),
        lambda w: [("/api/inspector/record-kinds/withdraw", {
            "project": "project-keyboard", "client": "codex", "kind": "future_codex_item"})]),
    Act("adopt", make_adopt, reach_adopt, "Adopt this task group into a plan?", ("Ship the importer",),
        lambda w: [("/api/inspector/legacy-groups/adopt", {
            "project": "project-keyboard", "group": "old-group", "title": "old-group",
            "task_versions": {"t1": 3, "t2": 1}, "worktree": w.value["worktree"]})]),
    Act("sidebar-import", make_sidebar_import, reach_sidebar_import, "Import this project memory?", (),
        lambda w: [(IMPORT, {"path": "/projects/a/.memory/devbrain", "root": "/projects/a", "dry_run": False,
                             "replace_confirmed": False})]),
    Act("team-memory", make_team, reach_team, "Import this team memory?", (),
        lambda w: [("/api/memory/import-published", {
            "path": "/projects/a/.memory/cairnmind-published", "project": "project-a", "take_published": [],
            "dry_run": False, "replace_confirmed": False, "expected_replaced": []})]),
]
EVERY_ACT = pytest.mark.parametrize("act", ACTS, ids=[act.id for act in ACTS])


def heading_of(modal):
    return str(modal.query_one(".heading", Static).render())


@pytest.mark.asyncio
@EVERY_ACT
@pytest.mark.parametrize("language", ["en", "pl"])
async def test_a_persons_act_is_sent_only_from_its_consent_dialog(tmp_path, monkeypatch, act, language):
    app, workspace, sent = act.make(tmp_path, monkeypatch, language)
    async with app.run_test(size=(120, 40)) as pilot:
        await act.reach(app, pilot, act.text)
        modal = await consent_drawn(app, pilot)
        assert sent() == []  # the form, the choice or the button sent nothing
        assert heading_of(modal) == tr(act.title, language)
        shown = words(app)
        for part in act.shown:
            assert part in shown, part
        if act.text:
            assert act.text in shown
        await early_enter_sends_nothing(app, pilot, modal)
        assert sent() == []  # a key made before the dialog was drawn answers nothing
        await pilot.press("enter")  # made now, on the approval button
        await wait_until(pilot, lambda: sent())
        await wait_until(pilot, lambda: not isinstance(app.screen, ConfirmModal))
        assert sent() == act.expected(workspace)


@pytest.mark.asyncio
@EVERY_ACT
@pytest.mark.parametrize("how", ["escape", "cancel"])
async def test_cancel_and_esc_send_nothing(tmp_path, monkeypatch, act, how):
    app, workspace, sent = act.make(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(120, 40)) as pilot:
        await act.reach(app, pilot, act.text)
        await consent_drawn(app, pilot)
        if how == "escape":
            await pilot.press("escape")
        else:
            await pilot.click("#confirm-no")
        await wait_until(pilot, lambda: not isinstance(app.screen, ConfirmModal))
        await settle(app, pilot)
        assert sent() == []


@pytest.mark.asyncio
@EVERY_ACT
async def test_the_dialog_fits_and_scrolls_at_80_by_24(tmp_path, monkeypatch, act):
    app, workspace, sent = act.make(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(80, 24)) as pilot:
        await act.reach(app, pilot, LONG if act.text else None)
        modal = await consent_drawn(app, pilot)
        for identifier in ("#confirm-yes", "#confirm-no"):
            region = modal.query_one(identifier, Button).region
            assert region.width > 0 and region.x >= 0 and region.right <= 80 and region.bottom <= 24, identifier
        if act.text:
            body = modal.query_one(".cm-dialog-body")
            assert body.max_scroll_y > 0
            body.scroll_end(animate=False)
            await pilot.pause()
            assert LAST in words(app)
        await pilot.click("#confirm-no")
        await wait_until(pilot, lambda: not isinstance(app.screen, ConfirmModal))
        await settle(app, pilot)
        assert sent() == []


@pytest.mark.asyncio
async def test_apply_is_refused_when_the_import_check_changed_while_the_dialog_was_open(tmp_path, monkeypatch):
    app, workspace, sent = make_apply(tmp_path, monkeypatch, "en")
    async with app.run_test(size=(120, 40)) as pilot:
        await reach_apply(app, pilot, None)
        await consent_drawn(app, pilot)
        app.pending_import = {**app.pending_import}  # a new check replaced the one the person read
        await pilot.click("#confirm-yes")
        await wait_until(pilot, lambda: not isinstance(app.screen, ConfirmModal))
        await wait_until(pilot, lambda: "Check the import with its source and destination folders first."
                         in words(app))
        assert sent() == []


# --- the helper itself --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agreed_is_true_only_for_an_explicit_yes():
    answers = [True, False, None, {"note": ""}, 1]
    seen = []

    async def push_screen_wait(screen):
        seen.append(screen)
        return answers.pop(0)

    app = SimpleNamespace(push_screen_wait=push_screen_wait)
    got = [await agreed(app, "Confirm this record?", "text", "Confirm review") for _ in range(5)]
    assert got == [True, False, False, False, False]
    assert all(isinstance(screen, ConfirmModal) and screen.consent for screen in seen)
    assert seen[0].heading == "Confirm this record?" and seen[0].approve == "Confirm review"


def test_every_new_sentence_has_polish():
    for module in (terminal_person_act, terminal_plan_confirm, terminal_release, terminal_record_kinds):
        for key, polish in module.MESSAGES.items():
            assert CATALOG[key] == polish, key
    for key in ("Confirm this record?", "Dispute this record?", "Save this decision?", "Save this verdict?",
                "Confirm this plan?", "Release this record?", "Treat this kind as technical?"):
        assert tr(key, "pl") != key
    assert tr(NOT_GIVEN, "pl", label=tr("Reason", "pl")) == "Uzasadnienie: nie podano."


def test_typed_text_is_shown_whole_and_escaped():
    def english(text, **values):
        return tr(text, "en", **values)

    shown = typed(english, "Reason", "a\x1b[31mb\n" + "x" * 3000)
    assert "\\x1b[31m" in shown and "\x1b" not in shown and "x" * 3000 in shown
    assert typed(english, "Reason", "  ") == "Reason: none given."
    line = record_line({"content": "first line\nsecond " + "y" * 500})
    assert "\n" not in line and len(line) <= 201
