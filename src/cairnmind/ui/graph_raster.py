"""Lit, perspective-correct ASCII geometry for the terminal memory graph.

The graph supplies every position and connection.  Spheres merely give those
real nodes a visible surface: camera-space rays, normals and a depth buffer
produce the same sort of depth cues as the classic rotating ASCII donut.
"""
from __future__ import annotations

import math
import time

from rich.style import Style
from rich.text import Text

CELL_ASPECT = 2.0  # A typical terminal cell is twice as tall as it is wide.
NEAR = 1.0
FAR = 600.0
NODE_LIMIT = 300
EDGE_LIMIT = 800
LUMINANCE = ".,-~:;=!*#$@"

# Deliberately restrained: the selected *real* node is the warm accent.
NODE_COLORS = {
    "project": (190, 236, 194), "group": (136, 207, 153),
    "source": (92, 195, 181), "message": (92, 195, 181),
    "transcript": (92, 195, 181), "technical": (105, 155, 143),
    "decision": (116, 222, 209), "fact": (149, 217, 162),
    "task": (169, 220, 153), "constraint": (133, 198, 162),
    "revision": (101, 178, 158), "plan": (142, 217, 184),
    "file": (111, 177, 164), "symbol": (145, 209, 193),
    "checkpoint": (129, 214, 195), "unassigned": (147, 187, 159),
}
SELECTED_COLOR = (255, 207, 127)
EDGE_COLOR = (75, 96, 120)


class _Raster:
    """One inverse-depth sample per character, shared by edges and surfaces."""

    def __init__(self, columns, rows):
        self.columns = min(240, max(1, int(columns)))
        self.rows = min(100, max(1, int(rows)))
        self.focal = min(self.columns, self.rows * CELL_ASPECT) * 1.08
        count = self.columns * self.rows
        self.inverse_depth = [0.0] * count
        self.glyphs = [" "] * count
        self.inks = [None] * count
        self.styles = {}
        self.rays_x = [(x + .5 - self.columns / 2) / self.focal for x in range(self.columns)]
        self.rays_y = [(self.rows / 2 - y - .5) * CELL_ASPECT / self.focal
                       for y in range(self.rows)]

    def project(self, point):
        x, y, depth = point
        return (self.columns / 2 + x * self.focal / depth,
                self.rows / 2 - y * self.focal / (CELL_ASPECT * depth), depth)

    def put(self, x, y, inverse_depth, glyph, color, brightness):
        index = y * self.columns + x
        if inverse_depth <= self.inverse_depth[index]:
            return
        self.inverse_depth[index] = inverse_depth
        self.glyphs[index] = glyph
        # Quantization bounds Style allocation and joins identical adjacent ink.
        shade = max(2, min(15, round(brightness * 15)))
        self.inks[index] = (color, shade)

    def line(self, first, second, *, structural=False):
        """Clip before sampling; interpolate reciprocal depth in screen space."""
        a, b = list(first), list(second)
        for plane, keep_greater in ((NEAR + .001, True), (FAR, False)):
            inside_a = a[2] >= plane if keep_greater else a[2] <= plane
            inside_b = b[2] >= plane if keep_greater else b[2] <= plane
            if not inside_a and not inside_b:
                return
            if inside_a != inside_b:
                fraction = (plane - a[2]) / (b[2] - a[2])
                clipped = [a[i] + fraction * (b[i] - a[i]) for i in range(3)]
                if inside_a:
                    b = clipped
                else:
                    a = clipped
        x0, y0, z0 = self.project(a)
        x1, y1, z1 = self.project(b)
        dx, dy, low, high = x1 - x0, y1 - y0, 0.0, 1.0
        for p, q in ((-dx, x0), (dx, self.columns - 1 - x0),
                     (-dy, y0), (dy, self.rows - 1 - y0)):
            if p == 0:
                if q < 0:
                    return
            else:
                ratio = q / p
                if p < 0:
                    low = max(low, ratio)
                else:
                    high = min(high, ratio)
        if low > high:
            return
        steps = max(1, min(2 * (self.columns + self.rows),
                           math.ceil(max(abs(dx), abs(dy)) * (high - low) * 1.2)))
        for index in range(steps + 1):
            t = low + (high - low) * index / steps
            inverse = (1 - t) / z0 + t / z1
            depth = 1 / inverse
            fog = max(.24, min(.75, 78 / depth))
            # Dense, near links are solid; distant links recede into sparse dots.
            if (structural or depth > 135) and index % 3 == 0:
                continue
            if depth > 165:
                glyph = "."
            elif abs(dy) < abs(dx) * .35:
                glyph = "-" if depth < 120 else "."
            elif abs(dx) < abs(dy) * .35:
                glyph = "|" if depth < 120 else ":"
            else:
                glyph = "\\" if dx * dy > 0 else "/"
            x = min(self.columns - 1, max(0, int(x0 + dx * t)))
            y = min(self.rows - 1, max(0, int(y0 + dy * t)))
            self.put(x, y, inverse, glyph, EDGE_COLOR, fog)

    def sphere(self, center, radius, color):
        """Intersect view rays with a sphere; light its true surface normals."""
        cx, cy, cz = center
        if cz + radius <= NEAR or cz - radius >= FAR:
            return
        extent = max(NEAR, cz + radius)
        if (abs(cx) - radius > extent * (self.columns / 2 + 1) / self.focal
                or abs(cy) - radius > extent * (self.rows / 2 + 1) * CELL_ASPECT / self.focal):
            return
        if cz <= radius + NEAR:
            bounds = (0, self.columns - 1, 0, self.rows - 1)
        else:
            # Tangent-cone bounds also account for off-axis perspective stretch.
            divisor = cz * cz - radius * radius
            spread_x = radius * math.sqrt(cx * cx + divisor)
            spread_y = radius * math.sqrt(cy * cy + divisor)
            left, right = ((cx * cz - spread_x) / divisor,
                           (cx * cz + spread_x) / divisor)
            bottom, top = ((cy * cz - spread_y) / divisor,
                           (cy * cz + spread_y) / divisor)
            bounds = (max(0, math.floor(self.columns / 2 + left * self.focal)),
                      min(self.columns - 1, math.ceil(self.columns / 2 + right * self.focal)),
                      max(0, math.floor(self.rows / 2 - top * self.focal / CELL_ASPECT)),
                      min(self.rows - 1, math.ceil(self.rows / 2 - bottom * self.focal / CELL_ASPECT)))
        left, right, top, bottom = bounds
        constant = cx * cx + cy * cy + cz * cz - radius * radius
        inverse_radius = 1 / radius
        fog = max(.38, min(1.0, 120 / max(1, cz)))
        closest_inverse = 1 / max(NEAR, cz - radius)
        for y in range(top, bottom + 1):
            ray_y = self.rays_y[y]
            row_index = y * self.columns
            for x in range(left, right + 1):
                if self.inverse_depth[row_index + x] >= closest_inverse:
                    continue
                ray_x = self.rays_x[x]
                quadratic = ray_x * ray_x + ray_y * ray_y + 1
                dot = ray_x * cx + ray_y * cy + cz
                discriminant = dot * dot - quadratic * constant
                if discriminant <= 0:
                    continue
                root = math.sqrt(discriminant)
                depth = (dot - root) / quadratic
                if depth <= NEAR:
                    depth = (dot + root) / quadratic
                if not NEAR < depth < FAR:
                    continue
                inverse = 1 / depth
                if inverse <= self.inverse_depth[row_index + x]:
                    continue
                nx = (ray_x * depth - cx) * inverse_radius
                ny = (ray_y * depth - cy) * inverse_radius
                nz = (depth - cz) * inverse_radius
                diffuse = max(0.0, -.436 * nx + .655 * ny - .617 * nz)
                specular = max(0.0, -.24 * nx + .36 * ny - .90 * nz) ** 16
                light = min(1.0, .16 + .76 * diffuse + .20 * specular)
                luminance = max(.04, light * fog)
                glyph = LUMINANCE[min(len(LUMINANCE) - 1, int(luminance * len(LUMINANCE)))]
                self.put(x, y, inverse, glyph, color, .38 + .62 * luminance)

    def render(self):
        result = Text(no_wrap=True, overflow="crop")
        for y in range(self.rows):
            offset = y * self.columns
            start = 0
            while start < self.columns:
                ink = self.inks[offset + start]
                end = start + 1
                while end < self.columns and self.inks[offset + end] == ink:
                    end += 1
                style = None
                if ink is not None:
                    style = self.styles.get(ink)
                    if style is None:
                        color, shade = ink
                        rgb = tuple(round(channel * shade / 15) for channel in color)
                        style = Style(color=f"rgb({rgb[0]},{rgb[1]},{rgb[2]})")
                        self.styles[ink] = style
                result.append("".join(self.glyphs[offset + start:offset + end]), style=style)
                start = end
            if y != self.rows - 1:
                result.append("\n")
        return result


def render_scene(nodes, edges, positions, camera, selected, pulses, columns, rows,
                 *, now=None, animate=True):
    """Render only supplied graph IDs, with depth, lit surfaces and real edges.

    ``camera.view`` transforms world coordinates into camera space.  ``pulses``
    maps newly arrived node IDs to monotonic timestamps; it never changes graph
    membership or selection.  No node title is interpreted as terminal markup.
    """
    canvas = _Raster(columns, rows)
    moment = time.monotonic() if now is None else now
    view = {}
    visible = []
    for node in nodes[:NODE_LIMIT]:
        identifier = node.get("id")
        point = positions.get(identifier)
        if point is None:
            continue
        try:
            center = camera.view(point)
        except (TypeError, ValueError, OverflowError):
            continue
        if len(center) != 3 or not all(math.isfinite(value) for value in center):
            continue
        view[identifier] = center
        visible.append((center[2], str(identifier), node, center))
    # Stable order makes exact intersections independent of API list ordering.
    links = sorted(edges[:EDGE_LIMIT], key=lambda edge: (str(edge.get("from_id")), str(edge.get("to_id")),
                                                        str(edge.get("id", ""))))
    for edge in links:
        first, second = view.get(edge.get("from_id")), view.get(edge.get("to_id"))
        if first is not None and second is not None:
            canvas.line(first, second, structural=bool(edge.get("structural")))
    for _, _, node, center in sorted(visible, key=lambda item: (item[0], item[1])):
        identifier = node["id"]
        radius = 5.6 if identifier == selected else (5.0 if node.get("kind") == "project" else 4.3)
        if animate:
            age = moment - pulses.get(identifier, -math.inf)
            if 0 <= age < 1:
                radius *= 1 + .25 * (1 - age) ** 2
        color = SELECTED_COLOR if identifier == selected else NODE_COLORS.get(node.get("kind"), (132, 204, 168))
        canvas.sphere(center, radius, color)
    return canvas.render()
