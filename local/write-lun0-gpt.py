#!/usr/bin/env python3
"""Write the LUN0 GPT that lun0-gpt.py derived, through the Recovery container U-Boot fastboot.

    write-lun0-gpt.py --head SRC-HEAD --tail SRC-TAIL --gpt DIR --work WORK [--run [--serial S]]

SRC-HEAD/SRC-TAIL are the read-only captures lun0-gpt.py read (LUN0 LBA 0-7 and the
six blocks from the last usable LBA); DIR is its output. The plan uses only commands
the container's fastboot offers: `fastboot stage` (download into the 0xC0000000
buffer) and `fastboot oem run:` (one U-Boot command, OKAY or FAIL):

1. the live head and tail must still equal the captures (`scsi read` into
   0xC4000000, `cmp.l` against the staged capture), so a table that changed since
   the capture is never overwritten;
2. each run of changed blocks is staged and written with `scsi write`;
3. each run is read back and compared with its staged bytes.

Without --run it writes the run files into WORK and prints the plan; with --run it
also executes it in order and stops at the first failure. Start it from the
container menu's *Enable fastboot mode*. Writing the GPT is a device write.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

BLOCK = 4096
HEAD_BLOCKS, TAIL_BLOCKS = 8, 6
STAGE = 0xC0000000
READBACK = 0xC4000000


def require(condition, message):
    if not condition:
        raise SystemExit("write-lun0-gpt: " + message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def runs(writes):
    """Consecutive LBAs of layout.json's writes, as [first, count]."""
    groups = []
    for lba in sorted(int(w["lba"], 16) for w in writes):
        if groups and groups[-1][0] + groups[-1][1] == lba:
            groups[-1][1] += 1
        else:
            groups.append([lba, 1])
    return groups


def oem(command):
    line = "oem run:" + command
    require(len(line) <= 64, f"fastboot command longer than 64 bytes: {line}")
    return ["oem", "run:" + command]


def check_live(path, lba, blocks):
    return [("stage", str(path)), tuple(oem(f"scsi read {READBACK:#x} {lba:#x} {blocks:#x}")),
            tuple(oem(f"cmp.l {STAGE:#x} {READBACK:#x} {blocks * BLOCK // 4:#x}"))]


def plan(src_head, src_tail, layout, new_head, new_tail, work):
    """The fastboot steps and the run files they stage."""
    require(len(src_head) == HEAD_BLOCKS * BLOCK and len(src_tail) == TAIL_BLOCKS * BLOCK and
            len(new_head) == len(src_head) and len(new_tail) == len(src_tail), "unexpected capture shapes")
    require(layout["source"]["head_sha256"] == sha(src_head) and layout["source"]["tail_sha256"] == sha(src_tail),
            "the captures are not the ones lun0-gpt.py derived this table from")
    require(layout["head_sha256"] == sha(new_head) and layout["tail_sha256"] == sha(new_tail),
            "the derived images differ from layout.json")
    tail_lba = int(layout["tail_first_lba"], 16)
    images = ((0, src_head, new_head), (tail_lba, src_tail, new_tail))
    expected = {int(w["lba"], 16): w["sha256"] for w in layout["writes"]}

    def block(lba):
        for base, _, image in images:
            if base <= lba < base + len(image) // BLOCK:
                return image[(lba - base) * BLOCK:(lba - base + 1) * BLOCK]
        raise SystemExit(f"write-lun0-gpt: LBA {lba:#x} is outside the captured head and tail")

    for lba, digest in expected.items():
        require(sha(block(lba)) == digest, f"LBA {lba:#x} differs from layout.json")
    work.mkdir(parents=True, exist_ok=False)
    (work / "live-head.bin").write_bytes(src_head)
    (work / "live-tail.bin").write_bytes(src_tail)
    steps = [tuple(oem("scsi dev 0"))]
    steps += check_live(work / "live-head.bin", 0, HEAD_BLOCKS)
    steps += check_live(work / "live-tail.bin", tail_lba, TAIL_BLOCKS)
    files = []
    for lba, count in runs(layout["writes"]):
        data = b"".join(block(lba + i) for i in range(count))
        path = work / f"write-{lba:#x}-{count}.bin"
        path.write_bytes(data)
        files.append(path)
        steps += [("stage", str(path)), tuple(oem(f"scsi write {STAGE:#x} {lba:#x} {count:#x}"))]
    for path in files:
        lba, count = (int(part, 0) for part in path.stem.split("-")[1:])
        steps += check_live(path, lba, count)
    return steps


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--head", type=Path, required=True, help="capture of LUN0 LBA 0-7 lun0-gpt.py read")
    parser.add_argument("--tail", type=Path, required=True, help="capture of the last six LUN0 blocks")
    parser.add_argument("--gpt", type=Path, required=True, help="lun0-gpt.py output directory")
    parser.add_argument("--work", type=Path, required=True, help="new directory for the staged run files")
    parser.add_argument("--run", action="store_true", help="execute the plan with fastboot")
    parser.add_argument("--serial", help="fastboot -s serial")
    args = parser.parse_args()
    layout = json.loads((args.gpt / "layout.json").read_text())
    steps = plan(args.head.read_bytes(), args.tail.read_bytes(), layout,
                 (args.gpt / "sda-head-8x4k.bin").read_bytes(), (args.gpt / "sda-tail-6x4k.bin").read_bytes(),
                 args.work)
    fastboot = ["fastboot", *(["-s", args.serial] if args.serial else [])]
    for step in steps:
        print(" ".join([*fastboot, *step]))
    if not args.run:
        return
    for step in steps:
        result = subprocess.run([*fastboot, *step], capture_output=True, text=True)
        output = (result.stdout + result.stderr).strip()
        print(f"$ {' '.join(step)}\n{output}")
        if result.returncode or "FAILED" in output:
            sys.exit(f"write-lun0-gpt: stopped at {' '.join(step)}")
    print("write-lun0-gpt: GPT written and read back")


if __name__ == "__main__":
    main()
