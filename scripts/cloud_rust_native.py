#!/usr/bin/env python3
"""Read literal Rust native-link inputs without inspecting a build filesystem."""
from __future__ import annotations

import posixpath
import re

from cloud_source_reads import iter_invocations


_NATIVE_FILE = re.compile(r'\.(?:o|a|so(?:\.\d+)*)$')
_DYNAMIC_NATIVE_SUFFIX = re.compile(r'\.(?:o|a|so)(?:[.$`*?\[]|$)')
_UNRESOLVED_CHARACTERS = '$`*?['
_NON_INPUT_OPTIONS = {
    '-o', '--output', '-L', '--library-path', '-l', '--library',
    '-soname', '--soname', '-h', '-rpath', '--rpath', '-rpath-link',
    '--rpath-link', '-R', '-Map', '--Map', '--exclude-libs',
    '-dynamic-linker', '--dynamic-linker', '-target', '--target',
    '--sysroot', '-isysroot', '-B', '-m', '-z', '-MF', '-MT', '-MQ',
}


def _absolute(path, cwd):
    path = posixpath.normpath(path if path.startswith('/') else posixpath.join(cwd, path))
    return '/' + path.lstrip('/') if path.startswith('/') else path


def _codegen_link_arguments(argv):
    # Response expansion precedes rustc option parsing. Neither a warm file nor
    # another tool's response grammar can establish its hidden inputs here.
    if any(argument.startswith('@') for argument in argv[1:]):
        raise ValueError('Unsupported Rust response-file operand in native input audit')
    arguments = []
    index = 1
    while index < len(argv):
        token = argv[index]
        option = None
        if token == '--':
            break
        if token in ('-C', '--codegen'):
            index += 1
            if index == len(argv):
                raise ValueError('Missing Rust codegen option after ' + token)
            option = argv[index]
        elif token.startswith('-C'):
            option = token[2:]
        elif token.startswith('--codegen='):
            option = token[len('--codegen='):]
        if option is not None:
            name, separator, value = option.partition('=')
            if name in ('link-arg', 'link-args'):
                if not separator:
                    raise ValueError('Missing value for Rust codegen option ' + name)
                # link-arg is one argument, including any embedded spaces.
                # link-args is rustc's whitespace list, not a second shell.
                arguments.extend([value] if name == 'link-arg' else value.split())
        index += 1
    return arguments


def _native_operands(arguments):
    flattened = []
    for argument in arguments:
        if argument.startswith('-Wl,'):
            flattened.extend(argument[4:].split(','))
        elif argument != '-Xlinker':
            flattened.append(argument)
    if any(argument.startswith('@') for argument in flattened):
        raise ValueError('Unsupported Rust native linker response-file operand')
    index = 0
    operands_only = False
    while index < len(flattened):
        argument = flattened[index]
        index += 1
        if not operands_only:
            if argument == '--':
                operands_only = True
                continue
            if argument in _NON_INPUT_OPTIONS:
                if index == len(flattened):
                    raise ValueError('Missing value for Rust native linker option ' + argument)
                index += 1
                continue
            if argument.startswith('-'):
                continue
        if (_NATIVE_FILE.search(argument)
                or (any(character in argument for character in _UNRESOLVED_CHARACTERS)
                    and _DYNAMIC_NATIVE_SUFFIX.search(posixpath.basename(argument)))):
            yield argument


def rust_native_file_reads(edge, source_root, out_root):
    """Return sorted literal generated native files passed to a Rust linker.

    Only actual ``rustc`` invocations and their ``link-arg(s)`` codegen options
    participate. Paths are resolved in their shell invocation's working
    directory; search flags never establish a file by basename. The caller
    must bind each result to an exact declared producer before admitting it.
    Unsupported Rust/linker responses and dynamic native operands raise
    ``ValueError`` rather than reading files or guessing their expansions.
    """
    source_root = posixpath.normpath(str(source_root))
    out_root = posixpath.normpath(str(out_root))
    if not source_root.startswith('/') or not out_root.startswith('/'):
        raise ValueError('Rust native input audit requires absolute source and OUT roots')
    source_root = _absolute(source_root, source_root)
    out_root = _absolute(out_root, source_root)
    if 'rustc' not in edge.get('command', ''):
        return []
    own = {_absolute(path, source_root) for path in edge.get('outputs', [])}
    required = set()
    for argv, cwd in iter_invocations(edge, source_root):
        if not argv or posixpath.basename(argv[0]) != 'rustc':
            continue
        for operand in _native_operands(_codegen_link_arguments(argv)):
            path = operand if operand.startswith('/') else posixpath.join(cwd, operand)
            if any(character in path for character in _UNRESOLVED_CHARACTERS):
                raise ValueError('Unresolved Rust native file operand: ' + operand)
            path = _absolute(path, source_root)
            if path.startswith(out_root.rstrip('/') + '/') and path not in own:
                required.add(path)
    return sorted(required)
