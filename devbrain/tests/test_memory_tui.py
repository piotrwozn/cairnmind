"""Portable-memory controls preserve explicit project/root and import decisions."""
from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Button, Input, Select, Static, TabbedContent

from devbrain.config import Settings
from devbrain.tui import DevBrainApp


class ViewStore:
    def __init__(self, projects=()):
        self.projects = list(projects)

    def list_projects(self):
        return self.projects

    def list_records(self, project_id):
        return []

    def status(self, project_id):
        return {"projects": len(self.projects)}

    def query(self, sql, params=None):
        if "project_roots" in sql:
            return [{"path": row["root"]} for row in self.projects if row["id"] == params["project"]]
        return []


async def settle(app, pilot):
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


def make_app(tmp_path, monkeypatch, projects=()):
    store = ViewStore(projects)
    app = DevBrainApp(Settings(data_dir=tmp_path / "data"), store=store)
    monkeypatch.setattr(app, "_coordination_data", lambda *args: {"plans": [], "next": {}, "checks": []})
    states = {}

    def portable_status(project_id, worktree):
        return states.get(project_id, {
            "project_id": project_id, "path": str(Path(worktree) / ".memory/devbrain/manifest.json"),
            "latest": None, "auto": {"enabled": False, "interval_minutes": 15},
        })

    monkeypatch.setattr(app, "_portable_status_data", portable_status)
    return app, store, states


@pytest.mark.asyncio
async def test_detecting_snapshot_only_offers_import_and_shows_status(tmp_path, monkeypatch):
    project = {"id": "project-a", "name": "A", "root": str(tmp_path / "a")}
    app, _, states = make_app(tmp_path, monkeypatch, [project])
    path = str(Path(project["root"]) / ".memory/devbrain/manifest.json")
    states[project["id"]] = {
        "path": path, "latest": {"snapshot_id": "snapshot-a", "counts": {"sources": 8}},
        "auto": {"enabled": False, "interval_minutes": 15, "error": "external_manifest_change"},
    }

    async def unexpected_request(*args, **kwargs):
        pytest.fail("Detecting an existing snapshot must not start a service/import")

    monkeypatch.setattr(app, "_request", unexpected_request)
    async with app.run_test(size=(145, 52)) as pilot:
        await settle(app, pilot)
        assert "Wykryto snapshot" in str(app.query_one("#portable-detection", Static).render())
        status = str(app.query_one("#portable-status", Static).render())
        assert "snapshot-a" in status and "sources" in status
        assert "external_manifest_change" in status
        assert app.query_one("#portable-path", Input).value == path
        assert app.query_one("#portable-import", Button).disabled
        assert app.query_one("#portable-snapshot", Button).disabled
        assert "prywatne dane" in str(app.query_one("#portable-privacy", Static).render())


@pytest.mark.asyncio
async def test_snapshot_requires_preview_for_selected_project_and_scope(tmp_path, monkeypatch):
    first = {"id": "project-a", "name": "A", "root": str(tmp_path / "a")}
    second = {"id": "project-b", "name": "B", "root": str(tmp_path / "b")}
    app, _, _ = make_app(tmp_path, monkeypatch, [first, second])
    calls = []

    async def request(method, path, payload=None):
        calls.append((method, path, payload))
        return {"status": "preview" if path.endswith("preview") else "created",
                "project": first, "counts": {"sources": 12}, "bytes": 3456}

    monkeypatch.setattr(app, "_request", request)
    async with app.run_test(size=(150, 55)) as pilot:
        await settle(app, pilot)
        app.query_one(TabbedContent).active = "portable-tab"
        await pilot.pause()
        assert app.query_one("#portable-snapshot", Button).disabled
        await pilot.click("#portable-preview")
        await settle(app, pilot)
        assert "3456" in str(app.query_one("#portable-detail", Static).render())
        assert not app.query_one("#portable-snapshot", Button).disabled
        await pilot.click("#portable-snapshot")
        await settle(app, pilot)
        assert calls == [
            ("POST", "/memory/preview", {"project": first["id"], "worktree": first["root"]}),
            ("POST", "/memory/snapshot", {"project": first["id"], "worktree": first["root"]}),
        ]
        app.query_one("#project-select", Select).value = second["id"]
        await settle(app, pilot)
        assert app.query_one("#portable-snapshot", Button).disabled


@pytest.mark.asyncio
async def test_first_project_import_requires_explicit_root_and_matching_dry_run(tmp_path, monkeypatch):
    app, store, _ = make_app(tmp_path, monkeypatch)
    root = str(tmp_path / "new-project")
    manifest = str(tmp_path / "checkout/.memory/devbrain")
    imported = {"id": "portable-project", "name": "Imported", "root": root}
    calls = []

    async def request(method, path, payload=None):
        calls.append((method, path, dict(payload)))
        if path == "/memory/inspect":
            return {"status": "valid", "project": imported, "snapshot_id": "snapshot-a", "counts": {"records": 4}}
        if payload["dry_run"]:
            return {"status": "dry_run", "project": imported}
        store.projects = [imported]
        return {"status": "imported", "project": imported, "verification": "historical"}

    monkeypatch.setattr(app, "_request", request)
    async with app.run_test(size=(150, 55)) as pilot:
        await settle(app, pilot)
        assert app.selected_project is None
        assert not app.query_one("#portable-inspect", Button).disabled
        app.query_one("#portable-path", Input).value = manifest
        await pilot.pause()
        app.portable_operation("portable-inspect")
        await settle(app, pilot)
        app.portable_operation("portable-dry-run")
        await settle(app, pilot)
        assert "jawny katalog" in str(app.query_one("#portable-detail", Static).render())
        assert len(calls) == 1
        app.query_one("#portable-root", Input).value = root
        await pilot.pause()
        app.portable_operation("portable-dry-run")
        await settle(app, pilot)
        assert not app.query_one("#portable-import", Button).disabled
        app.query_one("#portable-name", Input).value = "Imported"
        await pilot.pause()
        assert app.query_one("#portable-import", Button).disabled
        app.portable_operation("portable-dry-run")
        await settle(app, pilot)
        app.portable_operation("portable-import")
        await settle(app, pilot)
        assert calls[-1] == ("POST", "/memory/import", {
            "path": manifest, "root": root, "name": "Imported", "dry_run": False,
        })
        assert app.selected_project == imported["id"]
        assert "historical" in str(app.query_one("#portable-detail", Static).render())


@pytest.mark.asyncio
async def test_automatic_snapshot_is_explicit_and_errors_are_visible(tmp_path, monkeypatch):
    project = {"id": "project-a", "name": "A", "root": str(tmp_path / "a")}
    app, _, states = make_app(tmp_path, monkeypatch, [project])
    calls = []

    async def request(method, path, payload=None):
        calls.append((method, path, payload))
        if payload["enabled"]:
            states[project["id"]] = {"path": "manifest.json", "latest": None, "auto": {
                "enabled": True, "interval_minutes": payload["interval_minutes"],
                "worktree": project["root"], "last_success_at": "2026-11-06T10:00:00+00:00",
            }}
            return {"status": "configured", **states[project["id"]]}
        raise RuntimeError("HTTP 409: snapshot changed externally")

    monkeypatch.setattr(app, "_request", request)
    async with app.run_test(size=(150, 55)) as pilot:
        await settle(app, pilot)
        assert calls == []
        app.query_one("#portable-interval", Input).value = "30"
        app.portable_operation("portable-auto")
        await settle(app, pilot)
        assert calls[0] == ("PATCH", "/memory/settings", {
            "project": project["id"], "worktree": project["root"], "enabled": True, "interval_minutes": 30,
        })
        assert str(app.query_one("#portable-auto", Button).label) == "Wyłącz automat"
        app.portable_operation("portable-auto")
        await settle(app, pilot)
        assert "snapshot changed externally" in str(app.query_one("#portable-detail", Static).render())
        assert not app.query_one("#portable-auto", Button).disabled
