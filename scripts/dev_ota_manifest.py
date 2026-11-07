#!/usr/bin/env python3
"""The signed crux-ota/1 phone manifest of one verified Dev delivery.

A phone release carries crux-ota-manifest.txt and crux-ota-manifest.sig beside
the eight standard delivery assets. The phone updater and the PE Recovery
installer read the manifest, verify the signature with the A release key and
install the delivery's own system.img.zst (zstd of the sparse image),
vendor.img (sparse) and rom-cepheus.itb with data kept. The manifest is derived
from the verified delivery bytes and three release inputs: the installed
builds allowed to update, the serial the FITs carry (or ``any``) and the
Recovery-container U-Boot role. The owner signs it offline; release-dev.yml
regenerates it and verifies the signature against keys/crux-ota-a-p256.pub
before the draft takes either file.

    dev_ota_manifest.py sign --assembly-run RUN --tag TAG --start-builds B[,B...]
        --serial SERIAL|any --key PRIVATE.pem --out NEW_DIR

`sign` repeats the release workflow's source verification (GH_TOKEN, or the
token of `gh auth token`), writes both files to NEW_DIR and prints the
signature to pass as the release workflow's ota_signature input.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

from dev_ota import DevOtaError, REPO_ROOT, PRODUCT, DEVICE, require

MANIFEST = "crux-ota-manifest.txt"
SIGNATURE = "crux-ota-manifest.sig"
PUBLIC_KEY = REPO_ROOT / "keys/crux-ota-a-p256.pub"
UBOOT_ROLE = "recovery-container-crux-install-1"
ROM_WINDOW_BYTES = 0x3100 * 4096
RAW_LIMITS = {"system": 0xF0000 * 4096, "vendor": 0x70000 * 4096}
BUILDS_RE = re.compile(r"[0-9]{10}(,[0-9]{10})*")
SERIAL_RE = re.compile(r"[0-9A-Za-z]{1,32}")
# SubjectPublicKeyInfo prefix of an uncompressed P-256 point.
P256_SPKI = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
# What the target system needs to take the next phone update itself.
UPDATE_STACK_KEY = "/system/etc/crux-ota/a-p256.pub"
UPDATE_STACK = ("/system/bin/crux-installer", "/system/etc/init/crux_ota.rc",
                "/system/system_ext/priv-app/CruxUpdater/CruxUpdater.apk",
                "/system/system_ext/etc/permissions/privapp-permissions-cruxupdater.xml")


def expand_sparse(stream, sink=None):
    """Expanded size and SHA-256 of an Android sparse v1.0 stream (DONT_CARE as zeros).

    With `sink` (a seekable binary file) the raw image is also written there.
    """
    def read(count):
        data = stream.read(count)
        require(len(data) == count, "truncated sparse image")
        return data

    digest = hashlib.sha256()
    zero = bytes(1 << 20)
    magic, major, _, file_header, chunk_header, block, blocks, chunks, _ = struct.unpack(
        "<IHHHHIIII", read(28))
    require(magic == 0xED26FF3A and major == 1 and block == 4096 and file_header >= 28
            and chunk_header >= 12, "not an Android sparse v1.0 image with 4096-byte blocks")
    read(file_header - 28)
    produced = 0
    for _ in range(chunks):
        kind, _, count, _ = struct.unpack("<HHII", read(12))
        read(chunk_header - 12)
        size = count * block
        if kind == 0xCAC1:
            left = size
            while left:
                data = read(min(left, 1 << 22))
                digest.update(data)
                if sink:
                    sink.write(data)
                left -= len(data)
        elif kind == 0xCAC2:
            fill = read(4) * (1 << 18)
            left = size
            while left:
                step = min(left, len(fill))
                digest.update(fill[:step])
                if sink:
                    sink.write(fill[:step])
                left -= step
        elif kind == 0xCAC3:
            left = size
            while left:
                step = min(left, len(zero))
                digest.update(zero[:step])
                left -= step
            if sink:
                sink.seek(size, os.SEEK_CUR)
        elif kind == 0xCAC4:
            read(4)
            size = 0
        else:
            raise DevOtaError(f"unknown sparse chunk type {kind:#06x}")
        produced += size
    require(produced == blocks * block, "sparse chunks do not cover the declared image size")
    if sink:
        sink.truncate(produced)
    return produced, digest.hexdigest()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest(system_zst, vendor, rom_fit, *, build, fingerprint, start_builds, serial,
             marker="none", source=(), uboot_role=UBOOT_ROLE):
    """The manifest bytes; `source` is a sequence of (key, value) provenance pairs."""
    require(re.fullmatch(r"[0-9]{10}", build), "target build is not 10 digits")
    require(fingerprint.endswith(f"/{build}:userdebug/test-keys"),
            "target fingerprint is not the Dev test-keys identity of the build")
    require(BUILDS_RE.fullmatch(start_builds), "start builds must be comma-separated 10-digit builds")
    require(serial == "any" or SERIAL_RE.fullmatch(serial), "serial must be alphanumeric or any")
    fit = Path(rom_fit).read_bytes()
    require(fit[:4] == bytes.fromhex("d00dfeed") and int.from_bytes(fit[4:8], "big") == len(fit),
            "ROM FIT is not one complete flattened image tree")
    require(len(fit) <= ROM_WINDOW_BYTES, "ROM FIT exceeds the 0x3100-block landing window")
    serials = {value.decode() for value in re.findall(rb"androidboot\.serialno=([0-9A-Za-z]+)", fit)}
    require(not serials if serial == "any" else serials <= {serial},
            f"ROM FIT serial identity {sorted(serials)} does not allow serial {serial}")
    with subprocess.Popen(["zstd", "-dc", "--long=27", str(system_zst)], stdout=subprocess.PIPE) as zstd:
        system = expand_sparse(zstd.stdout)
        require(not zstd.stdout.read(1), "system.img.zst has data after its sparse image")
    require(zstd.returncode == 0, "system.img.zst does not decompress")
    with Path(vendor).open("rb") as handle:
        vendor_raw = expand_sparse(handle)
        require(not handle.read(1), "vendor.img has data after its sparse image")
    for role, (size, _) in (("system", system), ("vendor", vendor_raw)):
        require(size <= RAW_LIMITS[role], f"{role} expands beyond its installable size")
    lines = ["schema=crux-ota/1", "kind=a-ota", f"device={DEVICE}", f"product={PRODUCT}",
             f"serialno={serial}", f"uboot_role={uboot_role}", "data_policy=preserve",
             f"target_build={build}", f"target_fingerprint={fingerprint}", f"target_marker={marker}",
             f"start_builds={start_builds}"]
    lines += [f"source.{key}={value}" for key, value in source]
    for role, path, kind, raw in (("system", system_zst, "zstd-sparse", system),
                                  ("vendor", vendor, "sparse", vendor_raw)):
        lines += [f"asset.{role}.name={Path(path).name}", f"asset.{role}.format={kind}",
                  f"asset.{role}.bytes={Path(path).stat().st_size}", f"asset.{role}.sha256={sha256(path)}",
                  f"asset.{role}.raw_bytes={raw[0]}", f"asset.{role}.raw_sha256={raw[1]}"]
    lines += [f"asset.rom_fit.name={Path(rom_fit).name}", f"asset.rom_fit.bytes={len(fit)}",
              f"asset.rom_fit.sha256={hashlib.sha256(fit).hexdigest()}"]
    require(all(re.fullmatch(r"[a-z0-9_.]{1,63}=[^\n]{1,255}", line) for line in lines),
            "manifest line exceeds the installer's key/value limits")
    return ("\n".join(lines) + "\n").encode()


def require_update_stack(system_zst, debugfs=None):
    """Refuse a target system without the A update stack or with another A key.

    Installing such a build would leave the phone unable to take the next phone
    update. Needs debugfs (e2fsprogs; set DEBUGFS when it is not on PATH).
    """
    debugfs = debugfs or os.environ.get("DEBUGFS") or shutil.which("debugfs")
    require(debugfs, "debugfs is required to inspect the target system; set DEBUGFS")
    with tempfile.TemporaryDirectory(prefix="crux-ota-stack-") as work:
        image = Path(work) / "system.raw.img"
        with subprocess.Popen(["zstd", "-dc", "--long=27", str(system_zst)], stdout=subprocess.PIPE) as zstd, \
                image.open("wb") as sink:
            expand_sparse(zstd.stdout, sink)
        require(zstd.returncode == 0, "system.img.zst does not decompress")

        def debug(command):
            return subprocess.run([debugfs, "-R", command, str(image)], capture_output=True, text=True,
                                  timeout=120).stdout

        missing = [path for path in (UPDATE_STACK_KEY, *UPDATE_STACK) if "Type: regular" not in debug(f"stat {path}")]
        require(not missing, "the target system lacks the A update stack: " + ", ".join(missing))
        key = debug(f"cat {UPDATE_STACK_KEY}").strip()
    require(key == public_key().hex(), "the target system carries another A release key")


def from_delivery(source, *, start_builds, serial):
    """The manifest of a verified delivery (dev_release_source.VerifiedDelivery)."""
    directory = Path(source.directory)
    require_update_stack(directory / "system.img.zst")
    record = json.loads((directory / "delivery-record.json").read_text())
    return manifest(directory / "system.img.zst", directory / "vendor.img", directory / "rom-cepheus.itb",
                    build=record["build_number"], fingerprint=record["source_selection"]["env"]["BUILD_FINGERPRINT"],
                    start_builds=start_builds, serial=serial,
                    source=(("assembly_run", source.assembly_run), ("compile_run", source.compile_run),
                            ("artifact_digest", source.artifact_digest)))


def public_key(path=None):
    """The A release key as raw X||Y; the file holds 64 bytes or their 128 hex digits."""
    path = PUBLIC_KEY if path is None else path
    raw = Path(path).read_bytes()
    if len(raw) != 64:
        text = raw.decode("ascii").strip()
        require(re.fullmatch(r"[0-9a-f]{128}", text), f"{path} is not a raw P-256 public key")
        raw = bytes.fromhex(text)
    return raw


def _der_integer(value):
    value = value.lstrip(b"\0") or b"\0"
    if value[0] & 0x80:
        value = b"\0" + value
    return b"\x02" + bytes([len(value)]) + value


def verify(text, signature, key=None):
    """True when `signature` (raw r||s) is a valid P-256 ECDSA-SHA256 signature of `text`."""
    if len(signature) != 64:
        return False
    body = _der_integer(signature[:32]) + _der_integer(signature[32:])
    point = public_key() if key is None else key
    with tempfile.TemporaryDirectory(prefix="crux-ota-verify-") as work:
        work = Path(work)
        (work / "key.der").write_bytes(P256_SPKI + b"\x04" + point)
        (work / "signature.der").write_bytes(b"\x30" + bytes([len(body)]) + body)
        (work / MANIFEST).write_bytes(text)
        result = subprocess.run(["openssl", "dgst", "-sha256", "-keyform", "DER", "-verify", str(work / "key.der"),
                                 "-signature", str(work / "signature.der"), str(work / MANIFEST)],
                                capture_output=True, text=True, timeout=60)
    return result.returncode == 0 and result.stdout.strip() == "Verified OK"


def _raw_signature(der):
    require(der[0] == 0x30 and der[1] < 0x80, "unexpected ECDSA signature encoding")
    position, raw = 2, b""
    for _ in range(2):
        require(der[position] == 0x02, "unexpected ECDSA signature integer")
        length = der[position + 1]
        value = der[position + 2:position + 2 + length].lstrip(b"\0")
        require(len(value) <= 32, "ECDSA signature integer is not a P-256 value")
        raw += value.rjust(32, b"\0")
        position += 2 + length
    return raw


def sign(text, private_key):
    """Raw r||s signature of `text` with an offline P-256 private key (PEM)."""
    with tempfile.TemporaryDirectory(prefix="crux-ota-sign-") as work:
        path = Path(work) / MANIFEST
        path.write_bytes(text)
        der = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private_key), str(path)],
                             capture_output=True, check=True, timeout=60).stdout
    return _raw_signature(der)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    signing = commands.add_parser("sign", help="verify a delivery as release-dev.yml does, then sign its manifest")
    signing.add_argument("--assembly-run", required=True)
    signing.add_argument("--tag", required=True)
    signing.add_argument("--start-builds", required=True)
    signing.add_argument("--serial", required=True)
    signing.add_argument("--key", type=Path, required=True, help="offline A P-256 private key (PEM)")
    signing.add_argument("--out", type=Path, required=True, help="new directory for the manifest and signature")
    args = parser.parse_args()
    from dev_release_source import verify_source

    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        os.environ["GH_TOKEN"] = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                                                check=True).stdout.strip()
    try:
        args.out.mkdir(parents=True, exist_ok=False)
        with tempfile.TemporaryDirectory(prefix="crux-ota-manifest-") as work:
            source = verify_source(args.assembly_run, args.tag, Path(work))
            text = from_delivery(source, start_builds=args.start_builds, serial=args.serial)
        signature = sign(text, args.key)
        require(verify(text, signature), "the signature does not verify with keys/crux-ota-a-p256.pub")
        (args.out / MANIFEST).write_bytes(text)
        (args.out / SIGNATURE).write_bytes(signature)
    except (DevOtaError, OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"dev-ota-manifest: {error}\n")
    print(text.decode(), end="")
    print(json.dumps({"tag": args.tag, "manifest_sha256": hashlib.sha256(text).hexdigest(),
                      "ota_signature": signature.hex()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
