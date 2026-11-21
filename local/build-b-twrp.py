#!/usr/bin/env python3
"""Build the RAM-only B install TWRP (crux-install/1) from the exact RAM TWRP v2 FIT.

    build-b-twrp.py --v2-fit TWRP-RAM-INSTALL-ONLY.ITB --public-key KEY --mkimage MKIMAGE --out DIR [--without-identity]

Host only. Keeps v2's retained CI kernel and DT bytes, its PID 1 storage guard,
empty fstab/flags and capability bounding set (no SYS_ADMIN, so TWRP's
BLKROSET(0) unlock and BCB access stay ineffective). Ramdisk changes:
  - the zip-flash slider first sets tw_unmount_system=0 (there is no /system_root
    route to unmount) and tw_signed_zip_verify=0 (the package installer enforces
    the crux-install/1 signature itself), then flashes;
  - /crux/install-keys/b-p256.pub holds the B install public key (raw P-256 X||Y);
  - recovery.fstab names /misc as the in-RAM file /crux/bcb-sink.img, so that
    TWRP's init can record "boot-recovery" and perform reboot,recovery (it
    refuses to reboot when it cannot write a BCB); the shared misc stays
    unaddressed and read-only under the guard;
  - crux-ram-twrp-profile carries a new profile ID, distinct from v2 and menu 3.
Boot it only from the Recovery container U-Boot with `fastboot boot`; never flash it.

v2's DT bootargs carry the captured device identity. --without-identity drops
the ten ABL identity tokens (package-pe13.py's list) from /chosen/bootargs with
fdtput and keeps every other DT node, property and the string table; the new DT
digest enters the profile ID, so it is a new profile. The Recovery container
then supplies the recipient's identity from ABL (`crux_install bootargs`).
"""

import argparse
import gzip
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
PACKAGE_PE13 = HERE.parent / "references/pe13-boot-contracts/package-pe13.py"
# RAM TWRP v2: the retained CI TWRP FIT (df812eef...) with the PID 1 storage guard,
# no mount routes and a bounded capability set; kept outside the repositories
# because its DT bootargs carry the device identity.
V2_FIT_SHA256 = "3dab88d57ea205ac54db167282a0547461eee81a1e6eebdfdc528ba4e348e808"
V2_PROFILE_ID = "d9e985bf6ad11b6143787603898f041764813026d0da6d3b001c4cef2a8d4297"
THEME = "twres/portrait.xml"
KEY_DIR = ("crux", "crux/install-keys")
KEY = "crux/install-keys/b-p256.pub"
SINK = "crux/bcb-sink.img"
FSTAB = "system/etc/recovery.fstab"
V2_FSTAB = b"# RAM-only Crux installer: no persistent mount routes.\n"
B_FSTAB = (b"# B install TWRP: no persistent mount routes. /misc is an in-RAM BCB sink so that\n"
           b"# init can perform reboot,recovery without addressing the shared misc partition.\n"
           b"/crux/bcb-sink.img    /misc    emmc    defaults    defaults\n")
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


def strip_identity(fdt, require):
    """v2's DT with the ABL identity tokens dropped from /chosen/bootargs and nothing else changed."""
    spec = importlib.util.spec_from_file_location("package_pe13", PACKAGE_PE13)
    recipe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recipe)

    def bootargs(blob):
        return recipe.strings(recipe.find(recipe.Fdt(blob).root, "/chosen")["props"]["bootargs"])[0]

    def rest(blob):
        props = {path: dict(node["props"]) for path, node in recipe.walk(recipe.Fdt(blob).root)}
        props["/chosen"].pop("bootargs")
        return props

    def strings_block(blob):
        offset, size = struct.unpack(">I", blob[12:16])[0], struct.unpack(">I", blob[32:36])[0]
        return blob[offset:offset + size]

    stripped = recipe.without_identity(bootargs(fdt))
    require(stripped != bootargs(fdt), "v2 DT bootargs carry no identity tokens")
    fdtput = shutil.which("fdtput")
    require(fdtput, "fdtput (dtc) is required for --without-identity")
    with tempfile.TemporaryDirectory(prefix=".b-fdt-") as directory:
        path = Path(directory) / "fdt.dtb"
        path.write_bytes(fdt)
        subprocess.run([fdtput, "-t", "s", str(path), "/chosen", "bootargs", stripped], check=True)
        result = path.read_bytes()
    require(bootargs(result) == stripped, "stripped bootargs readback differs")
    require(rest(result) == rest(fdt), "the DT changed beyond /chosen/bootargs")
    require(strings_block(result) == strings_block(fdt), "the DT string table changed")
    return result


def main():
    spec = importlib.util.spec_from_file_location("ram_twrp_pack", HERE / "ram_twrp_pack.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pack = vars(module)
    require, sha = pack["require"], pack["sha"]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--v2-fit", type=Path, required=True, help="RAM TWRP v2 FIT (3dab88d5...)")
    parser.add_argument("--public-key", type=Path, required=True, help="raw 64-byte P-256 X||Y public key")
    parser.add_argument("--mkimage", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="new output directory")
    parser.add_argument("--without-identity", action="store_true",
                        help="drop the ABL identity tokens from v2's DT bootargs (a new profile)")
    args = parser.parse_args()
    source = args.v2_fit.read_bytes()
    require(sha(source) == V2_FIT_SHA256, "source is not the exact RAM TWRP v2 FIT")
    key = args.public_key.read_bytes()
    require(len(key) == 64, "public key must be raw 64-byte P-256 X||Y")
    components = pack["fit_components"](source)
    v2_fdt = components["fdt"]["data"]
    if args.without_identity:
        components = {**components, "fdt": {**components["fdt"], "data": strip_identity(v2_fdt, require)}}
    original = pack["cpio_entries"](gzip.decompress(components["ramdisk"]["data"]))
    require(not ({*KEY_DIR, KEY, SINK} & original.keys()), "v2 already has install keys or a BCB sink")
    require(original[FSTAB].data == V2_FSTAB, "unexpected v2 recovery.fstab")
    entries = original.copy()
    trailer = entries.pop("TRAILER!!!")
    entries.pop(pack["PROFILE"])
    theme = entries[THEME].data.decode()
    require(theme.count(SLIDER) == 1, "unexpected zip-flash slider in the v2 theme")
    entries[THEME] = entries[THEME].with_data(theme.replace(SLIDER, B_SLIDER).encode())
    entries[FSTAB] = entries[FSTAB].with_data(B_FSTAB)
    inode = max(entry.fields[0] for entry in original.values()) + 1
    for name, mode, data in ((KEY_DIR[0], stat.S_IFDIR | 0o755, b""), (KEY_DIR[1], stat.S_IFDIR | 0o755, b""),
                             (KEY, stat.S_IFREG | 0o644, key), (SINK, stat.S_IFREG | 0o600, bytes(16384))):
        entries[name] = pack["Entry"](name, (inode, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name) + 1, 0), data)
        inode += 1
    seed = V2_FIT_SHA256.encode() + (sha(components["fdt"]["data"]).encode() if args.without_identity else b"")
    identity = sha(seed + pack["cpio_archive"]({**entries, "TRAILER!!!": trailer}))
    entries[pack["PROFILE"]] = original[pack["PROFILE"]].with_data((identity + "\n").encode())
    entries["TRAILER!!!"] = trailer
    ramdisk = gzip.compress(pack["cpio_archive"](entries), compresslevel=9, mtime=0)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    destination = out / "twrp-b-install.itb"
    with tempfile.TemporaryDirectory(prefix=".b-fit-", dir=out) as directory:
        fit = pack["write_fit"](Path(directory), components, ramdisk, args.mkimage.resolve())
        packed = fit.read_bytes()
        assembled = pack["fit_components"](packed)
        for role, props in components.items():
            keys = ("type", "arch", "os", "compression", "load", "entry")
            require(all(props.get(k) == assembled[role].get(k) for k in keys), f"changed {role} FIT metadata")
            if role != "ramdisk":
                require(props["data"] == assembled[role]["data"], f"changed retained {role}")
        readback = pack["cpio_entries"](gzip.decompress(assembled["ramdisk"]["data"]))
        require(readback == entries, "B TWRP CPIO readback differs")
        changed = sorted(n for n in original if n in readback and original[n] != readback[n])
        added = sorted(readback.keys() - original.keys())
        require(changed == sorted([THEME, FSTAB, pack["PROFILE"]]) and added == sorted([*KEY_DIR, KEY, SINK]),
                "B TWRP changed entries other than the theme, fstab, profile, install key and BCB sink")
        fit.rename(destination)
    receipt = {
        "schema_version": 1,
        "role": "crux-install/1 B install TWRP, RAM-only (fastboot boot from the Recovery container U-Boot)",
        "profile_id": identity,
        "status": "host_verified_only",
        "fit": {"path": str(destination), "sha256": sha(packed), "bytes": len(packed)},
        "components": {name: {"sha256": sha(props["data"]), "bytes": len(props["data"])}
                       for name, props in assembled.items()},
        "source": {"fit_sha256": V2_FIT_SHA256, "v2_profile_id": V2_PROFILE_ID, "v2_fdt_sha256": sha(v2_fdt)},
        "device_identity_in_dt_bootargs": not args.without_identity,
        "install_key_sha256": sha(key),
        "ramdisk_changed": changed,
        "ramdisk_added": added,
        "bcb_sink": "/misc -> /crux/bcb-sink.img (16 KiB, RAM only)",
        "retained_from_v2": ["kernel", "fdt except the identity tokens in /chosen/bootargs"
                             if args.without_identity else "fdt", "PID 1 storage guard",
                             "no persistent mount routes or flags",
                             "recovery capabilities CHOWN DAC_OVERRIDE FOWNER SETGID SETUID"],
    }
    (out / "candidate.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
