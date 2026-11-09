import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import pe_fits
import ram_twrp


class RamTwrpTests(unittest.TestCase):
    def test_manifest_uses_external_pin_and_verified_fit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fit = root / "recovery.itb"
            fit.write_bytes(b"expected FIT")
            manifest = root / "candidate.json"
            manifest.write_text(json.dumps({"schema_version": 1, "profile_id": "a" * 64,
                                             "fit": {"path": str(fit), "sha256": hashlib.sha256(fit.read_bytes()).hexdigest()}}))
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            ram = mock.Mock()
            value = ram_twrp.validate_manifest(manifest, digest, ram)
            self.assertEqual(value["profile_id"], "a" * 64)
            ram.fit_components.assert_called_once_with(b"expected FIT", "recovery")
            fit.write_bytes(b"changed")
            with self.assertRaisesRegex(ram_twrp.RamTwrpError, "FIT SHA-256 differs"):
                ram_twrp.validate_manifest(manifest, digest, ram)
            with self.assertRaisesRegex(ram_twrp.RamTwrpError, "manifest SHA-256 differs"):
                ram_twrp.validate_manifest(manifest, "b" * 64, ram)

    def test_exact_geometry_and_independent_protected_crc_ranges(self):
        session = mock.Mock()
        session.send.side_effect = [
            "Device 0: (0:0)\nCapacity: 237 GB (62222336 x 4096)\n",
            *(f"pe_check_start={start:x}\npe_check_size={size:x}\n"
              for start, size in ram_twrp.GEOMETRY.values()),
        ]
        ram_twrp.verify_geometry(session)
        session.send.side_effect = ["Device 0: (0:0)\nCapacity: 237 GB (62222336 x 512)\n"]
        with self.assertRaisesRegex(ram_twrp.RamTwrpError, "4096"):
            ram_twrp.verify_geometry(session)
        self.assertEqual(pe_fits.neighbors(
            {"lba": 0x38000, "blocks": 0x3b00}, {"lba": 0x3c000, "blocks": 0x3100}), [
                ("miui", 0x30000, 0x28bd), ("gap", 0x328bd, 0x1743),
                ("twrp", 0x34000, 0x2924), ("gap", 0x36924, 0x16dc),
                ("gap", 0x3bb00, 0x500), ("gap", 0x3f100, 0x8f00),
            ])

    def test_rom_return_uses_the_boot_identity_entry(self):
        ram = mock.Mock(UBOOT_SERIAL="1499001831")
        session = mock.Mock()
        good = (f"serial#=1499001831\ncrux_uboot_role={ram_twrp.UBOOT_ROLE}\n"
                f"crux_boot_pe_rom={ram_twrp.ROM_BOOT}\n")
        session.send.return_value = good
        ram_twrp.read_environment(session, ram, "version")
        ram.verify_environment.assert_called_once_with(session, "version")
        self.assertIn("if crux_install bootargs; then bootm prep; run boot_go; fi", ram_twrp.ROM_BOOT)
        for text, error in ((good.replace(ram_twrp.UBOOT_ROLE, "recovery-container-crux-install-1"), "identity build"),
                            (good.replace("crux_uboot_role", "other_role"), "identity build"),
                            (good.replace("if crux_install bootargs; then ", "").replace("; fi", ""), "ROM menu command")):
            with self.subTest(error=error):
                session.send.return_value = text
                with self.assertRaisesRegex(ram_twrp.RamTwrpError, error):
                    ram_twrp.read_environment(session, ram, "version")
        ram_twrp.boot_rom(ram, session)
        ram.guarded_handoff.assert_called_once_with(session, "run crux_boot_pe_rom")
        session.save.assert_called_once_with()

    def test_fit_windows_are_the_uboot_read_lengths(self):
        self.assertEqual(pe_fits.WRITE_WINDOWS, {"recovery": (0x38000, 0x3b00), "rom": (0x3c000, 0x3100)})

    def test_fit_writer_rejects_ranges_beyond_the_windows_before_console_access(self):
        session = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            fit = Path(directory) / "recovery.itb"
            fit.write_bytes(bytes.fromhex("d00dfeed"))
            small = {"path": fit, "lba": 0x38000, "blocks": 1,
                     "sha256": hashlib.sha256(bytes.fromhex("d00dfeed").ljust(4096, b"\0")).hexdigest()}
            for recovery, rom in (({"lba": 0x38000, "blocks": 0x3b01}, {"lba": 0x3c000, "blocks": 0x3100}),
                                  ({"lba": 0x38001, "blocks": 0x3aff}, {"lba": 0x3c000, "blocks": 0x3100}),
                                  (small, {"lba": 0x3c000, "blocks": 0x3101})):
                with self.assertRaisesRegex(RuntimeError, "outside the approved PE FIT write window"):
                    pe_fits.write_fits(session, mock.Mock(), recovery, rom,
                                            Path(directory), "version", Path("witness"))
        session.send.assert_not_called()

    def test_readiness_polls_until_candidate_adb_process_appears(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(ram_twrp.subprocess, "run", side_effect=[
                    SimpleNamespace(stdout="", stderr="no devices\n", returncode=1),
                    SimpleNamespace(stdout="3.7.1_12\n", stderr="", returncode=1),
                    SimpleNamespace(stdout="3.7.1_12\n123\n", stderr="", returncode=0),
                ]) as adb, mock.patch.object(ram_twrp.time, "sleep"):
            ram_twrp.wait_recovery("8bd37df4", Path(directory))
            self.assertEqual(adb.call_count, 3)

if __name__ == "__main__":
    unittest.main()
