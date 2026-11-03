import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("cloud_index", SCRIPTS / "cloud_index.py")
cloud_index = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_index)


class CloudIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.build_number = "runner-20261103"
        self.graph_run = "41677764699"
        self.source_root = "/home/runner/work/pe13"
        self.manifest = self.root / "source-manifest.json"
        self.manifest.write_text(json.dumps({"schema_version": 1,
                                             "source_root": self.source_root,
                                             "projects": []}, sort_keys=True) + "\n")
        self.out_root = self.root / "android-out"
        self.out_root.mkdir()
        self.write_graph()
        self.graph_evidence = self.root / "evidence"
        (self.graph_evidence / "graph").mkdir(parents=True)
        self.write_graph_receipt()
        self.output = self.root / "index-output"
        self.output.mkdir()
        (self.output / "workflow.log").write_text("existing log is allowed")

    def tearDown(self):
        self.temporary.cleanup()

    def write_graph(self, targets=None):
        targets = targets or cloud_index.TARGETS
        combined = self.out_root / "combined-aosp_crux.ninja"
        lines = ["rule action", "  command = touch $out", "build bootimage.bin: action input/boot"]
        if "bootimage" in targets:
            lines.append("build bootimage: phony bootimage.bin")
        lines.append("subninja " + str(self.out_root / "nested.ninja"))
        combined.write_text("\n".join(lines) + "\n")
        nested = ["build recoveryimage.bin: action input/recovery"]
        if "recoveryimage" in targets:
            nested.append("build recoveryimage: phony recoveryimage.bin")
        nested.append("build systemimage.bin: action input/system")
        if "systemimage" in targets:
            nested.append("build systemimage: phony systemimage.bin")
        nested.append("build vendorimage.bin: action input/vendor")
        if "vendorimage" in targets:
            nested.append("build vendorimage: phony vendorimage.bin")
        (self.out_root / "nested.ninja").write_text("\n".join(nested) + "\n")
        for relative in cloud_index.GRAPH_FILES:
            path = self.out_root / relative
            if path != combined:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("graph artifact " + relative + "\n")

    def write_graph_receipt(self, **overrides):
        graph_files = []
        for relative in cloud_index.GRAPH_FILES:
            path = self.out_root / relative
            graph_files.append({"path": str(path), "size": path.stat().st_size,
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        receipt = {
            "schema_version": 1,
            "status": "success",
            "source_root": self.source_root,
            "out_root": str(self.out_root),
            "build_number": self.build_number,
            "source_manifest": {"sha256": hashlib.sha256(self.manifest.read_bytes()).hexdigest()},
            "graph_files": graph_files,
        }
        receipt.update(overrides)
        (self.graph_evidence / "graph/graph-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")

    def invoke(self):
        return cloud_index.main(["--graph-evidence", str(self.graph_evidence),
                                 "--source-manifest", str(self.manifest),
                                 "--build-number", self.build_number,
                                 "--graph-run", self.graph_run,
                                 "--output", str(self.output)])

    def test_indexes_verified_nested_graph_and_records_targets_and_rowcounts(self):
        self.assertEqual(self.invoke(), 0)
        result = json.loads((self.output / "index-result.json").read_text())
        receipt = json.loads((self.output / "index-receipt.json").read_text())
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["graph_run"], self.graph_run)
        self.assertEqual(receipt["build_number"], self.build_number)
        self.assertEqual(receipt["parser_sha256"], cloud_index.sha256(cloud_index.GRAPH_PARSER))
        self.assertEqual(result["edge_count"], receipt["row_counts"]["edges"])
        self.assertEqual(set(receipt["target_producers"]), set(cloud_index.TARGETS))
        self.assertEqual(receipt["row_counts"]["files"], 2)
        self.assertGreater(receipt["row_counts"]["edges"], 0)
        self.assertEqual(receipt["database_sha256"], cloud_index.sha256(self.output / "index.sqlite"))
        self.assertEqual((self.output / "workflow.log").read_text(), "existing log is allowed")
        with sqlite3.connect(self.output / "index.sqlite") as connection:
            nested = connection.execute("SELECT COUNT(*) FROM files WHERE path=?",
                                        (str((self.out_root / "nested.ninja").resolve()),)).fetchone()[0]
        self.assertEqual(nested, 1)

    def test_missing_target_is_rejected_after_indexing(self):
        self.write_graph(targets={"bootimage", "recoveryimage", "vendorimage"})
        self.write_graph_receipt()
        self.assertEqual(self.invoke(), 1)
        receipt = json.loads((self.output / "index-receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("systemimage", receipt["error"])
        self.assertTrue((self.output / "index-result.json").is_file())

    def test_corrupt_graph_digest_is_rejected_before_indexing(self):
        receipt_path = self.graph_evidence / "graph/graph-receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["graph_files"][0]["sha256"] = "0" * 64
        receipt_path.write_text(json.dumps(receipt))
        self.assertEqual(self.invoke(), 1)
        failed = json.loads((self.output / "index-receipt.json").read_text())
        self.assertEqual(failed["status"], "failed")
        self.assertIn("SHA-256", failed["error"])
        self.assertFalse((self.output / "index.sqlite").exists())

    def test_stale_database_is_rejected_without_reuse_or_overwrite(self):
        database = self.output / "index.sqlite"
        database.write_bytes(b"stale database")
        self.assertEqual(self.invoke(), 1)
        failed = json.loads((self.output / "index-receipt.json").read_text())
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Stale SQLite index", failed["error"])
        self.assertEqual(database.read_bytes(), b"stale database")

    def test_wrong_build_or_source_manifest_is_rejected(self):
        self.write_graph_receipt(build_number="different-build")
        self.assertEqual(self.invoke(), 1)
        receipt = json.loads((self.output / "index-receipt.json").read_text())
        self.assertIn("build number", receipt["error"])

    def test_wrong_manifest_sha_is_rejected(self):
        self.write_graph_receipt(source_manifest={"sha256": "0" * 64})
        self.assertEqual(self.invoke(), 1)
        receipt = json.loads((self.output / "index-receipt.json").read_text())
        self.assertIn("different source manifest", receipt["error"])
        self.assertFalse((self.output / "index.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
