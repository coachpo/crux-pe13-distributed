#!/usr/bin/env python3
"""Manage an independent local build cache without moving its Ninja paths."""
import argparse
import getpass
import json
from pathlib import Path
import subprocess

NAME = "local-build-cache.json"
SUCCESS = "local-build-success.json"
IDENTITY_OUTPUTS = ("target/product/crux/obj/PACKAGING/system_build_prop_intermediates/buildinfo.prop",
                    "target/product/crux/vendor/build.prop")


def read(state):
    state = Path(state).resolve(strict=True)
    record = json.loads((state / NAME).read_text())
    if record.get("schema_version") != 1 or record.get("kind") != "writable-build-cache":
        raise ValueError("not a managed writable build cache")
    logical = Path(record["logical_out"])
    if not logical.is_absolute() or not logical.is_dir() or not (state / "android-out").is_dir():
        raise ValueError("cache output paths are missing or invalid")
    return state, logical


def initialize(state, seed=None, logical_out=None):
    state = Path(state).resolve()
    if seed and logical_out:
        raise ValueError("a seeded cache keeps the seed's logical OUT")
    seed = Path(seed).resolve(strict=True) if seed else None
    if seed and (state == seed or seed in state.parents or state in seed.parents):
        raise ValueError("cache and seed must be separate, non-nested directories")
    if seed:
        source = seed / "android-out"
        if not source.is_dir():
            raise ValueError("seed has no android-out")
        logical = read(seed)[1] if (seed / NAME).exists() else source
    elif logical_out:
        logical = Path(logical_out)
        if not logical.is_absolute() or not logical.is_dir() or any(logical.iterdir()):
            raise ValueError("the logical OUT must be an existing, empty mount point")
    else:
        logical = state / "android-out"
    state.mkdir()
    if seed:
        # Never fall back to a full copy or hard links. A failed clone remains
        # incomplete (without metadata) and cannot accidentally become resumable.
        subprocess.run(["cp", "-a", "--reflink=always", str(source), str(state / "android-out")], check=True)
    else:
        (state / "android-out").mkdir()
    (state / NAME).write_text(json.dumps({"schema_version": 1, "kind": "writable-build-cache",
                                        "logical_out": str(logical),
                                        "seed": str(seed) if seed else None}, indent=2) + "\n")
    return state, logical


def run(state, argv):
    state = Path(state).resolve(strict=True)
    # Legacy accepted states remain readable for packaging, never resumable.
    state, logical = read(state) if (state / NAME).exists() else (state, state / "android-out")
    physical = state / "android-out"
    if physical == logical:
        return subprocess.run(argv).returncode
    # Both build and collection need the mapping: generated commands and output
    # symlinks can retain the seed's absolute OUT path. Mounts die with this process.
    return subprocess.run([
        "sudo", "-n", "unshare", "--mount", "--propagation", "private", "bash", "-ec",
        'mount --bind "$1" "$2"; user=$3; shift 3; exec runuser -u "$user" -- "$@"',
        "local-build-output", str(physical), str(logical), getpass.getuser(), *argv,
    ]).returncode


def prepare_build(cache, build, now, record=None):
    cache, _ = read(cache)
    out = cache / "android-out"
    number = out / "soong/build_number.txt"
    date = out / "build_date.txt"
    epoch = (date.read_text().strip() if number.exists() and number.read_text().strip() == build
             and date.exists() and date.read_text().strip().isdigit() else str(now))
    success = json.loads((cache / SUCCESS).read_text()) if (cache / SUCCESS).exists() else {}
    removed = []
    if success.get("build") != build or success.get("epoch") != epoch:
        # sysprop.mk uses order-only build_number/date dependencies for buildinfo
        # and no fingerprint dependency for vendor/build.prop. Invalidate only
        # these two generated identities; system props, Recovery and images are
        # their ordinary downstream targets. Never clean compilation objects.
        for relative in IDENTITY_OUTPUTS:
            path = out / relative
            if path.exists():
                path.unlink()
                removed.append(relative)
    if record:
        Path(record).write_text(json.dumps({"build": build, "epoch": epoch,
                                           "invalidated": removed}, indent=2) + "\n")
    return epoch


def complete_build(cache, build):
    cache, _ = read(cache)
    out = cache / "android-out"
    for relative, prefix in (("target/product/crux/system/build.prop", "ro.build.version.incremental="),
                             ("target/product/crux/vendor/build.prop", "ro.vendor.build.version.incremental=")):
        if prefix + build not in (out / relative).read_text().splitlines():
            raise ValueError("built properties do not match requested identity: " + relative)
    (cache / SUCCESS).write_text(json.dumps({"build": build,
                                           "epoch": (out / "build_date.txt").read_text().strip()}) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    init = commands.add_parser("init")
    init.add_argument("state")
    init.add_argument("--seed-from")
    init.add_argument("--logical-out", help="existing empty mount point the build sees as OUT")
    inspect = commands.add_parser("logical-out")
    inspect.add_argument("state")
    execute = commands.add_parser("run")
    execute.add_argument("state")
    execute.add_argument("command", nargs=argparse.REMAINDER)
    prepare = commands.add_parser("prepare-build")
    prepare.add_argument("state")
    prepare.add_argument("build")
    prepare.add_argument("now")
    prepare.add_argument("--record")
    complete = commands.add_parser("complete-build")
    complete.add_argument("state")
    complete.add_argument("build")
    args = parser.parse_args()
    if args.operation == "init":
        print(initialize(args.state, args.seed_from, args.logical_out)[1])
    elif args.operation == "logical-out":
        print(read(args.state)[1])
    elif args.operation == "prepare-build":
        print(prepare_build(args.state, args.build, args.now, args.record))
    elif args.operation == "complete-build":
        complete_build(args.state, args.build)
    else:
        argv = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not argv:
            parser.error("run requires a command")
        raise SystemExit(run(args.state, argv))


if __name__ == "__main__":
    main()
