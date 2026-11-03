#!/usr/bin/env python3
"""Convert a repo revision manifest into complete, cloud-fetched source layers."""

import argparse
import json
import posixpath
from pathlib import Path
from urllib.parse import urljoin
import xml.etree.ElementTree as ET


def convert(xml_path, layout_path, layer_count):
    if layer_count < 1:
        raise ValueError('layer count must be positive')
    manifest = ET.parse(xml_path).getroot()
    remotes = {row.get('name'): row.get('fetch') for row in manifest.findall('remote')}
    default = manifest.find('default')
    projects = []
    layout = {}
    for row in manifest.findall('project'):
        path = row.get('path', row.get('name'))
        revision = row.get('revision')
        if not revision or len(revision) != 40:
            raise ValueError('project is not commit-pinned: ' + path)
        remote = row.get('remote', default.get('remote') if default is not None else None)
        url = urljoin(remotes[remote].rstrip('/') + '/', row.get('name'))
        projects.append({'path': path, 'url': url, 'revision': revision})
        for kind in ('copyfile', 'linkfile'):
            for item in row.findall(kind):
                layout[item.get('dest')] = {
                    'type': 'copy' if kind == 'copyfile' else 'link',
                    'source': posixpath.join(path, item.get('src')),
                    'destination': item.get('dest'),
                }
    for path, name, revision in (
        ('vendor/gms', 'vendor_gms', 'f0e7f260c5b34a3f3917cfda4dc7b825b96511ff'),
        ('external/faceunlock', 'external_faceunlock', 'cd8cfdecdfbacbf40c42968863c7525025765e55'),
    ):
        if path not in {item['path'] for item in projects}:
            projects.append({
                'path': path, 'revision': revision, 'archive_type': 'git-bundle',
                'url': 'https://archive.org/download/gitlab.pixelexperience.org_bundles_20240412/'
                       'https%3A__gitlab.pixelexperience.org_android_' + name + '.git.bundle',
            })
    # These repo-generated links survive local manifest project replacements but
    # are omitted by repo's resolved XML. Preserve their observed layout explicitly.
    observed = json.loads(Path(layout_path).read_text())
    for link in observed['links']:
        destination = link['dest']
        source = posixpath.normpath(posixpath.join(posixpath.dirname(destination), link['target']))
        if source.startswith('../') or source.startswith('/'):
            raise ValueError('source layout link escapes tree: ' + destination)
        layout[destination] = {'type': 'link', 'source': source, 'destination': destination}
    layout['Makefile'] = {'type': 'copy', 'source': 'build/make/core/root.mk', 'destination': 'Makefile'}
    projects.sort(key=lambda item: item['path'])
    layers = [{'id': f'source-{i:02d}', 'projects': []} for i in range(layer_count)]
    for number, project in enumerate(projects):
        layers[number % layer_count]['projects'].append(project['path'])
    return {
        'schema_version': 1,
        'source_root': '/home/qingli/crux-pe13-offline-2026-09-25/pe13',
        'source_scope': 'complete commit-pinned repo projects and the two retained upstream bundles; '
                        'excludes Git object storage, build outputs and local diagnostic experiments',
        'projects': projects,
        'layout': sorted(layout.values(), key=lambda item: item['destination']),
        'layers': layers,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xml', required=True)
    parser.add_argument('--layout', required=True)
    parser.add_argument('--layers', type=int, default=32)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = convert(args.xml, args.layout, args.layers)
    Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'projects': len(result['projects']), 'layers': len(result['layers']),
                      'layout': len(result['layout'])}))


if __name__ == '__main__':
    main()
