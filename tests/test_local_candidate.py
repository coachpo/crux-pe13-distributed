import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'local_replay'))
import candidate


class LocalCandidateTests(unittest.TestCase):
    def test_persisted_capsule_receipt_is_the_result_of_the_existing_pack_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for operation, api, function_status in (('audit', 'audit_shard', 'success'),
                                                    ('pack', 'pack_shard', 'success'),
                                                    ('pack', 'pack_shard', 'failed')):
                with self.subTest(operation=operation, status=function_status):
                    target = root / (operation + '-' + function_status)
                    receipt = {'shard_id': 'w001-s003', 'status': function_status}
                    code = 0 if function_status == 'success' else 1
                    if code:
                        receipt['error'] = 'archive mismatch'
                    build = lambda: candidate.build_capsule(
                        'w001-s003', operation=operation, shards=root / 'shards', inventory=root / 'inventory.json',
                        receipt=root / 'slice.json', manifest=root / 'source.json', pruned=root / 'index.sqlite',
                        destination=target)
                    with mock.patch.object(candidate.cloud_pack, api, return_value=(code, receipt)):
                        if code:
                            with self.assertRaisesRegex(ValueError, 'archive mismatch'):
                                build()
                        else:
                            self.assertEqual(build(), receipt)
                    self.assertEqual(candidate.replay.read(target / 'w001-s003/pack-receipt.json'), receipt)

    def test_pack_scope_includes_all_transitive_predecessors_and_no_unrelated_tasks(self):
        context = {'waves': [['base', 'unrelated'], ['left', 'right'], ['assembly-producer']],
                   'tasks': {name: {'depends_on': deps} for name, deps in (
                       ('base', []), ('unrelated', []), ('left', ['base']), ('right', ['base']),
                       ('assembly-producer', ['right', 'left']))}}
        self.assertEqual(candidate.closure(context, ['assembly-producer']),
                         ['base', 'left', 'right', 'assembly-producer'])

    def test_graph_and_index_must_bind_the_pinned_source_and_exact_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph, index = root / 'graph', root / 'index'
            (graph / 'graph').mkdir(parents=True)
            (graph / 'source').mkdir()
            index.mkdir()
            manifest = root / 'source-manifest.json'
            manifest.write_text('{}')
            sha = candidate.cloud_build_plan.sha(manifest)
            receipt = graph / 'graph/graph-receipt.json'
            build = '2026111653'
            source = '/fresh/source'
            receipt.write_text(json.dumps({'status': 'success', 'build_number': build,
                                           'source_root': source, 'source_manifest': {'sha256': sha}}))
            (index / 'index-receipt.json').write_text(json.dumps({
                'status': 'success', 'build_number': build, 'graph_run': '42', 'manifest_sha256': sha,
                'graph_receipt_sha256': candidate.cloud_build_plan.sha(receipt)}))
            mount = graph / 'source/source-mount.json'
            mount.write_text(json.dumps({'source_root': source, 'source_run': '41', 'manifest_sha256': sha}))
            actual, _ = candidate.graph_binding(manifest, graph, index, build)
            self.assertEqual(actual['source_root'], source)
            mount.write_text(json.dumps({'source_root': source, 'source_run': '41', 'manifest_sha256': 'other'}))
            with self.assertRaisesRegex(ValueError, 'Freeze source mount'):
                candidate.graph_binding(manifest, graph, index, build)
            receipt.write_text(receipt.read_text() + '\n')
            with self.assertRaisesRegex(ValueError, 'canonical index'):
                candidate.graph_binding(manifest, graph, index, build)


if __name__ == '__main__':
    unittest.main()
