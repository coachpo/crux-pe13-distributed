import importlib.util
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cloud_source_reads = load("cloud_source_reads", SCRIPTS / "cloud_source_reads.py")
graph = load("cloud_source_reads_graph", SCRIPTS / "graph.py")


class CloudSourceReadsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.out = self.root / "out"
        self.source.mkdir()
        self.out.mkdir()
        (self.source / "LICENSE root.txt").write_text("root license text")
        (self.source / "child/LICENSE child.txt").parent.mkdir(parents=True)
        (self.source / "child/LICENSE child.txt").write_text("child license text")
        self.config_dir = self.source / "proguard rules"
        (self.config_dir / "nested").mkdir(parents=True)
        (self.config_dir / "main.pro").write_text(
            "-include nested/child.pro\n@\"nested/other rules.pro\"\n-dontwarn example.**\n")
        (self.config_dir / "nested/child.pro").write_text('-include "deep rules.pro"\n')
        (self.config_dir / "nested/deep rules.pro").write_text("-keep class example.Deep { *; }\n")
        (self.config_dir / "nested/other rules.pro").write_text("-dontshrink\n")
        self.ninja = self.root / "build.ninja"
        self.write_ninja()
        self.database = self.root / "index.sqlite"
        graph.index_graph(self.ninja, self.source, self.database)
        self.index = graph.Graph(self.database)

    def tearDown(self):
        if hasattr(self, "index"):
            self.index.db.close()
        self.temporary.cleanup()

    def write_ninja(self, missing_include=False, preserve_configs=False, metadata_cycle=False,
                    root_basename=False, unknown_license_rsp=False, unknown_r8_rsp=False,
                    source_license_rsp=False, source_r8_rsp=False):
        root_dep = self.out / ("meta_lic" if root_basename else "root.meta_lic")
        child_dep = self.out / "child metadata.meta_lic"
        notice = self.out / "notice.txt"
        notice2 = self.out / "notice-copy.txt"
        r8 = self.out / "classes.jar"
        license_args = self.source / "license args.rsp"
        r8_args = self.source / "r8 args.rsp"
        if source_license_rsp:
            license_args.write_text(f'-n "LICENSE root.txt" -s "{self.out}/old compiled.bin" '
                                    f'-d "{self.out}/child metadata.meta_lic:static"')
        if source_r8_rsp:
            (self.config_dir / "extra.pro").write_text("-dontoptimize\n")
            r8_args.write_text(f'--pg-conf "{self.config_dir / "extra.pro"}"')
        nested_config = "missing.pro" if missing_include else "deep rules.pro"
        if not preserve_configs:
            (self.config_dir / "nested/child.pro").write_text(f'-include "{nested_config}"\n')
        lines = [
            "rule license_root",
            "  command = /tools/build_license_metadata -o $out @" +
            (str(root_dep) + ".untracked.rsp" if unknown_license_rsp else "$out.rsp"),
            "  rspfile = $out.rsp",
            (f'  rspfile_content = @"{license_args}"' if source_license_rsp else
             f'  rspfile_content = -n "LICENSE root.txt" -s "{self.out}/old compiled.bin" -d "{child_dep}:static"'),
            "rule license_child",
            "  command = /tools/build_license_metadata -o $out @$out.rsp",
            "  rspfile = $out.rsp",
            '  rspfile_content = -n "child/LICENSE child.txt" -s missing-source.bin' +
            (f' -d "{root_dep}:static"' if metadata_cycle else ""),
            "rule textnotice",
            "  command = /tools/textnotice -o " + str(notice) + " -d " + str(self.out / "notice.d") + " " + str(root_dep),
            "rule r8",
            "  command = java -cp /tools/r8.jar com.android.tools.r8.R8 --pg-conf \"" +
            str(self.config_dir / "main.pro") + "\" --pg-conf=" + str(self.out / "old.pro") +
            (" @" + str(self.out / "r8.untracked.rsp") if unknown_r8_rsp else
             " " + shlex.quote("@" + str(r8_args)) if source_r8_rsp else ""),
            f"build {str(child_dep).replace(' ', '$ ')}: license_child" +
            (f" {root_dep}" if metadata_cycle else ""),
            f"build {root_dep}: license_root {str(child_dep).replace(' ', '$ ')}",
            f"build {notice}: textnotice {root_dep}",
            f"build {notice2}: textnotice {root_dep}",
            f"build {r8}: r8 input/classes.jar",
        ]
        self.ninja.write_text("\n".join(lines) + "\n")

    def selected(self, outputs):
        producers = self.index.producers(outputs, directory_fallback=False)
        records, _ = self.index.edge_records(set(producers.values()))
        metadata = self.index.native_metadata(records)
        return records, metadata, producers

    def test_transitive_license_reader_collects_notice_texts_and_current_metadata_ids(self):
        notice = str(self.out / "notice.txt")
        records, native, producers = self.selected([notice])
        report = cloud_source_reads.derive_source_reads(
            self.index, records, native, str(self.source), str(self.out))
        notice_edge = producers[notice]
        details = next(item for item in report["notice_readers"] if item["edge"] == notice_edge)
        expected_texts = {str(self.source / "LICENSE root.txt"),
                          str(self.source / "child/LICENSE child.txt")}
        expected_metadata = {str(self.out / "root.meta_lic"), str(self.out / "child metadata.meta_lic")}
        self.assertEqual(set(details["license_texts"]), expected_texts)
        self.assertTrue(expected_metadata.issubset(set(report["additions"][notice_edge])))
        self.assertTrue(expected_texts.issubset(set(report["additions"][notice_edge])))
        current_writers = {self.index.producers([str(self.out / "root.meta_lic")], False)[str(self.out / "root.meta_lic")],
                           self.index.producers([str(self.out / "child metadata.meta_lic")], False)[str(self.out / "child metadata.meta_lic")]}
        self.assertEqual({item["edge"] for item in details["metadata_writers"]}, current_writers)
        self.assertNotIn(str(self.out / "old compiled.bin"), report["additions"][notice_edge])

    def test_shared_metadata_roots_are_batched_across_notice_consumers(self):
        notice = str(self.out / "notice.txt")
        notice2 = str(self.out / "notice-copy.txt")
        records, native, producers = self.selected([notice, notice2])
        original = self.index.native_metadata
        with mock.patch.object(self.index, "native_metadata", wraps=original) as exports:
            report = cloud_source_reads.derive_source_reads(
                self.index, records, native, str(self.source), str(self.out))
        self.assertEqual(set(producers) & {notice, notice2}, {notice, notice2})
        # One frontier export each for the root and nested metadata writers,
        # rather than one export per consumer/path.
        self.assertEqual(exports.call_count, 2)
        self.assertEqual(len(report["notice_readers"]), 2)

    def test_license_metadata_cycle_is_visited_without_rejection(self):
        self.write_ninja(metadata_cycle=True)
        self.database = self.root / "index-metadata-cycle.sqlite"
        graph.index_graph(self.ninja, self.source, self.database)
        self.index.db.close()
        self.index = graph.Graph(self.database)
        notice = str(self.out / "notice.txt")
        records, native, producers = self.selected([notice])
        report = cloud_source_reads.derive_source_reads(
            self.index, records, native, str(self.source), str(self.out))
        edge = producers[notice]
        self.assertEqual(set(report["additions"][edge]), {
            str(self.out / "root.meta_lic"), str(self.out / "child metadata.meta_lic"),
            str(self.source / "LICENSE root.txt"), str(self.source / "child/LICENSE child.txt")})

    def test_declared_metadata_named_meta_lic_is_a_notice_root(self):
        self.write_ninja(root_basename=True)
        self.database = self.root / "index-meta-basename.sqlite"
        graph.index_graph(self.ninja, self.source, self.database)
        self.index.db.close()
        self.index = graph.Graph(self.database)
        notice = str(self.out / "notice.txt")
        records, native, producers = self.selected([notice])
        report = cloud_source_reads.derive_source_reads(
            self.index, records, native, str(self.source), str(self.out))
        details = next(item for item in report["notice_readers"] if item["edge"] == producers[notice])
        self.assertIn(str(self.out / "meta_lic"), details["metadata_roots"])
        self.assertIn(str(self.source / "LICENSE root.txt"), report["additions"][producers[notice]])

    def test_metadata_writer_alone_does_not_read_license_texts_or_descriptive_sources(self):
        root_meta = str(self.out / "root.meta_lic")
        records, native, producers = self.selected([root_meta])
        report = cloud_source_reads.derive_source_reads(
            self.index, records, native, str(self.source), str(self.out))
        writer_edge = producers[root_meta]
        self.assertNotIn(writer_edge, report["additions"])

    def test_missing_license_text_without_notice_reader_is_not_opened(self):
        (self.source / "LICENSE root.txt").unlink()
        root_meta = str(self.out / "root.meta_lic")
        records, native, producers = self.selected([root_meta])
        report = cloud_source_reads.derive_source_reads(self.index, records, native,
                                                       str(self.source), str(self.out))
        self.assertNotIn(producers[root_meta], report["additions"])

    def test_unknown_metadata_writer_response_fails_only_when_notice_reads_it(self):
        self.write_ninja(unknown_license_rsp=True)
        self.database = self.root / "index-unknown-license-rsp.sqlite"
        graph.index_graph(self.ninja, self.source, self.database)
        self.index.db.close()
        self.index = graph.Graph(self.database)
        root_meta = str(self.out / "root.meta_lic")
        records, native, _ = self.selected([root_meta])
        # Selecting a writer without a notice consumer does not inspect its -n
        # payload (nor its unconsumed response).
        report = cloud_source_reads.derive_source_reads(
            self.index, records, native, str(self.source), str(self.out))
        self.assertEqual(report["additions"], {})

        notice = str(self.out / "notice.txt")
        records, native, _ = self.selected([notice])
        with self.assertRaisesRegex(cloud_source_reads.SourceReadError,
                                    "license metadata writer edge .* unresolved response input"):
            cloud_source_reads.derive_source_reads(self.index, records, native,
                                                   str(self.source), str(self.out))

    def test_source_root_license_response_is_expanded_and_reported_to_notice_reader(self):
        self.write_ninja(source_license_rsp=True)
        self.database = self.root / "index-source-license-rsp.sqlite"
        graph.index_graph(self.ninja, self.source, self.database)
        self.index.db.close()
        self.index = graph.Graph(self.database)
        notice = str(self.out / "notice.txt")
        records, native, producers = self.selected([notice])
        report = cloud_source_reads.derive_source_reads(
            self.index, records, native, str(self.source), str(self.out))
        edge = producers[notice]
        self.assertIn(str(self.source / "license args.rsp"), report["additions"][edge])
        self.assertIn(str(self.source / "LICENSE root.txt"), report["additions"][edge])
        details = next(item for item in report["notice_readers"] if item["edge"] == edge)
        self.assertIn(str(self.source / "license args.rsp"), details["response_files"])

    def test_missing_license_text_read_by_notice_is_an_error(self):
        (self.source / "LICENSE root.txt").unlink()
        notice = str(self.out / "notice.txt")
        records, native, _ = self.selected([notice])
        with self.assertRaisesRegex(cloud_source_reads.SourceReadError, "source-read file is missing"):
            cloud_source_reads.derive_source_reads(self.index, records, native,
                                                   str(self.source), str(self.out))

    def test_r8_resolves_recursive_includes_with_spaces_relative_to_including_file(self):
        output = str(self.out / "classes.jar")
        records, native, producers = self.selected([output])
        report = cloud_source_reads.derive_source_reads(
            self.index, records, native, str(self.source), str(self.out))
        edge = producers[output]
        expected = {
            str(self.config_dir / "main.pro"),
            str(self.config_dir / "nested/child.pro"),
            str(self.config_dir / "nested/deep rules.pro"),
            str(self.config_dir / "nested/other rules.pro"),
        }
        details = next(item for item in report["r8_embedded_includes"] if item["edge"] == edge)
        self.assertEqual(set(details["configs"]), expected)
        self.assertEqual(set(report["additions"][edge]), expected)
        self.assertNotIn(str(self.out / "old.pro"), report["additions"][edge])

    def test_missing_proguard_include_fails_instead_of_being_ignored(self):
        self.write_ninja(missing_include=True)
        self.database = self.root / "index-missing.sqlite"
        graph.index_graph(self.ninja, self.source, self.database)
        self.index.db.close()
        self.index = graph.Graph(self.database)
        output = str(self.out / "classes.jar")
        records, native, _ = self.selected([output])
        with self.assertRaisesRegex(cloud_source_reads.SourceReadError, "Proguard config is missing"):
            cloud_source_reads.derive_source_reads(self.index, records, native,
                                                   str(self.source), str(self.out))

    def test_r8_unknown_command_response_is_not_silently_ignored(self):
        self.write_ninja(unknown_r8_rsp=True)
        self.database = self.root / "index-unknown-r8-rsp.sqlite"
        graph.index_graph(self.ninja, self.source, self.database)
        self.index.db.close()
        self.index = graph.Graph(self.database)
        output = str(self.out / "classes.jar")
        records, native, _ = self.selected([output])
        with self.assertRaisesRegex(cloud_source_reads.SourceReadError,
                                    "R8 edge .* unresolved response input"):
            cloud_source_reads.derive_source_reads(self.index, records, native,
                                                   str(self.source), str(self.out))

    def test_source_root_r8_response_reveals_pg_conf_and_is_reported(self):
        self.write_ninja(source_r8_rsp=True)
        self.database = self.root / "index-source-r8-rsp.sqlite"
        graph.index_graph(self.ninja, self.source, self.database)
        self.index.db.close()
        self.index = graph.Graph(self.database)
        output = str(self.out / "classes.jar")
        records, native, producers = self.selected([output])
        report = cloud_source_reads.derive_source_reads(
            self.index, records, native, str(self.source), str(self.out))
        edge = producers[output]
        self.assertIn(str(self.source / "r8 args.rsp"), report["additions"][edge])
        self.assertIn(str(self.config_dir / "extra.pro"), report["additions"][edge])
        details = next(item for item in report["r8_embedded_includes"] if item["edge"] == edge)
        self.assertEqual(details["response_files"], [str(self.source / "r8 args.rsp")])

    def test_recursive_proguard_include_cycle_is_detected(self):
        first = self.config_dir / "main.pro"
        first.write_text("-include nested/child.pro\n")
        child = self.config_dir / "nested/child.pro"
        child.write_text("-include ../main.pro\n")
        self.write_ninja(preserve_configs=True)
        self.database = self.root / "index-cycle.sqlite"
        graph.index_graph(self.ninja, self.source, self.database)
        self.index.db.close()
        self.index = graph.Graph(self.database)
        output = str(self.out / "classes.jar")
        records, native, _ = self.selected([output])
        with self.assertRaisesRegex(cloud_source_reads.SourceReadError, "recursive R8 Proguard include"):
            cloud_source_reads.derive_source_reads(self.index, records, native,
                                                   str(self.source), str(self.out))


if __name__ == "__main__":
    unittest.main()
