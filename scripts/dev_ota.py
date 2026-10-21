#!/usr/bin/env python3
"""Host-driven development OTA channel for the Crux PE13 dev product.

The channel is the set of GitHub prereleases named
``dev-pe13-<build>-v<n>`` in ``coachpo/crux-pe13-distributed``.  Each release
must carry ``delivery-record.json`` and ``SHA256SUMS``.  This tool discovers
the newest channel entry, verifies the record and its assets, compares the
connected device, prepares raw system/vendor images and padded FITs, and can
drive the proven U-Boot/TWRP install.

This is deliberately **not** the public release OTA feed.  The public
``r6-...`` recommendation in ``coachpo/crux-pe13-ota`` is never read or
written by this tool.

Commands:
    check   show the channel head and whether the device is current
    plan    download/verify assets, expand images, pad FITs, write plan.json
    update  run the install after --execute (destructive: discards PE data)
    verify  check the running device against the plan
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

import dev_ota_fits


REPOSITORY = "coachpo/crux-pe13-distributed"
CHANNEL_PREFIX = "dev-pe13-"
TAG_RE = re.compile(r"^dev-pe13-(\d{10})-v(\d+)$")
PRODUCT = "aosp_crux-userdebug"
DEVICE = "crux"
DEFAULT_SERIAL = "8bd37df4"
SLOTS = {"recovery": (0x38000, 0x3693), "rom": (0x3c000, 0x2bae)}
TWRP_SLOT = {"lba": 0x34000, "blocks": 0x2924}
DELIVERY_NAMES = ("delivery-record.json", "SHA256SUMS", "boot.img", "recovery.img",
                  "rom-cepheus.itb", "recovery-cepheus.itb", "system.img.zst",
                  "vendor.img")

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = REPO_ROOT.parents[2]
INSTALL_SCRIPT = WORKSPACE / "u-boot-port/scripts/install-pe13.py"
RAM_SCRIPT = WORKSPACE / "u-boot-port/scripts/ram-boot-pe13.py"
TWRP_FIT = WORKSPACE / "out/crux-pe13-baseline-2026-10-05/fits/twrp-menu.itb"


class DevOtaError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise DevOtaError(message)


def run(argv, timeout=60, required=True):
    completed = subprocess.run([str(item) for item in argv], capture_output=True,
                               text=True, timeout=timeout)
    if required and completed.returncode:
        raise DevOtaError("command failed (%s): %s\n%s" % (
            completed.returncode, " ".join(map(str, argv)),
            (completed.stdout + completed.stderr)[-4000:]))
    return completed


def gh_json(*argv):
    completed = run(["gh", *argv], timeout=120)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise DevOtaError("invalid gh JSON: %s" % error)


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            result.update(chunk)
    return result.hexdigest()


def parse_tag(tag):
    match = TAG_RE.fullmatch(tag)
    if not match:
        return None
    return {"build": match.group(1), "version": int(match.group(2))}


def discover(repository):
    releases = gh_json("api", "repos/%s/releases?per_page=100" % repository)
    result = []
    for release in releases:
        parsed = parse_tag(release.get("tag_name", ""))
        if not release.get("prerelease") or parsed is None:
            continue
        names = {asset.get("name") for asset in release.get("assets", [])}
        if not {"delivery-record.json", "SHA256SUMS"} <= names:
            continue
        result.append({"tag": release["tag_name"], **parsed,
                       "published_at": release.get("published_at") or release.get("created_at"),
                       "assets": release.get("assets", [])})
    return result


def select_head(releases, tag=None):
    if tag is not None:
        matches = [release for release in releases if release["tag"] == tag]
        require(matches, "channel tag not found: " + tag)
        return matches[0]
    require(releases, "no dev channel releases found")
    return max(releases, key=lambda release: (release["build"], release["version"],
                                               release["published_at"]))


def parse_sums(text):
    sums = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        require(len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]),
                "invalid SHA256SUMS line: " + line)
        digest, name = parts
        require(name not in sums, "duplicate SHA256SUMS entry: " + name)
        sums[name] = digest
    require(sums, "empty SHA256SUMS")
    return sums


def download(tag, names, directory):
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        if (directory / name).is_file():
            continue
        run(["gh", "release", "download", tag, "--repo", REPOSITORY,
             "--pattern", name, "--dir", str(directory), "--clobber"], timeout=1800)


def load_record(release, directory):
    download(release["tag"], ("delivery-record.json", "SHA256SUMS"), directory)
    record_path = directory / "delivery-record.json"
    sums_path = directory / "SHA256SUMS"
    sums = parse_sums(sums_path.read_text())
    require(sums.get("delivery-record.json") == sha256(record_path),
            "delivery-record.json digest differs from SHA256SUMS")
    record = json.loads(record_path.read_text())
    validate_record(release, record, sums)
    return record, sums


def validate_record(release, record, sums):
    require(record.get("schema_version") == 1, "unsupported delivery record schema")
    require(record.get("status") == "success", "delivery record is not successful")
    require(record.get("product") == PRODUCT, "unexpected product: %s" % record.get("product"))
    require(record.get("build_number") == release["build"],
            "delivery build differs from the tag")
    env = record.get("source_selection", {}).get("env", {})
    fingerprint = env.get("BUILD_FINGERPRINT", "")
    require(fingerprint.endswith("/%s:userdebug/test-keys" % release["build"]),
            "delivery is not the dev test-keys identity: " + fingerprint)
    delivery = record.get("delivery")
    require(isinstance(delivery, dict) and delivery, "delivery record has no assets")
    require(set(sums) == set(delivery) | {"delivery-record.json"},
            "SHA256SUMS and delivery assets differ")
    for name, identity in delivery.items():
        require(sums.get(name) == identity.get("sha256"),
                "delivery digest differs for " + name)
        asset = next((item for item in release["assets"] if item.get("name") == name), None)
        require(asset is not None, "release is missing " + name)
        require(asset.get("size") == identity.get("size"),
                "release size differs for " + name)
        if asset.get("digest"):
            require(asset["digest"] == "sha256:" + identity["sha256"],
                    "release digest differs for " + name)
    fit = record.get("fit_validation", {})
    require(fit.get("release_ready") is False, "dev delivery must not claim release readiness")
    require(fit.get("development_adb") is True, "dev delivery must carry the development ADB profile")
    images = record.get("gates", {}).get("images", {})
    for role in ("boot", "recovery"):
        require(images.get(role, {}).get("failures") == [],
                "image gate failed: " + role)
    system = record.get("system_image", {})
    require(system.get("compressed_as") == "system.img.zst",
            "unexpected system image encoding")
    require(system.get("compressed", {}).get("sha256") == sums.get("system.img.zst"),
            "system image digest differs")
    return {"tag": release["tag"], "build": release["build"], "fingerprint": fingerprint,
            "sums": sums, "delivery": delivery}


def adb(serial, *args, timeout=15):
    return run(["adb", "-s", serial, *args], timeout=timeout)


def device_state(serial):
    try:
        fingerprint = adb(serial, "shell", "getprop", "ro.build.fingerprint").stdout.strip()
        incremental = adb(serial, "shell", "getprop", "ro.build.version.incremental").stdout.strip()
        boot = adb(serial, "shell", "getprop", "sys.boot_completed").stdout.strip()
        ce = adb(serial, "shell", "getprop", "sys.user.0.ce_available").stdout.strip()
    except Exception:
        return None
    if not fingerprint:
        return None
    return {"fingerprint": fingerprint, "incremental": incremental,
            "boot_completed": boot, "ce_available": ce}


def check(args):
    releases = discover(REPOSITORY)
    release = select_head(releases, args.tag)
    record, _ = load_record(release, args.cache / release["tag"])
    device = device_state(args.serial)
    current = bool(device and device["incremental"] == release["build"])
    summary = {"channel": "dev", "repository": REPOSITORY, "tag": release["tag"],
               "build": release["build"], "fingerprint":
               record["source_selection"]["env"]["BUILD_FINGERPRINT"],
               "release_ready": False, "device": device, "current": current}
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print("dev OTA channel: %s" % release["tag"])
    print("  build: %s" % release["build"])
    print("  fingerprint: %s" % summary["fingerprint"])
    print("  release_ready: false (dev/test-keys only)")
    if device:
        print("  device: %s (%s)" % (device["incremental"], device["fingerprint"]))
        print("  status: %s" % ("up to date" if current else "update available"))
    else:
        print("  device: not connected; channel record is ready")
    return 0


def ensure_delivery(release, directory):
    if directory is not None:
        directory = Path(directory)
        missing = [name for name in DELIVERY_NAMES if not (directory / name).is_file()]
        require(not missing, "delivery directory is missing: " + " ".join(missing))
    else:
        directory = Path(directory or (Path.home() / ".cache/crux-dev-ota" / release["tag"]))
        download(release["tag"], DELIVERY_NAMES, directory)
    record, sums = load_record(release, directory)
    for name, identity in record["delivery"].items():
        path = directory / name
        require(path.stat().st_size == identity["size"], "local size differs for " + name)
        require(sha256(path) == identity["sha256"], "local digest differs for " + name)
    return directory, record, sums


def ext4_ok(path):
    with Path(path).open("rb") as handle:
        header = handle.read(2048)
    require(struct.unpack_from("<I", header)[0] != 0xED26FF3A,
            "%s is sparse; expand it first" % path)
    require(struct.unpack_from("<H", header, 1024 + 56)[0] == 0xEF53,
            "%s has no ext4 superblock" % path)
    return True


def expand_images(delivery, work):
    work.mkdir(parents=True, exist_ok=True)
    sparse_system = work / "system.img"
    if not sparse_system.is_file():
        run(["zstd", "-d", "-f", str(delivery / "system.img.zst"), "-o", str(sparse_system)],
            timeout=600)
    system_raw = work / "system.raw.img"
    vendor_raw = work / "vendor.raw.img"
    if not system_raw.is_file():
        run(["simg2img", str(sparse_system), str(system_raw)], timeout=600)
    if not vendor_raw.is_file():
        run(["simg2img", str(delivery / "vendor.img"), str(vendor_raw)], timeout=600)
    ext4_ok(system_raw)
    ext4_ok(vendor_raw)
    return {"system": {"path": str(system_raw), "size": system_raw.stat().st_size,
                       "sha256": sha256(system_raw)},
            "vendor": {"path": str(vendor_raw), "size": vendor_raw.stat().st_size,
                       "sha256": sha256(vendor_raw)}}


def pad_fits(delivery, work):
    result = {}
    for role, name in (("recovery", "recovery-cepheus.itb"), ("rom", "rom-cepheus.itb")):
        lba, blocks = SLOTS[role]
        source = delivery / name
        data = source.read_bytes()
        slot = blocks * 4096
        require(len(data) <= slot, "%s is larger than its cache slot" % name)
        padded = data + b"\0" * (slot - len(data))
        require(padded[:4] == bytes.fromhex("d00dfeed"), "%s is not a FIT" % name)
        path = work / (name.removesuffix(".itb") + ".padded.bin")
        path.write_bytes(padded)
        result[role] = {"path": str(path), "source": str(source),
                        "lba": lba, "blocks": blocks,
                        "sha256": hashlib.sha256(padded).hexdigest()}
    return result


def vold_digest(system_raw, work):
    """Extract /system/bin/vold when a host or OrbStack debugfs is available."""
    system_raw = Path(system_raw).resolve()
    target = work / "vold"
    if shutil.which("debugfs"):
        completed = run(["debugfs", "-R", "dump /system/bin/vold " + str(target),
                         str(system_raw)], required=False, timeout=120)
        if completed.returncode == 0 and target.is_file():
            return sha256(target)
    if shutil.which("orb"):
        command = ('debugfs -R "dump /system/bin/vold /tmp/crux-dev-ota-vold" %s '
                   '>/dev/null 2>&1 && sha256sum /tmp/crux-dev-ota-vold'
                   % str(system_raw))
        completed = run(["orb", "-m", "cruxbuild", "bash", "-lc", command],
                        required=False, timeout=180)
        if completed.returncode == 0:
            match = re.search(r"([0-9a-f]{64})\s+/tmp/crux-dev-ota-vold", completed.stdout)
            if match:
                return match.group(1)
    return None


def make_plan(args, release):
    delivery, record, sums = ensure_delivery(release, args.delivery_dir)
    work = Path(args.out) / "work" / release["tag"]
    raw = expand_images(delivery, work)
    fits = pad_fits(delivery, work)
    vold = vold_digest(raw["system"]["path"], work)
    require(Path(args.twrp_fit).is_file(),
            "missing retained TWRP FIT: " + str(args.twrp_fit))
    device = device_state(args.serial)
    plan = {
        "schema_version": 1,
        "channel": "dev",
        "repository": REPOSITORY,
        "tag": release["tag"],
        "build": release["build"],
        "fingerprint": record["source_selection"]["env"]["BUILD_FINGERPRINT"],
        "product": PRODUCT,
        "device": DEVICE,
        "serial": args.serial,
        "delivery_dir": str(delivery),
        "assets": {name: {"path": str(delivery / name), "size": identity["size"],
                          "sha256": identity["sha256"]}
                   for name, identity in record["delivery"].items()},
        "raw": raw,
        "fits": fits,
        "vold_sha256": vold,
        "data_policy": "discard",
        "cache_slots": {"recovery": list(SLOTS["recovery"]), "rom": list(SLOTS["rom"])},
        "twrp_fit": str(args.twrp_fit),
        "twrp_crc32": dev_ota_fits.crc_bytes(
            dev_ota_fits.padded(args.twrp_fit, dev_ota_fits.TWRP["blocks"])),
        "uboot_version": args.uboot_version,
        "current_device": device,
        "note": ("Host-driven dev OTA: U-Boot cache FITs plus TWRP "
                 "install-pe13.py. PE data is discarded by this v1 update."),
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    return plan


def print_plan(plan):
    print("dev OTA plan: %s" % plan["tag"])
    print("  build: %s" % plan["build"])
    print("  fingerprint: %s" % plan["fingerprint"])
    print("  delivery: %s" % plan["delivery_dir"])
    print("  raw system: %s %s" % (plan["raw"]["system"]["sha256"],
                                   plan["raw"]["system"]["size"]))
    print("  raw vendor: %s %s" % (plan["raw"]["vendor"]["sha256"],
                                   plan["raw"]["vendor"]["size"]))
    for role in ("recovery", "rom"):
        fit = plan["fits"][role]
        print("  %s FIT: lba=0x%x blocks=0x%x sha=%s" % (
            role, fit["lba"], fit["blocks"], fit["sha256"]))
    print("  vold: %s" % (plan["vold_sha256"] or "not extracted"))
    print("  data policy: %s (destroys peuserdata/pemetadata)" % plan["data_policy"])
    if plan.get("current_device"):
        print("  device now: %s" % plan["current_device"]["incremental"])
    print("  update requires --execute; plan is read-only")


def plan_command(args):
    release = select_head(discover(REPOSITORY), args.tag)
    plan = make_plan(args, release)
    print_plan(plan)
    return 0


def enter_uboot(out, serial, ram):
    offset = ram.LOG.stat().st_size
    run(["adb", "-s", serial, "reboot"], timeout=20)
    session = ram.Session(Path(out))
    state = session.recover(offset, require_menu=True, timeout=120)
    session.save()
    return session, state


def wait_for(serial, prop, expected, timeout=300):
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            last = adb(serial, "shell", "getprop", prop, timeout=8).stdout.strip()
        except Exception:
            last = ""
        if last == expected:
            return True
        time.sleep(2)
    raise DevOtaError("timed out waiting for %s=%s (last %r)" % (prop, expected, last))


def install(plan, out):
    require(INSTALL_SCRIPT.is_file(), "missing install script: " + str(INSTALL_SCRIPT))
    result_dir = Path(out) / "install"
    result_dir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(INSTALL_SCRIPT),
               "--system", plan["raw"]["system"]["path"],
               "--system-sha256", plan["raw"]["system"]["sha256"],
               "--vendor", plan["raw"]["vendor"]["path"],
               "--vendor-sha256", plan["raw"]["vendor"]["sha256"],
               "--out", str(result_dir), "--discard-pe-data"]
    completed = run(command, timeout=3600, required=False)
    (result_dir / "install.log").write_text(completed.stdout + completed.stderr)
    require(completed.returncode == 0, "install-pe13.py failed; see " + str(result_dir))
    result = json.loads((result_dir / "result.json").read_text())
    require(result.get("status") == "fresh_pe_installed_readback_and_format_verified",
            "unexpected install status: %s" % result.get("status"))
    return result


def verify_device(plan, args, out):
    wait_for(args.serial, "sys.boot_completed", "1", timeout=args.boot_timeout)
    try:
        adb(args.serial, "root", timeout=20)
        time.sleep(2)
        adb(args.serial, "wait-for-device", timeout=30)
    except Exception:
        pass
    def shell(command, timeout=20):
        return adb(args.serial, "shell", command, timeout=timeout).stdout.strip()
    fingerprint = shell("getprop ro.build.fingerprint")
    incremental = shell("getprop ro.build.version.incremental")
    ce = shell("getprop sys.user.0.ce_available")
    fstab = shell("grep peuserdata /vendor/etc/fstab.crux_uboot")
    vold = shell("sha256sum /system/bin/vold")
    media = shell("logcat -d -b all 2>/dev/null | grep -m1 'Encryption policy of /data/media/0'")
    dmesg_v1 = shell("dmesg | grep -c \"v1 policies can.t be used\" || true")
    zygote = shell("logcat -d -b all 2>/dev/null | grep -c zygote-fatal || true")
    uptime = shell("cat /proc/uptime")
    record = {"schema_version": 1, "tag": plan["tag"], "expected_fingerprint":
              plan["fingerprint"], "fingerprint": fingerprint, "incremental": incremental,
              "ce_available": ce, "fstab": fstab, "vold": vold, "media_v2": media,
              "dmesg_v1_count": dmesg_v1, "zygote_fatal_count": zygote,
              "uptime": uptime}
    require(fingerprint == plan["fingerprint"],
            "fingerprint differs: %s" % fingerprint)
    require(incremental == plan["build"], "incremental differs: %s" % incremental)
    require(ce == "true", "CE is not available")
    require("formattable" in fstab and "ice::v2" in fstab,
            "vendor fstab is not the v2 formattable table")
    require("v2" in media, "vold did not report /data/media/0 v2")
    require(dmesg_v1 == "0", "dmesg reports v1 policy use: " + dmesg_v1)
    require(zygote == "0", "zygote-fatal present: " + zygote)
    if plan.get("vold_sha256"):
        require(plan["vold_sha256"] in vold,
                "runtime vold differs from the system image")
    if args.verify_seconds:
        time.sleep(args.verify_seconds)
        require(shell("getprop sys.boot_completed") == "1",
                "device lost boot_completed during the stability window")
        record["stability_seconds"] = args.verify_seconds
    record["status"] = "verified"
    (Path(out) / "verify.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def update_command(args):
    release = select_head(discover(REPOSITORY), args.tag)
    plan = make_plan(args, release)
    print_plan(plan)
    if not args.execute:
        print("dry run only; pass --execute to write the device")
        return 0
    require(RAM_SCRIPT.is_file(), "missing U-Boot helper: " + str(RAM_SCRIPT))
    require(Path(plan["twrp_fit"]).is_file(), "missing retained TWRP FIT: " + plan["twrp_fit"])
    current = device_state(args.serial)
    if current and current["incremental"] == plan["build"] and not args.force:
        print("device already reports build %s; use --force to reinstall" % plan["build"])
        return 0
    watcher = run(["pgrep", "-f", "uconsole_watch.py"], required=False)
    require(watcher.returncode == 0 and watcher.stdout.strip(),
            "start u-boot-port/scripts/uconsole_watch.py before --execute")
    ram = dev_ota_fits.load_ram(WORKSPACE)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    session, menu = enter_uboot(out, args.serial, ram)
    print("U-Boot:", menu.get("menu_evidence"))
    dev_ota_fits.write_fits(session, ram, plan["fits"]["recovery"], plan["fits"]["rom"],
                            out / "fit-write", args.uboot_version, plan["twrp_fit"])
    session.send("run boot_twrp", prompt=False, timeout=20)
    session.save()
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            version = adb(args.serial, "shell", "getprop ro.twrp.version").stdout.strip()
        except Exception:
            version = ""
        if version.startswith("3.7.1_12"):
            break
        time.sleep(2)
    else:
        raise DevOtaError("TWRP did not expose the expected ADB target")
    result = install(plan, out)
    (out / "install-result.json").write_text(json.dumps(result, indent=2) + "\n")
    session, menu = enter_uboot(out, args.serial, ram)
    session.send("run boot_pe_rom", prompt=False, timeout=20)
    session.save()
    verify = verify_device(plan, args, out)
    print(json.dumps(verify, indent=2))
    return 0


def verify_command(args):
    plan_path = Path(args.out) / "plan.json"
    if plan_path.is_file():
        plan = json.loads(plan_path.read_text())
    else:
        release = select_head(discover(REPOSITORY), args.tag)
        plan = make_plan(args, release)
    record = verify_device(plan, args, args.out)
    print(json.dumps(record, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/crux-dev-ota",
                        help="record download cache")
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    check_parser = sub.add_parser("check")
    check_parser.add_argument("--tag")
    check_parser.add_argument("--json", action="store_true")
    check_parser.set_defaults(func=check)

    for name, function in (("plan", plan_command), ("update", update_command),
                           ("verify", verify_command)):
        command = sub.add_parser(name)
        command.add_argument("--tag")
        command.add_argument("--delivery-dir", type=Path,
                             help="use an existing delivery directory instead of downloading")
        command.add_argument("--out", type=Path, default=Path("dev-ota-evidence"))
        command.add_argument("--twrp-fit", type=Path, default=TWRP_FIT)
        command.add_argument("--uboot-version",
                             default="2026.10-rc5-00614-g35579e6f8c14-dirty")
        command.add_argument("--boot-timeout", type=int, default=300)
        command.add_argument("--verify-seconds", type=int, default=0)
        command.add_argument("--execute", action="store_true",
                             help="perform device writes (update only)")
        command.add_argument("--force", action="store_true",
                             help="reinstall the same build (update only)")
        command.set_defaults(func=function)

    args = parser.parse_args()
    try:
        return args.func(args)
    except DevOtaError as error:
        parser.exit(1, "dev-ota: %s\n" % error)


if __name__ == "__main__":
    sys.exit(main())
