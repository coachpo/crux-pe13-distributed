#!/usr/bin/env python3
"""Resolve one wave task against verified content-addressed native outputs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil

import cloud_artifacts
import cloud_build_plan
import cloud_compile
import cloud_reuse
import cloud_reuse_cache


def resolve(context, task_id, output, staging, environment=None):
    environment = dict(os.environ if environment is None else environment)
    cloud_compile.validate_context(context, task_id, environment)
    output, staging = Path(output), Path(staging)
    output.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    task = context['tasks'][task_id]
    token = environment.get('GH_TOKEN') or environment.get('GITHUB_TOKEN')
    receipts = {}
    for dep in task['depends_on']:
        directory = staging / 'dependencies' / dep
        cloud_artifacts.download_artifacts(context['execution_id'], context['repository'], directory,
                                          token=token, name='fresh-receipt-' + dep)
        receipt = json.loads((directory / 'receipt.json').read_text())
        cloud_build_plan.validate_receipt(context, receipt, dep)
        receipts[dep] = receipt
    runtime = cloud_reuse.runtime_identity(task['reuse_descriptor'], environment)
    kernel = context['out_root'] + '/target/product/crux/obj/KERNEL_OBJ'
    if context.get('assembly_exports', {}).get(kernel) == task_id:
        # mkcompile_h reads date/whoami/hostname outside Ninja's declared files.
        # Preserve the current kernel behavior instead of reusing stale values.
        runtime['untracked_kernel_identity'] = context['execution_id']
    basis = cloud_reuse.content_basis(context, task_id, runtime, receipts)
    result = {'schema_version': 1, 'id': task_id, 'execution_id': context['execution_id'],
              'worker_commit': context['worker_commit'], 'basis': basis,
              'key': cloud_reuse.content_key(basis), 'mode': 'miss'}
    candidate = None
    if not context['reuse_policy']['force_cold']:
        try:
            candidate = cloud_reuse_cache.find(context, task_id, basis, staging / 'cache', token)
        except cloud_reuse_cache.CacheIntegrityError as error:
            # Reject corrupt candidates; the only replacement is the ordinary
            # verified current-source cold path. Transport failures propagate.
            result['rejected_candidate'] = str(error)
    if candidate is not None:
        origin, old, archive = candidate
        adopted = dict(old)
        adopted.update(id=task_id, run_id=context['execution_id'], worker_commit=context['worker_commit'],
                       manifest_sha256=task['manifest_sha256'], source_root=context['source_root'],
                       out_root=context['out_root'], repository=context['repository'],
                       fresh_build=cloud_build_plan.fresh_binding(context),
                       declared_dependencies=[cloud_build_plan.producer_binding(context, dep) for dep in task['depends_on']],
                       additional_exports=sorted(task.get('extra_exports', [])),
                       prebuild_targets=task.get('local_runtime_targets', []),
                       dependencies=[{'id': dep, 'manifest_sha256': receipts[dep]['manifest_sha256'],
                                      'archive_sha256': receipts[dep]['archive_sha256'],
                                      'receipt_sha256': cloud_reuse.file_digest(staging / 'dependencies' / dep / 'receipt.json')}
                                     for dep in task['depends_on']],
                       reuse=cloud_reuse.envelope(context, task_id, basis, old, origin))
        adopted['admitted_runtime_inputs'] = cloud_compile._verify_runtime_inputs(
            task, receipts, {dep: staging / 'dependencies' / dep for dep in receipts})
        adopted['admitted_local_runtime_targets'] = task.get('local_runtime_targets', [])
        cloud_build_plan.validate_receipt(context, adopted, task_id)
        shutil.copyfile(archive, output / 'outputs.tar.zst')
        (output / 'receipt.json').write_text(json.dumps(adopted, indent=2, sort_keys=True) + '\n')
        result['mode'] = 'reused'
    (output / 'decision.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('context', 'id', 'output', 'staging'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--github-output')
    args = parser.parse_args()
    result = resolve(json.loads(Path(args.context).read_text()), args.id, args.output, args.staging)
    if args.github_output:
        with open(args.github_output, 'a') as stream:
            print('mode=' + result['mode'], file=stream)
    print(json.dumps({'id': args.id, 'mode': result['mode'], 'key': result['key']}))


if __name__ == '__main__':
    main()
