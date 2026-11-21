#!/usr/bin/env python3
"""Package a new U-Boot body into the physical Recovery v1 container.

Recipe of the accepted 2026-10-06 container (header-v1-rdtbo-zero): an Android
boot image v1 header (header size 1648, zero Recovery DTBO, legacy DT-zero ID)
whose kernel is gzip -9 u-boot-nodtb.bin with u-boot.dtb appended, spliced over
the start of the existing 128 MiB container, with the existing AVB NONE vbmeta
and footer kept and only the hash descriptor digest recomputed over the same
image size. Host only: writes a new output directory, never a device.

The output holds recovery.img, the deployment patch files for every changed
4096-byte block range and container.json with digests and LUN0 LBAs.
"""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib

WORKSPACE = Path(__file__).resolve().parents[2]
MKBOOTIMG = WORKSPACE / "tools/android-tools/mkbootimg.py"
BASE_SHA256 = "aa5dfe7599c587683fe861a04b30f1cd25dc838a4184be9d76530f28e45fbca5"
RECOVERY_LBA = 0x28000
PARTITION_BYTES = 128 << 20
BLOCK = 4096
MKBOOTIMG_ARGS = ["--base", "0", "--kernel_offset", "0x8000", "--ramdisk_offset", "0x1000000",
                  "--second_offset", "0xf00000", "--tags_offset", "0x100", "--pagesize", "4096",
                  "--header_version", "1"]


def require(condition, message):
    if not condition:
        raise SystemExit("package-recovery-container: " + message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def crc(data):
    return f"{zlib.crc32(data) & 0xffffffff:08x}"


def header(data):
    fields = struct.unpack_from("<8s10I16s", data)
    return {"magic": fields[0], "kernel_size": fields[1], "kernel_addr": fields[2],
            "ramdisk_size": fields[3], "ramdisk_addr": fields[4], "second_size": fields[5],
            "second_addr": fields[6], "tags_addr": fields[7], "page_size": fields[8],
            "header_version": fields[9], "os_version": fields[10], "name": fields[11],
            "cmdline": data[64:576], "id": data[576:608], "extra_cmdline": data[608:1632],
            "recovery_dtbo_size": struct.unpack_from("<I", data, 1632)[0],
            "recovery_dtbo_offset": struct.unpack_from("<Q", data, 1636)[0],
            "header_size": struct.unpack_from("<I", data, 1644)[0]}


def avb_hash_descriptor(image):
    """Locate the single hash descriptor of the NONE vbmeta named by the footer."""
    footer = image[-64:]
    require(footer[:4] == b"AVBf", "no AVB footer")
    original, vbmeta_offset, vbmeta_size = struct.unpack_from(">QQQ", footer, 12)
    vbmeta = image[vbmeta_offset:vbmeta_offset + vbmeta_size]
    require(vbmeta[:4] == b"AVB0", "no vbmeta at the footer offset")
    auth_size, aux_size, algorithm = struct.unpack_from(">QQI", vbmeta, 12)
    descriptors_offset, descriptors_size = struct.unpack_from(">QQ", vbmeta, 96)
    require(algorithm == 0 and auth_size == 0, "vbmeta is not the NONE schema")
    position = vbmeta_offset + 256 + auth_size + descriptors_offset
    end = position + descriptors_size
    found = []
    while position < end:
        tag, following = struct.unpack_from(">QQ", image, position)
        if tag == 2:
            image_size = struct.unpack_from(">Q", image, position + 16)[0]
            algo = image[position + 24:position + 56].rstrip(b"\0")
            name_len, salt_len, digest_len = struct.unpack_from(">III", image, position + 56)
            name_at = position + 132
            salt_at = name_at + name_len
            digest_at = salt_at + salt_len
            found.append({"image_size": image_size, "algorithm": algo.decode(),
                          "name": image[name_at:salt_at].decode(), "salt": image[salt_at:digest_at],
                          "digest_offset": digest_at, "digest_len": digest_len})
        position += 16 + following
    require(len(found) == 1 and found[0]["algorithm"] == "sha256" and found[0]["digest_len"] == 32 and
            found[0]["name"] == "recovery" and found[0]["image_size"] == original,
            "unexpected AVB hash descriptor set")
    return found[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uboot-nodtb", type=Path, required=True)
    parser.add_argument("--uboot-dtb", type=Path, required=True)
    parser.add_argument("--base-container", type=Path, required=True,
                        help="the deployed 128 MiB container (SHA-256 aa5dfe75...)")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    base = args.base_container.read_bytes()
    require(len(base) == PARTITION_BYTES and sha256(base) == BASE_SHA256, "base container differs")
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    nodtb = args.uboot_nodtb.read_bytes()
    dtb = args.uboot_dtb.read_bytes()
    require(dtb[:4] == bytes.fromhex("d00dfeed"), "u-boot.dtb is not a DT")
    body = gzip.compress(nodtb, compresslevel=9, mtime=0) + dtb
    (out / "uboot-body.bin").write_bytes(body)
    with tempfile.TemporaryDirectory() as directory:
        prefix_path = Path(directory) / "prefix.img"
        subprocess.run([sys.executable, str(MKBOOTIMG), "--kernel", str(out / "uboot-body.bin"),
                        *MKBOOTIMG_ARGS, "--output", str(prefix_path)], check=True)
        prefix = bytearray(prefix_path.read_bytes())
    # mkbootimg zeroes the ramdisk/second addresses when their sizes are zero;
    # the accepted container (like the Boot image) keeps the offset values.
    struct.pack_into("<I", prefix, 20, 0x01000000)
    struct.pack_into("<I", prefix, 28, 0x00f00000)
    prefix = bytes(prefix)
    old, new = header(base), header(prefix)
    require(new["header_version"] == 1 and new["header_size"] == 1648 and new["page_size"] == BLOCK and
            new["recovery_dtbo_size"] == 0 and new["recovery_dtbo_offset"] == 0 and
            new["kernel_size"] == len(body), "unexpected new v1 header")
    for key in ("magic", "kernel_addr", "ramdisk_size", "ramdisk_addr", "second_size", "second_addr",
                "tags_addr", "page_size", "header_version", "os_version", "name", "cmdline",
                "extra_cmdline", "recovery_dtbo_size", "recovery_dtbo_offset", "header_size"):
        require(old[key] == new[key], f"v1 header field {key} differs from the accepted container")
    require(len(prefix) % BLOCK == 0 and prefix[BLOCK:BLOCK + len(body)] == body, "prefix layout differs")
    descriptor = avb_hash_descriptor(base)
    require(len(prefix) < descriptor["image_size"], "new prefix reaches the hashed image end")
    old_digest = base[descriptor["digest_offset"]:descriptor["digest_offset"] + 32]
    require(hashlib.sha256(descriptor["salt"] + base[:descriptor["image_size"]]).digest() == old_digest,
            "base container AVB digest does not verify")
    image = bytearray(base)
    image[:len(prefix)] = prefix
    digest = hashlib.sha256(descriptor["salt"] + bytes(image[:descriptor["image_size"]])).digest()
    image[descriptor["digest_offset"]:descriptor["digest_offset"] + 32] = digest
    image = bytes(image)
    (out / "recovery.img").write_bytes(image)
    changed = [index for index in range(PARTITION_BYTES // BLOCK)
               if image[index * BLOCK:(index + 1) * BLOCK] != base[index * BLOCK:(index + 1) * BLOCK]]
    ranges = []
    for index in changed:
        if ranges and ranges[-1][1] == index:
            ranges[-1][1] = index + 1
        else:
            ranges.append([index, index + 1])
    patches = []
    for start, end in ranges:
        data = image[start * BLOCK:end * BLOCK]
        name = f"patch-{start:04x}-{end - start:x}.bin"
        rollback = f"rollback-{start:04x}-{end - start:x}.bin"
        (out / name).write_bytes(data)
        (out / rollback).write_bytes(base[start * BLOCK:end * BLOCK])
        patches.append({"relative_block": hex(start), "blocks": hex(end - start),
                        "lba": hex(RECOVERY_LBA + start), "patch": name, "patch_crc32": crc(data),
                        "patch_sha256": sha256(data), "rollback": rollback,
                        "rollback_crc32": crc(base[start * BLOCK:end * BLOCK])})
    vbmeta_offset = struct.unpack_from(">Q", image[-64:], 20)[0]
    record = {"schema_version": 1, "recipe": "physical Recovery v1 container, header-v1-rdtbo-zero",
              "base_sha256": BASE_SHA256, "uboot_nodtb_sha256": sha256(nodtb), "uboot_dtb_sha256": sha256(dtb),
              "body_bytes": len(body), "body_sha256": sha256(body), "prefix_bytes": len(prefix),
              "prefix_sha256": sha256(prefix), "v1_id": new["id"].hex(),
              "avb_image_size": descriptor["image_size"], "avb_salt": descriptor["salt"].hex(),
              "avb_digest": digest.hex(), "vbmeta_sha256": sha256(image[vbmeta_offset:vbmeta_offset + 512]),
              "container_bytes": len(image), "container_sha256": sha256(image), "container_crc32": crc(image),
              "changed_ranges": patches, "lun": 0, "partition_lba": hex(RECOVERY_LBA),
              "partition_blocks": hex(PARTITION_BYTES // BLOCK), "device_writes": False}
    (out / "container.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
