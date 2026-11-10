#!/usr/bin/env python3
"""Generate a fresh PE13 Soong/Kati graph on a hosted Linux runner."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time


IMAGES = ("bootimage", "recoveryimage", "systemimage", "vendorimage")
GOGC_MARKER = "// CRUX_HOSTED_FREEZE_GOGC: env -i otherwise drops the runner setting."
GOGC_INSERTION = '''\n\t// CRUX_HOSTED_FREEZE_GOGC: env -i otherwise drops the runner setting.\n\tif gogc := os.Getenv("CRUX_SOONG_GOGC"); gogc != "" {\n\t\tinvocationEnv["GOGC"] = gogc\n\t}\n'''
EXPECTED_GRAPHS = (
    "soong/build.ninja",
    "build-aosp_crux.ninja",
    "build-aosp_crux-package.ninja",
    "combined-aosp_crux.ninja",
)


class FreezeError(ValueError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_source_manifest(path: Path, source_root: Path) -> tuple[str, list[dict[str, str]]]:
    if not path.is_file():
        raise FreezeError("Missing pinned source manifest: " + str(path))
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or not isinstance(manifest.get("projects"), list):
        raise FreezeError("Source manifest must contain a projects list")
    declared_root = manifest.get("source_root")
    if declared_root is not None and Path(declared_root).resolve() != source_root:
        raise FreezeError("Source manifest root differs from --source-root")
    projects = []
    soong_found = False
    for project in manifest["projects"]:
        if not isinstance(project, dict):
            raise FreezeError("Invalid source manifest project entry")
        relpath, revision = project.get("path"), project.get("revision")
        if not isinstance(relpath, str) or not relpath or Path(relpath).is_absolute() or ".." in Path(relpath).parts:
            raise FreezeError("Invalid project path in source manifest")
        if not isinstance(revision, str) or not revision:
            raise FreezeError("Missing project revision in source manifest: " + relpath)
        projects.append({"path": relpath, "revision": revision})
        if relpath.rstrip("/") == "build/soong":
            soong_found = True
    if not soong_found:
        raise FreezeError("Pinned source manifest has no build/soong project revision")
    return sha256(path), projects


def inject_gogc(source_root: Path) -> dict[str, str]:
    """Patch only the ephemeral hosted source view so GOGC survives env -i."""
    path = source_root / "build/soong/ui/build/soong.go"
    if not path.is_file():
        raise FreezeError("Missing Soong launcher source: " + str(path))
    original = path.read_text()
    original_digest = hashlib.sha256(original.encode()).hexdigest()
    if GOGC_MARKER in original:
        if original.count(GOGC_MARKER) != 1 or original.count(GOGC_INSERTION) != 1:
            raise FreezeError("Found an incomplete or duplicate hosted GOGC injection")
        unpatched = original.replace(GOGC_INSERTION, "", 1)
        anchor = "\tinvocationEnv := make(map[string]string)\n"
        if unpatched.count(anchor) != 1:
            raise FreezeError("Cannot reconstruct the original Soong invocation source")
        return {"path": "build/soong/ui/build/soong.go", "status": "already-injected",
                "original_sha256": hashlib.sha256(unpatched.encode()).hexdigest(),
                "result_sha256": original_digest,
                "injection": GOGC_INSERTION.strip()}
    anchor = "\tinvocationEnv := make(map[string]string)\n"
    if original.count(anchor) != 1:
        raise FreezeError("Expected one unique Soong invocationEnv insertion point")
    patched = original.replace(anchor, anchor + GOGC_INSERTION, 1)
    path.write_text(patched)
    return {"path": "build/soong/ui/build/soong.go", "status": "patched",
            "original_sha256": original_digest,
            "result_sha256": hashlib.sha256(patched.encode()).hexdigest(),
            "injection": GOGC_INSERTION.strip()}


def validate_paths(source_root: Path, out_root: Path, evidence_dir: Path) -> None:
    if not source_root.is_dir() or not (source_root / "build/envsetup.sh").is_file():
        raise FreezeError("Source root is not a mounted Android tree: " + str(source_root))
    if out_root == source_root or source_root in out_root.parents:
        raise FreezeError("OUT_DIR must be outside the source tree")
    if evidence_dir == source_root or source_root in evidence_dir.parents:
        raise FreezeError("Evidence directory must be outside the source tree")
    if evidence_dir == out_root or out_root in evidence_dir.parents:
        raise FreezeError("Evidence directory must be separate from OUT_DIR")
    if out_root.exists() and (not out_root.is_dir() or any(out_root.iterdir())):
        raise FreezeError("OUT_DIR must be fresh and empty: " + str(out_root))


def build_command() -> list[str]:
    shell = (
        "set -e; source build/envsetup.sh >/dev/null; "
        "lunch aosp_crux-userdebug >/dev/null; "
        "m --skip-ninja " + " ".join(IMAGES) + " -j4"
    )
    return ["/usr/bin/time", "-v", "bash", "-c", shell]


def observe_graph_sandbox(source_root: Path, out_root: Path) -> dict:
    """Read Soong's actual post-wrap command log; never run a replacement probe."""
    log = out_root / "soong.log"
    wrapped, disabled, samples = 0, False, []
    if log.is_file():
        with log.open(errors="replace") as stream:
            for line in stream:
                selected = re.search(r'executing "[^"\n]*\bnsjail"', line) is not None
                fallback = "Build sandboxing disabled due to nsjail error." in line
                wrapped += int(selected)
                disabled = disabled or fallback
                if (selected or fallback or "nsjail failed with" in line) and len(samples) < 8:
                    samples.append(line.strip()[:1200])
    status = ("mixed-observed" if wrapped and disabled else "wrapped-commands-observed" if wrapped
              else "disabled-observed" if disabled else "unobserved")
    return {"scope": "Soong graph-host commands only; not direct-Ninja worker sandboxing",
            "status": status, "wrapped_commands": wrapped, "fallback_observed": disabled,
            "binary_present": (source_root / "prebuilts/build-tools/linux-x86/bin/nsjail").is_file(),
            "log": str(log), "samples": samples}


def observe_graph_dexpreopt(out_root: Path) -> dict:
    """Report generated Make/Soong settings, not assumptions from a source pin."""
    path = out_root / "soong/dexpreopt.config"
    record = {"scope": "generated graph configuration, not boot/runtime acceptance",
              "path": str(path), "status": "unobserved", "flags": {}}
    if not path.is_file():
        return record
    try:
        config = json.loads(path.read_text())
    except (ValueError, OSError) as error:
        return {**record, "status": "invalid", "error": str(error)}
    keys = ("DisablePreopt", "DisablePreoptBootImages", "DisableGenerateProfile",
            "OnlyPreoptBootImageAndSystemServer")
    if not isinstance(config, dict) or any(type(config.get(key)) is not bool for key in keys):
        return {**record, "status": "invalid", "error": "missing/non-boolean preopt configuration"}
    return {**record, "status": "observed", "flags": {key: config[key] for key in keys},
            "disabled_modules": config.get("DisablePreoptModules")}


def monitor_resources(directory: Path, stop: threading.Event) -> None:
    with (directory / "resources.jsonl").open("x") as stream:
        while True:
            disk = os.statvfs(directory)
            sample = {"unix_time": time.time(), "disk_available_bytes": disk.f_bavail * disk.f_frsize}
            try:
                memory = {}
                for line in Path("/proc/meminfo").read_text().splitlines():
                    name, value = line.split(":", 1)
                    if name in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                        memory[name] = int(value.split()[0]) * 1024
                sample.update(memory)
            except OSError:
                pass
            stream.write(json.dumps(sample) + "\n")
            stream.flush()
            if stop.wait(2):
                return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--out-root", required=True)
    parser.add_argument("--build-number", required=True)
    parser.add_argument("--build-datetime", required=True, type=int)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--source-receipts")
    parser.add_argument("--require-dexpreopt", action="store_true",
                        help="require global dexpreopt/boot/profile defaults (A2), not override module exclusions")
    parser.add_argument("--gogc", type=int, default=50,
                        help="Go GC target percentage passed to soong_build (default: 50)")
    args = parser.parse_args(argv)
    if not args.build_number or any(ch.isspace() for ch in args.build_number):
        parser.error("--build-number must be non-empty and contain no whitespace")
    if args.build_datetime < 0 or args.gogc < 1:
        parser.error("--build-datetime must be non-negative and --gogc positive")

    source_root = Path(args.source_root).resolve()
    out_root = Path(args.out_root).resolve()
    evidence_dir = Path(args.evidence).resolve()
    manifest_path = Path(args.source_manifest).resolve()
    validate_paths(source_root, out_root, evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    log_path = evidence_dir / "cloud-freeze.log"
    receipt_path = evidence_dir / "graph-receipt.json"
    if log_path.exists() or receipt_path.exists():
        raise FreezeError("Evidence outputs already exist; refusing to overwrite them")
    out_root.mkdir(parents=True, exist_ok=True)

    manifest_sha, source_revisions = load_source_manifest(manifest_path, source_root)
    soong_revision = next(item["revision"] for item in source_revisions
                          if item["path"].rstrip("/") == "build/soong")
    patch = inject_gogc(source_root)
    command = build_command()
    environment = os.environ.copy()
    # Soong persists its available environment under OUT_DIR. Job authentication
    # belongs to the workflow's transport steps, not to the Android build inputs.
    removed_environment = sorted(variable for variable in environment
                                 if variable.startswith(("ACTIONS_", "GITHUB_"))
                                 or variable == "GH_TOKEN")
    for variable in removed_environment:
        environment.pop(variable)
    for variable in ("CRUX_UBOOT_DUALBOOT", "CC_WRAPPER", "OUT_DIR_COMMON_BASE"):
        environment.pop(variable, None)
    environment.update({"OUT_DIR": str(out_root), "BUILD_NUMBER": args.build_number,
                        "BUILD_DATETIME": str(args.build_datetime),
                        "BUILD_FINGERPRINT": "Xiaomi/crux/crux:13/TQ3A.230901.001.B1/"
                                             + args.build_number + ":userdebug/test-keys",
                        "USE_CCACHE": "0", "CCACHE_DISABLE": "1",
                        "CRUX_SOONG_GOGC": str(args.gogc)})
    receipt = {
        "schema_version": 1,
        "status": "running",
        "source_root": str(source_root),
        "out_root": str(out_root),
        "build_number": args.build_number,
        "build_datetime": args.build_datetime,
        "build_fingerprint": environment["BUILD_FINGERPRINT"],
        "source_manifest": {"path": str(manifest_path), "sha256": manifest_sha,
                             "project_count": len(source_revisions),
                             "projects": source_revisions},
        "soong_source_revision": soong_revision,
        "gogc": args.gogc,
        "host_source_patch": patch,
        "removed_ci_environment_variables": removed_environment,
        "command": command,
        "graph_files": [],
    }
    start = time.monotonic()
    stop_monitor = threading.Event()
    monitor = threading.Thread(target=monitor_resources, args=(evidence_dir, stop_monitor), daemon=True)
    monitor.start()
    try:
        with log_path.open("x") as log:
            log.write("Command: " + " ".join(command) + "\n")
            log.flush()
            completed = subprocess.run(command, cwd=source_root, env=environment,
                                       stdout=log, stderr=subprocess.STDOUT, check=False)
        receipt["elapsed_seconds"] = round(time.monotonic() - start, 3)
        receipt["exit_code"] = completed.returncode
        observation = observe_graph_sandbox(source_root, out_root)
        receipt["graph_host_sandbox"] = observation
        (evidence_dir / "graph-host-sandbox.json").write_text(json.dumps(observation, indent=2) + "\n")
        print(json.dumps({"graph_host_sandbox": observation}))
        dexpreopt = observe_graph_dexpreopt(out_root)
        receipt["graph_dexpreopt"] = dexpreopt
        (evidence_dir / "graph-dexpreopt.json").write_text(json.dumps(dexpreopt, indent=2) + "\n")
        print(json.dumps({"graph_dexpreopt": dexpreopt}))
        if (completed.returncode == 0 and args.require_dexpreopt and
                (dexpreopt["status"] != "observed" or any(dexpreopt["flags"].values()))):
            raise FreezeError("A2 graph must enable full dexpreopt, boot preopt and profile generation")
        expected = [out_root / relative for relative in EXPECTED_GRAPHS]
        receipt["graph_files"] = [{"path": str(path), "size": path.stat().st_size,
                                   "sha256": sha256(path)} for path in expected if path.is_file()]
        missing = [str(path) for path in expected if not path.is_file()]
        receipt["missing_graph_files"] = missing
        receipt["status"] = "success" if completed.returncode == 0 and not missing else "failed"
        receipt["source_receipts_dir"] = (str(Path(args.source_receipts).resolve())
                                          if args.source_receipts else None)
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        if receipt["status"] != "success":
            print("Cloud graph generation failed; inspect " + str(log_path))
            return completed.returncode or 1
        print("Fresh Soong/Kati graph generated; receipt: " + str(receipt_path))
        return 0
    except Exception as error:
        receipt["elapsed_seconds"] = round(time.monotonic() - start, 3)
        receipt["status"] = "failed"
        receipt["error"] = str(error)
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        raise
    finally:
        stop_monitor.set()
        monitor.join()


if __name__ == "__main__":
    raise SystemExit(main())
