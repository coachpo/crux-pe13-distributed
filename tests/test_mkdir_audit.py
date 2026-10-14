import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


module_spec = importlib.util.spec_from_file_location("mkdir_audit", Path(__file__).parents[1] / "scripts/mkdir-audit.py")
mkdir_audit = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(mkdir_audit)

MOUNT_POINTS = ("debug_ramdisk", "dev", "metadata", "mnt", "proc", "second_stage_resources", "sys")


class MkdirAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.out = self.root / "out"
        self.out.mkdir()
        self.ramdisk = self.out / "target/product/crux/ramdisk"
        self.mkdir = "mkdir -p " + " ".join(str(self.ramdisk / name) for name in MOUNT_POINTS)
        self.command = '/bin/bash -c "(%s ) && (/host/bin/mkbootfs -d %s %s | /host/bin/minigzip > %s/ramdisk.img )"' % (
            self.mkdir, self.out / "system", self.ramdisk, self.out / "target/product/crux")

    def manifest(self, declared=(), edge_output="target/product/crux/ramdisk.img"):
        return {"out_root": str(self.out),
                "outputs": [str(self.out / edge_output), str(self.ramdisk / "init")],
                "declared_side_output_dirs": [str(self.out / value) for value in declared],
                "edges": [{"outputs": [str(self.out / edge_output)], "command": self.command}]}

    def audit(self, **kwargs):
        path = self.root / "manifest.json"
        path.write_text(json.dumps(self.manifest(**kwargs)))
        return mkdir_audit.audit(str(path))

    def test_created_and_packed_directory_without_declaration_is_reported(self):
        out_root, unowned = self.audit()
        self.assertEqual(out_root, str(self.out))
        self.assertEqual(sorted(unowned), sorted(str(self.ramdisk / name) for name in MOUNT_POINTS))

    def test_declared_staging_tree_is_owned(self):
        _, unowned = self.audit(declared=("target/product/crux/ramdisk",))
        self.assertEqual(unowned, {})

    def test_directory_that_no_packer_reads_is_not_reported(self):
        path = self.root / "manifest.json"
        path.write_text(json.dumps({"out_root": str(self.out), "outputs": [str(self.out / "obj/lib.so")],
                                    "edges": [{"outputs": [str(self.out / "obj/lib.so")],
                                               "command": "mkdir -p %s/classes && /host/bin/clang -o %s/classes/a.o" % (
                                                   self.out / "obj", self.out / "obj")}]}))
        _, unowned = mkdir_audit.audit(str(path))
        self.assertEqual(unowned, {})

    def test_literal_paths_only_ignore_shell_expansion(self):
        self.assertEqual(mkdir_audit.created_directories('mkdir -p "$OUT/dev" ${OUT}/proc /tmp/plain'),
                         {"/tmp/plain"})


if __name__ == "__main__":
    unittest.main()
