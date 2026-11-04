import copy
import importlib.util
from pathlib import Path
import shlex
import sys
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('cloud_rust_native', SCRIPTS / 'cloud_rust_native.py')
rust_native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rust_native)


class RustNativeFileReadTests(unittest.TestCase):
    source = '/home/qingli/crux-pe13-offline-2026-09-25/pe13'
    out = '/home/runner/work/_temp/android-out'

    def read(self, command, **metadata):
        edge = {'command': command, 'outputs': [self.out + '/obj/consumer'], **metadata}
        return rust_native.rust_native_file_reads(edge, self.source, self.out)

    def test_keystore_link_args_retain_native_libraries_and_crt_objects(self):
        intermediates = self.out + '/soong/.intermediates/'
        apc = (intermediates + 'system/security/keystore2/apc_compat/libkeystore2_apc_compat/'
               'android_arm64_armv8-2a_shared_cfi/libkeystore2_apc_compat.so')
        crypto = (intermediates + 'system/security/keystore2/src/crypto/libkeystore2_crypto/'
                  'android_arm64_armv8-2a_shared_cfi/libkeystore2_crypto.so')
        crt = intermediates + 'bionic/libc/crtbegin_dynamic/android_arm64_armv8-2a/crtbegin_dynamic.o'
        native_archive = intermediates + 'native/libbinding.a'
        command = ('ANDROID_RUST_VERSION=1.59.0 prebuilts/rust/linux-x86/1.59.0/bin/rustc '
                   '-C linker=prebuilts/clang/host/linux-x86/clang-r450784d/bin/clang++ '
                   '-C ' + shlex.quote('link-args=' + ' '.join([
                       crt, '-target', 'aarch64-linux-android', '-Wl,-z,relro', apc, crypto,
                       native_archive, apc,
                       'prebuilts/clang/host/linux-x86/clang-r450784d/lib64/libclang_rt.builtins.a']))
                   + ' --extern=keystore2=' + intermediates + 'rust/libkeystore2.rlib'
                   + ' -L dependency=' + intermediates + 'rust -o ' + self.out + '/obj/consumer')
        self.assertEqual(self.read(command), sorted([apc, crypto, crt, native_archive]))

    def test_split_and_joined_codegen_options(self):
        for prefix in ('-C ', '-C', '--codegen ', '--codegen='):
            for option in ('link-arg', 'link-args'):
                with self.subTest(prefix=prefix, option=option):
                    value = self.out + '/libffi.so.1.2'
                    self.assertEqual(self.read('rustc ' + prefix + shlex.quote(option + '=' + value)), [value])

    def test_link_arg_preserves_spaces_and_literal_shell_punctuation(self):
        path = self.out + '/native inputs;literal/libffi.so'
        self.assertEqual(self.read('rustc -C ' + shlex.quote('link-arg=' + path)), [path])

    def test_link_args_uses_whitespace_not_shell_tokenization(self):
        paths = [self.out + '/one.a', self.out + '/two.so', self.out + '/three.o']
        command = 'rustc -C ' + shlex.quote('link-args=' + '\t'.join(paths[:2]) + '\n' + paths[2])
        self.assertEqual(self.read(command), sorted(paths))

    def test_shell_wrappers_cwd_and_normalization(self):
        inner = ('cd ' + self.out + '/staging && env -C ../objects '
                 'prebuilts/rust/bin/rustc -Clink-arg=../libs/./native.a')
        command = ('(/bin/bash -c ' + shlex.quote(inner) + ') && '
                   'rustc -Clink-arg=source-relative.so && '
                   'env -C ' + self.out + '/objects rustc --codegen link-arg=../other.so')
        self.assertEqual(self.read(command), [self.out + '/libs/native.a', self.out + '/other.so'])

    def test_only_actual_rustc_invocations_participate(self):
        for command in ('echo ' + shlex.quote('rustc -Clink-arg=' + self.out + '/ignored.so'),
                        'rustc-wrapper -Clink-arg=' + self.out + '/ignored.so',
                        'clang -Clink-arg=' + self.out + '/ignored.so',
                        "printf 'unrelated malformed shell"):
            with self.subTest(command=command):
                self.assertEqual(self.read(command), [])

    def test_non_input_flags_do_not_create_reads_or_unresolved_errors(self):
        payload = ' '.join([
            '-o', self.out + '/output.so', '--output', self.out + '/output.a',
            '-L', self.out + '/search.a', '--library-path', self.out + '/search.so',
            '-soname', self.out + '/soname.so', '--soname', self.out + '/soname.a',
            '-h', self.out + '/soname.o', '-rpath', '$ORIGIN/runtime.so',
            '--rpath', self.out + '/rpath.so', '-rpath-link', self.out + '/rpathlink.so',
            '-Map', self.out + '/map.a', '--Map', self.out + '/map.o',
            '--exclude-libs', self.out + '/excluded.a', '-dynamic-linker', self.out + '/ld.so',
            '-l', self.out + '/not-an-exact-file.so', '-o' + self.out + '/joined.so',
            '--output=' + self.out + '/joined.a', '-L' + self.out + '/joined-search.so',
            '--rpath=$ORIGIN/joined.so', '-Map=' + self.out + '/joined-map.so',
            '-Wl,-rpath,$ORIGIN/other.so,-soname,' + self.out + '/ignored.so'
            + ',-L,' + self.out + '/ignored.a,--exclude-libs,' + self.out + '/ignored.o',
            self.out + '/read.so',
        ])
        self.assertEqual(self.read('rustc -C ' + shlex.quote('link-args=' + payload)), [self.out + '/read.so'])

    def test_flag_values_can_cross_codegen_option_boundaries(self):
        values = ['-rpath', '$ORIGIN/runtime.so', '-L', self.out + '/search.a',
                  '-Xlinker', '--soname', '-Xlinker', self.out + '/soname.so',
                  self.out + '/read.so']
        command = 'rustc ' + ' '.join('-C ' + shlex.quote('link-arg=' + value) for value in values)
        self.assertEqual(self.read(command), [self.out + '/read.so'])

    def test_root_boundaries_self_outputs_and_duplicate_paths(self):
        self_output = self.out + '/own.so'
        payload = ' '.join([self.out + '/child/../own.so', self.out + '/one.a',
                            self.out + '/./one.a', self.out + '-sibling/ignored.so',
                            self.out + '/../../outside.so', self.source + '/prebuilts/runtime.a'])
        self.assertEqual(self.read('rustc -Clink-args=' + shlex.quote(payload), outputs=[self_output]),
                         [self.out + '/one.a'])
        edge = {'command': 'rustc -Clink-arg=out/own.so', 'outputs': ['out/own.so']}
        self.assertEqual(rust_native.rust_native_file_reads(edge, '/SOURCE', '/SOURCE/out'), [])

    def test_unresolved_actual_native_operands_are_rejected(self):
        for path in ('$OUT/lib.so', self.out + '/$name.so', '`pwd`/lib.so',
                     self.out + '/lib*.so', self.out + '/lib?.a', self.out + '/[ab].o',
                     self.out + '/lib.so.$VERSION', self.out + '/lib.so.*', self.out + '/lib.a*'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Unresolved Rust native file operand'):
                self.read('rustc -C ' + shlex.quote('link-arg=' + path))
        command = 'cd ' + shlex.quote('$OUT') + ' && rustc -Clink-arg=relative.so'
        with self.assertRaisesRegex(ValueError, 'Unresolved Rust native file operand'):
            self.read(command)

    def test_unsupported_rust_and_linker_responses_are_explicit_errors(self):
        for command in ('rustc @' + self.out + '/rust.rsp',
                        'rustc -C link-args=@' + self.out + '/link.rsp',
                        'rustc -C link-arg=-Wl,@' + self.out + '/link.rsp'):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, 'response-file operand'):
                self.read(command, rspfile=self.out + '/rust.rsp', rspfile_content='recorded but unsupported')
        self.assertEqual(self.read('rustc source.rs', rspfile=self.out + '/unused.rsp'), [])

    def test_missing_codegen_values_fail_clearly(self):
        for argument in ('-C', '--codegen', '-Clink-arg', '--codegen=link-args'):
            with self.subTest(argument=argument), self.assertRaisesRegex(ValueError, 'Missing'):
                self.read('rustc ' + argument)

    def test_audit_is_read_only_and_does_not_probe_filesystem(self):
        edge = {'command': 'rustc -Clink-arg=' + self.out + '/missing.a',
                'outputs': [self.out + '/consumer'], 'rspfile_content': 'unchanged'}
        before = copy.deepcopy(edge)
        with mock.patch('builtins.open', side_effect=AssertionError('filesystem read')), \
                mock.patch('os.stat', side_effect=AssertionError('filesystem stat')), \
                mock.patch('os.scandir', side_effect=AssertionError('filesystem enumeration')):
            self.assertEqual(rust_native.rust_native_file_reads(edge, self.source, self.out),
                             [self.out + '/missing.a'])
        self.assertEqual(edge, before)


if __name__ == '__main__':
    unittest.main()
