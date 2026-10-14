#!/usr/bin/env python3
"""Read-only APK/APEX binary signature acceptance for a signed target-files ZIP.

Expected identities come from the reviewed config + exported public material.
Original metadata only routes shipping packages to their expected key mapping.
"""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import ssl
import struct
import subprocess
import tempfile
import zipfile


class VerificationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_member(archive, name):
    digest = hashlib.sha256()
    with archive.open(name) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def certificate_fingerprint(path):
    path = Path(path)
    require(path.name.endswith('.x509.pem'), 'Only explicit .x509.pem certificates are accepted')
    pem = path.read_text()
    require(pem.lstrip().startswith('-----BEGIN CERTIFICATE-----'), 'Expected a public X.509 certificate')
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def parse_signers(output):
    pattern = re.compile(r'^Signer (?:#\d+|\(minSdkVersion=[^\n]*\)) certificate SHA-256 digest: ([0-9a-fA-F]{64})$', re.MULTILINE)
    fingerprints = sorted(set(value.lower() for value in pattern.findall(output)))
    require(fingerprints, 'Verifier did not report any signer certificate')
    require(re.search(r'^Verifies$', output, re.MULTILINE), 'Verifier did not confirm the binary signature')
    return fingerprints


class HostTools:
    def __init__(self, host, java_home, sdk=33):
        self.host = Path(host)
        self.java = Path(java_home) / 'bin/java'
        self.sdk = sdk
        self.commands = []
        self.environment = dict(os.environ)
        self.environment.pop('ANDROID_SECURE_STORAGE_CMD', None)
        self.environment['JAVA_HOME'] = str(java_home)
        self.environment['PATH'] = str(Path(java_home) / 'bin') + os.pathsep + str(self.host / 'bin') + os.pathsep + self.environment.get('PATH', '')
        self.environment['LD_LIBRARY_PATH'] = str(self.host / 'lib64') + os.pathsep + self.environment.get('LD_LIBRARY_PATH', '')
        for path in (self.java, self.host / 'framework/apksigner.jar', self.host / 'bin/apksigner', self.host / 'bin/deapexer', self.host / 'bin/debugfs_static', self.host / 'bin/avbtool'):
            require(path.is_file(), 'Missing matching host tool: ' + str(path))

    def run(self, argv):
        process = subprocess.run([str(value) for value in argv], env=self.environment, text=True, capture_output=True)
        record = {'argv': [str(value) for value in argv], 'exit_code': process.returncode,
                  'stdout': process.stdout, 'stderr': process.stderr}
        self.commands.append(record)
        require(process.returncode == 0, 'Command failed (exit %d): %s\n%s' % (process.returncode, shlex.join(record['argv']), process.stderr.strip()))
        return process.stdout

    def apk_signers(self, path):
        output = self.run([self.java, '-Djava.library.path=' + str(self.host / 'lib64'),
                           '-jar', self.host / 'framework/apksigner.jar', 'verify', '--verbose', '--print-certs',
                           '--min-sdk-version', str(self.sdk), '--max-sdk-version', str(self.sdk), '--in', path])
        return parse_signers(output)

    def uncompressed_apex(self, path, scratch):
        command = [self.host / 'bin/deapexer', '--debugfs_path', self.host / 'bin/debugfs_static']
        kind = self.run(command + ['info', '--print-type', path]).strip()
        require(kind in ('COMPRESSED', 'UNCOMPRESSED'), 'Unsupported APEX type: ' + kind)
        if kind == 'UNCOMPRESSED':
            return Path(path), kind
        destination = Path(scratch) / 'original.apex'
        self.run(command + ['decompress', '--input', path, '--output', destination])
        return destination, kind

    def verify_payload(self, image, public_pem=None):
        command = [self.host / 'bin/avbtool', 'verify_image', '--image', image]
        if public_pem is not None:
            command += ['--key', public_pem]
        self.run(command)

    def manifest(self, apex):
        return json.loads(self.run([self.host / 'bin/deapexer', '--debugfs_path',
                                   self.host / 'bin/debugfs_static', 'info', apex]))

    def payload_root_digest(self, image):
        output = self.run([self.host / 'bin/avbtool', 'print_partition_digests', '--image', image])
        digests = re.findall(r'^[^:\n]*: ([0-9a-fA-F]+)$', output, re.MULTILINE)
        require(len(digests) == 1, 'Expected one APEX payload AVB root digest')
        return digests[0]

    def encoded_public_key(self, public_pem, destination):
        # The public-material resolver has already checked the public-only PEM header.
        self.run([self.host / 'bin/avbtool', 'extract_public_key', '--key', public_pem, '--output', destination])
        return Path(destination).read_bytes()


class PublicMaterials:
    def __init__(self, root):
        self.root = Path(root).resolve()
        manifest = json.loads((self.root / 'manifest.json').read_text())
        self.certificates = set(manifest['certificates'])
        self.public_keys = set(manifest['public_keys'])
        self.fingerprints = {}

    def path(self, relative, permitted):
        require(relative in permitted, 'Public material is not declared in manifest: ' + relative)
        path = (self.root / relative).resolve()
        require(self.root in path.parents and path.is_file(), 'Public material must be a declared file inside public-material directory')
        return path

    def certificate(self, prefix):
        relative = prefix + '.x509.pem'
        path = self.path(relative, self.certificates)
        if relative not in self.fingerprints:
            self.fingerprints[relative] = certificate_fingerprint(path)
        return self.fingerprints[relative]

    def payload_key(self, relative):
        path = self.path(relative, self.public_keys)
        with path.open('rb') as stream:
            header = stream.readline().strip()
        require(header in (b'-----BEGIN PUBLIC KEY-----', b'-----BEGIN RSA PUBLIC KEY-----'),
                'Payload verification input must be exported PUBLIC KEY PEM')
        return path


def read_metadata(archive, name):
    require(archive.namelist().count(name) == 1, 'Expected one metadata member: ' + name)
    rows = {}
    for line in archive.read(name).decode().splitlines():
        if not line.strip():
            continue
        fields = dict(token.split('=', 1) for token in shlex.split(line))
        if 'name' in fields:
            rows.setdefault(fields['name'], []).append(fields)
    return rows


def consumed_row(rows, name, fields):
    require(name in rows, 'Missing original signing routing declaration: ' + name)
    identities = {tuple(row.get(field) for field in fields) for row in rows[name]}
    require(len(identities) == 1, 'Conflicting consumed original signing declarations: ' + name)
    return rows[name][-1]


def apk_source_key(row):
    certificate = row['certificate']
    if certificate == 'PRESIGNED':
        return 'PRESIGNED'
    require(certificate.endswith('.x509.pem'), 'Unsupported original APK certificate routing')
    return certificate[:-len('.x509.pem')]


def shipping_members(archive):
    names = archive.namelist()
    files = [name for name in names if name.endswith(('.apk', '.apk.gz', '.apex', '.capex'))]
    require(len(files) == len(set(files)), 'Duplicate shipping archive member')
    return sorted(name for name in files if name.endswith(('.apk', '.apk.gz'))), sorted(name for name in files if name.endswith(('.apex', '.capex')))


def approved_unrouted_members(flat, rows, approved):
    unknown = {path for path in flat if Path(path).name.removesuffix('.gz') not in rows}
    require(len(approved) == len(set(approved)), 'Duplicate approved preserved APK path')
    require(set(approved) == unknown,
            'Explicit approved preserved paths must exactly cover only APKs missing routing: unknown %s, approved %s' % (sorted(unknown), sorted(approved)))
    return unknown


def extract_member(archive, name, destination):
    destination = Path(destination)
    with archive.open(name) as source, destination.open('wb') as target:
        if name.endswith('.apk.gz'):
            with gzip.GzipFile(fileobj=source) as decoded:
                shutil.copyfileobj(decoded, target)
        else:
            shutil.copyfileobj(source, target)
    return destination


def embedded_avb_key(image):
    # APEX payloads are raw filesystem images, with the AVB footer at the end.
    with Path(image).open('rb') as stream:
        stream.seek(-64, os.SEEK_END)
        footer = stream.read(64)
        require(footer[:4] == b'AVBf', 'Expected raw APEX payload AVB footer')
        vbmeta_offset = struct.unpack_from('>Q', footer, 20)[0]
        stream.seek(vbmeta_offset)
        header = stream.read(256)
        require(len(header) == 256 and header[:4] == b'AVB0', 'Expected APEX payload vbmeta header')
        authentication_size = struct.unpack_from('>Q', header, 12)[0]
        algorithm = struct.unpack_from('>I', header, 28)[0]
        public_key_offset, public_key_size = struct.unpack_from('>QQ', header, 64)
        require(algorithm != 0 and public_key_size > 0, 'APEX payload must have an AVB signature and public key')
        stream.seek(vbmeta_offset + 256 + authentication_size + public_key_offset)
        public_key = stream.read(public_key_size)
        require(len(public_key) == public_key_size, 'Truncated payload public key')
        return public_key, algorithm


def apex_payload(path, directory):
    with zipfile.ZipFile(path) as apex:
        require(apex.namelist().count('apex_payload.img') == 1 and apex.namelist().count('apex_pubkey') == 1,
                'APEX lacks unique binary payload/public key')
        payload = extract_member(apex, 'apex_payload.img', Path(directory) / 'apex_payload.img')
        embedded, algorithm = embedded_avb_key(payload)
        require(apex.read('apex_pubkey') == embedded, 'APEX apex_pubkey differs from payload embedded key')
        return payload, embedded, algorithm


def check_flat(tools, materials, config, original, signed, path, row, scratch, approved_unrouted=False):
    require(row is not None or approved_unrouted, 'Unrouted APK needs explicit preserved-path approval')
    actual = extract_member(signed, path, Path(scratch) / 'signed.apk')
    observed = tools.apk_signers(actual)
    source_key = apk_source_key(row) if row is not None else 'APPROVED_PRESERVED_NO_ROUTING'
    result = {'path': path, 'observed_certificate_sha256': observed, 'original_routing_key': source_key}
    if source_key in ('PRESIGNED', 'APPROVED_PRESERVED_NO_ROUTING'):
        baseline = extract_member(original, path, Path(scratch) / 'original.apk')
        expected = tools.apk_signers(baseline)
        require(observed == expected, 'Preserved APK signer identity changed')
        require(sha256_file(actual) == sha256_file(baseline), 'Preserved APK bytes changed')
        result.update({'mode': 'EXPLICITLY_PRESERVED' if approved_unrouted else 'PRESIGNED',
                       'original_certificate_sha256': expected, 'preserved_binary_sha256': sha256_file(actual)})
        if approved_unrouted:
            original_blob = sha256_member(original, path)
            signed_blob = sha256_member(signed, path)
            require(original_blob == signed_blob, 'Explicitly preserved APK compressed/archive bytes changed')
            result['preserved_archive_blob_sha256'] = signed_blob
    else:
        require(source_key in config['key_map'], 'Missing reviewed APK mapping: ' + source_key)
        target_key = config['key_map'][source_key]
        expected = materials.certificate(target_key)
        require(observed == [expected], 'APK certificate does not match configured public certificate: expected %s, observed %s' % (expected, observed))
        result.update({'mode': 'RESIGNED', 'configured_key': target_key, 'expected_certificate_sha256': expected})
    return result


def check_capex_coherence(outer_key, inner_key, outer_manifest, inner_manifest, root_digest):
    require(outer_key == inner_key, 'CAPEX outer/inner apex_pubkey differs')
    outer_version = outer_manifest.get('version', 0)
    inner_version = inner_manifest.get('version', 0)
    require(str(outer_version) == str(inner_version), 'CAPEX outer/inner manifest version differs')
    original_digest = outer_manifest.get('capexMetadata', {}).get('originalApexDigest')
    require(original_digest == root_digest, 'CAPEX originalApexDigest differs from verified payload AVB root digest')
    return {'outer_version': outer_version, 'inner_version': inner_version,
            'original_apex_digest': original_digest, 'verified_payload_root_digest': root_digest,
            'outer_inner_public_key_sha256': hashlib.sha256(inner_key).hexdigest()}


def check_apex(tools, materials, config, original, signed, path, scratch):
    name = Path(path).name.replace('.capex', '.apex')
    require(name in config['apex'], 'Missing reviewed APEX mapping: ' + name)
    mapping = config['apex'][name]
    actual = extract_member(signed, path, Path(scratch) / Path(path).name)
    observed = tools.apk_signers(actual)
    unpacked, kind = tools.uncompressed_apex(actual, scratch)
    inner = tools.apk_signers(unpacked) if kind == 'COMPRESSED' else observed
    payload, embedded, algorithm = apex_payload(unpacked, scratch)
    result = {'path': path, 'name': name, 'type': kind, 'outer_certificate_sha256': observed,
              'inner_certificate_sha256': inner, 'payload_embedded_public_key_sha256': hashlib.sha256(embedded).hexdigest(),
              'payload_avb_algorithm_type': algorithm}
    if mapping['container_key'] == mapping['payload_key'] == 'PRESIGNED':
        baseline = extract_member(original, path, Path(scratch) / ('baseline-' + Path(path).name))
        original_signers = tools.apk_signers(baseline)
        baseline_directory = Path(scratch) / 'baseline'
        baseline_directory.mkdir()
        baseline_unpacked, baseline_kind = tools.uncompressed_apex(baseline, baseline_directory)
        baseline_inner = tools.apk_signers(baseline_unpacked) if baseline_kind == 'COMPRESSED' else original_signers
        baseline_payload, baseline_embedded, baseline_algorithm = apex_payload(baseline_unpacked, baseline_directory)
        require(observed == original_signers and inner == baseline_inner, 'PRESIGNED APEX signer identity changed')
        require(kind == baseline_kind and embedded == baseline_embedded and algorithm == baseline_algorithm,
                'PRESIGNED APEX payload key identity changed')
        require(sha256_file(actual) == sha256_file(baseline), 'PRESIGNED APEX binary changed')
        tools.verify_payload(payload)
        tools.verify_payload(baseline_payload)
        result.update({'mode': 'PRESIGNED', 'original_certificate_sha256': original_signers,
                       'preserved_binary_sha256': sha256_file(actual)})
    else:
        require(mapping['container_key'] != 'PRESIGNED' and mapping['payload_key'] != 'PRESIGNED', 'Mixed APEX preservation mapping')
        expected_certificate = materials.certificate(mapping['container_key'])
        require(observed == inner == [expected_certificate], 'APEX container certificate does not match configured public certificate: expected %s, outer %s, inner %s' % (expected_certificate, observed, inner))
        public_pem = materials.payload_key(mapping['payload_key'])
        expected_key = tools.encoded_public_key(public_pem, Path(scratch) / 'expected.avbpubkey')
        require(embedded == expected_key, 'APEX payload embedded key does not match exported configured public key: expected %s, observed %s' % (hashlib.sha256(expected_key).hexdigest(), hashlib.sha256(embedded).hexdigest()))
        tools.verify_payload(payload, public_pem)
        result.update({'mode': 'RESIGNED', 'configured_container_key': mapping['container_key'],
                       'configured_payload_public_key': mapping['payload_key'],
                       'expected_certificate_sha256': expected_certificate,
                       'expected_payload_public_key_sha256': hashlib.sha256(expected_key).hexdigest()})
    if kind == 'COMPRESSED':
        with zipfile.ZipFile(actual) as outer:
            require(outer.namelist().count('apex_pubkey') == 1, 'CAPEX lacks a unique bundled public key')
            outer_key = outer.read('apex_pubkey')
        result['capex_coherence'] = check_capex_coherence(
            outer_key, embedded, tools.manifest(actual), tools.manifest(unpacked),
            tools.payload_root_digest(payload))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signed-target-files', type=Path, required=True)
    parser.add_argument('--original-target-files', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--public-materials', type=Path, required=True, help='Only the exported public-signing directory with manifest.json')
    parser.add_argument('--host-tools', type=Path, required=True, help='Matching host/linux-x86 directory')
    parser.add_argument('--java-home', type=Path, required=True, help='Declared JDK11 directory')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--preserved-flat-apks', type=Path, help='Root-approved JSON paths for shipping APKs with no original routing; no implicit preservation')
    parser.add_argument('--sdk', type=int, default=33)
    parser.add_argument('--expected-flat-apks', type=int, default=187, help='Expected flat APK files covered by original routing, excluding explicitly approved unrouted files')
    parser.add_argument('--expected-apex', type=int, default=27)
    parser.add_argument('--expected-resigned-apex', type=int, default=26)
    args = parser.parse_args()
    report = {'scope': 'shipping flat APK + APEX outer/inner container + payload AVB binary signatures',
              'state': 'RUNNING', 'sdk': args.sdk, 'inputs': {key: str(getattr(args, key)) for key in ('signed_target_files', 'original_target_files', 'config', 'public_materials', 'host_tools', 'java_home')},
              'flat_apks': [], 'apex': [], 'errors': [],
              'limits': ['Nested APK certificate mapping inside APEX payloads is not separately checked.',
                         'APK signatures are checked for the selected runtime API, default33.',
                         'PRESIGNED and explicitly approved unrouted identities are grounded in the supplied original target-files binary.',
                         'Tool writes temporary extracted public artifacts and the report; never edits source/archive/device or reads a private-key directory.']}
    tools = None
    try:
        require(args.signed_target_files.resolve() != args.original_target_files.resolve(), 'Signed and original target-files must be distinct acceptance inputs')
        config = json.loads(args.config.read_text())
        require(config.get('schema_version') == 1, 'Unsupported reviewed signing config')
        tools = HostTools(args.host_tools, args.java_home, args.sdk)
        materials = PublicMaterials(args.public_materials)
        with zipfile.ZipFile(args.original_target_files) as original, zipfile.ZipFile(args.signed_target_files) as signed:
            flat, apex = shipping_members(original)
            signed_flat, signed_apex = shipping_members(signed)
            require(flat == signed_flat and apex == signed_apex, 'Signed shipping package set differs from original actual archive members')
            rows = read_metadata(original, 'META/apkcerts.txt')
            approval = json.loads(args.preserved_flat_apks.read_text()) if args.preserved_flat_apks else {'paths': []}
            approved = approval['paths']
            require(isinstance(approved, list) and all(isinstance(path, str) for path in approved), 'Preserved APK approval must contain an explicit paths list')
            unrouted = approved_unrouted_members(flat, rows, approved)
            require(len(flat) - len(unrouted) == args.expected_flat_apks and len(apex) == args.expected_apex, 'Actual routed shipping package counts differ from accepted inventory')
            require({Path(path).name.replace('.capex', '.apex') for path in apex} == set(config['apex']), 'APEX config does not cover exactly actual shipping containers')
            report['counts'] = {'flat_apks': len(flat), 'routed_flat_apks': len(flat) - len(unrouted),
                                'explicitly_preserved_flat_apks': len(unrouted), 'apex': len(apex)}
            report['approved_preserved_without_routing'] = sorted(unrouted)
            report['preserved_path_approval_input'] = str(args.preserved_flat_apks) if args.preserved_flat_apks else None
            for index, path in enumerate(flat):
                try:
                    with tempfile.TemporaryDirectory(prefix='crux-flat-signature-') as scratch:
                        name = Path(path).name.removesuffix('.gz')
                        row = None if path in unrouted else consumed_row(rows, name, ('certificate', 'private_key'))
                        result = check_flat(tools, materials, config, original, signed, path, row, scratch,
                                            approved_unrouted=path in unrouted)
                        result['state'] = 'PASS'
                        report['flat_apks'].append(result)
                except (VerificationError, OSError, ValueError, zipfile.BadZipFile) as error:
                    report['flat_apks'].append({'path': path, 'state': 'FAIL', 'error': str(error)})
                    report['errors'].append(path + ': ' + str(error))
                print('flat %d/%d %s %s' % (index + 1, len(flat), report['flat_apks'][-1]['state'], path), flush=True)
            for index, path in enumerate(apex):
                try:
                    with tempfile.TemporaryDirectory(prefix='crux-apex-signature-') as scratch:
                        result = check_apex(tools, materials, config, original, signed, path, scratch)
                        result['state'] = 'PASS'
                        report['apex'].append(result)
                except (VerificationError, OSError, ValueError, zipfile.BadZipFile, struct.error) as error:
                    report['apex'].append({'path': path, 'state': 'FAIL', 'error': str(error)})
                    report['errors'].append(path + ': ' + str(error))
                print('apex %d/%d %s %s' % (index + 1, len(apex), report['apex'][-1]['state'], path), flush=True)
            resigned = sum(row.get('mode') == 'RESIGNED' for row in report['apex'])
            require(resigned == args.expected_resigned_apex, 'Resigned APEX acceptance count mismatch')
            report['counts']['resigned_apex'] = resigned
    except (VerificationError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        report['errors'].append(str(error))
    report['commands'] = tools.commands if tools else []
    report['state'] = 'PASS' if not report['errors'] else 'FAIL'
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(report['state'] + ': ' + str(args.report), flush=True)
    return 0 if report['state'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
