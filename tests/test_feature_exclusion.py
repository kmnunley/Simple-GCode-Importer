"""Early infill/support exclusion without allocating discarded geometry."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import bpy
from bgcode_fixtures import binary

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('feature_test_importer', ROOT/'simple_gcode_importer/__init__.py')
importer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = importer
spec.loader.exec_module(importer)


class FeatureExclusionTests(unittest.TestCase):
    def setUp(self):
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)

    def paths(self, text, **options):
        importer.create_paths(text.splitlines(True), profile='ROUND', **options)
        return [obj for obj in bpy.context.collection.objects if obj.type == 'CURVE']

    def coordinates(self, obj):
        return [[tuple(p.co) for p in s.bezier_points] for s in obj.data.splines]

    def snapshot(self):
        return {kind: set(getattr(bpy.data, kind)) for kind in ('objects', 'curves', 'materials')}

    def test_supported_role_names_split_paths_and_keep_surfaces(self):
        for option, roles in (('exclude_infill', importer.INFILL_ROLES),
                              ('exclude_supports', importer.SUPPORT_ROLES)):
            for role in roles:
                for prefix in ('TYPE:', 'FEATURE:'):
                    with self.subTest(option=option, role=role, prefix=prefix):
                        self.setUp()
                        obj, = self.paths('M83\nG1 X10 E1\n;' + prefix + role.upper() +
                            '\nG1 X20 E1\n;TYPE:Perimeter\nG1 X30 E1\n', **{option: True})
                        self.assertEqual(self.coordinates(obj),
                            [[(0, 0, 0), (10, 0, 0)], [(20, 0, 0), (30, 0, 0)]])
        for role in ('Top solid infill', 'Bottom surface', 'Top surface', 'SKIN',
                     'Bridge infill', 'Outer wall', 'unknown feature'):
            self.setUp()
            obj, = self.paths(f';TYPE:{role}\nG1 X10 E1\n',
                              exclude_infill=True, exclude_supports=True)
            self.assertEqual(self.coordinates(obj), [[(0, 0, 0), (10, 0, 0)]])

    def test_options_are_independent_and_default_off(self):
        text = 'M83\n;TYPE:Sparse infill\nG1 X10 E1\n;TYPE:Support\nG1 X20 E1\n'
        for options, expected in (({}, [[(0, 0, 0), (10, 0, 0), (20, 0, 0)]]),
                ({'exclude_infill': True}, [[(10, 0, 0), (20, 0, 0)]]),
                ({'exclude_supports': True}, [[(0, 0, 0), (10, 0, 0)]])):
            self.setUp()
            obj, = self.paths(text, **options)
            self.assertEqual(self.coordinates(obj), expected)

    def test_excluded_arcs_are_not_sampled_and_no_data_is_created(self):
        for profile in ('BEAD', 'ROUND'):
            for role, option in (('Internal infill', 'exclude_infill'),
                                 ('Support interface', 'exclude_supports')):
                before = self.snapshot()
                with patch.object(importer, 'arc_points', side_effect=AssertionError('Sampled excluded arc')):
                    importer.create_paths(['G0 X10', f';TYPE:{role}', 'G3 X0 Y10 I-10 E1'],
                                          profile=profile, **{option: True})
                self.assertEqual(self.snapshot(), before)

    def test_excluded_moves_preserve_modes_tools_g92_and_retraction(self):
        obj, = self.paths('M82\nG1 X10 E10\n;TYPE:Internal infill\nG1 X100 E20\n'
            'G1 E18\nG91\nG1 X10 E19\n;TYPE:Perimeter\nG1 X30 E22\n'
            ';TYPE:Support\nT1\nG92 E0\nG1 X10 E5\nT0\nG1 X10 E23\n'
            ';TYPE:Perimeter\nG1 X10 E24\n', exclude_infill=True, exclude_supports=True)
        self.assertEqual(self.coordinates(obj), [[(0, 0, 0), (10, 0, 0)],
            [(120, 0, 0), (140, 0, 0)], [(160, 0, 0), (170, 0, 0)]])

    def test_excluded_arcs_advance_endpoint_without_becoming_a_bridge(self):
        with patch.object(importer, 'arc_points', side_effect=AssertionError('Sampled excluded arc')):
            obj, = self.paths('G0 X10\n;TYPE:Support\nG3 X0 Y10 I-10 E1\n'
                ';TYPE:Perimeter\nG1 X10 E1\n', exclude_supports=True)
        self.assertEqual(self.coordinates(obj), [[(0, 10, 0), (10, 10, 0)]])

    def test_excluded_sections_do_not_enter_bead_paths_or_profile_estimates(self):
        original = importer.build_bead_paths
        captured = {}
        def capture(paths, created, name, profile, **options):
            captured.update(paths={tool: [[tuple(path[i:i+3]) for i in range(0, len(path), 3)]
                                          for path in tool_paths]
                                   for tool, tool_paths in paths.items()}, profile=profile)
            original(paths, created, name, profile, **options)
        with patch.object(importer, 'build_bead_paths', capture):
            importer.create_paths(['M83', ';LAYER:0', ';TYPE:Perimeter', ';WIDTH:.45',
                ';HEIGHT:.2', 'G0 Z.2', 'G1 X10 E1', ';TYPE:Sparse infill', ';WIDTH:8',
                'G1 X100 E100', ';TYPE:Support', 'G1 X200 E100'],
                exclude_infill=True, exclude_supports=True)
        self.assertEqual(captured['paths'], {'T0': [[(0, 0, .2), (10, 0, .2)]]})
        self.assertEqual(captured['profile'].width, .45)

    def test_operator_text_binary_batch_and_tower_combination(self):
        importer.register()
        self.addCleanup(importer.unregister)
        props = bpy.ops.import_gcode.operator.get_rna_type().properties
        self.assertFalse(props['exclude_infill'].default)
        self.assertFalse(props['exclude_supports'].default)
        text = ('M83\n;TYPE:Sparse infill\nG1 X10 E1\n;FEATURE:Support interface\nG1 X20 E1\n'
                ';WIPE_TOWER_START\n;TYPE:Perimeter\nG1 X30 E1\n;WIPE_TOWER_END\nG1 X40 E1\n')
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'one.gcode').write_text(text)
            (Path(folder)/'two.bgcode').write_bytes(binary(text, 3, 2))
            self.assertEqual(bpy.ops.import_gcode.operator(directory=folder, profile='ROUND',
                files=[{'name': 'one.gcode'}, {'name': 'two.bgcode'}],
                exclude_infill=True, exclude_supports=True), {'FINISHED'})
        objects = [o for o in bpy.context.collection.objects if o.type == 'CURVE']
        self.assertEqual(len(objects), 2)
        for obj in objects:
            self.assertEqual(self.coordinates(obj), [[(30, 0, 0), (40, 0, 0)]])


if __name__ == '__main__':
    unittest.main()
