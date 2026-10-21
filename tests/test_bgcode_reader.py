"""Run without Blender: python -m unittest discover -s tests -p test_bgcode_reader.py."""

import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest

from bgcode_fixtures import binary, block, meatpack

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'bgcode_reader', ROOT / 'simple_gcode_importer' / 'gcode_lib.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)

TEXT = 'M83\nG0 X10\nG3 X0 Y10 I-10 E1\nT1\nG1 X-10 E1\n'


class ReaderTests(unittest.TestCase):
    def load(self, data, name='sample.bgcode'):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / name
            path.write_bytes(data)
            return reader.load(path)

    def test_all_compressions_encodings_and_checksums(self):
        for compression in range(4):
            for encoding in range(3):
                for checksum in range(2):
                    with self.subTest(compression=compression, encoding=encoding,
                                      checksum=checksum):
                        self.assertEqual(''.join(self.load(binary(
                            TEXT, compression, encoding, checksum))), TEXT)

    def test_text_preserves_comments_thumbnails_line_numbers_and_bom(self):
        # Image comments must never consume commands, even if their end is missing.
        text = '; thumbnail begin 16x16 4\n; AAAA\n\n' + TEXT
        self.assertEqual(''.join(self.load(b'\xef\xbb\xbf' + text.encode())), text)

    def test_magic_detection_does_not_depend_on_extension(self):
        self.assertEqual(''.join(self.load(binary(TEXT), 'misnamed.gcode')), TEXT)
        self.assertEqual(''.join(self.load(TEXT.encode())), TEXT)

    def test_multiple_blocks_join_without_inserting_a_line_break(self):
        data = b'GCDE' + struct.pack('<IH', 1, 0)
        data += block(b'G1 X', checksum=0) + block(b'10 E1\n', checksum=0)
        self.assertEqual(self.load(data), ['G1 X10 E1\n'])

    def test_empty_comment_only_and_unrecognized_binary_fail(self):
        for data in (b'', b'; just comments\n\n', b'OTHER\x00binary'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.load(data)

    def test_unknown_version_checksum_compression_block_and_encoding_fail(self):
        for data in (
            b'GCDE' + struct.pack('<IH', 2, 1),
            b'GCDE' + struct.pack('<IH', 1, 2),
            b'GCDE' + struct.pack('<IH', 1, 0) + struct.pack('<HHI', 1, 4, 0),
            b'GCDE' + struct.pack('<IH', 1, 0) + struct.pack('<HHI', 6, 0, 0),
            b'GCDE' + struct.pack('<IH', 1, 1) + block(b'G1 X1\n', encoding=3),
        ):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, 'Unsupported'):
                self.load(data)

    def test_truncated_headers_payload_and_crc_fail(self):
        data = binary(TEXT)
        for end in (4, 9, 11, 18, len(data) - 1):
            with self.subTest(end=end), self.assertRaisesRegex(ValueError, 'Truncated'):
                self.load(data[:end])

    def test_crc_corruption_fails(self):
        data = bytearray(binary(TEXT))
        data[-5] ^= 1
        with self.assertRaisesRegex(ValueError, 'CRC32'):
            self.load(data)

    def test_declared_size_mismatch_and_allocation_limit_fail(self):
        header = b'GCDE' + struct.pack('<IH', 1, 1)
        for size in (1, 100, 2**32 - 1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.load(header + block(b'G1 X1\n', compression=1, declared_size=size))

    def test_truncated_deflate_and_heatshrink_fail_even_with_valid_crc(self):
        # payload is supplied directly here, so the compressed data really is broken.
        for compression, payload in ((1, b'x\x9c'), (2, b'\x80'), (3, b'\x80')):
            data = b'GCDE' + struct.pack('<IH', 1, 0)
            data += struct.pack('<HHIIH', 1, compression, 20, len(payload), 0) + payload
            with self.subTest(compression=compression), self.assertRaises(ValueError):
                self.load(data)

    def test_heatshrink_overlapping_back_reference(self):
        for window in (11, 12):
            # Literal A, then copy distance 1 / length 4. Output must be AAAAA.
            bits = '1' + format(ord('A'), '08b') + '0' + '0' * window + '0011'
            bits += '0' * (-len(bits) % 8)
            payload = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
            self.assertEqual(reader._heatshrink_decompress(payload, window, 4, 5), b'AAAAA')
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                reader._heatshrink_decompress(payload, window, 4, 4)
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                reader._heatshrink_decompress(payload + b'\x00', window, 4, 5)

    def test_meatpack_escapes_modes_and_odd_line_padding(self):
        text = '; preserved 3D comment\nM862.3 P "MK4S"\nM83\nG29 P9 X10 Y-4 W32 H4\nG1 X12 Y-3.5 E1\n'
        for no_spaces in (False, True):
            with self.subTest(no_spaces=no_spaces):
                self.assertEqual(reader._meatpack_decode(meatpack(text, no_spaces)), text)
        for data in (b'\xff\xff', b'\xff\xff\xfb\xff', b'\xff\xff\xfb\x1f'):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, 'Truncated'):
                reader._meatpack_decode(data)


if __name__ == '__main__':
    unittest.main(verbosity=2)
