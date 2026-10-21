# G-code reader attribution

`gcode_lib.py` contains a modified reader subset of **gcode-lib 1.0.0** by
**Hyiger**, <https://github.com/hyiger/gcode-lib>, licensed under GNU GPL v3 or later.
The license text is included in `LICENSE.gcode-lib`.

The original vendored source was contributed by **amoose136** in
[PR #18](https://github.com/kmnunley/Simple-GCode-Importer/pull/18), commit
`74befbedac8bf66909f8c7bb0db87548f917b750`.

Local modifications on 2026-10-21 retain only file reading, Heatshrink and
MeatPack decoding. Transformation, writing, thumbnail rendering, and printer
network/upload utilities were removed. Text lines are preserved for the
existing importer. Binary header/checksum handling and malformed-stream
validation were corrected. `load()` returns a list of source strings instead
of the upstream structured object.

Binary format reference:
<https://github.com/prusa3d/libbgcode/blob/main/doc/specifications.md>.
