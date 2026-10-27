# Import format reference

Detailed format support and filtering behavior for troubleshooting G-code imports.

## Wipe and prime tower markers

**Exclude wipe/prime tower (PrusaSlicer and OrcaSlicer only)** is enabled by
default in the import options and drag-and-drop confirmation. Disable it to
include the tower. Filtering recognizes `;TYPE:Wipe tower`,
`;TYPE:Prime tower`, `; FEATURE: Prime tower`, and `WIPE_TOWER_START` /
`WIPE_TOWER_END` comment markers. Other feature labels end a tower role;
explicit tower blocks end at `WIPE_TOWER_END`. Movement, tool, and extrusion
state still update while tower geometry is excluded, and model paths remain
disconnected across excluded sections. Ordinary `WIPE_START` / `WIPE_END`
comments do not identify towers. Files without recognized markers import
unchanged; this option does not infer towers from their position or shape.

## Binary G-code

Binary imports support BGCode version 1 with uncompressed, DEFLATE, or Heatshrink (11/4 and 12/4) blocks, raw or MeatPack G-code, and optional CRC32 checksums. Metadata and thumbnails do not create geometry. Decompressed G-code blocks are limited to 64 MiB each. The path and arc rules below apply to both formats.

## Import errors

If a file cannot be read or contains invalid supported commands, Blender reports the filename and error. All paths, curves, and materials created by that import are removed, including earlier files in a multi-file selection; existing scene data is preserved. Empty and comment-only files report an error. Travel-only G-code imports successfully without creating paths.

## Motion and arcs

The importer supports linear moves (`G0`/`G1`) and clockwise/counterclockwise XY arcs (`G2`/`G3`). Arcs can use relative `I`/`J` center offsets or a signed `R` radius: positive radii select the short arc, negative radii select the long arc. Full circles use `I`/`J` with matching or omitted XY endpoints. Z changes interpolate evenly along the arc for helical moves. Only motion depositing new filament creates geometry; travel arcs still update the next move's starting position.

Arc sampling uses a maximum angle of 11.25 degrees per segment. XYZ endpoints follow `G90`/`G91`, while extrusion follows `M82`/`M83`. Arc centers remain relative to the start (`G91.1`). I/J arcs allow a start/end radius difference of up to 0.003 mm or 0.01% of the larger radius, whichever is greater, to accommodate rounded slicer coordinates; the final point remains exactly the commanded endpoint. Non-XY planes (`G18`/`G19`), absolute arc centers (`G90.1`), and `K` offsets are unsupported. Invalid arc geometry or unsupported modes stop the import with an error. Commands and parameters must be separated by spaces.
