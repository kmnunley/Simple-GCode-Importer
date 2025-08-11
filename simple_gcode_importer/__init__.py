import bpy
import os
import math

from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper
from bpy.types import Operator

bl_info = {
    "name": "GCode Importer",
    "author": "Kevin Nunley",
    "version": (1, 0, 1),
    "blender": (2, 90, 0),
    "location": "File > Import",
    "description": "Import GCode files and visualize them as 3D models",
    "warning": "",
    "doc_url": "https://github.com/kmnunley/Blender-GCode-Importer",
    "category": "Import-Export",
}

def create_paths(gcode_lines):
    # Initialize the toolhead position and extruder temperature
    toolhead_pos = (0, 0, 0)

    print("Creating paths...")

    absolute_coord = True
    absolute_extrude = False

    x = 0
    y = 0
    z = 0
    e = 0
    max_e = 0
    point_data = []

    def get_params(params):
        coord = {
            "X": None,
            "Y": None,
            "Z": None,
            "E": None,
            "I": None,
            "J": None,
            "K": None,
            "R": None,
        }
        for param in params:
            try:
                key = param[0]
                if key in coord:
                    coord[key] = float(param[1:])
            except:
                pass
        return coord

    def dump_curve(points):
        if len(points) >= 2:
            curve_data = bpy.data.curves.new("Path", type='CURVE')
            curve_data.dimensions = '3D'
            curve_data.use_fill_caps = True

            curve_spline = curve_data.splines.new('BEZIER')
            for index, point in enumerate(points):
                if index == 0:
                    curve_spline.bezier_points[0].co = point
                else:
                    curve_spline.bezier_points.add(1)
                    curve_spline.bezier_points[-1].co = point
                curve_spline.bezier_points[-1].handle_left = point
                curve_spline.bezier_points[-1].handle_right = point

            curve_object = bpy.data.objects.new("Path", curve_data)
            bpy.context.collection.objects.link(curve_object)

    # Iterate through the gcode instructions
    for i, line in enumerate(gcode_lines):
        # Skip comments
        if line[0] == ";":
            continue

        # Split the line into words
        words = line.split()
        if not words:
            continue

        # Extract the command and parameters
        command = words[0]
        params = words[1:]

        # Handle the movement command
        if command == "G1" or command == "G0":
            coord = get_params(params)

            if absolute_coord:
                toolhead_pos = (
                    toolhead_pos[0] if coord["X"] is None else coord["X"],
                    toolhead_pos[1] if coord["Y"] is None else coord["Y"],
                    toolhead_pos[2] if coord["Z"] is None else coord["Z"],
                )
            else:
                new_pos = []
                for i in range(3):
                    key = ["X", "Y", "Z"][i]
                    offset = coord[key] if coord[key] is not None else 0
                    new_pos.append(toolhead_pos[i] + offset)
                toolhead_pos = tuple(new_pos)

            if coord["E"] is not None:
                if absolute_extrude:
                    e = coord["E"]
                else:
                    e = e + coord["E"]

            if e >= max_e:
                point_data.append(toolhead_pos)
                max_e = e
            else:
                dump_curve(point_data)
                point_data = []

        elif command == "G2" or command == "G3":
            coord = get_params(params)

            if absolute_coord:
                end_pos = (
                    toolhead_pos[0] if coord["X"] is None else coord["X"],
                    toolhead_pos[1] if coord["Y"] is None else coord["Y"],
                    toolhead_pos[2] if coord["Z"] is None else coord["Z"],
                )
            else:
                end_pos = (
                    toolhead_pos[0] + (coord["X"] if coord["X"] is not None else 0),
                    toolhead_pos[1] + (coord["Y"] if coord["Y"] is not None else 0),
                    toolhead_pos[2] + (coord["Z"] if coord["Z"] is not None else 0),
                )

            start_e = e
            if coord["E"] is not None:
                if absolute_extrude:
                    e = coord["E"]
                else:
                    e = e + coord["E"]
            end_e = e

            if coord["R"] is not None:
                r = abs(coord["R"])
                dx = end_pos[0] - toolhead_pos[0]
                dy = end_pos[1] - toolhead_pos[1]
                d_sq = dx * dx + dy * dy
                if d_sq == 0:
                    continue
                h_sq = r * r - d_sq / 4.0
                if h_sq < 0:
                    h_sq = 0
                h = math.sqrt(h_sq)
                sign = -1 if command == "G2" else 1
                cx = (toolhead_pos[0] + end_pos[0]) / 2 + sign * h * dy / math.sqrt(d_sq)
                cy = (toolhead_pos[1] + end_pos[1]) / 2 - sign * h * dx / math.sqrt(d_sq)
            else:
                i_off = coord["I"] if coord["I"] is not None else 0
                j_off = coord["J"] if coord["J"] is not None else 0
                cx = toolhead_pos[0] + i_off
                cy = toolhead_pos[1] + j_off

            radius = math.sqrt((toolhead_pos[0] - cx) ** 2 + (toolhead_pos[1] - cy) ** 2)
            start_ang = math.atan2(toolhead_pos[1] - cy, toolhead_pos[0] - cx)
            end_ang = math.atan2(end_pos[1] - cy, end_pos[0] - cx)
            if command == "G2":
                if end_ang >= start_ang:
                    end_ang -= 2 * math.pi
            else:
                if end_ang <= start_ang:
                    end_ang += 2 * math.pi
            delta_ang = end_ang - start_ang
            segments = max(2, int(abs(delta_ang) / (math.pi / 16)))

            for s in range(1, segments + 1):
                ang = start_ang + delta_ang * s / segments
                x = cx + radius * math.cos(ang)
                y = cy + radius * math.sin(ang)
                z = toolhead_pos[2] + (end_pos[2] - toolhead_pos[2]) * s / segments
                if coord["E"] is not None:
                    e = start_e + (end_e - start_e) * s / segments
                toolhead_pos = (x, y, z)
                if e >= max_e:
                    point_data.append(toolhead_pos)
                    max_e = e
                else:
                    dump_curve(point_data)
                    point_data = []

            toolhead_pos = end_pos

        # Handle mode commands
        elif command == "M82":
            absolute_extrude = True

        elif command == "M83":
            absolute_extrude = False

        elif command == "G90":
            absolute_coord = True

        elif command == "G91":
            absolute_coord = False

        elif command == "G92":
            coord = get_params(params)

            toolhead_pos = (
                toolhead_pos[0] if coord["X"] is None else coord["X"],
                toolhead_pos[1] if coord["Y"] is None else coord["Y"],
                toolhead_pos[2] if coord["Z"] is None else coord["Z"],
            )

            if coord["E"] is not None:
                e = coord["E"]
                max_e = e


def import_gcode(filepath):
    # Load the gcode file
    gcode_file = open(filepath, "r")
    gcode_lines = gcode_file.readlines()

    # Create the geometry
    create_paths(gcode_lines)

# Define the operator class
class ImportGCodeOperator(Operator, ImportHelper):
    bl_idname = "import_gcode.operator"
    bl_label = "Import GCode"

    filter_glob: StringProperty(
        default="*.gcode",
        options={'HIDDEN'},
    )

    def execute(self, context):

        filename, extension = os.path.splitext(self.filepath)

        import_gcode(self.filepath)
        return {'FINISHED'}

@bpy.app.handlers.persistent
def register():
    # Register the operator
    bpy.utils.register_class(ImportGCodeOperator)

    # Add the operator to the File > Import menu
    bpy.types.TOPBAR_MT_file_import.append(menu_func)

@bpy.app.handlers.persistent
def unregister():
    # Remove the operator from the File > Import menu
    bpy.types.TOPBAR_MT_file_import.remove(menu_func)

    # Unregister the operator
    bpy.utils.unregister_class(ImportGCodeOperator)

def menu_func(self, context):
    self.layout.operator(ImportGCodeOperator.bl_idname, text="GCode (.gcode)")

if __name__ == "__main__":
    register()