import gzip
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import package_fits
import worker


recipe = package_fits.load_recipe()
FSTAB = b"""/dev/block/by-name/pesystem /system ext4 ro wait,first_stage_mount
/dev/block/by-name/pevendor /vendor ext4 ro wait,first_stage_mount
/dev/block/by-name/pemetadata /metadata ext4 noatime wait,first_stage_mount
/dev/block/by-name/peuserdata /data ext4 noatime wait,fileencryption=ice,keydirectory=/metadata/vold/metadata_encryption
"""


def tree(properties):
    fdt = recipe.Fdt.__new__(recipe.Fdt)
    fdt.root = recipe.node("")
    fdt.root["props"] = properties
    fdt.reservations = []
    fdt.boot_cpuid = 0
    return fdt.encode()


def ramdisk(role):
    files = {"init": b"native-init"}
    if role == "boot":
        files.update({"system/etc/ramdisk/build.prop": b"ro.bootimage.build.version.release=13\n", "fstab.crux_uboot": FSTAB})
    else:
        files.update({"prop.default": b"ro.secure=1\nro.adb.secure=0\nro.adb.secure.recovery=0\n", "system/etc/recovery.fstab": FSTAB})
    data = bytearray()
    for index, (name, contents) in enumerate([*files.items(), ("TRAILER!!!", b"")], 1):
        fields = [index, 0o100644, 0, 0, 1, 0, len(contents), 0, 0, 0, 0, len(name) + 1, 0]
        data.extend(b"070701" + b"".join(f"{value:08x}".encode() for value in fields))
        data.extend(name.encode() + b"\0")
        data.extend(b"\0" * (-len(data) % 4))
        data.extend(contents)
        data.extend(b"\0" * (-len(data) % 4))
    return gzip.compress(bytes(data), mtime=0)


@unittest.skipUnless(shutil.which("zstd"), "zstd is required for FIT/symbol archives")
class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.product = self.root / "product"
        self.kernel = self.product / "obj/KERNEL_OBJ"
        self.qcom = self.kernel / "arch/arm64/boot/dts/qcom"
        self.qcom.mkdir(parents=True)
        self.output = self.root / "package"
        self.image = self.kernel / "arch/arm64/boot/Image"
        image = bytearray(64)
        struct.pack_into("<3Q", image, 8, 0x80000, 64, 0)
        image[56:60] = b"ARM\x64"
        self.image.write_bytes(image)
        elf = bytearray(64)
        elf[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", elf, 18, 183)
        (self.kernel / "vmlinux").write_bytes(elf)
        (self.kernel / "System.map").write_text("ffffff8008080000 T _text\n")
        (self.kernel / ".config").write_text("CONFIG_ARM64=y\n")
        for name in package_fits.BASES:
            (self.qcom / (name + ".dtb")).write_bytes(tree({"qcom,msm-id": struct.pack(">2I", *recipe.BASE_IDS[name])}))
        self.overlay = self.qcom / "crux-sm8150-overlay.dtbo"
        self.overlay.write_bytes(tree({"model": b"SDX50M CRUX\0", "compatible": b"qcom,sm8150\0",
                                       "qcom,board-id": struct.pack(">2I", 0x2B, 1)}))
        (self.product / "ramdisk.img").write_bytes(ramdisk("boot"))
        (self.product / "ramdisk-recovery.img").write_bytes(ramdisk("recovery"))
        specs = [worker.describe(path) for path in self.product.rglob("*") if path.is_file()]
        self.accepted = {spec["path"]: spec for spec in specs}
        self.receipts = [{"status": "success", "id": "kernel", "run_id": "123", "manifest_sha256": "1" * 64,
                          "archive_sha256": "2" * 64, "outputs": specs}]

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, **kwargs):
        return package_fits.prepare(self.accepted, self.receipts, self.output, self.product,
                                    self.kernel, source_selection={"kernel": "fresh-source"}, **kwargs)

    def test_inputs_only_keeps_fresh_symbol_pair_and_raw_inputs(self):
        result = self.prepare(build_fits=False)
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertFalse(result["fits_generated"])
        raw = subprocess.run(["zstd", "-dc", str(self.output / "kernel-symbols.tar.zst")], check=True, stdout=subprocess.PIPE).stdout
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            self.assertTrue({"Image", "vmlinux", "System.map", ".config", "kernel-build.json"}.issubset(archive.getnames()))
            self.assertEqual(archive.extractfile("Image").read(), self.image.read_bytes())
            build = json.load(archive.extractfile("kernel-build.json"))
            self.assertEqual(build["kernel_producer"]["run_id"], "123")
            self.assertEqual(build["kernel"]["sha256"], worker.digest(self.image))
        self.assertEqual((self.output / "fit-inputs/crux-sm8150-overlay.dtbo").read_bytes(), self.overlay.read_bytes())
        self.assertEqual(len(list((self.output / "fit-inputs/base-dtbs").glob("*.dtb"))), 4)

    def test_old_ambient_image_without_receipt_cannot_seed_package(self):
        del self.accepted[str(self.image)]
        result = self.prepare(build_fits=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Missing fresh", result["error"])

    def test_symbols_from_a_different_build_are_rejected(self):
        symbol = str(self.kernel / "System.map")
        moved = next(spec for spec in self.receipts[0]["outputs"] if spec["path"] == symbol)
        self.receipts[0]["outputs"].remove(moved)
        self.receipts.append({"status": "success", "id": "old-kernel", "run_id": "122",
                              "manifest_sha256": "3" * 64, "archive_sha256": "4" * 64, "outputs": [moved]})
        result = self.prepare(build_fits=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("share one successful kernel build", result["error"])

    def test_dtbo_table_extracts_fresh_overlay_from_kernel_dependency(self):
        payload = self.overlay.read_bytes()
        self.receipts[0]["outputs"] = [spec for spec in self.receipts[0]["outputs"] if spec["path"] != str(self.overlay)]
        del self.accepted[str(self.overlay)]
        compressed = gzip.compress(payload, mtime=0)
        table = struct.pack(">8I", 0xD7B7AB1E, 64 + len(compressed), 32, 32, 1, 32, 4096, 1)
        table += struct.pack(">8I", len(compressed), 64, 0, 0, 2, 0, 0, 0) + compressed
        path = self.product / "dtbo.img"
        path.write_bytes(table)
        spec = worker.describe(path)
        self.accepted[str(path)] = spec
        self.receipts.append({"status": "success", "id": "dtbo", "run_id": "124", "manifest_sha256": "3" * 64,
                              "archive_sha256": "4" * 64, "outputs": [spec],
                              "dependencies": [{"id": "kernel", "manifest_sha256": "1" * 64, "archive_sha256": "2" * 64}]})
        result = self.prepare(build_fits=False)
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertEqual((self.output / "fit-inputs/crux-sm8150-overlay.dtbo").read_bytes(), payload)

    def test_distinct_crux_overlays_are_ambiguous(self):
        first = self.overlay.read_bytes()
        second = tree({**recipe.Fdt(first).root["props"], "new-property": b"different\0"})
        total = 96 + len(first) + len(second)
        table = struct.pack(">8I", 0xD7B7AB1E, total, 32, 32, 2, 32, 4096, 0)
        table += struct.pack(">8I", len(first), 96, 0, 0, 0, 0, 0, 0)
        table += struct.pack(">8I", len(second), 96 + len(first), 0, 0, 0, 0, 0, 0)
        with self.assertRaisesRegex(ValueError, "one distinct Crux overlay"):
            package_fits.extract_crux_overlay(table + first + second, recipe)

    def test_native_recipe_uses_dualboot_development_profile_and_both_ramdisks(self):
        calls = []
        original_run = subprocess.run
        def run(command, **kwargs):
            if command in (["mkimage", "-V"], ["fdtoverlay", "--version"]):
                return subprocess.CompletedProcess(command, 0, stdout="fixture host tool\n")
            if len(command) > 1 and command[1] == str(package_fits.RECIPE):
                calls.append(command)
                directory = Path(command[command.index("--output-dir") + 1])
                directory.mkdir()
                for mode in ("rom", "recovery"):
                    (directory / (mode + "-cepheus.itb")).write_bytes(b"mock validated FIT")
                (directory / "fit-validation.json").write_text(json.dumps({"fits": {mode: {"file_size": len(b"mock validated FIT"),
                    "loads": {"fdt": 0x84800000, "ramdisk": 0x83000000}} for mode in ("rom", "recovery")}}))
                return subprocess.CompletedProcess(command, 0)
            return original_run(command, **kwargs)
        with patch("package_fits.subprocess.run", run):
            result = self.prepare()
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertTrue(result["fits_generated"])
        self.assertIn("--dualboot", calls[0]); self.assertIn("--development-adb", calls[0])
        self.assertIn("--boot-ramdisk", calls[0]); self.assertIn("--recovery-ramdisk", calls[0])
        self.assertFalse(result["physical_recovery_container_generated"])
        self.assertIn('../fit-inputs/Image', (self.output / "fits/rom-cepheus.its").read_text())

    def test_missing_fit_output_cannot_claim_success_from_report_alone(self):
        original_run = subprocess.run
        def run(command, **kwargs):
            if command in (["mkimage", "-V"], ["fdtoverlay", "--version"]):
                return subprocess.CompletedProcess(command, 0, stdout="fixture host tool\n")
            if len(command) > 1 and command[1] == str(package_fits.RECIPE):
                directory = Path(command[command.index("--output-dir") + 1])
                directory.mkdir()
                (directory / "fit-validation.json").write_text(json.dumps({"fits": {"rom": {"file_size": 10}, "recovery": {"file_size": 10}}}))
                return subprocess.CompletedProcess(command, 0)
            return original_run(command, **kwargs)
        with patch("package_fits.subprocess.run", run):
            result = self.prepare()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["fits_generated"])
        self.assertIn("did not produce", result["error"])

    def test_native_recovery_v1_ramdisk_is_derived_from_fresh_image(self):
        native_ramdisk = self.product / "ramdisk-recovery.img"
        payload = native_ramdisk.read_bytes()
        del self.accepted[str(native_ramdisk)]
        self.receipts[0]["outputs"] = [spec for spec in self.receipts[0]["outputs"] if spec["path"] != str(native_ramdisk)]
        native_ramdisk.write_bytes(b"unreceipted obsolete ramdisk")
        header = bytearray(4096)
        header[:8] = b"ANDROID!"
        struct.pack_into("<I", header, 8, 64)
        struct.pack_into("<I", header, 16, len(payload))
        struct.pack_into("<2I", header, 36, 4096, 1)
        struct.pack_into("<I", header, 1644, 1648)
        recovery = self.product / "recovery.img"
        recovery.write_bytes(header + self.image.read_bytes() + bytes(4096 - 64) + payload)
        spec = worker.describe(recovery)
        self.accepted[str(recovery)] = spec
        self.receipts[0]["outputs"].append(spec)
        result = self.prepare(build_fits=False)
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertEqual((self.output / "fit-inputs/ramdisk-recovery.img").read_bytes(), payload)
        manifest = json.loads((self.output / "fit-inputs/packaging-manifest.json").read_text())
        self.assertTrue(manifest["ramdisk_sources"]["recovery"]["extracted_from_android_image"])
        self.assertEqual(manifest["ramdisk_sources"]["recovery"]["header_version"], 1)
        self.assertEqual(manifest["ramdisk_sources"]["recovery"]["offset"], 8192)

    def test_truncated_native_recovery_ramdisk_is_rejected(self):
        image = bytearray(8192)
        image[:8] = b"ANDROID!"
        struct.pack_into("<I", image, 8, 64)
        struct.pack_into("<I", image, 16, 1000)
        struct.pack_into("<2I", image, 36, 4096, 1)
        struct.pack_into("<I", image, 1644, 1648)
        with self.assertRaisesRegex(ValueError, "outside its image"):
            package_fits.extract_android_ramdisk(image)


if __name__ == "__main__":
    unittest.main()
