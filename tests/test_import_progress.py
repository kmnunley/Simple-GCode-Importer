"""Verify the interactive pipeline reports progress and rolls back on close."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

import bpy
from bgcode_fixtures import binary

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'progress_test_importer', ROOT / 'simple_gcode_importer' / '__init__.py')
addon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = addon
spec.loader.exec_module(addon)


class ImportProgressTests(unittest.TestCase):
    def test_status_redraw_preserves_the_bar_with_a_cached_header(self):
        header = bpy.types.STATUSBAR_HT_header
        workspace = bpy.data.workspaces[0]
        original = header.draw
        status = addon._ImportStatus()

        def other_addon_draw(self, context):
            original(self, context)

        try:
            workspace.status_text_set(None)
            header.draw = other_addon_draw
            status.start(workspace)
            status.redraw()
            self.assertIs(header.draw, status.draw)
            status.complete(65.2)
            self.assertEqual(status.message, 'G-code import complete — 1m 5.2s')
            self.assertTrue(bpy.app.timers.is_registered(status.expire))
            status.clear()
            self.assertIs(header.draw, other_addon_draw)
            self.assertFalse(bpy.app.timers.is_registered(status.expire))
        finally:
            status.clear()
            header.draw = original

    def test_cancel_during_curves_rolls_back_the_entire_batch(self):
        def snapshot():
            return tuple(set(getattr(bpy.data, kind)) for kind in ('objects', 'curves', 'materials'))

        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / name for name in ('one.bgcode', 'two.bgcode')]
            for path in paths:
                path.write_bytes(binary('M83\nG0 Z.2\nG1 X10 E1\n', 3, 2))
            before = snapshot()
            steps = addon._import_gcodes_steps(paths, interactive=True)
            try:
                seen = []
                for fraction, stage in steps:
                    seen.append((fraction, stage))
                    if stage.startswith('Building curves') and 'two.bgcode' in stage:
                        break
                self.assertEqual(sorted(seen), seen)
                self.assertTrue(any(fraction > 0 and stage.startswith('Reading 1/2')
                                    for fraction, stage in seen))
                self.assertTrue(seen[-1][1].startswith('Building curves'))
                self.assertNotEqual(snapshot(), before)
            finally:
                steps.close()
            self.assertEqual(snapshot(), before)
