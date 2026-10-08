#!/usr/bin/env python3
"""Prepare fresh kernel symbols and the maintained Crux U-Boot FIT packages."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import zlib

import worker


REPOSITORY = Path(__file__).resolve().parents[1]
REFERENCES = REPOSITORY / "references/pe13-boot-contracts"
RECIPE = REFERENCES / "package-pe13.py"
BASES = ("sm8150", "sm8150-v2", "sm8150p", "sm8150p-v2")


def load_recipe(path=RECIPE):
    spec = importlib.util.spec_from_file_location("crux_pe13_recipe", path)
    recipe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recipe)
    return recipe


def producer_key(receipt):
    return (str(receipt.get("run_id", "")), receipt["id"], receipt["manifest_sha256"], receipt["archive_sha256"])


def fresh_inputs(accepted, receipts, paths):
    owners = {}
    for receipt in receipts:
        if receipt.get("status") != "success":
            raise ValueError("FIT input producer did not succeed: " + receipt.get("id", "unknown"))
        key = producer_key(receipt)
        for output in receipt["outputs"]:
            if output["path"] in accepted and output == accepted[output["path"]]:
                owners.setdefault(output["path"], set()).add(key)
    for path in paths:
        spec = accepted.get(str(path))
        if spec is None or spec["type"] != "file" or spec["size"] <= 0:
            raise ValueError("Missing fresh regular FIT/symbol input: " + str(path))
        worker.verify_spec(spec)
        if str(path) not in owners:
            raise ValueError("FIT/symbol input has no successful producer: " + str(path))
    return owners


def extract_crux_overlay(data, recipe):
    """Read Android 13's big-endian DT table and select Crux by DT metadata."""
    # Android 13 libufdt utils/src/dt_table.{h,c} defines the table fields,
    # network byte order, and version-1 compression flags.
    if len(data) < 32:
        raise ValueError("Truncated Android DTBO table")
    magic, total, header, entry_size, count, offset, _, version = struct.unpack_from(">8I", data)
    if (magic != 0xD7B7AB1E or not 32 <= header <= offset <= total <= len(data)
            or entry_size < 32 or offset + entry_size * count > total or version not in (0, 1)):
        raise ValueError("Invalid or unsupported Android DTBO table")
    selected = {}
    for index in range(count):
        values = struct.unpack_from(">8I", data, offset + index * entry_size)
        size, start = values[:2]
        if start < offset + entry_size * count or size == 0 or start + size > total:
            raise ValueError("Android DTBO entry outside its table")
        payload = data[start:start + size]
        compression = values[4] & 15 if version == 1 else 0
        if compression == 1:
            payload = zlib.decompress(payload)
        elif compression == 2:
            payload = gzip.decompress(payload)
        elif compression != 0:
            raise ValueError("Unsupported Android DTBO entry compression")
        props = recipe.Fdt(payload).root["props"]
        if (props.get("model") == b"SDX50M CRUX\0" and
                recipe.cells(props.get("qcom,board-id", b"")) == (0x2B, 1) and
                b"qcom,sm8150\0" in props.get("compatible", b"")):
            selected[payload] = {"entry_index": index, "table_version": version,
                                 "sha256": hashlib.sha256(payload).hexdigest()}
    if len(selected) != 1:
        raise ValueError("Android DTBO table must contain one distinct Crux overlay")
    payload, selection = next(iter(selected.items()))
    return payload, selection


def extract_android_ramdisk(data):
    """Extract the gzip/newc ramdisk from Crux's native Android v0/v1/v2 image."""
    if len(data) < 48 or data[:8] != b"ANDROID!":
        raise ValueError("Native Android image lacks its boot header")
    kernel_size, ramdisk_size = struct.unpack_from("<I", data, 8)[0], struct.unpack_from("<I", data, 16)[0]
    page_size, version = struct.unpack_from("<2I", data, 36)
    header_size = {0: 1632, 1: 1648, 2: 1660}.get(version)
    if header_size is None or page_size < header_size or page_size & (page_size - 1):
        raise ValueError("Unsupported or invalid native Android image header")
    if len(data) < page_size or kernel_size == 0 or ramdisk_size == 0:
        raise ValueError("Native Android image has no complete kernel/ramdisk")
    if version and struct.unpack_from("<I", data, 1644)[0] != header_size:
        raise ValueError("Native Android image header size is incoherent")
    offset = page_size + ((kernel_size + page_size - 1) // page_size) * page_size
    if offset + ramdisk_size > len(data):
        raise ValueError("Native Android ramdisk lies outside its image")
    payload = data[offset:offset + ramdisk_size]
    return payload, {"header_version": version, "page_size": page_size, "offset": offset,
                     "size": ramdisk_size, "sha256": hashlib.sha256(payload).hexdigest()}


def relative_archive(directory, archive_path):
    with open(archive_path, "wb") as output:
        process = subprocess.Popen(["zstd", "-T0", "-6", "-q", "-c"], stdin=subprocess.PIPE, stdout=output)
        try:
            with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                for child in sorted(Path(directory).iterdir()):
                    archive.add(child, arcname=child.name, recursive=True)
            process.stdin.close()
            if process.wait() != 0:
                raise ValueError("FIT/symbol archive compression failed")
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
            process.wait()


def prepare(accepted, receipts, output_dir, product_dir=None, kernel_dir=None,
            source_selection=None, build_fits=True, mkimage="mkimage", fdtoverlay="fdtoverlay",
            recipe_path=RECIPE, reference_dir=REFERENCES):
    """Consume already-restored successful worker receipts; never use ambient OUT files."""
    started = time.monotonic()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    result = {"schema_version": 1, "status": "failed", "started_at": datetime.now(timezone.utc).isoformat(),
              "hardware_verified": False, "physical_recovery_container_generated": False,
              "fits_generated": False, "build_profile": "aosp_crux-userdebug"}
    with open(output / "fit-packaging.log", "w", buffering=1) as log:
        try:
            if product_dir is None:
                roots = {receipt.get("out_root") for receipt in receipts}
                if len(roots) != 1 or None in roots:
                    raise ValueError("FIT producers must declare one actual out_root")
                product = Path(next(iter(roots))) / "target/product/crux"
            else:
                product = Path(product_dir)
            kernel = Path(kernel_dir) if kernel_dir else product / "obj/KERNEL_OBJ"
            qcom = kernel / "arch/arm64/boot/dts/qcom"
            image = kernel / "arch/arm64/boot/Image"
            symbols = [kernel / name for name in ("vmlinux", "System.map", ".config")]
            bases = [qcom / (name + ".dtb") for name in BASES]
            ramdisk_names = ("ramdisk.img", "ramdisk-recovery.img")
            ramdisks = [product / name if str(product / name) in accepted else product / image_name
                        for name, image_name in zip(ramdisk_names, ("boot.img", "recovery.img"))]
            core = [image, *symbols, *bases]
            owners = fresh_inputs(accepted, receipts, [*core, *ramdisks])
            kernel_producers = set.intersection(*(owners[str(path)] for path in core))
            if not kernel_producers:
                raise ValueError("Image, all four DTBs and symbols must share one successful kernel build")
            primary = sorted(kernel_producers)[0]
            recipe = load_recipe(recipe_path)
            image_report = recipe.validate_image(image.read_bytes())
            with symbols[0].open("rb") as stream:
                elf = stream.read(20)
            if len(elf) != 20 or elf[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", elf, 18)[0] != 183:
                raise ValueError("vmlinux is not the matching ARM64 ELF symbol image")
            for name, path in zip(BASES, bases):
                if recipe.cells(recipe.read_tree(path).root["props"]["qcom,msm-id"]) != recipe.BASE_IDS[name]:
                    raise ValueError("Wrong fresh DTB base identity: " + name)
            ramdisk_data, ramdisk_reports, ramdisk_sources = {}, {}, {}
            for role, name, path in zip(("boot", "recovery"), ramdisk_names, ramdisks):
                data = path.read_bytes()
                provenance = {"source_path": str(path), "source_sha256": accepted[str(path)]["sha256"],
                              "extracted_from_android_image": path.name != name}
                if path.name != name:
                    data, extraction = extract_android_ramdisk(data)
                    provenance.update(extraction)
                ramdisk_data[name] = data
                ramdisk_sources[role] = provenance
                ramdisk_reports[role] = recipe.validate_ramdisk(data, False, role, dualboot=True, development_adb=True)
            raw_overlay = qcom / "crux-sm8150-overlay.dtbo"
            overlay_source = raw_overlay if str(raw_overlay) in accepted else product / "dtbo.img"
            overlay_owners = fresh_inputs(accepted, receipts, [overlay_source])
            if primary not in overlay_owners[str(overlay_source)]:
                derived = [receipt for receipt in receipts if producer_key(receipt) in overlay_owners[str(overlay_source)]]
                if not any(any((dependency.get("id"), dependency.get("manifest_sha256"), dependency.get("archive_sha256"))
                               == primary[1:] for dependency in receipt.get("dependencies", [])) for receipt in derived):
                    raise ValueError("Crux overlay is not from or derived from the paired kernel build")
            overlay_data = overlay_source.read_bytes()
            overlay_report = {"source_path": str(overlay_source), "source_sha256": accepted[str(overlay_source)]["sha256"],
                              "extracted_from_android_dtbo_table": overlay_source != raw_overlay}
            if overlay_source != raw_overlay:
                overlay_data, selection = extract_crux_overlay(overlay_data, recipe)
                overlay_report.update(selection)
            props = recipe.Fdt(overlay_data).root["props"]
            if (props.get("model") != b"SDX50M CRUX\0" or recipe.cells(props.get("qcom,board-id", b"")) != (0x2B, 1)
                    or b"qcom,sm8150\0" not in props.get("compatible", b"")):
                raise ValueError("Fresh overlay does not identify the Crux board")
            for name in ("fit-inputs", "kernel-symbols", "fits", "kernel-symbols.tar.zst", "fit-inputs.tar.zst", "uboot-fits.tar.zst"):
                if (output / name).exists():
                    raise ValueError("Choose a fresh FIT output directory: " + str(output / name))
            inputs = output / "fit-inputs"
            symbols_dir = output / "kernel-symbols"
            inputs.mkdir(); symbols_dir.mkdir()
            (inputs / "base-dtbs").mkdir()
            shutil.copy2(image, inputs / image.name)
            for name, path in zip(ramdisk_names, ramdisks):
                if path.name == name:
                    shutil.copy2(path, inputs / name)
                else:
                    (inputs / name).write_bytes(ramdisk_data[name])
            for path in bases:
                shutil.copy2(path, inputs / "base-dtbs" / path.name)
            (inputs / "crux-sm8150-overlay.dtbo").write_bytes(overlay_data)
            (inputs / "README.md").write_text("# Crux PE13 U-Boot FIT inputs\n\nThese raw kernel, DTB/overlay and native ramdisk inputs come from successful distributed build receipts in packaging-manifest.json. The maintained recipe applies the Crux overlay to all four source bases and creates separate native ROM and PE Recovery FITs.\n\nThe launch chain is ABL to U-Boot to PE. Android boot.img and recovery.img remain intermediate build images. The physical Recovery loader is a separate U-Boot Android container with its verified v1 header; this package does not recreate it. Every debug kernel handoff must verify the watchdog@17c10000 30-second guard and use run boot_go. No phone operation or runtime acceptance is performed by this package.\n")
            for path in [image, *symbols]:
                shutil.copy2(path, symbols_dir / path.name)
            release = kernel / "include/config/kernel.release"
            if str(release) in accepted:
                release_owners = fresh_inputs(accepted, receipts, [release])
                if primary not in release_owners[str(release)]:
                    raise ValueError("kernel.release does not belong to the paired kernel build")
                shutil.copy2(release, symbols_dir / "kernel.release")
            manifest = {"schema_version": 1, "kernel_producer": {"run_id": primary[0], "id": primary[1],
                         "manifest_sha256": primary[2], "archive_sha256": primary[3]},
                        "inputs": {str(path): accepted[str(path)] for path in [*core, *ramdisks, overlay_source]},
                        "kernel": image_report, "overlay": overlay_report, "ramdisks": ramdisk_reports,
                        "ramdisk_sources": ramdisk_sources,
                        "source_selection": source_selection, "recipe_sha256": worker.digest(recipe_path),
                        "boot_chain": ["ABL", "U-Boot", "PE ROM / PE Recovery FIT"],
                        "physical_recovery": {"separate_uboot_container": True, "required_header_version": 1,
                                              "generated_here": False},
                        "watchdog": {"device": "watchdog@17c10000", "timeout_ms": 30000,
                                     "handoff": "run boot_go", "actual_handoff_verification_required": True},
                        "hardware_verified": False, "fits_generated": False}
            (symbols_dir / "README.md").write_text("# Fresh Crux PE13 kernel symbols\n\nKeep Image, vmlinux, System.map and .config paired with the same kernel producer and Image SHA256 in kernel-build.json. These symbols belong to this fresh build. Runtime/device validation is separate.\n")
            if build_fits:
                manifest["host_tools"] = {}
                for name, executable, flag in (("mkimage", mkimage, "-V"), ("fdtoverlay", fdtoverlay, "--version")):
                    version = subprocess.run([str(executable), flag], check=True, text=True,
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    manifest["host_tools"][name] = {"executable": str(executable), "version": version.stdout.strip()}
                command = [sys.executable, str(recipe_path), "--image", str(inputs / "Image"),
                           "--base-dir", str(inputs / "base-dtbs"), "--overlay", str(inputs / "crux-sm8150-overlay.dtbo"),
                           "--boot-ramdisk", str(inputs / "ramdisk.img"), "--recovery-ramdisk", str(inputs / "ramdisk-recovery.img"),
                           "--dualboot", "--development-adb", "--reference-dir", str(reference_dir),
                           "--output-dir", str(output / "fits"), "--mkimage", str(mkimage), "--fdtoverlay", str(fdtoverlay)]
                print(json.dumps(command), file=log)
                subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
                validation = worker.read_json(output / "fits/fit-validation.json")
                if set(validation["fits"]) != {"rom", "recovery"}:
                    raise ValueError("Maintained recipe did not validate both ROM and Recovery FITs")
                for mode in ("rom", "recovery"):
                    fit = output / "fits" / (mode + "-cepheus.itb")
                    if not fit.is_file() or fit.stat().st_size <= 0 or fit.stat().st_size != validation["fits"][mode]["file_size"]:
                        raise ValueError("Maintained recipe did not produce its validated FIT: " + mode)
                    loads = validation["fits"][mode]["loads"]
                    ramdisk_name = "ramdisk.img" if mode == "rom" else "ramdisk-recovery.img"
                    portable = recipe.its_text(mode, "../fit-inputs/Image", mode + "-cepheus.dtb",
                                               "../fit-inputs/" + ramdisk_name, loads["fdt"], loads["ramdisk"],
                                               "development-adb")
                    (output / "fits" / (mode + "-cepheus.its")).write_text(portable)
                (output / "fits/README.md").write_text("# Crux PE13 U-Boot FITs\n\nrom-cepheus.itb and recovery-cepheus.itb were checked by the maintained recipe against the fresh kernel, final DT and native ramdisk bytes. Host verification does not establish device behavior.\n\nFor the saved ITS files, extract fit-inputs.tar.zst into a sibling fit-inputs directory and run mkimage from this fits directory. The physical Recovery loader remains a separate verified U-Boot v1 container. Any later debug launch requires the actual watchdog guard and run boot_go.\n")
                # ITS paths changed to portable sibling directories; update
                # only that archive's inventory after the intentional rewrite.
                inventory = [f"{worker.digest(path)}  {path.name}" for path in sorted((output / "fits").iterdir())
                             if path.is_file() and path.name != "FIT-SHA256SUMS"]
                (output / "fits/FIT-SHA256SUMS").write_text("\n".join(inventory) + "\n")
                manifest["fits_generated"] = True
                manifest["fit_validation"] = validation
                result["fits_generated"] = True
            (symbols_dir / "kernel-build.json").write_text(json.dumps(manifest, indent=2) + "\n")
            relative_archive(symbols_dir, output / "kernel-symbols.tar.zst")
            (inputs / "packaging-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            relative_archive(inputs, output / "fit-inputs.tar.zst")
            if build_fits:
                relative_archive(output / "fits", output / "uboot-fits.tar.zst")
            result.update(status="success", kernel_producer=manifest["kernel_producer"],
                          archives=[worker.describe(path) for path in output.glob("*.tar.zst")])
        except Exception as error:
            result["error"] = str(error)
            print("ERROR: " + str(error), file=log)
        finally:
            result["elapsed_seconds"] = time.monotonic() - started
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            (output / "fit-packaging-receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer-dir", action="append", required=True, help="Downloaded successful worker artifact directory")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--product-dir")
    parser.add_argument("--kernel-dir")
    parser.add_argument("--source-selection", default="manifests/source-revisions.json")
    parser.add_argument("--inputs-only", action="store_true")
    parser.add_argument("--mkimage", default="mkimage")
    parser.add_argument("--fdtoverlay", default="fdtoverlay")
    args = parser.parse_args()
    accepted, _ = worker.merge_dependencies(args.producer_dir)
    receipts = [worker.read_json(Path(path) / "receipt.json") for path in args.producer_dir]
    result = prepare(accepted, receipts, args.output_dir, args.product_dir, args.kernel_dir,
                     worker.read_json(args.source_selection), not args.inputs_only, args.mkimage, args.fdtoverlay)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
