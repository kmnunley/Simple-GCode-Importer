"""One shared printed-bead profile, tool grouping, purge exclusion and caps."""
import importlib.util
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import bpy
from bgcode_fixtures import binary, block

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bead_test_importer', ROOT/'simple_gcode_importer/__init__.py')
importer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = importer
spec.loader.exec_module(importer)

HEADER = ';filament_diameter = 1.75\n;layer_height = .2\nM83\n'
# A 0.45 x 0.20 stadium extruded over 10 mm with 1.75 mm filament.
MODEL_E = ((.45-.2)*.2 + math.pi*.1**2) * 10 / (math.pi*1.75**2/4)


class BeadProfileTests(unittest.TestCase):
    def setUp(self):
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)

    def paths(self, text, **options):
        importer.create_paths(text.splitlines(True), **options)
        bpy.context.view_layer.update()
        return [o for o in bpy.context.collection.objects if o.type == 'CURVE']

    def root(self):
        root, = [o for o in bpy.context.collection.objects if o.type == 'EMPTY']
        return root

    def dimensions(self):
        return self.root()['bead_width'], self.root()['bead_height']

    def vertices(self, obj):
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        mesh = evaluated.to_mesh()
        try:
            return [tuple(obj.matrix_world @ v.co) for v in mesh.vertices]
        finally:
            evaluated.to_mesh_clear()

    def snapshot(self):
        return {kind: set(getattr(bpy.data, kind)) for kind in ('objects', 'curves', 'materials')}

    def test_startup_purge_cannot_change_model_profile(self):
        for purge in (';TYPE:Custom\n;WIDTH:8\n;HEIGHT:1\nG0 Z1\nG1 X200 E500\n',
                      'G0 Z.8\nG1 X200 E1000\n',
                      ';TYPE:WALL-OUTER\n;WIDTH:4\nG0 Z.8\nG1 X200 E1000\n'):
            self.setUp()
            self.paths(HEADER + purge + ';LAYER_CHANGE\n;TYPE:Perimeter\nG0 X0 Z.2\n' + f'G1 X10 E{MODEL_E}\n')
            self.assertEqual(self.dimensions(), (.45, .2))
            self.assertEqual(self.root()['bead_profile_source'], 'model extrusion')

    def test_unlabelled_startup_is_excluded_using_layer_boundary(self):
        self.paths(HEADER + ';LAYER:-1\nG0 Z.8\nG1 X200 E1000\n;LAYER:0\nG0 X0 Z.2\n' + f'G1 X10 E{MODEL_E}\n')
        self.assertEqual(self.dimensions(), (.45, .2))

    def test_no_model_markers_uses_settings_not_purge(self):
        self.paths(HEADER + ';line_width = .48\n;first_layer_extrusion_width = 5\n;WIDTH:9\nG0 Z1\nG1 X200 E1000\n')
        self.assertEqual(self.dimensions(), (.48, .2))
        self.assertEqual(self.root()['bead_profile_source'], 'slicer settings')

    def test_unmarked_file_uses_reported_fallback_instead_of_guessing(self):
        self.paths(';WIDTH:8\n;HEIGHT:1\nG0 Z1\nG1 X200 E1000\n')
        self.assertEqual(self.dimensions(), (.45, .2))
        self.assertEqual(self.root()['bead_profile_source'], 'fallback')
        self.assertIn('no identified model extrusion', self.root()['bead_profile_assumptions'])

    def test_all_tools_and_variable_paths_use_one_profile(self):
        objects = self.paths(HEADER + ';TYPE:Perimeter\n;HEIGHT:.2\n;WIDTH:.45\nG0 Z.2\nG1 X20 E1\n'
            'T1\n;WIDTH:.6\nG1 X25 E1\n;HEIGHT:.3\nG1 X30 E1\n'
            'T0\n;HEIGHT:.2\n;WIDTH:.45\nG1 X50 E1\n')
        self.assertEqual(self.dimensions(), (.45, .2))
        self.assertEqual(len(objects), 2)
        root = self.root()
        self.assertEqual(len(root.children), 2)
        self.assertEqual(set(root.children), set(objects))
        self.assertEqual({o.name.split('.')[0] for o in objects}, {'T0', 'T1'})
        self.assertTrue(all(o.data.bevel_mode == 'ROUND' for o in objects))
        self.assertEqual({round(o.data.bevel_depth, 5) for o in objects}, {.1})
        self.assertEqual({round(o.data.extrude, 5) for o in objects}, {.125})
        self.assertIsNot(objects[0].data.materials[0], objects[1].data.materials[0])
        self.assertEqual(len(objects[0].data.splines), 2)
        self.assertEqual(len(bpy.context.collection.objects), 3)
        for obj in objects:
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            mesh = evaluated.to_mesh()
            try:
                self.assertEqual(set(f.material_index for f in mesh.polygons), {0})
                self.assertIs(mesh.materials[0].original, obj.data.materials[0])
            finally:
                evaluated.to_mesh_clear()

    def test_flat_top_bottom_rounded_sides_and_editable_rounded_ends(self):
        for axis in ('X', 'Y'):
            self.setUp()
            obj, = self.paths(HEADER + ';TYPE:Perimeter\n;WIDTH:.45\nG0 Z.2\n' + f'G1 {axis}10 E1\n')
            vertices = self.vertices(obj)
            across, along = (1, 0) if axis == 'X' else (0, 1)
            self.assertAlmostEqual(max(p[across] for p in vertices), .225, places=5)
            self.assertAlmostEqual(min(p[across] for p in vertices), -.225, places=5)
            self.assertAlmostEqual(min(p[2] for p in vertices), 0, places=5)
            self.assertAlmostEqual(max(p[2] for p in vertices), .2, places=5)
            self.assertAlmostEqual(min(p[along] for p in vertices), -.225, places=5)
            self.assertAlmostEqual(max(p[along] for p in vertices), 10.225, places=5)
            spline = obj.data.splines[0]
            self.assertEqual(spline.bezier_points[0].radius, 0)
            self.assertEqual(spline.bezier_points[-1].radius, 0)
            self.assertTrue(all(a.radius < b.radius for a,b in zip(spline.bezier_points[:5], spline.bezier_points[1:6])))
            self.assertFalse(obj.data.use_fill_caps)
            self.assertTrue(all(o.type in ('EMPTY', 'CURVE') for o in bpy.context.collection.objects))
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            mesh = evaluated.to_mesh()
            try:
                top = [f for f in mesh.polygons if all(abs(mesh.vertices[i].co.z-.2)<1e-5 for i in f.vertices)]
                self.assertTrue(top)
                self.assertTrue(all(f.normal.z > .99 for f in top))
            finally:
                evaluated.to_mesh_clear()

    def test_closed_paths_are_uncapped_and_width_hints_do_not_split(self):
        obj, = self.paths(HEADER + ';TYPE:Perimeter\n;WIDTH:.45\nG0 Z.2\nG1 X10 E1\n'
            ';WIDTH:.6\nG1 Y10 E1\nG1 X0 E1\nG1 Y0 E1\n')
        self.assertEqual(len(obj.data.splines), 1)
        self.assertTrue(obj.data.splines[0].use_cyclic_u)
        self.assertEqual(len(obj.data.splines[0].bezier_points), 4)
        self.assertTrue(all(p.radius == 1 for p in obj.data.splines[0].bezier_points))

    def test_tower_bridges_skirts_and_purge_blocks_never_size_profile(self):
        text = HEADER + ';LAYER_CHANGE\nG0 Z.2\n'
        for role in ('Wipe tower', 'Prime tower', 'Skirt', 'Brim', 'Bridge infill', 'Custom'):
            text += f';TYPE:{role}\n;WIDTH:8\n;HEIGHT:1\nG1 X200 E1000\nG0 X0\n'
        text += ';TYPE:Perimeter\n;WIDTH:.45\n;HEIGHT:.2\nG1 X10 E1\n'
        text += ';PURGE_START\n;WIDTH:9\nG1 X500 E1000\n;PURGE_END\n'
        self.paths(text, exclude_wipe_tower=False)
        self.assertEqual(self.dimensions(), (.45, .2))

    def test_retraction_recovery_does_not_inflate_width(self):
        obj, = self.paths(HEADER + ';TYPE:Perimeter\nG0 Z.2\nG1 E-1\n' + f'G1 X20 E{1+2*MODEL_E}\n')
        # The same E/distance ratio applies to the deposited fraction only.
        expected_area = (1+2*MODEL_E) * math.pi*1.75**2/4 / 20
        expected_width = round(expected_area/.2 + .2*(1-math.pi/4), 2)
        self.assertEqual(self.dimensions(), (expected_width, .2))
        self.assertGreater(obj.data.splines[0].bezier_points[5].co.x, 0)

    def test_layer_inference_ignores_purge_elevation_and_travel_hops(self):
        self.paths(';filament_diameter = 1.75\nG0 Z1\nG1 X200 E100\n;TYPE:Perimeter\n;WIDTH:.45\n'
                   'G0 X0 Z.2\nG1 X1 E1\nG0 Z2\nG0 Z.45\nG1 X30 E1\n')
        self.assertEqual(self.dimensions(), (.45, .25))

    def test_dominant_model_profile_not_first_layer_profile(self):
        self.paths(HEADER + ';TYPE:Perimeter\n;WIDTH:.6\n;HEIGHT:.3\nG0 Z.3\nG1 X10 E1\n'
                   'G0 X0 Z.5\n;WIDTH:.45\n;HEIGHT:.2\nG1 X30 E1\n')
        self.assertEqual(self.dimensions(), (.45, .2))

    def test_arc_volume_uses_true_length_independent_of_sampling(self):
        e = MODEL_E * math.pi/2
        for angle in (math.pi/8, math.pi/32):
            self.setUp()
            with patch.object(importer, 'ARC_MAX_ANGLE', angle):
                self.paths(HEADER + ';TYPE:Perimeter\nG0 X10 Z.2\n' + f'G3 X0 Y10 I-10 E{e}\n')
            self.assertEqual(self.dimensions(), (.45, .2))

    def test_filament_size_volumetric_and_firmware_flow(self):
        for diameter, commands, e in ((2.85, '', MODEL_E*1.75**2/2.85**2),
                                      (1.75, 'M200 D1.75\n', MODEL_E*math.pi*1.75**2/4),
                                      (1.75, 'M221 S50\n', MODEL_E*2)):
            self.setUp()
            self.paths(f';filament_diameter = {diameter}\n;layer_height = .2\n;TYPE:Perimeter\n' + commands + f'G0 Z.2\nG1 X10 E{e}\n')
            self.assertEqual(self.dimensions(), (.45, .2))

    def test_zero_flow_travel_and_tower_only_create_no_data(self):
        for text in ('M221 S0\nG1 X10 E1\n', 'G0 X10\nT1\nG0 X20\n', ';TYPE:Wipe tower\nG1 X10 E1\n'):
            before = self.snapshot()
            self.paths(text)
            self.assertEqual(self.snapshot(), before)

    def test_failed_imports_preserve_existing_data(self):
        self.paths(HEADER + ';TYPE:Perimeter\nG1 X10 E1\n')
        before = self.snapshot()
        for suffix in ('G3 X100 R1 E1', 'G20', 'M221 Snan'):
            with self.assertRaises(ValueError):
                self.paths('G1 X10 E1\nG0 X20\n'+suffix)
            self.assertEqual(self.snapshot(), before)

    def test_failed_bead_batch_removes_already_created_tool_curves(self):
        self.paths(HEADER + ';TYPE:Perimeter\nG1 X10 E1\n')
        before = self.snapshot()
        with tempfile.TemporaryDirectory() as folder:
            good, bad = Path(folder)/'good.gcode', Path(folder)/'bad.gcode'
            good.write_text(HEADER + ';TYPE:Perimeter\nG1 X10 E1\nT1\nG1 X20 E1\n')
            bad.write_text('G1 X10 E1\nG0 X20\nG3 X100 R1 E1\n')
            with self.assertRaises(ValueError):
                importer.import_gcodes([str(good), str(bad)])
        self.assertEqual(self.snapshot(), before)

    def test_default_operator_binary_batch_preserves_tool_hierarchy(self):
        importer.register()
        self.addCleanup(importer.unregister)
        text = HEADER + ';TYPE:Perimeter\nG0 Z.2\n' + f'G1 X10 E{MODEL_E}\nT1\nG1 Y10 E{MODEL_E}\n'
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'one.gcode').write_text(text)
            (Path(folder)/'two.bgcode').write_bytes(binary(text, 3, 2))
            result = bpy.ops.import_gcode.operator(directory=folder, files=[{'name':'one.gcode'}, {'name':'two.bgcode'}])
        self.assertEqual(result, {'FINISHED'})
        roots = sorted((o for o in bpy.context.collection.objects if o.type == 'EMPTY'), key=lambda o:o.name)
        self.assertEqual(len(bpy.context.collection.objects), 6)
        self.assertEqual(len(roots), 2)
        for root in roots:
            self.assertEqual(len(root.children), 2)
            self.assertTrue(all(o.type == 'CURVE' for o in root.children))
            self.assertEqual(root['bead_width'], .45)
        first = roots[0].children[0]
        before = self.vertices(first)
        roots[0].scale = (2, 2, 2)
        roots[0].location.x = 5
        bpy.context.view_layer.update()
        after = self.vertices(first)
        self.assertAlmostEqual(after[0][0], 2*before[0][0]+5, places=5)
        self.assertAlmostEqual(after[0][2], 2*before[0][2], places=5)
        self.assertEqual(roots[1].scale.x, 1)

    def test_bgcode_metadata_can_supply_profile_without_sampling_purge(self):
        text = 'G0 Z1\nG1 X200 E1000\n'
        metadata = b'filament_diameter = 1.75\nlayer_height = .2\nline_width = .48\n'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'print.bgcode'
            for compression in range(4):
                self.setUp()
                path.write_bytes(binary(text) + block(metadata, compression=compression, block_type=2))
                self.assertEqual(importer.import_gcode(str(path)), [])
                self.assertEqual(self.dimensions(), (.48, .2))


if __name__ == '__main__':
    unittest.main()
