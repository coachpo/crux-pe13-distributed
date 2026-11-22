import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cloud_schedule


POLICY = {'schema_version': 1, 'version': 'w10-1', 'wave_capacity': 4,
          'max_actions': 4000, 'max_parallel': 20,
          'category_order': ['identity', 'kernel', 'other', 'native', 'java', 'image']}


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(cloud_schedule, 'load_policy', return_value=copy.deepcopy(POLICY))
        patch.start()
        self.addCleanup(patch.stop)
        self.context = {
            'schedule_policy': copy.deepcopy(POLICY),
            'waves': [['w000-s000', 'w000-s001'], ['w001-s000']],
            'tasks': {
                'w000-s000': {'id': 'w000-s000', 'wave': 0, 'category': 'other', 'depends_on': []},
                'w000-s001': {'id': 'w000-s001', 'wave': 0, 'category': 'other', 'depends_on': []},
                'w001-s000': {'id': 'w001-s000', 'wave': 1, 'category': 'other', 'depends_on': ['w000-s000']}}}

    def test_unused_slot_is_inactive_but_real_wave_has_exact_task_matrix(self):
        self.assertEqual(cloud_schedule.matrix(self.context, 1), (True, {'id': ['w001-s000']}))
        self.assertEqual(cloud_schedule.matrix(self.context, 3), (False, {'id': ['unused-slot']}))
        for slot in (-1, 4):
            with self.subTest(slot=slot), self.assertRaisesRegex(ValueError, 'outside configured'):
                cloud_schedule.matrix(self.context, slot)

    def test_missing_empty_duplicate_and_over_capacity_waves_are_rejected(self):
        bad = [[], [['w000-s000'], []], [['w000-s000'], ['w000-s000']],
               [['w000-s000']], [[str(index)] for index in range(5)]]
        for waves in bad:
            with self.subTest(waves=waves), self.assertRaises(ValueError):
                cloud_schedule.validate_context({**self.context, 'waves': waves})

    def test_future_same_wave_and_wrong_wave_task_bindings_are_rejected(self):
        for task_id, change in [('w000-s000', {'depends_on': ['w001-s000']}),
                                ('w000-s000', {'depends_on': ['w000-s001']}),
                                ('w001-s000', {'wave': 0})]:
            context = copy.deepcopy(self.context)
            context['tasks'][task_id].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'task binding'):
                cloud_schedule.validate_context(context)

    def test_context_requires_the_exact_configured_policy_not_just_its_version(self):
        for policy in (None, {**POLICY, 'wave_capacity': 5}, {**POLICY, 'max_actions': 5000}):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, 'schedule policy differs'):
                cloud_schedule.validate_context({**self.context, 'schedule_policy': policy})

    def test_failed_or_skipped_real_or_unused_slot_cannot_be_a_successful_summary(self):
        results = {'bootstrap': {'result': 'success'}, **{
            'wave' + str(index): {'result': 'success'} for index in range(4)}}
        cloud_schedule.validate_slot_results(self.context, results)
        for name in ('bootstrap', 'wave1', 'wave3'):
            for result in ('failure', 'cancelled', 'skipped'):
                changed = copy.deepcopy(results)
                changed[name]['result'] = result
                with self.subTest(name=name, result=result), self.assertRaises(ValueError):
                    cloud_schedule.validate_slot_results(self.context, changed)
        results.pop('wave3')
        with self.assertRaises(ValueError):
            cloud_schedule.validate_slot_results(self.context, results)

    def test_preparation_plan_must_bind_full_policy_and_matching_action_limits(self):
        plan = {'partition_policy': copy.deepcopy(POLICY), 'max_actions': 4000, 'max_parallel': 20,
                'waves': [[copy.deepcopy(self.context['tasks'][ident]) for ident in wave]
                          for wave in self.context['waves']]}
        self.assertEqual(cloud_schedule.validate_plan(plan), (POLICY, self.context['waves']))
        for change in ({'partition_policy': None}, {'max_actions': 3000}, {'max_parallel': 21}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'partition policy differs'):
                cloud_schedule.validate_plan({**plan, **change})

    def test_preparation_plan_cannot_relabel_a_task_declared_for_another_wave(self):
        plan = {'partition_policy': copy.deepcopy(POLICY), 'max_actions': 4000, 'max_parallel': 20,
                'waves': [[copy.deepcopy(self.context['tasks'][ident]) for ident in wave]
                          for wave in self.context['waves']]}
        plan['waves'][1][0]['wave'] = 0
        with self.assertRaisesRegex(ValueError, 'Invalid task binding'):
            cloud_schedule.validate_plan(plan)
        plan['waves'][1][0].pop('wave')
        with self.assertRaisesRegex(ValueError, 'Invalid task binding'):
            cloud_schedule.validate_plan(plan)

    def test_preparation_and_context_reject_missing_or_unknown_category(self):
        for category in (None, 'ordinary-not-in-policy'):
            context = copy.deepcopy(self.context)
            context['tasks']['w001-s000']['category'] = category
            with self.subTest(category=category), self.assertRaisesRegex(ValueError, 'category enum'):
                cloud_schedule.validate_context(context)
            plan = {'partition_policy': copy.deepcopy(POLICY), 'max_actions': 4000, 'max_parallel': 20,
                    'waves': [[copy.deepcopy(context['tasks'][ident]) for ident in wave]
                              for wave in context['waves']]}
            with self.subTest(category=category), self.assertRaisesRegex(ValueError, 'category enum'):
                cloud_schedule.validate_plan(plan)
    def test_generator_serializes_each_configured_slot_and_all_slot_results(self):
        source = '\n  wave0:\n    needs: bootstrap\n\n  summary:\n    needs: wave0\n' \
                 '    runs-on: ubuntu-22.04\n\n  report:\n    needs: [bootstrap, wave0, summary, next]\n'
        rendered = cloud_schedule.render_workflow(source, POLICY)
        for index in range(4):
            block = rendered.split('\n  wave' + str(index) + ':\n', 1)[1].split('\n\n', 1)[0]
            previous = 'bootstrap' if index == 0 else 'wave' + str(index - 1)
            self.assertIn("needs." + previous + ".result == 'success'", block)
            self.assertIn('wave: ' + str(index), block)
        self.assertIn('summary:\n    needs: [bootstrap, wave0, wave1, wave2, wave3]', rendered)
        self.assertIn('report:\n    needs: [bootstrap, wave0, wave1, wave2, wave3, summary, next]', rendered)
        self.assertEqual(cloud_schedule.render_workflow(rendered, POLICY), rendered)


class PolicyTests(unittest.TestCase):
    def test_workflow_declares_the_configured_serial_slots_and_complete_summary_needs(self):
        source = cloud_schedule.WORKFLOW.read_text()
        self.assertEqual(cloud_schedule.render_workflow(source, cloud_schedule.load_policy()), source)

    def test_policy_rejects_missing_invalid_or_boolean_capacity(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'policy.json'
            for policy in ({}, {**POLICY, 'wave_capacity': True}, {**POLICY, 'wave_capacity': 0},
                           {**POLICY, 'max_parallel': -1}):
                path.write_text(json.dumps(policy))
                with self.subTest(policy=policy), self.assertRaises(ValueError):
                    cloud_schedule.load_policy(path)
            path.write_text(json.dumps(POLICY))
            self.assertEqual(cloud_schedule.load_policy(path), POLICY)


if __name__ == '__main__':
    unittest.main()
