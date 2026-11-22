import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
graph = load('graph', SCRIPTS / 'graph.py')
prune_index = load('prune_index', SCRIPTS / 'prune-index.py')


class PruneIndexTests(unittest.TestCase):
    def build(self, root, unused_rule=False):
        (root / 'build.ninja').write_text(
            'rule cp\n  command = cp $in $out\n'
            'build a: cp input\nbuild b: cp a\nbuild final: phony b\n'
            'build unrelated: cp other\n')
        if unused_rule:
            with (root / 'build.ninja').open('a') as stream:
                stream.write('rule spare\n  command = unused $out\nbuild spare-output: spare\n')
        (root / 'input').write_text('seed')
        (root / 'other').write_text('decoy')
        database = root / 'index.sqlite'
        graph.index_graph(root / 'build.ninja', root, database)
        return database

    def test_pruned_index_keeps_the_closure_and_drops_unrelated_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.build(root)
            full = graph.Graph(database).closure(['final'])
            self.assertEqual(len(full['edge_ids']), 3)
            self.assertEqual(full['leaf_inputs'], ['input'])
            output = root / 'pruned.sqlite'
            record = root / 'prune-record.json'
            result = prune_index.prune(str(database), None, str(output), str(record), ['final'])
            self.assertEqual(result['closure_edge_count'], 3)
            self.assertEqual(result['output_row_counts']['edges'], 3)
            self.assertEqual(result['source_row_counts']['edges'], 4)
            self.assertEqual(json.loads(record.read_text()), result)
            pruned = graph.Graph(output)
            self.assertEqual(pruned.closure(['final'])['edge_ids'], full['edge_ids'])
            self.assertEqual(pruned.closure(['final'])['leaf_inputs'], full['leaf_inputs'])
            self.assertNotIn('unrelated', [row[0] for row in
                                           sqlite3.connect(output).execute('SELECT path FROM outputs')])

    def test_reused_closure_record_matches_the_database_walk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.build(root)
            closure = root / 'closure.json'
            closure.write_text(json.dumps(graph.Graph(database).closure(['final'])))
            record = root / 'prune-record.json'
            first = prune_index.prune(str(database), str(closure), str(root / 'a.sqlite'),
                                      str(record), ['final'])
            second = prune_index.prune(str(database), None, str(root / 'b.sqlite'),
                                       str(root / 'b.json'), ['final'])
            self.assertEqual(first['closure_edge_count'], second['closure_edge_count'])
            self.assertEqual(first['output_row_counts'], second['output_row_counts'])

    def test_existing_output_and_missing_source_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.build(root)
            existing = root / 'taken.sqlite'
            existing.write_text('')
            with self.assertRaises(prune_index.PruneError):
                prune_index.prune(str(database), None, str(existing), str(root / 'r.json'), ['final'])
            with self.assertRaises(prune_index.PruneError):
                prune_index.prune(str(root / 'absent.sqlite'), None, str(root / 'new.sqlite'),
                                  str(root / 'r.json'), ['final'])

    def test_inconsistent_closure_record_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.build(root)
            closure = root / 'closure.json'
            closure.write_text(json.dumps({'edge_ids': [1, 2], 'edge_count': 5}))
            with self.assertRaises(prune_index.PruneError):
                prune_index.prune(str(database), str(closure), str(root / 'out.sqlite'),
                                  str(root / 'r.json'), ['final'])

    def test_pruning_projects_ownership_and_preserves_shared_rule_adaptations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.build(root, unused_rule=True)
            graph.augment_graph(database, {
                'schema_version': 1,
                'side_output_dirs': [
                    {'producer': path, 'dirs': [path + '-tree'],
                     'rationale': 'fixture side effect', 'evidence': ['fixture recipe']}
                    for path in ['a', 'unrelated']],
                'group_export_dirs': [{'producer': path, 'dirs': [path + '-export']}
                                      for path in ['b', 'unrelated']],
                'groups': [['a', 'b'], ['unrelated']],
            })
            profile = {'schema_version': 1, 'command_adaptations': [
                {'producer': 'unrelated', 'scope': 'rule', 'replace': 'cp $in $out',
                 'with': 'mapped $in $out', 'expected_matches': 1,
                 'rationale': 'shared lexical rule', 'evidence': ['fixture recipe']},
                {'producer': 'unrelated', 'scope': 'edge', 'replace': 'build unrelated: cp other',
                 'with': 'build unrelated: cp other\n  command = unused', 'expected_matches': 1,
                 'rationale': 'outside target closure', 'evidence': ['fixture recipe']},
                {'producer': 'b', 'scope': 'edge', 'replace': 'build b: cp a',
                 'with': 'build b: cp a\n  command = retained', 'expected_matches': 1,
                 'rationale': 'selected edge override', 'evidence': ['fixture recipe']},
                {'producer': 'spare-output', 'replace': 'unused $out', 'with': 'not-selected $out',
                 'expected_matches': 1, 'rationale': 'unselected lexical rule', 'evidence': ['fixture recipe']},
            ]}
            graph.adapt_graph(database, profile)
            original = graph.Graph(database)
            expected = original.closure(['final'])
            metadata = original.native_metadata(original.edge_records(expected['edge_ids'])[0])
            original.db.close()
            output = root / 'pruned.sqlite'
            prune_index.prune(str(database), None, str(output), str(root / 'r.json'), ['final'])
            reduced = graph.Graph(output)
            try:
                self.assertEqual(reduced.closure(['final']), expected)
                actual = reduced.native_metadata(reduced.edge_records(expected['edge_ids'])[0])
                self.assertEqual(actual, metadata)
                projected = reduced.meta['runtime_adaptations']['command_adaptations']
                self.assertEqual([(item['producer'], item['scope']) for item in projected],
                                 [('a', 'rule'), ('b', 'edge')])
                self.assertEqual(set(reduced.side_output_dirs), {'a-tree'})
                self.assertEqual(set(reduced.group_export_dirs), {'b-export'})
                self.assertEqual(reduced.meta['ownership_profile']['groups'], [['a', 'b']])
                self.assertEqual(len(reduced.meta['atomic_groups']), 1)
            finally:
                reduced.db.close()

    def test_incomplete_but_count_consistent_closure_record_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.build(root)
            current = graph.Graph(database)
            payload = current.closure(['final'])
            current.db.close()
            payload['edge_ids'] = payload['edge_ids'][1:]
            payload['edge_count'] = len(payload['edge_ids'])
            closure = root / 'closure.json'
            closure.write_text(json.dumps(payload))
            with self.assertRaisesRegex(prune_index.PruneError, 'source graph closure'):
                prune_index.prune(str(database), str(closure), str(root / 'out.sqlite'),
                                  str(root / 'r.json'), ['final'])


if __name__ == '__main__':
    unittest.main()
