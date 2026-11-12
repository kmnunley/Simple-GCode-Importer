# SPDX-License-Identifier: GPL-3.0-or-later
"""Low allocation preflight counters for an approximate viewport RAM estimate."""
from dataclasses import dataclass, field
from math import ceil, tau

from .extrusion import INFILL_ROLES, SUPPORT_ROLES, role_marker, wipe_tower_state
from .gcode_lib import iter_line_batches
from .gcode_motion import ARC_MAX_ANGLE, arc_sample_count, get_params, move_position


INFILL, SUPPORT = 1, 2
MAX_ARC_SAMPLES = ceil(tau / ARC_MAX_ANGLE)


def _role_counts():
    return [[0, 0] for _ in range(4)]


@dataclass
class Scan:
    lines: int = 0
    # Each bucket stores move samples and starts of extrusion runs. Keep both
    # tower settings because they can assign different roles after a block.
    with_tower: list = field(default_factory=_role_counts)
    without_tower: list = field(default_factory=_role_counts)

    def points(self, *, profile='BEAD', exclude_infill=False, exclude_supports=False,
               exclude_wipe_tower=True):
        mask = (INFILL if exclude_infill else 0) | (SUPPORT if exclude_supports else 0)
        counts = self.without_tower if exclude_wipe_tower else self.with_tower
        # Allow a first point and, for Simulated Bead, four cap points per run.
        ends = 5 if profile == 'BEAD' else 1
        return sum(samples + ends * runs
                   for flags, (samples, runs) in enumerate(counts) if not flags & mask)


def scan_batches(path, scan):
    """Count candidate extrusion moves and yield after each decoded batch."""
    role_flags = outer_flags = 0
    tower_role = tower_block = False
    run_flags = outer_run_flags = None
    absolute_xyz = True
    absolute_e = False
    position = (0, 0, 0)
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
                                  | (SUPPORT if role in SUPPORT_ROLES else 0))
                    if not tower_block:
                        outer_flags = role_flags
                if not code.strip():
                    tower_role, tower_block = wipe_tower_state(comment, tower_role, tower_block)
                    if tower_role or tower_block:
                        outer_run_flags = None
            words = code.split()
            if not words:
                continue
            command = words[0]
            if command in ('G0', 'G1', 'G2', 'G3'):
                coord = get_params(words[1:])
                start = position
                position = move_position(start, coord, absolute_xyz)
                if 'E' in coord:
                    e = coord['E'] if absolute_e else e + coord['E']
                arc = command in ('G2', 'G3')
                if e > high_water and (arc or position != start):
                    samples = 1
                    if arc:
                        try:
                            samples = arc_sample_count(start, position, coord, command == 'G2')
                        except ValueError:
                            # A filtered arc may be malformed without preventing import.
                            samples = MAX_ARC_SAMPLES
                    bucket = scan.with_tower[role_flags]
                    bucket[0] += samples
                    bucket[1] += run_flags != role_flags
                    run_flags = role_flags
                    if not (tower_role or tower_block):
                        bucket = scan.without_tower[outer_flags]
                        bucket[0] += samples
                        bucket[1] += outer_run_flags != outer_flags
                        outer_run_flags = outer_flags
                else:
                    run_flags = outer_run_flags = None
                high_water = max(e, high_water)
            elif command in ('G90', 'G91'):
                absolute_xyz = command == 'G90'
            elif command in ('M82', 'M83'):
                absolute_e = command == 'M82'
            elif command == 'G92':
                coord = get_params(words[1:])
                position = move_position(position, coord, True)
                if 'E' in coord:
                    e = high_water = coord['E']
            elif command.startswith('T') and command[1:].isascii() and command[1:].isdigit():
                next_tool = f'T{int(command[1:])}'
                if next_tool != tool:
                    extruders[tool] = e, high_water
                    tool = next_tool
                    e, high_water = extruders.get(tool, (0.0, 0.0))
                    run_flags = outer_run_flags = None
        yield


def estimate_mib(scan, **options):
    """Return a broad peak additional system RAM range for viewport imports."""
    points = scan.points(**options)
    # Source decoding and viewport evaluation peak in different phases.
    source_mib = scan.lines * 120 / 1024**2
    if not points:
        return int(source_mib * .65), ceil(source_mib * 1.5)
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
