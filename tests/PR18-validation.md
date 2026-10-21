# PR #18 local integration validation

Validated on 2026-10-21 against main commit
`1b17e23af28987da372c28bd86dd85f62f5f6b67`, retaining
amoose136's original commit `74befbedac8bf66909f8c7bb0db87548f917b750` as a
merge parent. No push, publication, or GitHub discussion update was performed.

## Results

- Python 3.12: all 12 reader tests pass, including the 24-combination binary
  compression/encoding/checksum matrix.
- Blender 4.4.3: all 34 material, arc and file-import tests pass.
- Blender 5.2.2 LTS: the same 34 tests pass.
- Blender 4.4 extension build and archive manifest validation pass. The archive
  includes both the third-party notice and GPL v3 license, and no bytecode caches.
- `git diff --check` passes.

See `tests/README.md` for commands. The manifest minimum remains Blender 4.2;
4.2 itself was not installed for this run. The package version remains 1.1.1;
the test archive is a local development build, not a release.

## Independent reference files

The reader was also compared with the public Prusa libbgcode test files at
commit `d4da9073616d70a43c151e8c1d7fbff879d2e08a`:

<https://github.com/prusa3d/libbgcode/tree/d4da9073616d70a43c151e8c1d7fbff879d2e08a/tests/data>

Ignoring comments, empty lines and whitespace, both decoded outputs exactly
match their corresponding `_ref.gcode` commands:

| Binary fixture | Commands | SHA-256 of binary fixture |
| --- | ---: | --- |
| mini_cube_b.bgcode | 23,557 | 84a942615e644430c8b47f02422ceb5682ad2a1f2860100bb6a8fe18ec22f6ec |
| mini_cube_ps2.8.1.bgcode | 2,147 | 0b8a2f0884f05f49b9c40e975dd628ebd938374347bdd62cc2f022e2351acd01 |

Downloaded reference files remain in the ignored `.test-tmp/prusa-reference/`
directory; they are not redistributed in the source or extension archive.

## Manual UI check

A physical mouse drag into the visible Blender window was not performed.
Registration, allowed drop contexts, mixed file execution, and confirmation
versus file-browser routing are covered by the background tests. Before
release, drag one `.gcode`, one `.bgcode`, and a mixed selection into a 3D
Viewport or the View Layer Outliner and confirm the imports.

## MK4 MMU3 rounded-arc follow-up

The user's `MK4_MMU3_2COLOR_TEST_0.4n_0.15mm_PLA_16m.bgcode` decodes to
16,681 lines containing 1,729 I/J arcs. The previous 0.001 mm absolute radius
tolerance rejected three arcs. At line 1495 the radius difference is
0.0011499327 mm: rounded coordinates, rather than a binary decoding failure.

The absolute tolerance is now 0.003 mm, covering the worst-case difference
from rounding XY endpoints and I/J offsets to 0.001 mm (approximately
0.002828 mm). The existing 0.01% relative tolerance remains. Sampling retains
the exact commanded endpoint. Tests include both clockwise and counterclockwise
arcs reduced from this file, plus rejection immediately beyond the new bound.

The complete file imports in Blender 4.4.3 and 5.2.2 with 1,267 paths, 9,842 control
points, and `T0`/`T1` materials. All 36 Blender regression tests pass in both after the
fix (18 arc, 11 material, 7 file-import tests). The source BGCode is not modified
or bundled with the extension.
