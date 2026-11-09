import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import nightly  # noqa: E402

REPOSITORY = "coachpo/crux-pe13-distributed"
MAKEFILE = "# Recovery\nTARGET_USES_AOSP_RECOVERY := true\n\nPRODUCT_BUILD_USERDATA_IMAGE := false\n"


def git(cwd, *argv):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *argv], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


class MarkerPatchTests(unittest.TestCase):
    def test_patch_installs_the_marker_of_this_build_from_the_makefile_end(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            (tree / "aosp_crux.mk").write_text(MAKEFILE)
            (tree / "marker.patch").write_text(nightly.marker_patch(MAKEFILE, "2026111050"))
            subprocess.run(["git", "apply", "--binary", "marker.patch"], cwd=tree, check=True)
            self.assertEqual((tree / "aosp_crux.mk").read_text(),
                             MAKEFILE + "".join(line + "\n" for line in nightly.MARKER_RULE))
            self.assertEqual((tree / nightly.MARKER_FILE).read_text(), "crux-pe13-dev-2026111050\n")

    def test_makefile_that_already_installs_a_marker_is_refused(self):
        installed = MAKEFILE + "".join(line + "\n" for line in nightly.MARKER_RULE)
        for makefile in (installed, MAKEFILE.rstrip("\n")):
            with self.subTest(makefile=makefile[-20:]), self.assertRaises(nightly.NightlyError):
                nightly.marker_patch(makefile, "2026111050")


class PinSourcesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        shutil.copytree(ROOT / "config", self.root / "config")

    def fake_marker(self, project, build, patches):
        patch = patches / f"device-crux-dev-marker-{build}.patch"
        patch.write_text(nightly.marker_patch(MAKEFILE, build))
        self.marked = project["revision"]
        return ({"path": f"config/source-patches/{patch.name}",
                 "sha256": hashlib.sha256(patch.read_bytes()).hexdigest()}, "c" * 40)

    def config(self):
        manifest = json.loads((self.root / "config/cloud-source-manifest.json").read_text())
        overrides = json.loads((self.root / "config/cloud-source-overrides.json").read_text())["projects"]
        return {project["path"]: project for project in manifest["projects"]}, overrides

    def test_moved_forks_and_the_new_marker_are_pinned_consistently(self):
        projects, _ = self.config()
        old_patch = projects[nightly.DEVICE]["patches"][0]["path"]
        changes = {nightly.DEVICE: {"revision": "d" * 40}, "bootable/recovery": {"revision": "e" * 40},
                   "kernel/xiaomi/crux-pe-cepheus": {"revision": "f" * 40}}
        nightly.pin_sources(self.root, changes, "2026111050", marker=self.fake_marker)
        projects, overrides = self.config()
        self.assertEqual(self.marked, "d" * 40)
        for path, fields in changes.items():
            self.assertEqual(projects[path]["revision"], fields["revision"])
        device = projects[nightly.DEVICE]
        self.assertEqual(device["expected_tree"], "c" * 40)
        self.assertEqual(device["patches"][0]["path"], "config/source-patches/device-crux-dev-marker-2026111050.patch")
        self.assertFalse((self.root / old_patch).exists())
        # The same invariants tests.test_cloud_source_config checks on the real configuration.
        for path, fields in overrides.items():
            for key, value in fields.items():
                self.assertEqual(projects[path][key], value, (path, key))
        for patch in device["patches"]:
            self.assertEqual(hashlib.sha256((self.root / patch["path"]).read_bytes()).hexdigest(), patch["sha256"])

    def test_unmoved_device_still_gets_the_marker_of_the_new_build(self):
        projects, _ = self.config()
        revision = projects[nightly.DEVICE]["revision"]
        nightly.pin_sources(self.root, {}, "2026111051", marker=self.fake_marker)
        projects, overrides = self.config()
        self.assertEqual((self.marked, projects[nightly.DEVICE]["revision"]), (revision, revision))
        self.assertEqual(overrides[nightly.DEVICE]["patches"], projects[nightly.DEVICE]["patches"])

    def test_all_maintained_forks_keep_manifest_and_overrides_consistent(self):
        changes = {path: {"revision": f"{number:040x}"}
                   for number, path in enumerate(nightly.TRACKED, start=1)}
        nightly.pin_sources(self.root, changes, "2026111052", marker=self.fake_marker)
        projects, overrides = self.config()
        for path, fields in changes.items():
            self.assertEqual(projects[path]["revision"], fields["revision"])
        for path, fields in overrides.items():
            for key, value in fields.items():
                self.assertEqual(projects[path][key], value, (path, key))


class ChangeDetectionTests(unittest.TestCase):
    def test_fork_uses_the_explicit_android13_branch_not_default_branch(self):
        project = {"path": "system/core", "url": "https://github.com/coachpo/crux-pe13-system-core"}
        with mock.patch.object(nightly, "api", return_value={"sha": "c" * 40}) as api:
            self.assertEqual(nightly.fork_head(project), "c" * 40)
        api.assert_called_once_with("repos/coachpo/crux-pe13-system-core/commits/crux-pe13-baseline")

    def test_fit_recipe_change_starts_a_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "--quiet")
            recipe = root / "references/pe13-boot-contracts/package-pe13.py"
            recipe.parent.mkdir(parents=True)
            recipe.write_text("before\n")
            git(root, "add", "-A")
            git(root, "commit", "--quiet", "-m", "base")
            baseline = git(root, "rev-parse", "HEAD")
            recipe.write_text("after\n")
            git(root, "commit", "--quiet", "-am", "recipe")
            self.assertTrue(nightly.build_paths_changed(baseline, root))

    def test_only_build_paths_since_the_last_full_freeze_start_a_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "--quiet")
            (root / "docs").mkdir()
            (root / "scripts").mkdir()
            (root / "docs/guide.md").write_text("one\n")
            git(root, "add", "-A")
            git(root, "commit", "--quiet", "-m", "base")
            baseline = git(root, "rev-parse", "HEAD")
            self.assertFalse(nightly.build_paths_changed(baseline, root))
            (root / "docs/guide.md").write_text("two\n")
            git(root, "commit", "--quiet", "-am", "docs")
            self.assertFalse(nightly.build_paths_changed(baseline, root))
            (root / "scripts/tool.py").write_text("print()\n")
            git(root, "add", "-A")
            git(root, "commit", "--quiet", "-m", "code")
            self.assertTrue(nightly.build_paths_changed(baseline, root))
            self.assertTrue(nightly.build_paths_changed(None, root))

    def test_build_number_is_the_first_unused_ci_number_of_the_day(self):
        used = {"2026111050", "2026111051", "2026111001"}
        self.assertEqual(nightly.next_build("20261110", used), "2026111052")
        with self.assertRaisesRegex(nightly.NightlyError, "every CI build number"):
            nightly.next_build("20261110", {f"20261110{n}" for n in range(50, 100)})

    def test_snapshot_release_recorded_by_snapshot_json_is_locked(self):
        commit, asset = "1" * 40, "vendor-gms-" + "1" * 40 + ".tar.gz"
        record = {"project": "vendor/gms", "commit": commit, "git_tree": "2" * 40, "name": asset,
                  "size_bytes": 10, "sha256": "3" * 64}
        responses = {
            "repos/coachpo/crux-pe13-vendor-gms-snapshot": {"id": 7, "default_branch": "main"},
            "repos/coachpo/crux-pe13-vendor-gms-snapshot/contents/snapshot.json?ref=main":
                {"content": base64.b64encode(json.dumps(record).encode()).decode()},
            f"repos/coachpo/crux-pe13-vendor-gms-snapshot/releases/tags/snapshot-{commit}":
                {"id": 8, "tag_name": "snapshot-" + commit, "draft": False,
                 "assets": [{"id": 9, "name": asset, "size": 10, "digest": "sha256:" + "3" * 64}]},
        }
        project = next(p for p in json.loads((ROOT / "config/cloud-source-manifest.json").read_text())["projects"]
                       if p["path"] == "vendor/gms")
        with mock.patch.object(nightly, "api", side_effect=lambda endpoint, token=None: responses[endpoint]):
            fields = nightly.snapshot_update(project, "token")
            self.assertEqual(fields["revision"], commit)
            self.assertEqual(fields["expected_tree"], "2" * 40)
            self.assertEqual(fields["release_snapshot"]["asset_id"], 9)
            responses[f"repos/coachpo/crux-pe13-vendor-gms-snapshot/releases/tags/snapshot-{commit}"]["assets"][0][
                "size"] = 11
            with self.assertRaisesRegex(nightly.NightlyError, "differs from its snapshot.json"):
                nightly.snapshot_update(project, "token")


class NightlyRunTests(unittest.TestCase):
    def run_night(self, *, moved=None, paths_changed=False, active=None, force=False):
        manifest = json.loads((ROOT / "config/cloud-source-manifest.json").read_text())
        pins = {project["path"]: project["revision"] for project in manifest["projects"]}
        moved = moved or {}
        freezes = [{"id": 43000000002, "head_sha": "a" * 40, "conclusion": "failure"},
                   {"id": 43000000001, "head_sha": "b" * 40, "conclusion": "success"}]
        with mock.patch.object(nightly, "chain_in_flight", return_value=active), \
                mock.patch.object(nightly, "fork_head", side_effect=lambda p: moved.get(p["path"], pins[p["path"]])), \
                mock.patch.object(nightly, "snapshot_update", return_value=None), \
                mock.patch.object(nightly, "full_freezes", return_value=freezes), \
                mock.patch.object(nightly, "build_paths_changed", return_value=paths_changed) as changed, \
                mock.patch.object(nightly, "used_builds", return_value={"2026111050"}), \
                mock.patch.object(nightly, "pin_sources") as pin, \
                mock.patch.object(nightly, "commit_and_push") as push, \
                mock.patch.object(nightly.subprocess, "run") as run:
            result = nightly.run(REPOSITORY, "token", "20261110", force=force)
        return result, changed, pin, push, run

    def test_quiet_night_neither_commits_nor_dispatches(self):
        result, changed, pin, push, run = self.run_night()
        self.assertEqual(result, {"result": "no-change"})
        changed.assert_called_once_with("a" * 40, nightly.ROOT)
        pin.assert_not_called()
        push.assert_not_called()
        run.assert_not_called()

    def test_manual_force_builds_without_changes(self):
        result, changed, pin, push, run = self.run_night(force=True)
        self.assertEqual(result["result"], "started")
        changed.assert_not_called()
        self.assertEqual(pin.call_args.args[1:], ({}, "2026111051"))
        push.assert_called_once()
        self.assertIn("build_number=2026111051", run.call_args.args[0])

    def test_manual_force_does_not_duplicate_an_active_chain(self):
        result, _, pin, push, run = self.run_night(force=True, active="compile running")
        self.assertEqual(result["result"], "skipped")
        pin.assert_not_called()
        push.assert_not_called()
        run.assert_not_called()

    def test_each_maintained_fork_can_trigger_the_chain(self):
        for path in nightly.TRACKED:
            with self.subTest(path=path):
                result, _, pin, _, _ = self.run_night(moved={path: "9" * 40})
                self.assertEqual(result["result"], "started")
                self.assertEqual(pin.call_args.args[1], {path: {"revision": "9" * 40}})

    def test_running_chain_skips_the_night(self):
        result, _, pin, _, run = self.run_night(moved={"art": "9" * 40}, active="release.yml run 1 is queued")
        self.assertEqual(result["result"], "skipped")
        pin.assert_not_called()
        run.assert_not_called()

    def test_moved_fork_is_pinned_committed_and_frozen_with_reusable_layers(self):
        result, _, pin, push, run = self.run_night(moved={"art": "9" * 40})
        self.assertEqual(result["build"], "2026111051")
        self.assertEqual(pin.call_args.args[1:], ({"art": {"revision": "9" * 40}}, "2026111051"))
        push.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["gh", "workflow", "run", "freeze-standard.yml"])
        self.assertIn("build_number=2026111051", argv)
        self.assertIn("reuse_sources_from=43000000001", argv)

    def test_build_path_change_alone_starts_a_build(self):
        result, _, pin, _, _ = self.run_night(paths_changed=True)
        self.assertEqual(result["result"], "started")
        self.assertEqual(pin.call_args.args[1], {})


if __name__ == "__main__":
    unittest.main()
