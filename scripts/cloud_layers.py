#!/usr/bin/env python3
"""Restore and mount complete source layers from a standard-runner source run."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def restore_layer(manifest_sha, layer, revisions, run_id, repository, staging):
    name = layer['id']
    directory = staging / name
    directory.mkdir()
    subprocess.run(['gh', 'run', 'download', str(run_id), '--repo', repository,
                    '--name', 'cloud-' + name, '--dir', str(directory)], check=True)
    receipt_path = directory / 'layer.json'
    receipt = json.loads(receipt_path.read_text())
    if receipt['layer_id'] != name or receipt['manifest_sha256'] != manifest_sha:
        raise ValueError('Source layer identity differs from the pinned manifest: ' + name)
    expected = set(layer['projects'])
    projects = receipt['projects']
    successes = {'fetched-and-extracted', 'fetched-bundle-and-archived', 'fetched-git-and-archived'}
    if (len(projects) != len(expected) or {item['path'] for item in projects} != expected
            or any(item['status'] not in successes or item['revision'] != revisions[item['path']]
                   for item in projects)):
        raise ValueError('Incomplete source layer: ' + name)
    image = staging / (name + '.sqfs')
    parts = directory / receipt['squashfs']['parts_directory']
    with image.open('xb') as output:
        for part in receipt['squashfs']['parts']:
            source = parts / part['name']
            if source.stat().st_size != part['size'] or digest(source) != part['sha256']:
                raise ValueError('Source image part failed identity check: ' + str(source))
            with source.open('rb') as stream:
                shutil.copyfileobj(stream, output, 8 * 1024 * 1024)
            source.unlink()
    if image.stat().st_size != receipt['squashfs']['size']:
        raise ValueError('Reassembled source image has incorrect size: ' + name)
    print(json.dumps({'layer': name, 'bytes': image.stat().st_size, 'restored': True}), flush=True)
    return image, receipt_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--source-run', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--staging', required=True)
    parser.add_argument('--evidence', required=True)
    args = parser.parse_args()
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text())
    manifest_sha = digest(manifest_path)
    revisions = {project['path']: project['revision'] for project in manifest['projects']}
    staging = Path(args.staging).resolve()
    staging.mkdir(parents=True, exist_ok=False)
    evidence = Path(args.evidence).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    source = Path(manifest['source_root'])
    if source.exists() and any(source.iterdir()):
        raise ValueError('Source mount point must be empty: ' + str(source))
    with ThreadPoolExecutor(max_workers=6) as pool:
        outputs = list(pool.map(lambda layer: restore_layer(manifest_sha, layer, revisions, args.source_run,
                                                           args.repository, staging), manifest['layers']))
    mounts = []
    for image, receipt in outputs:
        destination = staging / (image.stem + '-mount')
        destination.mkdir()
        subprocess.run(['sudo', 'mount', '-t', 'squashfs', '-o', 'loop,ro', str(image), str(destination)], check=True)
        mounts.append(destination)
        shutil.copyfile(receipt, evidence / (image.stem + '.json'))
    upper = staging / 'upper'
    work = staging / 'work'
    upper.mkdir()
    work.mkdir()
    subprocess.run(['sudo', 'mkdir', '-p', str(source)], check=True)
    options = 'lowerdir=' + ':'.join(str(item) for item in reversed(mounts))
    options += ',upperdir=' + str(upper) + ',workdir=' + str(work)
    subprocess.run(['sudo', 'mount', '-t', 'overlay', 'overlay', '-o', options, str(source)], check=True)
    uid_gid = f'{os.getuid()}:{os.getgid()}'
    # The one editable host launcher is copied into the writable overlay by chown.
    subprocess.run(['sudo', 'chown', uid_gid, str(source),
                    str(source / 'build/soong/ui/build/soong.go')], check=True)
    subprocess.run(['findmnt', '-R', str(source)], check=True)
    receipt = {'schema_version': 1, 'source_run': args.source_run,
               'manifest_sha256': manifest_sha, 'source_root': str(source),
               'layer_count': len(outputs), 'images_bytes': sum(image.stat().st_size for image, _ in outputs),
               'mounts': [str(item) for item in mounts], 'writable_overlay': str(upper)}
    (evidence / 'source-mount.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
