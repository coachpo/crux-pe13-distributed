#!/usr/bin/env python3
"""Derive runtime source reads for selected native PE build actions."""
from __future__ import annotations

import os
from pathlib import Path
import posixpath
import shlex


NOTICE_TOOLS = {"textnotice", "htmlnotice", "xmlnotice", "compliancenotice_shippedlibs",
                "shippedlibs"}
LICENSE_TOOL = "build_license_metadata"
R8_CLASS = "com.android.tools.r8.R8"


class SourceReadError(ValueError):
    pass


def _absolute(path: str, cwd: str) -> str:
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(cwd, path))


def _inside(path: str, root: str) -> bool:
    try:
        return posixpath.commonpath((path, root)) == root
    except ValueError:
        return False


def _shell_segments(command: str):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments, current = [], []
    for token in lexer:
        if token in ("&&", "||", ";", "|", "&", "(", ")"):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def iter_invocations(metadata: dict, source_root: str):
    """Yield (argv, cwd) command contexts, unwrapping common shell launchers."""
    def visit(command: str, cwd: str):
        for segment in _shell_segments(command):
            local_cwd = cwd
            index = 0
            while index < len(segment):
                token = segment[index]
                name = posixpath.basename(token)
                if "=" in token and token.split("=", 1)[0].replace("_", "a").isalnum():
                    index += 1
                elif name in {"exec", "command", "then", "else", "do", "time"}:
                    index += 1
                elif name == "env":
                    index += 1
                    while index < len(segment):
                        option = segment[index]
                        if option == "--":
                            index += 1
                            break
                        if option in {"-i", "--ignore-environment", "-0", "--null"}:
                            index += 1
                        elif option in {"-u", "--unset", "-C", "--chdir"} and index + 1 < len(segment):
                            if option in {"-C", "--chdir"}:
                                local_cwd = _absolute(segment[index + 1], local_cwd)
                            index += 2
                        elif "=" in option and not option.startswith("-"):
                            index += 1
                        else:
                            break
                elif name in {"ccache", "sccache", "distcc"}:
                    index += 1
                    while index < len(segment) and segment[index].startswith("-"):
                        index += 1
                else:
                    break
            if index >= len(segment):
                continue
            argv = segment[index:]
            program = posixpath.basename(argv[0])
            if program == "cd":
                if len(argv) > 1:
                    cwd = _absolute(argv[1], cwd)
                continue
            if program in {"bash", "sh", "dash"}:
                for position, option in enumerate(argv[1:], 1):
                    if option.startswith("-") and not option.startswith("--") and "c" in option[1:]:
                        if position + 1 < len(argv):
                            yield from visit(argv[position + 1], local_cwd)
                        break
                continue
            yield argv, local_cwd

    yield from visit(metadata.get("command", ""), source_root)


def _expand_inline_response(argv: list[str], metadata: dict, cwd: str,
                            source_root: str, out_root: str) -> tuple[list[str], list[str], list[str]]:
    response = metadata.get("rspfile")
    content = metadata.get("rspfile_content")
    response_path = _absolute(response, source_root) if isinstance(response, str) else None
    source_files, unresolved = set(), []

    def expand(tokens, active=frozenset()):
        expanded = []
        for token in tokens:
            if not token.startswith("@") or len(token) == 1:
                expanded.append(token)
                continue
            token_path = _absolute(token[1:], cwd)
            if response_path == token_path:
                if not isinstance(content, str):
                    unresolved.append(token_path)
                    expanded.append(token)
                    continue
                if response_path in active:
                    raise SourceReadError("recursive declared response file: " + response_path)
                nested = shlex.split(content, comments=False, posix=True)
                expanded.extend(expand(nested, active | {response_path}))
                continue
            if _inside(token_path, out_root):
                unresolved.append(token_path)
                expanded.append(token)
                continue
            if _inside(token_path, source_root) and Path(token_path).is_file():
                if token_path in active:
                    raise SourceReadError("recursive source response file: " + token_path)
                try:
                    nested = shlex.split(Path(token_path).read_text(), comments=False, posix=True)
                except (OSError, UnicodeDecodeError, ValueError) as error:
                    raise SourceReadError("cannot read source response file " + token_path + ": " + str(error)) from error
                source_files.add(token_path)
                expanded.extend(expand(nested, active | {token_path}))
                continue
            unresolved.append(token_path)
            expanded.append(token)
        return expanded

    return expand(argv), sorted(source_files), sorted(set(unresolved))


def _option_values(argv: list[str], name: str) -> list[str]:
    values = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == name:
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                raise SourceReadError("missing argument for " + name)
            values.append(argv[index + 1])
            index += 2
        elif token.startswith(name + "="):
            values.append(token[len(name) + 1:])
            index += 1
        else:
            index += 1
    return values


def _license_options(argv: list[str]) -> tuple[list[str], list[str]]:
    notices = [value.partition(":")[0] for value in _option_values(argv, "-n")]
    dependencies = [value.partition(":")[0] for value in _option_values(argv, "-d")]
    return notices, dependencies


def _source_file(value: str, cwd: str, source_root: str, out_root: str) -> str:
    path = _absolute(value, cwd)
    if _inside(path, out_root):
        raise SourceReadError("source-read path points into OUT_DIR: " + path)
    if not _inside(path, source_root):
        raise SourceReadError("source-read path is outside source_root: " + path)
    if not Path(path).is_file():
        raise SourceReadError("source-read file is missing: " + path)
    return path


def _graph_path_candidates(value: str, cwd: str, source_root: str) -> list[str]:
    candidates = [value, _absolute(value, cwd), _absolute(value, source_root)]
    for candidate in list(candidates):
        if candidate.startswith("/") and _inside(posixpath.normpath(candidate), source_root):
            candidates.append(posixpath.relpath(posixpath.normpath(candidate), source_root))
    return list(dict.fromkeys(posixpath.normpath(item) for item in candidates))


def derive_source_reads(g, records: dict, native: dict, source_root: str, out_root: str) -> dict:
    """Derive source leaves and metadata-reader dependencies from current graph edges.

    ``records`` and ``native`` are the current indexed graph's edge records and
    ``Graph.native_metadata`` output for the selected edges. Additional license
    metadata producers are queried from that same graph as recursive ``-d``
    inputs are discovered; database state is never modified.
    """
    source_root = posixpath.normpath(str(source_root))
    out_root = posixpath.normpath(str(out_root))
    if not source_root.startswith("/") or not out_root.startswith("/"):
        raise SourceReadError("source_root and out_root must be absolute")
    if _inside(out_root, source_root) or _inside(source_root, out_root):
        raise SourceReadError("source_root and out_root must not contain one another")
    records = dict(records)
    native = dict(native)
    selected_edge_ids = tuple(records)
    additions: dict[int, set[str]] = {}
    notice_readers = []
    r8_embedded_includes = []
    metadata_cache = {}
    writer_cache = {}

    def edge_native(edge_id):
        if edge_id not in records:
            more, _ = g.edge_records([edge_id])
            records.update(more)
        if edge_id not in native:
            more = g.native_metadata({edge_id: records[edge_id]})
            native.update(more)
        return native.get(edge_id, {})

    def prefetch_edges(edge_ids):
        missing_records = [edge_id for edge_id in set(edge_ids) if edge_id not in records]
        if missing_records:
            more, _ = g.edge_records(missing_records)
            records.update(more)
        missing_native = [edge_id for edge_id in set(edge_ids) if edge_id not in native]
        if missing_native:
            batch = {edge_id: records[edge_id] for edge_id in missing_native if edge_id in records}
            if batch:
                native.update(g.native_metadata(batch))

    def invocations(edge_id):
        if edge_id not in metadata_cache:
            metadata = edge_native(edge_id)
            metadata_cache[edge_id] = []
            for argv, cwd in iter_invocations(metadata, source_root):
                expanded, source_responses, unresolved = _expand_inline_response(
                    argv, metadata, cwd, source_root, out_root)
                metadata_cache[edge_id].append((argv, cwd, expanded, source_responses, unresolved))
        return metadata_cache[edge_id]

    def license_writer(edge_id):
        if edge_id in writer_cache:
            return writer_cache[edge_id]
        result = None
        if LICENSE_TOOL not in edge_native(edge_id).get("command", ""):
            writer_cache[edge_id] = None
            return None
        for argv, cwd, expanded, source_responses, unresolved in invocations(edge_id):
            if any(posixpath.basename(token) == LICENSE_TOOL for token in expanded):
                if unresolved:
                    response_path = unresolved[0]
                    raise SourceReadError(
                        f"license metadata writer edge {edge_id} has unresolved response input: {response_path}")
                notices, dependencies = _license_options(expanded)
                result = {"edge": edge_id, "notices": notices,
                          "dependencies": dependencies, "cwd": cwd,
                          "outputs": records[edge_id]["outputs"],
                          "response_files": source_responses}
                break
        writer_cache[edge_id] = result
        return result

    def producer_batch(items):
        candidates = [_graph_path_candidates(path, cwd, source_root) for path, cwd in items]
        requested = sorted({candidate for options in candidates for candidate in options})
        owners = g.producers(requested, directory_fallback=False) if requested else {}
        found = []
        for options in candidates:
            owner = next((owners.get(candidate) for candidate in options if candidate in owners), None)
            if owner is None:
                found.append((None, None))
            else:
                graph_path = next(candidate for candidate in options if candidate in owners)
                found.append((graph_path, owner))
        return found

    def add(edge_id: int, path: str):
        additions.setdefault(edge_id, set()).add(path)

    notice_work = []
    for edge_id in selected_edge_ids:
        selected_record = records[edge_id]
        command = edge_native(edge_id).get("command", "")
        if not any(tool in command for tool in NOTICE_TOOLS):
            continue
        for _, _, argv, source_responses, unresolved in invocations(edge_id):
            program = posixpath.basename(argv[0]) if argv else ""
            if program not in NOTICE_TOOLS and not any(posixpath.basename(arg) in NOTICE_TOOLS for arg in argv):
                continue
            if unresolved:
                raise SourceReadError(
                    f"notice reader edge {edge_id} has unresolved response input: {unresolved[0]}")
            is_metadata_file = lambda path: path.endswith(".meta_lic") or posixpath.basename(path) == "meta_lic"
            roots = {path for path, _ in selected_record.get("deps", []) if is_metadata_file(path)}
            for token in argv:
                if token.startswith("-") or token.startswith("@"):
                    continue
                if is_metadata_file(token):
                    roots.add(token)
            details = {"edge": edge_id, "outputs": selected_record["outputs"],
                       "metadata_roots": sorted(roots), "metadata_writers": [],
                       "metadata_sources": [], "license_texts": set(),
                       "response_files": set(source_responses)}
            for response_file in source_responses:
                add(edge_id, response_file)
            notice_work.append({"details": details, "visited": set(),
                                "frontier": [(root, source_root) for root in sorted(roots)]})

    # Process metadata dependency frontiers across all notice consumers
    # together. This keeps native_metadata's temporary Ninja export batched by
    # depth rather than exporting once per metadata file/consumer.
    while True:
        frontier_entries = []
        for work in notice_work:
            details = work["details"]
            consumer = details["edge"]
            for path, cwd in work["frontier"]:
                frontier_entries.append((work, consumer, path, cwd))
            work["frontier"] = []
        if not frontier_entries:
            break
        resolved = producer_batch([(path, cwd) for _, _, path, cwd in frontier_entries])
        new_owners = sorted({owner for _, owner in resolved
                             if owner is not None and owner not in writer_cache})
        prefetch_edges(new_owners)
        for (work, consumer, path, cwd), (graph_path, owner) in zip(frontier_entries, resolved):
            normalized = graph_path or _absolute(path, cwd)
            identity = _absolute(normalized, source_root)
            if identity in work["visited"]:
                continue
            work["visited"].add(identity)
            add(consumer, normalized)
            if owner is None:
                if _inside(normalized, out_root):
                    raise SourceReadError("generated license metadata has no current producer: " + normalized)
                if not Path(normalized).is_file():
                    raise SourceReadError("license metadata input is missing: " + normalized)
                work["details"]["metadata_sources"].append(normalized)
                continue
            writer = license_writer(owner)
            if writer is None:
                work["details"]["metadata_sources"].append(normalized)
                continue
            work["details"]["metadata_writers"].append({"edge": owner, "output": normalized})
            for notice in writer["notices"]:
                source = _source_file(notice, writer["cwd"], source_root, out_root)
                add(consumer, source)
                work["details"]["license_texts"].add(source)
            for response_file in writer["response_files"]:
                add(consumer, response_file)
                work["details"]["response_files"].add(response_file)
            work["frontier"].extend((child, writer["cwd"]) for child in writer["dependencies"])

    for work in notice_work:
        details = work["details"]
        details["metadata_writers"] = sorted(
            {(item["edge"], item["output"]): item for item in details["metadata_writers"]}.values(),
            key=lambda item: (item["edge"], item["output"]))
        details["metadata_sources"] = sorted(set(details["metadata_sources"]))
        details["license_texts"] = sorted(details["license_texts"])
        details["response_files"] = sorted(details["response_files"])
        notice_readers.append(details)

    def proguard_config(path: str, cwd: str, edge_id: int, included: set[str], active: set[str], chain: list[str]):
        absolute = _absolute(path, cwd)
        if _inside(absolute, out_root):
            return
        if not _inside(absolute, source_root):
            raise SourceReadError("R8 Proguard config is outside source_root: " + absolute)
        if absolute in active:
            raise SourceReadError("recursive R8 Proguard include: " + " -> ".join(chain + [absolute]))
        if absolute in included:
            return
        if not Path(absolute).is_file():
            raise SourceReadError("R8 Proguard config is missing: " + absolute)
        included.add(absolute)
        active.add(absolute)
        add(edge_id, absolute)
        try:
            lines = Path(absolute).read_text().splitlines()
        except UnicodeDecodeError as error:
            raise SourceReadError("R8 Proguard config is not UTF-8: " + absolute) from error
        for line_number, line in enumerate(lines, 1):
            try:
                tokens = shlex.split(line, comments=True, posix=True)
            except ValueError as error:
                raise SourceReadError(f"invalid Proguard config {absolute}:{line_number}: {error}") from error
            index = 0
            while index < len(tokens):
                token = tokens[index]
                include = None
                if token == "-include":
                    if index + 1 >= len(tokens):
                        raise SourceReadError(f"missing -include path at {absolute}:{line_number}")
                    include = tokens[index + 1]
                    index += 2
                elif token.startswith("-include="):
                    include = token[len("-include="):]
                    index += 1
                elif token.startswith("@") and len(token) > 1:
                    include = token[1:]
                    index += 1
                else:
                    index += 1
                if include:
                    proguard_config(include, posixpath.dirname(absolute), edge_id,
                                    included, active, chain + [absolute])
        active.remove(absolute)

    for edge_id in selected_edge_ids:
        record = records[edge_id]
        if R8_CLASS not in edge_native(edge_id).get("command", ""):
            continue
        for argv, cwd, expanded, source_responses, unresolved in invocations(edge_id):
            if R8_CLASS not in expanded:
                continue
            if unresolved:
                response_path = unresolved[0]
                raise SourceReadError(f"R8 edge {edge_id} has unresolved response input: {response_path}")
            for response_file in source_responses:
                add(edge_id, response_file)
            configs = []
            index = 0
            while index < len(expanded):
                token = expanded[index]
                if token == "--pg-conf":
                    if index + 1 >= len(expanded):
                        raise SourceReadError("missing --pg-conf path for R8 edge " + str(edge_id))
                    configs.append(expanded[index + 1])
                    index += 2
                elif token.startswith("--pg-conf="):
                    configs.append(token[len("--pg-conf="):])
                    index += 1
                else:
                    index += 1
            included = set()
            for config in configs:
                proguard_config(config, cwd, edge_id, included, set(), [])
            r8_embedded_includes.append({"edge": edge_id, "outputs": record["outputs"],
                                         "configs": sorted(included),
                                         "response_files": sorted(source_responses)})

    return {"additions": {edge_id: sorted(paths) for edge_id, paths in sorted(additions.items())},
            "notice_readers": sorted(notice_readers, key=lambda item: item["edge"]),
            "r8_embedded_includes": sorted(r8_embedded_includes, key=lambda item: item["edge"])}
