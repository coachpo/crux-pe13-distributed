#!/usr/bin/env python3
"""Supplemental read-only PE13 acceptance; requires already-root ROM ADB."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import time


MARKER = "/system/etc/crux-delivery-verification.txt"
MIN_SECONDS = 300
FATAL = re.compile(r"zygote-fatal", re.IGNORECASE)
BOOT = "cat /proc/sys/kernel/random/boot_id; getprop sys.boot_completed"
SNAPSHOT = r"""
set -eu
echo "uid=$(id -u)"
test "$(id -u)" = 0
for key in ro.product.device ro.boot.serialno ro.build.type ro.build.version.incremental \
           ro.build.fingerprint sys.boot_completed sys.user.0.ce_available ro.crypto.state \
           ro.crypto.type ro.crypto.volume.options init.svc.vold; do
    echo "$key=$(getprop "$key")"
done
echo "boot_id=$(cat /proc/sys/kernel/random/boot_id)"
for entry in pesystem:sda35 pevendor:sda36 peuserdata:sda37 pemetadata:sda38; do
    part=${entry%:*}
    node=${entry#*:}
    test "$(readlink -f "/dev/block/by-name/$part")" = "/dev/block/$node"
    echo "$part=$(cat "/sys/class/block/$node/dev")"
done
awk '$5 == "/" || $5 == "/vendor" || $5 == "/metadata" || $5 == "/data" {print "mount=" $0}' /proc/self/mountinfo
data=$(awk '$5 == "/data" {print $3}' /proc/self/mountinfo)
echo "data_node=$(basename "$(readlink -f "/sys/dev/block/$data")")"
test -d "/sys/dev/block/$data/slaves"
for slave in "/sys/dev/block/$data/slaves/"*; do
    if [ -e "$slave" ]; then echo "data_slave=$(basename "$slave")"; fi
done
grep -E '^[^#]*[[:space:]]/data[[:space:]]' /vendor/etc/fstab.crux_uboot | sed 's/^/fstab=/'
pid=$(pidof vold)
exe=$(readlink "/proc/$pid/exe")
echo "vold_exe=$exe"
sha256sum /system/bin/vold "/proc/$pid/exe" | sed 's/^/vold_hash=/'
test -r /system/etc && test -x /system/etc
if [ -e /system/etc/crux-delivery-verification.txt ] || [ -L /system/etc/crux-delivery-verification.txt ]; then
    test -f /system/etc/crux-delivery-verification.txt
    test ! -L /system/etc/crux-delivery-verification.txt
    echo 'marker=present'
else
    echo 'marker=absent'
fi
echo 'probe_complete=1'
"""


def require(condition, error):
    if not condition:
        raise RuntimeError(error)


def checked(adb, command, path):
    with path.open("wb") as out, path.with_suffix(".stderr.txt").open("wb") as err:
        subprocess.run([*adb, "shell", "-T", command], stdout=out, stderr=err, check=True, timeout=20)
    return path.read_bytes()


def identity(args):
    plan = json.loads(args.identity.read_text())
    build = plan.get("build") or plan.get("build_number")
    fingerprint = plan["fingerprint"]
    vold = plan.get("vold_sha256") or args.vold_sha256
    if args.vold_sha256:
        require(args.vold_sha256 == vold, "supplied vold SHA-256 differs from the identity")
    require(re.fullmatch(r"\d{10}", build or ""), "missing ten-digit image build")
    require(re.fullmatch(r"[0-9a-f]{64}", vold or ""), "require vold SHA-256 from actual system image")
    require(plan.get("serial", args.serial) == args.serial, "identity serial differs")
    marker = None if args.marker_absent else args.marker
    if "marker" in plan:
        require(plan["marker"] == marker, "marker expectation differs from the image validation record")
    return {"build": build, "fingerprint": fingerprint, "vold_sha256": vold, "marker": marker,
            "serial": args.serial, "source": str(args.identity.resolve())}


def snapshot(adb, expected, out, label):
    lines = checked(adb, SNAPSHOT, out / f"{label}-snapshot.txt").decode().splitlines()
    fields = {}
    for line in lines:
        key, sep, value = line.partition("=")
        require(sep, f"{label}: malformed snapshot line {line!r}")
        fields.setdefault(key, []).append(value)
    for key, value in {
        "probe_complete": "1", "uid": "0", "ro.product.device": "crux",
        "ro.boot.serialno": expected["serial"], "ro.build.type": "userdebug",
        "ro.build.version.incremental": expected["build"], "ro.build.fingerprint": expected["fingerprint"],
        "sys.boot_completed": "1", "sys.user.0.ce_available": "true", "init.svc.vold": "running",
        "ro.crypto.state": "encrypted", "ro.crypto.type": "file", "ro.crypto.volume.options": "::v2",
        "data_slave": "sda37", "vold_exe": "/system/bin/vold",
        "marker": "absent" if expected["marker"] is None else "present",
    }.items():
        require(fields.get(key) == [value], f"{label}: {key} expected {value!r}, got {fields.get(key)!r}")
    mount_sources = {}
    for row in fields.get("mount", []):
        columns = row.split()
        require(len(columns) >= 10 and columns[columns.index("-") + 1] == "ext4",
                f"{label}: invalid ext4 mountinfo row")
        mount_sources.setdefault(columns[4], []).append(columns[2])
    for mount, part in (("/", "pesystem"), ("/vendor", "pevendor"), ("/metadata", "pemetadata")):
        require(mount_sources.get(mount) == fields.get(part) and fields.get(part),
                f"{label}: {mount} does not resolve to {part}")
    require(len(mount_sources.get("/data", [])) == 1 and
            re.fullmatch(r"dm-\d+", fields.get("data_node", [""])[0]), f"{label}: /data is not PE dm-backed")
    require(any("peuserdata" in line and "fileencryption=ice::v2" in line and "formattable" in line and
                "keydirectory=/metadata/vold/metadata_encryption" in line for line in fields.get("fstab", [])),
            f"{label}: live PE v2/formattable fstab is missing")
    require([line.split()[0] for line in fields.get("vold_hash", [])] == [expected["vold_sha256"]] * 2,
            f"{label}: file or live vold digest differs from the image")
    if expected["marker"] is not None:
        actual = checked(adb, f"cat {MARKER}", out / f"{label}-marker.txt")
        require(actual == (expected["marker"] + "\n").encode(), f"{label}: exact marker bytes differ")
    boot_id, = fields.get("boot_id", [])
    require(re.fullmatch(r"[0-9a-f-]{36}", boot_id), f"{label}: boot ID is not readable")
    return boot_id


def observe(probe, boot_id, seconds, stream, evidence, clock=time.monotonic, sleep=time.sleep):
    started = clock()
    while True:
        observed, boot_completed = probe()
        elapsed = clock() - started
        evidence.append({"elapsed_seconds": round(elapsed, 3), "boot_id": observed, "boot_completed": boot_completed})
        require(observed == boot_id, f"device rebooted: boot ID {boot_id} → {observed}")
        require(boot_completed == "1", "boot_completed no longer 1")
        require(stream.poll() is None, "continuous logcat stream disconnected")
        if elapsed >= seconds:
            return elapsed
        sleep(min(5, seconds - elapsed))


def inspect_log(path):
    media = None
    lines = 0
    with path.open(errors="replace") as handle:
        for line in handle:
            lines += 1
            require(not FATAL.search(line), "zygote-fatal present in continuous logcat")
            if "vold" in line and "/data/media/0" in line and " v2 modes " in line and "policy" in line:
                media = line.strip()
    require(media is not None, "vold FBE v2 policy for /data/media/0 missing from logcat")
    return {"log_lines": lines, "media_v2": media, "zygote_fatal": False}


def run(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    record = {"status": "failed", "started_utc": datetime.now(timezone.utc).isoformat(), "samples": []}
    stream = None
    try:
        expected = record["expected"] = identity(args)
        adb = [args.adb, "-s", args.serial]
        with (out / "logcat.txt").open("wb") as log, (out / "logcat.stderr.txt").open("wb") as err:
            stream = subprocess.Popen([*adb, "logcat", "-b", "all", "-v", "epoch"], stdout=log, stderr=err)
            boot_id = snapshot(adb, expected, out, "initial")

            def poll():
                # Overwrite the local probe scratch only; each parsed sample is journaled below.
                return checked(adb, BOOT, out / "last-probe.txt").decode().splitlines()

            record["seconds_observed"] = observe(poll, boot_id, MIN_SECONDS, stream, record["samples"])
            require(snapshot(adb, expected, out, "final") == boot_id, "boot ID changed at the final snapshot")
            require(stream.poll() is None, "continuous logcat stream disconnected before final snapshot")
        stream.terminate()
        stream.wait(timeout=5)
        record.update(inspect_log(out / "logcat.txt"))
        record["status"] = "verified"
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        record["error"] = f"{type(error).__name__}: {error}"
    finally:
        if stream is not None and stream.poll() is None:
            stream.kill()
            stream.wait()
        record["finished_utc"] = datetime.now(timezone.utc).isoformat()
        (out / "verify-rom.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    return 0 if record["status"] == "verified" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", type=Path, required=True, help="A plan.json or B image-validation.json")
    parser.add_argument("--vold-sha256", help="Image-extracted vold digest when absent from the identity")
    parser.add_argument("--serial", default="8bd37df4")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--out", type=Path, required=True, help="Fresh local evidence directory")
    marker = parser.add_mutually_exclusive_group(required=True)
    marker.add_argument("--marker", help="Expected exact marker line without its trailing LF")
    marker.add_argument("--marker-absent", action="store_true", help="Use only when the target image has no marker")
    raise SystemExit(run(parser.parse_args()))
