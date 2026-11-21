import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location("direct_install", Path(__file__).with_name("direct-install-pe13.py"))
install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install)
REVIEWED = install.WORKSPACE / "out/crux-pe13-device-validation-2026-11-05/B/write-plan-review.json"
IDS = ["2eaf4541-48e4-4cb9-a5c3-2559b8f54a98", "5b8472c1-c29e-49ed-a787-99c287ca96dd",
       "6866efd5-179d-4974-80a4-62f0506c5c48"]


def execute_args():
    return SimpleNamespace(serial="8bd37df4", expected_uboot_version="fixture", watcher_pid=42,
                           expected_start_build="2026110502", expected_start_fingerprint="fixture",
                           expected_start_marker="crux-pe13-local-2026110502", expected_start_no_marker=False,
                           rehearse=False)


class DirectInstallTests(unittest.TestCase):
    def test_fit_failure_stops_before_raw_installer_or_recovery_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ram = SimpleNamespace(LOG=Mock(), CMD=Mock())
            session = Mock()
            pgrep = SimpleNamespace(returncode=0, stdout="42\n")
            with patch.object(install.subprocess, "run", return_value=pgrep), \
                 patch.object(install, "read_rom", side_effect=[IDS[0], IDS[1]]), \
                 patch.object(install, "wait_boot"), \
                 patch.object(install.recovery, "reboot_to_shell", return_value=session) as enter, \
                 patch.object(install.recovery, "boot") as boot_recovery, \
                 patch.object(install.recovery, "boot_rom"), \
                 patch.object(install.recovery, "verify_geometry"), \
                 patch.object(install.recovery, "helper") as helper, \
                 patch.object(install.fits, "write_fits", side_effect=RuntimeError("FIT readback failed")) as write:
                with self.assertRaisesRegex(RuntimeError, "FIT readback failed"):
                    install.execute(execute_args(), root, ram, {"fits": {"recovery": {}, "rom": {}}},
                                    {"profile_id": "fixture"}, {"path": "fixture"}, {})
            write.assert_called_once()
            session.save.assert_called_once()
            boot_recovery.assert_called_once()  # Only the non-writing rehearsal boot.
            helper.assert_not_called()
            self.assertEqual(enter.call_count, 3)
            self.assertEqual(json.loads((root / "result.json").read_text())["phase"], "write-pe-fits")

    def test_rehearsal_stops_before_geometry_check_or_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ram = SimpleNamespace(LOG=Mock(), CMD=Mock())
            session = Mock()
            pgrep = SimpleNamespace(returncode=0, stdout="42\n")
            args = execute_args()
            args.rehearse = True
            state = {}
            with patch.object(install.subprocess, "run", return_value=pgrep), \
                 patch.object(install, "read_rom", side_effect=[IDS[0], IDS[1]]), \
                 patch.object(install, "wait_boot"), \
                 patch.object(install.recovery, "reboot_to_shell", return_value=session) as enter, \
                 patch.object(install.recovery, "boot") as boot_recovery, \
                 patch.object(install.recovery, "boot_rom"), \
                 patch.object(install.recovery, "verify_geometry") as geometry, \
                 patch.object(install.recovery, "helper") as helper, \
                 patch.object(install.fits, "write_fits") as write:
                install.execute(args, root, ram, {"fits": {"recovery": {}, "rom": {}}},
                                {"profile_id": "fixture"}, {"path": "fixture"}, state)
            boot_recovery.assert_called_once()
            self.assertEqual(enter.call_count, 2)
            geometry.assert_not_called()
            write.assert_not_called()
            helper.assert_not_called()
            self.assertEqual(state["status"], "no_write_rehearsal_passed")
            self.assertEqual(json.loads((root / "result.json").read_text())["phase"], "rehearsal-complete")

    def test_wrong_starting_rom_stops_before_any_reboot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ram = SimpleNamespace(LOG=Mock(), CMD=Mock())
            pgrep = SimpleNamespace(returncode=0, stdout="42\n")
            with patch.object(install.subprocess, "run", return_value=pgrep), \
                 patch.object(install, "read_rom", side_effect=RuntimeError("ROM image marker differs")), \
                 patch.object(install.recovery, "reboot_to_shell") as enter, \
                 patch.object(install.recovery, "boot") as boot_recovery, \
                 patch.object(install.fits, "write_fits") as write:
                with self.assertRaisesRegex(RuntimeError, "marker differs"):
                    install.execute(execute_args(), root, ram, {"fits": {}}, {"profile_id": "fixture"},
                                    {"path": "fixture"}, {})
            enter.assert_not_called()
            boot_recovery.assert_not_called()
            write.assert_not_called()

    def test_second_watcher_stops_before_reading_the_rom(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ram = SimpleNamespace(LOG=Mock(), CMD=Mock())
            pgrep = SimpleNamespace(returncode=0, stdout="42\n43\n")
            with patch.object(install.subprocess, "run", return_value=pgrep), \
                 patch.object(install, "read_rom") as read:
                with self.assertRaisesRegex(RuntimeError, "watcher"):
                    install.execute(execute_args(), root, ram, {}, {}, {}, {})
            read.assert_not_called()

    def test_reviewed_cache_ranges_are_reproduced(self):
        reviewed = json.loads(REVIEWED.read_text())
        ranges = install.cache_ranges(reviewed["fits"]["recovery"], reviewed["fits"]["rom"],
                                      install.fits.RETAINED_TWRP_CRC32)
        self.assertEqual(ranges, reviewed["cache_crc_witness_ranges"])

    def test_plan_comparison_rejects_changed_bytes_or_ranges(self):
        reviewed = json.loads(REVIEWED.read_text())
        moved = copy.deepcopy(reviewed)
        for row in moved["writes_in_order"]:
            row["path"] = "/elsewhere/" + Path(row.get("path", "x")).name
        install.compare_plans(moved, reviewed)  # Paths alone may differ.
        for mutate, section in (
            (lambda plan: plan["writes_in_order"][1].update(lba="0x3c001"), "writes"),
            (lambda plan: plan["raw"]["pesystem"].update(sha256="0" * 64), "raw"),
            (lambda plan: plan["cache_crc_witness_ranges"][2].update(expected_retained_crc32="00000000"), "ranges"),
            (lambda plan: plan.update(target_marker=None), "scalars"),
        ):
            changed = copy.deepcopy(reviewed)
            mutate(changed)
            with self.assertRaisesRegex(RuntimeError, section):
                install.compare_plans(changed, reviewed)

    def test_execute_and_rehearse_require_explicit_starting_identity(self):
        for execute, rehearse in ((True, False), (False, True)):
            args = SimpleNamespace(execute=execute, rehearse=rehearse, dry_run=False, watcher_pid=42,
                                   expected_start_build="2026110502", expected_start_fingerprint=None,
                                   expected_start_marker=None, expected_start_no_marker=False,
                                   out=Path("/nonexistent/unused"))
            with self.assertRaises(SystemExit):
                install.install_command(args)


if __name__ == "__main__":
    unittest.main()
