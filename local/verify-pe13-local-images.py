"""Check a local PE13 B candidate from real VM image bytes before packaging it.

    verify-pe13-local-images.py WORKSPACE PRODUCT_OUT PAYLOAD BUILD_NUMBER MARKER

Run inside cruxbuild (debugfs, the same-OUT product directory). Prints the
image identity record (image-validation.json) on stdout. Generalized from the
accepted 2026110502 checker; the fingerprint tail must be the build number.
"""

import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import struct
import subprocess
import sys


workspace, product, payload = map(Path, sys.argv[1:4])
number, expected_marker = sys.argv[4:6]
sys.dont_write_bytecode = True
sys.path.insert(0, str(workspace / "crux-pe13-distributed/scripts"))
from assemble_dev import FINGERPRINT_PROPERTY, parse_android_boot, parse_avb  # noqa: E402
from package_fits import extract_android_ramdisk, validate_lto_initcall_order  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "local_fit", workspace / "crux-pe13-distributed/references/pe13-boot-contracts/package-pe13.py")
fit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fit)


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def debugfs(image, path):
    result = subprocess.run(["debugfs", "-R", "cat " + path, str(payload / image)],
                            check=True, capture_output=True)
    require(result.stdout, f"{image}:{path}: no bytes read ({result.stderr.decode().strip()})")
    return result.stdout


def properties(image, path):
    return dict(line.split("=", 1) for line in debugfs(image, path).decode().splitlines()
                if "=" in line and not line.startswith("#"))


require(re.fullmatch(r"\d{10}", number), "build number is not 10 digits")
marker = debugfs("system.raw.img", "/system/etc/crux-delivery-verification.txt")
require(marker == (expected_marker + "\n").encode(), "wrong baked-in marker")
system = properties("system.raw.img", "/system/build.prop")
vendor = properties("vendor.raw.img", "/build.prop")
for actual, expected in (
    (system, {"ro.build.version.incremental": number, "ro.build.type": "userdebug",
              "ro.build.product": "crux", "ro.product.system.name": "aosp_crux",
              "ro.adb.secure": "0", "ro.adb.secure.recovery": "0", "persist.sys.usb.config": "adb"}),
    (vendor, {"ro.vendor.build.version.incremental": number, "ro.vendor.build.type": "userdebug",
              "ro.product.vendor.name": "aosp_crux", "ro.product.vendor.device": "crux"}),
):
    for key, value in expected.items():
        require(actual.get(key) == value, f"{key}: {actual.get(key)!r} != {value!r}")
fingerprint = system["ro.build.fingerprint"]
# The exported BUILD_FINGERPRINT replaces PE's UTC HHMM tail, as in the cloud freeze.
require(re.fullmatch(rf"Xiaomi/crux/crux:13/[^/]+/{number}:userdebug/test-keys", fingerprint) and
        fingerprint == (product / "build_fingerprint.txt").read_text().strip(),
        f"system fingerprint differs from the exported build identity: {fingerprint}")
require(system["ro.system.build.fingerprint"] == fingerprint, "system fingerprint split")
require(vendor["ro.vendor.build.fingerprint"] == fingerprint, "vendor fingerprint split")
vendor_fstab = debugfs("vendor.raw.img", "/etc/fstab.crux_uboot")
require(debugfs("vendor.raw.img", "/etc/fstab.qcom") == vendor_fstab, "vendor PE fstab aliases differ")
fstab = fit.validate_dualboot_fstab(vendor_fstab)

inputs = payload / "fit-inputs"
kernel = (product / "kernel").read_bytes()
native = (inputs / "Image").read_bytes()
require(kernel.startswith(b"UNCOMPRESSED_IMG") and
        struct.unpack_from("<I", kernel, 16)[0] == len(native) and
        kernel[20:20 + len(native)] == native, "native Image not paired with Android's Image-dtb")
report = {"build_number": number, "fingerprint": fingerprint, "marker": marker.decode().strip(),
          "system_properties": system, "vendor_properties": vendor, "vendor_pe_fstab": fstab,
          "native_image_matches_android_kernel": True, "android_images": {}}

for role, standalone in (("boot", "ramdisk.img"), ("recovery", "ramdisk-recovery.img")):
    path = payload / f"{role}.img"
    data = path.read_bytes()
    header = parse_android_boot(path)
    avb = parse_avb(path)
    require(header["header_version"] == 1 and header["header_size"] == 1648 and
            header["page_size"] == 4096 and header["ramdisk_gzip"], f"{role}: wrong v1 header")
    require(header["expected_image_size"] == avb["original_image_size"] == avb["vbmeta_offset"],
            f"{role}: Android image and AVB disagree about original size")
    require(data[header["page_size"]:header["page_size"] + header["kernel_size"]] == kernel,
            f"{role}: kernel differs from same-OUT Image-dtb")
    descriptor, = avb["hash_descriptors"]
    require(descriptor["partition_name"] == role and descriptor["hash_algorithm"] == "sha256" and
            descriptor["image_size"] == avb["original_image_size"], f"{role}: invalid AVB hash descriptor")
    digest = hashlib.sha256(bytes.fromhex(descriptor["salt_hex"]) + data[:descriptor["image_size"]])
    require(digest.hexdigest() == descriptor["digest_hex"], f"{role}: AVB digest disagrees")
    require(avb["properties"].get(FINGERPRINT_PROPERTY[role]) == fingerprint,
            f"{role}: AVB fingerprint differs from installed system properties")
    ramdisk, extraction = extract_android_ramdisk(data)
    require(ramdisk == (inputs / standalone).read_bytes(), f"{role}: standalone ramdisk differs")
    files, modes = fit.cpio_files(gzip.decompress(ramdisk), include_modes=True)
    init = fit.resolve_cpio_path("init", files, modes)
    require(b"crux-pe-storage-guard:" in files[init], f"{role}: native PE storage guard not found")
    ramdisk_report = fit.validate_ramdisk(ramdisk, False, role, dualboot=True, development_adb=True)
    report["android_images"][role] = {"header": header, "avb": avb, "ramdisk_extraction": extraction,
                                      "ramdisk": ramdisk_report, "native_storage_guard_present": True}

symbols = payload / "kernel-symbols"
require((symbols / "kernel.release").read_text().strip() in (symbols / "utsrelease.h").read_text(),
        "kernel release metadata differs")
lto = symbols / ".tmp_lto.lds"
if lto.is_file():
    report["lto_initcall_order"] = validate_lto_initcall_order(lto.read_text())
print(json.dumps(report, indent=2))
