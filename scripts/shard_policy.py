#!/usr/bin/env python3
"""Classify native Ninja actions for stable W10 partition ordering."""
from __future__ import annotations

import fnmatch
import posixpath
from pathlib import PurePosixPath
from typing import Mapping, Sequence

from cloud_source_reads import iter_invocations


CATEGORIES = frozenset({'identity', 'kernel', 'image', 'java', 'native', 'other'})


def _normalized_path(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or not value.startswith('/'):
        raise ValueError(label + ' must be an absolute POSIX path')
    return posixpath.normpath(value)


def _pattern(value: str, source_root: str, out_root: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError('partition-policy output patterns must be non-empty strings')
    expanded = value.replace('${SRC}', source_root).replace('${OUT}', out_root)
    if '${SRC}' in expanded or '${OUT}' in expanded:
        raise ValueError('unsupported partition-policy path variable: ' + value)
    if not expanded.startswith('/'):
        raise ValueError('partition-policy output pattern must be absolute: ' + value)
    return posixpath.normpath(expanded)


def _patterns(profile: Mapping, key: str, source_root: str, out_root: str) -> tuple[str, ...]:
    values = profile.get(key, [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError('partition policy ' + key + ' must be an array of path patterns')
    return tuple(_pattern(value, source_root, out_root) for value in values)


def _under_root(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip('/') + '/')


def _tool_names(profile: Mapping, key: str) -> frozenset[str]:
    values = profile.get(key)
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise ValueError('partition policy ' + key + ' must be an array of tool basenames')
    return frozenset(value.lower() for value in values)


def _tool_category(command: str, source_root: str, profile: Mapping) -> str:
    if not command:
        return 'other'
    java_tools = _tool_names(profile, 'java_tools')
    native_tools = _tool_names(profile, 'native_tools')
    java = native = False
    metadata = {'command': command}
    for argv, _cwd in iter_invocations(metadata, source_root, strict_scripts=False):
        if not argv:
            continue
        program = PurePosixPath(argv[0]).name.lower()
        if program in java_tools:
            java = True
        elif (program in native_tools
              or ('clang' in native_tools and program.startswith('clang-') and len(program) > len('clang-'))):
            native = True
    if java and native:
        return 'other'
    if java:
        return 'java'
    if native:
        return 'native'
    return 'other'


def classify_action(record: Mapping, metadata: Mapping | None, source_root: str,
                    out_root: str, profile: Mapping) -> str:
    """Return a stable W10 category for one Ninja action, based on outputs and tool.

    Selectors are exact paths/globs from the approved profile. Identity is not
    propagated from dependencies: an action is identity-class only when one of
    its own outputs matches an identity selector.
    """
    if not isinstance(record, Mapping) or not isinstance(profile, Mapping):
        raise ValueError('action record and partition profile must be mappings')
    if profile.get('schema_version') != 1 or profile.get('version') != 'w10-1':
        raise ValueError('unsupported W10 partition profile')
    source_root = _normalized_path(source_root, 'source_root')
    out_root = _normalized_path(out_root, 'out_root')
    if source_root == out_root:
        raise ValueError('source_root and out_root must differ')
    outputs = record.get('outputs')
    if not isinstance(outputs, (list, tuple)) or not outputs:
        raise ValueError('native action must declare at least one output')
    normalized_outputs = tuple(_normalized_path(value, 'action output') for value in outputs)

    kernel_roots = _patterns(profile, 'kernel_output_roots', source_root, out_root)
    if any(_under_root(path, root) for path in normalized_outputs for root in kernel_roots):
        return 'kernel'

    identity_patterns = _patterns(profile, 'identity_output_patterns', source_root, out_root)
    if any(fnmatch.fnmatchcase(path, pattern)
           for path in normalized_outputs for pattern in identity_patterns):
        return 'identity'

    image_patterns = _patterns(profile, 'image_outputs', source_root, out_root)
    if any(fnmatch.fnmatchcase(path, pattern)
           for path in normalized_outputs for pattern in image_patterns):
        return 'image'

    metadata = {} if metadata is None else metadata
    if not isinstance(metadata, Mapping):
        raise ValueError('resolved action metadata must be a mapping')
    command = metadata.get('command', '')
    if not isinstance(command, str):
        raise ValueError('resolved command must be a string')
    category = _tool_category(command, source_root, profile)
    return category if category in CATEGORIES else 'other'


def stable_unit_key(primary_outputs: Sequence[str]) -> tuple[str, ...]:
    """Canonical unit ordering independent of graph database/edge identifiers."""
    if not isinstance(primary_outputs, (list, tuple, set, frozenset)) or not primary_outputs:
        raise ValueError('unit must have at least one primary output')
    return tuple(sorted({_normalized_path(value, 'primary output') for value in primary_outputs}))
