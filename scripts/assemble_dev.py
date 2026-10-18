#!/usr/bin/env python3
"""Assemble the development PE13 delivery from verified distributed producers.

The release assembler (scripts/assemble.py) expects one shard to carry the four
frontend identity files.  In the development graph they are owned by separate
shards (build_number: w000-s000, build_fingerprint: w000-s012, buildinfo:
w001-s000) and `build_thumbprint.txt` is a frontend scalar with no shard
producer, so this assembler verifies the identity files that exist, materializes
the admitted thumbprint, and then applies the same strict image-property, FIT
packaging and delivery gates as the release path.

Outputs: delivery/{boot.img,recovery.img,system.img,vendor.img,
rom-cepheus.itb,recovery-cepheus.itb,SHA256SUMS,delivery-record.json}
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import package_fits
import verify_properties
import worker

REQUIRED_IMAGES = ("boot.img", "recovery.img", "system.img", "vendor.img")
DELIVERY_FITS = ("rom-cepheus.itb", "recovery-cepheus.itb")
MOUNT_POINTS = ("debug_ramdisk", "dev", "metadata", "mnt", "proc", "second_stage_resources", "sys")
AVB_ALGORITHMS = {0: "NONE", 1: "SHA256_RSA2048", 2: "SHA256_RSA4096", 3: "SHA256_RSA8192"}
FINGERPRINT_PROPERTY = {"boot": "com.android.build.boot.fingerprint",
                        "recovery": "com.android.build.recovery.fingerprint"}


def u32be(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def u64be(data, offset):
    return struct.unpack_from(">Q", data, offset)[0]


def u32le(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def u64le(data, offset):
    return struct.unpack_from("<Q", data, offset)[0]


def align(value, page):
    return (value + page - 1) // page * page


def parse_avb(image):
    """Structural `avbtool info_image` equivalent for a hash-footered image."""
    data = Path(image).read_bytes()
    if len(data) < 64 or data[-64:-60] != b"AVBf":
        raise ValueError("missing AVB footer: " + str(image))
    footer = data[-64:]
    major, minor = struct.unpack_from(">II", footer, 4)
    original, vbmeta_offset, vbmeta_size = struct.unpack_from(">QQQ", footer, 12)
    if vbmeta_offset + vbmeta_size > len(data):
        raise ValueError("AVB vbmeta lies outside the image: " + str(image))
    vbmeta = data[vbmeta_offset:vbmeta_offset + vbmeta_size]
    if vbmeta[:4] != b"AVB0":
        raise ValueError("missing AVB0 vbmeta header: " + str(image))
    auth_size, aux_size = u64be(vbmeta, 12), u64be(vbmeta, 20)
    algorithm = u32be(vbmeta, 28)
    rollback = u64be(vbmeta, 112)
    release = vbmeta[128:176].rstrip(b"\0").decode(errors="replace")
    desc_offset, desc_size = u64be(vbmeta, 96), u64be(vbmeta, 104)
    aux_start = 256 + auth_size
    if aux_start + desc_offset + desc_size > len(vbmeta):
        raise ValueError("AVB descriptor block lies outside vbmeta: " + str(image))
    descriptors = vbmeta[aux_start + desc_offset:aux_start + desc_offset + desc_size]
    hashes, props = [], {}
    offset = 0
    while offset + 16 <= len(descriptors):
        tag, length = u64be(descriptors, offset), u64be(descriptors, offset + 8)
        if offset + 16 + length > len(descriptors):
            raise ValueError("AVB descriptor length exceeds its block: " + str(image))
        body = descriptors[offset + 16:offset + 16 + length]
        if tag == 2 and len(body) >= 116:
            image_size = u64be(body, 0)
            hash_algorithm = body[8:40].rstrip(b"\0").decode()
            name_len, salt_len, digest_len, flags = struct.unpack_from(">IIII", body, 40)
            name = body[116:116 + name_len].decode()
            salt = body[116 + name_len:116 + name_len + salt_len]
            digest = body[116 + name_len + salt_len:116 + name_len + salt_len + digest_len]
            hashes.append({"image_size": image_size, "hash_algorithm": hash_algorithm,
                           "partition_name": name, "salt_len": salt_len,
                           "salt_hex": salt.hex(), "digest_len": digest_len,
                           "digest_hex": digest.hex(), "flags": flags})
        elif tag == 0 and len(body) >= 16:
            key_len, value_len = u64be(body, 0), u64be(body, 8)
            key = body[16:16 + key_len].decode(errors="replace")
            start = 16 + key_len
            if body[start:start + 1] == b"\x00":
                start += 1
            value = body[start:start + value_len].decode(errors="replace")
            props[key] = value
        offset += 16 + length
    return {"footer_version": [major, minor], "original_image_size": original,
            "vbmeta_offset": vbmeta_offset, "vbmeta_size": vbmeta_size,
            "authentication_block_size": auth_size, "auxiliary_block_size": aux_size,
            "algorithm": algorithm, "algorithm_name": AVB_ALGORITHMS.get(algorithm, str(algorithm)),
            "rollback_index": rollback, "release_string": release,
            "hash_descriptors": hashes, "properties": props}


def parse_android_boot(image):
    """Parse the Android v1 boot/recovery header and its ramdisk placement."""
    data = Path(image).read_bytes()
    if data[:8] != b"ANDROID!":
        raise ValueError("missing ANDROID boot magic: " + str(image))
    kernel_size, ramdisk_size = u32le(data, 8), u32le(data, 16)
    second_size, page_size = u32le(data, 24), u32le(data, 36)
    header_version, os_version = u32le(data, 40), u32le(data, 44)
    if page_size <= 0 or page_size & (page_size - 1):
        raise ValueError("invalid boot page size: " + str(image))
    header_size = u32le(data, 1644) if header_version >= 1 else 1632
    recovery_dtbo_size = u32le(data, 1632) if header_version >= 1 else 0
    recovery_dtbo_offset = u64le(data, 1636) if header_version >= 1 else 0
    ramdisk_offset = page_size + align(kernel_size, page_size)
    if ramdisk_offset + ramdisk_size > len(data):
        raise ValueError("ramdisk lies outside the image: " + str(image))
    expected = align(page_size + align(kernel_size, page_size) + ramdisk_size
                     + second_size + recovery_dtbo_size, page_size)
    return {"header_version": header_version, "header_size": header_size,
            "os_version": os_version, "page_size": page_size,
            "kernel_size": kernel_size, "ramdisk_size": ramdisk_size,
            "second_size": second_size, "recovery_dtbo_size": recovery_dtbo_size,
            "recovery_dtbo_offset": recovery_dtbo_offset,
            "ramdisk_offset": ramdisk_offset,
            "ramdisk_gzip": data[ramdisk_offset:ramdisk_offset + 2] == b"\x1f\x8b",
            "expected_image_size": expected}


def cpio_entries(ramdisk):
    data = gzip.decompress(Path(ramdisk).read_bytes())
    entries, offset = [], 0
    while offset + 110 <= len(data) and data[offset:offset + 6] == b"070701":
        fields = [data[offset + 6 + 8 * index: offset + 6 + 8 * (index + 1)] for index in range(13)]
        name_size = int(fields[11], 16)
        file_size = int(fields[6], 16)
        name_start = offset + 110
        name = data[name_start:name_start + name_size - 1].decode(errors="replace")
        entries.append(name)
        offset = (name_start + name_size + 3) & ~3
        offset = (offset + file_size + 3) & ~3
    return entries


def fit_listing(dumpimage, fit):
    result = subprocess.run([str(dumpimage), "-l", str(fit)], capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError("dumpimage failed for " + str(fit) + ": " + result.stderr.strip())
    types = sorted({match.strip() for match in re.findall(r"^\s+Type:\s+(.+)$", result.stdout, re.M)})
    default = re.search(r"Default Configuration:\s+'([^']+)'", result.stdout)
    return {"types": types, "default_configuration": default.group(1) if default else None,
            "listing": result.stdout}


def write_thumbprint(identity, destination):
    text = identity["frontend_thumbprint"]
    destination.write_text(text)
    return text


def validate_identity_files(identity, product, out_root):
    errors = []
    expected_buildinfo = identity["required_buildinfo"]
    buildinfo_path = product / "obj/PACKAGING/system_build_prop_intermediates/buildinfo.prop"
    buildinfo = {}
    for line in buildinfo_path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            buildinfo[key] = value
    for key, value in expected_buildinfo.items():
        if buildinfo.get(key) != value:
            errors.append({"scope": "buildinfo", "key": key, "expected": value,
                           "actual": buildinfo.get(key)})
    number = out_root / "soong/build_number.txt"
    if number.read_text() != expected_buildinfo["ro.build.version.incremental"]:
        errors.append({"scope": "build_number", "expected": expected_buildinfo["ro.build.version.incremental"],
                       "actual": number.read_text()})
    fingerprint_path = product / "build_fingerprint.txt"
    expected_fingerprint = identity["required_final_keys"]["ro.build.fingerprint"] + "\n"
    if fingerprint_path.read_text() != expected_fingerprint:
        errors.append({"scope": "build_fingerprint", "expected": expected_fingerprint,
                       "actual": fingerprint_path.read_text()})
    return {"errors": errors, "buildinfo_members": len(buildinfo)}


def gate_images(product, identity, thumbprint_path, debugfs, simg2img, dumpimage, out_root):
    gates = {"images": {}, "boot_headers": {}, "avb": {}, "ramdisks": {}, "fits": {}}
    images = {partition: product / (partition + ".img") for partition in verify_properties.NATIVE_PARTITIONS
              if (product / (partition + ".img")).is_file()}
    property_report = verify_properties.verify_images(
        identity, product / "obj/PACKAGING/system_build_prop_intermediates/buildinfo.prop",
        thumbprint_path, images, debugfs, simg2img)
    gates["property_verification"] = property_report
    expected_fingerprint = identity["required_final_keys"]["ro.build.fingerprint"]
    for role, name in (("boot", "boot.img"), ("recovery", "recovery.img")):
        header = parse_android_boot(product / name)
        avb = parse_avb(product / name)
        failures = []
        if header["header_version"] != 1 or header["header_size"] != 1648:
            failures.append("expected Android v1 header (version=1 size=1648)")
        if header["page_size"] != 4096 or not header["ramdisk_gzip"]:
            failures.append("page size or ramdisk gzip placement differs")
        if header["expected_image_size"] != avb["original_image_size"]:
            failures.append("v1 header size %d != AVB original size %d"
                            % (header["expected_image_size"], avb["original_image_size"]))
        if avb["vbmeta_offset"] != avb["original_image_size"]:
            failures.append("vbmeta does not start at the original image end")
        if not avb["hash_descriptors"]:
            failures.append("missing AVB hash descriptor")
        else:
            descriptor = avb["hash_descriptors"][0]
            if descriptor["partition_name"] != role:
                failures.append("hash descriptor partition %r != %r" % (descriptor["partition_name"], role))
            if descriptor["image_size"] != avb["original_image_size"]:
                failures.append("hash descriptor image size differs from the footer")
            if descriptor["hash_algorithm"] != "sha256":
                failures.append("hash algorithm %r != sha256" % descriptor["hash_algorithm"])
        if avb["properties"].get(FINGERPRINT_PROPERTY[role]) != expected_fingerprint:
            failures.append("AVB %s fingerprint property differs" % role)
        gates["boot_headers"][role] = header
        gates["avb"][role] = avb
        gates["images"][role] = {"failures": failures}
        if failures:
            raise ValueError("AVB/v1 gate failed for " + role + ": " + "; ".join(failures))
    ramdisk_entries = cpio_entries(product / "ramdisk.img")
    missing = [name for name in MOUNT_POINTS if name not in ramdisk_entries]
    if missing:
        raise ValueError("boot ramdisk is missing mount points: " + ", ".join(missing))
    recovery_entries = cpio_entries(product / "ramdisk-recovery.img")
    if "system/etc/recovery.fstab" not in recovery_entries:
        raise ValueError("recovery ramdisk is missing system/etc/recovery.fstab")
    gates["ramdisks"] = {"boot_entries": len(ramdisk_entries), "boot_mount_points": list(MOUNT_POINTS),
                         "recovery_entries": len(recovery_entries),
                         "recovery_fstab": "system/etc/recovery.fstab"}
    return gates


def assemble(producers, dependency_root, output_dir, identity_path, build_number,
             source_selection, mkimage="mkimage", fdtoverlay="fdtoverlay",
             debugfs="debugfs", simg2img="simg2img", dumpimage="dumpimage"):
    started = time.monotonic()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    identity = json.loads(Path(identity_path).read_text())
    verify_properties.validate_expected(identity)
    receipt = {"schema_version": 1, "status": "failed", "build_number": build_number,
               "product": "aosp_crux-userdebug", "started_at": datetime.now(timezone.utc).isoformat()}
    delivery = output_dir / "delivery"
    if delivery.exists():
        shutil.rmtree(delivery)
    delivery.mkdir()
    try:
        directories = [Path(dependency_root) / str(index) for index in range(len(producers))]
        native_receipts = []
        bindings = []
        for index, producer in enumerate(producers):
            native = worker.read_json(directories[index] / "receipt.json")
            if native.get("status") != "success":
                raise ValueError("producer did not succeed: " + producer["id"])
            if native.get("id") != producer["id"]:
                raise ValueError("producer receipt id differs: " + producer["id"])
            if str(native.get("run_id")) != str(producer["run_id"]):
                raise ValueError("producer run differs: " + producer["id"])
            if native.get("worker_commit") != producer["expected_worker_commit"]:
                raise ValueError("producer worker commit differs: " + producer["id"])
            if native.get("manifest_sha256") != producer["expected_manifest_sha256"]:
                raise ValueError("producer manifest differs: " + producer["id"])
            native_receipts.append(native)
            bindings.append({"id": producer["id"], "run_id": producer["run_id"],
                             "artifact": producer["artifact"],
                             "expected_worker_commit": producer["expected_worker_commit"],
                             "expected_manifest_sha256": producer["expected_manifest_sha256"]})
        source_roots = {receipt.get("source_root") for receipt in native_receipts}
        out_roots = {receipt.get("out_root") for receipt in native_receipts}
        if len(source_roots) != 1 or len(out_roots) != 1:
            raise ValueError("producers must share one source_root and out_root")
        source_root = Path(next(iter(source_roots)))
        out_root = Path(next(iter(out_roots)))
        product = out_root / "target/product/crux"
        accepted, _ = worker.merge_dependencies(directories, bindings, source_root, out_root)
        for name in REQUIRED_IMAGES:
            spec = accepted.get(str(product / name))
            if spec is None or spec["type"] != "file" or spec["size"] <= 0:
                raise ValueError("required image absent: " + name)
            worker.verify_spec(spec)
        identity_report = validate_identity_files(identity, product, out_root)
        if identity_report["errors"]:
            raise ValueError("frontend identity files differ: " + json.dumps(identity_report["errors"])[:400])
        thumbprint_path = output_dir / "build_thumbprint.txt"
        write_thumbprint(identity, thumbprint_path)
        gates = gate_images(product, identity, thumbprint_path, debugfs, simg2img, dumpimage, out_root)
        fit_stage = output_dir / "fit-stage"
        if fit_stage.exists():
            shutil.rmtree(fit_stage)
        packaged = package_fits.prepare(accepted, native_receipts, fit_stage, product,
                                        product / "obj/KERNEL_OBJ", source_selection, True,
                                        mkimage, fdtoverlay)
        if packaged["status"] != "success" or not packaged["fits_generated"]:
            raise ValueError("FIT packaging failed: " + packaged.get("error", "unknown"))
        fits_dir = fit_stage / "fits"
        fit_reports = {}
        for name in DELIVERY_FITS:
            fit = fits_dir / name
            report = fit_listing(dumpimage, fit)
            for expected in ("Kernel Image", "RAMDisk Image", "Flat Device Tree"):
                if expected not in report["types"]:
                    raise ValueError(name + " lacks " + expected)
            report.update({"file_size": fit.stat().st_size, "sha256": worker.digest(fit)})
            fit_reports[name] = report
        validation = worker.read_json(fits_dir / "fit-validation.json")
        for mode, name in (("rom", "rom-cepheus.itb"), ("recovery", "recovery-cepheus.itb")):
            if validation["fits"][mode]["file_size"] != fit_reports[name]["file_size"]:
                raise ValueError("FIT size differs from the recipe validation: " + name)
        for name in REQUIRED_IMAGES:
            shutil.copy2(product / name, delivery / name)
        for name in DELIVERY_FITS:
            shutil.copy2(fits_dir / name, delivery / name)
        record = {
            "schema_version": 1, "status": "success", "build_number": build_number,
            "product": "aosp_crux-userdebug",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "identity": {"source": str(identity_path), "sha256": worker.digest(identity_path),
                         "frontend_thumbprint": identity["frontend_thumbprint"]},
            "producers": producers,
            "source_selection": source_selection,
            "delivery": {path.name: {"size": path.stat().st_size, "sha256": worker.digest(path)}
                         for path in sorted(delivery.iterdir()) if path.is_file() and path.name != "SHA256SUMS"},
            "gates": gates,
            "fits": fit_reports,
            "fit_validation": validation,
            "fit_receipt": worker.read_json(fit_stage / "fit-packaging-receipt.json"),
            "boot_chain": {"loader": "U-Boot", "chain": ["ABL", "U-Boot", "PE ROM / PE Recovery FIT"]},
            "limitations": ["Host assembly only; no device launch or runtime acceptance.",
                            "The physical Recovery loader is a separate U-Boot v1 container, not generated here."],
        }
        (delivery / "delivery-record.json").write_text(json.dumps(record, indent=2) + "\n")
        (output_dir / "delivery-record.json").write_text(json.dumps(record, indent=2) + "\n")
        sums = []
        for path in sorted(delivery.iterdir()):
            if path.is_file() and path.name != "SHA256SUMS":
                sums.append(worker.digest(path) + "  " + path.name)
        (delivery / "SHA256SUMS").write_text("\n".join(sums) + "\n")
        receipt.update(status="success", delivery=str(delivery),
                       assets={key: value["sha256"] for key, value in record["delivery"].items()})
        print(json.dumps({"status": "success", "delivery": str(delivery),
                          "assets": len(record["delivery"]),
                          "rom_fit": record["fits"]["rom-cepheus.itb"]["sha256"][:16],
                          "recovery_fit": record["fits"]["recovery-cepheus.itb"]["sha256"][:16]}, indent=2))
    except Exception as error:
        receipt["error"] = str(error)
        print("ERROR: " + str(error))
    finally:
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 1)
        (output_dir / "assembly-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer-runs", required=True)
    parser.add_argument("--dependency-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--build-number", required=True)
    parser.add_argument("--source-selection", required=True)
    parser.add_argument("--mkimage", default="mkimage")
    parser.add_argument("--fdtoverlay", default="fdtoverlay")
    parser.add_argument("--debugfs", default="debugfs")
    parser.add_argument("--simg2img", default="simg2img")
    parser.add_argument("--dumpimage", default="dumpimage")
    args = parser.parse_args()
    producers = worker.read_json(args.producer_runs)
    source_selection = worker.read_json(args.source_selection)
    receipt = assemble(producers, args.dependency_root, args.output_dir, args.identity,
                       args.build_number, source_selection, args.mkimage, args.fdtoverlay,
                       args.debugfs, args.simg2img, args.dumpimage)
    return 0 if receipt["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
