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
import cloud_assemble
import release_source as source


BUILD = "2026110303"
COMPILE_RUN = "42018017251"
ASSEMBLY_RUN = "42059779500"
TAG = "crux-pe13-2026110303-v1"
COMMIT = "a" * 40
WORKER = "b" * 40


def sha(data):
    return hashlib.sha256(data).hexdigest()


def valid_summary():
    producers = []
    for wave in range(9):
        count = 5 if wave < 4 else 4
        for shard in range(count):
            ident = f"w{wave:03}-s{shard:03}"
            producers.append({"id": ident, "run_id": COMPILE_RUN,
                              "artifact": "shard-" + ident,
                              "expected_worker_commit": WORKER,
                              "expected_manifest_sha256": "c" * 64})
    return {"schema_version": 1, "status": "success", "task_count": 40, "execution_id": COMPILE_RUN,
            "worker_commit": WORKER,
            "fresh_build": {"execution_id": COMPILE_RUN, "pack_run": "1", "shard_run": "2",
                            "plan_sha256": "d" * 64, "source_manifest_sha256": "e" * 64},
            "producers": producers}


def make_artifact(producers, missing=None, bad_sum=False, bad_source=False, altered_asset=False,
                  foreign_fit=False, serial="any"):
    delivery, contents = {}, {}
    for name in sorted(source.DELIVERY_ASSET_NAMES):
        data = ("payload:" + name).encode()
        contents[name] = data
        delivery[name] = {"size": len(data), "sha256": sha(data)}
    record = {"schema_version": 1, "status": "success", "build_number": BUILD, "product": source.PRODUCT,
              "delivery": delivery, "producers": producers,
              "source_selection": {"shard_run": "wrong" if bad_source else "2",
                                   "source_manifest_sha256": "0" * 64 if bad_source else "e" * 64,
                                   "build_number": BUILD, "target_product": "aosp_crux",
                                   "target_build_variant": "userdebug",
                                   "env": {"USE_CCACHE": "0", "CCACHE_DISABLE": "1",
                                           "BUILD_FINGERPRINT":
                                               f"Xiaomi/crux/crux:13/TQ3A.230901.001.B1/{BUILD}:userdebug/test-keys"}},
              "fit_validation": {"release_ready": False, "development_adb": True},
              "gates": {"images": {"boot": {"failures": []}, "recovery": {"failures": []}}}}
    record_bytes = (json.dumps(record, indent=2) + "\n").encode()
    sums = {name: item["sha256"] for name, item in delivery.items()}
    sums["delivery-record.json"] = sha(record_bytes)
    if bad_sum:
        sums["boot.img"] = "0" * 64
    sums_bytes = "".join(f"{value}  {name}\n" for name, value in sorted(sums.items())).encode()
    receipt = {"schema_version": 1, "status": "success", "build_number": BUILD, "product": source.PRODUCT,
               "assets": {name: item["sha256"] for name, item in delivery.items()}}
    if altered_asset:
        contents["boot.img"] = b"x" * len(contents["boot.img"])
    payload = {"update-binary": b"\x7fELF installer", "README.txt": b"readme",
               "system.raw.img.zst": b"system", "vendor.raw.img.zst": b"vendor",
               "recovery-cepheus.itb": b"other FIT" if foreign_fit else contents["recovery-cepheus.itb"],
               "rom-cepheus.itb": contents["rom-cepheus.itb"]}
    fields = {"schema": "crux-install/1", "kind": "zip", "device": "crux", "product": source.PRODUCT,
              "serialno": serial, "data_policy": "discard", "target_build": BUILD,
              "fit.recovery.entry": "recovery-cepheus.itb", "fit.recovery.sha256": delivery["recovery-cepheus.itb"]["sha256"],
              "fit.rom.entry": "rom-cepheus.itb", "fit.rom.sha256": delivery["rom-cepheus.itb"]["sha256"]}
    manifest = "".join(f"{key}={value}\n" for key, value in fields.items()).encode()
    payload["crux-install.txt"] = manifest
    payload["payload.json"] = json.dumps({"manifest_sha256": sha(manifest),
                                          "installer_sha256": sha(payload["update-binary"])}).encode()
    result = BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_STORED) as archive:
        for name, data in contents.items():
            if name != missing:
                archive.writestr("delivery/" + name, data)
        archive.writestr("delivery/delivery-record.json", record_bytes)
        archive.writestr("delivery/SHA256SUMS", sums_bytes)
        archive.writestr("assembly-receipt.json", json.dumps(receipt))
        for name, data in payload.items():
            if name != missing:
                archive.writestr("zip-payload/" + name, data)
    return result.getvalue()


def valid_context(summary, owner_count=8):
    """Build a compact compile context whose output-owner set is configurable."""
    producers = summary["producers"]
    task_ids = [producer["id"] for producer in producers]
    task_by_id = {producer["id"]: {
        "id": producer["id"], "wave": int(producer["id"][1:4]), "depends_on": [],
        "manifest_sha256": producer["expected_manifest_sha256"],
    } for producer in producers}
    waves = [[task_id for task_id in task_ids if int(task_id[1:4]) == wave] for wave in range(9)]
    out_root = "/tmp/accepted-out"
    product = out_root + "/target/product/crux"
    required_paths = [
        out_root + "/soong/build_number.txt",
        product + "/build_fingerprint.txt",
        product + "/obj/PACKAGING/system_build_prop_intermediates/buildinfo.prop",
        *(product + "/" + name + ".img" for name in ("boot", "recovery", "system", "vendor")),
        product + "/obj/KERNEL_OBJ",
    ]
    owners = [producer["id"] for producer in producers[:owner_count]]
    return {
        "schema_version": 1,
        "execution_id": COMPILE_RUN,
        "worker_commit": WORKER,
        "repository": source.REPOSITORY,
        "build_number": BUILD,
        "pack_run": summary["fresh_build"]["pack_run"],
        "shard_run": summary["fresh_build"]["shard_run"],
        "graph_run": "3",
        "index_run": "4",
        "source_run": "5",
        "source_manifest_sha256": summary["fresh_build"]["source_manifest_sha256"],
        "plan_sha256": summary["fresh_build"]["plan_sha256"],
        "source_root": "/tmp/accepted-source",
        "out_root": out_root,
        "waves": waves,
        "tasks": task_by_id,
        "assembly_exports": {path: owners[index % len(owners)] for index, path in enumerate(required_paths)},
    }


class ReleaseSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def invoke(self, *, mutate_summary=None, mutate_context=None, mutate_record_producers=None,
               mutate_assembly=None, bad_zip_digest=False, tag=TAG, manifest_build=BUILD,
               sign=False, owner_count=8, **kwargs):
        summary = valid_summary()
        if mutate_summary:
            mutate_summary(summary)
        context = valid_context(summary, owner_count)
        _, expected_owners = cloud_assemble.required_exports(context)
        producers_by_id = {producer["id"]: producer for producer in summary["producers"]}
        record_producers = [producers_by_id[ident] for ident in sorted(expected_owners)]
        if mutate_record_producers:
            mutate_record_producers(record_producers, summary)
        if mutate_context:
            mutate_context(context)
        summary_zip = BytesIO()
        with zipfile.ZipFile(summary_zip, "w") as archive:
            archive.writestr("build-summary.json", json.dumps(summary))
        context_zip = BytesIO()
        with zipfile.ZipFile(context_zip, "w") as archive:
            archive.writestr("context.json", json.dumps(context))
        manifest_zip = BytesIO()
        with zipfile.ZipFile(manifest_zip, "w") as archive:
            archive.writestr("crux-install.txt", f"schema=crux-install/1\nkind=zip\nproduct={source.PRODUCT}\n"
                                                 f"serialno=any\ndata_policy=discard\ntarget_build={manifest_build}\n")
        assembly_zip = make_artifact(record_producers, **kwargs)
        runs = {
            ASSEMBLY_RUN: {"id": int(ASSEMBLY_RUN), "conclusion": "success", "status": "completed",
                           "event": "workflow_dispatch", "head_branch": "main", "path": source.ASSEMBLY_WORKFLOW,
                           "display_title": "Verify fresh PE13 compilation " + COMPILE_RUN, "head_sha": COMMIT},
            COMPILE_RUN: {"id": int(COMPILE_RUN), "conclusion": "success", "status": "completed",
                          "event": "workflow_dispatch", "head_branch": "main", "path": source.COMPILE_WORKFLOW,
                          "display_title": "Fresh PE13 compile " + BUILD, "head_sha": WORKER},
        }
        if mutate_assembly:
            mutate_assembly(runs[ASSEMBLY_RUN])

        def info(ident, name, data, wrong=False):
            return {name: {"id": ident, "name": name, "digest": "sha256:" + ("f" * 64 if wrong else sha(data)),
                           "size_in_bytes": len(data), "expired": False}}

        inventory = {ASSEMBLY_RUN: {**info(77, f"cloud-fresh-rom-{COMPILE_RUN}", assembly_zip, wrong=bad_zip_digest),
                                    **info(88, source.ZIP_MANIFEST_ARTIFACT, manifest_zip.getvalue())},
                     COMPILE_RUN: {**info(66, "cloud-fresh-build-summary", summary_zip.getvalue()),
                                   **info(67, "cloud-build-context", context_zip.getvalue())}}
        data_by_id = {"77": assembly_zip, "66": summary_zip.getvalue(),
                      "67": context_zip.getvalue(), "88": manifest_zip.getvalue()}
        with mock.patch.dict("os.environ", {"GH_TOKEN": "test-token"}), \
                mock.patch.object(source, "_json_api", side_effect=lambda path, _: runs[path.rsplit("/", 1)[1]]), \
                mock.patch.object(source.cloud_layers, "artifact_inventory", side_effect=lambda run, *_: inventory[run]), \
                mock.patch.object(source.cloud_layers, "_open_retry",
                                  side_effect=lambda request: BytesIO(data_by_id[request.full_url.split("/")[-2]])):
            work = Path(tempfile.mkdtemp(dir=self.root))
            if sign:
                return source.signing_manifest(ASSEMBLY_RUN, work)
            return source.verify_source(ASSEMBLY_RUN, tag, work)

    def test_signing_manifest_is_the_install_manifest_of_the_assembly_build(self):
        build, manifest = self.invoke(sign=True)
        self.assertEqual(build, BUILD)
        self.assertIn(f"target_build={BUILD}\n", manifest.read_text())

    def test_signing_refuses_another_build_or_an_unsuccessful_assembly(self):
        with self.assertRaisesRegex(source.SourceError, "identity-free clean-install package"):
            self.invoke(sign=True, manifest_build="2026110304")
        with self.assertRaisesRegex(source.SourceError, "did not complete successfully"):
            self.invoke(sign=True, mutate_assembly=lambda run: run.update(conclusion="failure"))

    def test_verifies_lineage_delivery_and_payload(self):
        result = self.invoke()
        self.assertEqual((result.compile_run, result.assembly_commit, result.build), (COMPILE_RUN, COMMIT, BUILD))
        self.assertEqual(set(path.name for path in result.payload.iterdir()), set(source.PAYLOAD_NAMES))
        self.assertEqual(result.manifest_sha256, sha((result.payload / "crux-install.txt").read_bytes()))

    def test_assembly_producer_set_is_derived_from_output_owners_not_a_fixed_count(self):
        for owner_count in (4, 6, 8):
            with self.subTest(owner_count=owner_count):
                self.assertEqual(self.invoke(owner_count=owner_count).compile_run, COMPILE_RUN)

    def test_rejects_missing_extra_duplicate_and_foreign_assembly_producers(self):
        mutations = {
            "missing": lambda producers, summary: producers.pop(),
            "extra": lambda producers, summary: producers.append(summary["producers"][-1]),
            "duplicate": lambda producers, summary: producers.__setitem__(-1, dict(producers[0])),
            "foreign": lambda producers, summary: producers.__setitem__(-1, {
                **summary["producers"][-1], "id": "w999-s000", "artifact": "shard-w999-s000"}),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), self.assertRaisesRegex(source.SourceError, "producer IDs differ"):
                self.invoke(mutate_record_producers=mutate)

    def test_rejects_an_assembly_producer_with_foreign_compile_binding(self):
        def mutate(producers, summary):
            producers[0] = {**producers[0], "run_id": "999"}
        with self.assertRaisesRegex(source.SourceError, "bindings differ"):
            self.invoke(mutate_record_producers=mutate)

    def test_rejects_context_from_another_compile_execution_or_plan(self):
        for field, value in (("execution_id", "999"), ("worker_commit", "f" * 40),
                             ("repository", "other/repository"), ("build_number", "2026110399"),
                             ("plan_sha256", "f" * 64), ("source_manifest_sha256", "f" * 64)):
            with self.subTest(field=field), self.assertRaisesRegex(source.SourceError, "context identity|fresh binding"):
                self.invoke(mutate_context=lambda context, field=field, value=value:
                            context.__setitem__(field, value))

    def test_rejects_context_tasks_that_differ_from_summary_bindings(self):
        def missing_task(context):
            context["tasks"].pop("w000-s000")

        with self.assertRaisesRegex(source.SourceError, "tasks differ"):
            self.invoke(mutate_context=missing_task)

        def altered_task_manifest(context):
            context["tasks"]["w000-s000"]["manifest_sha256"] = "f" * 64

        with self.assertRaisesRegex(source.SourceError, "producer binding differs"):
            self.invoke(mutate_context=altered_task_manifest)

    def test_rejects_missing_assembly_role_and_owner_outside_compile_context(self):
        def missing_system_image(context):
            context["assembly_exports"].pop("/tmp/accepted-out/target/product/crux/system.img")

        with self.assertRaisesRegex(source.SourceError, "invalid assembly output owners"):
            self.invoke(mutate_context=missing_system_image)

        def foreign_kernel_owner(context):
            context["assembly_exports"]["/tmp/accepted-out/target/product/crux/obj/KERNEL_OBJ"] = "w999-s000"

        with self.assertRaisesRegex(source.SourceError, "outside the successful compile task inventory"):
            self.invoke(mutate_context=foreign_kernel_owner)

        def split_kernel_child(context):
            kernel = "/tmp/accepted-out/target/product/crux/obj/KERNEL_OBJ"
            owner = context["assembly_exports"][kernel]
            other = next(ident for ident in context["tasks"] if ident != owner)
            context["assembly_exports"][kernel + "/vmlinux"] = other

        with self.assertRaisesRegex(source.SourceError, "Kernel child is not owned"):
            self.invoke(mutate_context=split_kernel_child)

    def test_rejects_wrong_tag_workflow_or_failed_assembly(self):
        with self.assertRaisesRegex(source.SourceError, "invalid release tag"):
            self.invoke(tag="dev-pe13-2026110303-v1")
        with self.assertRaisesRegex(source.SourceError, "wrong workflow"):
            self.invoke(mutate_assembly=lambda run: run.update(path="wrong.yml"))
        with self.assertRaisesRegex(source.SourceError, "did not complete successfully"):
            self.invoke(mutate_assembly=lambda run: run.update(conclusion="failure"))

    def test_rejects_untrusted_zip_bytes(self):
        with self.assertRaisesRegex(source.SourceError, "ZIP digest differs"):
            self.invoke(bad_zip_digest=True)

    def test_rejects_wrong_producer_lineage(self):
        with self.assertRaises(source.SourceError):
            self.invoke(mutate_summary=lambda summary: summary["producers"][0].update(run_id="3"))

    def test_rejects_incomplete_artifacts(self):
        for missing in ("vendor.img", "system.raw.img.zst"):
            with self.subTest(missing=missing), self.assertRaisesRegex(source.SourceError, "missing required files"):
                self.invoke(missing=missing)

    def test_rejects_bad_or_altered_delivery_bytes(self):
        for kwargs in ({"bad_sum": True}, {"altered_asset": True}):
            with self.subTest(**kwargs), self.assertRaisesRegex(source.SourceError, "asset digest differs"):
                self.invoke(**kwargs)

    def test_rejects_source_selection_not_bound_to_fresh_compile(self):
        with self.assertRaises(source.SourceError):
            self.invoke(bad_source=True)

    def test_rejects_a_payload_that_is_not_this_build(self):
        with self.assertRaisesRegex(source.SourceError, "recovery FIT is not the verified assembly FIT"):
            self.invoke(foreign_fit=True)
        with self.assertRaisesRegex(source.SourceError, "identity-free clean-install package"):
            self.invoke(serial="8bd37df4")

    def test_summary_requires_every_task_and_all_nine_waves(self):
        def grow(summary):
            summary["producers"].append(dict(summary["producers"][-1], id="w005-s005", artifact="shard-w005-s005"))
            summary["task_count"] = len(summary["producers"])
        self.assertEqual(self.invoke(mutate_summary=grow).compile_run, COMPILE_RUN)
        with self.assertRaisesRegex(source.SourceError, "bind every compiled task"):
            self.invoke(mutate_summary=lambda summary: summary["producers"].pop())

        def drop_last_wave(summary):
            summary["producers"] = [p for p in summary["producers"] if not p["id"].startswith("w008-")]
            summary["task_count"] = len(summary["producers"])
        with self.assertRaisesRegex(source.SourceError, "all nine waves"):
            self.invoke(mutate_summary=drop_last_wave)


if __name__ == "__main__":
    unittest.main()
