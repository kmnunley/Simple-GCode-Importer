"""Build a clearly labeled local test ZIP without editing release metadata."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blender', default='blender', help='Blender executable')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    scratch = root / '.test-tmp'
    scratch.mkdir(exist_ok=True)
    output = root / 'releases'
    output.mkdir(exist_ok=True)
    version = '2.0.0-dev.' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    name = 'Simple Gcode Importer (DEV)'

    with tempfile.TemporaryDirectory(prefix='dev-build-', dir=scratch) as temp:
        source = Path(temp) / 'simple_gcode_importer'
        shutil.copytree(root / 'simple_gcode_importer', source,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        manifest = source / 'blender_manifest.toml'
        text = manifest.read_text(encoding='utf-8')
        text = re.sub(r'^version = .*$', f'version = "{version}"', text, flags=re.M)
        text = re.sub(r'^name = .*$', f'name = "{name}"', text, flags=re.M)
        manifest.write_text(text, encoding='utf-8')

        # Keep legacy metadata consistent in the staged copy as well.
        init = source / '__init__.py'
        text = init.read_text(encoding='utf-8')
        text = re.sub(r'"name": "GCode Importer"', f'"name": "{name}"', text)
        text = re.sub(r'"version": \([\d, ]+\)', '"version": (2, 0, 0)', text)
        init.write_text(text, encoding='utf-8')

        subprocess.run([args.blender, '--background', '--factory-startup',
                        '--command', 'extension', 'build',
                        '--source-dir', str(source), '--output-dir', str(output)],
                       check=True)
        package = output / f'simple_gcode_importer-{version}.zip'
        subprocess.run([args.blender, '--background', '--factory-startup',
                        '--command', 'extension', 'validate',
                        str(package)], check=True)
        print(f'\nLocal test package: {package}', flush=True)
        print('Installs as Simple Gcode Importer (DEV), replacing the same extension ID.')


if __name__ == '__main__':
    main()
