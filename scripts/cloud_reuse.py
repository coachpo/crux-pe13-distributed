#!/usr/bin/env python3
"""Content contracts for native shard reuse, independent of Actions run identity."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


def file_digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def execution_digest(root=ROOT):
    """Pin execution implementation, not the bot commit or source-lock document."""
    root = Path(root)
    paths = sorted(set(root.glob('scripts/*.py')) | set(root.glob('.github/workflows/*.yml'))
                   | set(root.glob('.github/actions/**/*')) | set(root.glob('config/*.json')))
    return digest([{'path': str(path.relative_to(root)), 'sha256': file_digest(path)}
                   for path in paths if path.is_file()
                   and path.name not in {'cloud-source-manifest.json', 'cloud-source-overrides.json'}])


def identity_spec(spec):
    kind = spec['type']
    result = {key: spec[key] for key in ('path', 'type', 'mode')}
    if kind == 'file':
        result.update(size=spec['size'], sha256=spec['sha256'])
    elif kind == 'symlink':
        result['target'] = spec['target']
    elif kind != 'directory':
        raise ValueError('Unsupported content spec type')
    return result


def input_descriptor(manifest, metadata, implementation):
    """Bind resolved Ninja semantics and the audited source closure.

    Absolute execution paths are deliberately retained. Only graph bookkeeping
    is omitted: its effective commands and topology are represented explicitly.
    No build scalar, source content or consumed runtime value is normalized out.
    """
    if metadata.get('allowed_generated_inputs') != []:
        raise ValueError('Reusable cold inputs cannot contain generated exemptions')
    graph = {key: value for key, value in manifest.items()
             if key not in {'graph_inputs', 'runtime_order_inputs', 'runtime_adaptations', 'ownership_profile'}}
    if any(edge.get('dyndep') for edge in manifest.get('edges', [])):
        raise ValueError('Dynamic Ninja discovery needs an explicit reuse input contract')
    if manifest.get('runtime_order_inputs'):
        graph['runtime_order_inputs'] = sorted(manifest['runtime_order_inputs'].values(), key=digest)
    task_root = Path(metadata['slice_path'])
    source_inputs = []
    for spec in metadata['files']:
        path = Path(spec['path'])
        if task_root == path or task_root in path.parents:
            continue
        source_inputs.append(identity_spec(spec))
    source_inputs.sort(key=lambda spec: spec['path'])
    descriptor = {'schema_version': 1, 'version': VERSION, 'execution_digest': implementation,
                  'source_root': metadata['source_root'], 'out_root': metadata['out_root'],
                  'graph_sha256': digest(graph), 'source_inputs_sha256': digest(source_inputs),
                  'source_input_count': len(source_inputs),
                  'external_inputs_sha256': digest(metadata['external_inputs']),
                  'external_input_dirs_sha256': digest(metadata['external_input_dirs']),
                  'required_interpreters': metadata['required_interpreters'],
                  'system_tools': metadata['system_tools']}
    descriptor['input_identity'] = digest(descriptor)
    return descriptor


def native_environment(environment=None):
    """Keep run IDs, credentials and incidental runner variables out of tools."""
    environment = os.environ if environment is None else environment
    result = {'PATH': os.defpath, 'HOME': environment.get('HOME', '/home/runner'),
              'USER': environment.get('USER', 'runner'), 'LOGNAME': environment.get('LOGNAME', 'runner'),
              'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TZ': 'UTC', 'SHELL': '/bin/bash'}
    if environment.get('MAKEFLAGS'):
        result['MAKEFLAGS'] = environment['MAKEFLAGS']
    return result


def runtime_identity(descriptor, environment=None):
    environment = os.environ if environment is None else environment
    packages = subprocess.run(['dpkg-query', '-W', '-f=${binary:Package}=${Version}\n'],
                              capture_output=True, text=True, check=True).stdout
    tools = []
    for value in sorted(set(descriptor['system_tools'])):
        path = Path(value)
        if path.is_dir():
            members = []
            for child in sorted(path.rglob('*')):
                mode = stat.S_IMODE(child.lstat().st_mode)
                if child.is_symlink():
                    members.append({'path': str(child.relative_to(path)), 'mode': mode, 'target': os.readlink(child)})
                elif child.is_file():
                    members.append({'path': str(child.relative_to(path)), 'mode': mode, 'sha256': file_digest(child)})
                elif child.is_dir():
                    members.append({'path': str(child.relative_to(path)), 'mode': mode, 'type': 'directory'})
            tools.append({'path': value, 'mode': stat.S_IMODE(path.stat().st_mode), 'members': digest(members)})
        elif path.is_file():
            tools.append({'path': value, 'resolved': str(path.resolve()),
                          'mode': stat.S_IMODE(path.stat().st_mode), 'sha256': file_digest(path)})
        else:
            raise ValueError('Declared host input is missing: ' + value)
    return {'schema_version': 1, 'image': environment.get('ImageOS'),
            'image_version': environment.get('ImageVersion'), 'architecture': platform.machine(),
            'kernel': platform.release(), 'packages': hashlib.sha256(packages.encode()).hexdigest(),
            'tools': tools, 'environment': native_environment(environment), 'jobs': 4,
            'CCACHE_DISABLE': '1'}


def content_basis(context, task_id, runtime, dependency_receipts):
    task = context['tasks'][task_id]
    if set(dependency_receipts) != set(task['depends_on']):
        raise ValueError('Content key requires every admitted predecessor')
    # Importing a predecessor currently transports its full export tree. Bind
    # that exact content, including runtime extras, without run/task identifiers.
    dependencies = []
    for receipt in dependency_receipts.values():
        specs = sorted((identity_spec(spec) for spec in receipt['outputs']), key=lambda spec: spec['path'])
        dependencies.append({'output_count': len(specs), 'sha256': digest(specs)})
    dependencies.sort(key=digest)
    return {'schema_version': 1, 'version': VERSION,
            'execution_digest': context['reuse_policy']['execution_digest'],
            'input_identity': task['input_identity'], 'runtime': runtime, 'dependencies': dependencies,
            'additional_exports': sorted(task.get('extra_exports', [])),
            'prebuild_targets': task.get('local_runtime_targets', []),
            'kernel_source_alias': context.get('kernel_source_alias')}


def content_key(basis):
    return digest(basis)


def validate_receipt(context, receipt, task_id):
    if 'reuse_policy' not in context:
        return
    policy, task = context['reuse_policy'], context['tasks'][task_id]
    envelope = receipt.get('reuse')
    if not isinstance(envelope, dict) or envelope.get('schema_version') != 1:
        raise ValueError('Missing versioned native reuse contract: ' + task_id)
    basis = envelope.get('basis', {})
    if (basis.get('version') != VERSION or policy.get('version') != VERSION
            or basis.get('input_identity') != task.get('input_identity')
            or basis.get('execution_digest') != policy.get('execution_digest')
            or envelope.get('key') != content_key(basis)
            or sorted(basis.get('additional_exports', [])) != sorted(task.get('extra_exports', []))
            or basis.get('prebuild_targets') != task.get('local_runtime_targets', [])
            or basis.get('kernel_source_alias') != context.get('kernel_source_alias')
            or len(basis.get('dependencies', [])) != len(task['depends_on'])
            or basis.get('runtime', {}).get('CCACHE_DISABLE') != '1'):
        raise ValueError('Native reuse content binding differs: ' + task_id)
    if envelope.get('mode') not in {'built', 'reused'}:
        raise ValueError('Unknown native reuse mode')
    if envelope['mode'] == 'reused' and policy.get('force_cold'):
        raise ValueError('Forced cold execution adopted an older output')
    native = envelope.get('native_receipt')
    if not isinstance(native, dict) or native.get('status') != 'success' or native.get('diagnostic_only'):
        raise ValueError('Native origin is not a successful production receipt')
    if (native.get('outputs') != receipt.get('outputs')
            or native.get('build_environment', {}).get('CCACHE_DISABLE') != '1'):
        raise ValueError('Native output content differs from adoption receipt')
    origin = envelope.get('origin', {})
    if (origin.get('repository') != context['repository']
            or str(native.get('run_id')) != str(origin.get('run_id'))
            or native.get('worker_commit') != origin.get('worker_commit')
            or native.get('id') != origin.get('task_id')
            or digest(native) != origin.get('receipt_sha256')):
        raise ValueError('Native origin identity differs')
    if envelope['mode'] == 'built':
        if (str(native.get('run_id')) != str(context['execution_id'])
                or native.get('worker_commit') != context['worker_commit']
                or native.get('manifest_sha256') != task['manifest_sha256']):
            raise ValueError('Built origin is not this native execution')
    elif (type(origin.get('artifact_id')) is not int
          or not re.fullmatch(r'sha256:[0-9a-f]{64}', origin.get('artifact_digest', ''))):
        raise ValueError('Reused origin has no verified Actions artifact identity')


def envelope(context, task_id, basis, native, origin=None):
    if origin is None:
        origin = {'repository': context['repository'], 'run_id': native['run_id'],
                  'worker_commit': native['worker_commit'], 'task_id': native['id'],
                  'receipt_sha256': digest(native)}
    elif 'reuse' in native:
        carried = native['reuse']
        native = carried['native_receipt']
        immediate = dict(origin)
        origin = dict(carried['origin'])
        if carried['mode'] == 'built':
            origin.update({key: immediate[key] for key in ('artifact_id', 'artifact_digest')})
        origin['adopted_from'] = immediate
    return {'schema_version': 1, 'mode': 'reused' if 'artifact_id' in origin else 'built',
            'key': content_key(basis), 'basis': basis, 'native_receipt': native, 'origin': origin}


def validate_summary(context, summary):
    if 'reuse_policy' not in context:
        return
    tasks = context['tasks']
    reuse, outputs = summary.get('reuse'), summary.get('output_identity')
    if not isinstance(reuse, dict) or not isinstance(outputs, dict) or set(reuse) != set(tasks) or set(outputs) != set(tasks):
        raise ValueError('Reuse summary does not cover the exact admitted task set')
    for ident, task in tasks.items():
        record = {'reuse': reuse[ident], 'outputs': outputs[ident]}
        validate_receipt(context, record, ident)
        expected = content_basis(context, ident, reuse[ident]['basis']['runtime'],
                                 {dep: {'outputs': outputs[dep]} for dep in task['depends_on']})
        if reuse[ident]['basis'] != expected:
            raise ValueError('Reuse summary imported different predecessor content: ' + ident)


def verify_native_output(receipt, directory):
    """Verify an entire native archive without materializing historical paths."""
    archive = Path(directory) / 'outputs.tar.zst'
    if (archive.stat().st_size != receipt['archive_bytes']
            or file_digest(archive) != receipt['archive_sha256']):
        raise ValueError('Native archive bytes/digest differ')
    specs = {spec['path']: spec for spec in receipt['outputs']}
    if len(specs) != len(receipt['outputs']):
        raise ValueError('Duplicate native output path')
    process = subprocess.Popen(['zstd', '-d', '-q', '-c', str(archive)], stdout=subprocess.PIPE)
    seen = set()
    try:
        with tarfile.open(fileobj=process.stdout, mode='r|') as stream:
            for member in stream:
                path = PurePosixPath(member.name)
                if path.is_absolute() or '..' in path.parts:
                    raise ValueError('Unsafe native archive path')
                name = '/' + str(path)
                if name in seen or name not in specs:
                    raise ValueError('Unrecorded/duplicate native output')
                seen.add(name)
                spec = specs[name]
                kind = 'file' if member.isfile() else 'symlink' if member.issym() else 'directory' if member.isdir() else None
                if kind != spec['type'] or stat.S_IMODE(member.mode) != spec['mode']:
                    raise ValueError('Native member type/mode differs')
                if kind == 'symlink' and member.linkname != spec['target']:
                    raise ValueError('Native member link differs')
                if kind == 'file':
                    checksum = hashlib.sha256()
                    with stream.extractfile(member) as content:
                        for block in iter(lambda: content.read(1024 * 1024), b''):
                            checksum.update(block)
                    if member.size != spec['size'] or checksum.hexdigest() != spec['sha256']:
                        raise ValueError('Native member content differs')
            for block in iter(lambda: stream.fileobj.read(1024 * 1024), b''):
                if any(block):
                    raise ValueError('Non-padding data after native tar terminator')
        if process.wait() or seen != set(specs):
            raise ValueError('Native archive decompression/member inventory differs')
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
        process.wait()
    return archive
