import contextlib
from copy import deepcopy
import io
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import dev_ota
import dev_release


class FakeReleases:
    def __init__(self):
        self.release = None
        self.refs = []
        self.assets = {}
        self.writes = []
        self.fail_asset = None
        self.leave_starter = False
        self.lose_publish_response = False

    def listed_releases(self):
        if self.release is None:
            return []
        return [dict(self.release, assets=list(self.assets.values()))]

    def api(self, endpoint, *, pages=False, method="GET", payload=None):
        if method == "GET":
            if endpoint == "releases?per_page=100" and pages:
                return deepcopy(self.listed_releases())
            if endpoint.startswith("git/matching-refs/tags/"):
                return deepcopy(self.refs)
            if endpoint == "releases/123/assets?per_page=100" and pages:
                return deepcopy(list(self.assets.values()))
            if endpoint == "releases/123":
                return deepcopy(self.release)
        elif method == "POST" and endpoint == "releases":
            assert self.release is None
            assert payload["target_commitish"] == "main"
            self.writes.append(("create", payload["tag_name"]))
            self.release = {**payload, "id": 123, "published_at": None}
            return deepcopy(self.release)
        elif method == "PATCH" and endpoint == "releases/123":
            assert self.release["draft"] is True
            assert payload == {"draft": False, "prerelease": True, "make_latest": "false"}
            self.writes.append(("publish", self.release["tag_name"]))
            self.release.update(payload, published_at="2026-11-05T16:10:00Z")
            self.refs = [{"ref": f"refs/tags/{self.release['tag_name']}", "object": {
                "type": "commit", "sha": "c" * 40, "url": "commit-url"}}]
            if self.lose_publish_response:
                raise OSError("connection closed after server processed the publish")
            return deepcopy(self.release)
        raise AssertionError((endpoint, method, pages))

    def upload(self, release_id, path):
        assert release_id == 123 and self.release["draft"] is True
        assert path.name not in self.assets, f"duplicate upload of {path.name}"
        self.writes.append(("upload", path.name))
        if self.fail_asset == path.name:
            if self.leave_starter:
                self.assets[path.name] = {"name": path.name, "state": "starter", "size": 0}
            raise OSError("interrupted upload")
        self.assets[path.name] = {
            "name": path.name, "state": "uploaded", "size": path.stat().st_size,
            "digest": "sha256:" + dev_ota.sha256(path),
        }
        return deepcopy(self.assets[path.name])


class DevReleaseTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory(prefix="crux-test-dev-release-")
        self.addCleanup(work.cleanup)
        delivery = Path(work.name)
        assets = {}
        for name in dev_ota.DELIVERY_NAMES:
            path = delivery / name
            path.write_bytes(f"verified asset: {name}\n".encode())
            assets[name] = {"size": path.stat().st_size, "digest": "sha256:" + dev_ota.sha256(path)}
        self.source = SimpleNamespace(
            tag="dev-pe13-2026110303-v1", directory=delivery, assembly_commit="a" * 40,
            assembly_run="42059779500", compile_run="42018017251", artifact_id=13431937735,
            artifact_digest="sha256:" + "b" * 64, assets=assets,
        )
        self.api = FakeReleases()

    def deliver(self, operation):
        with contextlib.redirect_stderr(io.StringIO()):
            return dev_release.deliver(self.api, self.source, operation)

    def channel(self):
        old_tag = "dev-pe13-2026101105-v3"
        old = {"tag_name": old_tag, "draft": False, "prerelease": True,
               "published_at": "2026-10-21T20:25:31Z", "assets": [
                   {"name": name, "size": 100, "state": "uploaded"}
                   for name in dev_ota.DELIVERY_NAMES]}
        with mock.patch.object(dev_ota, "gh_json", return_value=[self.api.listed_releases(), [old]]):
            return dev_ota.discover(dev_ota.REPOSITORY)

    def test_preflight_has_no_writes_and_publish_requires_a_draft(self):
        self.assertEqual(self.deliver("preflight"), {
            "state": "available", "missing": sorted(self.source.assets), "changed": False})
        with self.assertRaisesRegex(dev_ota.DevOtaError, "complete, verified draft"):
            self.deliver("publish")
        self.assertEqual(self.api.writes, [])

    def test_write_operations_cannot_be_run_by_the_local_cli(self):
        for operation in ("draft", "publish"):
            argv = ["dev_release.py", "--operation", operation,
                    "--tag", self.source.tag, "--assembly-run", self.source.assembly_run]
            with self.subTest(operation=operation), mock.patch.object(sys, "argv", argv), \
                    mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(dev_release, "verify_source") as source, \
                    contextlib.redirect_stderr(io.StringIO()) as error, \
                    self.assertRaises(SystemExit) as exit_error:
                dev_release.main()
            self.assertEqual(exit_error.exception.code, 2)
            self.assertIn("must be dispatched through release-dev.yml on main", error.getvalue())
            source.assert_not_called()

    def test_interrupted_draft_resumes_missing_assets_only_then_becomes_discoverable(self):
        old_tag = dev_ota.select_head(self.channel())["tag"]
        self.api.fail_asset = "vendor.img"
        with self.assertRaisesRegex(OSError, "interrupted upload"):
            self.deliver("draft")
        self.assertEqual(set(self.api.assets), set(self.source.assets) - {"vendor.img"})
        self.assertEqual(dev_ota.select_head(self.channel())["tag"], old_tag)

        self.api.writes.clear()
        self.assertEqual(self.deliver("preflight")["missing"], ["vendor.img"])
        with self.assertRaisesRegex(dev_ota.DevOtaError, "complete, verified draft"):
            self.deliver("publish")
        self.assertEqual(self.api.writes, [])

        self.api.fail_asset = None
        self.assertEqual(self.deliver("draft"), {"state": "draft", "release_id": 123, "changed": True})
        self.assertEqual(self.api.writes, [("upload", "vendor.img")])
        self.assertEqual({name: {key: asset[key] for key in ("digest", "size")}
                          for name, asset in self.api.assets.items()}, self.source.assets)
        self.assertEqual(dev_ota.select_head(self.channel())["tag"], old_tag)

        self.api.writes.clear()
        self.assertFalse(self.deliver("draft")["changed"])
        self.assertEqual(self.api.writes, [])
        self.assertTrue(self.deliver("publish")["changed"])
        self.assertEqual(self.api.writes, [("publish", self.source.tag)])
        releases = self.channel()
        self.assertEqual(dev_ota.select_head(releases)["tag"], self.source.tag)
        self.assertEqual(dev_ota.select_head(releases, old_tag)["tag"], old_tag)
        self.api.writes.clear()
        for operation in ("preflight", "draft", "publish"):
            self.assertFalse(self.deliver(operation)["changed"])
        self.assertEqual(self.api.writes, [])

    def test_starter_asset_and_conflicting_digest_block_mutation(self):
        self.api.fail_asset = "vendor.img"
        self.api.leave_starter = True
        with self.assertRaisesRegex(OSError, "interrupted upload"):
            self.deliver("draft")
        self.api.writes.clear()
        expected = self.source.assets["vendor.img"]
        for state, digest in (("starter", expected["digest"]), ("uploaded", "sha256:" + "0" * 64)):
            self.api.assets["vendor.img"] = {
                "name": "vendor.img", "state": state, "size": expected["size"], "digest": digest}
            for operation in ("preflight", "draft", "publish"):
                with self.subTest(state=state, operation=operation), \
                        self.assertRaisesRegex(dev_ota.DevOtaError, "asset conflict for vendor.img"):
                    self.deliver(operation)
        self.assertEqual(self.api.writes, [])
        self.assertTrue(self.api.release["draft"])

    def test_existing_tag_without_release_cannot_be_taken_over(self):
        self.api.refs = [{"ref": f"refs/tags/{self.source.tag}", "object": {
            "type": "commit", "sha": self.source.assembly_commit, "url": "commit-url"}}]
        with self.assertRaisesRegex(dev_ota.DevOtaError, "Git tag exists"):
            self.deliver("draft")
        self.assertEqual(self.api.writes, [])

    def test_different_source_cannot_reuse_a_draft_tag(self):
        self.deliver("draft")
        self.source.artifact_id += 1
        self.api.writes.clear()
        for operation in ("preflight", "draft", "publish"):
            with self.subTest(operation=operation), \
                    self.assertRaisesRegex(dev_ota.DevOtaError, "source identity differs"):
                self.deliver(operation)
        self.assertEqual(self.api.writes, [])

    def test_existing_alias_cannot_reuse_the_same_numeric_version(self):
        self.deliver("draft")
        self.api.release["tag_name"] = "dev-pe13-2026110303-v01"
        self.api.writes.clear()
        with self.assertRaisesRegex(dev_ota.DevOtaError, "same dev version"):
            self.deliver("draft")
        self.assertEqual(self.api.writes, [])

    def test_incomplete_published_release_is_neither_repaired_nor_selected(self):
        old_tag = dev_ota.select_head(self.channel())["tag"]
        self.deliver("draft")
        self.deliver("publish")
        del self.api.assets["boot.img"]
        self.api.writes.clear()
        for operation in ("draft", "publish"):
            with self.subTest(operation=operation), \
                    self.assertRaisesRegex(dev_ota.DevOtaError, "published delivery is incomplete"):
                self.deliver(operation)
        self.assertEqual(dev_ota.select_head(self.channel())["tag"], old_tag)
        self.assertEqual(self.api.writes, [])

    def test_lost_publish_response_is_reported_and_same_inputs_reconcile(self):
        self.deliver("draft")
        self.api.lose_publish_response = True
        with self.assertRaisesRegex(dev_ota.DevOtaError, "publish not confirmed.*inspect"):
            self.deliver("publish")
        self.api.writes.clear()
        self.assertEqual(self.deliver("publish"), {
            "state": "published", "release_id": 123, "changed": False})
        self.assertEqual(self.api.writes, [])


if __name__ == "__main__":
    unittest.main()
