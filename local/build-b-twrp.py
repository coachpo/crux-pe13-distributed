#!/usr/bin/env python3
"""Build the RAM-only B install TWRP (crux-install/1) from the public twrp-crux FIT.

    build-b-twrp.py --source-fit twrp-crux-3.7.1_12.itb --preinit ELF --public-key KEY
                    --mkimage MKIMAGE --out DIR

Host only. The source is the identity-free FIT that twrp-crux builds reproducibly
(the same bytes from the VM and from its Actions artifact); its SHA-256 is pinned
below. Kernel and DT bytes are kept; the DT bootargs must carry no ABL identity
token, so the Recovery container supplies the recipient's identity from ABL
(`crux_install bootargs`). Ramdisk changes:
  - /init is the static AArch64 build of ram_twrp_preinit.c, the PID 1 storage
    guard that leaves only the four PE partitions of a known LUN0 layout writable;
  - twrp.flags names no route, and recovery.fstab names only /misc as the in-RAM
    file /crux/bcb-sink.img, so that TWRP's init can record "boot-recovery" and
    perform reboot,recovery (it refuses to reboot when it cannot write a BCB);
    the shared misc stays unaddressed and read-only under the guard;
  - the recovery service runs with the capability bounding set CHOWN
    DAC_OVERRIDE FOWNER SETGID SETUID (no SYS_ADMIN, so TWRP's BLKROSET(0) unlock
    and BCB access stay ineffective), and the Qualcomm decryption services are
    not imported;
  - /TWRP/.twrpcf marks TWRP's default folder in RAM;
  - the zip-flash slider first sets tw_unmount_system=0 (there is no /system_root
    route to unmount) and tw_signed_zip_verify=0 (the package installer enforces
    the crux-install/1 signature itself), then flashes;
  - /crux/install-keys/b-p256.pub holds the B install public key (raw P-256 X||Y);
  - crux-ram-twrp-profile carries the profile ID, derived from the source FIT,
    the preinit and the resulting archive.
Boot it only from the Recovery container U-Boot with `fastboot boot`; never flash it.
"""

import argparse
import gzip
import importlib.util
import json
from pathlib import Path
import re
import stat
import struct
import tempfile

HERE = Path(__file__).resolve().parent
PACKAGE_PE13 = HERE.parent / "references/pe13-boot-contracts/package-pe13.py"
# twrp-crux e9a39b6 (kernel 2900b2cc1af5): twrp-crux-3.7.1_12.itb, identical from the
# VM cold build and from Actions run 45329517224.
SOURCE_FIT_SHA256 = "2df4c8d6effc88a02f230f660d8ff2b0ca4dc7a2d613fefde2eeff2acaa131bd"
THEME = "twres/portrait.xml"
FSTAB = "system/etc/recovery.fstab"
FLAGS = "system/etc/twrp.flags"
SERVICES = "init.recovery.service.rc"
QCOM_RC = "init.recovery.qcom.rc"
KEY_DIR = ("crux", "crux/install-keys")
KEY = "crux/install-keys/b-p256.pub"
SINK = "crux/bcb-sink.img"
FOLDER = ("TWRP", "TWRP/.twrpcf")
NO_ROUTES = b"# RAM-only Crux installer: no persistent mount routes.\n"
B_FSTAB = (b"# B install TWRP: no persistent mount routes. /misc is an in-RAM BCB sink so that\n"
           b"# init can perform reboot,recovery without addressing the shared misc partition.\n"
           b"/crux/bcb-sink.img    /misc    emmc    defaults    defaults\n")
STANZA = b"service recovery /system/bin/recovery\n"
CAPABILITIES = b"    capabilities CHOWN DAC_OVERRIDE FOWNER SETGID SETUID\n"
DECRYPT_IMPORT = b"import /init.recovery.qcom_decrypt.rc\n"
NO_DECRYPT = b"# No Qualcomm decryption services in the RAM-only installer.\n"
SLIDER = ("\t\t\t<slider>\n"
          "\t\t\t\t<text>{@swipe_flash=Swipe to confirm Flash}</text>\n"
          "\t\t\t\t<action function=\"flash\">flash_zip</action>\n"
          "\t\t\t</slider>\n")
B_SLIDER = ("\t\t\t<slider>\n"
            "\t\t\t\t<text>{@swipe_flash=Swipe to confirm Flash}</text>\n"
            "\t\t\t\t<actions>\n"
            "\t\t\t\t\t<action function=\"set\">tw_unmount_system=0</action>\n"
            "\t\t\t\t\t<action function=\"set\">tw_signed_zip_verify=0</action>\n"
            "\t\t\t\t\t<action function=\"flash\">flash_zip</action>\n"
            "\t\t\t\t</actions>\n"
            "\t\t\t</slider>\n")
CHANGED = sorted(["init", FSTAB, FLAGS, SERVICES, QCOM_RC, THEME])


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module)


PACK = load("ram_twrp_pack", HERE / "ram_twrp_pack.py")
require, sha = PACK["require"], PACK["sha"]


def check_preinit(blob):
    require(blob[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<HH", blob, 16) == (2, 183),
            "preinit must be a 64-bit little-endian AArch64 executable")
    phoff, = struct.unpack_from("<Q", blob, 32)
    count, = struct.unpack_from("<H", blob, 56)
    kinds = {struct.unpack_from("<I", blob, phoff + 56 * i)[0] for i in range(count)}
    require(1 in kinds and not kinds & {2, 3}, "preinit must be static (no dynamic loader)")


def check_dt(fdt):
    """The source DT takes the ramdisk entry and carries no device identity."""
    recipe = load("package_pe13", PACKAGE_PE13)
    tree = PACK["fdt_nodes"](fdt)
    bootargs = tree["/chosen"]["bootargs"].rstrip(b"\0").decode("ascii")
    require(recipe["without_identity"](bootargs) == " ".join(bootargs.split()),
            "source DT bootargs carry ABL identity tokens")
    tokens = bootargs.split()
    require("init=/init" in tokens and not any(t.startswith(("root=", "rdinit=", "androidboot.force_normal_boot="))
                                               for t in tokens),
            "source DT would not take the guarded ramdisk entry")
    routes = {name: props["dev"].rstrip(b"\0").decode("ascii") for name, props in tree.items()
              if name.startswith("/firmware/android/fstab/")
              and props.get("status", b"ok\0").rstrip(b"\0") in {b"ok", b"okay"}}
    require(routes == {"/firmware/android/fstab/vendor": "/dev/block/platform/soc/1d84000.ufshc/by-name/vendor"},
            "unexpected selectable DT fstab entry")


def install_ramdisk(source, preinit, key):
    """The install ramdisk entries and profile ID from the source FIT's ramdisk entries."""
    Entry = PACK["Entry"]
    require(len(key) == 64, "public key must be raw 64-byte P-256 X||Y")
    require(not ({PACK["PROFILE"], *KEY_DIR, KEY, SINK, *FOLDER} & source.keys()),
            "source ramdisk already has install TWRP entries")
    init = source["init"]
    require(init.data == b"/system/bin/init" and stat.S_ISLNK(init.fields[1]) and init.fields[2:5] == (0, 0, 1),
            "unexpected source /init link")
    services = source[SERVICES].data
    require(services.count(STANZA) == 1 and b"capabilities" not in services, "unexpected source recovery service")
    qcom = source[QCOM_RC].data
    require(qcom.count(DECRYPT_IMPORT) == 1, "missing source recovery decryption import")
    theme = source[THEME].data.decode()
    require(theme.count(SLIDER) == 1, "unexpected zip-flash slider in the source theme")
    entries = source.copy()
    trailer = entries.pop("TRAILER!!!")
    entries["init"] = init.with_data(preinit, stat.S_IFREG | 0o750)
    entries[FSTAB] = entries[FSTAB].with_data(B_FSTAB)
    entries[FLAGS] = entries[FLAGS].with_data(NO_ROUTES)
    entries[SERVICES] = entries[SERVICES].with_data(services.replace(STANZA, STANZA + CAPABILITIES))
    entries[QCOM_RC] = entries[QCOM_RC].with_data(qcom.replace(DECRYPT_IMPORT, NO_DECRYPT))
    entries[THEME] = entries[THEME].with_data(theme.replace(SLIDER, B_SLIDER).encode())
    inode = max(entry.fields[0] for entry in source.values()) + 1
    for name, mode, data in ((FOLDER[0], stat.S_IFDIR | 0o755, b""), (FOLDER[1], stat.S_IFREG | 0o644, b""),
                             (KEY_DIR[0], stat.S_IFDIR | 0o755, b""), (KEY_DIR[1], stat.S_IFDIR | 0o755, b""),
                             (KEY, stat.S_IFREG | 0o644, key), (SINK, stat.S_IFREG | 0o600, bytes(16384))):
        entries[name] = Entry(name, (inode, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name) + 1, 0), data)
        inode += 1
    seed = SOURCE_FIT_SHA256.encode() + sha(preinit).encode()
    profile = sha(seed + PACK["cpio_archive"]({**entries, "TRAILER!!!": trailer}))
    marker = (profile + "\n").encode()
    entries[PACK["PROFILE"]] = Entry(PACK["PROFILE"], (inode, stat.S_IFREG | 0o444, 0, 0, 1, 0, len(marker),
                                                       0, 0, 0, 0, len(PACK["PROFILE"]) + 1, 0), marker)
    entries["TRAILER!!!"] = trailer
    check_install_ramdisk(source, entries)
    return entries, profile


def check_install_ramdisk(source, entries):
    changed = sorted(name for name in source if name in entries and source[name] != entries[name])
    added = sorted(entries.keys() - source.keys())
    require(changed == CHANGED and added == sorted([PACK["PROFILE"], *KEY_DIR, KEY, SINK, *FOLDER])
            and source.keys() <= entries.keys(), "install ramdisk changed unexpected entries")
    # Another vendor/ODM/board fstab would let TWRP import a route after the guard.
    for name in entries:
        if name not in {FSTAB, FLAGS}:
            require("fstab" not in name.rsplit("/", 1)[-1], f"unexpected recovery fstab alias: {name}")
    require(entries[FSTAB].data == B_FSTAB, "recovery.fstab names more than the BCB sink")
    require(all(not line.strip() or line.startswith(b"#") for line in entries[FLAGS].data.splitlines()),
            "persistent routes in twrp.flags")
    for name, entry in entries.items():
        if not name.endswith(".rc") or name.startswith(("init.recovery.qcom_decrypt", "init.recovery.services.qcom_decrypt")):
            continue
        require(not re.search(rb"(?m)^\s*(?:mount_all\s|mount\s+\S+\s+/dev/block/|import\s+\S*qcom_decrypt)",
                              entry.data), f"persistent mount/decrypt trigger in {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-fit", type=Path, required=True, help="twrp-crux FIT (2df4c8d6...)")
    parser.add_argument("--preinit", type=Path, required=True, help="static AArch64 build of ram_twrp_preinit.c")
    parser.add_argument("--public-key", type=Path, required=True, help="raw 64-byte P-256 X||Y public key")
    parser.add_argument("--mkimage", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="new output directory")
    args = parser.parse_args()
    source_fit = args.source_fit.read_bytes()
    require(sha(source_fit) == SOURCE_FIT_SHA256, "source is not the pinned twrp-crux FIT")
    preinit = args.preinit.read_bytes()
    check_preinit(preinit)
    key = args.public_key.read_bytes()
    components = PACK["fit_components"](source_fit)
    check_dt(components["fdt"]["data"])
    source = PACK["cpio_entries"](gzip.decompress(components["ramdisk"]["data"]))
    entries, profile = install_ramdisk(source, preinit, key)
    ramdisk = gzip.compress(PACK["cpio_archive"](entries), compresslevel=9, mtime=0)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    destination = out / "twrp-b-install.itb"
    with tempfile.TemporaryDirectory(prefix=".b-fit-", dir=out) as directory:
        fit = PACK["write_fit"](Path(directory), components, ramdisk, args.mkimage.resolve())
        packed = fit.read_bytes()
        assembled = PACK["fit_components"](packed)
        for role, props in components.items():
            keys = ("type", "arch", "os", "compression", "load", "entry")
            require(all(props.get(k) == assembled[role].get(k) for k in keys), f"changed {role} FIT metadata")
            if role != "ramdisk":
                require(props["data"] == assembled[role]["data"], f"changed source {role}")
        require(PACK["cpio_entries"](gzip.decompress(assembled["ramdisk"]["data"])) == entries,
                "install TWRP CPIO readback differs")
        fit.rename(destination)
    receipt = {
        "schema_version": 2,
        "role": "crux-install/1 B install TWRP, RAM-only (fastboot boot from the Recovery container U-Boot)",
        "profile_id": profile,
        "status": "host_verified_only",
        "fit": {"name": destination.name, "sha256": sha(packed), "bytes": len(packed)},
        "components": {name: {"sha256": sha(props["data"]), "bytes": len(props["data"])}
                       for name, props in assembled.items()},
        "source": {"fit_sha256": SOURCE_FIT_SHA256, "project": "twrp-crux e9a39b6, kernel 2900b2cc1af5"},
        "device_identity_in_dt_bootargs": False,
        "install_key_sha256": sha(key),
        "preinit_sha256": sha(preinit),
        "ramdisk_changed": CHANGED,
        "ramdisk_added": sorted(entries.keys() - source.keys()),
        "bcb_sink": "/misc -> /crux/bcb-sink.img (16 KiB, RAM only)",
        "recovery_capabilities": CAPABILITIES.decode().split(None, 1)[1].strip(),
        "retained_from_source": ["kernel", "fdt"],
    }
    (out / "candidate.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
