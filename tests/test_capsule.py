import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "capsule.py"
SPEC = importlib.util.spec_from_file_location("capsule", MODULE)
capsule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capsule)


class CapsuleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        self.out = self.base / "out"
        self.slice = self.base / "slice"
        self.slice.mkdir()
        self.source.mkdir()
        self.out.mkdir()
        self.write(self.source / "prebuilts/build-tools/linux-x86/bin/ninja", "ninja", 0o755)
        self.write(self.source / "lib/unit.c", '#include "local.h"\nint value = LOCAL;\n')
        self.write(self.source / "lib/local.h", "#define LOCAL 7\n")
        self.write(self.slice / "build.ninja", "rule cc\n  command = cc -c $in -o $out\n")
        self.manifest = {"schema_version": 1, "source_root": str(self.source),
                         "runtime_dir": ".crux-task/graph", "ninja": "build.ninja",
                         "targets": [str(self.out / "unit.o")],
                         "outputs": [str(self.out / "unit.o")],
                         "external_inputs": [], "leaf_inputs": ["lib/unit.c"],
                         "commands": [], "edges": []}

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, content, mode=0o644):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        path.chmod(mode)

    def collect(self):
        (self.slice / "manifest.json").write_text(json.dumps(self.manifest))
        collector = capsule.Collector(self.manifest, self.source, self.out)
        task_path = collector.collect(self.slice)
        metadata = collector.metadata(self.slice / "manifest.json", task_path)
        stream = io.BytesIO()
        collector.archive(stream, metadata, task_path)
        stream.seek(0)
        return collector, metadata, tarfile.open(fileobj=stream, mode="r:")

    def test_cold_capsule_contains_hidden_headers_and_executable_modes(self):
        self.write(self.out / "unit.o", b"\x7fELFstale-object")
        _, metadata, archive = self.collect()
        names = archive.getnames()
        self.assertIn(str(self.source / "lib/local.h").lstrip("/"), names)
        self.assertNotIn(str(self.out / "unit.o").lstrip("/"), names)
        self.assertEqual(archive.getmember(str(self.source / "prebuilts/build-tools/linux-x86/bin/ninja").lstrip("/")).mode, 0o755)
        self.assertEqual(metadata["slice_path"], str(self.source / ".crux-task/graph"))
        self.assertIn(str(self.source / ".crux-task/graph/manifest.json").lstrip("/"), names)

    def test_compiled_out_leaf_fails_instead_of_seeding_previous_build(self):
        stale = self.out / "libupstream.a"
        self.write(stale, b"!<arch>\nold")
        self.manifest["leaf_inputs"].append(str(stale))
        self.manifest["allowed_generated_inputs"] = [{"path": str(stale), "reason": "metadata"}]
        with self.assertRaisesRegex(capsule.CapsuleError, "Compiled OUT input has no selected producer"):
            self.collect()

    def test_out_metadata_needs_explicit_reason_and_is_recorded(self):
        generated = self.out / "soong/soong.variables"
        self.write(generated, '{"DeviceName":"crux"}\n')
        self.manifest["leaf_inputs"].append(str(generated))
        with self.assertRaisesRegex(capsule.CapsuleError, "explicit metadata approval"):
            self.collect()
        self.manifest["allowed_generated_inputs"] = [{"path": str(generated), "reason": "frozen Soong configuration"}]
        _, metadata, archive = self.collect()
        self.assertEqual(metadata["allowed_generated_inputs"], self.manifest["allowed_generated_inputs"])
        self.assertIn(str(generated).lstrip("/"), archive.getnames())

    def test_source_symlink_and_target_are_preserved(self):
        external = self.base / "kernel"
        self.write(external / "include/kernel.h", "#define KERNEL 1\n")
        (self.source / "kernel").symlink_to(external, target_is_directory=True)
        self.manifest["leaf_inputs"].append("kernel/include/kernel.h")
        _, metadata, archive = self.collect()
        link = archive.getmember(str(self.source / "kernel").lstrip("/"))
        self.assertTrue(link.issym())
        self.assertEqual(link.linkname, str(external))
        self.assertIn(str(external / "include/kernel.h").lstrip("/"), archive.getnames())
        self.assertNotIn(str(self.source / "kernel/include/kernel.h").lstrip("/"), archive.getnames())

    def test_used_toolchain_and_python_siblings_are_available(self):
        used = self.source / "prebuilts/clang/host/linux-x86/clang-r1"
        unused = self.source / "prebuilts/clang/host/linux-x86/clang-r2"
        self.write(used / "bin/clang", "compiler", 0o755)
        self.write(used / "lib64/clang/14/lib/linux/libclang_rt.a", "runtime")
        self.write(unused / "bin/clang", "other compiler", 0o755)
        self.write(self.source / "build/tools/tool.py", "import sibling\n", 0o755)
        self.write(self.source / "build/tools/sibling.py", "VALUE = 3\n")
        self.manifest["commands"] = ["prebuilts/clang/host/linux-x86/clang-r1/bin/clang -c lib/unit.c -o " + str(self.out / "unit.o"),
                                      "python3 build/tools/tool.py"]
        _, _, archive = self.collect()
        names = archive.getnames()
        self.assertIn(str(used / "lib64/clang/14/lib/linux/libclang_rt.a").lstrip("/"), names)
        self.assertNotIn(str(unused / "bin/clang").lstrip("/"), names)
        self.assertIn(str(self.source / "build/tools/sibling.py").lstrip("/"), names)

    def test_response_include_dirs_and_observed_depfile_inputs_are_collected(self):
        self.write(self.source / "include space/hidden.hpp", "#define HIDDEN 1\n")
        self.write(self.source / "other/header with space.h", "#define SPACE 1\n")
        self.write(self.out / "unit.d", str(self.out / "unit.o") + ": lib/unit.c other/header\\ with\\ space.h\n")
        self.manifest["edges"] = [{"rspfile_content": '-I"include space"', "depfile": str(self.out / "unit.d")}]
        _, _, archive = self.collect()
        self.assertIn(str(self.source / "include space/hidden.hpp").lstrip("/"), archive.getnames())
        self.assertIn(str(self.source / "other/header with space.h").lstrip("/"), archive.getnames())

    def test_nested_shell_command_discovers_toolchains_and_include_options(self):
        jdk = self.source / "prebuilts/jdk/jdk11/linux-x86"
        self.write(jdk / "bin/javac", "javac", 0o755)
        self.write(jdk / "lib/modules", "modules")
        self.write(self.source / "include/system/api.h", "#define API 1\n")
        self.write(self.source / "include/quote/local.h", "#define LOCAL 1\n")
        self.write(self.source / "include/ordinary/plain.h", "#define PLAIN 1\n")
        self.manifest["commands"] = [
            '/bin/bash -c "(prebuilts/jdk/jdk11/linux-x86/bin/javac '
            '-isystem include/system -iquoteinclude/quote -Iinclude/ordinary lib/unit.c) '
            '&& (echo done)"'
        ]
        _, _, archive = self.collect()
        names = archive.getnames()
        self.assertIn(str(jdk / "lib/modules").lstrip("/"), names)
        for header in ["include/system/api.h", "include/quote/local.h", "include/ordinary/plain.h"]:
            self.assertIn(str(self.source / header).lstrip("/"), names)

    def test_make_environment_includes_compiler_runtimes_and_source_tree(self):
        clang = self.source / "prebuilts/clang/host/linux-x86/clang-r1"
        kernel_tools = self.source / "prebuilts/kernel-build-tools/linux-x86"
        self.write(clang / "bin/clang", "compiler", 0o755)
        self.write(clang / "lib64/runtime.so", "runtime")
        self.write(kernel_tools / "bin/pahole", "pahole", 0o755)
        self.write(kernel_tools / "lib64/libdwarves.so", "dwarves")
        self.write(self.source / "prebuilts/tools-custom/common/perl-base/module.pm", "perl")
        self.write(self.source / "prebuilts/build-tools/common/bison/skeletons/yacc.c", "bison")
        self.write(self.source / "kernel/drivers/driver.c", "kernel driver")
        self.manifest["commands"] = [
            'PATH=' + str(clang / "bin") + ':/usr/bin:$PATH '
            'PERL5LIB=' + str(self.source / "prebuilts/tools-custom/common/perl-base") + ' '
            'BISON_PKGDATADIR=' + str(self.source / "prebuilts/build-tools/common/bison") + ' '
            'CC="/usr/bin/ccache clang --cuda-path=/dev/null" '
            'PAHOLE=' + str(kernel_tools / "bin/pahole") + ' make -C kernel O=' + str(self.out / "kernel")
        ]
        _, _, archive = self.collect()
        names = archive.getnames()
        for path in [clang / "lib64/runtime.so", kernel_tools / "lib64/libdwarves.so",
                     self.source / "prebuilts/tools-custom/common/perl-base/module.pm",
                     self.source / "prebuilts/build-tools/common/bison/skeletons/yacc.c",
                     self.source / "kernel/drivers/driver.c"]:
            self.assertIn(str(path).lstrip("/"), names)
        self.assertFalse(any(name.startswith("usr/") for name in names))

    def test_external_produced_objects_are_required_from_receipts(self):
        external = self.out / "upstream.o"
        self.write(external, b"\x7fELFold")
        self.manifest["leaf_inputs"].append(str(external))
        self.manifest["external_inputs"].append(str(external))
        _, metadata, archive = self.collect()
        self.assertEqual(metadata["external_inputs"], [{"path": str(external)}])
        self.assertNotIn(str(external).lstrip("/"), archive.getnames())

    def test_generated_directory_tree_is_built_or_received_instead_of_seeded(self):
        kernel_output = self.out / "obj/KERNEL_OBJ"
        external_output = self.out / "obj/EXTERNAL_OBJ"
        for directory in (kernel_output, external_output):
            self.write(directory / "usr/include/display/header.h", "#define OLD 1\n")
            self.write(directory / "kernel.o", b"\x7fELFold")
            self.manifest["leaf_inputs"].append(str(directory / "usr/include/display/header.h"))
            self.manifest["commands"].append("cc -I" + str(directory / "usr/include") + " -c lib/unit.c")
        self.manifest["outputs"].append(str(kernel_output))
        self.manifest["external_inputs"].append(str(external_output))
        _, metadata, archive = self.collect()
        names = archive.getnames()
        self.assertEqual(metadata["output_dirs"], [str(kernel_output)])
        self.assertEqual(metadata["external_input_dirs"], [str(external_output)])
        for directory in (kernel_output, external_output):
            self.assertFalse(any(name == str(directory).lstrip("/")
                                 or name.startswith(str(directory).lstrip("/") + "/") for name in names))

    def test_system_tools_are_recorded_without_archiving_host_paths(self):
        self.manifest["commands"] = ["/usr/bin/python3 -c 'pass'"]
        _, metadata, archive = self.collect()
        self.assertIn(str(Path("/usr/bin/python3").resolve()), metadata["system_tools"])
        self.assertFalse(any(name.startswith("usr/") or name == "bin" for name in archive.getnames()))

    def test_writefile_payload_is_data_instead_of_an_input_path(self):
        self.manifest["commands"] = ["/bin/bash -c 'echo -e -n \"$0\" > " + str(self.out / "config.json")
                                      + "' '" + "A" * 8192 + "'"]
        _, _, archive = self.collect()
        self.assertIn(str(self.source / "lib/unit.c").lstrip("/"), archive.getnames())

    def test_transfer_digest_cache_reuses_unchanged_input_and_refreshes_changed_bytes(self):
        cache_path = self.base / "digests.json"
        source_path = self.source / "lib/unit.c"
        first = capsule.DigestCache(cache_path)
        original = first(source_path)
        first.save()
        second = capsule.DigestCache(cache_path)
        self.assertEqual(second(source_path), original)
        self.assertEqual((second.computed, second.reused), (0, 1))
        self.write(source_path, "int changed = 22;\n")
        self.assertNotEqual(second(source_path), original)
        self.assertEqual((second.computed, second.reused), (1, 1))

    @unittest.skipUnless(shutil.which("zstd"), "zstd is required")
    def test_pack_emits_compressed_archive_and_transfer_digest(self):
        manifest_path = self.slice / "manifest.json"
        manifest_path.write_text(json.dumps(self.manifest))
        output = self.base / "inputs.tar.zst"
        metadata_path, metadata = capsule.pack(manifest_path, self.source, output, self.out)
        self.assertTrue(metadata_path.is_file())
        self.assertEqual(metadata["archive"]["sha256"], capsule.digest(output))
        compressed = subprocess.run(["zstd", "-q", "-dc", str(output)], check=True, stdout=subprocess.PIPE).stdout
        with tarfile.open(fileobj=io.BytesIO(compressed), mode="r:") as archive:
            self.assertIn(str(self.source / ".crux-task/graph/bundle.json").lstrip("/"), archive.getnames())


if __name__ == "__main__":
    unittest.main()
