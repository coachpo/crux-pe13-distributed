"""A generated Recovery tree must survive until its local packing consumers."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('recovery_partition_graph', ROOT / 'scripts/graph.py')
graph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(graph)
NINJA = os.environ.get('NINJA') or shutil.which('ninja')


class RecoveryPartitionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.out = self.root / 'out'
        self.product = self.out / 'target/product/crux'
        self.tree = self.product / 'recovery/root'
        stamp = str(self.product / 'obj/PACKAGING/recovery_intermediates/ramdisk_files-timestamp')
        cpio = str(self.product / 'ramdisk-recovery.cpio')
        ramdisk = str(self.product / 'ramdisk-recovery.img')
        recovery = str(self.product / 'recovery.img')
        self.resource = str(self.product / 'vendor/etc/recovery-resource.dat')
        self.group = [stamp, cpio, ramdisk, recovery, self.resource]
        asset = self.source / 'asset.input'
        asset.write_text('required recovery resource\n')
        helper = self.source / 'step.py'
        helper.write_text(
            'from pathlib import Path\nimport shutil, sys\n'
            'mode, output = sys.argv[1], Path(sys.argv[2])\n'
            'output.parent.mkdir(parents=True, exist_ok=True)\n'
            "if mode == 'stage': output.write_text('ready\\n')\n"
            "elif mode == 'stamp':\n"
            "    tree = Path(sys.argv[3]); (tree / 'res').mkdir(parents=True)\n"
            "    shutil.copyfile(sys.argv[4], tree / 'res/asset.input'); output.touch()\n"
            "elif mode in {'cpio', 'resource'}:\n"
            "    output.write_bytes((Path(sys.argv[3]) / 'res/asset.input').read_bytes())\n"
            'else: output.write_bytes(Path(sys.argv[3]).read_bytes())\n')
        rows = []

        def build(name, output, inputs, arguments):
            rows.extend(['rule ' + name,
                         '  command = ' + shlex.join([sys.executable, str(helper), *arguments]),
                         'build ' + output + ': ' + name + ' ' + ' '.join(inputs)])

        stages = [str(self.out / ('stage' + str(index))) for index in range(6)]
        for index, stage in enumerate(stages):
            build('s' + str(index), stage, [str(asset)] if index == 0 else [stages[index - 1]],
                  ['stage', stage])
        build('tree', stamp, [stages[4]], ['stamp', stamp, str(self.tree), str(asset)])
        build('cpio', cpio, [stamp], ['cpio', cpio, str(self.tree)])
        # The late ordinary predecessor makes natural same-category packing
        # insufficient: the directory owner can otherwise finish one job early.
        build('gzip', ramdisk, [cpio, stages[5]], ['copy', ramdisk, cpio])
        build('image', recovery, [ramdisk], ['copy', recovery, ramdisk])
        build('resources', self.resource, [recovery], ['resource', self.resource, str(self.tree)])
        self.ninja = self.source / 'build.ninja'
        self.ninja.write_text('\n'.join(rows) + '\n')
        self.policy = json.loads((ROOT / 'config/cloud-sharding-profile.json').read_text())
        self.policy.update(max_actions=5, max_parallel=20)
        ownership = json.loads((ROOT / 'config/cloud-ownership-profile.json').read_text())
        self.production_group = next(group for group in ownership['groups']
                                     if any(path.endswith('/vendor/etc/recovery-resource.dat') for path in group))
        self.production_group = [path.replace('${OUT}', str(self.out)) for path in self.production_group]

    def plan(self, label, groups):
        case = self.root / label
        case.mkdir()
        database = case / 'graph.sqlite'
        graph.index_graph(self.ninja, self.source, database)
        graph.augment_graph(database, {'schema_version': 1, 'side_output_dirs': [{
            'producer': self.group[0], 'dirs': [str(self.tree)],
            'rationale': 'Fixture completed tree', 'evidence': ['Synthetic Recovery packing chain']}],
            'groups': groups})
        current = graph.Graph(database)
        try:
            plan = current.shard([self.resource], case / 'shards', 5, 20,
                                 out_root=self.out, partition_profile=self.policy)
        finally:
            current.db.close()
        return case, plan

    def test_recovery_group_is_convex_and_all_readers_share_its_owner(self):
        _, plan = self.plan('atomic', [self.production_group])
        owners = {target: task['id'] for wave in plan['waves'] for task in wave for target in task['targets']}
        self.assertEqual(len({owners[path] for path in self.group}), 1)
        with self.assertRaisesRegex(ValueError, 'cycle'):
            self.plan('endpoints', [[self.group[0], self.group[-1]]])

    def execute(self, label, groups):
        case, plan = self.plan(label, groups)
        exports = {}
        evidence = case / 'out-evidence'
        evidence.mkdir()
        failed = None
        for wave in plan['waves']:
            for job in wave:
                self.assertFalse(self.out.exists())
                self.out.mkdir()
                for owner in job['depends_on']:
                    shutil.copytree(exports[owner], self.out, dirs_exist_ok=True, symlinks=True)
                work = case / ('work-' + job['id'])
                work.mkdir()
                result = subprocess.run([NINJA, '-f', str(case / 'shards' / job['id'] / 'build.ninja'),
                                         '-j', '1', *job['targets']], cwd=work, capture_output=True, text=True)
                if result.returncode:
                    failed = result.stdout + result.stderr
                    self.out.rename(evidence / job['id'])
                    break
                snapshot = case / 'exports' / job['id']
                snapshot.mkdir(parents=True)
                for value in job['export_outputs']:
                    path = Path(value)
                    target = snapshot / path.relative_to(self.out)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if path.is_dir():
                        shutil.copytree(path, target, symlinks=True)
                    else:
                        shutil.copy2(path, target, follow_symlinks=False)
                exports[job['id']] = snapshot
                self.out.rename(evidence / job['id'])
            if failed:
                break
        return plan, failed

    @unittest.skipUnless(NINJA, 'Ninja is required for cold generated-tree transport')
    def test_cold_generated_tree_needs_its_packing_group_not_transitive_exports(self):
        broken, failure = self.execute('without-atomic', [])
        self.assertIsNotNone(failure)
        self.assertIn('res/asset.input', failure)
        fixed, failure = self.execute('with-atomic', [self.production_group])
        self.assertIsNone(failure)
        self.assertEqual(broken['action_contract_sha256'], fixed['action_contract_sha256'])
