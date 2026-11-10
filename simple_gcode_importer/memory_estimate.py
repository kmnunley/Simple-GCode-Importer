# SPDX-License-Identifier: GPL-3.0-or-later
"""Low allocation preflight counters for an approximate viewport RAM estimate."""
from dataclasses import dataclass, field
from math import ceil
import re

from .extrusion import INFILL_ROLES, SUPPORT_ROLES, role_marker
from .gcode_lib import iter_line_batches


INFILL, SUPPORT, TOWER = 1, 2, 4
MOVE = re.compile(r'^\s*G([0-3])(?=\s|$)')
POSITION = re.compile(r'\s[XYZ]')
EXTRUSION = re.compile(r'\sE([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)(?=\s|$)')


@dataclass
class Scan:
    lines: int = 0
    # Each role bucket stores linear moves, arcs, and starts of extrusion runs.
    counts: list = field(default_factory=lambda: [[0, 0, 0] for _ in range(8)])

    def points(self, *, profile='BEAD', exclude_infill=False, exclude_supports=False,
               exclude_wipe_tower=True):
        mask = (INFILL if exclude_infill else 0) | (SUPPORT if exclude_supports else 0)
        if exclude_wipe_tower:
            mask |= TOWER
        # Arcs stand for several interpolated points. Each disconnected run
        # adds its first point plus four rounded-end points for Simulated Bead.
        ends = 5 if profile == 'BEAD' else 1
        return sum(linear + 5 * arcs + ends * runs
                   for flags, (linear, arcs, runs) in enumerate(self.counts) if not flags & mask)


def scan_batches(path, scan):
    """Count candidate extrusion moves and yield after each decoded batch."""
    role_flags = 0
    tower_block = False
    run_flags = None
    absolute_e = False
    e = high_water = 0.0
    tool = 'T0'
    extruders = {}
    for lines in iter_line_batches(path):
        scan.lines += len(lines)
        for line in lines:
            code, separator, comment = line.partition(';')
            if separator:
                role = role_marker(comment)
                if role is not None:
                    role_flags = ((INFILL if role in INFILL_ROLES else 0)
                                  | (SUPPORT if role in SUPPORT_ROLES else 0)
                                  | (TOWER if role in ('wipe tower', 'prime tower') else 0))
                marker = comment.strip().casefold()
                if marker == 'wipe_tower_start':
                    tower_block = True
                elif marker == 'wipe_tower_end':
                    tower_block = False
                    role_flags &= ~TOWER
            move = MOVE.match(code)
            if move:
                extrusion = EXTRUSION.search(code)
                if extrusion:
                    amount = float(extrusion[1])
                    e = amount if absolute_e else e + amount
                arc = move[1] in '23'
                flags = role_flags | (TOWER if tower_block else 0)
                if e > high_water and (arc or POSITION.search(code)):
                    bucket = scan.counts[flags]
                    bucket[int(arc)] += 1
                    bucket[2] += run_flags != flags
                    run_flags = flags
                else:
                    run_flags = None
                high_water = max(e, high_water)
            elif code.strip():
                command = code.split(maxsplit=1)[0]
                if command in ('M82', 'M83'):
                    absolute_e = command == 'M82'
                elif command == 'G92' and (extrusion := EXTRUSION.search(code)):
                    e = high_water = float(extrusion[1])
                elif command.startswith('T') and command[1:].isascii() and command[1:].isdigit():
                    if command != tool:
                        extruders[tool] = e, high_water
                        tool = command
                        e, high_water = extruders.get(tool, (0.0, 0.0))
                        run_flags = None
        yield


def estimate_mib(scan, **options):
    """Return a broad peak additional system RAM range for viewport imports."""
    points = scan.points(**options)
    if not points:
        return 0, 0
    # Source decoding and viewport evaluation peak in different phases.
    source_mib = scan.lines * 120 / 1024**2
    profile = options.get('profile', 'BEAD')
    if profile == 'BEAD':
        # Visible Blender 5.2 OpenGL measurements, including multicolor
        # prints and two large rockets. Leave headroom beyond the sample spread.
        center = max(source_mib, 32 + points * 6900 / 1024**2)
        return int(center * .8), ceil(center * 1.25)
    # Keep a wider range for the profiles with fewer measurements. Unbeveled
    # paths still allocate editable curves and viewport line data.
    point_bytes = 700 if profile == 'NONE' else 4500
    center = max(source_mib, points * point_bytes / 1024**2)
    return int(center * .65), ceil(center * 1.5)
