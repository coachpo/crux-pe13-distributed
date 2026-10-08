#!/usr/bin/env python3
"""Freeze dispatch waves, optionally preparing cold capsules when a wave is ready."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


ASSET_PART_BYTES = 1792 * 1024 * 1024  # Below GitHub's 2 GiB release-asset limit.


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def frozen_file(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': digest(path)}


def assets_for(archive):
    archive = Path(archive)
    if not archive.is_file():
        raise ValueError(f'Missing cold input capsule: {archive}')
    if archive.stat().st_size < ASSET_PART_BYTES:
        return [archive]
    parts = []
    with archive.open('rb') as source:
        number = 0
        while source.tell() < archive.stat().st_size:
            part = archive.with_name(archive.name + f'.part-{number:04d}')
            remaining = ASSET_PART_BYTES
            with part.open('wb') as destination:
                while remaining:
                    data = source.read(min(remaining, 64 * 1024 * 1024))
                    if not data:
                        break
                    destination.write(data)
                    remaining -= len(data)
            parts.append(part)
            number += 1
    return parts


def convert(graph_plan, capsule_dir, source_archives=None, preparation=None, graph_dir=None):
    located = {job['id']: index for index, jobs in enumerate(graph_plan['waves']) for job in jobs}
    if len(located) != graph_plan['job_count']:
        raise ValueError('Duplicate or inconsistent graph shard IDs')
    waves = []
    uploads = []
    for index, jobs in enumerate(graph_plan['waves']):
        tasks = []
        for job in jobs:
            archive = Path(capsule_dir).resolve() / (job['id'] + '.tar.zst')
            task = {'id': job['id']}
            if preparation:
                manifest = Path(graph_dir).resolve() / job['manifest']
                slice_files = [frozen_file(path) for path in sorted(manifest.parent.rglob('*'))
                               if path.is_file()]
                task['preparation'] = {'archive': str(archive), 'manifest': str(manifest),
                                       'slice_files': slice_files}
                metadata = archive.with_name(archive.name.removesuffix('.tar.zst') + '.json')
                if archive.is_file() and metadata.is_file():
                    task['preparation']['existing_metadata'] = frozen_file(metadata)
            else:
                parts = assets_for(archive)
                uploads.extend(parts)
                task['capsule_assets'] = [part.name for part in parts]
            dependencies = []
            for identity in job['depends_on']:
                if located[identity] >= index:
                    raise ValueError(f'Dependency is not in an earlier wave: {identity}')
                dependencies.append({'wave': located[identity], 'shard': identity})
            task['dependencies'] = dependencies
            for key in ('shared_capsules', 'source_overlays', 'source_archives'):
                if key in job:
                    task[key] = job[key]
            if source_archives:
                task['source_archives'] = source_archives
            tasks.append(task)
        waves.append({'id': index, 'tasks': tasks})
    terminals = []
    for path in graph_plan.get('terminal_outputs', []):
        identity = graph_plan['export_producers'][path]
        terminals.append({'path': path, 'wave': located[identity], 'shard': identity})
    plan = {'schema_version': 1, 'source_root': graph_plan['source_root'],
            'targets': graph_plan['targets'], 'action_count': graph_plan['action_count'],
            'terminal_outputs': terminals, 'waves': waves}
    if preparation:
        plan['schema_version'] = 2
        plan['preparation'] = preparation
    return plan, uploads


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph-plan', required=True)
    parser.add_argument('--capsule-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--upload', action='store_true')
    parser.add_argument('--repo')
    parser.add_argument('--tag')
    parser.add_argument('--lazy', action='store_true', help='pack and upload each wave on demand')
    parser.add_argument('--pack-command', help='JSON array prefix, e.g. ["orb", "-m", "cruxbuild", "python3", "/path/capsule.py"]')
    parser.add_argument('--out-root', default='/home/qingli/crux-pe13-install-regression-2026-10-08/android-out')
    parser.add_argument('--shared-manifest')
    parser.add_argument('--digest-cache')
    parser.add_argument('--source-profile', help='frozen public source archive profile to upload')
    args = parser.parse_args()
    if args.lazy and args.upload:
        parser.error('--lazy uploads through scheduler, not --upload')
    graph_path = Path(args.graph_plan).resolve()
    preparation = None
    if args.lazy:
        if not args.pack_command:
            parser.error('--lazy requires --pack-command')
        command = json.loads(args.pack_command)
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            parser.error('--pack-command must be a non-empty JSON array of strings')
        preparation = {'pack_command': command, 'out_root': args.out_root,
                       'graph_plan': frozen_file(graph_path)}
        if args.shared_manifest:
            preparation['shared_manifest'] = frozen_file(args.shared_manifest)
        if args.digest_cache:
            preparation['digest_cache'] = str(Path(args.digest_cache).resolve())
    source_archives = None
    if args.source_profile:
        source_archives = {'profile_asset': Path(args.source_profile).name}
    plan, uploads = convert(json.loads(graph_path.read_text()), args.capsule_dir,
                            source_archives, preparation, graph_path.parent)
    if args.source_profile:
        plan['release_inputs'] = [frozen_file(args.source_profile)]
        uploads.append(Path(args.source_profile))
    Path(args.output).write_text(json.dumps(plan, indent=2) + '\n')
    if args.upload:
        if not args.repo or not args.tag:
            parser.error('--upload requires --repo and --tag')
        for path in uploads:
            print(f'Uploading {path.name} ({path.stat().st_size} bytes)', flush=True)
            subprocess.run(['gh', 'release', 'upload', args.tag, str(path),
                            '--repo', args.repo], check=True)
    print(json.dumps({'waves': len(plan['waves']), 'tasks': sum(len(w['tasks']) for w in plan['waves']),
                      'assets': len(uploads)}))


if __name__ == '__main__':
    main()
