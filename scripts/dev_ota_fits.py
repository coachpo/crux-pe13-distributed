#!/usr/bin/env python3
"""Write development OTA FITs into the Crux U-Boot cache slots.

The cache layout is fixed by the deployed U-Boot environment:

    MIUI        0x30000 0x28bd
    TWRP        0x34000 0x2924
    PE Recovery 0x38000 0x3693
    PE ROM      0x3c000 0x2bae

Only the recovery and ROM windows are written.  The TWRP slot and the gaps
between slots are read before and after and must keep their CRC32.  The
caller supplies one U-Boot shell session from ``ram-boot-pe13.py``.
"""

import hashlib
import importlib.util
import json
import re
import zlib
from pathlib import Path


ADDRESS = "0xC0000000"
CACHE_START = 0x30000
CACHE_END = 0x48000
MIUI = {"lba": 0x30000, "blocks": 0x28bd}
TWRP = {"lba": 0x34000, "blocks": 0x2924}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def load_ram(workspace):
    path = Path(workspace) / "u-boot-port/scripts/ram-boot-pe13.py"
    require(path.is_file(), "Missing U-Boot session helper: " + str(path))
    spec = importlib.util.spec_from_file_location("ram_boot_pe13", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def crc_bytes(data):
    return f"{zlib.crc32(data) & 0xffffffff:08x}"


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            result.update(chunk)
    return result.hexdigest()


def range_crc(session, ram, start, count):
    require(CACHE_START <= start and start + count <= CACHE_END, "read outside cache")
    session.send(f"scsi read {ADDRESS} 0x{start:x} 0x{count:x}", timeout=60)
    text = session.send(f"crc32 {ADDRESS} 0x{count * 4096:x}", timeout=60)
    values = re.findall(r"(?i)==>\s*([0-9a-f]{8})\b", text)
    require(len(values) == 1, f"ambiguous crc for 0x{start:x}+0x{count:x}: {text!r}")
    return values[0].lower()


def padded(path, blocks):
    data = Path(path).read_bytes()
    slot = blocks * 4096
    require(len(data) <= slot, f"{path}: FIT is larger than its cache slot")
    value = data + b"\0" * (slot - len(data))
    require(value[:4] == bytes.fromhex("d00dfeed"), f"{path}: not a FIT")
    return value


def neighbors(recovery, rom):
    """Ranges that must not change: MIUI+TWRP, the recovery/ROM gap, the tail."""
    result = [(MIUI["lba"], recovery["lba"] - MIUI["lba"])]
    recovery_end = recovery["lba"] + recovery["blocks"]
    if rom["lba"] > recovery_end:
        result.append((recovery_end, rom["lba"] - recovery_end))
    rom_end = rom["lba"] + rom["blocks"]
    if CACHE_END > rom_end:
        result.append((rom_end, CACHE_END - rom_end))
    return result


def write_fits(session, ram, recovery, rom, out, uboot_version, twrp_fit):
    """Write and verify the two PE FIT windows.

    ``recovery`` and ``rom`` are dicts with ``path``, ``sha256``, ``lba`` and
    ``blocks``.  ``twrp_fit`` is the retained TWRP FIT used to prove the TWRP
    cache slot is still the expected one.
    """
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for role, spec in (("recovery", recovery), ("rom", rom)):
        data = padded(spec["path"], spec["blocks"])
        actual = hashlib.sha256(data).hexdigest()
        require(actual == spec["sha256"], f"{role}: padded FIT digest differs")
        rows.append({"role": role, "lba": spec["lba"], "blocks": spec["blocks"],
                     "sha256": actual, "crc32": crc_bytes(data), "data": data,
                     "path": str(spec["path"])})

    twrp = padded(twrp_fit, TWRP["blocks"])
    twrp_expected = crc_bytes(twrp)

    environment = ram.verify_environment(session, uboot_version)
    session.send("scsi dev 0")
    text = session.send(
        "if part start scsi 0 cache pe_cache_start && part size scsi 0 cache "
        "pe_cache_size; then printenv pe_cache_start pe_cache_size; else false; fi")
    values = dict(re.findall(r"(?m)^(pe_cache_(?:start|size))=(\w+)$", text))
    require(int(values.get("pe_cache_start", "0"), 16) == CACHE_START, "cache start")
    require(int(values.get("pe_cache_size", "0"), 16) == CACHE_END - CACHE_START, "cache size")

    twrp_value = range_crc(session, ram, TWRP["lba"], TWRP["blocks"])
    require(twrp_value == twrp_expected,
            f"cache TWRP slot does not match the retained TWRP FIT: {twrp_value}")

    record = {"schema_version": 1, "environment": environment,
              "twrp_slot_crc32": twrp_value, "twrp_expected_crc32": twrp_expected,
              "neighbors_before": [], "rows": [], "writes": [], "neighbors_after": []}
    gaps = neighbors(recovery, rom)
    for start, count in gaps:
        record["neighbors_before"].append(
            {"lba": hex(start), "blocks": hex(count),
             "crc32": range_crc(session, ram, start, count)})
    for row in rows:
        record["rows"].append(
            {"role": row["role"], "lba": hex(row["lba"]), "blocks": hex(row["blocks"]),
             "sha256": row["sha256"], "crc32": row["crc32"],
             "before_crc32": range_crc(session, ram, row["lba"], row["blocks"])})
    (out / "before.json").write_text(json.dumps(record, indent=2) + "\n")

    for row in rows:
        data = row["data"]
        session.send("setenv pe_update_stage 'run stop_acm; fastboot -l 0xC0000000 usb 0; run serial_gadget'")
        offset = ram.LOG.stat().st_size
        session.send("run pe_update_stage", prompt=False)
        transport = ram.wait_fastboot(session, "fastboot", ram.UBOOT_SERIAL)
        product = session.run(["fastboot", "-s", transport, "getvar", "product"])
        require(re.search(r"product:\s*xiaomi-crux\b", product.stdout + product.stderr),
                "wrong U-Boot fastboot target")
        session.run(["fastboot", "-s", transport, "stage", row["path"]], timeout=60)
        returned = session.run(["fastboot", "-s", transport, "continue"],
                               timeout=30, required=False)
        if returned.returncode:
            require(re.search(r"(?:unknown|unrecognized|unsupported|usage:)",
                             returned.stdout + returned.stderr, re.I),
                    "continue failed; do not resend")
            protocol_out = out / ("continue-" + row["role"])
            protocol_out.mkdir(exist_ok=True)
            ram.protocol_continue(transport, protocol_out)
        session.recover(offset, timeout=30)
        text = session.send(f"crc32 {ADDRESS} 0x{len(data):x}", timeout=30)
        require(re.search(r"==>\s*" + row["crc32"] + r"\b", text, re.I),
                f"{row['role']}: RAM CRC mismatch")
        session.send("scsi dev 0")
        session.send(f"scsi write {ADDRESS} 0x{row['lba']:x} 0x{row['blocks']:x}",
                     timeout=60)
        readback = range_crc(session, ram, row["lba"], row["blocks"])
        require(readback == row["crc32"],
                f"{row['role']}: cache readback mismatch: {readback}")
        record["writes"].append(
            {"role": row["role"], "lba": hex(row["lba"]), "blocks": hex(row["blocks"]),
             "sha256": row["sha256"], "crc32": row["crc32"],
             "readback_crc32": readback})

    for start, count in gaps:
        record["neighbors_after"].append(
            {"lba": hex(start), "blocks": hex(count),
             "crc32": range_crc(session, ram, start, count)})
    for before, after in zip(record["neighbors_before"], record["neighbors_after"]):
        require(before["crc32"] == after["crc32"],
                f"cache neighbor changed at {before['lba']}")
    record["status"] = "verified"
    (out / "fit-write.json").write_text(json.dumps(record, indent=2) + "\n")
    return record
