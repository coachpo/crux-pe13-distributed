import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

module_path = Path(__file__).resolve().parents[1] / 'scripts/scheduler.py'
spec = importlib.util.spec_from_file_location('scheduler', module_path)
scheduler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)


class SchedulerTests(unittest.TestCase):
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
