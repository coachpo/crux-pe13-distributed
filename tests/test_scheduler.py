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
                    patch.object(scheduler, 'wait', return_value={'conclusion': 'success', 'jobs': []}), \
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
