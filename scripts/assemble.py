#!/usr/bin/env python3
"""Assemble verified PE13 image outputs from successful distributed workers."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

import worker


SOURCE_ROOT = Path("/home/qingli/crux-pe13-offline-2026-09-25/pe13")
PRODUCT_DIR = SOURCE_ROOT / "out/target/product/crux"
REQUIRED_IMAGES = ("boot.img", "recovery.img", "system.img", "vendor.img")
ARCHIVE_NAME = "crux-pe13-build-images.tar.zst"


README = """# Crux PixelExperience 13 build images

This archive contains boot.img, recovery.img, system.img and vendor.img produced
by the successful distributed build jobs recorded in build-manifest.json.
Optional kernel, DTB and ramdisk files are included when the producer receipts
contain them. source-revisions.json records the selected source revisions.

This is an image build archive. It is not a full OTA or an installation package.
Successful compilation and image assembly do not establish runtime behavior or
certify recovery touch, MTP, decryption, backup or restore.

The Crux development boot chain uses ABL to load U-Boot, then U-Boot to load PE.
These build images must pass the maintained U-Boot packaging and container
verification before any device use. The physical Recovery container requires
the accepted v1 header and coherent metadata. No U-Boot launch containers, phone
writes or on-device validation are claimed by this image archive.

Build profile, image integrity records and exact GitHub Actions producers are
in build-manifest.json.
"""


def validate_producers(producers):
    if not isinstance(producers, list) or not producers:
        raise ValueError("producer_runs must be a nonempty JSON array")
    seen = set()
    for producer in producers:
        if not str(producer["run_id"]).isdigit() or not re.fullmatch(r"shard-[A-Za-z0-9][A-Za-z0-9_.-]*", producer["artifact"]):
            raise ValueError("invalid producer run ID or artifact name")
        key = (str(producer["run_id"]), producer["artifact"])
        if key in seen:
            raise ValueError("duplicate producer run and artifact")
        seen.add(key)


def package_directory(directory, destination):
    with open(destination, "wb") as output:
        process = subprocess.Popen(["zstd", "-T0", "-6", "-q", "-c"], stdin=subprocess.PIPE, stdout=output)
        try:
            with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                for path in sorted(Path(directory).iterdir()):
                    archive.add(path, arcname=path.name, recursive=False)
            process.stdin.close()
            if process.wait() != 0:
                raise ValueError("image archive compression failed")
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
            process.wait()


def optional_image(name):
    return (name.startswith("ramdisk") and name.endswith(".img")) or name in {
        "kernel", "dtb", "dtb.img", "dtbo.img", "Image", "Image.gz", "Image-dtb", "Image.gz-dtb"
    } or name.endswith(".dtb")


def assemble(producers, dependency_root, output_dir, source_selection,
             product_dir=PRODUCT_DIR, source_root=SOURCE_ROOT, build_profile="aosp_crux-userdebug"):
    started = time.monotonic()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    product_dir = Path(product_dir)
    source_root = Path(source_root)
    destination = output_dir / ARCHIVE_NAME
    destination.unlink(missing_ok=True)
    (output_dir / "build-manifest.json").unlink(missing_ok=True)
    receipt = {"schema_version": 1, "status": "failed", "package_kind": "image-build-archive",
               "started_at": datetime.now(timezone.utc).isoformat(), "required_images": list(REQUIRED_IMAGES)}
    with open(output_dir / "assembly.log", "w", buffering=1) as log:
        try:
            validate_producers(producers)
            receipt["producer_runs"] = producers
            source_selection = Path(source_selection)
            selection = worker.read_json(source_selection)
            producer_details = []
            image_producers = {}
            directories = []
            for index, requested in enumerate(producers):
                directory = Path(dependency_root) / str(index)
                native = worker.read_json(directory / "receipt.json")
                if native.get("status") != "success":
                    raise ValueError(f"unsuccessful producer: {requested}")
                if native.get("source_root") != str(source_root):
                    raise ValueError(f"producer source root differs: {requested}")
                if requested["artifact"] != "shard-" + native["id"]:
                    raise ValueError(f"producer artifact and receipt IDs differ: {requested}")
                if "run_id" in native and str(native["run_id"]) != str(requested["run_id"]):
                    raise ValueError(f"producer run and receipt IDs differ: {requested}")
                detail = {**requested, "shard_id": native["id"], "manifest_sha256": native["manifest_sha256"],
                          "archive_sha256": native["archive_sha256"], "receipt_sha256": worker.digest(directory / "receipt.json"),
                          "source_root": native["source_root"], "worker_commit": native.get("worker_commit"),
                          "build_environment": native.get("build_environment", {})}
                producer_details.append(detail)
                for image in native["outputs"]:
                    image_producers.setdefault(image["path"], []).append({"run_id": requested["run_id"],
                                                                           "artifact": requested["artifact"],
                                                                           "shard_id": native["id"]})
                directories.append(directory)
            print(f"Verifying and merging {len(directories)} successful producer artifacts", file=log)
            accepted, _ = worker.merge_dependencies(directories)
            required_paths = [product_dir / name for name in REQUIRED_IMAGES]
            for path in required_paths:
                spec = accepted.get(str(path))
                if spec is None:
                    raise ValueError(f"required image absent from successful producer receipts: {path}")
                if spec["type"] != "file" or spec["size"] <= 0:
                    raise ValueError(f"required image must be a nonempty regular file: {path}")
                worker.verify_spec(spec)
            paths = set(required_paths)
            paths.update(Path(path) for path, spec in accepted.items()
                         if Path(path).parent == product_dir and optional_image(Path(path).name)
                         and spec["type"] == "file" and spec["size"] > 0)
            images = [{"file": path.name, "source_path": str(path), "size": accepted[str(path)]["size"],
                       "mode": accepted[str(path)]["mode"], "sha256": accepted[str(path)]["sha256"],
                       "producers": image_producers[str(path)]} for path in sorted(paths)]
            manifest = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                        "device": "crux", "build_profile": build_profile, "package_kind": "image-build-archive",
                        "full_ota": False, "source_root": str(source_root), "product_dir": str(product_dir),
                        "source_selection": selection, "source_selection_sha256": worker.digest(source_selection),
                        "producers": producer_details, "images": images,
                        "boot_chain": {"loader": "U-Boot", "chain": ["ABL", "U-Boot", "PixelExperience"],
                                       "device_container_packaging_verified": False, "recovery_container_header": "v1 required"},
                        "validation": {"successful_producer_receipts": True, "all_four_images_present": True,
                                       "output_integrity_verified": True, "runtime_tested": False}}
            with tempfile.TemporaryDirectory(dir=output_dir) as temporary:
                package = Path(temporary)
                for path in paths:
                    shutil.copy2(path, package / path.name)
                (package / "README.md").write_text(README)
                (package / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
                shutil.copy2(source_selection, package / "source-revisions.json")
                package_directory(package, destination)
            (output_dir / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            receipt.update(status="success", image_count=len(images), archive=destination.name,
                           archive_bytes=destination.stat().st_size, archive_sha256=worker.digest(destination))
            print(f"Assembled {len(images)} verified image files into {destination.name}", file=log)
        except Exception as error:
            receipt["error"] = str(error)
            destination.unlink(missing_ok=True)
            print(f"ERROR: {error}", file=log)
        finally:
            receipt["elapsed_seconds"] = time.monotonic() - started
            receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
            (output_dir / "assembly-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer-runs", required=True, help="JSON array file: [{run_id, artifact}]")
    parser.add_argument("--dependency-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-selection", default="manifests/source-revisions.json")
    parser.add_argument("--product-dir", default=str(PRODUCT_DIR))
    parser.add_argument("--source-root", default=str(SOURCE_ROOT))
    parser.add_argument("--build-profile", default="aosp_crux-userdebug")
    args = parser.parse_args()
    result = assemble(worker.read_json(args.producer_runs), args.dependency_root, args.output_dir,
                      args.source_selection, args.product_dir, args.source_root, args.build_profile)
    print(json.dumps({key: result.get(key) for key in ("status", "archive", "image_count", "error")}))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
