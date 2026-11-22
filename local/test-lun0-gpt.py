"""Host-only fixtures for the LUN0 GPT derivation, on synthetic tables of the crux disk shape."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
import zlib

TOOL = Path(__file__).with_name("lun0-gpt.py")
spec = importlib.util.spec_from_file_location("lun0_gpt", TOOL)
gpt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpt)

B = gpt.BLOCK
DISK = 0x3b57000          # LUN0 size in 4 KiB blocks
LAST_USABLE = DISK - 6
SYSTEM_TYPE = "97d7b011-54da-4835-b3c4-917ad6e73d74"


def entry(name, first, blocks, kind=SYSTEM_TYPE):
    return {"type": kind, "guid": str(uuid.uuid4()), "first": first, "last": first + blocks - 1,
            "attr": 0, "name": name}


def official_entries():
    """Entries 1-34 as the MIUI EDL package leaves them: 30 small partitions, then the tail."""
    entries = [entry(f"part{i + 1}", 0x6 + i * 0x3000, 0x3000) for i in range(30)]
    entries += [entry(name, first, blocks) for name, first, blocks in gpt.OFFICIAL_TAIL]
    entries.append(entry("userdata", gpt.OFFICIAL_USERDATA, LAST_USABLE - gpt.OFFICIAL_USERDATA + 1))
    return entries + [None] * 30


def pe_only_entries():
    entries = official_entries()[:30]
    kinds = dict(gpt.PE_ENTRIES)
    entries += [entry(name, first, blocks, kinds.get(name, SYSTEM_TYPE)) for name, first, blocks in gpt.SOURCE_TAIL]
    return entries + [None] * 26


def captures(entries):
    """LBA 0-7 and the last six blocks of a disk holding ENTRIES, primary and backup identical."""
    disk_guid = uuid.uuid4().bytes_le
    header = {"signature": b"EFI PART", "revision": 0x10000, "size": 92, "crc": 0, "reserved": 0,
              "current": 1, "alternate": DISK - 1, "first_usable": 6, "last_usable": LAST_USABLE,
              "disk_guid": disk_guid, "entry_lba": 2, "entries": 64, "entry_size": 128, "entry_crc": 0}
    array = gpt.pack_entries(entries, header)
    crc = zlib.crc32(array)
    backup = dict(header, current=DISK - 1, alternate=1, entry_lba=LAST_USABLE + 1)
    head = b"\x55" * B + gpt.pack_header(header, crc) + array + b"\x11" * (4 * B)
    tail = b"\x22" * B + array + b"\0" * (2 * B) + gpt.pack_header(backup, crc)
    return head, tail


def run(head, tail):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "head.bin").write_bytes(head)
        (tmp / "tail.bin").write_bytes(tail)
        result = subprocess.run([sys.executable, "-B", str(TOOL), "--head", str(tmp / "head.bin"),
                                 "--tail", str(tmp / "tail.bin"), "--out", str(tmp / "out")],
                                capture_output=True, text=True)
        if result.returncode:
            return result, None, None
        out = tmp / "out"
        new_head, new_tail = (out / "sda-head-8x4k.bin").read_bytes(), (out / "sda-tail-6x4k.bin").read_bytes()
        _, _, parsed = gpt.read_tables(new_head, new_tail)
        return result, parsed, json.loads((out / "layout.json").read_text())


def extents(entries):
    return [(e["name"], e["first"], e["last"] - e["first"] + 1, e["type"]) if e else None for e in entries]


class Lun0GptTests(unittest.TestCase):
    def check_reclaimed(self, parsed, record):
        self.assertIsNone(parsed[30], "cust must be removed and its slot left empty")
        for e in parsed[31:38]:
            first, blocks = gpt.MOVED[e["name"]]
            self.assertEqual((e["first"], e["last"] - e["first"] + 1), (first, blocks), e["name"])
        self.assertEqual([e["name"] for e in parsed[31:38]],
                         ["vendor", "system", "userdata", "pesystem", "pevendor", "peuserdata", "pemetadata"])
        self.assertFalse(any(parsed[38:]))
        self.assertLessEqual(parsed[36]["last"], LAST_USABLE)
        self.assertEqual({w["lba"] for w in record["writes"]},
                         {"0x1", "0x2", "0x3", f"{LAST_USABLE + 1:#x}", f"{LAST_USABLE + 2:#x}", f"{DISK - 1:#x}"})

    def test_official_table_becomes_the_reclaimed_layout(self):
        source = official_entries()
        result, parsed, record = run(*captures(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record["source"]["kind"], "official")
        self.check_reclaimed(parsed, record)
        self.assertEqual(parsed[:30], source[:30], "entries 1-30 must be untouched")
        for e, (name, kind) in zip(parsed[34:38], gpt.PE_ENTRIES):
            self.assertEqual((e["name"], e["type"]), (name, kind))
            self.assertEqual(e["guid"], str(uuid.uuid5(gpt.PE_GUID_NAMESPACE, name)))
        for old, new in zip(source[31:34], parsed[31:34]):
            self.assertEqual((old["type"], old["guid"], old["attr"]), (new["type"], new["guid"], new["attr"]))

    def test_pe_only_table_keeps_its_guids_and_matches_the_official_result(self):
        source = pe_only_entries()
        result, parsed, record = run(*captures(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record["source"]["kind"], "pe-only")
        self.check_reclaimed(parsed, record)
        self.assertEqual([e["guid"] for e in parsed[31:38]], [e["guid"] for e in source[31:38]])
        _, from_official, _ = run(*captures(official_entries()))
        self.assertEqual(extents(parsed[30:]), extents(from_official[30:]))

    def test_other_tables_are_rejected(self):
        reclaimed = official_entries()
        _, parsed, _ = run(*captures(reclaimed))
        result, _, _ = run(*captures(parsed))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither the official MIUI table", result.stderr)
        short = official_entries()
        short[33] = entry("userdata", gpt.OFFICIAL_USERDATA, 0x1000)
        self.assertNotEqual(run(*captures(short))[0].returncode, 0)

    def test_corrupt_capture_is_rejected(self):
        head, tail = captures(official_entries())
        head = head[:2 * B] + bytes([head[2 * B] ^ 1]) + head[2 * B + 1:]
        result, _, _ = run(head, tail)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CRC mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()
