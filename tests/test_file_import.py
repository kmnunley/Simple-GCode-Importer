"""Run with Blender --background --factory-startup --python-exit-code 1 --python this_file."""

import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bgcode_fixtures import binary

spec = importlib.util.spec_from_file_location(
    'file_test_importer', ROOT / 'simple_gcode_importer' / '__init__.py')
importer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = importer
spec.loader.exec_module(importer)

TEXT = 'M83\nG0 X10\nG3 X0 Y10 I-10 E1\nT1\nG1 X-10 E1\n'


class FileImportTests(unittest.TestCase):
    def setUp(self):
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        importer.register()
        self.addCleanup(importer.unregister)

    def write(self, name, data):
        path = self.folder / name
        path.write_bytes(data)
        return str(path)

    def snapshot(self):
        return {kind: set(getattr(bpy.data, kind))
                for kind in ('objects', 'curves', 'materials')}

    def geometry(self):
        return sorted((tuple(tuple(round(v, 5) for v in p.co)
                             for p in obj.data.splines[0].bezier_points),
                       round(obj.data.bevel_depth, 5), obj.data.use_fill_caps)
                      for obj in bpy.context.collection.objects)

    def test_binary_import_matches_text_arcs_materials_and_bevel(self):
        text = self.write('sample.gcode', TEXT.encode())
        self.assertEqual(bpy.ops.import_gcode.operator(filepath=text), {'FINISHED'})
        expected = self.geometry()
        self.assertEqual(len(expected), 2)
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        for compression in range(4):
            for encoding in range(3):
                with self.subTest(compression=compression, encoding=encoding):
                    path = self.write('sample.bgcode', binary(TEXT, compression, encoding))
                    self.assertEqual(bpy.ops.import_gcode.operator(filepath=path), {'FINISHED'})
                    self.assertEqual(self.geometry(), expected)
                    objects = list(bpy.context.collection.objects)
                    self.assertIsNot(objects[0].data.materials[0], objects[1].data.materials[0])
                    for obj in objects:
                        bpy.data.objects.remove(obj, do_unlink=True)

    def test_mixed_multi_file_import(self):
        self.write('one.gcode', TEXT.encode())
        self.write('two.bgcode', binary(TEXT, 3, 2))
        result = bpy.ops.import_gcode.operator(
            directory=str(self.folder), files=[{'name': 'one.gcode'}, {'name': 'two.bgcode'}])
        self.assertEqual(result, {'FINISHED'})
        self.assertEqual(len(bpy.context.collection.objects), 4)

    def test_batch_failure_rolls_back_successful_files_and_partial_geometry(self):
        importer.create_paths(['G1 X5 E1'])
        before = self.snapshot()
        self.write('one.gcode', TEXT.encode())
        self.write('bad.bgcode', binary('G1 X5 E1\nG0 X10\nG3 X100 R1 E1\n'))
        for attempt in range(2):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(RuntimeError, 'bad.bgcode: Line 3:'):
                bpy.ops.import_gcode.operator(directory=str(self.folder), files=[
                    {'name': 'one.gcode'}, {'name': 'bad.bgcode'}])
            self.assertEqual(self.snapshot(), before)

    def test_loading_errors_report_filename_and_preserve_existing_scene(self):
        importer.create_paths(['G1 X5 E1'])
        before = self.snapshot()
        for name, data in (('empty.gcode', b''), ('corrupt.bgcode', b'GCDE'),
                           ('binary.gcode', b'UNKNOWN\x00'), ('text.gcode', b'\xff\xfe')):
            path = self.write(name, data)
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, name):
                bpy.ops.import_gcode.operator(filepath=path)
            self.assertEqual(self.snapshot(), before)
        with self.assertRaisesRegex(RuntimeError, 'missing.gcode'):
            bpy.ops.import_gcode.operator(filepath=str(self.folder / 'missing.gcode'))
        self.assertEqual(self.snapshot(), before)

    def test_file_handler_registration_extensions_and_drop_context(self):
        self.assertTrue(hasattr(bpy.types, 'GCODE_FH_import'))
        handler = importer.GCODE_FH_import
        self.assertEqual(handler.bl_import_operator, 'import_gcode.operator')
        self.assertEqual(handler.bl_file_extensions, '.gcode;.bgcode')
        for area_type, expected in (('VIEW_3D', True), ('OUTLINER', True),
                                    ('TEXT_EDITOR', False), (None, False)):
            context = types.SimpleNamespace(area=(types.SimpleNamespace(type=area_type,
                spaces=types.SimpleNamespace(active=types.SimpleNamespace(display_mode='VIEW_LAYER')))
                                                 if area_type else None))
            with self.subTest(area=area_type):
                self.assertEqual(handler.poll_drop(context), expected)

    def test_drop_invocation_uses_import_confirmation(self):
        # Background Blender skips invoke callbacks, so exercise the real
        # ImportHelper popup with a window manager that records UI requests.
        for dropped in (True, False):
            manager = Mock()
            manager.invoke_props_dialog.return_value = {'RUNNING_MODAL'}
            context = types.SimpleNamespace(window_manager=manager)
            operator = types.SimpleNamespace(
                properties=types.SimpleNamespace(is_property_set=lambda name: dropped),
                filepath='dropped.bgcode', files=[], bl_label='Import GCode/BGCode')
            operator.invoke_popup = types.MethodType(
                importer.ImportGCodeOperator.invoke_popup, operator)
            with self.subTest(dropped=dropped):
                self.assertEqual(importer.ImportGCodeOperator.invoke(operator, context, None),
                                 {'RUNNING_MODAL'})
                if dropped:
                    manager.invoke_props_dialog.assert_called_once()
                    manager.fileselect_add.assert_not_called()
                else:
                    manager.fileselect_add.assert_called_once_with(operator)
                    manager.invoke_props_dialog.assert_not_called()

    def test_registration_can_be_repeated(self):
        importer.unregister()
        self.assertFalse(hasattr(bpy.types, 'GCODE_FH_import'))
        importer.register()
        self.assertTrue(hasattr(bpy.types, 'GCODE_FH_import'))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(FileImportTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise RuntimeError('Blender file import regression checks failed')
