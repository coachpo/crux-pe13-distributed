import hashlib
import importlib.util
import json
import os
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
        dependency_context = self.identity()
        task = self.fresh_context["tasks"][dependency_id]
        receipt = {"schema_version": 1, "id": dependency_id, "status": "success",
                   "run_id": self.fresh_context["execution_id"],
                   "run_attempt": "1", "worker_commit": self.fresh_context["worker_commit"],
                   "manifest_sha256": task["manifest_sha256"],
                   "source_root": self.fresh_context["source_root"],
                   "out_root": self.fresh_context["out_root"],
                   "fresh_build": dependency_context, "outputs": [],
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
                    dependency_dirs, jobs, dependency_bindings):
        self.worker_calls.append({"manifest": Path(manifest_path), "bundle": Path(bundle_path),
                                  "id": shard_id, "dependency_dirs": list(dependency_dirs),
                                  "jobs": jobs, "bindings": dependency_bindings})
        self.assertNotIn("GH_TOKEN", os.environ)
        self.assertNotIn("GITHUB_TOKEN", os.environ)
        manifest = json.loads(Path(manifest_path).read_text())
        receipt = {"schema_version": 1, "id": shard_id, "status": "success",
                   "run_id": self.fresh_context["execution_id"],
                   "run_attempt": "1", "worker_commit": self.fresh_context["worker_commit"],
                   "manifest_sha256": hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest(),
                   "source_root": manifest["source_root"], "out_root": self.fresh_context["out_root"],
                   "outputs": [], "archive_sha256": hashlib.sha256(b"fresh outputs").hexdigest()}
        Path(output_dir, "receipt.json").write_text(json.dumps(receipt))
        return receipt

    def run_compile(self):
        with mock.patch.dict(os.environ, self.runner_env, clear=True), \
                mock.patch.object(cloud_compile.cloud_artifacts, "download_artifacts", side_effect=self.fake_download), \
                mock.patch.object(cloud_compile.subprocess, "run", side_effect=self.fake_tar), \
                mock.patch.object(cloud_compile.worker, "run_shard", side_effect=self.fake_worker):
            return cloud_compile.compile_shard(self.context_path, self.shard_id,
                                                self.output, self.staging)

    def test_fresh_capsule_worker_records_context_and_disables_auth_in_build_child(self):
        code, receipt = self.run_compile()
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["fresh_build"], self.identity())
        self.assertEqual(self.downloads[0]["run"], self.pack_run)
        self.assertEqual(self.downloads[0]["name"], "cloud-capsule-" + self.shard_id)
        self.assertEqual(self.worker_calls[0]["jobs"], 4)
        self.assertEqual(self.worker_calls[0]["bindings"], [])
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


if __name__ == "__main__":
    unittest.main()
