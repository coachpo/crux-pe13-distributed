import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("cloud_source_reuse", SCRIPTS / "cloud_source_reuse.py")
cloud_source_reuse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_source_reuse)


class CloudSourceReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest = {
            "schema_version": 1,
            "source_root": "/mnt/pe13",
            "projects": [
                {"path": "build/soong", "revision": "a" * 40,
                 "url": "https://example.invalid/soong"},
                {"path": "build/soong/vendor", "revision": "b" * 40,
                 "url": "https://example.invalid/nested"},
                {"path": "vendor/crux", "revision": "c" * 40,
                 "url": "https://example.invalid/crux", "archive_revision": "d" * 40,
                 "patches": [{"path": "patches/fix.patch", "sha256": "e" * 64}],
                 "expected_tree": "f" * 40},
                {"path": "system/libfoo", "revision": "9" * 40,
                 "url": "https://example.invalid/libfoo"},
            ],
            "layers": [
                {"id": "source-00", "projects": ["build/soong", "vendor/crux"]},
                {"id": "source-01", "projects": ["build/soong/vendor", "system/libfoo"]},
            ],
            "layout": [
                {"type": "link", "source": "build/soong/root.bp", "destination": "Android.bp"},
                {"type": "copy", "source": "vendor/crux/root.mk", "destination": "vendor.mk"},
                {"type": "link", "source": "build/soong/vendor/nested.bp", "destination": "nested.bp"},
            ],
        }
        self.manifest_path = self.root / "manifest.json"
        self.write_manifest()
        self.output = self.root / "reused-layer"
        self.source_run = "41664251881"
        self.repository = "owner/repo"
        self.old_bytes = self.manifest_path.read_bytes()
        self.artifact_receipt_bytes, self.part_bytes = self.make_artifact()
        self.download_calls = 0
        self.prior_reuse = False

    def tearDown(self):
        self.temporary.cleanup()

    def write_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest, sort_keys=True) + "\n")

    def make_artifact(self):
        part_bytes = b"compressed-squashfs-layer"
        receipt = {
            "schema_version": 1,
            "manifest_sha256": hashlib.sha256(self.manifest_path.read_bytes()).hexdigest(),
            "source_root": self.manifest["source_root"],
            "layer_id": "source-00",
            "projects": [
                {"path": "build/soong", "revision": "a" * 40,
                 "status": "fetched-and-extracted"},
                {"path": "vendor/crux", "revision": "c" * 40,
                 "status": "fetched-git-and-archived"},
            ],
            "squashfs": {
                "parts_directory": "layer.sqfs.parts",
                "parts": [{"name": "part-00000", "size": len(part_bytes),
                           "sha256": hashlib.sha256(part_bytes).hexdigest()}],
                "size": len(part_bytes),
            },
        }
        return json.dumps(receipt, indent=2, sort_keys=True).encode() + b"\n", part_bytes

    def fake_gh(self, command, **kwargs):
        if command[:2] == ["gh", "api"]:
            route = command[4]
            if "/actions/runs/" in route:
                return subprocess.CompletedProcess(command, 0, json.dumps({"head_sha": "1" * 40}), "")
            self.assertIn("contents/config/cloud-source-manifest.json", route)
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "encoding": "base64", "content": base64.b64encode(self.old_bytes).decode()
            }), "")
        if command[:3] == ["gh", "run", "download"]:
            self.download_calls += 1
            artifact_dir = Path(command[command.index("--dir") + 1])
            (artifact_dir / "layer.json").write_bytes(self.artifact_receipt_bytes)
            if self.prior_reuse:
                (artifact_dir / "reused-layer-original.json").write_text('{}')
            parts = artifact_dir / "layer.sqfs.parts"
            parts.mkdir()
            (parts / "part-00000").write_bytes(self.part_bytes)
            return subprocess.CompletedProcess(command, 0, "", "")
        self.fail("unexpected gh command: " + repr(command))

    def reuse(self):
        with mock.patch.object(cloud_source_reuse.subprocess, "run", side_effect=self.fake_gh):
            return cloud_source_reuse.reuse_layer(self.manifest_path, "source-00", self.source_run,
                                                   self.repository, self.output)

    def test_contract_scopes_layout_by_longest_project_owner(self):
        contract = cloud_source_reuse.layer_contract(self.manifest, "source-00")
        self.assertEqual([item["source"] for item in contract["layout"]],
                         ["build/soong/root.bp", "vendor/crux/root.mk"])

    def test_wrong_fork_url_invalidates_contract(self):
        before = cloud_source_reuse.layer_contract_sha256(self.manifest, "source-00")
        changed = json.loads(json.dumps(self.manifest))
        next(project for project in changed["projects"] if project["path"] == "vendor/crux")["url"] = \
            "https://example.invalid/wrong-fork"
        self.assertNotEqual(before, cloud_source_reuse.layer_contract_sha256(changed, "source-00"))

    def test_patch_change_invalidates_contract(self):
        before = cloud_source_reuse.layer_contract_sha256(self.manifest, "source-00")
        changed = json.loads(json.dumps(self.manifest))
        next(project for project in changed["projects"] if project["path"] == "vendor/crux")["patches"][0]["sha256"] = "0" * 64
        self.assertNotEqual(before, cloud_source_reuse.layer_contract_sha256(changed, "source-00"))

    def test_owned_layout_change_invalidates_contract(self):
        before = cloud_source_reuse.layer_contract_sha256(self.manifest, "source-00")
        changed = json.loads(json.dumps(self.manifest))
        changed["layout"][0]["destination"] = "different.bp"
        self.assertNotEqual(before, cloud_source_reuse.layer_contract_sha256(changed, "source-00"))

    def test_untouched_layer_reuses_verified_parts_and_binds_new_receipt(self):
        self.manifest['projects'][-1]['revision'] = '8' * 40
        self.write_manifest()
        receipt = self.reuse()
        self.assertEqual(receipt["manifest_sha256"], hashlib.sha256(self.manifest_path.read_bytes()).hexdigest())
        self.assertEqual(receipt["reuse"]["source_run"], self.source_run)
        self.assertEqual(receipt["reuse"]["original_manifest_sha256"], hashlib.sha256(self.old_bytes).hexdigest())
        self.assertEqual(receipt["reuse"]["original_receipt_sha256"],
                         hashlib.sha256(self.artifact_receipt_bytes).hexdigest())
        self.assertEqual((self.output / "reused-layer-original.json").read_bytes(), self.artifact_receipt_bytes)
        self.assertEqual((self.output / "layer.sqfs.parts/part-00000").read_bytes(), self.part_bytes)
        self.assertEqual(json.loads((self.output / "layer.json").read_text()), receipt)
        self.assertEqual(self.download_calls, 1)

    def test_a_verified_reused_layer_can_be_reused_again(self):
        self.prior_reuse = True
        receipt = self.reuse()
        self.assertEqual((self.output / 'reused-layer-original.json').read_bytes(), self.artifact_receipt_bytes)
        self.assertEqual(receipt['reuse']['source_run'], self.source_run)

    def test_contract_change_refuses_output_before_artifact_download(self):
        self.manifest["projects"][0]["url"] = "https://example.invalid/replaced"
        self.write_manifest()
        with self.assertRaises(cloud_source_reuse.ReuseError):
            self.reuse()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.download_calls, 0)

    def test_corrupt_part_is_rejected_without_output(self):
        original = self.fake_gh

        def corrupt(command, **kwargs):
            result = original(command, **kwargs)
            if command[:3] == ["gh", "run", "download"]:
                part = Path(command[command.index("--dir") + 1]) / "layer.sqfs.parts/part-00000"
                part.write_bytes(b"tampered")
            return result

        with mock.patch.object(cloud_source_reuse.subprocess, "run", side_effect=corrupt):
            with self.assertRaises(cloud_source_reuse.ReuseError):
                cloud_source_reuse.reuse_layer(self.manifest_path, "source-00", self.source_run,
                                               self.repository, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_prior_artifact_fails_without_creating_output(self):
        original = self.fake_gh

        def missing_artifact(command, **kwargs):
            if command[:3] == ["gh", "run", "download"]:
                self.download_calls += 1
                return subprocess.CompletedProcess(command, 1, "", "artifact not found")
            return original(command, **kwargs)

        with mock.patch.object(cloud_source_reuse.subprocess, "run", side_effect=missing_artifact):
            with self.assertRaisesRegex(cloud_source_reuse.ReuseError, "build it fresh"):
                cloud_source_reuse.reuse_layer(self.manifest_path, "source-00", self.source_run,
                                               self.repository, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.download_calls, 1)


if __name__ == "__main__":
    unittest.main()
