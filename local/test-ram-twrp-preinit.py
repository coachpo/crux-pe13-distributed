"""Host fixtures for the install TWRP storage guard decision, on a synthetic sysfs.

ram_twrp_preinit.c is built with -DCRUX_GUARD_TEST, which replaces PID 1 with a
report of the decision (guarded layout or read-only) and of the nodes it would
make read-only. Linux only (the guard uses Linux headers): run it in the VM.
"""

import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).with_name("ram_twrp_preinit.c")
UFS = "sys/devices/platform/soc/1d84000.ufshc/host0/target0:0:0"
B = 8  # 512-B sectors per 4096-B block

NAMED = {"misc": "sda11", "metadata": "sda19", "recovery": "sda28", "cache": "sda29",
         "vendor": "sda32", "system": "sda33", "userdata": "sda34",
         "vbmeta": "sde10", "dtbo": "sde45", "boot": "sde49"}
RECLAIM = {"pemetadata": ("sda38", 0x63000, 0x1000), "pesystem": ("sda35", 0x64000, 0x100000),
           "pevendor": ("sda36", 0x164000, 0x70000), "peuserdata": ("sda37", 0x1d4000, 0x3982e00)}
PE_ONLY = {"pemetadata": ("sda38", 0x122000, 0x1000), "pesystem": ("sda35", 0x123000, 0x100000),
           "pevendor": ("sda36", 0x223000, 0x70000), "peuserdata": ("sda37", 0x293000, 0x38c3e00)}


def table(pe=RECLAIM, official=False):
    """{lun disk: [(node, partname, start, blocks)]} shaped like the crux disks."""
    parts = {f"sda{i}": f"lun0part{i}" for i in range(1, 35)}
    for name, node in NAMED.items():
        if node.startswith("sda"):
            parts[node] = name
    if official:
        parts["sda31"] = "cust"
    else:
        del parts["sda31"]
    lun0 = [(node, name, 0x6 + int(node[3:]) * 0x100, 0x10) for node, name in parts.items()]
    if not official:
        lun0 += [(node, name, start, blocks) for name, (node, start, blocks) in pe.items()]
    disks = {"sda": lun0}
    for i, disk in enumerate("bcdf", start=1):
        disks[f"sd{disk}"] = [(f"sd{disk}{n}", f"lun{i}part{n}", 0x6 + n * 0x10, 0x10) for n in (1, 2)]
    lun4 = [(f"sde{n}", f"lun4part{n}", 0x6 + n * 0x100, 0x10) for n in range(1, 50)]
    disks["sde"] = [(node, {v: k for k, v in NAMED.items()}.get(node, name), s, b) for node, name, s, b in lun4]
    return disks


@unittest.skipUnless(platform.system() == "Linux" and shutil.which("cc"), "Linux C compiler required")
class Guard(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="crux-guard-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.binary = self.root / "guard"
        subprocess.run(["cc", "-std=gnu11", "-Wall", "-Werror", "-DCRUX_GUARD_TEST",
                        f'-DSCRATCH="{self.root}"', "-o", str(self.binary), str(SOURCE)], check=True)
        self.minor = 0

    def node(self, directory, name, devtype, size, start=None, partname=None):
        directory.mkdir(parents=True)
        self.minor += 1
        lines = [f"MAJOR=8", f"MINOR={self.minor}", f"DEVNAME={name}", f"DEVTYPE={devtype}"]
        if partname:
            lines.append(f"PARTNAME={partname}")
        (directory / "uevent").write_text("\n".join(lines) + "\n")
        (directory / "size").write_text(f"{size}\n")
        if start is not None:
            (directory / "start").write_text(f"{start}\n")
        link = self.root / "sys/class/block" / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(os.path.relpath(directory, link.parent))

    def sysfs(self, disks, extra=()):
        for lun, (disk, parts) in enumerate(sorted(disks.items())):
            base = self.root / UFS / f"0:0:0:{lun}/block/{disk}"
            self.node(base, disk, "disk", 0x3b57000 * B)
            for node, name, start, blocks in parts:
                self.node(base / node, node, "partition", blocks * B, start * B, name)
        for name, partname in extra:
            self.node(self.root / "sys/devices/virtual/block" / name, name, "partition" if partname else "disk",
                      0x100000 * B, 0 if partname else None, partname)

    def run_guard(self):
        result = subprocess.run([str(self.binary)], capture_output=True, text=True)
        return result.returncode, result.stdout.splitlines()

    def ufs_nodes(self, disks):
        return sorted([*disks] + [node for parts in disks.values() for node, *_ in parts])

    def test_reclaimed_table_is_guarded(self):
        self.sysfs(table(), extra=[("loop0", None)])
        code, lines = self.run_guard()
        self.assertEqual((code, lines[0]), (0, "guarded 2"))
        rows = {line.split()[0]: line.split()[1:] for line in lines[1:]}
        self.assertEqual(rows["pesystem"], ["sda35", "rw"])
        self.assertEqual(rows["boot"], ["sde49", "ro"])
        self.assertEqual(sum(mode == "rw" for _, mode in rows.values()), 4)

    def test_pe_only_table_is_guarded(self):
        self.sysfs(table(PE_ONLY))
        self.assertEqual(self.run_guard()[1][0], "guarded 1")

    def test_official_table_makes_all_ufs_read_only(self):
        disks = table(official=True)
        self.sysfs(disks, extra=[("loop0", None), ("ram0", None)])
        code, lines = self.run_guard()
        self.assertEqual((code, lines[0]), (0, "read-only luns-complete"))
        self.assertEqual(sorted(line.split()[0] for line in lines[1:]), self.ufs_nodes(disks))
        self.assertTrue(all(line.endswith(" ro") for line in lines[1:]))

    def test_unknown_tables_are_read_only(self):
        mixed = {**RECLAIM, "pesystem": PE_ONLY["pesystem"]}
        moved = {**RECLAIM, "pesystem": ("sda39", 0x64000, 0x100000)}
        short = {**RECLAIM, "peuserdata": ("sda37", 0x1d4000, 0x3982000)}
        for label, disks, extra in (("mixed layouts", table(mixed), ()), ("moved node", table(moved), ()),
                                    ("short extent", table(short), ()),
                                    ("second pesystem off UFS", table(), [("loop1", "pesystem")])):
            with self.subTest(label):
                shutil.rmtree(self.root / "sys", ignore_errors=True)
                self.sysfs(disks, extra)
                self.assertEqual(self.run_guard()[1][0], "read-only luns-complete")

    def test_missing_lun_is_reported(self):
        disks = table(official=True)
        del disks["sdf"]
        self.sysfs(disks)
        self.assertEqual(self.run_guard()[1][0], "read-only luns-missing")

    def test_malformed_sysfs_stops(self):
        self.sysfs(table())
        (self.root / UFS / "0:0:0:0/block/sda/sda35/uevent").write_text("DEVTYPE=partition\nPARTNAME=pesystem\n")
        self.assertEqual(self.run_guard()[0], 2)


if __name__ == "__main__":
    unittest.main()
