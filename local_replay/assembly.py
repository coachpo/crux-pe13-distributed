#!/usr/bin/env python3
"""Local-only adapter for existing image gates, FIT recipe and unsigned payload."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay
import container
import assemble_dev
import cloud_assemble
import cloud_reuse
import crux_zip
import image_inventory
import package_fits
import worker


def source_selection(context, manifest_path):
    if worker.digest(manifest_path) != context['source_manifest_sha256']:
        raise ValueError('Source manifest differs from the selected source pins')
    if not replay.local_preparation(context):
        result = cloud_assemble.derive_source_selection(context, manifest_path)
    else:
        manifest = replay.read(manifest_path)
        expected = [{'path': item['path'], 'revision': item['revision']} for item in manifest['projects']]
        frontend, graph = cloud_assemble._graph_frontend(context)
        if (manifest['source_root'] != context['source_root']
                or expected != context['source_selection']['projects']
                or frontend.get('source_revisions') != expected):
            raise ValueError('Local frontend/source selection differs from the locked manifest')
        result = {'schema_version': 1, 'build_number': context['build_number'],
                  'build_datetime': graph['build_datetime'], 'source_root': context['source_root'],
                  'source_manifest_sha256': context['source_manifest_sha256'], 'projects': expected,
                  'target_product': 'aosp_crux', 'target_build_variant': 'userdebug',
                  'kernel_source_alias': context.get('kernel_source_alias')}
    result.update(diagnostic_only=True, purpose='Local cold image/FIT/unsigned-payload diagnostic; no publication')
    return result


def execute():
    record = replay.read('/inputs/input.json')
    if (record.get('schema') != replay.SCHEMA or record.get('diagnostic_only') is not True
            or record.get('operation') != 'assembly'):
        raise ValueError('Expected a local-only assembly input record')
    replay.check_file(record['context'], '/inputs/context.json')
    replay.check_file(record['source_manifest'], '/inputs/source-manifest.json')
    context = replay.read('/inputs/context.json')
    replay.validate_reference(context, next(iter(context['tasks'])))
    if cloud_reuse.execution_digest(replay.ROOT) != record['execution_digest']:
        raise ValueError('Assembly implementation differs from the input record')
    source, out = Path(context['source_root']), Path(context['out_root'])
    if os.path.lexists(source) or os.path.lexists(out):
        raise ValueError('Local assembly requires a fresh SOURCE/OUT namespace')
    container.reject_actions_environment(os.environ)
    environment = cloud_reuse.native_environment()
    environment['PATH'] += ':/usr/sbin:/sbin'
    os.environ.clear(); os.environ.update(environment)
    actions_context = None
    if record.get('actions_context'):
        replay.check_file(record['actions_context'], '/inputs/actions-context.json')
        actions_context = replay.read('/inputs/actions-context.json')
    _, producers = cloud_assemble.required_exports(context)
    if (len(record['dependencies']) != len(producers)
            or {item['id'] for item in record['dependencies']} != producers):
        raise ValueError('Assembly inputs must cover exactly the required producer set')
    directories, receipts = [], []
    for item in record['dependencies']:
        directory = Path('/inputs/dependencies') / item['id']
        replay.check_file(item['receipt'], directory / 'receipt.json')
        replay.check_file(item['archive'], directory / 'outputs.tar.zst')
        receipt = replay.admit_dependency(context, item['id'], directory, item['kind'],
                                           record['context']['sha256'], actions_context)
        if any(not Path(spec['path']).is_relative_to(out) for spec in receipt['outputs']):
            raise ValueError('Assembly producer exports outside the reviewed OUT namespace')
        directories.append(directory); receipts.append(receipt)
    accepted, _ = worker.merge_dependencies(directories)
    identity = cloud_assemble.derive_identity(context)
    selection = source_selection(context, Path('/inputs/source-manifest.json'))
    product = out / 'target/product/crux'
    output = Path('/diagnostic-output')
    report = assemble_dev.validate_identity_files(identity, product, out)
    if report['errors']:
        raise ValueError('Restored identity inputs differ: ' + json.dumps(report['errors']))
    thumbprint = output / 'build_thumbprint.txt'
    assemble_dev.write_thumbprint(identity, thumbprint)
    gates = assemble_dev.gate_images(product, identity, thumbprint, 'debugfs', 'simg2img', 'dumpimage', out, accepted)
    contract = cloud_assemble.SOURCE_CONTRACT
    selection['fit_source_contract'] = {'path': str(contract), 'sha256': worker.digest(contract)}
    fits = package_fits.prepare(accepted, receipts, output / 'fit-stage', source_selection=selection,
                                source_contract=contract, product_dir=product,
                                kernel_dir=product / 'obj/KERNEL_OBJ', build_fits=True)
    if fits.get('status') != 'success' or not fits.get('fits_generated'):
        raise ValueError('Existing FIT gates failed: ' + str(fits.get('error')))
    installer = Path('/tmp/local-replay-installer')
    shutil.copytree(replay.ROOT / 'installer', installer, ignore=shutil.ignore_patterns('build', '__pycache__'))
    with (output / 'installer.log').open('w') as log:
        subprocess.run(['make', '-C', str(installer), 'test'], check=True, stdout=log, stderr=subprocess.STDOUT)
        subprocess.run(['make', '-C', str(installer), 'CROSS=aarch64-linux-gnu-'], check=True,
                       stdout=log, stderr=subprocess.STDOUT)
    raw = Path('/tmp/local-replay-raw')
    raw.mkdir()
    for name in ('system', 'vendor'):
        subprocess.run(['simg2img', str(product / (name + '.img')), str(raw / (name + '.img'))], check=True)
    inventory = image_inventory.check(context['image_inventory'],
                                      {'system': raw / 'system.img', 'vendor': raw / 'vendor.img'}, 'debugfs')
    if any(entry['missing'] for entry in inventory.values()):
        raise ValueError('Installed files missing from the image: ' + json.dumps(inventory))
    pin = replay.read(replay.ROOT / 'config/install-twrp.json')
    fit_directory = output / 'fit-stage/fits'
    # The forbidden literals are A's CRUX_FORBID_TEXT secret; this credential-free adapter has none,
    # so its payload records forbidden_text_checked 0 and that scan stays an A check.
    crux_zip.prepare(argparse.Namespace(system=raw / 'system.img', vendor=raw / 'vendor.img',
        recovery_fit=fit_directory / 'recovery-cepheus.itb', rom_fit=fit_directory / 'rom-cepheus.itb',
        installer=installer / 'build/crux-installer', build_number=context['build_number'],
        any_serial=True, serial=None, twrp_profile=[pin['profile_id']], uboot_role=crux_zip.UBOOT_ROLE,
        zstd_level=12, forbid_text=[], out=output / 'unsigned-payload'))
    result = {'schema': replay.SCHEMA, 'diagnostic_only': True, 'status': 'success',
              'signed': False, 'installable': False, 'publication_admission': False,
              'context_sha256': record['context']['sha256'], 'execution_digest': record['execution_digest'],
              'source_selection': selection, 'producer_inputs': record['dependencies'],
              'identity': identity, 'image_gates': gates, 'image_inventory': inventory, 'fits': fits,
              'unsigned_payload': replay.read(output / 'unsigned-payload/payload.json')}
    (output / 'local-assembly.json').write_text(json.dumps(result, indent=2) + '\n')
    (output / 'LOCAL-DIAGNOSTIC-NOT-INSTALLABLE.txt').write_text(
        'Unpublished diagnostic staging only. No signature or installable ZIP was generated.\n')
    print(json.dumps({'schema': replay.SCHEMA, 'diagnostic_only': True, 'status': 'success',
                      'signed': False, 'installable': False}), flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(execute())
    except Exception as error:
        Path('/diagnostic-output/local-assembly.json').write_text(json.dumps({
            'schema': replay.SCHEMA, 'diagnostic_only': True, 'status': 'failed',
            'signed': False, 'installable': False, 'publication_admission': False,
            'error_type': type(error).__name__, 'error': str(error)}, indent=2) + '\n')
        raise
