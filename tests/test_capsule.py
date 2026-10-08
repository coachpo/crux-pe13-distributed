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

    def test_two_hop_python_link_restores_logical_script_and_sibling_import(self):
        target = self.source / "build/make/tools/fs_config/fs_config_generator.py"
        self.write(target, "import sibling\nprint(sibling.VALUE)\n", 0o755)
        self.write(target.parent / "sibling.py", "VALUE = 42\n")
        (self.source / "build/tools").symlink_to("make/tools", target_is_directory=True)
        logical = self.source / "bionic/libc/fs_config_generator.py"
        logical.parent.mkdir(parents=True)
        logical.symlink_to("../../build/tools/fs_config/fs_config_generator.py")
        self.manifest["leaf_inputs"].append(str(logical))
        _, _, archive = self.collect()
        self.assertTrue(archive.getmember(str(self.source / "build/tools").lstrip("/")).issym())
        self.assertIn(str(target.parent / "sibling.py").lstrip("/"), archive.getnames())
        shutil.rmtree(self.source)
        archive.extractall("/")
        result = subprocess.run([shutil.which("python3"), str(logical)], check=True,
                                stdout=subprocess.PIPE, text=True)
        self.assertEqual(result.stdout.strip(), "42")

    def test_go_test_runner_restores_binary_and_ancestor_runtime_fixtures(self):
        package = self.source / "project/internal/zip"
        self.write(package / "reader_test.go", "package zip\n")
        self.write(package / "testdata/archive.zip", b"PK\x03\x04binary zip fixture")
        self.write(self.source / "project/testdata/reference.png", b"\x89PNGfixture")
        self.write(self.source / "project/.git", "gitdir: metadata\n")
        script = package / "check.py"
        self.write(script, "from pathlib import Path\nassert Path('testdata/archive.zip').read_bytes().startswith(b'PK')\nassert Path('../../testdata/reference.png').read_bytes().startswith(b'\\x89PNG')\nprint('PASS')\n")
        self.manifest["commands"] = [str(self.out / "bin/gotestrunner") + " -p project/internal/zip -f "
                                      + str(self.out / "test.passed") + " -- " + str(self.out / "test") + " -test.short"]
        _, _, archive = self.collect()
        shutil.rmtree(self.source)
        archive.extractall("/")
        result = subprocess.run([shutil.which("python3"), str(script)], cwd=package, check=True,
                                stdout=subprocess.PIPE, text=True)
        self.assertEqual(result.stdout.strip(), "PASS")

    def test_used_toolchain_and_python_siblings_are_available(self):
        used = self.source / "prebuilts/clang/host/linux-x86/clang-r1"
        unused = self.source / "prebuilts/clang/host/linux-x86/clang-r2"
        self.write(used / "bin/clang", "compiler", 0o755)
        self.write(used / "lib64/clang/14/lib/linux/libclang_rt.a", "runtime")
        self.write(unused / "bin/clang", "other compiler", 0o755)
        self.write(self.source / "build/tools/tool.py", "import sibling\n", 0o755)
        self.write(self.source / "build/tools/sibling.py", "VALUE = 3\n")
        go = self.source / "prebuilts/go/linux-x86"
        self.write(go / "pkg/tool/linux_amd64/compile", "go compiler", 0o755)
        self.write(go / "pkg/linux_amd64/runtime.a", "go runtime")
        self.manifest["commands"] = ["prebuilts/clang/host/linux-x86/clang-r1/bin/clang -c lib/unit.c -o " + str(self.out / "unit.o"),
                                      "python3 build/tools/tool.py", "prebuilts/go/linux-x86/pkg/tool/linux_amd64/compile lib/unit.c"]
        _, _, archive = self.collect()
        names = archive.getnames()
        self.assertIn(str(used / "lib64/clang/14/lib/linux/libclang_rt.a").lstrip("/"), names)
        self.assertNotIn(str(unused / "bin/clang").lstrip("/"), names)
        self.assertIn(str(self.source / "build/tools/sibling.py").lstrip("/"), names)
        self.assertIn(str(go / "pkg/linux_amd64/runtime.a").lstrip("/"), names)

    def test_response_include_dirs_and_observed_depfile_inputs_are_collected(self):
        self.write(self.source / "include space/hidden.hpp", "#define HIDDEN 1\n")
        self.write(self.source / "other/header with space.h", "#define SPACE 1\n")
        self.write(self.out / "unit.d", str(self.out / "unit.o") + ": lib/unit.c other/header\\ with\\ space.h\n")
        self.manifest["edges"] = [{"rspfile_content": '-I"include space"', "depfile": str(self.out / "unit.d")}]
        _, _, archive = self.collect()
        self.assertIn(str(self.source / "include space/hidden.hpp").lstrip("/"), archive.getnames())
        self.assertIn(str(self.source / "other/header with space.h").lstrip("/"), archive.getnames())

    def test_compiler_depfile_output_is_observed_without_becoming_a_seed(self):
        depfile = self.out / "unit.o.d"
        self.write(depfile, str(self.out / "unit.o") + ": lib/unit.c lib/local.h\n")
        self.manifest["depfiles"] = [str(depfile)]
        self.manifest["commands"] = ["cc -MF " + str(depfile) + " -c lib/unit.c -o " + str(self.out / "unit.o")]
        _, _, archive = self.collect()
        self.assertNotIn(str(depfile).lstrip("/"), archive.getnames())
        self.assertIn(str(self.source / "lib/local.h").lstrip("/"), archive.getnames())

    def test_forced_include_basename_uses_declared_include_directory(self):
        self.write(self.source / "include/fenv-access.h", "#pragma STDC FENV_ACCESS ON\n")
        self.manifest["commands"] = ["cc -include fenv-access.h -Iinclude -c lib/unit.c"]
        _, _, archive = self.collect()
        self.assertIn(str(self.source / "include/fenv-access.h").lstrip("/"), archive.getnames())

    def test_optional_header_scan_preserves_broken_source_symlink(self):
        broken = self.source / "include/dne"
        broken.parent.mkdir()
        broken.symlink_to("missing-target")
        self.manifest["commands"] = ["cc -Iinclude -c lib/unit.c"]
        _, _, archive = self.collect()
        member = archive.getmember(str(broken).lstrip("/"))
        self.assertTrue(member.issym())
        self.assertEqual(member.linkname, "missing-target")

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

    def test_host_link_flags_preserve_startup_objects_and_sysroot_libraries(self):
        gcc = self.source / "prebuilts/gcc/linux-x86/host/x86_64-linux-glibc2.17-4.8"
        startup = gcc / "lib/gcc/x86_64-linux/4.8.3"
        sysroot = gcc / "sysroot"
        self.write(startup / "crtbeginS.o", b"\x7fELFstartup object")
        self.write(sysroot / "usr/lib/crti.o", b"\x7fELFinit object")
        self.write(sysroot / "usr/lib/libc.so", "GROUP (libc.so.6 libc_nonshared.a)")
        self.write(sysroot / "usr/lib/libc.so.6", b"\x7fELFlibc")
        runtime = self.source / "prebuilts/custom-runtime"
        self.write(runtime / "libgcc.a", b"!<arch>\nruntime")
        resource = self.source / "prebuilts/custom-resource"
        self.write(resource / "lib/libclang_rt.a", b"!<arch>\nclang runtime")
        self.manifest["commands"] = ["clang++ --gcc-toolchain=" + str(gcc)
                                      + " -B" + str(startup) + " -L " + str(runtime)
                                      + " --sysroot " + str(sysroot) + " -resource-dir=" + str(resource)
                                      + " -shared -o " + str(self.out / "lib.so")]
        _, _, archive = self.collect()
        for path in [startup / "crtbeginS.o", sysroot / "usr/lib/crti.o", sysroot / "usr/lib/libc.so.6",
                     runtime / "libgcc.a", resource / "lib/libclang_rt.a"]:
            self.assertIn(str(path).lstrip("/"), archive.getnames())

    def test_legacy_env_python2_script_requires_frozen_hermetic_interpreter(self):
        runtime = self.source / "prebuilts/build-tools/linux-x86/bin/py2-cmd"
        self.write(runtime, b"\x7fELFhermetic Python2 runtime", 0o755)
        script = self.source / "build/generator.py"
        self.write(script, "#!/usr/bin/env python2\nimport ConfigParser\n", 0o755)
        self.manifest["leaf_inputs"].append(str(script))
        _, metadata, archive = self.collect()
        self.assertEqual(metadata["required_interpreters"], [{"name":"python2","aliases":["python2","python2.7"],
                         "executable":str(runtime),"source_scripts":[str(script)]}])
        self.assertIn(str(runtime).lstrip("/"), archive.getnames())

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
        self.manifest["commands"] = ["PWD=/proc/self/cwd /usr/bin/python3 -c 'pass'"]
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

    @unittest.skipUnless(shutil.which("zstd"), "zstd is required")
    def test_shared_layer_and_task_capsule_restore_verified_complete_inputs(self):
        used = self.source / "prebuilts/clang/host/linux-x86/clang-r1"
        unused = self.source / "prebuilts/clang/host/linux-x86/clang-r2"
        self.write(used / "bin/clang", "compiler", 0o755)
        self.write(used / "lib64/runtime.so", "compiler runtime")
        self.write(unused / "bin/clang", "unused compiler", 0o755)
        self.manifest["commands"] = ["prebuilts/clang/host/linux-x86/clang-r1/bin/clang -c lib/unit.c"]
        manifest_path = self.slice / "manifest.json"
        manifest_path.write_text(json.dumps(self.manifest))
        cache_path = self.base / "input-digests.json"
        layer_archive = self.base / "layer.tar.zst"
        layer_path, layer = capsule.build_layer([self.slice], self.source, layer_archive, self.out,
                                                 digest_cache=cache_path)
        task_archive = self.base / "task.tar.zst"
        _, task = capsule.pack(manifest_path, self.source, task_archive, self.out,
                               digest_cache=cache_path, shared_manifest=layer_path)
        self.assertFalse(any(spec["path"].startswith(str(unused)) for spec in layer["files"]))
        self.assertFalse(any(spec["path"].startswith(str(self.out)) for spec in layer["files"]))
        self.assertEqual(task["shared_layers"][0]["layer_id"], layer["layer_id"])
        compiler_name = str(used / "bin/clang").lstrip("/")
        task_data = subprocess.run(["zstd", "-q", "-dc", str(task_archive)], check=True, stdout=subprocess.PIPE).stdout
        with tarfile.open(fileobj=io.BytesIO(task_data), mode="r:") as archive:
            self.assertNotIn(compiler_name, archive.getnames())
        self.assertIn(str(used / "bin/clang"), [spec["path"] for spec in task["files"]])
        shutil.rmtree(self.source)
        for archive_path in [layer_archive, task_archive]:
            data = subprocess.run(["zstd", "-q", "-dc", str(archive_path)], check=True, stdout=subprocess.PIPE).stdout
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                self.assertTrue(all(member.name.startswith(str(self.base).lstrip("/") + "/") for member in archive))
                archive.extractall("/")
        worker_spec = importlib.util.spec_from_file_location("worker", MODULE.parent / "worker.py")
        worker = importlib.util.module_from_spec(worker_spec)
        worker_spec.loader.exec_module(worker)
        worker.verify_bundle(self.source / ".crux-task/graph/bundle.json",
                             self.source / ".crux-task/graph/manifest.json")
        self.assertEqual((used / "lib64/runtime.so").read_text(), "compiler runtime")

    @unittest.skipUnless(shutil.which("zstd"), "zstd is required")
    def test_task_rejects_source_changed_after_shared_layer_freeze(self):
        manifest_path = self.slice / "manifest.json"
        manifest_path.write_text(json.dumps(self.manifest))
        layer_path, _ = capsule.build_layer([self.slice], self.source, self.base / "layer.tar.zst", self.out)
        self.write(self.source / "prebuilts/build-tools/linux-x86/bin/ninja", "different executable", 0o755)
        with self.assertRaisesRegex(capsule.CapsuleError, "differs from frozen shared source layer"):
            capsule.pack(manifest_path, self.source, self.base / "task.tar.zst", self.out,
                          shared_manifest=layer_path)


if __name__ == "__main__":
    unittest.main()
