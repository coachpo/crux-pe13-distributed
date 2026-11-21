"""Host-only fixtures for the guarded serial handoff."""

import importlib.util
from pathlib import Path
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("ram_boot_pe13", Path(__file__).with_name("ram-boot-pe13.py"))
ram = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ram)


class GuardedConsoleTests(unittest.TestCase):
    def test_fresh_menu_uses_observed_key(self):
        menu = "[watch] port open\n*** U-Boot Boot Menu ***\n5. Drop to shell\n"
        with mock.patch.object(ram, "read_log", side_effect=[menu, menu + "\n=> "]), \
                mock.patch.object(ram, "CMD") as wire, mock.patch.object(ram.time, "sleep"):
            state = ram.Session.recover(mock.Mock(), 0, require_menu=True, strict=True)
        wire.write_text.assert_called_once_with("5")
        self.assertEqual(state["menu_key"], "5")

    def test_fresh_partial_menu_can_use_explicit_exit(self):
        menu = ("[watch] port open\nvironment\x1b[14;7Hb. UFS device info"
                "\x1b[15;7Hc. Reset device\x1b[16;7H0. Exit"
                "\x1b[18;3HHit any key to stop autoboot: 86400 ")
        with mock.patch.object(ram, "read_log", side_effect=[menu, menu + "\n=> "]), \
                mock.patch.object(ram, "CMD") as wire, mock.patch.object(ram.time, "sleep"):
            ram.Session.recover(mock.Mock(), 0, require_menu=True, strict=True)
        wire.write_text.assert_called_once_with("0")

    def test_countdown_without_live_choice_is_not_a_key(self):
        with mock.patch.object(ram, "read_log", return_value=(
                "[watch] port reopened\nHit any key to stop autoboot: 86400 ")), \
                mock.patch.object(ram, "CMD") as wire, mock.patch.object(ram.time, "sleep"), \
                mock.patch.object(ram.time, "monotonic", side_effect=[0, .5, 2, 3]):
            with self.assertRaisesRegex(RuntimeError, "No verified U-Boot recovery"):
                ram.Session.recover(mock.Mock(), 0, timeout=1, require_menu=True, strict=True)
        wire.write_text.assert_not_called()

    def test_waiting_menu_asks_capable_watcher_to_redraw_before_selection(self):
        countdown = ("[watch] port open\n[watch] feature menu-redraw-v1\n"
                     "\x1b[18;3HHit any key to stop autoboot: 86398 "
                     "\x1b[18;3HHit any key to stop autoboot: 86397 ")
        redrawn = countdown + "\x1b[11;7H8. Drop to shell"
        with mock.patch.object(ram, "read_log", side_effect=[countdown, redrawn, redrawn + "\n=> "]), \
                mock.patch.object(ram, "CMD") as wire, mock.patch.object(ram.time, "sleep"):
            state = ram.Session.recover(mock.Mock(), 0, require_menu=True, strict=True)
        self.assertEqual(wire.write_text.call_args_list, [mock.call("@menu-redraw"), mock.call("8")])
        self.assertTrue(state["menu_redraw_sent"])

    def test_unexpected_reboot_during_fastboot_resume_has_no_fallback(self):
        with mock.patch.object(ram, "read_log", return_value=(
                "[watch] port reopened\n*** U-Boot Boot Menu ***\n8. Drop to shell\n")), \
                mock.patch.object(ram, "CMD") as wire:
            with self.assertRaisesRegex(RuntimeError, "Unexpected reboot"):
                ram.Session.recover(mock.Mock(), 0, strict=True)
        wire.write_text.assert_not_called()

    def test_watchdog_witness_must_precede_kernel(self):
        watchdog = "WDT: Started watchdog@17c10000 with servicing every 1000ms (30s timeout)\n"
        kernel = "Starting kernel ...\n"
        session = mock.Mock()
        with mock.patch.object(ram, "LOG") as log, \
                mock.patch.object(ram, "read_log", side_effect=[watchdog + kernel, kernel + watchdog]):
            log.stat.return_value.st_size = 789
            self.assertEqual(ram.guarded_handoff(session), 789)
            with self.assertRaisesRegex(RuntimeError, "No watchdog-start success"):
                ram.guarded_handoff(session, "run boot_pe_rom")
        self.assertEqual(session.send.call_args_list, [mock.call("run boot_go", prompt=False),
                                                       mock.call("run boot_pe_rom", prompt=False)])


if __name__ == "__main__":
    unittest.main()
