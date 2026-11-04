#!/usr/bin/env python3
"""Complete fresh graph transport dependencies and slice it on a hosted runner."""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph
import cloud_frontends
import cloud_index
from cloud_host_runtime import derive_host_runtime, derive_rust_native
from cloud_source_reads import derive_source_reads, iter_invocations

TARGETS = cloud_index.TARGETS


def replace_roots(value, out_root):
    if isinstance(value, str):
        return value.replace('${OUT}', str(out_root))
    if isinstance(value, list):
        return [replace_roots(item, out_root) for item in value]
    if isinstance(value, dict):
        return {key: replace_roots(item, out_root) for key, item in value.items()}
    return value


def is_scalar_touch(command, output, source_root):
    invocations = list(iter_invocations({'command': command}, str(source_root)))
    if len(invocations) != 1:
        return False
    argv, cwd = invocations[0]
    return (len(argv) == 2 and posixpath.basename(argv[0]) == 'touch'
            and posixpath.normpath(argv[1] if argv[1].startswith('/') else
                                   posixpath.join(cwd, argv[1])) == output)


def append_companion(database, source_root, companion, wrapper, epoch):
    """Append source actors without reparsing or changing the native graph files."""
    prior = graph.Graph(database)
    meta = dict(prior.meta)
    prior.db.close()
    stamp = int(epoch) * 1_000_000_000
    os.utime(companion, ns=(stamp, stamp))
    indexer = object.__new__(graph.Indexer)
    indexer.db = sqlite3.connect(database)
    indexer.db.execute('PRAGMA journal_mode=OFF')
    indexer.db.execute('PRAGMA synchronous=OFF')
    indexer.source_root = str(source_root)
    indexer.files = indexer.db.execute('SELECT MAX(id) FROM files').fetchone()[0]
    indexer.stmts = indexer.db.execute('SELECT MAX(id) FROM statements').fetchone()[0]
    indexer.scopes = indexer.db.execute('SELECT MAX(scope) FROM files').fetchone()[0]
    indexer.edges = meta['edge_count']
    indexer.duplicates = meta['duplicates']
    indexer.active = set()
    indexer.file_stats = dict(meta['file_stats'])
    try:
        companion_id = indexer.parse(companion)
        if indexer.duplicates != meta['duplicates']:
            raise ValueError('Source frontend actors introduce duplicate output producers')
        lines = ['subninja ' + graph.ninja_escape(meta['ninja']) + '\n',
                 'subninja ' + graph.ninja_escape(str(companion)) + '\n']
        wrapper.write_text(''.join(lines))
        os.utime(wrapper, ns=(stamp, stamp))
        wrapper_id = indexer.files + 1
        indexer.scopes += 1
        indexer.db.execute('INSERT INTO files VALUES (?,?,?,?)',
                          (wrapper_id, str(wrapper), indexer.scopes, None))
        offset = 0
        for ordinal, (line, child) in enumerate(zip(lines, [meta['entry_file'], companion_id])):
            indexer.stmts += 1
            indexer.db.execute('INSERT INTO statements VALUES (?,?,?,?,?,?,?,?)',
                              (indexer.stmts, wrapper_id, ordinal, 'subninja', line.split()[1],
                               offset, len(line.encode()), child))
            offset += len(line.encode())
        indexer.db.execute('UPDATE files SET parent=? WHERE id IN (?,?)',
                          (wrapper_id, meta['entry_file'], companion_id))
        details = wrapper.stat()
        indexer.file_stats[str(wrapper)] = {'size': details.st_size, 'mtime_ns': details.st_mtime_ns}
        updates = {'entry_file': wrapper_id, 'cold_wrapper_ninja': str(wrapper),
                   'edge_count': indexer.edges, 'duplicates': indexer.duplicates,
                   'file_stats': indexer.file_stats}
        for key, value in updates.items():
            indexer.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))
        indexer.db.commit()
    finally:
        indexer.db.close()


class Mutations:
    """Keep dependency data and exported Ninja edge bindings in the same state."""
    def __init__(self, database):
        self.database = Path(database)
        self.original = {}
        self.updated = {}
        self.producers = {}
        self.reasons = {}

    def raw(self, edge):
        if edge not in self.original:
            current = graph.Graph(self.database)
            handles = {}
            try:
                row = current.db.execute('SELECT * FROM statements WHERE id=?', (edge,)).fetchone()
                self.original[edge] = current.raw(row, handles)
                self.updated[edge] = self.original[edge]
                self.producers[edge] = json.loads(current.db.execute(
                    'SELECT outputs FROM edges WHERE id=?', (edge,)).fetchone()[0])[0]
            finally:
                for handle in handles.values():
                    handle.close()
                current.db.close()
        return self.updated[edge]

    def scalar(self, edge, desired):
        raw = self.raw(edge)
        if 'command' in graph.bindings(raw):
            raise ValueError('Scalar action already overrides command: ' + desired['output'])
        self.updated[edge] = raw.rstrip() + '\n  command = ' + desired['desired_command'].replace('$', '$$') + '\n'
        self.reasons.setdefault(edge, set()).add('fresh frontend scalar rematerialization')

    def dependencies(self, additions, reason):
        changed = 0
        connection = sqlite3.connect(self.database)
        try:
            for edge, paths in sorted(additions.items()):
                edge = int(edge)
                row = connection.execute('SELECT outputs,deps FROM edges WHERE id=?', (edge,)).fetchone()
                if row is None:
                    raise ValueError('Unknown dependency consumer edge: ' + str(edge))
                outputs, deps = json.loads(row[0]), json.loads(row[1])
                existing = {path for path, _ in deps} | set(outputs)
                extra = sorted(set(paths) - existing)
                if not extra:
                    continue
                self.updated[edge] = graph.append_order_only_inputs(self.raw(edge), extra)
                deps.extend((path, 'order') for path in extra)
                connection.execute('UPDATE edges SET deps=? WHERE id=?', (json.dumps(deps), edge))
                self.reasons.setdefault(edge, set()).add(reason)
                changed += len(extra)
            connection.commit()
        finally:
            connection.close()
        return changed

    def publish(self):
        profile = {'schema_version': 1, 'command_adaptations': [
            {'producer': self.producers[edge], 'scope': 'edge',
             'replace': self.original[edge], 'with': self.updated[edge], 'expected_matches': 1,
             'rationale': '; '.join(sorted(self.reasons[edge])),
             'evidence': ['fresh hosted canonical graph and source-derived runtime dependency audit']}
            for edge in sorted(self.updated)]}
        graph.adapt_graph(self.database, profile, replace_profile=True)
        return profile


def frontend_dependencies(records, native, descriptor):
    scalars = [row['output'] for row in descriptor['scalars']]
    bpglob = descriptor['bpglob']['output']
    path_outputs = descriptor['path_interposer']['outputs']
    path_prefix = str(Path(path_outputs[0]).parent / '.path')
    actor_outputs = set(descriptor['outputs'])
    additions = {}
    path_consumers = []
    for edge, entry in native.items():
        own = set(records[edge]['outputs'])
        if own & actor_outputs:
            continue
        command = entry.get('command', '') + '\n' + entry.get('rspfile_content', '')
        required = {path for path in [*scalars, bpglob] if path in command and path not in own}
        if path_prefix in command:
            required.update(set(path_outputs) - own)
            path_consumers.append(edge)
        if required:
            additions[edge] = sorted(required)
    return additions, path_consumers


def cold_probe(source_root, frontend_out, directory):
    output = directory / 'out'
    descriptor, companion = cloud_frontends.generate(
        source_root, frontend_out, output, directory / 'graph', [])
    environment = {key: value for key, value in os.environ.items()
                   if key != 'GH_TOKEN' and not key.startswith(('GITHUB_', 'ACTIONS_'))}
    (output / '.cloud-frontends/ninja').mkdir(parents=True, exist_ok=True)
    command = [str(source_root / 'prebuilts/build-tools/linux-x86/bin/ninja'),
               '-f', str(companion), '-j4', *descriptor['outputs']]
    with (directory / 'cold-probe.log').open('w') as log:
        subprocess.run(command, cwd=source_root, env=environment, stdout=log,
                       stderr=subprocess.STDOUT, check=True)
    for row in descriptor['scalars']:
        if (output / Path(row['output']).relative_to(output)).read_bytes() != base64.b64decode(row['contents_base64']):
            raise ValueError('Cold source actor scalar differs: ' + row['name'])
    expr = subprocess.run([str(output / '.path/expr'), '1', '+', '2'],
                          capture_output=True, text=True, env=environment, check=True)
    tr = subprocess.run([str(output / '.path/tr'), 'a-z', 'A-Z'], input='crux\n',
                        capture_output=True, text=True, env=environment, check=True)
    if expr.stdout.strip() != '3' or tr.stdout != 'CRUX\n':
        raise ValueError('Cold source path_interposer functional probe failed')
    help_run = subprocess.run([str(output / 'soong/bpglob'), '-h'],
                              capture_output=True, text=True, env=environment)
    if help_run.returncode not in (0, 2) or 'Usage' not in help_run.stderr + help_run.stdout:
        raise ValueError('Cold source bpglob startup probe failed')
    return {'status': 'success', 'actions': len(descriptor['actions']),
            'expr': expr.stdout.strip(), 'tr': tr.stdout.strip(), 'bpglob_help_exit': help_run.returncode}


def scalar_probe(database, source_root, overrides, directory):
    results = []
    directory.mkdir(parents=True, exist_ok=True)
    for position, desired in enumerate(overrides):
        current = graph.Graph(database)
        closure = current.closure([desired['output']])
        records, _ = current.edge_records(closure['edge_ids'])
        if sum(row['rule'] != 'phony' for row in records.values()) != 1:
            current.db.close()
            raise ValueError('Scalar probe would execute more than its single writer')
        path = Path(desired['output'])
        details, contents = path.stat(), path.read_bytes()
        probe = directory / str(position)
        current.slice([str(path)], probe, runtime_dir=str(probe))
        current.db.close()
        try:
            path.unlink()
            with (directory / f'{position}.log').open('w') as log:
                subprocess.run([str(source_root / 'prebuilts/build-tools/linux-x86/bin/ninja'),
                                '-f', str(probe / 'build.ninja'), '-j1', str(path)],
                               cwd=source_root, stdout=log, stderr=subprocess.STDOUT, check=True)
            if path.read_bytes() != base64.b64decode(desired['contents_base64']):
                raise ValueError('Native scalar action did not rematerialize its exact frontend bytes')
            if stat.S_IMODE(path.stat().st_mode) != desired['mode']:
                raise ValueError('Native scalar action did not rematerialize its frontend mode')
            results.append({'output': str(path), 'status': 'success'})
        finally:
            path.write_bytes(contents)
            path.chmod(stat.S_IMODE(details.st_mode))
            os.utime(path, ns=(details.st_atime_ns, details.st_mtime_ns))
    return results


def integrate(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    receipt_path = output / 'shard-receipt.json'
    database = output / 'index-derived.sqlite'
    if receipt_path.exists() or database.exists():
        raise ValueError('Derived index/receipt already exists')
    receipt = {'schema_version': 1, 'status': 'running', 'graph_run': args.graph_run,
               'index_run': args.index_run, 'build_number': args.build_number}
    start = time.monotonic()
    try:
        manifest_path = Path(args.source_manifest)
        out_root, source_string, manifest_sha = cloud_index.read_graph_inputs(
            Path(args.graph_evidence), manifest_path, args.build_number)
        source_root = Path(source_string)
        index_input = Path(args.index_input)
        index_receipt = json.loads((index_input / 'index-receipt.json').read_text())
        native_database = Path(args.database)
        if (index_receipt.get('status') != 'success' or index_receipt.get('graph_run') != args.graph_run
                or index_receipt.get('build_number') != args.build_number
                or index_receipt.get('manifest_sha256') != manifest_sha
                or index_receipt.get('parser_sha256') != cloud_index.sha256(cloud_index.GRAPH_PARSER)
                or native_database.stat().st_size != index_receipt['database_bytes']
                or cloud_index.sha256(native_database) != index_receipt['database_sha256']):
            raise ValueError('Canonical index input identity differs from the accepted receipt')
        graph_receipt = json.loads((Path(args.graph_evidence) / 'graph/graph-receipt.json').read_text())
        shutil.copyfile(native_database, database)
        profile = replace_roots(json.loads(Path(args.ownership).read_text()), out_root)
        graph.augment_graph(database, profile)
        current = graph.Graph(database)
        desired = [str(out_root / item) for item in (
            'soong/bpglob', *cloud_frontends.PATH_INTERPOSER_RELATIVE_OUTPUTS,
            *(relative for _, relative in cloud_frontends.SCALAR_OUTPUTS))]
        existing = current.producers(desired, directory_fallback=False)
        current.db.close()
        descriptor, companion = cloud_frontends.generate(
            source_root, out_root, out_root, out_root / '.cloud-preparation/frontends', existing)
        wrapper = out_root / '.cloud-preparation/combined.ninja'
        append_companion(database, source_root, companion, wrapper, graph_receipt['build_datetime'])
        profile['side_output_dirs'].append({
            'producer': str(out_root / '.path_interposer_origpath'), 'dirs': [str(out_root / '.path')],
            'rationale': 'The source metadata actor creates the admitted expr/tr alias directory.',
            'evidence': ['build/soong/ui/build/path.go', 'source-built frontend metadata initializer']})
        graph.augment_graph(database, profile, replace_profile=True)
        mutations = Mutations(database)
        current = graph.Graph(database)
        for desired in descriptor['native_scalar_overrides']:
            owner = current.producers([desired['output']], directory_fallback=False)[desired['output']]
            records, _ = current.edge_records([owner])
            metadata = current.native_metadata(records)
            if (len(records[owner]['outputs']) != 1
                    or not is_scalar_touch(metadata[owner]['command'], desired['output'], source_root)):
                raise ValueError('Unexpected native frontend scalar producer: ' + metadata[owner]['command'])
            mutations.scalar(owner, desired)
        current.db.close()
        mutations.publish()
        receipt['scalar_probe'] = scalar_probe(database, source_root, descriptor['native_scalar_overrides'],
                                                output / 'scalar-probe')
        runtime_profile = json.loads(Path(args.runtime_profile).read_text())
        current = graph.Graph(database)
        path_additions = {}
        path_owners = []
        for consumer in runtime_profile['path_consumers']:
            producer = replace_roots(consumer['output'], out_root)
            owner = current.producers([producer], directory_fallback=False).get(producer)
            if owner is None:
                raise ValueError('Reviewed platform PATH consumer has no fresh producer: ' + producer)
            path_additions[owner] = descriptor['path_interposer']['outputs']
            path_owners.append({'output': producer, 'edge': owner, 'kind': consumer['kind']})
        current.db.close()
        mutations.dependencies(path_additions, 'reviewed source-derived platform PATH execution context')
        mutations.publish()
        receipt['platform_path_consumers'] = path_owners
        iterations = []
        while True:
            current = graph.Graph(database)
            closure = current.closure(TARGETS)
            records, _ = current.edge_records(closure['edge_ids'])
            native = current.native_metadata(records)
            frontend, _ = frontend_dependencies(records, native, descriptor)
            source_reads = derive_source_reads(current, records, native, source_root, out_root)
            host_reads = derive_host_runtime(current, records, native, source_root, out_root)
            rust_reads = derive_rust_native(current, records, native, source_root, out_root)
            current.db.close()
            changes = 0
            for changeset, reason in (
                (frontend, 'source-built frontend runtime readiness'),
                (source_reads['additions'], 'source and license-reader runtime inputs'),
                (host_reads['additions'], 'generated host-tool shared-library closure'),
                (rust_reads['additions'], 'generated Rust native archive imports')):
                changes += mutations.dependencies(changeset, reason)
            iterations.append({'selected_edges': closure['edge_count'], 'new_dependencies': changes})
            print(json.dumps({'phase': 'dependency-closure', 'iteration': len(iterations), **iterations[-1]}), flush=True)
            if not changes:
                break
            mutations.publish()
        (output / 'source-reads.json').write_text(json.dumps(source_reads, indent=2) + '\n')
        (output / 'host-runtime.json').write_text(json.dumps(host_reads, indent=2) + '\n')
        (output / 'rust-native.json').write_text(json.dumps(rust_reads, indent=2) + '\n')
        (output / 'runtime-adaptations.json').write_text(json.dumps(mutations.publish(), indent=2) + '\n')
        receipt['cold_frontend_probe'] = cold_probe(source_root, out_root, output / 'cold-frontend-probe')
        current = graph.Graph(database)
        final_closure = current.closure(TARGETS)
        (output / 'closure.json').write_text(json.dumps(final_closure, indent=2) + '\n')
        current.db.close()
        prune_spec = importlib.util.spec_from_file_location('cloud_prune', Path(__file__).with_name('prune-index.py'))
        prune = importlib.util.module_from_spec(prune_spec)
        prune_spec.loader.exec_module(prune)
        pruned = output / 'index-pruned.sqlite'
        prune.prune(database, output / 'closure.json', pruned, output / 'prune-record.json', TARGETS)
        prune_record = json.loads((output / 'prune-record.json').read_text())
        current = graph.Graph(pruned)
        shards = output / 'shards'
        plan = current.shard(TARGETS, shards, args.max_actions, args.max_parallel, out_root=out_root)
        file_stats = dict(current.meta['file_stats'])
        current.db.close()
        # The same source-derived platform PATH is part of every cold task's
        # execution context; the worker retains its admitted OS fallback.
        path_directory = source_root / 'prebuilts/build-tools/path/linux-x86'
        for path in sorted(shards.glob('*/manifest.json')):
            manifest = json.loads(path.read_text())
            manifest['leaf_inputs'] = sorted(set(manifest['leaf_inputs']) | {str(path_directory)})
            manifest['build_environment'] = {
                **manifest.get('build_environment', {}), 'OUT_DIR': str(out_root),
                'ANDROID_BUILD_TOP': str(source_root), 'PATH': str(path_directory) + ':' + str(out_root / '.path'),
                **runtime_profile.get('build_environment', {})}
            if any(item['output'] in manifest['outputs'] for item in path_owners):
                kernel_sources = [str(source_root / value) for value in runtime_profile['kernel_source_leaves']]
                missing = [value for value in kernel_sources if not Path(value).exists()]
                if missing:
                    raise ValueError('Missing source-derived kernel platform input: ' + ', '.join(missing))
                manifest['leaf_inputs'] = sorted(set(manifest['leaf_inputs']) | set(kernel_sources))
                if str(out_root / '.path') not in manifest['outputs']:
                    manifest['external_input_dirs'] = sorted(set(manifest.get('external_input_dirs', [])) |
                                                              {str(out_root / '.path')})
            path.write_text(json.dumps(manifest, indent=2) + '\n')
        slice_inventory = {'schema_version': 1, 'plan_sha256': cloud_index.sha256(shards / 'plan.json'),
                           'shards': {path.parent.name: {
                               'manifest_sha256': cloud_index.sha256(path),
                               'ninja_files': {ninja.name: cloud_index.sha256(ninja)
                                               for ninja in sorted(path.parent.glob('*.ninja'))}}
                               for path in sorted(shards.glob('*/manifest.json'))}}
        (output / 'slice-inventory.json').write_text(json.dumps(slice_inventory, indent=2, sort_keys=True) + '\n')
        graph_inventory = []
        original_hashes = {row['path']: row['sha256'] for row in graph_receipt['graph_files']}
        for name, details in sorted(file_stats.items()):
            path = Path(name)
            if path.stat().st_size != details['size'] or path.stat().st_mtime_ns != details['mtime_ns']:
                raise ValueError('Indexed graph changed during slicing: ' + name)
            graph_inventory.append({'path': name, **details,
                                    'sha256': original_hashes.get(name) or cloud_index.sha256(path)})
        (output / 'graph-inventory.json').write_text(json.dumps(graph_inventory, indent=2) + '\n')
        (output / 'graph-files.txt').write_text(''.join(row['path'].lstrip('/') + '\n' for row in graph_inventory))
        receipt.update({'status': 'success', 'iterations': iterations, 'source_root': str(source_root),
                        'out_root': str(out_root), 'manifest_sha256': manifest_sha,
                        'derived_database_bytes': database.stat().st_size,
                        'derived_database_sha256': prune_record['source']['sha256'],
                        'pruned_database': prune_record['output'],
                        'plan': {key: value for key, value in plan.items()
                                 if isinstance(value, (str, int, float, bool))},
                        'elapsed_seconds': round(time.monotonic() - start, 3)})
        return receipt
    except Exception as error:
        receipt.update({'status': 'failed', 'error': str(error),
                        'elapsed_seconds': round(time.monotonic() - start, 3)})
        raise
    finally:
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('graph-evidence', 'source-manifest', 'graph-run', 'index-input', 'index-run',
                 'database', 'build-number', 'output', 'ownership'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--max-actions', type=int, default=4000)
    parser.add_argument('--max-parallel', type=int, default=20)
    parser.add_argument('--runtime-profile', default=str(Path(__file__).resolve().parents[1] /
                                                       'config/cloud-runtime-profile.json'))
    args = parser.parse_args()
    receipt = integrate(args)
    print(json.dumps(receipt, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
