import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location('cloud_layers', Path(__file__).resolve().parents[1] / 'scripts/cloud_layers.py')
layers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(layers)


class CloudLayersTests(unittest.TestCase):
    def test_restore_checks_pinned_projects_and_part_identity_before_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / 'artifact'
            parts = artifact / 'layer.sqfs.parts'
            parts.mkdir(parents=True)
            (parts / 'part-00000').write_bytes(b'compressed image')
            receipt = {
                'layer_id': 'source-00', 'manifest_sha256': 'manifest',
                'projects': [{'path': 'build/soong', 'revision': 'revision', 'status': 'fetched-and-extracted'}],
                'squashfs': {'parts_directory': 'layer.sqfs.parts', 'size': 16,
                             'parts': [{'name': 'part-00000', 'size': 16,
                                        'sha256': layers.digest(parts / 'part-00000')}]},
            }
            (artifact / 'layer.json').write_text(json.dumps(receipt))
            staging = root / 'stage'
            staging.mkdir()

            def download(command, check):
                shutil.copytree(artifact, Path(command[-1]), dirs_exist_ok=True)

            with mock.patch.object(layers.subprocess, 'run', side_effect=download):
                image, _ = layers.restore_layer('manifest', {'id': 'source-00', 'projects': ['build/soong']},
                                                {'build/soong': 'revision'}, '123', 'owner/repo', staging)
                self.assertEqual(image.read_bytes(), b'compressed image')
                self.assertFalse((staging / 'source-00/layer.sqfs.parts/part-00000').exists())

    def test_incorrect_source_revision_fails_even_with_matching_manifest_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def download(command, check):
                destination = Path(command[-1])
                (destination / 'layer.json').write_text(json.dumps({
                    'layer_id': 'source-00', 'manifest_sha256': 'manifest',
                    'projects': [{'path': 'build/soong', 'revision': 'old', 'status': 'fetched-and-extracted'}],
                }))

            with mock.patch.object(layers.subprocess, 'run', side_effect=download):
                with self.assertRaisesRegex(ValueError, 'Incomplete source layer'):
                    layers.restore_layer('manifest', {'id': 'source-00', 'projects': ['build/soong']},
                                         {'build/soong': 'new'}, '123', 'owner/repo', root)


if __name__ == '__main__':
    unittest.main()
