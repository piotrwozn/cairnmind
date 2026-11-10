"""Interactive perspective 3D memory rendered with lit terminal geometry.

Only read-only graph projections are fetched. Opening a node changes the local
workspace's inspection selection; it never starts a coding client or ACKs it.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
import hashlib
import math
import os
import subprocess
import time

from rich.console import Console
from rich.segment import Segment
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, Footer, OptionList, Static
from textual.widgets.option_list import Option

from .terminal_safe import plain, safe_text
from .config import Settings
from .terminal_i18n import localize_bindings
from .runtime_selection import resolve_tmux
from .terminal_ui import BasePanel
from .graph_raster import render_scene

NODE_LIMIT = 300
EDGE_LIMIT = 800
FPS = 10
COLORS = {"project": "#d2bc8c", "group": "#b9d791", "source": "#85c7c1", "message": "#85c7c1",
          "technical": "#75858c", "transcript": "#85c7c1", "decision": "#a9bbeb", "fact": "#b9d791",
          "task": "#dfbc78", "constraint": "#dfb98b", "revision": "#9a97bc", "plan": "#a1b5da",
          "file": "#9daab2", "symbol": "#bac6d1", "checkpoint": "#90b9d4", "unassigned": "#d0ac83"}
Vec3 = tuple[float, float, float]


def label(value, limit=160):
    return safe_text(value, limit=limit).replace("\n", " ").replace("\t", "  ")


@dataclass
class Camera:
    yaw: float = .35
    pitch: float = -.15
    distance: float = 105.0

    def rotate(self, yaw=0.0, pitch=0.0):
        self.yaw = (self.yaw + yaw) % (math.tau)
        self.pitch = max(-1.45, min(1.45, self.pitch + pitch))

    def zoom(self, factor):
        self.distance = max(22.0, min(280.0, self.distance * factor))

    def view(self, point: Vec3) -> Vec3:
        x, y, z = point
        cy, sy, cp, sp = math.cos(self.yaw), math.sin(self.yaw), math.cos(self.pitch), math.sin(self.pitch)
        horizontal, depth = cy * x + sy * z, -sy * x + cy * z
        return horizontal, cp * y - sp * depth, self.distance + sp * y + cp * depth

    def project(self, point: Vec3, width: int, height: int):
        x, y, depth = self.view(point)
        if not 1.0 < depth < 600.0 or not all(math.isfinite(v) for v in (x, y, depth)):
            return None
        focal = min(width, height) * .85
        return width / 2 + x * focal / depth, height / 2 - y * focal / depth, depth


class BrailleCanvas:
    """A 2x4-dot cell lattice with a per-dot depth buffer."""

    BITS = ((1, 8), (2, 16), (4, 32), (64, 128))

    def __init__(self, columns, rows):
        self.columns, self.rows = max(1, columns), max(1, rows)
        self.width, self.height = self.columns * 2, self.rows * 4
        self.dots = {}

    def plot(self, x, y, depth, color):
        x, y = round(x), round(y)
        if 0 <= x < self.width and 0 <= y < self.height and depth > 0:
            old = self.dots.get((x, y))
            if old is None or depth < old[0]:
                self.dots[x, y] = depth, color

    def line(self, first, second, color, *, dotted=False):
        # Clip in screen space before sampling; a near-plane point cannot make
        # a millions-of-pixels loop in a small terminal.
        x0, y0, z0 = first
        x1, y1, z1 = second
        dx, dy, low, high = x1 - x0, y1 - y0, 0.0, 1.0
        for p, q in ((-dx, x0), (dx, self.width - 1 - x0), (-dy, y0), (dy, self.height - 1 - y0)):
            if p == 0:
                if q < 0:
                    return
                continue
            ratio = q / p
            if p < 0:
                low = max(low, ratio)
            else:
                high = min(high, ratio)
        if low > high:
            return
        steps = max(1, math.ceil(max(abs(dx), abs(dy)) * (high - low)))
        steps = min(steps, 2 * (self.width + self.height))
        for index in range(steps + 1):
            if dotted and index % 3 == 0:
                continue
            t = low + (high - low) * index / steps
            depth = 1.0 / ((1 - t) / z0 + t / z1)
            self.plot(x0 + dx * t, y0 + dy * t, depth, color)

    def render(self):
        cells = {}
        for (x, y), (depth, color) in self.dots.items():
            key = x // 2, y // 4
            bits, nearest, shade = cells.get(key, (0, math.inf, color))
            cells[key] = bits | self.BITS[y % 4][x % 2], min(nearest, depth), color if depth < nearest else shade
        text = Text(no_wrap=True, overflow="crop")
        for row in range(self.rows):
            for column in range(self.columns):
                bits, _, color = cells.get((column, row), (0, 0, "#52675c"))
                text.append(chr(0x2800 | bits) if bits else " ", style=color)
            if row != self.rows - 1:
                text.append("\n")
        return text


def seed_position(identifier: str) -> Vec3:
    digest = hashlib.sha256(identifier.encode()).digest()
    angle = int.from_bytes(digest[:4]) / 2**32 * math.tau
    vertical = int.from_bytes(digest[4:8]) / 2**32 * 2 - 1
    radius = 22 + digest[8] / 255 * 25
    ring = math.sqrt(max(0, 1 - vertical * vertical))
    return radius * ring * math.cos(angle), radius * vertical, radius * ring * math.sin(angle)


def layout_positions(nodes, edges, positions):
    """Bounded 3D spring relaxation; called in a worker, never a key handler."""
    result = {node["id"]: positions.get(node["id"], seed_position(node["id"])) for node in nodes}
    anchors = {node["id"]: seed_position(node["id"]) for node in nodes}
    new = {node["id"] for node in nodes if node["id"] not in positions}
    # New children start beside their actual structural group, not a title guess.
    for edge in edges:
        if edge.get("structural") and edge["to_id"] in new and edge["from_id"] in result:
            a, b = result[edge["from_id"]], result[edge["to_id"]]
            result[edge["to_id"]] = tuple(.75 * a[i] + .3 * b[i] for i in range(3))
    for _ in range(8):
        movement = {identifier: [(anchors[identifier][i] - point[i]) * .025 for i in range(3)]
                    for identifier, point in result.items()}
        for edge in edges:
            first, second = edge["from_id"], edge["to_id"]
            if first not in result or second not in result:
                continue
            delta = tuple(result[second][i] - result[first][i] for i in range(3))
            distance = math.sqrt(sum(value * value for value in delta)) or 1
            force = max(-.6, min(.6, (distance - 16) * .018))
            for i in range(3):
                movement[first][i] += delta[i] / distance * force
                movement[second][i] -= delta[i] / distance * force
        result = {identifier: tuple(point[i] + movement[identifier][i] for i in range(3))
                  for identifier, point in result.items()}
    return result


@dataclass(frozen=True)
class GraphScope:
    project: str | None = None
    worktree: str | None = None
    group: str | None = None
    page: str | None = None


class GraphModel:
    def __init__(self, *, motion=True):
        self.camera = Camera()
        self.scope = GraphScope()
        self.generation = 0
        self.nodes, self.edges, self.groups = [], [], []
        self.positions, self.targets, self.born = {}, {}, {}
        self.selected_id = None
        self.next_cursor = None
        self.previous_pages = []
        self.cursor = None
        self.counts = {}
        self.truncated = False
        self.motion = motion
        self.paused = not motion
        self.topology = None
        self.orbit_time = 0.0

    def set_scope(self, scope):
        if scope == self.scope:
            return False
        self.scope, self.generation = scope, self.generation + 1
        self.nodes, self.edges, self.groups = [], [], []
        self.next_cursor, self.selected_id, self.topology = None, None, None
        return True

    def accept(self, data, generation=None):
        if generation is not None and generation != self.generation:
            return False
        if (not isinstance(data, dict) or not isinstance(data.get("nodes", []), list)
                or not isinstance(data.get("edges", []), list) or not isinstance(data.get("groups", []), list)):
            raise ValueError("Niepoprawny format projekcji grafu.")
        unique = {}
        for node in data.get("nodes", []):
            if isinstance(node, dict) and isinstance(node.get("id"), str) and node["id"] not in unique:
                unique[node["id"]] = dict(node)
                if len(unique) == NODE_LIMIT:
                    break
        nodes = list(unique.values())
        edges = [dict(edge) for edge in data.get("edges", []) if isinstance(edge, dict)
                 and isinstance(edge.get("from_id"), str) and isinstance(edge.get("to_id"), str)
                 and edge["from_id"] in unique and edge["to_id"] in unique][:EDGE_LIMIT]
        now = time.monotonic()
        for identifier in unique:
            if identifier not in self.positions:
                self.positions[identifier] = seed_position(identifier)
                self.born[identifier] = now
        if len(self.positions) > 3000:
            self.positions = {identifier: self.positions[identifier] for identifier in unique}
            self.born = {identifier: self.born.get(identifier, now) for identifier in unique}
        self.nodes, self.edges = nodes, edges
        self.groups = [dict(group) for group in data.get("groups", []) if isinstance(group, dict)]
        self.next_cursor, self.cursor = data.get("next_cursor"), data.get("cursor")
        self.counts = data.get("counts", {})
        self.truncated = bool(data.get("truncated") or len(data.get("nodes", [])) > len(nodes)
                              or len(data.get("edges", [])) > len(edges))
        if self.selected_id not in unique:
            self.selected_id = nodes[0]["id"] if nodes else None
        return True

    def cycle(self, step=1):
        if self.nodes:
            ids = [node["id"] for node in self.nodes]
            index = ids.index(self.selected_id) if self.selected_id in ids else 0
            self.selected_id = ids[(index + step) % len(ids)]

    @property
    def selected(self):
        return next((node for node in self.nodes if node["id"] == self.selected_id), None)

    def advance(self, elapsed):
        if self.motion and not self.paused:
            elapsed = max(0.0, min(.2, elapsed))
            self.orbit_time += elapsed
            self.camera.rotate(yaw=elapsed * .28)
        for node in self.nodes:
            identifier = node["id"]
            if identifier in self.targets:
                old, target = self.positions[identifier], self.targets[identifier]
                amount = .2 if self.motion and not self.paused else 1
                self.positions[identifier] = tuple(old[i] + (target[i] - old[i]) * amount for i in range(3))


def selection_for(node):
    project = node.get("project_id")
    entity_type = node.get("entity_type")
    identifier = node.get("id", "")
    if entity_type == "revision":
        owner_type = node.get("owner_type")
        owner_id = node.get("owner_id")
        revision_id = node.get("revision_id")
        if owner_type not in {"record", "plan"} or not isinstance(owner_id, str) or not isinstance(revision_id, str):
            raise ValueError("Niepełna tożsamość wersji historycznej.")
        return {"project_id": project, "entity_type": "revision", "id": revision_id,
                "owner_type": owner_type, "owner_id": owner_id, "revision": node["version"]}
    for kind, field in (("record", "record_id"), ("plan", "plan_id"), ("source", "source_id"), ("inbox", "inbox_id")):
        if node.get(field) or identifier.startswith(kind + ":"):
            return {"project_id": project, "entity_type": kind,
                    "id": node.get(field) or identifier.removeprefix(kind + ":")}
    return {"project_id": project, "entity_type": entity_type or "node", "id": identifier}


def terminal_visible(workspace, size, tmux=None):
    if workspace.get("layout") == "compact" and workspace.get("focus") != "graph":
        return False
    if workspace.get("zoom") and workspace.get("focus") != "graph":
        return False
    if size[0] < 10 or size[1] < 4:
        return False
    return tmux is not False


def tmux_visible(settings=None):
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return True
    try:
        binary = resolve_tmux(settings or Settings.load())
        if binary is None:
            return False
        result = subprocess.run([binary, "display-message", "-p", "-t", pane,
            "#{session_attached}:#{window_active}:#{pane_width}:#{pane_height}"],
            capture_output=True, text=True, timeout=.5, check=False)
        attached, active, width, height = map(int, result.stdout.strip().split(":"))
        return attached > 0 and active > 0 and width >= 10 and height >= 4
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


class TerminalGraph(Widget, can_focus=True):
    DEFAULT_CSS = "TerminalGraph { height: 1fr; min-height: 3; background: #0e1713; }"
    BINDINGS = [Binding("left", "left", show=False), Binding("right", "right", show=False),
                Binding("up", "up", show=False), Binding("down", "down", show=False),
                Binding("plus,equals_sign", "zoom_in", "+ zoom"), Binding("minus", "zoom_out", "− zoom"),
                Binding("space", "pause", "Pause"), Binding("r", "reset_camera", "Camera"),
                Binding("right_square_bracket", "next_node", "Next node"), Binding("left_square_bracket", "previous_node", "Previous node"),
                Binding("enter", "open", "Open"), Binding("g", "groups", "Groups"),
                Binding("n", "next_page", "Next"), Binding("p", "previous_page", "Previous"),
                Binding("a", "global_view", "Global"), Binding("backspace", "parent", "Parent")]

    def __init__(self, model, **kwargs):
        super().__init__(**kwargs)
        self.model = model
        self.frame = Text("")
        self._frame_strips = []
        self._frame_console = Console(width=240, color_system="truecolor", force_terminal=True,
                                      legacy_windows=False, markup=False, highlight=False)

    def set_frame(self, frame: Text):
        """Keep raster colors as native strips, outside Rich-to-Content conversion.

        Textual's generic Rich conversion is unnecessary for this fixed-cell
        raster and has failed on a styled frame in installed clients. Passing
        segments straight to the painter also avoids thousands of Content spans.
        """
        self.frame = frame
        self._frame_strips = [Strip(line) for line in Segment.split_lines(frame.render(self._frame_console))]
        self.refresh()

    def render(self):
        return Content(self.frame.plain)

    def render_line(self, y: int) -> Strip:
        style = self.visual_style.rich_style
        if 0 <= y < len(self._frame_strips):
            return self._frame_strips[y].adjust_cell_length(self.size.width, style).apply_style(style)
        return Strip.blank(self.size.width, style)

    def on_resize(self):
        if self.is_mounted:
            self.app.invalidate_frame()

    def action_left(self):
        self.model.camera.rotate(yaw=-.12)
        self.app.invalidate_frame()

    def action_right(self):
        self.model.camera.rotate(yaw=.12)
        self.app.invalidate_frame()

    def action_up(self):
        self.model.camera.rotate(pitch=.1)
        self.app.invalidate_frame()

    def action_down(self):
        self.model.camera.rotate(pitch=-.1)
        self.app.invalidate_frame()

    def action_zoom_in(self):
        self.model.camera.zoom(.85)
        self.app.invalidate_frame()

    def action_zoom_out(self):
        self.model.camera.zoom(1.18)
        self.app.invalidate_frame()

    def action_pause(self):
        self.model.paused = not self.model.paused
        self.app.invalidate_frame()

    def action_reset_camera(self):
        self.model.camera = Camera()
        self.app.invalidate_frame()

    def action_next_node(self):
        self.model.cycle()
        self.app.invalidate_frame()

    def action_previous_node(self):
        self.model.cycle(-1)
        self.app.invalidate_frame()

    def action_open(self):
        if self.model.selected:
            self.app.open_node(dict(self.model.selected))

    def action_groups(self):
        self.app.show_groups()

    def action_next_page(self):
        self.app.next_page()

    def action_previous_page(self):
        self.app.previous_page()

    def action_global_view(self):
        self.app.global_view()

    def action_parent(self):
        self.app.parent_view()


class GroupPicker(ModalScreen[dict | None]):
    CSS = """
    GroupPicker { align: center middle; background: $background 80%; }
    GroupPicker > Vertical { width: 85%; height: 85%; border: round #68835a; background: #111c16; }
    GroupPicker Static { height: auto; padding: 0 1; }
    GroupPicker OptionList { height: 1fr; }
    """
    BINDINGS = [("escape", "cancel", "Close"), ("n", "next_page", "More groups"), ("p", "previous_page", "Previous")]

    def __init__(self, app):
        super().__init__()
        self.graph_app = app
        self.items = []
        self.cursor = None
        self.history = []
        self.next_cursor = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(plain(self.graph_app.t("Memory groups · Enter: select · n/p: pages · Esc: back")))
            yield OptionList(id="graph-groups")
            yield Static("", id="group-notice", markup=False)

    def on_mount(self):
        localize_bindings(self, self.graph_app.language)
        self.query_one(OptionList).focus()
        self.load_page()

    @work(group="graph-group-page", exclusive=True)
    async def load_page(self):
        try:
            scope = self.graph_app.model.scope
            generation = self.graph_app.model.generation
            if scope.project is None:
                data = await self.graph_app.request("GET", "/api/state")
                self.items = [{"id": "project:" + p["id"], "kind": "project", "project_id": p["id"],
                               "label": p["name"], "project": p} for p in data.get("projects", [])]
                self.next_cursor = None
            else:
                params = {"project": scope.project, "worktree": scope.worktree, "cursor": self.cursor, "limit": 100}
                data = await self.graph_app.request("GET", "/api/inspector/graph/groups",
                                                    params={k: v for k, v in params.items() if v is not None})
                self.items, self.next_cursor = data.get("items", []), data.get("next_cursor")
            if generation != self.graph_app.model.generation:
                self.items = []
                self.query_one("#group-notice", Static).update(plain(self.graph_app.t("Scope changed; return and select the group again.")))
                return
            choices = self.query_one(OptionList)
            choices.clear_options()
            choices.add_options([Option(plain(f"{label(item.get('label', item['id']))} · {item.get('count', '')}"), id=str(i))
                                 for i, item in enumerate(self.items)])
            choices.highlighted = 0 if self.items else None
            self.query_one("#group-notice", Static).update(plain(f"{len(self.items)} " + self.graph_app.t("groups") + (" · " + self.graph_app.t("n: next page") if self.next_cursor else "")))
        except (OSError, ValueError, RuntimeError) as exc:
            self.query_one("#group-notice", Static).update(plain(exc, limit=250))

    @on(OptionList.OptionSelected)
    def select_option(self, event):
        if event.option.id is not None:
            self.dismiss(self.items[int(event.option.id)])

    def action_next_page(self):
        if self.next_cursor:
            self.history.append(self.cursor)
            self.cursor = self.next_cursor
            self.load_page()

    def action_previous_page(self):
        if self.history:
            self.cursor = self.history.pop()
            self.load_page()

    def action_cancel(self):
        self.dismiss(None)


class GraphApp(BasePanel):
    TITLE = "Cairnmind · Memory graph"
    panel_role = "graph"
    CSS = """
    #graph-heading { height: auto; max-height: 2; color: #bddb9e; }
    #graph-selection { height: auto; max-height: 3; color: #d4dfd3; }
    #graph-status { height: auto; max-height: 2; color: #8eaa99; }
    #graph-actions { height: 3; }
    #graph-actions Button { min-width: 8; width: 1fr; margin: 0; }
    """

    def __init__(self, settings, workspace_id="default", *, broker=None, workspace=None, motion=True):
        super().__init__(settings, workspace_id, broker=broker, workspace=workspace)
        from .cockpit_state import CockpitState
        preferences = CockpitState(settings).load()
        self._motion_requested = motion
        self._reduced_motion = preferences.get("reduced_motion", False)
        self._pause_before_reduction = not motion
        self.model = GraphModel(motion=motion and not self._reduced_motion)
        self._global = False
        self._dirty = True
        self._loading = False
        self._frame_loading = False
        self._frame_dirty = True
        self._active = True
        self._tmux_visible = True
        self._last_frame = time.monotonic()
        self._page_history = []

    def compose(self) -> ComposeResult:
        yield Static(plain(self.t("Memory graph · Connecting…")), id="graph-heading")
        yield TerminalGraph(self.model, id="terminal-graph")
        with Horizontal(id="graph-actions"):
            yield Button(self.t("Open"), id="graph-open")
            yield Button(self.t("Groups"), id="graph-groups")
            yield Button(self.t("Pause"), id="graph-pause")
            yield Button(self.t("Back"), id="graph-back")
        yield Static("", id="graph-selection", markup=False)
        yield Static("", id="graph-status", markup=False)
        yield Static("", id="panel-notice", markup=False)
        yield Footer()

    def on_mount(self):
        localize_bindings(self.query_one(TerminalGraph), self.language)
        self.query_one(TerminalGraph).focus()
        self.set_interval(.5, self._poll_workspace)
        self.set_interval(.25, self._try_refresh)
        self.set_interval(1 / FPS, self._schedule_frame)
        self.set_interval(2, self._check_visibility)
        self.start_panel()

    def refresh_language(self):
        localize_bindings(self.query_one(TerminalGraph), self.language)
        for name, text in (("open", "Open"), ("groups", "Groups"), ("pause", "Pause"), ("back", "Back")):
            self.query_one("#graph-" + name, Button).label = self.t(text)
        self.invalidate_frame()

    async def ready_panel(self):
        self._try_refresh()
        self.listen_events()
        self._check_visibility()

    async def workspace_changed(self, value):
        from .cockpit_state import CockpitState
        reduced = CockpitState(self.settings).load().get("reduced_motion", False)
        if reduced != self._reduced_motion:
            if reduced:
                self._pause_before_reduction = self.model.paused
            self._reduced_motion = reduced
            self.model.motion = self._motion_requested and not reduced
            self.model.paused = reduced or self._pause_before_reduction
            self.invalidate_frame()
        scope = self.model.scope
        project, worktree = (None, None) if self._global else (value.get("project_id"), value.get("worktree"))
        if (scope.project, scope.worktree) != (project, worktree):
            self.model.set_scope(GraphScope(project, worktree))
            self._page_history.clear()
            self._dirty = True
            self.invalidate_frame()

    @on(Button.Pressed)
    def graph_action(self, event):
        event.stop()
        widget = self.query_one(TerminalGraph)
        if event.button.id == "graph-open":
            widget.action_open()
        elif event.button.id == "graph-groups":
            widget.action_groups()
        elif event.button.id == "graph-pause":
            widget.action_pause()
        elif event.button.id == "graph-back":
            widget.action_parent()

    @work(group="graph-visibility", exclusive=True)
    async def _check_visibility(self):
        self._tmux_visible = await asyncio.to_thread(tmux_visible, self.settings)

    def invalidate_frame(self):
        self._frame_dirty = True

    def _visible(self):
        if not self._active or not self.is_running:
            return False  # The app is shutting down; screens may be mid-teardown.
        try:
            if isinstance(self.screen, GroupPicker):
                return False
            widget = self.query_one(TerminalGraph)
        except (NoMatches, ScreenStackError):
            return False  # A pending timer or worker can run while the pane is unmounting.
        return self._active and terminal_visible(self.workspace_data, (widget.size.width, widget.size.height), self._tmux_visible)

    def set_active(self, active):
        self._active = bool(active)
        if active:
            self._dirty = True
            self.invalidate_frame()

    def _try_refresh(self):
        if self._dirty and not self._loading and self._visible():
            self._loading = True
            self._dirty = False
            self.load_graph()

    @work(group="graph-fetch", exclusive=True)
    async def load_graph(self):
        scope, generation = self.model.scope, self.model.generation
        try:
            params = {
                "project": scope.project, "worktree": scope.worktree, "group": scope.group,
                "page_cursor": scope.page, "limit": NODE_LIMIT}
            data = await self.request("GET", "/api/inspector/graph", params={k: v for k, v in params.items() if v is not None})
            if not self.model.accept(data, generation):
                return
            topology = (tuple(n["id"] for n in self.model.nodes), tuple((e["from_id"], e["to_id"]) for e in self.model.edges))
            if topology != self.model.topology:
                self.model.topology = topology
                targets = await asyncio.to_thread(layout_positions, list(self.model.nodes), list(self.model.edges), dict(self.model.positions))
                if generation == self.model.generation:
                    self.model.targets = targets
            self.notice("")
            self.invalidate_frame()
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)
        finally:
            self._loading = False

    @work(group="graph-events", exclusive=True)
    async def listen_events(self):
        async for event in self.broker.events(after=self.model.cursor):
            data = event.get("data") or {}
            if not isinstance(data, dict):
                self.notice("Niejednoznaczne zdarzenie pamięci; odświeżono projekcję.")
                self._dirty = True
                continue
            scope = self.model.scope
            if event.get("event") == "unavailable":
                self.notice("Połączenie z pamięcią przerwane; zachowano widok i kamerę.")
            elif (event.get("event") == "reset" or scope.project is None
                  or data.get("project_id") in {None, scope.project}):
                self._dirty = True

    def _schedule_frame(self):
        if not self._visible() or self._frame_loading:
            return
        if not self._frame_dirty and (self.model.paused or not self.model.motion):
            return
        self._frame_loading = True
        self._frame_dirty = False
        self.render_frame()

    @work(group="graph-frame", exclusive=True)
    async def render_frame(self):
        generation = self.model.generation
        try:
            now = time.monotonic()
            self.model.advance(now - self._last_frame)
            self._last_frame = now
            if not self._visible():
                return  # Started after shutdown began or while hidden.
            widget = self.query_one(TerminalGraph)
            camera = replace(self.model.camera, pitch=self.model.camera.pitch + .22 * math.sin(self.model.orbit_time * .35))
            text = await asyncio.to_thread(render_scene, list(self.model.nodes), list(self.model.edges), dict(self.model.positions),
                camera, self.model.selected_id, dict(self.model.born), widget.size.width, widget.size.height,
                animate=self.model.motion and not self.model.paused)
            if generation != self.model.generation or not self._visible():
                return
            widget.set_frame(text)
            scope = self.model.scope
            title = self.t("Global memory") if scope.project is None else label(self.workspace_data.get("project_name") or scope.project)
            self.query_one("#graph-heading", Static).update(plain(self.t("Memory graph") + " · " + title + (" · " + label(scope.group) if scope.group else "")))
            selected = self.model.selected
            self.query_one("#graph-selection", Static).update(plain(
                "Enter: " + label(selected.get("label", selected["id"])) + " · " + label(selected.get("kind", ""))
                if selected else self.t("No nodes in this scope.")))
            self.query_one("#graph-status", Static).update(plain(
                f"{len(self.model.nodes)}/{NODE_LIMIT} " + self.t("nodes") + f" · {len(self.model.edges)}/{EDGE_LIMIT} " + self.t("relationships") + " · "
                + self.t("paused" if self.model.paused or not self.model.motion else "moving")
                + (" · " + self.t("n: next page") if self.model.next_cursor else "")
                + (" · " + self.t("limited view, g: groups") if self.model.truncated else "")))
        finally:
            self._frame_loading = False

    def _scope_changed(self, scope):
        self.model.set_scope(scope)
        self._dirty = True
        self.invalidate_frame()

    def next_page(self):
        if self.model.next_cursor:
            self._page_history.append(self.model.scope.page)
            self._scope_changed(replace(self.model.scope, page=self.model.next_cursor))

    def previous_page(self):
        if self._page_history:
            self._scope_changed(replace(self.model.scope, page=self._page_history.pop()))

    def global_view(self):
        self._global = not self._global
        scope = GraphScope() if self._global else GraphScope(self.workspace_data.get("project_id"), self.workspace_data.get("worktree"))
        self._page_history.clear()
        self._scope_changed(scope)

    def parent_view(self):
        if self.model.scope.group or self.model.scope.page:
            self._page_history.clear()
            self._scope_changed(replace(self.model.scope, group=None, page=None))
        else:
            self._global = True
            self._scope_changed(GraphScope())

    @work(group="graph-picker", exclusive=True)
    async def show_groups(self):
        result = await self.push_screen_wait(GroupPicker(self))
        if result:
            self.open_node(result)

    @work(group="graph-selection", exclusive=True)
    async def open_node(self, node):
        generation = self.model.generation
        try:
            kind = node.get("kind")
            if kind == "project":
                data = await self.request("GET", "/api/state")
                if generation != self.model.generation:
                    return
                project = next((p for p in data.get("projects", []) if p["id"] == node.get("project_id")), None)
                if not project:
                    raise ValueError("Projekt nie jest już zarejestrowany.")
                self._global = False
                value = await asyncio.to_thread(self.workspace.select_project, project, project["root"], intent="inspect")
                self.workspace_data = value
                await self.workspace_changed(value)
            elif node.get("entity_type") == "inbox_group":
                self._global = True
                self._scope_changed(GraphScope(group="inbox"))
                await asyncio.to_thread(self.workspace.update, {"project_id": None, "project_name": None, "worktree": None,
                    "intent": "inspect", "work_request": None, "selected_entity": {
                        "project_id": None, "entity_type": "inbox_group", "id": "inbox"}, "focus": "inspector"})
            elif kind == "group":
                self._page_history.clear()
                self._scope_changed(replace(self.model.scope, group=node.get("group", node["id"]), page=None))
            else:
                selection = selection_for(node)
                current = await asyncio.to_thread(self.workspace.read)
                if selection["project_id"] != current.get("project_id"):
                    if selection["project_id"] is None:
                        await asyncio.to_thread(self.workspace.select_project, None, intent="inspect")
                    else:
                        data = await self.request("GET", "/api/state")
                        project = next((p for p in data.get("projects", []) if p["id"] == selection["project_id"]), None)
                        if not project:
                            raise ValueError("Nieznany projekt wskazanego wpisu.")
                        await asyncio.to_thread(self.workspace.select_project, project, intent="inspect")
                await asyncio.to_thread(self.workspace.update, {"selected_entity": selection, "focus": "inspector"})
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice(exc)

    def on_unmount(self):
        self._active = False
        super().on_unmount()
        with suppress(Exception):
            self.broker.close()
