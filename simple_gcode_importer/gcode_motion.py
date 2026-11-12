# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared motion parsing and XY arc sampling for import and preflight."""
import math


# Maximum angle between samples; a full circle has at least 32 segments.
ARC_MAX_ANGLE = math.pi / 16
ARC_TOLERANCE = 1e-7
# Rounding XY endpoints and I/J offsets to 0.001 mm can make the two radii
# differ by up to 2 * sqrt(2) * 0.001 mm. Allow that quantization error.
ARC_RADIUS_ABS_TOLERANCE = 0.003


def _arc_geometry(start, end, coord, clockwise):
    """Return the XY sweep and sample count without allocating points."""
    if "K" in coord:
        raise ValueError("K arc offsets are unsupported; use XY arcs with I/J or R")
    if "R" in coord:
        if "I" in coord or "J" in coord:
            raise ValueError("An arc must use either R or I/J, not both")
        radius = abs(coord["R"])
        dx, dy = end[0] - start[0], end[1] - start[1]
        chord = math.hypot(dx, dy)
        if radius <= ARC_TOLERANCE or chord <= ARC_TOLERANCE:
            raise ValueError("R arcs need a nonzero radius and distinct XY endpoints")
        if chord > 2 * radius + ARC_TOLERANCE:
            raise ValueError("Arc radius is smaller than half the endpoint distance")
        height = math.sqrt(max(0, radius * radius - chord * chord / 4))
        side = -1 if clockwise else 1
        if coord["R"] < 0:
            side = -side
        cx = (start[0] + end[0]) / 2 - side * height * dy / chord
        cy = (start[1] + end[1]) / 2 + side * height * dx / chord
    else:
        if "I" not in coord and "J" not in coord:
            raise ValueError("An arc requires I/J center offsets or an R radius")
        cx = start[0] + coord.get("I", 0)
        cy = start[1] + coord.get("J", 0)
        radius = math.hypot(start[0] - cx, start[1] - cy)
        if radius <= ARC_TOLERANCE:
            raise ValueError("Arc center must differ from its start point")
        end_radius = math.hypot(end[0] - cx, end[1] - cy)
        if not math.isclose(radius, end_radius, rel_tol=1e-4,
                            abs_tol=ARC_RADIUS_ABS_TOLERANCE):
            raise ValueError("Arc endpoints have different distances from the I/J center")

    angle = math.atan2(start[1] - cy, start[0] - cx)
    end_angle = math.atan2(end[1] - cy, end[0] - cx)
    sweep = (end_angle - angle) % math.tau
    if clockwise:
        sweep -= math.tau
    elif sweep == 0:
        sweep = math.tau
    segments = max(2, math.ceil(abs(sweep) / ARC_MAX_ANGLE))
    return cx, cy, radius, angle, sweep, segments


def arc_sample_count(start, end, coord, clockwise):
    return _arc_geometry(start, end, coord, clockwise)[-1]


def arc_points(start, end, coord, clockwise, *, with_length=False):
    """Sample an XY arc, with relative I/J centers and linear Z interpolation."""
    cx, cy, radius, angle, sweep, segments = _arc_geometry(start, end, coord, clockwise)
    points = []
    for step in range(1, segments + 1):
        fraction = step / segments
        theta = angle + sweep * fraction
        points.append((cx + radius * math.cos(theta),
                       cy + radius * math.sin(theta),
                       start[2] + (end[2] - start[2]) * fraction))
    # Keep adjacent moves joined exactly, even after trig/rounding errors.
    points[-1] = end
    if with_length:
        return points, math.hypot(radius * abs(sweep), end[2] - start[2])
    return points


def get_params(params):
    coord = {}
    for param in params:
        if param[0] in "XYZEIJKR":
            value = float(param[1:])
            if not math.isfinite(value):
                raise ValueError("G-code coordinates must be finite")
            coord[param[0]] = value
    return coord

def move_position(start, coord, absolute):
    if absolute:
        return (coord.get('X', start[0]), coord.get('Y', start[1]), coord.get('Z', start[2]))
    return tuple(start[i] + coord.get(axis, 0) for i, axis in enumerate('XYZ'))
