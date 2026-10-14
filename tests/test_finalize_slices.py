import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
graph = load('graph', SCRIPTS / 'graph.py')
finalize_slices = load('finalize_slices', SCRIPTS / 'finalize_slices.py')


class FinalizeSlicesTests(unittest.TestCase):
    """The 2026101104 slice set is `graph.py shard` plus these audit rules, and a
    hosted prepare run is only accepted when every manifest digest matches. These
    cases pin the two things that can silently change that digest: extra declared
    leaves and order-only imports for generated child tools."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        # Resolve once: macOS tempdirs are symlinked and the closed graph
        # compares recorded paths verbatim.
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'source'
        self.out = self.root / 'out'
        (self.source / 'src').mkdir(parents=True)
        (self.source / 'src/notice_reader.cpp').write_text('reader')
        (self.source / 'src/implicit.h').write_text('implicit')
        (self.source / 'src/NOTICE').write_text('notice')
        (self.source / 'src/r8.pro').write_text('pro')
        (self.source / 'src/packaging.mk').write_text('mk')
        (self.source / 'src/kernel_src').write_text('kernel')
        self.target_files = (str(self.out / 'target/product/crux/obj/PACKAGING/'
                                 'target_files_intermediates/'
                                 'aosp_crux_release-target_files-2026101104.zip'))
        self.tool = str(self.out / 'host/linux-x86/bin/readiness-tool')
        self.kernel_tree = str(self.out / 'target/product/crux/obj/KERNEL_OBJ')
        # A validation dependency keeps the child tool inside the sliced closure
        # and ordered into an earlier wave, but it never becomes an external
        # input of the terminal shard: that is exactly the 2026101104 case.
        (self.source / 'build.ninja').write_text(
            'rule cp\n  command = cp $in $out\n'
            f'build {self.tool}: cp src/notice_reader.cpp\n'
            f'build {self.kernel_tree}: cp src/kernel_src\n'
            f'build {self.target_files}: cp src/implicit.h || {self.kernel_tree} |@ {self.tool}\n')
        self.shards = self.root / 'shards'
        database = self.root / 'index.sqlite'
        graph.index_graph(self.source / 'build.ninja', self.source, database)
        graph.Graph(database).shard([self.target_files], self.shards,
                                    max_actions=1, max_parallel=1)
        plan = json.loads((self.shards / 'plan.json').read_text())
        self.jobs = {job['id']: job for wave in plan['waves'] for job in wave}
        self.terminal_job = next(job for job in self.jobs.values()
                                 if self.target_files in job['targets'])
        self.audit = self.root / 'graph-export-audit'
        self.audit.mkdir()
        (self.audit / 'target-files-source-closure.json').write_text(json.dumps({
            'notice_readers': [{'outputs': [self.tool], 'source_license_texts': ['src/NOTICE']}],
            'r8_embedded_includes': [{'r8_consumers': [{'outputs': [self.tool]}],
                                      'included_file': 'src/r8.pro'}],
            'selected_packaging_source_paths': ['src/packaging.mk'],
            'source_files': [{'path': 'src/implicit.h', 'relative': 'implicit.h',
                              'reasons': ['implicit_native_source_operand']}],
            'generated_child_readiness_inputs': [{'output': self.tool}],
        }))
        (self.audit / 'host-runtime.json').write_text(json.dumps({
            'records': [{'new_path': self.tool, 'transitive_generated_runtime_inputs': []}]}))
        self.contract = self.root / 'integration-contract.json'
        self.contract.write_text(json.dumps({
            'out_root': str(self.out), 'source_root': str(self.source),
            'consumers': [], 'kernel_source_leaves_after_slicing': [],
            # The kernel tree is exported whole by its mkdir producer; the audit
            # reads the owner of the directory path recorded here.
            'kernel_tree_export': True,
            'consumer_build_environment': {}, 'consumer_external_input_dirs': [],
        }))

    def tearDown(self):
        self.temporary.cleanup()

    def run_finalize(self):
        return finalize_slices.finalize(self.shards, self.audit / 'target-files-source-closure.json',
                                        self.audit / 'host-runtime.json', self.contract,
                                        self.root / 'slice-finalization.json', '2026101104')

    def manifest(self, job_id):
        return json.loads((self.shards / self.jobs[job_id]['manifest']).read_text())

    def plan(self):
        return json.loads((self.shards / 'plan.json').read_text())

    def job(self, job_id):
        return {job['id']: job for wave in self.plan()['waves'] for job in wave}[job_id]

    def test_child_tools_become_order_only_imports_of_the_terminal_shard(self):
        before = (self.shards / self.terminal_job['id'] / 'manifest.json').read_text()
        result = self.run_finalize()
        self.assertEqual(result['target_files_owner'], self.terminal_job['id'])
        self.assertEqual(result['child_inputs'], 1)
        manifest = self.manifest(self.terminal_job['id'])
        self.assertIn(self.tool, manifest['external_inputs'])
        self.assertIn(self.tool, manifest['external_phony_inputs'])
        tool_job = next(job for job in self.jobs if self.tool in self.jobs[job]['targets'])
        self.assertEqual(tool_job, 'w000-s000')
        self.assertIn(tool_job, self.job(self.terminal_job['id'])['depends_on'])
        self.assertEqual(set(self.job(self.terminal_job['id'])['depends_on']) -
                         set(self.jobs[self.terminal_job['id']]['depends_on']), {tool_job})
        ninja = (self.shards / self.terminal_job['id'] / manifest['ninja']).read_text()
        self.assertIn('build ' + graph.ninja_escape(self.tool) + ': phony', ninja)
        self.assertNotEqual(before, (self.shards / self.terminal_job['id'] / 'manifest.json').read_text())

    def test_audited_leaves_are_declared_without_changing_native_commands(self):
        commands = {job: self.manifest(job)['commands'] for job in self.jobs}
        self.run_finalize()
        for job, recorded in commands.items():
            self.assertEqual(self.manifest(job)['commands'], recorded, job)
        tool_manifest = self.manifest(next(job for job in self.jobs
                                           if self.tool in self.jobs[job]['targets']))
        self.assertIn('src/NOTICE', tool_manifest['leaf_inputs'])
        self.assertIn('src/r8.pro', tool_manifest['leaf_inputs'])
        terminal = self.manifest(self.terminal_job['id'])
        self.assertIn('src/implicit.h', terminal['leaf_inputs'])
        self.assertIn('src/packaging.mk', terminal['leaf_inputs'])

    def test_export_producers_cover_every_exported_path(self):
        self.run_finalize()
        plan = json.loads((self.shards / 'plan.json').read_text())
        exported = {path for job in self.jobs.values() for path in job['export_outputs']}
        self.assertEqual(set(plan['export_producers']), exported)
        self.assertEqual(plan['export_producers'][self.target_files], self.terminal_job['id'])
        self.assertTrue(plan['cold_transport_contract']['full_declared_output_exports'])

    def test_child_tool_from_a_later_wave_is_refused(self):
        plan_path = self.shards / 'plan.json'
        plan = json.loads(plan_path.read_text())
        waves = {job['id']: index for index, wave in enumerate(plan['waves']) for job in wave}
        tool_job = next(job for job in self.jobs if self.tool in self.jobs[job]['targets'])
        self.assertLess(waves[tool_job], waves[self.terminal_job['id']])
        # Report the child as produced in the terminal wave: the audit must refuse
        # to create a same-wave import that no earlier task could export.
        for wave in plan['waves']:
            for job in wave:
                if job['id'] == tool_job:
                    job['wave'] = waves[self.terminal_job['id']]
        plan_path.write_text(json.dumps(plan, indent=2) + '\n')
        with self.assertRaises(ValueError):
            self.run_finalize()

    def test_unknown_terminal_action_is_refused(self):
        body = (self.audit / 'target-files-source-closure.json').read_text()
        (self.audit / 'target-files-source-closure.json').write_text(
            json.dumps({**json.loads(body), 'generated_child_readiness_inputs':
                        [{'output': str(self.out / 'host/linux-x86/bin/absent-tool')}]}))
        with self.assertRaises(ValueError):
            self.run_finalize()


if __name__ == '__main__':
    unittest.main()
