"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""

import importlib.util
from pathlib import Path
import unittest

import bpy


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "material_test_importer", ROOT / "simple_gcode_importer" / "__init__.py"
)
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


class MaterialImportTests(unittest.TestCase):
    def paths(self, text):
        before = set(bpy.data.objects)
        importer.create_paths(text.splitlines(keepends=True))
        return [obj for obj in bpy.context.collection.objects if obj not in before]

    def assert_points(self, obj, expected):
        actual = obj.data.splines[0].bezier_points
        self.assertEqual(len(actual), len(expected))
        for point, coords in zip(actual, expected):
            for value, target in zip(point.co, coords):
                self.assertAlmostEqual(value, target, places=5)

    def test_fixture_splits_materials_and_preserves_boundary(self):
        objects = self.paths((ROOT / "pr6-materials.gcode").read_text())
        self.assertEqual(len(objects), 2)
        left, right = objects
        self.assertTrue(left.data.materials[0].name.startswith("T0"))
        self.assertTrue(right.data.materials[0].name.startswith("T1"))
        self.assertIsNot(left.data.materials[0], right.data.materials[0])
        self.assert_points(left, [(0, 0, .2), (20, 0, .2), (20, 20, .2)])
        self.assert_points(right, [(20, 20, .2), (40, 20, .2), (40, 0, .2)])
        for obj in objects:
            self.assertAlmostEqual(obj.data.bevel_depth, .2)
            self.assertTrue(obj.data.use_fill_caps)

    def test_no_tool_command_defaults_to_t0_and_flushes_at_eof(self):
        objects = self.paths("G1 X0 E1\nG1 X10 E1\n")
        self.assertEqual(len(objects), 1)
        self.assertTrue(objects[0].data.materials[0].name.startswith("T0"))
        self.assert_points(objects[0], [(0, 0, 0), (10, 0, 0)])

    def test_repeated_selection_does_not_split_the_path(self):
        objects = self.paths("T0\nG1 X0 E1\nG1 X10 E1\nT00\nG1 X20 E1\n")
        self.assertEqual(len(objects), 1)
        self.assert_points(objects[0], [(0, 0, 0), (10, 0, 0), (20, 0, 0)])

    def test_travel_only_file_does_not_create_geometry(self):
        for move in ("G0", "G1"):
            with self.subTest(move=move):
                objects = self.paths(f"G92 E0\n{move} X10 Y10\n{move} X100 Y100\n")
                self.assertEqual(objects, [])

    def test_parking_moves_do_not_extend_the_last_printed_path(self):
        objects = self.paths(
            "G0 X10 Y10\nG1 X20 Y10 E1\nG92 E0\n"
            "G0 X100 Y100\nG0 X110 Y100\n"
        )
        self.assertEqual(len(objects), 1)
        self.assert_points(objects[0], [(10, 10, 0), (20, 10, 0)])

    def test_travel_gap_starts_next_path_at_the_new_position(self):
        objects = self.paths(
            "G0 X10\nG1 X20 E1\nG0 X100\nG1 X110 E1\n"
        )
        self.assertEqual(len(objects), 2)
        self.assert_points(objects[0], [(10, 0, 0), (20, 0, 0)])
        self.assert_points(objects[1], [(100, 0, 0), (110, 0, 0)])

    def test_stationary_priming_and_unretraction_do_not_create_geometry(self):
        objects = self.paths(
            "G1 X10 E1\nG1 E-1\nG0 X20\nG1 E1\nG0 X30\nG0 X40\n"
        )
        self.assertEqual(len(objects), 1)
        self.assert_points(objects[0], [(0, 0, 0), (10, 0, 0)])

    def test_travel_only_tool_switch_does_not_create_geometry(self):
        objects = self.paths("G0 X10\nG0 X20\nT1\nG0 X30\nG0 X40\n")
        self.assertEqual(objects, [])

    def test_returning_to_a_tool_reuses_material_and_extrusion_position(self):
        objects = self.paths(
            "M82\nG1 X0 E0\nG1 X10 E10\n"
            "T1\nG1 X20 E1\nT0\nG1 X30 E11\n"
        )
        self.assertEqual(len(objects), 3)
        self.assertIs(objects[0].data.materials[0], objects[2].data.materials[0])
        self.assertIsNot(objects[0].data.materials[0], objects[1].data.materials[0])
        self.assert_points(objects[1], [(10, 0, 0), (20, 0, 0)])
        self.assert_points(objects[2], [(20, 0, 0), (30, 0, 0)])

    def test_paths_separated_by_retraction_share_material(self):
        objects = self.paths(
            "G1 X0 E1\nG1 X10 E1\nG1 E-1\nG92 E0\n"
            "G1 X20 E1\nG1 X30 E1\nG1 E-1\n"
        )
        self.assertEqual(len(objects), 2)
        self.assertIs(objects[0].data.materials[0], objects[1].data.materials[0])

    def test_registration_and_operator_import(self):
        before = set(bpy.data.objects)
        importer.register()
        try:
            result = bpy.ops.import_gcode.operator(filepath=str(ROOT / "pr6-materials.gcode"))
            self.assertEqual(result, {'FINISHED'})
            self.assertEqual(len(set(bpy.data.objects) - before), 2)
        finally:
            importer.unregister()


suite = unittest.defaultTestLoader.loadTestsFromTestCase(MaterialImportTests)
result = unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful():
    raise RuntimeError("Blender material regression checks failed")
