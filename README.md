# Simple GCode Importer for Blender

![A 3D render of a low-poly Thinker statue](/images/low_poly_thinker.png)
<sup>*Lowest Poly The Thinker* model by [LXO on Printables.com](https://www.printables.com/model/1165-lowest-poly-the-thinker)</sup>

## About

Simple GCode Importer is a simple plug-in that translates printer GCode into Bezier curve paths in Blender. This results in a very performant representation of the object in a highly customizable format.

## Installation
To install the plugin, add it to Blender from the [Extension website](https://extensions.blender.org/add-ons/simple-gcode-importer/) or straight from Blender via Edit > Preferences > Get Extensions.

## Usage
In Blender 4.2 or newer, go to File > Import > GCode (.gcode, .bgcode), select one or more files, and click Import GCode/BGCode. You can also drag text `.gcode` or Prusa binary `.bgcode` files into the 3D Viewport or Outliner and confirm the import.

Binary imports support BGCode version 1 with uncompressed, DEFLATE, or Heatshrink (11/4 and 12/4) blocks, raw or MeatPack G-code, and optional CRC32 checksums. Metadata and thumbnails do not create geometry. Decompressed G-code blocks are limited to 64 MiB each. The existing path and arc rules below apply to both formats.

If a file cannot be read or contains invalid supported commands, Blender reports the filename and error. All paths, curves, and materials created by that import are removed, including earlier files in a multi-file selection; existing scene data is preserved. Empty and comment-only files report an error. Travel-only G-code imports successfully without creating paths.

Imported paths have a bevel depth of 0.2. Paths from the same tool share a material, named `T0`, `T1`, and so on, so editing that material updates all paths for that tool within the import. Files without tool commands use `T0`. Materials and default bevel support are adapted from [Mitsunori YOSHIDA's contribution in PR #6](https://github.com/kmnunley/Simple-GCode-Importer/pull/6).

The importer supports linear moves (`G0`/`G1`) and clockwise/counterclockwise XY arcs (`G2`/`G3`). Arcs can use relative `I`/`J` center offsets or a signed `R` radius: positive radii select the short arc, negative radii select the long arc. Full circles use `I`/`J` with matching or omitted XY endpoints. Z changes interpolate evenly along the arc for helical moves. Only motion depositing new filament creates geometry; travel arcs still update the next move's starting position.

Arc sampling uses a maximum angle of 11.25 degrees per segment. XYZ endpoints follow `G90`/`G91`, while extrusion follows `M82`/`M83`. Arc centers remain relative to the start (`G91.1`). I/J arcs allow a start/end radius difference of up to 0.003 mm or 0.01% of the larger radius, whichever is greater, to accommodate rounded slicer coordinates; the final point remains exactly the commanded endpoint. Non-XY planes (`G18`/`G19`), absolute arc centers (`G90.1`), and `K` offsets are unsupported. Invalid arc geometry or unsupported modes stop the import with an error. Commands and parameters must be separated by spaces.

## FAQ
**How long do models take to import?**<br>
This depends heavily on the computer you are running on, but imports can take anywhere from just a couple seconds to several minutes depening on the size and complexity of the model that is being imported.

**How do I give the imported paths thickness?**<br>
These options can be found in the Properties area (bottom right panel in the default layout) when a path is selected. Under the 'Object Data Properties' tab there is a 'Geometry' dropdown. Under this dropdown, the two most useful values are 'Extrusion' and 'Bevel'. Adjust these values until the desired look of the model has been reached.

## Planned Features
- Add import options for things such as skipping layers, path resolution, or defining custom GCode parameters.
- Make it easier to edit all imported paths simultaneously
- Make it easier to animate paths
- Add the ability to import GCode as a point cloud

## Troubleshooting
If you are experiencing issues with the plugin, feel free to open an issue and I'll respond when i can.

## Credits

Drag-and-drop and binary G-code support are adapted from [amoose136's PR #18](https://github.com/kmnunley/Simple-GCode-Importer/pull/18). Binary decoding uses a modified reader subset of [Hyiger's gcode-lib](https://github.com/hyiger/gcode-lib); attribution, modification notes, and its GPL v3 license are included in the extension.


Like my work?
[Buy me a coffee!](https://www.buymeacoffee.com/kmnunley) :coffee:
