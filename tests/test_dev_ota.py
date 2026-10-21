import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import dev_ota


def sums_for(record):
    values = {name: identity["sha256"] for name, identity in record["delivery"].items()}
    values["delivery-record.json"] = "c" * 64
    return values


def record_for(fingerprint="Xiaomi/crux/crux:13/TQ3A.230901.001.B1/2026101105:userdebug/test-keys"):
    return {
        "schema_version": 1,
        "status": "success",
        "product": "aosp_crux-userdebug",
        "build_number": "2026101105",
        "source_selection": {"env": {"BUILD_FINGERPRINT": fingerprint}},
        "delivery": {
            "boot.img": {"size": 10, "sha256": "a" * 64},
            "system.img.zst": {"size": 20, "sha256": "b" * 64},
        },
        "fit_validation": {"release_ready": False, "development_adb": True},
        "gates": {"images": {"boot": {"failures": []}, "recovery": {"failures": []}}},
        "system_image": {"compressed_as": "system.img.zst",
                         "compressed": {"sha256": "b" * 64}},
    }


def release_for(record):
    return {
        "tag": "dev-pe13-2026101105-v3",
        "build": "2026101105",
        "version": 3,
        "published_at": "2026-10-21T20:25:31Z",
        "assets": [
            {"name": name, "size": identity["size"],
             "digest": "sha256:" + identity["sha256"]}
            for name, identity in record["delivery"].items()
        ],
    }


class DevOtaTests(unittest.TestCase):
    def test_parse_tag(self):
        self.assertEqual(dev_ota.parse_tag("dev-pe13-2026101105-v3"),
                         {"build": "2026101105", "version": 3})
        self.assertIsNone(dev_ota.parse_tag("dev-pe13-2026101105"))
        self.assertIsNone(dev_ota.parse_tag("r6-2026101104-hosted"))

    def test_select_head_orders_by_build_then_version(self):
        releases = [
            {"tag": "dev-pe13-2026101104-v9", "build": "2026101104", "version": 9,
             "published_at": "2026-10-22T00:00:00Z"},
            {"tag": "dev-pe13-2026101105-v3", "build": "2026101105", "version": 3,
             "published_at": "2026-10-21T00:00:00Z"},
            {"tag": "dev-pe13-2026101105-v1", "build": "2026101105", "version": 1,
             "published_at": "2026-10-20T00:00:00Z"},
        ]
        self.assertEqual(dev_ota.select_head(releases)["tag"], "dev-pe13-2026101105-v3")
        self.assertEqual(dev_ota.select_head(releases, "dev-pe13-2026101104-v9")["build"],
                         "2026101104")

    def test_parse_sums(self):
        text = "a" * 64 + "  boot.img\n" + "b" * 64 + "  system.img.zst\n"
        self.assertEqual(dev_ota.parse_sums(text),
                         {"boot.img": "a" * 64, "system.img.zst": "b" * 64})
        with self.assertRaises(dev_ota.DevOtaError):
            dev_ota.parse_sums("not-a-digest  boot.img\n")

    def test_validate_record_accepts_dev_record(self):
        record = record_for()
        release = release_for(record)
        dev_ota.validate_record(release, record, sums_for(record))

    def test_validate_record_rejects_release_identity(self):
        record = record_for("Xiaomi/crux/crux:13/TQ3A.230901.001.B1/2026101105:user/release-keys")
        with self.assertRaises(dev_ota.DevOtaError):
            dev_ota.validate_record(release_for(record), record, sums_for(record))

    def test_validate_record_rejects_release_ready(self):
        record = record_for()
        record["fit_validation"]["release_ready"] = True
        with self.assertRaises(dev_ota.DevOtaError):
            dev_ota.validate_record(release_for(record), record, sums_for(record))

    def test_validate_record_rejects_bad_sum(self):
        record = record_for()
        sums = sums_for(record)
        sums["boot.img"] = "d" * 64
        with self.assertRaises(dev_ota.DevOtaError):
            dev_ota.validate_record(release_for(record), record, sums)


if __name__ == "__main__":
    unittest.main()
