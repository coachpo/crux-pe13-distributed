import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import image_inventory


OUT = '/home/runner/work/_temp/android-out'
PRODUCT = OUT + '/target/product/crux/'

# Directory listings in the exact `debugfs -f` batch form: the echoed command,
# then /inode/mode/uid/gid/name/size/ rows.
TREE = {
    '/': [('040755', '.'), ('040755', '..'), ('040755', 'system'), ('120644', 'vendor')],
    '/system': [('040755', '.'), ('040755', '..'), ('040755', 'lib64'), ('100644', 'build.prop')],
    '/system/lib64': [('040755', '.'), ('040755', '..'), ('100644', 'libkeystore2_crypto.so')],
}


def fake_debugfs(directory, tree):
    script = Path(directory) / 'debugfs'
    script.write_text(textwrap.dedent(f'''\
        #!{sys.executable}
        import json, sys
        tree = json.loads({json.dumps(json.dumps(tree))})
        print('debugfs 1.47.0 (5-Feb-2023)')
        commands = open(sys.argv[sys.argv.index('-f') + 1]).read().splitlines()
        for command in commands:
            print('debugfs: ' + command)
            directory = command[len('ls -p "'):-1]
            for number, (mode, name) in enumerate(tree.get(directory, []), 2):
                print(f'/{{number}}/{{mode}}/0/0/{{name}}/0/')
            print()
        '''))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


class ImageInventoryTest(unittest.TestCase):
    def test_declared_keeps_only_installed_staging_paths(self):
        outputs = [PRODUCT + 'system/lib64/libkeystore2_aaid.so', PRODUCT + 'system/bin/keystore2',
                   PRODUCT + 'vendor/firmware/wlan/qca_cld/', PRODUCT + 'vendor/build.prop',
                   PRODUCT + 'obj/PACKAGING/systemimage_intermediates/system.img',
                   PRODUCT + 'installed-files.txt', PRODUCT + 'recovery/root/init.rc', PRODUCT + 'system/']
        self.assertEqual(image_inventory.declared(outputs, OUT + '/'), {
            'system': ['bin/keystore2', 'lib64/libkeystore2_aaid.so'],
            'vendor': ['build.prop', 'firmware/wlan/qca_cld']})

    def test_image_paths_walks_every_directory_level(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = image_inventory.image_paths(Path(directory) / 'system.raw.img', fake_debugfs(directory, TREE))
        self.assertEqual(paths, {'/system', '/vendor', '/system/lib64', '/system/build.prop',
                                 '/system/lib64/libkeystore2_crypto.so'})

    def test_missing_installed_file_fails_the_check(self):
        inventory = {'system': ['build.prop', 'lib64/libkeystore2_aaid.so', 'lib64/libkeystore2_crypto.so'],
                     'vendor': ['bin']}
        vendor_tree = {'/': [('040755', '.'), ('040755', 'bin')]}
        with tempfile.TemporaryDirectory() as directory:
            system = fake_debugfs(directory, TREE)
            report = image_inventory.check({'system': inventory['system']}, {'system': 'system.raw.img'}, system)
            os.remove(system)
            vendor = fake_debugfs(directory, vendor_tree)
            clean = image_inventory.check({'vendor': inventory['vendor']}, {'vendor': 'vendor.raw.img'}, vendor)
        self.assertEqual(report['system'], {'declared': 3, 'missing': ['/system/lib64/libkeystore2_aaid.so']})
        self.assertEqual(clean['vendor'], {'declared': 1, 'missing': []})

    def test_plan_without_installed_files_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'declares no vendor installed files'):
            image_inventory.check({'vendor': []}, {'vendor': 'vendor.raw.img'}, 'debugfs')

    def test_cli_reports_and_exits_on_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = root / 'context.json'
            context.write_text(json.dumps({'image_inventory': {
                'system': ['lib64/libkeystore2_aaid.so'], 'vendor': ['bin']}}))
            tree = {**TREE, '/': TREE['/'] + [('040755', 'bin')]}
            debugfs = fake_debugfs(directory, tree)
            original = image_inventory.crux_zip.debugfs
            image_inventory.crux_zip.debugfs = lambda: debugfs
            try:
                with self.assertRaisesRegex(SystemExit, 'libkeystore2_aaid.so'):
                    image_inventory.main(['--context', str(context), '--system', 'system.raw.img',
                                          '--vendor', 'vendor.raw.img', '--report', str(root / 'report.json')])
            finally:
                image_inventory.crux_zip.debugfs = original
            report = json.loads((root / 'report.json').read_text())
        self.assertEqual(report['system']['missing'], ['/system/lib64/libkeystore2_aaid.so'])
        self.assertEqual(report['vendor']['missing'], [])


if __name__ == '__main__':
    unittest.main()
