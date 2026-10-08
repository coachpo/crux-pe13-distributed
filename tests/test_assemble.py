import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import struct
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import assemble
import worker
import package_fits


@unittest.skipUnless(shutil.which("zstd"), "zstd is required for image archives")
class AssembleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "pe13"
        self.out_root = self.root / "external-android-out"
        self.product = self.out_root / "target/product/crux"
        self.product.mkdir(parents=True)
        self.dependencies = self.root / "dependencies"
        self.output = self.root / "result"
        self.selection = self.root / "source-revisions.json"
        self.selection.write_text(json.dumps({"record_date": "2026-10-08 Asia/Shanghai", "sources": {"device": "selected-revision"}}))
        self.approved_workers = ["a" * 40]
        self.producers = [{"run_id": 101, "artifact": "shard-images-a", "expected_worker_commit": "a" * 40,
                           "expected_manifest_sha256": "0" * 64},
                          {"run_id": 102, "artifact": "shard-images-b", "expected_worker_commit": "a" * 40,
                           "expected_manifest_sha256": "1" * 64}]

    def tearDown(self):
        self.temporary.cleanup()

    def producer(self, index, names, content_prefix="built"):
        directory = self.dependencies / str(index)
        directory.mkdir(parents=True)
        specs = []
        for name in names:
            image = self.product / name
            image.write_bytes((content_prefix + "-" + name).encode())
            image.chmod(0o640)
            specs.append(worker.describe(image))
        archive = directory / "outputs.tar.zst"
        worker.pack_outputs(specs, archive)
        receipt = {"schema_version": 1, "status": "success", "id": "images-" + "ab"[index],
                   "run_id": str(self.producers[index]["run_id"]), "source_root": str(self.source),
                   "out_root": str(self.out_root), "worker_commit": self.producers[index]["expected_worker_commit"],
                   "manifest_sha256": str(index) * 64, "archive_sha256": worker.digest(archive),
                   "outputs": specs, "build_environment": {"CCACHE_DISABLE": "1"}}
        (directory / "receipt.json").write_text(json.dumps(receipt))
        for name in names:
            (self.product / name).unlink()

    def assemble(self):
        return assemble.assemble(self.producers, self.dependencies, self.output, self.selection,
                                 accepted_worker_commits=self.approved_workers, with_fits=False)

    def prepare_images(self, second=("system.img", "vendor.img"), optional=()):
        self.producer(0, ("boot.img", "recovery.img", *optional))
        self.producer(1, second)

    def contents(self):
        raw = subprocess.run(["zstd", "-dc", str(self.output / assemble.ARCHIVE_NAME)],
                             check=True, stdout=subprocess.PIPE).stdout
        return tarfile.open(fileobj=io.BytesIO(raw))

    def test_success_packages_four_image_basenames_and_source_provenance(self):
        self.prepare_images()
        result = self.assemble()
        self.assertEqual(result["status"], "success", result.get("error"))
        with self.contents() as package:
            self.assertEqual(set(package.getnames()), {*assemble.REQUIRED_IMAGES, "README.md", "build-manifest.json", "source-revisions.json"})
            self.assertEqual(package.extractfile("system.img").read(), b"built-system.img")
            self.assertEqual(package.getmember("system.img").mode, 0o640)
            manifest = json.load(package.extractfile("build-manifest.json"))
            self.assertFalse(manifest["full_ota"])
            self.assertFalse(manifest["validation"]["runtime_tested"])
            self.assertEqual(manifest["build_profile"], "aosp_crux-userdebug")
            self.assertEqual(manifest["source_selection"]["sources"]["device"], "selected-revision")
            self.assertEqual([p["run_id"] for p in manifest["producers"]], [101, 102])
            self.assertEqual(manifest["out_root"], str(self.out_root))
            self.assertEqual(manifest["product_dir"], str(self.product))
            self.assertIn("not a full OTA", package.extractfile("README.md").read().decode())

    def test_missing_image_fails_even_if_an_unreceipted_copy_exists(self):
        self.prepare_images(second=("system.img",))
        (self.product / "vendor.img").write_bytes(b"unverified-vendor")
        result = self.assemble()
        self.assertEqual(result["status"], "failed")
        self.assertIn("vendor.img", result["error"])
        self.assertFalse((self.output / assemble.ARCHIVE_NAME).exists())
        self.assertTrue((self.output / "assembly.log").exists())
        self.assertEqual(json.loads((self.output / "assembly-receipt.json").read_text())["status"], "failed")

    def test_failed_producer_cannot_supply_images(self):
        self.prepare_images()
        path = self.dependencies / "1/receipt.json"
        receipt = json.loads(path.read_text())
        receipt["status"] = "failed"
        path.write_text(json.dumps(receipt))
        result = self.assemble()
        self.assertEqual(result["status"], "failed")
        self.assertIn("unsuccessful producer", result["error"])
        self.assertFalse((self.output / assemble.ARCHIVE_NAME).exists())

    def test_corrupted_producer_archive_cannot_be_packaged(self):
        self.prepare_images()
        with (self.dependencies / "1/outputs.tar.zst").open("ab") as archive:
            archive.write(b"corruption")
        result = self.assemble()
        self.assertEqual(result["status"], "failed")
        self.assertIn("integrity mismatch", result["error"])
        self.assertFalse((self.output / assemble.ARCHIVE_NAME).exists())

    def test_optional_images_only_come_from_verified_receipts(self):
        self.prepare_images(optional=("kernel", "ramdisk.img", "dtb.img"))
        (self.product / "ramdisk-ambient.img").write_bytes(b"ambient")
        result = self.assemble()
        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertEqual(result["image_count"], 7)
        with self.contents() as package:
            self.assertTrue({"kernel", "ramdisk.img", "dtb.img"}.issubset(package.getnames()))
            self.assertNotIn("ramdisk-ambient.img", package.getnames())

    def test_conflicting_image_producers_fail_assembly(self):
        self.producer(0, ("boot.img", "recovery.img"), content_prefix="first-build")
        self.producer(1, ("boot.img", "system.img", "vendor.img"), content_prefix="different-build")
        result = self.assemble()
        self.assertEqual(result["status"], "failed")
        self.assertIn("conflicting dependency output", result["error"])
        self.assertFalse((self.output / assemble.ARCHIVE_NAME).exists())

    def test_exact_run_identity_is_checked(self):
        self.prepare_images()
        self.producers[1]["run_id"] = 999
        result = self.assemble()
        self.assertEqual(result["status"], "failed")
        self.assertIn("run and receipt IDs differ", result["error"])
        self.assertFalse((self.output / assemble.ARCHIVE_NAME).exists())

    def test_explicitly_approved_workers_can_be_mixed_with_exact_bindings(self):
        self.approved_workers.append("b" * 40)
        self.producers[1]["expected_worker_commit"] = "b" * 40
        self.prepare_images()
        result = self.assemble()
        self.assertEqual(result["status"], "success", result.get("error"))
        manifest = json.loads((self.output / "build-manifest.json").read_text())
        self.assertEqual([p["worker_commit"] for p in manifest["producers"]], ["a" * 40, "b" * 40])

    def test_manifest_binding_mismatch_fails_before_merge(self):
        self.prepare_images()
        self.producers[1]["expected_manifest_sha256"] = "f" * 64
        result = self.assemble()
        self.assertEqual(result["status"], "failed")
        self.assertIn("manifest differs", result["error"])
        self.assertFalse((self.product / "boot.img").exists())

    def test_unapproved_worker_cannot_enter_final_package(self):
        self.prepare_images()
        self.producers[1]["expected_worker_commit"] = "b" * 40
        result = self.assemble()
        self.assertEqual(result["status"], "failed")
        self.assertIn("no explicit approval", result["error"])

    def test_mixed_output_roots_are_rejected(self):
        self.prepare_images()
        receipt_path = self.dependencies / "1/receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["out_root"] = str(self.root / "other-output")
        receipt_path.write_text(json.dumps(receipt))
        result = self.assemble()
        self.assertEqual(result["status"], "failed")
        self.assertIn("share one source_root and out_root", result["error"])

    def test_strict_fit_failure_cannot_publish_image_archive(self):
        self.prepare_images()
        result = assemble.assemble(self.producers, self.dependencies, self.output, self.selection,
                                   accepted_worker_commits=self.approved_workers)
        self.assertEqual(result["status"], "failed")
        self.assertIn("strict U-Boot FIT packaging failed", result["error"])
        self.assertFalse((self.output / assemble.ARCHIVE_NAME).exists())
        self.assertTrue((self.output / "fit-packaging-receipt.json").exists())

    def test_final_package_integrates_external_out_symbols_and_derived_recovery_ramdisk(self):
        from test_package_fits import tree, ramdisk
        recipe = package_fits.load_recipe()
        kernel = self.product / "obj/KERNEL_OBJ"
        qcom = kernel / "arch/arm64/boot/dts/qcom"
        qcom.mkdir(parents=True)
        image = bytearray(64)
        struct.pack_into("<3Q", image, 8, 0x80000, 64, 0)
        image[56:60] = b"ARM\x64"
        (kernel / "arch/arm64/boot/Image").write_bytes(image)
        elf = bytearray(64); elf[:6] = b"\x7fELF\x02\x01"; struct.pack_into("<H", elf, 18, 183)
        (kernel / "vmlinux").write_bytes(elf)
        (kernel / "System.map").write_text("ffffff8008080000 T _text\n")
        (kernel / ".config").write_text("CONFIG_ARM64=y\n")
        for name in package_fits.BASES:
            (qcom / (name + ".dtb")).write_bytes(tree({"qcom,msm-id": struct.pack(">2I", *recipe.BASE_IDS[name])}))
        (qcom / "crux-sm8150-overlay.dtbo").write_bytes(tree({"model": b"SDX50M CRUX\0", "compatible": b"qcom,sm8150\0",
                                                             "qcom,board-id": struct.pack(">2I", 0x2B, 1)}))
        (self.product / "ramdisk.img").write_bytes(ramdisk("boot"))
        (self.product / "boot.img").write_bytes(b"compiled-boot-image")
        payload = ramdisk("recovery")
        header = bytearray(4096); header[:8] = b"ANDROID!"
        struct.pack_into("<I", header, 8, 64); struct.pack_into("<I", header, 16, len(payload))
        struct.pack_into("<2I", header, 36, 4096, 1); struct.pack_into("<I", header, 1644, 1648)
        (self.product / "recovery.img").write_bytes(header + image + bytes(4096 - 64) + payload)
        for name in ("system.img", "vendor.img"):
            (self.product / name).write_bytes(("compiled-" + name).encode())
        primary = [path for path in self.product.rglob("*") if path.is_file() and path.name not in ("recovery.img", "system.img", "vendor.img")]
        secondary = [self.product / name for name in ("recovery.img", "system.img", "vendor.img")]
        for index, paths in enumerate((primary, secondary)):
            directory = self.dependencies / str(index); directory.mkdir(parents=True)
            specs = [worker.describe(path) for path in paths]
            archive = directory / "outputs.tar.zst"; worker.pack_outputs(specs, archive)
            receipt = {"schema_version": 1, "status": "success", "id": "images-" + "ab"[index],
                       "run_id": str(self.producers[index]["run_id"]), "source_root": str(self.source),
                       "out_root": str(self.out_root), "worker_commit": "a" * 40,
                       "manifest_sha256": str(index) * 64, "archive_sha256": worker.digest(archive), "outputs": specs}
            (directory / "receipt.json").write_text(json.dumps(receipt))
            for path in paths: path.unlink()
        original_run = subprocess.run
        def native_recipe(command, **kwargs):
            if command in (["mkimage", "-V"], ["fdtoverlay", "--version"]):
                return subprocess.CompletedProcess(command, 0, stdout="fixture host tool\n")
            if len(command) > 1 and command[1] == str(package_fits.RECIPE):
                fits = Path(command[command.index("--output-dir") + 1]); fits.mkdir()
                for mode in ("rom", "recovery"):
                    (fits / (mode + "-cepheus.itb")).write_bytes(b"fixture validated FIT")
                    (fits / (mode + "-cepheus.dtb")).write_bytes(b"fixture final DT")
                (fits / "fit-validation.json").write_text(json.dumps({"fits": {mode: {"file_size": len(b"fixture validated FIT"),
                    "loads": {"fdt": 0x84800000, "ramdisk": 0x83000000}} for mode in ("rom", "recovery")}}))
                return subprocess.CompletedProcess(command, 0)
            return original_run(command, **kwargs)
        with patch("package_fits.subprocess.run", native_recipe):
            result = assemble.assemble(self.producers, self.dependencies, self.output, self.selection,
                                       accepted_worker_commits=self.approved_workers)
        self.assertEqual(result["status"], "success", result.get("error"))
        with self.contents() as archive:
            self.assertTrue({*assemble.REQUIRED_IMAGES, "kernel-symbols.tar.zst", "fit-inputs.tar.zst", "uboot-fits.tar.zst"}.issubset(archive.getnames()))
            self.assertNotIn("uboot-stage", archive.getnames())
            manifest = json.load(archive.extractfile("build-manifest.json"))
            self.assertTrue(manifest["validation"]["matching_fresh_kernel_symbols"])
            self.assertTrue(manifest["validation"]["both_fits_strictly_validated"])
            self.assertFalse(manifest["boot_chain"]["device_container_packaging_verified"])


if __name__ == "__main__":
    unittest.main()
