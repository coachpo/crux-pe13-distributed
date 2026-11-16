"""Observable graph/ownership contracts across W10 partition boundaries."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('partition_graph', ROOT / 'scripts/graph.py')
graph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(graph)


class PartitionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'src'
        self.source.mkdir()
        self.out = self.root / 'out'
        self.policy = json.loads((ROOT / 'config/cloud-sharding-profile.json').read_text())
        self.policy.update(max_actions=8, max_parallel=20)
        self.number = str(self.out / 'soong/build_number.txt')
        self.kernel = str(self.out / 'target/product/crux/obj/KERNEL_OBJ')
        self.image = str(self.out / 'target/product/crux/boot.img')
        self.library = str(self.out / 'lib.o')

    def tearDown(self):
        self.temporary.cleanup()

    def indexed(self, reversed_declarations=False):
        statements = [
            f'build {self.number}: scalar input',
            f'build {self.kernel}: scalar input',
            f'build {self.kernel}/.config: scalar {self.kernel}',
            f'build {self.kernel}/arch/arm64/boot/Image-dtb: scalar {self.kernel}/.config',
            f'build {self.library}: cc input',
            f'build {self.image}: pack {self.kernel}/arch/arm64/boot/Image-dtb {self.number} {self.library}',
        ]
        if reversed_declarations:
            statements.reverse()
        ninja = self.root / ('reverse.ninja' if reversed_declarations else 'original.ninja')
        ninja.write_text('rule scalar\n  command = printf value > $out\n'
                         'rule cc\n  command = /usr/bin/clang -c $in -o $out\n'
                         'rule pack\n  command = cp $in $out\n' + '\n'.join(statements) + '\n')
        database = ninja.with_suffix('.sqlite')
        graph.index_graph(ninja, self.source, database)
        graph.augment_graph(database, {'schema_version': 1, 'side_output_dirs': [],
                            'groups': [[self.kernel, self.kernel + '/.config',
                                        self.kernel + '/arch/arm64/boot/Image-dtb']]})
        return graph.Graph(database)

    def plan(self, current, name, policy):
        return current.shard([self.image], self.root / name, 8, 20, export=False,
                             out_root=self.out, partition_profile=policy)

    def test_partition_preserves_actions_and_atomic_kernel_while_isolating_identity(self):
        current = self.indexed()
        legacy = self.plan(current, 'baseline', None)
        candidate = self.plan(current, 'candidate', self.policy)
        self.assertEqual(candidate['action_contract_sha256'], legacy['action_contract_sha256'])
        self.assertEqual(candidate['action_count'], legacy['action_count'])
        self.assertEqual(candidate['terminal_outputs'], legacy['terminal_outputs'])
        jobs = [job for wave in candidate['waves'] for job in wave]
        owners = {target: job for job in jobs for target in job['targets']}
        self.assertEqual(owners[self.number]['category'], 'identity')
        self.assertEqual(owners[self.image]['category'], 'image')
        self.assertEqual(owners[self.library]['category'], 'other')
        self.assertEqual(owners[self.library]['action_categories']['native'], 1)
        kernel = owners[self.kernel]
        self.assertEqual(kernel['category'], 'kernel')
        self.assertEqual(set(kernel['targets']), {self.kernel, self.kernel + '/.config',
                                                self.kernel + '/arch/arm64/boot/Image-dtb'})
        self.assertNotEqual(kernel['id'], owners[self.library]['id'])
        earlier = set()
        for wave in candidate['waves']:
            for job in wave:
                self.assertLessEqual(set(job['depends_on']), earlier)
            earlier.update(job['id'] for job in wave)
        current.db.close()

    def test_stable_partition_is_independent_of_sqlite_statement_assignment(self):
        left = self.indexed()
        a = self.plan(left, 'left', self.policy)
        left.db.close()
        right = self.indexed(True)
        b = self.plan(right, 'right', self.policy)
        right.db.close()
        shape = lambda plan: [[(job['category'], sorted(job['targets']), job['depends_on'])
                               for job in wave] for wave in plan['waves']]
        self.assertEqual(shape(a), shape(b))

    def test_compiler_helper_language_chain_stays_local_without_protected_outputs(self):
        ninja = self.root / 'tool-chain.ninja'
        outputs = [str(self.out / name) for name in ('compiler', 'helper', 'classes.jar', 'final.o')]
        ninja.write_text('rule native\n  command = /usr/bin/clang -c $in -o $out\n'
                         'rule helper\n  command = /usr/bin/python3 install.py $in $out\n'
                         'rule java\n  command = /usr/bin/javac $in -d $out\n'
                         + '\n'.join(f'build {output}: {rule} {outputs[index - 1] if index else "input"}'
                                     for index, (output, rule) in enumerate(zip(
                                         outputs, ('native', 'helper', 'java', 'native')))) + '\n')
        database = self.root / 'tool-chain.sqlite'
        graph.index_graph(ninja, self.source, database)
        current = graph.Graph(database)
        self.addCleanup(current.db.close)
        plan = current.shard([outputs[-1]], self.root / 'tool-chain-plan', 8, 20, export=False,
                             out_root=self.out, partition_profile=self.policy)
        self.assertEqual(plan['wave_count'], 1)
        self.assertEqual(plan['job_count'], 1)
        task = plan['waves'][0][0]
        self.assertEqual(set(task['targets']), set(outputs))
        self.assertEqual(task['category'], 'other')
        self.assertEqual(task['action_categories'], {'native': 2, 'other': 1, 'java': 1})

    def test_ready_ordinary_tool_family_uses_profile_order_before_output_name(self):
        ninja = self.root / 'families.ninja'
        java, native = str(self.out / 'a.jar'), str(self.out / 'z.o')
        ninja.write_text('rule native\n  command = /usr/bin/clang -c $in -o $out\n'
                         'rule java\n  command = /usr/bin/javac $in -d $out\n'
                         f'build {java}: java Main.java\n'
                         f'build {native}: native native.c\n')
        database = self.root / 'families.sqlite'
        graph.index_graph(ninja, self.source, database)
        current = graph.Graph(database)
        self.addCleanup(current.db.close)
        policy = {**self.policy, 'max_actions': 1}
        plan = current.shard([java, native], self.root / 'families-plan', 1, 20, export=False,
                             out_root=self.out, partition_profile=policy)
        self.assertEqual([[job['targets'] for job in wave] for wave in plan['waves']],
                         [[[native], [java]]])
        self.assertEqual([job['category'] for job in plan['waves'][0]], ['other', 'other'])

    def test_image_packaging_chain_keeps_helpers_and_listing_cooutputs_local(self):
        product = self.out / 'target/product/crux'
        boot = str(product / 'boot.img')
        recovery = str(product / 'recovery.img')
        resources = str(product / 'vendor/etc/recovery-resource.dat')
        listing = str(product / 'installed-files-vendor.txt')
        listing_json = str(product / 'installed-files-vendor.json')
        patch = str(product / 'obj/PACKAGING/recovery_patch_intermediates/recovery_from_boot.p')
        vendor = str(product / 'vendor.img')
        ninja = self.root / 'packaging.ninja'
        ninja.write_text('rule native\n  command = /usr/bin/clang -c $in -o $out\n'
                         'rule pack\n  command = package $in -o $out\n'
                         f'build {self.library}: native source.c\n'
                         f'build {boot}: pack {self.library}\n'
                         f'build {recovery}: pack {boot}\n'
                         f'build {resources}: pack {recovery}\n'
                         f'build {listing} {listing_json}: pack {resources}\n'
                         f'build {patch}: pack {boot} {recovery}\n'
                         f'build {vendor}: pack {listing} {listing_json} {patch}\n')
        database = self.root / 'packaging.sqlite'
        graph.index_graph(ninja, self.source, database)
        current = graph.Graph(database)
        self.addCleanup(current.db.close)
        destination = self.root / 'packaging-plan'
        plan = current.shard([boot, recovery, vendor], destination, 8, 20,
                             out_root=self.out, partition_profile=self.policy)
        self.assertEqual([len(wave) for wave in plan['waves']], [1, 1])
        ordinary, images = plan['waves'][0][0], plan['waves'][1][0]
        self.assertEqual(ordinary['targets'], [self.library])
        self.assertEqual(images['category'], 'image')
        self.assertEqual(images['action_count'], 6)
        self.assertEqual(set(images['targets']), {boot, recovery, resources, listing, patch, vendor})
        self.assertEqual(images['depends_on'], [ordinary['id']])
        manifest = json.loads((destination / images['manifest']).read_text())
        listing_edge = next(edge for edge in manifest['edges'] if listing in edge['outputs'])
        self.assertEqual(set(listing_edge['outputs']), {listing, listing_json})
        self.assertEqual(set(plan['terminal_outputs']), {boot, recovery, vendor})

    def test_system_filesystem_builder_and_listing_are_image_work_not_ordinary(self):
        product = self.out / 'target/product/crux'
        intermediate = str(product / 'obj/PACKAGING/systemimage_intermediates/system.img')
        listing = str(product / 'installed-files.txt')
        listing_json = str(product / 'installed-files.json')
        final = str(product / 'system.img')
        ninja = self.root / 'system-image.ninja'
        ninja.write_text('rule native\n  command = clang -c $in -o $out\n'
                         'rule pack\n  command = package $in -o $out\n'
                         f'build {self.library}: native source.c\n'
                         f'build {listing} {listing_json}: pack {self.library}\n'
                         f'build {intermediate}: pack {self.library} {listing}\n'
                         f'build {final}: pack {intermediate}\n')
        database = self.root / 'system-image.sqlite'
        graph.index_graph(ninja, self.source, database)
        current = graph.Graph(database)
        self.addCleanup(current.db.close)
        plan = current.shard([final], self.root / 'system-image-plan', 8, 20, export=False,
                             out_root=self.out, partition_profile=self.policy)
        self.assertEqual([len(wave) for wave in plan['waves']], [1, 1])
        ordinary, image = plan['waves'][0][0], plan['waves'][1][0]
        self.assertEqual(ordinary['targets'], [self.library])
        self.assertEqual(image['category'], 'image')
        self.assertEqual(set(image['targets']), {listing, intermediate, final})


if __name__ == '__main__':
    unittest.main()
