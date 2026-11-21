#!/usr/bin/env python3
"""Plan and run a host-driven direct install of a reviewed local PE13 payload.

`plan` checks explicitly supplied raw system/vendor images, the paired
Recovery/ROM FITs and their host identity record against the image bytes, pads
the FITs to the fixed PE cache windows and writes a review plan into a new
directory. It never contacts a device. `--compare-with` checks that the new
plan has the same bytes and ranges as an earlier reviewed plan.

`install --dry-run` revalidates a reviewed plan by SHA-256, the pinned RAM-only
TWRP v2 manifest and the protected installer's own dry run, without a device.
`install --execute` also requires the exact starting ROM (serial, build,
fingerprint and marker), exactly one existing console watcher, an exclusive
device window and separate write authority. It runs a fresh no-install-write
rehearsal, rechecks LUN 0 geometry, writes and reads back the two PE FIT
windows with cache-neighbor CRCs, stages the same protected RAM TWRP to write
and read back pesystem/pevendor and format peuserdata/pemetadata, then
watchdog-boots the new ROM and checks a fresh boot and its identity. It never
boots the cache TWRP, writes original partitions, retries a failed phase or
rolls back. Runtime acceptance is the separate verify-pe-rom.py run named in
result.json.

`install --rehearse` takes the same inputs as `--execute` but stops after the
starting-ROM check and the no-install-write rehearsal. The first device use of
this tool or of changed write windows starts there; writes need separate
authority afterwards.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

SCRIPTS = Path(__file__).resolve().parent
WORKSPACE = SCRIPTS.parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(WORKSPACE / "crux-pe13-distributed/scripts"))
import pe_fits as fits  # noqa: E402
import ram_twrp as recovery  # noqa: E402

RAM = SCRIPTS / "ram-boot-pe13.py"
INSTALL = SCRIPTS / "install-pe13.py"
VERIFY = WORKSPACE / "out/crux-pe13-device-validation-2026-11-05/device/verify-pe-rom.py"
UBOOT_VERSION = "2026.10-rc5-00618-g5ba2e98a115a"
PLAN_SCHEMA = "crux-pe13-direct-install-plan/1"
MARKER = "/system/etc/crux-delivery-verification.txt"
BOOT_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
FINGERPRINT = re.compile(r"Xiaomi/crux/crux:13/[^/]+/\d+:userdebug/test-keys")
FIT_TARGETS = {"recovery": "cache / PE Recovery FIT", "rom": "cache / PE ROM FIT"}
FORMAT_OPERATION = "format PE ext4; allowed scope is the volume, not a full raw overwrite"
DATA_EFFECT = "PE data and encryption metadata are lost. No other target is authorized. No release upload."
require = fits.require


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load_module("install_pe13", INSTALL)


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def debugfs_tool():
    found = shutil.which("debugfs")
    if found:
        return found
    brew = Path("/opt/homebrew/opt/e2fsprogs/sbin/debugfs")
    return str(brew) if brew.is_file() else None


def image_file(image, path, tool, missing_ok=False):
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "file"
        completed = subprocess.run([tool, "-R", f"dump {path} {target}", str(image)],
                                   capture_output=True, timeout=180)
        error = completed.stderr.decode(errors="replace")
        if missing_ok and completed.returncode == 0 and not target.exists() and "File not found" in error:
            return None
        require(completed.returncode == 0 and target.is_file(), f"cannot read {path} from {image}: {error}")
        return target.read_bytes()


def image_identity(system, identity, tool):
    """Bind the identity record to the actual system image bytes."""
    props = dict(line.split("=", 1) for line in image_file(system, "/system/build.prop", tool).decode().splitlines()
                 if "=" in line and not line.startswith("#"))
    require(props.get("ro.build.version.incremental") == identity["build_number"] and
            props.get("ro.build.fingerprint") == identity["fingerprint"] and
            props.get("ro.product.system.name") == "aosp_crux" and props.get("ro.build.type") == "userdebug",
            "system image build.prop differs from the identity record")
    require(FINGERPRINT.fullmatch(identity["fingerprint"]), "identity is not a Crux Dev test-keys fingerprint")
    marker = image_file(system, MARKER, tool, missing_ok=True)
    marker = None if marker is None else marker.decode()
    expected = identity.get("marker")
    require(marker == (None if expected is None else expected + "\n"),
            f"system image marker differs from the identity record: {marker!r}")
    return hashlib.sha256(image_file(system, "/system/bin/vold", tool)).hexdigest()


def hexa(value):
    return f"0x{value:x}"


def cache_ranges(recovery_fit, rom_fit, twrp_crc):
    labels = {"miui": "MIUI", "twrp": "TWRP"}
    rows = []
    for name, start, count in fits.neighbors(recovery_fit, rom_fit):
        target = "tail" if name == "gap" and start + count == fits.CACHE_END else labels.get(name, name)
        row = {"target": target, "lba": hexa(start), "blocks": hexa(count)}
        if target == "TWRP":
            row["expected_retained_crc32"] = twrp_crc
        rows.append(row)
    return rows


def make_plan(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    identity = load(args.identity)
    for key in ("build_number", "fingerprint"):
        require(isinstance(identity.get(key), str) and identity[key], f"identity has no {key}")
    require(re.fullmatch(r"\d{10}", identity["build_number"]), "identity build number is not 10 digits")
    manifest = recovery.validate_manifest(args.ram_twrp_manifest, args.ram_twrp_manifest_sha256,
                                          fits.load_ram(WORKSPACE))
    require(manifest["fit_sha256"] == recovery.V2_FIT_SHA256 and manifest["profile_id"] == recovery.V2_PROFILE_ID,
            "plans use only the accepted RAM TWRP v2 FIT and profile")
    twrp = load(args.ram_twrp_manifest)["retained_fit"]
    require(fits.digest(twrp["path"]) == twrp["sha256"] == fits.RETAINED_TWRP_SHA256,
            "retained TWRP CRC-witness FIT differs")
    twrp_crc = fits.crc_bytes(fits.padded(twrp["path"], fits.TWRP["blocks"]))
    require(twrp_crc == fits.RETAINED_TWRP_CRC32, "retained TWRP witness CRC differs")
    raw = {}
    for part, path in (("pesystem", args.system), ("pevendor", args.vendor)):
        raw[part] = installer.validate_image(path.resolve(), fits.digest(path), part)
    tool = debugfs_tool()
    require(tool, "debugfs (e2fsprogs) is required to bind the identity record to the system image")
    vold = image_identity(args.system.resolve(), identity, tool)
    plan_fits, writes = {}, []
    for role, source in (("recovery", args.recovery_fit), ("rom", args.rom_fit)):
        lba, blocks = fits.WRITE_WINDOWS[role]
        source = source.resolve()
        data = fits.padded(source, blocks)
        stage = out / f"{source.stem}.padded.bin"
        stage.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        plan_fits[role] = {"path": str(stage), "sha256": digest, "lba": lba, "blocks": blocks}
        writes.append({"target": FIT_TARGETS[role], "lba": hexa(lba), "blocks": hexa(blocks),
                       "last_lba": hexa(lba + blocks - 1), "padded_sha256": digest, "path": str(stage),
                       "source": str(source), "source_bytes": source.stat().st_size,
                       "expected_readback_crc32": fits.crc_bytes(data)})
    for part in ("pesystem", "pevendor", "peuserdata", "pemetadata"):
        start, limit = recovery.GEOMETRY[part]
        target = f"{part} / {installer.PARTITIONS[part]['node']}"
        if part in raw:
            image = raw[part]
            require(image["bytes"] % 4096 == 0 and image["bytes"] // 4096 <= limit,
                    f"{part}: raw image is not whole 4096-byte blocks inside the volume")
            blocks = image["bytes"] // 4096
            writes.append({"target": target, "lba": hexa(start), "blocks": hexa(blocks),
                           "last_lba": hexa(start + blocks - 1), "bytes": image["bytes"],
                           "sha256": image["sha256"], "path": image["path"]})
        else:
            writes.append({"target": target, "operation": FORMAT_OPERATION, "lba": hexa(start),
                           "volume_blocks": hexa(limit), "last_lba": hexa(start + limit - 1)})
    plan = {
        "schema": PLAN_SCHEMA,
        "status": "host_plan_only_no_write_authority",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "serial": args.serial,
        "geometry_must_be_rechecked_before_write": True,
        "block_bytes": 4096,
        "lun": 0,
        "writes_in_order": writes,
        "cache_crc_witness_ranges": cache_ranges(plan_fits["recovery"], plan_fits["rom"], twrp_crc),
        "data_effect": DATA_EFFECT,
        "target_build": identity["build_number"],
        "target_fingerprint": identity["fingerprint"],
        "target_marker": identity.get("marker"),
        "identity": {"path": str(args.identity.resolve()), "sha256": fits.digest(args.identity)},
        "ram_twrp_manifest": {"path": manifest["manifest"], "sha256": manifest["manifest_sha256"]},
        "recovery_profile_id": manifest["profile_id"],
        "fits": plan_fits,
        "raw": raw,
        "expected_vold_sha256": vold,
    }
    if args.compare_with:
        compare_plans(plan, load(args.compare_with))
    save(out / "write-plan.json", plan)
    print(json.dumps({"plan": str(out / "write-plan.json"), "plan_sha256": fits.digest(out / "write-plan.json"),
                      "matches_reviewed_plan": str(args.compare_with.resolve()) if args.compare_with else None},
                     indent=2))
    return 0


def identity_view(plan):
    """Bytes and ranges that a reviewed plan approves; paths may differ."""
    paths = {"path", "source"}
    return {
        "scalars": {key: plan.get(key) for key in ("serial", "lun", "block_bytes", "target_build",
                                                  "target_fingerprint", "target_marker",
                                                  "recovery_profile_id", "expected_vold_sha256")},
        "writes": [{key: value for key, value in row.items() if key not in paths}
                   for row in plan["writes_in_order"]],
        "ranges": plan["cache_crc_witness_ranges"],
        "fits": {role: {key: item[key] for key in ("sha256", "lba", "blocks")}
                 for role, item in plan["fits"].items()},
        "raw": {part: {key: value for key, value in item.items() if key != "path"}
                for part, item in plan["raw"].items()},
    }


def compare_plans(plan, reviewed):
    actual, expected = identity_view(plan), identity_view(reviewed)
    for section in actual:
        require(actual[section] == expected[section], f"plan differs from the reviewed plan: {section}")


def host_plan(args, out, ram):
    reviewed_bytes = args.plan.read_bytes()
    digest = hashlib.sha256(reviewed_bytes).hexdigest()
    require(args.plan_sha256 == digest, "reviewed write-plan SHA-256 differs")
    plan = json.loads(reviewed_bytes)
    identity = load(args.identity)
    require(plan["serial"] == args.serial == ram.SERIAL and plan["lun"] == 0 and plan["block_bytes"] == 4096,
            "reviewed serial or geometry differs")
    require(plan["target_build"] == identity["build_number"] and
            plan["target_fingerprint"] == identity["fingerprint"] and
            plan.get("target_marker") == identity.get("marker"),
            "reviewed target differs from the image identity record")
    require(re.fullmatch(r"\d{10}", plan["target_build"]) and FINGERPRINT.fullmatch(plan["target_fingerprint"]),
            "reviewed target is not a Crux Dev test-keys build")
    if "identity" in plan:
        require(plan["identity"]["sha256"] == fits.digest(args.identity), "identity record differs from the plan")
    candidate = recovery.validate_manifest(args.ram_twrp_manifest, args.ram_twrp_manifest_sha256, ram)
    require(candidate["fit_sha256"] == recovery.V2_FIT_SHA256 and
            candidate["profile_id"] == recovery.V2_PROFILE_ID == plan["recovery_profile_id"],
            "install requires the accepted RAM TWRP v2 FIT and profile")
    operations = plan["writes_in_order"]
    require(len(operations) == 6 and set(plan["raw"]) == {"pesystem", "pevendor"},
            "plan must describe exactly two FITs, two raw images, and two PE formats")
    for role, row in zip(("recovery", "rom"), operations[:2]):
        item = plan["fits"][role]
        require(row["target"] == FIT_TARGETS[role] and (item["lba"], item["blocks"]) == fits.WRITE_WINDOWS[role] and
                (hexa(item["lba"]), hexa(item["blocks"]), item["sha256"]) ==
                (row["lba"], row["blocks"], row["padded_sha256"]),
                f"{role} differs from its fixed PE FIT write window")
        path = Path(item["path"])
        require(path == Path(row["path"]) and path.stat().st_size == item["blocks"] * 4096,
                f"{role} stage file is not the exact padded write size")
        payload = fits.padded(path, item["blocks"])
        require(hashlib.sha256(payload).hexdigest() == item["sha256"] and
                fits.crc_bytes(payload) == row["expected_readback_crc32"], f"{role} stage file differs")
    for part, row in zip(("pesystem", "pevendor", "peuserdata", "pemetadata"), operations[2:]):
        start, limit = recovery.GEOMETRY[part]
        blocks = int(row["blocks"] if part in plan["raw"] else row["volume_blocks"], 16)
        require(row["target"] == f"{part} / {installer.PARTITIONS[part]['node']}" and int(row["lba"], 16) == start and
                0 < blocks <= limit and int(row["last_lba"], 16) == start + blocks - 1,
                f"{part} range differs")
        if part in plan["raw"]:
            image = plan["raw"][part]
            require(all(image[key] == row[key] for key in ("path", "bytes", "sha256")) and
                    row["bytes"] == blocks * 4096, f"{part} raw image differs from its reviewed range")
        else:
            require(blocks == limit and row["operation"] == FORMAT_OPERATION, f"{part} format scope differs")
    twrp = load(args.ram_twrp_manifest)["retained_fit"]
    require(fits.digest(twrp["path"]) == twrp["sha256"] == fits.RETAINED_TWRP_SHA256,
            "retained TWRP CRC-witness FIT differs")
    expected_ranges = cache_ranges(plan["fits"]["recovery"], plan["fits"]["rom"], fits.RETAINED_TWRP_CRC32)
    require(plan["cache_crc_witness_ranges"] == expected_ranges, "cache witness ranges differ")
    require(re.fullmatch(r"[0-9a-f]{64}", plan.get("expected_vold_sha256") or ""), "plan has no vold digest")
    log = out / "installer-dry-run.json"
    recovery.helper([*install_args(plan, out / "unused", candidate["profile_id"]), "--dry-run"], log, 180)
    preview = load(log)
    require(preview["dry_run"] is True and preview["images"] == plan["raw"] and
            preview["ram_twrp_id"] == candidate["profile_id"], "protected installer dry-run differs from the plan")
    return plan, digest, candidate, twrp


def install_args(plan, out, profile):
    return [INSTALL, "--system", plan["raw"]["pesystem"]["path"],
            "--system-sha256", plan["raw"]["pesystem"]["sha256"],
            "--vendor", plan["raw"]["pevendor"]["path"],
            "--vendor-sha256", plan["raw"]["pevendor"]["sha256"],
            "--ram-twrp-id", profile, "--out", out]


def adb(serial, *argv, timeout=15):
    return subprocess.run(["adb", "-s", serial, *argv], stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=timeout)


def read_rom(serial, out, build, fingerprint, marker):
    """Return the boot ID after checking serial, build, mounts and marker."""
    out.mkdir(exist_ok=True)
    checked = adb(serial, "shell", "-T",
                  "set -e; getprop ro.boot.serialno; getprop ro.product.device; getprop sys.boot_completed; "
                  "getprop ro.build.version.incremental; getprop ro.build.fingerprint; "
                  "cat /proc/sys/kernel/random/boot_id; cat /proc/mounts")
    (out / "rom.txt").write_bytes(checked.stdout + checked.stderr)
    lines = checked.stdout.decode().splitlines()
    require(checked.returncode == 0 and lines[:5] == [serial, "crux", "1", build, fingerprint] and
            len(lines) > 6 and BOOT_ID.fullmatch(lines[5]), "ROM ADB serial/build/fingerprint/boot ID differs")
    for part, mount in (("pesystem", "/"), ("pevendor", "/vendor"), ("pemetadata", "/metadata")):
        require(any(re.fullmatch(rf"/dev/block/(?:bootdevice/)?by-name/{part} {mount} ext4 .+", line)
                    for line in lines[6:]), f"ROM mount {mount} is not {part}")
    command = (f'test -r /system/etc && test -x /system/etc && test ! -e {MARKER} && test ! -L {MARKER}'
               if marker is None else f"test -f {MARKER} && test ! -L {MARKER} && cat {MARKER}")
    actual = adb(serial, "shell", "-T", command)
    (out / "marker.txt").write_bytes(actual.stdout + actual.stderr)
    require(actual.returncode == 0 and actual.stdout == (b"" if marker is None else (marker + "\n").encode()),
            "ROM image marker differs")
    return lines[5]


def wait_boot(serial, out):
    out.mkdir(exist_ok=True)
    deadline = time.monotonic() + 300
    with (out / "boot-completed.log").open("x") as log:
        while time.monotonic() < deadline:
            try:
                result = adb(serial, "shell", "-T", "getprop sys.boot_completed", timeout=8)
                log.write(f"{result.returncode}: {(result.stdout + result.stderr).decode(errors='replace')!r}\n")
                if result.returncode == 0 and result.stdout.strip() == b"1":
                    return
            except subprocess.TimeoutExpired:
                log.write("ADB query timed out\n")
            time.sleep(2)
    raise RuntimeError("boot_completed=1 was not observed")


def execute(args, out, ram, plan, candidate, twrp, state):
    def phase(name):
        state["phase"] = name
        save(out / "result.json", state)

    serial, version = args.serial, args.expected_uboot_version
    start_marker = None if args.expected_start_no_marker else args.expected_start_marker
    watcher = subprocess.run(["pgrep", "-f", r"[ /]uconsole_watch[.]py([[:space:]]|$)"],
                             capture_output=True, text=True, check=False)
    require(watcher.returncode == 0 and watcher.stdout.split() == [str(args.watcher_pid)] and
            ram.LOG.is_file() and ram.CMD.is_file(), "expected exactly the supplied watcher and bridge")
    phase("check-starting-rom")
    state["starting_boot_id"] = read_rom(serial, out / "start-rom", args.expected_start_build,
                                         args.expected_start_fingerprint, start_marker)
    phase("no-write-rehearsal")
    recovery.reboot_to_shell(ram, serial, out / "rom-to-rehearsal", version)
    recovery.boot(candidate, RAM, INSTALL, out / "ram-twrp-rehearsal", version, args.watcher_pid, serial)
    session = recovery.reboot_to_shell(ram, serial, out / "rehearsal-to-rom", version)
    recovery.boot_rom(ram, session)
    wait_boot(serial, out / "rehearsal-rom")
    state["rehearsal_boot_id"] = read_rom(serial, out / "rehearsal-rom", args.expected_start_build,
                                          args.expected_start_fingerprint, start_marker)
    require(state["rehearsal_boot_id"] != state["starting_boot_id"],
            "no-write rehearsal did not witness a fresh returning ROM boot")
    if args.rehearse:
        state["status"] = "no_write_rehearsal_passed"
        phase("rehearsal-complete")
        return
    phase("check-live-write-geometry")
    session = recovery.reboot_to_shell(ram, serial, out / "rom-to-install-menu", version)
    try:
        recovery.verify_geometry(session)
        phase("write-pe-fits")
        written = fits.write_fits(session, ram, plan["fits"]["recovery"], plan["fits"]["rom"],
                                  out / "fit-write", version, twrp["path"], strict_console=True)
        require(written["status"] == "verified", "PE FIT writes/readbacks or neighbor CRCs did not verify")
    finally:
        session.save()
    phase("protected-installer")
    profile = candidate["profile_id"]
    recovery.boot(candidate, RAM, INSTALL, out / "ram-twrp-install", version, args.watcher_pid, serial)
    recovery.helper([*install_args(plan, out / "install", profile), "--discard-pe-data"],
                    out / "installer.log", 3600)
    installed = load(out / "install/result.json")
    require(installed["status"] == "fresh_pe_installed_readback_and_format_verified" and
            installed["ram_twrp_id"] == profile and
            set(installed["formatted_volumes"]) == {"peuserdata", "pemetadata"} and
            all(installed["device_images"][part] == {"written_bytes": spec["bytes"], "readback_sha256": spec["sha256"],
                                                    "readback_verified": True} for part, spec in plan["raw"].items()),
            "raw readback or PE format evidence differs")
    phase("boot-target-rom")
    session = recovery.reboot_to_shell(ram, serial, out / "installer-to-rom", version)
    recovery.boot_rom(ram, session)
    wait_boot(serial, out / "target-rom")
    state["target_boot_id"] = read_rom(serial, out / "target-rom", plan["target_build"],
                                       plan["target_fingerprint"], plan.get("target_marker"))
    require(state["target_boot_id"] not in {state["starting_boot_id"], state["rehearsal_boot_id"]},
            "target ROM did not report a fresh boot")
    state["status"] = "target_rom_booted_pending_300s_runtime_acceptance"
    phase("runtime-handoff")


def install_command(args):
    if (args.execute or args.rehearse) and not (
            args.watcher_pid and args.watcher_pid > 0 and args.expected_start_build and
            args.expected_start_fingerprint and (args.expected_start_marker or args.expected_start_no_marker)):
        raise SystemExit("--execute and --rehearse require --watcher-pid, --expected-start-build, "
                         "--expected-start-fingerprint and --expected-start-marker or --expected-start-no-marker")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    state = {"status": "failed", "phase": "host-validation", "started_utc": datetime.now(timezone.utc).isoformat(),
             "plan": str(args.plan.resolve())}
    try:
        ram = fits.load_ram(WORKSPACE)
        plan, digest, candidate, twrp = host_plan(args, out, ram)
        marker = plan.get("target_marker")
        state.update(review_sha256=digest, ram_twrp=candidate, serial=args.serial,
                     reviewed_targets=plan["writes_in_order"],
                     runtime_acceptance=[sys.executable, str(VERIFY), "--identity", str(args.identity.resolve()),
                                         "--vold-sha256", plan["expected_vold_sha256"],
                                         *(["--marker", marker] if marker else ["--marker-absent"]),
                                         "--out", str(out / "runtime")])
        if args.dry_run:
            state["status"] = "host_only_dry_run_passed"
        else:
            state["expected_start"] = {"build": args.expected_start_build,
                                       "fingerprint": args.expected_start_fingerprint,
                                       "marker": None if args.expected_start_no_marker else args.expected_start_marker}
            execute(args, out, ram, plan, candidate, twrp, state)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        state.update(status="failed", error=f"{type(error).__name__}: {error}",
                     next_action="Stop. Inspect saved evidence and the current endpoint before any separately "
                                 "authorized recovery. No retry or rollback was attempted.")
    finally:
        state["finished_utc"] = datetime.now(timezone.utc).isoformat()
        save(out / "result.json", state)
    print(json.dumps(state, indent=2))
    return 1 if state["status"] == "failed" else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="host-only review plan from explicit payload files")
    plan.add_argument("--system", type=Path, required=True, help="expanded raw ext4 PE system image")
    plan.add_argument("--vendor", type=Path, required=True, help="expanded raw ext4 PE vendor image")
    plan.add_argument("--recovery-fit", type=Path, required=True)
    plan.add_argument("--rom-fit", type=Path, required=True)
    plan.add_argument("--identity", type=Path, required=True, help="host image identity (image-validation.json)")
    plan.add_argument("--compare-with", type=Path, help="earlier reviewed plan whose bytes and ranges must match")
    install = sub.add_parser("install", help="validate (--dry-run) or run (--execute) a reviewed plan")
    mode = install.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="host only; no watcher, USB, ADB or fastboot access")
    mode.add_argument("--execute", action="store_true",
                      help="with separate authority, replace PE FITs/raw and erase PE data")
    mode.add_argument("--rehearse", action="store_true",
                      help="exclusive window: check the starting ROM, run the no-write rehearsal, then stop")
    install.add_argument("--plan", type=Path, required=True)
    install.add_argument("--plan-sha256", required=True)
    install.add_argument("--identity", type=Path, required=True, help="host image identity of the target build")
    install.add_argument("--watcher-pid", type=int)
    install.add_argument("--expected-start-build")
    install.add_argument("--expected-start-fingerprint")
    marker = install.add_mutually_exclusive_group()
    marker.add_argument("--expected-start-marker")
    marker.add_argument("--expected-start-no-marker", action="store_true")
    install.add_argument("--expected-uboot-version", default=UBOOT_VERSION)
    for command in (plan, install):
        command.add_argument("--out", type=Path, required=True, help="new output directory")
        command.add_argument("--ram-twrp-manifest", type=Path, required=True)
        command.add_argument("--ram-twrp-manifest-sha256", required=True)
        command.add_argument("--serial", default=installer.SERIAL)
    args = parser.parse_args()
    if args.command == "plan":
        try:
            return make_plan(args)
        except (OSError, RuntimeError, ValueError, KeyError) as error:
            parser.exit(1, f"direct-install-pe13: {error}\n")
    return install_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
