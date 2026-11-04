import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location('cloud_shard', Path(__file__).resolve().parents[1] / 'scripts/cloud_shard.py')
shard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shard)


class CloudShardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.native = self.root / 'native.ninja'
        self.number = str(self.root / 'build_number.txt')
        self.other = str(self.root / 'other.txt')
        self.native.write_text('rule native\n  command = touch $out\n'
                               + f'build {self.number}: native\n'
                               + f'build {self.other}: native\n')
        self.database = self.root / 'index.sqlite'
        shard.graph.index_graph(self.native, self.source, self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def test_edge_command_override_preserves_another_user_of_the_same_rule(self):
        original = self.native.read_text()
        current = shard.graph.Graph(self.database)
        owners = current.producers([self.number, self.other])
        current.db.close()
        mutation = shard.Mutations(self.database)
        desired = {'output': self.number, 'desired_command': '/usr/bin/python3 -c "print(42)"'}
        mutation.scalar(owners[self.number], desired)
        mutation.publish()
        current = shard.graph.Graph(self.database)
        records, _ = current.edge_records(owners.values())
        metadata = current.native_metadata(records)
        current.db.close()
        self.assertEqual(metadata[owners[self.number]]['command'], desired['desired_command'])
        self.assertEqual(metadata[owners[self.other]]['command'], 'touch ' + self.other)
        self.assertEqual(self.native.read_text(), original)

    def test_scalar_touch_recognition_unwraps_only_the_native_writer(self):
        self.assertTrue(shard.is_scalar_touch('/bin/bash -c "touch ' + self.number + '"',
                                              self.number, self.source))
        self.assertTrue(shard.is_scalar_touch('touch ' + self.number, self.number, self.source))
        self.assertFalse(shard.is_scalar_touch('/bin/bash -c "rm -f other; touch ' + self.number + '"',
                                               self.number, self.source))
        self.assertFalse(shard.is_scalar_touch('touch another', self.number, self.source))

    def test_companion_scope_and_new_dependencies_are_canonical_and_transportable(self):
        companion = self.root / 'companion.ninja'
        produced = str(self.root / 'generated')
        companion.write_text('rule cold\n  command = touch $out\n' + f'build {produced}: cold\n')
        wrapper = self.root / 'wrapper.ninja'
        shard.append_companion(self.database, self.source, companion, wrapper, 1700000000)
        current = shard.graph.Graph(self.database)
        consumer = current.producers([self.other])[self.other]
        owner = current.producers([produced])[produced]
        self.assertEqual(current.meta['file_stats'][str(companion)]['mtime_ns'], 1700000000 * 10**9)
        current.db.close()
        mutation = shard.Mutations(self.database)
        self.assertEqual(mutation.dependencies({consumer: [produced]}, 'generated dependency'), 1)
        mutation.publish()
        self.assertEqual(mutation.dependencies({consumer: [produced, self.other]}, 'same dependency'), 0)
        second = str(self.root / 'second-input')
        self.assertEqual(mutation.dependencies({consumer: [second]}, 'second dependency category'), 1)
        mutation.publish()
        current = shard.graph.Graph(self.database)
        closure = current.closure([self.other])
        self.assertIn(owner, closure['edge_ids'])
        metadata = current.native_metadata(current.edge_records(closure['edge_ids'])[0])
        self.assertEqual(metadata[consumer]['command'], 'touch ' + self.other)
        raw = mutation.updated[consumer]
        self.assertIn(produced, raw)
        self.assertIn(second, raw)
        self.assertEqual(len(current.meta['file_stats']), 3)
        current.db.close()

    def test_bulk_dependencies_update_multiple_consumers_consistently(self):
        current = shard.graph.Graph(self.database)
        owners = current.producers([self.number, self.other])
        current.db.close()
        # Exceed SQLite's ordinary page cache, as real notice/host-tool reads
        # do, so the write transaction cannot rely on staying memory-only.
        leaves = [str(self.source / ('x' * 180) / ('y' * 180) / (str(index) + '-' + 'z' * 150))
                  for index in range(8192)]
        mutation = shard.Mutations(self.database)
        additions = {owners[self.number]: leaves, owners[self.other]: ['another-input']}
        connect = shard.sqlite3.connect
        def constrained_cache(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.execute('PRAGMA cache_size=1')
            connection.execute('PRAGMA cache_spill=1')
            return connection
        with mock.patch.object(shard.sqlite3, 'connect', side_effect=constrained_cache):
            self.assertEqual(mutation.dependencies(additions, 'bulk runtime inputs'), len(leaves) + 1)
        current = shard.graph.Graph(self.database)
        records, _ = current.edge_records(owners.values())
        current.db.close()
        self.assertEqual({path for path, kind in records[owners[self.number]]['deps']}, set(leaves))
        self.assertIn('another-input', mutation.updated[owners[self.other]])

    def test_noop_candidates_do_not_publish_command_adaptations(self):
        current = shard.graph.Graph(self.database)
        owners = current.producers([self.number, self.other])
        current.db.close()
        mutation = shard.Mutations(self.database)
        self.assertEqual(mutation.dependencies({owners[self.number]: [self.number]}, 'own output'), 0)
        self.assertEqual(mutation.publish()['command_adaptations'], [])
        self.assertEqual(mutation.dependencies({owners[self.other]: ['new-input']}, 'new dependency'), 1)
        profile = mutation.publish()
        self.assertEqual([item['producer'] for item in profile['command_adaptations']], [self.other])


if __name__ == '__main__':
    unittest.main()
