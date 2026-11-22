#!/usr/bin/env python3
"""Resolve notice reads from recorded license producers, never from OUT files."""
from __future__ import annotations

import posixpath

from cloud_source_reads import (
    LICENSE_TOOL, NOTICE_TOOLS, _absolute, _inside, _license_options,
    _make_argument_file, _option_values, _read_rsp_tokens, _shell_command_contexts,
)


class LicenseMetadataReads:
    """Resolve generated metadata content and notice inputs from native edges.

    ``edges`` is an iterable of recorded per-edge metadata dictionaries. Copies
    establish content provenance, not additional notice reads: opening a copied
    metadata file follows its stored ``-d`` fields, not the copy's source path.
    The caller remains responsible for producer scheduling and source delivery.
    """

    def __init__(self, edges, source_root, out_root):
        self.source_root = posixpath.normpath(str(source_root))
        self.out_root = posixpath.normpath(str(out_root))
        if not self.source_root.startswith('/') or not self.out_root.startswith('/'):
            raise ValueError('License input audit requires absolute SOURCE and OUT roots')
        if _inside(self.source_root, self.out_root) or _inside(self.out_root, self.source_root):
            raise ValueError('License input audit requires disjoint SOURCE and OUT roots')
        self._edges = {}
        for edge in edges:
            for output in edge.get('outputs', []):
                path = _absolute(output, self.source_root)
                if path in self._edges and self._edges[path] is not edge:
                    raise ValueError('Duplicate declared license input owner: ' + path)
                self._edges[path] = edge
        self._contexts = {}
        self._definitions = {}
        self._contents = {}
        self._notices = {}

    @staticmethod
    def _literal(value, cwd):
        path = value if value.startswith('/') else posixpath.join(cwd, value)
        if any(character in path for character in '$`*?['):
            raise ValueError('Unresolved license input path: ' + value)
        return _absolute(path, cwd)

    def _metadata_path(self, value, cwd):
        path = self._literal(value, cwd)
        if not _inside(path, self.out_root):
            raise ValueError('License metadata input is not a recorded OUT file: ' + path)
        if path not in self._edges:
            raise ValueError('License metadata has no exact declared producer: ' + path)
        return path

    def _source_path(self, value, cwd):
        path = self._literal(value, cwd)
        if not _inside(path, self.source_root):
            raise ValueError('License notice text is outside SOURCE: ' + path)
        return path

    def _scripts(self, edge):
        response = edge.get('rspfile')
        return ({self._literal(response, self.source_root): edge.get('rspfile_content')}
                if isinstance(response, str) else {})

    def _edge_contexts(self, edge):
        identity = tuple(edge.get(name) for name in ('command', 'rspfile', 'rspfile_content'))
        if identity not in self._contexts:
            self._contexts[identity] = list(_shell_command_contexts(
                edge.get('command', ''), self.source_root, self._scripts(edge)))
        return self._contexts[identity]

    def _arguments(self, edge, argv, cwd, position, make_arguments=False):
        """Expand original Go @operands once; emitted @tokens remain literal."""
        expanded = []
        declared = edge.get('rspfile')
        declared = self._literal(declared, self.source_root) if isinstance(declared, str) else None
        for token in argv:
            if not token.startswith('@') or len(token) == 1:
                expanded.append(token)
                continue
            path = self._literal(token[1:], cwd)
            if path == declared:
                content = edge.get('rspfile_content')
                if not isinstance(content, str):
                    raise ValueError('Declared license response has no recorded content: ' + path)
            elif make_arguments:
                content = _make_argument_file(edge.get('command', ''), self.source_root,
                                              path, position, self._scripts(edge))
            else:
                raise ValueError('Unsupported notice response input: ' + path)
            expanded.extend(_read_rsp_tokens(content))
        return expanded

    def _copy_operands(self, argv, cwd):
        arguments = []
        options = True
        for token in argv[1:]:
            if options and token == '--':
                options = False
            elif options and token.startswith('-'):
                if token not in {'-f', '-p', '-fp', '-pf', '--force', '--preserve'}:
                    raise ValueError('Unsupported license metadata copy option: ' + token)
            else:
                arguments.append(self._literal(token, cwd))
        if len(arguments) != 2:
            raise ValueError('License metadata copy must have one source and destination')
        return arguments

    def _definition(self, path):
        if path in self._definitions:
            return self._definitions[path]
        edge = self._edges[path]
        candidates = []
        programs = set()
        for argv, cwd, position, _ in self._edge_contexts(edge):
            program = posixpath.basename(argv[0]) if argv else ''
            programs.add(program)
            if program == LICENSE_TOOL:
                expanded = self._arguments(edge, argv, cwd, position, make_arguments=True)
                outputs = [self._literal(value, cwd) for value in _option_values(expanded, '-o')]
                if path not in outputs:
                    continue
                notices, dependencies = _license_options(expanded)
                candidates.append(('writer', (
                    frozenset(self._metadata_path(child, cwd) for child in dependencies),
                    frozenset(self._source_path(notice, cwd) for notice in notices)), position))
            elif program == 'cp':
                source, destination = self._copy_operands(argv, cwd)
                if destination == path:
                    candidates.append(('copy', self._metadata_path(source, cwd), position))
            elif program not in {'rm', 'mkdir', 'touch', 'echo'}:
                raise ValueError('Unsupported generated license metadata writer: ' + program)
        if len(candidates) != 1:
            raise ValueError('License metadata has no unique recorded content writer: ' + path)
        kind, payload, writer_position = candidates[0]
        result = kind, payload
        if result[0] == 'copy' and programs - {'cp', 'rm', 'mkdir'}:
            raise ValueError('Unsupported license metadata copy transformation: ' + path)
        for argv, cwd, position, _ in self._edge_contexts(edge):
            if position <= writer_position:
                continue
            program = posixpath.basename(argv[0])
            mutations = []
            if program in {'rm', 'touch', 'mkdir'}:
                mutations.extend(token for token in argv[1:] if not token.startswith('-'))
            mutations.extend(argv[index + 1] for index, token in enumerate(argv[:-1])
                             if token in {'>', '>>', '>|'})
            if any(self._literal(value, cwd) == path for value in mutations):
                raise ValueError('Unsupported license metadata output mutation: ' + path)
        self._definitions[path] = result
        return result

    def _content(self, path):
        trail = []
        active = set()
        current = path
        while current not in self._contents:
            if current in active:
                raise ValueError('License metadata copy cycle has no recorded payload: ' + current)
            active.add(current)
            trail.append(current)
            kind, payload = self._definition(current)
            if kind == 'writer':
                self._contents[current] = payload
                break
            current = payload
        content = self._contents[current]
        for alias in trail:
            self._contents[alias] = content
        return content

    def _notice_roots(self, argv, cwd):
        roots = set()
        index = 1
        positional = False
        value_options = {'-o', '-d', '-product', '-title', '-strip_prefix'}
        while index < len(argv):
            token = argv[index]
            index += 1
            if not positional and token == '#':
                break
            if not positional and token == '--':
                positional = True
                continue
            if not positional and token.startswith('-'):
                name, separator, _ = token.partition('=')
                name = '-' + name.lstrip('-')
                if name not in value_options:
                    raise ValueError('Unsupported notice-reader option: ' + token)
                if not separator:
                    if index == len(argv):
                        raise ValueError('Missing notice-reader option value: ' + token)
                    index += 1
                continue
            roots.add(self._metadata_path(token, cwd))
        return roots

    def notice_reads(self, edge):
        """Return actual notice metadata/text inputs, or ``None`` for other tools.

        All response and script payloads must be recorded in this edge or
        reconstructed from its literal Make argument writer. No files are read.
        Dependency cycles terminate; copy-only cycles and unknown writers fail.
        """
        key = tuple(edge.get(name) for name in ('command', 'rspfile', 'rspfile_content'))
        if key not in self._notices:
            if not any(tool in ((edge.get('command') or '') + '\n'
                                + (edge.get('rspfile_content') or '')) for tool in NOTICE_TOOLS):
                return None
            roots = set()
            found = False
            for argv, cwd, position, _ in self._edge_contexts(edge):
                if not argv or posixpath.basename(argv[0]) not in NOTICE_TOOLS:
                    continue
                found = True
                expanded = self._arguments(edge, argv, cwd, position)
                roots.update(self._notice_roots(expanded, cwd))
            if not found:
                return None
            metadata = set()
            sources = set()
            pending = list(roots)
            while pending:
                path = pending.pop()
                if path in metadata:
                    continue
                metadata.add(path)
                dependencies, notices = self._content(path)
                sources.update(notices)
                pending.extend(dependencies - metadata)
            self._notices[key] = (tuple(sorted(metadata)), tuple(sorted(sources)))
        metadata, sources = self._notices[key]
        return {'metadata_inputs': list(metadata), 'source_inputs': list(sources)}
