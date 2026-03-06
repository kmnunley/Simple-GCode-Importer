#!/usr/bin/env python3
"""
gcode_lib — general-purpose G-code manipulation library.

Provides data structures and utilities for parsing, analysing, and
transforming G-code files in both plain-text (.gcode) and Prusa binary
(.bgcode) formats.

Key capabilities
================
- Auto-detect and load text or binary G-code from disk.
- Parse G-code into structured ``GCodeLine`` objects with command, axis
  words, and comment cleanly separated.
- Track G-code modal state (G90/G91, M82/M83, G90.1/G91.1, G92) and
  absolute tool position through a sequence of lines.
- Linearize G2/G3 arcs into G1 segments (configurable precision).
- Apply arbitrary XY transforms: skew correction, translation, or any
  user-supplied ``fn(x, y) -> (x', y')``.
- Compute statistics: move counts, XY/Z bounds, feedrates, layer heights,
  total extrusion.
- Save modified G-code back to disk, preserving file format and all
  non-GCode blocks (thumbnails, metadata) in binary .bgcode files.

Design constraints
==================
- Standard library only (``struct``, ``zlib``, ``re``, ``math``, …).
  No third-party runtime dependencies.
- Python 3.10+; uses ``from __future__ import annotations``.
- Relative XY (G91) is **not** supported for XY transforms.  Callers
  must linearize arcs and confirm G90 is active before transforming.
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EPS = 1e-9                    # Floating-point comparison tolerance
DEFAULT_ARC_SEG_MM = 0.20     # Max chord length (mm) per linearised arc segment
DEFAULT_ARC_MAX_DEG = 5.0     # Max sweep angle (°) per linearised arc segment
DEFAULT_XY_DECIMALS = 3       # Output decimal places for X/Y axes
DEFAULT_OTHER_DECIMALS = 5    # Output decimal places for E/F/Z/I/J/K

# Pre-compiled regexes
_MOVE_RE = re.compile(r"^(G0|G1)\b", re.IGNORECASE)
_ARC_RE  = re.compile(r"^(G2|G3)\b", re.IGNORECASE)
_NUM_RE  = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_AXIS_RE = re.compile(rf"([XYZEFRIJK])\s*({_NUM_RE})", re.IGNORECASE)

# Binary .bgcode constants
_BGCODE_MAGIC = b"GCDE"
_BLK_GCODE      = 1
_BLK_THUMBNAIL  = 5
_COMP_NONE              = 0
_COMP_DEFLATE           = 1
_COMP_HEATSHRINK_11_4   = 2   # window=2048 B, lookahead=16 B
_COMP_HEATSHRINK_12_4   = 3   # window=4096 B, lookahead=16 B
_ENC_RAW                = 0
_ENC_MEATPACK           = 1
_ENC_MEATPACK_COMMENTS  = 2

# Thumbnail image format codes (matching libbgcode EImageFormat)
_IMG_PNG = 0
_IMG_JPG = 1
_IMG_QOI = 2

# Map magic byte prefixes to format codes
_IMG_MAGIC: List[Tuple[bytes, int]] = [
    (b"\x89PNG", _IMG_PNG),
    (b"\xff\xd8", _IMG_JPG),
    (b"qoif",    _IMG_QOI),
]

# Map text keyword → format code (case-insensitive match on keyword suffix)
_THUMB_KEYWORD_FMT: Dict[str, int] = {
    "thumbnail":     _IMG_PNG,
    "thumbnail_png": _IMG_PNG,
    "thumbnail_jpg": _IMG_JPG,
    "thumbnail_qoi": _IMG_QOI,
}
# Map format code → keyword used when re-emitting plain-text thumbnails
_THUMB_FMT_KEYWORD: Dict[int, str] = {
    _IMG_PNG: "thumbnail",      # PrusaSlicer-compatible default for PNG
    _IMG_JPG: "thumbnail_JPG",
    _IMG_QOI: "thumbnail_QOI",
}
_THUMB_B64_LINE_LEN = 76        # base64 characters per comment line

# Regexes for plain-text thumbnail comment blocks
_THUMB_BEGIN_RE = re.compile(
    r"^;\s*(thumbnail(?:_\w+)?)\s+begin\s+(\d+)x(\d+)\s+(\d+)",
    re.IGNORECASE,
)
_THUMB_END_RE = re.compile(r"^;\s*thumbnail(?:_\w+)?\s+end\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ModalState:
    """G-code modal state, updated line-by-line during parsing.

    Tracks coordinate mode, extrusion mode, arc-centre mode, and the
    current absolute tool position.
    """
    abs_xy: bool = True          # True = G90 (absolute), False = G91 (relative)
    abs_e: bool = True           # True = M82 (absolute E), False = M83 (relative E)
    ij_relative: bool = True     # True = G91.1 (relative IJ), False = G90.1 (absolute IJ)
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    e: float = 0.0
    f: Optional[float] = None

    def copy(self) -> "ModalState":
        """Return an independent copy of this state."""
        return ModalState(
            abs_xy=self.abs_xy,
            abs_e=self.abs_e,
            ij_relative=self.ij_relative,
            x=self.x, y=self.y, z=self.z,
            e=self.e, f=self.f,
        )


@dataclass
class GCodeLine:
    """A single parsed G-code line.

    Attributes
    ----------
    raw:     Original line text (trailing newline stripped).
    command: Uppercased command token (e.g. ``"G1"``, ``"M82"``), or ``""``
             for blank / comment-only lines.
    words:   Axis-word dict parsed from the code portion, e.g.
             ``{"X": 10.0, "Y": 20.0, "E": 1.5}``.
    comment: Comment portion including the leading ``";"``, or ``""``.
    """
    raw: str
    command: str
    words: Dict[str, float]
    comment: str

    @property
    def is_move(self) -> bool:
        """True if this is a G0 or G1 command."""
        return bool(_MOVE_RE.match(self.command))

    @property
    def is_arc(self) -> bool:
        """True if this is a G2 or G3 command."""
        return bool(_ARC_RE.match(self.command))

    @property
    def is_blank(self) -> bool:
        """True if the line carries no command and no meaningful comment."""
        return not self.command and not self.comment.strip("; \t")


@dataclass
class Thumbnail:
    """An image thumbnail from a G-code file (text or binary).

    ``params`` holds a 6-byte block (width uint16, height uint16, format
    uint16) following the libbgcode spec.  For thumbnails parsed from
    plain-text files the format code is inferred from the keyword
    (``thumbnail_JPG`` / ``thumbnail_QOI``) or from the decoded image magic
    bytes.  ``_raw_block`` is set only for .bgcode sources; it carries the
    verbatim block bytes used for lossless binary round-trips.
    """
    params: bytes        # 6-byte block params (width, height, fmt_code)
    data: bytes          # Decompressed / decoded image bytes
    _raw_block: bytes    # Full bgcode block bytes (b"" for text sources)

    @property
    def width(self) -> int:
        """Image width in pixels."""
        return struct.unpack_from("<H", self.params, 0)[0]

    @property
    def height(self) -> int:
        """Image height in pixels."""
        return struct.unpack_from("<H", self.params, 2)[0]

    @property
    def format_code(self) -> int:
        """Raw format code from the bgcode thumbnail block params."""
        return struct.unpack_from("<H", self.params, 4)[0]


@dataclass
class GCodeFile:
    """Parsed G-code file — top-level container for this library.

    Attributes
    ----------
    lines:         All G-code lines in source order.
    thumbnails:    Thumbnail images (populated only for .bgcode sources).
    source_format: ``"text"`` or ``"bgcode"``.
    """
    lines: List[GCodeLine]
    thumbnails: List[Thumbnail]
    source_format: str                               # "text" | "bgcode"
    _bgcode_file_hdr: Optional[bytes] = field(default=None, repr=False)
    _bgcode_nongcode_blocks: Optional[List[bytes]] = field(default=None, repr=False)


@dataclass
class Bounds:
    """Axis-aligned bounding box computed from G-code move endpoints.

    Starts at ±∞; call ``expand(x, y)`` to include points.  Check
    ``valid`` before reading coordinates when the input may be empty.
    """
    x_min: float = float("inf")
    x_max: float = float("-inf")
    y_min: float = float("inf")
    y_max: float = float("-inf")
    z_min: float = float("inf")
    z_max: float = float("-inf")

    @property
    def valid(self) -> bool:
        """True if at least one XY point has been added."""
        return self.x_min != float("inf")

    def expand(self, x: float, y: float) -> None:
        """Expand the XY box to include ``(x, y)``."""
        if x < self.x_min: self.x_min = x
        if x > self.x_max: self.x_max = x
        if y < self.y_min: self.y_min = y
        if y > self.y_max: self.y_max = y

    def expand_z(self, z: float) -> None:
        """Expand the Z range to include ``z``."""
        if z < self.z_min: self.z_min = z
        if z > self.z_max: self.z_max = z

    @property
    def width(self) -> float:
        """X extent ``(x_max - x_min)``, or 0.0 if not valid."""
        return self.x_max - self.x_min if self.valid else 0.0

    @property
    def height(self) -> float:
        """Y extent ``(y_max - y_min)``, or 0.0 if not valid."""
        return self.y_max - self.y_min if self.valid else 0.0

    @property
    def center_x(self) -> float:
        """X midpoint, or 0.0 if not valid."""
        return 0.5 * (self.x_min + self.x_max) if self.valid else 0.0

    @property
    def center_y(self) -> float:
        """Y midpoint, or 0.0 if not valid."""
        return 0.5 * (self.y_min + self.y_max) if self.valid else 0.0


@dataclass
class GCodeStats:
    """Statistical summary of a G-code line sequence."""
    total_lines: int = 0
    blank_lines: int = 0
    comment_only_lines: int = 0
    move_count: int = 0          # G0 + G1 total
    arc_count: int = 0           # G2 + G3 total
    travel_count: int = 0        # non-extruding moves
    extrude_count: int = 0       # moves with positive extrusion delta
    retract_count: int = 0       # moves with negative extrusion delta
    total_extrusion: float = 0.0
    bounds: Bounds = field(default_factory=Bounds)
    z_heights: List[float] = field(default_factory=list)   # unique Z values, in order
    feedrates: List[float] = field(default_factory=list)   # unique F values, in order

    @property
    def layer_count(self) -> int:
        """Approximate layer count (number of unique Z heights seen)."""
        return len(self.z_heights)


# ---------------------------------------------------------------------------
# Parsing utilities
# ---------------------------------------------------------------------------

def split_comment(line: str) -> Tuple[str, str]:
    """Split a G-code line into ``(code, comment)`` at the first ``';'``.

    The comment string includes the leading ``";"`` if present.
    Trailing whitespace is stripped from the code portion.

    >>> split_comment("G1 X10 Y20 ; move to start")
    ('G1 X10 Y20', '; move to start')
    >>> split_comment("G1 X10 Y20")
    ('G1 X10 Y20', '')
    """
    if ";" in line:
        code, comment = line.split(";", 1)
        return code.rstrip(), ";" + comment
    return line.rstrip(), ""


def parse_words(code: str) -> Dict[str, float]:
    """Parse axis words from a G-code command string.

    Returns a ``{axis: value}`` dict for every recognised axis letter
    (X/Y/Z/E/F/I/J/K/R) found in *code*.  Letter keys are always uppercase.

    >>> parse_words("G1 X10.5 Y-3 E0.1")
    {'X': 10.5, 'Y': -3.0, 'E': 0.1}
    """
    return {m.group(1).upper(): float(m.group(2)) for m in _AXIS_RE.finditer(code)}


def parse_line(raw_line: str) -> GCodeLine:
    """Parse one line of G-code text into a :class:`GCodeLine`.

    The trailing newline is stripped before processing.
    """
    line = raw_line.rstrip("\n")
    code, comment = split_comment(line)
    s = code.strip()
    parts = s.split(None, 1)
    command = parts[0].upper() if parts else ""
    words = parse_words(code) if s else {}
    return GCodeLine(raw=line, command=command, words=words, comment=comment)


def parse_lines(text: str) -> List[GCodeLine]:
    """Parse a multi-line G-code string into a list of :class:`GCodeLine` objects."""
    return [parse_line(ln) for ln in text.splitlines()]


# ---------------------------------------------------------------------------
# Modal state management
# ---------------------------------------------------------------------------

def advance_state(state: ModalState, line: GCodeLine) -> None:
    """Update *state* in-place to reflect the given *line*.

    Handles:
    - Mode changes: G90/G91, M82/M83, G90.1/G91.1
    - G0/G1 linear moves (absolute and relative XY/Z)
    - G2/G3 arc moves (endpoint update only)
    - G92 position reset
    - E and F tracking on all motion commands
    """
    cmd = line.command
    words = line.words

    # --- Modal flag updates ---
    if cmd == "G90":
        state.abs_xy = True; return
    if cmd == "G91":
        state.abs_xy = False; return
    if cmd == "M82":
        state.abs_e = True; return
    if cmd == "M83":
        state.abs_e = False; return
    if cmd == "G90.1":
        state.ij_relative = False; return
    if cmd == "G91.1":
        state.ij_relative = True; return

    # --- G92: set position ---
    if cmd == "G92":
        if "X" in words: state.x = words["X"]
        if "Y" in words: state.y = words["Y"]
        if "Z" in words: state.z = words["Z"]
        if "E" in words: state.e = words["E"]
        return

    # --- G0/G1 linear move ---
    if _MOVE_RE.match(cmd):
        if state.abs_xy:
            if "X" in words: state.x = words["X"]
            if "Y" in words: state.y = words["Y"]
            if "Z" in words: state.z = words["Z"]
        else:
            state.x += words.get("X", 0.0)
            state.y += words.get("Y", 0.0)
            state.z += words.get("Z", 0.0)
        if "E" in words:
            state.e = words["E"] if state.abs_e else state.e + words["E"]
        if "F" in words:
            state.f = words["F"]
        return

    # --- G2/G3 arc move (update endpoint only; arc-centre handling not needed here) ---
    if _ARC_RE.match(cmd):
        if state.abs_xy:
            if "X" in words: state.x = words["X"]
            if "Y" in words: state.y = words["Y"]
            state.z = words.get("Z", state.z)
        else:
            state.x += words.get("X", 0.0)
            state.y += words.get("Y", 0.0)
            state.z += words.get("Z", 0.0)
        if "E" in words:
            state.e = words["E"] if state.abs_e else state.e + words["E"]
        if "F" in words:
            state.f = words["F"]
        return

    # --- Anything else that carries an E word (e.g. bare E-only commands) ---
    if "E" in words:
        state.e = words["E"] if state.abs_e else state.e + words["E"]


def iter_with_state(
    lines: List[GCodeLine],
    initial_state: Optional[ModalState] = None,
) -> Iterator[Tuple[GCodeLine, ModalState]]:
    """Yield ``(line, state)`` for every line in *lines*.

    *state* is the :class:`ModalState` **before** the line is processed —
    i.e. the printer's mode when executing that line.  A snapshot copy is
    yielded; callers may safely retain it.
    """
    state = initial_state.copy() if initial_state else ModalState()
    for line in lines:
        yield line, state.copy()
        advance_state(state, line)


def iter_moves(
    lines: List[GCodeLine],
    initial_state: Optional[ModalState] = None,
) -> Iterator[Tuple[GCodeLine, ModalState]]:
    """Yield ``(line, state)`` for G0/G1 lines only."""
    for line, state in iter_with_state(lines, initial_state):
        if line.is_move:
            yield line, state


def iter_arcs(
    lines: List[GCodeLine],
    initial_state: Optional[ModalState] = None,
) -> Iterator[Tuple[GCodeLine, ModalState]]:
    """Yield ``(line, state)`` for G2/G3 lines only."""
    for line, state in iter_with_state(lines, initial_state):
        if line.is_arc:
            yield line, state


def iter_extruding(
    lines: List[GCodeLine],
    initial_state: Optional[ModalState] = None,
) -> Iterator[Tuple[GCodeLine, ModalState]]:
    """Yield ``(line, state)`` for lines that deposit material.

    A line is considered extruding when it is a G0/G1/G2/G3 move that
    carries an E word whose value is greater than the current accumulator
    (absolute E) or greater than zero (relative E).
    """
    for line, state in iter_with_state(lines, initial_state):
        if not (line.is_move or line.is_arc):
            continue
        if "E" not in line.words:
            continue
        e_word = line.words["E"]
        if state.abs_e:
            if e_word > state.e:
                yield line, state
        else:
            if e_word > 0.0:
                yield line, state


# ---------------------------------------------------------------------------
# Formatting utilities
# ---------------------------------------------------------------------------

def fmt_float(v: float, places: int) -> str:
    """Format *v* to *places* decimal places, trimming trailing zeros.

    >>> fmt_float(10.0, 3)
    '10'
    >>> fmt_float(10.125, 3)
    '10.125'
    >>> fmt_float(-0.0, 3)
    '0'
    """
    s = f"{v:.{places}f}".rstrip("0").rstrip(".")
    if not s or s == "-":
        return "0"
    # Negative zero: "-0" should render as "0"
    if s == "-0":
        return "0"
    return s


def fmt_axis(
    axis: str,
    v: float,
    xy_decimals: int = DEFAULT_XY_DECIMALS,
    other_decimals: int = DEFAULT_OTHER_DECIMALS,
) -> str:
    """Format an axis value with axis-appropriate precision.

    X and Y use *xy_decimals*; all other axes use *other_decimals*.
    """
    places = xy_decimals if axis.upper() in ("X", "Y") else other_decimals
    return fmt_float(v, places)


def replace_or_append(
    code: str,
    axis: str,
    val: float,
    xy_decimals: int = DEFAULT_XY_DECIMALS,
    other_decimals: int = DEFAULT_OTHER_DECIMALS,
) -> str:
    """Replace the value of *axis* in *code*, or append it if absent.

    Only the first occurrence of the axis letter is replaced.
    """
    axis = axis.upper()
    tok = f"{axis}{fmt_axis(axis, val, xy_decimals, other_decimals)}"
    pat = re.compile(rf"(?i)\b{axis}\s*({_NUM_RE})\b")
    return pat.sub(tok, code, count=1) if pat.search(code) else (code + " " + tok)


# ---------------------------------------------------------------------------
# Plain-text thumbnail helpers (private)
# ---------------------------------------------------------------------------

def _parse_text_thumbnails(
    lines: List[GCodeLine],
) -> Tuple[List[GCodeLine], List[Thumbnail]]:
    """Extract thumbnail comment blocks from plain-text G-code lines.

    Scans *lines* for ``; thumbnail[_FORMAT] begin WxH SIZE`` …
    ``; thumbnail[_FORMAT] end`` blocks, decodes the base64 payload, and
    returns ``(filtered_lines, thumbnails)`` where *filtered_lines* has all
    thumbnail comment lines removed.

    Supports keywords: ``thumbnail`` / ``thumbnail_PNG`` (PNG),
    ``thumbnail_JPG`` (JPEG), ``thumbnail_QOI`` (QOI).  The format code is
    taken from the keyword when unambiguous; otherwise it is inferred from
    the decoded image magic bytes.
    """
    result: List[GCodeLine] = []
    thumbnails: List[Thumbnail] = []
    i = 0
    while i < len(lines):
        m = _THUMB_BEGIN_RE.match(lines[i].raw)
        if not m:
            result.append(lines[i])
            i += 1
            continue

        keyword, w_str, h_str = m.group(1), m.group(2), m.group(3)
        width, height = int(w_str), int(h_str)

        # Collect base64 payload lines until the matching end marker
        b64_parts: List[str] = []
        i += 1
        while i < len(lines):
            raw = lines[i].raw
            if _THUMB_END_RE.match(raw):
                i += 1  # consume end line
                break
            # Strip leading "; " or ";" comment prefix
            if raw.startswith("; "):
                b64_parts.append(raw[2:])
            elif raw.startswith(";"):
                b64_parts.append(raw[1:])
            else:
                b64_parts.append(raw)
            i += 1

        try:
            img_data = base64.b64decode("".join(b64_parts))
        except Exception:
            # Malformed block — skip silently (lines already consumed)
            continue

        # Determine format code from keyword, falling back to magic bytes
        fmt_code = _THUMB_KEYWORD_FMT.get(keyword.lower(), -1)
        if fmt_code == -1:
            fmt_code = _IMG_PNG  # default
            for magic, code in _IMG_MAGIC:
                if img_data[: len(magic)] == magic:
                    fmt_code = code
                    break

        params = struct.pack("<HHH", width, height, fmt_code)
        thumbnails.append(Thumbnail(params=params, data=img_data, _raw_block=b""))

    return result, thumbnails


def _render_text_thumbnails(thumbnails: List[Thumbnail]) -> str:
    """Render *thumbnails* as plain-text G-code comment blocks.

    Each thumbnail is wrapped in ``; keyword begin WxH SIZE`` /
    ``; keyword end`` with the base64 payload split into
    ``_THUMB_B64_LINE_LEN``-character comment lines.  A blank line
    separates consecutive thumbnails.
    """
    parts: List[str] = []
    for thumb in thumbnails:
        keyword = _THUMB_FMT_KEYWORD.get(thumb.format_code, "thumbnail")
        b64 = base64.b64encode(thumb.data).decode("ascii")
        parts.append(
            f"; {keyword} begin {thumb.width}x{thumb.height} {len(b64)}"
        )
        for off in range(0, len(b64), _THUMB_B64_LINE_LEN):
            parts.append("; " + b64[off : off + _THUMB_B64_LINE_LEN])
        parts.append(f"; {keyword} end")
        parts.append("")  # blank separator between thumbnails
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def from_text(text: str) -> GCodeFile:
    """Create a :class:`GCodeFile` from a G-code text string.

    Thumbnail comment blocks (``; thumbnail begin`` … ``; thumbnail end``)
    are extracted into :attr:`GCodeFile.thumbnails` and removed from
    :attr:`GCodeFile.lines`, mirroring the behaviour of binary .bgcode
    loading.
    """
    lines, thumbnails = _parse_text_thumbnails(parse_lines(text))
    return GCodeFile(lines=lines, thumbnails=thumbnails, source_format="text")


def to_text(gf: GCodeFile) -> str:
    """Render a :class:`GCodeFile` back to a G-code text string.

    If *gf* carries thumbnails they are re-emitted as ``; thumbnail begin``
    comment blocks at the top of the output, followed by the G-code lines.
    Untransformed lines preserve their original formatting via
    ``GCodeLine.raw``; transformed lines carry regenerated text.
    """
    gcode = "\n".join(line.raw for line in gf.lines) + "\n"
    if not gf.thumbnails:
        return gcode
    return _render_text_thumbnails(gf.thumbnails) + "\n" + gcode


def load(path: str) -> GCodeFile:
    """Load a G-code file from *path*, auto-detecting text vs binary format.

    Raises :class:`ValueError` if the file is binary but not a recognised
    Prusa .bgcode file.
    """
    if _is_bgcode_file(path):
        with open(path, "rb") as fh:
            return _load_bgcode(fh.read())
    with open(path, "rb") as fh:
        head = fh.read(512)
    if b"\x00" in head:
        raise ValueError(
            f"{path!r} appears to be binary but is not a valid .bgcode file "
            "(expected 'GCDE' magic).  Supported formats: text .gcode and Prusa .bgcode."
        )
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    lines, thumbnails = _parse_text_thumbnails(parse_lines(text))
    return GCodeFile(lines=lines, thumbnails=thumbnails, source_format="text")


def save(gf: GCodeFile, path: str) -> None:
    """Atomically write *gf* to *path*, preserving the original file format.

    For .bgcode sources all non-GCode blocks (thumbnails, slicer/printer/
    print metadata) are preserved verbatim so the resulting file can be
    uploaded to PrusaConnect and printed directly.
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    text = to_text(gf)
    if gf.source_format == "bgcode" and gf._bgcode_file_hdr is not None:
        data = _bgcode_reassemble(
            gf._bgcode_file_hdr,
            gf._bgcode_nongcode_blocks or [],
            text,
        )
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    else:
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Binary .bgcode support (private)
# ---------------------------------------------------------------------------

def _is_bgcode_file(path: str) -> bool:
    """Return True if *path* starts with the ``GCDE`` magic bytes."""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == _BGCODE_MAGIC
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Heatshrink decompression (pure-Python LZSS decoder)
# ---------------------------------------------------------------------------

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
                bit_buf = data[byte_pos] if byte_pos < input_len else 0
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
            for _ in range(count):
                if out_pos >= expected_size:
                    break
                c = window[(head - neg_offset) & mask]
                output[out_pos] = c
                window[head & mask] = c
                head += 1
                out_pos += 1

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

_MP_SPACE_RE = re.compile(r"(?<=[0-9.])(?=[A-Z])")
"""Insert whitespace before an uppercase axis/command letter that immediately
follows a digit or period.  MeatPack *no-spaces* mode strips inter-word
spaces; this regex restores them so that downstream ``parse_line()`` works."""


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
                if i < n:
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
            pending_raw = 1
        else:
            # Both nibbles are valid packed characters.
            out.append(table[lo])
            out.append(table[hi])

    # Flush any held character that was never followed by a raw byte
    # (shouldn't happen in well-formed data, but be safe).
    if held_char is not None:
        out.append(held_char)

    text = "".join(out)

    # MeatPack no-spaces mode strips whitespace between G-code words.
    # Re-insert spaces so that "G1X10Y20" becomes "G1 X10 Y20" and
    # parse_line() can split the command from its axes.
    return _MP_SPACE_RE.sub(" ", text)


def _bgcode_split(
    data: bytes,
) -> Tuple[bytes, List[bytes], List[Thumbnail], str]:
    """Parse a .bgcode byte string into its components.

    Returns
    -------
    file_hdr
        10-byte file prefix (magic + version + checksum_type).
    nongcode_blocks
        Raw bytes of every non-GCode block in original order.  These are
        suitable for verbatim re-embedding on reassembly and include
        thumbnail blocks.
    thumbnails
        Parsed :class:`Thumbnail` objects (subset of nongcode_blocks).
    gcode_text
        Concatenated UTF-8 G-code text from all GCode blocks.

    Raises :class:`ValueError` for invalid, truncated, or unsupported files.
    """
    if len(data) < 10:
        raise ValueError(f"Data too short ({len(data)} bytes) to be a .bgcode file")
    if data[:4] != _BGCODE_MAGIC:
        raise ValueError(
            f"Not a .bgcode file: expected magic {_BGCODE_MAGIC!r}, got {data[:4]!r}"
        )

    file_hdr = data[:10]
    pos = 10
    gcode_parts: List[str] = []
    nongcode_raws: List[bytes] = []
    thumbnails: List[Thumbnail] = []

    while pos < len(data):
        block_start = pos
        if pos + 8 > len(data):
            raise ValueError(f"Truncated block header at offset {pos}")

        btype, comp = struct.unpack_from("<HH", data, pos)
        uncomp_size, = struct.unpack_from("<I", data, pos + 4)

        if comp == _COMP_NONE:
            comp_size = uncomp_size
            hdr_len = 8
        elif comp in (_COMP_DEFLATE, _COMP_HEATSHRINK_11_4, _COMP_HEATSHRINK_12_4):
            if pos + 12 > len(data):
                raise ValueError(f"Truncated compressed block header at offset {pos}")
            comp_size, = struct.unpack_from("<I", data, pos + 8)
            hdr_len = 12
        else:
            raise ValueError(f"Unknown compression type {comp} at offset {pos}")

        params_len = 6 if btype == _BLK_THUMBNAIL else 2
        params_start = pos + hdr_len
        payload_start = params_start + params_len
        payload_end = payload_start + comp_size

        if payload_end + 4 > len(data):
            raise ValueError(
                f"Truncated block payload at offset {pos}: "
                f"need {payload_end + 4 - len(data)} more bytes"
            )

        stored_crc, = struct.unpack_from("<I", data, payload_end)
        computed_crc = zlib.crc32(data[pos:payload_end]) & 0xFFFFFFFF
        if computed_crc != stored_crc:
            raise ValueError(
                f"CRC32 mismatch in block type {btype} at offset {pos}: "
                f"computed {computed_crc:#010x}, stored {stored_crc:#010x}"
            )

        block_end = payload_end + 4
        raw_block = data[block_start:block_end]
        payload = data[payload_start:payload_end]

        if btype == _BLK_GCODE:
            enc, = struct.unpack_from("<H", data, params_start)
            if comp == _COMP_NONE:
                raw_payload = payload
            elif comp == _COMP_DEFLATE:
                try:
                    raw_payload = zlib.decompress(payload)
                except zlib.error as exc:
                    raise ValueError(
                        f"DEFLATE decompression failed at offset {pos}: {exc}"
                    ) from exc
            elif comp == _COMP_HEATSHRINK_11_4:
                raw_payload = _heatshrink_decompress(payload, 11, 4, uncomp_size)
            elif comp == _COMP_HEATSHRINK_12_4:
                raw_payload = _heatshrink_decompress(payload, 12, 4, uncomp_size)
            else:
                raise ValueError(
                    f"Unsupported GCode block compression {comp} at offset {pos}"
                )
            if enc == _ENC_RAW:
                gcode_parts.append(raw_payload.decode("utf-8"))
            elif enc in (_ENC_MEATPACK, _ENC_MEATPACK_COMMENTS):
                gcode_parts.append(_meatpack_decode(raw_payload))
            else:
                raise ValueError(
                    f"Unsupported GCode block encoding {enc} at offset {pos}"
                )

        elif btype == _BLK_THUMBNAIL:
            params_bytes = data[params_start:payload_start]
            if comp == _COMP_NONE:
                img_data = payload
            elif comp == _COMP_DEFLATE:
                try:
                    img_data = zlib.decompress(payload)
                except zlib.error as exc:
                    raise ValueError(
                        f"DEFLATE decompression of thumbnail failed at offset {pos}: {exc}"
                    ) from exc
            elif comp == _COMP_HEATSHRINK_11_4:
                img_data = _heatshrink_decompress(payload, 11, 4, uncomp_size)
            elif comp == _COMP_HEATSHRINK_12_4:
                img_data = _heatshrink_decompress(payload, 12, 4, uncomp_size)
            else:
                img_data = payload  # store as-is for unknown compression
            thumbnails.append(Thumbnail(params=params_bytes, data=img_data, _raw_block=raw_block))
            nongcode_raws.append(raw_block)

        else:
            nongcode_raws.append(raw_block)

        pos = block_end

    if not gcode_parts:
        raise ValueError("No GCode blocks found in .bgcode data")

    return file_hdr, nongcode_raws, thumbnails, "".join(gcode_parts)


def _bgcode_reassemble(
    file_hdr: bytes,
    nongcode_blocks: List[bytes],
    gcode_text: str,
) -> bytes:
    """Rebuild a .bgcode byte string from its components.

    A single new GCode block (COMP_NONE, ENC_RAW) is appended after all
    non-GCode blocks, matching the libbgcode convention (metadata before
    G-code).
    """
    payload = gcode_text.encode("utf-8")
    hdr    = struct.pack("<HHI", _BLK_GCODE, _COMP_NONE, len(payload))
    params = struct.pack("<H", _ENC_RAW)
    cksum  = zlib.crc32(hdr) & 0xFFFFFFFF
    cksum  = zlib.crc32(params, cksum) & 0xFFFFFFFF
    cksum  = zlib.crc32(payload, cksum) & 0xFFFFFFFF
    gcode_block = hdr + params + payload + struct.pack("<I", cksum)
    return file_hdr + b"".join(nongcode_blocks) + gcode_block


def _load_bgcode(data: bytes) -> GCodeFile:
    """Create a :class:`GCodeFile` from raw .bgcode bytes."""
    file_hdr, nongcode_blocks, thumbnails, gcode_text = _bgcode_split(data)
    return GCodeFile(
        lines=parse_lines(gcode_text),
        thumbnails=thumbnails,
        source_format="bgcode",
        _bgcode_file_hdr=file_hdr,
        _bgcode_nongcode_blocks=nongcode_blocks,
    )


# ---------------------------------------------------------------------------
# Arc geometry helpers
# ---------------------------------------------------------------------------

def _arc_center(state: ModalState, words: Dict[str, float]) -> Tuple[float, float]:
    """Compute the absolute arc centre from I/J words and modal state."""
    I = words.get("I", 0.0)
    J = words.get("J", 0.0)
    if state.ij_relative:
        return (state.x + I, state.y + J)
    return (I, J)


def _arc_end_abs(state: ModalState, words: Dict[str, float]) -> Tuple[float, float]:
    """Compute the absolute arc endpoint from words and modal state."""
    if state.abs_xy:
        return words.get("X", state.x), words.get("Y", state.y)
    return state.x + words.get("X", 0.0), state.y + words.get("Y", 0.0)


def _sweep_angle(a0: float, a1: float, cw: bool) -> float:
    """Compute the signed sweep angle from *a0* to *a1* for CW or CCW arcs."""
    da = a1 - a0
    while da <= -math.pi: da += 2 * math.pi
    while da >   math.pi: da -= 2 * math.pi
    if cw and da > 0:     da -= 2 * math.pi
    if not cw and da < 0: da += 2 * math.pi
    return da


def linearize_arc_points(
    state: ModalState,
    words: Dict[str, float],
    cw: bool,
    seg_mm: float = DEFAULT_ARC_SEG_MM,
    max_deg: float = DEFAULT_ARC_MAX_DEG,
) -> List[Tuple[float, float]]:
    """Convert a G2/G3 arc into a list of ``(x, y)`` interpolated points.

    The last point is exactly the arc endpoint.  Returns at least one point.

    Parameters
    ----------
    state:   Current modal state (provides start position and IJ mode).
    words:   Parsed axis words for the arc command.
    cw:      True for G2 (clockwise), False for G3 (counter-clockwise).
    seg_mm:  Maximum chord length (mm) per segment.
    max_deg: Maximum sweep angle (degrees) per segment.
    """
    x0, y0 = state.x, state.y
    x1, y1 = _arc_end_abs(state, words)
    cx, cy = _arc_center(state, words)

    r0 = math.hypot(x0 - cx, y0 - cy)
    r1 = math.hypot(x1 - cx, y1 - cy)
    r  = 0.5 * (r0 + r1) if (r0 > 0 and r1 > 0) else max(r0, r1)

    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    da = _sweep_angle(a0, a1, cw=cw)

    # Full-circle arc: start == end gives da ≈ 0; detect by non-zero radius
    if abs(da) < EPS and r > EPS:
        da = -2 * math.pi if cw else 2 * math.pi

    arc_len = abs(da) * r if r > 0 else math.hypot(x1 - x0, y1 - y0)
    max_rad = math.radians(max_deg) if max_deg > 0 else abs(da)
    steps_len = int(math.ceil(arc_len / max(seg_mm, 1e-6))) if seg_mm > 0 else 1
    steps_ang = int(math.ceil(abs(da) / max(max_rad, 1e-9))) if max_deg > 0 else 1
    steps = max(1, steps_len, steps_ang)

    pts: List[Tuple[float, float]] = []
    for i in range(1, steps + 1):
        t  = i / steps
        ai = a0 + da * t
        xi = cx + r * math.cos(ai)
        yi = cy + r * math.sin(ai)
        if i == steps:
            xi, yi = x1, y1
        pts.append((xi, yi))
    return pts


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------

def linearize_arcs(
    lines: List[GCodeLine],
    seg_mm: float = DEFAULT_ARC_SEG_MM,
    max_deg: float = DEFAULT_ARC_MAX_DEG,
    xy_decimals: int = DEFAULT_XY_DECIMALS,
    other_decimals: int = DEFAULT_OTHER_DECIMALS,
    initial_state: Optional[ModalState] = None,
) -> List[GCodeLine]:
    """Replace all G2/G3 arcs with equivalent G1 segments.

    E is distributed proportionally across segments; feedrate, Z word,
    and comment are preserved on the first emitted segment only.  Returns
    a new list; the input is unchanged.

    Note: helical arcs (G2/G3 with a Z word) are not interpolated — the Z
    value is placed on the first segment so the state advances correctly,
    but intermediate segments are at the original Z height.  For purely
    planar FFF G-code this is never an issue.
    """
    result: List[GCodeLine] = []
    state = initial_state.copy() if initial_state else ModalState()

    for line in lines:
        if not line.is_arc:
            result.append(line)
            advance_state(state, line)
            continue

        words = line.words
        cw  = (line.command.upper() == "G2")
        pts = linearize_arc_points(state, words, cw, seg_mm, max_deg)
        n   = len(pts)

        has_e = "E" in words
        e0    = state.e
        if has_e:
            if state.abs_e:
                e_end = words["E"]
                dE    = e_end - e0
            else:
                dE    = words["E"]
                e_end = e0 + dE
        else:
            dE    = 0.0
            e_end = e0

        has_f  = "F" in words
        f_word = words.get("F")
        has_z  = "Z" in words
        z_word = words.get("Z")

        # Per-segment E for relative extrusion: distribute dE across segments
        # in output precision and close out on the last segment.
        e_accum = 0.0
        per_e   = round(dE / n, other_decimals) if (has_e and not state.abs_e and n > 0) else 0.0

        for i, (xi, yi) in enumerate(pts, start=1):
            parts = [
                "G1",
                f"X{fmt_axis('X', xi, xy_decimals, other_decimals)}",
                f"Y{fmt_axis('Y', yi, xy_decimals, other_decimals)}",
            ]

            if has_e:
                if state.abs_e:
                    t  = i / n
                    ei = (e0 + dE * t) if i < n else e_end
                else:
                    if i < n:
                        ei       = per_e
                        e_accum += ei
                    else:
                        ei = round(dE - e_accum, other_decimals)
                parts.append(f"E{fmt_axis('E', ei, xy_decimals, other_decimals)}")

            # Z and F are placed on the first segment only (non-helical convention)
            if has_z and z_word is not None and i == 1:
                parts.append(f"Z{fmt_axis('Z', z_word, xy_decimals, other_decimals)}")
            if has_f and f_word is not None and i == 1:
                parts.append(f"F{fmt_axis('F', f_word, xy_decimals, other_decimals)}")

            raw = " ".join(parts)
            seg_comment = (line.comment if i == 1 else "")
            if seg_comment:
                raw += " " + seg_comment.lstrip()

            new_words: Dict[str, float] = {"X": xi, "Y": yi}
            if has_e:
                new_words["E"] = ei  # type: ignore[possibly-undefined]
            if has_z and z_word is not None and i == 1:
                new_words["Z"] = z_word
            if has_f and f_word is not None and i == 1:
                new_words["F"] = f_word

            result.append(GCodeLine(
                raw=raw,
                command="G1",
                words=new_words,
                comment=seg_comment,
            ))

        # Advance state to arc endpoint
        if state.abs_xy:
            state.x = words.get("X", state.x)
            state.y = words.get("Y", state.y)
            state.z = words.get("Z", state.z)
        else:
            state.x += words.get("X", 0.0)
            state.y += words.get("Y", 0.0)
            state.z += words.get("Z", 0.0)
        if has_e:
            state.e = e_end
        if has_f and f_word is not None:
            state.f = f_word

    return result


def apply_xy_transform(
    lines: List[GCodeLine],
    fn: Callable[[float, float], Tuple[float, float]],
    *,
    xy_decimals: int = DEFAULT_XY_DECIMALS,
    other_decimals: int = DEFAULT_OTHER_DECIMALS,
    initial_state: Optional[ModalState] = None,
    skip_negative_y: bool = True,
) -> List[GCodeLine]:
    """Apply an arbitrary XY transform to all G0/G1 move endpoints.

    *fn* receives ``(x, y)`` in original absolute coordinates and returns
    ``(new_x, new_y)``.  Non-move lines are passed through unchanged.

    Arcs (G2/G3) are **not** transformed — call :func:`linearize_arcs`
    first when the input may contain arc commands.

    Modal state is tracked internally; position updates use the **original**
    (pre-transform) coordinates so that relative moves and arc centres
    continue to function correctly for any downstream operations.

    Raises :class:`ValueError` when a move with X/Y words is encountered
    in relative XY mode (G91), as the transform requires absolute coords.

    When *skip_negative_y* is ``True`` (the default), moves whose effective
    absolute Y position is negative are passed through untransformed.  This
    prevents PrusaSlicer purge-line and wipe moves (which dip below Y=0)
    from being modified.
    """
    result: List[GCodeLine] = []
    state = initial_state.copy() if initial_state else ModalState()

    for line in lines:
        if line.is_move and ("X" in line.words or "Y" in line.words):
            if not state.abs_xy:
                raise ValueError(
                    "apply_xy_transform: relative XY (G91) is not supported. "
                    "Ensure G90 is active or pre-process with to_absolute_xy()."
                )
            x_t = line.words.get("X", state.x)
            y_t = line.words.get("Y", state.y)

            if skip_negative_y and y_t < 0:
                result.append(line)
                advance_state(state, line)
                continue
            xs, ys = fn(x_t, y_t)

            code, comment = split_comment(line.raw)
            new_code = replace_or_append(code, "X", xs, xy_decimals, other_decimals)
            new_code = replace_or_append(new_code, "Y", ys, xy_decimals, other_decimals)
            for ax in ("Z", "E", "F"):
                if ax in line.words:
                    new_code = replace_or_append(
                        new_code, ax, line.words[ax], xy_decimals, other_decimals
                    )

            new_raw = new_code.rstrip() + ("" if not comment else " " + comment.lstrip())
            new_words = dict(line.words)
            new_words["X"] = xs
            new_words["Y"] = ys

            result.append(GCodeLine(
                raw=new_raw,
                command=line.command,
                words=new_words,
                comment=comment,
            ))

            # State tracks original (untransformed) position
            # (state.abs_xy is guaranteed True here — G91 raises above)
            state.x = x_t
            state.y = y_t
            if "Z" in line.words:
                state.z = line.words["Z"]
            if "E" in line.words:
                state.e = (line.words["E"] if state.abs_e
                           else state.e + line.words["E"])
            if "F" in line.words:
                state.f = line.words["F"]
        else:
            result.append(line)
            advance_state(state, line)

    return result


def apply_skew(
    lines: List[GCodeLine],
    skew_deg: float,
    y_ref: float = 0.0,
    *,
    xy_decimals: int = DEFAULT_XY_DECIMALS,
    other_decimals: int = DEFAULT_OTHER_DECIMALS,
    initial_state: Optional[ModalState] = None,
    skip_negative_y: bool = True,
) -> List[GCodeLine]:
    """Apply XY skew correction to all G0/G1 move endpoints.

    Transform model (Marlin M852-compatible)::

        x' = x + (y - y_ref) * tan(skew_deg)
        y' = y

    Arcs must be linearized before calling this function — call
    :func:`linearize_arcs` first if the G-code contains G2/G3 commands.

    Parameters
    ----------
    skew_deg: Skew angle in degrees.
    y_ref:    Y reference for the shear pivot; reduces displacement for
              prints centred away from Y = 0.
    """
    k = math.tan(math.radians(skew_deg))
    return apply_xy_transform(
        lines,
        lambda x, y: (x + (y - y_ref) * k, y),
        xy_decimals=xy_decimals,
        other_decimals=other_decimals,
        initial_state=initial_state,
        skip_negative_y=skip_negative_y,
    )


def translate_xy(
    lines: List[GCodeLine],
    dx: float,
    dy: float,
    *,
    xy_decimals: int = DEFAULT_XY_DECIMALS,
    other_decimals: int = DEFAULT_OTHER_DECIMALS,
    initial_state: Optional[ModalState] = None,
    skip_negative_y: bool = True,
) -> List[GCodeLine]:
    """Translate all G0/G1 XY move endpoints by ``(dx, dy)``."""
    return apply_xy_transform(
        lines,
        lambda x, y: (x + dx, y + dy),
        xy_decimals=xy_decimals,
        other_decimals=other_decimals,
        initial_state=initial_state,
        skip_negative_y=skip_negative_y,
    )


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def compute_bounds(
    lines: List[GCodeLine],
    *,
    extruding_only: bool = False,
    include_arcs: bool = True,
    skip_negative_y: bool = False,
    arc_seg_mm: float = DEFAULT_ARC_SEG_MM,
    arc_max_deg: float = DEFAULT_ARC_MAX_DEG,
    initial_state: Optional[ModalState] = None,
) -> Bounds:
    """Compute XY/Z bounding box from a sequence of G-code lines.

    Parameters
    ----------
    extruding_only: Include only endpoints/points from extruding moves.
    include_arcs:   Linearize G2/G3 arcs and include their sample points.
    skip_negative_y: Exclude moves whose effective Y position is negative.
    """
    bounds = Bounds()
    state  = initial_state.copy() if initial_state else ModalState()

    for line in lines:
        if line.is_move:
            if state.abs_xy:
                x1 = line.words.get("X", state.x)
                y1 = line.words.get("Y", state.y)
                z1 = line.words.get("Z", state.z)
            else:
                x1 = state.x + line.words.get("X", 0.0)
                y1 = state.y + line.words.get("Y", 0.0)
                z1 = state.z + line.words.get("Z", 0.0)

            if "X" in line.words or "Y" in line.words:
                is_ext = False
                if "E" in line.words:
                    e_w    = line.words["E"]
                    is_ext = (e_w > state.e) if state.abs_e else (e_w > 0.0)
                skip_y = skip_negative_y and y1 < 0
                if (not extruding_only or is_ext) and not skip_y:
                    bounds.expand(x1, y1)

            if "Z" in line.words:
                bounds.expand_z(z1)

            advance_state(state, line)

        elif line.is_arc and include_arcs:
            words  = line.words
            cw     = (line.command.upper() == "G2")
            pts    = linearize_arc_points(state, words, cw, arc_seg_mm, arc_max_deg)
            is_ext = False
            if "E" in words:
                e_w    = words["E"]
                is_ext = (e_w > state.e) if state.abs_e else (e_w > 0.0)
            if not extruding_only or is_ext:
                for xi, yi in pts:
                    if not (skip_negative_y and yi < 0):
                        bounds.expand(xi, yi)
            advance_state(state, line)

        else:
            advance_state(state, line)

    return bounds


def compute_stats(
    lines: List[GCodeLine],
    initial_state: Optional[ModalState] = None,
) -> GCodeStats:
    """Compute a statistical summary of a G-code line sequence.

    Returns a :class:`GCodeStats` with move counts, bounds, layer heights,
    feedrate list, and total extrusion.
    """
    stats    = GCodeStats()
    state    = initial_state.copy() if initial_state else ModalState()
    seen_z:  List[float] = []
    seen_f:  List[float] = []
    seen_f_set: set = set()   # for O(1) global uniqueness check
    last_z:  Optional[float] = None

    for line in lines:
        stats.total_lines += 1

        if line.is_blank:
            stats.blank_lines += 1
            advance_state(state, line)
            continue

        if not line.command and line.comment:
            stats.comment_only_lines += 1
            advance_state(state, line)
            continue

        if line.is_move:
            stats.move_count += 1
            words = line.words

            if state.abs_xy:
                x1 = words.get("X", state.x)
                y1 = words.get("Y", state.y)
                z1 = words.get("Z", state.z)
            else:
                x1 = state.x + words.get("X", 0.0)
                y1 = state.y + words.get("Y", 0.0)
                z1 = state.z + words.get("Z", 0.0)

            if "X" in words or "Y" in words:
                stats.bounds.expand(x1, y1)

            if "Z" in words:
                stats.bounds.expand_z(z1)
                if last_z is None or abs(z1 - last_z) > EPS:
                    seen_z.append(z1)
                    last_z = z1

            if "F" in words:
                f = words["F"]
                if f not in seen_f_set:
                    seen_f.append(f)
                    seen_f_set.add(f)

            is_ext = False
            if "E" in words:
                e_word = words["E"]
                if state.abs_e:
                    dE     = e_word - state.e
                    is_ext = dE > 0
                    if is_ext:
                        stats.total_extrusion += dE
                    elif dE < -EPS:
                        stats.retract_count += 1
                else:
                    is_ext = e_word > 0.0
                    if is_ext:
                        stats.total_extrusion += e_word
                    elif e_word < -EPS:
                        stats.retract_count += 1

            if is_ext:
                stats.extrude_count += 1
            else:
                stats.travel_count += 1

        elif line.is_arc:
            stats.arc_count += 1
            words = line.words
            cw    = (line.command.upper() == "G2")
            pts   = linearize_arc_points(state, words, cw)

            for xi, yi in pts:
                stats.bounds.expand(xi, yi)

            if "Z" in words:
                if state.abs_xy:
                    z1 = words["Z"]
                else:
                    z1 = state.z + words["Z"]
                stats.bounds.expand_z(z1)
                if last_z is None or abs(z1 - last_z) > EPS:
                    seen_z.append(z1)
                    last_z = z1

            if "F" in words:
                f = words["F"]
                if f not in seen_f_set:
                    seen_f.append(f)
                    seen_f_set.add(f)

            is_ext = False
            if "E" in words:
                e_word = words["E"]
                if state.abs_e:
                    dE     = e_word - state.e
                    is_ext = dE > 0
                    if is_ext:
                        stats.total_extrusion += dE
                    elif dE < -EPS:
                        stats.retract_count += 1
                else:
                    is_ext = e_word > 0.0
                    if is_ext:
                        stats.total_extrusion += e_word
                    elif e_word < -EPS:
                        stats.retract_count += 1

            if is_ext:
                stats.extrude_count += 1
            else:
                stats.travel_count += 1

        advance_state(state, line)

    stats.z_heights = seen_z
    stats.feedrates  = seen_f
    return stats


# ===========================================================================
# EXTENSIONS — Planar G-Code Toolkit
# (Engineering Master Document, Hyiger/gcode-lib, sections 4–9)
# ===========================================================================

# ---------------------------------------------------------------------------
# Additional constants
# ---------------------------------------------------------------------------

# Minimal valid .bgcode v2 file header: magic + version(uint32) + checksum_type(uint16)
_BGCODE_VERSION = 2
_BGCODE_CHECKSUM_CRC32 = 1
_BGCODE_FILE_HDR_V2: bytes = (
    _BGCODE_MAGIC
    + struct.pack("<I", _BGCODE_VERSION)
    + struct.pack("<H", _BGCODE_CHECKSUM_CRC32)
)

# ---------------------------------------------------------------------------
# §4.1 — to_absolute_xy: G91 → G90 normalisation
# ---------------------------------------------------------------------------

def to_absolute_xy(
    lines: List[GCodeLine],
    initial_state: Optional[ModalState] = None,
    *,
    xy_decimals: int = DEFAULT_XY_DECIMALS,
    other_decimals: int = DEFAULT_OTHER_DECIMALS,
) -> List[GCodeLine]:
    """Convert all G91 (relative XY) motion to absolute G90 equivalents.

    G91 mode-switch lines are removed from the output.  Relative G0/G1 and
    G2/G3 moves are rewritten with absolute X/Y/Z coordinates computed from
    the running modal state.  Z, E, F, I, J, K words are handled correctly:

    * Z is converted to absolute along with X/Y.
    * E and F are preserved verbatim (relative E / M83 mode is untouched).
    * I/J for arcs are left as-is; they are arc-centre *offsets* from the
      current position and do not need conversion when the mode is G91.1
      (the default).

    The returned list is safe to pass directly to :func:`apply_xy_transform`,
    :func:`apply_skew`, or :func:`translate_xy`.

    Parameters
    ----------
    lines:         Input G-code line list (may mix G90 and G91 sections).
    initial_state: Optional starting :class:`ModalState`.
    xy_decimals:   Decimal places for output X/Y values.
    other_decimals: Decimal places for output Z values.
    """
    result: List[GCodeLine] = []
    state = initial_state.copy() if initial_state else ModalState()
    found_g91 = False

    for line in lines:
        cmd = line.command

        # Drop G91 mode-switch lines; everything in the output is absolute.
        if cmd == "G91":
            found_g91 = True
            advance_state(state, line)
            continue

        # Convert relative move/arc endpoints to absolute.
        if (line.is_move or line.is_arc) and not state.abs_xy:
            words = line.words
            x_abs = state.x + words.get("X", 0.0)
            y_abs = state.y + words.get("Y", 0.0)
            z_abs = state.z + words.get("Z", 0.0)

            code, comment = split_comment(line.raw)
            new_code = code
            if "X" in words:
                new_code = replace_or_append(new_code, "X", x_abs, xy_decimals, other_decimals)
            if "Y" in words:
                new_code = replace_or_append(new_code, "Y", y_abs, xy_decimals, other_decimals)
            if "Z" in words:
                new_code = replace_or_append(new_code, "Z", z_abs, xy_decimals, other_decimals)
            # I, J, K, E, F — preserved verbatim.

            new_raw = new_code.rstrip() + ("" if not comment else " " + comment.lstrip())
            new_words = dict(words)
            if "X" in words: new_words["X"] = x_abs
            if "Y" in words: new_words["Y"] = y_abs
            if "Z" in words: new_words["Z"] = z_abs

            result.append(GCodeLine(raw=new_raw, command=cmd, words=new_words, comment=comment))

            # Update state with the absolute position so subsequent lines
            # continue to track correctly.
            state.x = x_abs
            state.y = y_abs
            state.z = z_abs
            if "E" in words:
                state.e = words["E"] if state.abs_e else state.e + words["E"]
            if "F" in words:
                state.f = words["F"]
        else:
            result.append(line)
            advance_state(state, line)

    # If we dropped any G91 lines, prepend an explicit G90 so printers that
    # power on in relative mode are handled safely.
    if found_g91:
        result.insert(0, parse_line("G90"))

    return result


# ---------------------------------------------------------------------------
# §4.2 — translate_xy_allow_arcs: translate without prior linearization
# ---------------------------------------------------------------------------

def translate_xy_allow_arcs(
    lines: List[GCodeLine],
    dx: float,
    dy: float,
    *,
    xy_decimals: int = DEFAULT_XY_DECIMALS,
    other_decimals: int = DEFAULT_OTHER_DECIMALS,
    initial_state: Optional[ModalState] = None,
    skip_negative_y: bool = True,
) -> List[GCodeLine]:
    """Translate XY by ``(dx, dy)``, handling G2/G3 arcs natively.

    Unlike :func:`translate_xy`, this function does **not** require arcs to
    be linearized first.  For a pure translation:

    * G0/G1 endpoints are shifted by ``(dx, dy)``.
    * G2/G3 endpoints are shifted by ``(dx, dy)``.
    * I/J offsets are **unchanged** when using G91.1 (relative arc centre,
      the default), because I/J are offsets from the (already-shifted)
      current position.
    * I/J are also shifted by ``(dx, dy)`` when using G90.1 (absolute arc
      centre), since the absolute centre moves with the translation.

    Raises :class:`ValueError` if a move with X/Y words is encountered while
    in G91 (relative XY) mode.  Call :func:`to_absolute_xy` first if the
    file uses G91 sections.

    When *skip_negative_y* is ``True`` (the default), moves whose effective
    absolute Y position is negative are passed through untransformed.

    Parameters
    ----------
    lines:         Input G-code line list.
    dx, dy:        Translation offsets in mm.
    xy_decimals:   Decimal places for X/Y output.
    other_decimals: Decimal places for other axes.
    initial_state: Optional starting :class:`ModalState`.
    skip_negative_y: Skip moves at Y < 0 (default ``True``).
    """
    result: List[GCodeLine] = []
    state = initial_state.copy() if initial_state else ModalState()

    for line in lines:
        words = line.words
        has_xy = "X" in words or "Y" in words

        if (line.is_move or line.is_arc) and has_xy:
            if not state.abs_xy:
                raise ValueError(
                    "translate_xy_allow_arcs: relative XY (G91) is not supported. "
                    "Call to_absolute_xy() first."
                )

            y_eff = words.get("Y", state.y)
            if skip_negative_y and y_eff < 0:
                result.append(line)
                advance_state(state, line)
                continue

            code, comment = split_comment(line.raw)
            new_code = code

            if "X" in words:
                new_code = replace_or_append(
                    new_code, "X", words["X"] + dx, xy_decimals, other_decimals
                )
            if "Y" in words:
                new_code = replace_or_append(
                    new_code, "Y", words["Y"] + dy, xy_decimals, other_decimals
                )

            new_words = dict(words)
            if "X" in words: new_words["X"] = words["X"] + dx
            if "Y" in words: new_words["Y"] = words["Y"] + dy

            # For arcs with absolute arc-centre mode (G90.1), I/J must also shift.
            if line.is_arc and not state.ij_relative:
                if "I" in words:
                    new_words["I"] = words["I"] + dx
                    new_code = replace_or_append(
                        new_code, "I", new_words["I"], xy_decimals, other_decimals
                    )
                if "J" in words:
                    new_words["J"] = words["J"] + dy
                    new_code = replace_or_append(
                        new_code, "J", new_words["J"], xy_decimals, other_decimals
                    )

            new_raw = new_code.rstrip() + ("" if not comment else " " + comment.lstrip())

            result.append(GCodeLine(
                raw=new_raw, command=line.command, words=new_words, comment=comment
            ))
        else:
            result.append(line)

        advance_state(state, line)

    return result


# ---------------------------------------------------------------------------
# §4.2b — rotate_xy: rotate XY with optional bed boundary validation
# ---------------------------------------------------------------------------

def rotate_xy(
    lines: List[GCodeLine],
    angle_deg: float,
    *,
    pivot_x: Optional[float] = None,
    pivot_y: Optional[float] = None,
    bed_min_x: Optional[float] = None,
    bed_max_x: Optional[float] = None,
    bed_min_y: Optional[float] = None,
    bed_max_y: Optional[float] = None,
    margin: float = 0.0,
    xy_decimals: int = DEFAULT_XY_DECIMALS,
    other_decimals: int = DEFAULT_OTHER_DECIMALS,
    initial_state: Optional[ModalState] = None,
    skip_negative_y: bool = True,
) -> List[GCodeLine]:
    """Rotate XY coordinates by *angle_deg* degrees (counter-clockwise positive).

    Handles G0/G1 moves **and** G2/G3 arcs natively — no prior
    linearization required.  The I/J arc-centre offsets are rotated by the
    same angle so arcs remain geometrically correct.

    When *skip_negative_y* is ``True`` (the default), moves whose effective
    absolute Y position is negative are passed through untransformed.  The
    default pivot and bed validation also use extruding-only bounds so that
    purge-line moves do not affect rotation centre or boundary checks.

    Parameters
    ----------
    lines:         Input G-code line list.
    angle_deg:     Rotation angle in degrees.  Positive = counter-clockwise.
    pivot_x, pivot_y:
        Centre of rotation.  When ``None`` (the default), the centroid of
        the print's bounding box is used.
    bed_min_x, bed_max_x, bed_min_y, bed_max_y:
        If **all four** are provided the rotated print is validated against
        the bed area.  If the rotated print fits, it is re-centred within
        the available area.  If it does not fit (even after re-centring),
        :class:`ValueError` is raised.
    margin:        Inset applied to all four bed edges (mm).
    xy_decimals:   Decimal places for X/Y output.
    other_decimals: Decimal places for other axes.
    initial_state: Optional starting :class:`ModalState`.
    skip_negative_y: Skip moves at Y < 0 (default ``True``).

    Raises
    ------
    ValueError
        If relative XY mode (G91) is encountered, or if the rotated print
        exceeds the bed area after re-centring.
    """
    theta = math.radians(angle_deg)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)

    # --- Determine pivot ------------------------------------------------
    if pivot_x is None or pivot_y is None:
        bounds = compute_bounds(
            lines, skip_negative_y=skip_negative_y,
            initial_state=initial_state,
        )
        if not bounds.valid:
            return list(lines)          # nothing to rotate
        if pivot_x is None:
            pivot_x = bounds.center_x
        if pivot_y is None:
            pivot_y = bounds.center_y

    # --- Helper: rotate a point around the pivot ------------------------
    def _rot(x: float, y: float) -> Tuple[float, float]:
        dx = x - pivot_x
        dy = y - pivot_y
        return (pivot_x + dx * cos_t - dy * sin_t,
                pivot_y + dx * sin_t + dy * cos_t)

    # --- Helper: rotate a relative vector (I/J offset) ------------------
    def _rot_vec(i: float, j: float) -> Tuple[float, float]:
        return (i * cos_t - j * sin_t,
                i * sin_t + j * cos_t)

    # --- Rotation pass --------------------------------------------------
    result: List[GCodeLine] = []
    state = initial_state.copy() if initial_state else ModalState()

    for line in lines:
        words = line.words
        has_xy = "X" in words or "Y" in words

        if (line.is_move or line.is_arc) and has_xy:
            if not state.abs_xy:
                raise ValueError(
                    "rotate_xy: relative XY (G91) is not supported. "
                    "Call to_absolute_xy() first."
                )

            # Resolve full absolute position then rotate
            x_abs = words.get("X", state.x)
            y_abs = words.get("Y", state.y)

            if skip_negative_y and y_abs < 0:
                result.append(line)
                advance_state(state, line)
                continue

            xr, yr = _rot(x_abs, y_abs)

            code, comment = split_comment(line.raw)
            new_code = code
            new_words = dict(words)

            if "X" in words:
                new_words["X"] = xr
                new_code = replace_or_append(new_code, "X", xr, xy_decimals, other_decimals)
            if "Y" in words:
                new_words["Y"] = yr
                new_code = replace_or_append(new_code, "Y", yr, xy_decimals, other_decimals)

            # Rotate arc I/J offsets
            if line.is_arc:
                i_val = words.get("I", 0.0)
                j_val = words.get("J", 0.0)
                if state.ij_relative:
                    # Relative IJ: rotate the offset vector
                    ir, jr = _rot_vec(i_val, j_val)
                else:
                    # Absolute IJ: rotate as a point around the pivot
                    ir, jr = _rot(i_val, j_val)
                if "I" in words or not state.ij_relative:
                    new_words["I"] = ir
                    new_code = replace_or_append(new_code, "I", ir, xy_decimals, other_decimals)
                if "J" in words or not state.ij_relative:
                    new_words["J"] = jr
                    new_code = replace_or_append(new_code, "J", jr, xy_decimals, other_decimals)

            new_raw = new_code.rstrip() + ("" if not comment else " " + comment.lstrip())
            result.append(GCodeLine(
                raw=new_raw, command=line.command, words=new_words, comment=comment
            ))
        else:
            result.append(line)

        advance_state(state, line)

    # --- Bed boundary validation and re-centring ------------------------
    has_bed = (bed_min_x is not None and bed_max_x is not None
               and bed_min_y is not None and bed_max_y is not None)
    if has_bed:
        rb = compute_bounds(result, skip_negative_y=skip_negative_y)
        if rb.valid:
            avail_x = (bed_max_x - bed_min_x) - 2 * margin
            avail_y = (bed_max_y - bed_min_y) - 2 * margin
            if rb.width > avail_x + EPS or rb.height > avail_y + EPS:
                raise ValueError(
                    f"rotate_xy: rotated print ({rb.width:.2f} x {rb.height:.2f} mm) "
                    f"exceeds available bed area ({avail_x:.2f} x {avail_y:.2f} mm)."
                )
            bed_cx = (bed_min_x + bed_max_x) / 2
            bed_cy = (bed_min_y + bed_max_y) / 2
            dx = bed_cx - rb.center_x
            dy = bed_cy - rb.center_y
            if abs(dx) > EPS or abs(dy) > EPS:
                result = translate_xy_allow_arcs(
                    result, dx, dy,
                    xy_decimals=xy_decimals,
                    other_decimals=other_decimals,
                    skip_negative_y=skip_negative_y,
                )

    return result


# ---------------------------------------------------------------------------
# §4.3 — Out-of-bounds detection
# ---------------------------------------------------------------------------

@dataclass
class OOBHit:
    """A move endpoint that lies outside the allowed bed polygon.

    Attributes
    ----------
    line_number:      0-based index of the offending line in the input list.
    x, y:             Absolute coordinates of the out-of-bounds point.
    distance_outside: Distance (mm) from the point to the nearest polygon edge.
    """
    line_number: int
    x: float
    y: float
    distance_outside: float


def _point_in_polygon(x: float, y: float, polygon: List[Tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test (Jordan curve theorem)."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _dist_to_segment(
    px: float, py: float,
    ax: float, ay: float,
    bx: float, by: float,
) -> float:
    """Minimum distance from point ``(px, py)`` to line segment ``(ax,ay)–(bx,by)``."""
    dx, dy = bx - ax, by - ay
    len_sq = dx * dx + dy * dy
    if len_sq < EPS * EPS:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _min_dist_to_polygon_boundary(
    x: float, y: float, polygon: List[Tuple[float, float]]
) -> float:
    """Minimum distance from ``(x, y)`` to any edge of *polygon*."""
    n = len(polygon)
    min_d = float("inf")
    for i in range(n):
        ax, ay = polygon[i]
        bx, by = polygon[(i + 1) % n]
        d = _dist_to_segment(x, y, ax, ay, bx, by)
        if d < min_d:
            min_d = d
    return min_d


def find_oob_moves(
    lines: List[GCodeLine],
    bed_polygon: List[Tuple[float, float]],
    initial_state: Optional[ModalState] = None,
) -> List[OOBHit]:
    """Return all G0/G1 move endpoints that fall outside *bed_polygon*.

    *bed_polygon* is a list of ``(x, y)`` vertices defining the printable
    area (e.g. ``[(0,0),(250,0),(250,220),(0,220)]`` for a 250×220 mm bed).
    The polygon is treated as closed (last vertex connects back to first).

    Parameters
    ----------
    lines:       G-code line list.  Both G90 (absolute) and G91 (relative)
                 XY modes are handled correctly.
    bed_polygon: Convex or concave polygon as ``[(x,y), …]``.
    initial_state: Optional starting :class:`ModalState`.
    """
    hits: List[OOBHit] = []
    state = initial_state.copy() if initial_state else ModalState()

    for idx, line in enumerate(lines):
        if line.is_move and ("X" in line.words or "Y" in line.words):
            if state.abs_xy:
                x = line.words.get("X", state.x)
                y = line.words.get("Y", state.y)
            else:
                x = state.x + line.words.get("X", 0.0)
                y = state.y + line.words.get("Y", 0.0)

            if not _point_in_polygon(x, y, bed_polygon):
                dist = _min_dist_to_polygon_boundary(x, y, bed_polygon)
                hits.append(OOBHit(line_number=idx, x=x, y=y, distance_outside=dist))

        advance_state(state, line)

    return hits


def max_oob_distance(
    lines: List[GCodeLine],
    bed_polygon: List[Tuple[float, float]],
    initial_state: Optional[ModalState] = None,
) -> float:
    """Return the maximum out-of-bounds distance across all moves.

    Returns ``0.0`` if all moves are within the bed polygon.
    """
    hits = find_oob_moves(lines, bed_polygon, initial_state)
    return max((h.distance_outside for h in hits), default=0.0)


# ---------------------------------------------------------------------------
# §4.4 — recenter_to_bed
# ---------------------------------------------------------------------------

def recenter_to_bed(
    lines: List[GCodeLine],
    bed_min_x: float,
    bed_max_x: float,
    bed_min_y: float,
    bed_max_y: float,
    margin: float = 0.0,
    mode: str = "center",
    *,
    xy_decimals: int = DEFAULT_XY_DECIMALS,
    other_decimals: int = DEFAULT_OTHER_DECIMALS,
    initial_state: Optional[ModalState] = None,
    skip_negative_y: bool = True,
) -> List[GCodeLine]:
    """Centre or scale a print to fit within the specified bed extents.

    Parameters
    ----------
    lines:     G-code line list (arcs should be linearized before calling
               in ``"fit"`` mode; ``"center"`` handles arcs natively).
    bed_min_x, bed_max_x, bed_min_y, bed_max_y:
               Bed bounds in mm.
    margin:    Clearance (mm) to leave around the scaled/centred print.
    mode:      ``"center"`` — translate the print centre to the bed centre
               (no scaling, arcs preserved).
               ``"fit"`` — uniformly scale the print to fill the available
               bed space (arcs must be linearized beforehand).
    xy_decimals, other_decimals: Output decimal precision.
    initial_state: Optional starting :class:`ModalState`.
    skip_negative_y: Skip moves at Y < 0 (default ``True``).

    Raises
    ------
    ValueError
        If ``mode`` is not ``"center"`` or ``"fit"``.
    ValueError
        If the print has zero width or height (in ``"fit"`` mode).
    """
    if mode not in ("center", "fit"):
        raise ValueError(f"recenter_to_bed: mode must be 'center' or 'fit', got {mode!r}")

    bounds = compute_bounds(
        lines, skip_negative_y=skip_negative_y, initial_state=initial_state,
    )
    if not bounds.valid:
        return list(lines)

    bed_cx = 0.5 * (bed_min_x + bed_max_x)
    bed_cy = 0.5 * (bed_min_y + bed_max_y)

    if mode == "center":
        dx = bed_cx - bounds.center_x
        dy = bed_cy - bounds.center_y
        return translate_xy_allow_arcs(
            lines, dx, dy,
            xy_decimals=xy_decimals,
            other_decimals=other_decimals,
            initial_state=initial_state,
            skip_negative_y=skip_negative_y,
        )

    # "fit" mode — uniform scale + translate.
    avail_x = (bed_max_x - bed_min_x) - 2.0 * margin
    avail_y = (bed_max_y - bed_min_y) - 2.0 * margin
    if avail_x <= 0 or avail_y <= 0:
        raise ValueError("recenter_to_bed: bed area after margin is zero or negative")
    if bounds.width < EPS or bounds.height < EPS:
        raise ValueError(
            "recenter_to_bed: print has zero width or height — cannot fit-scale"
        )

    scale = min(avail_x / bounds.width, avail_y / bounds.height)
    px_c, py_c = bounds.center_x, bounds.center_y

    def _scale_to_bed(x: float, y: float) -> Tuple[float, float]:
        xs = bed_cx + (x - px_c) * scale
        ys = bed_cy + (y - py_c) * scale
        return xs, ys

    return apply_xy_transform(
        lines, _scale_to_bed,
        xy_decimals=xy_decimals,
        other_decimals=other_decimals,
        initial_state=initial_state,
        skip_negative_y=skip_negative_y,
    )


# ---------------------------------------------------------------------------
# §4.5 — analyze_xy_transform: dry-run analysis
# ---------------------------------------------------------------------------

def analyze_xy_transform(
    lines: List[GCodeLine],
    transform_fn: Callable[[float, float], Tuple[float, float]],
    initial_state: Optional[ModalState] = None,
) -> Dict[str, object]:
    """Analyse the effect of *transform_fn* without modifying any lines.

    Iterates every G0/G1 move, applies *transform_fn*, and records the
    per-point displacement ``√(dx²+dy²)``.

    Returns a dict with keys:

    ``max_dx``
        Maximum absolute change in X across all move endpoints (mm).
    ``max_dy``
        Maximum absolute change in Y across all move endpoints (mm).
    ``max_displacement``
        Maximum Euclidean displacement ``√(Δx²+Δy²)`` (mm).
    ``line_number``
        0-based index of the line with the maximum displacement (or
        ``-1`` if no moves were found).
    ``move_count``
        Number of G0/G1 moves examined.
    """
    max_dx = 0.0
    max_dy = 0.0
    max_disp = 0.0
    worst_line = -1
    move_count = 0

    state = initial_state.copy() if initial_state else ModalState()

    for idx, line in enumerate(lines):
        if line.is_move and ("X" in line.words or "Y" in line.words) and state.abs_xy:
            x_orig = line.words.get("X", state.x)
            y_orig = line.words.get("Y", state.y)
            x_new, y_new = transform_fn(x_orig, y_orig)

            adx = abs(x_new - x_orig)
            ady = abs(y_new - y_orig)
            disp = math.hypot(adx, ady)
            move_count += 1

            if adx > max_dx: max_dx = adx
            if ady > max_dy: max_dy = ady
            if disp > max_disp:
                max_disp = disp
                worst_line = idx

        advance_state(state, line)

    return {
        "max_dx": max_dx,
        "max_dy": max_dy,
        "max_displacement": max_disp,
        "line_number": worst_line,
        "move_count": move_count,
    }


# ---------------------------------------------------------------------------
# §4.6 — Layer iterator and per-layer transform
# ---------------------------------------------------------------------------

def iter_layers(
    lines: List[GCodeLine],
    initial_state: Optional[ModalState] = None,
) -> Iterator[Tuple[float, List[GCodeLine]]]:
    """Yield ``(z_height, layer_lines)`` for each distinct Z level.

    A new layer group is started whenever a move changes the current Z height.
    The line that causes the Z change is included at the **start** of the new
    group (i.e. it belongs to the layer it moves *to*, not the one it left).
    The final group is always yielded, even if no Z change follows it.

    The first group has ``z_height`` equal to the initial state's Z (default
    0.0), and contains all lines before the first Z change.

    Parameters
    ----------
    lines:         Full list of :class:`GCodeLine` objects.
    initial_state: Optional starting :class:`ModalState`.
    """
    state = initial_state.copy() if initial_state else ModalState()
    current_z: float = state.z
    current_group: List[GCodeLine] = []

    for line in lines:
        # Peek at whether this line will change Z (moves and arcs both carry Z).
        z_changes = False
        next_z = current_z
        if (line.is_move or line.is_arc) and "Z" in line.words:
            next_z = line.words["Z"] if state.abs_xy else state.z + line.words["Z"]
            if abs(next_z - current_z) > EPS:
                z_changes = True

        if z_changes and current_group:
            yield (current_z, current_group)
            current_group = []
            current_z = next_z

        current_group.append(line)
        advance_state(state, line)

    if current_group:
        yield (current_z, current_group)


def apply_xy_transform_by_layer(
    lines: List[GCodeLine],
    transform_fn: Callable[[float, float], Tuple[float, float]],
    z_min: Optional[float] = None,
    z_max: Optional[float] = None,
    *,
    xy_decimals: int = DEFAULT_XY_DECIMALS,
    other_decimals: int = DEFAULT_OTHER_DECIMALS,
    initial_state: Optional[ModalState] = None,
    skip_negative_y: bool = True,
) -> List[GCodeLine]:
    """Apply *transform_fn* only to layers within ``[z_min, z_max]``.

    Layers whose Z height is below *z_min* or above *z_max* are passed
    through unchanged.  Omitting either bound means no restriction on that
    end (e.g. ``z_min=None, z_max=5.0`` transforms all layers up to 5 mm).

    Arcs (G2/G3) must be linearized first; relative XY (G91) raises
    :class:`ValueError`.

    Parameters
    ----------
    lines:        G-code line list.
    transform_fn: ``(x, y) -> (x', y')`` callable.
    z_min, z_max: Optional Z range filter (inclusive).
    skip_negative_y: Skip moves at Y < 0 (default ``True``).
    """
    result: List[GCodeLine] = []
    state = initial_state.copy() if initial_state else ModalState()
    current_z: float = state.z

    for line in lines:
        # Determine current layer's Z before state update.
        in_range = (
            (z_min is None or current_z >= z_min - EPS) and
            (z_max is None or current_z <= z_max + EPS)
        )

        if in_range and line.is_move and ("X" in line.words or "Y" in line.words):
            if not state.abs_xy:
                raise ValueError(
                    "apply_xy_transform_by_layer: relative XY (G91) is not supported. "
                    "Call to_absolute_xy() first."
                )
            x_orig = line.words.get("X", state.x)
            y_orig = line.words.get("Y", state.y)

            if skip_negative_y and y_orig < 0:
                result.append(line)
                advance_state(state, line)
                # state.abs_xy is guaranteed True here — G91 raises above
                if "Z" in line.words:
                    current_z = line.words["Z"]
                continue
            x_new, y_new = transform_fn(x_orig, y_orig)

            code, comment = split_comment(line.raw)
            new_code = replace_or_append(code, "X", x_new, xy_decimals, other_decimals)
            new_code = replace_or_append(new_code, "Y", y_new, xy_decimals, other_decimals)
            for ax in ("Z", "E", "F"):
                if ax in line.words:
                    new_code = replace_or_append(
                        new_code, ax, line.words[ax], xy_decimals, other_decimals
                    )

            new_raw = new_code.rstrip() + ("" if not comment else " " + comment.lstrip())
            new_words = dict(line.words)
            new_words["X"] = x_new
            new_words["Y"] = y_new
            result.append(GCodeLine(
                raw=new_raw, command=line.command, words=new_words, comment=comment
            ))

            # Update state with the ORIGINAL coordinates so subsequent
            # layer tracking and relative calculations remain correct.
            state.x = x_orig
            state.y = y_orig
            if "Z" in line.words:
                state.z = line.words["Z"]
                current_z = state.z
            if "E" in line.words:
                state.e = line.words["E"] if state.abs_e else state.e + line.words["E"]
            if "F" in line.words:
                state.f = line.words["F"]
        else:
            result.append(line)
            prev_z = state.z
            advance_state(state, line)
            if abs(state.z - prev_z) > EPS:
                current_z = state.z

    return result


# ---------------------------------------------------------------------------
# §5 — Printer and filament presets
# ---------------------------------------------------------------------------

PRINTER_PRESETS: Dict[str, Dict[str, float]] = {
    "COREONE": {
        "bed_x": 250.0,
        "bed_y": 220.0,
        "max_z": 250.0,
    },
    "COREONEL": {
        "bed_x": 300.0,
        "bed_y": 300.0,
        "max_z": 330.0,
    },
    "MK4": {
        "bed_x": 250.0,
        "bed_y": 210.0,
        "max_z": 220.0,
    },
    "MK3S": {
        "bed_x": 250.0,
        "bed_y": 210.0,
        "max_z": 210.0,
    },
    "MINI": {
        "bed_x": 180.0,
        "bed_y": 180.0,
        "max_z": 180.0,
    },
    "XL": {
        "bed_x": 360.0,
        "bed_y": 360.0,
        "max_z": 360.0,
    },
}
"""Printable area (mm) for common Prusa printers.

Keys: ``bed_x``, ``bed_y``, ``max_z``.  Origin is at ``(0, 0)``.
"""

FILAMENT_PRESETS: Dict[str, Dict[str, object]] = {
    "PLA": {
        "hotend": 215,
        "bed": 60,
        "fan": 100,
        "retract": 0.8,
        "temp_min": 190,
        "temp_max": 230,
        "speed": 60,
        "enclosure": False,
    },
    "PETG": {
        "hotend": 240,
        "bed": 80,
        "fan": 40,
        "retract": 0.8,
        "temp_min": 220,
        "temp_max": 260,
        "speed": 50,
        "enclosure": False,
    },
    "ASA": {
        "hotend": 260,
        "bed": 100,
        "fan": 20,
        "retract": 0.8,
        "temp_min": 240,
        "temp_max": 280,
        "speed": 45,
        "enclosure": True,
    },
    "TPU": {
        "hotend": 230,
        "bed": 50,
        "fan": 50,
        "retract": 1.5,
        "temp_min": 210,
        "temp_max": 250,
        "speed": 25,
        "enclosure": False,
    },
    "ABS": {
        "hotend": 255,
        "bed": 100,
        "fan": 20,
        "retract": 0.8,
        "temp_min": 230,
        "temp_max": 270,
        "speed": 45,
        "enclosure": True,
    },
    "PA": {
        "hotend": 260,
        "bed": 80,
        "fan": 30,
        "retract": 1.0,
        "temp_min": 240,
        "temp_max": 280,
        "speed": 40,
        "enclosure": True,
    },
    "PC": {
        "hotend": 275,
        "bed": 110,
        "fan": 20,
        "retract": 0.8,
        "temp_min": 260,
        "temp_max": 300,
        "speed": 40,
        "enclosure": True,
    },
    "PCTG": {
        "hotend": 250,
        "bed": 80,
        "fan": 50,
        "retract": 0.8,
        "temp_min": 230,
        "temp_max": 270,
        "speed": 50,
        "enclosure": False,
    },
    "PP": {
        "hotend": 240,
        "bed": 85,
        "fan": 30,
        "retract": 1.2,
        "temp_min": 220,
        "temp_max": 260,
        "speed": 35,
        "enclosure": True,
    },
    "PPA": {
        "hotend": 280,
        "bed": 100,
        "fan": 20,
        "retract": 0.8,
        "temp_min": 260,
        "temp_max": 310,
        "speed": 40,
        "enclosure": True,
    },
    "HIPS": {
        "hotend": 230,
        "bed": 100,
        "fan": 20,
        "retract": 0.8,
        "temp_min": 220,
        "temp_max": 250,
        "speed": 45,
        "enclosure": True,
    },
    "PLA-CF": {
        "hotend": 220,
        "bed": 60,
        "fan": 100,
        "retract": 0.8,
        "temp_min": 200,
        "temp_max": 240,
        "speed": 50,
        "enclosure": False,
    },
    "PETG-CF": {
        "hotend": 250,
        "bed": 80,
        "fan": 30,
        "retract": 0.8,
        "temp_min": 230,
        "temp_max": 270,
        "speed": 45,
        "enclosure": False,
    },
    "PA-CF": {
        "hotend": 270,
        "bed": 80,
        "fan": 20,
        "retract": 1.0,
        "temp_min": 250,
        "temp_max": 290,
        "speed": 40,
        "enclosure": True,
    },
}
"""Common filament temperature and retraction settings.

Keys: ``hotend`` (°C), ``bed`` (°C), ``fan`` (0–100 %), ``retract`` (mm),
``temp_min`` (°C), ``temp_max`` (°C), ``speed`` (mm/s),
``enclosure`` (bool — whether an enclosure is recommended).
"""

_M862_3_RE = re.compile(
    r'^M862\.3\s+P\s*"?([A-Za-z0-9_]+)"?',
)
"""Matches ``M862.3 P "COREONE"`` or ``M862.3 P COREONE`` and captures the
printer name."""


def detect_printer_preset(lines: List[GCodeLine]) -> Optional[str]:
    """Detect the printer preset from an ``M862.3 P`` line in *lines*.

    Scans for a Prusa ``M862.3`` printer-model check command and returns
    the matching key from :data:`PRINTER_PRESETS`, or ``None`` if no
    recognised model is found.

    The match is case-insensitive so ``"coreone"`` in the G-code will
    still match the ``"COREONE"`` preset key.
    """
    for line in lines:
        if not line.command.startswith("M862"):
            continue
        m = _M862_3_RE.match(line.raw.strip())
        if m:
            name = m.group(1).upper()
            if name in PRINTER_PRESETS:
                return name
    return None


def detect_print_volume(lines: List[GCodeLine]) -> Optional[Dict[str, float]]:
    """Detect the print volume from an ``M862.3 P`` line in *lines*.

    Returns the matching :data:`PRINTER_PRESETS` entry (a dict with keys
    ``bed_x``, ``bed_y``, ``max_z``) or ``None`` if no recognised printer
    model is found.
    """
    name = detect_printer_preset(lines)
    if name is not None:
        return dict(PRINTER_PRESETS[name])
    return None


# ---------------------------------------------------------------------------
# §6 — Template rendering
# ---------------------------------------------------------------------------

_TEMPLATE_VAR_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
"""Matches only lowercase ``{identifier}`` placeholders."""


def render_template(template_text: str, variables: Dict[str, object]) -> str:
    """Substitute ``{key}`` placeholders in *template_text* from *variables*.

    Only simple lowercase identifiers of the form ``{[a-z][a-z0-9_]*}`` are
    substituted.  Placeholders with no matching key are left unchanged.
    PrusaSlicer conditional syntax (``{if …}``, ``{elsif …}``, etc.) and
    any placeholder containing uppercase letters or special characters are
    never touched, making this safe to use on raw slicer output.

    Parameters
    ----------
    template_text: String that may contain ``{key}`` placeholders.
    variables:     Mapping of placeholder name → value.  Values are
                   converted to ``str`` before substitution.

    Example
    -------
    >>> render_template("M104 S{temp}", {"temp": 215})
    'M104 S215'
    >>> render_template("{if layer == 0}G28{endif}", {})
    '{if layer == 0}G28{endif}'
    """
    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        key = m.group(1)
        if key in variables:
            return str(variables[key])
        return m.group(0)

    return _TEMPLATE_VAR_RE.sub(_replace, template_text)


# ---------------------------------------------------------------------------
# §7 — Thumbnail comment block (public API)
# ---------------------------------------------------------------------------

def encode_thumbnail_comment_block(
    width: int,
    height: int,
    png_bytes: bytes,
) -> str:
    """Encode a PNG image as a PrusaSlicer-compatible thumbnail comment block.

    Returns a multi-line string suitable for prepending to a plain-text
    ``.gcode`` file.  The format is::

        ; thumbnail begin WxH <b64_length>
        ; <base64 data …>
        ; thumbnail end

    This is the same format that PrusaSlicer, OrcaSlicer, SuperSlicer, and
    Cura embed in exported ``.gcode`` files.

    Parameters
    ----------
    width:     Image width in pixels.
    height:    Image height in pixels.
    png_bytes: Raw PNG image data (any size).
    """
    params = struct.pack("<HHH", width, height, _IMG_PNG)
    thumb = Thumbnail(params=params, data=png_bytes, _raw_block=b"")
    return _render_text_thumbnails([thumb])


# ---------------------------------------------------------------------------
# §8 — BGCode public I/O API
# ---------------------------------------------------------------------------

def read_bgcode(data: bytes) -> "GCodeFile":
    """Load a :class:`GCodeFile` from raw Prusa ``.bgcode`` bytes.

    Equivalent to :func:`load` for binary data already in memory.

    Raises :class:`ValueError` for invalid, truncated, or unsupported files.
    """
    return _load_bgcode(data)


def write_bgcode(
    ascii_gcode: str,
    thumbnails: Optional[List[Thumbnail]] = None,
) -> bytes:
    """Serialise ASCII G-code (and optional thumbnails) to ``.bgcode`` bytes.

    Creates a minimal but spec-compliant Prusa BGCode v2 file.  Thumbnail
    blocks are written before the GCode block, matching the layout produced
    by PrusaSlicer.

    Parameters
    ----------
    ascii_gcode: Plain G-code text to embed.
    thumbnails:  Optional list of :class:`Thumbnail` objects.  Thumbnails
                 that have a ``_raw_block`` (i.e. from a previously loaded
                 ``.bgcode`` file) are embedded verbatim for lossless
                 round-trips; others are serialised as uncompressed blocks.

    Returns
    -------
    bytes
        Raw ``.bgcode`` file data that can be written directly to disk.
    """
    thumb_blocks: List[bytes] = []
    for thumb in (thumbnails or []):
        if thumb._raw_block:
            # Verbatim re-embed (lossless round-trip).
            thumb_blocks.append(thumb._raw_block)
        else:
            # Build a fresh uncompressed thumbnail block.
            payload = thumb.data
            params  = thumb.params   # 6 bytes: width, height, fmt
            hdr     = struct.pack("<HHI", _BLK_THUMBNAIL, _COMP_NONE, len(payload))
            cksum   = zlib.crc32(hdr)    & 0xFFFFFFFF
            cksum   = zlib.crc32(params, cksum) & 0xFFFFFFFF
            cksum   = zlib.crc32(payload, cksum) & 0xFFFFFFFF
            thumb_blocks.append(hdr + params + payload + struct.pack("<I", cksum))

    return _bgcode_reassemble(_BGCODE_FILE_HDR_V2, thumb_blocks, ascii_gcode)


# ---------------------------------------------------------------------------
# §9 — PrusaSlicer CLI helpers
# ---------------------------------------------------------------------------

@dataclass
class PrusaSlicerCapabilities:
    """Detected PrusaSlicer executable capabilities.

    Attributes
    ----------
    version_text:          Full version string as reported by ``--help``.
    has_export_gcode:      ``--export-gcode`` / ``-g`` flag present.
    has_load_config:       ``--load`` flag present.
    has_help_fff:          ``--help-fff`` flag present.
    supports_binary_gcode: ``--export-binary-gcode`` or ``--binary`` flag present.
    raw_help:              Full output of ``prusa-slicer --help``.
    raw_help_fff:          Output of ``prusa-slicer --help-fff``, or ``None``.
    """
    version_text: str
    has_export_gcode: bool
    has_load_config: bool
    has_help_fff: bool
    supports_binary_gcode: bool
    raw_help: str
    raw_help_fff: Optional[str]


@dataclass
class RunResult:
    """Result of a PrusaSlicer CLI invocation.

    Attributes
    ----------
    cmd:        Full command list that was executed.
    returncode: Process exit code (0 = success).
    stdout:     Captured standard output.
    stderr:     Captured standard error.
    """
    cmd: List[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """True if the process exited with code 0."""
        return self.returncode == 0


@dataclass
class SliceRequest:
    """Parameters for a single PrusaSlicer slicing operation.

    Attributes
    ----------
    input_path:          Path to the 3-D model (``.stl``, ``.3mf``, …).
    output_path:         Desired output G-code path.
    config_ini:          Path to a PrusaSlicer ``.ini`` config file (or
                         ``None`` to use the slicer's built-in defaults).
    printer_technology:  ``"FFF"`` (default) or ``"SLA"``.
    extra_args:          Additional raw CLI arguments passed verbatim.
    """
    input_path: str
    output_path: str
    config_ini: Optional[str]
    printer_technology: str = "FFF"
    extra_args: List[str] = field(default_factory=list)


# Common executable names / paths searched by find_prusaslicer_executable.
_PS_PATHS_MACOS: List[str] = [
    "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer-console",
    "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
    "/Applications/Original Prusa Drivers/PrusaSlicer.app/Contents/MacOS/PrusaSlicer-console",
]
_PS_PATHS_WIN: List[str] = [
    r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer-console.exe",
    r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer.exe",
    r"C:\Program Files (x86)\Prusa3D\PrusaSlicer\prusa-slicer-console.exe",
    r"C:\Program Files (x86)\Prusa3D\PrusaSlicer\prusa-slicer.exe",
]
_PS_PATH_NAMES: List[str] = [
    "prusa-slicer-console",
    "prusa-slicer",
    "PrusaSlicer-console",
    "PrusaSlicer",
    "prusaslicer",
]


def find_prusaslicer_executable(
    prefer_console: bool = True,
    explicit_path: Optional[str] = None,
) -> str:
    """Locate the PrusaSlicer executable on the current machine.

    Search order
    ------------
    1. *explicit_path* if supplied (raises :class:`FileNotFoundError` if absent).
    2. Platform-specific well-known installation paths.
    3. ``PATH`` entries via :func:`shutil.which`.

    Parameters
    ----------
    prefer_console: Prefer the ``PrusaSlicer-console`` / ``prusa-slicer-console``
                    variant (no GUI) when both are available.
    explicit_path:  Override all search logic with an exact path.

    Raises
    ------
    FileNotFoundError
        If no PrusaSlicer executable can be located.
    """
    if explicit_path is not None:
        if not os.path.isfile(explicit_path):
            raise FileNotFoundError(
                f"Explicit PrusaSlicer path not found: {explicit_path!r}"
            )
        return explicit_path

    candidates: List[str] = []

    if sys.platform == "darwin":
        if prefer_console:
            candidates += _PS_PATHS_MACOS
        else:
            candidates += _PS_PATHS_MACOS[::-1]
    elif sys.platform == "win32":
        if prefer_console:
            candidates += _PS_PATHS_WIN
        else:
            candidates += _PS_PATHS_WIN[::-1]

    # Search PATH (cross-platform fallback).
    names = _PS_PATH_NAMES if prefer_console else _PS_PATH_NAMES[::-1]
    for name in names:
        found = shutil.which(name)
        if found and found not in candidates:
            candidates.append(found)

    for path in candidates:
        if os.path.isfile(path):
            return path

    raise FileNotFoundError(
        "PrusaSlicer executable not found.  Install PrusaSlicer or pass "
        "`explicit_path` to find_prusaslicer_executable()."
    )


def probe_prusaslicer_capabilities(exe: str) -> PrusaSlicerCapabilities:
    """Query a PrusaSlicer executable for its version and supported flags.

    Runs ``exe --help`` (and ``exe --help-fff`` if available) and parses the
    output to populate a :class:`PrusaSlicerCapabilities` object.

    Parameters
    ----------
    exe: Path to the PrusaSlicer executable.

    Raises
    ------
    RuntimeError
        If ``--help`` times out or the executable cannot be run.
    """
    try:
        r = subprocess.run(
            [exe, "--help"],
            capture_output=True, text=True, timeout=30,
        )
        raw_help = r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"PrusaSlicer --help timed out: {exe!r}")
    except OSError as exc:
        raise RuntimeError(f"Cannot run PrusaSlicer {exe!r}: {exc}") from exc

    # Extract version string (e.g. "PrusaSlicer-2.8.0+").
    v_match = re.search(
        r"PrusaSlicer[- ]([0-9]+\.[0-9]+\.[0-9]+[^\s]*)", raw_help, re.IGNORECASE
    )
    version_text = v_match.group(0) if v_match else "unknown"

    has_export_gcode   = "--export-gcode" in raw_help or " -g " in raw_help
    has_load_config    = "--load" in raw_help
    has_help_fff       = "--help-fff" in raw_help
    supports_bgcode    = "--export-binary-gcode" in raw_help or "--binary" in raw_help

    raw_help_fff: Optional[str] = None
    if has_help_fff:
        try:
            r_fff = subprocess.run(
                [exe, "--help-fff"],
                capture_output=True, text=True, timeout=30,
            )
            raw_help_fff = r_fff.stdout + r_fff.stderr
        except (subprocess.TimeoutExpired, OSError):
            pass

    return PrusaSlicerCapabilities(
        version_text=version_text,
        has_export_gcode=has_export_gcode,
        has_load_config=has_load_config,
        has_help_fff=has_help_fff,
        supports_binary_gcode=supports_bgcode,
        raw_help=raw_help,
        raw_help_fff=raw_help_fff,
    )


def run_prusaslicer(
    exe: str,
    args: List[str],
    timeout_s: int = 600,
) -> RunResult:
    """Execute PrusaSlicer with *args* and return the :class:`RunResult`.

    Parameters
    ----------
    exe:       Path to the PrusaSlicer executable.
    args:      Additional arguments (do **not** include the executable itself).
    timeout_s: Maximum wall-clock time (seconds) before raising
               :class:`RuntimeError`.

    Raises
    ------
    RuntimeError
        On timeout or if the executable cannot be launched.
    """
    cmd = [exe] + args
    try:
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout_s,
        )
        return RunResult(cmd=cmd, returncode=r.returncode, stdout=r.stdout, stderr=r.stderr)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"PrusaSlicer timed out after {timeout_s}s.  Command: {cmd!r}"
        )
    except OSError as exc:
        raise RuntimeError(f"Cannot run PrusaSlicer {exe!r}: {exc}") from exc


def slice_model(exe: str, req: SliceRequest) -> RunResult:
    """Slice a 3-D model with PrusaSlicer and write the G-code output.

    Builds the CLI command from *req* and delegates to
    :func:`run_prusaslicer`.

    Parameters
    ----------
    exe: Path to the PrusaSlicer executable.
    req: :class:`SliceRequest` describing the slicing job.

    Returns
    -------
    RunResult
        Exit code, stdout, and stderr from PrusaSlicer.
    """
    args: List[str] = []
    if req.config_ini:
        args += ["--load", req.config_ini]
    args += ["--export-gcode", "--output", req.output_path]
    args += req.extra_args
    args.append(req.input_path)
    return run_prusaslicer(exe, args)


def slice_batch(
    exe: str,
    inputs: List[str],
    output_dir: str,
    config_ini: Optional[str],
    naming: str = "{stem}.gcode",
    parallelism: int = 1,
) -> List[RunResult]:
    """Slice multiple models, writing output files to *output_dir*.

    Parameters
    ----------
    exe:         Path to the PrusaSlicer executable.
    inputs:      List of input model paths.
    output_dir:  Directory for output ``.gcode`` files (created if absent).
    config_ini:  Path to a PrusaSlicer ``.ini`` config (or ``None``).
    naming:      Output filename template.  ``{stem}`` is replaced with the
                 input file stem (filename without extension).
    parallelism: Number of concurrent PrusaSlicer processes (1 = serial).

    Returns
    -------
    List[RunResult]
        One :class:`RunResult` per input, in the same order as *inputs*.
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def _do_one(inp: str) -> RunResult:
        stem = Path(inp).stem
        out_name = render_template(naming, {"stem": stem})
        req = SliceRequest(
            input_path=inp,
            output_path=str(out_dir / out_name),
            config_ini=config_ini,
        )
        return slice_model(exe, req)

    if parallelism <= 1:
        return [_do_one(inp) for inp in inputs]

    with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = [pool.submit(_do_one, inp) for inp in inputs]
        return [f.result() for f in futures]


# ---------------------------------------------------------------------------
# §10 — PrusaLink API client
# ---------------------------------------------------------------------------


class PrusaLinkError(Exception):
    """Raised when a PrusaLink API call fails.

    Attributes
    ----------
    status_code: HTTP status code (0 for connection/timeout errors).
    message:     Human-readable error description.
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"PrusaLink error {status_code}: {message}")


@dataclass
class PrusaLinkInfo:
    """Printer identification from ``GET /api/version``.

    Attributes
    ----------
    api:      API version string.
    server:   Server version string.
    original: Original PrusaLink version.
    text:     Human-readable description.
    """
    api: str
    server: str
    original: str
    text: str


@dataclass
class PrusaLinkStatus:
    """Printer status from ``GET /api/v1/status``.

    Attributes
    ----------
    printer_state: Current state (``"IDLE"``, ``"PRINTING"``, ``"BUSY"``, …).
    temp_nozzle:   Current nozzle temperature in °C (or ``None``).
    temp_bed:      Current bed temperature in °C (or ``None``).
    raw:           Full JSON response for extensibility.
    """
    printer_state: str
    temp_nozzle: Optional[float]
    temp_bed: Optional[float]
    raw: dict


@dataclass
class PrusaLinkJob:
    """Active job info from ``GET /api/v1/job``.

    Attributes
    ----------
    job_id:         Numeric job ID (or ``None`` if no job).
    progress:       Print progress 0–100 (or ``None``).
    time_remaining: Estimated seconds remaining (or ``None``).
    state:          Job state string.
    raw:            Full JSON response for extensibility.
    """
    job_id: Optional[int]
    progress: Optional[float]
    time_remaining: Optional[int]
    state: str
    raw: dict


def _prusalink_request(
    base_url: str,
    api_key: str,
    method: str,
    path: str,
    data: Optional[bytes] = None,
    content_type: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
) -> bytes:
    """Make an HTTP request to PrusaLink and return the raw response body.

    Parameters
    ----------
    base_url:       Printer base URL, e.g. ``"http://192.168.1.100"``.
    api_key:        PrusaLink API key (sent as ``X-Api-Key`` header).
    method:         HTTP method (``"GET"``, ``"PUT"``, ``"POST"``, ``"DELETE"``).
    path:           API path, e.g. ``"/api/version"``.
    data:           Optional request body bytes.
    content_type:   Optional ``Content-Type`` header value.
    extra_headers:  Additional headers to include.
    timeout:        Request timeout in seconds.

    Returns
    -------
    bytes
        Raw response body.

    Raises
    ------
    PrusaLinkError
        On HTTP errors or connection failures.
    """
    url = base_url.rstrip("/") + path
    headers: Dict[str, str] = {"X-Api-Key": api_key}
    if content_type:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)

    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise PrusaLinkError(exc.code, body or str(exc)) from exc
    except URLError as exc:
        raise PrusaLinkError(0, str(exc.reason)) from exc
    except TimeoutError as exc:
        raise PrusaLinkError(0, "Connection timed out") from exc


def prusalink_get_version(
    base_url: str,
    api_key: str,
    timeout: float = 10.0,
) -> PrusaLinkInfo:
    """Query printer identification via ``GET /api/version``.

    This is the lightest endpoint and works well as a connectivity test.

    Parameters
    ----------
    base_url: Printer base URL.
    api_key:  PrusaLink API key.
    timeout:  Request timeout in seconds.

    Returns
    -------
    PrusaLinkInfo
    """
    body = _prusalink_request(base_url, api_key, "GET", "/api/version",
                              timeout=timeout)
    d = json.loads(body)
    return PrusaLinkInfo(
        api=d.get("api", ""),
        server=d.get("server", ""),
        original=d.get("original", ""),
        text=d.get("text", ""),
    )


def prusalink_get_status(
    base_url: str,
    api_key: str,
    timeout: float = 10.0,
) -> PrusaLinkStatus:
    """Query current printer status via ``GET /api/v1/status``.

    Parameters
    ----------
    base_url: Printer base URL.
    api_key:  PrusaLink API key.
    timeout:  Request timeout in seconds.

    Returns
    -------
    PrusaLinkStatus
    """
    body = _prusalink_request(base_url, api_key, "GET", "/api/v1/status",
                              timeout=timeout)
    d = json.loads(body)
    printer = d.get("printer", {})
    return PrusaLinkStatus(
        printer_state=printer.get("state", "UNKNOWN"),
        temp_nozzle=printer.get("temp_nozzle"),
        temp_bed=printer.get("temp_bed"),
        raw=d,
    )


def prusalink_get_job(
    base_url: str,
    api_key: str,
    timeout: float = 10.0,
) -> PrusaLinkJob:
    """Query active print job via ``GET /api/v1/job``.

    Parameters
    ----------
    base_url: Printer base URL.
    api_key:  PrusaLink API key.
    timeout:  Request timeout in seconds.

    Returns
    -------
    PrusaLinkJob
    """
    body = _prusalink_request(base_url, api_key, "GET", "/api/v1/job",
                              timeout=timeout)
    d = json.loads(body)
    return PrusaLinkJob(
        job_id=d.get("id"),
        progress=d.get("progress"),
        time_remaining=d.get("time_remaining"),
        state=d.get("state", "UNKNOWN"),
        raw=d,
    )


def _build_multipart(
    fields: Dict[str, str],
    file_field: str,
    file_name: str,
    file_data: bytes,
    file_content_type: str = "application/octet-stream",
) -> Tuple[bytes, str]:
    """Build a multipart/form-data body from fields and one file.

    Returns ``(body_bytes, content_type_header)`` including the boundary.
    """
    boundary = uuid.uuid4().hex
    parts: List[bytes] = []
    for key, val in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{val}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}";'
        f' filename="{file_name}"\r\n'
        f"Content-Type: {file_content_type}\r\n\r\n".encode()
    )
    parts.append(file_data)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    ct = f"multipart/form-data; boundary={boundary}"
    return body, ct


def prusalink_upload(
    base_url: str,
    api_key: str,
    gcode_path: str,
    print_after_upload: bool = False,
    timeout: float = 120.0,
) -> str:
    """Upload a G-code file to the printer via PrusaLink.

    Uses ``PUT /api/v1/files/usb/<filename>`` with the raw file body.

    Parameters
    ----------
    base_url:           Printer base URL.
    api_key:            PrusaLink API key.
    gcode_path:         Local path to the ``.gcode`` file.
    print_after_upload: If ``True``, start printing immediately after upload.
    timeout:            Request timeout in seconds (uploads can be slow).

    Returns
    -------
    str
        The filename as stored on the printer.

    Raises
    ------
    PrusaLinkError
        On HTTP or connection errors.
    FileNotFoundError
        If *gcode_path* does not exist.
    """
    p = Path(gcode_path)
    if not p.is_file():
        raise FileNotFoundError(f"G-code file not found: {gcode_path}")

    file_data = p.read_bytes()
    filename = p.name
    path = f"/api/v1/files/usb/{filename}"

    extra_headers: Dict[str, str] = {
        "Content-Length": str(len(file_data)),
        "Content-Type": "application/octet-stream",
    }
    if print_after_upload:
        extra_headers["Print-After-Upload"] = "1"

    _prusalink_request(
        base_url, api_key, "PUT", path,
        data=file_data,
        extra_headers=extra_headers,
        timeout=timeout,
    )
    return filename
