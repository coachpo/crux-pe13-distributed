import importlib.util
import json
import copy
from pathlib import Path
import shutil
import sys
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
                                           "out_root": str(self.source / "out"),
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

    def test_source_python2_shebang_uses_verified_declared_runtime(self):
        interpreter = self.source / "prebuilts/build-tools/linux-x86/bin/py2-cmd"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text(f"#!{sys.executable}\nimport os,sys\nos.execv(sys.executable,[sys.executable,*sys.argv[1:]])\n")
        interpreter.chmod(0o755)
        source_script = self.source / "generated.py"
        source_script.write_text("#!/usr/bin/env python2\nfrom pathlib import Path\np=Path('out/result.bin')\np.parent.mkdir(parents=True,exist_ok=True)\np.write_bytes(b'python2 shebang resolved')\n")
        source_script.chmod(0o755)
        self.ninja.write_text(f"#!{sys.executable}\nimport subprocess\nsubprocess.run(['./generated.py'],check=True)\n")
        self.write_manifests()
        bundle = json.loads(self.bundle.read_text())
        bundle["files"] += [worker.describe(interpreter), worker.describe(source_script)]
        self.bundle.write_text(json.dumps(bundle))
        receipt = self.run_worker()
        self.assertEqual(receipt["status"], "success", receipt.get("error"))
        self.assertEqual(self.output.read_bytes(), b"python2 shebang resolved")
        self.assertEqual([item["name"] for item in receipt["runtime_aliases"]], ["python", "python2", "python2.7"])
        self.assertEqual(receipt["runtime_aliases"][0]["target_sha256"], worker.digest(interpreter))
        self.assertEqual(receipt["build_environment"]["PATH"].split(":")[0], str(self.source / ".crux-task/host-bin"))
        command = json.loads((self.root / "producer/command.json").read_text())
        self.assertEqual(command["runtime_aliases"], receipt["runtime_aliases"])

    def test_native_python_shebang_uses_frozen_runtime_and_preserves_python3(self):
        interpreter = self.source / "prebuilts/build-tools/linux-x86/bin/py2-cmd"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text(f"#!{sys.executable}\nimport os,sys\nfrom pathlib import Path\np=Path('out/interpreter-calls.txt')\np.parent.mkdir(parents=True,exist_ok=True)\np.open('a').write('frozen runtime\\n')\nos.execv(sys.executable,[sys.executable,*sys.argv[1:]])\n")
        interpreter.chmod(0o755)
        script = self.source / "native-script.py"
        script.write_text("#!/usr/bin/env python\nimport subprocess\nfrom pathlib import Path\nsubprocess.run(['python3','-c','print(\"python3 retained\")'],check=True)\nPath('out/result.bin').write_bytes(b'native python resolved')\n")
        script.chmod(0o755)
        self.ninja.write_text(f"#!{sys.executable}\nimport subprocess\nsubprocess.run(['./native-script.py'],check=True)\n")
        self.write_manifests()
        bundle = json.loads(self.bundle.read_text())
        bundle["files"] += [worker.describe(interpreter), worker.describe(script)]
        self.bundle.write_text(json.dumps(bundle))
        receipt = self.run_worker()
        self.assertEqual(receipt["status"], "success", receipt.get("error"))
        self.assertEqual(self.output.read_bytes(), b"native python resolved")
        self.assertEqual((self.source / "out/interpreter-calls.txt").read_text(), "frozen runtime\n")
        self.assertIn("python3 retained", (self.root / "producer/build.log").read_text())

    def test_conflicting_native_python_alias_fails_before_compile(self):
        interpreter = self.source / "prebuilts/build-tools/linux-x86/bin/py2-cmd"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text(f"#!{sys.executable}\nprint('declared frozen runtime')\n")
        interpreter.chmod(0o755)
        bundle = json.loads(self.bundle.read_text())
        bundle["files"].append(worker.describe(interpreter))
        self.bundle.write_text(json.dumps(bundle))
        directory = self.source / ".crux-task/host-bin"
        directory.mkdir()
        (directory / "python").symlink_to(sys.executable)
        receipt = self.run_worker()
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("conflicting Python 2 runtime alias", receipt["error"])
        self.assertFalse(self.output.exists())

    def test_native_command_receives_verified_external_output_context(self):
        actual_out = self.root / "external-android-out"
        self.ninja.write_text(f"#!{sys.executable}\nimport json,os\nfrom pathlib import Path\np=Path('out/result.bin')\np.parent.mkdir(parents=True,exist_ok=True)\np.write_text(json.dumps({{name:os.environ[name] for name in ('OUT_DIR','ANDROID_BUILD_TOP')}}))\n")
        self.write_manifests()
        bundle = json.loads(self.bundle.read_text())
        bundle["out_root"] = str(actual_out)
        self.bundle.write_text(json.dumps(bundle))
        receipt = self.run_worker()
        self.assertEqual(receipt["status"], "success", receipt.get("error"))
        captured = json.loads(self.output.read_text())
        self.assertEqual(captured, {"OUT_DIR": str(actual_out), "ANDROID_BUILD_TOP": str(self.source)})
        self.assertNotEqual(captured["OUT_DIR"], captured["ANDROID_BUILD_TOP"])
        self.assertEqual(receipt["out_root"], str(actual_out))
        command = json.loads((self.root / "producer/command.json").read_text())
        self.assertEqual(command["environment"]["OUT_DIR"], captured["OUT_DIR"])

    def test_explicit_output_context_must_match_frozen_capsule(self):
        manifest = json.loads(self.manifest.read_text())
        manifest["build_environment"] = {"OUT_DIR": str(self.root / "wrong-output")}
        self.manifest.write_text(json.dumps(manifest))
        bundle = json.loads(self.bundle.read_text())
        bundle["manifest_sha256"] = worker.digest(self.manifest)
        self.bundle.write_text(json.dumps(bundle))
        receipt = self.run_worker()
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("OUT_DIR differs", receipt["error"])
        self.assertFalse(self.output.exists())

    def test_default_dist_preserves_abi_diagnostics_and_compiler_failure(self):
        self.ninja.write_text(f"#!{sys.executable}\nimport os\nfrom pathlib import Path\np=Path(os.environ['DIST_DIR'])/'abidiffs'/'fixture.abidiff'\np.parent.mkdir(parents=True,exist_ok=True)\np.write_text('legitimate ABI change')\nraise SystemExit(7)\n")
        self.write_manifests()
        receipt = self.run_worker()
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["returncode"], 7)
        self.assertEqual(receipt["build_environment"]["DIST_DIR"], str(self.source / "out/dist"))
        self.assertEqual((self.source / "out/dist/abidiffs/fixture.abidiff").read_text(), "legitimate ABI change")
        self.assertEqual((self.root / "producer/abidiffs/fixture.abidiff").read_text(), "legitimate ABI change")
        self.assertEqual(receipt["abi_diagnostics"][0]["file"], "abidiffs/fixture.abidiff")
        self.assertFalse((self.root / "producer/outputs.tar.zst").exists())

    def test_captured_dist_directory_takes_precedence_over_default(self):
        custom = self.root / "captured-dist"
        manifest = json.loads(self.manifest.read_text())
        manifest["build_environment"] = {"DIST_DIR": str(custom)}
        self.manifest.write_text(json.dumps(manifest))
        bundle = json.loads(self.bundle.read_text())
        bundle["manifest_sha256"] = worker.digest(self.manifest)
        self.bundle.write_text(json.dumps(bundle))
        receipt = self.run_worker()
        self.assertEqual(receipt["status"], "success", receipt.get("error"))
        self.assertEqual(receipt["build_environment"]["DIST_DIR"], str(custom))

    def bound_producer(self):
        self.assertEqual(self.run_worker()["status"], "success")
        self.output.unlink()
        path = self.root / "producer/receipt.json"
        receipt = json.loads(path.read_text())
        receipt.update(run_id="301", worker_commit="a" * 40)
        path.write_text(json.dumps(receipt))
        return {"id": "producer", "run_id": 301, "artifact": "shard-producer",
                "expected_worker_commit": "a" * 40, "expected_manifest_sha256": receipt["manifest_sha256"]}

    def test_controller_bound_dependency_is_installed(self):
        binding = self.bound_producer()
        accepted, _ = worker.merge_dependencies([self.root / "producer"], [binding], self.source, self.source / "out")
        self.assertIn(str(self.output), accepted)
        self.assertTrue(self.output.exists())

    def test_dependency_identity_mismatch_never_installs_outputs(self):
        binding = self.bound_producer()
        for changes in ({"id": "other", "artifact": "shard-other"}, {"run_id": 302},
                        {"expected_worker_commit": "b" * 40}, {"expected_manifest_sha256": "f" * 64},
                        {"artifact": "shard-other"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                worker.merge_dependencies([self.root / "producer"], [{**binding, **changes}], self.source, self.source / "out")
            self.assertFalse(self.output.exists())

    def test_dependency_context_mismatch_never_installs_outputs(self):
        binding = self.bound_producer()
        path = self.root / "producer/receipt.json"
        original = json.loads(path.read_text())
        for field in ("source_root", "out_root"):
            receipt = {**original, field: str(self.root / "other-context")}
            path.write_text(json.dumps(receipt))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field + " differs"):
                worker.merge_dependencies([self.root / "producer"], [binding], self.source, self.source / "out")
            self.assertFalse(self.output.exists())

    def test_all_dependency_headers_are_preflighted_before_any_member(self):
        binding = self.bound_producer()
        second = self.root / "second-producer"
        shutil.copytree(self.root / "producer", second)
        receipt_path = second / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["run_id"] = "302"
        receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "run_id differs"):
            worker.merge_dependencies([self.root / "producer", second], [binding, copy.deepcopy(binding)],
                                      self.source, self.source / "out")
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
