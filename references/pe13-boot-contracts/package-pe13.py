#!/usr/bin/env python3
"""Package matching Cepheus-derived Crux PE13 kernel/DTs for U-Boot.

Example (after copying the completed build artifacts from cruxbuild):
  python3 package-pe13.py --image Image --base-dir dts/qcom \
      --overlay dts/qcom/crux-sm8150-overlay.dtbo \
      --recovery-ramdisk ramdisk-recovery.img --boot-ramdisk ramdisk.img \
      --dualboot --output-dir output

This script never accesses a device or changes an input artifact.  All four
source bases must accept the single Crux overlay.  Only the base whose msm-id
matches the recorded device trees is packaged.  The hardware graph, symbols,
and phandles always come from the new kernel's base and overlay. Existing PE
boot contracts remain available as development profiles. --boot-ramdisk selects
native Android first-stage init and removes the legacy ROM/debug arguments;
--dualboot selects the source crux_uboot fstab for Recovery too. Packaging does
not establish source provenance or hardware/release validation. Diagnostic
ramdisks must be selected explicitly with --diagnostic-recovery-ramdisk.
--development-adb permits source-built ROM/Recovery ADB authentication
overrides for development while retaining native boot/storage validation.
"""

import argparse
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import tempfile


BASE_IDS = {
    "sm8150": (339, 0x10000),
    "sm8150-v2": (339, 0x20000),
    "sm8150p": (361, 0x10000),
    "sm8150p-v2": (361, 0x20000),
}
REFERENCE_SHA256 = {
    "rom-pstore.dtb": "0786c26214617bcd3b5002062003426be847820e89fe614207dea6d01b2e3790",
    "recovery-pstore.dtb": "e70ab2f59216877b14c04c0b64ffc8e3174425377ece8230bb2b27a0144461c3",
}
EXPECTED_BANKS = [(0x80000000, 0x3BB00000),
                  (0xC0000000, 0xC0000000),
                  (0x180000000, 0x100000000)]
FIT_ADDRESS = 0xC0000000
KERNEL_ADDRESS = 0x80080000
READ_BLOCKS = {"rom": 0x3100, "recovery": 0x3B00}
PSTORE = {
    "compatible": b"ramoops\0",
    "reg": struct.pack(">4I", 0, 0xB0000000, 0, 0x400000),
    "record-size": struct.pack(">I", 0x100000),
    "console-size": struct.pack(">I", 0x200000),
    "pmsg-size": struct.pack(">I", 0x100000),
    "ftrace-size": struct.pack(">I", 0),
    "ecc-size": struct.pack(">I", 0),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def cells(data):
    require(len(data) % 4 == 0, "Property has incomplete 32-bit cells")
    return struct.unpack(">" + "I" * (len(data) // 4), data)


def strings(data):
    require(data.endswith(b"\0"), "Property is not a terminated string")
    return data[:-1].decode().split("\0")


def align(value, boundary=4):
    return (value + boundary - 1) & ~(boundary - 1)


def node(name):
    return {"name": name, "props": {}, "children": []}


def find(root, path):
    current = root
    for name in path.strip("/").split("/") if path != "/" else []:
        found = [c for c in current["children"] if c["name"] == name]
        require(len(found) == 1, f"Missing or duplicate DT node: {path}")
        current = found[0]
    return current


def walk(root, path="/"):
    yield path, root
    for child in root["children"]:
        yield from walk(child, path.rstrip("/") + "/" + child["name"])


class Fdt:
    """Minimal binary FDT reader/writer, preserving raw property values."""

    def __init__(self, data):
        require(len(data) >= 40, "Truncated FDT header")
        h = struct.unpack_from(">10I", data)
        magic, total, off_struct, off_strings, off_reserve = h[:5]
        version, compatible_version, self.boot_cpuid, strings_size, struct_size = h[5:]
        require(magic == 0xD00DFEED, "Invalid FDT/FIT magic")
        require(40 <= total <= len(data), "Invalid FDT totalsize")
        require(version == 17 and compatible_version <= 17, "Unsupported FDT version")
        require(off_struct + struct_size <= total and
                off_strings + strings_size <= total, "FDT blocks outside totalsize")
        self.reservations = []
        p = off_reserve
        while True:
            require(p + 16 <= total, "Truncated FDT reserve map")
            address, size = struct.unpack_from(">QQ", data, p)
            p += 16
            if address == size == 0:
                break
            self.reservations.append((address, size))
        names = data[off_strings:off_strings + strings_size]
        p, end = off_struct, off_struct + struct_size
        stack = []
        self.root = None
        while p < end:
            token = struct.unpack_from(">I", data, p)[0]
            p += 4
            if token == 1:
                stop = data.find(b"\0", p, end)
                require(stop >= p, "Unterminated FDT node name")
                current = node(data[p:stop].decode())
                p = align(stop + 1)
                if stack:
                    stack[-1]["children"].append(current)
                else:
                    require(self.root is None, "Multiple FDT roots")
                    self.root = current
                stack.append(current)
            elif token == 2:
                require(stack, "Unbalanced FDT end-node")
                stack.pop()
            elif token == 3:
                require(stack and p + 8 <= end, "Truncated FDT property")
                length, offset = struct.unpack_from(">II", data, p)
                p += 8
                require(offset < len(names), "FDT property name outside string block")
                stop = names.find(b"\0", offset)
                require(stop >= offset and p + length <= end, "Truncated FDT property data")
                name = names[offset:stop].decode()
                require(name not in stack[-1]["props"], "Duplicate FDT property")
                stack[-1]["props"][name] = data[p:p + length]
                p = align(p + length)
            elif token == 4:
                pass
            elif token == 9:
                require(not stack and self.root is not None, "Unbalanced FDT structure")
                break
            else:
                raise ValueError(f"Invalid FDT structure token {token}")
        else:
            raise ValueError("Missing FDT end token")

    def encode(self):
        names, offsets = bytearray(), {}
        for _, current in walk(self.root):
            for name in current["props"]:
                if name not in offsets:
                    offsets[name] = len(names)
                    names.extend(name.encode() + b"\0")
        body = bytearray()

        def emit(current):
            body.extend(struct.pack(">I", 1) + current["name"].encode() + b"\0")
            body.extend(b"\0" * (align(len(body)) - len(body)))
            for name, value in current["props"].items():
                body.extend(struct.pack(">III", 3, len(value), offsets[name]) + value)
                body.extend(b"\0" * (align(len(body)) - len(body)))
            for child in current["children"]:
                emit(child)
            body.extend(struct.pack(">I", 2))

        emit(self.root)
        body.extend(struct.pack(">I", 9))
        reserve = b"".join(struct.pack(">QQ", *r) for r in self.reservations) + b"\0" * 16
        off_struct, off_strings = 40 + len(reserve), 40 + len(reserve) + len(body)
        header = struct.pack(">10I", 0xD00DFEED, off_strings + len(names),
                             off_struct, off_strings, 40, 17, 16,
                             self.boot_cpuid, len(names), len(body))
        return header + reserve + body + names


def read_tree(path):
    return Fdt(path.read_bytes())


def reserved_by_name(root):
    result = {}
    for child in find(root, "/reserved-memory")["children"]:
        key = child["name"].split("@")[0]
        require(key not in result, f"Duplicate reserved-memory region {key}")
        result[key] = child
    return result


def validate_graph(tree):
    seen = {}
    for path, current in walk(tree.root):
        for prop in ("phandle", "linux,phandle"):
            if prop in current["props"]:
                value = cells(current["props"][prop])
                require(len(value) == 1 and value[0] not in (0, 0xFFFFFFFF),
                        f"Invalid phandle at {path}")
                require(value[0] not in seen or seen[value[0]] == path,
                        f"Duplicate phandle {value[0]:x}")
                seen[value[0]] = path
    for value in find(tree.root, "/__symbols__")["props"].values():
        for path in strings(value):
            find(tree.root, path)
    require(len(find(tree.root, "/reserved-memory")["children"]) <= 64,
            "Reserved-memory count exceeds new kernel MAX_RESERVED_REGIONS=64")


def validate_ufs(root):
    props = find(root, "/soc/ufshc@1d84000")["props"]
    require(cells(props["reg"]) == (0x1D84000, 0x2500, 0x1D90000, 0x8000) and
            strings(props["reg-names"]) == ["ufs_mem", "ufs_ice"] and
            props.get("status") in (b"ok\0", b"okay\0"),
            "Matching source DT lacks enabled UFS/ICE boot contract")


def validate_usb1(root):
    require(find(root, "/vendor/extcon_usb1")["props"].get("status") == b"disabled\0",
            "Matching source DT must disable the unused USB1 extcon provider")


def cpio_files(archive, include_modes=False):
    """Read newc entries without extracting or changing host files."""
    cursor, files, modes = 0, {}, {}
    while cursor + 110 <= len(archive):
        header = archive[cursor:cursor + 110]
        require(header[:6] == b"070701", "Expected a newc recovery archive")
        size = int(header[54:62], 16)
        name_size = int(header[94:102], 16)
        require(name_size > 0 and cursor + 110 + name_size <= len(archive),
                "Truncated cpio filename")
        name_data = archive[cursor + 110:cursor + 110 + name_size]
        require(name_data.endswith(b"\0"), "Unterminated cpio filename")
        name = name_data[:-1].decode()
        if name.startswith("./"):
            name = name[2:]
        cursor = align(cursor + 110 + name_size)
        require(cursor + size <= len(archive), "Truncated cpio payload")
        data = archive[cursor:cursor + size]
        cursor = align(cursor + size)
        if name == "TRAILER!!!":
            return (files, modes) if include_modes else files
        require(name not in files, f"Duplicate cpio entry: {name}")
        files[name] = data
        modes[name] = int(header[14:22], 16)
    raise ValueError("Missing cpio trailer")


def validate_dualboot_fstab(data):
    expected = {"/system": "pesystem", "/vendor": "pevendor",
                "/data": "peuserdata", "/metadata": "pemetadata"}
    original_partitions = {"system", "vendor", "userdata", "metadata",
                           "misc", "cache", "boot", "recovery"}
    entries = {}
    for line in data.decode().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        require(len(fields) == 5, "Malformed dualboot fstab entry")
        device, mount, filesystem, options, flags = fields
        require(mount not in {"/cache", "/boot", "/recovery", "/misc"} and
                device.rsplit("/", 1)[-1] not in original_partitions,
                f"Dualboot fstab must not expose original/shared partition: {device} at {mount}")
        if mount in expected:
            require(mount not in entries, f"Duplicate dualboot fstab mount: {mount}")
            devices = {prefix + expected[mount] for prefix in
                       ("/dev/block/by-name/", "/dev/block/bootdevice/by-name/")}
            require(device in devices and filesystem == "ext4",
                    f"Dualboot fstab maps {mount} to an unexpected partition/filesystem")
            entries[mount] = {"device": device, "filesystem": filesystem,
                              "mount_options": options.split(","), "fs_mgr_flags": flags.split(",")}
    require(set(entries) == set(expected), "Dualboot fstab lacks a required PE mount")
    require({"fileencryption=ice", "keydirectory=/metadata/vold/metadata_encryption"}
            <= set(entries["/data"]["fs_mgr_flags"]),
            "Dualboot fstab must retain native FBE and metadata keydirectory")
    return {"sha256": sha256(data), "mounts": entries,
            "native_fbe_flags_present": True, "device_encryption_verified": False}


def resolve_cpio_path(path, files, modes):
    """Resolve archive symlinks as absolute paths inside the packaged root."""
    pending, resolved, links = list(PurePosixPath(path.lstrip("/")).parts), [], 0
    while pending:
        part = pending.pop(0)
        if part == ".":
            continue
        if part == "..":
            require(resolved, f"Fstab alias escapes ramdisk root: {path}")
            resolved.pop()
            continue
        resolved.append(part)
        name = "/".join(resolved)
        if modes.get(name, 0) & 0o170000 == 0o120000:
            links += 1
            require(links <= 40, f"Unresolvable fstab alias: {path}")
            target = files[name].decode()
            resolved = [] if target.startswith("/") else resolved[:-1]
            pending = list(PurePosixPath(target.lstrip("/")).parts) + pending
    name = "/".join(resolved)
    require(name in files and modes[name] & 0o170000 == 0o100000,
            f"Fstab alias lacks a packaged regular target: {path} -> {name}")
    return name


def validate_dualboot_fstab_aliases(files, modes):
    # fs_mgr tries suffix, hardware and platform fstabs at several locations.
    # Check every packaged name, including fallbacks whose current suffix is
    # unknown; recovery.fstab can also be selected through the /etc symlink.
    aliases = sorted(name for name in files
                     if PurePosixPath(name).name.startswith("fstab.") or
                     PurePosixPath(name).name == "recovery.fstab")
    require(aliases, "Dualboot ramdisk lacks packaged fstab aliases")
    resolved = {}
    for path in aliases:
        target = resolve_cpio_path(path, files, modes)
        try:
            validate_dualboot_fstab(files[target])
        except ValueError as error:
            raise ValueError(f"Unsafe dualboot fstab alias {path}: {error}") from error
        resolved[path] = target
    return resolved


def validate_no_dt_fstab_routes(root):
    # PE13 fs_mgr reads DT entries before appending file entries. It checks the
    # table/entry status itself, without inheriting a disabled ancestor status.
    for path, current in walk(root):
        if current["name"] != "fstab":
            continue
        # ReadDtFile removes the final byte, then compares the whole string;
        # compatible lists are not matched by their first string or membership.
        compatible = current["props"].get("compatible", b"")
        if not compatible or compatible[:-1] != b"android,fstab":
            continue
        parent = find(root, path.rsplit("/", 1)[0] or "/")
        compatible = parent["props"].get("compatible", b"")
        if not compatible or compatible[:-1] != b"android,firmware":
            continue
        status = current["props"].get("status", b"")
        if status and status[:-1] not in (b"ok", b"okay"):
            continue
        for child in current["children"]:
            status = child["props"].get("status", b"")
            if not status or status[:-1] in (b"ok", b"okay"):
                raise ValueError(f"Active DT fstab route at {path}/{child['name']} can override "
                                 "PE first-stage fstab; disable or remove the source DT table")


def validate_ramdisk(data, diagnostic, role="recovery", dualboot=False, development_adb=False):
    require(not (diagnostic and development_adb),
            "Development ADB and diagnostic ramdisk profiles are mutually exclusive")
    require(data[:2] == b"\x1f\x8b", f"{role} ramdisk must be gzip-compressed newc")
    files, modes = cpio_files(gzip.decompress(data), include_modes=True)
    require("init" in files, f"{role} ramdisk lacks init")
    if role == "recovery":
        require("prop.default" in files, "Recovery ramdisk lacks prop.default")
        property_files = ["prop.default"]
        fstab_path = "system/etc/recovery.fstab" if dualboot else None
    else:
        require("system/bin/recovery" not in files, "Boot ramdisk contains Recovery rather than native init")
        require("system/etc/ramdisk/build.prop" in files, "Native boot ramdisk lacks ramdisk/build.prop")
        property_files = [name for name in ("prop.default", "system/etc/ramdisk/build.prop") if name in files]
        fstab_paths = [name for name in ("fstab.crux_uboot", "system/etc/fstab.crux_uboot",
                                         "first_stage_ramdisk/fstab.crux_uboot",
                                         "first_stage_ramdisk/system/etc/fstab.crux_uboot") if name in files]
        require(fstab_paths, "Native boot ramdisk lacks the source fstab.crux_uboot")
        fstab_contents = [files[resolve_cpio_path(name, files, modes)] for name in fstab_paths]
        require(all(data == fstab_contents[0] for data in fstab_contents),
                "Native boot ramdisk contains conflicting crux_uboot fstabs")
        fstab_path = fstab_paths[0]
    fstab_report = None
    if fstab_path:
        require(fstab_path in files, f"{role} ramdisk lacks {fstab_path}")
        target = resolve_cpio_path(fstab_path, files, modes)
        fstab_report = dict(validate_dualboot_fstab(files[target]), path=fstab_path)
    if dualboot or role == "boot":
        aliases = validate_dualboot_fstab_aliases(files, modes)
        fstab_report["checked_alias_paths"] = list(aliases)
        fstab_report["resolved_alias_paths"] = aliases
    properties = {}
    for line in "\n".join(files[name].decode() for name in property_files).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if key.strip() in {"ro.secure", "ro.adb.secure", "ro.adb.secure.recovery",
                               "ro.debuggable", "persist.sys.usb.config", "ro.build.type"}:
                key, value = key.strip(), value.strip()
                require(key not in properties or properties[key] == value,
                        f"Conflicting recovery security property: {key}")
                properties[key] = value
    embedded_keys = sorted(name for name in files
                           if name in {"adb_keys", "data/misc/adb/adb_keys"})
    if not diagnostic:
        if role == "recovery":
            require(properties.get("ro.secure") == "1", "Recovery ramdisk must retain ro.secure=1")
        else:
            require(properties.get("ro.secure", "1") == "1",
                    "Native boot ramdisk contains insecure property overrides")
        if development_adb:
            require(properties.get("ro.adb.secure", "1" if role == "boot" else None) in {"0", "1"},
                    "Development ADB ramdisk must use ro.adb.secure=0 or 1")
            require(properties.get("ro.adb.secure.recovery", "1") in {"0", "1"},
                    "Development ADB ramdisk must use ro.adb.secure.recovery=0 or 1")
        else:
            require(properties.get("ro.adb.secure", "1" if role == "boot" else None) == "1",
                    f"{role} ramdisk must retain ro.adb.secure=1; use --development-adb "
                    "for source-built development ADB or --diagnostic-recovery-ramdisk "
                    "for an explicit Recovery diagnostic payload")
            require(properties.get("ro.adb.secure.recovery") != "0",
                    "Recovery ramdisk disables recovery ADB authentication")
        require(not embedded_keys, "Recovery ramdisk contains pre-authorized ADB keys")
    return {"sha256": sha256(data), "file_size": len(data),
            "classification": "diagnostic" if diagnostic else ("development-adb" if development_adb else
                              ("authenticated-recovery" if role == "recovery" else "native-first-stage")),
            "adb_authentication_profile": "diagnostic" if diagnostic else ("development" if development_adb else "authenticated"),
            "runtime_adb_authentication_verified": False,
            "property_files": property_files,
            "security_properties_from_system_at_runtime": role == "boot",
            "dualboot_fstab": fstab_report,
            "security_properties": properties, "embedded_adb_key_paths": embedded_keys,
            "validation_scope": "gzip/newc, prop.default authentication properties and ADB key paths; "
                                "does not certify runtime policy or device behavior"}


def native_bootargs(bootargs, dualboot):
    """Retain captured board identity while using native init/source defaults."""
    removed_keys = {"root", "rootwait", "ro", "skip_initramfs", "init", "nokaslr",
                    "softlockup_panic", "panic_on_oops", "panic", "printk.devkmsg",
                    "androidboot.crux_debug", "androidboot.crux_minimal_art",
                    "androidboot.init_log_level", "androidboot.init_fatal_panic",
                    "androidboot.selinux", "watchdog_v2.enable", "msm_poweroff.download_mode",
                    "androidboot.init_fatal_reboot_target", "androidboot.boot_devices", "kpti"}
    if dualboot:
        removed_keys.add("androidboot.fstab_suffix")
    tokens = [token for token in bootargs.split()
              if token.split("=", 1)[0] not in removed_keys
              and not token.split("=", 1)[0].startswith("hung_task")]
    if dualboot:
        tokens.append("androidboot.fstab_suffix=crux_uboot")
    # These are the current PE13 BoardConfig's native defaults.  The fatal
    # reboot target is the U-Boot bootloader: PE has no /misc fstab entry, so
    # a BCB recovery route cannot write shared misc and cannot land in the
    # MIUI-side physical recovery.
    for token in ("androidboot.init_fatal_reboot_target=bootloader", "kpti=off"):
        if token not in tokens:
            tokens.append(token)
    require("androidboot.init_fatal_reboot_target=bootloader" in tokens and
            not any(t.startswith("androidboot.init_fatal_reboot_target=") and
                    t != "androidboot.init_fatal_reboot_target=bootloader" for t in tokens),
            "native bootargs must keep the fatal reboot target on the bootloader")
    # ueventd only creates the /dev/block/by-name aliases for devices listed in
    # androidboot.boot_devices; the recovery reference bootargs predate the
    # BoardConfig token, so derive it from the captured bootdevice value.
    bootdevice = next((t.split("=", 1)[1] for t in tokens
                       if t.startswith("androidboot.bootdevice=")), None)
    require(bootdevice, "native bootargs lack androidboot.bootdevice for boot_devices derivation")
    tokens.append("androidboot.boot_devices=soc/" + bootdevice)
    return " ".join(tokens)


def validate_source_pstore(root):
    nodes = [current for _, current in walk(root)
             if current["props"].get("compatible") == b"ramoops\0"]
    require(len(nodes) == 1, "Matching source DT must contain exactly one ramoops node")
    properties = {k: v for k, v in nodes[0]["props"].items()
                  if k not in {"phandle", "linux,phandle"}}
    require(properties == PSTORE,
            "Crux source overlay must already provide B0000000/4MiB pstore with 1+2+1MiB layout")


def ranges(data, address_cells=2, size_cells=2):
    values, stride = cells(data), address_cells + size_cells
    require(len(values) % stride == 0, "Malformed address/size ranges")
    result = []
    for offset in range(0, len(values), stride):
        address = size = 0
        for value in values[offset:offset + address_cells]:
            address = (address << 32) | value
        for value in values[offset + address_cells:offset + stride]:
            size = (size << 32) | value
        result.append((address, size))
    return result


def contained(address, size, banks):
    return size > 0 and any(start <= address and address + size <= start + length
                           for start, length in banks)


def overlap(a, b):
    return a[0] < b[0] + b[1] and b[0] < a[0] + a[1]


def boot_tree(merged, reference, mode, native=False, dualboot=False):
    tree = copy.deepcopy(merged)
    ref_chosen = find(reference.root, "/chosen")["props"]
    require(set(ref_chosen) == {"bootargs"}, "Unexpected reference /chosen properties")
    bootargs = strings(ref_chosen["bootargs"])[0]
    require(not re.search(r"(?i)warm.?reset", bootargs), "Warm-reset flag in reference bootargs")
    if native or dualboot:
        bootargs = native_bootargs(bootargs, dualboot or native)
    find(tree.root, "/chosen")["props"]["bootargs"] = bootargs.encode() + b"\0"
    memory = find(reference.root, "/memory")
    require(set(memory["props"]) <= {"device_type", "reg", "ddr_device_type"},
            "Unexpected reference memory properties; do not copy phandles")
    expected_memory = [(0, 0)] if mode == "rom" else EXPECTED_BANKS
    require(sorted(ranges(memory["props"]["reg"])) == expected_memory,
            "Reference RAM banks/template differ from verified device")
    existing_memory = [(p, n) for p, n in walk(tree.root)
                       if n["props"].get("device_type") == b"memory\0"]
    require(len(existing_memory) == 1 and existing_memory[0][0] == "/memory" and
            existing_memory[0][1]["props"] == {"device_type": b"memory\0", "reg": b"\0" * 16},
            "New source memory node differs from expected bootloader-filled template")
    existing_memory[0][1]["props"].update(memory["props"])

    old_regions, new_regions = reserved_by_name(reference.root), reserved_by_name(tree.root)
    # Crux's source overlay already fixes the layout for AOSP packaging too.
    # Retain its phandle; canonicalize the node name for the FIT boot contract.
    validate_source_pstore(tree.root)
    ramoops = [n for n in new_regions.values()
               if n["props"].get("compatible") == b"ramoops\0"]
    require(len(ramoops) == 1, "New source must contain exactly one ramoops node")
    current = ramoops[0]
    old_path = "/reserved-memory/" + current["name"]
    current["name"] = "ramoops@b0000000"
    old_pstore = [n for n in old_regions.values()
                  if n["props"].get("compatible") == b"ramoops\0"]
    require(len(old_pstore) == 1 and old_pstore[0]["props"] == PSTORE,
            "Recorded pstore contract has changed")
    handles = {k: v for k, v in current["props"].items()
               if k in {"phandle", "linux,phandle"}}
    current["props"] = dict(PSTORE, **handles)
    # Normal PE reboots must preserve the DDR-backed pmsg/console rings.
    # msm-poweroff otherwise selects a PMIC hard reset for an empty command.
    restart = find(tree.root, "/soc/restart@c264000")
    require(restart["props"].get("compatible") == b"qcom,pshold\0",
            "Unexpected PE restart controller")
    restart["props"]["qcom,force-warm-reboot"] = b""
    for name, value in find(tree.root, "/__symbols__")["props"].items():
        if value == old_path.encode() + b"\0":
            find(tree.root, "/__symbols__")["props"][name] = b"/reserved-memory/ramoops@b0000000\0"

    extras, omitted = [], []
    for name, old in old_regions.items():
        if old["props"].get("compatible") == b"ramoops\0":
            continue
        if name in new_regions:
            for prop in ("reg", "size", "alloc-ranges", "no-map", "reusable"):
                require(old["props"].get(prop) == new_regions[name]["props"].get(prop),
                        f"New source/reference reservation differs: {name}/{prop}")
        else:
            # The new SM8150 source removed this dynamic dump allocation and
            # has no dump_mem symbol/consumer. It is not bootloader metadata.
            if name == "mem_dump_region":
                require(old["props"].get("compatible") == b"shared-dma-pool\0" and
                        "reg" not in old["props"] and "reusable" in old["props"] and
                        cells(old["props"]["size"]) == (0, 0x2400000),
                        "Unexpected old dynamic dump allocation")
                omitted.append(name)
                continue
            # These ROM-only fixed carveouts are firmware/display reservations,
            # not references to the old hardware graph. Copy no phandle/symbol.
            require(mode == "rom" and name in {"xbl_dump_mem", "ramdump_fb_region"},
                    f"Unexplained reference-only reservation: {name}")
            require("reg" in old["props"] and "no-map" in old["props"] and not old["children"],
                    f"Non-fixed reference-only reservation: {name}")
            preserved = node(old["name"])
            preserved["props"] = {k: v for k, v in old["props"].items()
                                  if k in {"reg", "no-map", "compatible"}}
            find(tree.root, "/reserved-memory")["children"].append(preserved)
            extras.append(name)
    validate_graph(tree)
    reservations = []
    for child in find(tree.root, "/reserved-memory")["children"]:
        for address, size in ranges(child["props"].get("reg", b"")):
            require(contained(address, size, EXPECTED_BANKS),
                    f"Reservation outside verified RAM: {child['name']}")
            reservations.append((address, size, child["name"]))
    for i, (address, size, name) in enumerate(reservations):
        for other_address, other_size, other_name in reservations[:i]:
            require(not overlap((address, size), (other_address, other_size)),
                    f"Reserved-memory overlap: {name} and {other_name}")
    for path, current in walk(tree.root):
        require(not any(re.search(r"(?i)warm.?reset", prop) for prop in current["props"]),
                f"Warm-reset property at {path}")
    if native or dualboot:
        validate_no_dt_fstab_routes(tree.root)
    return tree, reservations, extras, omitted


def validate_image(data):
    require(len(data) >= 64 and data[56:60] == b"ARM\x64", "Not an uncompressed arm64 Image")
    text_offset, image_size, flags = struct.unpack_from("<3Q", data, 8)
    require(text_offset == 0x80000, "Unexpected arm64 text offset")
    require(image_size >= len(data), "Image size does not cover its text/data file")
    require(not flags & 1, "Big-endian arm64 Image is unsupported")
    require(contained(KERNEL_ADDRESS, image_size, EXPECTED_BANKS), "Kernel footprint outside RAM")
    return {"file_size": len(data), "image_size": image_size,
            "text_offset": text_offset, "flags": flags, "sha256": sha256(data)}


def checked_load(address, size, reservations, occupied):
    return contained(address, size, EXPECTED_BANKS) and not any(
        overlap((address, size), (start, length))
        for start, length in occupied + [(a, s) for a, s, _ in reservations])


def its_text(mode, image, dtb, ramdisk, fdt_load, ramdisk_load=None, profile="development"):
    def component(name, path, kind, address, linux=False):
        os_property = '\n            os = "linux";' if linux else ""
        entry = f"\n            entry = <0x{address:x}>;" if name == "kernel" else ""
        return f'''        {name} {{
            description = "Crux Cepheus-derived {name}";
            data = /incbin/({json.dumps(str(path))});
            type = "{kind}";
            arch = "arm64";{os_property}
            compression = "none";
            load = <0x{address:x}>;{entry}
            hash-1 {{ algo = "sha256"; }};
        }};
'''
    images = component("kernel", image, "kernel", KERNEL_ADDRESS, True)
    images += component("fdt", dtb, "flat_dt", fdt_load)
    if ramdisk:
        images += component("ramdisk", ramdisk, "ramdisk", ramdisk_load, True)
    rd_config = ' ramdisk = "ramdisk";' if ramdisk else ""
    return f'''/dts-v1/;
/ {{
    description = "Crux Cepheus-derived {mode}; matching source DT; {profile}";
    #address-cells = <1>;
    #size-cells = <1>;
    images {{
{images}    }};
    configurations {{
        default = "conf";
        conf {{ kernel = "kernel"; fdt = "fdt";{rd_config} }};
    }};
}};
'''


def validate_fit(path, mode, expected, loads):
    data = path.read_bytes()
    tree = Fdt(data)
    require(struct.unpack_from(">I", data, 4)[0] == len(data), "Unexpected FIT trailing/external data")
    require(len(data) <= READ_BLOCKS[mode] * 4096, f"{mode} FIT exceeds cache read limit")
    image_nodes = find(tree.root, "/images")["children"]
    require({n["name"] for n in image_nodes} == set(expected), "Unexpected FIT image set")
    for current in image_nodes:
        name, props = current["name"], current["props"]
        require(props.get("data") == expected[name], f"Wrong embedded FIT bytes: {name}")
        require(props.get("compression") == b"none\0" and props.get("arch") == b"arm64\0",
                f"Unexpected FIT architecture/compression: {name}")
        expected_type = "flat_dt" if name == "fdt" else name
        require(props.get("type") == expected_type.encode() + b"\0", f"Wrong FIT type: {name}")
        if name != "fdt":
            require(props.get("os") == b"linux\0", f"Wrong FIT OS: {name}")
        if name == "kernel":
            require(cells(props["entry"]) == (KERNEL_ADDRESS,), "Wrong FIT kernel entry")
        require(cells(props["load"]) == (loads[name],), f"Wrong FIT load address: {name}")
        hashes = current["children"]
        require(len(hashes) == 1 and hashes[0]["props"].get("algo") == b"sha256\0" and
                hashes[0]["props"].get("value") == hashlib.sha256(expected[name]).digest(),
                f"Bad FIT SHA-256: {name}")
    require(find(tree.root, "/configurations")["props"]["default"] == b"conf\0",
            "Wrong default FIT configuration")
    config = find(tree.root, "/configurations/conf")["props"]
    require(config == {name: name.encode() + b"\0" for name in expected},
            "Wrong FIT configuration references")
    return {"file_size": len(data), "cache_read_blocks": READ_BLOCKS[mode],
            "cache_read_bytes": READ_BLOCKS[mode] * 4096,
            "sha256": sha256(data), "loads": loads,
            "embedded_sha256": {k: sha256(v) for k, v in expected.items()}}


def main():
    workspace = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path,
                        default=workspace / "u-boot-port/references/pe13-boot-contracts")
    ramdisk_group = parser.add_mutually_exclusive_group(required=True)
    ramdisk_group.add_argument("--recovery-ramdisk", type=Path,
                               help="PE recovery ramdisk, authenticated unless --development-adb is selected")
    ramdisk_group.add_argument("--diagnostic-recovery-ramdisk", type=Path,
                               help="Explicit diagnostic ramdisk, may bypass ADB authentication")
    rom_group = parser.add_mutually_exclusive_group()
    rom_group.add_argument("--boot-ramdisk", type=Path,
                           help="Source-built native first-stage ROM ramdisk with fstab.crux_uboot")
    rom_group.add_argument("--legacy-ramdiskless", action="store_true",
                           help="Explicitly retain the old development ROM root/PARTUUID recipe")
    rom_group.add_argument("--recovery-only", action="store_true",
                           help="Package only Recovery; do not generate a legacy ROM candidate")
    parser.add_argument("--dualboot", action="store_true",
                        help="Use source crux_uboot fstab and native bootargs in Recovery too")
    parser.add_argument("--development-adb", action="store_true",
                        help="Permit source-built ROM/Recovery RSA-disabled development ADB; retain ro.secure and storage checks")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mkimage", default=shutil.which("mkimage") or
                        str(workspace / "u-boot-port/tools/host/mkimage"))
    parser.add_argument("--fdtoverlay", default=shutil.which("fdtoverlay") or "fdtoverlay")
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()
    diagnostic = args.diagnostic_recovery_ramdisk is not None
    require(not (diagnostic and args.development_adb),
            "--development-adb cannot be combined with --diagnostic-recovery-ramdisk")
    image = args.image.resolve()
    ramdisk = (args.diagnostic_recovery_ramdisk if diagnostic else args.recovery_ramdisk).resolve()
    image_data, ramdisk_data = image.read_bytes(), ramdisk.read_bytes()
    ramdisk_report = validate_ramdisk(ramdisk_data, diagnostic, dualboot=args.dualboot,
                                     development_adb=args.development_adb)
    boot_ramdisk = args.boot_ramdisk.resolve() if args.boot_ramdisk else None
    boot_ramdisk_data = boot_ramdisk.read_bytes() if boot_ramdisk else None
    boot_ramdisk_report = validate_ramdisk(boot_ramdisk_data, False, "boot", dualboot=True,
                                          development_adb=args.development_adb) if boot_ramdisk else None
    native = boot_ramdisk is not None
    modes = ["recovery"] if args.recovery_only else list(READ_BLOCKS)
    header = validate_image(image_data)
    references = {}
    for mode in READ_BLOCKS:
        path = args.reference_dir / f"{mode}-pstore.dtb"
        require(sha256(path.read_bytes()) == REFERENCE_SHA256[path.name],
                f"Recorded {mode} device tree changed")
        references[mode] = read_tree(path)
    require(cells(references["rom"].root["props"]["qcom,msm-id"]) ==
            cells(references["recovery"].root["props"]["qcom,msm-id"]),
            "Recorded device trees disagree on SoC revision")
    device_id = cells(references["rom"].root["props"]["qcom,msm-id"])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = [f"{name}-crux-merged.dtb" for name in BASE_IDS]
    outputs += [f"{mode}-cepheus.{ext}" for mode in modes for ext in ("dtb", "its", "itb")]
    outputs += ["fit-validation.json", "FIT-SHA256SUMS"]
    require(not any((args.output_dir / name).exists() for name in outputs),
            "FIT outputs already exist; choose a fresh output directory")
    report = {"hardware_verified": False, "source_build_verified": False,
              "classification": "development-adb" if args.development_adb else
                                ("native-fbe-source-candidate" if native or args.dualboot else "development"),
              "development_adb": args.development_adb,
              "rom_boot_type": None if args.recovery_only else ("native-first-stage-ramdisk" if native else "legacy-ramdiskless"),
              "recovery_only": args.recovery_only,
              "recovery_fstab_profile": "crux_uboot" if args.dualboot else "legacy-default",
              "release_ready": False,
              "release_limitations": ["Native init/FBE/runtime policy require device verification" if native or args.dualboot else "Preserved development PE bootargs",
                                      "Source provenance/build and device behavior require separate verification"] +
                                     (["Explicit development ADB profile permits RSA authentication overrides; not a release profile"]
                                      if args.development_adb else []),
              "kernel": header, "recovery_ramdisk": ramdisk_report, "boot_ramdisk": boot_ramdisk_report,
              "inputs": {"image": str(image), "base_dir": str(args.base_dir.resolve()),
                         "overlay": str(args.overlay.resolve()), "ramdisk": str(ramdisk),
                         "boot_ramdisk": str(boot_ramdisk) if boot_ramdisk else None,
                         "packager_sha256": sha256(Path(__file__).read_bytes())},
              "reference_sha256": REFERENCE_SHA256, "overlay_sha256": sha256(args.overlay.read_bytes()),
              "bases": {}, "selected_base": None, "fits": {}}
    # Qualcomm DTBO root identity is entry metadata, outside the fragments.
    # fdtoverlay preserves the base identity, so install the board metadata.
    overlay_props = read_tree(args.overlay).root["props"]
    identity = {key: overlay_props[key] for key in ("model", "compatible", "qcom,board-id")}
    require(cells(identity["qcom,board-id"]) == (0x2B, 1) and
            strings(identity["model"]) == ["SDX50M CRUX"] and
            "qcom,sm8150" in strings(identity["compatible"]), "Unexpected Crux DTBO identity")
    report["board_identity_from_dtbo_metadata"] = True

    with tempfile.TemporaryDirectory(prefix=".fit-build-", dir=args.output_dir) as scratch:
        stage = Path(scratch)
        selected = None
        for name, expected_id in BASE_IDS.items():
            base = args.base_dir / (name + ".dtb")
            require(cells(read_tree(base).root["props"]["qcom,msm-id"]) == expected_id,
                    f"Unexpected source base msm-id: {name}")
            merged_path = stage / (name + "-crux-merged.dtb")
            subprocess.run([args.fdtoverlay, "-i", str(base), "-o", str(merged_path),
                            str(args.overlay)], check=True)
            merged = read_tree(merged_path)
            merged.root["props"].update(identity)
            merged_path.write_bytes(merged.encode())
            validate_graph(merged)
            validate_ufs(merged.root)
            validate_usb1(merged.root)
            validate_source_pstore(merged.root)
            if native or args.dualboot:
                validate_no_dt_fstab_routes(merged.root)
            require(cells(merged.root["props"]["qcom,msm-id"]) == expected_id and
                    cells(merged.root["props"]["qcom,board-id"]) == (0x2B, 1) and
                    strings(merged.root["props"]["model"]) == ["SDX50M CRUX"],
                    f"Crux overlay/base identity mismatch: {name}")
            report["bases"][name] = {"msm_id": expected_id, "base_sha256": sha256(base.read_bytes()),
                                     "merged_sha256": sha256(merged_path.read_bytes()),
                                     "single_overlay_applied": True}
            if expected_id == device_id:
                require(selected is None, "Multiple bases match the recorded device")
                selected, report["selected_base"] = merged, name
        require(selected is not None, "No new source base matches recorded device msm-id")
        for mode in modes:
            reference = references[mode]
            profile = "diagnostic" if mode == "recovery" and diagnostic else (
                "development-adb" if args.development_adb and (mode == "recovery" or native) else
                ("native-fbe-source-candidate" if (mode == "rom" and native) or
                 (mode == "recovery" and args.dualboot) else "development"))
            tree, reservations, extras, omitted = boot_tree(selected, reference, mode,
                                                          native=native and mode == "rom",
                                                          dualboot=args.dualboot and mode == "recovery")
            mode_ramdisk = ramdisk if mode == "recovery" else boot_ramdisk
            mode_ramdisk_data = ramdisk_data if mode == "recovery" else boot_ramdisk_data
            kernel_range = (KERNEL_ADDRESS, header["image_size"])
            require(checked_load(*kernel_range, reservations, []),
                    "Kernel text/data/BSS overlaps a firmware reservation")
            dtb_path = stage / f"{mode}-cepheus.dtb"
            dtb_data = tree.encode()
            require(Fdt(dtb_data).root == tree.root, "FDT binary round-trip changed the graph")
            dtb_path.write_bytes(dtb_data)
            # Verified U-Boot bootm reserves the entire arm64 image_size, then
            # relocates FDT/initrd outside it with initrd_high/fdt_high unset.
            # FIT load addresses are staging addresses: overlap with BSS is
            # safe before entry; overlap with bytes in the Image file is not.
            occupied = [(KERNEL_ADDRESS, len(image_data)),
                        (FIT_ADDRESS, READ_BLOCKS[mode] * 4096)]
            require(checked_load(FIT_ADDRESS, READ_BLOCKS[mode] * 4096, reservations,
                                 [(KERNEL_ADDRESS, header["image_size"])]),
                    "FIT cache read buffer is outside RAM or overlaps a reservation/kernel")
            ramdisk_load = None
            if mode_ramdisk:
                ramdisk_load = next((a for a in (0x83000000, 0xD0000000)
                                     if checked_load(a, len(mode_ramdisk_data), reservations, occupied)), None)
                require(ramdisk_load is not None, f"No safe {mode} ramdisk staging address")
                occupied.append((ramdisk_load, len(mode_ramdisk_data)))
            fdt_load = next((a for a in (0x84800000, 0xCF000000)
                             if checked_load(a, len(dtb_data), reservations, occupied)), None)
            require(fdt_load is not None, "No safe FDT staging address")
            its_path, fit_path = stage / f"{mode}-cepheus.its", stage / f"{mode}-cepheus.itb"
            its_path.write_text(its_text(mode, image, dtb_path,
                                         mode_ramdisk, fdt_load, ramdisk_load, profile))
            try:
                subprocess.run([args.mkimage, "-f", str(its_path), str(fit_path)],
                               check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except subprocess.CalledProcessError as error:
                raise ValueError(f"mkimage failed (exit {error.returncode}):\n{error.stdout}") from error
            expected = {"kernel": image_data, "fdt": dtb_data}
            loads = {"kernel": KERNEL_ADDRESS, "fdt": fdt_load}
            if mode_ramdisk:
                expected["ramdisk"], loads["ramdisk"] = mode_ramdisk_data, ramdisk_load
            verification = validate_fit(fit_path, mode, expected, loads)
            verification.update({"extra_fixed_reservations": extras,
                                 "omitted_legacy_dynamic_reservations": omitted,
                                 "reserved_memory_child_count": len(find(tree.root, "/reserved-memory")["children"]),
                                 "reserved_regions": [{"name": n, "address": a, "size": s}
                                                      for a, s, n in reservations],
                                 "bootargs": strings(find(tree.root, "/chosen")["props"]["bootargs"])[0],
                                 "classification": profile,
                                 "boot_type": "recovery-ramdisk" if mode == "recovery" else ("native-first-stage-ramdisk" if native else "legacy-ramdiskless"),
                                 "boot_command": (f"scsi dev 0; scsi read 0xC0000000 "
                                                  f"0x{'38000' if mode == 'recovery' else '3c000'} "
                                                  f"0x{READ_BLOCKS[mode]:x}; setenv fdt_high; setenv initrd_high; "
                                                  "bootm start 0xC0000000; bootm loados; "
                                                  + ("bootm ramdisk; " if mode_ramdisk else "")
                                                  + "bootm prep; run boot_go"),
                                 "staging_and_kernel_file_ranges_disjoint": True,
                                 "staging_overlaps_bss": {name: overlap((address, len(expected[name])), kernel_range)
                                                          for name, address in loads.items() if name != "kernel"},
                                 "required_uboot_contract": {"reserve_arm64_image_size": True,
                                                             "fixup_memory_banks_from_previous_bootloader": True,
                                                             "initrd_high_unset": True,
                                                             "fdt_high_unset": True,
                                                             "relocate_initrd_and_fdt_before_kernel_entry": True}})
            report["fits"][mode] = verification
            # Save a reproducible ITS pointing to final paths, not the scratch directory.
            its_path.write_text(its_text(mode, image, args.output_dir.resolve() / dtb_path.name,
                                         mode_ramdisk, fdt_load, ramdisk_load, profile))
        (stage / "fit-validation.json").write_text(json.dumps(report, indent=2) + "\n")
        checksums = [f"{sha256((stage / name).read_bytes())}  {name}" for name in outputs
                     if name != "FIT-SHA256SUMS"]
        (stage / "FIT-SHA256SUMS").write_text("\n".join(checksums) + "\n")
        for name in outputs:
            os.replace(stage / name, args.output_dir / name)
    print(json.dumps({"selected_base": report["selected_base"],
                      "fits": {k: {p: v[p] for p in ("file_size", "sha256", "loads")}
                               for k, v in report["fits"].items()},
                      "hardware_verified": False}, indent=2))


if __name__ == "__main__":
    main()
