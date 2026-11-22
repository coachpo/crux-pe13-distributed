#!/usr/bin/env python3
"""Derive the reclaimed LUN0 GPT from a readback of the PE-only table.

    lun0-gpt.py --head sda-head-8x4k.bin --tail sda-tail-6x4k.bin --out DIR

The inputs are the read-only captures of LUN0 LBA 0-7 and of its last six blocks. The
source must be the verified 38-entry PE-only layout with identical primary and backup
tables. MIUI's cust (entry 31) is removed, vendor becomes a 16 MiB name stub (the PE
guard looks it up by name), and the vendor/system/userdata stubs and the PE
partitions move down to 0x60000 so that peuserdata takes all the space up to the end
of the disk. Every remaining node number is unchanged. DIR receives the new head and
tail images of the same shape, the blocks to write and a layout record. No device
access.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import uuid
import zlib

BLOCK = 4096
HEADER = struct.Struct("<8sIIII QQQQ16sQIII")
ENTRY = struct.Struct("<16s16sQQQ72s")

# Entries 31-38 of the source (PE-only) layout: (name, first LBA, blocks).
SOURCE_TAIL = [("cust", 0x60000, 0x40000), ("vendor", 0xa0000, 0x80000),
               ("system", 0x120000, 0x1000), ("userdata", 0x121000, 0x1000),
               ("pesystem", 0x123000, 0x100000), ("pevendor", 0x223000, 0x70000),
               ("peuserdata", 0x293000, 0x38c3e00), ("pemetadata", 0x122000, 0x1000)]
REMOVED = {"cust"}
# Entries kept by index; type, unique GUID and attributes are preserved. The PE
# extents equal installer/crux_installer.c volumes[] and the U-Boot CRUX_PESYSTEM_LBA.
MOVED = {"vendor": (0x60000, 0x1000), "system": (0x61000, 0x1000), "userdata": (0x62000, 0x1000),
         "pemetadata": (0x63000, 0x1000), "pesystem": (0x64000, 0x100000),
         "pevendor": (0x164000, 0x70000), "peuserdata": (0x1d4000, 0x3982e00)}


def fail(message):
    raise SystemExit(f"lun0-gpt: {message}")


def require(condition, message):
    if not condition:
        fail(message)


def parse_header(block):
    fields = HEADER.unpack_from(block)
    header = dict(zip("signature revision size crc reserved current alternate first_usable "
                      "last_usable disk_guid entry_lba entries entry_size entry_crc".split(), fields))
    raw = bytearray(block[:header["size"]])
    raw[16:20] = b"\0" * 4
    require(header["signature"] == b"EFI PART" and header["size"] == 92, "not a GPT header")
    require(zlib.crc32(raw) == header["crc"], f"header CRC mismatch at LBA {header['current']:#x}")
    return header


def parse_entries(array, header):
    require(zlib.crc32(array) == header["entry_crc"], "entry array CRC mismatch")
    entries = []
    for i in range(header["entries"]):
        kind, unique, first, last, attr, name = ENTRY.unpack_from(array, i * header["entry_size"])
        entries.append(None if kind == b"\0" * 16 else {
            "type": str(uuid.UUID(bytes_le=kind)), "guid": str(uuid.UUID(bytes_le=unique)),
            "first": first, "last": last, "attr": attr,
            "name": name.decode("utf-16le").rstrip("\0")})
    return entries


def pack_entries(entries, header):
    array = bytearray(header["entries"] * header["entry_size"])
    for i, e in enumerate(entries):
        if e:
            ENTRY.pack_into(array, i * header["entry_size"], uuid.UUID(e["type"]).bytes_le,
                            uuid.UUID(e["guid"]).bytes_le, e["first"], e["last"], e["attr"],
                            e["name"].encode("utf-16le"))
    return bytes(array)


def pack_header(header, entry_crc):
    fields = dict(header, crc=0, entry_crc=entry_crc)
    raw = bytearray(HEADER.pack(*(fields[k] for k in (
        "signature revision size crc reserved current alternate first_usable last_usable "
        "disk_guid entry_lba entries entry_size entry_crc").split())))
    raw[16:20] = struct.pack("<I", zlib.crc32(raw))
    return bytes(raw).ljust(BLOCK, b"\0")


def read_tables(head, tail):
    require(len(head) == 8 * BLOCK and len(tail) == 6 * BLOCK, "unexpected capture sizes")
    primary = parse_header(head[BLOCK:])
    tail_base = primary["last_usable"]
    backup = parse_header(tail[5 * BLOCK:])
    require(primary["current"] == 1 and primary["entry_lba"] == 2, "primary header location")
    require(backup["current"] == primary["alternate"] == tail_base + 5 and backup["alternate"] == 1,
            "backup header location")
    size = primary["entries"] * primary["entry_size"]
    require(size == 2 * BLOCK and backup["entry_lba"] == tail_base + 1, "entry array location")
    entries = parse_entries(head[2 * BLOCK:2 * BLOCK + size], primary)
    backup_offset = (backup["entry_lba"] - tail_base) * BLOCK
    require(tail[backup_offset:backup_offset + size] == head[2 * BLOCK:2 * BLOCK + size],
            "backup entry array differs from the primary")
    for key in ("disk_guid", "first_usable", "last_usable", "entries", "entry_size", "entry_crc"):
        require(primary[key] == backup[key], f"primary and backup {key} differ")
    return primary, backup, entries


def check_layout(header, entries):
    used = sorted((e for e in entries if e), key=lambda e: e["first"])
    names = [e["name"] for e in used]
    require(len(set(names)) == len(names), "duplicate partition names")
    require(len({e["guid"] for e in used}) == len(used), "duplicate partition GUIDs")
    previous = header["first_usable"] - 1
    for e in used:
        require(e["first"] > previous and e["last"] >= e["first"], f"{e['name']} overlaps")
        previous = e["last"]
    require(previous <= header["last_usable"], "a partition passes the last usable LBA")


def derive(primary, backup, entries):
    used = [e for e in entries if e]
    require(len(used) == 38 and all(entries[:38]) and not any(entries[38:]),
            "source is not the verified 38-entry PE-only layout")
    require([(e["name"], e["first"], e["last"] - e["first"] + 1) for e in entries[30:38]]
            == SOURCE_TAIL, "entries 31-38 differ from the verified layout")
    check_layout(primary, entries)
    new = [None if e and e["name"] in REMOVED else (dict(e) if e else None) for e in entries]
    for e in new[30:38]:
        if e and e["name"] in MOVED:
            first, blocks = MOVED[e["name"]]
            e["first"], e["last"] = first, first + blocks - 1
    check_layout(primary, new)
    array = pack_entries(new, primary)
    crc = zlib.crc32(array)
    return new, array, pack_header(primary, crc), pack_header(backup, crc)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--tail", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    head, tail = args.head.read_bytes(), args.tail.read_bytes()
    primary, backup, entries = read_tables(head, tail)
    new, array, primary_block, backup_block = derive(primary, backup, entries)
    tail_base = primary["last_usable"]
    entry_offset = (backup["entry_lba"] - tail_base) * BLOCK
    new_head = head[:BLOCK] + primary_block + array + head[4 * BLOCK:]
    new_tail = (tail[:entry_offset] + array + tail[entry_offset + len(array):5 * BLOCK]
                + backup_block)
    # The derived images must parse back to exactly the derived layout.
    re_primary, _, re_entries = read_tables(new_head, new_tail)
    require(re_entries == new and re_primary["disk_guid"] == primary["disk_guid"],
            "derived tables do not parse back")
    writes = []
    for base, old, image in ((0, head, new_head), (tail_base, tail, new_tail)):
        for i in range(len(image) // BLOCK):
            block = image[i * BLOCK:(i + 1) * BLOCK]
            if block != old[i * BLOCK:(i + 1) * BLOCK]:
                writes.append({"lba": f"{base + i:#x}", "sha256": hashlib.sha256(block).hexdigest()})
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "sda-head-8x4k.bin").write_bytes(new_head)
    (args.out / "sda-tail-6x4k.bin").write_bytes(new_tail)
    record = {
        "source": {"head_sha256": hashlib.sha256(head).hexdigest(),
                   "tail_sha256": hashlib.sha256(tail).hexdigest()},
        "head_sha256": hashlib.sha256(new_head).hexdigest(),
        "tail_sha256": hashlib.sha256(new_tail).hexdigest(),
        "tail_first_lba": f"{tail_base:#x}",
        "writes": writes,
        "layout": [{"index": i + 1, "node": f"sda{i + 1}", "name": e["name"],
                    "first": f"{e['first']:#x}", "blocks": f"{e['last'] - e['first'] + 1:#x}",
                    "type": e["type"], "guid": e["guid"]}
                   for i, e in enumerate(new) if e and i >= 30],
    }
    (args.out / "layout.json").write_text(json.dumps(record, indent=2) + "\n")
    for w in writes:
        print(f"write LBA {w['lba']} sha256 {w['sha256']}")
    for e in record["layout"]:
        print(f"{e['node']:6} {e['name']:11} {e['first']:>10} {e['blocks']:>10} "
              f"{int(e['blocks'], 16) * BLOCK / 2**30:9.3f} GiB")


if __name__ == "__main__":
    main()
