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
import verify_properties
from test_properties import expected_identity, encoded, system_values, native_partition


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
        self.identity = self.root / "frontend-identity.json"
        self.identity.write_text(json.dumps(expected_identity()))
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
        with patch("assemble.verify_properties.verify_images", self.property_inspection_fixture):
            return assemble.assemble(self.producers, self.dependencies, self.output, self.selection,
                                     accepted_worker_commits=self.approved_workers, with_fits=False,
                                     expected_identity=self.identity)

    def property_inspection_fixture(self, expected, buildinfo, thumbprint, images, debugfs, simg2img):
        # Native extraction is exercised by the separate real-ext4 integration
        # tests. These archival tests isolate producer/receipt boundaries.
        return verify_properties.validate_property_bytes(expected, Path(buildinfo).read_bytes(),
            [("system", "fixture-system:/system/build.prop", encoded(system_values())),
             ("vendor", "fixture-vendor:/build.prop", encoded(native_partition("vendor")))], Path(thumbprint).read_bytes())

    def frontend_producer(self):
        if len(self.producers) > 2:
            return
        expected = expected_identity()
        descriptor = {"run_id": 103, "artifact": "shard-frontend-fixture", "expected_worker_commit": "a" * 40,
                      "expected_manifest_sha256": "2" * 64, "roles": ["frontend_identity"]}
        self.producers.append(descriptor)
        paths = {self.out_root / "soong/build_number.txt": b"1791434921",
                 self.product / "build_fingerprint.txt": (expected["required_final_keys"]["ro.build.fingerprint"] + "\n").encode(),
                 self.product / "build_thumbprint.txt": expected["frontend_thumbprint"].encode(),
                 self.product / "obj/PACKAGING/system_build_prop_intermediates/buildinfo.prop": encoded(expected["required_buildinfo"])}
        directory = self.dependencies / "2"; directory.mkdir(parents=True)
        specs = []
        for path, data in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data); specs.append(worker.describe(path))
        archive = directory / "outputs.tar.zst"; worker.pack_outputs(specs, archive)
        receipt = {"schema_version": 1, "status": "success", "id": "frontend-fixture", "run_id": "103",
                   "source_root": str(self.source), "out_root": str(self.out_root), "worker_commit": "a" * 40,
                   "manifest_sha256": "2" * 64, "archive_sha256": worker.digest(archive), "outputs": specs}
        (directory / "receipt.json").write_text(json.dumps(receipt))
        for path in paths: path.unlink()

    def prepare_images(self, second=("system.img", "vendor.img"), optional=()):
        self.producer(0, ("boot.img", "recovery.img", *optional))
        self.producer(1, second)
        self.frontend_producer()

    def contents(self):
        raw = subprocess.run(["zstd", "-dc", str(self.output / assemble.ARCHIVE_NAME)],
                             check=True, stdout=subprocess.PIPE).stdout
        return tarfile.open(fileobj=io.BytesIO(raw))

    def test_success_packages_four_image_basenames_and_source_provenance(self):
        self.prepare_images()
        result = self.assemble()
        self.assertEqual(result["status"], "success", result.get("error"))
        with self.contents() as package:
            self.assertEqual(set(package.getnames()), {*assemble.REQUIRED_IMAGES, "README.md", "build-manifest.json", "source-revisions.json", "property-verification.json"})
            self.assertEqual(package.extractfile("system.img").read(), b"built-system.img")
            self.assertEqual(package.getmember("system.img").mode, 0o640)
            manifest = json.load(package.extractfile("build-manifest.json"))
            self.assertFalse(manifest["full_ota"])
            self.assertFalse(manifest["validation"]["runtime_tested"])
            self.assertEqual(manifest["build_profile"], "aosp_crux-userdebug")
            self.assertEqual(manifest["source_selection"]["sources"]["device"], "selected-revision")
            self.assertEqual([p["run_id"] for p in manifest["producers"]], [101, 102, 103])
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
        self.assertEqual([p["worker_commit"] for p in manifest["producers"]], ["a" * 40, "b" * 40, "a" * 40])

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
        with patch("assemble.verify_properties.verify_images", self.property_inspection_fixture):
            result = assemble.assemble(self.producers, self.dependencies, self.output, self.selection,
                                       accepted_worker_commits=self.approved_workers, expected_identity=self.identity)
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
        self.frontend_producer()
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
        with patch("package_fits.subprocess.run", native_recipe), patch("assemble.verify_properties.verify_images", self.property_inspection_fixture):
            result = assemble.assemble(self.producers, self.dependencies, self.output, self.selection,
                                       accepted_worker_commits=self.approved_workers, expected_identity=self.identity)
        self.assertEqual(result["status"], "success", result.get("error"))
        with self.contents() as archive:
            self.assertTrue({*assemble.REQUIRED_IMAGES, "kernel-symbols.tar.zst", "fit-inputs.tar.zst", "uboot-fits.tar.zst"}.issubset(archive.getnames()))
            self.assertNotIn("uboot-stage", archive.getnames())
            manifest = json.load(archive.extractfile("build-manifest.json"))
            self.assertTrue(manifest["validation"]["matching_fresh_kernel_symbols"])
            self.assertTrue(manifest["validation"]["both_fits_strictly_validated"])
            self.assertFalse(manifest["boot_chain"]["device_container_packaging_verified"])

    def test_missing_frontend_role_blocks_success_and_retains_report(self):
        self.prepare_images()
        self.producers[2].pop("roles")
        result = self.assemble()
        self.assertEqual(result["status"], "failed")
        report = json.loads((self.output / "property-verification.json").read_text())
        self.assertIn("frontend_identity", report["errors"][0]["reason"])
        self.assertFalse((self.output / assemble.ARCHIVE_NAME).exists())

    def test_property_failure_never_publishes_success_archive_or_manifest(self):
        self.prepare_images()
        def bad_image(expected, buildinfo, thumbprint, images, debugfs, simg2img):
            bad_system = {**system_values(), "ro.build.version.incremental": ""}
            return verify_properties.validate_property_bytes(expected, Path(buildinfo).read_bytes(),
                [("system", "actual-fixture-member", encoded(bad_system)),
                 ("vendor", "actual-fixture-vendor", encoded(native_partition("vendor")))], Path(thumbprint).read_bytes())
        with patch("assemble.verify_properties.verify_images", bad_image):
            result = assemble.assemble(self.producers, self.dependencies, self.output, self.selection,
                                       accepted_worker_commits=self.approved_workers, with_fits=False,
                                       expected_identity=self.identity)
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.output / assemble.ARCHIVE_NAME).exists())
        self.assertFalse((self.output / "build-manifest.json").exists())
        report = json.loads((self.output / "property-verification.json").read_text())
        self.assertTrue(any(error.get("reason") == "blank required identity" for error in report["errors"]))


DEBUGFS = shutil.which("debugfs") or ("/opt/homebrew/opt/e2fsprogs/sbin/debugfs" if Path("/opt/homebrew/opt/e2fsprogs/sbin/debugfs").exists() else None)
MKE2FS = ("/opt/homebrew/opt/e2fsprogs/sbin/mke2fs" if Path("/opt/homebrew/opt/e2fsprogs/sbin/mke2fs").exists() else shutil.which("mke2fs"))


@unittest.skipUnless(DEBUGFS and MKE2FS, "native ext4 file-inspection tools unavailable")
class RealPropertyAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = AssembleTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def image(self, partition, values):
        fixture = self.fixture
        files = fixture.root / (partition + "-files")
        destination = files / ("system/build.prop" if partition == "system" else "build.prop")
        destination.parent.mkdir(parents=True)
        destination.write_bytes(encoded(values))
        image = fixture.product / (partition + ".img")
        with image.open("wb") as output: output.truncate(16 * 1024 * 1024)
        subprocess.run([MKE2FS, "-q", "-F", "-t", "ext4", "-d", str(files), str(image)],
                       check=True, capture_output=True)
        return image

    def prepare(self, system=None, vendor=None):
        fixture = self.fixture
        fixture.producer(0, ("boot.img", "recovery.img"))
        images = [self.image("system", system_values() if system is None else system),
                  self.image("vendor", native_partition("vendor") if vendor is None else vendor)]
        directory = fixture.dependencies / "1"; directory.mkdir(parents=True)
        specs = [worker.describe(path) for path in images]
        archive = directory / "outputs.tar.zst"; worker.pack_outputs(specs, archive)
        receipt = {"schema_version": 1, "status": "success", "id": "images-b", "run_id": "102",
                   "source_root": str(fixture.source), "out_root": str(fixture.out_root), "worker_commit": "a" * 40,
                   "manifest_sha256": "1" * 64, "archive_sha256": worker.digest(archive), "outputs": specs}
        (directory / "receipt.json").write_text(json.dumps(receipt))
        for image in images: image.unlink()
        fixture.frontend_producer()

    def assemble(self):
        fixture = self.fixture
        return assemble.assemble(fixture.producers, fixture.dependencies, fixture.output, fixture.selection,
                                 accepted_worker_commits=fixture.approved_workers, with_fits=False,
                                 expected_identity=fixture.identity, debugfs=DEBUGFS)

    def test_real_ext4_properties_and_actual_frontend_members_admit_archive(self):
        self.prepare()
        result = self.assemble()
        self.assertEqual(result["status"], "success", result.get("error"))
        report = json.loads((self.fixture.output / "property-verification.json").read_text())
        self.assertEqual(report["status"], "success")
        self.assertEqual([item["format"] for item in report["image_inspection"]], ["raw", "raw"])
        self.assertEqual(report["frontend_producer"]["id"], "frontend-fixture")
        self.assertEqual(set(report["frontend_producer"]["members"]), {"build_number", "fingerprint", "thumbprint", "buildinfo"})
        manifest = json.loads((self.fixture.output / "build-manifest.json").read_text())
        self.assertTrue(manifest["validation"]["actual_final_image_properties_verified"])

    def test_real_final_image_with_blank_incremental_blocks_archive(self):
        self.prepare(system={**system_values(), "ro.build.version.incremental": ""})
        result = self.assemble()
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.fixture.output / assemble.ARCHIVE_NAME).exists())
        report = json.loads((self.fixture.output / "property-verification.json").read_text())
        self.assertTrue(any(error.get("reason") == "blank required identity" for error in report["errors"]))

    def test_real_vendor_cannot_borrow_partition_identity_from_system(self):
        self.prepare(system={**system_values(), **native_partition("vendor")}, vendor={"ro.vendor.other": "value"})
        result = self.assemble()
        self.assertEqual(result["status"], "failed")
        report = json.loads((self.fixture.output / "property-verification.json").read_text())
        self.assertTrue(any(error.get("scope") == "partition vendor native members" for error in report["errors"]))

    def test_missing_actual_frontend_member_blocks_before_inspecting_images(self):
        self.prepare()
        fixture = self.fixture
        path = fixture.dependencies / "2/receipt.json"
        receipt = json.loads(path.read_text())
        missing = str(fixture.product / "build_thumbprint.txt")
        receipt["outputs"] = [spec for spec in receipt["outputs"] if spec["path"] != missing]
        # Rebuild a complete, internally valid archive without that member.
        fixture.product.mkdir(parents=True, exist_ok=True)
        for spec in receipt["outputs"]:
            original = (b"1791434921" if spec["path"].endswith("build_number.txt") else
                        (expected_identity()["required_final_keys"]["ro.build.fingerprint"] + "\n").encode() if spec["path"].endswith("build_fingerprint.txt") else
                        encoded(expected_identity()["required_buildinfo"]))
            member = Path(spec["path"]); member.parent.mkdir(parents=True, exist_ok=True); member.write_bytes(original); member.chmod(spec["mode"])
        archive = fixture.dependencies / "2/outputs.tar.zst"; worker.pack_outputs(receipt["outputs"], archive)
        receipt["archive_sha256"] = worker.digest(archive); path.write_text(json.dumps(receipt))
        for spec in receipt["outputs"]: Path(spec["path"]).unlink()
        result = self.assemble()
        self.assertEqual(result["status"], "failed")
        report = json.loads((fixture.output / "property-verification.json").read_text())
        self.assertIn("fresh regular member", report["errors"][0]["reason"])
        self.assertFalse((fixture.output / assemble.ARCHIVE_NAME).exists())


if __name__ == "__main__":
    unittest.main()
