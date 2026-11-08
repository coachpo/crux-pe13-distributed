#!/usr/bin/env python3
"""Package one PE13 build as a signed crux-install/1 TWRP ZIP (clean install).

The Actions assembly (A) and the local OrbStack build (B) both use this tool,
in three steps that keep the signing key offline:

    crux_zip.py prepare --system RAW --vendor RAW --recovery-fit FIT --rom-fit FIT
        --installer BIN --build-number N (--any-serial | --serial S)
        --twrp-profile ID [--twrp-profile ID ...] --out NEW_DIR
    crux_zip.py sign --payload DIR --key PRIVATE.pem
    crux_zip.py finalize --payload DIR [--signature HEX] --public-key PUBLIC.pem --out NEW_DIR

prepare checks the expanded raw ext4 images (sizes, and the system image's
own build identity and marker, read with debugfs) and the FITs (complete,
inside their U-Boot read windows, no device serial unless --serial names it).
It compresses the raw images with zstd and writes the unsigned manifest
crux-install.txt, the update-binary, both FITs, README.txt and payload.json
into a new directory. sign writes crux-install.sig there and prints the
signature as hex. finalize verifies the signature with the public key and
every payload digest against the manifest, then writes crux-pe13-<build>.zip
(STORED entries with fixed times), its .sha256 file and README.txt.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

UBOOT_ROLE = "recovery-container-crux-install-1"
BLOCK = 4096
WINDOWS = {"recovery": 0x3b00 * BLOCK, "rom": 0x3100 * BLOCK}
LIMITS = {"pesystem": 0xf0000 * BLOCK, "pevendor": 0x70000 * BLOCK}
# The install TWRP keeps the pushed ZIP in its RAM /tmp; GitHub Release assets
# must stay below 2 GiB.
ZIP_LIMIT = min(2_600_000_000, 2 * 1024 ** 3 - 1)
ZIP_TIME = (2026, 11, 8, 0, 0, 0)
FINGERPRINT = r"Xiaomi/crux/crux:13/[^/:]+/{build}:userdebug/test-keys"
MARKER = "/system/etc/crux-delivery-verification.txt"
SERIAL = re.compile(rb"androidboot\.serialno=([0-9A-Za-z]+)")
ENTRIES = {
    "installer": "META-INF/com/google/android/update-binary",
    "manifest": "crux-install.txt",
    "signature": "crux-install.sig",
    "readme": "README.txt",
    "recovery": "recovery-cepheus.itb",
    "rom": "rom-cepheus.itb",
    "pesystem": "system.raw.img.zst",
    "pevendor": "vendor.raw.img.zst",
}
ZIP_ORDER = ("installer", "manifest", "signature", "readme", "recovery", "rom", "pesystem", "pevendor")


class ZipError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ZipError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def tool(name, *candidates):
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which(name)
    require(found, f"{name} is required")
    return found


def openssl():
    return tool("openssl", "/opt/homebrew/opt/openssl@3/bin/openssl")


def debugfs():
    return tool("debugfs", "/opt/homebrew/opt/e2fsprogs/sbin/debugfs", "/sbin/debugfs")


def image_file(image, path, missing_ok=False):
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "file"
        done = subprocess.run([debugfs(), "-R", f"dump {path} {target}", str(image)],
                              capture_output=True, timeout=180)
        error = done.stderr.decode(errors="replace")
        if missing_ok and done.returncode == 0 and not target.exists() and "File not found" in error:
            return None
        require(done.returncode == 0 and target.is_file(), f"cannot read {path} from {image}: {error}")
        return target.read_bytes()


def check_raw(path, part):
    """An expanded raw ext4 image that fits the PE volume it is written to."""
    size = path.stat().st_size
    require(BLOCK <= size <= LIMITS[part] and size % BLOCK == 0,
            f"{part}: raw image size {size} does not fit its installable size")
    with path.open("rb") as handle:
        header = handle.read(2048)
    require(struct.unpack_from("<I", header)[0] != 0xED26FF3A,
            f"{part}: Android sparse image; supply the expanded raw ext4 image")
    superblock = header[1024:2048]
    require(struct.unpack_from("<H", superblock, 56)[0] == 0xEF53, f"{part}: missing ext4 superblock magic")
    block_size = 1024 << struct.unpack_from("<I", superblock, 24)[0]
    blocks = struct.unpack_from("<I", superblock, 4)[0]
    if struct.unpack_from("<I", superblock, 96)[0] & 0x80:
        blocks |= struct.unpack_from("<I", superblock, 0x150)[0] << 32
    require(0 < blocks * block_size <= size, f"{part}: filesystem is larger than the raw image")
    return {"bytes": size, "sha256": digest(path)}


def system_identity(system, build):
    props = dict(line.split("=", 1) for line in image_file(system, "/system/build.prop").decode().splitlines()
                 if "=" in line and not line.startswith("#"))
    fingerprint = props.get("ro.build.fingerprint", "")
    require(props.get("ro.build.version.incremental") == build and
            re.fullmatch(FINGERPRINT.format(build=re.escape(build)), fingerprint) and
            props.get("ro.product.system.name") == "aosp_crux" and props.get("ro.build.type") == "userdebug",
            f"system image build.prop is not aosp_crux-userdebug build {build}")
    marker = image_file(system, MARKER, missing_ok=True)
    return {"build": build, "fingerprint": fingerprint,
            "marker": marker.decode().strip() if marker else "none"}


def check_fit(path, role, serial):
    data = path.read_bytes()
    require(data[:4] == bytes.fromhex("d00dfeed") and int.from_bytes(data[4:8], "big") == len(data) and
            len(data) <= WINDOWS[role], f"{role} FIT is not a complete FIT inside its window")
    serials = set(SERIAL.findall(data))
    if serial == "any":
        require(not serials, f"{role} FIT carries a device serial; --any-serial needs FITs without identity")
    else:
        require(serials <= {serial.encode()}, f"{role} FIT is bound to another serial: {serials}")
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def zstd_version():
    done = subprocess.run(["zstd", "--version"], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def compress(raw, target, level, expected):
    subprocess.run(["zstd", "-q", "-T0", f"-{level}", "--long=27", "-o", str(target), str(raw)], check=True)
    h = hashlib.sha256()
    with subprocess.Popen(["zstd", "-dc", "--long=27", str(target)], stdout=subprocess.PIPE) as decoder:
        for chunk in iter(lambda: decoder.stdout.read(1 << 22), b""):
            h.update(chunk)
        require(decoder.wait() == 0, f"{target.name}: zstd cannot decompress it")
    require(h.hexdigest() == expected, f"{target.name} does not expand to the raw image")
    return {"stored_bytes": target.stat().st_size, "stored_sha256": digest(target)}


def manifest_text(fields):
    return "".join(f"{key}={value}\n" for key, value in fields.items()).encode()


def readme(manifest, zip_name):
    serial = manifest["serialno"]
    boot = ("\n5. Boot 分区里是 Boot 身份版 U-Boot（boot-crux-identity-1）。本包的 FIT 不含设备身份，"
            "普通开机要靠它从 ABL 补上序列号和屏幕参数。" if serial == "any" else "")
    profiles = manifest["twrp_profiles"].replace(",", ", ")
    return f"""Crux PE13 卡刷包 — {zip_name}

用途：Xiaomi Mi 9 Pro 5G（crux）上的 PE13 构建 {manifest['target_build']}，清装。
目标指纹：{manifest['target_fingerprint']}
镜像 marker：{manifest['target_marker']}
适用设备序列号：{serial if serial != 'any' else '任意（FIT 不含个人身份）'}

会写入：pesystem、pevendor（整盘重写），并清除 PE 用户数据（peuserdata）和
PE 加密元数据（pemetadata）；把同批 PE Recovery 和 PE ROM 两个 FIT 暂存在
pesystem 尾区，由 U-Boot 落进 cache 窗口。不写 MIUI 的任何分区、misc 或 GPT。

前置条件（缺一项安装器都会在写入前拒绝）：
1. 设备是 crux，GPT 里有 pesystem/pevendor/peuserdata/pemetadata（与已验证设备相同的位置）。
2. 物理 Recovery 分区里是 crux-install/1 容器版 U-Boot（{manifest['uboot_role']}）。
3. 安装用 TWRP（twrp-b-install.itb，只在 RAM 里运行），profile 之一：{profiles}。
4. 电脑上有 Android platform-tools（adb、fastboot）。{boot}

须知：这是 userdebug 开发构建。ADB 默认开启、不做 RSA 主机认证，任何连上 USB 的
电脑都能直接拿到 root shell。不要在存有个人数据的设备上使用，也不要把它当作正式发布版。

步骤：
1. 核对 SHA-256：shasum -a 256 {zip_name}，与 {zip_name}.sha256 一致才继续；TWRP 也一样核对。
2. 进入 Recovery 容器的 U-Boot 菜单：在 PE 系统里执行 adb reboot recovery。
   （按键组合进入 recovery 未经验证，不要依赖。）
3. 菜单里选「Enable fastboot mode」，然后在电脑上执行：
   fastboot boot twrp-b-install.itb
4. TWRP 起来后：adb push {zip_name} /tmp/
5. TWRP 里：Install → 返回上一级到 /tmp → 选择 {zip_name} → 滑动确认。
6. 安装器列出写入目标后，按音量上键确认（音量下键取消，60 秒不按自动取消）。
7. 写完后手机自动重启到 Recovery，U-Boot 落窗后启动 PE。首次启动需要几分钟。
   若没有自动重启：TWRP 里选 Reboot → Recovery。

失败处理：
- 写入前被拒：手机没有任何改动，按提示修正（换对应的包、TWRP 或 U-Boot）。
- 写入中途中断：重新从第 2 步开始刷同一个包（会整盘重写）。
- U-Boot 停在菜单并提示事务失败：不要反复重试，记下屏幕上的事务号和阶段，联系提供者。
"""


def prepare(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    build = args.build_number
    require(re.fullmatch(r"\d{10}", build), "build number is not 10 digits")
    for profile in args.twrp_profile:
        require(re.fullmatch(r"[0-9a-f]{64}", profile), f"invalid TWRP profile ID {profile}")
    serial = "any" if args.any_serial else args.serial
    require(serial == "any" or re.fullmatch(r"[0-9A-Za-z]{4,32}", serial), "invalid serial")
    installer = args.installer.read_bytes()
    require(installer[:4] == b"\x7fELF" and installer[4] == 2 and installer[18:20] == b"\xb7\x00",
            "the installer is not an AArch64 ELF")
    raw = {"pesystem": check_raw(args.system, "pesystem"), "pevendor": check_raw(args.vendor, "pevendor")}
    identity = system_identity(args.system, build)
    fits = {"recovery": check_fit(args.recovery_fit, "recovery", serial),
            "rom": check_fit(args.rom_fit, "rom", serial)}
    stored = {}
    for part, source in (("pesystem", args.system), ("pevendor", args.vendor)):
        stored[part] = compress(source, out / ENTRIES[part], args.zstd_level, raw[part]["sha256"])
    fields = {
        "schema": "crux-install/1", "kind": "zip", "device": "crux", "product": "aosp_crux-userdebug",
        "serialno": serial, "uboot_role": args.uboot_role, "twrp_profiles": ",".join(args.twrp_profile),
        "data_policy": "discard", "target_build": build, "target_fingerprint": identity["fingerprint"],
        "target_marker": identity["marker"],
    }
    for part in ("pesystem", "pevendor"):
        fields.update({f"image.{part}.entry": ENTRIES[part], f"image.{part}.compression": "zstd",
                       f"image.{part}.stored_bytes": stored[part]["stored_bytes"],
                       f"image.{part}.stored_sha256": stored[part]["stored_sha256"],
                       f"image.{part}.bytes": raw[part]["bytes"], f"image.{part}.sha256": raw[part]["sha256"]})
    for role in ("recovery", "rom"):
        fields.update({f"fit.{role}.entry": ENTRIES[role], f"fit.{role}.bytes": fits[role]["bytes"],
                       f"fit.{role}.sha256": fits[role]["sha256"]})
    text = manifest_text(fields)
    zip_name = f"crux-pe13-{build}.zip"
    (out / ENTRIES["manifest"]).write_bytes(text)
    (out / "update-binary").write_bytes(installer)
    shutil.copyfile(args.recovery_fit, out / ENTRIES["recovery"])
    shutil.copyfile(args.rom_fit, out / ENTRIES["rom"])
    (out / ENTRIES["readme"]).write_text(readme(fields, zip_name))
    record = {
        "schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(), "zip_name": zip_name,
        "manifest_sha256": hashlib.sha256(text).hexdigest(), "identity": identity, "raw": raw, "stored": stored,
        "fits": fits, "installer_sha256": hashlib.sha256(installer).hexdigest(),
        "zstd": {"version": zstd_version(), "level": args.zstd_level, "long": 27},
        "inputs": {name: str(getattr(args, name).resolve())
                   for name in ("system", "vendor", "recovery_fit", "rom_fit", "installer")},
    }
    (out / "payload.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"payload": str(out), "manifest_sha256": record["manifest_sha256"],
                      "zip_name": zip_name}, indent=2))


def der_to_raw(der):
    """ECDSA-Sig-Value SEQUENCE { r INTEGER, s INTEGER } -> 64-byte r||s."""
    require(der[0] == 0x30, "unexpected signature encoding")
    pos = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7f)
    out = b""
    for _ in range(2):
        require(der[pos] == 0x02, "unexpected signature integer")
        length = der[pos + 1]
        value = der[pos + 2:pos + 2 + length].lstrip(b"\0")
        require(len(value) <= 32, "signature integer too large")
        out += value.rjust(32, b"\0")
        pos += 2 + length
    return out


def raw_to_der(raw):
    def integer(value):
        value = value.lstrip(b"\0") or b"\0"
        if value[0] & 0x80:
            value = b"\0" + value
        return bytes([0x02, len(value)]) + value
    body = integer(raw[:32]) + integer(raw[32:])
    return bytes([0x30, len(body)]) + body


def verify(manifest, signature, public_key):
    with tempfile.TemporaryDirectory() as directory:
        der = Path(directory) / "sig.der"
        der.write_bytes(raw_to_der(signature))
        done = subprocess.run([openssl(), "dgst", "-sha256", "-verify", str(public_key), "-signature", str(der),
                               str(manifest)], capture_output=True, text=True)
    require(done.returncode == 0, "the manifest signature does not verify with " + str(public_key))


def sign(args):
    payload = args.payload.resolve()
    manifest = payload / ENTRIES["manifest"]
    require(manifest.is_file(), f"{payload} has no {ENTRIES['manifest']}")
    with tempfile.TemporaryDirectory() as directory:
        der, public = Path(directory) / "sig.der", Path(directory) / "public.pem"
        subprocess.run([openssl(), "dgst", "-sha256", "-sign", str(args.key), "-out", str(der), str(manifest)],
                       check=True)
        subprocess.run([openssl(), "ec", "-in", str(args.key), "-pubout", "-out", str(public)], check=True,
                       capture_output=True)
        signature = der_to_raw(der.read_bytes())
        verify(manifest, signature, public)
    (payload / ENTRIES["signature"]).write_bytes(signature)
    print(signature.hex())


def manifest_fields(text):
    return dict(line.split("=", 1) for line in text.decode().splitlines() if line and "=" in line)


def finalize(args):
    payload = args.payload.resolve()
    manifest = payload / ENTRIES["manifest"]
    text = manifest.read_bytes()
    if args.signature:
        require(re.fullmatch(r"[0-9a-f]{128}", args.signature), "the signature is not 128 hex digits")
        signature = bytes.fromhex(args.signature)
    else:
        signature = (payload / ENTRIES["signature"]).read_bytes()
    require(len(signature) == 64, "the signature is not 64 bytes")
    verify(manifest, signature, args.public_key)
    fields = manifest_fields(text)
    require(fields.get("schema") == "crux-install/1" and fields.get("kind") == "zip",
            "the payload manifest is not a crux-install/1 ZIP manifest")
    for part in ("pesystem", "pevendor"):
        path = payload / fields[f"image.{part}.entry"]
        require(path.stat().st_size == int(fields[f"image.{part}.stored_bytes"]) and
                digest(path) == fields[f"image.{part}.stored_sha256"], f"{path.name} differs from the manifest")
    for role in ("recovery", "rom"):
        path = payload / fields[f"fit.{role}.entry"]
        require(path.stat().st_size == int(fields[f"fit.{role}.bytes"]) and
                digest(path) == fields[f"fit.{role}.sha256"], f"{path.name} differs from the manifest")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    zip_name = f"crux-pe13-{fields['target_build']}.zip"
    sources = {"installer": payload / "update-binary", "readme": payload / ENTRIES["readme"],
               "recovery": payload / ENTRIES["recovery"], "rom": payload / ENTRIES["rom"],
               "pesystem": payload / ENTRIES["pesystem"], "pevendor": payload / ENTRIES["pevendor"]}
    zip_path = out / zip_name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for key in ZIP_ORDER:
            info = zipfile.ZipInfo(ENTRIES[key], date_time=ZIP_TIME)
            info.external_attr = (0o755 if key == "installer" else 0o644) << 16
            if key == "manifest":
                archive.writestr(info, text)
            elif key == "signature":
                archive.writestr(info, signature)
            else:
                with sources[key].open("rb") as source, archive.open(info, "w") as target:
                    shutil.copyfileobj(source, target, 1 << 22)
    size = zip_path.stat().st_size
    require(size <= ZIP_LIMIT, f"{zip_name} is {size} bytes; the limit is {ZIP_LIMIT}")
    zip_sha = digest(zip_path)
    (out / f"{zip_name}.sha256").write_text(f"{zip_sha}  {zip_name}\n")
    shutil.copyfile(payload / ENTRIES["readme"], out / "README.txt")
    record = {"schema_version": 1, "zip": zip_name, "zip_bytes": size, "zip_sha256": zip_sha,
              "manifest_sha256": hashlib.sha256(text).hexdigest(), "signature": signature.hex(),
              "public_key_sha256": digest(args.public_key)}
    (out / "zip.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare")
    for name in ("--system", "--vendor", "--recovery-fit", "--rom-fit", "--installer", "--out"):
        p.add_argument(name, type=Path, required=True)
    p.add_argument("--build-number", required=True)
    serial = p.add_mutually_exclusive_group(required=True)
    serial.add_argument("--serial")
    serial.add_argument("--any-serial", action="store_true")
    p.add_argument("--twrp-profile", action="append", required=True)
    p.add_argument("--uboot-role", default=UBOOT_ROLE)
    p.add_argument("--zstd-level", type=int, default=12)
    s = commands.add_parser("sign")
    s.add_argument("--payload", type=Path, required=True)
    s.add_argument("--key", type=Path, required=True)
    f = commands.add_parser("finalize")
    f.add_argument("--payload", type=Path, required=True)
    f.add_argument("--signature")
    f.add_argument("--public-key", type=Path, required=True)
    f.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        {"prepare": prepare, "sign": sign, "finalize": finalize}[args.command](args)
    except (ZipError, OSError, subprocess.CalledProcessError, KeyError, ValueError) as error:
        print("crux_zip: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
