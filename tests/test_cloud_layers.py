import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from email.utils import formatdate
import unittest
import urllib.error
import zipfile
from unittest import mock


SPEC = importlib.util.spec_from_file_location('cloud_layers', Path(__file__).resolve().parents[1] / 'scripts/cloud_layers.py')
layers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(layers)


class CloudLayersTests(unittest.TestCase):
    @staticmethod
    def response(data, headers=None):
        class Response:
            def __init__(self, payload, response_headers):
                self.payload = payload
                self.headers = response_headers
                self.read_sizes = []
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size=-1):
                self.read_sizes.append(size)
                if size is None or size < 0:
                    size = len(self.payload)
                data, self.payload = self.payload[:size], self.payload[size:]
                return data
        return Response(data, headers or {})

    @staticmethod
    def artifact_zip(artifact):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            for path in Path(artifact).rglob('*'):
                if path.is_file():
                    archive.write(path, path.relative_to(artifact).as_posix())
        return data.getvalue()

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

            payload = self.artifact_zip(artifact)
            responses = []
            def download(request):
                self.assertIn('/actions/artifacts/321/zip', request.full_url)
                response = self.response(payload)
                responses.append(response)
                return response

            image, _ = layers.restore_layer('manifest', {'id': 'source-00', 'projects': ['build/soong']},
                                            {'build/soong': 'revision'}, '123', 'owner/repo', staging,
                                            {'id': 321, 'name': 'cloud-source-00'}, 'token', opener=download)
            self.assertEqual(image.read_bytes(), b'compressed image')
            self.assertFalse((staging / 'source-00/layer.sqfs.parts/part-00000').exists())
            self.assertTrue(responses[0].read_sizes)
            self.assertTrue(all(0 < size <= 8 * 1024 * 1024 for size in responses[0].read_sizes))

    def test_incorrect_source_revision_fails_even_with_matching_manifest_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def download(record, destination, token, repository, *args):
                (destination / 'layer.json').write_text(json.dumps({
                    'layer_id': 'source-00', 'manifest_sha256': 'manifest',
                    'projects': [{'path': 'build/soong', 'revision': 'old', 'status': 'fetched-and-extracted'}],
                }))

            with mock.patch.object(layers, '_extract_artifact', side_effect=download):
                with self.assertRaisesRegex(ValueError, 'Incomplete source layer'):
                    layers.restore_layer('manifest', {'id': 'source-00', 'projects': ['build/soong']},
                                         {'build/soong': 'new'}, '123', 'owner/repo', root,
                                         {'id': 321, 'name': 'cloud-source-00'}, 'token')

    def test_inventory_enumerates_all_artifacts_in_one_request_for_one_page(self):
        calls = []
        payload = json.dumps({'total_count': 2, 'artifacts': [
            {'id': 1, 'name': 'cloud-source-00', 'expired': False},
            {'id': 2, 'name': 'cloud-source-01', 'expired': False},
        ]}).encode()
        def opener(request):
            calls.append(request.full_url)
            return self.response(payload)
        found = layers.artifact_inventory('123', 'owner/repo', 'token', opener=opener)
        self.assertEqual(set(found), {'cloud-source-00', 'cloud-source-01'})
        self.assertEqual(len(calls), 1)
        self.assertIn('per_page=100&page=1', calls[0])

    def test_inventory_selects_unexpired_exact_artifact_and_downloads_by_id(self):
        files = []
        inventory_payload = json.dumps({'total_count': 2, 'artifacts': [
            {'id': 7, 'name': 'cloud-source-00', 'expired': False},
            {'id': 8, 'name': 'cloud-source-00-old', 'expired': True},
        ]}).encode()
        with tempfile.TemporaryDirectory() as temporary:
            artifact_dir = Path(temporary) / 'artifact'
            artifact_dir.mkdir()
            (artifact_dir / 'layer.json').write_text('{}')
            zip_bytes = self.artifact_zip(artifact_dir)
            def opener(request):
                files.append(request.full_url)
                return self.response(inventory_payload if '/runs/123/artifacts?' in request.full_url else zip_bytes)
            selected = layers.artifact_inventory('123', 'owner/repo', 'token', opener=opener)
            destination = Path(temporary) / 'download'
            destination.mkdir()
            layers._extract_artifact(selected['cloud-source-00'], destination, 'token', 'owner/repo', opener=opener)
            self.assertTrue((destination / 'layer.json').is_file())
            self.assertIn('/actions/artifacts/7/zip', files[-1])
            self.assertEqual(sum('/runs/123/artifacts?' in url for url in files), 1)

    def test_primary_rate_limit_waits_until_reset_and_retries(self):
        calls, waits = [], []
        error = urllib.error.HTTPError('https://api.github.com/test', 403, 'rate limit',
                                       {'X-RateLimit-Reset': '150'}, io.BytesIO(
                                           b'{"message":"API rate limit exceeded for token"}'))
        def opener(request):
            calls.append(request)
            if len(calls) == 1:
                raise error
            return self.response(b'{}')
        result, _ = layers._request('https://api.github.com/test', 'token', opener=opener,
                                    sleep=waits.append, now=lambda: 100)
        self.assertEqual(result, b'{}')
        self.assertEqual(waits, [51])
        self.assertEqual(len(calls), 2)

    def test_primary_429_with_past_reset_still_waits_before_retry(self):
        waits = []
        error = urllib.error.HTTPError('https://api.github.com/test', 429, 'limited',
                                       {'X-RateLimit-Reset': '99', 'X-RateLimit-Remaining': '0'},
                                       io.BytesIO(b'{"message":"rate limited"}'))
        opener = mock.Mock(side_effect=[error, self.response(b'{}')])
        layers._request('https://api.github.com/test', 'token', opener=opener,
                        sleep=waits.append, now=lambda: 100)
        self.assertEqual(waits, [1])

    def test_transient_http_statuses_use_bounded_exponential_backoff(self):
        calls, waits = [], []
        statuses = (503, 500, 502, 504)
        errors = [urllib.error.HTTPError('https://api.github.com/test', status, 'transient', {},
                                         io.BytesIO(b'<Error>ServerBusy</Error>'))
                  for status in statuses]
        def opener(request):
            calls.append(request)
            if len(calls) <= len(errors):
                raise errors[len(calls) - 1]
            return self.response(b'{}')
        result, _ = layers._request('https://api.github.com/test', 'token', opener=opener,
                                    sleep=waits.append, now=lambda: 100)
        self.assertEqual(result, b'{}')
        self.assertEqual(waits, [1, 2, 4, 8])
        self.assertEqual(len(calls), 5)

    def test_secondary_429_retry_after_seconds_takes_precedence(self):
        waits, calls = [], []
        error = urllib.error.HTTPError('https://api.github.com/test', 429, 'secondary limit',
                                       {'Retry-After': '17', 'X-RateLimit-Remaining': '9'},
                                       io.BytesIO(b'{"message":"secondary rate limit"}'))
        def opener(request):
            calls.append(request)
            if len(calls) == 1:
                raise error
            return self.response(b'{}')
        result, _ = layers._request('https://api.github.com/test', 'token', opener=opener,
                                    sleep=waits.append, now=lambda: 100)
        self.assertEqual(result, b'{}')
        self.assertEqual(waits, [17])
        self.assertEqual(len(calls), 2)

    def test_transient_http_date_retry_after_uses_injected_clock(self):
        waits = []
        error = urllib.error.HTTPError('https://api.github.com/test', 503, 'busy',
                                       {'Retry-After': formatdate(111, usegmt=True)},
                                       io.BytesIO(b'<Error>ServerBusy</Error>'))
        opener = mock.Mock(side_effect=[error, self.response(b'{}')])
        layers._request('https://api.github.com/test', 'token', opener=opener,
                        sleep=waits.append, now=lambda: 100)
        self.assertEqual(waits, [11])
        self.assertEqual(opener.call_count, 2)

    def test_transient_http_retries_exhaust_then_fail(self):
        waits, calls = [], []
        error = urllib.error.HTTPError('https://api.github.com/test', 503, 'busy', {},
                                       io.BytesIO(b'<Error>ServerBusy</Error>'))
        def opener(request):
            calls.append(request)
            raise error
        with self.assertRaisesRegex(RuntimeError, '503.*after 5 transient retries'):
            layers._request('https://api.github.com/test', 'token', opener=opener,
                            sleep=waits.append, now=lambda: 100)
        self.assertEqual(waits, [1, 2, 4, 8, 16])
        self.assertEqual(len(calls), 6)

    def test_inventory_paginates_without_relisting_for_each_layer(self):
        records = [{'id': index + 1, 'name': 'artifact-' + str(index), 'expired': False}
                   for index in range(101)]
        requests = []
        def opener(request):
            requests.append(request.full_url)
            page = records[:100] if len(requests) == 1 else records[100:]
            return self.response(json.dumps({'total_count': 101, 'artifacts': page}).encode())
        found = layers.artifact_inventory('123', 'owner/repo', 'token', opener=opener)
        self.assertEqual(len(found), 101)
        self.assertEqual(len(requests), 2)
        self.assertIn('page=2', requests[-1])

    def test_inventory_relists_when_concurrent_upload_shifts_a_page_boundary(self):
        records = [{'id': i, 'name': 'artifact-' + str(i), 'expired': False}
                   for i in range(1, 102)]
        newer = {'id': 102, 'name': 'new-upload', 'expired': False}
        current = [newer, *records]
        replies = [
            {'total_count': 101, 'artifacts': records[:100]},
            {'total_count': 102, 'artifacts': current[100:]},
            {'total_count': 102, 'artifacts': current[:100]},
            {'total_count': 102, 'artifacts': current[100:]},
        ]
        requests, waits = [], []
        def opener(request):
            requests.append(request.full_url)
            return self.response(json.dumps(replies.pop(0)).encode())
        found = layers.artifact_inventory('123', 'owner/repo', 'token', opener=opener, sleep=waits.append)
        self.assertEqual(len(found), 102)
        self.assertEqual(found['artifact-100']['id'], 100)
        self.assertIn('new-upload', found)
        self.assertEqual(waits, [1])
        self.assertEqual([url.split('page=')[-1] for url in requests], ['1', '2', '1', '2'])

    def test_inventory_rejects_different_artifact_ids_with_one_name(self):
        payload = {'total_count': 2, 'artifacts': [
            {'id': 1, 'name': 'selected', 'expired': False},
            {'id': 2, 'name': 'selected', 'expired': False}]}
        with self.assertRaisesRegex(ValueError, 'Duplicate artifact name'):
            layers.artifact_inventory('123', 'owner/repo', 'token',
                                     opener=lambda _: self.response(json.dumps(payload).encode()),
                                     sleep=lambda _: self.fail('a real ambiguity must not be retried away'))

    def test_inventory_rejects_conflicting_observations_of_one_artifact_id(self):
        payload = {'total_count': 2, 'artifacts': [
            {'id': 1, 'name': 'selected', 'digest': 'sha256:first', 'expired': False},
            {'id': 1, 'name': 'selected', 'digest': 'sha256:changed', 'expired': False}]}
        with self.assertRaisesRegex(ValueError, 'Conflicting artifact metadata'):
            layers.artifact_inventory('123', 'owner/repo', 'token',
                                     opener=lambda _: self.response(json.dumps(payload).encode()))

    def test_inventory_does_not_return_an_unstable_or_truncated_snapshot(self):
        same = {'id': 1, 'name': 'selected', 'expired': False}
        waits = []
        with self.assertRaisesRegex(ValueError, 'did not stabilize'):
            layers.artifact_inventory('123', 'owner/repo', 'token', sleep=waits.append,
                                     opener=lambda _: self.response(json.dumps(
                                         {'total_count': 2, 'artifacts': [same, same]}).encode()))
        self.assertEqual(waits, [1, 2, 4, 8, 16])
        replies = [{'total_count': 2, 'artifacts': [same]}, {'total_count': 2, 'artifacts': []}]
        with self.assertRaisesRegex(ValueError, 'ended before total_count'):
            layers.artifact_inventory('123', 'owner/repo', 'token',
                                     opener=lambda _: self.response(json.dumps(replies.pop(0)).encode()),
                                     sleep=lambda _: self.fail('truncation is not a valid inventory'))

    def test_non_rate_limit_api_error_is_not_retried(self):
        for status, message in ((403, 'Resource not accessible by integration'), (404, 'Not Found')):
            calls = []
            error = urllib.error.HTTPError('https://api.github.com/test', status, 'error', {},
                                           io.BytesIO(json.dumps({'message': message}).encode()))
            def opener(request):
                calls.append(request)
                raise error
            with self.subTest(status=status):
                with self.assertRaisesRegex(RuntimeError, str(status) + '.*' + message):
                    layers._request('https://api.github.com/test', 'token', opener=opener,
                                    sleep=lambda _: self.fail('must not sleep'))
                self.assertEqual(len(calls), 1)

    def test_artifact_blob_redirect_does_not_forward_github_credentials(self):
        request = layers.urllib.request.Request('https://api.github.com/artifact',
                                                 headers={'Authorization': 'Bearer token'})
        handler = layers._SafeRedirect()
        blob = handler.redirect_request(request, None, 302, '', {}, 'https://blob.example/artifact')
        self.assertFalse(blob.has_header('Authorization'))
        same_origin = handler.redirect_request(request, None, 302, '', {}, 'https://api.github.com/next')
        self.assertEqual(same_origin.get_header('Authorization'), 'Bearer token')

    def test_main_reaches_mount_commands_with_subprocess_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / 'manifest.json'
            source = root / 'empty-source'
            manifest.write_text(json.dumps({'source_root': str(source), 'projects': [], 'layers': []}))
            staging, evidence = root / 'staging', root / 'evidence'
            argv = ['cloud_layers.py', '--manifest', str(manifest), '--source-run', '123',
                    '--repository', 'owner/repo', '--staging', str(staging), '--evidence', str(evidence)]
            calls = []
            with (mock.patch.object(sys, 'argv', argv),
                  mock.patch.dict(os.environ, {'GH_TOKEN': 'test-token'}),
                  mock.patch.object(layers, 'artifact_inventory', return_value={}),
                  mock.patch.object(layers.subprocess, 'run', side_effect=lambda command, **kwargs: calls.append(command))):
                layers.main()
            self.assertTrue(any(command[:3] == ['sudo', 'mount', '-t'] for command in calls))
            self.assertTrue(any(command[0] == 'findmnt' for command in calls))


if __name__ == '__main__':
    unittest.main()
