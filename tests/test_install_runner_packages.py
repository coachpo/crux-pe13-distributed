from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_runner_packages


class InstallRunnerPackagesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.mirror_list = self.root / "apt-mirrors.txt"

    def tearDown(self):
        self.temporary.cleanup()

    def test_filter_removes_only_azure_rows_and_preserves_order_comments_and_priorities(self):
        original = (
            "# runner mirror preference\n"
            "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
            "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
            "# keep this comment mentioning azure.archive.ubuntu.com\n"
            "https://security.ubuntu.com/ubuntu/\tpriority:3\n"
        )
        self.assertEqual(install_runner_packages.filter_azure_mirror_rows(original), (
            "# runner mirror preference\n"
            "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
            "# keep this comment mentioning azure.archive.ubuntu.com\n"
            "https://security.ubuntu.com/ubuntu/\tpriority:3\n"
        ))

    def test_empty_mirror_remainder_fails_before_writing_or_running_apt(self):
        original = "# only mirror\nhttp://azure.archive.ubuntu.com/ubuntu\n"
        self.mirror_list.write_text(original)
        run = mock.Mock()
        with self.assertRaisesRegex(ValueError, "no remaining source rows"):
            install_runner_packages.install_packages(["zstd"], self.mirror_list, run)
        run.assert_not_called()
        self.assertEqual(self.mirror_list.read_text(), original)

    def test_installs_filtered_file_then_updates_and_installs_requested_packages(self):
        original = (
            "# preserve comments and priority\n"
            "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
            "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
            "https://security.ubuntu.com/ubuntu/\tpriority:3\n"
        )
        expected = (
            "# preserve comments and priority\n"
            "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
            "https://security.ubuntu.com/ubuntu/\tpriority:3\n"
        )
        self.mirror_list.write_text(original)
        calls = []

        def run(command, check):
            self.assertTrue(check)
            calls.append(command)
            if command[1] == "install":
                shutil.copyfile(command[-2], command[-1])
            return subprocess.CompletedProcess(command, 0)

        install_runner_packages.install_packages(["squashfs-tools", "git", "curl"], self.mirror_list, run)
        self.assertEqual(self.mirror_list.read_text(), expected)
        self.assertEqual(calls[0][:3], ["sudo", "install", "-m0644"])
        self.assertEqual(Path(calls[0][-1]), self.mirror_list)
        self.assertFalse(Path(calls[0][-2]).exists())
        self.assertEqual(calls[1:], [
            ["sudo", "apt-get", "update"],
            ["sudo", "apt-get", "install", "-y", "squashfs-tools", "git", "curl"],
        ])

    def test_unchanged_mirror_list_is_not_rewritten(self):
        original = (
            "# azure.archive.ubuntu.com in documentation is not a mirror row\n"
            "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
        )
        self.mirror_list.write_text(original)
        calls = []

        def run(command, check):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        install_runner_packages.install_packages(["zstd"], self.mirror_list, run)
        self.assertEqual(self.mirror_list.read_text(), original)
        self.assertEqual(calls, [
            ["sudo", "apt-get", "update"],
            ["sudo", "apt-get", "install", "-y", "zstd"],
        ])

    def test_apt_failures_propagate_and_stop_the_remaining_steps(self):
        for failed_step in ("update", "install"):
            with self.subTest(failed_step=failed_step):
                self.mirror_list.write_text(
                    "http://azure.archive.ubuntu.com/ubuntu\nhttps://archive.ubuntu.com/ubuntu\n")
                calls = []

                def run(command, check):
                    self.assertTrue(check)
                    calls.append(command)
                    if command[1] == "install":
                        shutil.copyfile(command[-2], command[-1])
                    if command[:2] == ["sudo", "apt-get"] and command[2] == failed_step:
                        raise subprocess.CalledProcessError(100, command)
                    return subprocess.CompletedProcess(command, 0)

                with self.assertRaises(subprocess.CalledProcessError):
                    install_runner_packages.install_packages(["zstd"], self.mirror_list, run)
                self.assertEqual(calls[-1][2], failed_step)
                if failed_step == "update":
                    self.assertEqual(len(calls), 2)
                else:
                    self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
