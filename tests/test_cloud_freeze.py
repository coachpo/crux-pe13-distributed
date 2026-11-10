import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("cloud_freeze", SCRIPTS / "cloud_freeze.py")
cloud_freeze = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_freeze)


class CloudFreezeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "src"
        (self.source / "build/envsetup.sh").parent.mkdir(parents=True)
        (self.source / "build/envsetup.sh").write_text("")
        launcher = self.source / "build/soong/ui/build/soong.go"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("package build\nfunc f() {\n\tinvocationEnv := make(map[string]string)\n}\n")
        self.manifest = self.root / "source-manifest.json"
        self.manifest.write_text(json.dumps({
            "schema_version": 1,
            "source_root": str(self.source),
            "projects": [
                {"path": "build/soong", "revision": "a" * 40},
                {"path": "build/make", "revision": "b" * 40},
            ],
        }))
        self.out = self.root / "external-out"
        self.evidence = self.root / "evidence"

    def tearDown(self):
        self.temporary.cleanup()

    def run_freeze(self, runner, gogc="50"):
        argv = ["--source-root", str(self.source), "--out-root", str(self.out),
                "--build-number", "runner-1", "--build-datetime", "1790000000",
                "--evidence", str(self.evidence), "--source-manifest", str(self.manifest),
                "--gogc", gogc]
        with mock.patch.object(cloud_freeze.subprocess, "run", side_effect=runner):
            return cloud_freeze.main(argv)

    def test_build_command_is_graph_only_for_four_images(self):
        command = cloud_freeze.build_command()
        self.assertIn("/usr/bin/time", command)
        self.assertIn("m --skip-ninja bootimage recoveryimage systemimage vendorimage -j4", command[-1])

    def test_graph_sandbox_requires_a_real_wrapped_command_not_just_a_binary(self):
        binary = self.source / "prebuilts/build-tools/linux-x86/bin/nsjail"
        binary.parent.mkdir(parents=True)
        binary.write_text("fixture")
        self.assertEqual(cloud_freeze.observe_graph_sandbox(self.source, self.out)["status"], "unobserved")
        self.out.mkdir()
        log = self.out / "soong.log"
        log.write_text('[prebuilts/build-tools/linux-x86/bin/nsjail -H android-build]\n')
        self.assertEqual(cloud_freeze.observe_graph_sandbox(self.source, self.out)["status"], "unobserved")
        log.write_text('exec.go:64: "ckati" executing "prebuilts/build-tools/linux-x86/bin/nsjail" [-x ckati]\n')
        record = cloud_freeze.observe_graph_sandbox(self.source, self.out)
        self.assertEqual(record["status"], "wrapped-commands-observed")
        self.assertEqual(record["wrapped_commands"], 1)

    def test_graph_sandbox_reports_fallback_and_mixed_logs_without_guessing(self):
        self.out.mkdir()
        log = self.out / "soong.log"
        log.write_text('Build sandboxing disabled due to nsjail error.\nnsjail failed with missing binary\n')
        self.assertEqual(cloud_freeze.observe_graph_sandbox(self.source, self.out)["status"], "disabled-observed")
        with log.open('a') as stream:
            stream.write('"dumpvars" executing "prebuilts/build-tools/linux-x86/bin/nsjail" [-x ckati]\n')
        self.assertEqual(cloud_freeze.observe_graph_sandbox(self.source, self.out)["status"], "mixed-observed")

    def test_gogc_injection_is_unique_and_reports_source_digests(self):
        source_file = self.source / "build/soong/ui/build/soong.go"
        original = source_file.read_text()
        patch = cloud_freeze.inject_gogc(self.source)
        self.assertEqual(patch["status"], "patched")
        self.assertEqual(patch["original_sha256"], hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(patch["result_sha256"], cloud_freeze.sha256(source_file))
        self.assertIn('invocationEnv["GOGC"] = gogc', source_file.read_text())
        again = cloud_freeze.inject_gogc(self.source)
        self.assertEqual(again["status"], "already-injected")
        self.assertEqual(again["original_sha256"], patch["original_sha256"])

    def test_success_records_manifest_patch_and_all_graph_outputs(self):
        def fake_run(command, cwd, env, stdout, stderr, check):
            self.assertEqual(cwd, self.source)
            self.assertEqual(env["OUT_DIR"], str(self.out))
            self.assertEqual(env["BUILD_NUMBER"], "runner-1")
            self.assertEqual(env["BUILD_DATETIME"], "1790000000")
            self.assertEqual(env["CRUX_SOONG_GOGC"], "50")
            for relative in cloud_freeze.EXPECTED_GRAPHS:
                path = self.out / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
            return subprocess.CompletedProcess(command, 0)

        self.assertEqual(self.run_freeze(fake_run), 0)
        receipt = json.loads((self.evidence / "graph-receipt.json").read_text())
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["source_manifest"]["project_count"], 2)
        self.assertEqual(receipt["soong_source_revision"], "a" * 40)
        self.assertEqual(receipt["host_source_patch"]["status"], "patched")
        self.assertEqual(len(receipt["graph_files"]), len(cloud_freeze.EXPECTED_GRAPHS))
        self.assertTrue((self.evidence / "cloud-freeze.log").is_file())

    def test_gogc_can_be_configured_for_the_env_reset_child(self):
        def fake_run(command, cwd, env, stdout, stderr, check):
            self.assertEqual(env["CRUX_SOONG_GOGC"], "75")
            for relative in cloud_freeze.EXPECTED_GRAPHS:
                path = self.out / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
            return subprocess.CompletedProcess(command, 0)

        self.assertEqual(self.run_freeze(fake_run, gogc="75"), 0)
        receipt = json.loads((self.evidence / "graph-receipt.json").read_text())
        self.assertEqual(receipt["gogc"], 75)

    def test_ci_authentication_is_not_passed_to_the_android_environment_snapshot(self):
        def fake_run(command, cwd, env, stdout, stderr, check):
            self.assertNotIn('GH_TOKEN', env)
            self.assertNotIn('ACTIONS_RUNTIME_TOKEN', env)
            self.assertNotIn('GITHUB_RUN_ID', env)
            for relative in cloud_freeze.EXPECTED_GRAPHS:
                path = self.out / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative)
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.dict(os.environ, {'GH_TOKEN': 'fixture', 'ACTIONS_RUNTIME_TOKEN': 'fixture',
                                         'GITHUB_RUN_ID': '123'}):
            self.assertEqual(self.run_freeze(fake_run), 0)
            self.assertEqual(os.environ['GH_TOKEN'], 'fixture')
        receipt = json.loads((self.evidence / 'graph-receipt.json').read_text())
        self.assertIn('GH_TOKEN', receipt['removed_ci_environment_variables'])

    def test_nonzero_build_exit_is_recorded_and_fails(self):
        result = self.run_freeze(lambda command, **kwargs: subprocess.CompletedProcess(command, 7))
        self.assertEqual(result, 7)
        receipt = json.loads((self.evidence / "graph-receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["exit_code"], 7)

    def test_missing_expected_graph_output_fails_even_after_zero_exit(self):
        result = self.run_freeze(lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
        self.assertEqual(result, 1)
        receipt = json.loads((self.evidence / "graph-receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(set(receipt["missing_graph_files"]),
                         {str(self.out / path) for path in cloud_freeze.EXPECTED_GRAPHS})

    def test_nonempty_out_root_is_rejected(self):
        self.out.mkdir()
        (self.out / "seed").write_text("stale graph")
        with self.assertRaises(cloud_freeze.FreezeError):
            cloud_freeze.validate_paths(self.source, self.out, self.evidence)


if __name__ == "__main__":
    unittest.main()
