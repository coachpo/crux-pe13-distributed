"""Host-only fixtures for the install TWRP ramdisk and DT checks, on synthetic inputs (no mkimage)."""

import importlib.util
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

TOOL = Path(__file__).with_name("build-b-twrp.py")
spec = importlib.util.spec_from_file_location("build_b_twrp", TOOL)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
Entry = b.PACK["Entry"]

PREINIT = b"\x7fELF-static-guard"
KEY = bytes(range(64))


def entry(ino, name, mode, data=b""):
    return Entry(name, (ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name) + 1, 0), data)


def source(**override):
    files = {
        "init": (stat.S_IFLNK | 0o777, b"/system/bin/init"),
        b.FSTAB: (stat.S_IFREG | 0o644, b"/dev/block/by-name/system /system ext4 ro wait\n"),
        b.FLAGS: (stat.S_IFREG | 0o644, b"/data ext4 /dev/block/by-name/userdata\n"),
        b.SERVICES: (stat.S_IFREG | 0o644, b"service adbd /system/bin/adbd\n" + b.STANZA + b"    seclabel u:r:recovery:s0\n"),
        b.QCOM_RC: (stat.S_IFREG | 0o644, b"import /init.recovery.qcom_decrypt.rc\non init\n"),
        "init.recovery.qcom_decrypt.rc": (stat.S_IFREG | 0o644, b"on fs\n    mount_all /vendor/etc/fstab.qcom\n"),
        b.THEME: (stat.S_IFREG | 0o644, ("<page>\n" + b.SLIDER + "</page>\n").encode()),
        "system/bin/recovery": (stat.S_IFREG | 0o755, b"twrp"),
    }
    files.update(override)
    entries = {name: entry(i + 1, name, mode, data) for i, (name, (mode, data)) in enumerate(files.items())}
    entries["TRAILER!!!"] = entry(0, "TRAILER!!!", 0)
    return entries


class InstallRamdisk(unittest.TestCase):
    def test_edits_and_additions(self):
        src = source()
        entries, profile = b.install_ramdisk(src, PREINIT, KEY)
        self.assertEqual(entries["init"].data, PREINIT)
        self.assertEqual(entries["init"].fields[1], stat.S_IFREG | 0o750)
        self.assertEqual(entries[b.FSTAB].data, b.B_FSTAB)
        self.assertEqual(entries[b.FLAGS].data, b.NO_ROUTES)
        self.assertIn(b.STANZA + b.CAPABILITIES, entries[b.SERVICES].data)
        self.assertNotIn(b.DECRYPT_IMPORT, entries[b.QCOM_RC].data)
        self.assertEqual(entries[b.THEME].data.decode().count(b.B_SLIDER), 1)
        self.assertEqual(entries[b.KEY].data, KEY)
        self.assertEqual(entries[b.SINK].data, bytes(16384))
        self.assertTrue(stat.S_ISDIR(entries["TWRP"].fields[1]) and entries["TWRP/.twrpcf"].data == b"")
        self.assertEqual(entries[b.PACK["PROFILE"]].data, (profile + "\n").encode())
        self.assertEqual(list(entries)[-1], "TRAILER!!!")
        self.assertEqual(entries["system/bin/recovery"], src["system/bin/recovery"])
        inodes = [e.fields[0] for e in entries.values() if e.name != "TRAILER!!!"]
        self.assertEqual(len(inodes), len(set(inodes)))

    def test_profile_binds_preinit_and_content(self):
        _, first = b.install_ramdisk(source(), PREINIT, KEY)
        self.assertEqual(first, b.install_ramdisk(source(), PREINIT, KEY)[1])
        self.assertNotEqual(first, b.install_ramdisk(source(), PREINIT + b"x", KEY)[1])
        self.assertNotEqual(first, b.install_ramdisk(source(), PREINIT, bytes(64))[1])

    def test_rejects_unexpected_source(self):
        cases = {
            "init is not the stock link": {"init": (stat.S_IFREG | 0o750, b"guard")},
            "service already bounded": {b.SERVICES: (stat.S_IFREG | 0o644, b.STANZA + b.CAPABILITIES)},
            "no decryption import": {b.QCOM_RC: (stat.S_IFREG | 0o644, b"on init\n")},
            "slider changed": {b.THEME: (stat.S_IFREG | 0o644, b"<page/>\n")},
            "extra fstab": {"vendor/etc/fstab.qcom": (stat.S_IFREG | 0o644, b"")},
            "mount trigger": {"init.recovery.crux.rc": (stat.S_IFREG | 0o644, b"on fs\n    mount_all /fstab\n")},
            "already derived": {b.KEY: (stat.S_IFREG | 0o644, KEY)},
        }
        for label, override in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                b.install_ramdisk(source(**override), PREINIT, KEY)
        with self.assertRaises(ValueError):
            b.install_ramdisk(source(), PREINIT, KEY[:63])


@unittest.skipUnless(shutil.which("dtc"), "dtc is required")
class SourceDt(unittest.TestCase):
    VENDOR = "/dev/block/platform/soc/1d84000.ufshc/by-name/vendor"

    def dtb(self, bootargs, extra=""):
        source = f"""/dts-v1/;
/ {{ chosen {{ bootargs = "{bootargs}"; }};
     firmware {{ android {{ fstab {{ vendor {{ dev = "{self.VENDOR}"; status = "okay"; }}; {extra} }}; }}; }}; }};
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.dts"
            path.write_text(source)
            return subprocess.run(["dtc", "-I", "dts", "-O", "dtb", str(path)], check=True, capture_output=True).stdout

    def test_accepts_identity_free_ramdisk_entry(self):
        b.check_dt(self.dtb("console=ttyMSM0 init=/init androidboot.hardware=qcom"))

    def test_rejects(self):
        cases = {
            "identity token": self.dtb("init=/init androidboot.serialno=0123abcd"),
            "no init": self.dtb("console=ttyMSM0"),
            "root entry": self.dtb("init=/init root=/dev/sda1"),
            "extra fstab route": self.dtb("init=/init", 'system { dev = "/dev/block/by-name/system"; };'),
        }
        for label, blob in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                b.check_dt(blob)


if __name__ == "__main__":
    unittest.main()
