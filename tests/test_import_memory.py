"""Bounded checks for compact storage, early release and unchanged geometry."""
from array import array
import gc
import importlib.util
from pathlib import Path
import sys
import unittest
import weakref
from unittest.mock import patch

import bpy

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('memory_test_importer', ROOT/'simple_gcode_importer/__init__.py')
importer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = importer
spec.loader.exec_module(importer)


class ImportMemoryTests(unittest.TestCase):
    def setUp(self):
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)

    def raw_geometry(self, created):
        return [[(s.use_cyclic_u, tuple((tuple(p.co), tuple(p.handle_left), tuple(p.handle_right),
                  p.radius, p.tilt) for p in s.bezier_points)) for s in curve.splines]
                for curve in created['curves']]

    def test_packed_and_tuple_paths_produce_identical_geometry(self):
        paths = [[(1.23456789,2.34567891,.234567891), (3.45678912,4.56789123,.345678912)],
                 [(0,0,.2),(1,0,.2),(1,1,.2),(0,0,.2)],
                 [(10,0,.2)] + importer.arc_points((10,0,.2), (0,10,.4), {'I':-10}, False)]
        expected = None
        for packed in (False, True):
            supplied = {'T0': [array('d', (v for p in path for v in p)) if packed else path[:]
                               for path in paths]}
            created = {kind: [] for kind in ('objects', 'curves', 'materials')}
            profile = importer.ProfileEstimator([]).result()
            importer.build_bead_paths(supplied, created, 'test', profile)
            actual = self.raw_geometry(created)
            if expected is None:
                expected = actual
            else:
                self.assertEqual(actual, expected)
            self.assertEqual(len(supplied['T0']), 3)  # Public builder retains caller-owned paths.

    def test_parser_releases_decoded_lines_before_building_and_keeps_precision(self):
        class SourceLines(list):
            pass
        def load_lines(*args, **kwargs):
            nonlocal reference
            lines = SourceLines(['G0 X1.234567891 Z.2', 'G1 X2.345678912 E1'])
            reference = weakref.ref(lines)
            return lines

        reference = None
        checked = []
        def inspect_paths(paths, created, name, profile, **options):
            gc.collect()
            self.assertIsNone(reference(), 'Decoded source retained during construction')
            path, = paths['T0']
            self.assertIsInstance(path, array)
            self.assertEqual(path.typecode, 'd')
            self.assertEqual(tuple(path), (1.234567891,0,.2,2.345678912,0,.2))
            self.assertTrue(options['consume_paths'])
            checked.append(True)
        with (patch.object(importer, 'load', load_lines),
              patch.object(importer, 'build_bead_paths', inspect_paths)):
            importer.import_gcode('sample.gcode')
        self.assertEqual(checked, [True])

    def test_consumed_path_is_released_before_building_the_next_spline(self):
        first = array('d', (0, 0, .2, 1, 0, .2))
        reference = weakref.ref(first)

        def next_path():
            self.assertIsNone(reference())
            yield from [(0, 1, .2), (1, 1, .2)]

        paths = {'T0': [first, next_path()]}
        del first
        created = {kind: [] for kind in ('objects', 'curves', 'materials')}
        profile = importer.ProfileEstimator([]).result()
        importer.build_bead_paths(paths, created, 'test', profile, consume_paths=True)
        self.assertEqual(paths['T0'], [])
        self.assertEqual(len(created['curves'][0].splines), 2)

    def test_caller_source_list_is_not_mutated(self):
        lines = ['M83', 'G1 X1 E1', 'G0 X2', 'G1 X3 E1']
        expected = lines[:]
        importer.create_paths(lines)
        self.assertEqual(lines, expected)


if __name__ == '__main__':
    unittest.main()
