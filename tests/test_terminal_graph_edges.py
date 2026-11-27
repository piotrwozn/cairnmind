"""Edge-case tests for terminal_graph (BrailleCanvas, layout, tmux_visible, GroupPicker, GraphApp)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from cairnmind.terminal.cockpit_state import CockpitState
from cairnmind.config import Settings
from cairnmind.terminal.terminal_graph import (
    BrailleCanvas,
    GraphApp,
    GraphModel,
    GraphScope,
    GroupPicker,
    TerminalGraph,
    layout_positions,
    selection_for,
    tmux_visible,
)
from cairnmind.terminal.workspace_state import WorkspaceState
from test_terminal_graph import ControlledBroker, eventually


def test_braille_canvas_clipping_dotted_layout_model_and_selection():
    canvas = BrailleCanvas(4, 2)
    # Parallel outside viewport (p == 0 and q < 0)
    canvas.line((-5, 1, 10), (-5, 5, 10), "white")
    assert not canvas.dots
    # Completely outside diagonally (low > high)
    canvas.line((-10, -10, 10), (-2, -8, 10), "white")
    assert not canvas.dots
    # Dotted line across viewport and multi-row render
    canvas.line((0, 0, 10), (7, 7, 10), "green", dotted=True)
    rendered = canvas.render().plain
    assert "\n" in rendered and any(ch != " " and ch != "\n" for ch in rendered)

    # Structural edge pulls new child toward parent; dangling edge is ignored
    nodes = [{"id": "parent"}, {"id": "child"}]
    edges = [
        {"from_id": "parent", "to_id": "child", "structural": True},
        {"from_id": "parent", "to_id": "missing"},
    ]
    laid = layout_positions(nodes, edges, {"parent": (0.0, 0.0, 0.0)})
    assert set(laid) == {"parent", "child"}

    # GraphModel edge cases: identical scope, invalid format, >3000 positions pruning
    model = GraphModel(motion=False)
    assert model.set_scope(GraphScope()) is False
    with pytest.raises(ValueError, match="Invalid graph projection"):
        model.accept("not-a-dict")
    model.positions = {f"n{i}": (0.0, 0.0, 0.0) for i in range(3005)}
    assert model.accept({"nodes": [{"id": "n1"}], "edges": [], "groups": []})
    assert list(model.positions) == ["n1"]

    # selection_for fallback for generic node
    assert selection_for({"id": "custom:1", "project_id": "p", "entity_type": "symbol"}) == {
        "project_id": "p", "entity_type": "symbol", "id": "custom:1"
    }


def test_tmux_visible_paths(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "data")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert tmux_visible(settings) is True

    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr("cairnmind.terminal.terminal_graph.resolve_tmux", lambda s: None)
    assert tmux_visible(settings) is False

    monkeypatch.setattr("cairnmind.terminal.terminal_graph.resolve_tmux", lambda s: "tmux")
    monkeypatch.setattr(
        "cairnmind.terminal.terminal_graph.subprocess.run",
        lambda *a, **k: SimpleNamespace(stdout="1:1:80:24\n"),
    )
    assert tmux_visible(settings) is True

    monkeypatch.setattr(
        "cairnmind.terminal.terminal_graph.subprocess.run",
        lambda *a, **k: SimpleNamespace(stdout="bad-output"),
    )
    assert tmux_visible(settings) is False


@pytest.mark.asyncio
async def test_graph_app_buttons_motion_events_group_picker_and_open_node_edges(tmp_path):
    settings = Settings(tmp_path / "data")
    projects = [
        {"id": "alpha", "name": "Alpha", "root": str(tmp_path / "alpha")},
        {"id": "beta", "name": "Beta", "root": str(tmp_path / "beta")},
    ]
    for p in projects:
        (tmp_path / p["id"]).mkdir()
    workspace = WorkspaceState(settings, "graph-edge-tests")
    workspace.select_project(projects[0], intent="inspect")
    workspace.update({"focus": "graph"})
    broker = ControlledBroker(projects)
    app = GraphApp(settings, broker=broker, workspace=workspace, motion=True)

    async with app.run_test(size=(100, 35)) as pilot:
        await eventually(pilot, lambda: len(app.model.nodes) == 3)
        widget = app.query_one(TerminalGraph)
        assert widget.render() is not None

        # Keyboard actions for down, zoom_out, reset_camera, previous_node
        await pilot.press("down", "minus", "left_square_bracket", "r")
        await pilot.pause()

        # Buttons: pause, back (parent_view -> global), open, groups
        was_paused = app.model.paused
        await pilot.click("#graph-pause")
        assert app.model.paused is (not was_paused)

        # Toggle reduced_motion in CockpitState and trigger workspace_changed
        CockpitState(settings).update({"reduced_motion": True})
        await app.workspace_changed(workspace.read())
        assert app._reduced_motion is True and app.model.paused is True

        # set_active(True) marks dirty
        app.set_active(False)
        app.set_active(True)
        assert app._dirty is True

        # GroupPicker in project scope: next_page -> previous_page -> cancel
        await pilot.click("#graph-groups")
        await eventually(pilot, lambda: isinstance(app.screen, GroupPicker) and len(app.screen.items) == 1)
        await pilot.press("n")
        await eventually(pilot, lambda: app.screen.items and app.screen.items[0]["id"] == "group:last")
        await pilot.press("p")
        await eventually(pilot, lambda: app.screen.items and app.screen.items[0]["id"] == "group:first")
        await pilot.press("escape")
        await eventually(pilot, lambda: not isinstance(app.screen, GroupPicker))

        # Back button from project scope switches to global view; GroupPicker in global scope loads projects
        await pilot.click("#graph-back")
        await eventually(pilot, lambda: app.model.scope.project is None)
        await pilot.click("#graph-groups")
        await eventually(pilot, lambda: isinstance(app.screen, GroupPicker) and len(app.screen.items) == 2)
        await pilot.press("escape")
        await eventually(pilot, lambda: not isinstance(app.screen, GroupPicker))

        # parent_view from group scope clears group
        app._global = False
        app._scope_changed(GraphScope("alpha", projects[0]["root"], group="first"))
        app.parent_view()
        assert app.model.scope.group is None

        # listen_events: ambiguous non-dict data and unavailable notice
        await broker.queue.put({"event": "change", "data": "unexpected-string"})
        await eventually(pilot, lambda: any("ambiguous" in n.text.lower() for n in app.notice_history.entries))
        await broker.queue.put({"event": "unavailable", "data": {}})
        await eventually(pilot, lambda: any("interrupted" in n.text.lower() for n in app.notice_history.entries))

        # open_node: inbox_group, group, missing project error, cross-project node (None + beta + unknown)
        app.open_node({"id": "inbox", "entity_type": "inbox_group"})
        await eventually(pilot, lambda: (workspace.read().get("selected_entity") or {}).get("entity_type") == "inbox_group")

        app.open_node({"id": "source:beta-1", "entity_type": "source", "source_id": "beta-1", "project_id": "beta"})
        await eventually(pilot, lambda: workspace.read().get("project_id") == "beta")

        app.open_node({"id": "inbox:x", "entity_type": "inbox", "inbox_id": "x", "project_id": None})
        await eventually(pilot, lambda: workspace.read().get("project_id") is None)

        app.open_node({"id": "project:gone", "kind": "project", "project_id": "gone"})
        await eventually(pilot, lambda: any("no longer registered" in n.text for n in app.notice_history.entries))

        app.open_node({"id": "source:ghost", "entity_type": "source", "source_id": "ghost", "project_id": "ghost"})
        await eventually(pilot, lambda: any("unknown" in n.text for n in app.notice_history.entries))
