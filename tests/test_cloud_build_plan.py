import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('cloud_build_plan', SCRIPTS / 'cloud_build_plan.py')
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


class BuildPlanTests(unittest.TestCase):
    def test_pack_revision_requires_its_reviewed_registry_contents(self):
        original = {'capsules': [{'archive_sha256': 'original'}]}
        repaired = {'capsules': [{'archive_sha256': 'repaired'}]}
        with patch.dict(plan.PACK_REGISTRIES, {'1': 'original.json', '2': 'repaired.json'}, clear=True), \
                patch.object(plan, 'read', side_effect=lambda path: {'original.json': original,
                                                                    'repaired.json': repaired}[path]):
            plan.admit_pack_registry(original, '1')
            plan.admit_pack_registry(repaired, '2')
            with self.assertRaisesRegex(ValueError, 'differs'):
                plan.admit_pack_registry(original, '2')
            with self.assertRaisesRegex(ValueError, 'no reviewed'):
                plan.admit_pack_registry(repaired, '3')

    def inputs(self):
        waves, capsules, inventory = [], [], {}
        prior = []
        for wave, count in enumerate([13, 3, 7, 6, 5, 2, 2, 1]):
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
                    'graph_index': {'bytes': 1, 'sha256': 'index'}, 'shard_count': 39, 'capsules': capsules}
        receipt = {k: registry[k] for k in ['status', 'build_number', 'graph_run', 'index_run', 'source_run']}
        receipt.update(manifest_sha256='source', pruned_database=registry['graph_index'])
        return registry, receipt, {'plan_sha256': 'plan', 'shards': inventory}, {'waves': waves}

    def test_admits_exact_inventory_and_prior_wave_dependencies(self):
        waves, tasks = plan.admit_inputs(*self.inputs(), 'plan')
        self.assertEqual(list(map(len, waves)), [13, 3, 7, 6, 5, 2, 2, 1])
        self.assertEqual(len(tasks), 39)
        self.assertEqual(tasks['w001-s000']['depends_on'], ['w000-s000'])

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


if __name__ == '__main__':
    unittest.main()
