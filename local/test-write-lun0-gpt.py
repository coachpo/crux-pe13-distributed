"""Host-only fixtures for the U-Boot fastboot GPT write plan, on a synthetic official LUN0 table."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


write = load("write_lun0_gpt", "write-lun0-gpt.py")
fixtures = load("test_lun0_gpt", "test-lun0-gpt.py")
B = write.BLOCK


class WritePlan(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="crux-gpt-plan-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=True))
        self.head, self.tail = fixtures.captures(fixtures.official_entries())
        (self.tmp / "head.bin").write_bytes(self.head)
        (self.tmp / "tail.bin").write_bytes(self.tail)
        subprocess.run([sys.executable, "-B", str(HERE / "lun0-gpt.py"), "--head", str(self.tmp / "head.bin"),
                        "--tail", str(self.tmp / "tail.bin"), "--out", str(self.tmp / "gpt")],
                       check=True, capture_output=True)
        self.layout = json.loads((self.tmp / "gpt/layout.json").read_text())
        self.new_head = (self.tmp / "gpt/sda-head-8x4k.bin").read_bytes()
        self.new_tail = (self.tmp / "gpt/sda-tail-6x4k.bin").read_bytes()

    def plan(self, **override):
        args = dict(src_head=self.head, src_tail=self.tail, layout=self.layout,
                    new_head=self.new_head, new_tail=self.new_tail, work=self.tmp / "work")
        args.update(override)
        return write.plan(**args)

    def test_official_table_plan(self):
        steps = self.plan()
        tail = int(self.layout["tail_first_lba"], 16)
        self.assertEqual(write.runs(self.layout["writes"]), [[0x1, 3], [tail + 1, 2], [tail + 5, 1]])
        commands = [" ".join(step) for step in steps]
        self.assertEqual(commands[0], "oem run:scsi dev 0")
        # Both live captures are compared before the first write.
        first_write = next(i for i, c in enumerate(commands) if "scsi write" in c)
        self.assertEqual(sum("cmp.l" in c for c in commands[:first_write]), 2)
        self.assertIn(f"oem run:scsi read 0xc4000000 {tail:#x} 0x6", commands)
        writes = [c for c in commands if "scsi write" in c]
        self.assertEqual(writes, ["oem run:scsi write 0xc0000000 0x1 0x3",
                                  f"oem run:scsi write 0xc0000000 {tail + 1:#x} 0x2",
                                  f"oem run:scsi write 0xc0000000 {tail + 5:#x} 0x1"])
        self.assertEqual(sum("cmp.l" in c for c in commands[first_write:]), 3)
        self.assertTrue(all(len(" ".join(step)) <= 64 for step in steps if step[0] == "oem"))
        # Each staged write run holds the derived blocks.
        staged = [Path(step[1]) for step in steps if step[0] == "stage" and "write-" in step[1]]
        self.assertEqual((self.tmp / "work/write-0x1-3.bin").read_bytes(), self.new_head[B:4 * B])
        self.assertEqual(Path(staged[2]).read_bytes(), self.new_tail[5 * B:6 * B])

    def test_refuses_mismatched_inputs(self):
        other_head, other_tail = fixtures.captures(fixtures.official_entries())
        with self.assertRaises(SystemExit):
            self.plan(src_head=other_head, src_tail=other_tail)
        with self.assertRaises(SystemExit):
            self.plan(new_head=self.new_head[:B] + bytes(B) + self.new_head[2 * B:], work=self.tmp / "work2")
        bad = json.loads(json.dumps(self.layout))
        bad["writes"][0]["sha256"] = "0" * 64
        with self.assertRaises(SystemExit):
            self.plan(layout=bad, work=self.tmp / "work3")


if __name__ == "__main__":
    unittest.main()
