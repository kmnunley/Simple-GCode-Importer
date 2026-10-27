build:
	blender --command extension build --source-dir="./simple_gcode_importer" --output-dir="./releases"

build-dev:
	python scripts/build_dev.py
