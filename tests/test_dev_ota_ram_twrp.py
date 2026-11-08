import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import dev_ota
import dev_ota_fits
import dev_ota_ram_twrp as ram_twrp


class RamTwrpTests(unittest.TestCase):
    def test_manifest_uses_external_pin_and_verified_fit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fit = root / "recovery.itb"
            fit.write_bytes(b"expected FIT")
            manifest = root / "candidate.json"
            manifest.write_text(json.dumps({"schema_version": 1, "profile_id": "a" * 64,
                                             "fit": {"path": str(fit), "sha256": dev_ota.sha256(fit)}}))
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

    def test_execute_requires_explicit_tag_review_and_start_before_discovery(self):
        fields = {"tag": "dev-pe13-2026110501-v1", "reviewed_plan": Path("review.json"),
                  "expected_start_fingerprint": "approved fingerprint"}
        for field, flag in (("tag", "--tag"), ("reviewed_plan", "--reviewed-plan"),
                            ("expected_start_fingerprint", "--expected-start-fingerprint")):
            with self.subTest(field=field), \
                    mock.patch.multiple(dev_ota, discover=mock.DEFAULT, device_state=mock.DEFAULT) as calls:
                args = SimpleNamespace(execute=True, out="unused", **{**fields, field: None})
                with self.assertRaisesRegex(dev_ota.DevOtaError, flag):
                    dev_ota.update_command(args)
                calls["discover"].assert_not_called()
                calls["device_state"].assert_not_called()

    def test_execute_without_manifest_does_not_even_discover(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.multiple(dev_ota, RAM_SCRIPT=Path(__file__), INSTALL_SCRIPT=Path(__file__),
                                     WATCH_SCRIPT=Path(__file__), discover=mock.DEFAULT) as calls, \
                mock.patch.object(dev_ota_fits, "load_ram"):
            root = Path(directory)
            tag = "dev-pe13-2026110501-v1"
            review = root / "review.json"
            review.write_text(json.dumps({"tag": tag}))
            args = SimpleNamespace(execute=True, out=root / "new", tag=tag, reviewed_plan=review,
                                   expected_start_fingerprint="approved fingerprint",
                                   ram_twrp_manifest=None, ram_twrp_manifest_sha256=None)
            with self.assertRaisesRegex(ram_twrp.RamTwrpError, "approved"):
                dev_ota.update_command(args)
            calls["discover"].assert_not_called()

    def test_execute_requires_runtime_helper_files_before_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tag = "dev-pe13-2026110501-v1"
            review = root / "review.json"
            review.write_text(json.dumps({"tag": tag}))
            args = SimpleNamespace(execute=True, out=root / "new", tag=tag, reviewed_plan=review,
                                   expected_start_fingerprint="approved fingerprint")
            paths = dict.fromkeys(("RAM_SCRIPT", "INSTALL_SCRIPT", "WATCH_SCRIPT"), Path(__file__))
            for key in paths:
                with self.subTest(key=key), mock.patch.multiple(
                        dev_ota, discover=mock.DEFAULT, device_state=mock.DEFAULT,
                        **{**paths, key: root / "missing.py"}) as calls:
                    with self.assertRaisesRegex(dev_ota.DevOtaError, "missing"):
                        dev_ota.update_command(args)
                    calls["discover"].assert_not_called()
                    calls["device_state"].assert_not_called()

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
        self.assertEqual(dev_ota_fits.neighbors(
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
        self.assertIn("if crux_ota bootargs; then bootm prep; run boot_go; fi", ram_twrp.ROM_BOOT)
        for text, error in ((good.replace(ram_twrp.UBOOT_ROLE, "recovery-container-crux-install-1"), "identity build"),
                            (good.replace("crux_uboot_role", "other_role"), "identity build"),
                            (good.replace("if crux_ota bootargs; then ", "").replace("; fi", ""), "ROM menu command")):
            with self.subTest(error=error):
                session.send.return_value = text
                with self.assertRaisesRegex(ram_twrp.RamTwrpError, error):
                    ram_twrp.read_environment(session, ram, "version")
        ram_twrp.boot_rom(ram, session)
        ram.guarded_handoff.assert_called_once_with(session, "setenv crux_ota_extra; run crux_boot_pe_rom")
        session.save.assert_called_once_with()

    def test_fit_windows_are_the_uboot_read_lengths(self):
        self.assertEqual(dev_ota_fits.WRITE_WINDOWS, {"recovery": (0x38000, 0x3b00), "rom": (0x3c000, 0x3100)})

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
                    dev_ota_fits.write_fits(session, mock.Mock(), recovery, rom,
                                            Path(directory), "version", Path("witness"))
        session.send.assert_not_called()

    def test_readiness_polls_until_candidate_adb_process_appears(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(ram_twrp.subprocess, "run", side_effect=[
                    SimpleNamespace(stdout="", stderr="no devices\n", returncode=1),
                    SimpleNamespace(stdout="3.7.1_12\n", stderr="", returncode=1),
                    SimpleNamespace(stdout="3.7.1_12\n123\n", stderr="", returncode=0),
                ]) as adb, mock.patch.object(ram_twrp.time, "sleep"):
            ram_twrp.wait_recovery(dev_ota.DEFAULT_SERIAL, Path(directory))
            self.assertEqual(adb.call_count, 3)

    def run_simulated_update(self, failure=None):
        serial = dev_ota.DEFAULT_SERIAL
        current = {"boot_completed": "1", "incremental": "2026101105",
                   "fingerprint": "Xiaomi/crux/crux:13/TQ3A.230901.001.B1/2026101105:userdebug/test-keys"}
        shell = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "new"
            tag = "dev-pe13-2026110501-v1"
            review = root / "review.json"
            args = SimpleNamespace(execute=True, out=out, serial=serial, force=False,
                                   boot_timeout=300, uboot_version="U-Boot version", tag=tag,
                                   reviewed_plan=review, expected_start_fingerprint=current["fingerprint"],
                                   ram_twrp_manifest=Path("manifest"), ram_twrp_manifest_sha256="b" * 64)
            if failure == "wrong_start":
                args.expected_start_fingerprint = current["fingerprint"].replace("2026101105", "2026101104")
            plan = {"schema_version": 1, "serial": serial, "tag": tag, "build": "2026110501",
                    "fingerprint": current["fingerprint"].replace("2026101105", "2026110501"),
                    "data_policy": "discard", "vold_sha256": "vold", "uboot_version": args.uboot_version,
                    "twrp_crc32": dev_ota_fits.RETAINED_TWRP_CRC32, "twrp_fit": "cache-witness.itb",
                    "assets": {"boot.img": {"size": 8, "sha256": "boot"}},
                    "raw": {"system": {"path": "system.raw", "sha256": "s", "size": 4096},
                            "vendor": {"path": "vendor.raw", "sha256": "v", "size": 4096}},
                    "fits": {"recovery": {"lba": 0x38000, "blocks": 0x3b00, "sha256": "r"},
                             "rom": {"lba": 0x3c000, "blocks": 0x3100, "sha256": "b"}}}
            if failure == "wrong_cache_witness":
                plan["twrp_crc32"] = "bad"
            review.write_text(json.dumps(plan))
            if failure == "wrong_reviewed_bytes":
                plan["raw"]["system"]["sha256"] = "bad"
            ram = SimpleNamespace(SERIAL=serial, LOG=Path(__file__), CMD=Path(__file__))
            candidate = {"profile_id": ram_twrp.V2_PROFILE_ID, "fit_sha256": ram_twrp.V2_FIT_SHA256}
            if failure == "wrong_v2_fit":
                candidate["fit_sha256"] = "a" * 64
            if failure == "wrong_v2_profile":
                candidate["profile_id"] = "a" * 64
            calls = mock.Mock()
            calls.reboot_to_shell.return_value = shell
            if failure == "first_guard":
                calls.boot.side_effect = ram_twrp.RamTwrpError("first guard failed")
            elif failure == "second_guard":
                calls.boot.side_effect = [None, ram_twrp.RamTwrpError("second guard failed")]

            def make_plan(*unused, no_device=False):
                self.assertTrue(no_device)
                out.mkdir()
                return plan

            adb_responses = [
                SimpleNamespace(stdout="device\n"),
                SimpleNamespace(stdout=serial + "\n/dev/block/by-name/pesystem / ext4 ro 0 0\n"
                                          "/dev/block/by-name/pevendor /vendor ext4 ro 0 0\n"),
            ]
            with mock.patch.multiple(dev_ota, RAM_SCRIPT=Path(__file__), INSTALL_SCRIPT=Path(__file__),
                                     WATCH_SCRIPT=Path(__file__),
                                     select_head=mock.Mock(), discover=mock.Mock(),
                                     make_plan=make_plan, print_plan=mock.Mock(),
                                     sha256=mock.Mock(return_value=("wrong" if failure == "wrong_cache_sha"
                                                                   else dev_ota_fits.RETAINED_TWRP_SHA256)),
                                     device_state=mock.Mock(return_value=current),
                                     adb=mock.Mock(side_effect=adb_responses),
                                     run=mock.Mock(return_value=SimpleNamespace(stdout="1000\n", returncode=0)),
                                     wait_for=calls.wait_for, install=calls.install,
                                     verify_device=mock.Mock(return_value={"status": "verified"})), \
                    mock.patch.multiple(dev_ota_fits, load_ram=mock.Mock(return_value=ram),
                                        write_fits=calls.write_fits), \
                    mock.patch.multiple(ram_twrp, validate_manifest=mock.Mock(return_value=candidate),
                                        helper=calls.helper, reboot_to_shell=calls.reboot_to_shell,
                                        boot=calls.boot, boot_rom=calls.boot_rom,
                                        verify_geometry=calls.verify_geometry):
                calls.install.return_value = {"status": "installed"}
                if failure in ("wrong_v2_fit", "wrong_v2_profile"):
                    with self.assertRaisesRegex(dev_ota.DevOtaError, "accepted RAM TWRP v2"):
                        dev_ota.update_command(args)
                    dev_ota.discover.assert_not_called()
                    dev_ota.device_state.assert_not_called()
                elif failure in ("wrong_reviewed_bytes", "wrong_cache_witness", "wrong_cache_sha"):
                    with self.assertRaisesRegex(dev_ota.DevOtaError, "execution payload|accepted CRC|accepted SHA-256"):
                        dev_ota.update_command(args)
                    dev_ota.device_state.assert_not_called()
                elif failure == "wrong_start":
                    with self.assertRaisesRegex(dev_ota.DevOtaError, "approved starting fingerprint"):
                        dev_ota.update_command(args)
                elif failure:
                    with self.assertRaisesRegex(ram_twrp.RamTwrpError, "guard failed"):
                        dev_ota.update_command(args)
                else:
                    self.assertEqual(dev_ota.update_command(args), 0)
                    calls.install.assert_called_once_with(plan, out, candidate["profile_id"])
            return [record[0] for record in calls.mock_calls]

    def test_rehearsal_closes_before_fits_and_second_guard_precedes_raw_install(self):
        self.assertEqual(self.run_simulated_update(), [
            "helper", "reboot_to_shell", "boot", "reboot_to_shell", "boot_rom", "wait_for",
            "reboot_to_shell", "verify_geometry", "write_fits", "boot", "install",
            "reboot_to_shell", "boot_rom",
        ])

    def test_failed_rehearsal_stops_all_writes(self):
        self.assertEqual(self.run_simulated_update("first_guard"), ["helper", "reboot_to_shell", "boot"])

    def test_failed_second_guard_stops_raw_install(self):
        calls = self.run_simulated_update("second_guard")
        self.assertEqual(calls[-2:], ["write_fits", "boot"])
        self.assertNotIn("install", calls)

    def test_wrong_dev_start_stops_before_reboot_or_write(self):
        self.assertEqual(self.run_simulated_update("wrong_start"), [])

    def test_wrong_v2_identity_stops_before_discovery(self):
        for failure in ("wrong_v2_fit", "wrong_v2_profile"):
            with self.subTest(failure=failure):
                self.assertEqual(self.run_simulated_update(failure), [])

    def test_wrong_reviewed_bytes_or_cache_witness_stops_before_device_access(self):
        for failure in ("wrong_reviewed_bytes", "wrong_cache_witness", "wrong_cache_sha"):
            with self.subTest(failure=failure):
                self.assertEqual(self.run_simulated_update(failure), [])

    def test_payload_pin_allows_relocation_but_rejects_changed_raw_bytes(self):
        reviewed = dict.fromkeys(("schema_version", "serial", "tag", "build", "fingerprint",
                                  "data_policy", "vold_sha256", "twrp_crc32", "uboot_version"), "verified")
        reviewed.update(assets={"boot.img": {"size": 1, "sha256": "a"}},
                        raw={"system": {"size": 2, "sha256": "b", "path": "review.raw"}},
                        fits={"rom": {"lba": 3, "blocks": 4, "sha256": "c"}})
        plan = json.loads(json.dumps(reviewed))
        plan["raw"]["system"]["path"] = "execution.raw"
        dev_ota.require_reviewed_payload(plan, reviewed)
        plan["raw"]["system"]["sha256"] = "unexpected"
        with self.assertRaisesRegex(dev_ota.DevOtaError, "raw/system"):
            dev_ota.require_reviewed_payload(plan, reviewed)


if __name__ == "__main__":
    unittest.main()
