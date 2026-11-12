"""Version main's first-parent history and build a release ZIP from a checkout."""

import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
from zipfile import ZipFile


# The performance and progress merge gets 2.1.0 and covers the accumulated
# changes since v1.2.1. Every later first-parent commit adds a bump.
VERSIONING_BASE = '1b26b6a7b9b66e93c9f5223e635f498ae871d8cf'
BASE_VERSION = (2, 1, 0)
EXTENSION = 'simple_gcode_importer'
CONVENTIONAL_HEADER = re.compile(
    r'(?P<type>[a-z][a-z0-9-]*)(?:\([^()\n]+\))?(?P<breaking>!)?: ',
    re.IGNORECASE | re.ASCII,
)


def git(*args):
    return subprocess.check_output(['git', *args], encoding='utf-8', errors='replace')


def version_for(commit):
    history = git('rev-list', '--first-parent', commit).splitlines()
    if VERSIONING_BASE not in history:
        raise ValueError('Select the versioning baseline or a commit with it in its first-parent history.')

    messages = git('log', '-z', '--format=%B', '--first-parent', '--reverse',
                   f'{VERSIONING_BASE}..{commit}').split('\0')
    major, minor, patch = BASE_VERSION
    for message in messages[:-1]:
        subject = message.splitlines()[0] if message else ''
        header = CONVENTIONAL_HEADER.match(subject)
        breaking_note = re.search(r'^BREAKING[ -]CHANGE: \S', message, re.M)
        if (header and header['breaking']) or breaking_note:
            major, minor, patch = major + 1, 0, 0
        elif header and header['type'].lower() == 'feat':
            minor, patch = minor + 1, 0
        else:
            patch += 1
    return f'{major}.{minor}.{patch}'


def replace_version(path, pattern, replacement):
    updated, count = re.subn(pattern, replacement, path.read_text(encoding='utf-8'), flags=re.M)
    if count != 1:
        raise ValueError(f'Expected exactly one version field in {path}, found {count}.')
    path.write_text(updated, encoding='utf-8')


def build(source, output, version, blender):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        staged = Path(temp) / EXTENSION
        shutil.copytree(source / EXTENSION, staged,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        replace_version(staged / 'blender_manifest.toml', r'^version\s*=\s*"[^"]+"$',
                        f'version = "{version}"')
        legacy_version = ', '.join(version.split('.'))
        replace_version(staged / '__init__.py', r'^(\s*"version": )\([\d, ]+\),$',
                        rf'\g<1>({legacy_version}),')

        command = [blender, '--background', '--factory-startup', '--command', 'extension']
        subprocess.run([*command, 'build', '--source-dir', str(staged),
                        '--output-dir', str(output)], check=True)
        package = output / f'{EXTENSION}-{version}.zip'
        subprocess.run([*command, 'validate', str(package)], check=True)
        with ZipFile(package) as archive:
            manifest = tomllib.loads(archive.read('blender_manifest.toml').decode('utf-8'))
            if manifest['version'] != version:
                raise ValueError(f'Unexpected manifest version in {package}.')
        with package.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        package.with_suffix('.zip.sha256').write_text(f'{checksum}  {package.name}\n', encoding='ascii')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    version_parser = commands.add_parser('version')
    version_parser.add_argument('commit', nargs='?', default='HEAD')
    build_parser = commands.add_parser('build')
    build_parser.add_argument('source', type=Path)
    build_parser.add_argument('output', type=Path)
    build_parser.add_argument('--blender', default='blender')
    args = parser.parse_args()
    try:
        if args.command == 'version':
            commit = git('rev-parse', '--verify', '--end-of-options', f'{args.commit}^{{commit}}').strip()
            print(version_for(commit))
        else:
            # CI creates a detached worktree for the selected main commit.
            commit = subprocess.check_output(['git', '-C', str(args.source), 'rev-parse', 'HEAD'],
                                             encoding='ascii').strip()
            build(args.source.resolve(), args.output.resolve(), version_for(commit), args.blender)
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
