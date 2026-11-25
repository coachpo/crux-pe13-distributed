#!/usr/bin/env python3
"""Restore and mount complete source layers from a standard-runner source run."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


API = 'https://api.github.com'


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header('Authorization')
            redirected.remove_header('Cookie')
        return redirected


_API_OPENER = urllib.request.build_opener(_SafeRedirect()).open
_TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
_TRANSIENT_RETRIES = 5
_TRANSIENT_BACKOFF_MAX = 30
_ARTIFACT_BODY_RETRIES = 2


def _retry_after_seconds(headers, now):
    value = headers.get('Retry-After')
    if not value:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0, retry_at.timestamp() - now())
        except (TypeError, ValueError, OverflowError):
            return None


def _open_retry(request, opener=_API_OPENER, sleep=time.sleep, now=time.time):
    transient_retries = 0
    while True:
        try:
            return opener(request)
        except urllib.error.HTTPError as error:
            body = error.read()
            try:
                message = json.loads(body).get('message', '')
            except (ValueError, AttributeError):
                message = ''
            reset = error.headers.get('X-RateLimit-Reset')
            primary = (error.headers.get('X-RateLimit-Remaining') == '0'
                       or 'API rate limit exceeded' in message)
            if error.code in (403, 429) and primary and reset:
                delay = max(1, int(reset) - int(now()) + 1)
                print(f'GitHub API primary rate limit reached; waiting {delay}s for reset', flush=True)
                sleep(delay)
                continue
            if error.code in _TRANSIENT_HTTP_CODES:
                if transient_retries >= _TRANSIENT_RETRIES:
                    raise RuntimeError(
                        f'GitHub API request failed ({error.code}) after {_TRANSIENT_RETRIES} transient retries: '
                        f'{message or body.decode(errors="replace")[:1000]}') from error
                delay = _retry_after_seconds(error.headers, now)
                if delay is None:
                    delay = min(2 ** transient_retries, _TRANSIENT_BACKOFF_MAX)
                transient_retries += 1
                print(f'GitHub API transient HTTP {error.code}; retry {transient_retries}/'
                      f'{_TRANSIENT_RETRIES} in {delay:g}s', flush=True)
                sleep(delay)
                continue
            raise RuntimeError(f'GitHub API request failed ({error.code}): {message or body.decode(errors="replace")[:1000]}') from error
        except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
            # Every caller sends an idempotent GET, so a connection dropped or
            # refused before any response shares the 5xx retry budget.
            reason = getattr(error, 'reason', error)
            if not isinstance(reason, (ConnectionError, TimeoutError, socket.gaierror)):
                raise
            if transient_retries >= _TRANSIENT_RETRIES:
                raise RuntimeError(f'GitHub API connection failed ({type(reason).__name__}) '
                                   f'after {_TRANSIENT_RETRIES} transient retries') from error
            delay = min(2 ** transient_retries, _TRANSIENT_BACKOFF_MAX)
            transient_retries += 1
            print(f'GitHub API connection failed ({type(reason).__name__}); retry {transient_retries}/'
                  f'{_TRANSIENT_RETRIES} in {delay:g}s', flush=True)
            sleep(delay)
            continue


def _request(url, token, opener=_API_OPENER, sleep=time.sleep, now=time.time):
    request = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'Authorization': 'Bearer ' + token,
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'crux-cloud-layers',
    })
    with _open_retry(request, opener, sleep, now) as response:
        return response.read(), response.headers


def artifact_inventory(run_id, repository, token, opener=_API_OPENER,
                       sleep=time.sleep, now=time.time):
    if not str(run_id).isdecimal() or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid GitHub source run or repository')
    # Offset pages are not an atomic snapshot while peer jobs upload. A new
    # artifact can shift the same ID onto two pages, and total_count can count
    # artifacts the pages do not list yet. Relist instead of accepting an
    # incomplete/overlapping view or picking a newest name. Distinct IDs sharing
    # a name still fail closed immediately.
    for attempt in range(_TRANSIENT_RETRIES + 1):
        records, identities, names = [], {}, {}
        page, total, changed, truncated = 1, None, False, False
        while total is None or len(records) < total:
            query = urllib.parse.urlencode({'per_page': 100, 'page': page})
            url = f'{API}/repos/{repository}/actions/runs/{run_id}/artifacts?{query}'
            payload, _ = _request(url, token, opener, sleep, now)
            response = json.loads(payload)
            if (not isinstance(response.get('artifacts'), list)
                    or type(response.get('total_count')) is not int or response['total_count'] < 0):
                raise ValueError('GitHub artifact inventory response is malformed')
            if total is not None and total != response['total_count']:
                changed = True
            total = response['total_count']
            for artifact in response['artifacts']:
                if not isinstance(artifact, dict):
                    raise ValueError('GitHub artifact inventory entry is malformed')
                identity, name = artifact.get('id'), artifact.get('name')
                if isinstance(identity, int):
                    if identity in identities:
                        if identities[identity] != artifact:
                            raise ValueError('Conflicting artifact metadata for ID: ' + str(identity))
                        changed = True
                    identities[identity] = artifact
                    if isinstance(name, str) and not artifact.get('expired'):
                        if name in names and names[name] != identity:
                            raise ValueError('Duplicate artifact name in selected run: ' + name)
                        names[name] = identity
                records.append(artifact)
            page += 1
            if not response['artifacts'] and len(records) < total:
                changed = truncated = True
                break
        if not changed:
            break
        if attempt == _TRANSIENT_RETRIES:
            if truncated:
                raise ValueError('GitHub artifact inventory ended before total_count after pagination retries')
            raise ValueError('GitHub artifact inventory did not stabilize after pagination retries')
        sleep(min(2 ** attempt, _TRANSIENT_BACKOFF_MAX))
    result = {}
    for artifact in records:
        name, artifact_id = artifact.get('name'), artifact.get('id')
        if not isinstance(name, str) or not isinstance(artifact_id, int) or artifact.get('expired'):
            continue
        if name in result:
            raise ValueError('Duplicate artifact name in selected run: ' + name)
        result[name] = artifact
    return result


def _extract_artifact(artifact, destination, token, repository, opener=_API_OPENER,
                      sleep=time.sleep, now=time.time):
    url = f'{API}/repos/{repository}/actions/artifacts/{artifact["id"]}/zip'
    request = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'Authorization': 'Bearer ' + token,
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'crux-cloud-layers',
    })
    for attempt in range(_ARTIFACT_BODY_RETRIES + 1):
        with tempfile.NamedTemporaryFile(prefix='cloud-artifact-', suffix='.zip',
                                         dir=destination) as archive_file:
            try:
                with _open_retry(request, opener, sleep, now) as response:
                    shutil.copyfileobj(response, archive_file, 8 * 1024 * 1024)
                archive_file.flush()
                archive = zipfile.ZipFile(archive_file)
            except (zipfile.BadZipFile, http.client.IncompleteRead, ConnectionError, TimeoutError) as error:
                failure = type(error).__name__
                if attempt == _ARTIFACT_BODY_RETRIES:
                    raise RuntimeError(f'GitHub artifact {artifact["id"]} download body failed ({failure}) '
                                       f'after {_ARTIFACT_BODY_RETRIES} retries') from error
            else:
                # Only retry before writing any members, so validation errors
                # never get retried over a partially extracted destination.
                with archive:
                    for member in archive.infolist():
                        name = Path(member.filename)
                        if name.is_absolute() or '..' in name.parts:
                            raise ValueError('Unsafe artifact member path: ' + member.filename)
                        target = destination / name
                        if member.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(member) as source, target.open('wb') as output:
                                shutil.copyfileobj(source, output, 8 * 1024 * 1024)
                return
        delay = 2 ** attempt
        print(f'GitHub artifact {artifact["id"]} download body failed ({failure}); '
              f'retry {attempt + 1}/{_ARTIFACT_BODY_RETRIES} in {delay}s', flush=True)
        sleep(delay)


def restore_layer(manifest_sha, layer, revisions, run_id, repository, staging, artifact,
                  token, opener=_API_OPENER, sleep=time.sleep, now=time.time):
    name = layer['id']
    directory = staging / name
    directory.mkdir()
    if artifact.get('name') != 'cloud-' + name or artifact.get('expired'):
        raise ValueError('Selected GitHub artifact identity differs from requested layer: ' + name)
    _extract_artifact(artifact, directory, token, repository, opener, sleep, now)
    image = staging / (name + '.sqfs')
    receipt_path = assemble_layer(manifest_sha, layer, revisions, directory, image, consume_parts=True)
    print(json.dumps({'layer': name, 'bytes': image.stat().st_size, 'restored': True}), flush=True)
    return image, receipt_path


def assemble_layer(manifest_sha, layer, revisions, directory, image, *, consume_parts=False):
    name = layer['id']
    receipt_path = directory / 'layer.json'
    receipt = json.loads(receipt_path.read_text())
    if receipt['layer_id'] != name or receipt['manifest_sha256'] != manifest_sha:
        raise ValueError('Source layer identity differs from the pinned manifest: ' + name)
    expected = set(layer['projects'])
    projects = receipt['projects']
    successes = {'fetched-and-extracted', 'fetched-bundle-and-archived', 'fetched-git-and-archived'}
    if (len(projects) != len(expected) or {item['path'] for item in projects} != expected
            or any(item['status'] not in successes or item['revision'] != revisions[item['path']]
                   for item in projects)):
        raise ValueError('Incomplete source layer: ' + name)
    def basename(value):
        if not isinstance(value, str) or value in {'', '.', '..'} or Path(value).name != value:
            raise ValueError('Invalid source layer part path: ' + str(value))
        return value

    parts = directory / basename(receipt['squashfs']['parts_directory'])
    if parts.is_symlink():
        raise ValueError('Source layer parts directory must not be a symlink: ' + str(parts))
    with image.open('xb') as output:
        seen = set()
        for part in receipt['squashfs']['parts']:
            source = parts / basename(part['name'])
            if (source in seen or source.is_symlink() or not source.is_file()
                    or source.stat().st_size != part['size'] or digest(source) != part['sha256']):
                raise ValueError('Source image part failed identity check: ' + str(source))
            seen.add(source)
            with source.open('rb') as stream:
                shutil.copyfileobj(stream, output, 8 * 1024 * 1024)
            if consume_parts:
                source.unlink()
    if image.stat().st_size != receipt['squashfs']['size']:
        raise ValueError('Reassembled source image has incorrect size: ' + name)
    return receipt_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--source-run', required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--staging', required=True)
    parser.add_argument('--evidence', required=True)
    args = parser.parse_args()
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        raise ValueError('GH_TOKEN or GITHUB_TOKEN is required to restore source artifacts')
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text())
    manifest_sha = digest(manifest_path)
    revisions = {project['path']: project['revision'] for project in manifest['projects']}
    staging = Path(args.staging).resolve()
    staging.mkdir(parents=True, exist_ok=False)
    evidence = Path(args.evidence).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    source = Path(manifest['source_root'])
    if source.exists() and any(source.iterdir()):
        raise ValueError('Source mount point must be empty: ' + str(source))
    artifacts = artifact_inventory(args.source_run, args.repository, token)
    selected = {}
    for layer in manifest['layers']:
        name = 'cloud-' + layer['id']
        artifact = artifacts.get(name)
        if artifact is None:
            raise ValueError('Source run has no unexpired artifact: ' + name)
        selected[layer['id']] = artifact
    with ThreadPoolExecutor(max_workers=6) as pool:
        outputs = list(pool.map(lambda layer: restore_layer(manifest_sha, layer, revisions, args.source_run,
                                                           args.repository, staging, selected[layer['id']], token),
                                 manifest['layers']))
    mounts = []
    for image, receipt in outputs:
        destination = staging / (image.stem + '-mount')
        destination.mkdir()
        subprocess.run(['sudo', 'mount', '-t', 'squashfs', '-o', 'loop,ro', str(image), str(destination)], check=True)
        mounts.append(destination)
        shutil.copyfile(receipt, evidence / (image.stem + '.json'))
    upper = staging / 'upper'
    work = staging / 'work'
    upper.mkdir()
    work.mkdir()
    subprocess.run(['sudo', 'mkdir', '-p', str(source)], check=True)
    options = 'lowerdir=' + ':'.join(str(item) for item in reversed(mounts))
    options += ',upperdir=' + str(upper) + ',workdir=' + str(work)
    subprocess.run(['sudo', 'mount', '-t', 'overlay', 'overlay', '-o', options, str(source)], check=True)
    uid_gid = f'{os.getuid()}:{os.getgid()}'
    # The one editable host launcher is copied into the writable overlay by chown.
    subprocess.run(['sudo', 'chown', uid_gid, str(source),
                    str(source / 'build/soong/ui/build/soong.go')], check=True)
    subprocess.run(['findmnt', '-R', str(source)], check=True)
    receipt = {'schema_version': 1, 'source_run': args.source_run,
               'manifest_sha256': manifest_sha, 'source_root': str(source),
               'layer_count': len(outputs), 'images_bytes': sum(image.stat().st_size for image, _ in outputs),
               'mounts': [str(item) for item in mounts], 'writable_overlay': str(upper)}
    (evidence / 'source-mount.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
