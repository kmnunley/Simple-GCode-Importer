import bpy
import os
import math
from array import array
from contextlib import contextmanager

from bpy.props import BoolProperty, StringProperty, CollectionProperty, EnumProperty, FloatProperty
from bpy_extras.io_utils import ImportHelper, poll_file_object_drop
from bpy.types import Operator, OperatorFileListElement, FileHandler

from .gcode_lib import load
from .extrusion import INFILL_ROLES, SUPPORT_ROLES, ProfileEstimator, role_marker


bl_info = {
    "name": "GCode Importer",
    "author": "Kevin Nunley",
    "version": (1, 2, 1),
    "blender": (4, 2, 0),
    "location": "File > Import",
    "description": "Import text or binary GCode as 3D paths",
    "warning": "",
    "doc_url": "https://github.com/kmnunley/Blender-GCode-Importer",
    "category": "Import-Export",
}

# Maximum angle between samples; a full circle has at least 32 segments.
ARC_MAX_ANGLE = math.pi / 16
ARC_TOLERANCE = 1e-7
# Rounding XY endpoints and I/J offsets to 0.001 mm can make the two radii
# differ by up to 2 * sqrt(2) * 0.001 mm. Allow that quantization error.
ARC_RADIUS_ABS_TOLERANCE = 0.003


def arc_points(start, end, coord, clockwise, *, with_length=False):
    """Sample an XY arc, with relative I/J centers and linear Z interpolation."""
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


def build_bead_paths(paths_by_tool, created_data, name, profile, *, consume_paths=False):
    """One native curve per tool, all using the same flattened profile."""
    if not paths_by_tool:
        return
    from mathutils import Vector

    root = bpy.data.objects.new(name, None)
    created_data['objects'].append(root)
    root.empty_display_type = 'PLAIN_AXES'
    root['bead_width'] = profile.width
    root['bead_height'] = profile.height
    root['bead_profile_source'] = profile.source
    root['bead_profile_assumptions'] = ', '.join(profile.assumptions)
    bpy.context.collection.objects.link(root)

    def nose(point, direction):
        tangent = direction.normalized()
        # One shoulder at 45 degrees plus a zero-radius tip per end. Keep the
        # rounded taper editable without five extra stations at each endpoint.
        return [(point + tangent * profile.width / 2 * math.sin(i * math.pi / 4),
                 0 if i == 2 else math.cos(i * math.pi / 4)) for i in (1, 2)]

    z_offset = Vector((0, 0, profile.height / 2)).z
    for tool, paths in paths_by_tool.items():
        data = bpy.data.curves.new(tool, 'CURVE')
        created_data['curves'].append(data)
        data.dimensions = '3D'
        data.twist_mode = 'Z_UP'
        data.resolution_u = 2
        data.render_resolution_u = 5
        data.bevel_depth = profile.height / 2
        data.extrude = (profile.width - profile.height) / 2
        data.bevel_resolution = 1
        data.use_fill_caps = False
        material = bpy.data.materials.new(tool)
        created_data['materials'].append(material)
        data.materials.append(material)
        obj = bpy.data.objects.new(tool, data)
        created_data['objects'].append(obj)
        obj.parent = root
        for path_index, points in enumerate(paths):
            # Match Blender's float32 coordinates without retaining a Python
            # Vector and station tuple for every point in the current path.
            coords = (array('f', points) if isinstance(points, array) else
                      array('f', (value for point in points for value in point)))
            for offset in range(2, len(coords), 3):
                coords[offset] -= z_offset
            first, last = Vector(coords[:3]), Vector(coords[-3:])
            closed = (first - last).length < 1e-6
            if closed:
                del coords[-3:]
                radii = array('f', [1]) * (len(coords) // 3)
            else:
                start_cap = list(reversed(nose(first, first - Vector(coords[3:6]))))
                end_cap = nose(last, last - Vector(coords[-6:-3]))
                coords = (array('f', (value for co, radius in start_cap for value in co)) +
                          coords + array('f', (value for co, radius in end_cap for value in co)))
                radii = array('f', [1]) * (len(coords) // 3)
                radii[:2] = array('f', (radius for co, radius in start_cap))
                radii[-2:] = array('f', (radius for co, radius in end_cap))
            point_count = len(radii)
            spline = data.splines.new('BEZIER')
            spline.bezier_points.add(point_count-1)
            spline.use_cyclic_u = closed
            if closed:
                left_handles = right_handles = coords
            else:
                left_handles = array('f', coords)
                right_handles = array('f', coords)
                # Radius interpolates along the segment parameter. Linear cap
                # handles keep position in step with radius at Render U > 2;
                # collapsed handles otherwise introduce bulges between stations.
                for edge in (0, 1, point_count-3, point_count-2):
                    for axis in range(3):
                        start, end = coords[3*edge + axis], coords[3*(edge+1) + axis]
                        delta = (end - start) / 3
                        right_handles[3*edge + axis] = start + delta
                        left_handles[3*(edge+1) + axis] = end - delta
            # Native extrusion extends along the profile's vertical axis;
            # rotate it sideways to keep the bead wide and vertically thin.
            tilts = array('f', [math.pi / 2]) * point_count
            bezier_points = spline.bezier_points
            # Set coordinates before handles: moving coordinates moves handles.
            bezier_points.foreach_set('co', coords)
            bezier_points.foreach_set('handle_left', left_handles)
            bezier_points.foreach_set('handle_right', right_handles)
            bezier_points.foreach_set('radius', radii)
            bezier_points.foreach_set('tilt', tilts)
            if consume_paths:
                paths[path_index] = None
            del points, coords, left_handles, right_handles, radii, tilts
        if consume_paths:
            paths.clear()
        # Link only after all the object's splines have been populated.
        bpy.context.collection.objects.link(obj)


@contextmanager
def import_transaction():
    """Remove only this import's data if any file in the batch fails."""
    created_data = {name: [] for name in ("objects", "curves", "materials")}
    try:
        yield created_data
    except Exception:
        # Remove users before their data, preserving all pre-existing scene data.
        for name, blocks in created_data.items():
            for block in reversed(blocks):
                getattr(bpy.data, name).remove(block, do_unlink=True)
        raise


def create_paths(gcode_lines, *, exclude_wipe_tower=True, exclude_infill=False,
                 exclude_supports=False, profile='BEAD',
                 filament_diameter=1.75, layer_height=.2, extrusion_width=.45):
    """Create paths atomically, rolling back this import's data on failure."""
    with import_transaction() as created_data:
        return _create_paths(lambda: gcode_lines, created_data, exclude_wipe_tower=exclude_wipe_tower,
                             exclude_infill=exclude_infill, exclude_supports=exclude_supports,
                             profile=profile, filament_diameter=filament_diameter,
                             layer_height=layer_height, extrusion_width=extrusion_width)


def _create_paths(read_lines, created_data, name="GCode", *, exclude_wipe_tower=True,
                  exclude_infill=False, exclude_supports=False,
                  profile='BEAD', filament_diameter=1.75, layer_height=.2, extrusion_width=.45):
    if profile not in ('BEAD', 'ROUND', 'NONE'):
        raise ValueError('Unknown extrusion profile')
    gcode_lines = read_lines()
    if not isinstance(gcode_lines, list):
        gcode_lines = list(gcode_lines)
    estimator = (ProfileEstimator(gcode_lines, filament_diameter, layer_height, extrusion_width)
                 if profile != 'NONE' else None)
    bead_paths = {}
    # Initialize the toolhead position and extruder temperature
    toolhead_pos = (0, 0, 0)

    print("Creating paths...")

    absolute_coord = True
    absolute_extrude = False

    e = 0
    max_e = 0
    point_data = []
    current_tool = "T0"
    tool_objects = {}
    root = None
    extruder_positions = {}
    tower_role = False
    tower_block = False
    feature_excluded = False

    def dump_curve(points):
        nonlocal root
        if len(points) < 2:
            return

        if profile == 'BEAD':
            # Retain full parser precision in a compact buffer (24 bytes/XYZ),
            # not a list of Python coordinate tuples and float objects.
            bead_paths.setdefault(current_tool, []).append(
                array('d', (value for point in points for value in point)))
            return

        if root is None:
            root = bpy.data.objects.new(name, None)
            created_data["objects"].append(root)
            root.empty_display_type = 'PLAIN_AXES'
            bpy.context.collection.objects.link(root)

        if current_tool not in tool_objects:
            curve_data = bpy.data.curves.new(current_tool, type='CURVE')
            created_data["curves"].append(curve_data)
            curve_data.dimensions = '3D'
            curve_data.resolution_u = 2
            curve_data.render_resolution_u = 5
            curve_data.bevel_resolution = 1
            if profile == 'ROUND':
                curve_data.use_fill_caps = True
                curve_data.bevel_depth = 0.2
            material = bpy.data.materials.new(current_tool)
            created_data["materials"].append(material)
            curve_data.materials.append(material)

            curve_object = bpy.data.objects.new(current_tool, curve_data)
            created_data["objects"].append(curve_object)
            curve_object.parent = root
            tool_objects[current_tool] = curve_object

        curve_data = tool_objects[current_tool].data
        curve_spline = curve_data.splines.new('BEZIER')
        curve_spline.bezier_points.add(len(points) - 1)
        coords = array('f', (value for point in points for value in point))
        for attribute in ('co', 'handle_left', 'handle_right'):
            curve_spline.bezier_points.foreach_set(attribute, coords)

    def get_params(params):
        coord = {}
        for param in params:
            if param[0] in "XYZEIJKR":
                value = float(param[1:])
                if not math.isfinite(value):
                    raise ValueError("G-code coordinates must be finite")
                coord[param[0]] = value
        return coord

    def record_move(start, end, start_e, end_e, length=None):
        nonlocal point_data, max_e
        if exclude_wipe_tower and (tower_role or tower_block):
            # Suppress geometry only: excluded moves still advance extrusion state.
            point_data = []
        elif end_e > max_e and end != start:
            move_length = math.dist(start, end) if length is None else length
            emitted_e = end_e - max(start_e, max_e)
            if profile == 'BEAD' and estimator.flow.get(current_tool, 1) == 0:
                dump_curve(point_data)
                point_data = []
                max_e = max(max_e, end_e)
                return
            # Unretraction restores old filament before depositing anything new.
            if start_e < max_e:
                fraction = (max_e - start_e) / (end_e - start_e)
                start = tuple(a + (b - a) * fraction for a, b in zip(start, end))
                dump_curve(point_data)
                point_data = []
            if not point_data:
                point_data.append(start)
            point_data.append(end)
            if profile == 'BEAD':
                estimator.observe(current_tool, start, end, emitted_e,
                                  move_length * emitted_e / (end_e-start_e))
        else:
            dump_curve(point_data)
            point_data = []
        max_e = max(max_e, end_e)

    # Iterate through the gcode instructions
    for line_number, line in enumerate(gcode_lines, 1):
        code, separator, comment = line.partition(";")
        if separator and (exclude_infill or exclude_supports):
            role = role_marker(comment)
            if role is not None:
                excluded = ((exclude_infill and role in INFILL_ROLES) or
                            (exclude_supports and role in SUPPORT_ROLES))
                if excluded != feature_excluded:
                    dump_curve(point_data)
                    point_data = []
                feature_excluded = excluded
        if separator and profile == 'BEAD':
            estimator.comment(comment)
        if exclude_wipe_tower and separator and not code.strip():
            marker = comment.strip().casefold()
            was_tower = tower_role or tower_block
            if marker.startswith(("type:", "feature:")):
                tower_role = marker.split(":", 1)[1].strip() in ("wipe tower", "prime tower")
            elif marker == "wipe_tower_start":
                tower_block = True
            elif marker == "wipe_tower_end":
                tower_block = False
                tower_role = False
            if was_tower != (tower_role or tower_block):
                # Finish model geometry before entering the tower and never retain
                # a boundary point across excluded sections.
                dump_curve(point_data)
                point_data = []
        # Split the line into words
        words = code.split()
        if not words:
            continue

        # Extract the command and parameters
        command = words[0]
        params = words[1:]

        # Handle the movement command
        if command in ("G0", "G1", "G2", "G3"):
            coord = get_params(params)
            start_pos = toolhead_pos
            start_e = e

            if absolute_coord:
                toolhead_pos = (
                    coord.get("X", toolhead_pos[0]),
                    coord.get("Y", toolhead_pos[1]),
                    coord.get("Z", toolhead_pos[2]),
                )
            else:

                toolhead_pos = tuple(toolhead_pos[i] + coord.get(axis, 0)
                                     for i, axis in enumerate("XYZ"))

            if "E" in coord:
                if absolute_extrude:
                    e = coord["E"]
                else:
                    e = e + coord["E"]

            if feature_excluded:
                # Keep XYZ, E and retraction recovery, but allocate no path or
                # arc samples and do not feed excluded moves into bead sizing.
                max_e = max(max_e, e)
                continue

            if command in ("G2", "G3"):
                try:
                    points, length = arc_points(start_pos, toolhead_pos, coord,
                                               command == "G2", with_length=True)
                except ValueError as error:
                    raise ValueError(f"Line {line_number}: {error}") from error
                previous, previous_e = start_pos, start_e
                for step, point in enumerate(points, 1):
                    step_e = start_e + (e - start_e) * step / len(points)
                    record_move(previous, point, previous_e, step_e, length / len(points))
                    previous, previous_e = point, step_e
            else:
                record_move(start_pos, toolhead_pos, start_e, e)

        elif command.startswith("T") and command[1:].isascii() and command[1:].isdigit():
            next_tool = f"T{int(command[1:])}"
            if next_tool != current_tool:
                # Save with the old material, retaining the shared boundary point.
                dump_curve(point_data)
                point_data = point_data[-1:]
                extruder_positions[current_tool] = (e, max_e)
                current_tool = next_tool
                e, max_e = extruder_positions.get(current_tool, (0, 0))

        # Handle mode commands
        elif command == "M82":
            absolute_extrude = True

        elif command == "M83":
            absolute_extrude = False

        elif command == "G90":
            absolute_coord = True

        elif command == "G91":
            absolute_coord = False

        elif command in ('M200', 'M221') and profile == 'BEAD':
            try:
                estimator.command(command, params, current_tool)
            except ValueError as error:
                raise ValueError(f'Line {line_number}: {error}') from error

        elif command == 'G20' and profile == 'BEAD':
            raise ValueError(f'Line {line_number}: inch units are unsupported; use G21 millimeters')

        elif command in ("G18", "G19", "G90.1"):
            raise ValueError(f"Line {line_number}: {command} is unsupported; "
                             "use XY arcs with relative I/J centers")

        elif command == "G92":
            coord = get_params(params)

            toolhead_pos = (
                coord.get("X", toolhead_pos[0]),
                coord.get("Y", toolhead_pos[1]),
                coord.get("Z", toolhead_pos[2]),
            )

            if "E" in coord:
                e = coord["E"]
                max_e = e

    dump_curve(point_data)
    point_data = []
    # Release decoded source before building printed beads or linking curves.
    # Caller-owned lists passed to create_paths are never mutated.
    del gcode_lines
    for obj in tool_objects.values():
        bpy.context.collection.objects.link(obj)
    if profile == 'BEAD' and bead_paths:
        bead_profile = estimator.result()
        build_bead_paths(bead_paths, created_data, name, bead_profile, consume_paths=True)
        return list(bead_profile.assumptions)
    return []


def import_gcode(filepath, **options):
    return import_gcodes([filepath], **options)


def import_gcodes(filepaths, **options):
    assumptions = set()
    with import_transaction() as created_data:
        for filepath in filepaths:
            try:
                assumptions.update(_create_paths(lambda: load(filepath, include_metadata=True), created_data,
                              name=os.path.splitext(os.path.basename(filepath))[0],
                              **options))
            except (OSError, ValueError) as error:
                raise ValueError(f"{os.path.basename(filepath)}: {error}") from error
    return sorted(assumptions)


class ImportGCodeOperator(Operator, ImportHelper):
    bl_idname = "import_gcode.operator"
    bl_label = "Import GCode/BGCode"

    filter_glob: StringProperty(
        default="*.gcode;*.bgcode",
        options={'HIDDEN'},
    )

    files: CollectionProperty(type=OperatorFileListElement)
    directory: StringProperty(subtype='DIR_PATH')

    profile: EnumProperty(name='Extrusion profile', items=[
        ('BEAD', 'Simulated Bead', 'One flattened profile for the entire print, with rounded ends'),
        ('ROUND', 'Round', 'Circular bevel with flat caps'),
        ('NONE', 'None', 'Editable extrusion paths without thickness')], default='BEAD')
    filament_diameter: FloatProperty(name='Fallback filament diameter (mm)', default=1.75, min=.01)
    layer_height: FloatProperty(name='Fallback layer height (mm)', default=.2, min=.001)
    extrusion_width: FloatProperty(name='Fallback bead width (mm)', default=.45, min=.001)
    exclude_wipe_tower: BoolProperty(
        name="Exclude wipe/prime tower (PrusaSlicer and OrcaSlicer only)",
        description="Skip paths marked as wipe/prime tower by PrusaSlicer or OrcaSlicer; "
                    "files without recognized markers are unchanged",
        default=True,
    )

    exclude_infill: BoolProperty(
        name='Exclude infill',
        description='Skip marked sparse, internal solid and gap infill; keep top/bottom surfaces. '
                    'Requires slicer TYPE or FEATURE comments',
        default=False,
    )
    exclude_supports: BoolProperty(
        name='Exclude supports',
        description='Skip marked supports and support interfaces. Requires slicer TYPE or FEATURE comments',
        default=False,
    )

    def draw(self, context):
        column = self.layout.column(align=True)
        column.prop(self, 'profile')
        if self.profile == 'BEAD':
            column.prop(self, 'filament_diameter')
            column.prop(self, 'layer_height')
            column.prop(self, 'extrusion_width')
        column.prop(self, "exclude_wipe_tower", text="Exclude wipe/prime tower")
        column.label(text="PrusaSlicer and OrcaSlicer only")
        column.prop(self, 'exclude_infill')
        column.prop(self, 'exclude_supports')

    def invoke(self, context, event):
        # A drop supplies the path already; show confirmation instead of reopening
        # the file browser. Menu imports still open the normal file selector.
        return self.invoke_popup(context)

    def execute(self, context):
        filepaths = ([os.path.join(self.directory, item.name) for item in self.files]
                     if self.files else [self.filepath])
        try:
            assumptions = import_gcodes(filepaths, exclude_wipe_tower=self.exclude_wipe_tower,
                exclude_infill=self.exclude_infill,
                exclude_supports=self.exclude_supports, profile=self.profile, filament_diameter=self.filament_diameter,
                layer_height=self.layer_height, extrusion_width=self.extrusion_width)
            if assumptions:
                self.report({'WARNING'}, 'Bead profile used: ' + ', '.join(assumptions))
        except (OSError, ValueError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


class GCODE_FH_import(FileHandler):
    bl_idname = "GCODE_FH_import"
    bl_label = "GCode/BGCode File Handler"
    bl_import_operator = "import_gcode.operator"
    bl_file_extensions = ".gcode;.bgcode"

    @classmethod
    def poll_drop(cls, context):
        return poll_file_object_drop(context)


classes = (
    ImportGCodeOperator,
    GCODE_FH_import,
)

@bpy.app.handlers.persistent
def register():
    # Register the classes (additional class for drag and drop)
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func)

@bpy.app.handlers.persistent
def unregister():
    # Remove the operator from the File > Import menu
    bpy.types.TOPBAR_MT_file_import.remove(menu_func)
    # Unregister the operators (in reverse order to avoid dependency issues)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

def menu_func(self, context):
    self.layout.operator(ImportGCodeOperator.bl_idname, text="GCode (.gcode, .bgcode)")

if __name__ == "__main__":
    register()
