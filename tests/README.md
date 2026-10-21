# Regression tests

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
| `test_materials.py` | Tool materials, bevel, travel gaps, and extrusion state |
| `test_file_import.py` | Registered operators, mixed batches, error reporting, and drop-handler routing |
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
