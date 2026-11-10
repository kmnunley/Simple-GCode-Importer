# Import format reference

Detailed format support and filtering behavior for troubleshooting G-code imports.

## Shared printed-bead profile

**Printed bead** uses one width and height for every printing operation in a
file. It preserves exactly one curve and material per tool beneath the file's
parent Empty, with no profile helper objects, size groups, or join meshes.
All layers, features, tools, bridges and included purge/tower paths receive
this same geometry. Each tool's curve still has its own editable geometry
settings after import.

The profile is a rectangle with semicircular sides, centered half its height
below the commanded Z. Native Extrude and Bevel controls form the profile;
curve tilt places the wide axis horizontally. Open path endpoints have rounded
tapered extensions, ending at zero-radius poles rather than flat disks.
These extensions remain editable in the curve and extend half a bead width
beyond each endpoint. Continuous closed paths have no end caps.

Each open end uses two added control points: a rounded shoulder and a zero-radius
tip. Imported curves use Bevel Resolution 1, Resolution Preview U 2 and Render U 5
in both profile modes. These defaults can be edited in Object Data Properties;
they are not import options. Compared with the previous defaults (bevel 3,
preview 1), short disconnected strokes use fewer polygons, while the higher
preview resolution can increase polygon counts on long or closed paths.

### Temporary import memory

Parsed bead paths are retained in packed double-precision XYZ buffers until
construction. File imports release decoded source lines before building curves,
and discard each path buffer after writing its spline. Construction uses packed
float32 buffers directly rather than per-point Python Vectors and station tuples.
Parser precision and the resulting editable geometry are preserved. These changes
reduce temporary import allocations; they do not reduce the completed curve's
control-point count or the size of Blender's evaluated beveled surface.

### Choosing the single profile without using startup purge lines

The estimator samples only recognized model feature sections (such as
perimeters/walls, infill, skin and support). When layer markers are present,
it also waits for the first print layer. Without recognized model features,
it can use deposition after `LAYER_CHANGE`, `CHANGE_LAYER`, or a nonnegative
`LAYER:` marker. Startup moves before these boundaries never contribute.
Custom, purge/prime/wipe tower, skirt/brim, bridge and ironing roles, and
explicit `PURGE_START` / `PURGE_END` or tower blocks do not size the profile.
Excluding a path from estimation does not remove it from the imported geometry.

Local `WIDTH:` / `HEIGHT:` and Orca-style `LINE_WIDTH:` / `LAYER_HEIGHT:` hints
supply candidate dimensions. Otherwise width is derived from deposited
volume divided by true path length, using the rectangular-plus-semicircles
area formula. Filament diameter can come from embedded settings or the import
fallback. Retraction recovery is excluded. XY arcs and helices use analytical
length, independent of sampling resolution. `M200` and `use_volumetric_e`
support volumetric E; `M221` flow scaling is applied once. Zero-flow moves do
not create printed-bead geometry. Inch-mode bead imports report an error.

Height comes from a local hint, nominal `layer_height`, increases between
planar model deposition elevations, or the fallback, in that order. Purge
elevations and travel Z hops do not enter inference. Candidate widths are
binned to 0.01 mm and heights to 0.001 mm. The pair covering the greatest
model path length is selected once, so a short unusual section does not
override the representative profile.

If no trustworthy model boundary exists, no movement is sampled: nominal
slicer width/height settings are used, followed by the import fallback values.
Width settings include `extrusion_width`, `line_width`, and perimeter widths;
first-layer width settings do not define the common profile. Numeric settings
are read from text headers/footers and BGCode INI metadata. Fallback assumptions
are reported in Blender. The file parent records `bead_width`, `bead_height`,
`bead_profile_source`, and `bead_profile_assumptions` as custom properties.

This is a nominal visualization. Rounded ends add some visual volume; pressure,
shrinkage, sag and deposited-bead overlap are not simulated. Helical/nonplanar
sections follow a Z-up curve frame, so their vertical bounds are approximate.
**Round (legacy)** retains the original 0.2-radius circular bevel and flat caps.

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

Binary imports support BGCode version 1 with uncompressed, DEFLATE, or Heatshrink (11/4 and 12/4) blocks, raw or MeatPack G-code, and optional CRC32 checksums. Metadata can supply the common bead settings; thumbnails do not create geometry. Decompressed G-code and decoded metadata blocks are limited to 64 MiB each. The path and arc rules below apply to both formats.

## Import errors

If a file cannot be read or contains invalid supported commands, Blender reports the filename and error. All paths, curves, and materials created by that import are removed, including earlier files in a multi-file selection; existing scene data is preserved. Empty and comment-only files report an error. Travel-only G-code imports successfully without creating paths.

## Motion and arcs

The importer supports linear moves (`G0`/`G1`) and clockwise/counterclockwise XY arcs (`G2`/`G3`). Arcs can use relative `I`/`J` center offsets or a signed `R` radius: positive radii select the short arc, negative radii select the long arc. Full circles use `I`/`J` with matching or omitted XY endpoints. Z changes interpolate evenly along the arc for helical moves. Only motion depositing new filament creates geometry; travel arcs still update the next move's starting position.

Arc sampling uses a maximum angle of 11.25 degrees per segment. XYZ endpoints follow `G90`/`G91`, while extrusion follows `M82`/`M83`. Arc centers remain relative to the start (`G91.1`). I/J arcs allow a start/end radius difference of up to 0.003 mm or 0.01% of the larger radius, whichever is greater, to accommodate rounded slicer coordinates; the final point remains exactly the commanded endpoint. Non-XY planes (`G18`/`G19`), absolute arc centers (`G90.1`), and `K` offsets are unsupported. Invalid arc geometry or unsupported modes stop the import with an error. Commands and parameters must be separated by spaces.
