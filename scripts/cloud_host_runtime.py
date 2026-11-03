#!/usr/bin/env python3
"""Derive generated host-tool runtime inputs from a fresh Ninja graph."""
from __future__ import annotations

import importlib.util
import posixpath
import re
import shlex
from pathlib import Path


def _normalize(value, source_root):
    value = value if value.startswith('/') else posixpath.join(source_root, value)
    return posixpath.normpath(value)


def _host_kind(path, out_root):
    """Classify only public files in the selected host arch's tool/runtime dirs."""
    prefix = out_root.rstrip('/') + '/host/'
    if not path.startswith(prefix):
        return None
    relative = path[len(prefix):]
    parts = relative.split('/')
    if len(parts) >= 3 and parts[1] in ('bin', 'framework') and all(parts[2:]):
        return 'tool'
    if (len(parts) >= 3 and parts[1] in ('lib', 'lib64')
            and re.search(r'\.so(?:\.\d+)*$', parts[-1])):
        return 'library'
    return None


def _literal_host_paths(text, out_root):
    """Find literal absolute OUT host paths in expanded commands/inline rsp text."""
    prefix = out_root.rstrip('/') + '/host/'
    separators = set(" \t\r\n\"'`,;()<>|")
    found = set()
    start = 0
    while True:
        index = text.find(prefix, start)
        if index < 0:
            return found
        end = index
        while end < len(text) and text[end] not in separators:
            end += 1
        candidate = text[index:end].rstrip(':')
        if _host_kind(candidate, out_root):
            found.add(candidate)
        start = index + len(prefix)


def derive_host_runtime(g, records, native, source_root, out_root):
    """Return edge-local order-only prerequisites and generated tool runtime closure.

    This reads only the indexed Ninja graph. It never opens compiled host files or
    changes the graph/index; unknown OUT host paths are retained as diagnostics.
    """
    source_root = posixpath.normpath(str(source_root))
    out_root = posixpath.normpath(str(out_root))
    additions = {}
    tools = {}
    unresolved = set()
    closure_cache = {}

    for edge in sorted(native):
        metadata = native[edge]
        record = records.get(edge)
        if record is None:
            continue
        own = {_normalize(path, source_root) for path in record.get('outputs', [])}
        declared = {_normalize(path, source_root) for path, _ in record.get('deps', [])}
        command = metadata.get('command', '')
        references = _literal_host_paths(command, out_root)
        references.update(_literal_host_paths(metadata.get('rspfile_content', ''), out_root))
        references.update(
            path for path, _ in record.get('deps', [])
            if _host_kind(_normalize(path, source_root), out_root))
        references = {_normalize(path, source_root) for path in references}
        tool_refs = sorted(path for path in references if _host_kind(path, out_root) == 'tool')
        library_refs = sorted(path for path in references if _host_kind(path, out_root) == 'library')
        extra = set()

        for path in tool_refs:
            if path in own:
                continue
            producer = g.producers([path], directory_fallback=False).get(path)
            if producer is None:
                unresolved.add((edge, path, 'no_exact_producer'))
                continue
            if path not in declared:
                extra.add(path)
            if path not in closure_cache:
                closure = g.closure([path])
                closure_cache[path] = sorted(
                    output for output in closure['outputs']
                    if _host_kind(output, out_root) == 'library')
            runtime = [library for library in closure_cache[path]
                       if library not in own and library not in declared]
            extra.update(runtime)
            tools[path] = {
                'producer': producer,
                'generated_runtime_inputs': closure_cache[path],
            }

        for path in library_refs:
            if path in own or path in declared:
                continue
            producer = g.producers([path], directory_fallback=False).get(path)
            if producer is None:
                unresolved.add((edge, path, 'no_exact_producer'))
            else:
                extra.add(path)

        if extra:
            additions[edge] = sorted(extra)

    return {
        'additions': additions,
        'tools': tools,
        'unresolved_host_paths': [
            {'edge': edge, 'path': path, 'reason': reason}
            for edge, path, reason in sorted(unresolved)
        ],
    }


_SOURCE_READS = None


def _source_read_invocations():
    """Load the shared Ninja shell-context parser without package assumptions."""
    global _SOURCE_READS
    if _SOURCE_READS is None:
        path = Path(__file__).with_name('cloud_source_reads.py')
        spec = importlib.util.spec_from_file_location('_cloud_host_source_reads', path)
        if spec is None or spec.loader is None:
            raise RuntimeError('cannot load cloud_source_reads invocation parser')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SOURCE_READS = module
    return _SOURCE_READS


def _expand_declared_response(argv, metadata, cwd, source_root):
    response = metadata.get('rspfile')
    content = metadata.get('rspfile_content')
    response_path = (posixpath.normpath(response if response.startswith('/') else
                                        posixpath.join(source_root, response))
                     if isinstance(response, str) else None)
    expanded = []
    for token in argv:
        token_path = token[1:] if token.startswith('@') else ''
        if token_path and not token_path.startswith('/'):
            token_path = posixpath.join(cwd, token_path)
        token_path = posixpath.normpath(token_path) if token_path else None
        if token_path is not None and token_path == response_path:
            if not isinstance(content, str):
                raise ValueError('declared Rust response file has no recorded content')
            expanded.extend(shlex.split(content, comments=False, posix=True))
        else:
            expanded.append(token)
    return expanded


def _rust_native_dirs(metadata, source_root, out_root, source_reads):
    directories = set()
    static_libraries = set()
    invocations = source_reads.iter_invocations(metadata, source_root)
    for original_argv, cwd in invocations:
        if not original_argv or posixpath.basename(original_argv[0]) != 'rustc':
            continue
        argv = _expand_declared_response(original_argv, metadata, cwd, source_root)
        index = 0
        while index < len(argv):
            token = argv[index]
            operand = None
            if token == '-L' and index + 1 < len(argv):
                operand = argv[index + 1]
                index += 2
            elif token.startswith('-L') and len(token) > 2:
                operand = token[2:]
                index += 1
            else:
                if token == '-l' and index + 1 < len(argv):
                    library = argv[index + 1]
                    index += 2
                elif token.startswith('-l') and len(token) > 2:
                    library = token[2:]
                    index += 1
                else:
                    index += 1
                    continue
                kind, separator, name = library.partition('=')
                if separator and kind.startswith('static') and name:
                    filename = name if name.endswith('.a') else 'lib' + name + '.a'
                    static_libraries.add(filename)
                continue

            if operand and operand.startswith('native='):
                value = operand[len('native='):]
                path = value if value.startswith('/') else posixpath.join(cwd, value)
                path = posixpath.normpath(path)
                if path.startswith(out_root.rstrip('/') + '/'):
                    directories.add(path)
    return sorted(directories), sorted(static_libraries)


def _global_archives(g, directory):
    """Read producer rows below a directory from the index, never from OUT files."""
    prefix = directory.rstrip('/') + '/'
    upper = prefix + chr(0x10ffff)
    rows = g.db.execute(
        'SELECT path,edge FROM outputs WHERE path >= ? AND path < ? ORDER BY path',
        (prefix, upper))
    return [(row['path'], row['edge']) for row in rows
            if posixpath.dirname(row['path']) == directory and row['path'].endswith('.a')]


def derive_rust_native(g, records, native, source_root, out_root):
    """Recover generated OUT_DIR archives visible through Rust `-L native`.

    Directory contents are derived from the global Ninja outputs table, not the
    filesystem. Only `rustc` invocations and OUT_DIR search paths participate.
    """
    source_root = posixpath.normpath(str(source_root))
    out_root = posixpath.normpath(str(out_root))
    additions = {}
    proofs = []
    unresolved = []
    source_reads = _source_read_invocations()

    for consumer in sorted(native):
        record = records.get(consumer)
        if record is None:
            continue
        command = native[consumer].get('command') or ''
        if not re.search(r'(?<![A-Za-z0-9_])rustc(?![A-Za-z0-9_])', command):
            continue
        own = {_normalize(path, source_root) for path in record.get('outputs', [])}
        declared = {_normalize(path, source_root) for path, _ in record.get('deps', [])}
        search_dirs, static_libraries = _rust_native_dirs(
            native[consumer], source_root, out_root, source_reads)
        if not search_dirs:
            continue

        archives = {}
        for directory in search_dirs:
            for path, producer in _global_archives(g, directory):
                if path not in own:
                    archives[path] = producer
        extra = {path for path in archives if path not in declared and path not in own}

        for filename in static_libraries:
            candidates = [posixpath.join(directory, filename) for directory in search_dirs]
            has_global_owner = any(
                g.producers([path], directory_fallback=False).get(path) is not None
                for path in candidates)
            if not has_global_owner:
                unresolved.append({
                    'edge': consumer,
                    'archive': filename,
                    'search_dirs': search_dirs,
                    'reason': 'no_exact_global_producer',
                })

        if extra:
            additions[consumer] = sorted(extra)
        proofs.append({
            'edge': consumer,
            'search_dirs': search_dirs,
            'archives': [
                {'path': path, 'producer': archives[path], 'added': path in extra}
                for path in sorted(archives)
            ],
            'static_libraries': static_libraries,
        })

    return {
        'additions': additions,
        'proofs': proofs,
        'unresolved_native_archives': unresolved,
    }
