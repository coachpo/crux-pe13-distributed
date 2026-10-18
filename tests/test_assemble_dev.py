import gzip
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import assemble_dev


def avb_image(path, algorithm=1, partition=b"recovery"):
    salt = b"\x01" * 32
    digest = b"\x02" * 32
    name = partition
    body = struct.pack(">Q", 4096) + b"sha256".ljust(32, b"\0")
    body += struct.pack(">IIII", len(name), len(salt), len(digest), 0) + b"\0" * 60
    body += name + salt + digest
    hash_descriptor = struct.pack(">QQ", 2, len(body)) + body
    key = b"com.android.build." + partition + b".fingerprint"
    value = b"Xiaomi/crux/crux:13/TQ3A.230901.001.B1/2026101105:userdebug/test-keys"
    property_body = struct.pack(">QQ", len(key), len(value)) + key + b"\0" + value
    property_descriptor = struct.pack(">QQ", 0, len(property_body)) + property_body
    descriptors = hash_descriptor + property_descriptor
    header = bytearray(256)
    header[0:4] = b"AVB0"
    struct.pack_into(">II", header, 4, 1, 0)
    struct.pack_into(">QQ", header, 12, 0, len(descriptors))
    struct.pack_into(">I", header, 28, algorithm)
    struct.pack_into(">QQ", header, 96, 0, len(descriptors))
    struct.pack_into(">Q", header, 112, 1)
    header[128:128 + 13] = b"avbtool 1.2.0"
    vbmeta = bytes(header) + descriptors
    original = 4096
    footer = b"AVBf" + struct.pack(">II", 1, 0)
    footer += struct.pack(">QQQ", original, original, len(vbmeta)) + b"\0" * 28
    data = b"\0" * original + vbmeta
    data = data.ljust(original + len(vbmeta) + 128, b"\0") + footer
    path.write_bytes(data)
    return value.decode()


def boot_image(path, ramdisk):
    page = 4096
    kernel = b"K" * 100
    header = bytearray(1648)
    header[0:8] = b"ANDROID!"
    struct.pack_into("<II", header, 8, len(kernel), 0x8000)
    struct.pack_into("<II", header, 16, len(ramdisk), 0x1000000)
    struct.pack_into("<II", header, 24, 0, 0)
    struct.pack_into("<I", header, 36, page)
    struct.pack_into("<II", header, 40, 1, 13)
    struct.pack_into("<I", header, 1632, 0)
    struct.pack_into("<Q", header, 1636, 0)
    struct.pack_into("<I", header, 1644, 1648)
    data = bytes(header).ljust(page, b"\0") + kernel.ljust(align(len(kernel), page), b"\0")
    data += ramdisk
    data = data.ljust(align(len(data), page), b"\0")
    path.write_bytes(data)
    return data


def align(value, page):
    return (value + page - 1) // page * page


def cpio_archive(names):
    entries = []
    for name in names:
        encoded = name.encode() + b"\0"
        fields = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, len(encoded), 0]
        entry = b"070701" + b"".join(b"%08x" % field for field in fields) + encoded
        entries.append(entry.ljust(align(len(entry), 4), b"\0"))
    return b"".join(entries)


class AssembleDevTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_avb_hash_footer_and_property_descriptors(self):
        image = self.root / "recovery.img"
        expected = avb_image(image)
        report = assemble_dev.parse_avb(image)
        self.assertEqual(report["footer_version"], [1, 0])
        self.assertEqual(report["algorithm_name"], "SHA256_RSA2048")
        self.assertEqual(report["hash_descriptors"][0]["partition_name"], "recovery")
        self.assertEqual(report["hash_descriptors"][0]["hash_algorithm"], "sha256")
        self.assertEqual(report["properties"]["com.android.build.recovery.fingerprint"], expected)

    def test_missing_avb_footer_is_refused(self):
        image = self.root / "plain.img"
        image.write_bytes(b"\0" * 4096)
        with self.assertRaises(ValueError):
            assemble_dev.parse_avb(image)

    def test_android_v1_header_and_ramdisk_placement(self):
        ramdisk = gzip.compress(cpio_archive(["debug_ramdisk", "dev"]))
        image = self.root / "boot.img"
        data = boot_image(image, ramdisk)
        header = assemble_dev.parse_android_boot(image)
        self.assertEqual(header["header_version"], 1)
        self.assertEqual(header["header_size"], 1648)
        self.assertEqual(header["ramdisk_size"], len(ramdisk))
        self.assertTrue(header["ramdisk_gzip"])
        self.assertEqual(header["expected_image_size"], len(data))

    def test_cpio_mount_points(self):
        ramdisk = self.root / "ramdisk.img"
        ramdisk.write_bytes(gzip.compress(cpio_archive(["debug_ramdisk", "dev", "metadata", "mnt",
                                                         "proc", "second_stage_resources", "sys"])))
        entries = assemble_dev.cpio_entries(ramdisk)
        for name in assemble_dev.MOUNT_POINTS:
            self.assertIn(name, entries)
