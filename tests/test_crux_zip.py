import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import crux_zip

BUILD = "2026110899"
FINGERPRINT = f"Xiaomi/crux/crux:13/TQ3A.230901.001.B1/{BUILD}:userdebug/test-keys"
PROFILE = "e" * 64


def find(name, *candidates):
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return shutil.which(name)


MKE2FS = find("mke2fs", "/opt/homebrew/opt/e2fsprogs/sbin/mke2fs", "/sbin/mke2fs")
TOOLS = MKE2FS and shutil.which("zstd") and find("debugfs", "/opt/homebrew/opt/e2fsprogs/sbin/debugfs",
                                                 "/sbin/debugfs")


def fit(size, serial=None):
    body = bytearray(size)
    body[0:4] = bytes.fromhex("d00dfeed")
    body[4:8] = size.to_bytes(4, "big")
    if serial:
        token = f"androidboot.serialno={serial} ".encode()
        body[64:64 + len(token)] = token
    return bytes(body)


@unittest.skipUnless(TOOLS, "mke2fs, debugfs and zstd are required")
class CruxZipTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.key, self.public = self.keypair("key")
        self.system = self.ext4("system", {"system/build.prop": (
            f"ro.build.version.incremental={BUILD}\nro.build.fingerprint={FINGERPRINT}\n"
            "ro.product.system.name=aosp_crux\nro.build.type=userdebug\n"),
            "system/etc/crux-delivery-verification.txt": f"crux-pe13-local-{BUILD}\n"})
        self.vendor = self.ext4("vendor", {"build.prop": "ro.vendor.build.type=userdebug\n"})
        self.installer = self.work / "update-binary"
        self.installer.write_bytes(b"\x7fELF\x02\x01\x01" + bytes(11) + b"\xb7\x00" + bytes(44))
        self.rec, self.rom = self.work / "recovery.itb", self.work / "rom.itb"
        self.rec.write_bytes(fit(3 * 4096 + 100))
        self.rom.write_bytes(fit(2 * 4096 + 7))

    def keypair(self, name):
        key, public = self.work / f"{name}.pem", self.work / f"{name}.pub.pem"
        subprocess.run([crux_zip.openssl(), "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key)],
                       check=True)
        subprocess.run([crux_zip.openssl(), "ec", "-in", str(key), "-pubout", "-out", str(public)], check=True,
                       capture_output=True)
        return key, public

    def ext4(self, name, files):
        root = self.work / f"{name}-root"
        for relative, text in files.items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(text)
        image = self.work / f"{name}.raw.img"
        subprocess.run([MKE2FS, "-q", "-F", "-t", "ext4", "-b", "4096", "-d", str(root), str(image), "1024"],
                       check=True, capture_output=True)
        return image

    def prepare(self, *extra, rec=None, build=BUILD, name="payload"):
        out = self.work / name
        argv = ["prepare", "--system", str(self.system), "--vendor", str(self.vendor),
                "--recovery-fit", str(rec or self.rec), "--rom-fit", str(self.rom),
                "--installer", str(self.installer), "--build-number", build, "--twrp-profile", PROFILE,
                "--out", str(out), *(extra or ("--any-serial",))]
        return crux_zip.main(argv), out

    def test_prepare_sign_finalize(self):
        status, payload = self.prepare()
        self.assertEqual(status, 0)
        self.assertEqual(crux_zip.main(["sign", "--payload", str(payload), "--key", str(self.key)]), 0)
        out = self.work / "zip"
        self.assertEqual(crux_zip.main(["finalize", "--payload", str(payload), "--public-key", str(self.public),
                                        "--out", str(out)]), 0)
        zip_path = out / f"crux-pe13-{BUILD}.zip"
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            self.assertEqual(names, [crux_zip.ENTRIES[key] for key in crux_zip.ZIP_ORDER])
            self.assertTrue(all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist()))
            fields = crux_zip.manifest_fields(archive.read("crux-install.txt"))
            self.assertEqual(archive.read("META-INF/com/google/android/update-binary"),
                             self.installer.read_bytes())
        self.assertEqual({k: fields[k] for k in ("kind", "serialno", "target_build", "target_marker",
                                                 "data_policy", "twrp_profiles", "target_fingerprint")},
                         {"kind": "zip", "serialno": "any", "target_build": BUILD,
                          "target_marker": f"crux-pe13-local-{BUILD}", "data_policy": "discard",
                          "twrp_profiles": PROFILE, "target_fingerprint": FINGERPRINT})
        self.assertEqual(fields["image.pesystem.sha256"], hashlib.sha256(self.system.read_bytes()).hexdigest())
        sha_line = (out / f"crux-pe13-{BUILD}.zip.sha256").read_text().split()
        self.assertEqual(sha_line, [crux_zip.digest(zip_path), zip_path.name])
        self.assertEqual(json.loads((out / "zip.json").read_text())["zip_bytes"], zip_path.stat().st_size)

    def test_finalize_refuses_another_key(self):
        _, payload = self.prepare()
        crux_zip.main(["sign", "--payload", str(payload), "--key", str(self.key)])
        _, other = self.keypair("other")
        self.assertEqual(crux_zip.main(["finalize", "--payload", str(payload), "--public-key", str(other),
                                        "--out", str(self.work / "zip")]), 1)
        self.assertFalse((self.work / "zip").exists())

    def test_finalize_refuses_a_changed_payload(self):
        _, payload = self.prepare()
        signature = subprocess.run([sys.executable, str(ROOT / "scripts/crux_zip.py"), "sign", "--payload",
                                    str(payload), "--key", str(self.key)],
                                   check=True, capture_output=True, text=True).stdout.strip()
        stored = payload / "vendor.raw.img.zst"
        stored.write_bytes(stored.read_bytes() + b"\0")
        self.assertEqual(crux_zip.main(["finalize", "--payload", str(payload), "--signature", signature,
                                        "--public-key", str(self.public), "--out", str(self.work / "zip")]), 1)

    def test_prepare_refusals(self):
        serial_fit = self.work / "serial.itb"
        serial_fit.write_bytes(fit(4096, serial="8bd37df4"))
        self.assertEqual(self.prepare(rec=serial_fit, name="p1")[0], 1)
        self.assertEqual(self.prepare("--serial", "8bd37df4", rec=serial_fit, name="p2")[0], 0)
        self.assertEqual(self.prepare("--serial", "0badc0de", rec=serial_fit, name="p3")[0], 1)
        self.assertEqual(self.prepare(build="2026110898", name="p4")[0], 1)
        big = self.work / "big.itb"
        big.write_bytes(fit(crux_zip.WINDOWS["recovery"] + 4096))
        self.assertEqual(self.prepare(rec=big, name="p5")[0], 1)

    def test_package_installs_with_the_host_installer(self):
        if not (shutil.which("make") and shutil.which("cc")):
            self.skipTest("make and a host C compiler are required")
        spec = importlib.util.spec_from_file_location("installer_tests", ROOT / "installer/test_installer.py")
        installer_tests = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer_tests)
        subprocess.run(["make", "-s", "host"], cwd=ROOT / "installer", check=True)
        (self.work / "device").mkdir()
        fixture = installer_tests.Fixture(self.work / "device")
        public = self.work / "fixture.pub.pem"
        subprocess.run([crux_zip.openssl(), "ec", "-in", str(fixture.key), "-pubout", "-out", str(public)],
                       check=True, capture_output=True)
        _, payload = self.prepare()
        crux_zip.main(["sign", "--payload", str(payload), "--key", str(fixture.key)])
        out = self.work / "zip"
        self.assertEqual(crux_zip.main(["finalize", "--payload", str(payload), "--public-key", str(public),
                                        "--out", str(out)]), 0)
        result = fixture.run(out / f"crux-pe13-{BUILD}.zip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fixture.read("pesystem", 0, self.system.stat().st_size), self.system.read_bytes())
        self.assertTrue((fixture.root / "reboot-request").read_text().startswith(f"z{BUILD}-"))


if __name__ == "__main__":
    unittest.main()
