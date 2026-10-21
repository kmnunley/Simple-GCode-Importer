# Regression checks

Run the reader tests with Python 3.10+ (no Blender required):

```powershell
python -m unittest discover -s tests -p test_bgcode_reader.py
```

Run the scene/operator tests in Blender 4.2 or newer:

```powershell
blender --background --factory-startup --python-exit-code 1 --python tests/test_materials.py --python tests/test_arcs.py --python tests/test_file_import.py
```

The reader checks all 24 combinations of compression, encoding and checksum
type, multi-block files, text preservation, corrupt/truncated streams, and
unsupported headers. The Blender checks compare text/binary curve geometry,
arcs, materials and bevel; mixed multi-file imports; scene rollback; error
reports; handler registration, drop contexts, and confirmation/browser routing.
The popup routing test uses Blender's real ImportHelper with a recording window
manager because background mode does not run operator invoke callbacks. A
physical mouse drag into a visible Blender window remains a manual UI check.

The fixture encoders in `bgcode_fixtures.py` are independent of the reader and
follow Prusa's public format specification. No network access is needed.

For sandboxed runs, point `TEMP` and `TMP` at a writable temporary directory.
`.test-tmp/` in the repository is ignored for this purpose.
