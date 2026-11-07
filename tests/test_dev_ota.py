import json
import contextlib
import io
import sys
import tempfile
import unittest
from unittest import mock
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
            "recovery.img": {"size": 11, "sha256": "c" * 64},
            "rom-cepheus.itb": {"size": 12, "sha256": "d" * 64},
            "recovery-cepheus.itb": {"size": 13, "sha256": "e" * 64},
            "system.img.zst": {"size": 20, "sha256": "b" * 64},
            "vendor.img": {"size": 14, "sha256": "f" * 64},
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
        "draft": False,
        "prerelease": True,
        "assets": [
            {"name": name, "size": identity["size"],
             "digest": "sha256:" + identity["sha256"]}
            for name, identity in record["delivery"].items()
        ],
    }


class DevOtaTests(unittest.TestCase):
    def test_workspace_resolution_accepts_explicit_override_and_maintained_layout(self):
        self.assertEqual(dev_ota.workspace_root(Path("/crux/crux-pe13-distributed")), Path("/crux"))
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(dev_ota.workspace_root(Path("/checkout"), directory), Path(directory).resolve())

    @staticmethod
    def release_payload(tag="dev-pe13-2026101105-v3", **overrides):
        names = (*dev_ota.DELIVERY_NAMES, "extra-not-used.bin")
        payload = {
            "tag_name": tag, "draft": False, "prerelease": True,
            "published_at": "2026-10-21T20:25:31Z",
            "assets": [{"name": name, "size": 100, "state": "uploaded"} for name in names],
        }
        payload.update(overrides)
        return payload

    def discover_payloads(self, *pages):
        with mock.patch.object(dev_ota, "gh_json", return_value=list(pages)) as gh:
            result = dev_ota.discover("owner/repo")
        gh.assert_called_once_with("api", "--paginate", "--slurp",
                                   "repos/owner/repo/releases?per_page=100")
        return result

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

    def test_discover_requires_published_non_draft_complete_release_and_orders_numerically(self):
        draft = self.release_payload("dev-pe13-2026101105-v99", draft=True)
        starter = self.release_payload("dev-pe13-2026101106-v1",
                                       assets=self.release_payload()["assets"][:2])
        eligible_newer = self.release_payload("dev-pe13-2026101105-v10")
        eligible_older = self.release_payload("dev-pe13-2026101105-v9")
        releases = self.discover_payloads([draft, starter, eligible_older],
                                          [eligible_newer])
        self.assertEqual([item["tag"] for item in releases],
                         ["dev-pe13-2026101105-v9", "dev-pe13-2026101105-v10"])
        self.assertEqual(dev_ota.select_head(releases)["tag"],
                         "dev-pe13-2026101105-v10")

    def test_discover_rejects_zero_sized_required_asset(self):
        release = self.release_payload()
        release["assets"][dev_ota.DELIVERY_NAMES.index("vendor.img")]["size"] = 0
        self.assertEqual(self.discover_payloads([release]), [])

    def test_discover_rejects_unfinished_required_asset(self):
        release = self.release_payload()
        release["assets"][dev_ota.DELIVERY_NAMES.index("vendor.img")]["state"] = "starter"
        self.assertEqual(self.discover_payloads([release]), [])

    def test_load_record_checks_cached_metadata_against_release(self):
        record = record_for()
        release = release_for(record)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "delivery-record.json"
            manifest.write_text(json.dumps(record))
            sums = sums_for(record)
            sums[manifest.name] = dev_ota.sha256(manifest)
            checksums = root / "SHA256SUMS"
            checksums.write_text("".join(f"{digest}  {name}\n" for name, digest in sums.items()))
            for path in (manifest, checksums):
                release["assets"].append({"name": path.name, "size": path.stat().st_size,
                                          "digest": "sha256:" + dev_ota.sha256(path)})
            self.assertEqual(dev_ota.load_record(release, root), (record, sums))
            checksums.write_text(checksums.read_text().replace("\n", "\r"))
            with self.assertRaisesRegex(dev_ota.DevOtaError, "release digest differs for SHA256SUMS"):
                dev_ota.load_record(release, root)

    def test_check_no_device_skips_adb(self):
        release = {"tag": "dev-pe13-2026101105-v3", "build": "2026101105",
                   "version": 3, "published_at": "2026-10-21T20:25:31Z"}
        record = {"source_selection": {"env": {"BUILD_FINGERPRINT": "fingerprint"}}}
        args = type("Args", (), {"tag": None, "cache": Path("cache"),
                                  "serial": "serial", "no_device": True,
                                  "json": True})()
        with mock.patch.object(dev_ota, "discover", return_value=[release]), \
                mock.patch.object(dev_ota, "load_record", return_value=(record, {})), \
                mock.patch.object(dev_ota, "device_state") as device_state, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(dev_ota.check(args), 0)
        device_state.assert_not_called()
        self.assertIsNone(json.loads(output.getvalue())["device"])

    def test_plan_no_device_verifies_payload_and_skips_adb(self):
        record = record_for()
        release = release_for(record)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache, out = root / "isolated-cache", root / "plan"
            twrp = root / "twrp.itb"
            twrp.touch()
            delivery = cache / release["tag"]
            raw = {"system": {"path": "system.raw.img", "size": 4096, "sha256": "a" * 64},
                   "vendor": {"path": "vendor.raw.img", "size": 4096, "sha256": "b" * 64}}
            argv = ["dev_ota.py", "--cache", str(cache), "plan", "--no-device",
                    "--tag", release["tag"], "--out", str(out), "--twrp-fit", str(twrp)]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(dev_ota, "discover", return_value=[release]), \
                    mock.patch.object(dev_ota, "ensure_delivery",
                                      return_value=(delivery, record, {})) as prepare, \
                    mock.patch.object(dev_ota, "expand_images", return_value=raw), \
                    mock.patch.object(dev_ota, "pad_fits", return_value={}), \
                    mock.patch.object(dev_ota, "vold_digest", return_value="c" * 64), \
                    mock.patch.object(dev_ota.dev_ota_fits, "padded", return_value=b"fixture"), \
                    mock.patch.object(dev_ota, "device_state") as device_state, \
                    mock.patch.object(dev_ota, "print_plan"):
                self.assertEqual(dev_ota.main(), 0)
            prepare.assert_called_once_with(release, None, cache)
            device_state.assert_not_called()
            plan = json.loads((out / "plan.json").read_text())
            self.assertEqual(plan["delivery_dir"], str(delivery))
            self.assertEqual(plan["raw"], raw)
            self.assertIsNone(plan["current_device"])

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

    def test_validate_record_rejects_incomplete_delivery_asset_set(self):
        record = record_for()
        record["delivery"].pop("vendor.img")
        sums = sums_for(record)
        with self.assertRaises(dev_ota.DevOtaError):
            dev_ota.validate_record(release_for(record), record, sums)


if __name__ == "__main__":
    unittest.main()
