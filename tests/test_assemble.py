import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import assemble
import worker


@unittest.skipUnless(shutil.which("zstd"), "zstd is required for image archives")
class AssembleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "pe13"
        self.product = self.source / "out/target/product/crux"
        self.product.mkdir(parents=True)
        self.dependencies = self.root / "dependencies"
        self.output = self.root / "result"
        self.selection = self.root / "source-revisions.json"
        self.selection.write_text(json.dumps({"record_date": "2026-10-08 Asia/Shanghai", "sources": {"device": "selected-revision"}}))
        self.producers = [{"run_id": 101, "artifact": "shard-images-a"},
                          {"run_id": 102, "artifact": "shard-images-b"}]

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
                   "manifest_sha256": str(index) * 64, "archive_sha256": worker.digest(archive),
                   "outputs": specs, "build_environment": {"CCACHE_DISABLE": "1"}}
        (directory / "receipt.json").write_text(json.dumps(receipt))
        for name in names:
            (self.product / name).unlink()

    def assemble(self):
        return assemble.assemble(self.producers, self.dependencies, self.output, self.selection,
                                 self.product, self.source)

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


if __name__ == "__main__":
    unittest.main()
