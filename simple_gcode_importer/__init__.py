import bpy
import os
import math
import time
from array import array
from contextlib import closing, contextmanager

from bpy.props import BoolProperty, StringProperty, CollectionProperty, EnumProperty, FloatProperty
from bpy_extras.io_utils import ImportHelper, poll_file_object_drop
from bpy.types import Operator, OperatorFileListElement, FileHandler

from .gcode_lib import load, load_steps
from .gcode_motion import arc_points, get_params, move_position
from .extrusion import INFILL_ROLES, SUPPORT_ROLES, ProfileEstimator, role_marker, wipe_tower_state
from .memory_estimate import Scan, estimate_mib, scan_batches


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

def _finish_steps(steps):
    while True:
        try:
            next(steps)
        except StopIteration as done:
            return done.value


def build_bead_paths(paths_by_tool, created_data, name, profile, *, consume_paths=False):
    return _finish_steps(_build_bead_paths_steps(
        paths_by_tool, created_data, name, profile, consume_paths=consume_paths))


def _build_bead_paths_steps(paths_by_tool, created_data, name, profile, *, consume_paths=False):
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
    total = sum(len(paths) for paths in paths_by_tool.values())
    completed = 0
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
            if completed % 128 == 0:
                yield completed / total
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
            completed += 1
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
    except (Exception, GeneratorExit):
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
        return _finish_steps(_create_paths_steps(
            gcode_lines, created_data, exclude_wipe_tower=exclude_wipe_tower,
            exclude_infill=exclude_infill, exclude_supports=exclude_supports,
            profile=profile, filament_diameter=filament_diameter,
            layer_height=layer_height, extrusion_width=extrusion_width))


def _create_paths_steps(gcode_lines, created_data, name="GCode", *, exclude_wipe_tower=True,
                        exclude_infill=False, exclude_supports=False,
                        profile='BEAD', filament_diameter=1.75, layer_height=.2, extrusion_width=.45):
    if profile not in ('BEAD', 'ROUND', 'NONE'):
        raise ValueError('Unknown extrusion profile')
    if not isinstance(gcode_lines, list):
        gcode_lines = list(gcode_lines)
    yield .55, 'Preparing paths'
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
        if (line_number - 1) % 2048 == 0:
            span = .17 if profile == 'BEAD' else .42
            yield .56 + span * (line_number - 1) / len(gcode_lines), 'Parsing G-code'
        code, separator, comment = line.partition(";")
        # Feature labels inside a skipped tower describe that block only.
        # Keep the surrounding feature active when model extrusion resumes.
        if separator and not tower_block and (exclude_infill or exclude_supports):
            role = role_marker(comment)
            if role is not None:
                excluded = ((exclude_infill and role in INFILL_ROLES) or
                            (exclude_supports and role in SUPPORT_ROLES))
                if excluded != feature_excluded:
                    dump_curve(point_data)
                    point_data = []
                feature_excluded = excluded
        if separator and profile == 'BEAD':
            estimator.comment(comment, feature_excluded=feature_excluded)
        if exclude_wipe_tower and separator and not code.strip():
            was_tower = tower_role or tower_block
            tower_role, tower_block = wipe_tower_state(comment, tower_role, tower_block)
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

            toolhead_pos = move_position(start_pos, coord, absolute_coord)

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

            toolhead_pos = move_position(toolhead_pos, coord, True)

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
        for fraction in _build_bead_paths_steps(
                bead_paths, created_data, name, bead_profile, consume_paths=True):
            yield .73 + .25 * fraction, 'Building curves'
        return list(bead_profile.assumptions)
    return []


def import_gcode(filepath, **options):
    return import_gcodes([filepath], **options)


def import_gcodes(filepaths, **options):
    return _finish_steps(_import_gcodes_steps(filepaths, **options))


def _import_gcodes_steps(filepaths, *, interactive=False, **options):
    filepaths = list(filepaths)
    assumptions = set()
    with import_transaction() as created_data:
        for index, filepath in enumerate(filepaths):
            filename = os.path.basename(filepath)
            label = f'{index + 1}/{len(filepaths)}: {filename}'
            try:
                yield index / len(filepaths), f'Reading {label}'
                if interactive:
                    with closing(load_steps(filepath, include_metadata=True)) as reader:
                        while True:
                            try:
                                decoded = next(reader)
                            except StopIteration as done:
                                lines = done.value
                                del done
                                break
                            yield (index + .54 * decoded) / len(filepaths), f'Reading {label}'
                else:
                    lines = load(filepath, include_metadata=True)
                steps = _create_paths_steps(
                    lines, created_data, name=os.path.splitext(filename)[0], **options)
                del lines
                with closing(steps):
                    while True:
                        try:
                            fraction, stage = next(steps)
                        except StopIteration as done:
                            assumptions.update(done.value)
                            break
                        yield (index + fraction) / len(filepaths), f'{stage} — {label}'
            except (OSError, ValueError) as error:
                raise ValueError(f"{filename}: {error}") from error
        if interactive:
            yield .99, 'Finalizing import'
            bpy.context.view_layer.update()
    return sorted(assumptions)


class _MemoryPreview:
    """Cache a selection's counters and scan in short Blender timer callbacks."""

    def __init__(self):
        self.callback = self.tick
        self.key = None
        self.scan = None
        self.job = None
        self.error = False

    def cancel(self):
        if bpy.app.timers.is_registered(self.callback):
            bpy.app.timers.unregister(self.callback)
        if self.job is not None:
            self.job.close()
        self.key = self.scan = self.job = None
        self.error = False

    def request(self, paths):
        try:
            key = tuple(self.signature(path) for path in paths)
        except OSError:
            self.cancel()
            return
        if key == self.key:
            return
        self.cancel()
        if not key:
            return
        self.key = key
        self.scan = Scan()
        self.job = self.count(paths)
        bpy.app.timers.register(self.callback, first_interval=.15)

    @staticmethod
    def signature(path):
        stat = os.stat(path)
        return path, stat.st_size, stat.st_mtime_ns

    def count(self, paths):
        for path in paths:
            yield from scan_batches(path, self.scan)

    def tick(self):
        try:
            deadline = time.perf_counter() + .01
            while time.perf_counter() < deadline:
                next(self.job)
        except (StopIteration, OSError, ValueError) as result:
            self.error = not isinstance(result, StopIteration)
            self.job.close()
            self.job = None
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    area.tag_redraw()
            return None
        return .02


_memory_preview = _MemoryPreview()
_active_import = None


class _ImportStatus:
    """Own the status display from import start until the completion message expires."""

    def __init__(self):
        self.workspace = None
        self.draw = None
        self.previous_draw = None
        self.message = None
        self.expire = self._expire

    def start(self, workspace):
        self.clear()
        self.workspace = workspace
        previous_draw = self.previous_draw = bpy.types.STATUSBAR_HT_header.draw

        def draw(header, context):
            if self.draw is draw and context.workspace == workspace:
                if self.message is not None:
                    header.layout.label(text=self.message)
                elif _active_import is not None:
                    row = header.layout.row()
                    row.ui_units_x = 24
                    row.progress(factor=_active_import._progress,
                                 text=f'{_active_import._progress:.0%} · {_active_import._stage}')
                    header.layout.label(text='Esc to cancel G-code import')
            previous_draw(header, context)

        self.draw = draw
        workspace.status_text_set(draw)

    def redraw(self):
        # The public text setter can restore Blender's cached header draw
        # function. Use its redraw notifier without replacing our callback.
        self.workspace.status_text_set_internal(None)

    def complete(self, elapsed):
        minutes, seconds = divmod(elapsed, 60)
        duration = f'{int(minutes)}m {seconds:.1f}s' if minutes else f'{seconds:.1f}s'
        self.message = f'G-code import complete — {duration}'
        self.redraw()
        bpy.app.timers.register(self.expire, first_interval=5.0)

    def clear(self):
        if bpy.app.timers.is_registered(self.expire):
            bpy.app.timers.unregister(self.expire)
        self._expire()

    def _expire(self):
        header = bpy.types.STATUSBAR_HT_header
        if self.draw is not None and header.draw is self.draw:
            # Restore the actual prior callback, including other add-ons.
            header.draw = self.previous_draw
            try:
                self.redraw()
            except ReferenceError:
                pass  # The workspace may have been removed.
        self.workspace = self.draw = self.previous_draw = self.message = None


_import_status = _ImportStatus()


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
        paths = self.selected_paths(context)
        _memory_preview.request(paths)
        if paths:
            preview = column.box()
            preview.label(text='Estimated RAM required')
            if _memory_preview.job is not None:
                preview.label(text='Reading G-code…')
            elif _memory_preview.error or _memory_preview.scan is None:
                preview.label(text='Estimate unavailable')
            else:
                low, high = estimate_mib(_memory_preview.scan, profile=self.profile,
                    exclude_infill=self.exclude_infill, exclude_supports=self.exclude_supports,
                    exclude_wipe_tower=self.exclude_wipe_tower)
                if high >= 1024:
                    label = f'About {low / 1024:.1f}–{high / 1024:.1f} GiB'
                elif high:
                    label = f'About {low}–{high} MiB'
                else:
                    label = 'No extrusion moves found'
                preview.label(text=label)

    def selected_paths(self, context):
        space = context.space_data
        if space and space.type == 'FILE_BROWSER' and space.params:
            directory = os.fsdecode(space.params.directory)
            names = [item.name for item in (getattr(context, 'selected_files', None) or ())]
            if not names and space.params.filename:
                names = [space.params.filename]
            paths = [os.path.join(directory, name) for name in names]
        else:
            paths = self.import_paths()
        return tuple(path for path in paths if path.lower().endswith(('.gcode', '.bgcode'))
                     and os.path.isfile(path))

    def import_paths(self):
        return ([os.path.join(self.directory, item.name) for item in self.files]
                if self.files else [self.filepath])

    def invoke(self, context, event):
        # A drop supplies the path already; show confirmation instead of reopening
        # the file browser. Menu imports still open the normal file selector.
        return self.invoke_popup(context)

    def cancel(self, context):
        _memory_preview.cancel()
        if _active_import is self:
            self._stop_import(context)

    def _stop_import(self, context, *, completed=False):
        global _active_import
        try:
            self._import_steps.close()
        finally:
            _active_import = None
            if self._import_timer is not None:
                context.window_manager.event_timer_remove(self._import_timer)
                self._import_timer = None
            if completed:
                _import_status.complete(time.perf_counter() - self._import_started)
            else:
                _import_status.clear()

    def execute(self, context):
        global _active_import
        if _active_import is not None:
            self.report({'WARNING'}, 'A G-code import is already running')
            return {'CANCELLED'}
        _memory_preview.cancel()
        filepaths = self.import_paths()
        options = dict(exclude_wipe_tower=self.exclude_wipe_tower,
            exclude_infill=self.exclude_infill, exclude_supports=self.exclude_supports,
            profile=self.profile, filament_diameter=self.filament_diameter,
            layer_height=self.layer_height, extrusion_width=self.extrusion_width)
        if self.options.is_invoke and not bpy.app.background and context.window:
            self._import_steps = _import_gcodes_steps(filepaths, interactive=True, **options)
            self._import_timer = None
            self._import_started = time.perf_counter()
            self._progress, self._stage = 0, 'Starting G-code import'
            _active_import = self
            try:
                self._import_timer = context.window_manager.event_timer_add(.03, window=context.window)
                context.window_manager.modal_handler_add(self)
                _import_status.start(context.workspace)
            except Exception:
                self.cancel(context)
                raise
            return {'RUNNING_MODAL'}
        try:
            self._report_assumptions(import_gcodes(filepaths, **options))
        except (OSError, ValueError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}

    def _report_assumptions(self, assumptions):
        if assumptions:
            self.report({'WARNING'}, 'Bead profile used: ' + ', '.join(assumptions))

    def modal(self, context, event):
        if event.type == 'ESC':
            self.cancel(context)
            return {'CANCELLED'}
        if event.type != 'TIMER':
            return {'RUNNING_MODAL'}
        try:
            deadline = time.perf_counter() + .02
            while True:
                previous_stage = self._stage
                self._progress, self._stage = next(self._import_steps)
                if self._stage != previous_stage or time.perf_counter() >= deadline:
                    break
        except StopIteration as done:
            self._stop_import(context, completed=True)
            self._report_assumptions(done.value)
            return {'FINISHED'}
        except Exception as error:
            self.cancel(context)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        _import_status.redraw()
        return {'RUNNING_MODAL'}


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
    if _active_import is not None:
        _active_import.cancel(bpy.context)
    _memory_preview.cancel()
    _import_status.clear()
    # Remove the operator from the File > Import menu
    bpy.types.TOPBAR_MT_file_import.remove(menu_func)
    # Unregister the operators (in reverse order to avoid dependency issues)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

def menu_func(self, context):
    self.layout.operator(ImportGCodeOperator.bl_idname, text="GCode (.gcode, .bgcode)")

if __name__ == "__main__":
    register()
