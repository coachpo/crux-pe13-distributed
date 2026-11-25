"""Host fixtures for checkout-from-lock.py on local Git remotes, checked with check_local_source.py."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
TOOL = HERE / "checkout-from-lock.py"
CHECK = HERE / "check_local_source.py"


def git(directory, *argv):
    return subprocess.run(["git", "-C", str(directory), *argv], check=True, capture_output=True,
                          text=True).stdout.strip()


def remote(root, name, files):
    """A remote whose locked revision is not its branch tip, so it is fetched by SHA."""
    path = root / "remotes" / name
    path.mkdir(parents=True)
    git(path, "init", "--quiet", "-b", "main")
    git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    for key, value in (("user.name", "t"), ("user.email", "t@example.invalid")):
        git(path, "config", key, value)
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "locked")
    revision = git(path, "rev-parse", "HEAD")
    tree = git(path, "rev-parse", "HEAD^{tree}")
    (path / "later.txt").write_text("after the lock\n")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "later")
    return path, revision, tree


class CheckoutFromLock(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="crux-lock-"))
        self.addCleanup(shutil.rmtree, self.root)
        make, make_rev, _ = remote(self.root, "make", {"core/root.mk": "root\n", "root.bp": "bp\n"})
        child, child_rev, _ = remote(self.root, "child", {"child.txt": "nested\n"})
        kern, kern_rev, _ = remote(self.root, "kernel", {"Makefile": "kernel\n"})
        gms, gms_rev, gms_tree = remote(self.root, "gms", {"gms.mk": "gms\n"})
        self.bundle = self.root / "gms.bundle"
        git(gms, "bundle", "create", str(self.bundle), "--all")
        self.revisions = {"build/make": make_rev, "build/make/tools/child": child_rev,
                          "kernel/xiaomi/crux-pe-cepheus": kern_rev, "vendor/gms": gms_rev}
        self.lock = {"projects": [
            {"path": "build/make", "url": make.as_uri(), "revision": make_rev},
            {"path": "build/make/tools/child", "url": child.as_uri(), "revision": child_rev},
            {"path": "kernel/xiaomi/crux-pe-cepheus", "url": kern.as_uri(), "revision": kern_rev},
            {"path": "vendor/gms", "url": "https://github.com/owner/vendor-gms-snapshot", "revision": gms_rev,
             "expected_tree": gms_tree, "archive_type": "github-release-snapshot", "release_snapshot": {}},
        ], "layout": [
            {"destination": "Makefile", "source": "build/make/core/root.mk", "type": "copy"},
            {"destination": "Android.bp", "source": "build/make/root.bp", "type": "link"},
            {"destination": "dangling.bp", "source": "build/make/missing.bp", "type": "link"},
        ]}
        self.manifest = self.root / "lock.json"
        self.manifest.write_text(json.dumps(self.lock))
        self.tree, self.kernel = self.root / "home/pe13", self.root / "home/kernel"

    def run_tool(self, *extra):
        result = subprocess.run([sys.executable, "-B", str(TOOL), "--manifest", str(self.manifest),
                                 "--tree", str(self.tree), "--kernel", str(self.kernel), "--jobs", "2", *extra],
                                capture_output=True, text=True)
        return result.returncode, json.loads(result.stdout)

    def check(self):
        return subprocess.run([sys.executable, "-B", str(CHECK), "--tree", str(self.tree),
                               "--manifest", str(self.manifest)], capture_output=True, text=True).returncode

    def test_full_checkout_passes_the_b_source_check(self):
        code, report = self.run_tool("--snapshot", f"vendor/gms={self.bundle}")
        self.assertEqual((code, report["fetched"], report["problems"]), (0, 4, []))
        for path, revision in self.revisions.items():
            self.assertEqual(git(self.tree / path, "rev-parse", "HEAD"), revision)
        self.assertEqual(git(self.tree / "build/make", "rev-parse", "--is-shallow-repository"), "true")
        link = self.tree / "kernel/xiaomi/crux-pe-cepheus"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self.kernel.resolve())
        self.assertEqual((self.tree / "Makefile").read_text(), "root\n")
        self.assertFalse((self.tree / "Makefile").is_symlink())
        self.assertEqual(os.readlink(self.tree / "Android.bp"), "build/make/root.bp")
        self.assertTrue((self.tree / "dangling.bp").is_symlink())
        self.assertFalse((self.tree / "build/make/later.txt").exists())
        self.assertEqual(self.check(), 0)
        # A second run keeps every checkout.
        code, report = self.run_tool("--snapshot", f"vendor/gms={self.bundle}")
        self.assertEqual((code, report["present"], report["fetched"], report["problems"]), (0, 4, 0, []))

    def test_missing_snapshot_is_reported_and_the_rest_is_fetched(self):
        code, report = self.run_tool()
        self.assertEqual(code, 1)
        self.assertEqual([p["path"] for p in report["problems"]], ["vendor/gms"])
        self.assertEqual(report["fetched"], 3)
        self.assertNotEqual(self.check(), 0)

    def test_snapshot_tree_must_match_the_lock(self):
        self.lock["projects"][3]["expected_tree"] = "0" * 40
        self.manifest.write_text(json.dumps(self.lock))
        code, report = self.run_tool("--snapshot", f"vendor/gms={self.bundle}")
        self.assertEqual(code, 1)
        self.assertIn("Git tree differs", report["problems"][0]["problem"])

    def test_patched_project_is_checked_by_commit_not_by_its_patched_tree(self):
        self.lock["projects"][0].update(expected_tree="1" * 40,
                                        patches=[{"path": "config/source-patches/marker.patch", "sha256": "0" * 64}])
        self.manifest.write_text(json.dumps(self.lock))
        code, report = self.run_tool("--snapshot", f"vendor/gms={self.bundle}")
        self.assertEqual((code, report["problems"]), (0, []))

    def test_tracked_changes_are_left_untouched(self):
        self.run_tool("--snapshot", f"vendor/gms={self.bundle}")
        (self.tree / "build/make/core/root.mk").write_text("local edit\n")
        self.lock["projects"][0]["revision"] = git(self.root / "remotes/make", "rev-parse", "HEAD")
        self.manifest.write_text(json.dumps(self.lock))
        code, report = self.run_tool("--snapshot", f"vendor/gms={self.bundle}")
        self.assertEqual(code, 1)
        self.assertEqual(report["problems"][0], {"path": "build/make", "problem": "tracked changes; left untouched"})
        self.assertEqual((self.tree / "build/make/core/root.mk").read_text(), "local edit\n")

    def test_only_limits_the_projects_and_their_layout(self):
        code, report = self.run_tool("--only", "build/make/tools/child")
        self.assertEqual((code, report["projects"], report["fetched"]), (0, 1, 1))
        self.assertTrue((self.tree / "build/make/tools/child/child.txt").exists())
        self.assertFalse((self.tree / "Makefile").exists())


if __name__ == "__main__":
    unittest.main()
