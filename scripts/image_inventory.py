#!/usr/bin/env python3
"""Require every installed system and vendor file of the admitted plan in its image.

The shard manifests of an admitted plan declare every output under the system
and vendor staging trees. A task that scans a staging tree (an installed-files
list or the image itself) sees only the files it imports, so a missing
transport edge silently drops installed files from the image while the reader
still succeeds. This check lists each expanded raw ext4 image with debugfs and
fails when any declared installed path is absent.

    image_inventory.py --context context.json --system system.raw.img \
        --vendor vendor.raw.img --report image-inventory.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import crux_zip

# Image path of each staging tree's root: system images hold /system, vendor
# images are the vendor tree itself.
PARTITIONS = {'system': '/system', 'vendor': ''}


def declared(outputs, out_root):
    """Relative installed paths under each image staging tree, from declared plan outputs."""
    product = str(out_root).rstrip('/') + '/target/product/crux/'
    inventory = {}
    for partition in PARTITIONS:
        prefix = product + partition + '/'
        inventory[partition] = sorted({path[len(prefix):].rstrip('/') for path in outputs
                                       if path.startswith(prefix)} - {''})
    return inventory


def image_paths(image, debugfs=None):
    """Every file, directory and link path in an ext4 image, listed one directory level per debugfs run."""
    debugfs = debugfs or crux_zip.debugfs()
    paths, level = set(), ['/']
    while level:
        if any('"' in directory or '\n' in directory for directory in level):
            raise ValueError(f'{image}: directory name cannot be passed to debugfs')
        with tempfile.NamedTemporaryFile('w', suffix='.debugfs') as commands:
            commands.write(''.join(f'ls -p "{directory}"\n' for directory in level))
            commands.flush()
            done = subprocess.run([debugfs, '-f', commands.name, str(image)], capture_output=True, timeout=1800)
        if done.returncode:
            raise ValueError(f'{image}: debugfs failed: ' + done.stderr.decode(errors='replace'))
        current, following = None, []
        for line in done.stdout.decode(errors='surrogateescape').splitlines():
            if line.startswith('debugfs: ls -p "') and line.endswith('"'):
                current = line[len('debugfs: ls -p "'):-1]
                continue
            # ls -p rows are /inode/mode/uid/gid/name/size/.
            fields = line.split('/')
            if current is None or len(fields) != 8 or fields[0] or fields[7]:
                continue
            mode, name = fields[2], fields[5]
            if name in ('', '.', '..'):
                continue
            path = current.rstrip('/') + '/' + name
            paths.add(path)
            if mode.startswith('040'):
                following.append(path)
        level = following
    return paths


def missing(image, partition, expected, debugfs=None):
    present = image_paths(image, debugfs)
    root = PARTITIONS[partition]
    return sorted(path for path in (root + '/' + relative for relative in expected) if path not in present)


def check(inventory, images, debugfs=None):
    report = {}
    for partition, image in images.items():
        expected = inventory[partition]
        if not expected:
            raise ValueError(f'The admitted plan declares no {partition} installed files')
        absent = missing(image, partition, expected, debugfs)
        report[partition] = {'declared': len(expected), 'missing': absent}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--context', required=True, type=Path)
    parser.add_argument('--system', required=True, type=Path, help='Expanded raw system ext4 image')
    parser.add_argument('--vendor', required=True, type=Path, help='Expanded raw vendor ext4 image')
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    inventory = json.loads(args.context.read_text())['image_inventory']
    report = check(inventory, {'system': args.system, 'vendor': args.vendor})
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    failures = {partition: entry['missing'] for partition, entry in report.items() if entry['missing']}
    for partition, entry in report.items():
        print(f"{partition}: {entry['declared']} declared installed files, {len(entry['missing'])} missing")
    if failures:
        raise SystemExit('Installed files missing from the image: ' + json.dumps(failures))
    return 0


if __name__ == '__main__':
    sys.exit(main())
