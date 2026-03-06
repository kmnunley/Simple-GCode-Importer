import bpy
import os

from bpy.props import StringProperty, CollectionProperty
from bpy_extras.io_utils import ImportHelper, poll_file_object_drop
from bpy.types import Operator, OperatorFileListElement, FileHandler

from .gcode_lib import load


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


            if e >= max_e:
                # Update the toolhead position and add the point to the curve data
                point_data.append(toolhead_pos)
                max_e = e
            
            else:
                # Check if there are enough points to create a curve
                if len(point_data) >= 2:
                    # Dump the curve data to a new curve object
                    # Create a new curve object
                    curve_data = bpy.data.curves.new("Path", type='CURVE')
                    curve_data.dimensions = '3D'
                    curve_data.use_fill_caps = True

                    # Create a curve spline and add the toolhead position as a control point
                    curve_spline = curve_data.splines.new('BEZIER')
                    for index, point in enumerate(point_data):
                        if index == 0:
                            curve_spline.bezier_points[0].co = point
                        else:
                            curve_spline.bezier_points.add(1)
                            curve_spline.bezier_points[-1].co = point
                        curve_spline.bezier_points[-1].handle_left = point
                        curve_spline.bezier_points[-1].handle_right = point

                    # Create a new object to hold the curve data
                    curve_object = bpy.data.objects.new("Path", curve_data)
                    #curve_object.location = (0, 0, 0)

                    # Link the object to the scene and the collection
                    bpy.context.collection.objects.link(curve_object)
                
                # Reset the point data
                point_data = []

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


def import_gcode(filepath):
    try:
        gf = load(filepath)
    except Exception as e:
        print(f"error: failed to load gcode/bgcode file '{filepath}': {e}")
        return

    gcode_lines = []
    for line in gf.lines:
        raw = line.raw
        if raw.endswith("\n"):
            gcode_lines.append(raw)
        else:
            gcode_lines.append(raw + "\n")

    if not gcode_lines:
        print("warning: no gcode lines found")
        return

    create_paths(gcode_lines)


class ImportGCodeOperator(Operator, ImportHelper):
    bl_idname = "import_gcode.operator"
    bl_label = "Import GCode/BGCode"

    filter_glob: StringProperty(
        default="*.gcode;*.bgcode",
        options={'HIDDEN'},
    )

    files: CollectionProperty(type=OperatorFileListElement)
    directory: StringProperty(subtype='DIR_PATH')

    def execute(self, context):
        if self.files:
            for file_elem in self.files:
                filepath = os.path.join(self.directory, file_elem.name)
                import_gcode(filepath)
        else:
            import_gcode(self.filepath)

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
    self.layout.operator(ImportGCodeOperator.bl_idname, text="GCode (.gcode)")

if __name__ == "__main__":
    register()