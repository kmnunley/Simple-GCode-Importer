"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""

import importlib.util
import sys
import math
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import bpy


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "arc_test_importer", ROOT / "simple_gcode_importer" / "__init__.py"
)
importer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = importer
spec.loader.exec_module(importer)


class ArcImportTests(unittest.TestCase):
    def setUp(self):
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)

    def paths(self, text):
        importer.create_paths(text.splitlines(keepends=True))
        return list(bpy.context.collection.objects)

    def points(self, obj):
        return [tuple(p.co) for p in obj.data.splines[0].bezier_points]

    def assert_point(self, point, expected):
        for actual, target in zip(point, expected):
            self.assertAlmostEqual(actual, target, places=5)

    def assert_sweep(self, points, center, expected):
        angles = [math.atan2(p[1] - center[1], p[0] - center[0]) for p in points]
        sweep = sum((b - a + math.pi) % math.tau - math.pi
                    for a, b in zip(angles, angles[1:]))
        self.assertAlmostEqual(sweep, expected, places=5)
        for p in points:
            self.assertAlmostEqual(math.hypot(p[0] - center[0], p[1] - center[1]),
                                   10, places=5)

    def test_ij_quarters_in_both_directions_flush_at_eof(self):
        for command, end, sweep in (("G3", (0, 10, 0), math.pi / 2),
                                    ("G2", (0, -10, 0), -math.pi / 2)):
            with self.subTest(command=command):
                self.setUp()
                objects = self.paths(f"G0 X10\n{command} X0 Y{end[1]} I-10 E1\n")
                self.assertEqual(len(objects), 1)
                points = self.points(objects[0])
                self.assert_point(points[0], (10, 0, 0))
                self.assert_point(points[-1], end)
                self.assert_sweep(points, (0, 0), sweep)

    def test_signed_radius_selects_short_or_long_sweep(self):
        cases = (("G3", 10, (0, 0), math.pi / 2),
                 ("G2", 10, (10, 10), -math.pi / 2),
                 ("G3", -10, (10, 10), 3 * math.pi / 2),
                 ("G2", -10, (0, 0), -3 * math.pi / 2))
        for command, radius, center, sweep in cases:
            with self.subTest(command=command, radius=radius):
                self.setUp()
                objects = self.paths(f"G0 X10\n{command} X0 Y10 R{radius} E1\n")
                self.assertEqual(len(objects), 1)
                self.assert_sweep(self.points(objects[0]), center, sweep)

    def test_radius_semicircles(self):
        for command, sweep in (("G2", -math.pi), ("G3", math.pi)):
            with self.subTest(command=command):
                self.setUp()
                obj, = self.paths(f"G0 X10\n{command} X-10 R10 E1\n")
                self.assert_sweep(self.points(obj), (0, 0), sweep)

    def test_full_circles_in_both_directions(self):
        for command, sweep in (("G2", -math.tau), ("G3", math.tau)):
            with self.subTest(command=command):
                self.setUp()
                obj, = self.paths(f"G0 X10\n{command} I-10 E1\n")
                points = self.points(obj)
                self.assert_point(points[0], points[-1])
                self.assertGreaterEqual(len(points), 33)
                self.assert_sweep(points, (0, 0), sweep)

    def test_helix_z_progresses_linearly(self):
        obj, = self.paths("G0 X10 Z2\nG3 X0 Y10 Z10 I-10 E1\n")
        points = self.points(obj)
        for index, p in enumerate(points):
            self.assertAlmostEqual(p[2], 2 + 8 * index / (len(points) - 1), places=5)
        self.assert_point(points[-1], (0, 10, 10))

    def test_relative_xyz_and_both_extrusion_modes(self):
        for mode in ("M82", "M83"):
            with self.subTest(mode=mode):
                self.setUp()
                end_e = 6 if mode == "M82" else 1
                obj, = self.paths(f"G0 X10\nG92 E5\n{mode}\nG91\n"
                                  f"G3 X-10 Y10 Z2 I-10 E{end_e}\n")
                points = self.points(obj)
                self.assert_point(points[0], (10, 0, 0))
                self.assert_point(points[-1], (0, 10, 2))
                self.assert_sweep(points, (0, 0), math.pi / 2)

    def test_nonextruding_and_retracting_arcs_create_no_geometry(self):
        for extrusion in ("", " E0", " E-1"):
            with self.subTest(extrusion=extrusion):
                self.setUp()
                self.assertEqual(self.paths(f"G0 X10\nG3 X0 Y10 I-10{extrusion}\n"), [])

    def test_travel_arc_splits_paths_and_updates_next_start(self):
        left, right = self.paths("G1 X10 E1\nG3 X0 Y10 I-10\nG1 X-10 E1\n")
        self.assertEqual(len(self.points(left)), 2)
        self.assert_point(self.points(left)[-1], (10, 0, 0))
        self.assert_point(self.points(right)[0], (0, 10, 0))
        self.assertIs(left.data.materials[0], right.data.materials[0])

    def test_recovery_draws_only_after_previous_extrusion_is_restored(self):
        obj, = self.paths("G0 X10\nG92 E1\nM82\nG1 E0\nG3 X0 Y10 I-10 E2\n")
        points = self.points(obj)
        self.assert_point(points[0], (math.sqrt(50), math.sqrt(50), 0))
        self.assert_point(points[-1], (0, 10, 0))
        self.assert_sweep(points, (0, 0), math.pi / 4)

    def test_arcs_retain_tool_materials_bevel_and_extrusion_positions(self):
        objects = self.paths("M82\nG0 X10\nG3 X0 Y10 I-10 E10\n"
                             "T1\nG3 X-10 Y0 J-10 E1\n"
                             "T0\nG3 X0 Y-10 I10 E11\n")
        self.assertEqual(len(objects), 3)
        self.assertIs(objects[0].data.materials[0], objects[2].data.materials[0])
        self.assertIsNot(objects[0].data.materials[0], objects[1].data.materials[0])
        for index, obj in enumerate(objects):
            self.assertEqual(len(self.points(obj)), 9)
            self.assertAlmostEqual(obj.data.bevel_depth, .2)
            self.assertTrue(obj.data.use_fill_caps)
        self.assert_point(self.points(objects[1])[0], (0, 10, 0))

    def test_blank_lines_and_inline_comments(self):
        obj, = self.paths("\n; comment\nG0 X10\nG17\nG91.1\n"
                         "G3 X0 Y10 I-10 E1 ; X999 E100\n")
        self.assert_point(self.points(obj)[-1], (0, 10, 0))

    def test_visual_fixture_imports_through_registered_operator(self):
        importer.register()
        try:
            result = bpy.ops.import_gcode.operator(
                filepath=str(ROOT / "tests" / "fixtures" / "pr14-arcs.gcode"))
            self.assertEqual(result, {'FINISHED'})
            objects = list(bpy.context.collection.objects)
            self.assertEqual(len(objects), 5)
            # Rounded center calculations may require one extra segment to
            # keep the maximum angular step, especially at a quadrant boundary.
            for obj, minimum in zip(objects, (9, 9, 25, 33, 33)):
                self.assertIn(len(self.points(obj)), (minimum, minimum + 1))
            self.assertIs(objects[0].data.materials[0], objects[2].data.materials[0])
            self.assertIs(objects[3].data.materials[0], objects[4].data.materials[0])
            self.assert_point(self.points(objects[-1])[-1], (40, 30, 10.2))
        finally:
            importer.unregister()

    def test_invalid_arcs_fail_with_line_context(self):
        for arc in ("G3 X0 Y10 R1 E1", "G3 R10 E1", "G3 X0 Y10 R0 E1",
                    "G3 X0 Y10 E1", "G3 X0 Y10 I0 J0 E1",
                    "G3 X0 Y10 I-2 E1", "G3 X0 Y10 R10 I-10 E1",
                    "G3 X0 Y10 I-10 K0 E1"):
            with self.subTest(arc=arc):
                with self.assertRaisesRegex(ValueError, "Line 2:"):
                    self.paths(f"G0 X10\n{arc}\n")

    def test_failed_imports_remove_only_their_own_scene_data(self):
        # Existing data may have the same names as data made by the importer.
        curve = bpy.data.curves.new("Path", type='CURVE')
        material = bpy.data.materials.new("T0")
        curve.materials.append(material)
        existing = bpy.data.objects.new("Path", curve)
        bpy.context.collection.objects.link(existing)
        before = {name: set(getattr(bpy.data, name))
                  for name in ("objects", "curves", "materials")}
        prefix = "G1 X10 E1\nT1\nG1 X20 E1\nG0 X30\n"
        for invalid in ("G3 X40 Y10 R1 E1", "G18", "G1 Xinvalid E1"):
            # Retrying the same failed import must not accumulate scene data.
            for attempt in range(2):
                with self.subTest(invalid=invalid, attempt=attempt):
                    with self.assertRaises(ValueError):
                        importer.create_paths((prefix + invalid + "\n").splitlines(True))
                    for name, original in before.items():
                        self.assertEqual(set(getattr(bpy.data, name)), original)
                    self.assertIn(existing, list(bpy.context.collection.objects))
                    self.assertIs(existing.data, curve)
                    self.assertIs(curve.materials[0], material)

    def test_operator_failure_leaves_scene_data_unchanged(self):
        before = {name: set(getattr(bpy.data, name))
                  for name in ("objects", "curves", "materials")}
        importer.register()
        try:
            text = "G1 X10 E1\nG0 X20\nG3 X30 Y10 R1 E1\n"
            with patch("builtins.open", return_value=io.BytesIO(text.encode("utf-8"))):
                # Blender exposes ERROR reports from CANCELLED operators as RuntimeError.
                with self.assertRaisesRegex(RuntimeError, "Line 3:"):
                    bpy.ops.import_gcode.operator(filepath="failed-import.gcode")
            for name, original in before.items():
                self.assertEqual(set(getattr(bpy.data, name)), original)
        finally:
            importer.unregister()

    def test_unsupported_plane_and_center_modes_fail_explicitly(self):
        for command in ("G18", "G19", "G90.1"):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    self.paths(command + "\n")


suite = unittest.defaultTestLoader.loadTestsFromTestCase(ArcImportTests)
result = unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful():
    raise RuntimeError("Blender arc regression checks failed")
