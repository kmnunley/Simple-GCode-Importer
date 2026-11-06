# SPDX-License-Identifier: GPL-3.0-or-later
"""Choose one representative bead profile, never sampling unmarked startup moves."""
from collections import defaultdict
from dataclasses import dataclass
import math
import re


MODEL_ROLES = {
    'perimeter', 'external perimeter', 'overhang perimeter', 'outer wall', 'inner wall',
    'wall outer', 'wall inner', 'fill', 'infill', 'solid infill', 'internal solid infill',
    'sparse infill', 'top solid infill', 'bottom surface', 'top surface', 'skin',
    'support', 'support material', 'support material interface', 'support interface',
    'support transition', 'gap fill', 'gap infill',
}
WIDTH_SETTINGS = (
    'extrusion_width', 'line_width', 'perimeter_extrusion_width',
    'perimeters extrusion width', 'outer_wall_line_width',
    'external_perimeter_extrusion_width', 'external perimeters extrusion width',
)


def positive(text):
    try:
        value = float(str(text).strip().removesuffix('mm').strip())
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError):
        return None


def role_marker(comment):
    marker = comment.strip().casefold()
    if marker.startswith(('type:', 'feature:')):
        return marker.split(':', 1)[1].strip().replace('-', ' ').replace('_', ' ')
    return None


def layer_marker(comment):
    marker = comment.strip().casefold()
    if marker in ('layer_change', 'change_layer'):
        return True
    if marker.startswith('layer:'):
        try:
            return int(marker.split(':', 1)[1]) >= 0
        except ValueError:
            pass
    return False


@dataclass(frozen=True)
class BeadProfile:
    width: float
    height: float
    source: str
    assumptions: tuple = ()


class ProfileEstimator:
    def __init__(self, lines, filament_diameter=1.75, layer_height=.2, width=.45):
        if not all(positive(v) for v in (filament_diameter, layer_height, width)):
            raise ValueError('Fallback bead dimensions must be finite and positive')
        self.default_diameter = filament_diameter
        self.default_height = layer_height
        self.default_width = width
        self.diameters = []
        self.tool_diameters = {}
        self.volumetric = {}
        self.default_volumetric = False
        self.flow = {}
        self.nominal_height = None
        self.nominal_width = None
        settings = {}
        comments = [line.partition(';')[2].strip() for line in lines if ';' in line]
        self.have_model_roles = any(role_marker(c) in MODEL_ROLES for c in comments)
        self.have_layer_markers = any(layer_marker(c) or c.casefold().startswith('layer:')
                                      for c in comments)
        for comment in comments:
            key, sep, value = comment.partition('=')
            if sep:
                settings[key.strip().casefold()] = value.strip()
        if 'filament_diameter' in settings:
            values = [positive(v) for v in re.split('[,;]', settings['filament_diameter'])]
            if values and all(values):
                self.diameters = values
        self.nominal_height = positive(settings.get('layer_height'))
        self.default_volumetric = settings.get('use_volumetric_e', '').casefold() in ('1', 'true')
        for key in WIDTH_SETTINGS:
            value = positive(settings.get(key))
            if value:
                self.nominal_width = value
                break
        self.layer_started = False
        self.model_role = False
        self.excluded_role = False
        self.excluded_blocks = set()
        self.local_width = self.local_height = None
        self.last_model_z = None
        self.inferred_height = None
        self.samples = defaultdict(float)
        self.sample_assumptions = defaultdict(set)

    def comment(self, comment):
        marker = comment.strip().casefold()
        # Excluded sections have their own annotations. Keep the surrounding
        # model's hints and feature/layer context intact until all blocks end.
        for block in ('wipe_tower', 'purge', 'prime_tower'):
            if marker == block + '_start':
                self.excluded_blocks.add(block)
                return
            if marker == block + '_end':
                self.excluded_blocks.discard(block)
                return
        if self.excluded_blocks:
            return
        role = role_marker(comment)
        if role is not None:
            was_model = self.model_role
            self.model_role = role in MODEL_ROLES
            self.excluded_role = not self.model_role
            self.local_width = None
            if not was_model or not self.model_role:
                self.local_height = None
        elif layer_marker(marker):
            self.layer_started = True
            self.local_width = self.local_height = None
            # A layer marker may follow a startup Custom role in files that
            # have no recognized model feature labels at all.
            self.excluded_role = False
        elif marker.startswith('layer:'):
            self.layer_started = False
        for tag, attribute in (('width:', 'local_width'), ('height:', 'local_height'),
                               ('line_width:', 'local_width'), ('layer_height:', 'local_height')):
            if marker.startswith(tag):
                setattr(self, attribute, positive(marker[len(tag):]))

    def command(self, command, params, tool):
        values = {p[0]: float(p[1:]) for p in params if p[:1] in ('D', 'S', 'T')}
        if not all(math.isfinite(v) for v in values.values()):
            raise ValueError('Extrusion settings must be finite')
        if 'T' in values and (values['T'] < 0 or not values['T'].is_integer()):
            raise ValueError('Extrusion tool must be a nonnegative integer')
        target = f"T{int(values['T'])}" if 'T' in values else tool
        if command == 'M221' and 'S' in values:
            if values['S'] < 0:
                raise ValueError('Flow percentage cannot be negative')
            self.flow[target] = values['S'] / 100
        elif command == 'M200':
            if 'D' in values:
                if values['D'] < 0:
                    raise ValueError('Filament diameter cannot be negative')
                self.volumetric[target] = values['D'] > 0
                if values['D'] > 0:
                    self.tool_diameters[target] = values['D']
            if 'S' in values:
                self.volumetric[target] = bool(values['S'])

    def observe(self, tool, start, end, deposited_e, length):
        eligible = self.model_role if self.have_model_roles else self.layer_started
        if self.have_layer_markers and not self.layer_started:
            return
        if not eligible or self.excluded_role or self.excluded_blocks or length <= 0:
            return
        if deposited_e <= 0 or self.flow.get(tool, 1) <= 0:
            return
        # Only model elevations contribute to layer inference. Startup purge
        # elevations and travel Z hops never enter this state.
        planar = abs(end[2] - start[2]) < 1e-6
        if planar and (self.last_model_z is None or end[2] > self.last_model_z + 1e-5):
            if self.last_model_z is not None:
                self.inferred_height = end[2] - self.last_model_z
            self.last_model_z = end[2]
        assumptions = set()
        height = self.local_height or self.nominal_height or self.inferred_height
        if height is None:
            height = self.default_height
            assumptions.add('fallback layer height')
        width = self.local_width
        if width is None or self.flow.get(tool, 1) != 1:
            volume = deposited_e
            if not self.volumetric.get(tool, self.default_volumetric):
                diameter = self.tool_diameters.get(tool)
                if diameter is None and self.diameters:
                    diameter = self.diameters[min(int(tool[1:]), len(self.diameters)-1)]
                if diameter is None:
                    diameter = self.default_diameter
                    assumptions.add('fallback filament diameter')
                volume *= math.pi * diameter**2 / 4
            area = volume * self.flow.get(tool, 1) / length
            width = area / height + height * (1-math.pi/4)
        if not math.isfinite(width) or width < height:
            return  # Thin/ironing-like moves cannot define the normal bead.
        key = (max(round(width, 2), round(height, 3)), round(height, 3))
        if key[1] <= 0:
            return
        self.samples[key] += length
        self.sample_assumptions[key].update(assumptions)

    def result(self):
        if self.samples:
            # Length-weighted mode resists tiny special-purpose paths and
            # rounding noise. All imported tools use this ONE selected pair.
            width, height = max(self.samples, key=lambda k: (self.samples[k], -k[0], -k[1]))
            return BeadProfile(width, height, 'model extrusion',
                               tuple(sorted(self.sample_assumptions[(width, height)])))
        height = self.nominal_height or self.default_height
        width = self.nominal_width or self.default_width
        assumptions = []
        if self.nominal_height is None:
            assumptions.append('fallback layer height')
        if self.nominal_width is None:
            assumptions.append('fallback bead width (no identified model extrusion)')
        return BeadProfile(max(width, height), height, 'slicer settings' if not assumptions else 'fallback',
                           tuple(assumptions))
