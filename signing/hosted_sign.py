#!/usr/bin/env python3
"""Run the release signing and gate stages on a standard runner.

This is the hosted form of the tier 1 recipes: the same native commands in the
same order, with the reviewed output contract after every stage. The private keys
arrive as environment secrets and are written to a private directory that is
removed when the stage finishes; nothing prints key material.

  package        inspect, validate, sign target-files and build image staging
  acceptance     APK/APEX, image AVB, Recovery inspection and native OTA gates
  delivery       fastboot rescue package plus unchanged signed OTA and Recovery
  verifier-tests retained Recovery verifier unit tests
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def say(message):
    print(message, flush=True)


def run(logged, label, command, cwd=None, env=None):
    started = time.time()
    with Path(logged).open('w') as log:
        completed = subprocess.run([str(part) for part in command], stdout=log, stderr=subprocess.STDOUT,
                                   cwd=str(cwd) if cwd else None, env=env)
    elapsed = round(time.time() - started, 1)
    if completed.returncode == 0:
        say('PASS (%ss): %s -> %s' % (elapsed, label, logged))
        return
    say('FAILED (exit %s, %ss): %s' % (completed.returncode, elapsed, label))
    tail = Path(logged).read_text(errors='replace').splitlines()[-35:]
    for line in tail:
        print(line, flush=True)
    raise SystemExit(1)


def require(condition, message):
    if not condition:
        raise SystemExit('error: ' + message)


class Signing:
    def __init__(self, args):
        self.args = args
        self.identity = json.loads(Path(args.identity).read_text())
        self.output = Path(args.output_root).resolve()
        self.package = self.output / self.identity['package_dir']
        self.unsigned = Path(self.output / 'input-binding.json')

    @property
    def binding(self):
        return json.loads((self.output / 'input-binding.json').read_text())

    def unsigned_path(self):
        return Path(self.binding()['provenance']['unsigned_target_files']['path'])

    def kernel_path(self, role):
        return Path(self.binding()['provenance']['kernel'][role]['path'])

    def contract(self, stage, extra):
        output = self.output / (stage + '-contract.json')
        run(self.output / (stage + '-contract.log'), 'output contract ' + stage,
            [sys.executable, REPO / 'signing/verify-output-contract.py', '--output-root', self.output,
             '--identity', self.args.identity, '--stage', stage])
        record = json.loads(output.read_text())
        record.update(extra)
        output.write_text(json.dumps(record, indent=2) + '\n')
        return record

    def package_stage(self):
        require((self.output / 'input-binding.json').is_file(),
                'run the binding step before the package stage')
        require(not self.package.exists(), 'package output root already exists; use a fresh output root')
        self.package.mkdir(parents=True)
        host = self.args.host_tools
        signer = Path(self.args.source_root) / 'device/xiaomi/crux/scripts/package_release.py'
        require(signer.is_file(), 'missing nested signer: ' + str(signer))
        Path(self.args.key_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        base = [sys.executable, '-B', signer]
        run(self.output / 'inspect.log', 'signing inventory',
            base + ['inspect', '--target-files', self.unsigned_path(), '--host-tools', host,
                    '--output', self.output / 'signing-inventory.json'])
        run(self.output / 'validate-package.log', 'package validation',
            base + ['package', '--validate-only', '--target-files', self.unsigned_path(),
                    '--key-dir', self.args.key_dir, '--config', self.args.config, '--host-tools', host,
                    '--output-dir', self.package, '--images'])
        run(self.output / 'package.log', 'package and sign',
            base + ['package', '--target-files', self.unsigned_path(),
                    '--key-dir', self.args.key_dir, '--config', self.args.config, '--host-tools', host,
                    '--output-dir', self.package, '--images'])
        signed = self.package / 'signed-target_files.zip'
        self.contract('package', {'signed_target_files_bytes': signed.stat().st_size})

    def acceptance_stage(self):
        require((self.output / 'package-contract.json').is_file(), 'run the package stage first')
        validation = self.output / 'validation'
        require(not validation.exists(), 'validation output already exists; use a fresh output root')
        validation.mkdir()
        gates = REPO / 'signing/gates'
        signed = self.package / 'signed-target_files.zip'
        gates_args = self.identity['gates']
        run(validation / 'apk-apex.log', 'APK/APEX signatures',
            [sys.executable, '-B', gates / 'verify_apk_apex_signatures.py',
             '--signed-target-files', signed, '--original-target-files', self.unsigned_path(),
             '--config', self.args.config, '--public-materials', self.args.public_materials,
             '--host-tools', self.args.host_tools, '--java-home', self.args.jdk_home,
             '--preserved-flat-apks', gates / 'approved-preserved-flat-apks.json',
             '--sdk', str(gates_args['sdk']), '--expected-flat-apks', str(gates_args['expected_flat_apks']),
             '--expected-apex', str(gates_args['expected_apex']),
             '--expected-resigned-apex', str(gates_args['expected_resigned_apex']),
             '--report', validation / 'apk-apex.json'])
        run(validation / 'image-avb.log', 'image AVB',
            [sys.executable, '-B', gates / 'verify-image-bundle-avb.py', '--target-files', signed,
             '--output-dir', validation / 'image-avb', '--host-tools', self.args.host_tools,
             '--public-keys', self.args.public_materials])
        run(validation / 'recovery-inspection.log', 'Recovery inspection',
            [sys.executable, '-B', gates / 'inspect-signed-recovery.py', '--signed-target-files', signed,
             '--output-dir', validation / 'recovery', '--host-tools', self.args.host_tools,
             '--public-keys', self.args.public_materials, '--matched-image', self.kernel_path('image')])
        run(validation / 'ota-native.log', 'native OTA signature',
            [sys.executable, '-B', gates / 'accept-ota-signature-host.py', '--tool', self.args.verifier,
             '--ota', self.package / 'ota.zip',
             '--release-cert', Path(self.args.public_materials) / 'releasekey.x509.pem',
             '--wrong-cert', Path(self.args.source_root) / 'build/target/product/security/testkey.x509.pem',
             '--otacerts', validation / 'recovery/otacerts.zip',
             '--output-dir', validation / 'ota-native'])
        self.contract('acceptance', {})

    def delivery_stage(self):
        require((self.output / 'acceptance-contract.json').is_file(), 'run the acceptance stage first')
        delivery = self.output / 'delivery'
        require(not delivery.exists(), 'delivery output already exists; use a fresh output root')
        delivery.mkdir()
        names = self.identity['delivery']
        packer = Path(self.args.source_root) / 'device/xiaomi/crux/scripts/package_fastboot.py'
        run(delivery / 'fastboot-package.log', 'fastboot rescue package',
            [sys.executable, '-B', packer, '--signed-target-files', self.package / 'signed-target_files.zip',
             '--output-dir', delivery / names['fastboot_dir']])
        shutil.copy2(self.package / 'ota.zip', delivery / names['ota'])
        shutil.copy2(self.output / 'validation/recovery/recovery.img', delivery / names['recovery'])
        record = self.contract('delivery', {})
        assets = {'ota': delivery / names['ota'], 'recovery': delivery / names['recovery']}
        assets['fastboot'] = delivery / names['fastboot_zip']
        manifest = {'schema_version': 1, 'build_number': self.identity['build_number'],
                    'fingerprint': self.identity['signed_fingerprint'],
                    'fastboot_archive_sha256': record.get('fastboot_archive_sha256'),
                    'fastboot_archive_bytes': record.get('fastboot_archive_bytes'), 'assets': {}}
        for role, path in assets.items():
            manifest['assets'][role] = {
                'name': path.name,
                'bytes': path.stat().st_size if path.is_file() else None,
                'sha256': self.sha256(path) if path.is_file() else None,
            }
        (delivery / 'candidate-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        say(json.dumps(manifest, indent=2))

    @staticmethod
    def sha256(path):
        import hashlib
        result = hashlib.sha256()
        with Path(path).open('rb') as stream:
            for block in iter(lambda: stream.read(1 << 22), b''):
                result.update(block)
        return result.hexdigest()

    def verifier_tests(self):
        report = self.output / 'verifier-tests'
        report.mkdir(parents=True, exist_ok=True)
        environment = {**os.environ, 'LD_LIBRARY_PATH': str(self.args.verifier_lib)}
        # The retained fixture directory next to the accepted binary carries the
        # `testdata` symlink the unit tests open by relative path.
        run(report / 'verifier-host-test.log', 'retained Recovery verifier tests',
            [str(self.args.verifier_test), '--gtest_output=xml:' + str(report / 'results.xml')],
            cwd=self.args.test_cwd, env=environment)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True,
                        choices=('package', 'acceptance', 'delivery', 'verifier-tests'))
    parser.add_argument('--output-root', required=True)
    parser.add_argument('--identity', required=True)
    parser.add_argument('--source-root', required=True,
                        help='Restored frozen source root holding the device packaging scripts')
    parser.add_argument('--host-tools', required=True)
    parser.add_argument('--jdk-home', required=True)
    parser.add_argument('--key-dir', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--public-materials', required=True)
    parser.add_argument('--verifier', required=True)
    parser.add_argument('--verifier-test', required=True)
    parser.add_argument('--verifier-lib', required=True)
    parser.add_argument('--test-cwd', help='Working directory holding the retained testdata fixture')
    args = parser.parse_args()
    signing = Signing(args)
    if args.stage == 'package':
        signing.package_stage()
    elif args.stage == 'acceptance':
        signing.acceptance_stage()
    elif args.stage == 'delivery':
        signing.delivery_stage()
    else:
        signing.verifier_tests()
    return 0


if __name__ == '__main__':
    sys.exit(main())
