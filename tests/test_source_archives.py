import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
source_archives = load('source_archives', SCRIPTS / 'source_archives.py')


class SelectArchivesTests(unittest.TestCase):
    """Selection binds a capsule's frozen inputs to the pinned archives that can
    restore them. Every shard also carries its own `.crux-task/graph` runtime and
    slice files, which differ per shard by design and are restorable from nothing;
    including them made a full-set restore fail on conflicting identities."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'pe13'
        self.install = self.source / 'prebuilts/tools'
        (self.install / 'bin').mkdir(parents=True)
        (self.install / 'bin/tool').write_text('tool')
        self.profile = {'schema_version': 1, 'source_root': str(self.source),
                        'archives': [{'id': 'tools', 'install_path': str(self.install),
                                      'url': 'https://example.invalid/' + 'a' * 40 + '/archive.tar.gz',
                                      'revision': 'a' * 40, 'strip_components': 0}]}

    def tearDown(self):
        self.temporary.cleanup()

    def bundle(self, name, extra=None):
        files = [{'path': str(self.install / 'bin/tool'), 'type': 'file', 'mode': 0o755,
                  'size': 4, 'sha256': source_archives.digest(self.install / 'bin/tool')}]
        if extra:
            files.append(extra)
        return {'schema_version': 1, 'source_root': str(self.source), 'files': files}

    def test_runtime_files_of_several_shards_do_not_conflict(self):
        marker = self.source / '.crux-task/graph/file-2.ninja'
        marker.parent.mkdir(parents=True)
        bundles = []
        for index, body in enumerate(('first shard slice', 'second shard slice')):
            marker.write_text(body)
            bundles.append(self.bundle('shard-%d' % index,
                                       {'path': str(marker), 'type': 'file', 'mode': 0o644,
                                        'size': len(body),
                                        'sha256': source_archives.digest(marker)}))
        root, selected = source_archives.select_archives(self.profile, bundles)
        self.assertEqual(root, self.source)
        self.assertEqual(len(selected), 1)
        self.assertEqual([spec['path'] for spec in selected[0][1]], [str(self.install / 'bin/tool')])

    def test_conflicting_source_identity_is_still_refused(self):
        other = self.install / 'bin/tool'
        bundles = [self.bundle('a')]
        conflicting = self.bundle('b')
        conflicting['files'][0] = {**conflicting['files'][0], 'sha256': 'b' * 64}
        bundles.append(conflicting)
        with self.assertRaises(source_archives.ArchiveError):
            source_archives.select_archives(self.profile, bundles)


if __name__ == '__main__':
    unittest.main()
