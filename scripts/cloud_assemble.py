#!/usr/bin/env python3
"""Admit fresh distributed worker artifacts and assemble an unpublished delivery."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

import assemble_dev
import cloud_artifacts


BUILD_ID = 'TQ3A.230901.001.B1'
PRODUCT = 'aosp_crux'
VARIANT = 'userdebug'
GOALS = ['bootimage', 'recoveryimage', 'systemimage', 'vendorimage']
STATIC_BUILDINFO = {
    'TARGET_BUILD_TYPE': 'userdebug',
    'TARGET_BUILD_FLAVOR': 'aosp_crux-userdebug',
    'TARGET_DEVICE': 'crux',
    'CUSTOM_DEVICE': 'crux',
    'BUILD_ID': BUILD_ID,
    'PLATFORM_VERSION': '13',
    'BUILD_VERSION_TAGS': 'release-keys',
}
SOURCE_CONTRACT = (Path(__file__).resolve().parents[1]
                   / 'references/pe13-boot-contracts/source-declarative-boot-contract.json')


class AssembleError(ValueError):
    pass


def _utc_date(epoch: int) -> str:
    value = datetime.fromtimestamp(epoch, timezone.utc)
    weekdays = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
    months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
    return f'{weekdays[value.weekday()]} {months[value.month - 1]} {value.day:2d} {value:%H:%M:%S} UTC {value.year}'


def _graph_frontend(context: dict) -> tuple[dict, dict]:
    frontend = context.get('frontend')
    if not isinstance(frontend, dict):
        raise AssembleError('Context has no fresh frontend metadata')
    graph = frontend.get('graph_receipt', frontend)
    if not isinstance(graph, dict):
        raise AssembleError('Frontend graph receipt summary is malformed')
    return frontend, graph


def derive_identity(context: dict) -> dict:
    """Build expected properties from admitted graph/capsule metadata, never images."""
    frontend, graph = _graph_frontend(context)
    build_number = context.get('build_number')
    build_datetime = graph.get('build_datetime')
    fingerprint = graph.get('build_fingerprint')
    if (not isinstance(build_number, str) or not build_number.isdecimal()
            or graph.get('status') != 'success' or graph.get('build_number') != build_number
            or type(build_datetime) is not int or build_datetime < 0
            or not isinstance(fingerprint, str)
            or not re.fullmatch(r'Xiaomi/crux/crux:13/' + re.escape(BUILD_ID) + r'/' + re.escape(build_number)
                                + r':userdebug/test-keys', fingerprint)):
        raise AssembleError('Fresh graph identity is incomplete or inconsistent with the build')
    if graph.get('source_root') != context.get('source_root') or graph.get('out_root') != context.get('out_root'):
        raise AssembleError('Fresh graph source/OUT roots differ from the compilation context')
    if graph.get('source_manifest', {}).get('sha256') != context.get('source_manifest_sha256'):
        raise AssembleError('Fresh graph source manifest differs from compilation context')

    build_date_value = frontend.get('build_date')
    if isinstance(build_date_value, str) and build_date_value.strip().isdecimal():
        build_date = int(build_date_value.strip())
    elif type(build_date_value) is int:
        build_date = build_date_value
    else:
        raise AssembleError('Fresh graph build_date scalar must be an admitted epoch')
    buildinfo_env = frontend.get('buildinfo_environment')
    if not isinstance(buildinfo_env, dict):
        raise AssembleError('Context has no admitted buildinfo producer environment')
    for key, expected in STATIC_BUILDINFO.items():
        if buildinfo_env.get(key) != expected:
            raise AssembleError('Buildinfo producer environment differs from admitted product contract: ' + key)
    number_expr = '$(cat OUT/soong/build_number.txt)'
    date_expr = 'date -d @$(cat OUT/build_date.txt)'
    admitted_number_expr = buildinfo_env.get('BUILD_NUMBER')
    if admitted_number_expr == '\\' + number_expr:
        admitted_number_expr = number_expr
    admitted_date_expr = buildinfo_env.get('DATE')
    if admitted_date_expr == 'date -d @\\' + '$(cat OUT/build_date.txt)':
        admitted_date_expr = date_expr
    admitted_description = buildinfo_env.get('PRIVATE_BUILD_DESC')
    escaped_number = '\\' + number_expr
    if isinstance(admitted_description, str) and admitted_description.count(escaped_number) == 1:
        admitted_description = admitted_description.replace(escaped_number, number_expr, 1)
    expected_description_expr = f'aosp_crux-userdebug 13 {BUILD_ID} {number_expr} release-keys'
    if (admitted_number_expr != number_expr or admitted_description != expected_description_expr
            or admitted_date_expr != date_expr):
        raise AssembleError('Buildinfo producer scalar expressions differ from admitted graph')
    literals = {key: buildinfo_env[key] for key in STATIC_BUILDINFO}
    branding = frontend.get('branding_keys')
    expected_branding = {'org.pixelexperience.build_date', 'org.pixelexperience.build_date_utc'}
    if (not isinstance(branding, dict) or set(branding) != expected_branding
            or any(not isinstance(value, str) or not value.strip() for value in branding.values())):
        raise AssembleError('Fresh capsule manifests lack unique PE branding literals')
    branding_epoch = branding.get('org.pixelexperience.build_date_utc', '')
    if not branding_epoch.isdecimal():
        raise AssembleError('Fresh capsule PE branding UTC literal is not an epoch')
    expected_branding_date = datetime.fromtimestamp(int(branding_epoch), timezone.utc).strftime('%Y%m%d-%H%M')
    if branding['org.pixelexperience.build_date'] != expected_branding_date:
        raise AssembleError('Fresh capsule PE branding date does not match its own UTC literal')
    commands = frontend.get('identity_commands')
    if not isinstance(commands, list) or not commands:
        raise AssembleError('Fresh source capsule identity commands are missing')
    command_literals = {key: set() for key in expected_branding}
    for row in commands:
        command = row.get('command', '') if isinstance(row, dict) else ''
        for key, value in re.findall(r'(org\.pixelexperience\.build_date(?:_utc)?)=(\d[\d-]*)', command):
            command_literals.setdefault(key, set()).add(value)
    if any(command_literals.get(key) != {value} for key, value in branding.items()):
        raise AssembleError('PE branding metadata is not uniquely supported by admitted capsule commands')
    thumbprint = frontend.get('build_thumbprint')
    expected_thumbprint = f'13/{BUILD_ID}/{build_number}:userdebug/release-keys\n'
    if not isinstance(thumbprint, str) or thumbprint != expected_thumbprint:
        raise AssembleError('Fresh graph build_thumbprint scalar is missing or malformed')
    absent = frontend.get('absent_partition_namespaces')
    if not isinstance(absent, dict):
        raise AssembleError('Fresh context has no source-derived absent partition policy')
    identity = {
        'schema_version': 1,
        'required_buildinfo': {
            'ro.build.version.incremental': build_number,
            'ro.build.description': expected_description_expr.replace(number_expr, build_number),
            'ro.build.id': literals['BUILD_ID'],
            'ro.build.date.utc': str(build_datetime),
            'ro.build.date': _utc_date(build_date),
            'ro.build.tags': literals['BUILD_VERSION_TAGS'],
            'ro.build.type': literals['TARGET_BUILD_TYPE'],
            'ro.build.flavor': literals['TARGET_BUILD_FLAVOR'],
            'org.pixelexperience.device': literals['CUSTOM_DEVICE'],
        },
        'required_final_keys': {
            'ro.build.version.incremental': build_number,
            'ro.build.fingerprint': fingerprint,
            'ro.system.build.version.incremental': build_number,
            'ro.system.build.fingerprint': fingerprint,
            'ro.system.build.date.utc': str(build_datetime),
        },
        'branding_keys': dict(branding),
        'frontend_thumbprint': thumbprint,
        'absent_partition_namespaces': absent,
    }
    assemble_dev.verify_properties.validate_expected(identity)
    return identity


def derive_source_selection(context: dict, manifest_path: Path | None = None) -> dict:
    manifest_path = manifest_path or Path(__file__).resolve().parents[1] / 'config/cloud-source-manifest.json'
    manifest_bytes = Path(manifest_path).read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != context.get('source_manifest_sha256'):
        raise AssembleError('Checked-out source manifest differs from the accepted build context')
    manifest = json.loads(manifest_bytes)
    declared = context.get('source_selection')
    if (not isinstance(declared, dict) or declared.get('schema_version') != 1
            or declared.get('build_number') != context.get('build_number')
            or declared.get('source_manifest_sha256') != context.get('source_manifest_sha256')):
        raise AssembleError('Source revision declaration is not pinned to the fresh build')
    expected_projects = [{'path': item['path'], 'revision': item['revision']} for item in manifest['projects']]
    if declared.get('projects') != expected_projects:
        raise AssembleError('Source revision declaration differs from the accepted source manifest')
    frontend, graph = _graph_frontend(context)
    if frontend.get('source_revisions') != expected_projects:
        raise AssembleError('Frontend source revisions differ from the accepted source manifest')
    env = {
        'OUT_DIR': context['out_root'], 'BUILD_NUMBER': context['build_number'],
        'BUILD_FINGERPRINT': graph['build_fingerprint'], 'BUILD_DATETIME': str(graph['build_datetime']),
        'USE_CCACHE': '0', 'CCACHE_DISABLE': '1',
    }
    return {
        'schema_version': 1, 'purpose': 'fresh standard-runner aosp_crux-userdebug build',
        'graph_run': context['graph_run'], 'index_run': context['index_run'],
        'shard_run': context['shard_run'], 'source_run': context['source_run'],
        'build_number': context['build_number'], 'build_datetime': graph['build_datetime'],
        'target_product': PRODUCT, 'target_build_variant': VARIANT, 'goals': list(GOALS),
        'ninja_rom_actions_executed': True, 'source_manifest_sha256': context['source_manifest_sha256'],
        'source_root': context['source_root'], 'env': env,
        'projects': expected_projects,
        'graph_receipt_sha256': graph.get('receipt_sha256'),
        'build_date_epoch': int(frontend['build_date']) if isinstance(frontend['build_date'], str)
                            else frontend['build_date'],
        'kernel_source_alias': context.get('kernel_source_alias'),
    }


def required_exports(context: dict) -> tuple[dict[str, str], set[str]]:
    out = Path(context['out_root'])
    product = out / 'target/product/crux'
    kernel = product / 'obj/KERNEL_OBJ'
    qcom = kernel / 'arch/arm64/boot/dts/qcom'
    required = {
        out / 'soong/build_number.txt', product / 'build_fingerprint.txt',
        product / 'obj/PACKAGING/system_build_prop_intermediates/buildinfo.prop',
        *(product / (name + '.img') for name in ('boot', 'recovery', 'system', 'vendor')),
        kernel,
    }
    kernel_children = {
        kernel / 'arch/arm64/boot/Image', kernel / 'vmlinux', kernel / 'System.map', kernel / '.config',
        *(qcom / (name + '.dtb') for name in ('sm8150', 'sm8150-v2', 'sm8150p', 'sm8150p-v2')),
    }
    optional = {
        product / 'ramdisk.img', product / 'ramdisk-recovery.img',
        kernel / 'include/config/kernel.release',
        qcom / 'crux-sm8150-overlay.dtbo',
        product / 'dtbo.img',
    }
    exports = context.get('assembly_exports')
    if not isinstance(exports, dict) or not exports:
        raise AssembleError('Context has no assembly output-owner map')
    normalized = {}
    for path, owner in exports.items():
        if not isinstance(path, str) or not Path(path).is_absolute() or not isinstance(owner, str):
            raise AssembleError('Assembly output-owner map must map absolute paths to shard IDs')
        normalized[str(Path(path))] = owner
    missing = {str(path) for path in required} - normalized.keys()
    if missing:
        raise AssembleError('Fresh plan omits required assembly exports: ' + ', '.join(sorted(missing)))
    kernel_owner = normalized[str(kernel)]
    for path in kernel_children:
        key = str(path)
        owner = normalized.setdefault(key, kernel_owner)
        if owner != kernel_owner:
            raise AssembleError('Kernel child is not owned by the KernelRoot producer: ' + key)
    permitted = {str(path) for path in required | kernel_children | optional}
    extras = set(normalized) - permitted
    if extras:
        raise AssembleError('Unexpected assembly exports outside the reviewed role set: ' + ', '.join(sorted(extras)))
    return normalized, {owner for owner in normalized.values()}


def _fresh_binding(context: dict) -> dict:
    return {key: context[key] for key in ('execution_id', 'pack_run', 'shard_run',
                                          'plan_sha256', 'source_manifest_sha256')}


def _binding(context: dict, task_id: str) -> dict:
    task = context['tasks'][task_id]
    return {'id': task_id, 'run_id': context['execution_id'], 'artifact': 'shard-' + task_id,
            'expected_worker_commit': context['worker_commit'],
            'expected_manifest_sha256': task['manifest_sha256']}


def validate_context(context: dict, manifest_path: Path | None = None) -> tuple[dict, dict, set[str]]:
    if not isinstance(context, dict) or context.get('schema_version') != 1:
        raise AssembleError('Unsupported compile context schema')
    for key in ('execution_id', 'pack_run', 'shard_run', 'graph_run', 'index_run', 'source_run'):
        if not isinstance(context.get(key), str) or not context[key].isdecimal():
            raise AssembleError('Context has invalid run identity: ' + key)
    if (not isinstance(context.get('repository'), str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', context['repository'])
            or not isinstance(context.get('worker_commit'), str)
            or not re.fullmatch(r'[a-f0-9]{40}', context['worker_commit'])
            or not isinstance(context.get('plan_sha256'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', context['plan_sha256'])
            or not isinstance(context.get('source_manifest_sha256'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', context['source_manifest_sha256'])):
        raise AssembleError('Context has invalid repository, worker SHA, or manifest/plan pin')
    if any(not isinstance(context.get(key), str) or not Path(context[key]).is_absolute()
           for key in ('source_root', 'out_root')):
        raise AssembleError('Context source/OUT roots must be absolute paths')
    graph_index = context.get('graph_index')
    if (not isinstance(graph_index, dict) or type(graph_index.get('bytes')) is not int
            or graph_index['bytes'] <= 0 or not re.fullmatch(r'[a-f0-9]{64}', graph_index.get('sha256', ''))):
        raise AssembleError('Context has invalid pruned graph identity')
    tasks, waves = context.get('tasks'), context.get('waves')
    if (not isinstance(tasks, dict) or not tasks or not isinstance(waves, list)
            or len(waves) != 9 or any(not isinstance(wave, list) or not wave for wave in waves)):
        raise AssembleError('Context is not an admitted nine-wave plan')
    flattened = [task_id for wave in waves for task_id in wave]
    if (len(flattened) != len(tasks) or set(flattened) != set(tasks) or len(set(flattened)) != len(flattened)
            or any(not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', task_id)
                   for task_id in flattened)):
        raise AssembleError('Context waves do not cover each admitted task exactly once')
    completed = set()
    for wave_index, wave in enumerate(waves):
        for task_id in wave:
            task = tasks[task_id]
            if (task.get('id') != task_id or task.get('wave') != wave_index
                    or not isinstance(task.get('manifest_sha256'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', task['manifest_sha256'])
                    or not isinstance(task.get('depends_on'), list)
                    or not set(task['depends_on']) <= completed):
                raise AssembleError('Invalid task binding or dependency order: ' + str(task_id))
        completed.update(wave)
    frontend, graph = _graph_frontend(context)
    if graph.get('build_number') != context.get('build_number'):
        raise AssembleError('Graph and compile build numbers differ')
    producers, producer_ids = required_exports(context)
    for owner in producer_ids:
        if owner not in tasks:
            raise AssembleError('Assembly output owner is outside the accepted plan: ' + owner)
    derive_identity(context)
    derive_source_selection(context, manifest_path)
    return producers, graph, producer_ids


def _download(run_id: str, repository: str, artifact: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    cloud_artifacts.download_artifacts(run_id, repository, destination, name=artifact)


def _validate_receipt(context: dict, task_id: str, receipt: dict) -> None:
    if receipt.get('diagnostic_only'):
        raise AssembleError('Diagnostic output cannot enter ROM assembly')
    task = context['tasks'][task_id]
    expected = _binding(context, task_id)
    if receipt.get('status') != 'success' or receipt.get('fresh_build') != _fresh_binding(context):
        raise AssembleError('Task is not a successful producer of this compile execution: ' + task_id)
    for key, value in (('id', task_id), ('run_id', context['execution_id']),
                       ('worker_commit', context['worker_commit']), ('manifest_sha256', task['manifest_sha256'])):
        if str(receipt.get(key)) != str(value):
            raise AssembleError('Fresh receipt binding differs for %s:%s' % (key, task_id))
    expected_dependencies = [_binding(context, dependency) for dependency in task['depends_on']]
    if receipt.get('declared_dependencies') != expected_dependencies:
        raise AssembleError('Fresh task imported undeclared producers: ' + task_id)
    if receipt.get('build_environment', {}).get('CCACHE_DISABLE') != '1':
        raise AssembleError('Fresh task did not disable ccache: ' + task_id)
    if receipt.get('archive_sha256') is None or receipt.get('status') != 'success':
        raise AssembleError('Fresh task has no successful output archive: ' + task_id)
    if not isinstance(receipt.get('outputs'), list):
        raise AssembleError('Fresh task has no output inventory: ' + task_id)


def admit_producers(context: dict, staging: Path, downloader=_download,
                    manifest_path: Path | None = None) -> tuple[list[dict], Path]:
    exports, _, producer_ids = validate_context(context, manifest_path)
    staging = Path(staging)
    receipts_root = staging / 'fresh-receipts'
    receipts_root.mkdir(parents=True, exist_ok=False)
    receipts = {}
    # All tasks are required, not just output owners: assembly is admitted only
    # after the complete dependency-ordered build succeeded from the cold plan.
    for task_id in sorted(context['tasks']):
        directory = receipts_root / task_id
        downloader(context['execution_id'], context['repository'], 'fresh-receipt-' + task_id, directory)
        receipt_path = directory / 'receipt.json'
        if not receipt_path.is_file() or receipt_path.is_symlink():
            raise AssembleError('Missing fresh task receipt: ' + task_id)
        receipt = json.loads(receipt_path.read_text())
        _validate_receipt(context, task_id, receipt)
        receipts[task_id] = receipt

    output_root = staging / 'producers'
    output_root.mkdir()
    bindings = []
    specs_by_owner = {}
    for index, task_id in enumerate(sorted(producer_ids)):
        directory = output_root / str(index)
        downloader(context['execution_id'], context['repository'], 'shard-' + task_id, directory)
        output_receipt_path = directory / 'receipt.json'
        if not output_receipt_path.is_file() or output_receipt_path.is_symlink():
            raise AssembleError('Missing output artifact receipt: ' + task_id)
        receipt = json.loads(output_receipt_path.read_text())
        if receipt != receipts[task_id]:
            raise AssembleError('Output artifact receipt differs from its fresh lineage receipt: ' + task_id)
        specs = {item['path']: item for item in receipt['outputs'] if isinstance(item, dict) and isinstance(item.get('path'), str)}
        if len(specs) != len(receipt['outputs']):
            raise AssembleError('Output receipt has invalid or duplicate output specs: ' + task_id)
        specs_by_owner[task_id] = specs
        bindings.append(_binding(context, task_id))

    for path, owner in exports.items():
        spec = specs_by_owner.get(owner, {}).get(path)
        if spec is None:
            raise AssembleError('Declared output owner receipt does not own path: ' + path)
        expected_type = 'directory' if path.endswith('/obj/KERNEL_OBJ') else 'file'
        if spec.get('type') != expected_type or (expected_type == 'file' and spec.get('size', 0) <= 0):
            raise AssembleError('Assembly output has unexpected type/size: ' + path)
    kernel_root = next(path for path in exports if path.endswith('/obj/KERNEL_OBJ'))
    kernel_owner = exports[kernel_root]
    for path in exports:
        if path.startswith(kernel_root + '/') and exports[path] != kernel_owner:
            raise AssembleError('Kernel output is not owned by the KernelRoot producer: ' + path)
    raw_overlay = kernel_root + '/arch/arm64/boot/dts/qcom/crux-sm8150-overlay.dtbo'
    product_dtbo = str(Path(context['out_root']) / 'target/product/crux/dtbo.img')
    raw_owned = raw_overlay in specs_by_owner[kernel_owner]
    product_owned = (product_dtbo in exports
                     and product_dtbo in specs_by_owner[exports[product_dtbo]])
    if not (raw_owned or product_owned):
        raise AssembleError('Fresh producer artifacts contain neither raw Crux overlay nor product DTBO image')
    return bindings, output_root


def assemble(context_path: Path, output: Path, staging: Path, downloader=_download,
             assembler=assemble_dev.assemble, manifest_path: Path | None = None) -> dict:
    context = json.loads(Path(context_path).read_text())
    staging, output = Path(staging), Path(output)
    if staging.exists() and any(staging.iterdir()):
        raise AssembleError('Assembly staging directory must be empty')
    staging.mkdir(parents=True, exist_ok=True)
    identity = derive_identity(context)
    source_selection = derive_source_selection(context, manifest_path)
    source_contract = SOURCE_CONTRACT
    if not source_contract.is_file():
        raise AssembleError('Missing fresh source-declarative boot contract: ' + str(source_contract))
    source_contract_pin = {'path': str(source_contract), 'sha256': hashlib.sha256(source_contract.read_bytes()).hexdigest()}
    source_selection['fit_source_contract'] = source_contract_pin
    identity_path = staging / 'fresh-identity.json'
    source_selection_path = staging / 'fresh-source-selection.json'
    identity_path.write_text(json.dumps(identity, indent=2, sort_keys=True) + '\n')
    source_selection_path.write_text(json.dumps(source_selection, indent=2, sort_keys=True) + '\n')
    bindings, dependency_root = admit_producers(context, staging, downloader, manifest_path)
    receipt = assembler(bindings, dependency_root, output, identity_path,
                        context['build_number'], source_selection, source_contract=source_contract)
    if receipt.get('status') != 'success':
        raise AssembleError('Fresh assembly gates failed: ' + str(receipt.get('error', 'unknown error')))
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--staging', required=True)
    args = parser.parse_args(argv)
    try:
        receipt = assemble(Path(args.context), Path(args.output), Path(args.staging))
    except (AssembleError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        print('cloud_assemble: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps({'status': receipt['status'], 'delivery': receipt.get('delivery'),
                      'assets': len(receipt.get('assets', {}))}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
