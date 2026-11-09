"""Perspective, bounded rendering and actual Textual graph interactions."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import math
import threading

import pytest
from textual.widgets import OptionList, Static

from devbrain.config import Settings
from devbrain.terminal_graph import (BrailleCanvas, Camera, EDGE_LIMIT, GraphApp, GraphModel, GraphScope,
                                    GroupPicker, NODE_LIMIT, TerminalGraph, layout_positions, render_scene,
                                    seed_position, selection_for, terminal_visible)
from devbrain.workspace_state import WorkspaceState


def projection(project="p", count=3):
    nodes = [{"id": f"source:{project}-{i}", "project_id": project, "kind": "source", "entity_type": "source",
              "source_id": f"{project}-{i}", "label": f"literal source {i}"} for i in range(count)]
    return {"nodes": nodes, "edges": [{"from_id": nodes[i]["id"], "to_id": nodes[i + 1]["id"]}
                                      for i in range(count - 1)], "groups": [],
            "cursor": {"epoch": "00000000-0000-0000-0000-000000000001", "seq": count}}


def test_real_xyz_projection_perspective_rotation_and_near_plane():
    camera = Camera(yaw=0, pitch=0, distance=100)
    near, far = camera.project((20, 0, -50), 160, 100), camera.project((20, 0, 50), 160, 100)
    assert near[0] - 80 == pytest.approx(3 * (far[0] - 80))
    assert near[2] < far[2]
    camera.rotate(yaw=math.pi / 2)
    rotated = camera.project((20, 0, 0), 160, 100)
    assert rotated[0] == pytest.approx(80) and rotated[2] == pytest.approx(80)
    camera.rotate(pitch=.5)
    assert camera.project((0, 20, 0), 160, 100)[2] != pytest.approx(100)
    assert Camera(yaw=0, pitch=0, distance=20).project((0, 0, -25), 160, 100) is None
    assert Camera().project((math.inf, 0, 0), 160, 100) is None


def test_braille_four_rows_two_columns_and_nearest_depth_win():
    canvas = BrailleCanvas(2, 1)
    canvas.plot(0, 0, 20, "red")
    canvas.plot(1, 3, 30, "green")
    assert canvas.render().plain[0] == chr(0x2800 + 1 + 128)
    canvas.plot(0, 0, 5, "blue")
    canvas.plot(0, 0, 50, "yellow")
    assert canvas.dots[(0, 0)] == (5, "blue")
    assert canvas.render().spans[0].style == "blue"
    # Clipping a line far beyond the viewport remains bounded and visible.
    canvas.line((-100000, 1, 2), (100000, 1, 4), "white")
    assert len(canvas.dots) <= canvas.width * canvas.height


def test_node_occlusion_is_independent_of_input_order():
    nodes = [{"id": "near", "kind": "decision"}, {"id": "far", "kind": "task"}]
    positions = {"near": (0, 0, -20), "far": (0, 0, 20)}
    camera = Camera(yaw=0, pitch=0, distance=100)
    first = render_scene(nodes, [], positions, camera, None, {}, 40, 12, animate=False)
    second = render_scene(list(reversed(nodes)), [], positions, camera, None, {}, 40, 12, animate=False)
    assert first.plain == second.plain
    assert first.spans == second.spans
    near_only = render_scene(nodes[:1], [], positions, camera, None, {}, 40, 12, animate=False)
    far_only = render_scene(nodes[1:], [], positions, camera, None, {}, 40, 12, animate=False)
    assert first.plain.strip()  # An actual lit surface reached the frame.
    assert (first.plain, first.spans) == (near_only.plain, near_only.spans)
    assert (first.plain, first.spans) != (far_only.plain, far_only.spans)


def test_layout_has_real_depth_and_keeps_stable_id_seed_positions():
    data = projection()
    old = {node["id"]: seed_position(node["id"]) for node in data["nodes"]}
    assert len({round(v[2], 4) for v in old.values()}) > 1
    first = layout_positions(data["nodes"], data["edges"], old)
    second = layout_positions(data["nodes"], data["edges"], old)
    assert first == second
    assert all(all(math.isfinite(v) for v in point) for point in first.values())


def test_bounded_projection_preserves_camera_selection_and_rejects_stale_scope():
    model = GraphModel(motion=False)
    data = projection(count=NODE_LIMIT + 30)
    data["edges"] += [{"from_id": "source:p-0", "to_id": "source:p-1"}] * 1000
    assert model.accept(data)
    assert len(model.nodes) == NODE_LIMIT and len(model.edges) == EDGE_LIMIT
    assert model.truncated
    assert all(e["from_id"] in model.positions and e["to_id"] in model.positions for e in model.edges)
    model.camera.rotate(yaw=.5, pitch=.2)
    model.cycle(10)
    pose, selected, positions = replace(model.camera), model.selected_id, dict(model.positions)
    assert model.accept(data)
    assert model.camera == pose and model.selected_id == selected and model.positions == positions
    generation = model.generation
    model.set_scope(GraphScope("other", "/other"))
    assert model.accept(data, generation) is False and not model.nodes


def test_exact_revision_owner_and_inbox_identity():
    node = {"id": "revision:record:task:3", "entity_type": "revision", "project_id": "p",
            "owner_type": "record", "owner_id": "task", "revision_id": "task:3", "version": 3}
    assert selection_for(node) == {"project_id": "p", "entity_type": "revision", "id": "task:3",
                                   "owner_type": "record", "owner_id": "task", "revision": 3}
    assert selection_for({"id": "inbox:x", "kind": "unassigned", "inbox_id": "x", "project_id": None}) == {
        "project_id": None, "entity_type": "inbox", "id": "x"}
    with pytest.raises(ValueError):
        selection_for({**node, "owner_id": None})


def test_hidden_compact_detached_and_tiny_panes_pause():
    assert terminal_visible({"layout": "wide", "focus": "sidebar"}, (70, 25), True)
    assert not terminal_visible({"layout": "compact", "focus": "inspector"}, (70, 25), True)
    assert terminal_visible({"layout": "compact", "focus": "graph"}, (70, 25), True)
    assert not terminal_visible({"layout": "wide", "focus": "graph"}, (70, 25), False)
    assert not terminal_visible({"zoom": True, "focus": "inspector"}, (70, 25), True)
    assert not terminal_visible({}, (2, 2), True)


class ControlledBroker:
    def __init__(self, projects):
        self.projects = projects
        self.data = {p["id"]: projection(p["id"]) for p in projects}
        self.calls = []
        self.queue = asyncio.Queue()
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False

    def ensure(self):
        return {"available": True}

    def request(self, method, path, *, params=None, json=None):
        params = params or {}
        self.calls.append((method, path, dict(params)))
        if path == "/api/state":
            return {"projects": self.projects}
        if path == "/api/inspector/graph/groups":
            if not params.get("cursor"):
                return {"items": [{"id": "group:first", "group": "first", "kind": "group", "label": "First"}],
                        "next_cursor": "groups-next"}
            return {"items": [{"id": "group:last", "group": "last", "kind": "group", "label": "Last"}], "next_cursor": None}
        if path == "/api/inspector/graph":
            data = deepcopy(self.data.get(params.get("project"), {
                "nodes": [{"id": "project:" + p["id"], "project_id": p["id"], "kind": "project", "label": p["name"]}
                          for p in self.projects], "edges": [], "groups": []}))
            if self.block:
                self.started.set()
                self.release.wait(3)
            if params.get("page_cursor"):
                data["nodes"] = [{"id": "source:page2", "source_id": "page2", "entity_type": "source",
                                  "project_id": params.get("project"), "kind": "source", "label": "Second page"}]
                data["edges"] = []
            else:
                data["next_cursor"] = "page-next"
            return data
        raise ValueError("Unexpected broker request")

    async def events(self, **kwargs):
        while True:
            yield await self.queue.get()

    def close(self):
        self.release.set()


@pytest.fixture
def panel(tmp_path):
    settings = Settings(tmp_path / "data")
    projects = [{"id": "alpha", "name": "Alpha", "root": str(tmp_path / "alpha")},
                {"id": "beta", "name": "Beta", "root": str(tmp_path / "beta")}]
    for project in projects:
        from pathlib import Path
        Path(project["root"]).mkdir()
    workspace = WorkspaceState(settings, "graph-tests")
    workspace.select_project(projects[0], intent="inspect")
    workspace.update({"focus": "graph"})
    broker = ControlledBroker(projects)
    return settings, workspace, broker


async def eventually(pilot, predicate, seconds=4):
    for _ in range(int(seconds / .05)):
        if predicate():
            return
        await pilot.pause(.05)
    assert predicate()


@pytest.mark.asyncio
async def test_keys_respond_while_fetch_is_blocked_then_pages_are_real(panel):
    settings, workspace, broker = panel
    broker.block = True
    app = GraphApp(settings, broker=broker, workspace=workspace, motion=False)
    async with app.run_test(size=(100, 35)) as pilot:
        await eventually(pilot, broker.started.is_set)
        assert app.query_one(TerminalGraph).has_focus
        original = replace(app.model.camera)
        await pilot.press("left", "plus")
        assert app.model.camera.yaw != original.yaw and app.model.camera.distance < original.distance
        broker.block = False
        broker.release.set()
        await eventually(pilot, lambda: bool(app.model.nodes))
        await pilot.press("n")
        await eventually(pilot, lambda: any(n["id"] == "source:page2" for n in app.model.nodes))
        assert app.model.scope.page == "page-next"
        await pilot.press("p")
        await eventually(pilot, lambda: app.model.scope.page is None and len(app.model.nodes) == 3)
        assert all(call[0] == "GET" for call in broker.calls)


@pytest.mark.asyncio
async def test_live_updates_preserve_camera_ids_and_archive_controls_are_inert(panel):
    settings, workspace, broker = panel
    app = GraphApp(settings, broker=broker, workspace=workspace, motion=False)
    async with app.run_test(size=(100, 35)) as pilot:
        await eventually(pilot, lambda: len(app.model.nodes) == 3)
        await pilot.press("right", "up", "tab")
        pose, chosen = replace(app.model.camera), app.model.selected_id
        before = len(broker.calls)
        await broker.queue.put({"event": "change", "data": {"project_id": "beta"}})
        await pilot.pause(.4)
        assert len(broker.calls) == before
        broker.data["alpha"] = projection("alpha", 4)
        broker.data["alpha"]["nodes"][1]["label"] = "[red]\x1b]52;c;SECRET\x07\u202e"
        await broker.queue.put({"event": "change", "data": {"project_id": "alpha"}})
        await eventually(pilot, lambda: len(app.model.nodes) == 4)
        assert app.model.camera == pose and app.model.selected_id == chosen
        app.model.selected_id = "source:alpha-1"
        app.invalidate_frame()
        await eventually(pilot, lambda: "SECRET" in str(app.query_one("#graph-selection", Static).render()))
        output = str(app.query_one("#graph-selection", Static).render())
        assert "\x1b" not in output and "\x07" not in output and "\u202e" not in output
        assert "[red]" in output and "\\x1b" in output


@pytest.mark.asyncio
async def test_global_project_and_exact_revision_selection_never_start_clients(panel):
    settings, workspace, broker = panel
    app = GraphApp(settings, broker=broker, workspace=workspace, motion=False)
    async with app.run_test(size=(100, 35)) as pilot:
        await eventually(pilot, lambda: bool(app.model.nodes))
        await pilot.press("a")
        await eventually(pilot, lambda: app.model.scope.project is None and len(app.model.nodes) == 2)
        app.model.selected_id = "project:beta"
        await pilot.press("enter")
        await eventually(pilot, lambda: workspace.read()["project_id"] == "beta")
        assert workspace.read()["intent"] == "inspect" and workspace.read()["work_request"] is None
        assert app.model.scope.project == "beta"
        node = {"id": "revision:record:beta-task:7", "entity_type": "revision", "kind": "revision", "project_id": "beta",
                "owner_type": "record", "owner_id": "beta-task", "revision_id": "beta-task:7", "version": 7}
        app.open_node(node)
        await eventually(pilot, lambda: workspace.read()["selected_entity"] is not None)
        assert workspace.read()["selected_entity"] == selection_for(node)
        assert workspace.read()["focus"] == "inspector"
        assert all(call[0] == "GET" for call in broker.calls)


@pytest.mark.asyncio
async def test_group_picker_pages_and_expands_backend_group(panel):
    settings, workspace, broker = panel
    app = GraphApp(settings, broker=broker, workspace=workspace, motion=False)
    async with app.run_test(size=(100, 35)) as pilot:
        await eventually(pilot, lambda: bool(app.model.nodes))
        await pilot.press("g")
        await eventually(pilot, lambda: isinstance(app.screen, GroupPicker) and len(app.screen.items) == 1)
        await pilot.press("n")
        await eventually(pilot, lambda: app.screen.items[0]["id"] == "group:last")
        assert app.screen.query_one(OptionList).option_count == 1
        await pilot.press("enter")
        await eventually(pilot, lambda: app.model.scope.group == "last")
        assert any(c[1].endswith("/groups") and c[2].get("cursor") == "groups-next" for c in broker.calls)


@pytest.mark.asyncio
async def test_tab_moves_focus_and_brackets_select_nodes(panel):
    settings, workspace, broker = panel
    app = GraphApp(settings, broker=broker, workspace=workspace, motion=False)
    async with app.run_test(size=(80, 24)) as pilot:
        await eventually(pilot, lambda: len(app.model.nodes) > 1)
        selected = app.model.selected_id
        await pilot.press("tab")
        assert app.focused.id == "graph-open"
        assert app.model.selected_id == selected
        await pilot.press("shift+tab", "right_square_bracket")
        assert app.model.selected_id != selected
        assert all(call[0] == "GET" for call in broker.calls)


@pytest.mark.asyncio
async def test_late_old_scope_response_cannot_replace_new_project(panel):
    settings, workspace, broker = panel
    broker.block = True
    app = GraphApp(settings, broker=broker, workspace=workspace, motion=False)
    async with app.run_test(size=(100, 35)) as pilot:
        await eventually(pilot, broker.started.is_set)
        workspace.select_project(broker.projects[1], intent="inspect")
        await eventually(pilot, lambda: app.model.scope.project == "beta")
        broker.block = False
        broker.release.set()
        await eventually(pilot, lambda: len(app.model.nodes) == 3)
        assert all(node["project_id"] == "beta" for node in app.model.nodes)
        app.set_active(False)
        calls = len(broker.calls)
        await broker.queue.put({"event": "change", "data": {"project_id": "beta"}})
        await pilot.pause(.5)
        assert len(broker.calls) == calls
