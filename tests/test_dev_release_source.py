import hashlib
from io import BytesIO
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import dev_ota
import dev_release_source as source


BUILD = "2026110303"
COMPILE_RUN = "42018017251"
ASSEMBLY_RUN = "42059779500"
TAG = "dev-pe13-2026110303-v1"
COMMIT = "a" * 40
WORKER = "b" * 40


def valid_summary():
    producers = []
    for wave in range(8):
        count = 5 if wave < 7 else 4
        for shard in range(count):
            ident = f"w{wave:03}-s{shard:03}"
            producers.append({"id": ident, "run_id": COMPILE_RUN,
                              "artifact": "shard-" + ident,
                              "expected_worker_commit": WORKER,
                              "expected_manifest_sha256": "c" * 64})
    return {"schema_version": 1, "status": "success", "task_count": 39, "execution_id": COMPILE_RUN,
            "worker_commit": WORKER,
            "fresh_build": {"execution_id": COMPILE_RUN, "pack_run": "1", "shard_run": "2",
                            "plan_sha256": "d" * 64, "source_manifest_sha256": "e" * 64},
            "producers": producers}


def make_artifact(missing=None, bad_sum=False, bad_source=False, altered_asset=False):
    delivery = {}
    contents = {}
    for name in sorted(dev_ota.DELIVERY_ASSET_NAMES):
        data = ("payload:" + name).encode()
        contents[name] = data
        delivery[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    producers = valid_summary()["producers"][:7]
    record = {"schema_version": 1, "status": "success", "build_number": BUILD,
              "product": dev_ota.PRODUCT,
              "delivery": delivery, "producers": producers,
              "source_selection": {"shard_run": "wrong" if bad_source else "2",
                                   "source_manifest_sha256": "0" * 64 if bad_source else "e" * 64,
                                   "build_number": BUILD, "target_product": "aosp_crux",
                                   "target_build_variant": "userdebug",
                                   "env": {"USE_CCACHE": "0", "CCACHE_DISABLE": "1",
                                           "BUILD_FINGERPRINT":
                                               f"Xiaomi/crux/crux:13/TQ3A.230901.001.B1/{BUILD}:userdebug/test-keys"}},
              "fit_validation": {"release_ready": False, "development_adb": True},
              "gates": {"images": {"boot": {"failures": []}, "recovery": {"failures": []}}},
              "system_image": {"compressed_as": "system.img.zst",
                               "compressed": {"sha256": delivery["system.img.zst"]["sha256"]}}}
    record_bytes = (json.dumps(record, indent=2) + "\n").encode()
    sums = {name: item["sha256"] for name, item in delivery.items()}
    sums["delivery-record.json"] = hashlib.sha256(record_bytes).hexdigest()
    if bad_sum:
        sums["boot.img"] = "0" * 64
    sums_bytes = "".join(f"{value}  {name}\n" for name, value in sorted(sums.items())).encode()
    receipt = {"schema_version": 1, "status": "success", "build_number": BUILD, "product": dev_ota.PRODUCT,
               "assets": {name: item["sha256"] for name, item in delivery.items()}}
    if altered_asset:
        contents["boot.img"] = b"x" * len(contents["boot.img"])
    result = BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_STORED) as archive:
        for name, data in contents.items():
            if name != missing:
                archive.writestr("delivery/" + name, data)
        archive.writestr("delivery/delivery-record.json", record_bytes)
        archive.writestr("delivery/SHA256SUMS", sums_bytes)
        archive.writestr("assembly-receipt.json", json.dumps(receipt))
    return result.getvalue()


class DevReleaseSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def invoke(self, *, mutate_summary=None, mutate_assembly=None, bad_zip_digest=False, **kwargs):
        summary = valid_summary()
        if mutate_summary:
            mutate_summary(summary)
        summary_zip = BytesIO()
        with zipfile.ZipFile(summary_zip, "w") as archive:
            archive.writestr("build-summary.json", json.dumps(summary))
        assembly_zip = make_artifact(**kwargs)
        run_values = {
            ASSEMBLY_RUN: {"id": int(ASSEMBLY_RUN), "conclusion": "success",
                           "status": "completed",
                           "event": "workflow_dispatch", "head_branch": "main",
                           "path": source.ASSEMBLY_WORKFLOW,
                           "display_title": "Verify fresh PE13 compilation " + COMPILE_RUN,
                           "head_sha": COMMIT},
            COMPILE_RUN: {"id": int(COMPILE_RUN), "conclusion": "success",
                          "status": "completed",
                          "event": "workflow_dispatch", "head_branch": "main",
                          "path": source.COMPILE_WORKFLOW,
                          "display_title": "Fresh PE13 compile " + BUILD,
                          "head_sha": WORKER},
        }
        if mutate_assembly:
            mutate_assembly(run_values[ASSEMBLY_RUN])

        def info(ident, name, data, wrong=False):
            digest = "f" * 64 if wrong else hashlib.sha256(data).hexdigest()
            return {name: {"id": ident, "name": name, "digest": "sha256:" + digest,
                           "size_in_bytes": len(data), "expired": False}}

        inventory = {
            ASSEMBLY_RUN: info(77, f"cloud-fresh-rom-{COMPILE_RUN}", assembly_zip, wrong=bad_zip_digest),
            COMPILE_RUN: info(66, "cloud-fresh-build-summary", summary_zip.getvalue()),
        }
        data_by_id = {"77": assembly_zip, "66": summary_zip.getvalue()}
        with mock.patch.dict("os.environ", {"GH_TOKEN": "test-token"}), \
                mock.patch.object(source, "_json_api",
                                  side_effect=lambda path, _: run_values[path.rsplit("/", 1)[1]]), \
                mock.patch.object(source.cloud_layers, "artifact_inventory",
                                  side_effect=lambda run, *_: inventory[run]), \
                mock.patch.object(source.cloud_layers, "_open_retry", side_effect=lambda request:
                                  BytesIO(data_by_id[request.full_url.split("/")[-2]])):
            return source.verify_source(ASSEMBLY_RUN, TAG, self.root)

    def test_verifies_delivery_and_returns_exact_asset_bindings(self):
        result = self.invoke()
        self.assertEqual(result.compile_run, COMPILE_RUN)
        self.assertEqual(result.assembly_commit, COMMIT)
        self.assertEqual(set(result.assets), set(dev_ota.DELIVERY_NAMES))
        self.assertEqual(set(path.name for path in result.directory.iterdir()), set(dev_ota.DELIVERY_NAMES))
        self.assertTrue(all(item["digest"].startswith("sha256:") for item in result.assets.values()))

    def test_rejects_wrong_workflow(self):
        with self.assertRaisesRegex(source.SourceError, "wrong workflow"):
            self.invoke(mutate_assembly=lambda run: run.update(path="wrong.yml"))

    def test_rejects_failed_assembly_even_with_artifact(self):
        with self.assertRaisesRegex(source.SourceError, "did not complete successfully"):
            self.invoke(mutate_assembly=lambda run: run.update(conclusion="failure"))

    def test_rejects_untrusted_zip_bytes(self):
        with self.assertRaisesRegex(source.SourceError, "ZIP digest differs"):
            self.invoke(bad_zip_digest=True)

    def test_rejects_wrong_producer_lineage(self):
        with self.assertRaises(source.SourceError):
            self.invoke(mutate_summary=lambda summary: summary["producers"][0].update(run_id="3"))

    def test_rejects_incomplete_delivery_archive(self):
        with self.assertRaises(source.SourceError):
            self.invoke(missing="vendor.img")

    def test_rejects_bad_asset_checksum(self):
        with self.assertRaisesRegex(dev_ota.DevOtaError, "asset digest differs from SHA256SUMS"):
            self.invoke(bad_sum=True)

    def test_rejects_asset_with_same_size_but_different_bytes(self):
        with self.assertRaisesRegex(dev_ota.DevOtaError, "asset digest differs from SHA256SUMS"):
            self.invoke(altered_asset=True)

    def test_rejects_source_selection_not_bound_to_fresh_compile(self):
        with self.assertRaises(source.SourceError):
            self.invoke(bad_source=True)

    def test_summary_requires_all_39_tasks_and_eight_waves(self):
        with self.assertRaisesRegex(source.SourceError, "39 producer bindings"):
            self.invoke(mutate_summary=lambda summary: summary["producers"].pop())


if __name__ == "__main__":
    unittest.main()
