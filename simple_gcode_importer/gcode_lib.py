# SPDX-License-Identifier: GPL-3.0-or-later
"""Read text G-code and Prusa binary G-code using only the standard library.

Reader subset adapted from Hyiger's gcode-lib 1.0.0, as contributed by
amoose136 in PR #18 (74befbedac8bf66909f8c7bb0db87548f917b750).
See THIRD_PARTY_NOTICES.md and LICENSE.gcode-lib for attribution and license.
Modified 2026-10-21: keep only reading/decoding; preserve text lines; validate
headers, checksums, sizes and truncated compressed/encoded streams.
Unlike upstream's structured API, load() returns strings for our path builder.
"""

import re
import struct
import zlib

# Limits apply to each G-code block before MeatPack decoding, not the whole file.
MAX_BLOCK_SIZE = 64 * 1024 * 1024
_BGCODE_MAGIC = b"GCDE"


def load(path):
    """Return source lines, auto-detecting binary magic rather than extension."""
    with open(path, "rb") as stream:
        data = stream.read()
    if data.startswith(_BGCODE_MAGIC):
        text = _load_bgcode(data)
    else:
        if b"\x00" in data:
            raise ValueError("Unrecognized binary file; expected Prusa BGCode (GCDE)")
        text = data.decode("utf-8-sig")
    lines = text.splitlines(keepends=True)
    if not any(line.split(";", 1)[0].strip() for line in lines):
        raise ValueError("No G-code commands found")
    return lines


def _decompress(payload, compression, size):
    if size > MAX_BLOCK_SIZE:
        raise ValueError("G-code block exceeds the 64 MiB decompression limit")
    if compression == 0:
        result = payload
    elif compression == 1:
        decoder = zlib.decompressobj()
        try:
            result = decoder.decompress(payload, size + 1)
        except zlib.error as error:
            raise ValueError(f"Invalid DEFLATE stream: {error}") from error
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("Invalid or oversized DEFLATE stream")
    else:
        result = _heatshrink_decompress(payload, 11 if compression == 2 else 12, 4, size)
    if len(result) != size:
        raise ValueError("Decompressed size does not match the block header")
    return result


def _load_bgcode(data):
    if len(data) < 10:
        raise ValueError("Truncated BGCode file header")
    version, checksum = struct.unpack_from("<IH", data, 4)
    if version != 1:
        raise ValueError(f"Unsupported BGCode version {version}")
    if checksum not in (0, 1):
        raise ValueError(f"Unsupported BGCode checksum type {checksum}")
    checksum_size = 4 if checksum else 0
    position = 10
    parts = []
    while position < len(data):
        start = position
        if position + 8 > len(data):
            raise ValueError(f"Truncated block header at offset {start}")
        block_type, compression, size = struct.unpack_from("<HHI", data, position)
        if block_type not in range(6):
            raise ValueError(f"Unsupported block type {block_type}")
        if compression not in range(4):
            raise ValueError(f"Unsupported compression type {compression}")
        position += 8
        if compression:
            if position + 4 > len(data):
                raise ValueError(f"Truncated compressed block header at offset {start}")
            payload_size, = struct.unpack_from("<I", data, position)
            position += 4
        else:
            payload_size = size
        params_size = 6 if block_type == 5 else 2
        payload_start = position + params_size
        payload_end = payload_start + payload_size
        block_end = payload_end + checksum_size
        if block_end > len(data):
            raise ValueError(f"Truncated block payload at offset {start}")
        if checksum:
            stored, = struct.unpack_from("<I", data, payload_end)
            if zlib.crc32(data[start:payload_end]) != stored:
                raise ValueError(f"CRC32 mismatch at offset {start}")
        if block_type == 1:
            encoding, = struct.unpack_from("<H", data, position)
            if encoding not in (0, 1, 2):
                raise ValueError(f"Unsupported G-code encoding {encoding}")
            raw = _decompress(data[payload_start:payload_end], compression, size)
            parts.append(raw.decode("utf-8") if encoding == 0 else _meatpack_decode(raw))
        # Metadata and thumbnails do not affect paths; skip without decoding.
        position = block_end
    if not parts:
        raise ValueError("No G-code blocks found in BGCode file")
    return "".join(parts)


def _heatshrink_decompress(
    data: bytes, window_sz2: int, lookahead_sz2: int, expected_size: int,
) -> bytes:
    """Decompress *data* using the Heatshrink LZSS algorithm.

    Parameters match the libbgcode convention:

    * ``window_sz2``   — log₂ of the sliding-window size (11 or 12).
    * ``lookahead_sz2`` — log₂ of the lookahead size (typically 4).
    * ``expected_size`` — exact number of uncompressed bytes to produce.

    The decoder reads bits MSB-first.  A tag bit of **1** introduces an
    8-bit literal; **0** introduces a back-reference of *window_sz2* index
    bits followed by *lookahead_sz2* count bits.
    """
    if expected_size == 0:
        if data:
            raise ValueError("Heatshrink stream exceeds declared size")
        return b""

    window_size = 1 << window_sz2
    mask = window_size - 1
    window = bytearray(window_size)
    head = 0

    output = bytearray(expected_size)
    out_pos = 0

    # ── bit reader state ──
    input_len = len(data)
    byte_pos = 0
    bit_buf = 0       # current byte being consumed
    bits_left = 0     # bits remaining in bit_buf (0–8)

    def _get_bits(count: int) -> int:
        """Read *count* bits MSB-first and return as an integer."""
        nonlocal byte_pos, bit_buf, bits_left
        result = 0
        remaining = count
        while remaining > 0:
            if bits_left == 0:
                if byte_pos >= input_len:
                    raise ValueError("Truncated Heatshrink stream")
                bit_buf = data[byte_pos]
                byte_pos += 1
                bits_left = 8
            take = min(remaining, bits_left)
            # Extract the top *take* bits from bit_buf.
            result = (result << take) | (bit_buf >> (bits_left - take))
            bit_buf &= (1 << (bits_left - take)) - 1
            bits_left -= take
            remaining -= take
        return result

    while out_pos < expected_size:
        tag = _get_bits(1)
        if tag:
            # Literal byte
            byte_val = _get_bits(8)
            output[out_pos] = byte_val
            window[head & mask] = byte_val
            head += 1
            out_pos += 1
        else:
            # Back-reference
            index = _get_bits(window_sz2)
            count = _get_bits(lookahead_sz2) + 1
            neg_offset = index + 1
            if out_pos + count > expected_size:
                raise ValueError("Heatshrink output exceeds declared size")
            for _ in range(count):
                c = window[(head - neg_offset) & mask]
                output[out_pos] = c
                window[head & mask] = c
                head += 1
                out_pos += 1

    if byte_pos < input_len or bit_buf:
        raise ValueError("Heatshrink stream exceeds declared size")
    return bytes(output)


# ---------------------------------------------------------------------------
# MeatPack decoding
# ---------------------------------------------------------------------------

# 4-bit nibble → character lookup (standard mode)
_MP_CHAR_TABLE: str = "0123456789. \nGX"
# Index 0xF (15) is the escape sentinel — not a real character.

# Command bytes sent after the 0xFF 0xFF signal prefix.
_MP_CMD_ENABLE_PACKING      = 251
_MP_CMD_DISABLE_PACKING     = 250
_MP_CMD_RESET_ALL           = 249
_MP_CMD_ENABLE_NO_SPACES    = 247
_MP_CMD_DISABLE_NO_SPACES   = 246

_MP_SPACE_RE = re.compile(r"(?<=[0-9.])(?=[XYZEIJKRFSPWHCAG])")
"""Insert whitespace before an uppercase axis/command letter that immediately
follows a digit or period.  MeatPack *no-spaces* mode strips inter-word
spaces; this regex restores them for the importer's whitespace parser."""


def _meatpack_decode(data: bytes) -> str:
    """Decode MeatPack / MeatPackComments encoded *data* to a string.

    MeatPack packs two common G-code characters into a single byte using
    4-bit nibbles.  Nibble value 0xF is an escape: the *next* full byte
    is a raw character.  The scheme is toggled on/off by a ``0xFF 0xFF
    <cmd>`` signal sequence embedded in the stream.

    Signal detection uses a one-byte lookahead: when a ``0xFF`` is seen,
    peek at the next byte.  If it is also ``0xFF`` the pair is a signal
    prefix and the following byte is a command.  Otherwise the ``0xFF``
    is normal data (a packed byte with both nibbles = escape when packing
    is enabled, or a raw ``0xFF`` when it is not).
    """
    out: list[str] = []
    table = list(_MP_CHAR_TABLE)
    packing = False
    # Number of pending full-char bytes to pass through raw.
    pending_raw = 0
    # Character held from unpacking the second nibble when the first
    # nibble was an escape.
    held_char: str | None = None

    i = 0
    n = len(data)

    while i < n:
        b = data[i]
        i += 1

        # ── signal detection (0xFF 0xFF <cmd>) ──
        if b == 0xFF:
            if i < n and data[i] == 0xFF:
                # Confirmed signal prefix — consume the second 0xFF.
                i += 1
                if i >= n:
                    raise ValueError("Truncated MeatPack command")
                cmd = data[i]
                i += 1
                if cmd == _MP_CMD_ENABLE_PACKING:
                    packing = True
                elif cmd == _MP_CMD_DISABLE_PACKING:
                    packing = False
                elif cmd == _MP_CMD_RESET_ALL:
                    packing = False
                    table = list(_MP_CHAR_TABLE)
                elif cmd == _MP_CMD_ENABLE_NO_SPACES:
                    table = list(_MP_CHAR_TABLE)
                    table[11] = "E"   # space slot becomes 'E'
                elif cmd == _MP_CMD_DISABLE_NO_SPACES:
                    table = list(_MP_CHAR_TABLE)
                continue
            # Single 0xFF (next byte is not 0xFF) — fall through and
            # process it as normal data below.

        # ── pending raw bytes (from a previous escape nibble) ──
        if pending_raw > 0:
            pending_raw -= 1
            # The raw byte is always the escaped character (came first in
            # the pair), so it must be emitted *before* any held second
            # nibble character.
            out.append(chr(b))
            if pending_raw == 0 and held_char is not None:
                out.append(held_char)
                held_char = None
            continue

        # ── raw mode (packing disabled) ──
        if not packing:
            out.append(chr(b))
            continue

        # ── packing mode — unpack two nibbles ──
        # MeatPack convention: low nibble is the FIRST character,
        # high nibble is the SECOND character.
        lo = b & 0x0F          # first character
        hi = (b >> 4) & 0x0F   # second character

        if lo == 0x0F and hi == 0x0F:
            # Both nibbles are escapes → next 2 bytes are raw chars.
            pending_raw = 2
        elif lo == 0x0F:
            # First char (lo) is escape → next byte is a raw char.
            # Hold the second char (hi) until after the raw byte.
            pending_raw = 1
            held_char = table[hi]
        elif hi == 0x0F:
            # Second char (hi) is escape → output first char now,
            # next byte is raw.
            out.append(table[lo])
            pending_raw = 0 if table[lo] == "\n" else 1
        else:
            # Both nibbles are valid packed characters.
            out.append(table[lo])
            if table[lo] != "\n":
                out.append(table[hi])

    if pending_raw or held_char is not None:
        raise ValueError("Truncated MeatPack escape")

    text = "".join(out)

    # MeatPack no-spaces mode strips whitespace between G-code words.
    # Re-insert spaces so that "G1X10Y20" becomes "G1 X10 Y20" and
    # the importer can split the command from its axes.
    # Normalize only G command lines, as libbgcode does. Applying this to
    # every line corrupts quoted printer names (e.g. M862.3 P "MK4S") and comments.
    lines = []
    for line in text.splitlines(keepends=True):
        code, separator, comment = line.partition(";")
        if re.match(r"^\s*G[0-9]", code):
            code = _MP_SPACE_RE.sub(" ", code)
        lines.append(code + separator + comment)
    return "".join(lines)
