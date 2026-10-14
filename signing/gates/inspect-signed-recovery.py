#!/usr/bin/env python3
"""Inspect actual signed Recovery bytes for the local tier-1 acceptance."""
import argparse
import gzip
import io
import json
import struct
import subprocess
import zipfile
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--signed-target-files', type=Path, required=True)
parser.add_argument('--output-dir', type=Path, required=True)
parser.add_argument('--host-tools', type=Path, required=True)
parser.add_argument('--public-keys', type=Path, required=True)
parser.add_argument('--matched-image', type=Path, required=True)
args = parser.parse_args()
out = args.output_dir
out.mkdir(parents=True, exist_ok=False)
with zipfile.ZipFile(args.signed_target_files) as archive:
    canonical = archive.read('IMAGES/recovery.img')
    installed = archive.read('VENDOR/etc/recovery.img')
    installer = archive.read('VENDOR/bin/install-recovery.sh').decode()
    target_certs = archive.read('RECOVERY/RAMDISK/system/etc/security/otacerts.zip')
    for p in ('boot', 'recovery', 'dtbo', 'vbmeta'):
        (out / (p + '.img')).write_bytes(archive.read('IMAGES/' + p + '.img'))
    (out / 'vendor-recovery.img').write_bytes(installed)
    (out / 'install-recovery.sh').write_text(installer)

def parse(image):
    assert image[:8] == b'ANDROID!'
    kernel_size, _, ramdisk_size = struct.unpack_from('<3I', image, 8)
    page_size, version = struct.unpack_from('<2I', image, 36)
    assert page_size == 4096 and version == 1
    dtbo_size, dtbo_offset = struct.unpack_from('<IQ', image, 1632)
    assert dtbo_size > 0
    ramdisk_offset = page_size + (kernel_size + page_size - 1) // page_size * page_size
    assert dtbo_offset == ramdisk_offset + (ramdisk_size + page_size - 1) // page_size * page_size
    assert dtbo_offset + dtbo_size <= len(image)
    cmdline = image[64:576].split(b'\0', 1)[0] + image[608:1632].split(b'\0', 1)[0]
    cmdline = cmdline.decode()
    for banned in ('crux_uboot', 'earlycon=', 'lpm_levels.sleep_disabled', 'kpti=', 'watchdog_v2.enable=0', 'msm_poweroff.download_mode=0'):
        assert banned not in cmdline, banned
    assert 'buildvariant=user' in cmdline
    return image[page_size:page_size + kernel_size], image[ramdisk_offset:ramdisk_offset + ramdisk_size], image[dtbo_offset:dtbo_offset + dtbo_size], {'page_size': page_size, 'version': version, 'kernel_size': kernel_size, 'ramdisk_size': ramdisk_size, 'recovery_dtbo_size': dtbo_size, 'recovery_dtbo_offset': dtbo_offset, 'command_line': cmdline, 'image_bytes': len(image)}

kernel, ramdisk, dtbo, header = parse(canonical)
vendor_kernel, vendor_ramdisk, vendor_dtbo, vendor_header = parse(installed)
assert (kernel, ramdisk, dtbo) == (vendor_kernel, vendor_ramdisk, vendor_dtbo)
assert kernel[:16] == b'UNCOMPRESSED_IMG'
wrapper_length = struct.unpack_from('<I', kernel, 16)[0]
matched_image = args.matched_image.read_bytes()
assert wrapper_length == len(matched_image)
linux = kernel[20:]
assert linux[56:60] == b'ARM\x64'
assert linux.startswith(matched_image), 'Kernel differs from the retained candidate Image/symbol pair'
header['wrapper_bytes'] = 20
header['wrapper_image_bytes'] = wrapper_length
header['appended_dtb_bytes'] = len(linux) - wrapper_length
header['arm64_image_size'] = struct.unpack_from('<Q', linux, 16)[0]
assert dtbo[:4] == b'\xd7\xb7\xab\x1e'

def members(raw):
    position = 0
    files = {}
    while position < len(raw):
        assert raw[position:position + 6] in (b'070701', b'070702')
        fields = [int(raw[position + 6 + n * 8:position + 14 + n * 8], 16) for n in range(13)]
        size, namesize = fields[6], fields[11]
        name = raw[position + 110:position + 110 + namesize - 1].decode()
        position = (position + 110 + namesize + 3) & ~3
        if name == 'TRAILER!!!':
            break
        assert name not in files
        files[name.removeprefix('./')] = raw[position:position + size]
        position = (position + size + 3) & ~3
    return files

files = members(gzip.decompress(ramdisk))
assert 'fstab.crux_uboot' not in files
fstab = files['system/etc/recovery.fstab'].decode()
assert '/by-name/pesystem' not in fstab and '/by-name/peuserdata' not in fstab
for mount in ('/system', '/vendor', '/data', '/metadata', '/boot', '/recovery', '/cache', '/misc'):
    assert any(line.split() and mount in line.split() for line in fstab.splitlines() if not line.lstrip().startswith('#')), mount
certs = files['system/etc/security/otacerts.zip']
assert certs == target_certs
with zipfile.ZipFile(io.BytesIO(certs)) as archive:
    assert {archive.read(name) for name in archive.namelist()} == {(args.public_keys / 'releasekey.x509.pem').read_bytes()}
props = files['prop.default'].decode()
properties = dict(line.split('=', 1) for line in props.splitlines() if '=' in line and not line.lstrip().startswith('#'))
for key, value in {'ro.build.type': 'user', 'ro.debuggable': '0', 'ro.adb.secure': '1', 'ro.vendor.crux.early_adb': '0'}.items():
    assert properties.get(key) == value, (key, properties.get(key))
assert properties['ro.build.tags'] == 'release-keys'
(out / 'otacerts.zip').write_bytes(certs)
(out / 'recovery.fstab').write_text(fstab)
(out / 'prop.default').write_text(props)
for name, data in [('kernel', kernel), ('ramdisk', ramdisk), ('recovery_dtbo', dtbo)]:
    (out / name).write_bytes(data)
commands = []
for name in ('recovery', 'vendor-recovery'):
    for operation in ('info_image', 'verify_image'):
        command = [str(args.host_tools / 'bin/avbtool'), operation, '--image', str(out / (name + '.img'))]
        if operation == 'verify_image':
            command += ['--key', str(args.public_keys / 'avb.pem')]
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (out / (name + '-' + operation + '.txt')).write_text(result.stdout)
        commands.append({'argv': command, 'exit_code': result.returncode})
        assert result.returncode == 0, result.stdout
report = {'status': 'PASS', 'header': header, 'vendor_header': vendor_header, 'components_match': True,
          'kernel_matches_retained_candidate': str(args.matched_image), 'recovery_certs_match_target_files': True,
          'recovery_trusts_only_release_cert': True, 'selected_properties': {key: properties[key] for key in ('ro.build.type', 'ro.debuggable', 'ro.adb.secure', 'ro.vendor.crux.early_adb', 'ro.build.tags', 'ro.build.fingerprint')},
          'commands': commands, 'scope': 'Final signed image host inspection and AVB validation; no ABL or Recovery runtime evidence'}
(out / 'inspection.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report))
