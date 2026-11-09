"""Terminal panels use explicit intents, scoped projections and literal archived text."""
from __future__ import annotations

from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest
from textual.widgets import Button, DataTable, Input, Select, Static, TextArea

from devbrain.config import Settings
from devbrain.terminal_inspector import InspectorApp
from devbrain.terminal_panels import SidebarApp
from devbrain.terminal_safe import plain, safe_text
from devbrain.terminal_ui import wide_dialog


class FakeWorkspace:
    def __init__(self, project=None):
        self.value = {"revision": 1, "workspace_id": "default", "project_id": project["id"] if project else None,
            "project_name": project["name"] if project else None, "worktree": project["root"] if project else None,
            "selected_entity": None, "focus": "sidebar", "slots": [], "run_ids": []}
        self.selections = []

    def read(self):
        return deepcopy(self.value)

    def update(self, changes, expected_revision=None):
        self.value.update(deepcopy(changes))
        self.value["revision"] += 1
        return self.read()

    def select_project(self, project, worktree=None, *, intent="inspect", requested_slot_ids=None):
        self.selections.append((project["id"], worktree, intent))
        return self.update({"project_id": project["id"], "project_name": project["name"],
            "worktree": worktree or project["root"], "selected_entity": None, "intent": intent,
            "requested_slot_ids": requested_slot_ids or []})


class FakeBroker:
    def __init__(self, project, *, managed=False):
        self.project = project
        self.calls = []
        self.threads = []
        self.ensures = 0
        self.record = {"id": "record-a", "kind": "task", "key": "task-a", "content": "Validate token safely.",
            "status": "proposed", "version": 3, "source_ids": ["source-a"],
            "metadata": {"state": "needs_review", **({"plan_ref": "plan-a"} if managed else {})}}
        self.inspect_memory = None
        self.corrupt_import = False
        self.review_conflict = False

    def ensure(self):
        self.ensures += 1

    async def events(self, **kwargs):
        if False:
            yield {}

    def request(self, method, path, *, params=None, json=None, timeout=20):
        params = params or {}
        self.calls.append((method, path, deepcopy(params), deepcopy(json)))
        self.threads.append(threading.get_ident())
        if path == "/api/state":
            return {"projects": [self.project], "profiles": [{"id": "codex", "client": "codex", "label": "Codex"}],
                    "settings": {"default_slots": [{"id": "codex", "profile_id": "codex"}], "scan_roots": []}, "runs": []}
        if path == "/api/projects/inspect":
            return {"root": json["root"], "memory_snapshot": self.inspect_memory}
        if path == "/api/projects":
            return {"project": self.project}
        if path == "/api/inspector/records":
            return {"items": [deepcopy(self.record)], "next_cursor": None, "total": 1}
        if path == "/api/inspector/records/record-a":
            return {"record": deepcopy(self.record), "verification": {"status": "reported"}}
        if path == "/api/inspector/records/record-a/review":
            if self.review_conflict:
                self.record["version"] += 1
                raise ValueError("Broker 409: record version conflict")
            self.record.update(version=self.record["version"] + 1, status="confirmed" if json["action"] == "confirm" else "disputed")
            return {"record": deepcopy(self.record)}
        if path == "/api/inspector/plans/plan-a":
            return {"item": {"id": "plan-a", "version": 5, "title": "Managed plan"}}
        if path == "/api/inspector/sources":
            return {"items": [{"id": "source-a", "kind": "transcript", "uri": "conversation://a"}], "next_cursor": None}
        if path == "/api/inspector/sources/source-a":
            return {"source": {"id": "source-a", "kind": "transcript", "uri": "conversation://a", "sha256": "source-sha"},
                    "text": "next-page" if params.get("offset") else "\x1b]52;c;secret\x07RAW", "next_offset": None if params.get("offset") else 16}
        if path.endswith("/revisions"):
            return {"items": [{"id": "record-a:1", "version": 1, "snapshot": {"content": "Earlier exact state."}}], "next_cursor": None}
        if path == "/api/memory/status":
            return {"path": "/projects/a/.memory/devbrain/manifest.json", "latest": None, "auto": {"enabled": False, "interval_minutes": 15}}
        if path == "/api/memory/import":
            if self.corrupt_import:
                raise ValueError("Corrupt memory source snapshot")
            return {"status": "dry_run" if json["dry_run"] else "imported", "project": self.project, "verification": "historical"}
        return {"items": [], "next_cursor": None}


def setup(tmp_path, monkeypatch, *, managed=False, project=True):
    value = {"id": "project-a", "name": "Project A", "root": str(tmp_path / "project")}
    broker = FakeBroker(value, managed=managed)
    workspace = FakeWorkspace(value if project else None)
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr("devbrain.workspace.refresh_workspace", lambda *args, **kwargs: workspace.read())
    monkeypatch.setattr("devbrain.terminal_ui.language_for", lambda settings: "pl")
    return settings, broker, workspace


async def settle(app, pilot):
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


def test_archived_control_sequences_and_rich_markup_are_literal():
    raw = "[bold]Zażółć[/bold]\x1b]52;c;secret\x07\rREPLACE\u202e"
    text = safe_text(raw)
    assert "\x1b" not in text and "\x07" not in text and "\r" not in text and "\u202e" not in text
    assert r"\x1b]52;c;secret\x07\x0dREPLACE\u202e" in text
    assert "Zażółć" in text and plain(raw).spans == []


@pytest.mark.asyncio
async def test_sidebar_initial_restore_and_inspection_never_start_clients(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(45, 45)) as pilot:
        await settle(app, pilot)
        assert workspace.selections == []
        assert not any(path == "/api/client-runs" for _, path, _, _ in broker.calls)
        await pilot.click("#project-inspect")
        await settle(app, pilot)
        assert workspace.selections[-1][2] == "inspect"
        await pilot.click("#project-work")
        await settle(app, pilot)
        assert workspace.selections[-1][2] == "work"
        assert workspace.read()["requested_slot_ids"] == ["codex"]
        app.action_projects()
        app.query_one("#project-rows", DataTable).focus()
        await pilot.press("enter")
        await settle(app, pilot)
        assert workspace.selections[-1][2] == "inspect"
        assert broker.ensures == 1


@pytest.mark.asyncio
async def test_manual_clone_preflight_preserves_snapshot_identity_and_reports_corruption(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    broker.inspect_memory = "/projects/clone/.memory/devbrain"
    broker.corrupt_import = True
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(90, 42)) as pilot:
        await settle(app, pilot)
        app.add_project()
        await pilot.pause()
        app.screen.query_one("#field-name", Input).value = "Clone"
        app.screen.query_one("#field-root", Input).value = "/projects/clone"
        await pilot.click("#form-submit")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#field-path", Input).value == broker.inspect_memory
        await pilot.click("#form-submit")
        await pilot.pause(.2)
        # The import failure is returned to the still-open parent form.
        assert "Corrupt memory" in str(app.screen.query_one("#form-error", Static).render())
        await pilot.press("escape")
        await settle(app, pilot)
        assert not any(path == "/api/projects" for _, path, _, _ in broker.calls)
        assert workspace.selections == []


@pytest.mark.asyncio
async def test_inspector_reads_sources_in_bounded_pages_and_escapes_terminal_controls(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(95, 40)) as pilot:
        await settle(app, pilot)
        app.query_one("#inspector-view", Select).value = "sources"
        await settle(app, pilot)
        app.open_selected("source-a")
        await settle(app, pilot)
        text = str(app.query_one("#entity-body", Static).render())
        assert "\\x1b]52" in text and "\x1b" not in text
        assert app.query_one("#entity-sources", Button).label.plain == "Widok"
        await pilot.click("#entity-more")
        await settle(app, pilot)
        assert "next-page" in str(app.query_one("#entity-body", Static).render())
        requests = [params for _, path, params, _ in broker.calls if path == "/api/inspector/sources/source-a"]
        assert requests[0]["limit"] == 65536 and requests[-1]["offset"] == 16


@pytest.mark.asyncio
async def test_managed_record_review_sends_both_versions_without_changing_task_progress(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch, managed=True)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        await pilot.click("#entity-confirm")
        await pilot.pause()
        app.screen.query_one("#field-reason", TextArea).text = "Read the source."
        await pilot.click("#form-submit")
        await settle(app, pilot)
        writes = [body for method, path, _, body in broker.calls if method == "POST" and path.endswith("/review")]
        assert writes == [{"project": "project-a", "action": "confirm", "expected_version": 3,
                           "expected_plan_version": 5, "reason": "Read the source."}]
        assert broker.record["metadata"]["state"] == "needs_review"
        assert broker.record["status"] == "confirmed"


@pytest.mark.asyncio
async def test_review_conflict_refreshes_without_resubmitting_intent(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    broker.review_conflict = True
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        await pilot.click("#entity-confirm")
        await pilot.pause()
        await pilot.click("#form-submit")
        await settle(app, pilot)
        assert len([1 for method, path, _, _ in broker.calls if method == "POST" and path.endswith("/review")]) == 1
        assert app.entity["version"] == 4
        assert "nie została ponowiona" in str(app.query_one("#panel-notice", Static).render())


@pytest.mark.asyncio
async def test_import_requires_dry_run_then_separate_apply_and_never_starts_clients(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch, project=False)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.action_snapshot()
        await settle(app, pilot)
        await pilot.click("#memory-import")
        await pilot.pause()
        app.screen.query_one("#field-path", Input).value = "/projects/a/.memory/devbrain"
        app.screen.query_one("#field-root", Input).value = "/projects/a"
        await pilot.click("#form-submit")
        await settle(app, pilot)
        assert not app.query_one("#memory-apply", Button).disabled
        writes = [body for method, path, _, body in broker.calls if method == "POST" and path == "/api/memory/import"]
        assert [item["dry_run"] for item in writes] == [True]
        workspace.update({"focus": "inspector", "pane_ids": {"inspector": "%test"}})
        await app._poll_workspace()
        assert app.pending_import and not app.query_one("#memory-apply", Button).disabled
        await pilot.click("#memory-apply")
        await settle(app, pilot)
        writes = [body for method, path, _, body in broker.calls if method == "POST" and path == "/api/memory/import"]
        assert [item["dry_run"] for item in writes] == [True, False]
        assert workspace.selections[-1][2] == "inspect"
        assert not any(path == "/api/client-runs" for _, path, _, _ in broker.calls)


@pytest.mark.asyncio
async def test_broker_calls_never_block_textual_event_loop(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    loop_thread = threading.get_ident()
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(90, 40)) as pilot:
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        assert broker.threads and all(thread != loop_thread for thread in broker.threads)


@pytest.mark.asyncio
@pytest.mark.parametrize("user_changes_focus", [False, True])
async def test_wide_dialog_zooms_real_pane_and_preserves_explicit_user_focus(tmp_path, monkeypatch, user_changes_focus):
    settings, _, workspace = setup(tmp_path, monkeypatch)
    workspace.update({"focus": "sidebar", "zoom": False})
    app = SimpleNamespace(settings=settings, workspace=workspace, workspace_id="default", is_headless=False)
    monkeypatch.setenv("TMUX_PANE", "%fixture")
    reconciled = []
    monkeypatch.setattr("devbrain.workspace.refresh_workspace", lambda *args: reconciled.append(workspace.read()))

    async def opened(screen):
        assert screen == "fixture-screen"
        assert workspace.read()["focus"] == "inspector" and workspace.read()["zoom"] is True
        if user_changes_focus:
            workspace.update({"focus": "graph", "zoom": False})
        return "explicit-modal-result"

    assert await wide_dialog(app, "fixture-screen", "inspector", opened) == "explicit-modal-result"
    assert workspace.read()["focus"] == ("graph" if user_changes_focus else "sidebar")
    assert workspace.read()["zoom"] is False
    assert reconciled and reconciled[0]["zoom"] is True


@pytest.mark.asyncio
async def test_review_cancels_when_project_changes_while_modal_is_open(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.open_selected("record-a")
        await settle(app, pilot)
        await pilot.click("#entity-confirm")
        await pilot.pause()
        workspace.update({"project_id": "project-b", "project_name": "B", "worktree": "/projects/b"})
        await app._poll_workspace()
        await pilot.click("#form-submit")
        await settle(app, pilot)
        assert not any(method == "POST" and path.endswith("/review") for method, path, _, _ in broker.calls)
        assert "Projekt zmienił się" in str(app.query_one("#panel-notice", Static).render())


@pytest.mark.asyncio
async def test_native_profile_model_override_is_explicit_and_does_not_start_clients(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(95, 42)) as pilot:
        await settle(app, pilot)
        app.edit_profiles()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#field-model", Input).value = "native-model-fixture"
        await pilot.click("#form-submit")
        await settle(app, pilot)
        writes = [body for method, path, _, body in broker.calls if method == "PUT" and path == "/api/profiles"]
        assert writes[0]["profiles"][0]["model"] == "native-model-fixture"
        assert not any(path == "/api/client-runs" for _, path, _, _ in broker.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(70, 18), (95, 24)])
async def test_current_inspector_view_label_is_visible_in_actual_pane_render(tmp_path, monkeypatch, size):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = InspectorApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        lines = [strip.text for strip in app.screen._compositor.render_strips()]
        select = app.query_one("#inspector-view", Select).region
        assert any("Ustalenia" in line for line in lines[select.y:select.bottom])
        app.query_one("#inspector-view", Select).value = "sources"
        await settle(app, pilot)
        lines = [strip.text for strip in app.screen._compositor.render_strips()]
        select = app.query_one("#inspector-view", Select).region
        assert any("Źródła" in line for line in lines[select.y:select.bottom])


@pytest.mark.asyncio
async def test_sidebar_action_labels_do_not_wrap_at_fixed_24_columns(tmp_path, monkeypatch):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(24, 55)) as pilot:
        await settle(app, pilot)
        lines = [strip.text for strip in app.screen._compositor.render_strips()]
        for identifier, label in (("auth-open", "Połączenia"), ("more-open", "Więcej"),
                                  ("project-add", "Dodaj folder"), ("project-inspect", "Otwórz")):
            region = app.query_one("#" + identifier, Button).region
            rendered = [line[region.x:region.right] for line in lines[region.y:region.bottom]]
            assert sum(label in line for line in rendered) == 1, (identifier, rendered)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("page", ["projects", "project"])
async def test_sidebar_resize_uses_current_event_and_keeps_actions_reachable(
    tmp_path, monkeypatch, language, page
):
    settings, broker, workspace = setup(tmp_path, monkeypatch)
    monkeypatch.setattr("devbrain.terminal_ui.language_for", lambda settings: language)
    app = SidebarApp(settings, broker=broker, workspace=workspace)
    async with app.run_test(size=(24, 33)) as pilot:
        await settle(app, pilot)
        app.show_page(page)
        await pilot.pause()

        def assert_complete_label(identifier, label):
            button = app.query_one("#" + identifier, Button)
            assert app.screen.can_view_entire(button), (identifier, button.region)
            region = button.region
            lines = [strip.text for strip in app.screen._compositor.render_strips()]
            rendered = [line[region.x:region.right] for line in lines[region.y:region.bottom]]
            assert sum(label in line for line in rendered) == 1, (identifier, rendered)

        for width, height in ((24, 33), (80, 21), (24, 21), (120, 33), (80, 21)):
            # Real Resize dispatch must update the class on this event, not the next one.
            await pilot.resize_terminal(width, height)
            await settle(app, pilot)
            assert app.size.width == width and app.size.height == height
            assert app.screen.has_class("narrow") is (width < 40)
            connections = app.query_one("#auth-open", Button)
            more = app.query_one("#more-open", Button)
            if width < 40:
                assert connections.region.bottom <= more.region.y
            else:
                assert connections.region.y == more.region.y
                assert connections.region.right <= more.region.x
                # At actual 80x21 the complete bottom actions must already fit.
                assert_complete_label("auth-open", app.t("Connections"))
                assert_complete_label("more-open", app.t("More"))

            # A genuinely narrow/short pane may scroll, but keyboard focus must reveal
            # each whole action without resizing again or scrolling it in the test.
            connections.focus()
            await pilot.pause()
            assert app.focused is connections
            assert_complete_label("auth-open", app.t("Connections"))
            await pilot.press("tab")
            await pilot.pause()
            assert app.focused is more
            assert_complete_label("more-open", app.t("More"))

        assert workspace.selections == []
        assert not any(path == "/api/client-runs" for _, path, _, _ in broker.calls)
