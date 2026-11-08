"""Host admission and same-session boot checks for the RAM-only installer."""

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path


class RamTwrpError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise RamTwrpError(message)


SHA256 = re.compile(r"[0-9a-f]{64}")
V2_FIT_SHA256 = "3dab88d57ea205ac54db167282a0547461eee81a1e6eebdfdc528ba4e348e808"
V2_PROFILE_ID = "d9e985bf6ad11b6143787603898f041764813026d0da6d3b001c4cef2a8d4297"
# Device-specific, 4096-byte LUN 0 geometry. Image lengths are checked
# separately by the installer; the approved ext4 write need not fill a volume.
GEOMETRY = {
    "cache": (0x30000, 0x18000),
    "pesystem": (0x29E4000, 0x100000),
    "pevendor": (0x2AE4000, 0x70000),
    "peuserdata": (0x2B54000, 0x1000000),
    "pemetadata": (0x3B54000, 0x1000),
}
# The Boot identity U-Boot's PE ROM entry. It replaces the FIT's device
# identity with the values ABL passed, so it also boots identity-free FITs
# (2026110801 and later); without them, crux_ota refuses and nothing starts.
UBOOT_ROLE = "boot-crux-identity-1"
ROM_BOOT = ("scsi dev 0; scsi read 0xC0000000 0x3c000 0x3100; "
            "setenv fdt_high; setenv initrd_high; setenv bootargs; bootm start 0xC0000000; "
            "bootm loados; bootm ramdisk; if crux_ota bootargs; then bootm prep; run boot_go; fi")


def validate_manifest(path, expected_sha, ram):
    """Pin the approved manifest externally and verify the inline FIT locally."""
    require(path is not None and SHA256.fullmatch(expected_sha or ""),
            "provide --ram-twrp-manifest and its approved --ram-twrp-manifest-sha256")
    try:
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        require(digest == expected_sha, "RAM TWRP manifest SHA-256 differs")
        record = json.loads(data)
        require(isinstance(record, dict) and record.get("schema_version") == 1,
                "unsupported RAM TWRP manifest")
        ident = record.get("profile_id")
        require(isinstance(ident, str) and SHA256.fullmatch(ident), "invalid RAM TWRP profile ID")
        fit = record["fit"]
        image = Path(fit["path"])
        sha = fit["sha256"]
        require(image.is_absolute() and isinstance(sha, str) and SHA256.fullmatch(sha),
                "RAM TWRP manifest requires an absolute FIT path and SHA-256")
        payload = image.read_bytes()
        require(hashlib.sha256(payload).hexdigest() == sha, "RAM TWRP FIT SHA-256 differs")
        components = ram.fit_components(payload, "recovery")
    except RamTwrpError:
        raise
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as error:
        raise RamTwrpError(f"invalid RAM TWRP manifest or FIT: {error}") from error
    return {"manifest": str(path.resolve()), "manifest_sha256": digest,
            "profile_id": ident, "fit": str(image), "fit_sha256": sha,
            "components": components}


def read_environment(session, ram, expected_version):
    ram.verify_environment(session, expected_version)
    text = session.send("printenv serial# crux_uboot_role crux_boot_pe_rom")
    values = dict(re.findall(r"(?m)^(serial#|crux_uboot_role|crux_boot_pe_rom)=(.*)$", text))
    require(values.get("serial#") == ram.UBOOT_SERIAL, "unexpected U-Boot console identity")
    require(values.get("crux_uboot_role") == UBOOT_ROLE, "Boot partition U-Boot is not the identity build")
    require(values.get("crux_boot_pe_rom") == ROM_BOOT, "unexpected guarded ROM menu command")


def reboot_to_shell(ram, serial, out, expected_version):
    out.mkdir()
    session = ram.Session(out)
    try:
        session.run(["adb", "-s", serial, "reboot"], timeout=20)
        state = session.recover(session.start_offset, timeout=120, require_menu=True, strict=True)
        (out / "menu.json").write_text(json.dumps(state, indent=2) + "\n")
        read_environment(session, ram, expected_version)
        return session
    finally:
        session.save()


def boot_rom(ram, session):
    try:
        ram.guarded_handoff(session, "setenv crux_ota_extra; run crux_boot_pe_rom")
    finally:
        session.save()


def verify_geometry(session):
    device = session.send("scsi dev 0")
    require("Device 0: (0:0)" in device and re.search(r"\(\d+ x 4096\)", device),
            "LUN 0 does not report 4096-byte blocks")
    for name, (start, size) in GEOMETRY.items():
        text = session.send(f"if part start scsi 0 {name} pe_check_start && "
                            f"part size scsi 0 {name} pe_check_size; "
                            "then printenv pe_check_start pe_check_size; else false; fi")
        found = dict(re.findall(r"(?m)^(pe_check_(?:start|size))=([0-9a-fA-F]+)$", text))
        actual = tuple(int(found.get(f"pe_check_{key}", "0"), 16) for key in ("start", "size"))
        require(actual == (start, size), f"LUN 0 {name} geometry differs: {actual!r}")


def helper(argv, log, timeout):
    """Keep the child console even on failure; do not retry state transitions."""
    with log.open("x") as stream:
        try:
            completed = subprocess.run([sys.executable, *map(str, argv)], stdin=subprocess.DEVNULL,
                                       stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RamTwrpError(f"helper did not complete: {log}: {error}") from error
    require(completed.returncode == 0, f"helper failed: {log}")


def wait_recovery(serial, out, timeout=60):
    deadline = time.monotonic() + timeout
    with (out / "adb-ready.log").open("x") as log:
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(["adb", "-s", serial, "shell",
                                         "getprop ro.twrp.version; "
                                         "test -s /crux-ram-twrp-guard.ready && pidof recovery"],
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
                log.write(result.stdout + result.stderr)
                lines = result.stdout.splitlines()
                if (result.returncode == 0 and len(lines) == 2
                        and lines[0].startswith("3.7.1_12") and lines[1].strip().isdecimal()):
                    return
            except subprocess.TimeoutExpired:
                log.write("ADB readiness query timed out\n")
            time.sleep(1)
    raise RamTwrpError("protected RAM recovery did not reach the expected ADB readiness")


def boot(candidate, ram_script, install_script, out, expected_version, watcher_pid, serial):
    out.mkdir()
    helper([ram_script, "--fit", candidate["fit"], "--fit-sha256", candidate["fit_sha256"],
            "--out", out / "boot", "--mode", "recovery", "--resume-shell", "--strict-console",
            "--expected-uboot-version", expected_version, "--watcher-pid", watcher_pid],
           out / "ram-boot.log", 300)
    receipt = json.loads((out / "boot/result.json").read_text())
    require(receipt.get("status") == "kernel_handoff_adb_observed"
            and receipt.get("fit_sha256") == candidate["fit_sha256"]
            and receipt.get("fit_crc_verified") is True
            and receipt.get("component_hashes_verified") is True
            and receipt.get("watchdog_start_verified") is True
            and receipt.get("kernel_handoff_observed") is True
            and receipt.get("bounded_diagnostic") is False
            and receipt.get("partition_writes") is False,
            "RAM TWRP helper did not establish the guarded recovery handoff")
    wait_recovery(serial, out)
    helper([install_script, "--preflight-only", "--ram-twrp-id", candidate["profile_id"],
            "--out", out / "guard"], out / "runtime-guard.log", 100)
    guard = json.loads((out / "guard/result.json").read_text())
    require(guard.get("status") == "protected_ram_twrp_preflight_verified"
            and guard.get("ram_twrp_id") == candidate["profile_id"],
            "RAM TWRP runtime guard did not pass")
