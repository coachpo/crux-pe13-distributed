import importlib.util
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('cloud_build_plan', SCRIPTS / 'cloud_build_plan.py')
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


class BuildPlanTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
