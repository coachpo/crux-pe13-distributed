#!/usr/bin/env python3
"""Read back the staged reports and preserve exact signed bytes in delivery.

This is the hosted form of the tier 1 output contract: identical checks, with the
release identity and delivery file names taken from `inputs/release-<build>.json`
instead of being hard-coded to the 2026101104 R6 names."""
import argparse
import json
from pathlib import Path
import sys
import zipfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    require(path.is_file(), 'Required output is absent: ' + str(path))
    return json.loads(path.read_text())


def same_path(actual, expected):
    return Path(actual).resolve() == expected.resolve()


def compare_streams(streams, label):
    while True:
        chunks = [stream.read(1024 * 1024) for stream in streams]
        require(all(chunk == chunks[0] for chunk in chunks[1:]), 'Signed bytes differ: ' + label)
        if not chunks[0]:
            return


def commands_pass(report):
    require(report.get('commands') and all(row['exit_code'] == 0 for row in report['commands']),
            'Native command failures in required report')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--identity', type=Path, required=True,
                        help='inputs/release-<build>.json: frozen release identity and delivery names')
    parser.add_argument('--stage', choices=('package', 'acceptance', 'delivery'), required=True)
    args = parser.parse_args()
    output = args.output_root.resolve()
    binding = read(output / 'input-binding.json')
    require(binding.get('state') == 'ACTUAL_ACTIONS_INPUT_BOUND', 'Actual Actions binding is missing')
    provenance = binding['provenance']
    identity = read(args.identity)
    expected = identity
    delivery_names = identity['delivery']
    unsigned = Path(provenance['unsigned_target_files']['path'])
    package = output / identity['package_dir']
    signed = package / 'signed-target_files.zip'
    report = read(package / 'package-report.json')
    require(report.get('status') == 'complete' and same_path(report['source_target_files'], unsigned),
            'Package report is incomplete or uses another unsigned input')
    commands_pass(report)
    for name in ('signed-target_files.zip', 'ota.zip', 'images.zip'):
        require((package / name).is_file() and (package / name).stat().st_size > 0, 'Missing package output: ' + name)
    props = report['signed_build_properties']
    require(props.get('ro.build.flavor') == expected['product']
            and props.get('ro.build.fingerprint') == expected['signed_fingerprint']
            and props.get('ro.build.version.incremental') == expected['incremental']
            and props.get('ro.build.date.utc') == expected['datetime']
            and props.get('ro.build.type') == 'user' and props.get('ro.build.tags') == 'release-keys',
            'Signed build identity differs from reviewed R6')
    ota_metadata = report['ota']['metadata']
    require(ota_metadata.get('post-build') == expected['signed_fingerprint']
            and ota_metadata.get('post-build-incremental') == expected['incremental']
            and ota_metadata.get('post-timestamp') == expected['datetime']
            and ota_metadata.get('pre-device') == 'crux' and ota_metadata.get('ota-type') == 'BLOCK'
            and 'pre-build' not in ota_metadata and ota_metadata.get('ota-wipe') != 'yes',
            'OTA metadata differs from reviewed full data-preserving R6')
    result = {'schema_version': 1, 'state': 'PASS', 'stage': args.stage,
              'incremental': expected['incremental'], 'fingerprint': expected['signed_fingerprint'],
              'signed_target_files': str(signed), 'runtime_acceptance': False}
    if args.stage != 'package':
        validation = output / 'validation'
        apks = read(validation / 'apk-apex.json')
        require(apks.get('state') == 'PASS' and not apks.get('errors')
                and apks['counts'] == {'flat_apks': 191, 'routed_flat_apks': 188,
                                      'explicitly_preserved_flat_apks': 3, 'apex': 27, 'resigned_apex': 26},
                'Current APK/APEX gate did not pass its actual shipping inventory')
        require(same_path(apks['inputs']['signed_target_files'], signed)
                and same_path(apks['inputs']['original_target_files'], unsigned), 'APK/APEX gate inputs differ')
        commands_pass(apks)
        avb = read(validation / 'image-avb/acceptance.json')
        require(avb.get('status') == 'PASS' and same_path(avb['target_files'], signed)
                and avb.get('flags') == 3 and len(avb['commands']) == 5, 'Current image AVB gate did not pass')
        commands_pass(avb)
        recovery = read(validation / 'recovery/inspection.json')
        require(recovery.get('status') == 'PASS' and recovery.get('components_match') is True
                and recovery.get('recovery_certs_match_target_files') is True
                and recovery.get('recovery_trusts_only_release_cert') is True
                and same_path(recovery['kernel_matches_retained_candidate'], Path(provenance['kernel']['image']['path']))
                and recovery['selected_properties']['ro.build.fingerprint'] == expected['signed_fingerprint'],
                'Current Recovery inspection/kernel/trust gate did not pass')
        commands_pass(recovery)
        ota = read(validation / 'ota-native/summary.json')
        expected_cases = {'release-public-certificate': 0, 'actual-recovery-otacerts': 0,
                          'wrong-public-certificate': 1, 'single-byte-damage': 1}
        require(ota.get('passed') is True and len(ota['cases']) == 4
                and {row['case']: row['actual_exit'] for row in ota['cases']} == expected_cases
                and all(row['passed'] is True and row['expected_exit'] == row['actual_exit'] for row in ota['cases'])
                and same_path(ota['input_ota'], package / 'ota.zip')
                and same_path(ota['actual_recovery_otacerts'], validation / 'recovery/otacerts.zip'),
                'Current four-case Recovery OTA signature gate did not pass')
        result['gates'] = {'apk_apex': 'PASS', 'image_avb': 'PASS', 'recovery': 'PASS', 'ota_native': 'PASS'}
        result['limits'] = ['Host integrity with AVB flags 3; no locked verified-boot claim.',
                            'API 33 flat APK/APEX gate excludes separate nested-APK key mapping.',
                            'Host gates do not establish device installer or runtime acceptance.']
    if args.stage == 'delivery':
        delivery = output / 'delivery'
        name = delivery_names['fastboot_dir']
        fastboot = delivery / name
        manifest = read(fastboot / 'manifest.json')
        fastboot_report = read(delivery / (name + '.package-report.json'))
        require(fastboot_report.get('status') == 'complete' and fastboot_report.get('runtime_acceptance') is False
                and same_path(fastboot_report['signed_target_files'], signed)
                and manifest['fingerprint'] == expected['signed_fingerprint']
                and manifest['version'] == expected['incremental']
                and manifest['signed_target_files_sha256'] == fastboot_report['signed_target_files_sha256'],
                'Fastboot package does not bind to current signed R6')
        partitions = {'boot', 'recovery', 'dtbo', 'system', 'vendor', 'vbmeta'}
        require(set(manifest['images']) == partitions, 'Fastboot bundle must contain exactly six Android images')
        with zipfile.ZipFile(signed) as target_files, zipfile.ZipFile(package / 'images.zip') as images, \
                zipfile.ZipFile(delivery / (name + '.zip')) as bundled:
            require(len(bundled.namelist()) == len(set(bundled.namelist())) == 12,
                    'Fastboot ZIP should contain exactly 12 unique files')
            require(json.loads(bundled.read(name + '/manifest.json')) == manifest,
                    'Fastboot ZIP manifest differs from directory manifest')
            for partition in sorted(partitions):
                with target_files.open('IMAGES/' + partition + '.img') as original, \
                        images.open(partition + '.img') as image_copy, \
                        (output / 'validation/image-avb' / (partition + '.img')).open('rb') as accepted, \
                        (fastboot / 'images' / (partition + '.img')).open('rb') as unpacked, \
                        bundled.open(name + '/images/' + partition + '.img') as zipped:
                    compare_streams([original, image_copy, accepted, unpacked, zipped], partition)
            with target_files.open('IMAGES/recovery.img') as original, \
                    (delivery / delivery_names['recovery']).open('rb') as published:
                compare_streams([original, published], 'published Recovery')
        with (package / 'ota.zip').open('rb') as original, \
                (delivery / delivery_names['ota']).open('rb') as published:
            compare_streams([original, published], 'published signed OTA')
        result['same_source_images'] = sorted(partitions)
        result['signed_ota_and_recovery_copied_unchanged'] = True
        result['fastboot_archive_sha256'] = fastboot_report['archive_sha256']
        result['fastboot_archive_bytes'] = fastboot_report['archive_bytes']
    (output / (args.stage + '-contract.json')).write_text(json.dumps(result, indent=2) + '\n')
    print('PASS: ' + identity['build_number'] + ' ' + args.stage + ' output contract')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile) as error:
        print('error: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
