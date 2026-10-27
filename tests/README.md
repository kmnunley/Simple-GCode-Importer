# Regression tests

## Local development install

Run `python scripts/build_dev.py` (or `make build-dev`) to build and validate a
test ZIP in `releases/`. Use `--blender "path/to/blender.exe"` if needed.
The script copies the current source to a temporary directory and overrides
only that copy's metadata. The release manifest and source version stay unchanged.

In Blender, use Preferences > Add-ons > Install from Disk to install the ZIP.
It appears as **Simple Gcode Importer (DEV)** with a timestamped
`2.0.0-dev.YYYYMMDDHHMMSS` version (UTC). It keeps the release extension ID,
so it replaces the existing install in the same extension repository rather
than registering a second importer. Verify the DEV name and version in
Preferences before testing. Reinstall the release ZIP to return to the release.

## Automated checks

Run the complete suite in Blender 4.2 or newer, from the repository root:

```powershell
blender --background --factory-startup --python-exit-code 1 --python tests/run_blender.py
```

The binary reader tests can also run independently with Python 3.10+:

```powershell
python -m unittest discover -s tests -p test_bgcode_reader.py
```

| File | Coverage |
| --- | --- |
| `test_bgcode_reader.py` | Compression, encoding, checksums, text preservation, and malformed files |
| `test_arcs.py` | XY arcs, coordinate modes, rounded coordinates, extrusion, and rollback |
| `test_materials.py` | Per-tool curves and materials, bevel, disconnected splines, and extrusion state |
| `test_wipe_tower.py` | Default-on import option, PrusaSlicer/OrcaSlicer markers, excluded geometry and preserved state, text/BGCode batches, and rollback |
| `test_file_import.py` | File root hierarchy, independent imports, parent transforms, registered operators, mixed batches, rollback, and drop-handler routing |
| `bgcode_fixtures.py` | Independent binary encoders shared by reader and import tests |
| `fixtures/` | Small text G-code samples for material and arc imports |

Each test module also supports direct execution in its required Python
environment. Importing a module does not run its tests. No network access or
downloaded printer files are required.

Background tests cover drop registration, supported contexts, and popup versus
file-browser routing. To check the actual UI, drag text and binary G-code files
into a 3D Viewport or View Layer Outliner, confirm, and verify the resulting paths.

For sandboxed runs, point `TEMP` and `TMP` at a writable temporary directory.
The repository's `.test-tmp/` directory is ignored for this purpose.
