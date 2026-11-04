import hashlib
import importlib.util
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
spec = importlib.util.spec_from_file_location("cloud_compile", SCRIPTS / "cloud_compile.py")
cloud_compile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_compile)


class CloudCompileTests(unittest.TestCase):
    def test_makeflags_source_alias_has_command_line_precedence_without_trace(self):
        make = shutil.which('make')
        if not make or 'GNU Make' not in subprocess.run([make, '--version'], capture_output=True, text=True).stdout:
            self.skipTest('GNU Make is required for the variable-precedence fixture')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'policy.mk'
            path.write_text('srctree := ignored\nall:\n\t@printf "%s|%s\\n" "$(srctree)" "$(origin srctree)"\n')
            result = subprocess.run([make, '--no-print-directory', '-f', str(path)],
                                    env={**os.environ, 'MAKEFLAGS': 'srctree=/k'},
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, '/k|command line\n')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.out_root = self.root / "android-out"
        self.context_path = self.root / "context.json"
        self.archive = b"accepted cold capsule archive"
        self.archive_name = "w000-s000.tar.zst"
        self.shard_id = "w000-s000"
        self.manifest_bytes = b'{"schema_version":1,"source_root":"' + str(self.source).encode() + b'"}\n'
        self.manifest_sha = hashlib.sha256(self.manifest_bytes).hexdigest()
        self.pack_run = "41779197896"
        self.shard_run = "41769467686"
        self.graph_index = {"bytes": 1234, "sha256": "b" * 64}
        self.fresh_context = {
            "schema_version": 1,
            "execution_id": "50000000001",
            "worker_commit": "a" * 40,
            "repository": "owner/repository",
            "source_root": str(self.source),
            "out_root": str(self.out_root),
            "pack_run": self.pack_run,
            "shard_run": self.shard_run,
            "build_number": "2026110303",
            "graph_run": "41677764699",
            "index_run": "41685367061",
            "source_run": "41677764699",
            "source_manifest_sha256": "c" * 64,
            "plan_sha256": "d" * 64,
            "graph_index": dict(self.graph_index),
            "waves": [[self.shard_id]],
            "tasks": {self.shard_id: {"id": self.shard_id, "wave": 0, "depends_on": [],
                                       "manifest_sha256": self.manifest_sha,
                                       "archive": self.archive_name,
                                       "archive_bytes": len(self.archive),
                                       "archive_sha256": hashlib.sha256(self.archive).hexdigest()}},
            "assembly_exports": {}, "frontend": {},
        }
        self.context_path.write_text(json.dumps(self.fresh_context, sort_keys=True) + "\n")
        self.output = self.root / "result"
        self.staging = self.root / "staging"
        self.downloads = []
        self.worker_calls = []
        self.active_dependency_context = self.fresh_context
        self.runner_env = {"GITHUB_RUN_ID": self.fresh_context["execution_id"],
                           "GITHUB_RUN_ATTEMPT": "1",
                           "GITHUB_SHA": self.fresh_context["worker_commit"],
                           "GITHUB_REPOSITORY": self.fresh_context["repository"],
                           "GH_TOKEN": "artifact-read-token", "GITHUB_TOKEN": "also-secret"}

    def tearDown(self):
        self.temporary.cleanup()

    def identity(self):
        return {field: self.fresh_context[field]
                for field in cloud_compile.FRESH_BUILD_FIELDS}

    def extracted_capsule(self):
        slice_dir = self.source / ".crux-task/graph"
        slice_dir.mkdir(parents=True)
        (self.source / cloud_compile.SHORT_KERNEL_SOURCE_PROJECT).mkdir(parents=True, exist_ok=True)
        (slice_dir / "manifest.json").write_bytes(self.manifest_bytes)
        (slice_dir / "bundle.json").write_text(json.dumps({
            "schema_version": 1, "source_root": str(self.source), "out_root": str(self.out_root),
            "manifest_sha256": self.manifest_sha, "allowed_generated_inputs": [], "files": [],
        }))

    def fake_download(self, run, repository, destination, token=None, name=None, pattern=None):
        self.downloads.append({"run": str(run), "repository": repository, "name": name,
                               "token": token, "destination": str(destination)})
        target = Path(destination)
        target.mkdir(parents=True, exist_ok=True)
        if name == "cloud-capsule-" + self.shard_id:
            archive_path = target / self.fresh_context["tasks"][self.shard_id]["archive"]
            archive_path.write_bytes(self.archive)
            return [{"name": name, "id": 701, "destination": str(target)}]
        dependency_id = name.removeprefix("shard-")
        dependency_context = {field: self.active_dependency_context[field]
                              for field in cloud_compile.FRESH_BUILD_FIELDS}
        task = self.active_dependency_context["tasks"][dependency_id]
        exported_paths = task.get("extra_exports", [])
        receipt = {"schema_version": 1, "id": dependency_id, "status": "success",
                   "run_id": self.active_dependency_context["execution_id"],
                   "run_attempt": "1", "worker_commit": self.active_dependency_context["worker_commit"],
                   "manifest_sha256": task["manifest_sha256"],
                   "source_root": self.fresh_context["source_root"],
                   "out_root": self.fresh_context["out_root"],
                   "fresh_build": dependency_context,
                   "additional_exports": list(exported_paths),
                   "outputs": [{"path": path, "type": "file", "mode": 0o644,
                                "size": 4, "sha256": "9" * 64} for path in exported_paths],
                   "archive_sha256": hashlib.sha256(b"dep archive").hexdigest()}
        (target / "receipt.json").write_text(json.dumps(receipt))
        (target / "outputs.tar.zst").write_bytes(b"dep archive")
        return [{"name": name, "id": 800, "destination": str(target)}]

    def fake_tar(self, command, check):
        if command[1] == "tar":
            self.assertEqual(command[:5], ["sudo", "tar", "--zstd", "--no-same-owner", "-xpf"])
            self.assertIn("-C", command)
            self.assertEqual(command[-1], "/")
            self.extracted_capsule()
        else:
            self.assertEqual(command[:2], ["sudo", "chown"])
        return subprocess.CompletedProcess(command, 0)

    def fake_worker(self, manifest_path, bundle_path, shard_id, output_dir,
                    dependency_dirs, jobs, dependency_bindings, extra_exports=(), prebuild_targets=()):
        self.worker_calls.append({"manifest": Path(manifest_path), "bundle": Path(bundle_path),
                                  "id": shard_id, "dependency_dirs": list(dependency_dirs),
                                  "jobs": jobs, "bindings": dependency_bindings,
                                  "extra_exports": list(extra_exports),
                                  "prebuild_targets": list(prebuild_targets),
                                  "makeflags": os.environ.get("MAKEFLAGS", "")})
        self.assertNotIn("GH_TOKEN", os.environ)
        self.assertNotIn("GITHUB_TOKEN", os.environ)
        manifest = json.loads(Path(manifest_path).read_text())
        receipt = {"schema_version": 1, "id": shard_id, "status": "success",
                   "run_id": self.fresh_context["execution_id"],
                   "run_attempt": "1", "worker_commit": self.fresh_context["worker_commit"],
                   "manifest_sha256": hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest(),
                   "source_root": manifest["source_root"], "out_root": self.fresh_context["out_root"],
                   "outputs": [], "additional_exports": list(extra_exports),
                   "prebuild_targets": list(prebuild_targets),
                   "archive_sha256": hashlib.sha256(b"fresh outputs").hexdigest()}
        Path(output_dir, "receipt.json").write_text(json.dumps(receipt))
        return receipt

    def run_compile(self, diagnostic_context_path=None, short_kernel_source=False):
        with mock.patch.dict(os.environ, self.runner_env, clear=True), \
                mock.patch.object(cloud_compile.cloud_artifacts, "download_artifacts", side_effect=self.fake_download), \
                mock.patch.object(cloud_compile.subprocess, "run", side_effect=self.fake_tar), \
                mock.patch.object(cloud_compile.worker, "run_shard", side_effect=self.fake_worker), \
                mock.patch.object(cloud_compile, "_diagnostic_process_limits", return_value={"arg_max": "2097152"}), \
                mock.patch.object(cloud_compile, "_mount_short_source_alias",
                                  return_value={"source": str(self.source / cloud_compile.SHORT_KERNEL_SOURCE_PROJECT),
                                                "alias": "/k", "mount": "bind"}) as mount, \
                mock.patch.object(cloud_compile, "_unmount_short_source_alias") as unmount:
            self.mount_calls = mount
            self.unmount_calls = unmount
            return cloud_compile.compile_shard(self.context_path, self.shard_id,
                                                self.output, self.staging,
                                                diagnostic_context_path=diagnostic_context_path,
                                                short_kernel_source=short_kernel_source)

    def test_fresh_capsule_worker_records_context_and_disables_auth_in_build_child(self):
        code, receipt = self.run_compile()
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["fresh_build"], self.identity())
        self.assertEqual(self.downloads[0]["run"], self.pack_run)
        self.assertEqual(self.downloads[0]["name"], "cloud-capsule-" + self.shard_id)
        self.assertEqual(self.worker_calls[0]["jobs"], 4)
        self.assertEqual(self.worker_calls[0]["bindings"], [])
        self.assertEqual(self.worker_calls[0]["prebuild_targets"], [])
        self.assertEqual(receipt["admitted_local_runtime_targets"], [])
        self.assertEqual(json.loads((self.output / "receipt.json").read_text())["fresh_build"], self.identity())
        self.assertFalse((self.staging / "capsule").exists())

    def test_dependency_must_come_from_current_fresh_execution_and_bind_manifest_commit(self):
        dependency_id = "w000-s000"
        consumer_id = "w001-s000"
        self.fresh_context["waves"] = [[dependency_id], [consumer_id]]
        self.fresh_context["tasks"] = {
            dependency_id: self.fresh_context["tasks"][dependency_id],
            consumer_id: {"id": consumer_id, "wave": 1, "depends_on": [dependency_id],
                          "manifest_sha256": "e" * 64, "archive": consumer_id + ".tar.zst",
                          "archive_bytes": 10, "archive_sha256": "f" * 64},
        }
        self.context_path.write_text(json.dumps(self.fresh_context))
        self.shard_id = consumer_id
        self.archive = b"consumer capsule"
        self.fresh_context["tasks"][consumer_id]["archive_bytes"] = len(self.archive)
        self.fresh_context["tasks"][consumer_id]["archive_sha256"] = hashlib.sha256(self.archive).hexdigest()
        self.context_path.write_text(json.dumps(self.fresh_context))
        self.manifest_bytes = (b'{"schema_version":1,"source_root":"' + str(self.source).encode() + b'"}\n')
        self.manifest_sha = hashlib.sha256(self.manifest_bytes).hexdigest()
        self.fresh_context["tasks"][consumer_id]["manifest_sha256"] = self.manifest_sha
        self.context_path.write_text(json.dumps(self.fresh_context))
        code, receipt = self.run_compile()
        self.assertEqual(code, 0)
        dependency_download = next(item for item in self.downloads if item["name"] == "shard-" + dependency_id)
        self.assertEqual(dependency_download["run"], self.fresh_context["execution_id"])
        self.assertEqual(self.worker_calls[0]["bindings"], [{
            "id": dependency_id, "run_id": self.fresh_context["execution_id"],
            "artifact": "shard-" + dependency_id,
            "expected_worker_commit": self.fresh_context["worker_commit"],
            "expected_manifest_sha256": self.fresh_context["tasks"][dependency_id]["manifest_sha256"],
        }])
        self.assertEqual(receipt["fresh_build"], self.identity())

    def test_old_run_dependency_is_rejected(self):
        dependency_id = self.shard_id
        task_id = "w001-s000"
        self.fresh_context["waves"] = [[dependency_id], [task_id]]
        self.fresh_context["tasks"][task_id] = {
            "id": task_id, "wave": 1, "depends_on": [dependency_id],
            "manifest_sha256": "e" * 64, "archive": task_id + ".tar.zst",
            "archive_bytes": 11, "archive_sha256": "f" * 64,
        }
        self.fresh_context["tasks"][dependency_id]["wave"] = 0
        self.fresh_context["tasks"][task_id]["manifest_sha256"] = self.manifest_sha
        self.fresh_context["tasks"][task_id]["archive_bytes"] = len(b"consumer capsule")
        self.fresh_context["tasks"][task_id]["archive_sha256"] = hashlib.sha256(b"consumer capsule").hexdigest()
        self.shard_id = task_id
        self.archive = b"consumer capsule"
        self.context_path.write_text(json.dumps(self.fresh_context))
        original = self.fake_download

        def old_dependency(run, repository, destination, token=None, name=None, pattern=None):
            result = original(run, repository, destination, token, name, pattern)
            if name == "shard-" + dependency_id:
                receipt_path = Path(result[0]["destination"]) / "receipt.json"
                old = json.loads(receipt_path.read_text())
                old["run_id"] = self.shard_run
                old["fresh_build"]["execution_id"] = self.shard_run
                receipt_path.write_text(json.dumps(old))
            return result

        with mock.patch.dict(os.environ, self.runner_env, clear=True), \
                mock.patch.object(cloud_compile.cloud_artifacts, "download_artifacts", side_effect=old_dependency), \
                mock.patch.object(cloud_compile.subprocess, "run", side_effect=self.fake_tar), \
                mock.patch.object(cloud_compile.worker, "run_shard", side_effect=self.fake_worker) as run_shard:
            code, receipt = cloud_compile.compile_shard(self.context_path, self.shard_id,
                                                        self.output, self.staging)
        self.assertEqual(code, 1)
        self.assertIn("run_id differs from this fresh build", receipt["error"])
        run_shard.assert_not_called()

    def test_wrong_worker_commit_dependency_is_rejected(self):
        dependency_id = self.shard_id
        consumer_id = "w001-s000"
        self.fresh_context["waves"] = [[dependency_id], [consumer_id]]
        self.fresh_context["tasks"][dependency_id]["wave"] = 0
        self.fresh_context["tasks"][consumer_id] = {
            "id": consumer_id, "wave": 1, "depends_on": [dependency_id],
            "manifest_sha256": self.manifest_sha, "archive": consumer_id + ".tar.zst",
            "archive_bytes": len(b"consumer capsule"),
            "archive_sha256": hashlib.sha256(b"consumer capsule").hexdigest(),
        }
        self.shard_id = consumer_id
        self.archive = b"consumer capsule"
        self.context_path.write_text(json.dumps(self.fresh_context))
        original = self.fake_download

        def wrong_commit(run, repository, destination, token=None, name=None, pattern=None):
            result = original(run, repository, destination, token, name, pattern)
            if name == "shard-" + dependency_id:
                receipt_path = Path(result[0]["destination"]) / "receipt.json"
                old = json.loads(receipt_path.read_text())
                old["worker_commit"] = "0" * 40
                old["fresh_build"]["worker_commit"] = "0" * 40
                receipt_path.write_text(json.dumps(old))
            return result

        with mock.patch.dict(os.environ, self.runner_env, clear=True), \
                mock.patch.object(cloud_compile.cloud_artifacts, "download_artifacts", side_effect=wrong_commit), \
                mock.patch.object(cloud_compile.subprocess, "run", side_effect=self.fake_tar), \
                mock.patch.object(cloud_compile.worker, "run_shard", side_effect=self.fake_worker) as run_shard:
            code, receipt = cloud_compile.compile_shard(self.context_path, self.shard_id,
                                                        self.output, self.staging)
        self.assertEqual(code, 1)
        self.assertIn("worker_commit", receipt["error"])
        run_shard.assert_not_called()

    def test_capsule_archive_hash_mismatch_is_rejected_before_extraction(self):
        self.fresh_context["tasks"][self.shard_id]["archive_sha256"] = "0" * 64
        self.context_path.write_text(json.dumps(self.fresh_context))
        with mock.patch.dict(os.environ, self.runner_env, clear=True), \
                mock.patch.object(cloud_compile.cloud_artifacts, "download_artifacts", side_effect=self.fake_download), \
                mock.patch.object(cloud_compile.subprocess, "run") as run, \
                mock.patch.object(cloud_compile.worker, "run_shard") as run_shard:
            code, receipt = cloud_compile.compile_shard(self.context_path, self.shard_id,
                                                        self.output, self.staging)
        self.assertEqual(code, 1)
        self.assertIn("archive bytes/SHA", receipt["error"])
        run.assert_not_called()
        run_shard.assert_not_called()

    def test_admitted_extra_exports_are_forwarded_to_worker_and_receipt(self):
        runtime_path = str(self.out_root / "host/linux-x86/framework/d8.jar")
        self.fresh_context["tasks"][self.shard_id]["extra_exports"] = [runtime_path]
        self.context_path.write_text(json.dumps(self.fresh_context))
        code, receipt = self.run_compile()
        self.assertEqual(code, 0, receipt.get("error"))
        self.assertEqual(self.worker_calls[0]["extra_exports"], [runtime_path])
        self.assertEqual(receipt["additional_exports"], [runtime_path])
        self.assertEqual(receipt["admitted_runtime_inputs"], [])

    def test_admitted_local_runtime_target_is_forwarded_and_receipted(self):
        target = str(self.out_root / "host/linux-x86/framework/d8.jar")
        self.fresh_context["tasks"][self.shard_id]["local_runtime_targets"] = [target]
        self.context_path.write_text(json.dumps(self.fresh_context))
        code, receipt = self.run_compile()
        self.assertEqual(code, 0, receipt.get("error"))
        self.assertEqual(self.worker_calls[0]["prebuild_targets"], [target])
        self.assertEqual(receipt["prebuild_targets"], [target])
        self.assertEqual(receipt["admitted_local_runtime_targets"], [target])

    def test_local_runtime_target_outside_out_is_rejected(self):
        self.fresh_context["tasks"][self.shard_id]["local_runtime_targets"] = [str(self.root / "outside.jar")]
        with self.assertRaisesRegex(cloud_compile.CompileError, "inside OUT_DIR"):
            cloud_compile.validate_context(self.fresh_context, self.shard_id, self.runner_env)

    def _prepare_runtime_input_context(self):
        producer_id, consumer_id = self.shard_id, "w001-s000"
        runtime_path = str(self.out_root / "host/linux-x86/framework/d8.jar")
        producer = self.fresh_context["tasks"][producer_id]
        producer["extra_exports"] = [runtime_path]
        producer["wave"] = 0
        consumer = {"id": consumer_id, "wave": 1, "depends_on": [producer_id],
                    "manifest_sha256": self.manifest_sha,
                    "archive": consumer_id + ".tar.zst", "archive_bytes": 12,
                    "archive_sha256": "f" * 64,
                    "runtime_inputs": [{"path": runtime_path, "producer": producer_id}]}
        self.fresh_context["waves"] = [[producer_id], [consumer_id]]
        self.fresh_context["tasks"][consumer_id] = consumer
        self.shard_id = consumer_id
        self.archive = b"consumer capsule"
        consumer["archive_bytes"] = len(self.archive)
        consumer["archive_sha256"] = hashlib.sha256(self.archive).hexdigest()
        self.context_path.write_text(json.dumps(self.fresh_context))
        return producer_id, consumer_id, runtime_path

    def test_runtime_input_requires_fresh_producer_file_spec_before_worker(self):
        producer_id, consumer_id, runtime_path = self._prepare_runtime_input_context()
        original = self.fake_download

        def missing_runtime_output(run, repository, destination, token=None, name=None, pattern=None):
            result = original(run, repository, destination, token, name, pattern)
            if name == "shard-" + producer_id:
                receipt_path = Path(result[0]["destination"]) / "receipt.json"
                producer_receipt = json.loads(receipt_path.read_text())
                producer_receipt["outputs"] = []
                receipt_path.write_text(json.dumps(producer_receipt))
            return result

        with mock.patch.dict(os.environ, self.runner_env, clear=True), \
                mock.patch.object(cloud_compile.cloud_artifacts, "download_artifacts", side_effect=missing_runtime_output), \
                mock.patch.object(cloud_compile.subprocess, "run", side_effect=self.fake_tar), \
                mock.patch.object(cloud_compile.worker, "run_shard", side_effect=self.fake_worker) as run_shard:
            code, receipt = cloud_compile.compile_shard(self.context_path, consumer_id,
                                                        self.output, self.staging)
        self.assertEqual(code, 1)
        self.assertIn("no unique producer receipt output", receipt["error"])
        run_shard.assert_not_called()

    def test_runtime_input_is_bound_to_current_producer_receipt(self):
        producer_id, consumer_id, runtime_path = self._prepare_runtime_input_context()
        code, receipt = self.run_compile()
        self.assertEqual(code, 0, receipt.get("error"))
        imported = receipt["admitted_runtime_inputs"]
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["path"], runtime_path)
        self.assertEqual(imported[0]["producer"], producer_id)
        self.assertEqual(imported[0]["size"], 4)
        self.assertEqual(imported[0]["sha256"], "9" * 64)
        dependency_download = next(item for item in self.downloads if item["name"] == "shard-" + producer_id)
        self.assertEqual(dependency_download["run"], self.fresh_context["execution_id"])
        self.assertEqual(self.worker_calls[0]["extra_exports"], [])

    def test_runtime_input_owner_and_path_must_be_admitted_and_inside_out(self):
        producer_id, consumer_id, runtime_path = self._prepare_runtime_input_context()
        task = self.fresh_context["tasks"][consumer_id]
        task["depends_on"] = []
        self.context_path.write_text(json.dumps(self.fresh_context))
        with self.assertRaisesRegex(cloud_compile.CompileError, "invalid producer binding"):
            cloud_compile.validate_context(self.fresh_context, consumer_id, self.runner_env)

        task["depends_on"] = [producer_id]
        task["runtime_inputs"][0]["path"] = str(self.root / "outside.jar")
        self.context_path.write_text(json.dumps(self.fresh_context))
        with self.assertRaisesRegex(cloud_compile.CompileError, "inside OUT_DIR"):
            cloud_compile.validate_context(self.fresh_context, consumer_id, self.runner_env)

    def test_empty_context_and_non_cold_roots_are_rejected(self):
        self.context_path.write_text("{}")
        with self.assertRaises(cloud_compile.CompileError):
            cloud_compile.validate_context({}, self.shard_id, self.runner_env)
        self.context_path.write_text(json.dumps(self.fresh_context))
        self.source.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, self.runner_env, clear=True), \
                mock.patch.object(cloud_compile.cloud_artifacts, "download_artifacts") as download:
            code, receipt = cloud_compile.compile_shard(self.context_path, self.shard_id,
                                                        self.output, self.staging)
        self.assertEqual(code, 1)
        self.assertIn("source_root must be cold", receipt["error"])
        download.assert_not_called()

    def test_diagnostic_parent_accepts_only_same_fresh_lineage_and_marks_probe(self):
        dependency_id = self.shard_id
        consumer_id = "w001-s000"
        self.fresh_context["waves"] = [[dependency_id], [consumer_id]]
        self.fresh_context["tasks"][consumer_id] = {
            "id": consumer_id, "wave": 1, "depends_on": [dependency_id],
            "manifest_sha256": self.manifest_sha, "archive": consumer_id + ".tar.zst",
            "archive_bytes": len(b"consumer capsule"),
            "archive_sha256": hashlib.sha256(b"consumer capsule").hexdigest(),
        }
        self.fresh_context["tasks"][dependency_id]["wave"] = 0
        self.shard_id = consumer_id
        self.archive = b"consumer capsule"
        self.fresh_context["tasks"][consumer_id]["archive_bytes"] = len(self.archive)
        self.fresh_context["tasks"][consumer_id]["archive_sha256"] = hashlib.sha256(self.archive).hexdigest()
        self.context_path.write_text(json.dumps(self.fresh_context))
        parent = dict(self.fresh_context)
        parent["execution_id"] = "41789247344"
        parent["worker_commit"] = "e" * 40
        parent_path = self.root / "parent.json"
        parent_path.write_text(json.dumps(parent))
        self.active_dependency_context = parent
        self.runner_env["GITHUB_RUN_ID"] = self.fresh_context["execution_id"]
        code, receipt = self.run_compile(parent_path)
        self.assertEqual(code, 0, receipt.get("error"))
        self.assertTrue(receipt["diagnostic_only"])
        self.assertEqual(receipt["diagnostic_parent"]["execution_id"], parent["execution_id"])
        download = next(item for item in self.downloads if item["name"] == "shard-" + dependency_id)
        self.assertEqual(download["run"], parent["execution_id"])
        self.assertEqual(self.worker_calls[0]["bindings"][0]["run_id"], parent["execution_id"])
        self.assertEqual(self.worker_calls[0]["bindings"][0]["expected_worker_commit"], parent["worker_commit"])
        self.assertIn("--trace", self.worker_calls[0]["makeflags"])
        self.assertEqual(receipt["diagnostic_process_limits"]["arg_max"], "2097152")

    def test_diagnostic_parent_rejects_changed_shared_lineage(self):
        parent = dict(self.fresh_context)
        parent["execution_id"] = "41789247344"
        parent["pack_run"] = "11111111111"
        parent_path = self.root / "parent.json"
        parent_path.write_text(json.dumps(parent))
        self.runner_env["GITHUB_RUN_ID"] = self.fresh_context["execution_id"]
        with mock.patch.dict(os.environ, self.runner_env, clear=True), \
                mock.patch.object(cloud_compile.cloud_artifacts, "download_artifacts") as download:
            with self.assertRaisesRegex(cloud_compile.CompileError, "Diagnostic parent differs"):
                cloud_compile.compile_shard(self.context_path, self.shard_id,
                                            self.output, self.staging,
                                            diagnostic_context_path=parent_path)
        download.assert_not_called()

    def test_default_mode_still_rejects_old_context_and_probe_trace_is_opt_in(self):
        old = dict(self.fresh_context)
        old["execution_id"] = "41789247344"
        with self.assertRaises(cloud_compile.CompileError):
            cloud_compile.validate_context(old, self.shard_id, self.runner_env)
        current, _ = cloud_compile.validate_context(self.fresh_context, self.shard_id, self.runner_env)
        self.assertEqual(current["execution_id"], self.runner_env["GITHUB_RUN_ID"])

    def _prepare_short_source_context(self):
        self.fresh_context["assembly_exports"] = {
            str(self.out_root / "target/product/crux/obj/KERNEL_OBJ"): self.shard_id}
        self.fresh_context["source_selection"] = {"projects": [{
            "path": cloud_compile.SHORT_KERNEL_SOURCE_PROJECT,
            "revision": cloud_compile.SHORT_KERNEL_SOURCE_REVISION,
        }]}
        self.context_path.write_text(json.dumps(self.fresh_context))
        parent = dict(self.fresh_context)
        parent["execution_id"] = "41789247344"
        parent_path = self.root / "parent.json"
        parent_path.write_text(json.dumps(parent))
        self.active_dependency_context = parent
        return parent_path

    def test_short_source_experiment_is_pinned_opt_in_and_cleans_mount(self):
        parent_path = self._prepare_short_source_context()
        code, receipt = self.run_compile(parent_path, short_kernel_source=True)
        self.assertEqual(code, 0, receipt.get("error"))
        self.mount_calls.assert_called_once()
        self.unmount_calls.assert_called_once()
        self.assertEqual(receipt["runtime_source_alias"], {
            "source": str(self.source / cloud_compile.SHORT_KERNEL_SOURCE_PROJECT),
            "alias": "/k", "mount": "bind",
            "source_revision": cloud_compile.SHORT_KERNEL_SOURCE_REVISION,
            "makeflags_addition": "srctree=/k",
            "validation_policy": None,
            "makeflags": "--trace srctree=/k",
        })
        self.assertIn("srctree=/k", self.worker_calls[0]["makeflags"])

    def test_short_source_rejects_wrong_task_and_revision(self):
        parent_path = self._prepare_short_source_context()
        self.fresh_context["assembly_exports"][str(self.out_root / "target/product/crux/obj/KERNEL_OBJ")] = "w000-s999"
        self.context_path.write_text(json.dumps(self.fresh_context))
        with mock.patch.dict(os.environ, self.runner_env, clear=True):
            with self.assertRaisesRegex(cloud_compile.CompileError, "task owning the kernel output"):
                cloud_compile.compile_shard(self.context_path, self.shard_id,
                                            self.root / "result-owner", self.root / "staging-owner",
                                            diagnostic_context_path=parent_path, short_kernel_source=True)

        self.fresh_context["assembly_exports"][str(self.out_root / "target/product/crux/obj/KERNEL_OBJ")] = self.shard_id
        self.fresh_context["source_selection"]["projects"][0]["revision"] = "0" * 40
        self.context_path.write_text(json.dumps(self.fresh_context))
        self.output = self.root / "result-revision"
        self.staging = self.root / "staging-revision"
        code, receipt = self.run_compile(parent_path, short_kernel_source=True)
        self.assertEqual(code, 1)
        self.assertIn("pinned kernel source revision", receipt["error"])
        self.mount_calls.assert_not_called()

    def test_existing_short_source_alias_is_rejected_without_mutation(self):
        source = self.source / cloud_compile.SHORT_KERNEL_SOURCE_PROJECT
        with mock.patch.object(cloud_compile.os.path, "lexists", return_value=True), \
                mock.patch.object(cloud_compile.subprocess, "run") as run:
            with self.assertRaisesRegex(cloud_compile.CompileError, "alias already exists"):
                cloud_compile._mount_short_source_alias(source)
        run.assert_not_called()

    def test_validated_policy_auto_enables_only_kernel_owner_in_production(self):
        self._prepare_short_source_context()
        self.fresh_context["kernel_source_alias"] = dict(cloud_compile.SHORT_KERNEL_SOURCE_POLICY)
        self.context_path.write_text(json.dumps(self.fresh_context))
        code, receipt = self.run_compile()
        self.assertEqual(code, 0, receipt.get("error"))
        self.assertNotIn("diagnostic_only", receipt)
        self.assertEqual(receipt["fresh_build"], self.identity())
        self.assertEqual(receipt["runtime_source_alias"]["validation_policy"],
                         cloud_compile.SHORT_KERNEL_SOURCE_POLICY)
        self.assertEqual(self.worker_calls[0]["makeflags"], "srctree=/k")
        self.mount_calls.assert_called_once()
        self.unmount_calls.assert_called_once()

    def test_validated_policy_does_not_mount_for_nonowner_and_wrong_policy_fails(self):
        self._prepare_short_source_context()
        self.fresh_context["kernel_source_alias"] = dict(cloud_compile.SHORT_KERNEL_SOURCE_POLICY)
        self.fresh_context["assembly_exports"][str(self.out_root / "target/product/crux/obj/KERNEL_OBJ")] = "w009-s009"
        self.context_path.write_text(json.dumps(self.fresh_context))
        code, receipt = self.run_compile()
        self.assertEqual(code, 0, receipt.get("error"))
        self.assertNotIn("runtime_source_alias", receipt)
        self.mount_calls.assert_not_called()

        self.fresh_context["kernel_source_alias"] = {**cloud_compile.SHORT_KERNEL_SOURCE_POLICY,
                                                       "revision": "0" * 40}
        self.context_path.write_text(json.dumps(self.fresh_context))
        with mock.patch.dict(os.environ, self.runner_env, clear=True):
            with self.assertRaisesRegex(cloud_compile.CompileError, "not the validated probe policy"):
                cloud_compile.compile_shard(self.context_path, self.shard_id,
                                            self.root / "result-policy", self.root / "stage-policy")


if __name__ == "__main__":
    unittest.main()
