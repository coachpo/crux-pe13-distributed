import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


graph = load('graph', ROOT / 'scripts/graph.py')
cloud_host_runtime = load('cloud_host_runtime', ROOT / 'scripts/cloud_host_runtime.py')


class CloudHostRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'source'
        self.out = self.root / 'out'
        self.source.mkdir()
        self.paths = {
            'lib_nested': self.out / 'host/linux-x86/lib64/bionic/libc.so.1',
            'lib_b': self.out / 'host/linux-x86/lib64/libB.so.1',
            'lib_a': self.out / 'host/linux-x86/lib64/libA.so',
            'helper': self.out / 'host/linux-x86/bin/helper',
            'tool': self.out / 'host/linux-x86/framework/tool.jar',
            'consumer': self.out / 'obj/consumer.o',
            'self': self.out / 'host/linux-x86/bin/self-tool',
        }
        self.ninja = self.source / 'build.ninja'
        self.ninja.write_text(
            'rule build\n  command = touch $out\n'
            f'build {self.paths["lib_b"]}: build {self.paths["lib_nested"]}\n'
            f'build {self.paths["lib_nested"]}: build seed\n'
            f'build {self.paths["lib_a"]}: build {self.paths["lib_b"]}\n'
            f'build {self.paths["helper"]}: build {self.paths["lib_b"]}\n'
            f'build {self.paths["tool"]}: build {self.paths["lib_a"]}\n'
            'rule use\n'
            f'  command = clang -c source.cc -o $out {self.paths["tool"]} '
            f'@{self.out}/obj/use.rsp {self.paths["lib_a"]} '
            f'{self.source}/prebuilts/clang/bin/clang {self.out}/host/linux-x86/bin/not-produced\n'
            '  rspfile = $out.rsp\n'
            f'  rspfile_content = --helper {self.paths["helper"]}\n'
            f'build {self.paths["consumer"]}: use source.cc\n'
            'rule declared_use\n  command = clang -c source.cc -o $out\n'
            f'build {self.out}/obj/declared.o: declared_use source.cc | {self.paths["tool"]}\n'
            'rule self\n'
            f'  command = touch {self.paths["self"]} && {self.paths["self"]}\n'
            f'build {self.paths["self"]}: self\n'
        )
        self.database = self.root / 'index.sqlite'
        graph.index_graph(self.ninja, self.source, self.database)
        self.indexed = graph.Graph(self.database)
        edge_ids = [row['id'] for row in self.indexed.db.execute('SELECT id FROM edges')]
        self.records, self.producers = self.indexed.edge_records(edge_ids)
        self.native = self.indexed.native_metadata(self.records)

    def tearDown(self):
        self.indexed.db.close()
        self.temporary.cleanup()

    def test_derives_transitive_generated_libraries_and_missing_tool_inputs_read_only(self):
        records_before = copy.deepcopy(self.records)
        native_before = copy.deepcopy(self.native)
        ninja_before = self.ninja.read_bytes()
        database_before = self.database.read_bytes()

        result = cloud_host_runtime.derive_host_runtime(
            self.indexed, self.records, self.native, str(self.source), str(self.out))

        consumer = next(edge for edge, record in self.records.items()
                        if record['outputs'] == [str(self.paths['consumer'])])
        declared_consumer = next(edge for edge, record in self.records.items()
                                 if record['outputs'] == [str(self.out / 'obj/declared.o')])
        self_edge = next(edge for edge, record in self.records.items()
                         if record['outputs'] == [str(self.paths['self'])])
        self.assertEqual(result['additions'][consumer], sorted([
            str(self.paths['tool']), str(self.paths['helper']),
            str(self.paths['lib_a']), str(self.paths['lib_b']),
            str(self.paths['lib_nested'])]))
        self.assertEqual(result['additions'][declared_consumer], sorted([
            str(self.paths['lib_a']), str(self.paths['lib_b']),
            str(self.paths['lib_nested'])]))
        self.assertEqual(result['tools'][str(self.paths['tool'])], {
            'producer': self.producers[str(self.paths['tool'])],
            'generated_runtime_inputs': sorted([str(self.paths['lib_a']), str(self.paths['lib_b']),
                                                str(self.paths['lib_nested'])]),
        })
        self.assertEqual(result['tools'][str(self.paths['helper'])], {
            'producer': self.producers[str(self.paths['helper'])],
            'generated_runtime_inputs': sorted([str(self.paths['lib_b']), str(self.paths['lib_nested'])]),
        })
        self.assertNotIn(self_edge, result['additions'])
        self.assertNotIn(str(self.paths['self']), result['additions'][consumer])
        self.assertNotIn(str(self.source / 'prebuilts/clang/bin/clang'), result['additions'][consumer])
        self.assertEqual(result['unresolved_host_paths'], [{
            'edge': consumer,
            'path': str(self.out / 'host/linux-x86/bin/not-produced'),
            'reason': 'no_exact_producer',
        }])
        self.assertEqual(self.records, records_before)
        self.assertEqual(self.native, native_before)
        self.assertEqual(self.ninja.read_bytes(), ninja_before)
        self.assertEqual(self.database.read_bytes(), database_before)

    def test_rust_native_search_dirs_recover_global_archives_without_filesystem_reads(self):
        native_dir = self.out / 'host/linux-x86/rust-native'
        inline_dir = self.out / 'host/linux-x86/rust-inline'
        archive_a = native_dir / 'libring_core.a'
        archive_b = inline_dir / 'libzstd_glue.a'
        ignored_so = native_dir / 'libnot_static.so'
        nested_archive = native_dir / 'nested/libnot_searched.a'
        consumer = native_dir / 'libconsumer.a'
        rust_ninja = self.source / 'rust.ninja'
        rust_ninja.write_text(
            'rule archive\n  command = touch $out\n'
            f'build {archive_a}: archive seed\n'
            f'build {archive_b}: archive seed\n'
            f'build {ignored_so}: archive seed\n'
            f'build {nested_archive}: archive seed\n'
            'rule rust\n'
            f'  command = {self.out}/host/linux-x86/bin/rustc --crate-name consumer '
            f'-Lnative={native_dir} -L native={self.source}/prebuilts/native '
            f'-l static=ring_core -l static=missing_native @${{out}}.rsp -o $out\n'
            '  rspfile = $out.rsp\n'
            f'  rspfile_content = -Lnative={inline_dir} '
            '-lstatic=zstd_glue -Lnative=' + str(self.source / 'src/native') + '\n'
            f'build {consumer}: rust source.rs\n'
            'rule rust_source_only\n'
            f'  command = {self.out}/host/linux-x86/bin/rustc -L native={self.source}/prebuilts/native '
            '-l static=source_only_lib -o $out\n'
            f'build {self.out}/obj/source-only.rlib: rust_source_only source.rs\n'
        )
        database = self.root / 'rust.sqlite'
        graph.index_graph(rust_ninja, self.source, database)
        indexed = graph.Graph(database)
        try:
            # Selected records omit archive providers; they remain globally
            # discoverable in SQLite.
            source_only = self.out / 'obj/source-only.rlib'
            closure = indexed.closure([str(consumer), str(source_only)])
            records, _ = indexed.edge_records(closure['edge_ids'])
            native = indexed.native_metadata(records)
            before_records = copy.deepcopy(records)
            before_native = copy.deepcopy(native)
            before_ninja = rust_ninja.read_bytes()
            before_database = database.read_bytes()

            result = cloud_host_runtime.derive_rust_native(
                indexed, records, native, str(self.source), str(self.out))

            consumer_edge = next(edge for edge, record in records.items()
                                 if record['outputs'] == [str(consumer)])
            source_only_edge = next(edge for edge, record in records.items()
                                    if record['outputs'] == [str(source_only)])
            self.assertEqual(result['additions'], {consumer_edge: sorted([
                str(archive_a), str(archive_b)])})
            self.assertEqual(result['proofs'], [{
                'edge': consumer_edge,
                'search_dirs': sorted([str(native_dir), str(inline_dir)]),
                'archives': sorted([
                    {'path': str(archive_a), 'producer': indexed.producers([str(archive_a)], False)[str(archive_a)],
                     'added': True},
                    {'path': str(archive_b), 'producer': indexed.producers([str(archive_b)], False)[str(archive_b)],
                     'added': True},
                ], key=lambda row: row['path']),
                'static_libraries': ['libmissing_native.a', 'libring_core.a', 'libzstd_glue.a'],
            }])
            self.assertEqual(result['unresolved_native_archives'], [{
                'edge': consumer_edge,
                'archive': 'libmissing_native.a',
                'search_dirs': sorted([str(native_dir), str(inline_dir)]),
                'reason': 'no_exact_global_producer',
            }])
            self.assertNotIn(source_only_edge, result['additions'])
            self.assertNotIn(source_only_edge, {proof['edge'] for proof in result['proofs']})
            self.assertNotIn(str(ignored_so), result['additions'][consumer_edge])
            self.assertNotIn(str(nested_archive), result['additions'][consumer_edge])
            self.assertNotIn(str(self.source / 'prebuilts/native'), result['additions'][consumer_edge])
            self.assertNotIn(str(consumer), result['additions'][consumer_edge])
            self.assertEqual(records, before_records)
            self.assertEqual(native, before_native)
            self.assertEqual(rust_ninja.read_bytes(), before_ninja)
            self.assertEqual(database.read_bytes(), before_database)
        finally:
            indexed.db.close()

    def test_rust_native_skips_unrelated_shell_commands_before_parsing(self):
        class GraphStub:
            db = None

        records = {17: {'outputs': ['/tmp/out/object.o'], 'deps': []}}
        native = {17: {'command': "bash -c 'cat <<EOF\nopaque shell text\nEOF'"}}
        parser = cloud_host_runtime._source_read_invocations()
        with mock.patch.object(parser, 'iter_invocations', side_effect=AssertionError(
                'non-Rust command should not reach shell parsing')):
            report = cloud_host_runtime.derive_rust_native(
                GraphStub(), records, native, '/tmp/source', '/tmp/out')
        self.assertEqual(report, {
            'additions': {}, 'proofs': [], 'unresolved_native_archives': [],
        })


if __name__ == '__main__':
    unittest.main()
