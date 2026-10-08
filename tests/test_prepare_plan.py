import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

module_path = Path(__file__).resolve().parents[1] / 'scripts/prepare_plan.py'
spec = importlib.util.spec_from_file_location('prepare_plan', module_path)
prepare_plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_plan)


class PreparePlanTests(unittest.TestCase):
    def test_lazy_plan_preserves_full_dependency_and_source_routes_without_capsules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for identity in ('common', 'first', 'last'):
                (root / identity).mkdir()
                (root / identity / 'manifest.json').write_text('{}')
                (root / identity / 'build.ninja').write_text('build output: phony\n')
            jobs = [
                {'id': 'common', 'manifest': 'common/manifest.json', 'depends_on': []},
                {'id': 'first', 'manifest': 'first/manifest.json', 'depends_on': ['common']},
                {'id': 'last', 'manifest': 'last/manifest.json', 'depends_on': ['common', 'first'],
                 'source_overlays': [{'assets': ['extra.tar.zst']}]}]
            graph = {'job_count': 3, 'source_root': '/source', 'targets': ['rom'],
                     'action_count': 3, 'waves': [[job] for job in jobs],
                     'terminal_outputs': ['image'], 'export_producers': {'image': 'last'}}
            plan, uploads = prepare_plan.convert(graph, root / 'capsules',
                {'profile_asset': 'profile.json'}, {'pack_command': ['python3', 'capsule.py']}, root)
            self.assertEqual(plan['schema_version'], 2)
            self.assertEqual(uploads, [])
            last = plan['waves'][2]['tasks'][0]
            self.assertEqual(last['dependencies'], [{'wave': 0, 'shard': 'common'}, {'wave': 1, 'shard': 'first'}])
            self.assertEqual(last['source_archives'], {'profile_asset': 'profile.json'})
            self.assertEqual(last['source_overlays'], [{'assets': ['extra.tar.zst']}])
            self.assertEqual(len(last['preparation']['slice_files']), 2)
            self.assertNotIn('capsule_assets', last)
            self.assertEqual(plan['terminal_outputs'], [{'path': 'image', 'wave': 2, 'shard': 'last'}])

    def test_multipart_source_round_trip_preserves_bytes_and_order(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'capsule.tar.zst'
            archive.write_bytes(b'abcdefghijk')
            with patch.object(prepare_plan, 'ASSET_PART_BYTES', 4):
                parts = prepare_plan.assets_for(archive)
            self.assertEqual([part.stat().st_size for part in parts], [4, 4, 3])
            self.assertEqual(b''.join(part.read_bytes() for part in parts), archive.read_bytes())
