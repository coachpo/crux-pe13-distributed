import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
import sys
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location('cloud_assemble', SCRIPTS / 'cloud_assemble.py')
cloud_assemble = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cloud_assemble)


class CloudAssembleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest_path = self.root / 'source-manifest.json'
        self.projects = [{'path': 'build/soong', 'revision': 'abc123'},
                         {'path': 'device/xiaomi/crux', 'revision': 'def456'}]
        self.manifest_path.write_text(json.dumps({'source_root': '/src', 'projects': self.projects}))
        self.manifest_sha = hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()
        self.out = Path('/out')
        self.product = self.out / 'target/product/crux'
        self.kernel = self.product / 'obj/KERNEL_OBJ'
        self.qcom = self.kernel / 'arch/arm64/boot/dts/qcom'
        self.ids = [f'task-{index:02d}' for index in range(39)]
        sizes = (13, 3, 7, 6, 5, 2, 2, 1)
        self.waves, cursor = [], 0
        for size in sizes:
            self.waves.append(self.ids[cursor:cursor + size])
            cursor += size
        self.worker_commit = 'b' * 40
        self.context = {
            'schema_version': 1, 'execution_id': '9001', 'worker_commit': self.worker_commit,
            'repository': 'owner/repo', 'build_number': '2026110303', 'pack_run': '41779197896',
            'shard_run': '41769467686', 'graph_run': '41677764699', 'index_run': '41685367061',
            'source_run': '41677764699', 'source_manifest_sha256': self.manifest_sha,
            'plan_sha256': 'c' * 64, 'graph_index': {'bytes': 300, 'sha256': 'd' * 64},
            'source_root': '/src', 'out_root': str(self.out), 'waves': self.waves,
            'tasks': {}, 'assembly_exports': {},
            'frontend': {
                'graph_receipt': {'status': 'success', 'build_number': '2026110303',
                    'build_datetime': 1793724452,
                    'build_fingerprint': 'Xiaomi/crux/crux:13/TQ3A.230901.001.B1/2026110303:userdebug/test-keys',
                    'source_root': '/src', 'out_root': str(self.out),
                    'source_manifest': {'sha256': self.manifest_sha}},
                'build_date': '1793724452',
                'build_thumbprint': '13/TQ3A.230901.001.B1/2026110303:userdebug/release-keys\n',
                'branding_keys': {'org.pixelexperience.build_date': '20261103-1712',
                                  'org.pixelexperience.build_date_utc': '1793725920'},
                'identity_commands': [{'command': 'echo org.pixelexperience.build_date=20261103-1712'},
                                      {'command': 'echo org.pixelexperience.build_date_utc=1793725920'}],
                'buildinfo_environment': {
                    **cloud_assemble.STATIC_BUILDINFO,
                    'BUILD_NUMBER': '$(cat OUT/soong/build_number.txt)',
                    'PRIVATE_BUILD_DESC': 'aosp_crux-userdebug 13 TQ3A.230901.001.B1 '
                                          '$(cat OUT/soong/build_number.txt) release-keys',
                    'DATE': 'date -d @$(cat OUT/build_date.txt)',
                },
                'absent_partition_namespaces': {'product': {'reason': 'no product image target',
                                                             'evidence': ['fresh plan has no product.img']}},
                'source_revisions': self.projects,
            },
            'source_selection': {'schema_version': 1, 'build_number': '2026110303',
                                 'source_manifest_sha256': self.manifest_sha, 'projects': self.projects},
        }
        for wave_index, wave in enumerate(self.waves):
            for task_id in wave:
                index = self.ids.index(task_id)
                dependencies = []
                self.context['tasks'][task_id] = {
                    'id': task_id, 'wave': wave_index, 'depends_on': dependencies,
                    'manifest_sha256': f'{index + 1:064x}',
                    'archive': task_id + '.tar.zst', 'archive_bytes': 100,
                    'archive_sha256': f'{index + 1:064x}',
                }
        image_owner, kernel_owner = self.ids[0], self.ids[1]
        required = [self.out / 'soong/build_number.txt', self.product / 'build_fingerprint.txt',
                    self.product / 'obj/PACKAGING/system_build_prop_intermediates/buildinfo.prop',
                    *(self.product / (name + '.img') for name in ('boot', 'recovery', 'system', 'vendor'))]
        kernel_paths = [self.kernel, self.kernel / 'arch/arm64/boot/Image', self.kernel / 'vmlinux',
                        self.kernel / 'System.map', self.kernel / '.config',
                        *(self.qcom / (name + '.dtb') for name in
                          ('sm8150', 'sm8150-v2', 'sm8150p', 'sm8150p-v2')),
                        self.qcom / 'crux-sm8150-overlay.dtbo']
        self.context['assembly_exports'] = {str(path): image_owner for path in required}
        self.context['assembly_exports'].update({str(path): kernel_owner for path in kernel_paths})
        self.image_owner, self.kernel_owner = image_owner, kernel_owner

    def tearDown(self):
        self.temporary.cleanup()

    def test_identity_comes_from_graph_and_capsule_metadata_not_image_bytes(self):
        identity = cloud_assemble.derive_identity(self.context)
        self.assertEqual(identity['required_buildinfo']['ro.build.date.utc'], '1793724452')
        self.assertEqual(identity['required_buildinfo']['ro.build.date'], 'Tue Nov  3 16:47:32 UTC 2026')
        self.assertEqual(identity['required_buildinfo']['ro.build.description'],
                         'aosp_crux-userdebug 13 TQ3A.230901.001.B1 2026110303 release-keys')
        self.assertEqual(identity['branding_keys']['org.pixelexperience.build_date'], '20261103-1712')
        self.assertNotEqual(identity['required_buildinfo']['ro.build.date'],
                            'Tue Nov  3 17:12:00 UTC 2026')

    def test_branding_timestamp_is_validated_independently_of_graph_build_date(self):
        identity = cloud_assemble.derive_identity(self.context)
        self.assertEqual(identity['required_buildinfo']['ro.build.date.utc'], '1793724452')
        self.assertEqual(identity['branding_keys']['org.pixelexperience.build_date_utc'], '1793725920')
        bad = json.loads(json.dumps(self.context))
        bad['frontend']['branding_keys']['org.pixelexperience.build_date'] = '20261103-1647'
        with self.assertRaisesRegex(cloud_assemble.AssembleError, 'own UTC literal'):
            cloud_assemble.derive_identity(bad)

    def test_stale_identity_or_unadmitted_build_date_is_rejected(self):
        stale = json.loads(json.dumps(self.context))
        stale['frontend']['graph_receipt']['build_number'] = '2026101105'
        with self.assertRaisesRegex(cloud_assemble.AssembleError, 'incomplete or inconsistent'):
            cloud_assemble.derive_identity(stale)
        no_date = json.loads(json.dumps(self.context))
        del no_date['frontend']['build_date']
        with self.assertRaisesRegex(cloud_assemble.AssembleError, 'build_date scalar'):
            cloud_assemble.derive_identity(no_date)

    def test_only_known_ninja_shell_escaped_scalar_reads_are_normalized(self):
        escaped = json.loads(json.dumps(self.context))
        environment = escaped['frontend']['buildinfo_environment']
        environment['BUILD_NUMBER'] = r'\$(cat OUT/soong/build_number.txt)'
        environment['PRIVATE_BUILD_DESC'] = (
            r'aosp_crux-userdebug 13 TQ3A.230901.001.B1 \$(cat OUT/soong/build_number.txt) release-keys')
        environment['DATE'] = r'date -d @\$(cat OUT/build_date.txt)'
        identity = cloud_assemble.derive_identity(escaped)
        self.assertEqual(identity['required_buildinfo']['ro.build.version.incremental'], '2026110303')
        environment['DATE'] = r'date -d @\$(cat OUT/other_date.txt)'
        with self.assertRaisesRegex(cloud_assemble.AssembleError, 'scalar expressions'):
            cloud_assemble.derive_identity(escaped)

    def _receipt(self, task_id, outputs):
        task = self.context['tasks'][task_id]
        expected_dependencies = [cloud_assemble._binding(self.context, dep) for dep in task['depends_on']]
        return {'schema_version': 1, 'status': 'success', 'id': task_id, 'run_id': '9001',
                'worker_commit': self.worker_commit, 'manifest_sha256': task['manifest_sha256'],
                'fresh_build': cloud_assemble._fresh_binding(self.context),
                'declared_dependencies': expected_dependencies,
                'build_environment': {'CCACHE_DISABLE': '1'}, 'archive_sha256': 'e' * 64,
                'outputs': outputs}

    def test_diagnostic_receipt_is_not_an_assembly_producer(self):
        task_id = self.ids[0]
        receipt = self._receipt(task_id, [])
        receipt['diagnostic_only'] = True
        with self.assertRaisesRegex(cloud_assemble.AssembleError, 'Diagnostic output'):
            cloud_assemble._validate_receipt(self.context, task_id, receipt)

    def test_admits_all_fresh_receipts_and_only_plan_owned_output_artifacts(self):
        for path in list(self.context['assembly_exports']):
            if path.startswith(str(self.kernel) + '/'):
                del self.context['assembly_exports'][path]
        exports, _ = cloud_assemble.required_exports(self.context)
        owner_outputs = {self.image_owner: [], self.kernel_owner: []}
        for path, owner in exports.items():
            kind = 'directory' if path.endswith('/obj/KERNEL_OBJ') else 'file'
            owner_outputs[owner].append({'path': path, 'type': kind, 'mode': 0o755 if kind == 'directory' else 0o644,
                                         'size': 0 if kind == 'directory' else 12,
                                         **({'sha256': 'f' * 64} if kind == 'file' else {})})
        overlay = str(self.qcom / 'crux-sm8150-overlay.dtbo')
        owner_outputs[self.kernel_owner].append({'path': overlay, 'type': 'file', 'mode': 0o644,
                                                 'size': 12, 'sha256': 'f' * 64})
        receipts = {task_id: self._receipt(task_id, owner_outputs.get(task_id, [])) for task_id in self.ids}
        downloads = []
        def downloader(run_id, repository, artifact, destination):
            downloads.append(artifact)
            destination.mkdir(parents=True)
            task_id = artifact.removeprefix('fresh-receipt-').removeprefix('shard-')
            (destination / 'receipt.json').write_text(json.dumps(receipts[task_id]))
        bindings, dependency_root = cloud_assemble.admit_producers(
            self.context, self.root / 'staging', downloader, self.manifest_path)
        self.assertEqual(len(bindings), 2)
        self.assertEqual({item['id'] for item in bindings}, {self.image_owner, self.kernel_owner})
        self.assertEqual(len([name for name in downloads if name.startswith('fresh-receipt-')]), 39)
        self.assertEqual(len([name for name in downloads if name.startswith('shard-')]), 2)
        self.assertTrue(dependency_root.is_dir())

    def test_missing_kernel_child_export_fails_closed(self):
        receipts = {task_id: self._receipt(task_id, []) for task_id in self.ids}
        for path, owner in self.context['assembly_exports'].items():
            if path == str(self.kernel / 'vmlinux'):
                continue
            kind = 'directory' if path.endswith('/obj/KERNEL_OBJ') else 'file'
            receipts[owner]['outputs'].append({'path': path, 'type': kind,
                'mode': 0o755 if kind == 'directory' else 0o644,
                'size': 0 if kind == 'directory' else 12,
                **({'sha256': 'f' * 64} if kind == 'file' else {})})
        def downloader(run_id, repository, artifact, destination):
            destination.mkdir(parents=True)
            task_id = artifact.removeprefix('fresh-receipt-').removeprefix('shard-')
            (destination / 'receipt.json').write_text(json.dumps(receipts[task_id]))
        with self.assertRaisesRegex(cloud_assemble.AssembleError, 'does not own path'):
            cloud_assemble.admit_producers(self.context, self.root / 'staging', downloader, self.manifest_path)

    def test_wrapper_calls_existing_assembler_with_derived_inputs(self):
        receipts = {task_id: self._receipt(task_id, []) for task_id in self.ids}
        for task_id, path in ((self.image_owner, str(self.product / 'boot.img')),
                              (self.kernel_owner, str(self.kernel))):
            receipts[task_id]['outputs'] = [{'path': candidate, 'type': 'directory' if candidate.endswith('/obj/KERNEL_OBJ') else 'file',
                                             'mode': 0o755 if candidate.endswith('/obj/KERNEL_OBJ') else 0o644,
                                             'size': 0 if candidate.endswith('/obj/KERNEL_OBJ') else 12,
                                             **({'sha256': 'f' * 64} if not candidate.endswith('/obj/KERNEL_OBJ') else {})}
                                            for candidate, owner in self.context['assembly_exports'].items()
                                            if owner == task_id]
        def downloader(run_id, repository, artifact, destination):
            destination.mkdir(parents=True)
            task_id = artifact.removeprefix('fresh-receipt-').removeprefix('shard-')
            (destination / 'receipt.json').write_text(json.dumps(receipts[task_id]))
        assembler = mock.Mock(return_value={'status': 'success', 'assets': {'boot.img': 'abc'}})
        context_path = self.root / 'context.json'
        context_path.write_text(json.dumps(self.context))
        result = cloud_assemble.assemble(context_path, self.root / 'out', self.root / 'staging',
                                        downloader, assembler, self.manifest_path)
        self.assertEqual(result['status'], 'success')
        args = assembler.call_args.args
        self.assertEqual(args[4], '2026110303')
        self.assertEqual(args[3].read_text(), json.dumps(cloud_assemble.derive_identity(self.context),
                                                         indent=2, sort_keys=True) + '\n')
        self.assertEqual(args[5]['source_manifest_sha256'], self.manifest_sha)
        self.assertEqual(assembler.call_args.kwargs['source_contract'], cloud_assemble.SOURCE_CONTRACT)
        self.assertEqual(args[5]['fit_source_contract']['sha256'],
                         hashlib.sha256(cloud_assemble.SOURCE_CONTRACT.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
