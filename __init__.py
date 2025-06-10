import bpy
import os

from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper
from bpy.types import Operator

bl_info = {
    "name": "GCode Importer",
    "author": "Kevin Nunley",
    "version": (1, 0, 0),
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

    # Create an empty collection to store the paths
    collection = bpy.data.collections.new("Paths")

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
    for line in gcode_lines:
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
                toolhead_pos = toolhead_pos + (
                    0 if coord[0] is None else coord[0],
                    0 if coord[1] is None else coord[1],
                    0 if coord[2] is None else coord[2]
                )

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
                    curve_data.resolution_u = 1

                    # Create a curve spline and add the toolhead position as a control point
                    curve_spline = curve_data.splines.new('BEZIER')
                    for index, point in enumerate(point_data):
                        if index == 0:
                            curve_spline.bezier_points[0].co = point
                        else:
                            curve_spline.bezier_points.add(1)
                            curve_spline.bezier_points[-1].co = point

                    # Create a new object to hold the curve data
                    curve_object = bpy.data.objects.new("Path", curve_data)
                    #curve_object.location = (0, 0, 0)

                    # Link the object to the scene and the collection
                    bpy.context.collection.objects.link(curve_object)
                    collection.objects.link(curve_object)
                
                # Reset the point data
                point_data = []

        # Handle mode commands
        elif command == "M82":
            absolute_extrude = True

        elif command == "M83":
            absolute_extrude = False

        elif command == "G90":
            absolute_coord = True
            absolute_extrude = True

        elif command == "G91":
            absolute_coord = False
            absolute_extrude = False

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