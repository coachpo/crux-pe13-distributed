import copy
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import cloud_reuse as reuse
import worker


def file_spec(path, data=b'object', mode=0o644):
    return {'path': path, 'type': 'file', 'mode': mode, 'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest()}


class InputIdentityTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {'schema_version': 1, 'source_root': '/source',
                         'targets': ['/out/result'], 'outputs': ['/out/result'],
                         'graph_inputs': ['/out/original.ninja'],
                         'edges': [{'outputs': ['/out/result'], 'rule': 'compile',
                                    'command': 'compiler @/out/args.rsp',
                                    'rspfile': '/out/args.rsp', 'rspfile_content': '-O2 input.c',
                                    'deps': [['input.c', 'explicit']], 'restat': '1'}],
                         'deferred_validations': [], 'build_environment': {'LC_ALL': 'C.UTF-8'}}
        self.metadata = {'source_root': '/source', 'out_root': '/out',
                         'slice_path': '/source/.crux-task/graph',
                         'files': [file_spec('/source/input.c'),
                                   {'path': '/source/include', 'type': 'directory', 'mode': 0o755},
                                   {'path': '/source/current.h', 'type': 'symlink', 'mode': 0o777,
                                    'target': 'include/one.h'},
                                   file_spec('/source/.crux-task/graph/build.ninja', b'graph')],
                         'allowed_generated_inputs': [], 'external_inputs': [],
                         'external_input_dirs': [], 'required_interpreters': [], 'system_tools': []}

    def identity(self, manifest=None, metadata=None):
        return reuse.input_descriptor(manifest or self.manifest, metadata or self.metadata, 'a' * 64)['input_identity']

    def test_source_bytes_mode_link_target_and_directory_membership_invalidate(self):
        original = self.identity()
        changes = [lambda m: m['files'][0].update(sha256='b' * 64),
                   lambda m: m['files'][0].update(mode=0o755),
                   lambda m: m['files'][2].update(target='include/two.h'),
                   lambda m: m['files'].append(file_spec('/source/include/new.h', b'new member'))]
        for change in changes:
            with self.subTest(change=change):
                metadata = copy.deepcopy(self.metadata)
                change(metadata)
                self.assertNotEqual(self.identity(metadata=metadata), original)

    def test_resolved_commands_response_contents_topology_and_restat_invalidate(self):
        original = self.identity()
        for field, value in [('command', 'other-compiler @/out/args.rsp'),
                             ('rspfile_content', '-O0 input.c'),
                             ('deps', [['input.c', 'order']]), ('restat', '0')]:
            with self.subTest(field=field):
                manifest = copy.deepcopy(self.manifest)
                manifest['edges'][0][field] = value
                self.assertNotEqual(self.identity(manifest=manifest), original)

    def test_graph_transport_bookkeeping_does_not_replace_effective_semantics(self):
        manifest, metadata = copy.deepcopy(self.manifest), copy.deepcopy(self.metadata)
        manifest['graph_inputs'] = ['/out/renumbered.ninja']
        metadata['files'][-1] = file_spec('/source/.crux-task/graph/build.ninja', b'renumbered graph')
        self.assertEqual(self.identity(manifest, metadata), self.identity())
        manifest['edges'][0]['command'] += ' --changed'
        self.assertNotEqual(self.identity(manifest, metadata), self.identity())

    def test_marker_content_is_a_real_input_even_when_delivery_bookkeeping_is_not(self):
        self.metadata['files'].append(file_spec('/source/device/crux/marker.txt', b'build 50'))
        original = self.identity()
        self.metadata['files'][-1] = file_spec('/source/device/crux/marker.txt', b'build 51')
        self.assertNotEqual(self.identity(), original)

    def test_dynamic_discovery_and_generated_exemptions_are_not_reusable(self):
        self.manifest['edges'][0]['dyndep'] = '/out/discovery.dd'
        with self.assertRaisesRegex(ValueError, 'Dynamic Ninja'):
            self.identity()
        del self.manifest['edges'][0]['dyndep']
        self.metadata['allowed_generated_inputs'] = [{'path': '/out/old-object'}]
        with self.assertRaisesRegex(ValueError, 'generated exemptions'):
            self.identity()

    def test_execution_identity_tracks_implementation_not_source_lock_or_marker_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, text in {'scripts/worker.py': 'implementation',
                               '.github/workflows/build.yml': 'workflow',
                               'config/runtime.json': '{}',
                               'config/cloud-source-manifest.json': '{"pin":"old"}',
                               'config/cloud-source-overrides.json': '{}',
                               'config/source-patches/marker.patch': 'marker 50'}.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            original = reuse.execution_digest(root)
            (root / 'config/cloud-source-manifest.json').write_text('{"pin":"new"}')
            (root / 'config/cloud-source-overrides.json').write_text('{"pin":"new"}')
            (root / 'config/source-patches/marker.patch').write_text('marker 51')
            self.assertEqual(reuse.execution_digest(root), original)
            for name in ('scripts/worker.py', '.github/workflows/build.yml', 'config/runtime.json'):
                with self.subTest(name=name):
                    previous = (root / name).read_text()
                    (root / name).write_text(previous + '\nchanged')
                    self.assertNotEqual(reuse.execution_digest(root), original)
                    (root / name).write_text(previous)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.context = {'repository': 'owner/build', 'execution_id': '100', 'worker_commit': 'a' * 40,
                        'reuse_policy': {'version': reuse.VERSION, 'execution_digest': 'b' * 64,
                                         'force_cold': False},
                        'tasks': {'compile': {'input_identity': 'c' * 64, 'manifest_sha256': 'd' * 64,
                                             'depends_on': [], 'extra_exports': [], 'local_runtime_targets': []}}}
        self.runtime = {'CCACHE_DISABLE': '1', 'environment': {'TZ': 'UTC'}}
        self.native = {'id': 'compile', 'run_id': '100', 'worker_commit': 'a' * 40,
                       'manifest_sha256': 'd' * 64, 'status': 'success',
                       'outputs': [file_spec('/out/result')], 'build_environment': {'CCACHE_DISABLE': '1'}}
        self.basis = reuse.content_basis(self.context, 'compile', self.runtime, {})

    def built(self):
        result = copy.deepcopy(self.native)
        result['reuse'] = reuse.envelope(self.context, 'compile', copy.deepcopy(self.basis), copy.deepcopy(self.native))
        return result

    def adopted(self):
        old = copy.deepcopy(self.native)
        old.update(id='earlier-task', run_id='90', worker_commit='e' * 40, manifest_sha256='f' * 64)
        origin = {'repository': 'owner/build', 'run_id': '90', 'worker_commit': 'e' * 40,
                  'task_id': 'earlier-task', 'receipt_sha256': reuse.digest(old),
                  'artifact_id': 77, 'artifact_digest': 'sha256:' + '9' * 64}
        result = copy.deepcopy(self.native)
        result['reuse'] = reuse.envelope(self.context, 'compile', copy.deepcopy(self.basis), old, origin)
        return result

    def test_predecessor_actual_output_changes_key_but_run_numbers_do_not(self):
        context = copy.deepcopy(self.context)
        context['tasks']['compile']['depends_on'] = ['producer']
        dependency = {'outputs': [file_spec('/out/upstream')], 'run_id': '10'}
        key = reuse.content_key(reuse.content_basis(context, 'compile', self.runtime, {'producer': dependency}))
        dependency['run_id'] = '11'
        self.assertEqual(key, reuse.content_key(reuse.content_basis(context, 'compile', self.runtime,
                                                                  {'producer': dependency})))
        dependency['outputs'][0] = file_spec('/out/upstream', b'changed object')
        self.assertNotEqual(key, reuse.content_key(reuse.content_basis(context, 'compile', self.runtime,
                                                                     {'producer': dependency})))
        with self.assertRaisesRegex(ValueError, 'every admitted predecessor'):
            reuse.content_basis(context, 'compile', self.runtime, {})

    def test_current_build_and_verified_old_origin_are_accepted(self):
        reuse.validate_receipt(self.context, self.built(), 'compile')
        reuse.validate_receipt(self.context, self.adopted(), 'compile')

    def test_forced_cold_refuses_adoption_but_allows_current_native_build(self):
        self.context['reuse_policy']['force_cold'] = True
        with self.assertRaisesRegex(ValueError, 'Forced cold'):
            reuse.validate_receipt(self.context, self.adopted(), 'compile')
        reuse.validate_receipt(self.context, self.built(), 'compile')

    def test_origin_and_native_receipt_tampering_is_rejected(self):
        changes = [lambda e: e['origin'].update(repository='other/build'),
                   lambda e: e['origin'].update(run_id='other'),
                   lambda e: e['origin'].update(worker_commit='0' * 40),
                   lambda e: e['origin'].update(task_id='other'),
                   lambda e: e['origin'].update(receipt_sha256='0' * 64),
                   lambda e: e['origin'].pop('artifact_id'),
                   lambda e: e['origin'].pop('artifact_digest'),
                   lambda e: e['native_receipt'].update(status='failed'),
                   lambda e: e['native_receipt'].update(diagnostic_only=True),
                   lambda e: e['native_receipt']['outputs'][0].update(sha256='0' * 64)]
        for change in changes:
            with self.subTest(change=change):
                receipt = self.adopted()
                change(receipt['reuse'])
                with self.assertRaises(ValueError):
                    reuse.validate_receipt(self.context, receipt, 'compile')

    def test_missing_or_changed_reuse_binding_is_rejected(self):
        for missing in ('reuse', 'native_receipt', 'origin'):
            with self.subTest(missing=missing):
                receipt = self.adopted()
                (receipt if missing == 'reuse' else receipt['reuse']).pop(missing)
                with self.assertRaises(ValueError):
                    reuse.validate_receipt(self.context, receipt, 'compile')
        receipt = self.built()
        receipt['reuse']['basis']['input_identity'] = '0' * 64
        receipt['reuse']['key'] = reuse.content_key(receipt['reuse']['basis'])
        with self.assertRaisesRegex(ValueError, 'binding differs'):
            reuse.validate_receipt(self.context, receipt, 'compile')

    def test_repeated_adoption_preserves_original_native_provenance(self):
        previous = self.adopted()
        immediate = {'repository': 'owner/build', 'run_id': '100', 'worker_commit': 'a' * 40,
                     'task_id': 'compile', 'receipt_sha256': reuse.digest(previous),
                     'artifact_id': 88, 'artifact_digest': 'sha256:' + '8' * 64}
        result = copy.deepcopy(self.native)
        result['reuse'] = reuse.envelope(self.context, 'compile', self.basis, previous, immediate)
        reuse.validate_receipt(self.context, result, 'compile')
        self.assertEqual(result['reuse']['native_receipt']['run_id'], '90')
        self.assertEqual(result['reuse']['origin']['artifact_id'], 77)
        self.assertEqual(result['reuse']['origin']['adopted_from']['artifact_id'], 88)

    def test_summary_binds_consumer_to_actual_predecessor_outputs(self):
        context = copy.deepcopy(self.context)
        context['tasks']['producer'] = copy.deepcopy(context['tasks']['compile'])
        context['tasks']['compile']['depends_on'] = ['producer']
        outputs = {'producer': [file_spec('/out/upstream')], 'compile': self.native['outputs']}
        records = {}
        for ident in ('producer', 'compile'):
            native = copy.deepcopy(self.native)
            native.update(id=ident, outputs=copy.deepcopy(outputs[ident]))
            basis = reuse.content_basis(context, ident, self.runtime,
                                        {dep: {'outputs': outputs[dep]} for dep in context['tasks'][ident]['depends_on']})
            records[ident] = reuse.envelope(context, ident, basis, native)
        summary = {'reuse': records, 'output_identity': copy.deepcopy(outputs)}
        reuse.validate_summary(context, summary)
        changed = file_spec('/out/upstream', b'different actual producer bytes')
        summary['output_identity']['producer'] = [changed]
        origin = records['producer']
        origin['native_receipt']['outputs'] = [copy.deepcopy(changed)]
        origin['origin']['receipt_sha256'] = reuse.digest(origin['native_receipt'])
        with self.assertRaisesRegex(ValueError, 'different predecessor content'):
            reuse.validate_summary(context, summary)


class RuntimeIdentityTests(unittest.TestCase):
    def test_host_tool_content_directory_membership_and_packages_bind_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = root / 'tool'
            tool.write_bytes(b'compiler version one')
            data = root / 'data'
            data.mkdir()
            (data / 'template').write_text('template one')
            descriptor = {'system_tools': [str(tool), str(data)]}
            packages = subprocess.CompletedProcess([], 0, 'libc=one\n', '')
            environment = {'ImageOS': 'ubuntu22', 'ImageVersion': 'runner-1', 'HOME': '/home/runner'}
            with mock.patch.object(reuse.subprocess, 'run', return_value=packages), \
                    mock.patch.object(reuse.platform, 'machine', return_value='x86_64'), \
                    mock.patch.object(reuse.platform, 'release', return_value='kernel-one'):
                original = reuse.runtime_identity(descriptor, environment)
                previous_mode = tool.stat().st_mode & 0o777
                tool.chmod(previous_mode ^ 0o100)
                self.assertNotEqual(reuse.runtime_identity(descriptor, environment), original)
                tool.chmod(previous_mode)
                tool.write_bytes(b'compiler version two')
                self.assertNotEqual(reuse.runtime_identity(descriptor, environment), original)
                tool.write_bytes(b'compiler version one')
                (data / 'new-template').write_text('new member')
                self.assertNotEqual(reuse.runtime_identity(descriptor, environment), original)
                (data / 'new-template').unlink()
                packages.stdout = 'libc=two\n'
                self.assertNotEqual(reuse.runtime_identity(descriptor, environment), original)
                packages.stdout = 'libc=one\n'
                self.assertNotEqual(reuse.runtime_identity(descriptor, {**environment, 'ImageVersion': 'runner-2'}), original)

    def test_missing_declared_host_tool_refuses_runtime_identity(self):
        packages = subprocess.CompletedProcess([], 0, 'libc=one\n', '')
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(reuse.subprocess, 'run', return_value=packages):
            with self.assertRaisesRegex(ValueError, 'host input is missing'):
                reuse.runtime_identity({'system_tools': [str(Path(temporary) / 'missing')]}, {})


class NativeEnvironmentTests(unittest.TestCase):
    def test_controlled_environment_does_not_import_ci_identity_or_credentials(self):
        environment = reuse.native_environment({'HOME': '/home/runner', 'USER': 'runner', 'LOGNAME': 'runner',
                                                'GITHUB_RUN_ID': '100', 'GH_TOKEN': 'secret',
                                                'ACTIONS_RUNTIME_TOKEN': 'secret', 'LANG': 'changed',
                                                'TZ': 'changed', 'MAKEFLAGS': 'srctree=/k'})
        self.assertEqual(environment['MAKEFLAGS'], 'srctree=/k')
        self.assertEqual(environment['TZ'], 'UTC')
        self.assertEqual(environment['LC_ALL'], 'C.UTF-8')
        self.assertNotIn('GITHUB_RUN_ID', environment)
        self.assertNotIn('GH_TOKEN', environment)
        self.assertNotIn('ACTIONS_RUNTIME_TOKEN', environment)

    def test_worker_keeps_receipt_identity_out_of_native_processes(self):
        with mock.patch.dict(os.environ, {'GITHUB_RUN_ID': '100', 'GITHUB_SHA': 'a' * 40,
                                          'MAKEFLAGS': 'srctree=/k'}, clear=True):
            environment = worker.native_process_environment({'GITHUB_REPOSITORY': 'owner/build',
                                                               'CCACHE_DISABLE': '1', 'OUT_DIR': '/out'})
        self.assertEqual(environment['MAKEFLAGS'], 'srctree=/k')
        self.assertEqual(environment['OUT_DIR'], '/out')
        self.assertEqual(environment['CCACHE_DISABLE'], '1')
        self.assertFalse(any(key.startswith('GITHUB_') for key in environment))


@unittest.skipUnless(shutil.which('zstd'), 'zstd is required for native artifact validation')
class NativeArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.members = [(file_spec('/out/object', b'compiled', 0o640), b'compiled'),
                        ({'path': '/out/link', 'type': 'symlink', 'mode': 0o777, 'target': 'object'}, b''),
                        ({'path': '/out/generated', 'type': 'directory', 'mode': 0o755}, b'')]

    def archive(self, members=None):
        members = self.members if members is None else members
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w') as stream:
            for spec, data in members:
                info = tarfile.TarInfo(spec['path'].lstrip('/'))
                info.mode = spec['mode']
                info.type = {'file': tarfile.REGTYPE, 'symlink': tarfile.SYMTYPE,
                             'directory': tarfile.DIRTYPE}[spec['type']]
                if spec['type'] == 'symlink':
                    info.linkname = spec['target']
                if spec['type'] == 'file':
                    info.size = len(data)
                stream.addfile(info, io.BytesIO(data) if spec['type'] == 'file' else None)
        archive = self.root / 'outputs.tar.zst'
        with archive.open('wb') as output:
            subprocess.run(['zstd', '-q', '-c'], input=raw.getvalue(), stdout=output, check=True)
        return {'archive_bytes': archive.stat().st_size, 'archive_sha256': reuse.file_digest(archive),
                'outputs': copy.deepcopy([spec for spec, _ in self.members])}

    def test_full_native_archive_verifies_file_mode_content_link_and_directory(self):
        receipt = self.archive()
        self.assertEqual(reuse.verify_native_output(receipt, self.root), self.root / 'outputs.tar.zst')

    def test_member_mode_size_sha_and_link_tampering_is_rejected(self):
        original = self.archive()
        for field, value in [('mode', 0o755), ('size', 999), ('sha256', '0' * 64)]:
            with self.subTest(field=field):
                receipt = copy.deepcopy(original)
                receipt['outputs'][0][field] = value
                with self.assertRaisesRegex(ValueError, 'Native member'):
                    reuse.verify_native_output(receipt, self.root)
        receipt = copy.deepcopy(original)
        receipt['outputs'][1]['target'] = 'wrong-object'
        with self.assertRaisesRegex(ValueError, 'Native member link'):
            reuse.verify_native_output(receipt, self.root)

    def test_duplicate_unrecorded_and_missing_members_are_rejected(self):
        cases = [self.members + [self.members[0]], self.members[:-1],
                 self.members + [(file_spec('/out/unrecorded'), b'object')]]
        for members in cases:
            with self.subTest(members=members):
                receipt = self.archive(members)
                with self.assertRaises(ValueError):
                    reuse.verify_native_output(receipt, self.root)

    def test_archive_bytes_and_missing_archive_are_rejected(self):
        receipt = self.archive()
        archive = self.root / 'outputs.tar.zst'
        with archive.open('ab') as output:
            output.write(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'archive bytes/digest'):
            reuse.verify_native_output(receipt, self.root)
        archive.unlink()
        with self.assertRaises(FileNotFoundError):
            reuse.verify_native_output(receipt, self.root)

    def test_duplicate_receipt_paths_member_type_and_unsafe_paths_are_rejected(self):
        receipt = self.archive()
        receipt['outputs'].append(copy.deepcopy(receipt['outputs'][0]))
        with self.assertRaisesRegex(ValueError, 'Duplicate native output path'):
            reuse.verify_native_output(receipt, self.root)
        replaced = copy.deepcopy(self.members)
        replaced[0] = ({'path': '/out/object', 'type': 'symlink', 'mode': 0o640, 'target': 'other'}, b'')
        receipt = self.archive(replaced)
        with self.assertRaisesRegex(ValueError, 'Native member type/mode'):
            reuse.verify_native_output(receipt, self.root)
        receipt = self.archive(self.members + [(file_spec('/../outside'), b'object')])
        with self.assertRaisesRegex(ValueError, 'Unsafe native archive path'):
            reuse.verify_native_output(receipt, self.root)


if __name__ == '__main__':
    unittest.main()
