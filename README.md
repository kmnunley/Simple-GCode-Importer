# Simple GCode Importer for Blender

![A 3D render of a low-poly Thinker statue](/images/low_poly_thinker.png)
<sup>*Lowest Poly The Thinker* model by [LXO on Printables.com](https://www.printables.com/model/1165-lowest-poly-the-thinker)</sup>

## About

Simple GCode Importer is a simple plug-in that translates printer GCode into Bezier curve paths in Blender. This results in a very performant representation of the object in a highly customizable format.

## Installation
To install the plugin, add it to Blender from the [Extension website](https://extensions.blender.org/add-ons/simple-gcode-importer/) or straight from Blender via Edit > Preferences > Get Extensions.

## Usage
In Blender 4.2 or newer, go to File > Import > GCode (.gcode, .bgcode), select one or more files, and click Import GCode/BGCode. You can also drag supported files into the 3D Viewport or Outliner and confirm the import.

Each file is grouped under its filename in the Outliner, with a curve for each tool (`T0`, `T1`, and so on). Select the parent Empty to move, rotate, or scale the whole import. Select a tool's curve to change thickness or material for all its paths, or enter Edit Mode to edit individual paths.

The importer supports text G-code, Prusa binary BGCode, and XY arcs, including helical moves. Only paths that deposit filament are imported. See the [import format reference](docs/import-format.md) for detailed compatibility and limitations.

### Import options

| Option | Default | Description |
| --- | --- | --- |
| Exclude wipe/prime tower (PrusaSlicer and OrcaSlicer only) | On | Excludes the tower when the slicer's feature comments identify it. Turn off to include the tower. Files without those comments import unchanged. |

## FAQ
**How long do models take to import?**<br>
This depends heavily on the computer you are running on, but imports can take anywhere from just a couple seconds to several minutes depending on the size and complexity of the model that is being imported.

**How do I give the imported paths thickness?**<br>
Select a tool's curve object, then open Object Data Properties > Geometry. Adjust Extrusion or Bevel to change all paths for that tool together. Selecting the root Empty controls transforms; it does not expose curve geometry settings.

## Planned Features
- Add import options for things such as skipping layers, path resolution, or defining custom GCode parameters.
- Make it easier to animate paths
- Add the ability to import GCode as a point cloud

## Troubleshooting
If you are experiencing issues with the plugin, feel free to open an issue and I'll respond when i can.

## Credits

Materials and default bevel support are adapted from [Mitsunori YOSHIDA's contribution in PR #6](https://github.com/kmnunley/Simple-GCode-Importer/pull/6).

Drag-and-drop and binary G-code support are adapted from [amoose136's PR #18](https://github.com/kmnunley/Simple-GCode-Importer/pull/18). Binary decoding uses a modified reader subset of [Hyiger's gcode-lib](https://github.com/hyiger/gcode-lib); attribution, modification notes, and its GPL v3 license are included in the extension.


Like my work?
[Buy me a coffee!](https://www.buymeacoffee.com/kmnunley) :coffee:
