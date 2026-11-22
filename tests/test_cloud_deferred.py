import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cloud_deferred


class DeferredPackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source, self.out = self.root / "source", self.root / "out"
        self.output, self.staging = self.root / "output", self.root / "staging"
        self.manifest_path = self.root / "source.json"
        self.manifest_path.write_text(json.dumps({"source_root": str(self.source)}))
        self.descriptor = {"input_identity": "a" * 64, "graph": {"commands": ["compile"]},
                           "source_inputs": [], "external_inputs": [], "external_input_dirs": [],
                           "system_tools": []}
        self.context = {
            "worker_commit": "b" * 40, "repository": "owner/repo", "execution_id": "100",
            "graph_run": "101", "index_run": "102", "shard_run": "103", "source_run": "99",
            "build_number": "2026111350", "source_root": str(self.source), "out_root": str(self.out),
            "source_manifest_sha256": hashlib.sha256(self.manifest_path.read_bytes()).hexdigest(),
            "tasks": {"w000-s000": {"reuse_descriptor": self.descriptor}}}
        self.environment = {"GITHUB_SHA": "b" * 40, "GITHUB_REPOSITORY": "owner/repo",
                            "GH_TOKEN": "not-to-be-retained"}
        self.commands = []

    def fake_run(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == "tar" and command[-1] == str(self.staging / "shard-tree"):
            selected = self.staging / "shard-tree/shards/w000-s000"
            selected.mkdir(parents=True)
            (selected / "manifest.json").write_text(json.dumps({"commands": ["compile"]}))
        return subprocess.CompletedProcess(command, 0)

    def fake_pack(self, shards, shard_id, inventory, receipt, manifest, index, output):
        (output / (shard_id + ".json")).write_text(json.dumps({"files": [], "archive": {"sha256": "c" * 64}}))
        (output / (shard_id + ".tar.zst")).write_bytes(b"verified current capsule")
        return 0, {"status": "success", "archive": shard_id + ".tar.zst", "archive_sha256": "c" * 64}

    def call_pack(self):
        return cloud_deferred.pack(self.context, "w000-s000", self.output, self.staging,
                                   self.manifest_path, environment=self.environment)

    def test_miss_packs_current_snapshot_and_rechecks_the_full_audit_descriptor(self):
        with mock.patch.object(cloud_deferred.pipeline_code, "verify") as code, \
                mock.patch.object(cloud_deferred.cloud_artifacts, "download_artifacts") as download, \
                mock.patch.object(cloud_deferred.subprocess, "run", side_effect=self.fake_run), \
                mock.patch.object(cloud_deferred.cloud_pack, "pack_shard", side_effect=self.fake_pack) as pack, \
                mock.patch.object(cloud_deferred.cloud_reuse, "execution_digest", return_value="d" * 64), \
                mock.patch.object(cloud_deferred.cloud_reuse, "input_descriptor", return_value=self.descriptor) as descriptor:
            result = self.call_pack()
        self.assertEqual(result["status"], "success")
        code.assert_called_once_with("owner/repo", "b" * 40, ["101", "102", "103"])
        download.assert_called_once_with("103", "owner/repo", self.staging / "shard-input",
                                         token="not-to-be-retained", name="cloud-shards-2026111350")
        mount = next(command for command in self.commands if "--source-run" in command)
        self.assertEqual(mount[mount.index("--source-run") + 1], "99")
        self.assertEqual(mount[mount.index("--manifest") + 1], str(self.manifest_path))
        pack.assert_called_once_with(self.staging / "shard-tree/shards", "w000-s000",
                                     self.staging / "shard-input/slice-inventory.json",
                                     self.staging / "shard-input/shard-receipt.json", self.manifest_path,
                                     self.staging / "index-pruned.sqlite", self.output)
        self.assertEqual(descriptor.call_args.args[0], {"commands": ["compile"]})
        self.assertEqual(descriptor.call_args.args[1]["archive"]["sha256"], "c" * 64)
        self.assertEqual(descriptor.call_args.args[2], "d" * 64)
        saved = json.loads((self.output / "pack-receipt.json").read_text())
        self.assertEqual(saved["reuse_descriptor"], self.descriptor)
        self.assertNotIn("not-to-be-retained", json.dumps(saved))

    def test_same_identity_with_changed_descriptor_cannot_be_admitted(self):
        changed = copy.deepcopy(self.descriptor)
        changed["graph"]["commands"] = ["different compile"]
        with mock.patch.object(cloud_deferred.pipeline_code, "verify"), \
                mock.patch.object(cloud_deferred.cloud_artifacts, "download_artifacts"), \
                mock.patch.object(cloud_deferred.subprocess, "run", side_effect=self.fake_run), \
                mock.patch.object(cloud_deferred.cloud_pack, "pack_shard", side_effect=self.fake_pack), \
                mock.patch.object(cloud_deferred.cloud_reuse, "execution_digest", return_value="d" * 64), \
                mock.patch.object(cloud_deferred.cloud_reuse, "input_descriptor", return_value=changed):
            result = self.call_pack()
        self.assertEqual(result["status"], "failed")
        self.assertIn("differ from the current audit", result["error"])

    def test_actual_descriptor_api_admits_packed_metadata_matching_the_audit(self):
        manifest = {"commands": ["compile"]}
        metadata = {"allowed_generated_inputs": [], "source_root": str(self.source),
                    "out_root": str(self.out), "slice_path": str(self.source / ".crux-task/graph"),
                    "files": [{"path": str(self.source / "input.cc"), "type": "file", "mode": 0o644,
                               "size": 7, "sha256": hashlib.sha256(b"input\n\n").hexdigest()}],
                    "external_inputs": [], "external_input_dirs": [],
                    "required_interpreters": [], "system_tools": ["/usr/bin/env"]}
        expected = cloud_deferred.cloud_reuse.input_descriptor(manifest, metadata, "d" * 64)
        self.context["tasks"]["w000-s000"]["reuse_descriptor"] = expected

        def pack_metadata(*args):
            result = self.fake_pack(*args)
            (self.output / "w000-s000.json").write_text(json.dumps(metadata))
            return result

        with mock.patch.object(cloud_deferred.pipeline_code, "verify"), \
                mock.patch.object(cloud_deferred.cloud_artifacts, "download_artifacts"), \
                mock.patch.object(cloud_deferred.subprocess, "run", side_effect=self.fake_run), \
                mock.patch.object(cloud_deferred.cloud_pack, "pack_shard", side_effect=pack_metadata), \
                mock.patch.object(cloud_deferred.cloud_reuse, "execution_digest", return_value="d" * 64):
            result = self.call_pack()
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertEqual(result["reuse_descriptor"], expected)

    def test_expired_explicit_shard_input_does_not_select_or_pack_another_run(self):
        with mock.patch.object(cloud_deferred.pipeline_code, "verify"), \
                mock.patch.object(cloud_deferred.cloud_artifacts, "download_artifacts",
                                  side_effect=ValueError("no unexpired artifact")) as download, \
                mock.patch.object(cloud_deferred.cloud_pack, "pack_shard") as pack:
            result = self.call_pack()
        self.assertEqual(result["status"], "failed")
        self.assertIn("no unexpired artifact", result["error"])
        self.assertEqual(download.call_count, 1)
        pack.assert_not_called()

    def test_source_mount_failure_prevents_packing(self):
        def fail_mount(command, **kwargs):
            if "--source-run" in command:
                raise subprocess.CalledProcessError(1, command)
            return self.fake_run(command, **kwargs)
        with mock.patch.object(cloud_deferred.pipeline_code, "verify"), \
                mock.patch.object(cloud_deferred.cloud_artifacts, "download_artifacts"), \
                mock.patch.object(cloud_deferred.subprocess, "run", side_effect=fail_mount), \
                mock.patch.object(cloud_deferred.cloud_pack, "pack_shard") as pack:
            self.assertEqual(self.call_pack()["status"], "failed")
        pack.assert_not_called()

    def test_ordinary_pack_integrity_failure_cannot_claim_success(self):
        with mock.patch.object(cloud_deferred.pipeline_code, "verify"), \
                mock.patch.object(cloud_deferred.cloud_artifacts, "download_artifacts"), \
                mock.patch.object(cloud_deferred.subprocess, "run", side_effect=self.fake_run), \
                mock.patch.object(cloud_deferred.cloud_pack, "pack_shard",
                                  return_value=(1, {"status": "failed", "error": "forbidden generated input"})), \
                mock.patch.object(cloud_deferred.cloud_reuse, "input_descriptor") as descriptor:
            result = self.call_pack()
        self.assertEqual(result["status"], "failed")
        self.assertIn("forbidden generated input", result["error"])
        descriptor.assert_not_called()

    def test_existing_source_is_rejected_before_restore(self):
        self.source.mkdir()
        with mock.patch.object(cloud_deferred.pipeline_code, "verify") as code, \
                mock.patch.object(cloud_deferred.cloud_artifacts, "download_artifacts") as download:
            with self.assertRaisesRegex(cloud_deferred.DeferredPackError, "cold source/OUT"):
                self.call_pack()
        code.assert_not_called()
        download.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_output_inside_source_is_rejected_before_restore(self):
        self.output = self.source / "capsule"
        with mock.patch.object(cloud_deferred.pipeline_code, "verify") as code, \
                mock.patch.object(cloud_deferred.cloud_artifacts, "download_artifacts") as download:
            with self.assertRaisesRegex(cloud_deferred.DeferredPackError, "paths must be separate"):
                self.call_pack()
        code.assert_not_called()
        download.assert_not_called()
        self.assertFalse(self.source.exists())

    def test_changed_source_manifest_is_rejected_before_restore(self):
        self.manifest_path.write_text("{}")
        with mock.patch.object(cloud_deferred.cloud_artifacts, "download_artifacts") as download:
            with self.assertRaisesRegex(cloud_deferred.DeferredPackError, "Source manifest differs"):
                self.call_pack()
        download.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
