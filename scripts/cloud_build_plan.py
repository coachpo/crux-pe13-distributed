#!/usr/bin/env python3
"""Admit prepared cold capsules and coordinate a new artifact-only compilation."""
from __future__ import annotations

import argparse
import shlex
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import cloud_artifacts
from cloud_license_inputs import LicenseMetadataReads
from cloud_rust_native import rust_native_file_reads
from cloud_source_reads import iter_invocations


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(data)
    return digest.hexdigest()


def fresh_binding(context):
    return {key: context[key] for key in ('execution_id', 'pack_run', 'shard_run',
                                          'plan_sha256', 'source_manifest_sha256')}


def admit_graph(graph_receipt_path, index_receipt, registry, build_number):
    """Bind the freeze run's graph receipt to the index that sliced it; return its summary."""
    graph_receipt = read(graph_receipt_path)
    receipt_sha = sha(graph_receipt_path)
    if (graph_receipt.get('status') != 'success' or graph_receipt.get('build_number') != build_number
            or graph_receipt.get('source_manifest', {}).get('sha256') != registry['source_manifest_sha256']):
        raise ValueError('Fresh graph receipt is not the successful graph of this build and source manifest')
    if (index_receipt.get('status') != 'success' or index_receipt.get('graph_run') != registry['graph_run']
            or index_receipt.get('build_number') != build_number
            or index_receipt.get('manifest_sha256') != registry['source_manifest_sha256']
            or index_receipt.get('graph_receipt_sha256') != receipt_sha):
        raise ValueError('Fresh graph metadata differs from the canonical index of the preparation run')
    manifest = {key: value for key, value in graph_receipt['source_manifest'].items() if key != 'projects'}
    return {**graph_receipt, 'source_manifest': manifest, 'receipt_sha256': receipt_sha}


def producer_binding(context, task_id):
    return {'id': task_id, 'run_id': context['execution_id'], 'artifact': 'shard-' + task_id,
            'expected_worker_commit': context['worker_commit'],
            'expected_manifest_sha256': context['tasks'][task_id]['manifest_sha256']}


def buildinfo_environment(command):
    argv = shlex.split(command)
    while argv and Path(argv[0]).name in ('bash', 'sh') and '-c' in argv:
        argv = shlex.split(argv[argv.index('-c') + 1])
    result = {}
    for token in argv:
        if not re.match(r'^[A-Z_][A-Z_0-9]*=', token):
            break
        key, value = token.split('=', 1)
        result[key] = value
    if not {'BUILD_ID', 'BUILD_VERSION_TAGS', 'PRIVATE_BUILD_DESC'} <= result.keys():
        raise ValueError('Admitted buildinfo command has no literal build environment')
    return result


def admit_host_jar_bundles(tasks, owners, commands, source_root, out_root):
    """Transport co-installed Java launcher runtimes from declared fresh owners."""
    pairs = {}
    for executable in owners:
        path = Path(executable)
        if (path.parent.name != 'bin' or not executable.startswith(out_root + '/')
                or '/host/' not in executable):
            continue
        runtime = str(path.parent.parent / 'framework' / (path.name + '.jar'))
        if runtime in owners or path.name in {'d8', 'r8', 'r8-compat-proguard', 'manifest-merger', 'retrace', 'extractmarker'}:
            pairs[executable] = runtime
    bundles = []
    for task_id, rows in commands.items():
        used = set()
        for command in rows:
            if not any(executable in command for executable in pairs):
                continue
            for argv, cwd in iter_invocations({'command': command}, source_root):
                program = os.path.normpath(argv[0] if argv[0].startswith('/') else str(Path(cwd) / argv[0]))
                if program in pairs:
                    used.add(program)
        for executable in sorted(used):
            runtime = pairs[executable]
            if runtime not in owners:
                raise ValueError('Invoked Java launcher runtime has no admitted declared producer: ' + runtime)
            producer = owners[runtime]
            if producer == task_id:
                tasks[task_id]['local_runtime_targets'] = sorted(set(tasks[task_id].get('local_runtime_targets', [])) | {runtime})
                bundles.append({'consumer': task_id, 'executable': executable, 'runtime': runtime,
                                'producer': producer, 'transport': 'local-prebuild'})
                continue
            if tasks[producer]['wave'] >= tasks[task_id]['wave']:
                raise ValueError('Java runtime producer is not in an earlier admitted wave: ' + executable)
            tasks[producer]['extra_exports'] = sorted(set(tasks[producer].get('extra_exports', [])) | {runtime})
            tasks[task_id]['depends_on'] = sorted(set(tasks[task_id]['depends_on']) | {producer})
            tasks[task_id].setdefault('runtime_inputs', []).append({'path': runtime, 'producer': producer})
            bundles.append({'consumer': task_id, 'executable': executable, 'runtime': runtime, 'producer': producer})
    return bundles


def admit_rust_native_inputs(tasks, owners, edges, source_root, out_root):
    """Transport literal Rust linker inputs from their declared fresh owners."""
    relations = {}
    for task_id, rows in edges.items():
        for edge_index, edge in enumerate(rows):
            for path in rust_native_file_reads(edge, source_root, out_root):
                producer = owners.get(path)
                if producer is None:
                    raise ValueError('Rust native link input has no admitted declared producer: ' + path)
                if producer == task_id:
                    # These remain outputs of this task's original sliced Ninja;
                    # their native dependency ordering is not changed here.
                    continue
                if tasks[producer]['wave'] >= tasks[task_id]['wave']:
                    raise ValueError('Rust native link producer is not in an earlier admitted wave: ' + path)
                tasks[producer]['extra_exports'] = sorted(set(tasks[producer].get('extra_exports', [])) | {path})
                tasks[task_id]['depends_on'] = sorted(set(tasks[task_id]['depends_on']) | {producer})
                inputs = {item['path']: item['producer'] for item in tasks[task_id].get('runtime_inputs', [])}
                if path in inputs and inputs[path] != producer:
                    raise ValueError('Conflicting runtime input owner: ' + path)
                inputs[path] = producer
                tasks[task_id]['runtime_inputs'] = [
                    {'path': value, 'producer': inputs[value]} for value in sorted(inputs)]
                relation = relations.setdefault((task_id, path), {
                    'consumer': task_id, 'path': path, 'producer': producer, 'edge_indices': []})
                relation['edge_indices'].append(edge_index)
    return [relations[key] for key in sorted(relations)]


def admit_license_metadata_inputs(tasks, owners, edges, source_root, out_root):
    """Admit complete notice reads without importing any previously built data."""
    resolver = LicenseMetadataReads(
        (edge for rows in edges.values() for edge in rows), source_root, out_root)
    exports, local, sources, imports, dependencies = {}, {}, {}, {}, {}
    readers = []
    for task_id, rows in edges.items():
        for edge_index, edge in enumerate(rows):
            reads = resolver.notice_reads(edge)
            if reads is None:
                continue
            readers.append({'consumer': task_id, 'edge_index': edge_index, **reads})
            sources.setdefault(task_id, set()).update(reads['source_inputs'])
            for path in reads['metadata_inputs']:
                producer = owners.get(path)
                if producer is None:
                    raise ValueError('License metadata has no admitted declared producer: ' + path)
                if producer == task_id:
                    local.setdefault(task_id, set()).add(path)
                    continue
                if tasks[producer]['wave'] >= tasks[task_id]['wave']:
                    raise ValueError('License metadata producer is not in an earlier admitted wave: ' + path)
                exports.setdefault(producer, set(tasks[producer].get('extra_exports', []))).add(path)
                inputs = imports.setdefault(task_id, {
                    item['path']: item['producer'] for item in tasks[task_id].get('runtime_inputs', [])})
                if path in inputs and inputs[path] != producer:
                    raise ValueError('Conflicting license metadata owner: ' + path)
                inputs[path] = producer
                dependencies.setdefault(task_id, set(tasks[task_id]['depends_on'])).add(producer)
    for producer, paths in exports.items():
        tasks[producer]['extra_exports'] = sorted(paths)
    for task_id, paths in local.items():
        tasks[task_id]['local_runtime_targets'] = sorted(
            set(tasks[task_id].get('local_runtime_targets', [])) | paths)
    for task_id, paths in sources.items():
        tasks[task_id]['required_notice_sources'] = sorted(paths)
    for task_id, inputs in imports.items():
        tasks[task_id]['runtime_inputs'] = [
            {'path': path, 'producer': inputs[path]} for path in sorted(inputs)]
        tasks[task_id]['depends_on'] = sorted(dependencies[task_id])
    return readers


def admit_inputs(registry, receipt, inventory, plan, plan_sha):
    if registry.get('status') != 'success' or receipt.get('status') != 'success':
        raise ValueError('Preparation input did not succeed')
    for key in ('build_number', 'graph_run', 'index_run', 'source_run'):
        if registry[key] != receipt[key]:
            raise ValueError('Preparation binding differs: ' + key)
    if registry['source_manifest_sha256'] != receipt['manifest_sha256']:
        raise ValueError('Source manifests differ')
    if registry['graph_index'] != {key: receipt['pruned_database'][key] for key in ('bytes', 'sha256')}:
        raise ValueError('Pruned graph identities differ')
    if registry['plan_sha256'] != plan_sha or inventory['plan_sha256'] != plan_sha:
        raise ValueError('Plan byte identity differs')
    waves = [[task['id'] for task in wave] for wave in plan['waves']]
    ids = [task for wave in waves for task in wave]
    capsules = {row['shard_id']: row for row in registry['capsules']}
    # The task count is the preparation registry's, which pack-summary checked against the slice plan.
    # Match build-standard.yml's serial calls, including the dexpreopt-enabled final wave.
    if (len(waves) != 9 or any(not wave for wave in waves) or len(set(ids)) != len(ids)
            or registry['shard_count'] != len(ids) or set(ids) != set(capsules)
            or set(ids) != set(inventory['shards'])):
        raise ValueError('Expected the complete nine-wave inventory of the preparation registry')
    tasks, seen = {}, set()
    for number, wave in enumerate(plan['waves']):
        for task in wave:
            if not set(task['depends_on']) <= seen:
                raise ValueError('Task depends on an uncompleted/same wave: ' + task['id'])
            row = capsules[task['id']]
            tasks[task['id']] = {'id': task['id'], 'wave': number,
                'depends_on': task['depends_on'],
                'manifest_sha256': inventory['shards'][task['id']]['manifest_sha256'],
                **{key: row[key] for key in ('archive', 'archive_bytes', 'archive_sha256')}}
        seen.update(task['id'] for task in wave)
    return waves, tasks


def bootstrap(args):
    output, staging = Path(args.output), Path(args.staging)
    output.mkdir(parents=True, exist_ok=True)
    execution = os.environ['GITHUB_RUN_ID']
    commit = os.environ['GITHUB_SHA']
    repository = os.environ['GITHUB_REPOSITORY']
    if not execution.isdecimal() or not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('Compilation requires a new Actions execution identity')
    cloud_artifacts.download_artifacts(args.pack_run, repository, staging / 'pack',
                                      name='cloud-pack-registry-' + args.build_number)
    cloud_artifacts.download_artifacts(args.shard_run, repository, staging / 'shards',
                                      name='cloud-shards-' + args.build_number)
    registry = read(staging / 'pack/pack-registry.json')
    shard_dir = staging / 'shards'
    receipt, inventory = read(shard_dir / 'shard-receipt.json'), read(shard_dir / 'slice-inventory.json')
    if registry['shard_run'] != args.shard_run or registry['build_number'] != args.build_number:
        raise ValueError('Requested preparation run/build differs')
    expanded = staging / 'expanded'
    expanded.mkdir(parents=True, exist_ok=True)
    subprocess.run(['tar', '--zstd', '-xf', str(shard_dir / 'shards.tar.zst'), '-C', str(expanded)], check=True)
    plan_path = expanded / 'shards/plan.json'
    plan = read(plan_path)
    waves, tasks = admit_inputs(registry, receipt, inventory, plan, sha(plan_path))
    commands, branding, product_recipe, buildinfo = [], {}, None, None
    owners, task_commands, task_edges = {}, {}, {}
    for task in tasks.values():
        manifest_path = expanded / 'shards' / task['id'] / 'manifest.json'
        if sha(manifest_path) != task['manifest_sha256']:
            raise ValueError('Shard manifest integrity differs: ' + task['id'])
        manifest = read(manifest_path)
        task_commands[task['id']] = manifest['commands']
        task_edges[task['id']] = manifest['edges']
        for path in manifest['outputs']:
            if path in owners and owners[path] != task['id']:
                raise ValueError('Multiple declared output owners: ' + path)
            owners[path] = task['id']
        for edge in manifest['edges']:
            if any(path.endswith('/buildinfo.prop') for path in edge.get('outputs', [])):
                commands.append({'outputs': edge['outputs'], 'command': edge['command']})
                buildinfo = buildinfo_environment(edge['command'])
            elif 'org.pixelexperience.build_date' in edge.get('command', ''):
                commands.append({'outputs': edge['outputs'], 'command': edge['command']})
            if any(path.endswith('/system/product/etc/build.prop') for path in edge.get('outputs', [])):
                product_recipe = edge['command']
            for key, value in re.findall(r'(org\.pixelexperience\.build_date(?:_utc)?)=(\d[\d-]*)', edge.get('command', '')):
                if key in branding and branding[key] != value:
                    raise ValueError('Conflicting branding literals in the admitted graph')
                branding[key] = value
    host_bundles = admit_host_jar_bundles(tasks, owners, task_commands, receipt['source_root'], receipt['out_root'])
    rust_inputs = admit_rust_native_inputs(tasks, owners, task_edges, receipt['source_root'], receipt['out_root'])
    license_readers = admit_license_metadata_inputs(tasks, owners, task_edges, receipt['source_root'], receipt['out_root'])
    if buildinfo is None or len(branding) != 2:
        raise ValueError('Missing independent graph-derived build identity')
    cloud_artifacts.download_artifacts(registry['graph_run'], repository, staging / 'graph',
                                      name='cloud-graph-' + args.build_number)
    cloud_artifacts.download_artifacts(registry['index_run'], repository, staging / 'index',
                                      name='cloud-index-' + args.build_number)
    graph_directory = staging / 'graph'
    graph = admit_graph(graph_directory / 'graph/graph-receipt.json', read(staging / 'index/index-receipt.json'),
                        registry, args.build_number)
    scalars = {}
    for key, relative in {'build_number': 'soong/build_number.txt', 'build_date': 'build_date.txt',
                          'build_fingerprint': 'target/product/crux/build_fingerprint.txt',
                          'build_thumbprint': 'target/product/crux/build_thumbprint.txt'}.items():
        value = subprocess.run(['tar', '--zstd', '-xOf', str(graph_directory / 'graph-out.tar.zst'),
                                'android-out/' + relative], check=True, capture_output=True).stdout.decode()
        scalars[key] = value
    if scalars['build_number'].strip() != args.build_number or scalars['build_fingerprint'].strip() != graph['build_fingerprint']:
        raise ValueError('Admitted frontend scalar identity differs')
    if (product_recipe is None or 'ro.product.build.' in product_recipe
            or any(path.endswith('/product.img') for path in plan['export_producers'])):
        raise ValueError('Product namespace absence requires a reviewed current graph policy')
    source_manifest_path = Path(__file__).resolve().parents[1] / 'config/cloud-source-manifest.json'
    if sha(source_manifest_path) != registry['source_manifest_sha256']:
        raise ValueError('Source revision declaration differs from the admitted capsules')
    manifest = read(source_manifest_path)
    suffixes = ('/boot.img', '/recovery.img', '/system.img', '/vendor.img', '/ramdisk.img',
                '/ramdisk-recovery.img', '/soong/build_number.txt', '/build_fingerprint.txt',
                '/buildinfo.prop', '/obj/KERNEL_OBJ')
    exports = {path: owner for path, owner in plan['export_producers'].items() if path.endswith(suffixes)}
    kernel = receipt['out_root'] + '/target/product/crux/obj/KERNEL_OBJ'
    if kernel not in exports:
        raise ValueError('Admitted plan does not export its paired kernel tree')
    for member in ('arch/arm64/boot/Image', 'vmlinux', 'System.map', '.config',
                   'arch/arm64/boot/dts/qcom/sm8150.dtb', 'arch/arm64/boot/dts/qcom/sm8150-v2.dtb',
                   'arch/arm64/boot/dts/qcom/sm8150p.dtb', 'arch/arm64/boot/dts/qcom/sm8150p-v2.dtb',
                   'arch/arm64/boot/dts/qcom/crux-sm8150-overlay.dtbo', 'include/config/kernel.release'):
        exports[kernel + '/' + member] = exports[kernel]
    buildinfo = {key: value.replace(receipt['out_root'], 'OUT') for key, value in buildinfo.items()}
    context = {'schema_version': 1, 'execution_id': execution, 'worker_commit': commit,
               'repository': repository, 'pack_run': args.pack_run, 'shard_run': args.shard_run,
               'source_root': receipt['source_root'], 'out_root': receipt['out_root'],
               **{key: registry[key] for key in ('build_number', 'graph_run', 'index_run', 'source_run',
                    'source_manifest_sha256', 'plan_sha256', 'graph_index')},
               'waves': waves, 'tasks': tasks, 'assembly_exports': exports,
               'host_jar_runtime_bundles': host_bundles,
               'rust_native_link_inputs': rust_inputs,
               'license_metadata_readers': license_readers,
               'frontend': {**graph, **scalars, 'graph_receipt': graph,
                   'identity_commands': commands, 'buildinfo_environment': buildinfo,
                   'branding_keys': branding,
                   'source_revisions': [{'path': p['path'], 'revision': p['revision']} for p in manifest['projects']],
                   'absent_partition_namespaces': {'product': {
                       'reason': 'The admitted current plan has no product image and its folded product property recipe omits the common ro.product.build namespace.',
                       'evidence': ['current plan export_producers', 'admitted system/product/etc/build.prop producer command',
                                    'build/make/core/sysprop.mk _skip_common_properties']}}},
               'source_selection': {'schema_version': 1, 'build_number': args.build_number,
                   'source_manifest_sha256': registry['source_manifest_sha256'],
                   'projects': [{'path': p['path'], 'revision': p['revision']} for p in manifest['projects']]}}
    runtime = read(Path(__file__).resolve().parents[1] / 'config/standard-build-runtime.json')
    if runtime.get('schema_version') != 1:
        raise ValueError('Unsupported admitted build runtime policy')
    context['kernel_source_alias'] = runtime['kernel_source_alias']
    write(output / 'context.json', context)
    print(json.dumps({'execution_id': execution, 'tasks': len(tasks), 'waves': list(map(len, waves))}))


def validate_receipt(context, receipt, task_id):
    if receipt.get('diagnostic_only'):
        raise ValueError('Diagnostic output is not a production build result')
    expected = producer_binding(context, task_id)
    if receipt.get('status') != 'success' or receipt.get('fresh_build') != fresh_binding(context):
        raise ValueError('Not a successful producer of this new execution: ' + task_id)
    for key, value in [('id', task_id), ('run_id', expected['run_id']),
                       ('worker_commit', expected['expected_worker_commit']),
                       ('manifest_sha256', expected['expected_manifest_sha256'])]:
        if str(receipt.get(key)) != str(value):
            raise ValueError('Producer binding mismatch: ' + key + ':' + task_id)
    declared = receipt.get('declared_dependencies')
    if declared != [producer_binding(context, dep) for dep in context['tasks'][task_id]['depends_on']]:
        raise ValueError('Producer imported an undeclared dependency: ' + task_id)
    if receipt.get('build_environment', {}).get('CCACHE_DISABLE') != '1':
        raise ValueError('Fresh compilation did not disable ccache')
    if sorted(receipt.get('additional_exports', [])) != sorted(context['tasks'][task_id].get('extra_exports', [])):
        raise ValueError('Producer extra export policy differs from the admitted context')
    if sorted(receipt.get('prebuild_targets', [])) != sorted(context['tasks'][task_id].get('local_runtime_targets', [])):
        raise ValueError('Producer local runtime ordering differs from the admitted context')


def summarize(args):
    context = read(args.context)
    root = Path(args.staging) / 'receipts'
    cloud_artifacts.download_artifacts(context['execution_id'], context['repository'], root,
                                      pattern='fresh-receipt-*')
    receipts = {p.parent.name.removeprefix('fresh-receipt-'): read(p)
                for p in root.glob('fresh-receipt-*/receipt.json')}
    if set(receipts) != set(context['tasks']):
        raise ValueError('Compilation receipts do not cover all admitted tasks')
    for task_id, receipt in receipts.items():
        validate_receipt(context, receipt, task_id)
    output = Path(args.output)
    producers = [producer_binding(context, task_id) for task_id in sorted(receipts)]
    write(output / 'producers.json', producers)
    write(output / 'build-summary.json', {'schema_version': 1, 'status': 'success',
          'execution_id': context['execution_id'], 'worker_commit': context['worker_commit'],
          'task_count': len(receipts), 'fresh_build': fresh_binding(context), 'producers': producers})
    print(json.dumps({'status': 'success', 'compiled_tasks': len(receipts)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    plan = sub.add_parser('bootstrap')
    for key in ('pack-run', 'shard-run', 'build-number', 'output', 'staging'):
        plan.add_argument('--' + key, required=True)
    matrix = sub.add_parser('matrix')
    matrix.add_argument('--context', required=True)
    matrix.add_argument('--wave', type=int, required=True)
    matrix.add_argument('--github-output', required=True)
    summary = sub.add_parser('summary')
    for key in ('context', 'output', 'staging'):
        summary.add_argument('--' + key, required=True)
    args = parser.parse_args()
    if args.mode == 'bootstrap':
        bootstrap(args)
    elif args.mode == 'summary':
        summarize(args)
    else:
        context = read(args.context)
        with open(args.github_output, 'a') as output:
            print('matrix=' + json.dumps({'id': context['waves'][args.wave]}, separators=(',', ':')), file=output)


if __name__ == '__main__':
    main()
