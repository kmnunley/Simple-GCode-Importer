import bpy
import os

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
    current_tool = "T0"
    materials = {}
    extruder_positions = {}

    def dump_curve(points):
        if len(points) < 2:
            return

        curve_data = bpy.data.curves.new("Path", type='CURVE')
        curve_data.dimensions = '3D'
        curve_data.use_fill_caps = True
        curve_data.bevel_depth = 0.2
        if current_tool not in materials:
            materials[current_tool] = bpy.data.materials.new(current_tool)
        curve_data.materials.append(materials[current_tool])

        curve_spline = curve_data.splines.new('BEZIER')
        curve_spline.bezier_points.add(len(points) - 1)
        for bezier_point, point in zip(curve_spline.bezier_points, points):
            bezier_point.co = point
            bezier_point.handle_left = point
            bezier_point.handle_right = point

        curve_object = bpy.data.objects.new("Path", curve_data)
        bpy.context.collection.objects.link(curve_object)

    def get_params(params):
        coord = [None, None, None, None]
        for param in params:
            try:
                if param[0] == "X":
                    coord[0] = float(param[1:])
                elif param[0] == "Y":
                    coord[1] = float(param[1:])
                elif param[0] == "Z":
                    coord[2] = float(param[1:])
                elif param[0] == "E":
                    coord[3] = float(param[1:])
            except:
                pass
        return tuple(coord)

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
            start_pos = toolhead_pos

            if absolute_coord:
                toolhead_pos = (
                    toolhead_pos[0] if coord[0] is None else coord[0],
                    toolhead_pos[1] if coord[1] is None else coord[1],
                    toolhead_pos[2] if coord[2] is None else coord[2]
                )
            else:

                new_pos = []
                for i in range(3):
                    offset = coord[i] if coord[i] is not None else 0
                    new_pos.append(toolhead_pos[i] + offset)
                toolhead_pos = tuple(new_pos)

            if coord[3] is not None:
                if absolute_extrude:
                    e = coord[3]
                else:
                    e = e + coord[3]


            if e > max_e and toolhead_pos != start_pos:
                # Draw only motion depositing new filament, starting at its origin.
                if not point_data:
                    point_data.append(start_pos)
                point_data.append(toolhead_pos)
            else:
                dump_curve(point_data)
                point_data = []
            max_e = max(max_e, e)

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

        elif command == "G92":
            coord = get_params(params)

            toolhead_pos = (
                toolhead_pos[0] if coord[0] is None else coord[0],
                toolhead_pos[1] if coord[1] is None else coord[1],
                toolhead_pos[2] if coord[2] is None else coord[2]
            )

            if coord[3] is not None:
                e = coord[3]
                max_e = e

    dump_curve(point_data)


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
