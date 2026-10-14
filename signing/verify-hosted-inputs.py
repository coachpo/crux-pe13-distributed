#!/usr/bin/env python3
"""Bind a signing run to the terminal artifact of an actual Actions compile run.

The tier 1 recipes ran on a build host and bound their input with a reviewed
provenance file plus local git pins. A hosted signing run has no build host, so it
binds the same properties from artifacts of the compile run itself:

  * the artifact was downloaded by run id and its GitHub artifact digest was
    verified by the caller,
  * the worker receipt inside the artifact records its own run id, worker commit,
    slice manifest digest and one entry per exported path,
  * the unsigned target-files and the four kernel files must match their receipt
    entries byte for byte,
  * `SYSTEM/build.prop` inside the target-files must carry the frozen release
    identity, and `BOOT/kernel` and `RECOVERY/kernel` must embed the actual cloud
    `Image` bytes behind the accepted uncompressed wrapper.

The result is `input-binding.json` in the same shape the staged output contract
reads, so `verify-output-contract.py` stays the single definition of what a signed
release has to look like.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import zipfile
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 22), b''):
            result.update(block)
    return result.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def declared(receipt, path):
    matches = [row for row in receipt['outputs'] if row['path'] == str(path)]
    require(len(matches) == 1, 'Receipt does not declare exactly one output: ' + str(path))
    return matches[0]


def bind_file(receipt, path, label):
    row = declared(receipt, path)
    path = Path(row['path'])
    require(row['type'] == 'file' and path.is_file(), label + ' is missing from the artifact: ' + str(path))
    require(path.stat().st_size == row['size'], label + ' size differs from the worker receipt')
    actual = digest(path)
    require(actual == row['sha256'], label + ' bytes differ from the worker receipt')
    return {'path': str(path), 'bytes': row['size'], 'sha256': actual}


def properties(archive, name):
    require(archive.namelist().count(name) == 1, 'Expected exactly one ' + name)
    return dict(line.split('=', 1) for line in archive.read(name).decode().splitlines()
                if '=' in line and not line.startswith('#'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', type=Path, required=True,
                        help='Terminal shard worker receipt (owns the target-files output)')
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--run-url', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--artifact', required=True)
    parser.add_argument('--artifact-digest', required=True)
    parser.add_argument('--kernel-receipt', type=Path, required=True,
                        help='Kernel shard worker receipt (owns the KERNEL_OBJ outputs)')
    parser.add_argument('--kernel-run-id', type=int, required=True)
    parser.add_argument('--kernel-run-url', required=True)
    parser.add_argument('--kernel-task-id', required=True)
    parser.add_argument('--kernel-artifact', required=True)
    parser.add_argument('--kernel-artifact-digest', required=True)
    parser.add_argument('--worker-commit', required=True)
    parser.add_argument('--identity', type=Path, required=True)
    parser.add_argument('--target-files', type=Path, required=True)
    parser.add_argument('--kernel-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()

    identity = read(args.identity)
    receipt = read(args.receipt)
    kernel_receipt = read(args.kernel_receipt)
    for label, record, run_id, task_id, digest_value in (
            ('Terminal', receipt, args.run_id, args.task_id, args.artifact_digest),
            ('Kernel', kernel_receipt, args.kernel_run_id, args.kernel_task_id, args.kernel_artifact_digest)):
        require(record.get('schema_version') == 1 and record.get('status') == 'success',
                label + ' worker receipt is not a successful schema 1 record')
        require(record.get('id') == task_id, label + ' receipt belongs to another task')
        require(record.get('run_id') == run_id, label + ' receipt run id differs from the dispatched run')
        require(record.get('worker_commit') == args.worker_commit,
                label + ' receipt worker commit differs from the pinned worker')
        require(isinstance(record.get('manifest_sha256'), str)
                and re.fullmatch('[0-9a-f]{64}', record['manifest_sha256']),
                label + ' receipt does not identify its slice manifest')
        require(re.fullmatch(r'sha256:[0-9a-f]{64}', digest_value),
                label + ' artifact was not verified against its GitHub digest')

    unsigned = bind_file(receipt, args.target_files, 'Unsigned target-files')
    kernel = {}
    for role, relative in identity['kernel']['files'].items():
        kernel[role] = bind_file(kernel_receipt, args.kernel_root / relative, 'Cloud kernel ' + role)

    with zipfile.ZipFile(unsigned['path']) as archive:
        props = properties(archive, 'SYSTEM/build.prop')
        require(props.get('ro.build.flavor') == identity['product'], 'Wrong target product/variant')
        require(props.get('ro.build.version.incremental') == identity['incremental'], 'Wrong incremental')
        require(props.get('ro.build.fingerprint') == identity['unsigned_fingerprint'],
                'Wrong unsigned fingerprint')
        require(props.get('ro.build.date.utc') == identity['datetime'], 'Wrong frozen build datetime')
        image = Path(kernel['image']['path']).read_bytes()
        for role in ('BOOT', 'RECOVERY'):
            wrapped = archive.read(role + '/kernel')
            require(wrapped[:16] == b'UNCOMPRESSED_IMG' and len(wrapped) >= 20,
                    role + ' kernel lacks the accepted wrapper')
            require(struct.unpack_from('<I', wrapped, 16)[0] == len(image)
                    and wrapped[20:].startswith(image),
                    role + ' kernel differs from the actual cloud Image')

    provenance = {
        'schema_version': 1,
        'state': 'ACTUAL_ACTIONS_INPUT_REVIEWED',
        'root_reviewed': True,
        'github_actions': {
            'repository': identity.get('repository', 'coachpo/crux-pe13-distributed'),
            'run_id': args.run_id,
            'run_url': args.run_url,
            'worker_commit': args.worker_commit,
            'conclusion': 'success',
            'runner_type': 'public_standard',
            'artifact': args.artifact,
            'artifact_digest': args.artifact_digest,
            'task_id': args.task_id,
            'receipt_manifest_sha256': receipt['manifest_sha256'],
            'kernel_run_id': args.kernel_run_id,
            'kernel_run_url': args.kernel_run_url,
            'kernel_artifact': args.kernel_artifact,
            'kernel_artifact_digest': args.kernel_artifact_digest,
            'kernel_task_id': args.kernel_task_id,
            'kernel_receipt_manifest_sha256': kernel_receipt['manifest_sha256'],
        },
        'expected_build': {key: identity[key] for key in
                           ('product', 'incremental', 'datetime',
                            'unsigned_fingerprint', 'signed_fingerprint')},
        'unsigned_target_files': unsigned,
        'kernel': kernel,
    }
    binding = {
        'schema_version': 1,
        'state': 'ACTUAL_ACTIONS_INPUT_BOUND',
        'provenance': provenance,
        'unsigned': {'path': unsigned['path'], 'sha256': unsigned['sha256']},
        'kernel': {role: {'path': row['path'], 'sha256': row['sha256']} for role, row in kernel.items()},
        'cloud_image_matches_boot_and_recovery_source': True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(binding, indent=2) + '\n')
    print(json.dumps({'state': binding['state'], 'unsigned_bytes': unsigned['bytes'],
                      'kernel': {role: row['bytes'] for role, row in kernel.items()},
                      'run_id': args.run_id, 'task_id': args.task_id}, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, TypeError, AttributeError, OSError, struct.error,
            zipfile.BadZipFile) as error:
        print('error: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
