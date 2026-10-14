#!/usr/bin/env python3
"""Report directories an edge creates under the out root that nobody owns.

A recipe may create a directory tree as a side effect instead of declaring it,
for example the AOSP boot ramdisk recipe:

  build/make/core/Makefile:906-913
  mkdir -p $(addprefix $(TARGET_RAMDISK_OUT)/,debug_ramdisk dev metadata mnt
            proc second_stage_resources sys) && mkbootfs ... > ramdisk.img

The capsule exchange carries declared outputs and declared side-output
directories. An undeclared staging directory is reproduced from files alone, so
its empty subdirectories disappear for every consumer. When that happened to the
boot ramdisk in the 2026101104 build, first-stage init failed its mount and mknod
CHECKCALLs, aborted with "Init encountered errors starting first stage" and the
device reset-looped.

Run this audit on the selected shard manifests before packing waves: it lists
every mkdir target under the out root that is not an edge output, not a declared
side-output directory and not beneath one. Declare the survivors in the plan's
ownership profile (rationale and source evidence are required by graph.py).

The match is heuristic: it reports directories that are created and then packed
by the same command, so a compiler that archives a tree it also created can
appear in the report. Review every line; use --strict to fail the run (exit 1)
instead of only reporting, and 2 for malformed input.
"""
import argparse
import json
import re
import sys
from pathlib import Path

CREATED = re.compile(r'(?<![\w/.$-])mkdir(?![\w-])')
# A created directory only matters when the same command packs or reads that tree
# into an artifact: a consumer can recreate a directory that merely receives
# declared files, but it cannot recreate an empty mount point baked into a ramdisk,
# an image or an archive.
TREE_READER = re.compile(r'(?<![\w-])(' + '|'.join((
    'mkbootfs', 'mkbootimg', 'unpack_bootimg', 'cpio', 'tar', 'bsdtar', 'zip', 'soong_zip', 'zip2zip',
    'zipmerge', 'e2fsdroid', 'mkuserimg_mke2fs', 'mkuserimg', 'mke2fs', 'mkf2fs', 'make_ext4fs',
    'img2simg', 'simg2img', 'append2simg', 'lpmake', 'avbtool', 'deapexer', 'apexer')) + r')(?![\w-])')


METACHARACTERS = {";", "&&", "||", "|", "&", "(", ")", "<", ">", ">|", ">>", "`"}


def normalized(value):
    text = str(value)
    return Path(text if text == "/" else text.rstrip("/"))


def beneath(path, root):
    """Lexical prefix test: recorded paths are absolute and verbatim, and macOS
    /home is a firmlink, so resolving would compare different spellings."""
    parts, root_parts = normalized(path).parts, normalized(root).parts
    return list(parts[:len(root_parts)]) == list(root_parts)


def literal(token):
    token = token.strip()
    if token.startswith(("'", '"')):
        quote = token[0]
        end = token.find(quote, 1)
        if end > 0:
            token = token[1:end]
        else:
            token = token[1:]
    if not token.startswith("/") or any(character in token for character in "$`*?[]{}"):
        return None
    return token.rstrip("/")


def created_directories(command):
    directories = set()
    tokens = command.split()
    for index, token in enumerate(tokens):
        if not CREATED.search(token):
            continue
        parents = False
        for following in tokens[index + 1:]:
            if following in METACHARACTERS or following.rstrip(")") in METACHARACTERS:
                break
            if following.startswith("-"):
                parents = parents or following.strip("()") in ("-p", "--parents")
                continue
            path = literal(following)
            if path:
                directories.add(path)
        if not parents and not directories:
            continue
    return directories


def owned(path, outputs, side_output_dirs, out_root):
    if not beneath(path, out_root):
        return True  # outside the exchanged tree: not this audit's subject
    for candidate in [*outputs, *side_output_dirs]:
        if path == candidate or beneath(path, candidate):
            return True
    return False


def audit(manifest_path, capsule_path=None, out_root_argument=None):
    manifest = json.loads(Path(manifest_path).read_text())
    capsule = json.loads(Path(capsule_path).read_text()) if capsule_path else {}
    declared = [*manifest.get("declared_side_output_dirs", []), *manifest.get("output_dirs", []),
                *capsule.get("output_dirs", []), *capsule.get("external_input_dirs", []),
                *manifest.get("external_input_dirs", [])]
    source = out_root_argument or capsule.get("out_root") or manifest.get("out_root")
    if source is None and declared:
        return None, {"": ["declare --out-root: no out root recorded in the manifest or capsule"]}
    out_root = str(normalized(source))
    outputs = [str(normalized(value)) for value in manifest.get("outputs", [])]
    side = [str(normalized(value)) for value in declared]
    external = []
    unowned = {}
    for edge in manifest.get("edges", []):
        command = edge.get("command") or ""
        if "mkdir" not in command or not TREE_READER.search(command):
            continue
        referenced = set(re.findall(r"/[\w./+-]+", command))
        for directory in created_directories(command):
            packed = any(candidate == directory or beneath(directory, candidate)
                         for candidate in referenced)
            if packed and not owned(directory, outputs, side + external, out_root):
                unowned.setdefault(directory, []).append(edge.get("outputs"))
    return out_root, unowned


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", action="append", required=True,
                        help="shard manifest path (repeat for every selected shard)")
    parser.add_argument("--capsule", action="append", default=[],
                        help="packed capsule metadata for the matching shard (recorded out root and output dirs)")
    parser.add_argument("--out-root", help="out root when neither the manifest nor a capsule records it")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 when any created directory is unowned (review report otherwise)")
    args = parser.parse_args()
    if args.capsule and len(args.capsule) != len(args.manifest):
        print("--capsule must be given once per --manifest", file=sys.stderr)
        return 2
    failures = 0
    for index, path in enumerate(args.manifest):
        try:
            out_root, unowned = audit(path, args.capsule[index] if args.capsule else None, args.out_root)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            print("%s: malformed manifest: %s" % (path, error), file=sys.stderr)
            return 2
        if unowned:
            failures += 1
            print("UNOWNED created directories in %s (out root %s):" % (path, out_root))
            for directory in sorted(unowned):
                print("  %s   (edges %s)" % (directory, unowned[directory]))
        else:
            print("owned: %s" % path)
    if failures:
        print("REVIEW: declare the created directories as side output dirs in the plan "
              "ownership profile, or have their producer declare them", file=sys.stderr)
        return 1 if args.strict else 0
    print("PASS: every mkdir target under the out root is owned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
