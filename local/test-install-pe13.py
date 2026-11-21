#!/usr/bin/env python3
"""Exercise the protected installer gate with a local TWRP-shaped shell fixture."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("install_pe13", Path(__file__).with_name("install-pe13.py"))
install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install)
PROFILE = "a" * 64


class ProtectedPreflightTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = root = Path(temp.name).resolve()
        self.write("crux-ram-twrp-guard.ready", PROFILE + "\n")
        self.write("proc/17/status", "CapBnd:\t00000000000000cb\n")
        self.write("proc/17/mountinfo", "1 0 0:1 / / rw - rootfs rootfs rw\n")
        self.write("proc/self/mountinfo", "1 0 0:1 / / rw - rootfs rootfs rw\n")
        self.write("proc/cmdline", "rootwait androidboot.serialno=8bd37df4\n")
        self.write("sys/class/block/sda/queue/logical_block_size", "4096\n")
        for tool in ("mke2fs", "e2fsck", "tune2fs", "recovery"):
            self.write("system/bin/" + tool, "unexpected tool execution\n").chmod(0o755)
        (root / "proc/17/exe").symlink_to(root / "system/bin/recovery")
        for name, node in install.PROTECTED.items():
            self.partition(name, node, readonly=1)
        for name, info in install.PARTITIONS.items():
            node = info["node"]
            self.partition(name, node, readonly=0)
            self.write(f"sys/class/block/{node}/start", str(install.PE_STARTS[name]))
            self.write(f"state/{name}.size", str(info["bytes"]))
        self.adb = self.write("fake-adb", """#!/usr/bin/env python3
from pathlib import Path
import subprocess
import sys
root = Path(__file__).parent
with (root / "adb.calls").open("a") as log:
    log.write(" ".join(sys.argv[1:4]) + "\\n")
assert sys.argv[1:4] == ["-s", "8bd37df4", "exec-out"]
script = sys.argv[4]
for prefix in ("/dev/block/", "/sys/class/block/", "/proc/",
               "/system/", "/etc/mke2fs.conf", "/crux-ram-twrp-guard.ready"):
    script = script.replace(prefix, str(root) + prefix)
# The regular fixture files represent block special nodes.
script = script.replace("test -b ", "test -f ")
stub = '''
id() { echo 0; }
getprop() { case "$1" in ro.product.device) echo crux;; ro.twrp.version) echo 3.7.1_12;; *) return 2;; esac; }
uname() { echo 4.14.305-test-e45a24f31ee0; }
pidof() { [ "$1" = recovery ] && echo 17; }
blockdev() {
    name=${2##*/}
    case "$1" in
        --getro) cat "$fixture/state/$name.ro";;
        --getsize64) cat "$fixture/state/$name.size";;
        *) return 23;;
    esac
}
'''
sys.exit(subprocess.call(["/bin/sh", "-c", "fixture=$1\\n" + stub + script, "fixture", str(root)]))
""")
        self.adb.chmod(0o755)

    def write(self, rel, content):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def partition(self, name, node, readonly):
        disk = self.write("dev/block/" + node, "fixture")
        by_name = self.root / "dev/block/by-name"
        by_name.mkdir(exist_ok=True)
        (by_name / name).symlink_to(disk)
        partition = self.root / "sys/class/block" / node
        self.write(f"sys/class/block/{node}/dev", f"8:{int(node[3:])}\n")
        (partition / "holders").mkdir()
        self.write(f"state/{name}.ro", str(readonly))

    def preflight(self, output="receipt", profile=PROFILE):
        out = self.root / output
        with contextlib.redirect_stdout(io.StringIO()):
            result = install.main(["--preflight-only", "--ram-twrp-id", profile,
                                   "--out", str(out), "--adb", str(self.adb)])
        return result, json.loads((out / "result.json").read_text())

    def test_readonly_receipt_does_not_authorize_or_attempt_writes(self):
        code, result = self.preflight()
        self.assertEqual(code, 0, (self.root / "receipt/preflight.stdout.txt").read_text()
                         + (self.root / "receipt/preflight.stderr.txt").read_text())
        self.assertEqual(result["status"], "protected_ram_twrp_preflight_verified")
        self.assertFalse(result["discard_pe_data_authorized"])
        self.assertEqual(result["device_writes_attempted"], [])
        self.assertEqual((self.root / "adb.calls").read_text(), "-s 8bd37df4 exec-out\n")

    def test_rejects_wrong_profile(self):
        code, result = self.preflight(profile="b" * 64)
        self.assertEqual((code, result["status"], result["device_writes_attempted"]), (1, "failed", []))

    def test_rejects_unsafe_storage_or_runtime(self):
        changes = (
            ("state/misc.ro", "0"),
            ("state/peuserdata.ro", "1"),
            ("sys/class/block/sda36/start", "0"),
            ("proc/17/status", "CapBnd:\t00000000002000cb\n"),
            ("proc/17/mountinfo", "2 1 8:34 / /data rw - ext4 /dev/block/sda34 rw\n"),
        )
        for index, (file, value) in enumerate(changes):
            with self.subTest(file=file):
                old = (self.root / file).read_text()
                self.write(file, value)
                try:
                    code, result = self.preflight(output=f"receipt-{index}")
                    self.assertEqual((code, result["status"], result["device_writes_attempted"]), (1, "failed", []))
                finally:
                    self.write(file, old)

    def test_discard_requires_protected_profile(self):
        out = self.root / "unprotected"
        argv = ["--discard-pe-data", "--system", str(self.root / "system.raw.img"),
                "--system-sha256", "0" * 64, "--vendor", str(self.root / "vendor.raw.img"),
                "--vendor-sha256", "0" * 64, "--out", str(out), "--adb", str(self.adb)]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            install.main(argv)
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(out.exists())
        self.assertFalse((self.root / "adb.calls").exists())


if __name__ == "__main__":
    unittest.main()
