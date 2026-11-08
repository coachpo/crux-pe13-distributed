"""Offline fault-injection tests for crux-installer (host build, CRUX_TEST).

A fixture root stands in for the phone: sparse files as block devices, by-name
links, sysfs extents, /proc/cmdline, the install TWRP guard receipts, fake
e2fsprogs and a test-only signing key. Each refusal case must leave every device file
without a single written block.
"""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile
import zlib

HERE = Path(__file__).resolve().parent
BINARY = HERE / "build/crux-installer-host"
OPENSSL = os.environ.get("OPENSSL") or next(
    (p for p in ("/opt/homebrew/opt/openssl@3/bin/openssl",) if Path(p).exists()), shutil.which("openssl"))
SERIAL = "8bd37df4"
ROLE = "recovery-container-crux-install-1"
PROFILE = "e" * 64
BLK = 4096
VOLUMES = {"pesystem": ("sda35", 0x29e4000, 0x100000), "pevendor": ("sda36", 0x2ae4000, 0x70000),
           "peuserdata": ("sda37", 0x2b54000, 0x1000000), "pemetadata": ("sda38", 0x3b54000, 0x1000),
           "cache": ("sda29", 0x30000, 0x18000)}
PROTECTED = {"userdata": "sda34", "metadata": "sda19", "misc": "sda11", "system": "sda33", "vendor": "sda32",
             "boot": "sde49", "recovery": "sda28", "dtbo": "sde45", "vbmeta": "sde10"}
STATE_REL, ROM_REL, REC_REL = 0xf0000, 0xf1000, 0xf8000
ROM_WIN, REC_WIN = 0x3100, 0x3b00
FAKE_TOOLS = {
    "mke2fs": '#!/bin/sh\necho "$@" >> "$(dirname "$0")/mke2fs.calls"\n'
              'for last; do :; done\nexit "${FAIL_MKE2FS:-0}"\n',
    "e2fsck": "#!/bin/sh\nexit 0\n",
    "tune2fs": '#!/bin/sh\ncase "$2" in *peuserdata) n=16777216; i=512; f="has_journal extent encrypt quota project";;\n'
               '*) n=4096; i=256; f="has_journal extent";; esac\n'
               'printf "Filesystem features:      %s\\nBlock count:              %s\\nInode size:\\t          %s\\n" "$f" "$n" "$i"\n',
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fit(size):
    body = bytearray(size)
    body[0:4] = bytes.fromhex("d00dfeed")
    body[4:8] = size.to_bytes(4, "big")
    body[8:] = os.urandom(size - 8)
    return bytes(body)


def raw_image(blocks):
    return os.urandom(blocks * BLK)


class Fixture:
    def __init__(self, directory):
        self.root = Path(directory) / "root"
        self.work = Path(directory) / "work"
        self.work.mkdir()
        dev = self.root / "dev/block"
        (dev / "by-name").mkdir(parents=True)
        for name, (node, start, blocks) in VOLUMES.items():
            path = dev / node
            with path.open("wb") as handle:
                handle.truncate(blocks * BLK)
            (dev / "by-name" / name).symlink_to(path)
            sysfs = self.root / "sys/class/block" / node
            sysfs.mkdir(parents=True)
            (sysfs / "start").write_text(f"{start * 8}\n")
            (sysfs / "size").write_text(f"{blocks * 8}\n")
            (sysfs / "uevent").write_text(f"MAJOR=259\nDEVNAME={node}\nDEVTYPE=partition\nPARTNAME={name}\n")
        for name, node in PROTECTED.items():
            (dev / node).write_bytes(b"")
            (dev / "by-name" / name).symlink_to(dev / node)
            (self.root / "sys/class/block" / node).mkdir(parents=True)
        for name in VOLUMES:
            self.set_ro(name, 0)
        for name in [*PROTECTED, "cache"]:
            self.set_ro(name, 1)
        (self.root / "proc").mkdir()
        (self.root / "proc/cmdline").write_text(
            f"console=ttyMSM0 androidboot.serialno={SERIAL} androidboot.crux_uboot={ROLE} rcu_nocbs=0-7\n")
        (self.root / "proc/mounts").write_text("rootfs / rootfs rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n")
        (self.root / "crux-ram-twrp-profile").write_text(PROFILE + "\n")
        (self.root / "crux-ram-twrp-guard.ready").write_text(PROFILE + "\n")
        tools = self.root / "system/bin"
        tools.mkdir(parents=True)
        for name, text in FAKE_TOOLS.items():
            (tools / name).write_text(text)
            (tools / name).chmod(0o755)
        self.key = self.work / "key.pem"
        subprocess.run([OPENSSL, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(self.key)],
                       check=True)
        public = subprocess.run([OPENSSL, "ec", "-in", str(self.key), "-pubout", "-outform", "DER"],
                                check=True, capture_output=True).stdout[-64:]
        (self.root / "crux/install-keys").mkdir(parents=True)
        (self.root / "crux/install-keys/b-p256.pub").write_bytes(public)
        self.system, self.vendor = raw_image(24), raw_image(8)
        self.rec_fit, self.rom_fit = fit(3 * BLK + 100), fit(2 * BLK + 7)

    def set_ro(self, name, value):
        node = VOLUMES[name][0] if name in VOLUMES else PROTECTED[name]
        (self.root / "sys/class/block" / node / "ro").write_text(f"{value}\n")

    def sign(self, text, key=None):
        path, der = self.work / "manifest", self.work / "sig.der"
        path.write_bytes(text)
        subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", str(key or self.key), "-out", str(der), str(path)],
                       check=True)
        data = der.read_bytes()
        pos, out = 2, b""
        for _ in range(2):
            length = data[pos + 1]
            out += data[pos + 2:pos + 2 + length].lstrip(b"\0").rjust(32, b"\0")
            pos += 2 + length
        return out

    def package(self, mutate=None, key=None, serial=SERIAL, tamper=None, rec_fit=None, truncate=False):
        stored = {}
        for part, data in (("pesystem", self.system), ("pevendor", self.vendor)):
            raw = self.work / f"{part}.raw"
            raw.write_bytes(data)
            subprocess.run(["zstd", "-q", "-f", "--long=27", str(raw), "-o", str(raw) + ".zst"], check=True)
            stored[part] = Path(str(raw) + ".zst").read_bytes()
        if truncate:
            stored["pesystem"] = stored["pesystem"][:len(stored["pesystem"]) // 2]
        rec_fit = rec_fit or self.rec_fit
        manifest = {"schema": "crux-install/1", "kind": "zip", "device": "crux", "product": "aosp_crux-userdebug",
                    "serialno": serial, "uboot_role": ROLE, "twrp_profiles": PROFILE, "data_policy": "discard",
                    "target_build": "2026110799", "target_fingerprint":
                    "Xiaomi/crux/crux:13/TQ3A.230901.001.B1/2026110799:userdebug/test-keys",
                    "target_marker": "crux-pe13-local-2026110799"}
        for part, data, name in (("pesystem", self.system, "system.raw.img.zst"),
                                 ("pevendor", self.vendor, "vendor.raw.img.zst")):
            manifest.update({f"image.{part}.entry": name, f"image.{part}.compression": "zstd",
                             f"image.{part}.stored_bytes": len(stored[part]),
                             f"image.{part}.stored_sha256": sha(stored[part]),
                             f"image.{part}.bytes": len(data), f"image.{part}.sha256": sha(data)})
        for role, data in (("recovery", rec_fit), ("rom", self.rom_fit)):
            manifest.update({f"fit.{role}.entry": f"{role}-cepheus.itb", f"fit.{role}.bytes": len(data),
                             f"fit.{role}.sha256": sha(data)})
        if mutate:
            mutate(manifest)
        text = "".join(f"{k}={v}\n" for k, v in manifest.items()).encode()
        signature = self.sign(text, key)
        if tamper:
            stored[tamper] = bytes([stored[tamper][0] ^ 1]) + stored[tamper][1:]
        path = self.work / "package.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
            archive.writestr("META-INF/com/google/android/update-binary", b"\x7fELF test")
            archive.writestr("crux-install.txt", text)
            archive.writestr("crux-install.sig", signature)
            archive.writestr("recovery-cepheus.itb", rec_fit)
            archive.writestr("rom-cepheus.itb", self.rom_fit)
            archive.writestr("system.raw.img.zst", stored["pesystem"])
            archive.writestr("vendor.raw.img.zst", stored["pevendor"])
        return path

    def run(self, package, confirm="yes", env=None):
        return subprocess.run([str(BINARY), "--root", str(self.root), "--confirm", confirm, str(package)],
                              capture_output=True, text=True, env={**os.environ, **(env or {})})

    def device(self, name):
        return self.root / "dev/block" / VOLUMES[name][0]

    def allocated(self):
        return {name: os.stat(self.device(name)).st_blocks for name in VOLUMES}

    def read(self, name, offset, size):
        with self.device(name).open("rb") as handle:
            handle.seek(offset)
            return handle.read(size)


class InstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(["make", "-s", "host"], cwd=HERE, check=True)

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.fx = Fixture(self.directory)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def refuse(self, result, message):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(message, result.stderr)
        self.assertIn("Nothing was written.", result.stderr)
        self.assertEqual(set(self.fx.allocated().values()), {0}, "a refused install wrote device blocks")
        self.assertFalse((self.fx.root / "system/bin/mke2fs.calls").exists())

    def test_clean_install_writes_volumes_stages_fits_and_state_last(self):
        result = self.fx.run(self.fx.package())
        self.assertEqual(result.returncode, 0, result.stderr)
        fx = self.fx
        self.assertEqual(fx.read("pesystem", 0, len(fx.system)), fx.system)
        self.assertEqual(fx.read("pevendor", 0, len(fx.vendor)), fx.vendor)
        rec = fx.read("pesystem", REC_REL * BLK, REC_WIN * BLK)
        rom = fx.read("pesystem", ROM_REL * BLK, ROM_WIN * BLK)
        self.assertEqual(rec, fx.rec_fit + bytes(REC_WIN * BLK - len(fx.rec_fit)))
        self.assertEqual(rom, fx.rom_fit + bytes(ROM_WIN * BLK - len(fx.rom_fit)))
        state = fx.read("pesystem", STATE_REL * BLK, BLK)
        self.assertEqual(int.from_bytes(state[:4], "little"), zlib.crc32(state[4:]) & 0xffffffff)
        fields = dict(item.decode().split("=", 1) for item in state[4:].split(b"\0") if item)
        txn = (fx.root / "reboot-request").read_text().strip()
        self.assertEqual(fields, {"crux_s_ver": "1", "crux_s_txn": txn, "crux_s_stage": "land", "crux_s_tries": "0",
                                  "crux_s_rom_crc": f"{zlib.crc32(rom) & 0xffffffff:08x}", "crux_s_rec": "1",
                                  "crux_s_rec_crc": f"{zlib.crc32(rec) & 0xffffffff:08x}"})
        self.assertTrue(txn.startswith("z2026110799-"))
        calls = (fx.root / "system/bin/mke2fs.calls").read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertIn("-I 512 -O encrypt,quota,project -E nodiscard,quotatype=usr:grp:prj", calls[0])
        self.assertTrue(calls[0].endswith("by-name/peuserdata 16777216"))
        self.assertTrue(calls[1].endswith("by-name/pemetadata 4096"))

    def test_cancel_writes_nothing(self):
        self.refuse(self.fx.run(self.fx.package(), confirm="no"), "installation cancelled")

    def test_foreign_signature_is_refused(self):
        other = self.fx.work / "other.pem"
        subprocess.run([OPENSSL, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(other)],
                       check=True)
        self.refuse(self.fx.run(self.fx.package(key=other)), "signature does not verify")

    def test_other_serial_is_refused(self):
        self.refuse(self.fx.run(self.fx.package(serial="0badc0de")), "this package is for serial 0badc0de")

    def test_plain_boot_uboot_is_refused(self):
        (self.fx.root / "proc/cmdline").write_text(f"androidboot.serialno={SERIAL} console=ttyMSM0\n")
        self.refuse(self.fx.run(self.fx.package()), "Recovery container U-Boot")

    def test_tampered_payload_is_refused(self):
        self.refuse(self.fx.run(self.fx.package(tamper="pevendor")), "differs from the manifest")

    def test_truncated_stream_is_refused(self):
        self.refuse(self.fx.run(self.fx.package(truncate=True)), "truncated zstd stream")

    def test_oversized_fit_is_refused(self):
        big = fit(REC_WIN * BLK + BLK)
        self.refuse(self.fx.run(self.fx.package(rec_fit=big)), "recovery FIT size does not fit its window")

    def test_wrong_geometry_is_refused(self):
        (self.fx.root / "sys/class/block/sda36/start").write_text("0\n")
        self.refuse(self.fx.run(self.fx.package()), "live GPT extent of pevendor")

    def test_writable_original_partition_is_refused(self):
        self.fx.set_ro("misc", 0)
        self.refuse(self.fx.run(self.fx.package()), "protected partition misc is not read-only")

    def test_unguarded_twrp_is_refused(self):
        (self.fx.root / "crux-ram-twrp-guard.ready").unlink()
        result = self.fx.run(self.fx.package())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(set(self.fx.allocated().values()), {0})

    def test_wrong_kind_is_refused(self):
        def mutate(manifest):
            manifest["data_policy"] = "preserve"
        self.refuse(self.fx.run(self.fx.package(mutate=mutate)), "not a crux-install/1 clean-install package")

    def test_failure_after_writes_never_writes_the_land_state(self):
        result = self.fx.run(self.fx.package(), env={"FAIL_MKE2FS": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("Re-run this ZIP", result.stderr)
        self.assertEqual(self.fx.read("pesystem", 0, len(self.fx.system)), self.fx.system)
        self.assertEqual(self.fx.read("pesystem", STATE_REL * BLK, BLK), bytes(BLK))
        self.assertEqual(self.fx.read("pesystem", ROM_REL * BLK, BLK), bytes(BLK))


if __name__ == "__main__":
    unittest.main()
