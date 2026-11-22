import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import verify_properties as properties


def expected_identity():
    return {
        "schema_version": 1,
        "required_buildinfo": {
            "ro.build.version.incremental": "1791434921",
            "ro.build.description": "aosp_crux-userdebug 13 TQ3A.230901.001.B1 1791434921 release-keys",
            "ro.build.id": "TQ3A.230901.001.B1",
            "ro.build.date.utc": "1791434831",
            "ro.build.date": "Thu Oct  8 04:47:11 UTC 2026",
            "ro.build.tags": "release-keys",
            "ro.build.type": "userdebug",
            "ro.build.flavor": "aosp_crux-userdebug",
            "org.pixelexperience.device": "crux",
        },
        "required_final_keys": {
            "ro.build.version.incremental": "1791434921",
            "ro.build.fingerprint": "Xiaomi/crux/crux:13/TQ3A.230901.001.B1/0448:userdebug/test-keys",
            "ro.system.build.version.incremental": "1791434921",
            "ro.system.build.fingerprint": "Xiaomi/crux/crux:13/TQ3A.230901.001.B1/0448:userdebug/test-keys",
            "ro.system.build.date.utc": "1791434831",
        },
        "branding_keys": {
            "org.pixelexperience.build_date": "20261008-0448",
            "org.pixelexperience.build_date_utc": "1791434880",
        },
        "frontend_thumbprint": "13/TQ3A.230901.001.B1/1791434921:userdebug/release-keys\n",
    }


def encoded(values):
    return ("# native property fixture\n" + "\n".join(key + "=" + value for key, value in values.items()) + "\n").encode()


def native_partition(partition):
    return {
        "ro." + partition + ".build.version.incremental": "1791434921",
        "ro." + partition + ".build.fingerprint": "Xiaomi/crux/crux:13/TQ3A.230901.001.B1/0448:userdebug/test-keys",
        "ro." + partition + ".build.date.utc": "1791434831",
        "ro." + partition + ".build.date": "Thu Oct  8 04:47:11 UTC 2026",
    }


def system_values():
    expected = expected_identity()
    return {**expected["required_buildinfo"], **expected["required_final_keys"],
            **expected["branding_keys"], **native_partition("system")}


class PropertyByteTests(unittest.TestCase):
    def validate(self, system=None, vendor=None, buildinfo=None, thumbprint=None, extra=()):
        expected = expected_identity()
        members = [("system", "system.img:/system/build.prop", system if system is not None else encoded(system_values())),
                   ("vendor", "vendor.img:/build.prop", vendor if vendor is not None else encoded(native_partition("vendor"))), *extra]
        return properties.validate_property_bytes(
            expected, buildinfo if buildinfo is not None else encoded(expected["required_buildinfo"]),
            members, thumbprint if thumbprint is not None else expected["frontend_thumbprint"].encode())

    def test_real_assignments_preserve_duplicates_and_equals_in_values(self):
        source = properties.property_source("member", b"# note\n key=value=tail\nkey=other\n")
        self.assertEqual([(x["key"], x["value"], x["line"]) for x in source["assignments"]],
                         [("key", "value=tail", 2), ("key", "other", 3)])

    def test_native_identity_passes_with_distinct_generic_and_pe_clocks(self):
        result = self.validate()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["native_partitions_checked"], ["system", "vendor"])

    def test_blank_incremental_in_actual_buildinfo_fails(self):
        values = dict(expected_identity()["required_buildinfo"], **{"ro.build.version.incremental": ""})
        result = self.validate(buildinfo=encoded(values))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(x["reason"] == "blank required identity" for x in result["errors"]))

    def test_empty_final_property_bytes_fail(self):
        self.assertEqual(self.validate(system=b"")["status"], "failed")

    def test_conflicting_expected_maps_cannot_override_required_identity(self):
        expected = expected_identity()
        expected["branding_keys"]["ro.build.version.incremental"] = "wrong"
        with self.assertRaisesRegex(properties.PropertyError, "Conflicting admitted values"):
            properties.validate_property_bytes(expected, encoded(expected["required_buildinfo"]),
                                               [("system", "member", encoded(system_values()))],
                                               expected["frontend_thumbprint"].encode())

    def test_old_double_gap_description_in_final_bytes_fails_despite_good_buildinfo(self):
        values = dict(system_values())
        values["ro.build.description"] = "aosp_crux-userdebug 13 TQ3A.230901.001.B1  release-keys"
        result = self.validate(system=encoded(values))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(x.get("key") == "ro.build.description" for x in result["errors"]))

    def test_contradictory_duplicate_fails_even_when_last_assignment_is_correct(self):
        data = encoded(system_values()) + b"ro.build.version.incremental=\nro.build.version.incremental=1791434921\n"
        result = self.validate(system=data)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(x["reason"] == "contradictory required duplicate" for x in result["errors"]))

    def test_duplicate_from_another_property_member_cannot_hide_behind_good_last_value(self):
        result = self.validate(extra=(("system", "system.img:/system/etc/build.prop",
                                       b"ro.build.fingerprint=old/wrong/fingerprint\n"),))
        self.assertEqual(result["status"], "failed")

    def test_identical_required_duplicates_are_valid(self):
        result = self.validate(system=encoded(system_values()) + b"ro.build.version.incremental=1791434921\n")
        self.assertEqual(result["status"], "success")

    def test_optional_partition_is_checked_only_when_its_native_member_exists(self):
        self.assertEqual(self.validate()["status"], "success")
        good = encoded(native_partition("system_ext"))
        self.assertEqual(self.validate(extra=(("system_ext", "system.img:/system_ext/build.prop", good),))["status"],
                         "success")
        wrong = dict(native_partition("system_ext"), **{"ro.system_ext.build.date.utc": "1791434880"})
        self.assertEqual(self.validate(extra=(("system_ext", "system.img:/system_ext/build.prop", encoded(wrong)),))["status"],
                         "failed")

    def test_declared_absent_partition_namespace_must_be_absent(self):
        expected = expected_identity()
        expected["absent_partition_namespaces"] = {
            "product": {"reason": "no product partition; the frozen rules skip the common /product properties"}}
        product = encoded({"ro.product.name": "aosp_crux"})
        base = [("system", "system.img:/system/build.prop", encoded(system_values())),
                ("vendor", "vendor.img:/build.prop", encoded(native_partition("vendor")))]
        members = [*base, ("product", "system.img:/system/product/etc/build.prop", product)]
        result = properties.validate_property_bytes(expected, encoded(expected["required_buildinfo"]), members,
                                                    expected["frontend_thumbprint"].encode())
        self.assertEqual(result["status"], "success")
        present = encoded({"ro.product.build.version.incremental": "1791434921"})
        result = properties.validate_property_bytes(
            expected, encoded(expected["required_buildinfo"]),
            [*base, ("product", "system.img:/system/product/etc/build.prop", present)],
            expected["frontend_thumbprint"].encode())
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("declared-absent" in entry["scope"] for entry in result["errors"]))
        plain = expected_identity()
        result = properties.validate_property_bytes(plain, encoded(plain["required_buildinfo"]), members,
                                                    plain["frontend_thumbprint"].encode())
        self.assertEqual(result["status"], "failed")

    def test_unreasoned_absent_partition_namespace_is_rejected(self):
        expected = expected_identity()
        expected["absent_partition_namespaces"] = {"product": {}}
        with self.assertRaisesRegex(properties.PropertyError, "declared-absent namespace"):
            properties.validate_property_bytes(expected, encoded(expected["required_buildinfo"]),
                                               [("system", "member", encoded(system_values()))],
                                               expected["frontend_thumbprint"].encode())

    def test_absent_partition_namespace_outside_native_partitions_is_rejected(self):
        expected = expected_identity()
        expected["absent_partition_namespaces"] = {"cache": {"reason": "not a property partition"}}
        with self.assertRaisesRegex(properties.PropertyError, "declared-absent namespace"):
            properties.validate_property_bytes(expected, encoded(expected["required_buildinfo"]),
                                               [("system", "member", encoded(system_values()))],
                                               expected["frontend_thumbprint"].encode())

    def test_emitted_thumbprint_is_conditional_but_exact(self):
        self.assertEqual(self.validate()["status"], "success")
        correct = expected_identity()["frontend_thumbprint"].rstrip("\n")
        self.assertEqual(self.validate(system=encoded({**system_values(), "ro.build.thumbprint": correct}))["status"], "success")
        self.assertEqual(self.validate(system=encoded({**system_values(), "ro.build.thumbprint": "wrong"}))["status"], "failed")

    def test_frontend_thumbprint_exact_bytes_include_native_newline(self):
        self.assertEqual(self.validate(thumbprint=expected_identity()["frontend_thumbprint"].rstrip("\n").encode())["status"],
                         "failed")

    def test_pe_clock_cannot_be_replaced_with_generic_date(self):
        values = dict(system_values(), **{"org.pixelexperience.build_date_utc": "1791434831"})
        self.assertEqual(self.validate(system=encoded(values))["status"], "failed")

    def test_inactive_build_utc_date_zero_is_not_an_accepted_property_value(self):
        values = dict(system_values(), **{"ro.system.build.date.utc": "0"})
        self.assertEqual(self.validate(system=encoded(values))["status"], "failed")

    def test_non_utf8_properties_are_not_treated_as_verified_text(self):
        with self.assertRaisesRegex(properties.PropertyError, "UTF-8"):
            self.validate(system=b"\xff")

    def test_missing_native_partition_identity_fails(self):
        self.assertEqual(self.validate(vendor=b"ro.vendor.other=value\n")["status"], "failed")

    def test_vendor_native_members_cannot_borrow_identity_from_system_members(self):
        system = encoded({**system_values(), **native_partition("vendor")})
        result = self.validate(system=system, vendor=b"ro.vendor.other=value\n")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(x["scope"] == "partition vendor native members" and x["reason"] == "missing required property"
                            for x in result["errors"]))

    def test_matching_vendor_identity_in_own_native_members_passes(self):
        system = encoded({**system_values(), **native_partition("vendor")})
        result = self.validate(system=system, vendor=encoded(native_partition("vendor")))
        self.assertEqual(result["status"], "success")


MKFS = shutil.which("mke2fs")
DEBUGFS = shutil.which("debugfs")
SIMG2IMG = os.environ.get("PROPERTY_TEST_SIMG2IMG") or shutil.which("simg2img")
IMG2SIMG = os.environ.get("PROPERTY_TEST_IMG2SIMG") or shutil.which("img2simg")


@unittest.skipUnless(MKFS and DEBUGFS, "native mke2fs and debugfs are unavailable for real ext4 image inspection")
class Ext4PropertyImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="crux-properties-test-")
        self.root = Path(self.temporary.name)
        self.expected = expected_identity()
        self.buildinfo = self.root / "buildinfo.prop"
        self.buildinfo.write_bytes(encoded(self.expected["required_buildinfo"]))
        self.thumbprint = self.root / "build_thumbprint.txt"
        self.thumbprint.write_text(self.expected["frontend_thumbprint"])

    def tearDown(self):
        self.temporary.cleanup()

    def image(self, partition, with_properties=True, optional_members=()):
        files = self.root / (partition + "-files")
        files.mkdir()
        if with_properties:
            if partition == "system":
                destination = files / "system/build.prop"
                data = encoded(system_values())
            else:
                destination = files / "build.prop"
                data = encoded(native_partition(partition))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        for optional in optional_members:
            destination = files / optional / "etc/build.prop"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(encoded(native_partition(optional)))
        image = self.root / (partition + ".img")
        with image.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024)
        subprocess.run([MKFS, "-q", "-F", "-t", "ext4", "-d", str(files), str(image)], check=True,
                       capture_output=True)
        return image

    def test_actual_raw_ext4_member_bytes_are_extracted_and_validated(self):
        system, vendor = self.image("system"), self.image("vendor")
        result = properties.verify_images(self.expected, self.buildinfo, self.thumbprint,
                                          {"system": system, "vendor": vendor}, DEBUGFS)
        self.assertEqual(result["status"], "success")
        self.assertEqual([x["format"] for x in result["image_inspection"]], ["raw", "raw"])
        self.assertEqual([x["member"] for x in result["image_inspection"][0]["members"]], ["/system/build.prop"])

    def test_debugfs_zero_exit_for_missing_member_does_not_pass(self):
        system, vendor = self.image("system"), self.image("vendor", with_properties=False)
        native = subprocess.run([DEBUGFS, "-R", "stat /build.prop", str(vendor)], capture_output=True, text=True)
        self.assertEqual(native.returncode, 0)
        self.assertIn("not found", native.stderr.lower())
        with self.assertRaisesRegex(properties.PropertyError, "No regular native property file"):
            properties.verify_images(self.expected, self.buildinfo, self.thumbprint,
                                     {"system": system, "vendor": vendor}, DEBUGFS)

    def test_actual_vendor_container_dlkm_property_members_are_checked_when_present(self):
        system = self.image("system")
        vendor = self.image("vendor", optional_members=("vendor_dlkm", "odm_dlkm"))
        result = properties.verify_images(self.expected, self.buildinfo, self.thumbprint,
                                          {"system": system, "vendor": vendor}, DEBUGFS)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["native_partitions_checked"], ["odm_dlkm", "system", "vendor", "vendor_dlkm"])
        self.assertEqual({x["member"] for x in result["image_inspection"][1]["members"]},
                         {"/build.prop", "/vendor_dlkm/etc/build.prop", "/odm_dlkm/etc/build.prop"})

    @unittest.skipUnless(SIMG2IMG and IMG2SIMG, "native img2simg/simg2img are unavailable for a real sparse fixture")
    def test_actual_sparse_ext4_images_use_native_converter_and_extracted_bytes(self):
        images = {}
        for partition in ("system", "vendor"):
            raw = self.image(partition)
            sparse = self.root / (partition + "-sparse.img")
            subprocess.run([IMG2SIMG, str(raw), str(sparse)], check=True, capture_output=True)
            with sparse.open("rb") as stream:
                self.assertEqual(stream.read(4), properties.SPARSE_MAGIC)
            images[partition] = sparse
        result = properties.verify_images(self.expected, self.buildinfo, self.thumbprint, images, DEBUGFS, SIMG2IMG)
        self.assertEqual(result["status"], "success")
        self.assertEqual([x["format"] for x in result["image_inspection"]], ["android-sparse", "android-sparse"])


class PropertyCliTests(unittest.TestCase):
    def test_missing_inspection_tool_records_blocker_and_never_fakes_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = root / "expected.json"
            expected.write_text(json.dumps(expected_identity()))
            buildinfo, thumbprint, image = root / "buildinfo.prop", root / "thumbprint", root / "image.img"
            buildinfo.write_bytes(encoded(expected_identity()["required_buildinfo"]))
            thumbprint.write_text(expected_identity()["frontend_thumbprint"])
            image.write_bytes(b"file-image-fixture")
            output = root / "report.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = properties.main(["--expected", str(expected), "--buildinfo", str(buildinfo),
                                        "--frontend-thumbprint", str(thumbprint), "--image", "system=" + str(image),
                                        "--image", "vendor=" + str(image), "--debugfs", str(root / "missing-debugfs"),
                                        "--output", str(output)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.read_text())["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
