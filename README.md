# Simple GCode Importer for Blender

![A 3D render of a low-poly Thinker statue](/images/low_poly_thinker.png)
<sup>*Lowest Poly The Thinker* model by [LXO on Printables.com](https://www.printables.com/model/1165-lowest-poly-the-thinker)</sup>

## About

Simple GCode Importer is a simple plug-in that translates printer GCode into Bezier curve paths in Blender. This results in a very performant representation of the object in a highly customizable format.

## Installation
To install the plugin, add it to Blender from the [Extension website](https://extensions.blender.org/add-ons/simple-gcode-importer/) or straight from Blender via Edit > Preferences > Get Extensions.

## Usage
To import a GCode file to Blender, simply go to File > Import and select the GCode option. In the new file browser window, find and select the .gcode file you would ike to import, then click 'Import Gcode'

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
