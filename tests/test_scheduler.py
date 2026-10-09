import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

module_path = Path(__file__).resolve().parents[1] / 'scripts/scheduler.py'
spec = importlib.util.spec_from_file_location('scheduler', module_path)
scheduler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)


class SchedulerTests(unittest.TestCase):
    def compile_job(self, identity, conclusion):
        return {'name': f'compile ({identity}, remote-source-profile.json, capsule.tar.zst',
                'status': 'completed', 'conclusion': conclusion}

    def test_partial_wave_retry_preserves_successes_and_uses_each_producer_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, state_path = root / 'plan.json', root / 'state.json'
            ids = ['one', 'two', 'three']
            plan_path.write_text(json.dumps({'schema_version': 2, 'waves': [
                {'id': 0, 'tasks': [{'id': identity, 'capsule_assets': [identity + '.tar.zst']}
                                    for identity in ids]},
                {'id': 1, 'tasks': [{'id': 'consumer', 'capsule_assets': ['consumer.tar.zst'],
                                    'dependencies': [{'wave': 0, 'shard': identity} for identity in ids]}]}]}))
            original_plan = plan_path.read_bytes()
            revision = 'a' * 40
            state_path.write_text(json.dumps({'repo': 'owner/project', 'tag': 'inputs',
                'plan_sha256': scheduler.digest(plan_path), 'worker_sha': revision,
                'worker_ref': 'fixed-worker', 'runs': {}}))
            argv = ['scheduler', '--plan', str(plan_path), '--state', str(state_path),
                    '--repo', 'owner/project', '--tag', 'inputs']
            captured = {}
            run_ids = iter([101, 202, 303])
            def dispatch(repo, tag, wave, ref):
                run_id = next(run_ids)
                captured[run_id] = json.loads((root / f'wave-{wave}.json').read_text())
                return {'databaseId': run_id, 'url': f'https://github.com/run/{run_id}', 'headSha': revision}
            results = [
                {'conclusion': 'failure', 'url': 'https://github.com/run/101', 'jobs': [self.compile_job('one', 'success'),
                    self.compile_job('two', 'success'), self.compile_job('three', 'failure')]},
                {'conclusion': 'success', 'jobs': [self.compile_job('three', 'success')]},
                {'conclusion': 'success', 'jobs': [self.compile_job('consumer', 'success')]}]
            with patch.object(sys, 'argv', argv), patch.object(scheduler, 'gh', return_value=revision), \
                    patch.object(scheduler, 'dispatch', side_effect=dispatch), \
                    patch.object(scheduler, 'wait', side_effect=results):
                with self.assertRaisesRegex(RuntimeError, 'Wave 0 failed'):
                    scheduler.main()
                failed = json.loads(state_path.read_text())
                self.assertEqual(set(failed['task_runs']), {'one', 'two'})
                self.assertEqual(failed['runs']['0']['conclusion'], 'failure')
                scheduler.main()
            self.assertEqual([task['id'] for task in captured[202]['include']], ['three'])
            self.assertEqual(captured[303]['include'][0]['dependencies'], [
                {'id': 'one', 'run_id': 101, 'artifact': 'shard-one', 'expected_worker_commit': revision},
                {'id': 'two', 'run_id': 101, 'artifact': 'shard-two', 'expected_worker_commit': revision},
                {'id': 'three', 'run_id': 202, 'artifact': 'shard-three', 'expected_worker_commit': revision}])
            state = json.loads(state_path.read_text())
            self.assertEqual(state['failed_runs']['0'][0]['run_id'], 101)
            self.assertEqual(state['failed_runs']['0'][0]['conclusion'], 'failure')
            self.assertEqual(state['task_runs']['one']['run_id'], 101)
            self.assertEqual(state['task_runs']['three']['run_id'], 202)
            self.assertEqual(plan_path.read_bytes(), original_plan)

    def test_explicit_worker_migration_reuses_old_success_and_dispatches_only_failed_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, state_path = root / 'plan.json', root / 'state.json'
            old = '838635345395b7d09915c1ae74813bd20da6d84f'
            current = '178b5441eddbbb05d638f69b08848c08c42e2ad1'
            plan_path.write_text(json.dumps({'schema_version': 2, 'waves': [
                {'id': 0, 'tasks': [{'id': identity, 'capsule_assets': [identity + '.tar.zst']}
                                    for identity in ['old-good', 'retry']]},
                {'id': 1, 'tasks': [{'id': 'consumer', 'capsule_assets': ['consumer.tar.zst'],
                    'dependencies': [{'wave': 0, 'shard': 'old-good'}, {'wave': 0, 'shard': 'retry'}]}]}]}))
            prior = {'run_id': 101, 'url': 'https://github.com/run/101', 'headSha': old,
                     'conclusion': 'failure', 'jobs': [self.compile_job('old-good', 'success'),
                                                     self.compile_job('retry', 'failure')]}
            state_path.write_text(json.dumps({'repo': 'owner/project', 'tag': 'inputs',
                'plan_sha256': scheduler.digest(plan_path), 'worker_sha': current, 'worker_ref': 'new-worker',
                'accepted_worker_commits': [old, current], 'runs': {'0': prior}}))
            argv = ['scheduler', '--plan', str(plan_path), '--state', str(state_path),
                    '--repo', 'owner/project', '--tag', 'inputs', '--ref', current]
            captured = {}
            run_ids = iter([202, 303])
            def dispatch(repo, tag, wave, ref):
                self.assertEqual(ref, 'new-worker')
                run_id = next(run_ids)
                captured[run_id] = json.loads((root / f'wave-{wave}.json').read_text())
                return {'databaseId': run_id, 'url': f'https://github.com/run/{run_id}', 'headSha': current}
            results = [{'conclusion': 'success', 'jobs': [self.compile_job('retry', 'success')]},
                       {'conclusion': 'success', 'jobs': [self.compile_job('consumer', 'success')]}]
            with patch.object(sys, 'argv', argv), patch.object(scheduler, 'gh', return_value=current), \
                    patch.object(scheduler, 'dispatch', side_effect=dispatch), \
                    patch.object(scheduler, 'wait', side_effect=results):
                scheduler.main()
            state = json.loads(state_path.read_text())
            self.assertEqual([task['id'] for task in captured[202]['include']], ['retry'])
            self.assertEqual(captured[303]['include'][0]['dependencies'], [
                {'id': 'old-good', 'run_id': 101, 'artifact': 'shard-old-good', 'expected_worker_commit': old},
                {'id': 'retry', 'run_id': 202, 'artifact': 'shard-retry', 'expected_worker_commit': current}])
            self.assertEqual(state['task_runs']['old-good']['headSha'], old)
            self.assertEqual(state['task_runs']['retry']['headSha'], current)
            self.assertEqual(state['failed_runs']['0'], [prior])
            with self.assertRaisesRegex(ValueError, 'different worker commit'):
                scheduler.validate_worker(state, 'c' * 40, 'Unapproved producer')

    def test_approved_input_correction_preserves_primary_capsule_and_dependencies(self):
        revision = 'a' * 40
        task = {'id': 'task', 'capsule_assets': ['original.tar.zst'],
                'dependencies': [{'run_id': 123, 'artifact': 'shard-producer'}],
                'source_overlays': [{'assets': ['existing.tar.zst']}],
                'source_archives': {'profile_asset': 'profile.json', 'bundle_assets': ['existing.json']}}
        original = json.dumps(task)
        correction = {'approved': True, 'expected_worker_sha': revision, 'expected_worker_ref': 'fixed-worker',
                      'source_overlays': [{'assets': ['approved-source-only.tar.zst']}],
                      'source_archives': {'bundle_assets': ['corrected.json']}}
        state = {'worker_sha': revision, 'worker_ref': 'fixed-worker', 'input_corrections': {'task': correction}}
        corrected = scheduler.apply_input_correction(state, task)
        self.assertEqual(corrected['capsule_assets'], task['capsule_assets'])
        self.assertEqual(corrected['dependencies'], task['dependencies'])
        self.assertEqual(corrected['source_overlays'], [{'assets': ['existing.tar.zst']},
                                                     {'assets': ['approved-source-only.tar.zst']}])
        self.assertEqual(corrected['source_archives'], {'profile_asset': 'profile.json',
                                                      'bundle_assets': ['corrected.json']})
        self.assertEqual(json.dumps(task), original)
        correction['approved'] = False
        with self.assertRaisesRegex(ValueError, 'explicit approval'):
            scheduler.apply_input_correction(state, task)
        correction['approved'] = True
        correction['expected_worker_sha'] = 'b' * 40
        with self.assertRaisesRegex(ValueError, 'different worker'):
            scheduler.apply_input_correction(state, task)

    def test_failed_task_binding_is_not_a_producer_even_if_run_succeeded(self):
        state = {'runs': {'0': {'run_id': 123, 'conclusion': 'success'}},
                 'task_runs': {'producer': {'run_id': 123, 'conclusion': 'failure'}}}
        with self.assertRaisesRegex(ValueError, 'Unsuccessful task dependency'):
            scheduler.dependency_producer(state, {'wave': 0, 'shard': 'producer'})

    def test_success_binding_carries_immutable_expected_manifest_for_assembly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, plan, archive = self.lazy_fixture(root)
            revision = 'a' * 40
            state = {'worker_sha': revision}
            run = {'run_id': 123, 'url': 'https://github.com/run/123', 'headSha': revision,
                   'conclusion': 'success', 'jobs': [self.compile_job(task['id'], 'success')]}
            scheduler.record_successful_tasks(state, {'id': 0, 'tasks': [task]}, run)
            self.assertEqual(state['task_runs'][task['id']]['manifest_sha256'],
                             scheduler.digest(root / 'manifest.json'))
            self.assertEqual(scheduler.dependency_producer(state, {'wave': 0, 'shard': task['id']}), {
                'id': task['id'], 'run_id': 123, 'artifact': 'shard-task',
                'expected_worker_commit': revision, 'expected_manifest_sha256': scheduler.digest(root / 'manifest.json')})

    def test_successful_subset_retry_cannot_close_wave_with_missing_original_producer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, state_path = root / 'plan.json', root / 'state.json'
            revision = 'a' * 40
            plan_path.write_text(json.dumps({'schema_version': 2, 'waves': [
                {'id': 0, 'tasks': [{'id': identity, 'capsule_assets': [identity + '.tar.zst']}
                                    for identity in ['one', 'two', 'three']]},
                {'id': 1, 'tasks': [{'id': 'consumer', 'capsule_assets': ['consumer.tar.zst']}]}]}))
            state_path.write_text(json.dumps({'repo': 'owner/project', 'tag': 'inputs',
                'plan_sha256': scheduler.digest(plan_path), 'worker_sha': revision,
                'task_runs': {'one': {'run_id': 101, 'headSha': revision, 'conclusion': 'success'}},
                'runs': {'0': {'run_id': 202, 'url': 'https://github.com/run/202', 'headSha': revision,
                              'conclusion': 'success', 'task_ids': ['three'],
                              'jobs': [self.compile_job('three', 'success')]}}}))
            argv = ['scheduler', '--plan', str(plan_path), '--state', str(state_path),
                    '--repo', 'owner/project', '--tag', 'inputs']
            with patch.object(sys, 'argv', argv), patch.object(scheduler, 'gh') as gh, \
                    patch.object(scheduler, 'dispatch') as dispatch:
                with self.assertRaisesRegex(ValueError, 'missing producer bindings: two'):
                    scheduler.main()
            gh.assert_not_called()
            dispatch.assert_not_called()
            self.assertNotIn('finished_at', json.loads(state_path.read_text()))

    def test_approved_variant_and_supplemental_producer_reach_control_and_success_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, _, _ = self.lazy_fixture(root)
            task['dependencies'] = [{'wave': 0, 'shard': 'base'}]
            original_sha = scheduler.frozen_manifest(task)
            variant = root / 'variant-manifest.json'
            variant.write_text(json.dumps({'runtime_dir': '.crux-task/graph',
                                           'external_inputs': ['/out/fresh-bionic-header.h']}))
            variant_sha = scheduler.digest(variant)
            proof = root / 'variant-proof.json'
            proof.write_text(json.dumps({'native_commands_unchanged': True,
                                         'original_manifest_sha256': original_sha,
                                         'variant_manifest_sha256': variant_sha}))
            graph = root / 'graph-plan.json'
            graph.write_text('{}')
            revision = 'a' * 40
            supplemental = {'id': 'bionic-headers-build-date', 'run_id': 789,
                            'artifact': 'shard-bionic-headers-build-date',
                            'expected_worker_commit': revision, 'expected_manifest_sha256': 'e' * 64}
            correction = {'approved': True, 'original_manifest_sha256': original_sha,
                          'variant_manifest_sha256': variant_sha,
                          'variant_provenance': {'verified': True,
                                                'proof': {'path': str(proof), 'sha256': scheduler.digest(proof)}},
                          'source_overlays': [{'assets': ['reviewed-variant.tar.zst']}],
                          'supplemental_dependencies': [supplemental, dict(supplemental)]}
            plan_path, state_path = root / 'plan.json', root / 'state.json'
            plan_path.write_text(json.dumps({'schema_version': 2, 'source_root': '/source',
                'preparation': {'out_root': '/out', 'graph_plan': {'path': str(graph), 'sha256': scheduler.digest(graph)}},
                'waves': [{'id': 0, 'tasks': [{'id': 'base', 'capsule_assets': ['base.tar.zst']}]},
                          {'id': 1, 'tasks': [task]}]}))
            original_plan = plan_path.read_bytes()
            state = {'repo': 'owner/project', 'tag': 'inputs', 'plan_sha256': scheduler.digest(plan_path),
                'worker_sha': revision, 'worker_ref': 'fixed-worker',
                'runs': {'0': {'run_id': 123, 'url': 'https://github.com/run/123',
                              'headSha': revision, 'conclusion': 'success'}},
                'inputs': {'task': {'uploaded': True, 'parts': [{'name': 'task.tar.zst'}]}},
                'task_runs': {'base': {'run_id': 123, 'headSha': revision, 'conclusion': 'success',
                                      'manifest_sha256': 'b' * 64, 'artifact': 'shard-base'},
                              'bionic-headers-build-date': {'run_id': 789, 'headSha': revision,
                                  'conclusion': 'success', 'manifest_sha256': 'e' * 64,
                                  'artifact': 'shard-bionic-headers-build-date'}},
                'input_corrections': {'task': correction}}
            state_path.write_text(json.dumps(state))
            argv = ['scheduler', '--plan', str(plan_path), '--state', str(state_path),
                    '--repo', 'owner/project', '--tag', 'inputs']
            with patch.object(sys, 'argv', argv), patch.object(scheduler, 'gh', return_value=revision), \
                    patch.object(scheduler, 'dispatch', return_value={'databaseId': 456,
                        'url': 'https://github.com/run/456', 'headSha': revision}), \
                    patch.object(scheduler, 'wait', return_value={'conclusion': 'success',
                                                               'jobs': [self.compile_job('task', 'success')]}):
                scheduler.main()
            control = json.loads((root / 'wave-1.json').read_text())['include'][0]
            self.assertEqual(control['capsule_assets'], ['task.tar.zst'])
            self.assertEqual(control['dependencies'], [
                {'id': 'base', 'run_id': 123, 'artifact': 'shard-base',
                 'expected_worker_commit': revision, 'expected_manifest_sha256': 'b' * 64}, supplemental])
            self.assertEqual(control['source_overlays'], [{'assets': ['reviewed-variant.tar.zst']}])
            bound = json.loads(state_path.read_text())['task_runs']['task']
            self.assertEqual(bound['manifest_sha256'], variant_sha)
            self.assertEqual(bound['variant_manifest_sha256'], variant_sha)
            self.assertEqual(bound['original_manifest_sha256'], original_sha)
            self.assertEqual(bound['variant_provenance'], correction['variant_provenance'])
            self.assertEqual(plan_path.read_bytes(), original_plan)
            prepared = {key: value for key, value in task.items() if key != 'preparation'}
            correction['original_manifest_sha256'] = 'c' * 64
            with self.assertRaisesRegex(ValueError, 'frozen original/provenance'):
                scheduler.apply_input_correction(state, prepared, task)
            correction['original_manifest_sha256'] = original_sha
            proof.write_text('changed proof')
            with self.assertRaisesRegex(ValueError, 'Frozen input changed'):
                scheduler.apply_input_correction(state, prepared, task)

    def test_supplemental_dependencies_reject_unknown_or_conflicting_producer_binding(self):
        revision = 'a' * 40
        descriptor = {'id': 'supplement', 'run_id': 789, 'artifact': 'shard-supplement',
                      'expected_worker_commit': revision, 'expected_manifest_sha256': 'e' * 64}
        producer = {'run_id': 789, 'headSha': revision, 'conclusion': 'success',
                    'artifact': 'shard-supplement', 'manifest_sha256': 'e' * 64}
        correction = {'approved': True, 'supplemental_dependencies': [descriptor]}
        task = {'id': 'task', 'capsule_assets': ['task.tar.zst'], 'dependencies': []}
        state = {'worker_sha': revision, 'task_runs': {'supplement': producer},
                 'input_corrections': {'task': correction}}
        for field, value in [('run_id', 790), ('expected_manifest_sha256', 'f' * 64),
                             ('expected_worker_commit', 'b' * 40), ('id', 'unknown')]:
            changed = dict(descriptor, **{field: value})
            if field == 'id':
                changed['artifact'] = 'shard-unknown'
            correction['supplemental_dependencies'] = [changed]
            with self.assertRaisesRegex(ValueError, 'successful binding'):
                scheduler.apply_input_correction(state, task)
        correction['supplemental_dependencies'] = [descriptor]
        task['dependencies'] = [dict(descriptor, run_id=111)]
        with self.assertRaisesRegex(ValueError, 'Conflicting supplemental dependency'):
            scheduler.apply_input_correction(state, task)
        task['dependencies'] = []
        producer['headSha'] = 'b' * 40
        descriptor['expected_worker_commit'] = 'b' * 40
        with self.assertRaisesRegex(ValueError, 'different worker commit'):
            scheduler.apply_input_correction(state, task)
        producer['headSha'] = revision
        descriptor['expected_worker_commit'] = revision
        del state['worker_sha']
        with self.assertRaisesRegex(ValueError, 'fixed current worker commit'):
            scheduler.apply_input_correction(state, task)

    def test_approved_primary_variant_bypasses_missing_original_capsule_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, plan, archive = self.lazy_fixture(root)
            archive.unlink()
            original = json.dumps(task)
            proof = root / 'proof.json'
            proof.write_text('native commands unchanged; imports augmented')
            variant_sha = 'd' * 64
            archive_sha = 'e' * 64
            metadata = {'schema_version': 1, 'source_root': '/source', 'out_root': '/out',
                        'slice_path': '/source/.crux-task/graph', 'manifest_sha256': variant_sha,
                        'archive': {'name': 'task-cooutputs-v1.tar.zst', 'sha256': archive_sha, 'size': 10},
                        'files': [{'path': '/source/.crux-task/graph/manifest.json', 'type': 'file',
                                   'mode': 0o644, 'size': 20, 'sha256': variant_sha, 'reasons': ['task-graph']},
                                  {'path': '/source/source.c', 'type': 'file', 'mode': 0o644,
                                   'size': 30, 'sha256': 'f' * 64, 'reasons': ['graph-leaf']}]}
            path = root / 'task-cooutputs-v1.json'
            path.write_text(json.dumps(metadata))
            part = {'name': 'task-cooutputs-v1.tar.zst', 'path': str(root / 'missing-archived-local-copy'),
                    'size': 10, 'sha256': archive_sha, 'uploaded': True,
                    'api_digest': 'sha256:' + archive_sha, 'api_size': 10}
            correction = {'approved': True, 'original_manifest_sha256': scheduler.frozen_manifest(task),
                          'variant_manifest_sha256': variant_sha,
                          'variant_provenance': {'verified': True, 'proof': {'path': str(proof),
                                                                          'sha256': scheduler.digest(proof)}},
                          'primary_input': {'metadata': {'path': str(path), 'sha256': scheduler.digest(path)},
                                            'parts': [part], 'archive_sha256': archive_sha,
                                            'manifest_sha256': variant_sha}}
            state = {'input_corrections': {'task': correction}}
            with patch.object(scheduler, 'gh') as gh, patch.object(scheduler.subprocess, 'run') as pack:
                prepared = scheduler.prepare_task(plan, task, state, root / 'state.json', 'owner/project', 'inputs')
            self.assertEqual(prepared['capsule_assets'], [part['name']])
            self.assertEqual(state['inputs']['task']['manifest_sha256'], variant_sha)
            self.assertEqual(state['frozen_inputs']['/source/source.c']['sha256'], 'f' * 64)
            self.assertEqual(json.dumps(task), original)
            pack.assert_not_called()
            gh.assert_not_called()
            del state['input_corrections']
            with self.assertRaisesRegex(ValueError, 'no approved primary input correction'):
                scheduler.prepare_task(plan, task, state, root / 'state.json', 'owner/project', 'inputs')
            state['input_corrections'] = {'task': correction}
            part['api_digest'] = 'sha256:' + 'a' * 64
            with self.assertRaisesRegex(ValueError, 'not API verified'):
                scheduler.prepare_task(plan, task, state, root / 'state.json', 'owner/project', 'inputs')

    def test_explicit_ready_subset_prepares_only_ready_task_and_waits_for_remaining_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = 'a' * 40
            plan_path, state_path = root / 'plan.json', root / 'state.json'
            plan_path.write_text(json.dumps({'schema_version': 2, 'waves': [
                {'id': 0, 'tasks': [{'id': 'ready', 'capsule_assets': ['ready.tar.zst']},
                                    {'id': 'unready', 'capsule_assets': ['unready.tar.zst']}]},
                {'id': 1, 'tasks': [{'id': 'later', 'capsule_assets': ['later.tar.zst']}]}]}))
            state = {'repo': 'owner/project', 'tag': 'inputs', 'plan_sha256': scheduler.digest(plan_path),
                     'worker_sha': revision, 'worker_ref': 'fixed-worker', 'runs': {},
                     'ready_subsets': {'0': {'approved': True, 'task_ids': ['ready'], 'reason': 'Controller approved'}}}
            state_path.write_text(json.dumps(state))
            argv = ['scheduler', '--plan', str(plan_path), '--state', str(state_path),
                    '--repo', 'owner/project', '--tag', 'inputs']
            with patch.object(sys, 'argv', argv), patch.object(scheduler, 'gh', return_value=revision), \
                    patch.object(scheduler, 'prepare_task', side_effect=lambda plan, task, *rest: task) as prepare, \
                    patch.object(scheduler, 'dispatch', return_value={'databaseId': 123,
                        'url': 'https://github.com/run/123', 'headSha': revision}) as dispatch, \
                    patch.object(scheduler, 'wait', return_value={'conclusion': 'success',
                        'jobs': [self.compile_job('ready', 'success')]}):
                scheduler.main()
            self.assertEqual([call.args[1]['id'] for call in prepare.call_args_list], ['ready'])
            dispatch.assert_called_once()
            completed = json.loads(state_path.read_text())
            self.assertEqual(completed['waves']['0']['status'], 'waiting_for_inputs')
            self.assertEqual(set(completed['task_runs']), {'ready'})
            self.assertNotIn('finished_at', completed)
            state['ready_subsets']['0']['task_ids'] = ['unknown']
            state_path.write_text(json.dumps(state))
            with patch.object(sys, 'argv', argv), patch.object(scheduler, 'prepare_task') as prepare:
                with self.assertRaisesRegex(ValueError, 'Invalid explicitly approved ready subset'):
                    scheduler.main()
            prepare.assert_not_called()

    def test_failed_wave_retry_preserves_previous_run_and_pins_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, state_path = root / 'plan.json', root / 'state.json'
            plan_path.write_text(json.dumps({'waves': [{'id': 0, 'tasks': [
                {'id': 'task', 'capsule_assets': ['task.tar.zst'], 'source_archives': {'profile_asset': 'profile.json'}}]}]}))
            prior = {'run_id': 123, 'url': 'https://github.com/run/123', 'conclusion': 'failure'}
            state_path.write_text(json.dumps({'repo': 'owner/project', 'tag': 'inputs',
                'plan_sha256': hashlib.sha256(plan_path.read_bytes()).hexdigest(), 'runs': {'0': prior}}))
            argv = ['scheduler', '--plan', str(plan_path), '--state', str(state_path), '--repo', 'owner/project', '--tag', 'inputs']
            revision = 'a' * 40
            with patch.object(sys, 'argv', argv), \
                    patch.object(scheduler, 'wait', return_value={'conclusion': 'success', 'jobs': [self.compile_job('task', 'success')]}), \
                    patch.object(scheduler, 'dispatch', return_value={'databaseId': 456, 'url': 'https://github.com/run/456', 'headSha': revision}) as dispatch, \
                    patch.object(scheduler, 'gh', return_value=revision):
                scheduler.main()
            dispatch.assert_called_once_with('owner/project', 'inputs', '0', 'worker-inputs-' + revision[:12])
            state = json.loads(state_path.read_text())
            self.assertEqual(state['failed_runs']['0'], [prior])
            self.assertEqual(state['worker_sha'], revision)
            self.assertEqual(state['runs']['0']['run_id'], 456)
            self.assertEqual(json.loads((root / 'wave-0.json').read_text())['include'][0]['source_archives'], {'profile_asset': 'profile.json'})

    def test_resume_keeps_fixed_worker_tag_after_main_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = 'a' * 40
            state = {'worker_sha': revision, 'worker_ref': 'fixed-worker'}
            with patch.object(scheduler, 'gh', return_value=revision) as gh:
                self.assertEqual(scheduler.pin_worker('owner/project', 'main', 'inputs', state, root / 'state.json'), 'fixed-worker')
            gh.assert_called_once_with('api', 'repos/owner/project/git/ref/tags/fixed-worker', '--jq', '.object.sha')

    def test_successful_saved_run_with_wrong_worker_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, state_path = root / 'plan.json', root / 'state.json'
            plan_path.write_text(json.dumps({'schema_version': 2, 'waves': [{'id': 0, 'tasks': []}]}))
            state_path.write_text(json.dumps({'repo': 'owner/project', 'tag': 'inputs',
                'plan_sha256': scheduler.digest(plan_path), 'worker_sha': 'a' * 40,
                'runs': {'0': {'run_id': 123, 'url': 'https://github.com/run/123', 'conclusion': 'success', 'headSha': 'b' * 40}}}))
            argv = ['scheduler', '--plan', str(plan_path), '--state', str(state_path), '--repo', 'owner/project', '--tag', 'inputs']
            with patch.object(sys, 'argv', argv), patch.object(scheduler, 'gh') as gh, patch.object(scheduler, 'wait') as wait:
                with self.assertRaisesRegex(ValueError, 'different worker commit'):
                    scheduler.main()
            gh.assert_not_called()
            wait.assert_not_called()

    def lazy_fixture(self, root):
        manifest = root / 'manifest.json'
        manifest.write_text(json.dumps({'runtime_dir': '.crux-task/graph'}))
        archive = root / 'task.tar.zst'
        archive.write_bytes(b'cold-source-test-archive')
        metadata = {'manifest_sha256': scheduler.digest(manifest), 'source_root': '/source',
                    'out_root': '/out', 'files': [{'path': '/source/.crux-task/graph/manifest.json',
                                                 'sha256': scheduler.digest(manifest),
                                                 'type': 'file', 'mode': 0o644, 'size': manifest.stat().st_size,
                                                 'reasons': ['task-graph']}],
                    'archive': {'name': archive.name, 'size': archive.stat().st_size, 'sha256': scheduler.digest(archive)}}
        (root / 'task.json').write_text(json.dumps(metadata))
        task = {'id': 'task', 'dependencies': [], 'source_archives': {'profile_asset': 'profile.json'},
                'preparation': {'archive': str(archive), 'manifest': str(manifest),
                                'existing_metadata': {'path': str(root / 'task.json'),
                                                      'sha256': scheduler.digest(root / 'task.json')},
                                'slice_files': [{'path': str(manifest), 'sha256': scheduler.digest(manifest)}]}}
        plan = {'source_root': '/source', 'preparation': {'out_root': '/out', 'pack_command': ['capsule.py']}}
        return task, plan, archive

    def test_lazy_capsule_is_uploaded_once_and_materialized_outside_immutable_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, plan, archive = self.lazy_fixture(root)
            original = json.dumps(task)
            state = {}
            with patch.object(scheduler, 'gh') as gh, patch.object(scheduler.subprocess, 'run') as pack:
                first = scheduler.prepare_task(plan, task, state, root / 'state.json', 'owner/project', 'inputs')
                second = scheduler.prepare_task(plan, task, state, root / 'state.json', 'owner/project', 'inputs')
            self.assertEqual(first, second)
            self.assertEqual(first['capsule_assets'], ['task.tar.zst'])
            self.assertNotIn('preparation', first)
            self.assertEqual(json.dumps(task), original)
            gh.assert_called_once()
            pack.assert_not_called()
            self.assertTrue(state['inputs']['task']['uploaded'])

    def test_capsule_mismatch_cannot_be_uploaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, plan, archive = self.lazy_fixture(root)
            archive.write_bytes(b'altered')
            with patch.object(scheduler, 'gh') as gh:
                with self.assertRaisesRegex(ValueError, 'transfer receipt'):
                    scheduler.prepare_task(plan, task, {}, root / 'state.json', 'owner/project', 'inputs')
            gh.assert_not_called()

    def test_cold_source_audit_failure_is_recorded_without_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, plan, archive = self.lazy_fixture(root)
            archive.unlink()
            state = {}
            with patch.object(scheduler, 'gh') as gh, \
                    patch.object(scheduler.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
                with self.assertRaisesRegex(RuntimeError, 'audit/packing failed'):
                    scheduler.prepare_task(plan, task, state, root / 'state.json', 'owner/project', 'inputs')
            self.assertEqual(json.loads((root / 'state.json').read_text())['inputs']['task']['status'], 'pack_failed')
            gh.assert_not_called()

    def test_source_shared_by_later_capsule_cannot_change_bytes_or_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, plan, archive = self.lazy_fixture(root)
            metadata_path = root / 'task.json'
            metadata = json.loads(metadata_path.read_text())
            metadata['files'][0]['reasons'] = ['task-graph']
            metadata['files'].append({'path': '/source/source.c', 'type': 'file', 'mode': 0o644,
                                      'size': 10, 'sha256': 'b' * 64, 'reasons': ['graph-leaf']})
            metadata_path.write_text(json.dumps(metadata))
            task['preparation']['existing_metadata']['sha256'] = scheduler.digest(metadata_path)
            state = {}
            with patch.object(scheduler, 'gh'):
                scheduler.prepare_task(plan, task, state, root / 'state.json', 'owner/project', 'inputs')
            later = {**task, 'id': 'later'}
            for key, value in [('sha256', 'c' * 64), ('mode', 0o755)]:
                changed = json.loads(json.dumps(metadata))
                changed['files'][1][key] = value
                metadata_path.write_text(json.dumps(changed))
                later['preparation']['existing_metadata']['sha256'] = scheduler.digest(metadata_path)
                with patch.object(scheduler, 'gh') as gh:
                    with self.assertRaisesRegex(ValueError, 'earlier cold capsule'):
                        scheduler.prepare_task(plan, later, state, root / 'state.json', 'owner/project', 'inputs')
                gh.assert_not_called()

    def test_partial_upload_resumes_same_accepted_capsule_without_repacking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, plan, archive = self.lazy_fixture(root)
            state = {}
            with patch.object(scheduler, 'gh', side_effect=RuntimeError('transport interrupted')):
                with self.assertRaisesRegex(RuntimeError, 'transport interrupted'):
                    scheduler.prepare_task(plan, task, state, root / 'state.json', 'owner/project', 'inputs')
            resumed = json.loads((root / 'state.json').read_text())
            self.assertEqual(resumed['inputs']['task']['status'], 'ready')
            self.assertNotIn('uploaded', resumed['inputs']['task'])
            with patch.object(scheduler, 'gh') as gh, patch.object(scheduler.subprocess, 'run') as pack:
                result = scheduler.prepare_task(plan, task, resumed, root / 'state.json', 'owner/project', 'inputs')
            self.assertEqual(result['capsule_assets'], ['task.tar.zst'])
            gh.assert_called_once()
            pack.assert_not_called()

    def test_unaccepted_ambient_capsule_is_repacked_for_fresh_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, plan, archive = self.lazy_fixture(root)
            del task['preparation']['existing_metadata']
            def current_pack(command, **kwargs):
                archive.write_bytes(b'current-frozen-source-archive')
                metadata_path = root / 'task.json'
                metadata = json.loads(metadata_path.read_text())
                metadata['archive'].update(size=archive.stat().st_size, sha256=scheduler.digest(archive))
                metadata_path.write_text(json.dumps(metadata))
                return SimpleNamespace(returncode=0)
            state = {}
            with patch.object(scheduler, 'gh'), patch.object(scheduler.subprocess, 'run', side_effect=current_pack) as pack:
                scheduler.prepare_task(plan, task, state, root / 'state.json', 'owner/project', 'inputs')
            pack.assert_called_once()
            self.assertEqual(state['inputs']['task']['archive_sha256'], hashlib.sha256(b'current-frozen-source-archive').hexdigest())

    def test_resume_waits_existing_run_without_dispatching_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, state_path = root / 'plan.json', root / 'state.json'
            plan_path.write_text(json.dumps({'waves': [{'id': 0, 'tasks': []}]}))
            state_path.write_text(json.dumps({
                'repo': 'owner/project', 'tag': 'inputs',
                'plan_sha256': hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                'runs': {'0': {'run_id': 123, 'url': 'https://github.com/run/123'}}}))
            argv = ['scheduler', '--plan', str(plan_path), '--state', str(state_path),
                    '--repo', 'owner/project', '--tag', 'inputs']
            with patch.object(sys, 'argv', argv), \
                    patch.object(scheduler, 'wait', return_value={'conclusion': 'success', 'jobs': []}) as wait, \
                    patch.object(scheduler, 'dispatch') as dispatch, \
                    patch.object(scheduler, 'gh') as gh:
                scheduler.main()
            wait.assert_called_once_with('owner/project', 123)
            dispatch.assert_not_called()
            gh.assert_not_called()
            self.assertEqual(json.loads(state_path.read_text())['runs']['0']['conclusion'], 'success')

    def test_changed_plan_cannot_reuse_successful_old_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, state_path = root / 'plan.json', root / 'state.json'
            plan_path.write_text(json.dumps({'waves': []}))
            state_path.write_text(json.dumps({'repo': 'owner/project', 'tag': 'inputs',
                                              'plan_sha256': 'old-plan', 'runs': {}}))
            argv = ['scheduler', '--plan', str(plan_path), '--state', str(state_path),
                    '--repo', 'owner/project', '--tag', 'inputs']
            with patch.object(sys, 'argv', argv), patch.object(scheduler, 'gh') as gh:
                with self.assertRaisesRegex(ValueError, 'different task plan'):
                    scheduler.main()
            gh.assert_not_called()
