import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.parse
import zipfile
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cloud_reuse
import cloud_reuse_cache
import cloud_layers
import release_source


def artifact_zip(members):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return output.getvalue()


class CloudReuseCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.execution_digest = "d" * 64
        self.current_task = "w000-s000"
        self.candidate_task = "w002-s017"
        self.manifest_sha = "a" * 64
        self.output_specs = [{"path": "/out/product/a", "type": "file", "mode": 0o644,
                              "size": 4, "sha256": "b" * 64}]
        self.basis = {"schema_version": 1, "version": 1,
                      "execution_digest": self.execution_digest,
                      "input_identity": "c" * 64,
                      "runtime": {"CCACHE_DISABLE": "1"}, "dependencies": [],
                      "additional_exports": [], "prebuild_targets": [],
                      "kernel_source_alias": None}
        self.current_context = {"repository": cloud_reuse_cache.REPOSITORY,
                                "execution_id": "999", "reuse_policy": {
                                    "version": 1, "execution_digest": self.execution_digest}}
        self.candidate_context = {
            "schema_version": 1, "repository": cloud_reuse_cache.REPOSITORY,
            "execution_id": "555", "worker_commit": "1" * 40,
            "pack_run": "500", "shard_run": "501", "plan_sha256": "2" * 64,
            "source_manifest_sha256": "3" * 64,
            "reuse_policy": {"version": 1, "execution_digest": self.execution_digest},
            "kernel_source_alias": None,
            "tasks": {self.candidate_task: {"manifest_sha256": self.manifest_sha,
                "depends_on": [], "input_identity": self.basis["input_identity"],
                "extra_exports": [], "local_runtime_targets": []}},
        }
        self.native_receipt = {
            "schema_version": 1, "status": "success", "id": self.candidate_task,
            "run_id": "555", "worker_commit": "1" * 40,
            "manifest_sha256": self.manifest_sha,
            "build_environment": {"CCACHE_DISABLE": "1"},
            "outputs": self.output_specs,
        }
        self.envelope = cloud_reuse.envelope(self.candidate_context, self.candidate_task,
                                             self.basis, self.native_receipt)
        self.carrier_receipt = {**self.native_receipt, "reuse": self.envelope,
                                "archive_bytes": 18,
                                "archive_sha256": "4" * 64}
        self.summary = {
            "schema_version": 1, "status": "success", "execution_id": "555",
            "worker_commit": "1" * 40, "task_count": 1,
            "fresh_build": {key: self.candidate_context[key] for key in (
                "execution_id", "pack_run", "shard_run", "plan_sha256", "source_manifest_sha256")},
            "producers": [{"id": self.candidate_task, "run_id": "555",
                           "artifact": "shard-" + self.candidate_task,
                           "expected_worker_commit": "1" * 40,
                           "expected_manifest_sha256": self.manifest_sha}],
            "reuse": {self.candidate_task: self.envelope},
            "output_identity": {self.candidate_task: self.output_specs},
        }
        self.artifact_payloads = {
            101: artifact_zip({"build-summary.json": json.dumps(self.summary).encode()}),
            102: artifact_zip({"context.json": json.dumps(self.candidate_context).encode()}),
            103: artifact_zip({"receipt.json": json.dumps(self.carrier_receipt).encode(),
                              "outputs.tar.zst": b"native zstd archive", "compile.log": b"done\n"}),
        }
        self.inventory = {name: {"id": artifact_id, "name": name, "expired": False,
                                 "digest": "sha256:" + hashlib.sha256(self.artifact_payloads[artifact_id]).hexdigest(),
                                 "size_in_bytes": len(self.artifact_payloads[artifact_id])}
                          for name, artifact_id in (
                              ("cloud-fresh-build-summary", 101),
                              ("cloud-build-context", 102),
                              ("shard-" + self.candidate_task, 103))}

    def tearDown(self):
        self.temporary.cleanup()

    def test_expiry_after_inventory_is_a_miss_through_real_transport_wrappers(self):
        for status in (404, 410):
            with self.subTest(status=status):
                http_error = urllib.error.HTTPError('https://api.github.com/artifact', status,
                                                    'gone', {}, io.BytesIO(b'{}'))
                transport = RuntimeError('GitHub API request failed (%s)' % status)
                transport.__cause__ = http_error
                wrapped = release_source.SourceTransportError('Actions artifact download failed: gone')
                wrapped.__cause__ = transport
                with mock.patch.object(release_source, '_download_zip', side_effect=wrapped):
                    with self.assertRaises(cloud_reuse_cache._CandidateUnavailable):
                        cloud_reuse_cache._download({}, self.root / str(status), 'token')

    def _run(self, run_id="555", path=cloud_reuse_cache.BUILD_WORKFLOW):
        return {"id": int(run_id), "status": "completed", "conclusion": "success",
                "event": "workflow_dispatch", "head_branch": "main", "path": path,
                "head_sha": "1" * 40}

    def _mock_run_pages(self, candidate=None):
        unrelated = self._run("444", ".github/workflows/nightly.yml")
        candidate = self._run() if candidate is None else candidate

        def request(url, token):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            page = query["page"][0]
            if page == "1":
                return json.dumps({"total_count": 2, "workflow_runs": [unrelated]}).encode(), {}
            if page == "2":
                return json.dumps({"total_count": 2, "workflow_runs": [candidate]}).encode(), {}
            raise AssertionError("run inventory requested past total_count")
        return request

    def _mock_download(self, artifact, directory, token):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (str(artifact["id"]) + ".zip")
        path.write_bytes(self.artifact_payloads[artifact["id"]])
        return path, artifact["digest"]

    def invoke(self):
        staging = self.root / "successful-staging"
        with mock.patch.object(cloud_layers, "_request", side_effect=self._mock_run_pages()), \
                mock.patch.object(cloud_layers, "artifact_inventory", return_value=self.inventory), \
                mock.patch.object(release_source, "_download_zip", side_effect=self._mock_download), \
                mock.patch.object(cloud_reuse, "verify_native_output", side_effect=lambda receipt, directory:
                                  Path(directory) / "outputs.tar.zst") as verify:
            result = cloud_reuse_cache.find(self.current_context, self.current_task, self.basis,
                                            staging, "test-token")
            return result, verify

    def test_finds_different_task_after_paging_and_verifies_complete_origin(self):
        result, verify = self.invoke()
        self.assertIsNotNone(result)
        origin, receipt, archive = result
        self.assertEqual(origin["run_id"], "555")
        self.assertEqual(origin["task_id"], self.candidate_task)
        self.assertEqual(origin["artifact_id"], 103)
        self.assertEqual(origin["artifact_digest"], self.inventory["shard-" + self.candidate_task]["digest"])
        self.assertEqual(origin["receipt_sha256"], cloud_reuse.digest(receipt))
        self.assertEqual(receipt, self.carrier_receipt)
        self.assertTrue(archive.is_file())
        verify.assert_called_once()

    def test_old_summary_without_reuse_is_a_cache_miss(self):
        self.summary.pop("reuse")
        self.summary.pop("output_identity")
        self.artifact_payloads[101] = artifact_zip({"build-summary.json": json.dumps(self.summary).encode()})
        self.inventory["cloud-fresh-build-summary"].update(
            digest="sha256:" + hashlib.sha256(self.artifact_payloads[101]).hexdigest(),
            size_in_bytes=len(self.artifact_payloads[101]))
        with tempfile.TemporaryDirectory(dir=self.root) as staging, \
                mock.patch.object(cloud_layers, "_request", side_effect=self._mock_run_pages()), \
                mock.patch.object(cloud_layers, "artifact_inventory", return_value=self.inventory), \
                mock.patch.object(release_source, "_download_zip", side_effect=self._mock_download):
            self.assertIsNone(cloud_reuse_cache.find(self.current_context, self.current_task,
                                                     self.basis, Path(staging), "test-token"))

    def test_expired_output_artifact_is_a_cache_miss(self):
        self.inventory["shard-" + self.candidate_task]["expired"] = True
        with tempfile.TemporaryDirectory(dir=self.root) as staging, \
                mock.patch.object(cloud_layers, "_request", side_effect=self._mock_run_pages()), \
                mock.patch.object(cloud_layers, "artifact_inventory", return_value=self.inventory), \
                mock.patch.object(release_source, "_download_zip", side_effect=self._mock_download):
            self.assertIsNone(cloud_reuse_cache.find(self.current_context, self.current_task,
                                                     self.basis, Path(staging), "test-token"))

    def test_matching_summary_with_output_mismatch_is_integrity_error(self):
        self.summary["output_identity"][self.candidate_task] = []
        self.artifact_payloads[101] = artifact_zip({"build-summary.json": json.dumps(self.summary).encode()})
        self.inventory["cloud-fresh-build-summary"].update(
            digest="sha256:" + hashlib.sha256(self.artifact_payloads[101]).hexdigest(),
            size_in_bytes=len(self.artifact_payloads[101]))
        with tempfile.TemporaryDirectory(dir=self.root) as staging, \
                mock.patch.object(cloud_layers, "_request", side_effect=self._mock_run_pages()), \
                mock.patch.object(cloud_layers, "artifact_inventory", return_value=self.inventory), \
                mock.patch.object(release_source, "_download_zip", side_effect=self._mock_download):
            with self.assertRaises(cloud_reuse_cache.CacheIntegrityError):
                cloud_reuse_cache.find(self.current_context, self.current_task,
                                       self.basis, Path(staging), "test-token")

    def test_output_artifact_rejects_traversal_member(self):
        self.artifact_payloads[103] = artifact_zip({
            "receipt.json": json.dumps(self.carrier_receipt).encode(),
            "outputs.tar.zst": b"native zstd archive", "../escape": b"no"})
        self.inventory["shard-" + self.candidate_task].update(
            digest="sha256:" + hashlib.sha256(self.artifact_payloads[103]).hexdigest(),
            size_in_bytes=len(self.artifact_payloads[103]))
        with tempfile.TemporaryDirectory(dir=self.root) as staging, \
                mock.patch.object(cloud_layers, "_request", side_effect=self._mock_run_pages()), \
                mock.patch.object(cloud_layers, "artifact_inventory", return_value=self.inventory), \
                mock.patch.object(release_source, "_download_zip", side_effect=self._mock_download):
            with self.assertRaises(cloud_reuse_cache.CacheIntegrityError):
                cloud_reuse_cache.find(self.current_context, self.current_task,
                                       self.basis, Path(staging), "test-token")


if __name__ == "__main__":
    unittest.main()
