"""Exercise the terminal painter, not just the graph model or offline raster.

The reported failure happened when Textual promoted a styled Rich Text frame to
its Content format.  A native Strip widget must also work when that converter
is unavailable; ordinary model-ready assertions never establish that a frame
has actually reached the compositor.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.geometry import Region
from textual.strip import Strip
from textual.style import Style

from cairnmind.config import Settings
from cairnmind.terminal_graph import Camera, GraphApp, GraphModel, TerminalGraph, render_scene
from cairnmind.workspace_state import WorkspaceState


class GraphPainterApp(App):
    """Mount the production widget with real Textual message and paint loops."""

    def compose(self) -> ComposeResult:
        yield TerminalGraph(GraphModel(motion=False))

    def invalidate_frame(self):
        # The enclosing GraphApp normally schedules the worker on resize.
        pass


def scene(columns, rows, camera=None):
    nodes = [{"id": "decision:near", "kind": "decision"},
             {"id": "task:far", "kind": "task"},
             {"id": "source:high", "kind": "source"}]
    positions = {"decision:near": (-15, -4, -20), "task:far": (20, 3, 25),
                 "source:high": (2, 20, -3)}
    edges = [{"from_id": "decision:near", "to_id": "task:far"},
             {"from_id": "task:far", "to_id": "source:high"}]
    frame = render_scene(nodes, edges, positions, camera or Camera(), None, {}, columns, rows, animate=False)
    assert frame.plain.strip() and frame.spans
    return frame


def painted(widget):
    """Force exactly the same styled lines consumed by Textual's compositor."""
    strips = widget.render_lines(Region(0, 0, widget.size.width, widget.size.height))
    assert all(isinstance(strip, Strip) for strip in strips)
    return strips


def fail_legacy_style_conversion(cls, *args, **kwargs):
    raise TypeError("Style.from_rich_style() missing 1 required positional argument: 'rich_style'")


@pytest.mark.asyncio
async def test_styled_xyz_frame_reaches_real_painter_without_rich_content_conversion(monkeypatch):
    # Reproduce the user's failing conversion boundary while the rest of the
    # real Textual renderer remains active.  No successful frame may use it.
    monkeypatch.setattr(Style, "from_rich_style", classmethod(fail_legacy_style_conversion))
    app = GraphPainterApp()
    async with app.run_test(size=(80, 24)) as pilot:
        widget = app.query_one(TerminalGraph)
        frame = scene(widget.size.width, widget.size.height)
        widget.set_frame(frame)
        await pilot.pause(.2)
        strips = painted(widget)
        text = "\n".join(strip.text for strip in strips)
        assert text.strip()
        assert any(segment.style and segment.style.color for strip in strips for segment in strip)
        assert isinstance(widget.render_line(widget.size.height // 2), Strip)
        # Compose the complete terminal output too; a nonempty offline Text is
        # insufficient if drawing only prints its repr or fails in a timer.
        terminal = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert terminal == text
        assert "Span(" not in terminal and "TypeError" not in terminal


@pytest.mark.asyncio
async def test_repeated_rotated_frames_and_resize_repaint_without_old_cells(monkeypatch):
    monkeypatch.setattr(Style, "from_rich_style", classmethod(fail_legacy_style_conversion))
    app = GraphPainterApp()
    async with app.run_test(size=(84, 26)) as pilot:
        widget = app.query_one(TerminalGraph)
        camera = Camera()
        snapshots = []
        for _ in range(3):
            widget.set_frame(scene(widget.size.width, widget.size.height, replace(camera)))
            await pilot.pause(.12)
            snapshots.append(tuple(strip.text for strip in painted(widget)))
            camera.rotate(yaw=.32, pitch=.08)
        assert len(set(snapshots)) == 3

        await pilot.resize_terminal(43, 12)
        widget.set_frame(scene(widget.size.width, widget.size.height, replace(camera)))
        await pilot.pause(.12)
        resized = painted(widget)
        assert len(resized) == 12 and all(strip.cell_length == 43 for strip in resized)
        assert "".join(strip.text for strip in resized).strip()

        # Clearing and then painting a shorter colored frame must erase old
        # graph cells both horizontally and below the new data.
        widget.set_frame(Text("@", style="#e9c677"))
        await pilot.pause(.12)
        shortened = painted(widget)
        assert shortened[0].text.rstrip() == "@"
        assert all(not strip.text.strip() for strip in shortened[1:])
        widget.set_frame(Text(""))
        await pilot.pause(.12)
        assert all(not strip.text.strip() for strip in painted(widget))


@pytest.mark.asyncio
async def test_frame_updates_from_background_worker_are_painted_and_shutdown_cleanly():
    app = GraphPainterApp()
    async with app.run_test(size=(72, 22)) as pilot:
        widget = app.query_one(TerminalGraph)
        camera = Camera()
        frames = []
        for _ in range(4):
            # Production GraphApp rasterizes off the event loop, then applies
            # the completed frame on the UI thread.
            frame = await asyncio.to_thread(scene, widget.size.width, widget.size.height, replace(camera))
            widget.set_frame(frame)
            await pilot.pause(.1)
            frames.append(tuple(strip.text for strip in painted(widget)))
            camera.rotate(yaw=.2)
        assert len(set(frames)) >= 3
        assert not app.workers


@pytest.mark.asyncio
async def test_production_graph_app_timer_applies_and_paints_completed_frames(tmp_path, monkeypatch):
    class ProjectionBroker:
        def ensure(self):
            return {"available": True}

        def request(self, method, path, **kwargs):
            assert method == "GET" and path == "/api/inspector/graph"
            nodes = [{"id": f"task:{index}", "kind": "task", "project_id": "paint-project"}
                     for index in range(6)]
            return {"nodes": nodes,
                    "edges": [{"from_id": nodes[index]["id"], "to_id": nodes[index + 1]["id"]}
                              for index in range(5)], "groups": []}

        async def events(self, **kwargs):
            yield await asyncio.Queue().get()

    settings = Settings(tmp_path / "data")
    root = tmp_path / "project"
    root.mkdir()
    workspace = WorkspaceState(settings, "paint-lifecycle")
    workspace.select_project({"id": "paint-project", "name": "Painter", "root": str(root)}, intent="inspect")
    workspace.update({"focus": "graph"})
    monkeypatch.setattr("cairnmind.terminal_graph.tmux_visible", lambda *args: True)
    app = GraphApp(settings, broker=ProjectionBroker(), workspace=workspace, motion=True)
    async with app.run_test(size=(90, 30)) as pilot:
        widget = app.query_one(TerminalGraph)
        # Await an actual colored graph painted by the production timer and
        # background worker, rather than returning as soon as nodes arrive.
        for _ in range(80):
            await pilot.pause(.05)
            if widget.frame.plain.strip() and any(strip.text.strip() for strip in painted(widget)):
                break
        else:
            pytest.fail("GraphApp loaded its model but never painted a completed frame")
        initial = tuple(strip.text for strip in painted(widget))
        yaw = app.model.camera.yaw
        for _ in range(40):
            await pilot.pause(.05)
            if app.model.camera.yaw != yaw and tuple(strip.text for strip in painted(widget)) != initial:
                break
        else:
            pytest.fail("GraphApp's automatic orbit did not repaint the terminal")
        await pilot.press("space")
        assert app.model.paused
        await pilot.resize_terminal(64, 22)
        await pilot.pause(.3)
        strips = painted(widget)
        assert all(strip.cell_length == 64 for strip in strips)
        assert any(strip.text.strip() for strip in strips)
