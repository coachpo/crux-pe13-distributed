"""Host-only profile fixtures; no Android build or existing OUT is used."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("b_host", Path(__file__).with_name("pe13-b-host-profile.py"))
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


class HostProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def tree(self, name):
        root = self.root / name
        for relative, upstream, patched in profile.RULES:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(upstream + "\n")
        binary = root / "prebuilts/build-tools/linux-x86/bin/nsjail"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"fixture host executable")
        binary.chmod(0o755)
        return root

    def test_upstream_profile_is_local_and_effective_make_flags_match_b(self):
        upstream, local = self.tree("upstream"), self.tree("local")
        report = profile.apply_profile(local)
        self.assertEqual([x["action"] for x in report["changes"]],
                         ["applied-in-overlay", "applied-in-overlay", "hidden-in-overlay"])
        self.assertEqual((upstream / profile.RULES[0][0]).read_text(), profile.DEFAULT_ON + "\n")
        self.assertTrue((upstream / "prebuilts/build-tools/linux-x86/bin/nsjail").exists())
        text = (local / profile.RULES[0][0]).read_text()
        text += "all:\n\t@echo $(WITH_DEXPREOPT):$(WITH_DEX_PREOPT_GENERATE_PROFILE)\n"
        result = subprocess.run(["make", "-s", "-f", "-", "HOST_OS=linux"], input=text,
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "false:false")
        hidden = local / "prebuilts/build-tools/linux-x86/bin/nsjail.old"
        self.assertEqual(hidden.read_bytes(), b"fixture host executable")
        self.assertEqual(hidden.stat().st_mode & 0o777, 0o755)

    def test_reapplication_is_idempotent_without_rewriting_files(self):
        root = self.tree("repeat")
        profile.apply_profile(root)
        paths = [root / relative for relative, _, _ in profile.RULES]
        paths.append(root / "prebuilts/build-tools/linux-x86/bin/nsjail.old")
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        result = profile.apply_profile(root)
        self.assertTrue(all(x["action"] == "already-pinned" for x in result["changes"]))
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)

    def test_userdebug_preopt_error_is_relaxed_only_in_b_view(self):
        upstream, local = self.tree("upstream"), self.tree("local")
        profile.apply_profile(local)
        for tree, allowed in ((upstream, False), (local, True)):
            rule = (tree / profile.RULES[1][0]).read_text()
            makefile = ("HOST_OS := linux\nTARGET_BUILD_VARIANT := userdebug\n"
                        "WITH_DEXPREOPT := false\npretty-error = $(error $(1))\n"
                        "ifeq ($(HOST_OS),linux)\n"
                        "ifeq (,$(filter eng,$(TARGET_BUILD_VARIANT)))\n"
                        "ifneq (true,$(WITH_DEXPREOPT))\n"
                        "ifneq (true,$(WITH_DEXPREOPT_BOOT_IMG_AND_SYSTEM_SERVER_ONLY))\n"
                        + rule + "endif\nendif\nendif\nendif\nall:\n\t@echo allowed\n")
            result = subprocess.run(["make", "-s", "-f", "-"], input=makefile,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode == 0, allowed)
            if allowed:
                self.assertEqual(result.stdout.strip(), "allowed")
            else:
                self.assertIn("DEXPREOPT must be enabled", result.stderr)

    def test_unsupported_make_rule_does_not_partially_modify_source(self):
        root = self.tree("unsupported")
        (root / profile.RULES[1][0]).write_text("unrecognized make rule\n")
        with self.assertRaisesRegex(ValueError, "Unrecognized Rosetta"):
            profile.apply_profile(root)
        self.assertEqual((root / profile.RULES[0][0]).read_text(), profile.DEFAULT_ON + "\n")
        self.assertTrue((root / "prebuilts/build-tools/linux-x86/bin/nsjail").exists())

    def test_ambiguous_binary_layout_does_not_change_make_rules(self):
        root = self.tree("ambiguous")
        (root / "prebuilts/build-tools/linux-x86/bin/nsjail.old").write_bytes(b"other")
        with self.assertRaisesRegex(ValueError, "Unrecognized nsjail"):
            profile.apply_profile(root)
        self.assertEqual((root / profile.RULES[0][0]).read_text(), profile.DEFAULT_ON + "\n")

    def test_cli_refuses_shared_checkout_before_mutation(self):
        root = self.tree("shared")
        result = subprocess.CompletedProcess([], 0, "btrfs\n", "")
        with mock.patch.object(profile.subprocess, "run", return_value=result), \
                mock.patch.object(profile, "apply_profile") as apply:
            with self.assertRaises(SystemExit):
                profile.main(["--tree", str(root), "--overlay-run", str(self.root / "run"),
                              "--report", str(self.root / "report")])
        apply.assert_not_called()

    def test_host_setting_changed_by_a_user_patch_is_not_silently_accepted(self):
        root = self.tree("patched")
        profile.apply_profile(root)
        path = root / profile.RULES[0][0]
        changed = path.read_text().replace("WITH_DEXPREOPT := false", "WITH_DEXPREOPT := true")
        path.write_text(changed)
        with self.assertRaisesRegex(ValueError, "Unrecognized Rosetta"):
            profile.apply_profile(root)
        self.assertEqual(path.read_text(), changed)

    def test_cli_refuses_incomplete_or_unrelated_overlay_paths(self):
        root = self.tree("overlay")
        run = self.root / "run"
        for mount in ("overlay rw\n",
                      f"overlay rw,upperdir=/other,workdir={run}/src-work\n",
                      f"overlay rw,upperdir={run}/src-upper,workdir=/other\n"):
            with self.subTest(mount=mount):
                result = subprocess.CompletedProcess([], 0, mount, "")
                with mock.patch.object(profile.subprocess, "run", return_value=result), \
                        mock.patch.object(profile, "apply_profile") as apply:
                    with self.assertRaises(SystemExit):
                        profile.main(["--tree", str(root), "--overlay-run", str(run),
                                      "--report", str(self.root / "report")])
                apply.assert_not_called()

    def test_cli_full_profile_applies_make_and_nsjail_only_in_private_overlay(self):
        base, root = self.tree("official-base"), self.tree("overlay-a2")
        run = self.root / "a2-run"
        result = subprocess.CompletedProcess([], 0,
            f"overlay rw,upperdir={run}/src-upper,workdir={run}/src-work\n", "")
        output = self.root / "a2-report.json"
        with mock.patch.object(profile.subprocess, "run", return_value=result):
            profile.main(["--tree", str(root), "--overlay-run", str(run),
                          "--report", str(output)])
        record = json.loads(output.read_text())
        self.assertEqual(record["application"], "upstream")
        self.assertEqual(record["profile"], "crux-b-rosetta/1")
        self.assertEqual(record["settings"], {"WITH_DEXPREOPT": "false",
                                             "WITH_DEX_PREOPT_GENERATE_PROFILE": "false", "nsjail": "hidden"})
        self.assertEqual([x["action"] for x in record["changes"]],
                         ["applied-in-overlay", "applied-in-overlay", "hidden-in-overlay"])
        for relative, upstream, legacy in profile.RULES:
            self.assertEqual((base / relative).read_text(), upstream + "\n")
            self.assertEqual((root / relative).read_text(), legacy + "\n")
        self.assertTrue((base / "prebuilts/build-tools/linux-x86/bin/nsjail").exists())
        self.assertFalse((root / "prebuilts/build-tools/linux-x86/bin/nsjail").exists())


if __name__ == "__main__":
    unittest.main()
