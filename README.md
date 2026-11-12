# Simple GCode Importer for Blender

![A 3D render of a low-poly Thinker statue](/images/low_poly_thinker.png)
<sup>*Lowest Poly The Thinker* model by [LXO on Printables.com](https://www.printables.com/model/1165-lowest-poly-the-thinker)</sup>

## About

Simple GCode Importer is a simple plug-in that translates printer GCode into Bezier curve paths in Blender. This results in a very performant representation of the object in a highly customizable format.

## Installation
To install the plugin, add it to Blender from the [Extension website](https://extensions.blender.org/add-ons/simple-gcode-importer/) or straight from Blender via Edit > Preferences > Get Extensions.

## Usage
In Blender 4.2 or newer, go to File > Import > GCode (.gcode, .bgcode), select one or more files, and click Import GCode/BGCode. You can also drag supported files into the 3D Viewport or Outliner and confirm the import.

**Estimated RAM required** shows roughly how much additional RAM Blender may
use for viewport import after a file is selected. It updates when you change
the extrusion profile or feature filters. Actual memory use varies with the
Blender version, scene, and graphics backend.

During interactive imports, Blender’s status bar shows the current stage and
approximate progress. Press Escape to cancel and remove the current import batch.
After a successful import, the status bar shows the elapsed time for five seconds.

Each file is grouped under its filename in the Outliner, with a curve for each tool (`T0`, `T1`, and so on). Select the parent Empty to move, rotate, or scale the whole import. Select a tool's curve to change thickness or material for all its paths, or enter Edit Mode to edit individual paths.

By default, every tool in a file uses one shared printed-bead profile: a flat top and bottom, rounded sides, and rounded path ends. The importer estimates a representative width and height from model extrusion sections, excluding startup purge lines. It keeps the same profile across layers, features, and tools, with no extra profile objects or geometry groups.

Rounded ends use two extra control points per end. Imported curves start with
Bevel Resolution **1**, Resolution Preview U **2**, and Render U **5**. Adjust these
after import in Object Data Properties; they are not exposed as import options.

The importer supports text G-code, Prusa binary BGCode, and XY arcs, including helical moves. Only paths that deposit filament are imported. See the [import format reference](docs/import-format.md) for detailed compatibility and limitations.

### Import options

| Option | Default | Description |
| --- | --- | --- |
| Extrusion profile | Simulated Bead | Simulated Bead gives paths a flattened profile with rounded ends. Round uses a circular bevel and flat caps. None imports editable paths without thickness. |
| Fallback filament diameter (mm) | 1.75 | Used for volume calculations when the file has no filament diameter. |
| Fallback layer height (mm) | 0.20 | Used when a representative height is unavailable. |
| Fallback bead width (mm) | 0.45 | Used when model extrusion cannot be identified and the file has no nominal width setting. |
| Exclude wipe/prime tower (PrusaSlicer and OrcaSlicer only) | On | Excludes the tower when the slicer's feature comments identify it. Turn off to include the tower. Files without those comments import unchanged. |
| Exclude infill | Off | Skips marked sparse, internal solid and gap infill before collecting paths or sampling arcs. Keeps top/bottom surfaces and bridge surfaces. |
| Exclude supports | Off | Skips marked supports and support interfaces before collecting paths or sampling arcs. |

Infill and support exclusion use slicer `;TYPE:` or `;FEATURE:` comments (including
PrusaSlicer, OrcaSlicer and Cura feature names). Files without recognized markers
are unchanged. Excluded moves still update position, tool and extrusion state so
the remaining paths resume correctly. The file still needs to be decoded and its
commands read; exclusions reduce path storage and generated geometry.

## FAQ
**How long do models take to import?**<br>
This depends heavily on the computer you are running on, but imports can take anywhere from just a couple seconds to several minutes depending on the size and complexity of the model that is being imported.

**How do I give the imported paths thickness?**<br>
Paths have thickness by default. Select a tool's curve object, then open Object Data Properties > Geometry. In Simulated Bead mode, Bevel Depth controls half the bead height and Extrude controls half the difference between width and height. These controls still affect all paths for that tool together. Rounded ends are editable curve points. Selecting the root Empty controls transforms; it does not expose curve geometry settings.

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
