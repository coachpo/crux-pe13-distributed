#!/usr/bin/env python3
"""Prepare a new diagnostic-only context and cold capsules in an isolated container."""
from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from functools import partial
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile

import replay
from container import reject_actions_environment

import cloud_assemble
import cloud_build_plan
import cloud_frontends
import cloud_layers
import cloud_pack
import cloud_reuse
import cloud_schedule
import cloud_shard
import cloud_source


IMAGE = 'crux-local-candidate:jammy'
INPUTS = Path('/inputs')
OUTPUT = Path('/diagnostic-output')
ROOT = replay.ROOT


def closure(context, targets):
    needed = set()
    pending = deque(targets)
    while pending:
        ident = pending.popleft()
        if ident in needed:
            continue
        needed.add(ident)
        pending.extend(context['tasks'][ident]['depends_on'])
    return [ident for wave in context['waves'] for ident in wave if ident in needed]


def graph_binding(manifest_path, graph_dir, index_dir, build):
    manifest_sha = cloud_build_plan.sha(manifest_path)
    index = replay.read(index_dir / 'index-receipt.json')
    graph_run = index.get('graph_run')
    if not isinstance(graph_run, str) or not graph_run.isdecimal():
        raise ValueError('Index must name its genuine freeze run')
    graph = cloud_build_plan.admit_graph(graph_dir / 'graph/graph-receipt.json', index,
                                       {'graph_run': graph_run, 'source_manifest_sha256': manifest_sha}, build)
    source = replay.read(graph_dir / 'source/source-mount.json')
    if (source.get('manifest_sha256') != manifest_sha or source.get('source_root') != graph['source_root']
            or not str(source.get('source_run', '')).isdecimal()):
        raise ValueError('Freeze source mount differs from the pinned source manifest')
    return graph, index


def restore_inputs(manifest_path, graph, index, index_dir, graph_dir, layers_dir, work):
    manifest = replay.read(manifest_path)
    source_name, _, _, _ = cloud_source._validate_manifest(manifest)
    source, out = Path(source_name), Path(graph['out_root'])
    def overlaps(left, right):
        return left == right or left in right.parents or right in left.parents

    if (source_name != graph['source_root'] or not out.is_absolute() or out.name != 'android-out'
            or any(root != root.resolve() for root in (source, out))
            or overlaps(source, out)
            or any(overlaps(root, mount) for root in (source, out) for mount in (INPUTS, OUTPUT, ROOT, work))
            or any(os.path.lexists(path) for path in (source, out))):
        raise ValueError('SOURCE/OUT must be disjoint, fresh container paths outside the transport mounts')
    packed_index = index_dir / 'index.sqlite.zst'
    compressed = index['compressed_database']
    if (compressed['file'] != packed_index.name or packed_index.stat().st_size != compressed['size']
            or cloud_build_plan.sha(packed_index) != compressed['sha256']):
        raise ValueError('Compressed index differs from the indexed artifact receipt')
    database = work / 'native-index.sqlite'
    subprocess.run(['zstd', '-d', str(packed_index), '-o', str(database)], check=True)
    uid = f'{os.getuid()}:{os.getgid()}'
    subprocess.run(['sudo', 'mkdir', '-p', str(out.parent), str(source)], check=True)
    subprocess.run(['sudo', 'chown', uid, str(out.parent), str(source)], check=True)
    subprocess.run(['tar', '--zstd', '--no-same-owner', '-xf', str(graph_dir / 'graph-out.tar.zst'),
                    '-C', str(out.parent)], check=True)
    revisions = {project['path']: project['revision'] for project in manifest['projects']}
    manifest_sha = graph['source_manifest']['sha256']
    for layer in manifest['layers']:
        name = layer['id']
        image = work / (name + '.sqfs')
        downloaded = layers_dir / ('cloud-' + name)
        receipt = cloud_layers.assemble_layer(manifest_sha, layer, revisions, downloaded, image)
        frozen_receipt = graph_dir / 'source' / (name + '.json')
        if not frozen_receipt.is_file() or cloud_build_plan.sha(receipt) != cloud_build_plan.sha(frozen_receipt):
            raise ValueError('Source layer was not the layer mounted by the frozen graph: ' + name)
        # Apply lower-priority layers first, matching the freeze's reversed lowerdir order.
        subprocess.run(['unsquashfs', '-force', '-strict-errors', '-no-progress', '-d', str(source),
                        str(image)], check=True)
        image.unlink()
    print(json.dumps({'phase': 'source-restored', 'layers': len(manifest['layers'])}), flush=True)
    return source, out, database


def build_capsule(ident, *, operation, shards, inventory, receipt, manifest, pruned, destination):
    print(json.dumps({'phase': operation, 'task': ident}), flush=True)
    function = cloud_pack.audit_shard if operation == 'audit' else cloud_pack.pack_shard
    directory = destination / ident
    status, result = function(shards, ident, inventory, receipt, manifest, pruned, directory)
    cloud_build_plan.write(directory / 'pack-receipt.json', result)
    if status:
        raise ValueError(f"Candidate {operation} failed for {ident}: {result.get('error')}")
    return result


def capsule_results(tasks, jobs, **arguments):
    build = partial(build_capsule, **arguments)
    if jobs == 1:
        yield from map(build, tasks)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as workers:
            yield from workers.map(build, tasks)


def prepare(inputs, destination, request, work):
    reject_actions_environment(os.environ)
    manifest = inputs / 'source-manifest.json'
    graph_dir, index_dir = inputs / 'graph', inputs / 'index'
    graph, index = graph_binding(manifest, graph_dir, index_dir, request['build_number'])
    _, out, database = restore_inputs(manifest, graph, index, index_dir, graph_dir, inputs / 'source', work)
    scalars = {name: (out / path).read_text() for name, path in cloud_frontends.SCALAR_OUTPUTS}
    policy = cloud_schedule.load_policy()
    evidence = destination / 'slices'
    # Retain genuine input provenance in the slice evidence only. There is no local run ID.
    cloud_shard.integrate(argparse.Namespace(
        output=evidence, graph_run=index['graph_run'], index_run=None, source_manifest=manifest,
        graph_evidence=graph_dir, index_input=index_dir, database=database, build_number=request['build_number'],
        ownership=ROOT / 'config/cloud-ownership-profile.json', max_actions=policy['max_actions'],
        max_parallel=policy['max_parallel'], partition_profile=cloud_schedule.PROFILE,
        runtime_profile=ROOT / 'config/cloud-runtime-profile.json'))
    shards, receipt = evidence / 'shards', evidence / 'shard-receipt.json'
    inventory = evidence / 'slice-inventory.json'
    pruned = evidence / 'index-pruned.sqlite'
    plan_path = shards / 'plan.json'
    plan = replay.read(plan_path)
    _, waves = cloud_schedule.validate_plan(plan)
    all_tasks = [ident for wave in waves for ident in wave]
    unknown = set(request['tasks']) - set(all_tasks)
    if unknown:
        raise ValueError('Requested task is absent from the fresh plan: ' + ', '.join(sorted(unknown)))
    common = dict(shards=shards, inventory=inventory, receipt=receipt, manifest=manifest, pruned=pruned)
    rows = list(capsule_results(all_tasks, request['jobs'], operation='audit',
                                destination=destination / 'audits', **common))
    plan_sha = cloud_build_plan.sha(plan_path)
    waves, tasks = cloud_build_plan.admit_task_capsules(replay.read(inventory), plan, plan_sha, rows, len(rows))
    implementation = cloud_reuse.execution_digest(ROOT)
    context = cloud_build_plan.compose_context(
        shards, plan, waves, tasks, replay.read(receipt), manifest, graph, scalars,
        {'schema_version': 1, 'version': 1, 'force_cold': True, 'execution_digest': implementation}, plan_sha)
    context.update(diagnostic_only=True, local_preparation={'schema': replay.SCHEMA,
                                                           'tool_commit': request['tool_commit']})
    for task in tasks:
        replay.validate_reference(context, task)
    cloud_assemble.derive_identity(context)
    _, required = cloud_assemble.required_exports(context)
    selected = closure(context, set(request['tasks']) | set(required))
    capsules = destination / 'capsules'
    for packed in capsule_results(selected, request['jobs'], operation='pack', destination=capsules, **common):
        ident = packed['shard_id']
        metadata = replay.read(capsules / ident / (ident + '.json'))
        descriptor = cloud_reuse.input_descriptor(replay.read(shards / ident / 'manifest.json'),
                                                  metadata, implementation)
        if descriptor != tasks[ident]['reuse_descriptor']:
            raise ValueError('Packed candidate inputs differ from their independent audit: ' + ident)
    shutil.copyfile(manifest, destination / manifest.name)
    cloud_build_plan.write(destination / 'context.json', context)
    return {'status': 'success', 'diagnostic_only': True, 'plan_sha256': plan_sha,
            'tasks': len(tasks), 'packed': selected, 'assembly_producers': sorted(required)}


def launch(args):
    inputs, destination = args.inputs.resolve(strict=True), args.output.resolve()
    if inputs == destination or inputs in destination.parents or destination in inputs.parents:
        raise ValueError('Preparation inputs and fresh result must be separate')
    if any(',' in str(path) for path in (inputs, destination)):
        raise ValueError('Docker bind paths must not contain commas')
    for expected in ('source-manifest.json', 'graph/graph/graph-receipt.json', 'index/index-receipt.json', 'source'):
        if not (inputs / expected).exists():
            raise ValueError('Missing verified preparation input: ' + expected)
    git = ['git', '-C', str(ROOT), 'rev-parse', '--verify']
    commit = args.tool_commit or subprocess.check_output([*git, 'HEAD'], text=True).strip()
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('The tool commit must be the full reference/base commit')
    image = subprocess.check_output(['docker', '--context', 'orbstack', 'image', 'inspect',
                                     '--format', '{{.Id}}', args.image], text=True).strip()
    destination.mkdir(parents=True, exist_ok=False)
    destination.chmod(0o777)
    with tempfile.TemporaryDirectory(prefix='crux-preparation-request-', dir=destination.parent) as temporary:
        temporary = Path(temporary)
        code = temporary / 'implementation'
        implementation = replay.snapshot_code(code)
        implementation['candidate_preparation'] = {
            name: cloud_build_plan.sha(code / 'local_replay' / name)
            for name in ('candidate.py', 'candidate.Dockerfile')}
        request = temporary / 'request.json'
        cloud_build_plan.write(request, {'build_number': args.build_number, 'tasks': args.task,
                                        'tool_commit': commit, 'jobs': args.jobs})
        command = ['docker', '--context', 'orbstack', 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
                   '--mount', f'type=bind,src={inputs},dst={INPUTS},readonly',
                   '--mount', f'type=bind,src={request},dst=/request.json,readonly',
                   '--mount', f'type=bind,src={code},dst=/opt/crux-code,readonly',
                   '--mount', f'type=bind,src={destination},dst={OUTPUT}',
                   image, 'python3', '/opt/crux-code/local_replay/candidate.py', 'container']
        cloud_build_plan.write(destination / 'invocation.json', {
            'diagnostic_only': True, 'build_number': args.build_number, 'tool_commit': commit,
            'tasks': args.task, 'jobs': args.jobs,
            'implementation': implementation, 'image_id': image, 'host_architecture': platform.machine()})
        try:
            with (destination / 'container.log').open('w') as log:
                return subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode
        finally:
            shutil.copytree(code, destination / 'implementation')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    host = sub.add_parser('prepare', help='run networkless preparation in OrbStack')
    host.add_argument('--inputs', type=Path, required=True)
    host.add_argument('--output', type=Path, required=True)
    host.add_argument('--build-number', required=True)
    host.add_argument('--tool-commit', help='full reference commit when invoking from a code snapshot')
    host.add_argument('--task', action='append', default=[], help='extra task ID and its transitive predecessors')
    host.add_argument('--jobs', type=int, default=1, help='independent audit/pack workers')
    host.add_argument('--image', default=IMAGE)
    sub.add_parser('container', help='internal isolated preparation process')
    args = parser.parse_args()
    if args.command == 'prepare':
        if not re.fullmatch('[0-9]{10}', args.build_number):
            parser.error('--build-number must be YYYYMMDDNN')
        if args.jobs < 1:
            parser.error('--jobs must be positive')
        return launch(args)
    try:
        with tempfile.TemporaryDirectory(prefix='crux-candidate-') as temporary:
            result = prepare(INPUTS, OUTPUT, replay.read('/request.json'), Path(temporary))
    except Exception as error:
        cloud_build_plan.write(OUTPUT / 'preparation.json', {'status': 'failed', 'diagnostic_only': True,
                                                            'error': str(error)})
        raise
    cloud_build_plan.write(OUTPUT / 'preparation.json', result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
