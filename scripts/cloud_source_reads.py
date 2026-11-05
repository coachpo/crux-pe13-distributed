#!/usr/bin/env python3
"""Derive runtime source reads for selected native PE build actions."""
from __future__ import annotations

import os
from pathlib import Path
import posixpath
import re
import shlex


NOTICE_TOOLS = {"textnotice", "htmlnotice", "xmlnotice", "compliancenotice_shippedlibs",
                "shippedlibs"}
LICENSE_TOOL = "build_license_metadata"
R8_CLASS = "com.android.tools.r8.R8"
R8_COMPAT_CLASS = "com.android.tools.r8.compatproguard.CompatProguard"
R8_CONFIG_OUTPUTS = {"--pg-conf-output", "--desugared-lib-pg-conf-output"}
_R8_LAUNCHER = re.compile(r'''(?:^|[/\s"'\\;&()<>])(?:r8-compat-proguard|r8)(?=$|[\s"'\\;&()<>])''')


class SourceReadError(ValueError):
    pass


def _absolute(path: str, cwd: str) -> str:
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(cwd, path))


def _inside(path: str, root: str) -> bool:
    try:
        return posixpath.commonpath((path, root)) == root
    except ValueError:
        return False


def _read_rsp_tokens(content: str) -> list[str]:
    """Match the pinned Soong response.ReadRspFile single-pass grammar."""
    values, current = [], []
    quote = None
    escaping = False
    for char in content:
        if escaping:
            if quote == '"' and char not in ('"', "\\"):
                current.append("\\")
            current.append(char)
            escaping = False
        elif char == "\\" and quote != "'":
            escaping = True
        elif quote is None and char in ('"', "'"):
            quote = char
        elif quote is not None and char == quote:
            quote = None
        elif quote is None and char.isspace():
            if current:
                values.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        values.append("".join(current))
    return values


def _raw_shell_segments(command: str, newlines: bool = False):
    """Yield raw shell command segments while preserving quote provenance."""
    current = []
    quote = None
    escaped = False
    index = 0
    separators = ";|&()" + ("\n" if newlines else "")
    while index < len(command):
        char = command[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
        elif char == "\\" and quote != "'":
            current.append(char)
            escaped = True
            index += 1
        elif quote:
            current.append(char)
            if char == quote:
                quote = None
            index += 1
        elif char in ("'", '"'):
            quote = char
            current.append(char)
            index += 1
        elif char in separators:
            segment = "".join(current).strip()
            current = []
            if segment:
                yield segment, None
            if char in "|&" and index + 1 < len(command) and command[index + 1] == char:
                separator = char * 2
                index += 2
            else:
                separator = char
                index += 1
            yield None, separator
        else:
            current.append(char)
            index += 1
    segment = "".join(current).strip()
    if segment:
        yield segment, None


def _shell_tokens(segment: str) -> list[str]:
    lexer = shlex.shlex(segment, posix=True, punctuation_chars="<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _shell_command_contexts(command: str, source_root: str, declared_scripts=None, strict_scripts=True):
    """Yield leaf contexts, optionally resolving only recorded shell scripts."""
    position = [0]

    def visit(script: str, initial_cwd: str, active_scripts=frozenset()):
        cwd_stack = [initial_cwd]

        def flush(raw):
            if not raw:
                return []
            segment = _shell_tokens(raw)
            if not segment:
                return []
            cwd = cwd_stack[-1]
            index = 0
            while index < len(segment):
                token = segment[index]
                name = posixpath.basename(token)
                assignment = token.split("=", 1)[0] if "=" in token else ""
                if assignment and assignment.replace("_", "a").isalnum():
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
                                cwd = _absolute(segment[index + 1], cwd)
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
                return []
            argv = segment[index:]
            program = posixpath.basename(argv[0])
            if program == "cd":
                path_index = 2 if len(argv) > 1 and argv[1] == "--" else 1
                if path_index < len(argv):
                    cwd_stack[-1] = _absolute(argv[path_index], cwd)
                return []
            if program in {"bash", "sh", "dash"}:
                for arg_index, option in enumerate(argv[1:], 1):
                    if option.startswith("-") and not option.startswith("--") and "c" in option[1:]:
                        if arg_index + 1 < len(argv):
                            return list(visit(argv[arg_index + 1], cwd, active_scripts))
                        return []
                if declared_scripts is not None:
                    arguments = argv[1:]
                    if arguments[:1] == ["--"]:
                        arguments = arguments[1:]
                    if len(arguments) != 1 or arguments[0].startswith("-"):
                        if strict_scripts:
                            raise SourceReadError("unsupported recorded shell script invocation")
                        return []
                    path = _absolute(arguments[0], cwd)
                    if path not in declared_scripts:
                        if strict_scripts:
                            raise SourceReadError("unrecorded shell script: " + path)
                        return []
                    content = declared_scripts[path]
                    if not isinstance(content, str):
                        raise SourceReadError("recorded shell script has no content: " + path)
                    if path in active_scripts:
                        raise SourceReadError("recursive recorded shell script: " + path)
                    return list(visit(content, cwd, active_scripts | {path}))
                return []
            command_position = position[0]
            position[0] += 1
            return [(argv, cwd, command_position, raw)]

        contexts = []
        for raw, separator in _raw_shell_segments(script, newlines=declared_scripts is not None):
            if raw is not None:
                contexts.extend(flush(raw))
            elif separator == "(":
                cwd_stack.append(cwd_stack[-1])
            elif separator == ")" and len(cwd_stack) > 1:
                cwd_stack.pop()
        return contexts

    yield from visit(command, source_root)


def iter_invocations(metadata: dict, source_root: str, declared_scripts=None, strict_scripts=True):
    """Yield (argv, cwd) command contexts, unwrapping common shell launchers."""
    for argv, cwd, _, _ in _shell_command_contexts(
            metadata.get("command", ""), source_root, declared_scripts, strict_scripts):
        yield argv, cwd


def _response_target(token: str, cwd: str, response_path: str) -> bool:
    return token.startswith("@") and len(token) > 1 and _absolute(token[1:], cwd) == response_path


def _make_argument_file(command: str, source_root: str, response_path: str,
                        license_position: int, declared_scripts=None) -> str:
    """Reconstruct Kati dump-words-to-file from literal writes in this edge."""
    contexts = list(_shell_command_contexts(command, source_root, declared_scripts))
    matching = [(argv, cwd, position) for argv, cwd, position, _ in contexts
                if position == license_position
                and any(posixpath.basename(token) == LICENSE_TOOL for token in argv)
                and any(_response_target(token, cwd, response_path) for token in argv)]
    if len(matching) != 1:
        raise SourceReadError(
            f"license metadata writer edge has no unique consumer for Make response: {response_path}")
    consumer_position = matching[0][2]
    events = []

    def resolved(token, cwd):
        return _absolute(token, cwd)

    for argv, cwd, position, raw in contexts:
        if position == consumer_position:
            continue
        program = posixpath.basename(argv[0]) if argv else ""
        redirect_index = None
        redirect_kind = None
        redirect_path = None
        for index, token in enumerate(argv[:-1]):
            if token in (">", ">>", "1>", "1>>"):
                redirect_index, redirect_kind = index, token
                redirect_path = resolved(argv[index + 1], cwd)
                break
            if token.startswith(">>") and len(token) > 2:
                redirect_index, redirect_kind = index, ">>"
                redirect_path = resolved(token[2:], cwd)
                break
            if token.startswith(">") and len(token) > 1:
                redirect_index, redirect_kind = index, ">"
                redirect_path = resolved(token[1:], cwd)
                break
        if redirect_path == response_path:
            if position > consumer_position:
                raise SourceReadError("Make response is written after its license reader: " + response_path)
            if program != "echo" or redirect_kind != ">>":
                raise SourceReadError("unrecognized Make response writer for " + response_path)
            if redirect_index != len(argv) - 2:
                raise SourceReadError("Make response echo has extra redirections or operands: " + response_path)
            arguments = argv[:redirect_index]
            if (len(arguments) != 3 or posixpath.basename(arguments[0]) != "echo"
                    or arguments[1] != "-n"):
                raise SourceReadError("Make response echo is not a literal echo -n append: " + response_path)
            if not re.fullmatch(r"\s*(?:[^\s]+/)?echo\s+-n\s+'[^']*'\s*>>\s*(?:.+?)\s*", raw):
                raise SourceReadError("Make response echo payload is not single-quoted literal text: " + response_path)
            events.append((position, "echo", arguments[2]))
            continue

        operands = [token for token in argv[1:] if token != "--" and not token.startswith("-")]
        matching_operands = [token for token in operands if resolved(token, cwd) == response_path]
        if not matching_operands:
            continue
        if position > consumer_position:
            raise SourceReadError("Make response is accessed after its license reader: " + response_path)
        if len(operands) != 1:
            raise SourceReadError("Make response path is mixed with unsupported command operands: " + response_path)
        if program not in {"rm", "touch"}:
            raise SourceReadError("unrecognized Make response file operation: " + response_path)
        events.append((position, program, None))

    removals = [event for event in events if event[1] == "rm"]
    touches = [event for event in events if event[1] == "touch"]
    echoes = [event for event in events if event[1] == "echo"]
    if len(removals) != 1 or len(touches) != 1:
        raise SourceReadError("Make response lacks the expected rm/touch initializer: " + response_path)
    if not (removals[0][0] < touches[0][0] < consumer_position):
        raise SourceReadError("Make response initializer order is unsupported: " + response_path)
    if any(not touches[0][0] < event[0] < consumer_position for event in echoes):
        raise SourceReadError("Make response append is outside its initialization/read window: " + response_path)
    return "".join(event[2] for event in sorted(echoes))


def _expand_inline_response(argv: list[str], metadata: dict, cwd: str,
                            source_root: str, out_root: str,
                            one_level: bool = False) -> tuple[list[str], list[str], list[str]]:
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
                nested = (_read_rsp_tokens(content) if one_level else
                          shlex.split(content, comments=False, posix=True))
                if one_level:
                    expanded.extend(nested)
                else:
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
                    text = Path(token_path).read_text()
                    nested = (_read_rsp_tokens(text) if one_level else
                              shlex.split(text, comments=False, posix=True))
                except (OSError, UnicodeDecodeError, ValueError) as error:
                    raise SourceReadError("cannot read source response file " + token_path + ": " + str(error)) from error
                source_files.add(token_path)
                if one_level:
                    expanded.extend(nested)
                else:
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


def r8_invocation(argv):
    """Identify a real launcher/main class, not a mention in its arguments."""
    index = 0
    while index < len(argv) and posixpath.basename(argv[index]) in {"exec", "command", "time"}:
        index += 1
    if index == len(argv):
        return None
    program = posixpath.basename(argv[index])
    index += 1
    if program == "r8-compat-proguard":
        return "compat", index
    if program == "r8":
        return "modern", index
    if program != "java":
        return None
    while index < len(argv):
        token = argv[index]
        if token in {"-cp", "-classpath", "--class-path", "--module-path", "-p"}:
            index += 2
        elif token in {"-jar", "-m", "--module"}:
            return None
        elif token.startswith("-"):
            index += 1
        else:
            return ({R8_CLASS: ("modern", index + 1), R8_COMPAT_CLASS: ("compat", index + 1)}
                    .get(token))
    return None


def r8_config_argument_indices(argv):
    """Config operands handled by the R8 reader, not generic file scanning."""
    invocation = r8_invocation(argv)
    if invocation is None:
        return set()
    kind, index = invocation
    flags = ({"-include", "-basedirectory"} if kind == "compat" else
             {"--pg-conf"} | R8_CONFIG_OUTPUTS)
    indices = set()
    while index < len(argv):
        token = argv[index]
        if kind == "modern":
            token = token.strip()
        elif token in _COMPAT_VALUE_FLAGS:
            index += 2
            continue
        if token in flags or (kind == "compat" and token == "@"):
            indices.update(range(index, min(index + 2, len(argv))))
            index += 2
        else:
            if (token.partition("=")[0] in flags
                    or (kind == "compat" and (token.startswith("@")
                        or any(token.startswith(flag + " ") for flag in flags)))):
                indices.add(index)
            index += 1
    return indices


# CompatProguard in the pinned 3.3.20-dev+aosp6 launcher consumes these options
# before reconstructing its remaining argv as ProGuard configuration text.
_COMPAT_VALUE_FLAGS = {"--min-api", "--output", "--main-dex-list", "--map-id-template",
                       "--source-file-template", "--deps-file"}
_COMPAT_BOOLEAN_FLAGS = {"-h", "--help", "--debug", "--release", "--force-proguard-compatibility",
                         "--no-data-resources", "--multi-dex", "--no-vertical-class-merging",
                         "--minimal-main-dex", "--core-library", "--no-locals"}


def _compat_config(argv):
    lines, line = [], []
    index = 0
    while index < len(argv) and argv[index].startswith("-J"):
        index += 1
    while index < len(argv):
        token = argv[index]
        index += 1
        if token in _COMPAT_VALUE_FLAGS:
            if index == len(argv):
                raise SourceReadError("missing R8 compatibility option value: " + token)
            index += 1
            continue
        if token in _COMPAT_BOOLEAN_FLAGS or token.startswith("--main-dex-list="):
            continue
        if token.startswith("-") and line:
            lines.append(" ".join(line))
            line = []
        line.append(token)
    if line:
        lines.append(" ".join(line))
    return "\n".join(lines)


_PROGUARD_TOKEN = re.compile(r'''\s+|#[^\r\n]*|(["'])(.*?)\1|[{}(),]|[^\s{}(),"']+''', re.DOTALL)
_CLASS_OPTIONS = {"-if", "-assumenosideeffects", "-assumevalues", "-whyareyoukeeping",
                  "-checkdiscard", "-identifiernamestring", "-assumenoexternalsideeffects",
                  "-assumenoexternalreturnvalues", "-assumenoescapingparameters"}
_CLASS_TYPES = {"class", "interface", "enum", "@interface"}
_NO_ARGUMENT_OPTIONS = {"-dontshrink", "-dontoptimize", "-dontobfuscate", "-dontpreverify",
                        "-allowaccessmodification", "-verbose", "-keepparameternames",
                        "-useuniqueclassmembernames", "-dontusemixedcaseclassnames"}


def _proguard_directives(text, origin):
    """Extract literal file directives, not the full ProGuard rule language.

    Match the pinned parser's file-name quoting and option boundaries. In
    particular, @ inside a class header/member block is not an include. An @
    after an opaque option is ambiguous and is rejected rather than guessed.
    """
    text = text.removeprefix("\ufeff")
    tokens = []
    offset = 0
    while offset < len(text):
        match = _PROGUARD_TOKEN.match(text, offset)
        if match is None:
            raise SourceReadError(f"invalid ProGuard token in {origin}:{text.count(chr(10), 0, offset) + 1}")
        offset = match.end()
        token = match.group()
        if not token.isspace() and not token.startswith("#"):
            tokens.append((match[2] if match[1] else token, bool(match[1]), match.start()))
    index, stack, state = 0, [], "boundary"
    while index < len(tokens):
        token, quoted, start = tokens[index]
        index += 1
        if not quoted and token in {"{", "("}:
            stack.append(token)
            continue
        if not quoted and token in {"}", ")"}:
            if stack and stack[-1] == ({"}": "{", ")": "("}[token]):
                stack.pop()
                if token == "}" and not stack:
                    state = "boundary"
            continue
        if stack:
            continue
        if not quoted and token.startswith(("-include", "-basedirectory")) and token not in {
                "-include", "-basedirectory"}:
            raise SourceReadError("unsupported joined ProGuard file directive in " + origin + ": " + token)
        directive = None
        if not quoted and token in {"-include", "-basedirectory"}:
            directive = token
        elif not quoted and token.startswith("@"):
            if state == "class-type" and token == "@interface":
                state = "class-name"
                continue
            if state in {"class-type", "parent-name"}:
                if token == "@" and index < len(tokens):
                    index += 1  # annotation name, not an include
                continue
            if state not in {"boundary", "class-tail"}:
                raise SourceReadError("ambiguous ProGuard @ operand in " + origin + ": " + token)
            directive = "-include"
        if directive is not None:
            if token.startswith("@") and len(token) > 1:
                start += 1
            else:
                if index == len(tokens) or (not tokens[index][1] and tokens[index][0].startswith("-")):
                    raise SourceReadError(f"missing {directive} path in {origin}")
                start = tokens[index][2]
            # Config syntax permits punctuation in an unquoted filename even
            # though those same characters delimit class rules elsewhere.
            if text[start] in "\"'":
                end = text.index(text[start], start + 1) + 1
                value = text[start + 1:end - 1]
            else:
                end = start
                while end < len(text) and not text[end].isspace() and text[end] != "(":
                    end += 1
                value = text[start:end]
            while index < len(tokens) and tokens[index][2] < end:
                index += 1
            if not value:
                raise SourceReadError(f"missing {directive} path in {origin}")
            yield directive, value
            state = "boundary"
            continue
        if not quoted and token.startswith("-"):
            if (token in _CLASS_OPTIONS or (token.startswith("-keep") and token not in {
                    "-keepattributes", "-keeppackagenames", "-keepdirectories",
                    "-keepparameternames", "-keepkotlinmetadata"})):
                state = "class-type"
            else:
                state = "boundary" if token in _NO_ARGUMENT_OPTIONS else "opaque"
        elif state == "class-type" and token.lstrip("!") in _CLASS_TYPES:
            state = "class-name"
        elif state in {"class-name", "parent-name"}:
            state = "class-tail"
        elif state == "class-tail" and token in {"extends", "implements", ","}:
            state = "class-name" if token == "," else "parent-name"


def _literal_r8_path(value, cwd):
    path = value if value.startswith("/") else posixpath.join(cwd, value)
    if not value or any(character in path for character in "$`*?[]<>\n\r"):
        raise SourceReadError("unresolved R8 path: " + value)
    return posixpath.normpath(path)


def r8_flagfile_arguments(content):
    """Pinned R8 FlagFile expands original @argv once, one argument per line."""
    return content.splitlines()


def r8_source_reads(metadata, source_root, out_root, read_source=None):
    """Discover literal SOURCE ProGuard reads without probing OUT config files."""
    command = metadata.get("command") or ""
    content = metadata.get("rspfile_content")
    if not any(_R8_LAUNCHER.search(text) or R8_CLASS in text or R8_COMPAT_CLASS in text
               for text in (command, content or "")):
        return None
    source_root = posixpath.normpath(str(source_root))
    out_root = posixpath.normpath(str(out_root))
    response = metadata.get("rspfile")
    response = _absolute(response, source_root) if isinstance(response, str) else None
    scripts = {response: content} if response is not None else None
    included, responses, active = set(), set(), []
    found = False

    def source_text(path):
        if not _inside(path, source_root):
            raise SourceReadError("R8 Proguard config is outside source_root: " + path)
        if read_source is not None:
            return read_source(path)
        file = Path(path)
        resolved = str(file.resolve())
        if _inside(resolved, out_root) or not _inside(resolved, source_root):
            raise SourceReadError("R8 Proguard config link escapes SOURCE: " + path)
        if not file.is_file():
            raise SourceReadError("R8 Proguard config is missing: " + path)
        try:
            return file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise SourceReadError("cannot read R8 Proguard config: " + path) from error

    def visit(value, base):
        path = _literal_r8_path(value, base)
        if _inside(path, out_root):
            return
        if path in active:
            raise SourceReadError("recursive R8 Proguard include: " + " -> ".join(active + [path]))
        if path in included:
            return
        text = source_text(path)
        included.add(path)
        active.append(path)
        follow(text, posixpath.dirname(path), path)
        active.pop()

    def follow(text, base, origin):
        for directive, value in _proguard_directives(text, origin):
            if directive == "-basedirectory":
                base = _literal_r8_path(value, base)
            else:
                visit(value, base)

    for argv, cwd in iter_invocations(metadata, source_root, scripts, strict_scripts=False):
        invocation = r8_invocation(argv)
        if invocation is None:
            continue
        found = True
        kind, start = invocation
        if kind == "compat":
            follow(_compat_config(argv[start:]), cwd, "CompatProguard argv")
            continue
        expanded = []
        for token in argv[start:]:
            if not token.startswith("@"):
                expanded.append(token)
                continue
            path = _literal_r8_path(token[1:], cwd)
            if path == response and isinstance(content, str):
                text = content
            elif _inside(path, out_root) or path == response:
                raise SourceReadError("unresolved response input: " + path)
            else:
                text = source_text(path)
                responses.add(path)
            expanded.extend(r8_flagfile_arguments(text))
        index = 0
        while index < len(expanded):
            token = expanded[index].strip()
            index += 1
            if token == "--pg-conf" or token in R8_CONFIG_OUTPUTS:
                if index == len(expanded):
                    raise SourceReadError("missing R8 option path: " + token)
                if token == "--pg-conf":
                    visit(expanded[index], cwd)
                index += 1
            elif token.startswith("--pg-conf="):
                visit(token.partition("=")[2], cwd)
    return {"configs": sorted(included), "response_files": sorted(responses)} if found else None


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

    def invocations(edge_id, one_level=False):
        cache_key = (edge_id, one_level)
        if cache_key not in metadata_cache:
            metadata = edge_native(edge_id)
            metadata_cache[cache_key] = []
            for argv, cwd, position, raw in _shell_command_contexts(metadata.get("command", ""), source_root):
                expanded, source_responses, unresolved = _expand_inline_response(
                    argv, metadata, cwd, source_root, out_root, one_level=one_level)
                metadata_cache[cache_key].append(
                    (argv, cwd, expanded, source_responses, unresolved, position, raw))
        return metadata_cache[cache_key]

    def license_writer(edge_id):
        if edge_id in writer_cache:
            return writer_cache[edge_id]
        result = None
        if LICENSE_TOOL not in edge_native(edge_id).get("command", ""):
            writer_cache[edge_id] = None
            return None
        metadata = edge_native(edge_id)
        for argv, cwd, expanded, source_responses, unresolved, position, _ in invocations(edge_id, one_level=True):
            if any(posixpath.basename(token) == LICENSE_TOOL for token in expanded):
                if unresolved:
                    response_args = {}
                    for response_path in unresolved:
                        if not _inside(response_path, out_root):
                            raise SourceReadError(
                                f"license metadata writer edge {edge_id} has unresolved response input: {response_path}")
                        try:
                            payload = _make_argument_file(metadata.get("command", ""), source_root,
                                                          response_path, position)
                        except SourceReadError as error:
                            raise SourceReadError(
                                f"license metadata writer edge {edge_id} has unresolved response input "
                                f"{response_path}: {error}") from error
                        response_args[response_path] = _read_rsp_tokens(payload)
                    resolved_args = []
                    replaced = set()
                    # Replace only original command-line @ operands. Tokens
                    # emitted by another response are literal to the Go tool.
                    for token in argv:
                        if token.startswith("@") and len(token) > 1:
                            response_path = _absolute(token[1:], cwd)
                            if response_path in response_args:
                                resolved_args.extend(response_args[response_path])
                                replaced.add(response_path)
                                continue
                        token_args, _, remaining = _expand_inline_response(
                            [token], metadata, cwd, source_root, out_root, one_level=True)
                        if remaining:
                            raise SourceReadError('Unresolved license response operand: ' + token)
                        resolved_args.extend(token_args)
                    if replaced != set(response_args):
                        missing = sorted(set(response_args) - replaced)
                        raise SourceReadError(
                            f"license metadata writer edge {edge_id} has unmatched response input: "
                            + ", ".join(missing))
                    expanded = resolved_args
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
        for _, _, argv, source_responses, unresolved, _, _ in invocations(edge_id):
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

    for edge_id in selected_edge_ids:
        try:
            reads = r8_source_reads(edge_native(edge_id), source_root, out_root)
        except SourceReadError as error:
            raise SourceReadError(f"R8 edge {edge_id}: {error}") from error
        if reads is None:
            continue
        for path in reads["configs"] + reads["response_files"]:
            add(edge_id, path)
        r8_embedded_includes.append({"edge": edge_id, "outputs": records[edge_id]["outputs"], **reads})

    return {"additions": {edge_id: sorted(paths) for edge_id, paths in sorted(additions.items())},
            "notice_readers": sorted(notice_readers, key=lambda item: item["edge"]),
            "r8_embedded_includes": sorted(r8_embedded_includes, key=lambda item: item["edge"])}
