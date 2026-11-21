#!/usr/bin/env python3
"""Reproduce the existing B Rosetta host settings in a private source overlay."""
import argparse
import difflib
import json
from pathlib import Path
import subprocess


DEFAULT_ON = "ifeq ($(HOST_OS),linux)\n  WITH_DEXPREOPT := true\nendif"
DEFAULT_OFF = """ifeq ($(HOST_OS),linux)
  # OrbStack/Rosetta workaround: host dex2oat crashes under Rosetta,
  # disable dexpreopt for local builds on this machine.
  WITH_DEXPREOPT := false
  WITH_DEX_PREOPT_GENERATE_PROFILE := false
endif"""
REQUIRE_PREOPT = "        $(call pretty-error, DEXPREOPT must be enabled for user and userdebug builds)"
ALLOW_NO_PREOPT = """        # OrbStack/Rosetta workaround: allow WITH_DEXPREOPT=false on userdebug builds.
        $(warning DEXPREOPT disabled for non-eng build (local Rosetta workaround))"""
RULES = (
    ("build/make/core/board_config.mk", DEFAULT_ON, DEFAULT_OFF),
    ("build/make/core/dex_preopt_config.mk", REQUIRE_PREOPT, ALLOW_NO_PREOPT),
)


def apply_profile(tree):
    """Apply known host deltas; caller must supply a private writable source view."""
    tree = Path(tree)
    edits, changes = [], []
    # Resolve every compatibility case before changing any file.
    for relative, upstream, legacy in RULES:
        path = tree / relative
        text = path.read_text()
        if text.count(upstream) == 1 and legacy not in text:
            updated = text.replace(upstream, legacy)
            edits.append((path, updated))
            changes.append({"path": relative, "action": "applied-in-overlay",
                            "patch": "".join(difflib.unified_diff(
                                text.splitlines(True), updated.splitlines(True),
                                fromfile="a/" + relative, tofile="b/" + relative))})
        elif text.count(legacy) == 1 and upstream not in text:
            changes.append({"path": relative, "action": "already-pinned"})
        else:
            raise ValueError("Unrecognized Rosetta host rule: " + relative)
    binary = tree / "prebuilts/build-tools/linux-x86/bin/nsjail"
    hidden = binary.with_name("nsjail.old")
    if binary.is_file() and not hidden.exists():
        hide_binary = True
    elif hidden.is_file() and not binary.exists():
        hide_binary = False
    else:
        raise ValueError("Unrecognized nsjail layout; expected exactly one known binary")
    for path, text in edits:
        path.write_text(text)
    if hide_binary:
        binary.rename(hidden)
    changes.append({"path": "prebuilts/build-tools/linux-x86/bin/nsjail",
                    "action": "hidden-in-overlay" if hide_binary else "already-pinned"})
    return {"schema_version": 1, "profile": "crux-b-rosetta/1",
            "application": "upstream",
            "scope": "B source overlay settings; not graph, build or device acceptance",
            "settings": {"WITH_DEXPREOPT": "false",
                         "WITH_DEX_PREOPT_GENERATE_PROFILE": "false",
                         "nsjail": "hidden"},
            "changes": changes}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--overlay-run", required=True, type=Path)
    args = parser.parse_args(argv)
    mount = subprocess.run(["findmnt", "-n", "-o", "FSTYPE,OPTIONS", "-T", str(args.tree)],
                           check=True, capture_output=True, text=True).stdout.strip().split(None, 1)
    options = dict(part.split("=", 1) for part in mount[1].split(",") if "=" in part) if len(mount) == 2 else {}
    run = args.overlay_run.resolve()
    expected = {"upperdir": run / "src-upper", "workdir": run / "src-work"}
    if (not mount or mount[0] != "overlay" or
            any(key not in options or Path(options[key]).resolve() != path for key, path in expected.items())):
        parser.error("B host profile requires the private source overlay, not the shared checkout")
    report = apply_profile(args.tree)
    with args.report.open("x") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
