import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("cloud_pack", SCRIPTS / "cloud_pack.py")
cloud_pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud_pack)


class FakeZstd:
    def __init__(self, payload):
        self.stdout = io.BytesIO(payload)
        self._status = 0

    def wait(self):
        return self._status

    def poll(self):
        return self._status

    def kill(self):
        self._status = -9


class CloudPackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.out_root = self.root / "out"
        self.source.mkdir()
        self.out_root.mkdir()
        self.shard_id = "w000-s000"
        self.shards = self.root / "shards"
        self.shard_dir = self.shards / self.shard_id
        self.shard_dir.mkdir(parents=True)
        self.ninja = {"build.ninja": b"rule all\n", "file-2.ninja": b"build output: phony\n"}
        for name, data in self.ninja.items():
            (self.shard_dir / name).write_bytes(data)
        self.slice_manifest = {
            "schema_version": 1,
            "source_root": str(self.source),
            "runtime_dir": ".crux-task/graph",
            "targets": [str(self.out_root / "final")],
            "outputs": [str(self.out_root / "final")],
            "external_inputs": [str(self.source / "external.txt")],
            "leaf_inputs": [],
            "commands": [],
            "edges": [],
        }
        self.slice_manifest_path = self.shard_dir / "manifest.json"
        self.write_slice_manifest()
        self.source_manifest_path = self.root / "source-manifest.json"
        self.source_manifest_path.write_text(json.dumps({"schema_version": 1,
                                                        "source_root": str(self.source)}, sort_keys=True) + "\n")
        self.manifest_sha = hashlib.sha256(self.source_manifest_path.read_bytes()).hexdigest()
        self.index_path = self.root / "index-pruned.sqlite"
        self.index_path.write_bytes(b"fresh-pruned-graph-index")
        self.pruned_sha = hashlib.sha256(self.index_path.read_bytes()).hexdigest()
        self.shard_receipt_path = self.root / "shard-receipt.json"
        self.write_shard_receipt()
        self.inventory_path = self.root / "slice-inventory.json"
        self.write_inventory()
        self.output = self.root / "packed"
        self.output.mkdir()
        (self.output / "workflow.log").write_text("allowed pre-existing runner log")
        self.archive_variant = None
        self.capsule_variant = None
        self.calls = []

    def tearDown(self):
        self.temporary.cleanup()

    def write_slice_manifest(self):
        self.slice_manifest_path.write_text(json.dumps(self.slice_manifest, indent=2, sort_keys=True) + "\n")

    def write_shard_receipt(self, **overrides):
        receipt = {"schema_version": 1, "status": "success", "source_root": str(self.source),
                   "out_root": str(self.out_root), "manifest_sha256": self.manifest_sha,
                   "pruned_database": {"bytes": self.index_path.stat().st_size,
                                       "sha256": self.pruned_sha}}
        receipt.update(overrides)
        self.shard_receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")

    def write_inventory(self, **overrides):
        inventory = {"schema_version": 1, "plan_sha256": "a" * 64,
                     "shards": {self.shard_id: {
                         "manifest_sha256": hashlib.sha256(self.slice_manifest_path.read_bytes()).hexdigest(),
                         "ninja_files": {name: hashlib.sha256(data).hexdigest()
                                         for name, data in self.ninja.items()},
                     }}}
        inventory.update(overrides)
        self.inventory_path.write_text(json.dumps(inventory, sort_keys=True) + "\n")

    def file_specs(self):
        first = self.source / "a.txt"
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"shared bytes")
        first.chmod(0o640)
        second = self.source / "b.txt"
        if second.exists() or second.is_symlink():
            second.unlink()
        os.link(first, second)
        link = self.source / "link.txt"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to("a.txt")
        directory = self.source / "empty-dir"
        if directory.exists():
            directory.rmdir()
        directory.mkdir()
        directory.chmod(0o755)
        items = [
            {"path": str(self.source / ".crux-task/graph/build.ninja"), "type": "file", "mode": 0o644,
             "size": len(self.ninja["build.ninja"]), "sha256": hashlib.sha256(self.ninja["build.ninja"]).hexdigest(),
             "reasons": ["task-graph"]},
            {"path": str(self.source / ".crux-task/graph/file-2.ninja"), "type": "file", "mode": 0o644,
             "size": len(self.ninja["file-2.ninja"]), "sha256": hashlib.sha256(self.ninja["file-2.ninja"]).hexdigest(),
             "reasons": ["task-graph"]},
            {"path": str(self.source / ".crux-task/graph/manifest.json"), "type": "file", "mode": 0o644,
             "size": self.slice_manifest_path.stat().st_size,
             "sha256": hashlib.sha256(self.slice_manifest_path.read_bytes()).hexdigest(),
             "reasons": ["task-graph"]},
            {"path": str(first), "type": "file", "mode": 0o640, "size": first.stat().st_size,
             "sha256": hashlib.sha256(first.read_bytes()).hexdigest(), "reasons": ["fixture"]},
            {"path": str(second), "type": "file", "mode": 0o640, "size": second.stat().st_size,
             "sha256": hashlib.sha256(second.read_bytes()).hexdigest(), "reasons": ["fixture"]},
            {"path": str(directory), "type": "directory", "mode": 0o755, "size": 0, "reasons": ["fixture"]},
            {"path": str(link), "type": "symlink", "mode": stat.S_IMODE(link.lstat().st_mode),
             "size": 0, "target": "a.txt", "reasons": ["fixture"]},
        ]
        return sorted(items, key=lambda item: item["path"])

    def create_tar(self, metadata, corrupt=None, bundle_mismatch=False):
        stream = io.BytesIO()
        first_name = str(self.source / "a.txt").lstrip("/")
        files = {item["path"]: item for item in metadata["files"]}
        data = {
            str(self.source / ".crux-task/graph/build.ninja"): self.ninja["build.ninja"],
            str(self.source / ".crux-task/graph/file-2.ninja"): self.ninja["file-2.ninja"],
            str(self.source / ".crux-task/graph/manifest.json"): self.slice_manifest_path.read_bytes(),
            str(self.source / "a.txt"): b"shared bytes",
        }
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for item in metadata["files"]:
                if (corrupt == "missing"
                        and item["path"] == str(self.source / ".crux-task/graph/file-2.ninja")):
                    continue
                name = item["path"].lstrip("/")
                info = tarfile.TarInfo(name)
                info.mode = item["mode"]
                info.uid = info.gid = 0
                if item["type"] == "directory":
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                elif item["type"] == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "wrong-target" if corrupt == "symlink" else item["target"]
                    archive.addfile(info)
                elif item["path"] == str(self.source / "b.txt"):
                    info.type = tarfile.LNKTYPE
                    info.linkname = first_name
                    archive.addfile(info)
                else:
                    payload = data[item["path"]]
                    if corrupt == "file" and item["path"] == str(self.source / "a.txt"):
                        payload = b"changed text"
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
            if corrupt == "extra":
                info = tarfile.TarInfo("unexpected.txt")
                payload = b"unexpected"
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            internal = {key: value for key, value in metadata.items()
                        if key not in {"archive", "digest_cache"}}
            if bundle_mismatch:
                internal = {**internal, "file_count": -1}
            payload = (json.dumps(internal, indent=2, sort_keys=True) + "\n").encode()
            info = tarfile.TarInfo(str(Path(metadata["slice_path"]) / "bundle.json").lstrip("/"))
            info.mode = 0o644
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        return stream.getvalue()

    def fake_capsule_pack(self, manifest, source_root, archive_path, out_root, **kwargs):
        self.calls.append((manifest, source_root, out_root, kwargs))
        self.assertEqual(Path(manifest), self.slice_manifest_path.resolve())
        self.assertEqual(Path(source_root), self.source.resolve())
        self.assertEqual(Path(out_root), self.out_root.resolve())
        self.assertEqual(kwargs["allowed_generated"], ())
        self.assertIsNone(kwargs["shared_manifest"])
        self.assertIsNone(kwargs["digest_cache"])
        self.assertEqual(Path(kwargs["graph_index"]), self.index_path.resolve())
        files = self.file_specs()
        metadata = {"schema_version": 1, "source_root": str(self.source), "out_root": str(self.out_root),
                    "slice_path": str(self.source / ".crux-task/graph"),
                    "manifest_sha256": hashlib.sha256(self.slice_manifest_path.read_bytes()).hexdigest(),
                    "files": files, "file_count": len(files),
                    "input_bytes": sum(item["size"] for item in files),
                    "allowed_generated_inputs": [], "external_inputs": [], "archive": {},
                    "digest_cache": {"computed": len(files), "reused": 0}}
        if self.capsule_variant == "allowed-generated":
            metadata["allowed_generated_inputs"] = [{"path": str(self.source / "generated.h"),
                                                      "reason": "unapproved"}]
        elif self.capsule_variant == "output-overlap":
            metadata["files"].append({"path": str(self.out_root / "final"), "type": "file",
                                      "mode": 0o644, "size": 0,
                                      "sha256": hashlib.sha256(b"").hexdigest(), "reasons": ["fixture"]})
            metadata["file_count"] += 1
        archive_bytes = (b"not-read-before-overlap-check" if self.capsule_variant == "output-overlap" else
                         self.create_tar(metadata, corrupt=self.archive_variant,
                                         bundle_mismatch=self.archive_variant == "bundle"))
        archive_path.write_bytes(archive_bytes)
        metadata["archive"] = {"name": archive_path.name, "size": len(archive_bytes),
                               "sha256": hashlib.sha256(archive_bytes).hexdigest()}
        metadata_path = archive_path.with_name(archive_path.name.removesuffix(".tar.zst") + ".json")
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        return metadata_path, metadata

    def invoke(self):
        args = ["--shards", str(self.shards), "--shard", self.shard_id,
                "--slice-inventory", str(self.inventory_path),
                "--shard-receipt", str(self.shard_receipt_path),
                "--source-manifest", str(self.source_manifest_path),
                "--graph-index", str(self.index_path), "--output", str(self.output)]
        with mock.patch.object(cloud_pack.capsule, "pack", side_effect=self.fake_capsule_pack), \
                mock.patch.object(cloud_pack.shutil, "which", return_value="zstd"), \
                mock.patch.object(cloud_pack.subprocess, "Popen",
                                  side_effect=lambda command, **kwargs: FakeZstd(Path(command[-1]).read_bytes())):
            return cloud_pack.main(args)

    def test_pack_verifies_fresh_cold_bundle_hardlink_and_sidecar(self):
        self.assertEqual(self.invoke(), 0)
        receipt = json.loads((self.output / "pack-receipt.json").read_text())
        self.assertEqual(receipt["status"], "success")
        self.assertEqual(receipt["shard_id"], self.shard_id)
        self.assertEqual(receipt["file_count"], 7)
        self.assertEqual(receipt["verification"]["hardlink_count"], 1)
        self.assertEqual(receipt["verification"]["member_count"], 8)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue((self.output / (self.shard_id + ".tar.zst")).is_file())
        self.assertTrue((self.output / (self.shard_id + ".json")).is_file())
        self.assertEqual((self.output / "workflow.log").read_text(), "allowed pre-existing runner log")

    def test_corrupt_regular_file_is_rejected(self):
        self.archive_variant = "file"
        self.assertEqual(self.invoke(), 1)
        receipt = json.loads((self.output / "pack-receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("content differs", receipt["error"])

    def test_wrong_symlink_target_is_rejected(self):
        self.archive_variant = "symlink"
        self.assertEqual(self.invoke(), 1)
        receipt = json.loads((self.output / "pack-receipt.json").read_text())
        self.assertIn("symlink target", receipt["error"])

    def test_mismatched_embedded_bundle_is_rejected(self):
        self.archive_variant = "bundle"
        self.assertEqual(self.invoke(), 1)
        receipt = json.loads((self.output / "pack-receipt.json").read_text())
        self.assertIn("bundle.json differs", receipt["error"])

    def test_missing_or_extra_archive_members_are_rejected(self):
        for variant, message in (("missing", "missing members"), ("extra", "Unexpected capsule archive member")):
            with self.subTest(variant=variant):
                self.archive_variant = variant
                self.output = self.root / ("packed-" + variant)
                self.output.mkdir()
                self.assertEqual(self.invoke(), 1)
                receipt = json.loads((self.output / "pack-receipt.json").read_text())
                self.assertIn(message, receipt["error"])

    def test_current_source_manifest_pin_is_required(self):
        self.write_shard_receipt(manifest_sha256="0" * 64)
        self.assertEqual(self.invoke(), 1)
        receipt = json.loads((self.output / "pack-receipt.json").read_text())
        self.assertIn("different source manifest", receipt["error"])
        self.assertEqual(self.calls, [])

    def test_pruned_graph_index_identity_is_required(self):
        self.index_path.write_bytes(b"changed graph database")
        self.assertEqual(self.invoke(), 1)
        receipt = json.loads((self.output / "pack-receipt.json").read_text())
        self.assertIn("differs from the shard receipt", receipt["error"])
        self.assertEqual(self.calls, [])

    def test_selected_slice_manifest_digest_is_required(self):
        self.write_inventory()
        data = self.slice_manifest_path.read_bytes() + b" "
        self.slice_manifest_path.write_bytes(data)
        self.assertEqual(self.invoke(), 1)
        receipt = json.loads((self.output / "pack-receipt.json").read_text())
        self.assertIn("Shard manifest differs", receipt["error"])
        self.assertEqual(self.calls, [])

    def test_selected_ninja_file_digest_is_required(self):
        inventory = json.loads(self.inventory_path.read_text())
        inventory["shards"][self.shard_id]["ninja_files"]["build.ninja"] = "0" * 64
        self.inventory_path.write_text(json.dumps(inventory))
        self.assertEqual(self.invoke(), 1)
        receipt = json.loads((self.output / "pack-receipt.json").read_text())
        self.assertIn("Ninja file differs", receipt["error"])
        self.assertEqual(self.calls, [])

    def test_allowed_generated_inputs_are_rejected(self):
        self.capsule_variant = "allowed-generated"
        self.assertEqual(self.invoke(), 1)
        receipt = json.loads((self.output / "pack-receipt.json").read_text())
        self.assertIn("allowed_generated_inputs", receipt["error"])

    def test_capsule_cannot_embed_declared_outputs_or_external_inputs(self):
        self.capsule_variant = "output-overlap"
        self.assertEqual(self.invoke(), 1)
        receipt = json.loads((self.output / "pack-receipt.json").read_text())
        self.assertIn("declared outputs/external inputs", receipt["error"])

    def test_transport_identity_keeps_a_symlink_name_distinct_from_its_target(self):
        target = self.root / 'target-data'
        target.write_text('source')
        alias = self.root / 'logical-alias'
        alias.symlink_to(target)
        self.assertEqual(cloud_pack._declared_paths([str(alias)], self.source), {str(alias)})


if __name__ == "__main__":
    unittest.main()
