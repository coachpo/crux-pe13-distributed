#!/usr/bin/env python3
"""Install fresh raw PE system/vendor and discard only the Crux PE data volumes.

Execution requires --discard-pe-data together with --ram-twrp-id: writes run
only inside the protected RAM-only TWRP (3.7.1_12 with the retained CI kernel
e45a24f31ee0) on serial 8bd37df4, after its storage guard and runtime identity
pass. --preflight-only runs that check without writes. --dry-run validates host
images and prints the exact checks/operations without any ADB call. The
installer does not boot TWRP or write original partitions.
Transfers run in bounded host subprocesses and print progress every 30 seconds;
the caller can run this script in an asynchronous exec session and retain its
command/output journal. Failed phases stop; there is no automatic retry.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import time
import uuid


SERIAL = "8bd37df4"
PARTITIONS = {
    "pesystem": {"node": "sda35", "bytes": 4 * 1024**3},
    "pevendor": {"node": "sda36", "bytes": 7 * 1024**3 // 4},
    "peuserdata": {"node": "sda37", "bytes": 64 * 1024**3},
    "pemetadata": {"node": "sda38", "bytes": 16 * 1024**2},
}
# LUN 0 GPT positions in Linux sysfs's 512-byte sectors, not U-Boot blocks.
PE_STARTS = {"pesystem": 0x29e4000 * 8, "pevendor": 0x2ae4000 * 8,
             "peuserdata": 0x2b54000 * 8, "pemetadata": 0x3b54000 * 8}
PROTECTED = {"userdata": "sda34", "metadata": "sda19", "misc": "sda11",
             "cache": "sda29", "system": "sda33", "vendor": "sda32",
             "boot": "sde49", "recovery": "sda28", "dtbo": "sde45", "vbmeta": "sde10"}
RAM_TWRP_READY = "/crux-ram-twrp-guard.ready"
RAM_TWRP_CAP_BOUND = "00000000000000cb"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def validate_image(path, expected_sha256, partition):
    require(re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256), "Invalid supplied SHA-256")
    size = path.stat().st_size
    require(2048 <= size <= PARTITIONS[partition]["bytes"], f"{partition}: raw image exceeds partition or is too small")
    with path.open("rb") as handle:
        header = handle.read(2048)
        require(struct.unpack_from("<I", header)[0] != 0xED26FF3A,
                f"{partition}: Android sparse image rejected; supply expanded raw ext4")
        superblock = header[1024:2048]
        require(struct.unpack_from("<H", superblock, 56)[0] == 0xEF53,
                f"{partition}: missing ext4 superblock magic")
        log_block_size = struct.unpack_from("<I", superblock, 24)[0]
        require(log_block_size <= 6, f"{partition}: invalid ext4 block size")
        block_size = 1024 << log_block_size
        blocks = struct.unpack_from("<I", superblock, 4)[0]
        incompat = struct.unpack_from("<I", superblock, 96)[0]
        if incompat & 0x80:
            blocks |= struct.unpack_from("<I", superblock, 0x150)[0] << 32
        filesystem_bytes = blocks * block_size
        require(0 < filesystem_bytes <= size and size % block_size == 0,
                f"{partition}: filesystem size does not fit complete raw image")
        handle.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    require(actual == expected_sha256.lower(), f"{partition}: host SHA-256 mismatch")
    return {"path": str(path), "bytes": size, "sha256": actual,
            "filesystem_bytes": filesystem_bytes, "block_size": block_size,
            "partition_bytes": PARTITIONS[partition]["bytes"]}


def check_unmounted(node, label, guarded):
    mountinfo = "/proc/self/mountinfo"
    if guarded:
        mountinfo += " /proc/$recovery_pid/mountinfo"
    return [
        f'dev=$(cat /sys/class/block/{node}/dev)',
        f'awk -v d="$dev" \'$3 == d {{exit 1}}\' {mountinfo}'
        f' || {{ echo "Partition mounted or mountinfo unavailable: {label}"; exit 1; }}',
        f'test -d /sys/class/block/{node}/holders',
        f'for holder in /sys/class/block/{node}/holders/*; do [ ! -e "$holder" ] || '
        f'{{ echo "Partition held by $holder: {label}"; exit 1; }}; done',
    ]


def ram_twrp_guard(profile_id):
    require(re.fullmatch(r"[0-9a-f]{64}", profile_id), "Invalid RAM TWRP profile ID")
    lines = [
        f'[ "$(cat {RAM_TWRP_READY})" = "{profile_id}" ]'
        ' || { echo "RAM TWRP profile/early guard witness differs"; exit 1; }',
        'recovery_pid=$(pidof recovery)',
        'case "$recovery_pid" in ""|*[!0-9]*) echo "Expected a single TWRP process"; exit 1 ;; esac',
        '[ "$(readlink /proc/$recovery_pid/exe)" = /system/bin/recovery ]',
        'recovery_capbnd=$(awk \'$1 == "CapBnd:" {print $2}\' /proc/$recovery_pid/status)',
        f'[ "$recovery_capbnd" = "{RAM_TWRP_CAP_BOUND}" ]'
        ' || { echo "Unexpected TWRP capability bounding set: $recovery_capbnd"; exit 1; }',
    ]
    for name, node in PROTECTED.items():
        target = f"/dev/block/by-name/{name}"
        lines += [
            f'[ "$(readlink -f {target})" = /dev/block/{node} ]',
            f'[ "$(blockdev --getro {target})" = 1 ]'
            f' || {{ echo "Protected partition is not read-only: {name}"; exit 1; }}',
            *check_unmounted(node, name, guarded=True),
            f'echo "PROTECTED_RO {name} {node}"',
        ]
    lines += [
        '[ "$(cat /sys/class/block/sda/queue/logical_block_size)" = 4096 ]',
        f'echo "RAM_TWRP_OK {profile_id} CapBnd=$recovery_capbnd"',
    ]
    return lines


def device_guard(ram_twrp_id=None):
    lines = [
        'set -e',
        '[ "$(id -u)" = 0 ]',
        '[ "$(getprop ro.product.device)" = crux ]',
        'case "$(getprop ro.twrp.version)" in 3.7.1_12*) ;; *) echo "Unexpected TWRP"; exit 1 ;; esac',
        'case "$(uname -r)" in 4.14.305*e45a24f31ee0*) ;; *) echo "Unexpected kernel"; exit 1 ;; esac',
        'case " $(cat /proc/cmdline) " in *" androidboot.serialno=8bd37df4 "*) ;; *) echo "Unexpected serial"; exit 1 ;; esac',
        'test -r /proc/self/mountinfo',
        'test -x /system/bin/mke2fs',
        'test -x /system/bin/e2fsck',
        'test -x /system/bin/tune2fs',
    ]
    if ram_twrp_id is not None:
        lines += ram_twrp_guard(ram_twrp_id)
    for name, info in PARTITIONS.items():
        node = info["node"]
        target = f"/dev/block/by-name/{name}"
        lines += [
            f'[ "$(readlink -f {target})" = /dev/block/{node} ]',
            f'test -b {target}',
            f'[ "$(blockdev --getsize64 {target})" = {info["bytes"]} ]',
            *check_unmounted(node, name, guarded=ram_twrp_id is not None),
        ]
        if ram_twrp_id is not None:
            lines += [
                f'[ "$(cat /sys/class/block/{node}/start)" = {PE_STARTS[name]} ]',
                f'[ "$(blockdev --getro {target})" = 0 ]'
                f' || {{ echo "PE target is not writable: {name}"; exit 1; }}',
            ]
        lines.append(f'echo "PARTITION_OK {name} {node} {info["bytes"]}"')
    return "\n".join(lines)


def format_command(name):
    require(name in ("peuserdata", "pemetadata"), "Formatting target must be a PE data volume")
    block_count = PARTITIONS[name]["bytes"] // 4096
    if name == "peuserdata":
        # PE fs_mgr prepares 512-byte inodes for project IDs and enables
        # usr/group/project quotas. Explicit quotatype avoids mke2fs's default
        # usr/group-only quota set. These flags preserve the source FBE contract.
        options = "-I 512 -O encrypt,quota,project -E nodiscard,quotatype=usr:grp:prj"
    else:
        options = "-I 256 -E nodiscard"
    return f"/system/bin/mke2fs -F -t ext4 -b 4096 -m 0 {options} /dev/block/by-name/{name} {block_count}"


class Installer:
    def __init__(self, adb, out, result):
        self.adb, self.out, self.result = adb, out, result
        self.guard = device_guard(result.get("ram_twrp_id"))
        self.commands = []
        self.remote_log_dir = "/tmp/crux-pe13-install-" + uuid.uuid4().hex

    def save(self):
        (self.out / "commands.json").write_text(json.dumps(self.commands, indent=2) + "\n")
        (self.out / "result.json").write_text(json.dumps(self.result, indent=2) + "\n")

    def run(self, label, script, timeout=60, input_path=None):
        marker = f"CRUX_PHASE_OK_{label}"
        script += "\necho " + marker
        remote_log = None
        if input_path:
            # Shell v2 closes stdin independently and returns the remote
            # exit status; raw exec-in omits that status. Keep the
            # small transfer log in recovery RAM for post-transfer inspection.
            remote_log = f"{self.remote_log_dir}/{label}.txt"
            script = f"mkdir -p {self.remote_log_dir}\n(\n{script}\n) > {remote_log} 2>&1"
        argv = ([self.adb, "-s", SERIAL, "shell", "-T", script] if input_path else
                [self.adb, "-s", SERIAL, "exec-out", script])
        stdout_path, stderr_path = self.out / f"{label}.stdout.txt", self.out / f"{label}.stderr.txt"
        record = {"label": label, "argv": argv, "timeout_s": timeout,
                  "stdin_file": str(input_path) if input_path else None,
                  "remote_transfer_log": remote_log,
                  "stdout_file": str(stdout_path), "stderr_file": str(stderr_path)}
        self.commands.append(record)
        self.result["phase"] = label
        self.save()
        print(f"Starting {label}", flush=True)
        started = time.monotonic()
        process = None
        source = None
        try:
            if input_path:
                source = input_path.open("rb")
            with stdout_path.open("wb") as output, stderr_path.open("wb") as errors:
                process = subprocess.Popen(argv, stdin=source or subprocess.DEVNULL,
                                           stdout=output, stderr=errors)
                deadline = started + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    require(remaining > 0, f"{label}: timeout after {timeout} seconds")
                    try:
                        record["exit_code"] = process.wait(timeout=min(30, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        print(f"{label}: {time.monotonic() - started:.0f}s elapsed", flush=True)
            text = stdout_path.read_text(errors="replace")
            require(record["exit_code"] == 0, f"{label}: ADB exited {record['exit_code']}; see stderr log")
            if input_path:
                collection = ("set -e\ni=0\nwhile [ $i -lt 30 ]; do\n"
                              f"if [ -f {remote_log} ] && grep -q '^{marker}$' {remote_log}; then break; fi\n"
                              "sleep 1\ni=$((i + 1))\ndone\n"
                              f"cat {remote_log}\ngrep -q '^{marker}$' {remote_log}")
                record["remote_output_journal"] = f"{label}_output.stdout.txt"
                text = self.run(label + "_output", collection, timeout=45)
            # The raw exec service may omit the remote exit status. Requiring
            # a marker after set -e also verifies completion of the shell phase.
            require(re.search(r"^" + marker + r"\r?$", text, re.M), f"{label}: remote completion marker missing")
            record["remote_completion_verified"] = True
            return text
        except Exception as error:
            record["error"] = f"{type(error).__name__}: {error}"
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            raise
        finally:
            if source:
                source.close()
            record["elapsed_s"] = time.monotonic() - started
            self.save()

    def preflight(self):
        config = ("\nif [ -f /system/etc/mke2fs.conf ]; then cat /system/etc/mke2fs.conf; "
                  "elif [ -f /etc/mke2fs.conf ]; then cat /etc/mke2fs.conf; fi")
        return self.run("preflight", self.guard + "\ngetprop ro.twrp.version\nuname -a\ncat /proc/self/mountinfo" + config)

    def write_image(self, name, image, timeout):
        require(name in ("pesystem", "pevendor"), "Raw image target must be PE system/vendor")
        # Verify the host image again immediately before using this file as
        # stdin. Device readback below must match this supplied hash as well.
        validate_image(Path(image["path"]), image["sha256"], name)
        # TWRP's Toybox 0.8.4 dd can overrun its single bs= buffer on partial
        # pipe reads. Separate ibs/obs allocate room for accumulated fragments.
        # count_bytes still limits the transfer to the exact image length.
        script = self.guard + (f"\ndd of=/dev/block/by-name/{name} "
                                   f"ibs=1048576 obs=1048576 count={image['bytes']} iflag=count_bytes conv=fsync\nsync")
        self.result["device_writes_attempted"].append(name)
        self.run(f"write_{name}", script, timeout=timeout, input_path=Path(image["path"]))
        output = self.run(f"verify_{name}", self.guard +
                          f"\nhead -c {image['bytes']} /dev/block/by-name/{name} | sha256sum", timeout=timeout)
        hashes = re.findall(r"(?m)^([0-9a-fA-F]{64})\s+", output)
        require(len(hashes) == 1 and hashes[0].lower() == image["sha256"], f"{name}: device readback SHA-256 mismatch")
        self.result["device_images"][name] = {"written_bytes": image["bytes"], "readback_sha256": hashes[0].lower(),
                                              "readback_verified": True}
        self.save()

    def format_volume(self, name, timeout):
        self.result["device_writes_attempted"].append(name)
        self.run(f"format_{name}", self.guard + "\n" + format_command(name) + "\nsync", timeout=timeout)
        script = self.guard + f"\n/system/bin/e2fsck -fn /dev/block/by-name/{name}\n/system/bin/tune2fs -l /dev/block/by-name/{name}"
        output = self.run(f"verify_{name}", script, timeout=timeout)
        def field(label):
            match = re.search(r"(?m)^" + re.escape(label) + r":\s*(.*)$", output)
            require(match, f"{name}: missing filesystem field {label}")
            return match.group(1).strip()
        features = field("Filesystem features").split()
        block_size, blocks = int(field("Block size")), int(field("Block count"))
        require(block_size == 4096 and blocks * block_size == PARTITIONS[name]["bytes"],
                f"{name}: formatted filesystem size differs from PE partition")
        inode_size = int(field("Inode size"))
        require(inode_size == (512 if name == "peuserdata" else 256), f"{name}: wrong inode size")
        require("has_journal" in features and "extent" in features, f"{name}: missing ext4 journal/extent features")
        if name == "peuserdata":
            require({"encrypt", "quota", "project"}.issubset(features), "PE userdata lost encryption/quota/project features")
            for label in ("User quota inode", "Group quota inode", "Project quota inode"):
                require(int(field(label)) > 0, f"Missing {label}")
        self.result["formatted_volumes"][name] = {"filesystem_bytes": blocks * block_size,
                                                  "inode_size": inode_size, "features": features,
                                                  "e2fsck_read_only_passed": True}
        self.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", type=Path, help="Expanded raw ext4 PE system image")
    parser.add_argument("--system-sha256")
    parser.add_argument("--vendor", type=Path, help="Expanded raw ext4 PE vendor image")
    parser.add_argument("--vendor-sha256")
    parser.add_argument("--out", type=Path, required=True, help="New operation journal directory")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--discard-pe-data", action="store_true", help="Explicitly permit replacing PE system/vendor and formatting PE userdata/metadata; requires --ram-twrp-id")
    mode.add_argument("--dry-run", action="store_true", help="Host input validation and plan only; no ADB/device access")
    mode.add_argument("--preflight-only", action="store_true", help="Read-only protected RAM TWRP check; no image or partition writes")
    parser.add_argument("--ram-twrp-id", help="Require the protected RAM TWRP profile with this identity (mandatory with --discard-pe-data and --preflight-only)")
    sdk_adb = Path.home() / "Library/Android/sdk/platform-tools/adb"
    parser.add_argument("--adb", default=shutil.which("adb") or str(sdk_adb))
    parser.add_argument("--transfer-timeout", type=int, default=900, help="Maximum seconds for each write/readback phase")
    parser.add_argument("--format-timeout", type=int, default=300, help="Maximum seconds for each format/check phase")
    args = parser.parse_args(argv)
    require(args.transfer_timeout > 0 and args.format_timeout > 0, "Timeouts must be positive")
    if args.discard_pe_data and args.ram_twrp_id is None:
        parser.error("--discard-pe-data requires --ram-twrp-id; writes run only in the protected RAM TWRP")
    image_args = (args.system, args.system_sha256, args.vendor, args.vendor_sha256)
    if args.preflight_only:
        if args.ram_twrp_id is None or any(image_args):
            parser.error("--preflight-only requires --ram-twrp-id and takes no image arguments")
        images = {}
    else:
        if not all(image_args):
            parser.error("supply both raw images and their SHA-256 digests")
        images = {"pesystem": validate_image(args.system.resolve(), args.system_sha256, "pesystem"),
                  "pevendor": validate_image(args.vendor.resolve(), args.vendor_sha256, "pevendor")}
    plan = {"serial": SERIAL, "partitions": PARTITIONS, "images": images,
            "ram_twrp_id": args.ram_twrp_id,
            "guard": device_guard(args.ram_twrp_id), "format_commands": {name: format_command(name) for name in ("peuserdata", "pemetadata")},
            "source_format_contract": {"file_encryption": "ice", "quota_types": ["usr", "grp", "prj"],
                                       "userdata_inode_size": 512, "discard": False, "lazy_init": "native defaults"},
            "boot_dtbo_cache_miui_gpt_writes": False}
    if args.dry_run:
        print(json.dumps(dict(plan, dry_run=True), indent=2))
        return 0
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    result = dict(plan, status="preflight", discard_pe_data_authorized=args.discard_pe_data,
                  device_writes_attempted=[], device_images={}, formatted_volumes={}, hardware_rom_boot_verified=False)
    installer = Installer(args.adb, args.out, result)
    try:
        installer.preflight()
        result["preflight_passed"] = True
        if args.preflight_only:
            result["status"] = "protected_ram_twrp_preflight_verified"
            print(json.dumps(result, indent=2), flush=True)
            return 0
        installer.save()
        for name in ("pesystem", "pevendor"):
            installer.write_image(name, images[name], args.transfer_timeout)
        for name in ("peuserdata", "pemetadata"):
            installer.format_volume(name, args.format_timeout)
        result.update(status="fresh_pe_installed_readback_and_format_verified", phase="complete")
        print(json.dumps(result, indent=2), flush=True)
        return 0
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        print(result["error"], flush=True)
        return 1
    finally:
        installer.save()


if __name__ == "__main__":
    raise SystemExit(main())
