import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import release_zip
from release_source import ReuseContentError, SourceError, sha256


class FakeReleases:
    def __init__(self):
        self.release = None
        self.refs = []
        self.assets = {}
        self.writes = []
        self.fail_asset = None
        self.leave_starter = False
        self.lose_publish_response = False

    def api(self, endpoint, *, pages=False, method="GET", payload=None):
        if method == "GET":
            if endpoint == "releases?per_page=100" and pages:
                return [] if self.release is None else [deepcopy(self.release)]
            if endpoint.startswith("git/matching-refs/tags/"):
                return deepcopy(self.refs)
            if endpoint == "releases/123/assets?per_page=100" and pages:
                return deepcopy(list(self.assets.values()))
            if endpoint == "releases/123":
                return deepcopy(self.release)
        elif method == "POST" and endpoint == "releases":
            assert self.release is None and payload["target_commitish"] == "main"
            self.writes.append(("create", payload["tag_name"]))
            self.release = {**payload, "id": 123, "published_at": None}
            return deepcopy(self.release)
        elif method == "PATCH" and endpoint == "releases/123":
            assert self.release["draft"] is True
            assert payload == {"draft": False, "prerelease": True, "make_latest": "false"}
            self.writes.append(("publish", self.release["tag_name"]))
            self.release.update(payload, published_at="2026-11-09T16:10:00Z")
            self.refs = [{"ref": f"refs/tags/{self.release['tag_name']}",
                          "object": {"type": "commit", "sha": "c" * 40, "url": "commit-url"}}]
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
        self.assets[path.name] = {"name": path.name, "state": "uploaded", "size": path.stat().st_size,
                                  "digest": "sha256:" + sha256(path)}
        return deepcopy(self.assets[path.name])


class ReleaseZipTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory(prefix="crux-test-release-")
        self.addCleanup(work.cleanup)
        self.staged = Path(work.name)
        self.assets = {}
        for name in ("crux-pe13-2026110303.zip", "crux-pe13-2026110303.zip.sha256", "README.txt",
                     "twrp-b-install.itb", "twrp-b-install.itb.sha256"):
            path = self.staged / name
            path.write_bytes(f"verified asset: {name}\n".encode())
            self.assets[name] = {"size": path.stat().st_size, "digest": "sha256:" + sha256(path)}
        self.source = SimpleNamespace(tag="crux-pe13-2026110303-v1", build="2026110303", assembly_commit="a" * 40,
                                      assembly_run="42059779500", compile_run="42018017251",
                                      artifact_id=13431937735, artifact_digest="sha256:" + "b" * 64,
                                      manifest_sha256="d" * 64,
                                      reuse_provenance={"w000-s001": {"mode": "reused", "key": "e" * 64,
                                          "origin": {"run_id": "41900000001", "worker_commit": "c" * 40,
                                                     "artifact_id": 123456, "artifact_digest": "sha256:" + "f" * 64}}})
        self.api = FakeReleases()

    def deliver(self, operation):
        with contextlib.redirect_stderr(io.StringIO()):
            return release_zip.deliver(self.api, self.source, self.staged, self.assets, operation)

    def test_preflight_has_no_writes_and_publish_requires_a_draft(self):
        self.assertEqual(self.deliver("preflight"),
                         {"state": "available", "missing": sorted(self.assets), "changed": False})
        with self.assertRaisesRegex(release_zip.ReleaseError, "complete, verified draft"):
            self.deliver("publish")
        self.assertEqual(self.api.writes, [])

    def test_write_operations_cannot_be_run_by_the_local_cli(self):
        for operation in ("draft", "publish"):
            argv = ["release_zip.py", "--operation", operation, "--tag", self.source.tag,
                    "--assembly-run", self.source.assembly_run, "--signature", "a" * 128]
            with self.subTest(operation=operation), mock.patch.object(sys, "argv", argv), \
                    mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(release_zip, "verify_source") as verify, \
                    contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as exit_error:
                release_zip.main()
            self.assertEqual(exit_error.exception.code, 2)
            self.assertIn("must be dispatched through release.yml on main", error.getvalue())
            verify.assert_not_called()

    def test_interrupted_draft_resumes_missing_assets_only(self):
        self.api.fail_asset = "twrp-b-install.itb"
        with self.assertRaisesRegex(OSError, "interrupted upload"):
            self.deliver("draft")
        later = {"twrp-b-install.itb", "twrp-b-install.itb.sha256"}
        self.assertEqual(set(self.api.assets), set(self.assets) - later)
        self.api.writes.clear()
        self.assertEqual(self.deliver("preflight")["missing"], sorted(later))
        with self.assertRaisesRegex(release_zip.ReleaseError, "complete, verified draft"):
            self.deliver("publish")
        self.api.fail_asset = None
        self.assertEqual(self.deliver("draft"), {"state": "draft", "release_id": 123, "changed": True})
        self.assertEqual(self.api.writes, [("upload", name) for name in sorted(later)])
        self.api.writes.clear()
        self.assertFalse(self.deliver("draft")["changed"])
        self.assertTrue(self.deliver("publish")["changed"])
        self.assertEqual(self.api.writes, [("publish", self.source.tag)])
        self.api.writes.clear()
        for operation in ("preflight", "draft", "publish"):
            self.assertFalse(self.deliver(operation)["changed"])
        self.assertEqual(self.api.writes, [])

    def test_starter_asset_and_conflicting_digest_block_mutation(self):
        self.api.fail_asset = "README.txt"
        self.api.leave_starter = True
        with self.assertRaisesRegex(OSError, "interrupted upload"):
            self.deliver("draft")
        self.api.writes.clear()
        expected = self.assets["README.txt"]
        for state, digest in (("starter", expected["digest"]), ("uploaded", "sha256:" + "0" * 64)):
            self.api.assets["README.txt"] = {"name": "README.txt", "state": state, "size": expected["size"],
                                             "digest": digest}
            for operation in ("preflight", "draft", "publish"):
                with self.subTest(state=state, operation=operation), \
                        self.assertRaisesRegex(release_zip.ReleaseError, "asset conflict for README.txt"):
                    self.deliver(operation)
        self.assertEqual(self.api.writes, [])

    def test_existing_tag_or_other_source_cannot_be_taken_over(self):
        self.api.refs = [{"ref": f"refs/tags/{self.source.tag}",
                          "object": {"type": "commit", "sha": "a" * 40, "url": "commit-url"}}]
        with self.assertRaisesRegex(release_zip.ReleaseError, "Git tag exists"):
            self.deliver("draft")
        self.api.refs = []
        self.deliver("draft")
        self.source.manifest_sha256 = "e" * 64
        self.api.writes.clear()
        for operation in ("preflight", "draft", "publish"):
            with self.subTest(operation=operation), \
                    self.assertRaisesRegex(release_zip.ReleaseError, "source identity differs"):
                self.deliver(operation)
        self.assertEqual(self.api.writes, [])

    def test_incomplete_published_release_is_not_repaired(self):
        self.deliver("draft")
        self.deliver("publish")
        del self.api.assets["README.txt"]
        self.api.writes.clear()
        for operation in ("draft", "publish"):
            with self.subTest(operation=operation), \
                    self.assertRaisesRegex(release_zip.ReleaseError, "published release is incomplete"):
                self.deliver(operation)
        self.assertEqual(self.api.writes, [])

    def test_lost_publish_response_is_reported_and_same_inputs_reconcile(self):
        self.deliver("draft")
        self.api.lose_publish_response = True
        with self.assertRaisesRegex(release_zip.ReleaseError, "publish not confirmed.*inspect"):
            self.deliver("publish")
        self.api.writes.clear()
        self.assertEqual(self.deliver("publish"), {"state": "published", "release_id": 123, "changed": False})
        self.assertEqual(self.api.writes, [])

    def test_tag_is_this_assemblys_release_or_the_next_free_version(self):
        build, assembly = self.source.build, self.source.assembly_run
        self.assertEqual(release_zip.next_tag(self.api, build, assembly), "crux-pe13-2026110303-v1")
        self.deliver("draft")
        self.assertEqual(release_zip.next_tag(self.api, build, assembly), self.source.tag)
        self.assertEqual(release_zip.next_tag(self.api, build, "42059779501"), "crux-pe13-2026110303-v2")
        self.api.refs = [{"ref": "refs/tags/crux-pe13-2026110303-v3", "object": {"type": "commit"}}]
        self.assertEqual(release_zip.next_tag(self.api, build, "42059779501"), "crux-pe13-2026110303-v4")
        self.assertEqual(release_zip.next_tag(self.api, "2026110304", assembly), "crux-pe13-2026110304-v1")

    def test_release_body_persists_validated_task_reuse_origin(self):
        body = release_zip.release_body(self.source)
        self.assertIn("Compile and verified shard origins", body)
        self.assertIn('"mode": "reused"', body)
        self.assertIn('"key": "' + "e" * 64 + '"', body)
        self.assertIn('"run_id": "41900000001"', body)
        self.assertIn('"artifact_digest": "sha256:' + "f" * 64 + '"', body)

    def test_release_operation_drafts_and_publishes_the_selected_tag(self):
        argv = ["release_zip.py", "--operation", "release", "--assembly-run", self.source.assembly_run,
                "--signature", "a" * 128]
        environment = {"GITHUB_WORKFLOW_REF": f"{release_zip.REPOSITORY}/.github/workflows/release.yml@refs/heads/main",
                       "GITHUB_EVENT_NAME": "workflow_dispatch", "RUNNER_ENVIRONMENT": "github-hosted"}
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(release_zip, "Releases", return_value=self.api), \
                mock.patch.object(release_zip, "assembly_build", return_value=self.source.build), \
                mock.patch.object(release_zip, "verify_source", return_value=self.source) as verify, \
                mock.patch.object(release_zip, "stage_assets", return_value=(self.staged, self.assets)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(release_zip.main(), 0)
        self.assertEqual(verify.call_args.args[1], "crux-pe13-2026110303-v1")
        self.assertEqual([write[0] for write in self.api.writes],
                         ["create"] + ["upload"] * len(self.assets) + ["publish"])
        self.assertFalse(self.api.release["draft"])

    def test_unpinned_install_twrp_stops_before_any_upload(self):
        with TemporaryDirectory() as directory, \
                self.assertRaisesRegex(release_zip.ReleaseError, "has no pinned asset yet"):
            release_zip.fetch_twrp(directory, {"release_tag": "crux-install-twrp-v3", "asset_id": None})

    def test_only_verified_reuse_content_failures_create_preflight_retry_evidence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "preflight-validation-failure.json"
            error = ReuseContentError("payload bytes differ", execution_id="42018017251",
                                      worker_commit="a" * 40, build_number="2026110303",
                                      reused_tasks=["w000-s001"])
            evidence = release_zip.write_preflight_retry_evidence(error, path)
            self.assertEqual(json.loads(path.read_text()), evidence)
            self.assertEqual(evidence, {
                "schema_version": 1, "status": "failed", "failure_class": "content-validation",
                "stage": "preflight", "execution_id": "42018017251", "worker_commit": "a" * 40,
                "build_number": "2026110303", "reused_tasks": ["w000-s001"],
                "error": "payload bytes differ"})
            path.unlink()
            with self.assertRaisesRegex(release_zip.ReleaseError, "only a verified reused-content"):
                release_zip.write_preflight_retry_evidence(SourceError("metadata request failed"), path)
            self.assertFalse(path.exists())

    def test_release_operation_records_only_typed_failure_before_release_writes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "preflight-validation-failure.json"
            error = ReuseContentError("delivery content differs", execution_id=self.source.compile_run,
                                      worker_commit="a" * 40, build_number=self.source.build,
                                      reused_tasks=["w000-s001"])
            argv = ["release_zip.py", "--operation", "release", "--assembly-run", self.source.assembly_run,
                    "--tag", self.source.tag, "--signature", "a" * 128,
                    "--retry-evidence", str(path)]
            environment = {"GITHUB_WORKFLOW_REF":
                           f"{release_zip.REPOSITORY}/.github/workflows/release.yml@refs/heads/main",
                           "GITHUB_EVENT_NAME": "workflow_dispatch", "RUNNER_ENVIRONMENT": "github-hosted"}
            with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(release_zip, "Releases", return_value=self.api), \
                    mock.patch.object(release_zip, "verify_source", side_effect=error), \
                    mock.patch.object(release_zip, "stage_assets") as stage, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                release_zip.main()
            self.assertEqual(json.loads(path.read_text())["execution_id"], self.source.compile_run)
            stage.assert_not_called()
            self.assertEqual(self.api.writes, [])

    def test_publish_api_failure_does_not_create_retry_evidence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "preflight-validation-failure.json"
            argv = ["release_zip.py", "--operation", "publish", "--assembly-run", self.source.assembly_run,
                    "--tag", self.source.tag, "--signature", "a" * 128]
            environment = {"GITHUB_WORKFLOW_REF":
                           f"{release_zip.REPOSITORY}/.github/workflows/release.yml@refs/heads/main",
                           "GITHUB_EVENT_NAME": "workflow_dispatch", "RUNNER_ENVIRONMENT": "github-hosted"}
            with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(release_zip, "Releases", return_value=self.api), \
                    mock.patch.object(release_zip, "verify_source", return_value=self.source), \
                    mock.patch.object(release_zip, "stage_assets", return_value=(self.staged, self.assets)), \
                    mock.patch.object(release_zip, "deliver", side_effect=release_zip.ReleaseError("publish not confirmed")), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                release_zip.main()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
