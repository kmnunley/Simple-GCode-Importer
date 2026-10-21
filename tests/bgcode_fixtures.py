"""Small independent BGCode encoders for exercising the reader (format v1).

Layout: https://github.com/prusa3d/libbgcode/blob/main/doc/specifications.md
These fixtures deliberately do not use the reader's own constants or helpers.
"""

import struct
import zlib


def heatshrink_literals(data):
    bits = ''.join('1' + format(byte, '08b') for byte in data)
    bits += '0' * (-len(bits) % 8)
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))


def meatpack(text, no_spaces=False):
    table = '0123456789.E\nGX' if no_spaces else '0123456789. \nGX'
    result = bytearray(b'\xff\xff\xfb')
    if no_spaces:
        result.extend(b'\xff\xff\xf7')
        text = '\n'.join(line.replace(' ', '') if line.startswith('G') else line
                         for line in text.split('\n'))
    # Real encoders pad odd lines with another newline in the upper nibble.
    for line in text.splitlines(keepends=True):
        if len(line) % 2:
            line += '\n'
        for i in range(0, len(line), 2):
            pair = line[i:i + 2]
            nibbles = [table.index(c) if c in table else 15 for c in pair]
            result.append(nibbles[0] | nibbles[1] << 4)
            result.extend(ord(c) for c, nibble in zip(pair, nibbles) if nibble == 15)
    return bytes(result) + b'\xff\xff\xf9'


def block(payload, compression=0, encoding=0, checksum=1, block_type=1,
          declared_size=None):
    size = len(payload) if declared_size is None else declared_size
    encoded = (zlib.compress(payload) if compression == 1 else
               heatshrink_literals(payload) if compression in (2, 3) else payload)
    header = struct.pack('<HHI', block_type, compression, size)
    if compression:
        header += struct.pack('<I', len(encoded))
    params = (struct.pack('<HHH', 0, 16, 16) if block_type == 5 else
              struct.pack('<H', encoding))
    body = header + params + encoded
    return body + (struct.pack('<I', zlib.crc32(body)) if checksum else b'')


def binary(text, compression=0, encoding=0, checksum=1):
    payload = text.encode() if encoding == 0 else meatpack(text, encoding == 2)
    return (b'GCDE' + struct.pack('<IH', 1, checksum)
            + block(b'printer_model=TEST\n', checksum=checksum, block_type=3)
            + block(b'ignored thumbnail', checksum=checksum, block_type=5)
            + block(payload, compression, encoding, checksum))
