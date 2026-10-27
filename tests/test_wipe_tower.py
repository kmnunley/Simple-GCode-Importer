"""Wipe tower filtering checks inside Blender."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
import bpy
from bgcode_fixtures import binary

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('wipe_test_importer', ROOT / 'simple_gcode_importer' / '__init__.py')
importer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = importer
spec.loader.exec_module(importer)


class WipeTowerTests(unittest.TestCase):
    def setUp(self):
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)

    def paths(self, text, **options):
        importer.create_paths(text.splitlines(True), **options)
        return [obj for obj in bpy.context.collection.objects if obj.type == 'CURVE']

    def coordinates(self, obj):
        return [[tuple(p.co) for p in spline.bezier_points] for spline in obj.data.splines]

    def test_feature_markers_split_model_paths(self):
        for prefix, role in ((';TYPE:', 'Wipe tower'), ('; FEATURE: ', 'Prime tower'), (';TYPE:', 'Prime tower')):
            with self.subTest(prefix=prefix, role=role):
                self.setUp()
                obj, = self.paths('M82\nG1 X10 E1\n' + prefix + role + '\nG1 X100 E20\n' + prefix + 'Perimeter\nG1 X110 E21\n')
                self.assertEqual(self.coordinates(obj), [[(0, 0, 0), (10, 0, 0)], [(100, 0, 0), (110, 0, 0)]])

    def test_disabled_keeps_original_continuity(self):
        obj, = self.paths('G1 X10 E1\n;TYPE:Wipe tower\nG1 X100 E1\n;TYPE:Perimeter\nG1 X110 E1\n', exclude_wipe_tower=False)
        self.assertEqual(self.coordinates(obj), [[(0, 0, 0), (10, 0, 0), (100, 0, 0), (110, 0, 0)]])

    def test_explicit_orca_block_ends_without_next_feature(self):
        obj, = self.paths('G1 X10 E1\n; WIPE_TOWER_START\n; FEATURE: Prime tower\nG1 X100 E1\n; FEATURE: Custom\nG1 X120 E1\n; WIPE_TOWER_END\nG1 X130 E1\n')
        self.assertEqual(self.coordinates(obj), [[(0, 0, 0), (10, 0, 0)], [(120, 0, 0), (130, 0, 0)]])

    def test_tower_only_creates_no_data(self):
        before = self.snapshot()
        self.paths(';TYPE:Wipe tower\nG1 X10 E1\nT1\nG1 X20 E1\n')
        self.assertEqual(self.snapshot(), before)

    def snapshot(self):
        return {kind: set(getattr(bpy.data, kind)) for kind in ('objects', 'curves', 'materials')}

    def test_tool_changes_and_g92_preserve_extrusion_state(self):
        objects = self.paths('M82\nG1 X10 E10\n;TYPE:Wipe tower\nG1 X100 E20\nT1\nG92 E0\nG1 X110 E5\n;TYPE:Perimeter\nG1 X120 E6\nT0\nG1 X130 E21\n')
        by_tool = {obj.data.materials[0].name.split('.')[0]: obj for obj in objects}
        self.assertEqual(self.coordinates(by_tool['T0']), [[(0, 0, 0), (10, 0, 0)], [(120, 0, 0), (130, 0, 0)]])
        self.assertEqual(self.coordinates(by_tool['T1']), [[(110, 0, 0), (120, 0, 0)]])

    def test_retraction_preserves_high_water_mark(self):
        obj, = self.paths('M82\n;TYPE:Wipe tower\nG1 X10 E10\nG1 E8\n;TYPE:Perimeter\nG1 X40 E11\n')
        self.assertEqual(self.coordinates(obj), [[(30, 0, 0), (40, 0, 0)]])

    def test_arcs_and_relative_xyz_advance_next_start(self):
        obj, = self.paths('G0 X10\n;TYPE:Wipe tower\nG3 X0 Y10 I-10 E1\nG91\nG1 X5 E1\n;TYPE:Perimeter\nG1 X10 E1\n')
        self.assertEqual(self.coordinates(obj), [[(5, 10, 0), (15, 10, 0)]])

    def test_ordinary_wipes_and_mentions_do_not_exclude(self):
        obj, = self.paths('; wipe_tower_x = 100\n; a wipe tower is enabled\n; WIPE_START\nG1 X10 E1\n; WIPE_END\nG1 X20 E1\n')
        self.assertEqual(self.coordinates(obj), [[(0, 0, 0), (10, 0, 0), (20, 0, 0)]])

    def test_invalid_excluded_arc_still_rolls_back(self):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Line 3:'):
            self.paths('G1 X10 E1\n;TYPE:Wipe tower\nG3 X100 R1 E1\n')
        self.assertEqual(self.snapshot(), before)

    def test_operator_default_and_text_binary_batch_options(self):
        importer.register()
        self.addCleanup(importer.unregister)
        prop = bpy.ops.import_gcode.operator.get_rna_type().properties['exclude_wipe_tower']
        self.assertTrue(prop.default)
        self.assertIn('PrusaSlicer and OrcaSlicer only', prop.name)
        text = ';TYPE:Wipe tower\nG1 X10 E1\n;TYPE:Perimeter\nG1 X20 E1\n'
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'one.gcode').write_text(text)
            (Path(folder) / 'two.bgcode').write_bytes(binary(text, 3, 2))
            for excluded, expected in ((True, [(10, 0, 0), (20, 0, 0)]), (False, [(0, 0, 0), (10, 0, 0), (20, 0, 0)])):
                with self.subTest(excluded=excluded):
                    self.setUp()
                    result = bpy.ops.import_gcode.operator(directory=folder, files=[{'name': 'one.gcode'}, {'name': 'two.bgcode'}], exclude_wipe_tower=excluded)
                    self.assertEqual(result, {'FINISHED'})
                    objects = [obj for obj in bpy.context.collection.objects if obj.type == 'CURVE']
                    self.assertEqual(len(objects), 2)
                    for obj in objects:
                        self.assertEqual(self.coordinates(obj), [expected])


if __name__ == '__main__':
    unittest.main()
