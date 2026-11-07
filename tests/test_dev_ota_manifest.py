import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import dev_ota
import dev_ota_manifest

BUILD = "2026110701"
E2FS = [shutil.which(tool) or f"/opt/homebrew/opt/e2fsprogs/sbin/{tool}" for tool in ("mke2fs", "debugfs")]
HAVE_E2FS = all(Path(tool).is_file() for tool in E2FS)
FINGERPRINT = f"Xiaomi/crux/crux:13/TQ3A.230901.001.B1/{BUILD}:userdebug/test-keys"
BLOCK = 4096


def sparse(chunks):
    """Android sparse v1.0 image and its expanded bytes; chunks are (kind, blocks, data)."""
    body, raw, total = b"", b"", 0
    for kind, blocks, data in chunks:
        if kind == "raw":
            body += struct.pack("<HHII", 0xCAC1, 0, blocks, 12 + len(data)) + data
            raw += data
        elif kind == "fill":
            body += struct.pack("<HHII", 0xCAC2, 0, blocks, 16) + data
            raw += data * (blocks * BLOCK // 4)
        elif kind == "skip":
            body += struct.pack("<HHII", 0xCAC3, 0, blocks, 12)
            raw += bytes(blocks * BLOCK)
        else:
            body += struct.pack("<HHII", 0xCAC4, 0, 0, 16) + b"\0\0\0\0"
        total += blocks
    header = struct.pack("<IHHHHIIII", 0xED26FF3A, 1, 0, 28, 12, BLOCK, total, len(chunks), 0)
    return header + body, raw


def fit(serial=b"8bd37df4", size=4096):
    body = b"bootargs=console=ttyMSM0 androidboot.serialno=" + serial if serial else b"bootargs=console=ttyMSM0"
    data = bytes.fromhex("d00dfeed") + size.to_bytes(4, "big") + body
    return data + bytes(size - len(data))


def keypair(directory):
    key = Path(directory) / "a.pem"
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key)],
                   check=True, capture_output=True)
    der = subprocess.run(["openssl", "ec", "-in", str(key), "-pubout", "-outform", "DER"],
                         check=True, capture_output=True).stdout
    return key, der[-64:]


def write_delivery(directory, *, rom=None):
    """The eight delivery files with tiny real system/vendor/ROM images."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    system, system_raw = sparse([("raw", 2, os.urandom(2 * BLOCK)), ("skip", 3, b""),
                                 ("fill", 2, b"\x01\x02\x03\x04"), ("crc", 0, b"")])
    vendor, vendor_raw = sparse([("fill", 1, b"\xaa\xbb\xcc\xdd"), ("raw", 1, os.urandom(BLOCK)), ("skip", 2, b"")])
    (directory / "system.img").write_bytes(system)
    subprocess.run(["zstd", "-q", "-f", "--long=27", str(directory / "system.img"), "-o",
                    str(directory / "system.img.zst")], check=True)
    (directory / "system.img").unlink()
    (directory / "vendor.img").write_bytes(vendor)
    (directory / "rom-cepheus.itb").write_bytes(fit() if rom is None else rom)
    for name in ("boot.img", "recovery.img", "recovery-cepheus.itb", "SHA256SUMS"):
        (directory / name).write_bytes(f"asset {name}\n".encode())
    (directory / "delivery-record.json").write_text(json.dumps({
        "build_number": BUILD, "source_selection": {"env": {"BUILD_FINGERPRINT": FINGERPRINT}}}))
    return system_raw, vendor_raw


def manifest_of(directory, **changes):
    directory = Path(directory)
    options = {"build": BUILD, "fingerprint": FINGERPRINT, "start_builds": "2026110700", "serial": "8bd37df4"}
    options.update(changes)
    return dev_ota_manifest.manifest(directory / "system.img.zst", directory / "vendor.img",
                                     directory / "rom-cepheus.itb", **options)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory(prefix="crux-test-ota-manifest-")
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.delivery = self.work / "delivery"
        self.system_raw, self.vendor_raw = write_delivery(self.delivery)

    def test_manifest_has_the_installer_and_updater_contract(self):
        text = manifest_of(self.delivery, source=(("assembly_run", "42"), ("compile_run", "41")))
        values = dict(line.split("=", 1) for line in text.decode().splitlines())
        system, vendor, rom = (self.delivery / name for name in ("system.img.zst", "vendor.img", "rom-cepheus.itb"))
        self.assertEqual(values, {
            "schema": "crux-ota/1", "kind": "a-ota", "device": "crux", "product": "aosp_crux-userdebug",
            "serialno": "8bd37df4", "uboot_role": "recovery-container-crux-install-1",
            "data_policy": "preserve", "target_build": BUILD, "target_fingerprint": FINGERPRINT,
            "target_marker": "none", "start_builds": "2026110700",
            "source.assembly_run": "42", "source.compile_run": "41",
            "asset.system.name": "system.img.zst", "asset.system.format": "zstd-sparse",
            "asset.system.bytes": str(system.stat().st_size), "asset.system.sha256": dev_ota.sha256(system),
            "asset.system.raw_bytes": str(len(self.system_raw)),
            "asset.system.raw_sha256": hashlib.sha256(self.system_raw).hexdigest(),
            "asset.vendor.name": "vendor.img", "asset.vendor.format": "sparse",
            "asset.vendor.bytes": str(vendor.stat().st_size), "asset.vendor.sha256": dev_ota.sha256(vendor),
            "asset.vendor.raw_bytes": str(len(self.vendor_raw)),
            "asset.vendor.raw_sha256": hashlib.sha256(self.vendor_raw).hexdigest(),
            "asset.rom_fit.name": "rom-cepheus.itb", "asset.rom_fit.bytes": str(rom.stat().st_size),
            "asset.rom_fit.sha256": dev_ota.sha256(rom)})
        self.assertEqual(text, manifest_of(self.delivery, source=(("assembly_run", "42"), ("compile_run", "41"))))

    def test_signature_verifies_only_the_signed_bytes_and_key(self):
        key, public = keypair(self.work)
        (self.work / "other").mkdir()
        _, other = keypair(self.work / "other")
        text = manifest_of(self.delivery)
        signature = dev_ota_manifest.sign(text, key)
        self.assertEqual(len(signature), 64)
        self.assertTrue(dev_ota_manifest.verify(text, signature, public))
        self.assertFalse(dev_ota_manifest.verify(text.replace(b"2026110700", b"2026110699"), signature, public))
        self.assertFalse(dev_ota_manifest.verify(text, signature, other))
        self.assertFalse(dev_ota_manifest.verify(text, signature[:63], public))

    def test_identity_and_window_refusals(self):
        cases = {
            "serial identity": {"serial": "00000000"},
            "start builds": {"start_builds": "any"},
            "test-keys identity": {"fingerprint": FINGERPRINT.replace("test-keys", "release-keys")},
        }
        for message, change in cases.items():
            with self.subTest(message), self.assertRaisesRegex(dev_ota.DevOtaError, message):
                manifest_of(self.delivery, **change)
        with self.assertRaisesRegex(dev_ota.DevOtaError, "serial identity"):
            manifest_of(self.delivery, serial="any")
        for rom, message in ((fit(size=0x3101 * BLOCK), "landing window"),
                             (fit()[:-1], "complete flattened image tree")):
            (self.delivery / "rom-cepheus.itb").write_bytes(rom)
            with self.subTest(message), self.assertRaisesRegex(dev_ota.DevOtaError, message):
                manifest_of(self.delivery)
        (self.delivery / "rom-cepheus.itb").write_bytes(fit(serial=None))
        self.assertIn(b"serialno=any\n", manifest_of(self.delivery, serial="any"))

    def test_truncated_or_padded_payloads_are_refused(self):
        vendor = (self.delivery / "vendor.img").read_bytes()
        for data, message in ((vendor[:-1], "truncated"), (vendor + b"\0", "data after")):
            (self.delivery / "vendor.img").write_bytes(data)
            with self.subTest(message), self.assertRaisesRegex(dev_ota.DevOtaError, message):
                manifest_of(self.delivery)

    @unittest.skipUnless(HAVE_E2FS, "needs mke2fs and debugfs")
    def test_target_must_carry_the_update_stack_and_release_key(self):
        _, public = keypair(self.work)
        (self.work / "a.pub").write_text(public.hex() + "\n")

        def system(files):
            tree = self.work / "tree"
            shutil.rmtree(tree, ignore_errors=True)
            for path, data in files.items():
                (tree / path.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
                (tree / path.lstrip("/")).write_bytes(data)
            image = self.work / "system.ext4"
            image.unlink(missing_ok=True)
            subprocess.run([E2FS[0], "-q", "-F", "-t", "ext4", "-b", "4096", "-d", str(tree), str(image), "8M"],
                           check=True, capture_output=True)
            raw = image.read_bytes()
            header = struct.pack("<IHHHHIIII", 0xED26FF3A, 1, 0, 28, 12, BLOCK, len(raw) // BLOCK, 1, 0)
            (self.work / "system.img").write_bytes(header + struct.pack("<HHII", 0xCAC1, 0, len(raw) // BLOCK,
                                                                       12 + len(raw)) + raw)
            subprocess.run(["zstd", "-q", "-f", str(self.work / "system.img"), "-o", str(self.work / "system.img.zst")],
                           check=True)
            return self.work / "system.img.zst"

        stack = {path: b"x" for path in dev_ota_manifest.UPDATE_STACK}
        stack[dev_ota_manifest.UPDATE_STACK_KEY] = public.hex().encode() + b"\n"
        with mock.patch.object(dev_ota_manifest, "PUBLIC_KEY", self.work / "a.pub"):
            dev_ota_manifest.require_update_stack(system(stack), debugfs=E2FS[1])
            partial = dict(stack)
            del partial["/system/bin/crux-installer"]
            with self.assertRaisesRegex(dev_ota.DevOtaError, "lacks the A update stack: /system/bin/crux-installer"):
                dev_ota_manifest.require_update_stack(system(partial), debugfs=E2FS[1])
            stack[dev_ota_manifest.UPDATE_STACK_KEY] = b"00" * 64 + b"\n"
            with self.assertRaisesRegex(dev_ota.DevOtaError, "another A release key"):
                dev_ota_manifest.require_update_stack(system(stack), debugfs=E2FS[1])

    def test_repository_key_is_a_raw_p256_point(self):
        self.assertEqual(len(dev_ota_manifest.public_key()), 64)
        with mock.patch.object(dev_ota_manifest, "PUBLIC_KEY", self.delivery / "boot.img"), \
                self.assertRaisesRegex(dev_ota.DevOtaError, "not a raw P-256 public key"):
            dev_ota_manifest.public_key()


if __name__ == "__main__":
    unittest.main()
