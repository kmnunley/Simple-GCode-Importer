"""Verify that the streaming preflight preserves line and filter boundaries."""
import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest

from bgcode_fixtures import block

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'estimate_test_importer', ROOT / 'simple_gcode_importer' / '__init__.py')
addon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = addon
spec.loader.exec_module(addon)
from estimate_test_importer.memory_estimate import Scan, estimate_mib, scan_batches


class MemoryEstimateTests(unittest.TestCase):
    def scan_text(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'model.gcode'
            path.write_text(text)
            scan = Scan()
            for _ in scan_batches(path, scan):
                pass
            return scan

    def test_counts_full_circle_and_relative_quarter_arc_samples(self):
        scan = self.scan_text(
            'M83\nG92 X10 Y0\nG91\nG3 I-10 E1\nG3 X-10 Y10 I-10 E1\n')
        # 32 points for the circle, eight for the quarter arc, plus one run.
        self.assertEqual(scan.points(), 45)

    def test_counts_trailing_decimals_and_ignores_nonmatching_tower_markers(self):
        scan = self.scan_text(
            'M83\nG1 X1 E1. ;TYPE:Wipe tower\n;TYPE:Wipe_tower\nG1 X2 E1.\n')
        self.assertEqual(scan.points(), 7)
        self.assertEqual(scan.points(exclude_wipe_tower=False), 7)

    def test_feature_state_after_tower_depends_on_tower_exclusion(self):
        scan = self.scan_text(
            'M83\n;TYPE:Perimeter\nG1 X10 E1\n;WIPE_TOWER_START\n'
            ';TYPE:Internal infill\nG1 X20 E1\n;WIPE_TOWER_END\nG1 X30 E1\n')
        self.assertEqual(scan.points(exclude_infill=True), 12)
        self.assertEqual(scan.points(exclude_infill=True, exclude_wipe_tower=False), 6)

    def test_file_picker_draw_can_run_before_selected_files_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'model.bgcode'
            path.touch()
            params = SimpleNamespace(directory=directory.encode(), filename='')
            context = SimpleNamespace(selected_files=None, space_data=SimpleNamespace(
                type='FILE_BROWSER', params=params))
            selected = addon.ImportGCodeOperator.selected_paths
            self.assertEqual(selected(None, context), ())
            params.filename = path.name
            self.assertEqual(selected(None, context), (str(path),))

    def test_binary_and_text_estimates_agree_across_a_split_line(self):
        parts = (b'M82\nG1 X1 E1\nG1 E0.5\nG1 X2 E1\n;TYPE:Sparse in',
                 b'fill\nG1 X3 E2\n;TYPE:Support\nG2 X4 Y0 I.5 E3\n;TYPE:Perimeter\nG1 X5 E4')
        binary = b'GCDE' + struct.pack('<IH', 1, 1) + b''.join(
            block(part, compression=1) for part in parts)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'part.bgcode').write_bytes(binary)
            (folder / 'part.gcode').write_bytes(b''.join(parts))
            scans = []
            for path in folder.iterdir():
                scan = Scan()
                for _ in scan_batches(path, scan):
                    pass
                scans.append(scan)
        self.assertEqual(scans[0], scans[1])
        scan = scans[0]
        self.assertEqual(scan.lines, 10)
        self.assertEqual(scan.points(exclude_infill=True, exclude_supports=True), 12)
        self.assertGreater(scan.points(), scan.points(exclude_supports=True))
        self.assertGreater(scan.points(), scan.points(profile='ROUND'))
        self.assertNotEqual(estimate_mib(scan), (0, 0))

    def test_unbeveled_paths_estimate_less_ram_than_either_bevel(self):
        scan = Scan(lines=60000)
        scan.without_tower[0] = [50000, 1000]
        low, high = estimate_mib(scan, profile='NONE')
        self.assertGreater(low, 0)
        for profile in ('BEAD', 'ROUND'):
            self.assertLess(high, estimate_mib(scan, profile=profile)[0])

    def test_fully_filtered_source_still_requires_ram(self):
        scan = Scan(lines=50_000_000)
        scan.without_tower[1] = [100, 1]
        self.assertEqual(scan.points(exclude_infill=True), 0)
        for profile in ('BEAD', 'ROUND', 'NONE'):
            with self.subTest(profile=profile):
                low, high = estimate_mib(scan, profile=profile, exclude_infill=True)
                self.assertGreater(low, 0)
                self.assertGreater(high, low)

    def test_replacing_pending_selection_closes_the_previous_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f'{name}.gcode' for name in ('first', 'second')]
            for path in paths:
                path.write_text('G1 X1 E1\n')
            preview = addon._MemoryPreview()
            self.addCleanup(preview.cancel)
            preview.request((paths[0],))
            previous = preview.job
            next(previous)
            preview.request((paths[1],))
            with self.assertRaises(StopIteration):
                next(previous)
            self.assertIsNone(preview.tick())
            self.assertEqual(preview.scan.points(), 6)
