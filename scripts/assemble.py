#!/usr/bin/env python3
"""Assemble verified PE13 image outputs from successful distributed workers."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

import worker
import package_fits


REQUIRED_IMAGES = ("boot.img", "recovery.img", "system.img", "vendor.img")
ARCHIVE_NAME = "crux-pe13-build-images.tar.zst"


README = """# Crux PixelExperience 13 build images

This archive contains boot.img, recovery.img, system.img and vendor.img produced
by the successful distributed build jobs recorded in build-manifest.json.
Optional kernel, DTB and ramdisk files are included when the producer receipts
contain them. source-revisions.json records the selected source revisions.
kernel-symbols.tar.zst contains the fresh matching Image, vmlinux, System.map
and kernel configuration. fit-inputs.tar.zst contains the raw source DTBs,
Crux overlay and native ramdisks; uboot-fits.tar.zst contains the validated
native ROM and PE Recovery FITs. Their provenance is in build-manifest.json.

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


def validate_producers(producers, accepted_worker_commits):
    if not isinstance(accepted_worker_commits, list) or not accepted_worker_commits or any(
            not isinstance(commit, str) or not re.fullmatch(r'[a-f0-9]{40}', commit) for commit in accepted_worker_commits):
        raise ValueError("accepted_worker_commits must explicitly list approved full commit SHAs")
    if not isinstance(producers, list) or not producers:
        raise ValueError("producer_runs must be a nonempty JSON array")
    seen = set()
    for producer in producers:
        if not str(producer["run_id"]).isdigit() or not re.fullmatch(r"shard-[A-Za-z0-9][A-Za-z0-9_.-]*", producer["artifact"]):
            raise ValueError("invalid producer run ID or artifact name")
        if producer.get("expected_worker_commit") not in accepted_worker_commits:
            raise ValueError("producer worker commit has no explicit approval")
        if not re.fullmatch(r'[a-f0-9]{64}', producer.get("expected_manifest_sha256", "")):
            raise ValueError("producer must bind its expected Ninja manifest SHA256")
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
             product_dir=None, source_root=None, build_profile="aosp_crux-userdebug",
             accepted_worker_commits=None, with_fits=True, mkimage="mkimage", fdtoverlay="fdtoverlay"):
    started = time.monotonic()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / ARCHIVE_NAME
    destination.unlink(missing_ok=True)
    (output_dir / "build-manifest.json").unlink(missing_ok=True)
    receipt = {"schema_version": 1, "status": "failed", "package_kind": "image-build-archive",
               "started_at": datetime.now(timezone.utc).isoformat(), "required_images": list(REQUIRED_IMAGES)}
    with open(output_dir / "assembly.log", "w", buffering=1) as log:
        try:
            validate_producers(producers, accepted_worker_commits)
            receipt["producer_runs"] = producers
            source_selection = Path(source_selection)
            selection = worker.read_json(source_selection)
            producer_details = []
            image_producers = {}
            directories = []
            native_receipts = []
            for index, requested in enumerate(producers):
                directory = Path(dependency_root) / str(index)
                native = worker.read_json(directory / "receipt.json")
                if native.get("status") != "success":
                    raise ValueError(f"unsuccessful producer: {requested}")
                if requested["artifact"] != "shard-" + native["id"]:
                    raise ValueError(f"producer artifact and receipt IDs differ: {requested}")
                if "run_id" in native and str(native["run_id"]) != str(requested["run_id"]):
                    raise ValueError(f"producer run and receipt IDs differ: {requested}")
                if native.get("worker_commit") != requested["expected_worker_commit"]:
                    raise ValueError(f"producer worker commit differs from its immutable binding: {requested}")
                if native.get("manifest_sha256") != requested["expected_manifest_sha256"]:
                    raise ValueError(f"producer manifest differs from its immutable binding: {requested}")
                detail = {**requested, "shard_id": native["id"], "manifest_sha256": native["manifest_sha256"],
                          "archive_sha256": native["archive_sha256"], "receipt_sha256": worker.digest(directory / "receipt.json"),
                          "source_root": native["source_root"], "worker_commit": native.get("worker_commit"),
                          "out_root": native.get("out_root"),
                          "build_environment": native.get("build_environment", {})}
                producer_details.append(detail)
                for image in native["outputs"]:
                    image_producers.setdefault(image["path"], []).append({"run_id": requested["run_id"],
                                                                           "artifact": requested["artifact"],
                                                                           "shard_id": native["id"]})
                directories.append(directory)
                native_receipts.append(native)
            source_roots = {native.get("source_root") for native in native_receipts}
            out_roots = {native.get("out_root") for native in native_receipts}
            if len(source_roots) != 1 or None in source_roots or len(out_roots) != 1 or None in out_roots:
                raise ValueError("successful producers must share one source_root and out_root")
            actual_source = Path(next(iter(source_roots)))
            out_root = Path(next(iter(out_roots)))
            if not actual_source.is_absolute() or not out_root.is_absolute():
                raise ValueError("producer source_root/out_root must be absolute paths")
            if source_root is not None and Path(source_root) != actual_source:
                raise ValueError("requested source root differs from successful producer context")
            source_root = actual_source
            actual_product = out_root / "target/product/crux"
            if product_dir is not None and Path(product_dir) != actual_product:
                raise ValueError("requested product directory differs from successful producer OUT_DIR")
            product_dir = actual_product
            receipt.update(source_root=str(source_root), out_root=str(out_root), product_dir=str(product_dir),
                           accepted_worker_commits=accepted_worker_commits, assembly_commit=os.environ.get("GITHUB_SHA"))
            print(f"Verifying and merging {len(directories)} successful producer artifacts", file=log)
            bindings = [{**producer, "id": producer["artifact"][6:]} for producer in producers]
            accepted, _ = worker.merge_dependencies(directories, bindings, source_root, out_root)
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
                        "full_ota": False, "source_root": str(source_root), "out_root": str(out_root), "product_dir": str(product_dir),
                        "accepted_worker_commits": accepted_worker_commits, "assembly_commit": os.environ.get("GITHUB_SHA"),
                        "source_selection": selection, "source_selection_sha256": worker.digest(source_selection),
                        "producers": producer_details, "images": images,
                        "boot_chain": {"loader": "U-Boot", "chain": ["ABL", "U-Boot", "PixelExperience"],
                                       "device_container_packaging_verified": False, "recovery_container_header": "v1 required"},
                        "validation": {"successful_producer_receipts": True, "all_four_images_present": True,
                                       "output_integrity_verified": True, "runtime_tested": False}}
            with tempfile.TemporaryDirectory(dir=output_dir) as temporary:
                package = Path(temporary)
                if with_fits:
                    fit_stage = package / "uboot-stage"
                    packaged = package_fits.prepare(accepted, native_receipts, fit_stage, product_dir,
                                                     product_dir / "obj/KERNEL_OBJ", selection, True, mkimage, fdtoverlay)
                    for name in ("fit-packaging.log", "fit-packaging-receipt.json"):
                        if (fit_stage / name).exists():
                            shutil.copy2(fit_stage / name, output_dir / name)
                    if packaged["status"] != "success" or not packaged["fits_generated"]:
                        raise ValueError("strict U-Boot FIT packaging failed: " + packaged.get("error", "both FITs required"))
                    bundles = []
                    for name in ("kernel-symbols.tar.zst", "fit-inputs.tar.zst", "uboot-fits.tar.zst"):
                        path = fit_stage / name
                        if not path.is_file() or path.stat().st_size == 0:
                            raise ValueError("strict packaging did not produce required bundle: " + name)
                        spec = next(item for item in packaged["archives"] if item["path"] == str(path))
                        bundles.append({"file": name, "size": spec["size"], "sha256": spec["sha256"]})
                        shutil.move(path, package / name)
                    manifest["uboot_packaging"] = {"kernel_producer": packaged["kernel_producer"], "bundles": bundles,
                                                    "fits_generated": True, "physical_recovery_container_generated": False}
                    manifest["validation"]["matching_fresh_kernel_symbols"] = True
                    manifest["validation"]["both_fits_strictly_validated"] = True
                    shutil.rmtree(fit_stage)
                for path in paths:
                    shutil.copy2(path, package / path.name)
                (package / "README.md").write_text(README if with_fits else README[:README.index("kernel-symbols.tar.zst")] + README[README.index("\nThis is an image build archive."):])
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
    parser.add_argument("--producer-runs", required=True,
                        help="JSON array file: [{run_id, artifact, expected_worker_commit, expected_manifest_sha256}]")
    parser.add_argument("--dependency-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-selection", default="manifests/source-revisions.json")
    parser.add_argument("--product-dir")
    parser.add_argument("--source-root")
    parser.add_argument("--accepted-worker-commits", required=True, help="JSON array of explicitly approved compilation worker SHAs")
    parser.add_argument("--mkimage", default="mkimage")
    parser.add_argument("--fdtoverlay", default="fdtoverlay")
    parser.add_argument("--build-profile", default="aosp_crux-userdebug")
    args = parser.parse_args()
    result = assemble(worker.read_json(args.producer_runs), args.dependency_root, args.output_dir,
                      args.source_selection, args.product_dir, args.source_root, args.build_profile,
                      json.loads(args.accepted_worker_commits), True, args.mkimage, args.fdtoverlay)
    print(json.dumps({key: result.get(key) for key in ("status", "archive", "image_count", "error")}))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
