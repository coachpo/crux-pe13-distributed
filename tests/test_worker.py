import importlib.util
import json
import copy
from pathlib import Path
import shutil
import sys
import io
import tarfile
import subprocess
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


@unittest.skipUnless(shutil.which("zstd"), "zstd is required for dependency archives")
class SupersessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"; self.source.mkdir()
        self.out = self.source / "out"; self.out.mkdir()
        self.metadata = self.out / "metadata.txt"
        self.number = self.out / "number.txt"
        self.library = self.out / "unaffected.so"
        self.old = self.producer("old-fixture", 401, {self.metadata: b"old-bad-metadata", self.number: b"", self.library: b"unaffected library"})
        self.new = self.producer("replacement-fixture", 402, {self.metadata: b"new-good-metadata", self.number: b"1791434921"})
        self.declarations = [self.declaration(path) for path in (self.metadata, self.number)]

    def tearDown(self):
        self.temporary.cleanup()

    def producer(self, identity, run, contents):
        directory = self.root / identity; directory.mkdir()
        specs = []
        for path, data in contents.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data); path.chmod(0o755 if path == self.library else 0o664)
            specs.append(worker.describe(path))
        archive = directory / "outputs.tar.zst"; worker.pack_outputs(specs, archive)
        manifest = str(run % 10) * 64
        receipt = {"schema_version": 1, "status": "success", "id": identity, "run_id": str(run),
                   "worker_commit": "a" * 40, "manifest_sha256": manifest,
                   "source_root": str(self.source), "out_root": str(self.out),
                   "outputs": specs, "archive_sha256": worker.digest(archive)}
        (directory / "receipt.json").write_text(json.dumps(receipt))
        for path in contents: path.unlink()
        return {"directory": directory, "receipt": receipt,
                "binding": {"id": identity, "run_id": run, "artifact": "shard-" + identity,
                            "expected_worker_commit": "a" * 40, "expected_manifest_sha256": manifest}}

    def endpoint(self, producer, path):
        return {"binding": copy.deepcopy(producer["binding"]),
                "receipt_sha256": worker.digest(producer["directory"] / "receipt.json"),
                "member": copy.deepcopy(next(spec for spec in producer["receipt"]["outputs"] if spec["path"] == str(path)))}

    def declaration(self, path):
        return {"approved": True, "path": str(path), "old": self.endpoint(self.old, path),
                "new": self.endpoint(self.new, path), "reason": "explicit fixture frontend correction"}

    def merge(self, producers=None, declarations=None, audit=None, forbidden=()):
        producers = producers or [self.old, self.new]
        return worker.merge_dependencies([p["directory"] for p in producers], [p["binding"] for p in producers],
                                         self.source, self.out, self.declarations if declarations is None else declarations,
                                         audit, forbidden)

    def clear_installed(self):
        for path in (self.metadata, self.number, self.library): path.unlink(missing_ok=True)

    def replace_old_tar(self, records):
        archive = self.old["directory"] / "outputs.tar.zst"
        with archive.open("wb") as output:
            process = subprocess.Popen(["zstd", "-q", "-c"], stdin=subprocess.PIPE, stdout=output)
            with tarfile.open(fileobj=process.stdin, mode="w|") as stream:
                for name, kind, data, mode in records:
                    member = tarfile.TarInfo(name)
                    member.mode = mode
                    if kind == "file":
                        member.size = len(data); stream.addfile(member, io.BytesIO(data))
                    elif kind == "symlink":
                        member.type = tarfile.SYMTYPE; member.linkname = "outside"; stream.addfile(member)
            process.stdin.close(); self.assertEqual(process.wait(), 0)
        self.old["receipt"]["archive_sha256"] = worker.digest(archive)
        (self.old["directory"] / "receipt.json").write_text(json.dumps(self.old["receipt"]))
        for declaration in self.declarations:
            declaration["old"]["receipt_sha256"] = worker.digest(self.old["directory"] / "receipt.json")

    def old_records(self):
        return [(str(path).lstrip("/"), "file", data, 0o755 if path == self.library else 0o664)
                for path, data in ((self.metadata, b"old-bad-metadata"), (self.number, b""), (self.library, b"unaffected library"))]

    def test_winner_and_unaffected_library_are_preserved_in_both_orders(self):
        for producers in ([self.old, self.new], [self.new, self.old]):
            with self.subTest(order=[p["binding"]["id"] for p in producers]):
                audit = []
                accepted, receipts = self.merge(producers, audit=audit)
                self.assertEqual(self.metadata.read_bytes(), b"new-good-metadata")
                self.assertEqual(self.number.read_bytes(), b"1791434921")
                self.assertEqual(self.library.read_bytes(), b"unaffected library")
                self.assertEqual(self.library.stat().st_mode & 0o7777, 0o755)
                self.assertEqual(accepted[str(self.metadata)], self.declarations[0]["new"]["member"])
                self.assertEqual(len(receipts), 2)
                self.assertTrue(all(item["result"] == "materialized" and item["old"]["integrity_verified"]
                                    and item["new"]["integrity_verified"] for item in audit))
                self.clear_installed()

    def test_excluded_old_byte_corruption_is_checked_after_outer_digest_passes(self):
        records = self.old_records(); records[0] = (records[0][0], "file", b"OLD-bad-metadata", 0o664)
        self.replace_old_tar(records)
        with self.assertRaisesRegex(ValueError, "member integrity mismatch"):
            self.merge([self.new, self.old])

    def test_excluded_old_type_mode_size_missing_and_traversal_are_checked(self):
        for kind in ("type", "mode", "size", "missing", "traversal"):
            records = self.old_records()
            if kind == "type": records[0] = (records[0][0], "symlink", b"", 0o664)
            elif kind == "mode": records[0] = (records[0][0], "file", records[0][2], 0o644)
            elif kind == "size": records[0] = (records[0][0], "file", b"short", 0o664)
            elif kind == "missing": records = records[1:]
            else: records[0] = ("../../outside", "file", records[0][2], 0o664)
            self.replace_old_tar(records)
            with self.subTest(kind=kind), self.assertRaises(ValueError): self.merge()
            self.clear_installed()

    def test_missing_failed_or_misbound_replacement_never_falls_back(self):
        with self.assertRaisesRegex(ValueError, "exactly once"):
            self.merge([self.old])
        self.assertFalse(self.library.exists())
        receipt_path = self.new["directory"] / "receipt.json"
        original = receipt_path.read_text()
        receipt = json.loads(original); receipt["status"] = "failed"; receipt_path.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "did not complete"):
            self.merge()
        receipt_path.write_text(original)
        self.assertFalse(self.library.exists())
        declaration = copy.deepcopy(self.declarations)
        declaration[0]["new"]["binding"]["run_id"] = 999
        with self.assertRaisesRegex(ValueError, "exactly once"):
            self.merge(declarations=declaration)
        self.assertFalse(self.library.exists())

    def test_wrong_receipt_pin_and_member_spec_are_rejected_before_install(self):
        for field in ("receipt", "member"):
            declarations = copy.deepcopy(self.declarations)
            if field == "receipt": declarations[0]["new"]["receipt_sha256"] = "f" * 64
            else: declarations[0]["new"]["member"]["sha256"] = "f" * 64
            with self.subTest(field=field), self.assertRaises(ValueError): self.merge(declarations=declarations)
            self.assertFalse(self.library.exists())

    def test_unlisted_conflict_and_undeclared_third_provider_are_rejected(self):
        third = self.producer("third-fixture", 403, {self.metadata: b"new-good-metadata"})
        with self.assertRaisesRegex(ValueError, "undeclared producer"):
            self.merge([self.old, self.new, third])
        self.assertFalse(self.library.exists())
        with self.assertRaisesRegex(ValueError, "conflicting dependency"):
            self.merge(declarations=[])
        self.assertFalse(self.library.exists())

    def test_duplicate_winner_or_unapproved_nonregular_path_is_rejected(self):
        duplicate = copy.deepcopy(self.declarations) + [copy.deepcopy(self.declarations[0])]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.merge(declarations=duplicate)
        for update in ({"approved": None}, {"approved": 1}, {"path": str(self.out / "*")}, {"path": str(self.root / "outside")}):
            declarations = copy.deepcopy(self.declarations); declarations[0].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError): self.merge(declarations=declarations)
        self.assertFalse(self.library.exists())

    def test_ambient_destination_is_rejected_even_when_equal_to_winner(self):
        self.metadata.write_bytes(b"new-good-metadata"); self.metadata.chmod(0o664)
        with self.assertRaisesRegex(ValueError, "already exists before merge"):
            self.merge()
        self.assertFalse(self.library.exists())

    def test_all_archive_digests_are_checked_before_any_installation(self):
        with (self.new["directory"] / "outputs.tar.zst").open("ab") as stream: stream.write(b"corrupt")
        with self.assertRaisesRegex(ValueError, "archive integrity mismatch"):
            self.merge()
        self.assertFalse(self.library.exists())

    def test_a_replacement_actor_cannot_import_its_own_output_seed(self):
        with self.assertRaisesRegex(ValueError, "declared consumer output"):
            self.merge(forbidden=[str(self.metadata)])
        self.assertFalse(self.library.exists())

    def test_excluded_archive_decompression_and_trailing_data_are_validated(self):
        archive = self.old["directory"] / "outputs.tar.zst"
        with archive.open("ab") as output: output.write(b"not a zstd frame")
        self.old["receipt"]["archive_sha256"] = worker.digest(archive)
        (self.old["directory"] / "receipt.json").write_text(json.dumps(self.old["receipt"]))
        for declaration in self.declarations:
            declaration["old"]["receipt_sha256"] = worker.digest(self.old["directory"] / "receipt.json")
        with self.assertRaisesRegex(ValueError, "decompression failed"):
            self.merge([self.new, self.old])

    def test_consumer_receipt_records_verified_supersession_lineage(self):
        ninja = self.source / "ninja"
        destination = self.out / "consumer.bin"
        ninja.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(destination)!r}).write_bytes(Path({str(self.number)!r}).read_bytes())\n")
        ninja.chmod(0o755)
        graph = self.source / "build.ninja"; graph.write_text("# fixture selected graph\n")
        manifest = self.source / "manifest.json"
        manifest.write_text(json.dumps({"schema_version": 1, "source_root": str(self.source), "ninja": graph.name,
                                       "targets": [str(destination)], "outputs": [str(destination)], "edge_count": 1,
                                       "leaf_inputs": [str(ninja)], "external_inputs": [str(self.metadata), str(self.number), str(self.library)]}))
        bundle = self.source / "bundle.json"
        bundle.write_text(json.dumps({"schema_version": 1, "source_root": str(self.source), "out_root": str(self.out),
                                     "manifest_sha256": worker.digest(manifest), "files": [worker.describe(ninja), worker.describe(graph)]}))
        receipt = worker.run_shard(manifest, bundle, "consumer-fixture", self.root / "result",
                                   [self.old["directory"], self.new["directory"]], ninja, 1,
                                   [self.old["binding"], self.new["binding"]], self.declarations)
        self.assertEqual(receipt["status"], "success", receipt.get("error"))
        self.assertEqual(destination.read_bytes(), b"1791434921")
        self.assertEqual(len(receipt["dependencies"]), 2)
        applied = receipt["dependency_supersessions"]
        self.assertEqual(applied[0]["old"]["binding"], self.old["binding"])
        self.assertEqual(applied[0]["new"]["binding"], self.new["binding"])
        self.assertEqual(applied[0]["reason"], self.declarations[0]["reason"])
        self.assertTrue(all(record["result"] == "materialized" for record in applied))


if __name__ == "__main__":
    unittest.main()
