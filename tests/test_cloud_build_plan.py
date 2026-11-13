import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import cloud_reuse
spec = importlib.util.spec_from_file_location('cloud_build_plan', SCRIPTS / 'cloud_build_plan.py')
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


class GraphAdmissionTests(unittest.TestCase):
    """The freeze's graph receipt is admitted through the index of the same preparation run."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'graph-receipt.json'
        self.receipt = {'schema_version': 1, 'status': 'success', 'build_number': '2026110950',
                        'build_fingerprint': 'Xiaomi/crux/crux:13/TQ3A.230901.001.B1/2026110950:userdebug/test-keys',
                        'source_manifest': {'path': '/m.json', 'sha256': 'source', 'project_count': 2,
                                            'projects': [{'path': 'a', 'revision': '1' * 40}]}}
        self.registry = {'graph_run': '41', 'source_manifest_sha256': 'source'}

    def write(self, receipt):
        self.path.write_text(json.dumps(receipt, indent=2) + '\n')
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        return {'status': 'success', 'graph_run': '41', 'build_number': '2026110950',
                'manifest_sha256': 'source', 'graph_receipt_sha256': digest}

    def test_summary_is_the_indexed_receipt_without_its_project_list(self):
        index = self.write(self.receipt)
        summary = plan.admit_graph(self.path, index, self.registry, '2026110950')
        self.assertEqual(summary['receipt_sha256'], index['graph_receipt_sha256'])
        self.assertEqual(summary['source_manifest'], {'path': '/m.json', 'sha256': 'source', 'project_count': 2})
        self.assertEqual(summary['build_fingerprint'], self.receipt['build_fingerprint'])

    def test_receipt_that_is_not_the_indexed_graph_is_rejected(self):
        index = self.write(self.receipt)
        self.write({**self.receipt, 'build_fingerprint': 'tampered'})
        with self.assertRaisesRegex(ValueError, 'canonical index'):
            plan.admit_graph(self.path, index, self.registry, '2026110950')

    def test_index_of_another_graph_build_or_manifest_is_rejected(self):
        index = self.write(self.receipt)
        for field, value in [('graph_run', '42'), ('build_number', '2026110951'),
                             ('manifest_sha256', 'other'), ('status', 'failed')]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'canonical index'):
                plan.admit_graph(self.path, {**index, field: value}, self.registry, '2026110950')

    def test_unsuccessful_or_foreign_graph_receipt_is_rejected(self):
        for field, value in [('status', 'failed'), ('build_number', '2026110951'),
                             ('source_manifest', {'sha256': 'other'})]:
            with self.subTest(field=field):
                index = self.write({**self.receipt, field: value})
                with self.assertRaisesRegex(ValueError, 'graph receipt'):
                    plan.admit_graph(self.path, index, self.registry, '2026110950')


class BuildPlanTests(unittest.TestCase):
    def inputs(self, counts=(13, 3, 7, 6, 5, 2, 2, 1, 1)):
        waves, capsules, inventory = [], [], {}
        prior = []
        for wave, count in enumerate(counts):
            rows = []
            for index in range(count):
                task = f'w{wave:03d}-s{index:03d}'
                rows.append({'id': task, 'depends_on': prior[:1]})
                capsules.append({'shard_id': task, 'archive': task + '.tar.zst',
                                 'archive_bytes': 10, 'archive_sha256': 'a' * 64})
                inventory[task] = {'manifest_sha256': 'b' * 64}
            waves.append(rows)
            prior += [row['id'] for row in rows]
        registry = {'status': 'success', 'build_number': 'new', 'graph_run': '1', 'index_run': '2',
                    'source_run': '1', 'source_manifest_sha256': 'source', 'plan_sha256': 'plan',
                    'graph_index': {'bytes': 1, 'sha256': 'index'}, 'shard_count': sum(counts),
                    'capsules': capsules}
        receipt = {k: registry[k] for k in ['status', 'build_number', 'graph_run', 'index_run', 'source_run']}
        receipt.update(manifest_sha256='source', pruned_database=registry['graph_index'])
        return registry, receipt, {'plan_sha256': 'plan', 'shards': inventory}, {'waves': waves}

    def test_admits_exact_inventory_and_prior_wave_dependencies(self):
        waves, tasks = plan.admit_inputs(*self.inputs(), 'plan')
        self.assertEqual(list(map(len, waves)), [13, 3, 7, 6, 5, 2, 2, 1, 1])
        self.assertEqual(len(tasks), 40)
        self.assertEqual(tasks['w001-s000']['depends_on'], ['w000-s000'])

    def test_admits_deferred_capsules_without_archive_bindings(self):
        args = self.inputs()
        for row in args[0]['capsules']:
            shard_id = row['shard_id']
            identity = 'a' * 64
            row.clear()
            row.update({'shard_id': shard_id, 'packing': 'deferred', 'input_identity': identity,
                        'reuse_descriptor': {'input_identity': identity, 'execution_digest': 'c' * 64}})
        waves, tasks = plan.admit_inputs(*args, 'plan')
        self.assertEqual(len(tasks), 40)
        self.assertEqual(tasks['w000-s000']['packing'], 'deferred')
        self.assertNotIn('archive_sha256', tasks['w000-s000'])

    def test_deferred_capsule_requires_matching_input_identity(self):
        args = self.inputs()
        row = args[0]['capsules'][0]
        row.update({'packing': 'deferred', 'input_identity': 'a' * 64,
                    'reuse_descriptor': {'input_identity': 'b' * 64}})
        with self.assertRaisesRegex(ValueError, 'invalid reuse identity'):
            plan.admit_inputs(*args, 'plan')

    def test_task_count_comes_from_the_preparation_registry(self):
        waves, tasks = plan.admit_inputs(*self.inputs((13, 3, 7, 6, 5, 3, 2, 1, 1)), 'plan')
        self.assertEqual(len(tasks), 41)
        args = self.inputs()
        args[0]['shard_count'] = 41
        with self.assertRaisesRegex(ValueError, 'complete nine-wave inventory'):
            plan.admit_inputs(*args, 'plan')

    def test_incomplete_final_wave_and_capsules_are_rejected(self):
        for counts in ((13, 3, 7, 6, 5, 2, 2, 1), (13, 3, 7, 6, 5, 2, 2, 1, 0)):
            with self.subTest(counts=counts), self.assertRaisesRegex(ValueError, 'complete nine-wave'):
                plan.admit_inputs(*self.inputs(counts), 'plan')
        args = self.inputs()
        args[0]['capsules'].pop()
        with self.assertRaisesRegex(ValueError, 'complete nine-wave'):
            plan.admit_inputs(*args, 'plan')

    def test_final_wave_cannot_depend_on_uncompleted_task(self):
        args = self.inputs()
        args[3]['waves'][8][0]['depends_on'] = ['w008-s000']
        with self.assertRaisesRegex(ValueError, 'same wave'):
            plan.admit_inputs(*args, 'plan')

    def test_identity_and_same_wave_dependency_are_rejected(self):
        args = self.inputs()
        args[1]['manifest_sha256'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'Source manifests'):
            plan.admit_inputs(*args, 'plan')
        args = self.inputs()
        args[3]['waves'][0][0]['depends_on'] = ['w000-s001']
        with self.assertRaisesRegex(ValueError, 'same wave'):
            plan.admit_inputs(*args, 'plan')

    def context_and_receipt(self):
        context = {'execution_id': '123', 'worker_commit': 'c' * 40, 'pack_run': '11', 'shard_run': '12',
                   'plan_sha256': 'plan', 'source_manifest_sha256': 'source',
                   'tasks': {'task': {'manifest_sha256': 'm', 'depends_on': []}}}
        receipt = {'status': 'success', 'fresh_build': plan.fresh_binding(context), 'id': 'task',
                   'run_id': '123', 'worker_commit': 'c' * 40, 'manifest_sha256': 'm',
                   'declared_dependencies': [], 'build_environment': {'CCACHE_DISABLE': '1'}}
        return context, receipt

    def test_fresh_producer_binding_and_cache_contract(self):
        context, receipt = self.context_and_receipt()
        plan.validate_receipt(context, receipt, 'task')
        for field, value in [('run_id', 'old'), ('worker_commit', 'd' * 40),
                             ('fresh_build', {}), ('declared_dependencies', [{'id': 'old'}]),
                             ('diagnostic_only', True),
                             ('build_environment', {'CCACHE_DISABLE': '0'})]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                plan.validate_receipt(context, {**receipt, field: value}, 'task')

    def test_reuse_receipt_keeps_current_binding_and_native_origin(self):
        context, receipt = self.context_and_receipt()
        context.update(repository='owner/repo', reuse_policy={
            'schema_version': 1, 'version': 1, 'force_cold': False, 'execution_digest': 'e' * 64})
        context['tasks']['task'].update(input_identity='a' * 64, reuse_descriptor={
            'input_identity': 'a' * 64, 'execution_digest': 'e' * 64})
        receipt['outputs'] = [{'path': '/out/file', 'type': 'file', 'mode': 0o644,
                               'size': 1, 'sha256': 'f' * 64}]
        native = {'schema_version': 1, 'id': 'task', 'run_id': '123',
                  'worker_commit': 'c' * 40, 'manifest_sha256': 'm', 'status': 'success',
                  'outputs': receipt['outputs'], 'build_environment': {'CCACHE_DISABLE': '1'}}
        runtime = {'CCACHE_DISABLE': '1'}
        basis = cloud_reuse.content_basis(context, 'task', runtime, {})
        receipt['reuse'] = cloud_reuse.envelope(context, 'task', basis, native)
        plan.validate_receipt(context, receipt, 'task')

        bad_origin = json.loads(json.dumps(receipt))
        bad_origin['reuse']['origin']['run_id'] = '999'
        with self.assertRaisesRegex(ValueError, 'Native origin identity differs'):
            plan.validate_receipt(context, bad_origin, 'task')

        missing = dict(receipt)
        missing.pop('reuse')
        with self.assertRaisesRegex(ValueError, 'Missing versioned native reuse contract'):
            plan.validate_receipt(context, missing, 'task')

    def test_summary_carries_complete_reuse_and_output_identity_maps(self):
        context, receipt = self.context_and_receipt()
        context.update(repository='owner/repo', reuse_policy={
            'schema_version': 1, 'version': 1, 'force_cold': False, 'execution_digest': 'e' * 64})
        context['tasks']['task'].update(input_identity='a' * 64, reuse_descriptor={
            'input_identity': 'a' * 64, 'execution_digest': 'e' * 64})
        receipt['outputs'] = [{'path': '/out/file', 'type': 'file', 'mode': 0o644,
                               'size': 1, 'sha256': 'f' * 64}]
        native = {'schema_version': 1, 'id': 'task', 'run_id': '123',
                  'worker_commit': 'c' * 40, 'manifest_sha256': 'm', 'status': 'success',
                  'outputs': receipt['outputs'], 'build_environment': {'CCACHE_DISABLE': '1'}}
        basis = cloud_reuse.content_basis(context, 'task', {'CCACHE_DISABLE': '1'}, {})
        receipt['reuse'] = cloud_reuse.envelope(context, 'task', basis, native)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context_path = root / 'context.json'
            context_path.write_text(json.dumps(context))
            staging, output = root / 'staging', root / 'output'

            def download(run_id, repository, destination, pattern=None):
                artifact = destination / 'fresh-receipt-task'
                artifact.mkdir(parents=True)
                (artifact / 'receipt.json').write_text(json.dumps(receipt))

            args = type('Args', (), {'context': context_path, 'staging': staging, 'output': output})()
            with mock.patch.object(plan.cloud_artifacts, 'download_artifacts', side_effect=download):
                plan.summarize(args)
            summary = json.loads((output / 'build-summary.json').read_text())
            self.assertEqual(summary['reuse'], {'task': receipt['reuse']})
            self.assertEqual(summary['output_identity'], {'task': receipt['outputs']})

    def test_buildinfo_static_environment_is_read_not_executed(self):
        command = '/bin/bash -c ' + __import__('shlex').quote(
            'BUILD_ID="TQ3" BUILD_VERSION_TAGS="release-keys" '
            'PRIVATE_BUILD_DESC="device $(cat /OUT/number) release-keys" bash buildinfo.sh > /OUT/props')
        result = plan.buildinfo_environment(command)
        self.assertEqual(result['BUILD_ID'], 'TQ3')
        self.assertIn('$(cat /OUT/number)', result['PRIVATE_BUILD_DESC'])

    def test_host_launchers_admit_only_declared_fresh_runtime_owners(self):
        tasks = {'host': {'wave': 0, 'depends_on': []}, 'reader': {'wave': 1, 'depends_on': []}}
        owners = {'/OUT/host/linux-x86/bin/d8': 'host', '/OUT/host/linux-x86/framework/d8.jar': 'host',
                  '/OUT/host/linux-x86/bin/manifest-merger': 'host',
                  '/OUT/host/linux-x86/framework/manifest-merger.jar': 'host'}
        commands = {'host': ['cp source /OUT/host/linux-x86/bin/d8'],
                    'reader': ['/bin/bash -c "/OUT/host/linux-x86/bin/d8 input.jar && '
                               '/OUT/host/linux-x86/bin/manifest-merger --help"']}
        bundles = plan.admit_host_jar_bundles(tasks, owners, commands, '/SOURCE', '/OUT')
        self.assertEqual(len(bundles), 2)
        self.assertEqual(tasks['reader']['depends_on'], ['host'])
        self.assertEqual(tasks['host']['extra_exports'], ['/OUT/host/linux-x86/framework/d8.jar',
                                                         '/OUT/host/linux-x86/framework/manifest-merger.jar'])
        self.assertNotIn('runtime_inputs', tasks['host'])

    def test_jar_runtime_does_not_create_an_undeclared_or_later_producer(self):
        tasks = {'host': {'wave': 1, 'depends_on': []}, 'reader': {'wave': 0, 'depends_on': []}}
        owners = {'/OUT/host/linux-x86/bin/tool': 'host'}
        commands = {'reader': ['/OUT/host/linux-x86/bin/tool']}
        self.assertEqual(plan.admit_host_jar_bundles(tasks, owners, commands, '/SOURCE', '/OUT'), [])
        owners['/OUT/host/linux-x86/framework/tool.jar'] = 'host'
        with self.assertRaisesRegex(ValueError, 'earlier admitted wave'):
            plan.admit_host_jar_bundles(tasks, owners, commands, '/SOURCE', '/OUT')
        owners = {'/OUT/host/linux-x86/bin/d8': 'host'}
        with self.assertRaisesRegex(ValueError, 'no admitted declared producer'):
            plan.admit_host_jar_bundles(tasks, owners, {'reader': ['/OUT/host/linux-x86/bin/d8']}, '/SOURCE', '/OUT')

    def test_owned_java_runtime_is_built_first_without_foreign_import(self):
        tasks = {'local': {'wave': 0, 'depends_on': []}}
        owners = {'/OUT/host/linux-x86/bin/xsdc': 'local', '/OUT/host/linux-x86/framework/xsdc.jar': 'local'}
        bundles = plan.admit_host_jar_bundles(tasks, owners,
                    {'local': ['/OUT/host/linux-x86/bin/xsdc input.xsd']}, '/SOURCE', '/OUT')
        self.assertEqual(tasks['local']['depends_on'], [])
        self.assertEqual(tasks['local']['local_runtime_targets'], ['/OUT/host/linux-x86/framework/xsdc.jar'])
        self.assertNotIn('runtime_inputs', tasks['local'])
        self.assertEqual(bundles[0]['transport'], 'local-prebuild')

    def test_rust_native_inputs_are_deduplicated_and_exported_from_declared_owner(self):
        tasks = {'native': {'wave': 0, 'depends_on': []},
                 'rust': {'wave': 1, 'depends_on': [],
                          'runtime_inputs': [{'path': '/OUT/apc.so', 'producer': 'native'}]}}
        owners = {'/OUT/apc.so': 'native', '/OUT/local.so': 'rust'}
        edges = {'rust': [{'command': 'rustc -C link-args="/OUT/apc.so /OUT/local.so" input.rs'},
                          {'command': 'rustc -Clink-arg=/OUT/apc.so input.rs'}]}
        relations = plan.admit_rust_native_inputs(tasks, owners, edges, '/SOURCE', '/OUT')
        self.assertEqual(relations, [{'consumer': 'rust', 'path': '/OUT/apc.so',
                                     'producer': 'native', 'edge_indices': [0, 1]}])
        self.assertEqual(tasks['native']['extra_exports'], ['/OUT/apc.so'])
        self.assertEqual(tasks['rust']['depends_on'], ['native'])
        self.assertEqual(tasks['rust']['runtime_inputs'], [{'path': '/OUT/apc.so', 'producer': 'native'}])
        self.assertNotIn('local_runtime_targets', tasks['rust'])

    def test_rust_native_inputs_reject_missing_or_not_ready_foreign_owners(self):
        edges = {'rust': [{'command': 'rustc -C link-args=/OUT/missing.so input.rs'}]}
        for owner_wave in (None, 1, 2):
            tasks = {'native': {'wave': owner_wave, 'depends_on': []},
                     'rust': {'wave': 1, 'depends_on': []}}
            owners = {} if owner_wave is None else {'/OUT/missing.so': 'native'}
            with self.subTest(owner_wave=owner_wave), self.assertRaises(ValueError):
                plan.admit_rust_native_inputs(tasks, owners, edges, '/SOURCE', '/OUT')

    def test_rust_native_inputs_reject_conflicting_existing_runtime_owner(self):
        tasks = {'native': {'wave': 0, 'depends_on': []},
                 'rust': {'wave': 1, 'depends_on': [],
                          'runtime_inputs': [{'path': '/OUT/apc.so', 'producer': 'wrong'}]}}
        with self.assertRaisesRegex(ValueError, 'Conflicting runtime input owner'):
            plan.admit_rust_native_inputs(tasks, {'/OUT/apc.so': 'native'},
                {'rust': [{'command': 'rustc -Clink-arg=/OUT/apc.so input.rs'}]}, '/SOURCE', '/OUT')

    def test_notice_closure_admits_foreign_metadata_local_readiness_and_source_texts(self):
        tasks = {'writer': {'wave': 0, 'depends_on': [], 'extra_exports': ['/OUT/native.so']},
                 'notice': {'wave': 1, 'depends_on': [],
                            'local_runtime_targets': ['/OUT/local.jar']}}
        edges = {
            'writer': [{'outputs': ['/OUT/child/meta_lic'],
                        'command': 'build_license_metadata -o /OUT/child/meta_lic -n /SOURCE/LICENSE'}],
            'notice': [{'outputs': ['/OUT/root.meta_lic'],
                        'command': 'build_license_metadata -o /OUT/root.meta_lic -d /OUT/child/meta_lic:static'},
                       {'outputs': ['/OUT/NOTICE.xml.gz'],
                        'command': 'xmlnotice -o /OUT/NOTICE.xml.gz /OUT/root.meta_lic'},
                       {'outputs': ['/OUT/NOTICE.html'],
                        'command': 'htmlnotice -o /OUT/NOTICE.html /OUT/root.meta_lic'}],
        }
        owners = {path: task for task, rows in edges.items() for edge in rows for path in edge['outputs']}
        readers = plan.admit_license_metadata_inputs(tasks, owners, edges, '/SOURCE', '/OUT')
        self.assertEqual(len(readers), 2)
        self.assertEqual(tasks['writer']['extra_exports'], ['/OUT/child/meta_lic', '/OUT/native.so'])
        self.assertEqual(tasks['notice']['runtime_inputs'], [{'path': '/OUT/child/meta_lic', 'producer': 'writer'}])
        self.assertEqual(tasks['notice']['depends_on'], ['writer'])
        self.assertEqual(tasks['notice']['local_runtime_targets'], ['/OUT/local.jar', '/OUT/root.meta_lic'])
        self.assertEqual(tasks['notice']['required_notice_sources'], ['/SOURCE/LICENSE'])

    def test_notice_metadata_requires_ready_foreign_owner(self):
        edges = {'writer': [{'outputs': ['/OUT/meta_lic'], 'command': 'build_license_metadata -o /OUT/meta_lic'}],
                 'notice': [{'outputs': ['/OUT/NOTICE'], 'command': 'xmlnotice -o /OUT/NOTICE /OUT/meta_lic'}]}
        for wave in (1, 2):
            tasks = {'writer': {'wave': wave, 'depends_on': []}, 'notice': {'wave': 1, 'depends_on': []}}
            with self.subTest(wave=wave), self.assertRaisesRegex(ValueError, 'earlier admitted wave'):
                plan.admit_license_metadata_inputs(tasks, {'/OUT/meta_lic': 'writer'}, edges, '/SOURCE', '/OUT')


if __name__ == '__main__':
    unittest.main()
