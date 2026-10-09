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

    def test_default_ninja_includes_declared_host_runtime_without_other_tool_commands(self):
        runtime = self.source / "prebuilts/build-tools/linux-x86/lib64/libjemalloc5.so"
        self.write(runtime, b"\x7fELFninja runtime")
        self.manifest["commands"] = []
        _, metadata, archive = self.collect()
        self.assertIn(str(runtime).lstrip("/"), archive.getnames())
        specification = next(item for item in metadata["files"] if item["path"] == str(runtime))
        self.assertEqual(specification["sha256"], capsule.digest(runtime))

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

    def test_proto_import_closure_cold_transport_preserves_search_order_and_generated_inputs(self):
        schema = self.source / "schemas/root.proto"
        chosen = self.source / "schemas/shared.data"
        nested = self.source / "schemas/nested/weak.schema"
        generated = self.out / "gen/generated.schema"
        self.write(schema, 'syntax = "proto2";\nimport public "sha" /* joined string */ "red.data";\n'
                           '// import "missing.proto";\n/* import "also-missing.proto"; */\n'
                           'option example = \'import "string-example-missing.proto";\';\n')
        self.write(chosen, 'import weak "nested/weak.schema";\n')
        self.write(nested, 'import "generated.schema";\n')
        self.write(generated, 'old generated bytes must not seed the capsule\n')
        self.write(self.source / "shadow/shared.data", "wrong search path\n")
        self.write(self.source / "unrelated/large.h", "unrelated C++ header\n")
        self.manifest["external_inputs"].append(str(generated))
        self.manifest["commands"] = [str(self.out / "bin/aprotoc") + " --cpp_out=lite:" + str(self.out / "gen")
                                     + " -I schemas -Ishadow --proto_path=" + str(self.out / "gen")
                                     + " schemas/root.proto"]
        _, metadata, archive = self.collect()
        names = archive.getnames()
        for path in [schema, chosen, nested]:
            self.assertIn(str(path).lstrip("/"), names)
        for path in [generated, self.source / "shadow/shared.data", self.source / "unrelated/large.h"]:
            self.assertNotIn(str(path).lstrip("/"), names)
        self.assertIn({"path": str(generated)}, metadata["external_inputs"])
        shutil.rmtree(self.source)
        shutil.rmtree(self.out)
        archive.extractall("/")
        # A producer receipt restores the generated import after source restore.
        self.write(generated, "fresh receipt bytes\n")
        self.assertIn('import public "sha"', schema.read_text())
        self.assertIn('import weak "nested/weak.schema";', chosen.read_text())
        self.assertIn('import "generated.schema";', nested.read_text())
        self.assertEqual(generated.read_text(), "fresh receipt bytes\n")

    def test_proto_virtual_mapping_and_response_preserve_literal_nonproto_import(self):
        self.write(self.source / "disk/root.proto", 'import "v/types.def";\n')
        self.write(self.source / "disk/types.def", 'message Value {}\n')
        response = self.source / "proto.rsp"
        self.write(response, "--proto_path=v=disk\nv/root.proto\n")
        self.manifest["commands"] = [str(self.out / "bin/protoc") + " @proto.rsp"]
        _, _, archive = self.collect()
        for path in [response, self.source / "disk/root.proto", self.source / "disk/types.def"]:
            self.assertIn(str(path).lstrip("/"), archive.getnames())

    def test_proto_response_sources_keep_outer_search_path_and_order(self):
        self.write(self.source / "schemas/root.proto", 'import "nested/types.proto";\n')
        self.write(self.source / "schemas/nested/types.proto", 'message Value {}\n')
        self.write(self.source / "shadow/nested/types.proto", 'import "wrong-missing.proto";\n')
        source_response = self.source / "sources.rsp"
        self.write(source_response, "root.proto")
        generated_response = self.out / "sources.rsp"
        command = str(self.out / "bin/protoc") + " -Ischemas @" + str(generated_response) + " -Ishadow"
        self.manifest["commands"] = [str(self.out / "bin/protoc") + " -Ischemas @sources.rsp -Ishadow"]
        self.manifest["edges"] = [{"command": command, "rspfile": str(generated_response),
                                     "rspfile_content": "root.proto"}]
        _, _, archive = self.collect()
        self.assertIn(str(source_response).lstrip("/"), archive.getnames())
        self.assertNotIn(str(generated_response).lstrip("/"), archive.getnames())
        self.assertNotIn(str(self.source / "shadow/nested/types.proto").lstrip("/"), archive.getnames())
        shutil.rmtree(self.source)
        archive.extractall("/")
        self.assertEqual(source_response.read_text(), "root.proto")
        self.assertEqual((self.source / "schemas/nested/types.proto").read_text(), "message Value {}\n")

    def test_proto_decode_redirection_reads_binary_stdin_and_does_not_seed_stdout(self):
        schema = self.source / "schema.proto"
        stdin = self.source / "input.bin"
        stdout = self.out / "result.txt"
        self.write(schema, "message Value {}\n")
        self.write(stdin, b"\x08\x01\x00binary stdin")
        self.write(stdout, "old stdout must not seed\n")
        self.manifest["commands"] = [str(self.out / "bin/protoc") + " --decode=Value schema.proto < input.bin > " + str(stdout)]
        _, _, archive = self.collect()
        for path in [schema, stdin]:
            self.assertIn(str(path).lstrip("/"), archive.getnames())
        self.assertNotIn(str(stdout).lstrip("/"), archive.getnames())

    def test_proto_response_line_is_one_argument_and_nested_at_is_literal(self):
        root = self.source / "schemas/root schema.proto"
        nested_at = self.source / "schemas/@literal.proto"
        self.write(root, "message Root {}\n")
        self.write(nested_at, "message Literal {}\n")
        self.write(self.source / "sources.rsp", "root schema.proto\n@literal.proto\n")
        self.manifest["commands"] = [str(self.out / "bin/protoc") + " -Ischemas @sources.rsp"]
        _, _, archive = self.collect()
        for path in [root, nested_at]:
            self.assertIn(str(path).lstrip("/"), archive.getnames())

    def test_license_metadata_rsp_strings_are_not_inputs_but_explicit_reads_still_fail(self):
        stale = self.out / "intermediates/compiler.jar"
        header = self.out / "gen/cap_names.h"
        self.write(stale, b"PK\x03\x04old compiled JAR")
        self.write(header, "old generated header\n")
        root = self.source / "project"
        self.write(root / "unread.h", "not read by license metadata\n")
        rsp = self.out / "meta_lic.rsp"
        command = str(self.out / "bin/build_license_metadata") + " -o " + str(self.out / "meta_lic") + " @" + str(rsp)
        self.manifest["edges"] = [{"command": command, "rspfile": str(rsp),
                                     "rspfile_content": "-is_container -r project -s '" + str(stale)
                                     + "' -t '" + str(header) + "' -d '" + str(stale) + ":static'"}]
        _, _, archive = self.collect()
        self.assertIn(str(root).lstrip("/"), archive.getnames())
        for path in [stale, header, root / "unread.h"]:
            self.assertNotIn(str(path).lstrip("/"), archive.getnames())
        self.manifest["commands"].append("java -classpath " + str(stale) + " Main")
        with self.assertRaisesRegex(capsule.CapsuleError, "Compiled OUT input has no selected producer"):
            self.collect()
        self.manifest["commands"] = []
        self.manifest["leaf_inputs"].append(str(stale))
        with self.assertRaisesRegex(capsule.CapsuleError, "Compiled OUT input has no selected producer"):
            self.collect()

    def test_license_metadata_source_response_is_read_and_transported(self):
        response = self.source / "license.rsp"
        self.write(response, "-s @" + str(self.out / "missing.jar") + " -n NOTICE -is_container=true")
        self.manifest["commands"] = [str(self.out / "bin/build_license_metadata") + " @license.rsp"]
        _, _, archive = self.collect()
        self.assertIn(str(response).lstrip("/"), archive.getnames())
        response.unlink()
        with self.assertRaisesRegex(capsule.CapsuleError, "Missing input.*license.rsp"):
            self.collect()

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

    def test_forced_host_header_resolves_sysroot_standard_include_location(self):
        sysroot = self.source / "prebuilts/host-sysroot"
        self.write(sysroot / "usr/include/stdio.h", "int puts(const char *);\n")
        self.manifest["commands"] = ["clang --sysroot " + str(sysroot) + " -include stdio.h -c lib/unit.c"]
        _, _, archive = self.collect()
        self.assertIn(str(sysroot / "usr/include/stdio.h").lstrip("/"), archive.getnames())

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

    def test_env_python_preserves_frozen_native_python2_default(self):
        runtime = self.source / "prebuilts/build-tools/linux-x86/bin/py2-cmd"
        self.write(runtime, b"\x7fELFhermetic Python2 runtime", 0o755)
        wrapper = self.source / "prebuilts/build-tools/path/linux-x86/python"
        wrapper.parent.mkdir(parents=True)
        wrapper.symlink_to("../../linux-x86/bin/py2-cmd")
        script = self.source / "build/hyphen.py"
        self.write(script, "#!/usr/bin/env python\nprint('original runtime')\n", 0o755)
        self.manifest["leaf_inputs"].append(str(script))
        _, metadata, _ = self.collect()
        item = metadata["required_interpreters"][0]
        self.assertEqual(item["aliases"], ["python", "python2", "python2.7"])
        self.assertEqual(item["native_default_wrapper"], str(wrapper))
        self.assertEqual(item["executable"], str(runtime))

    def test_symbolic_link_target_and_output_are_literal_operands(self):
        target=self.base / "outside-source-target"
        target.mkdir()
        destination=self.out / "root/d"
        destination.parent.mkdir()
        destination.symlink_to(target)
        self.manifest["commands"]=["/bin/bash -c 'ln -sfn " + str(target) + " " + str(destination) + "'"]
        _, _, archive=self.collect()
        self.assertNotIn(str(target).lstrip("/"), archive.getnames())
        self.assertNotIn(str(destination).lstrip("/"), archive.getnames())

    def test_rust_emit_transient_depfile_and_codegen_arguments_are_outputs_and_data(self):
        raw=self.out / "lib.rlib.d.raw"
        self.write(raw, "old compiler depfile\n")
        runtime=self.source / "prebuilts/linker-runtime"
        self.write(runtime / "libgcc.a", b"!<arch>\nruntime")
        self.manifest["commands"]=["rustc -C 'link-args=" + " -Wl,--no-undefined"*30
                                      + " -L"+str(runtime)+"' --emit dep-info="+str(raw)
                                      + " lib/unit.c && grep x "+str(raw)]
        _, _, archive=self.collect()
        self.assertNotIn(str(raw).lstrip("/"),archive.getnames())
        self.assertIn(str(runtime / "libgcc.a").lstrip("/"),archive.getnames())

    def test_notice_response_arguments_recreated_in_same_action_are_not_source_seeds(self):
        arguments = self.out / "notice/module.meta_lic/arguments"
        self.write(arguments, "stale response data\n")
        self.manifest["commands"] = ["/bin/bash -c 'rm -f " + str(arguments) + " && touch " + str(arguments)
            + " && echo -n source >> " + str(arguments) + " && build_license_metadata @" + str(arguments) + "'"]
        _, metadata, archive = self.collect()
        self.assertNotIn(str(arguments).lstrip("/"), archive.getnames())
        self.assertEqual(metadata["allowed_generated_inputs"], [])

    def test_action_fresh_write_does_not_hide_prior_reads_append_only_or_explicit_leaves(self):
        arguments = self.out / "arguments"
        self.write(arguments, "original response\n")
        commands = [
            "cat " + str(arguments) + " && rm -f " + str(arguments) + " && touch " + str(arguments)
                + " && echo new >> " + str(arguments),
            "echo appended >> " + str(arguments) + " && reader @" + str(arguments),
            "touch " + str(arguments) + " && echo appended >> " + str(arguments),
            "rm -f " + str(arguments) + " || touch " + str(arguments) + " ; reader " + str(arguments),
            "reader --input=" + str(arguments) + " && echo new > " + str(arguments),
            "reader CONFIG=" + str(arguments) + " && echo new > " + str(arguments),
            "CONFIG=" + str(arguments) + " reader && echo new > " + str(arguments),
        ]
        for command in commands:
            with self.subTest(command=command):
                self.manifest["commands"] = [command]
                with self.assertRaisesRegex(capsule.CapsuleError, "explicit metadata approval"):
                    self.collect()
        self.manifest["commands"] = ["echo new > " + str(arguments) + " && reader @" + str(arguments)]
        self.manifest["leaf_inputs"].append(str(arguments))
        with self.assertRaisesRegex(capsule.CapsuleError, "explicit metadata approval"):
            self.collect()

    def test_truncating_write_recreates_action_local_argument_file(self):
        arguments = self.out / "arguments"
        self.write(arguments, "old data\n")
        self.manifest["commands"] = ["echo new > " + str(arguments) + " && reader " + str(arguments)]
        _, _, archive = self.collect()
        self.assertNotIn(str(arguments).lstrip("/"), archive.getnames())

    def test_generator_depfiles_are_same_action_outputs_with_ordered_real_reads(self):
        depfile = self.out / "generated.d"
        self.write(depfile, "old generated dependency data\n")
        self.write(self.source / "input.aidl", "interface Input {}\n")
        self.write(self.source / "schema.proto", "message Input {}\n")
        for writer in ["aidl -d" + str(depfile) + " input.aidl", "aidl --dep=" + str(depfile) + " input.aidl",
                       "protoc --dependency_out=" + str(depfile) + " schema.proto"]:
            with self.subTest(writer=writer):
                self.manifest["commands"] = [writer + " && dep_fixer " + str(depfile)]
                _, _, archive = self.collect()
                self.assertNotIn(str(depfile).lstrip("/"), archive.getnames())
                self.manifest["commands"] = ["reader --input=" + str(depfile) + " && " + writer]
                with self.assertRaisesRegex(capsule.CapsuleError, "explicit metadata approval"):
                    self.collect()
        self.manifest["commands"] = ["aidl -d" + str(depfile) + " input.aidl", "reader " + str(depfile)]
        with self.assertRaisesRegex(capsule.CapsuleError, "explicit metadata approval"):
            self.collect()

    def test_fresh_aar_and_module_namespaces_skip_stale_children_but_keep_real_inputs(self):
        aar = self.source / "libs/input.aar"
        dependency = self.source / "libs/dependency.jar"
        stage = self.out / "module/aar"
        child = stage / "classes.jar"
        self.write(aar, b"PK\x03\x04source AAR")
        self.write(dependency, b"PK\x03\x04source classpath")
        self.write(child, b"PK\x03\x04stale compiled child")
        self.write(stage / "libs/library.jar", b"PK\x03\x04stale compiled nested child")
        self.manifest["commands"] = ["rm -rf " + str(stage) + " && mkdir -p " + str(stage)
                                     + " && unzip -qo -d " + str(stage) + " " + str(aar)
                                     + " && merge_zips " + str(self.out / "unit.o")
                                     + " $(ls " + str(child) + ") $(ls " + str(stage / "libs/*.jar") + ")"
                                     + " && javac -classpath " + str(dependency) + ":" + str(child) + " lib/unit.c"]
        _, metadata, archive = self.collect()
        for path in [aar, dependency]:
            self.assertIn(str(path).lstrip("/"), archive.getnames())
        self.assertNotIn(str(child).lstrip("/"), archive.getnames())
        self.assertEqual(metadata["allowed_generated_inputs"], [])
        self.manifest["commands"][0] = "java -classpath " + str(child) + " Main && " + self.manifest["commands"][0]
        with self.assertRaisesRegex(capsule.CapsuleError, "Compiled OUT input has no selected producer"):
            self.collect()

    def test_fresh_directory_scope_starts_after_reset_and_stops_at_action_boundary(self):
        stage = self.out / "modules"
        child = stage / "module.jar"
        self.write(child, b"PK\x03\x04stale module")
        prefix = "rm -rf " + str(stage) + " && mkdir -p " + str(stage / "jmod")
        self.manifest["commands"] = [prefix + " && /bin/bash -c 'reader " + str(child) + "' | cat"]
        _, _, archive = self.collect()
        self.assertNotIn(str(child).lstrip("/"), archive.getnames())
        for command in ["/bin/bash -c 'reader " + str(child) + "' && " + prefix,
                        "rm -rf " + str(stage) + " || true ; mkdir -p " + str(stage) + " ; reader " + str(child),
                        "rm -rf " + str(stage) + " | cat ; mkdir -p " + str(stage) + " ; reader " + str(child)]:
            self.manifest["commands"] = [command]
            with self.assertRaisesRegex(capsule.CapsuleError, "Compiled OUT input has no selected producer"):
                self.collect()
        for command in [prefix + " || true ; cat " + str(child), prefix + " ; cat " + str(child),
                        "(rm -rf " + str(stage) + " && mkdir -p " + str(stage) + ") || true ; cat " + str(child)]:
            self.manifest["commands"] = [command]
            with self.assertRaisesRegex(capsule.CapsuleError, "Compiled OUT input has no selected producer"):
                self.collect()
        self.manifest["commands"] = [prefix, "reader " + str(child)]
        with self.assertRaisesRegex(capsule.CapsuleError, "Compiled OUT input has no selected producer"):
            self.collect()
        self.manifest["commands"] = [prefix + " && reader " + str(child)]
        self.manifest["leaf_inputs"].append(str(child))
        with self.assertRaisesRegex(capsule.CapsuleError, "Compiled OUT input has no selected producer"):
            self.collect()

    def test_zip_rsp_keeps_sources_and_scopes_fresh_output_reader(self):
        temporary_zip = self.out / "gensrcs.zip"
        response = self.source / "sources.rsp"
        self.write(temporary_zip, b"PK\x03\x04stale generated zip")
        self.write(self.source / "inputs/src.txt", "original source bytes\n")
        self.write(response, "-C inputs -f inputs/src.txt")
        self.manifest["commands"] = ["soong_zip -o " + str(temporary_zip) + " @sources.rsp && zipsync " + str(temporary_zip)]
        _, _, archive = self.collect()
        for path in [response, self.source / "inputs/src.txt"]:
            self.assertIn(str(path).lstrip("/"), archive.getnames())
        self.assertNotIn(str(temporary_zip).lstrip("/"), archive.getnames())
        self.manifest["commands"] = ["reader --input=" + str(temporary_zip) + " && " + self.manifest["commands"][0]]
        with self.assertRaisesRegex(capsule.CapsuleError, "Compiled OUT input has no selected producer"):
            self.collect()

    def test_xmlnotice_literal_prefixes_keep_positional_metadata_dependencies(self):
        prefix = self.out / "boot.img"
        metadata = self.source / "notice.meta_lic"
        self.write(prefix, b"ANDROID!stale boot image")
        self.write(metadata, "license_kinds: \"SPDX-Apache-2.0\"\n")
        self.manifest["commands"] = ["xmlnotice -strip_prefix=" + str(prefix) + " -product " + str(prefix)
                                     + " -title " + str(prefix) + " " + str(metadata)]
        _, _, archive = self.collect()
        self.assertIn(str(metadata).lstrip("/"), archive.getnames())
        self.assertNotIn(str(prefix).lstrip("/"), archive.getnames())
        self.manifest["leaf_inputs"].append(str(prefix))
        with self.assertRaisesRegex(capsule.CapsuleError, "Compiled OUT input has no selected producer"):
            self.collect()

    def test_sourced_ninja_rsp_inherits_fresh_namespace_without_losing_copy_sources(self):
        stage = self.out / "image.apex"
        generated = stage / "etc/data.txt"
        source = self.source / "payload/data.txt"
        response = self.out / "copy_commands"
        self.write(generated, "stale staging data\n")
        self.write(source, "actual source data\n")
        body = "mkdir -p " + str(stage / "etc") + " && cp " + str(source) + " " + str(generated)
        command = "rm -rf " + str(stage) + " && mkdir -p " + str(stage) + " && (. " + str(response) + ") && apexer " + str(stage)
        self.manifest["edges"] = [{"command": command, "rspfile": str(response), "rspfile_content": body}]
        _, _, archive = self.collect()
        self.assertIn(str(source).lstrip("/"), archive.getnames())
        self.assertNotIn(str(generated).lstrip("/"), archive.getnames())
        self.assertNotIn(str(response).lstrip("/"), archive.getnames())
        self.manifest["commands"] = ["reader " + str(generated) + " && " + command]
        with self.assertRaisesRegex(capsule.CapsuleError, "explicit metadata approval"):
            self.collect()

    def test_first_append_after_successful_removal_creates_fresh_action_file(self):
        info = self.out / "image-info.txt"
        self.write(info, "old image metadata\n")
        command = "rm -f " + str(info) + " && echo first >> " + str(info) + " && producer | awk x && echo second >> " + str(info) + " && build_image " + str(info)
        self.manifest["commands"] = [command]
        _, _, archive = self.collect()
        self.assertNotIn(str(info).lstrip("/"), archive.getnames())
        self.manifest["commands"] = ["reader --input=" + str(info) + " && " + command]
        with self.assertRaisesRegex(capsule.CapsuleError, "explicit metadata approval"):
            self.collect()
        self.manifest["commands"] = ["rm -f " + str(info) + " || true ; echo first >> " + str(info)]
        with self.assertRaisesRegex(capsule.CapsuleError, "explicit metadata approval"):
            self.collect()

    def test_llvm_output_and_kotlin_xml_are_fresh_without_global_path_exemptions(self):
        bitcode = self.out / "lib.bc.unstripped"
        xml = self.out / "kotlin-build.xml"
        source_bitcode = self.source / "payload/input.bc"
        script = self.source / "build/soong/scripts/gen-kotlin-build-file.py"
        self.write(bitcode, b"BC\xc0\xdeold bitcode")
        self.write(xml, "old module XML\n")
        self.write(source_bitcode, b"BC\xc0\xdesource bitcode")
        self.write(script, "print('generator')\n")
        command = "llvm-link -o " + str(bitcode) + " " + str(source_bitcode) + " && bcc_strip_attr " + str(bitcode)
        xml_command = "python3 " + str(script) + " --out " + str(xml) + " && kotlinc -Xbuild-file=" + str(xml)
        self.manifest["commands"] = [command, xml_command]
        _, _, archive = self.collect()
        self.assertIn(str(source_bitcode).lstrip("/"), archive.getnames())
        self.assertNotIn(str(bitcode).lstrip("/"), archive.getnames())
        self.assertNotIn(str(xml).lstrip("/"), archive.getnames())
        self.manifest["commands"].append("reader " + str(xml))
        with self.assertRaisesRegex(capsule.CapsuleError, "explicit metadata approval"):
            self.collect()

    def test_rust_emit_output_is_scoped_to_its_own_action(self):
        depfile = self.out / "lib.rlib.d.raw"
        self.write(depfile, "old dependency data\n")
        self.manifest["commands"] = ["rustc --emit dep-info=" + str(depfile) + " lib/unit.c",
                                     "reader " + str(depfile)]
        with self.assertRaisesRegex(capsule.CapsuleError, "explicit metadata approval"):
            self.collect()
        self.manifest["commands"] = ["clippy-driver --emit dep-info=" + str(depfile) + " lib/unit.c && reader " + str(depfile)]
        _, _, archive = self.collect()
        self.assertNotIn(str(depfile).lstrip("/"), archive.getnames())

    def test_grouped_writers_and_path_string_tools_do_not_read_stale_outputs(self):
        output = self.out / "temporary.bc"
        info = self.out / "image-info.txt"
        self.write(output, b"BC\xc0\xdeold bitcode")
        self.write(info, "old image metadata\n")
        self.manifest["commands"] = ["/bin/bash -c '(mkdir -p $(dirname " + str(output) + ")) && "
                                     + "(llvm-link -o " + str(output) + " lib/unit.c) && (reader " + str(output) + ")'",
                                     "(rm -rf " + str(info) + ") && (echo first >> " + str(info)
                                     + ") && (build_image " + str(info) + ")"]
        _, _, archive = self.collect()
        self.assertNotIn(str(output).lstrip("/"), archive.getnames())
        self.assertNotIn(str(info).lstrip("/"), archive.getnames())

    def test_masked_command_substitution_reset_does_not_create_fresh_namespace(self):
        stage = self.out / "modules"
        child = stage / "module.jar"
        self.write(child, b"PK\x03\x04old compiled content")
        self.manifest["commands"] = ["echo $(rm -rf " + str(stage) + ") && mkdir -p " + str(stage)
                                     + " && reader " + str(child)]
        with self.assertRaisesRegex(capsule.CapsuleError, "Compiled OUT input has no selected producer"):
            self.collect()

    def test_process_substitution_keeps_parent_writer_order_and_actual_input_reads(self):
        stage = self.out / "temporary.bc.unstripped"
        input_file = self.source / "input.txt"
        filters = self.source / "filter-patterns.txt"
        self.write(stage, "old writer bytes\n")
        self.write(input_file, "fresh writer bytes\n")
        self.write(filters, "warning\n")
        self.manifest["leaf_inputs"].append(str(input_file))
        tool = self.source / "bin/llvm-link"
        self.write(tool, '#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\ncat "' + str(input_file)
                   + '" > "$2"\necho warning >&2\n', 0o755)
        command = "(" + str(tool) + " -o " + str(stage) + " 2> >(grep -v -f " + str(filters)
        command += " >&2)) && (cat " + str(stage) + ")"
        self.manifest["commands"] = [command]
        _, _, archive = self.collect()
        for path in [tool, input_file, filters]:
            self.assertIn(str(path).lstrip("/"), archive.getnames())
        self.assertNotIn(str(stage).lstrip("/"), archive.getnames())
        stage.unlink()
        result = subprocess.run(command, shell=True, executable="/bin/bash", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "fresh writer bytes\n")
        old_input = self.out / "actual-input.jar"
        self.write(old_input, b"PK\x03\x04old input jar")
        self.manifest["commands"] = [command.replace("-o " + str(stage), "<(cat " + str(old_input) + ") -o " + str(stage))]
        with self.assertRaisesRegex(capsule.CapsuleError, "Compiled OUT input has no selected producer"):
            self.collect()

    def test_failed_reset_rescued_by_or_or_semicolon_reads_real_old_bytes(self):
        stage = self.out / "modules"
        child = stage / "old.jar"
        data = b"PK\x03\x04old bytes remain after failed reset"
        self.write(child, data)
        remover = self.source / "bin/rm"
        self.write(remover, "#!/bin/sh\nexit 1\n", 0o755)
        for tail in [" || true ; cat ", " ; cat "]:
            command = str(remover) + " -rf " + str(stage) + " && mkdir -p " + str(stage) + tail + str(child)
            result = subprocess.run(command, shell=True, executable="/bin/bash", capture_output=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, data)
            self.manifest["commands"] = [command]
            with self.assertRaisesRegex(capsule.CapsuleError, "Compiled OUT input has no selected producer"):
                self.collect()

    def test_generated_header_scan_excludes_editor_config_and_extensionless_elf(self):
        headers=self.out / "include"
        self.write(headers / ".clang-format", "BasedOnStyle: LLVM\n")
        self.write(headers / "wpa_cli", b"\x7fELFpreviously built executable")
        self.manifest["commands"]=["cc -I"+str(headers)+" -c lib/unit.c"]
        _, _, archive=self.collect()
        self.assertNotIn(str(headers / ".clang-format").lstrip("/"),archive.getnames())
        self.assertNotIn(str(headers / "wpa_cli").lstrip("/"),archive.getnames())

    def test_source_extensionless_binary_is_not_header_but_literal_inputs_remain(self):
        root = self.source / "includes"
        text = root / "vector"
        image = root / "modules"
        macho = root / "emulator"
        self.write(text, "// C++ text header\nnamespace std {}\n")
        self.write(image, bytes.fromhex("dadafeca0000010000000000e5940000") + b"jimage")
        self.write(macho, bytes.fromhex("cffaedfe070000010300000002000000") + b"Mach-O")
        self.write(self.source / "asm/data.s", '.incbin "modules"\n')
        self.manifest["commands"] = ["cc -Iincludes -c lib/unit.c"]
        _, _, archive = self.collect()
        self.assertIn(str(text).lstrip("/"), archive.getnames())
        self.assertNotIn(str(image).lstrip("/"), archive.getnames())
        self.assertNotIn(str(macho).lstrip("/"), archive.getnames())
        self.manifest["commands"].append("clang -Iincludes -c asm/data.s")
        self.manifest["leaf_inputs"].append("asm/data.s")
        _, _, archive = self.collect()
        self.assertIn(str(image).lstrip("/"), archive.getnames())
        self.assertNotIn(str(macho).lstrip("/"), archive.getnames())

    def test_blueprint_glob_captures_runtime_asset_directory(self):
        self.write(self.source / "app/assets/nested/data.bin", b"opaque asset\x00")
        glob_tool=self.out / "soong/bpglob"
        self.manifest["outputs"].append(str(glob_tool))
        self.manifest["commands"]=[str(glob_tool)+" -o "+str(self.out / "assets.glob")
                                    +" -p 'app/assets/**/*' -e .git"]
        _, _, archive=self.collect()
        self.assertIn(str(self.source / "app/assets/nested/data.bin").lstrip("/"),archive.getnames())

    def test_assembler_include_recurses_through_command_search_paths_and_opaque_data(self):
        self.write(self.source / "asm/main.s", '.include "outer.s"\n.incbin "payload.bin"\n')
        self.write(self.source / "macros/outer.s", '.include "inner.S"\n')
        self.write(self.source / "macros/inner.S", '.macro RETURN\n ret\n.endm\n')
        self.write(self.source / "macros/payload.bin", b"opaque assembler input\x00")
        self.manifest["leaf_inputs"].append("asm/main.s")
        self.manifest["commands"] = ["clang -target aarch64-linux-android -Imacros -c asm/main.s -o "
                                      + str(self.out / "unit.o")]
        _, _, archive = self.collect()
        for relative in ["macros/outer.s", "macros/inner.S", "macros/payload.bin"]:
            self.assertIn(str(self.source / relative).lstrip("/"), archive.getnames())

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
