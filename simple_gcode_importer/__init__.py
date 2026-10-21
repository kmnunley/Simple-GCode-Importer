import bpy
import os
import math
from contextlib import contextmanager

from bpy.props import StringProperty, CollectionProperty
from bpy_extras.io_utils import ImportHelper, poll_file_object_drop
from bpy.types import Operator, OperatorFileListElement, FileHandler

from .gcode_lib import load


bl_info = {
    "name": "GCode Importer",
    "author": "Kevin Nunley",
    "version": (1, 2, 0),
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


def arc_points(start, end, coord, clockwise):
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
    return points


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


def create_paths(gcode_lines):
    """Create paths atomically, rolling back this import's data on failure."""
    with import_transaction() as created_data:
        _create_paths(gcode_lines, created_data)


def _create_paths(gcode_lines, created_data):
    # Initialize the toolhead position and extruder temperature
    toolhead_pos = (0, 0, 0)

    print("Creating paths...")

    absolute_coord = True
    absolute_extrude = False

    e = 0
    max_e = 0
    point_data = []
    current_tool = "T0"
    materials = {}
    extruder_positions = {}

    def dump_curve(points):
        if len(points) < 2:
            return

        curve_data = bpy.data.curves.new("Path", type='CURVE')
        created_data["curves"].append(curve_data)
        curve_data.dimensions = '3D'
        curve_data.use_fill_caps = True
        curve_data.bevel_depth = 0.2
        if current_tool not in materials:
            materials[current_tool] = bpy.data.materials.new(current_tool)
            created_data["materials"].append(materials[current_tool])
        curve_data.materials.append(materials[current_tool])

        curve_spline = curve_data.splines.new('BEZIER')
        curve_spline.bezier_points.add(len(points) - 1)
        for bezier_point, point in zip(curve_spline.bezier_points, points):
            bezier_point.co = point
            bezier_point.handle_left = point
            bezier_point.handle_right = point

        curve_object = bpy.data.objects.new("Path", curve_data)
        created_data["objects"].append(curve_object)
        bpy.context.collection.objects.link(curve_object)

    def get_params(params):
        coord = {}
        for param in params:
            if param[0] in "XYZEIJKR":
                value = float(param[1:])
                if not math.isfinite(value):
                    raise ValueError("G-code coordinates must be finite")
                coord[param[0]] = value
        return coord

    def record_move(start, end, start_e, end_e):
        nonlocal point_data, max_e
        if end_e > max_e and end != start:
            # Unretraction restores old filament before depositing anything new.
            if start_e < max_e:
                fraction = (max_e - start_e) / (end_e - start_e)
                start = tuple(a + (b - a) * fraction for a, b in zip(start, end))
                dump_curve(point_data)
                point_data = []
            if not point_data:
                point_data.append(start)
            point_data.append(end)
        else:
            dump_curve(point_data)
            point_data = []
        max_e = max(max_e, end_e)

    # Iterate through the gcode instructions
    for line_number, line in enumerate(gcode_lines, 1):
        # Split the line into words
        words = line.split(";", 1)[0].split()
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


            if command in ("G2", "G3"):
                try:
                    points = arc_points(start_pos, toolhead_pos, coord, command == "G2")
                except ValueError as error:
                    raise ValueError(f"Line {line_number}: {error}") from error
                previous, previous_e = start_pos, start_e
                for step, point in enumerate(points, 1):
                    step_e = start_e + (e - start_e) * step / len(points)
                    record_move(previous, point, previous_e, step_e)
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


def import_gcode(filepath):
    import_gcodes([filepath])


def import_gcodes(filepaths):
    with import_transaction() as created_data:
        for filepath in filepaths:
            try:
                _create_paths(load(filepath), created_data)
            except (OSError, ValueError) as error:
                raise ValueError(f"{os.path.basename(filepath)}: {error}") from error


class ImportGCodeOperator(Operator, ImportHelper):
    bl_idname = "import_gcode.operator"
    bl_label = "Import GCode/BGCode"

    filter_glob: StringProperty(
        default="*.gcode;*.bgcode",
        options={'HIDDEN'},
    )

    files: CollectionProperty(type=OperatorFileListElement)
    directory: StringProperty(subtype='DIR_PATH')

    def invoke(self, context, event):
        # A drop supplies the path already; show confirmation instead of reopening
        # the file browser. Menu imports still open the normal file selector.
        return self.invoke_popup(context)

    def execute(self, context):
        filepaths = ([os.path.join(self.directory, item.name) for item in self.files]
                     if self.files else [self.filepath])
        try:
            import_gcodes(filepaths)
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
