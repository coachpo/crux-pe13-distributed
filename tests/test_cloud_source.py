import gzip
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
from unittest import mock


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
cloud_source = load("cloud_source", SCRIPTS / "cloud_source.py")
REAL_SUBPROCESS_RUN = subprocess.run


def tar_gz(members):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, kind, body, mode, target in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if kind == "directory":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = target
                archive.addfile(info)
            else:
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
    return gzip.compress(raw.getvalue())


class CloudSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.source_root = "/opt/crux/pe13"
        self.parent_revision = "1" * 40
        self.child_revision = "2" * 40
        self.manifest = {
            "schema_version": 1,
            "source_root": self.source_root,
            "projects": [
                {"path": "platform", "url": "https://github.com/example/platform.git",
                 "revision": self.parent_revision},
                {"path": "platform/vendor/device/feature", "url": "https://android.googlesource.com/example/feature",
                 "revision": self.child_revision},
            ],
            "layout": [
                {"type": "copy", "source": "platform/README", "destination": "platform/README.copy"},
                {"type": "link", "source": "platform/vendor/device/feature/new.txt",
                 "destination": "links/feature.txt"},
                {"type": "link", "source": "platform/vendor", "destination": "links/vendor"},
                {"type": "link", "source": "platform/vendor/upstream", "destination": "links/upstream"},
            ],
            "layers": [{"id": "source-core", "projects": [
                "platform", "platform/vendor/device/feature"]}],
        }
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.parent_archive = tar_gz([
            ("platform-root/", "directory", b"", 0o755, ""),
            ("platform-root/README", "file", b"parent", 0o640, ""),
            ("platform-root/vendor/", "directory", b"", 0o750, ""),
            ("platform-root/vendor/device/", "directory", b"", 0o755, ""),
            ("platform-root/vendor/upstream", "symlink", b"", 0o777, "../README"),
            ("platform-root/vendor/device/old.txt", "file", b"old", 0o644, ""),
        ])
        self.child_archive = tar_gz([
            ("new.txt", "file", b"child", 0o755, ""),
        ])

    def tearDown(self):
        self.temporary.cleanup()

    def responses(self, url, timeout=300):
        if "codeload.github.com" in url:
            return io.BytesIO(self.parent_archive)
        if "googlesource.com" in url:
            return io.BytesIO(self.child_archive)
        raise AssertionError("unexpected source URL: " + url)

    def fake_run(self, command, **kwargs):
        if command[0] == "mksquashfs":
            staging = Path(command[1]).parent / "projects"
            self.assertEqual(list(staging.iterdir()), [])
            Path(command[2]).write_bytes(b"fixture squashfs")
            return subprocess.CompletedProcess(command, 0, "created", "")
        return REAL_SUBPROCESS_RUN(command, **kwargs)

    def test_build_preserves_nested_project_overlay_layout_and_content_identity(self):
        with mock.patch.object(cloud_source.urllib.request, "urlopen", side_effect=self.responses), \
                mock.patch.object(cloud_source.shutil, "which", return_value="mksquashfs"), \
                mock.patch.object(cloud_source.subprocess, "run", side_effect=self.fake_run):
            receipt = cloud_source.build(self.manifest_path, "source-core", self.output)

        parts = self.output / "layer.sqfs.parts"
        self.assertEqual((parts / "part-00000").read_bytes(), b"fixture squashfs")
        self.assertEqual([project["revision"] for project in receipt["projects"]],
                         [self.parent_revision, self.child_revision])
        self.assertEqual(receipt["content"]["entry_count"], len(receipt["content"]["entries"]))
        self.assertEqual(receipt["content"]["sha256"],
                         cloud_source._canonical_digest(receipt["content"]["entries"]))
        entries = {entry["path"]: entry for entry in receipt["content"]["entries"]}
        self.assertEqual(entries["platform/README"]["mode"], 0o640)
        self.assertEqual(entries["platform/vendor"]["mode"], 0o750)
        self.assertEqual(entries["platform/vendor/upstream"]["target"], "../README")
        self.assertEqual(entries["platform/vendor/device/old.txt"]["type"], "file")
        self.assertEqual(entries["platform/vendor/device/feature/new.txt"]["mode"], 0o755)
        self.assertEqual(entries["platform/README.copy"]["sha256"], entries["platform/README"]["sha256"])
        self.assertEqual(entries["links/feature.txt"]["target"], "../platform/vendor/device/feature/new.txt")
        self.assertEqual(entries["links/vendor"]["type"], "symlink")
        self.assertEqual(entries["links/vendor"]["target"], "../platform/vendor")
        self.assertEqual(entries["links/upstream"]["target"], "../platform/vendor/upstream")
        self.assertLess(max(part["size"] for part in receipt["squashfs"]["parts"]),
                        cloud_source.MAX_PART_BYTES + 1)
        self.assertTrue((self.output / "layer.json").is_file())
        self.assertFalse((self.output / "layer.sqfs").exists())

    def test_rejects_unknown_project_and_unsafe_layer_path(self):
        invalid = json.loads(json.dumps(self.manifest))
        invalid["layers"][0]["projects"].append("platform/../../outside")
        with self.assertRaises(cloud_source.SourceError):
            cloud_source._validate_manifest(invalid)
        invalid = json.loads(json.dumps(self.manifest))
        invalid["layers"][0]["projects"].append("unknown/project")
        with self.assertRaises(cloud_source.SourceError):
            cloud_source._validate_manifest(invalid)

    def test_archive_failure_writes_project_failure_receipt(self):
        with mock.patch.object(cloud_source.urllib.request, "urlopen", return_value=io.BytesIO(b"not gzip")), \
                mock.patch.object(cloud_source.shutil, "which", return_value="mksquashfs"):
            with self.assertRaises(cloud_source.SourceError):
                cloud_source.build(self.manifest_path, "source-core", self.output)
        self.assertFalse((self.output / "layer.json").exists())
        failure = json.loads((self.output / "failure.json").read_text())
        self.assertEqual(failure["stage"], "source")
        self.assertEqual(failure["project"]["path"], "platform")
        self.assertEqual(failure["project"]["revision"], self.parent_revision)
        self.assertEqual(failure["project"]["archive_url"],
                         "https://codeload.github.com/example/platform/tar.gz/" + self.parent_revision)

    def test_retries_transient_http_error_once(self):
        failure = urllib.error.HTTPError("https://codeload.github.com/example/platform/tar.gz",
                                         503, "busy", None, None)
        urlopen = mock.Mock(side_effect=[failure, io.BytesIO(self.parent_archive),
                                        io.BytesIO(self.child_archive)])
        with mock.patch.object(cloud_source.urllib.request, "urlopen", urlopen), \
                mock.patch.object(cloud_source.time, "sleep"), \
                mock.patch.object(cloud_source.shutil, "which", return_value="mksquashfs"), \
                mock.patch.object(cloud_source.subprocess, "run", side_effect=self.fake_run):
            receipt = cloud_source.build(self.manifest_path, "source-core", self.output)
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(receipt["projects"][0]["status"], "fetched-and-extracted")

    def test_git_bundle_materializes_exact_pinned_commit(self):
        repository = self.root / "bundle-source"
        repository.mkdir()
        subprocess.run(["git", "init", str(repository)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "-C", str(repository), "config", "user.email", "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repository), "config", "user.name", "Fixture"], check=True)
        (repository / "bundle.txt").write_text("bundle content\n")
        subprocess.run(["git", "-C", str(repository), "add", "bundle.txt"], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-m", "fixture"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        revision = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"], check=True,
                                  text=True, stdout=subprocess.PIPE).stdout.strip()
        bundle = self.root / "project.bundle"
        subprocess.run(["git", "-C", str(repository), "bundle", "create", str(bundle), "HEAD"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        bundle_bytes = bundle.read_bytes()
        manifest = {
            "schema_version": 1, "source_root": self.source_root,
            "projects": [{"path": "external/fixture", "url": "https://example.invalid/fixture.bundle",
                          "archive_type": "git-bundle", "revision": revision}],
            "layers": [{"id": "fixture", "projects": ["external/fixture"]}],
        }
        manifest_path = self.root / "bundle-manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        def bundle_response(url, timeout=300):
            return io.BytesIO(bundle_bytes)

        with mock.patch.object(cloud_source.urllib.request, "urlopen", side_effect=bundle_response), \
                mock.patch.object(cloud_source.shutil, "which", return_value="mksquashfs"), \
                mock.patch.object(cloud_source.subprocess, "run", side_effect=self.fake_run):
            receipt = cloud_source.build(manifest_path, "fixture", self.output)
        self.assertEqual(receipt["projects"][0]["resolved_commit"], revision)
        entries = {entry["path"]: entry for entry in receipt["content"]["entries"]}
        self.assertEqual(entries["external/fixture/bundle.txt"]["size"], len("bundle content\n"))


if __name__ == "__main__":
    unittest.main()
