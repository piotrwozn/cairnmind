"""Geometric and visual regressions for the shaded terminal graph renderer."""
from __future__ import annotations

import math
from statistics import median
from time import perf_counter

from conftest import time_budget

from cairnmind.graph_raster import LUMINANCE, SELECTED_COLOR, _Raster, render_scene
from cairnmind.terminal_graph import Camera, seed_position


def _occupied(text):
    return {(x, y): char for y, line in enumerate(text.plain.splitlines())
            for x, char in enumerate(line) if char != " "}


def _scene(positions, *, selected=None, edges=None, camera=None, columns=90, rows=35):
    nodes = [{"id": identifier, "kind": "decision", "title": "[blink]untrusted[/blink]\x1b[31m"}
             for identifier in positions]
    return render_scene(nodes, edges or [], positions, camera or Camera(yaw=0, pitch=0, distance=100),
                        selected, {}, columns, rows, animate=False)


def test_near_and_far_spheres_have_perspective_size_and_surface_shading():
    near = _scene({"node": (0, 0, -40)})
    far = _scene({"node": (0, 0, 40)})
    near_cells, far_cells = _occupied(near), _occupied(far)
    assert len(near_cells) >= 3 * len(far_cells) > 0
    assert len(set(near_cells.values())) >= 5
    assert set(near_cells.values()) <= set(LUMINANCE)
    assert "[blink]" not in near.plain and "\x1b" not in near.plain
    assert all(span.style.blink is not True for span in near.spans)


def test_near_sphere_completely_occludes_far_sphere_independent_of_input_order():
    front = _scene({"near": (0, 0, -20)})
    combined = _scene({"far": (0, 0, 25), "near": (0, 0, -20)})
    reversed_scene = _scene({"near": (0, 0, -20), "far": (0, 0, 25)})
    assert front.plain == combined.plain == reversed_scene.plain
    assert front.spans == combined.spans == reversed_scene.spans


def test_terminal_aspect_produces_round_physical_spheres_and_directional_light():
    raster = _Raster(100, 45)
    raster.sphere((0, 0, 100), 17, (150, 220, 180))
    cells = _occupied(raster.render())
    width = max(x for x, _ in cells) - min(x for x, _ in cells) + 1
    height = max(y for _, y in cells) - min(y for _, y in cells) + 1
    assert 1.7 < width / height < 2.3
    top_left = [LUMINANCE.index(glyph) for (x, y), glyph in cells.items() if x < 50 and y < 22]
    bottom_right = [LUMINANCE.index(glyph) for (x, y), glyph in cells.items() if x >= 50 and y > 22]
    assert sum(top_left) / len(top_left) > sum(bottom_right) / len(bottom_right) + 2


def test_edges_and_spheres_share_depth_buffer_in_both_directions():
    sphere = _Raster(80, 30)
    sphere.sphere((0, 0, 100), 10, (150, 220, 180))
    standalone = sphere.render()
    behind = _Raster(80, 30)
    behind.line((-20, 0, 130), (20, 0, 130))
    behind.sphere((0, 0, 100), 10, (150, 220, 180))
    in_front = _Raster(80, 30)
    in_front.line((-20, 0, 75), (20, 0, 75))
    in_front.sphere((0, 0, 100), 10, (150, 220, 180))
    center = 15 * (80 + 1) + 40
    assert behind.render().plain[center] == standalone.plain[center] != "-"
    assert in_front.render().plain[center] == "-"
    sphere.line((-20, 0, 130), (20, 0, 130))
    assert sphere.render().plain == behind.render().plain


def test_crossing_edges_use_perspective_inverse_depth_and_real_endpoints():
    raster = _Raster(80, 30)
    # The horizontal edge crosses depth75 at screen center.  Linear world-depth
    # interpolation would incorrectly place it behind the vertical depth100 edge.
    raster.line((-15, 0, 50), (45, 0, 150))
    raster.line((0, -15, 100), (0, 15, 100))
    index = 15 * 80 + 40
    assert raster.glyphs[index] == "-"
    assert math.isclose(raster.inverse_depth[index], 1 / 75, rel_tol=.05)
    no_edge = _scene({"only": (0, 0, 0)})
    unknown_edge = _scene({"only": (0, 0, 0)}, edges=[{"from_id": "missing", "to_id": "only"}])
    assert unknown_edge.plain == no_edge.plain
    assert unknown_edge.spans == no_edge.spans


def test_camera_rotates_real_xyz_positions_and_selected_node_stays_warm():
    positions = {"selected": (-17, 0, -25), "other": (18, 6, 20)}
    front = _scene(positions, selected="selected")
    rotated = _scene(positions, selected="selected", camera=Camera(yaw=math.pi / 2, pitch=.4, distance=100))
    assert front.plain != rotated.plain
    assert any(span.style.color.triplet.red > span.style.color.triplet.green for span in front.spans)
    raster = _Raster(90, 35)
    raster.sphere((0, 0, 100), 6, SELECTED_COLOR)
    assert all(ink[0] == SELECTED_COLOR for ink in raster.inks if ink)


def test_pulse_animates_only_real_node_surface_and_static_mode_is_repeatable():
    nodes = [{"id": "a", "kind": "fact"}]
    args = (nodes, [], {"a": (0, 0, 0)}, Camera(yaw=0, pitch=0), "a", {"a": 10}, 100, 40)
    new = render_scene(*args, now=10)
    settled = render_scene(*args, now=12)
    assert len(_occupied(new)) > len(_occupied(settled))
    static = render_scene(*args, now=10, animate=False)
    later = render_scene(*args, now=12, animate=False)
    assert static.plain == later.plain == settled.plain
    assert static.spans == later.spans == settled.spans


def test_empty_invalid_offscreen_and_near_plane_data_are_bounded():
    text = _scene({"bad": (math.nan, 0, 0), "offscreen": (1e100, 0, 0), "behind": (0, 0, -200)},
                  columns=70, rows=25)
    assert text.plain == "\n".join([" " * 70] * 25)
    tiny = _scene({"node": (0, 0, 0)}, columns=0, rows=0)
    assert len(tiny.plain) == 1
    raster = _Raster(70, 25)
    raster.line((-1e100, 0, -10), (1e100, 0, 150))
    assert len(raster.render().plain.splitlines()) == 25


def test_full_graph_render_budget_and_bounded_output():
    nodes = [{"id": str(i), "kind": ("fact", "decision", "task", "source")[i % 4]} for i in range(300)]
    positions = {node["id"]: seed_position(node["id"]) for node in nodes}
    edges = [{"from_id": str(i % 300), "to_id": str((i * 7 + 13) % 300)} for i in range(800)]
    for columns, rows in ((70, 25), (180, 55)):
        durations = []
        for angle in (0, .3, .6):
            start = perf_counter()
            result = render_scene(nodes, edges, positions, Camera(yaw=angle), "0", {}, columns, rows,
                                  animate=False)
            durations.append(perf_counter() - start)
        # The measured target is 100ms; leave CI headroom for the shared database
        # and browser processes.  A quadratic or unbounded raster regression fails.
        assert median(durations) < time_budget(.18)
        assert len(result.plain.splitlines()) == rows
        assert all(len(line) == columns for line in result.plain.splitlines())
        assert len(_occupied(result)) > 100
        assert len(result.spans) < columns * rows
