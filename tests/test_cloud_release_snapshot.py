"""Test locked source snapshots with a real Git archive and mocked HTTPS."""

import gzip
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cloud_release_snapshot as snapshot
import cloud_source
import cloud_source_manifest
from cloud_source_reuse import digest_bytes, layer_contract_sha256


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        source = Path(temporary.name)
        subprocess.run(["git", "init", "-q", source], check=True)
        (source / "payload.txt").write_text("locked release source\n")
        subprocess.run(["git", "-C", source, "add", "payload.txt"], check=True)
        subprocess.run(["git", "-C", source, "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
                       check=True)
        cls.revision = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"],
                                               text=True).strip()
        cls.tree = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD^{tree}"],
                                           text=True).strip()
        cls.archive = gzip.compress(subprocess.check_output(["git", "-C", source, "archive", "HEAD"]))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        repository = snapshot.PROJECT_REPOSITORIES["vendor/gms"]
        self.lock = {
            "repository": repository, "repository_id": 1, "release_id": 2, "asset_id": 3,
            "tag": f"snapshot-{self.revision}", "asset_name": f"vendor-gms-{self.revision}.tar.gz",
            "size_bytes": len(self.archive), "sha256": digest_bytes(self.archive),
        }
        self.project = {
            "path": "vendor/gms", "revision": self.revision, "expected_tree": self.tree,
            "archive_type": snapshot.ARCHIVE_TYPE, "url": f"https://github.com/{repository}",
            "release_snapshot": self.lock,
        }
        self.manifest = {
            "schema_version": 1, "source_root": "/source", "projects": [self.project],
            "layers": [{"id": "source-15", "projects": ["vendor/gms"]}],
        }

    def opener(self, archive=None):
        repository = {"id": self.lock["repository_id"], "private": True, "full_name": self.lock["repository"]}
        release = {"id": self.lock["release_id"], "draft": False, "tag_name": self.lock["tag"], "assets": [{
            "id": self.lock["asset_id"], "state": "uploaded", "name": self.lock["asset_name"],
            "size": self.lock["size_bytes"], "digest": "sha256:" + self.lock["sha256"],
        }]}
        opener = mock.Mock()
        opener.open.side_effect = [
            io.BytesIO(json.dumps(repository).encode()), io.BytesIO(json.dumps(release).encode()),
            io.BytesIO(archive if archive is not None else self.archive),
        ]
        return opener

    def extract(self, opener):
        with mock.patch.object(snapshot.urllib.request, "build_opener", return_value=opener):
            _, projects, _, _ = cloud_source._validate_manifest(self.manifest)
            return cloud_source._extract_archive(projects["vendor/gms"], self.root / "tree", 10,
                                                 snapshot_token="fixture-token")

    def test_locked_tar_and_tree(self):
        opener = self.opener()
        receipt, _ = self.extract(opener)
        self.assertEqual(receipt["resolved_commit"], self.revision)
        self.assertEqual(receipt["release_snapshot"], self.lock)
        _, tree = cloud_source._apply_project_patches(self.project, self.root / "tree")
        self.assertEqual(tree, self.tree)
        self.assertEqual((self.root / "tree/payload.txt").read_text(), "locked release source\n")
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual([r.full_url for r in requests], [
            f"{snapshot.API}/repos/{self.lock['repository']}",
            f"{snapshot.API}/repos/{self.lock['repository']}/releases/tags/{self.lock['tag']}",
            snapshot.asset_url(self.lock),
        ])
        self.assertEqual(requests[-1].get_header("Accept"), "application/octet-stream")

    def test_wrong_bytes_stop_before_extraction(self):
        with self.assertRaisesRegex(cloud_source.SourceError, "asset SHA-256 differs"):
            self.extract(self.opener(b"!" + self.archive[1:]))
        self.assertFalse((self.root / "tree").exists())

    def test_wrong_tree_fails_before_layer_merge(self):
        self.project["expected_tree"] = "0" * 40
        self.extract(self.opener())
        with self.assertRaisesRegex(cloud_source.SourceError, "reconstructed Git tree mismatch"):
            cloud_source._apply_project_patches(self.project, self.root / "tree")

    def test_rejects_archive_with_different_pax_commit_even_if_bytes_match_lock(self):
        tampered = gzip.compress(gzip.decompress(self.archive).replace(
            self.revision.encode(), ("0" * 40).encode(), 1))
        self.lock.update(size_bytes=len(tampered), sha256=digest_bytes(tampered))
        with self.assertRaisesRegex(cloud_source.SourceError, "git archive commit differs"):
            self.extract(self.opener(tampered))

    def test_redirect_has_no_authorization_and_requires_https(self):
        request = snapshot._request(snapshot.asset_url(self.lock), "application/octet-stream", "fixture-token")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")
        handler = snapshot.HTTPSRedirect()
        redirect = handler.redirect_request(request, None, 302, "Found", {},
                                            "https://release-assets.githubusercontent.com/file?signature=fixture")
        self.assertIsNone(redirect.get_header("Authorization"))
        with self.assertRaisesRegex(snapshot.SnapshotError, "requires HTTPS"):
            handler.redirect_request(request, None, 302, "Found", {}, "http://example.invalid/file")

    def test_http_failure_does_not_expose_the_redirect_url(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://release-assets.githubusercontent.com/secret?sig=fixture", 403, "denied", {}, None)
        with mock.patch.object(snapshot.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(snapshot.SnapshotError) as error:
                snapshot.download(self.project, self.root / "asset", "fixture-token", 10)
        self.assertEqual(str(error.exception), "Private snapshot API returned HTTP 403")

    def test_no_legacy_provider_and_reuse_tracks_asset_identity(self):
        original_contract = layer_contract_sha256(self.manifest, "source-15")
        self.lock["asset_id"] += 1
        self.assertNotEqual(layer_contract_sha256(self.manifest, "source-15"), original_contract)
        self.project.update(archive_type="git-bundle", url="https://archive.org/retired.bundle")
        with self.assertRaisesRegex(cloud_source.SourceError, "private Release snapshot is required"):
            cloud_source._validate_manifest(self.manifest)

    def test_missing_secret_never_starts_snapshot_layer(self):
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(cloud_source.SourceError, "missing CRUX_SOURCE_SNAPSHOTS_TOKEN"):
            cloud_source.build(manifest_path, "source-15", self.root / "output")
        self.assertFalse((self.root / "output").exists())

    def test_generator_applies_canonical_pins_with_and_without_xml_rows(self):
        root = Path(__file__).resolve().parents[1]
        active = json.loads((root / "config/cloud-source-overrides.json").read_text())["projects"]
        overrides = {path: active[path] for path in snapshot.PROJECT_REPOSITORIES}
        # Exercise a new lock to ensure the generator does not also pin the
        # current source revision in Python.
        overrides["vendor/gms"] = {k: v for k, v in self.project.items() if k != "path"}
        override_path = self.root / "overrides.json"
        override_path.write_text(json.dumps({"projects": overrides}))
        layout_path = self.root / "layout.json"
        layout_path.write_text('{"links":[]}')
        xml_path = self.root / "repo.xml"
        xml_path.write_text('<manifest><remote name="r" fetch="https://example.invalid"/>'
                            '<default remote="r"/><project name="vendor/gms" revision="' + "e" * 40
                            + '"/></manifest>')
        manifest = cloud_source_manifest.convert(xml_path, layout_path, 32, override_path)
        self.assertEqual({p["path"]: p["revision"] for p in manifest["projects"]},
                         {path: pin["revision"] for path, pin in overrides.items()})
        overrides.pop("external/faceunlock")
        override_path.write_text(json.dumps({"projects": overrides}))
        with self.assertRaisesRegex(ValueError, "Missing reviewed private Release snapshot override"):
            cloud_source_manifest.convert(xml_path, layout_path, 32, override_path)


if __name__ == "__main__":
    unittest.main()
