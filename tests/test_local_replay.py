"""The local diagnostic boundary must not become a second publication path."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('local_cold_replay', ROOT / 'local_replay/replay.py')
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)
sys.path.insert(0, str(ROOT / 'local_replay'))
container_spec = importlib.util.spec_from_file_location('local_replay_container', ROOT / 'local_replay/container.py')
container = importlib.util.module_from_spec(container_spec)
container_spec.loader.exec_module(container)
assembly_spec = importlib.util.spec_from_file_location('local_replay_assembly', ROOT / 'local_replay/assembly.py')
assembly = importlib.util.module_from_spec(assembly_spec)
assembly_spec.loader.exec_module(assembly)


class LocalReplayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.task_id = 'w000-s000'
        self.descriptor = {'input_identity': '2' * 64}
        self.task = {'id': self.task_id, 'wave': 0, 'category': 'identity', 'depends_on': [],
                     'packing': 'deferred', 'manifest_sha256': '1' * 64,
                     'input_identity': '2' * 64, 'reuse_descriptor': self.descriptor}
        self.context = {'schema_version': 1, 'worker_commit': 'a' * 40, 'execution_id': '1234',
                        'build_number': '2026111750', 'source_root': '/home/runner/local-fixture-source',
                        'out_root': '/home/runner/local-fixture-out', 'source_manifest_sha256': '3' * 64,
                        'plan_sha256': '4' * 64, 'schedule_policy': replay.cloud_schedule.load_policy(),
                        'waves': [[self.task_id]], 'tasks': {self.task_id: self.task},
                        'reuse_policy': {'execution_digest': replay.cloud_reuse.execution_digest(ROOT)},
                        'source_selection': {'schema_version': 1, 'build_number': '2026111750',
                                             'source_manifest_sha256': '3' * 64,
                                             'projects': [{'path': 'device/xiaomi/crux', 'revision': 'b' * 40}]}}
        self.context_path = self.root / 'context.json'
        self.context_path.write_text(json.dumps(self.context))
        self.capsule = self.root / 'capsule'
        self.capsule.mkdir()
        archive = self.capsule / 'task.tar.zst'
        archive.write_bytes(b'unit test bytes; native verification remains mandatory')
        (self.capsule / (self.task_id + '.json')).write_text('{}')
        self.packed = {'status': 'success', 'shard_id': self.task_id, 'execution_id': '1234',
                       'reuse_descriptor': self.descriptor, 'input_identity': '2' * 64,
                       'slice_manifest_sha256': '1' * 64, 'source_manifest_sha256': '3' * 64,
                       'plan_sha256': '4' * 64, 'archive': archive.name,
                       'archive_bytes': archive.stat().st_size, 'archive_sha256': replay.worker.digest(archive)}
        self.write_pack()

    def write_pack(self):
        (self.capsule / 'pack-receipt.json').write_text(json.dumps(self.packed))

    def prepare(self, dependencies=()):
        return replay.prepare(self.context_path, self.task_id, self.capsule, dependencies)

    def test_preparation_binds_archive_audit_and_code_without_restoring_source(self):
        record = self.prepare()
        self.assertTrue(record['diagnostic_only'])
        self.assertEqual(record['execution_digest'], self.context['reuse_policy']['execution_digest'])
        self.assertEqual(record['context']['sha256'], replay.worker.digest(self.context_path))
        self.assertEqual(record['capsule']['sha256'], self.packed['archive_sha256'])
        self.assertFalse(Path(self.context['source_root']).exists())

    def test_changed_archive_and_wrong_packing_lineage_are_rejected(self):
        (self.capsule / self.packed['archive']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'archive differs'):
            self.prepare()
        self.packed['execution_id'] = '9999'
        self.write_pack()
        with self.assertRaisesRegex(ValueError, 'packing receipt differs'):
            self.prepare()

    def test_missing_or_undeclared_predecessors_fail_closed(self):
        self.context['tasks'][self.task_id]['depends_on'] = ['prior']
        self.context['tasks'][self.task_id]['wave'] = 1
        self.context['tasks']['prior'] = {'id': 'prior', 'wave': 0, 'depends_on': [], 'category': 'other'}
        self.context['waves'] = [['prior'], [self.task_id]]
        self.context_path.write_text(json.dumps(self.context))
        with self.assertRaisesRegex(ValueError, 'Every declared predecessor'):
            self.prepare()
        with self.assertRaisesRegex(ValueError, 'Undeclared'):
            self.prepare([('unrelated', 'actions', self.root)])

    def test_native_mounts_only_selected_inputs_and_no_source_out_or_credentials(self):
        record = self.prepare()
        record_path = self.root / 'input.json'
        record_path.write_text(json.dumps(record))
        destination = self.root / 'result'
        destination.mkdir()
        command = replay.docker_command(record_path, record, destination)
        self.assertEqual(command[:4], ['docker', '--context', 'orbstack', 'run'])
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertEqual(command[command.index('--platform') + 1], 'linux/amd64')
        self.assertNotIn('--privileged', command)
        self.assertNotIn('--env', command)
        mounts = [command[index + 1] for index, value in enumerate(command) if value == '--mount']
        self.assertEqual(sum(not value.endswith(',readonly') for value in mounts), 1)
        self.assertFalse(any('dst=' + self.context[key] in value
                             for value in mounts for key in ('source_root', 'out_root')))
        self.assertFalse(any('src=' + str(ROOT) + ',' in value for value in mounts))

    def test_local_predecessor_cannot_claim_actions_identity_or_other_context(self):
        receipt = {'id': self.task_id, 'status': 'success', 'diagnostic_only': True,
                   'source_root': self.context['source_root'], 'out_root': self.context['out_root'],
                   'manifest_sha256': self.task['manifest_sha256'],
                   'build_environment': {'CCACHE_DISABLE': '1'},
                   'local_replay': {'schema': replay.SCHEMA, 'context_sha256': 'c' * 64}}
        directory = self.root / 'local'
        directory.mkdir()
        path = directory / 'receipt.json'
        path.write_text(json.dumps(receipt))
        self.assertEqual(replay.admit_dependency(self.context, self.task_id, directory, 'local', 'c' * 64), receipt)
        with self.assertRaisesRegex(ValueError, 'different reference'):
            replay.admit_dependency(self.context, self.task_id, directory, 'local', 'd' * 64)
        receipt['run_id'] = '1234'
        path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'impersonate'):
            replay.admit_dependency(self.context, self.task_id, directory, 'local', 'c' * 64)
        with self.assertRaisesRegex(ValueError, 'Diagnostic output'):
            replay.cloud_build_plan.validate_receipt(self.context, receipt, self.task_id)

    def test_reference_roots_cannot_replace_transport_or_tooling_mounts(self):
        for key in ('source_root', 'out_root'):
            context = copy.deepcopy(self.context)
            context[key] = '/opt/crux-code/masked'
            with self.assertRaisesRegex(ValueError, 'transport mount'):
                replay.validate_reference(context, self.task_id)

    def test_kernel_cleanup_failure_retains_failed_explicitly_local_receipt(self):
        output = self.root / 'native'
        output.mkdir()
        native = {'id': self.task_id, 'status': 'success'}
        local_info = {'schema': replay.SCHEMA, 'publication_admission': False}
        with patch.object(container.worker, 'run_shard', return_value=native), \
                patch.object(container.cloud_compile, '_unmount_short_source_alias', side_effect=RuntimeError('umount failed')):
            with self.assertRaisesRegex(RuntimeError, 'umount failed'):
                container.run_native('manifest', 'bundle', self.task, [], local_info, {}, True, output)
        receipt = json.loads((output / 'receipt.json').read_text())
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['native_status'], 'success')
        self.assertTrue(receipt['diagnostic_only'])
        self.assertFalse(receipt['local_replay']['publication_admission'])

    def test_local_preparation_needs_no_fabricated_actions_identity(self):
        self.context['diagnostic_only'] = True
        self.context['local_preparation'] = {'schema': replay.SCHEMA, 'tool_commit': self.context.pop('worker_commit')}
        self.context.pop('execution_id')
        self.context_path.write_text(json.dumps(self.context))
        for key in ('execution_id', 'reuse_descriptor', 'input_identity'):
            self.packed.pop(key)
        self.write_pack()
        self.assertTrue(self.prepare()['diagnostic_only'])
        self.context['execution_id'] = 'fake'
        with self.assertRaisesRegex(ValueError, 'invent an Actions identity'):
            replay.validate_reference(self.context, self.task_id)

    def test_credentials_and_actions_identity_are_not_a_native_environment(self):
        container.reject_actions_environment({'PATH': '/usr/bin', 'CCACHE_DISABLE': '1'})
        for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'GITHUB_RUN_ID', 'ACTIONS_RUNTIME_TOKEN', 'CRUX_INSTALL_SIGNING_KEY'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'identities or credentials'):
                container.reject_actions_environment({name: 'fixture-only'})

    def test_changed_dependency_or_runtime_provider_cannot_use_actions_reference(self):
        context = copy.deepcopy(self.context)
        context['diagnostic_only'] = True
        context['local_preparation'] = {'schema': replay.SCHEMA, 'tool_commit': context.pop('worker_commit')}
        context.pop('execution_id')
        directory = self.root / 'origin'
        directory.mkdir()
        (directory / 'receipt.json').write_text('{}')
        for field, value in (('depends_on', ['changed-producer']),
                             ('runtime_inputs', [{'path': '/generated', 'producer': 'changed-producer'}]),
                             ('required_notice_sources', ['/notice-source'])):
            candidate = copy.deepcopy(context)
            candidate['tasks'][self.task_id][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'source/task contract'):
                replay.admit_dependency(candidate, self.task_id, directory, 'actions', 'c' * 64, self.context)

    def test_dependency_root_selects_only_declared_ids_with_explicit_local_overrides(self):
        self.context['tasks'][self.task_id]['depends_on'] = ['one', 'two']
        local = ('two', 'local', self.root / 'local-result/native')
        actual = replay.dependency_arguments(self.context, self.task_id, self.root, [local])
        self.assertEqual(actual, [('one', 'actions', self.root / 'shard-one'), local])
        with self.assertRaisesRegex(ValueError, 'unique declared'):
            replay.dependency_arguments(self.context, self.task_id, self.root, [local, local])

    def test_native_code_snapshot_does_not_follow_later_checkout_edits(self):
        source, destination = self.root / 'checkout', self.root / 'snapshot'
        for name in ('scripts', 'config', '.github/workflows', '.github/actions', 'local_replay'):
            (source / name).mkdir(parents=True)
        for name in replay.LOCAL_CODE_FILES:
            (source / 'local_replay' / name).write_text('before')
        (source / 'scripts/worker.py').write_text('frozen worker')
        identity = replay.snapshot_code(destination, source)
        (source / 'scripts/worker.py').write_text('changed worker')
        (source / 'local_replay/replay.py').write_text('changed entry')
        self.assertEqual((destination / 'scripts/worker.py').read_text(), 'frozen worker')
        self.assertEqual((destination / 'local_replay/replay.py').read_text(), 'before')
        self.assertEqual(identity['local_runner']['replay.py'], replay.worker.digest(destination / 'local_replay/replay.py'))

    def test_assembly_mounts_no_capsule_source_or_kernel_mount_capability(self):
        record = {'operation': 'assembly', 'context': replay.file_record(self.context_path),
                  'source_manifest': replay.file_record(self.context_path), 'dependencies': []}
        destination = self.root / 'assembly-result'
        destination.mkdir()
        record_path = self.root / 'assembly-input.json'
        record_path.write_text(json.dumps(record))
        command = replay.docker_command(record_path, record, destination)
        mounts = [command[index + 1] for index, value in enumerate(command) if value == '--mount']
        self.assertEqual(command[-1], '/opt/crux-code/local_replay/assembly.py')
        self.assertNotIn('--cap-add', command)
        self.assertFalse(any('dst=/inputs/capsule' in value for value in mounts))
        self.assertTrue(any('dst=/opt/crux-code/installer,readonly' in value for value in mounts))
        self.assertEqual(sum(not value.endswith(',readonly') for value in mounts), 1)

    def test_local_assembly_source_pins_are_checked_without_actions_stage_ids(self):
        context = copy.deepcopy(self.context)
        context['diagnostic_only'] = True
        context['local_preparation'] = {'schema': replay.SCHEMA, 'tool_commit': context.pop('worker_commit')}
        context.pop('execution_id')
        projects = context['source_selection']['projects']
        manifest = self.root / 'source-manifest.json'
        manifest.write_text(json.dumps({'source_root': context['source_root'], 'projects': projects}))
        context['source_manifest_sha256'] = replay.worker.digest(manifest)
        context['source_selection']['source_manifest_sha256'] = context['source_manifest_sha256']
        context['frontend'] = {'source_revisions': projects, 'graph_receipt': {'build_datetime': 0}}
        selection = assembly.source_selection(context, manifest)
        self.assertTrue(selection['diagnostic_only'])
        self.assertEqual(selection['projects'], projects)
        self.assertNotIn('graph_run', selection)
        context['frontend']['source_revisions'] = []
        with self.assertRaisesRegex(ValueError, 'source selection differs'):
            assembly.source_selection(context, manifest)


if __name__ == '__main__':
    unittest.main()
