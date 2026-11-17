#!/usr/bin/env python3
"""Prepare and run a single local diagnostic in a fresh OrbStack container."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / 'scripts'))
import cloud_compile
import cloud_build_plan
import cloud_reuse
import cloud_schedule
import worker

SCHEMA = 'crux-local-replay/1'
IMAGE = 'crux-local-replay:jammy'
LOCAL_CODE_FILES = ('replay.py', 'container.py', 'assembly.py', 'Dockerfile')


def read(path):
    return json.loads(Path(path).read_text())


def file_record(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('Expected a regular input file: ' + str(path))
    return {'path': str(path), 'bytes': path.stat().st_size, 'sha256': worker.digest(path)}


def check_file(record, path=None):
    path = Path(record['path'] if path is None else path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Input is not a regular file: ' + str(path))
    if path.stat().st_size != record['bytes'] or worker.digest(path) != record['sha256']:
        raise ValueError('Input bytes/SHA changed: ' + str(path))
    return path


def local_preparation(context):
    return (context.get('diagnostic_only') is True
            and context.get('local_preparation', {}).get('schema') == SCHEMA)


def validate_reference(context, task_id):
    """Check reference data, not the identity of a pretend GitHub execution."""
    cloud_schedule.validate_context(context)
    if context.get('schema_version') != 1 or task_id not in context['tasks']:
        raise ValueError('Task is absent from the reference context')
    if local_preparation(context):
        if any(key in context for key in ('execution_id', 'worker_commit', 'repository',
                                          'pack_run', 'shard_run', 'graph_run', 'index_run', 'source_run')):
            raise ValueError('Local preparation must not invent an Actions identity')
        commit = context['local_preparation'].get('tool_commit')
    else:
        commit = context.get('worker_commit')
    if not re.fullmatch('[a-f0-9]{40}', str(commit or '')):
        raise ValueError('Invalid reference code commit')
    for field in ('source_manifest_sha256', 'plan_sha256'):
        if not re.fullmatch('[a-f0-9]{64}', str(context.get(field, ''))):
            raise ValueError('Invalid reference ' + field)
    source, out = (Path(context[key]) for key in ('source_root', 'out_root'))
    if (not source.is_absolute() or not out.is_absolute() or source == out
            or source in out.parents or out in source.parents):
        raise ValueError('Reference SOURCE and OUT must be separate absolute paths')
    reserved = (Path('/inputs'), Path('/opt/crux-code'), Path('/diagnostic-output'))
    if any(path == root or path in root.parents or root in path.parents
           for path in (source, out) for root in reserved):
        raise ValueError('Reference roots overlap a container transport mount')
    task = context['tasks'][task_id]
    if task.get('packing') != 'deferred':
        raise ValueError('Select the current deferred-capsule contract')
    selection = context.get('source_selection', {})
    projects = selection.get('projects', [])
    if (selection.get('schema_version') != 1 or selection.get('build_number') != context['build_number']
            or selection.get('source_manifest_sha256') != context['source_manifest_sha256']
            or not projects or len({item['path'] for item in projects}) != len(projects)
            or any(not re.fullmatch('[a-f0-9]{40}', str(item.get('revision', ''))) for item in projects)):
        raise ValueError('Reference has no complete pinned source selection')
    return task


def admit_dependency(context, task_id, directory, kind, context_sha, actions_context=None):
    directory = Path(directory)
    receipt = read(directory / 'receipt.json')
    if kind == 'actions':
        reference = context
        if local_preparation(context):
            if actions_context is None or local_preparation(actions_context):
                raise ValueError('An Actions predecessor needs its genuine origin context')
            reference = actions_context
            expected, original = context['tasks'][task_id], reference['tasks'][task_id]
            if (any(context[key] != reference[key] for key in
                    ('source_root', 'out_root', 'source_manifest_sha256', 'source_selection'))
                    or any(expected.get(key, []) != original.get(key, []) for key in
                           ('manifest_sha256', 'extra_exports', 'local_runtime_targets',
                            'depends_on', 'runtime_inputs', 'required_notice_sources'))):
                raise ValueError('Actions predecessor does not match the local source/task contract')
        cloud_build_plan.validate_receipt(reference, receipt, task_id)
        identity = {key: reference[key] for key in cloud_compile.FRESH_BUILD_FIELDS}
        return cloud_compile._read_fresh_dependency(task_id, directory, reference, identity)
    if kind != 'local':
        raise ValueError('Dependency kind must be actions or local')
    expected = {'id': task_id, 'status': 'success', 'diagnostic_only': True,
                'source_root': context['source_root'], 'out_root': context['out_root'],
                'manifest_sha256': context['tasks'][task_id]['manifest_sha256']}
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError('Local dependency is not bound to this task/context')
    diagnostic = receipt.get('local_replay', {})
    if diagnostic.get('schema') != SCHEMA or diagnostic.get('context_sha256') != context_sha:
        raise ValueError('Local dependency has a different reference context')
    if any(key in receipt for key in ('run_id', 'worker_commit', 'fresh_build', 'reuse')):
        raise ValueError('Local diagnostic must not impersonate an Actions receipt')
    task = context['tasks'][task_id]
    if (sorted(receipt.get('additional_exports', [])) != sorted(task.get('extra_exports', []))
            or receipt.get('prebuild_targets', []) != task.get('local_runtime_targets', [])
            or receipt.get('build_environment', {}).get('CCACHE_DISABLE') != '1'):
        raise ValueError('Local predecessor changed the production export/runtime contract')
    return receipt


def bind_dependencies(context, expected, dependencies, context_sha, actions_context):
    bindings = []
    for ident, kind, directory in dependencies:
        if ident not in expected or any(item['id'] == ident for item in bindings):
            raise ValueError('Undeclared or duplicate predecessor: ' + ident)
        admit_dependency(context, ident, directory, kind, context_sha, actions_context)
        receipt = file_record(Path(directory) / 'receipt.json')
        outputs = file_record(Path(directory) / 'outputs.tar.zst')
        if outputs['sha256'] != read(receipt['path'])['archive_sha256']:
            raise ValueError('Dependency output archive differs from its receipt')
        bindings.append({'id': ident, 'kind': kind, 'receipt': receipt, 'archive': outputs})
    if {item['id'] for item in bindings} != set(expected):
        raise ValueError('Every declared predecessor must be supplied, without filtering')
    return bindings


def prepare(context_path, task_id, capsule_directory, dependencies, actions_context_path=None):
    context_record = file_record(context_path)
    context = read(context_path)
    task = validate_reference(context, task_id)
    implementation = cloud_reuse.execution_digest(ROOT)
    if implementation != context['reuse_policy']['execution_digest']:
        raise ValueError('Production code/profile differs from the selected context')
    capsule_directory = Path(capsule_directory)
    packed = read(capsule_directory / 'pack-receipt.json')
    if (packed.get('status') != 'success' or packed.get('shard_id') != task_id
            or packed.get('slice_manifest_sha256') != task['manifest_sha256']
            or packed.get('source_manifest_sha256') != context['source_manifest_sha256']
            or packed.get('plan_sha256') != context['plan_sha256']):
        raise ValueError('Capsule packing receipt differs from the selected audit')
    if not local_preparation(context) and (
            packed.get('execution_id') != context['execution_id']
            or packed.get('reuse_descriptor') != task['reuse_descriptor']
            or packed.get('input_identity') != task['input_identity']):
        raise ValueError('Capsule packing receipt differs from the selected audit')
    name = packed['archive']
    if Path(name).name != name:
        raise ValueError('Capsule archive name must be a basename')
    archive = file_record(capsule_directory / name)
    if archive['bytes'] != packed['archive_bytes'] or archive['sha256'] != packed['archive_sha256']:
        raise ValueError('Capsule archive differs from its packing receipt')
    metadata = file_record(capsule_directory / (task_id + '.json'))
    actions_record = file_record(actions_context_path) if actions_context_path is not None else None
    actions_context = read(actions_context_path) if actions_record else None
    bindings = bind_dependencies(context, task['depends_on'], dependencies, context_record['sha256'], actions_context)
    result = {'schema': SCHEMA, 'diagnostic_only': True, 'operation': 'native', 'task_id': task_id,
            'context': context_record, 'execution_digest': implementation,
            'pack_receipt': file_record(capsule_directory / 'pack-receipt.json'),
            'capsule': archive, 'metadata': metadata, 'dependencies': bindings}
    if actions_record:
        result['actions_context'] = actions_record
    return result


def prepare_assembly(context_path, source_manifest, dependencies, actions_context_path=None):
    import cloud_assemble
    from assembly import source_selection
    context_record = file_record(context_path)
    context = read(context_path)
    validate_reference(context, next(iter(context['tasks'])))
    cloud_assemble.derive_identity(context)
    source_selection(context, Path(source_manifest))
    _, producers = cloud_assemble.required_exports(context)
    implementation = cloud_reuse.execution_digest(ROOT)
    if implementation != context['reuse_policy']['execution_digest']:
        raise ValueError('Assembly production code differs from the selected context')
    actions_record = file_record(actions_context_path) if actions_context_path else None
    bindings = bind_dependencies(context, producers, dependencies, context_record['sha256'],
                                 read(actions_context_path) if actions_record else None)
    result = {'schema': SCHEMA, 'diagnostic_only': True, 'operation': 'assembly',
              'context': context_record, 'source_manifest': file_record(source_manifest),
              'execution_digest': implementation, 'dependencies': bindings}
    if actions_record:
        result['actions_context'] = actions_record
    return result


def docker_command(record_path, record, destination, image=IMAGE, code_root=ROOT):
    context = read(record['context']['path'])
    assembly = record.get('operation') == 'assembly'
    task = validate_reference(context, next(iter(context['tasks'])) if assembly else record['task_id'])
    command = ['docker', '--context', 'orbstack', 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
               '--label', 'crux.local-replay=diagnostic-only']
    kernel = str(Path(context['out_root']) / 'target/product/crux/obj/KERNEL_OBJ')
    if not assembly and context.get('assembly_exports', {}).get(kernel) == task['id']:
        command += ['--cap-add', 'SYS_ADMIN', '--security-opt', 'apparmor=unconfined']
    mounts = [(Path(code_root) / name, '/opt/crux-code/' + name, True)
              for name in ('scripts', 'config', '.github/workflows', '.github/actions', 'local_replay')]
    mounts += [(record_path, '/inputs/input.json', True),
              (record['context']['path'], '/inputs/context.json', True),
              (destination, '/diagnostic-output', False)]
    if assembly:
        mounts += [(Path(code_root) / name, '/opt/crux-code/' + name, True) for name in ('references', 'installer')]
        mounts.append((record['source_manifest']['path'], '/inputs/source-manifest.json', True))
    else:
        mounts += [(record[key]['path'], '/inputs/' + name, True) for key, name in
                   (('pack_receipt', 'pack-receipt.json'), ('capsule', 'capsule.tar.zst'), ('metadata', 'metadata.json'))]
    if record.get('actions_context'):
        mounts.append((record['actions_context']['path'], '/inputs/actions-context.json', True))
    for item in record['dependencies']:
        for key, name in (('receipt', 'receipt.json'), ('archive', 'outputs.tar.zst')):
            mounts.append((item[key]['path'], '/inputs/dependencies/' + item['id'] + '/' + name, True))
    for source, target, readonly in mounts:
        source = Path(source).resolve(strict=True)
        if ',' in str(source):
            raise ValueError('Docker bind paths must not contain commas')
        command += ['--mount', 'type=bind,src=' + str(source) + ',dst=' + target
                    + (',readonly' if readonly else '')]
    command += [image, 'python3', '/opt/crux-code/local_replay/' + ('assembly.py' if assembly else 'container.py')]
    return command


def dependency_arguments(context, task_id, root, explicit):
    if task_id is None:
        import cloud_assemble
        _, expected = cloud_assemble.required_exports(context)
        expected = sorted(expected)
    else:
        expected = context['tasks'][task_id]['depends_on']
    overrides = {item[0]: item for item in explicit}
    if len(overrides) != len(explicit) or not set(overrides).issubset(expected):
        raise ValueError('Dependency overrides must be unique declared predecessors')
    if root is None:
        return explicit
    return [overrides[ident] if ident in overrides else (ident, 'actions', Path(root) / ('shard-' + ident))
            for ident in expected]


def snapshot_code(destination, root=ROOT, assembly=False):
    destination, root = Path(destination), Path(root)
    for name in ('scripts', 'config', '.github/workflows', '.github/actions', 'local_replay'):
        shutil.copytree(root / name, destination / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    if assembly:
        for name in ('references', 'installer'):
            shutil.copytree(root / name, destination / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'build'))
    return {'execution_digest': cloud_reuse.execution_digest(destination),
            'local_runner': {name: worker.digest(destination / 'local_replay' / name)
                             for name in LOCAL_CODE_FILES}}


def run(record_path, destination, image=IMAGE):
    record_path, destination = Path(record_path).resolve(), Path(destination).resolve()
    record = read(record_path)
    if record.get('schema') != SCHEMA or record.get('diagnostic_only') is not True:
        raise ValueError('Not a local diagnostic input record')
    assembly = record.get('operation') == 'assembly'
    keys = ('context', 'source_manifest') if assembly else ('context', 'pack_receipt', 'capsule', 'metadata')
    for key in keys:
        check_file(record[key])
    if record.get('actions_context'):
        check_file(record['actions_context'])
    if cloud_reuse.execution_digest(ROOT) != record['execution_digest']:
        raise ValueError('Production code/profile changed after input preparation')
    for item in record['dependencies']:
        check_file(item['receipt']); check_file(item['archive'])
    destination.mkdir(parents=True, exist_ok=False)
    # The image's runner owns no host directories; only this new diagnostic output
    # is writable. No SOURCE/OUT volume survives the container.
    destination.chmod(0o777)
    image_id = subprocess.check_output(['docker', '--context', 'orbstack', 'image', 'inspect',
                                        '--format', '{{.Id}}', image], text=True).strip()
    # A developer may edit the checkout during a long replay. Mount a frozen
    # implementation outside the writable result mount, then retain its bytes.
    with tempfile.TemporaryDirectory(prefix='crux-local-code-', dir=destination.parent) as temporary:
        code = Path(temporary)
        identity = snapshot_code(code, assembly=assembly)
        if identity['execution_digest'] != record['execution_digest']:
            raise ValueError('Production implementation changed while being snapshotted')
        command = docker_command(record_path, record, destination, image_id, code)
        (destination / 'invocation.json').write_text(json.dumps({
            'schema': SCHEMA, 'diagnostic_only': True, 'argv': command, 'implementation': identity,
            'host_architecture': platform.machine(), 'platform': 'linux/amd64', 'image_id': image_id,
            'note': 'OrbStack emulation is diagnostic, not native GitHub-runner equivalence.'}, indent=2) + '\n')
        try:
            with (destination / 'container.log').open('w') as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
        finally:
            shutil.copytree(code, destination / 'implementation')
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    image = commands.add_parser('image', help='Build only the isolated diagnostic runtime')
    image.add_argument('--tag', default=IMAGE)
    command = commands.add_parser('prepare', help='Bind existing verified cold inputs, without restoring them')
    command.add_argument('--context', required=True, type=Path)
    command.add_argument('--id', required=True)
    command.add_argument('--capsule-directory', required=True, type=Path)
    command.add_argument('--dependency', nargs=3, action='append', default=[], metavar=('ID', 'KIND', 'DIRECTORY'))
    command.add_argument('--dependency-root', type=Path,
                         help='Exact reference artifacts under shard-<id>; explicit dependencies override by ID')
    command.add_argument('--actions-context', type=Path,
                         help='Genuine origin context for Actions predecessors of a local preparation')
    command.add_argument('--output', required=True, type=Path)
    command = commands.add_parser('prepare-assembly', help='Bind image/FIT/unsigned-payload diagnostics, without signing')
    command.add_argument('--context', required=True, type=Path)
    command.add_argument('--source-manifest', required=True, type=Path)
    command.add_argument('--dependency-root', type=Path)
    command.add_argument('--dependency', nargs=3, action='append', default=[], metavar=('ID', 'KIND', 'DIRECTORY'))
    command.add_argument('--actions-context', type=Path)
    command.add_argument('--output', required=True, type=Path)
    command = commands.add_parser('run', help='Use a new networkless container and cold SOURCE/OUT')
    command.add_argument('--input', required=True, type=Path)
    command.add_argument('--output', required=True, type=Path)
    command.add_argument('--image', default=IMAGE)
    args = parser.parse_args()
    if args.command == 'image':
        return subprocess.call(['docker', '--context', 'orbstack', 'build', '--platform', 'linux/amd64', '--tag', args.tag,
                                '--file', str(Path(__file__).with_name('Dockerfile')), str(Path(__file__).parent)])
    if args.command in ('prepare', 'prepare-assembly'):
        task_id = args.id if args.command == 'prepare' else None
        dependencies = dependency_arguments(read(args.context), task_id, args.dependency_root, args.dependency)
        record = (prepare(args.context, args.id, args.capsule_directory, dependencies, args.actions_context)
                  if args.command == 'prepare' else prepare_assembly(args.context, args.source_manifest,
                                                                   dependencies, args.actions_context))
        with args.output.open('x') as stream:
            stream.write(json.dumps(record, indent=2) + '\n')
        return 0
    return run(args.input, args.output, args.image)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError) as error:
        print('local_replay: ' + str(error), file=sys.stderr)
        sys.exit(1)
