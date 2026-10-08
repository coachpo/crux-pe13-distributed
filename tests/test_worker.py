import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest


module_spec = importlib.util.spec_from_file_location("worker", Path(__file__).parents[1] / "scripts/worker.py")
worker = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(worker)


@unittest.skipUnless(shutil.which("zstd"), "zstd is required for artifact exchange")
class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.slice = self.source / ".crux-task/graph"
        self.slice.mkdir(parents=True)
        self.input = self.source / "source.txt"
        self.input.write_text("cold source\n")
        self.ninja = self.source / "ninja"
        self.ninja.write_text("#!/usr/bin/env python3\nfrom pathlib import Path\np=Path('out/result.bin')\np.parent.mkdir(parents=True,exist_ok=True)\np.write_bytes(Path('source.txt').read_bytes()+b'compiled\\n')\n")
        self.ninja.chmod(0o755)
        self.ninja_file = self.slice / "build.ninja"
        self.ninja_file.write_text("# smoke graph\n")
        self.manifest = self.slice / "manifest.json"
        self.bundle = self.slice / "bundle.json"
        self.output = self.source / "out/result.bin"
        self.write_manifests()

    def tearDown(self):
        self.temporary.cleanup()

    def write_manifests(self, external=()):
        self.manifest.write_text(json.dumps({"schema_version": 1, "source_root": str(self.source),
                                             "ninja": "build.ninja", "targets": ["out/result.bin"],
                                             "edge_count": 1, "outputs": ["out/result.bin"],
                                             "leaf_inputs": ["source.txt"], "external_inputs": list(external)}))
        self.bundle.write_text(json.dumps({"schema_version": 1, "source_root": str(self.source),
                                           "manifest_sha256": worker.digest(self.manifest),
                                           "files": [worker.describe(path) for path in
                                                     (self.input, self.ninja, self.ninja_file)]}))

    def run_worker(self, name="producer", dependencies=()):
        return worker.run_shard(self.manifest, self.bundle, name, self.root / name,
                                dependencies, self.ninja)

    def test_cold_compile_and_verified_exchange_preserves_bytes_and_mode(self):
        receipt = self.run_worker()
        self.assertEqual(receipt["status"], "success", receipt.get("error"))
        expected = self.output.read_bytes()
        mode = self.output.stat().st_mode & 0o7777
        self.output.unlink()
        accepted, receipts = worker.merge_dependencies([self.root / "producer"])
        self.assertEqual(self.output.read_bytes(), expected)
        self.assertEqual(self.output.stat().st_mode & 0o7777, mode)
        self.assertIn(str(self.output), accepted)
        self.assertEqual(receipts[0]["id"], "producer")
        self.assertEqual(receipt["command"][3:5], ["-j", "4"])

    def test_missing_external_dependency_fails_before_compile(self):
        self.write_manifests(["out/upstream.bin"])
        receipt = self.run_worker()
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("no producer receipt", receipt["error"])
        self.assertFalse(self.output.exists())
        self.assertFalse((self.root / "producer/outputs.tar.zst").exists())
        self.assertTrue((self.root / "producer/build.log").exists())

    def test_tampered_source_fails_before_compile(self):
        self.input.write_text("different source\n")
        receipt = self.run_worker()
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("input integrity mismatch", receipt["error"])
        self.assertFalse(self.output.exists())

    def test_existing_compiled_output_is_rejected(self):
        self.output.parent.mkdir()
        self.output.write_text("seed\n")
        receipt = self.run_worker()
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("pre-existing output", receipt["error"])
        self.assertEqual(self.output.read_text(), "seed\n")

    def test_tampered_artifact_bytes_are_rejected(self):
        self.assertEqual(self.run_worker()["status"], "success")
        self.output.unlink()
        archive = self.root / "producer/outputs.tar.zst"
        with archive.open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "archive integrity mismatch"):
            worker.merge_dependencies([self.root / "producer"])
        self.assertFalse(self.output.exists())

    def test_wrong_output_hash_cannot_install_an_artifact(self):
        self.assertEqual(self.run_worker()["status"], "success")
        self.output.unlink()
        receipt_path = self.root / "producer/receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["outputs"][0]["sha256"] = "0" * 64
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "member integrity mismatch"):
            worker.merge_dependencies([self.root / "producer"])
        self.assertFalse(self.output.exists())

    def test_archive_traversal_is_rejected(self):
        for value in ["/etc/passwd", "home/../../etc/passwd", "../outside"]:
            with self.assertRaises(ValueError):
                worker.archive_path(value)

    def test_internal_outputs_are_verified_without_being_exported(self):
        manifest = json.loads(self.manifest.read_text())
        manifest["export_outputs"] = []
        manifest["build_environment"] = {"ANDROID_BUILD_TOP": str(self.source)}
        self.manifest.write_text(json.dumps(manifest))
        bundle = json.loads(self.bundle.read_text())
        bundle["manifest_sha256"] = worker.digest(self.manifest)
        self.bundle.write_text(json.dumps(bundle))
        receipt = self.run_worker()
        self.assertEqual(receipt["status"], "success", receipt.get("error"))
        self.assertEqual(receipt["verified_output_count"], 1)
        self.assertEqual(receipt["outputs"], [])
        self.assertTrue(self.output.exists())
        self.assertEqual(receipt["build_environment"]["ANDROID_BUILD_TOP"], str(self.source))

    def test_ninja_failure_retains_log_without_publishing_outputs(self):
        self.ninja.write_text("#!/usr/bin/env python3\nprint('compiler failure')\nraise SystemExit(2)\n")
        self.write_manifests()
        receipt = self.run_worker()
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["returncode"], 2)
        self.assertIn("compiler failure", (self.root / "producer/build.log").read_text())
        self.assertFalse((self.root / "producer/outputs.tar.zst").exists())


if __name__ == "__main__":
    unittest.main()
