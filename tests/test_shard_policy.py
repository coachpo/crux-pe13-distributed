import sys
from pathlib import Path
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import shard_policy


SRC = '/src'
OUT = '/work/android-out'


def profile():
    return {
        'schema_version': 1,
        'version': 'w10-1',
        'wave_capacity': 20,
        'max_actions': 4000,
        'max_parallel': 20,
        'isolate_categories': ['identity', 'kernel', 'image'],
        'category_order': ['identity', 'kernel', 'other', 'native', 'java', 'image'],
        'identity_output_patterns': [
            '${OUT}/soong/build_number.txt',
            '${OUT}/build_date.txt',
            '${OUT}/target/product/crux/build_fingerprint.txt',
            '${OUT}/target/product/crux/build_thumbprint.txt',
            '${OUT}/target/product/crux/obj/PACKAGING/system_build_prop_intermediates/buildinfo.prop',
            '${OUT}/target/product/crux/**/build.prop',
            '${OUT}/target/product/crux/**/prop.default',
            '${OUT}/target/product/crux/system/etc/crux-delivery-verification.txt',
        ],
        'kernel_output_roots': ['${OUT}/target/product/crux/obj/KERNEL_OBJ'],
        'image_outputs': [
            '${OUT}/target/product/crux/boot.img',
            '${OUT}/target/product/crux/recovery.img',
            '${OUT}/target/product/crux/system.img',
            '${OUT}/target/product/crux/vendor.img',
            '${OUT}/target/product/crux/ramdisk.img',
            '${OUT}/target/product/crux/ramdisk-recovery.img',
            '${OUT}/target/product/crux/dtbo.img',
        ],
        'java_tools': ['java', 'javac', 'soong_javac_wrapper', 'd8', 'r8', 'turbine', 'metalava'],
        'native_tools': ['clang', 'clang++', 'gcc', 'g++', 'cc', 'c++', 'rustc'],
    }


def action(output, command=''):
    return {'rule': 'CUSTOM_COMMAND', 'outputs': [output], 'deps': []}, {'command': command}


class ShardPolicyTests(unittest.TestCase):
    def classify(self, output, command=''):
        record, metadata = action(output, command)
        return shard_policy.classify_action(record, metadata, SRC, OUT, profile())

    def test_exact_identity_image_and_kernel_outputs_precede_tool_category(self):
        self.assertEqual(self.classify(OUT + '/soong/build_number.txt', '/usr/bin/clang -c input.c'),
                         'identity')
        self.assertEqual(self.classify(OUT + '/target/product/crux/system/build.prop', 'javac Main.java'),
                         'identity')
        self.assertEqual(self.classify(OUT + '/target/product/crux/system.img', '/usr/bin/javac Main.java'),
                         'image')
        record = {'rule': 'CUSTOM_COMMAND', 'outputs': [
            OUT + '/target/product/crux/system.img',
            OUT + '/target/product/crux/obj/KERNEL_OBJ/arch/arm64/boot/Image'], 'deps': []}
        self.assertEqual(shard_policy.classify_action(record, {'command': '/usr/bin/javac'}, SRC, OUT, profile()),
                         'kernel')

    def test_source_and_output_pattern_variables_are_normalized(self):
        self.assertEqual(self.classify(OUT + '/target/product/crux/recovery/root/prop.default'), 'identity')
        self.assertEqual(self.classify(OUT + '/target/product/crux/ramdisk-recovery.img'), 'image')
        self.assertEqual(self.classify(OUT + '/target/product/crux/obj/KERNEL_OBJ/./vmlinux'), 'kernel')

    def test_java_and_native_are_based_on_executed_program_not_output_names(self):
        self.assertEqual(self.classify(OUT + '/host/obj/JavaThing.o',
                                       '/bin/bash -c "cd /src && /usr/bin/java -cp tools.jar com.example.Main"'),
                         'java')
        self.assertEqual(self.classify(OUT + '/host/bin/thing',
                                       '/usr/bin/env CC=clang ccache /prebuilt/bin/clang-r450784d -c x.c'),
                         'native')
        self.assertEqual(self.classify(OUT + '/host/clang-output.jar', '/usr/bin/python3 wrapper.py'), 'other')
        self.assertEqual(self.classify(OUT + '/host/classes.jar',
                                       OUT + '/host/linux-x86/bin/soong_javac_wrapper '
                                       '/prebuilt/jdk/bin/javac -d classes Main.java'), 'java')
        self.assertEqual(self.classify(OUT + '/host/object.o', 'javac Main.java && clang -c source.cc'),
                         'other')

    def test_response_file_text_is_not_mistaken_for_the_executed_tool(self):
        record, metadata = action(OUT + '/host/object.o', '/usr/bin/python3 compile.py @args.rsp')
        metadata['rspfile_content'] = '/prebuilt/bin/clang++ -c source.cc'
        self.assertEqual(shard_policy.classify_action(record, metadata, SRC, OUT, profile()), 'other')

    def test_stable_unit_key_uses_normalized_sorted_outputs_not_edge_ids(self):
        self.assertEqual(shard_policy.stable_unit_key([OUT + '/z.o', OUT + '/a.o', OUT + '/z.o']),
                         (OUT + '/a.o', OUT + '/z.o'))
        self.assertEqual(shard_policy.stable_unit_key([OUT + '/a.o', OUT + '/z.o']),
                         (OUT + '/a.o', OUT + '/z.o'))

    def test_malformed_profile_and_nonabsolute_outputs_fail_closed(self):
        record, metadata = action(OUT + '/x.o', '/usr/bin/clang -c x.c')
        with self.assertRaisesRegex(ValueError, 'unsupported W10'):
            shard_policy.classify_action(record, metadata, SRC, OUT, {**profile(), 'version': 'other'})
        with self.assertRaisesRegex(ValueError, 'absolute POSIX'):
            shard_policy.classify_action({'outputs': ['relative.o']}, metadata, SRC, OUT, profile())


if __name__ == '__main__':
    unittest.main()
