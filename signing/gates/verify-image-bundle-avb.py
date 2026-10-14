#!/usr/bin/env python3
"""Verify actual image bytes, configured AVB keys and root descriptors on host."""
import argparse
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--target-files', type=Path, required=True)
p.add_argument('--output-dir', type=Path, required=True)
p.add_argument('--host-tools', type=Path, required=True)
p.add_argument('--public-keys', type=Path, required=True)
a = p.parse_args()
a.output_dir.mkdir(parents=True, exist_ok=False)
names = ['boot', 'recovery', 'system', 'vendor', 'dtbo', 'vbmeta']
with zipfile.ZipFile(a.target_files) as z:
    for name in names:
        with z.open('IMAGES/' + name + '.img') as source, (a.output_dir / (name + '.img')).open('xb') as dest:
            shutil.copyfileobj(source, dest)
commands = []
tool = a.host_tools / 'bin/avbtool'

def run(argv, label):
    result = subprocess.run([str(x) for x in argv], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (a.output_dir / (label + '.txt')).write_text(result.stdout)
    commands.append({'argv': [str(x) for x in argv], 'exit_code': result.returncode, 'output': label + '.txt'})
    assert result.returncode == 0, result.stdout
    return result.stdout

run([tool, 'extract_public_key', '--key', a.public_keys / 'avb.pem', '--output', a.output_dir / 'system.avbpubkey'], 'extract-system-public-key')
root_info = run([tool, 'info_image', '--image', a.output_dir / 'vbmeta.img'], 'vbmeta-info')
assert re.search(r'^Flags:\s+3$', root_info, re.M)
assert 'SHA256_RSA4096' in root_info
run([tool, 'verify_image', '--image', a.output_dir / 'vbmeta.img', '--key', a.public_keys / 'avb-4096.pem',
     '--expected_chain_partition', 'system:1:' + str(a.output_dir / 'system.avbpubkey')], 'vbmeta-verify')
for name in ['system', 'recovery']:
    run([tool, 'verify_image', '--image', a.output_dir / (name + '.img'), '--key', a.public_keys / 'avb.pem'], name + '-verify')
report = {'status': 'PASS', 'target_files': str(a.target_files), 'commands': commands,
          'scope': 'Actual root vbmeta RSA4096 signature and configured public-key identity; system chain key/rollback-location1; root Boot/DTBO hashes and vendor hashtree; actual system and Recovery RSA2048 signatures/hashtrees.',
          'flags': 3, 'runtime_limit': 'Bootloader remains unlocked; flags3 disables runtime verification/verity. Host signature integrity is not locked verified-boot or runtime acceptance.'}
(a.output_dir / 'acceptance.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report), flush=True)
