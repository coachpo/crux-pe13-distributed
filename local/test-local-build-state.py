"""Host fixtures for cache independence and absolute Ninja path preservation."""
import importlib.util
import json
import os
import socket
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("local_state", Path(__file__).with_name("local-build-state.py"))
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_new_cache_is_resumable_but_existing_cache_is_not_replaced(self):
        cache = self.root / "cache"
        _, logical = state.initialize(cache)
        self.assertEqual(state.read(cache), (cache, logical))
        (logical / "object").write_text("keep")
        with self.assertRaises(FileExistsError):
            state.initialize(cache)
        self.assertEqual((logical / "object").read_text(), "keep")

    def test_cold_cache_builds_at_an_empty_neutral_mount_point(self):
        mount = self.root / "srv/android-out"
        mount.mkdir(parents=True)
        cache, logical = state.initialize(self.root / "cache", logical_out=mount)
        self.assertEqual((cache, mount), (cache, logical))
        self.assertEqual(state.read(cache), (cache, mount))
        self.assertTrue((cache / "android-out").is_dir())
        (mount / "stale").write_text("x")
        with self.assertRaisesRegex(ValueError, "empty mount point"):
            state.initialize(self.root / "other", logical_out=mount)
        self.assertFalse((self.root / "other").exists())

    def test_legacy_acceptance_output_cannot_be_resumed(self):
        legacy = self.root / "accepted"
        (legacy / "android-out").mkdir(parents=True)
        with self.assertRaises(FileNotFoundError):
            state.read(legacy)

    def test_nested_seed_is_rejected_before_creating_cache(self):
        seed = self.root / "seed"
        (seed / "android-out").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "non-nested"):
            state.initialize(seed / "cache", seed)
        self.assertFalse((seed / "cache").exists())

    def test_failed_reflink_does_not_create_resumable_metadata(self):
        seed = self.root / "seed"
        (seed / "android-out").mkdir(parents=True)
        cache = self.root / "cache"
        with mock.patch.object(state.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "cp")):
            with self.assertRaises(subprocess.CalledProcessError):
                state.initialize(cache, seed)
        self.assertFalse((cache / state.NAME).exists())

    def test_identity_refresh_repairs_seed_but_preserves_same_build_and_objects(self):
        cache, out = state.initialize(self.root / "cache")
        (out / "soong").mkdir()
        (out / "soong/build_number.txt").write_text("2026110901")
        (out / "build_date.txt").write_text("12345")
        (out / "object.o").write_text("compiled")
        for relative in state.IDENTITY_OUTPUTS:
            path = out / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stale 2026110803")
        record = self.root / "refresh.json"
        self.assertEqual(state.prepare_build(cache, "2026110901", "99999", record), "12345")
        self.assertEqual(json.loads(record.read_text())["invalidated"], list(state.IDENTITY_OUTPUTS))
        self.assertEqual((out / "object.o").read_text(), "compiled")
        for relative, prefix in (("target/product/crux/system/build.prop", "ro.build.version.incremental="),
                                 ("target/product/crux/vendor/build.prop", "ro.vendor.build.version.incremental=")):
            path = out / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(prefix + "2026110901\n")
        state.complete_build(cache, "2026110901")
        self.assertEqual(state.prepare_build(cache, "2026110901", "99999", record), "12345")
        self.assertEqual(json.loads(record.read_text())["invalidated"], [])
        self.assertTrue((out / "target/product/crux/vendor/build.prop").exists())
        self.assertEqual(state.prepare_build(cache, "2026110902", "99999"), "99999")

    def test_wrong_built_identity_cannot_mark_cache_successful(self):
        cache, out = state.initialize(self.root / "cache")
        prop = out / "target/product/crux/system/build.prop"
        prop.parent.mkdir(parents=True)
        prop.write_text("ro.build.version.incremental=old\n")
        with self.assertRaisesRegex(ValueError, "built properties"):
            state.complete_build(cache, "2026110901")
        self.assertFalse((cache / state.SUCCESS).exists())

    @unittest.skipUnless(sys.platform == "linux", "GNU reflink and private mount fixture runs in cruxbuild")
    def test_clone_and_private_mapping_preserve_original_backing_files(self):
        seed = self.root / "accepted"
        original = seed / "android-out"
        original.mkdir(parents=True)
        (original / "object").write_text("accepted")
        # Generated tools can have absolute symlinks into OUT.
        (original / "absolute-link").symlink_to(original / "object")
        os.mkfifo(original / "ninja-fifo")
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(original / "interposer.sock"))
        cache = self.root / "cache"
        _, logical = state.initialize(cache, seed)
        physical = cache / "android-out"
        self.assertEqual(logical, original)
        self.assertNotEqual((physical / "object").stat().st_ino, (original / "object").stat().st_ino)
        self.assertEqual(state.run(cache, [sys.executable, "-c",
            "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
            "assert (p/'absolute-link').read_text()=='accepted'; "
            "(p/'absolute-link').write_text('incremental')", str(logical)]), 0)
        self.assertEqual((physical / "object").read_text(), "incremental")
        self.assertEqual((original / "object").read_text(), "accepted")
        # Seeding from another working cache preserves the same logical paths.
        child = self.root / "child"
        self.assertEqual(state.initialize(child, cache)[1], logical)
        self.assertEqual((child / "android-out/object").read_text(), "incremental")

    @unittest.skipUnless(sys.platform == "linux", "private mount and Ninja fixture runs in cruxbuild")
    def test_ninja_resumes_at_logical_path_and_rebuilds_only_changed_input(self):
        ninja = os.environ.get("NINJA") or shutil.which("ninja")
        self.assertIsNotNone(ninja, "Ninja required for incremental fixture")
        seed = self.root / "accepted"
        logical = seed / "android-out"
        logical.mkdir(parents=True)
        for name in ("a", "b"):
            (self.root / name).write_text(name + " original")
        graph = logical / "build.ninja"
        graph.write_text("rule copy\n  command = cp -p $in $out\n" + "".join(
            f"build {logical / (name + '.o')}: copy {self.root / name}\n" for name in ("a", "b")))
        command = [ninja, "-C", str(logical)]
        subprocess.run(command, check=True, capture_output=True)
        cache = self.root / "cache"
        state.initialize(cache, seed)
        log = self.root / "ninja.log"
        capture = [sys.executable, "-c", "import subprocess,sys; "
                   "r=subprocess.run(sys.argv[2:],capture_output=True,text=True); "
                   "open(sys.argv[1],'w').write(r.stdout+r.stderr); sys.exit(r.returncode)", str(log), *command]
        self.assertEqual(state.run(cache, capture), 0)
        self.assertIn("no work to do", log.read_text())
        physical = cache / "android-out"
        b_time = (physical / "b.o").stat().st_mtime_ns
        source = self.root / "a"
        stamp = source.stat().st_mtime_ns + 2_000_000_000
        source.write_text("a changed")
        os.utime(source, ns=(stamp, stamp))
        self.assertEqual(state.run(cache, capture), 0)
        self.assertIn("[1/1]", log.read_text())
        self.assertEqual((physical / "a.o").read_text(), "a changed")
        self.assertEqual((physical / "b.o").stat().st_mtime_ns, b_time)
        self.assertEqual((logical / "a.o").read_text(), "a original")


if __name__ == "__main__":
    unittest.main()
