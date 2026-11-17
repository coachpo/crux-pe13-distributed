#!/usr/bin/env python3
"""The networkless container boundary around the unchanged production worker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay
import cloud_compile
import cloud_pack
import cloud_reuse
import worker


def reject_actions_environment(environment):
    if any(name.startswith(('GITHUB_', 'ACTIONS_')) or name in ('GH_TOKEN', 'CRUX_INSTALL_SIGNING_KEY')
           for name in environment):
        raise ValueError('The local native container must not receive Actions identities or credentials')


def run_native(manifest, bundle, task, directories, local_info, environment, alias,
               output=Path('/diagnostic-output/native')):
    """Finalize local provenance even if releasing the kernel alias fails."""
    receipt = None
    previous = dict(os.environ)
    try:
        os.environ.clear(); os.environ.update(environment)
        receipt = worker.run_shard(manifest, bundle, task['id'], output, directories, jobs=4,
                                  extra_exports=task.get('extra_exports', []),
                                  prebuild_targets=task.get('local_runtime_targets', []))
        receipt.update(diagnostic_only=True, local_replay=local_info)
        (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    finally:
        os.environ.clear(); os.environ.update(previous)
        if alias:
            try:
                cloud_compile._unmount_short_source_alias()
            except Exception as error:
                if receipt is not None:
                    receipt.update(native_status=receipt['status'], status='failed',
                                   cleanup_error=str(error), diagnostic_only=True, local_replay=local_info)
                    (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
                raise
    return receipt


def execute():
    record = replay.read('/inputs/input.json')
    if record.get('schema') != replay.SCHEMA or record.get('diagnostic_only') is not True:
        raise ValueError('Expected an explicitly local diagnostic record')
    for key, name in (('context', 'context.json'), ('pack_receipt', 'pack-receipt.json'),
                      ('capsule', 'capsule.tar.zst'), ('metadata', 'metadata.json')):
        replay.check_file(record[key], '/inputs/' + name)
    context = replay.read('/inputs/context.json')
    actions_context = None
    if record.get('actions_context'):
        replay.check_file(record['actions_context'], '/inputs/actions-context.json')
        actions_context = replay.read('/inputs/actions-context.json')
    task_id = record['task_id']
    task = replay.validate_reference(context, task_id)
    if cloud_reuse.execution_digest(replay.ROOT) != record['execution_digest']:
        raise ValueError('Mounted production implementation differs from the input record')
    source, out = Path(context['source_root']), Path(context['out_root'])
    if os.path.lexists(source) or os.path.lexists(out):
        raise ValueError('SOURCE and OUT must not exist in the fresh container')
    reject_actions_environment(os.environ)
    metadata = replay.read('/inputs/metadata.json')
    cloud_pack.verify_archive(Path('/inputs/capsule.tar.zst'), metadata)
    subprocess.run(['sudo', 'tar', '--zstd', '--no-same-owner', '-xpf',
                    '/inputs/capsule.tar.zst', '-C', '/'], check=True)
    subprocess.run(['sudo', 'chown', '-R', str(os.getuid()) + ':' + str(os.getgid()), str(source)], check=True)
    manifest, bundle = cloud_compile._validate_restored_capsule(source, out, task)
    worker.verify_bundle(bundle, manifest)
    reject_actions_environment(replay.read(manifest).get('build_environment', {}))
    actual = cloud_reuse.input_descriptor(replay.read(manifest), replay.read(bundle), record['execution_digest'])
    if actual != task['reuse_descriptor']:
        raise ValueError('Restored capsule source/command contract differs from the reference audit')
    cloud_compile._verify_required_notice_sources(task, source, out)
    directories, receipts, paths = [], {}, {}
    if {item['id'] for item in record['dependencies']} != set(task['depends_on']):
        raise ValueError('Only the complete declared dependency set may be mounted')
    for item in record['dependencies']:
        ident = item['id']
        directory = Path('/inputs/dependencies') / ident
        replay.check_file(item['receipt'], directory / 'receipt.json')
        replay.check_file(item['archive'], directory / 'outputs.tar.zst')
        receipts[ident] = replay.admit_dependency(context, ident, directory, item['kind'],
                                                 record['context']['sha256'], actions_context)
        directories.append(directory); paths[ident] = directory
    runtime = cloud_compile._verify_runtime_inputs(task, receipts, paths)
    alias = None
    kernel = str(out / 'target/product/crux/obj/KERNEL_OBJ')
    if context.get('assembly_exports', {}).get(kernel) == task_id:
        if context.get('kernel_source_alias') != cloud_compile.SHORT_KERNEL_SOURCE_POLICY:
            raise ValueError('Unknown kernel alias policy')
        kernel_source, revision = cloud_compile._validate_short_kernel_source(context, task_id, source)
        alias = {**cloud_compile._mount_short_source_alias(kernel_source), 'source_revision': revision}
    environment = cloud_reuse.native_environment()
    runtime_identity = cloud_reuse.runtime_identity(task['reuse_descriptor'], environment)
    if alias:
        environment['MAKEFLAGS'] = 'srctree=/k'
    code_commit = (context['local_preparation']['tool_commit'] if replay.local_preparation(context)
                   else context['worker_commit'])
    local_info = {'schema': replay.SCHEMA, 'context_sha256': record['context']['sha256'],
                  'execution_digest': record['execution_digest'], 'reference_pin': code_commit,
                  'reference_kind': 'local-preparation' if replay.local_preparation(context) else 'actions-context',
                  'source_manifest_sha256': context['source_manifest_sha256'], 'plan_sha256': context['plan_sha256'],
                  'runtime_inputs': runtime, 'runtime_identity': runtime_identity,
                  'local_runner_sha256': {name: worker.digest(Path(__file__).parent / name)
                                          for name in replay.LOCAL_CODE_FILES},
                  'kernel_source_alias': alias, 'architecture': platform.machine(),
                  'kernel': platform.release(), 'ccache': 'disabled', 'publication_admission': False}
    receipt = run_native(manifest, bundle, task, directories, local_info, environment, alias)
    print(json.dumps({'schema': replay.SCHEMA, 'id': task_id, 'status': receipt['status'],
                      'diagnostic_only': True, 'error': receipt.get('error')}), flush=True)
    return 0 if receipt['status'] == 'success' else 1


if __name__ == '__main__':
    try:
        sys.exit(execute())
    except Exception as error:
        Path('/diagnostic-output/diagnostic-error.json').write_text(json.dumps({
            'schema': replay.SCHEMA, 'diagnostic_only': True, 'status': 'failed',
            'error_type': type(error).__name__, 'error': str(error)}, indent=2) + '\n')
        raise
