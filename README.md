# Simple GCode Importer for Blender

![A 3D render of a low-poly Thinker statue](/images/low_poly_thinker.png)
<sup>*Lowest Poly The Thinker* model by [LXO on Printables.com](https://www.printables.com/model/1165-lowest-poly-the-thinker)</sup>

## About

Simple GCode Importer is a simple plug-in that translates printer GCode into Bezier curve paths in Blender. This results in a very performant representation of the object in a highly customizable format.

## Installation
To install the plugin, add it to Blender from the [Extension website](https://extensions.blender.org/add-ons/simple-gcode-importer/) or straight from Blender via Edit > Preferences > Get Extensions.

## Usage
To import a GCode file to Blender, simply go to File > Import and select the GCode option. In the new file browser window, find and select the .gcode file you would ike to import, then click 'Import Gcode'

Imported paths have a bevel depth of 0.2. Paths from the same tool share a material, named `T0`, `T1`, and so on, so editing that material updates all paths for that tool within the import. Files without tool commands use `T0`. Materials and default bevel support are adapted from [Mitsunori YOSHIDA's contribution in PR #6](https://github.com/kmnunley/Simple-GCode-Importer/pull/6).

The importer supports linear moves (`G0`/`G1`) and clockwise/counterclockwise XY arcs (`G2`/`G3`). Arcs can use relative `I`/`J` center offsets or a signed `R` radius: positive radii select the short arc, negative radii select the long arc. Full circles use `I`/`J` with matching or omitted XY endpoints. Z changes interpolate evenly along the arc for helical moves. Only motion depositing new filament creates geometry; travel arcs still update the next move's starting position.

Arc sampling uses a maximum angle of 11.25 degrees per segment. XYZ endpoints follow `G90`/`G91`, while extrusion follows `M82`/`M83`. Arc centers remain relative to the start (`G91.1`). Non-XY planes (`G18`/`G19`), absolute arc centers (`G90.1`), and `K` offsets are unsupported. Invalid arc geometry or unsupported modes stop the import with an error. Commands and parameters must be separated by spaces.

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


Like my work?
[Buy me a coffee!](https://www.buymeacoffee.com/kmnunley) :coffee:
