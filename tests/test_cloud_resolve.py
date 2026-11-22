import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cloud_build_plan
import cloud_reuse
import cloud_reuse_cache
import cloud_resolve


class CloudResolveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.out = self.root / "out"
        self.execution_digest = "d" * 64
        self.execution_id = "70000000001"
        self.worker_commit = "a" * 40
        self.dep_id = "w000-s000"
        self.task_id = "w001-s000"
        self.dep_outputs = [{"path": str(self.out / "dep.bin"), "type": "file", "mode": 0o644,
                             "size": 3, "sha256": "b" * 64}]
        self.task_descriptor = {"schema_version": 1, "version": 1,
                                "input_identity": "c" * 64,
                                "execution_digest": self.execution_digest,
                                "graph": {}, "source_inputs": [], "source_root": str(self.source),
                                "out_root": str(self.out), "external_inputs": [],
                                "external_input_dirs": [], "required_interpreters": [], "system_tools": []}
        common = {"packing": "deferred", "reuse_descriptor": self.task_descriptor,
                  "input_identity": self.task_descriptor["input_identity"],
                  "extra_exports": [], "local_runtime_targets": [], "required_notice_sources": []}
        self.context = {
            "schema_version": 1, "execution_id": self.execution_id,
            "worker_commit": self.worker_commit, "repository": "coachpo/crux-pe13-distributed",
            "build_number": "2026111301", "source_root": str(self.source), "out_root": str(self.out),
            "pack_run": "70000000002", "shard_run": "70000000003", "graph_run": "70000000004",
            "index_run": "70000000005", "source_run": "70000000006",
            "source_manifest_sha256": "e" * 64, "plan_sha256": "f" * 64,
            "graph_index": {"bytes": 32, "sha256": "1" * 64},
            "waves": [[self.dep_id], [self.task_id]],
            "tasks": {
                self.dep_id: {"id": self.dep_id, "wave": 0, "depends_on": [],
                              "manifest_sha256": "2" * 64, **common},
                self.task_id: {"id": self.task_id, "wave": 1, "depends_on": [self.dep_id],
                               "manifest_sha256": "3" * 64, **common},
            },
            "reuse_policy": {"version": 1, "execution_digest": self.execution_digest,
                             "force_cold": False},
            "assembly_exports": {}, "frontend": {}, "kernel_source_alias": None,
        }
        self.environment = {"GITHUB_RUN_ID": self.execution_id,
                            "GITHUB_SHA": self.worker_commit,
                            "GITHUB_REPOSITORY": self.context["repository"],
                            "GH_TOKEN": "test-token", "GITHUB_TOKEN": "other-token"}
        self.dep_receipt = self._built_receipt(self.dep_id, self.context["tasks"][self.dep_id],
                                               self.dep_outputs)

    def tearDown(self):
        self.temporary.cleanup()

    def _built_receipt(self, task_id, task, outputs):
        receipt = {"schema_version": 1, "id": task_id, "status": "success",
                   "run_id": self.execution_id, "worker_commit": self.worker_commit,
                   "manifest_sha256": task["manifest_sha256"],
                   "source_root": str(self.source), "out_root": str(self.out),
                   "fresh_build": cloud_build_plan.fresh_binding(self.context),
                   "declared_dependencies": [], "additional_exports": [],
                   "prebuild_targets": [], "build_environment": {"CCACHE_DISABLE": "1"},
                   "outputs": outputs, "archive_bytes": 9, "archive_sha256": "4" * 64}
        basis = {"schema_version": 1, "version": 1,
                 "execution_digest": self.execution_digest,
                 "input_identity": task["input_identity"],
                 "runtime": {"CCACHE_DISABLE": "1"},
                 "dependencies": [], "additional_exports": [],
                 "prebuild_targets": [], "kernel_source_alias": None}
        receipt["reuse"] = cloud_reuse.envelope(self.context, task_id, basis, dict(receipt))
        return receipt

    def _download_current_dependency(self, run_id, repository, destination, token=None, name=None,
                                     pattern=None):
        if str(run_id) != self.execution_id or repository != self.context["repository"]:
            raise AssertionError((run_id, repository))
        if name != "fresh-receipt-" + self.dep_id:
            raise AssertionError(name)
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "receipt.json").write_text(json.dumps(self.dep_receipt))
        return [{"name": name, "destination": str(directory)}]

    def _resolve(self, name, find_side_effect=None):
        output, staging = self.root / (name + "-out"), self.root / (name + "-staging")
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                cloud_resolve.cloud_artifacts, "download_artifacts",
                side_effect=self._download_current_dependency))
            stack.enter_context(mock.patch.object(
                cloud_reuse, "runtime_identity",
                return_value={"CCACHE_DISABLE": "1", "host": "fixture"}))
            find = stack.enter_context(mock.patch.object(
                cloud_resolve.cloud_reuse_cache, "find", side_effect=find_side_effect))
            result = cloud_resolve.resolve(self.context, self.task_id, output, staging,
                                           self.environment)
            return result, output, staging, find

    def test_cold_miss_decision_key_binds_current_dependency_outputs(self):
        observed = {}

        def miss(context, task_id, basis, staging, token):
            observed.update(task_id=task_id, basis=basis, token=token)
            return None

        result, _, _, _ = self._resolve("miss", find_side_effect=miss)
        self.assertEqual(result["mode"], "miss")
        self.assertEqual(result["id"], self.task_id)
        self.assertEqual(result["key"], cloud_reuse.content_key(observed["basis"]))
        self.assertEqual(observed["task_id"], self.task_id)
        self.assertEqual(observed["token"], "test-token")
        expected_specs = sorted((cloud_reuse.identity_spec(item) for item in self.dep_outputs),
                                key=lambda item: item["path"])
        self.assertEqual(observed["basis"]["dependencies"], [
            {"output_count": len(expected_specs), "sha256": cloud_reuse.digest(expected_specs)}])

    def test_dependency_content_change_changes_the_decision_key(self):
        first = {}
        self._resolve("key-before", find_side_effect=lambda context, task, basis, staging, token:
                      first.update(key=cloud_reuse.content_key(basis)) or None)
        changed_outputs = [{**self.dep_outputs[0], "sha256": "5" * 64}]
        self.dep_outputs = changed_outputs
        self.dep_receipt = self._built_receipt(self.dep_id, self.context["tasks"][self.dep_id],
                                               changed_outputs)
        second = {}
        self._resolve("key-after", find_side_effect=lambda context, task, basis, staging, token:
                      second.update(key=cloud_reuse.content_key(basis)) or None)
        self.assertNotEqual(first["key"], second["key"])

    def test_forced_cold_skips_cache_lookup(self):
        self.context["reuse_policy"]["force_cold"] = True
        result, _, _, find = self._resolve("forced-cold")
        find.assert_not_called()
        self.assertEqual(result["mode"], "miss")

    def test_verified_hit_emits_current_receipt_with_origin_and_current_dependencies(self):
        native_task_id = "w005-s009"
        native_run = "60000000001"
        native_commit = "9" * 40
        outputs = [{"path": str(self.out / "native.bin"), "type": "file", "mode": 0o644,
                    "size": 4, "sha256": "8" * 64}]
        native = {"schema_version": 1, "id": native_task_id, "status": "success",
                  "run_id": native_run, "worker_commit": native_commit,
                  "manifest_sha256": "7" * 64, "outputs": outputs,
                  "build_environment": {"CCACHE_DISABLE": "1"}}
        key_probe = {}
        cache_archive = self.root / "validated-native-outputs.tar.zst"
        cache_archive.write_bytes(b"verified native archive")

        def hit(context, task_id, basis, staging, token):
            key_probe.update(basis=basis)
            native_basis = {"schema_version": 1, "version": 1,
                            "execution_digest": self.execution_digest,
                            "input_identity": self.task_descriptor["input_identity"],
                            "runtime": basis["runtime"], "dependencies": basis["dependencies"],
                            "additional_exports": [], "prebuild_targets": [],
                            "kernel_source_alias": None}
            carrier = dict(native)
            candidate_context = {"repository": self.context["repository"], "tasks": {
                native_task_id: {"input_identity": self.task_descriptor["input_identity"],
                                 "depends_on": [], "extra_exports": [], "local_runtime_targets": [],
                                 "manifest_sha256": native["manifest_sha256"]}},
                "execution_id": native_run, "worker_commit": native_commit,
                "reuse_policy": {"version": 1, "execution_digest": self.execution_digest},
                "kernel_source_alias": None}
            carrier["reuse"] = cloud_reuse.envelope(candidate_context, native_task_id,
                                                    native_basis, dict(native))
            origin = {"repository": self.context["repository"], "run_id": native_run,
                      "worker_commit": native_commit, "task_id": native_task_id,
                      "artifact_id": 987, "artifact_digest": "sha256:" + "6" * 64,
                      "receipt_sha256": cloud_reuse.digest(carrier)}
            return origin, carrier, cache_archive

        result, output, _, _ = self._resolve("hit", find_side_effect=hit)
        receipt = json.loads((output / "receipt.json").read_text())
        self.assertEqual(result["mode"], "reused")
        self.assertEqual(result["key"], cloud_reuse.content_key(key_probe["basis"]))
        self.assertEqual(receipt["run_id"], self.execution_id)
        self.assertEqual(receipt["worker_commit"], self.worker_commit)
        self.assertEqual(receipt["id"], self.task_id)
        self.assertEqual(receipt["reuse"]["mode"], "reused")
        self.assertEqual(receipt["reuse"]["native_receipt"]["id"], native_task_id)
        self.assertEqual(receipt["reuse"]["origin"]["task_id"], native_task_id)
        self.assertEqual(receipt["reuse"]["origin"]["adopted_from"]["artifact_id"], 987)
        self.assertEqual(receipt["declared_dependencies"], [
            cloud_build_plan.producer_binding(self.context, self.dep_id)])
        self.assertEqual(receipt["dependencies"][0]["id"], self.dep_id)
        self.assertEqual(receipt["dependencies"][0]["manifest_sha256"],
                         self.dep_receipt["manifest_sha256"])
        self.assertEqual((output / "outputs.tar.zst").read_bytes(), cache_archive.read_bytes())

    def test_corrupt_candidate_falls_back_but_transport_error_propagates(self):
        corrupt, _, _, _ = self._resolve("corrupt", find_side_effect=
            mock.Mock(side_effect=cloud_reuse_cache.CacheIntegrityError("candidate archive identity failed")))
        self.assertEqual(corrupt["mode"], "miss")
        self.assertIn("rejected_candidate", corrupt)

        with self.assertRaisesRegex(RuntimeError, "network unavailable"):
            self._resolve("transport", find_side_effect=
                mock.Mock(side_effect=RuntimeError("network unavailable")))


if __name__ == "__main__":
    unittest.main()
