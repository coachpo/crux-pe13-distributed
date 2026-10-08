#!/usr/bin/env python3
"""Convert graph shards and existing cold capsules into dispatchable waves."""
import argparse
import json
from pathlib import Path
import subprocess


ASSET_PART_BYTES = 1792 * 1024 * 1024  # Below GitHub's 2 GiB release-asset limit.


def assets_for(archive):
    archive = Path(archive)
    if not archive.is_file():
        raise ValueError(f'Missing cold input capsule: {archive}')
    if archive.stat().st_size < ASSET_PART_BYTES:
        return [archive]
    parts = []
    with archive.open('rb') as source:
        number = 0
        while True:
            data = source.read(ASSET_PART_BYTES)
            if not data:
                break
            part = archive.with_name(archive.name + f'.part-{number:04d}')
            part.write_bytes(data)
            parts.append(part)
            number += 1
    return parts


def convert(graph_plan, capsule_dir):
    located = {job['id']: index for index, jobs in enumerate(graph_plan['waves']) for job in jobs}
    if len(located) != graph_plan['job_count']:
        raise ValueError('Duplicate or inconsistent graph shard IDs')
    waves = []
    uploads = []
    for index, jobs in enumerate(graph_plan['waves']):
        tasks = []
        for job in jobs:
            parts = assets_for(Path(capsule_dir) / (job['id'] + '.tar.zst'))
            uploads.extend(parts)
            dependencies = []
            for identity in job['depends_on']:
                if located[identity] >= index:
                    raise ValueError(f'Dependency is not in an earlier wave: {identity}')
                dependencies.append({'wave': located[identity], 'shard': identity})
            tasks.append({'id': job['id'], 'capsule_assets': [part.name for part in parts],
                          'dependencies': dependencies})
        waves.append({'id': index, 'tasks': tasks})
    terminals = []
    for path in graph_plan.get('terminal_outputs', []):
        identity = graph_plan['export_producers'][path]
        terminals.append({'path': path, 'wave': located[identity], 'shard': identity})
    plan = {'schema_version': 1, 'source_root': graph_plan['source_root'],
            'targets': graph_plan['targets'], 'action_count': graph_plan['action_count'],
            'terminal_outputs': terminals, 'waves': waves}
    return plan, uploads


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph-plan', required=True)
    parser.add_argument('--capsule-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--upload', action='store_true')
    parser.add_argument('--repo')
    parser.add_argument('--tag')
    args = parser.parse_args()
    plan, uploads = convert(json.loads(Path(args.graph_plan).read_text()), args.capsule_dir)
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
