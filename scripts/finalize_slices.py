#!/usr/bin/env python3
"""Complete the cold transport contracts of a freshly sliced shard set.

`graph.py shard` exports the native Ninja closure but cannot know which sources
the release audit found to be read implicitly (license notices, R8 embedded
includes, packaging sources) or which generated child tools the terminal
target-files action needs before it can run. This step adds them without
touching one byte of the recorded native commands:

  * declared source leaves and their export lists,
  * order-only imports for every generated child tool of the terminal action,
  * the kernel runtime environment inherited from the frozen path contract,
  * `export_producers` in the plan for every exported path.

The 2026101104 slice set was produced by exactly these rules; the hosted
`prepare` run must reproduce each `manifest.json` digest to be accepted.

Inputs are the audit products of the release preparation:
`target-files-source-closure.json`, `host-runtime.json` (graph-export-audit)
and `integration-contract.json` (path interposer).
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph  # noqa: E402  (ninja_escape, statements, append_order_only_inputs)


def finalize(shards, source_closure, host_runtime, integration_contract, report, build_number):
    shards = Path(shards)
    plan = json.loads((shards / 'plan.json').read_text())
    source = json.loads(Path(source_closure).read_text())
    host = json.loads(Path(host_runtime).read_text())
    path_contract = json.loads(Path(integration_contract).read_text())
    out = path_contract['out_root']
    root = path_contract['source_root']
    if plan.get('source_root') != root:
        raise ValueError('Plan source root differs from the integration contract')
    jobs = {job['id']: job for wave in plan['waves'] for job in wave}
    manifests = {name: json.loads((shards / job['manifest']).read_text())
                 for name, job in jobs.items()}
    owners = {}
    for name, manifest in manifests.items():
        for path in manifest['outputs']:
            if path in owners:
                raise ValueError('Duplicate output owner: ' + path)
            owners[path] = name
    extra_sources = collections.defaultdict(set)
    for reader in source['notice_readers']:
        extra_sources[reader['outputs'][0]].update(reader['source_license_texts'])
    for include in source['r8_embedded_includes']:
        for consumer in include['r8_consumers']:
            extra_sources[consumer['outputs'][0]].add(include['included_file'])
    target_files = (out + '/target/product/crux/obj/PACKAGING/target_files_intermediates/'
                    'aosp_crux_release-target_files-' + build_number + '.zip')
    extra_sources[target_files].update(source['selected_packaging_source_paths'])
    implicit = [row for row in source['source_files'] if 'implicit_native_source_operand' in row['reasons']]
    host_map = {row['new_path']: row for row in host['records']}
    child_inputs = {row['output'] for row in source['generated_child_readiness_inputs']}
    for tool in list(child_inputs):
        if tool in host_map:
            child_inputs.update(host_map[tool]['transitive_generated_runtime_inputs'])
    # The release plan ends in the target-files package; the dev product's
    # four-image plan has no target-files action, so the terminal child-tool
    # contract applies only when that action is part of the slice set. The
    # recorded key stays present (null for the dev plan) so the release plan
    # bytes are unchanged.
    target_owner = owners.get(target_files)
    if target_owner is not None:
        target_job = jobs[target_owner]
        for path in child_inputs:
            if path not in owners or jobs[owners[path]]['wave'] >= target_job['wave']:
                raise ValueError('Child tool is not produced by an earlier wave: ' + path)
    changed = []
    for name, manifest in manifests.items():
        job = jobs[name]
        before_commands = list(manifest['commands'])
        leaf = set(manifest['leaf_inputs'])
        own = set(manifest['outputs'])
        export = own | set(job.get('export_outputs', []))
        # Kernel symbols and the raw Image reside under the native completed tree,
        # whose mkdir/config/Image producers are already an atomic task group.
        if out + '/target/product/crux/obj/KERNEL_OBJ' in own:
            export.add(out + '/target/product/crux/obj/KERNEL_OBJ')
        needed = set()
        path_consumer_outputs = {row['producer'] for row in path_contract['consumers']}
        has_kernel_runtime = bool(own & path_consumer_outputs)
        for edge in manifest['edges']:
            primary = edge['outputs'][0]
            leaf.update(extra_sources.get(primary, set()))
            for row in implicit:
                if row['path'] in edge['command'] or row['relative'] in edge['command']:
                    leaf.add(row['path'])
            if primary == target_files:
                needed.update(child_inputs)
        if has_kernel_runtime:
            leaf.update(path_contract['kernel_source_leaves_after_slicing'])
            # Native header manifests carry their own PATH; kernel make inherits
            # the same source-derived hermetic PATH from its frontend contract.
            manifest['build_environment'] = {**manifest.get('build_environment', {}),
                                             **path_contract['consumer_build_environment']}
            manifest['external_input_dirs'] = sorted(set(manifest.get('external_input_dirs', [])) |
                                                      set(path_contract['consumer_external_input_dirs']))
        extra_imports = sorted(needed - own - set(manifest['external_inputs']))
        if extra_imports:
            manifest['external_inputs'] = sorted(set(manifest['external_inputs']) | set(extra_imports))
            manifest['external_phony_inputs'] = sorted(set(manifest.get('external_phony_inputs', [])) |
                                                       set(extra_imports))
            directory = shards / name
            entry = directory / manifest['ninja']
            with entry.open('a') as stream:
                stream.write('\n' + '\n'.join('build ' + graph.ninja_escape(path) + ': phony'
                                              for path in extra_imports) + '\n')
            for file in directory.glob('*.ninja'):
                text = file.read_text()
                needle = 'build ' + graph.ninja_escape(target_files) + ':'
                if needle not in text:
                    continue
                raw = next(raw for _, _, raw in graph.statements(file) if raw.lstrip().startswith(needle))
                updated = graph.append_order_only_inputs(raw, extra_imports)
                if text.count(raw) != 1:
                    raise ValueError('Terminal action text is ambiguous in ' + str(file))
                file.write_text(text.replace(raw, updated))
            for owner in {owners[path] for path in extra_imports}:
                if jobs[owner]['wave'] >= job['wave']:
                    raise ValueError('Imported child tool is not produced by an earlier wave: ' + owner)
                job['depends_on'] = sorted(set(job['depends_on']) | {owner})
            job['external_inputs'] = list(manifest['external_inputs'])
        manifest['leaf_inputs'] = sorted(leaf)
        manifest['export_outputs'] = sorted(export)
        job['export_outputs'] = sorted(export)
        if manifest['commands'] != before_commands:
            raise ValueError('Finalization changed native command bytes: ' + name)
        (shards / job['manifest']).write_text(json.dumps(manifest, indent=2) + '\n')
        changed.append({'id': name, 'declared_outputs': len(own), 'exported_outputs': len(export),
                        'source_leaves': len(leaf), 'terminal_child_import_additions': extra_imports,
                        'kernel_runtime_source_contract': has_kernel_runtime})
    plan['export_producers'] = {path: name for name, job in jobs.items() for path in job['export_outputs']}
    plan['cold_transport_contract'] = {'full_declared_output_exports': True,
        'source_closure_audit': 'task/graph-export-audit/target-files-source-closure.json',
        'source_compiled_out_seeds': 0,
        'kernel_tree_export_owner': owners[out + '/target/product/crux/obj/KERNEL_OBJ'],
        'target_files_owner': target_owner,
        'native_command_bytes_changed_by_finalization': False}
    (shards / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    if report:
        Path(report).write_text(json.dumps({'schema_version': 1, 'status': 'prepared',
            'task_count': len(jobs), 'jobs': changed, 'target_files_owner': target_owner,
            'kernel_tree_owner': plan['cold_transport_contract']['kernel_tree_export_owner'],
            'target_files_child_tools_and_runtime_inputs': sorted(child_inputs),
            'all_new_child_provider_tasks_are_strictly_earlier': True}, indent=2) + '\n')
    return {'jobs': len(jobs), 'target_files_owner': target_owner,
            'kernel_tree_owner': plan['cold_transport_contract']['kernel_tree_export_owner'],
            'child_inputs': len(child_inputs)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shards', required=True, help='Directory holding plan.json and <id>/manifest.json')
    parser.add_argument('--source-closure', required=True)
    parser.add_argument('--host-runtime', required=True)
    parser.add_argument('--integration-contract', required=True)
    parser.add_argument('--build-number', required=True)
    parser.add_argument('--report')
    args = parser.parse_args()
    try:
        result = finalize(args.shards, args.source_closure, args.host_runtime,
                          args.integration_contract, args.report, args.build_number)
    except (OSError, KeyError, StopIteration, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
