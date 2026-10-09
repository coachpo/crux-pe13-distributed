#!/usr/bin/env python3
"""Package the cold inputs of a Ninja graph slice for a standard Linux runner."""

import argparse
import ast
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile


DEFAULT_SOURCE = "/home/qingli/crux-pe13-offline-2026-09-25/pe13"
DEFAULT_OUT = "/home/qingli/crux-pe13-install-regression-2026-10-08/android-out"
FORBIDDEN_PARTS = {".git", ".repo", ".ssh", ".gnupg", ".aws"}
COMPILED_SUFFIXES = {
    ".o", ".a", ".so", ".jar", ".apk", ".dex", ".class", ".art",
    ".odex", ".vdex", ".img", ".dtb", ".dtbo", ".ko", ".bc", ".zip",
}
HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx", ".inc", ".inl", ".def"}
SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".s", ".S", ".m", ".mm"}


class CapsuleError(Exception):
    pass


def optional_probe(path, method="is_file"):
    # Commands such as Kati writeFile carry JSON as a quoted operand. Such text
    # can exceed a filename's byte limit and is never an input path.
    try:
        return getattr(path, method)()
    except OSError as error:
        if error.errno == errno.ENAMETOOLONG:
            return False
        raise


def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


class DigestCache:
    """Reuse transfer digests while the local source identity remains unchanged."""
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.entries = {}
        self.reused = self.computed = 0
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text())
            if data.get("schema_version") != 1:
                raise CapsuleError("Unsupported digest cache schema")
            self.entries = data["entries"]

    def identity(self, path):
        info = Path(path).stat()
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

    def __call__(self, path):
        key = str(Path(path).resolve())
        identity = self.identity(path)
        prior = self.entries.get(key)
        if prior and prior["identity"] == identity:
            self.reused += 1
            return prior["sha256"]
        result = digest(path)
        if identity != self.identity(path):
            raise CapsuleError("Input changed while its digest was computed: %s" % path)
        self.entries[key] = {"identity": identity, "sha256": result}
        self.computed += 1
        return result

    def save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", prefix=self.path.name + ".", dir=self.path.parent,
                                         delete=False) as temporary:
            json.dump({"schema_version": 1, "entries": self.entries}, temporary, sort_keys=True)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        temporary_path.replace(self.path)


def beneath(path, root):
    return path == root or root in path.parents


def make_dependencies(text):
    """Read GCC Make depfiles, including escaped spaces and line continuations."""
    text = text.replace("\\\n", " ")
    colon = re.search(r"(?<!\\):", text)
    if not colon:
        return []
    dependencies = []
    for token in re.findall(r"(?:\\.|[^\s])+", text[colon.end():]):
        if token.endswith(":"):
            continue  # GCC -MP emits an extra empty rule for each header.
        dependencies.append(re.sub(r"\\(.)", r"\1", token))
    return dependencies


def command_tokens(command):
    try:
        # shlex combines adjacent punctuation into "))". Shell parentheses
        # delimit separate scopes; preserve quoted and escaped literal names.
        separated, quote, index = [], None, 0
        while index < len(command):
            character = command[index]
            if character == "\\" and quote != "'" and index + 1 < len(command):
                separated.append(command[index:index + 2])
                index += 2
                continue
            if quote:
                separated.append(character)
                if character == quote:
                    quote = None
            elif character in {"'", '"'}:
                quote = character
                separated.append(character)
            else:
                separated.append(" " + character + " " if character in {"(", ")"} else character)
            index += 1
        lexer = shlex.shlex("".join(separated), posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError as error:
        raise CapsuleError("Cannot parse expanded command: %s" % error) from error


def command_operand(token):
    if token.startswith("@"):
        return token[1:]
    return token.partition("=")[2] if "=" in token else token


def command_invocations(tokens):
    """Locate the program of each simple shell command, including env prefixes."""
    start = 0
    for end in range(len(tokens) + 1):
        if end < len(tokens) and tokens[end] not in {";", "&&", "||", "|", "&", "(", ")"}:
            continue
        program = start
        while program < end and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[program]):
            program += 1
        if program < end and Path(tokens[program]).name == "env":
            program += 1
            while program < end and (tokens[program].startswith("-")
                                     or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[program])):
                program += 1
        if program < end:
            yield program, end
        start = end + 1


def substitution_positions(tokens):
    stack, positions = [], set()
    for index, token in enumerate(tokens):
        if token == "(":
            stack.append((bool(stack) and stack[-1]) or (index > 0 and tokens[index - 1] in {"$", "<", ">"}))
        elif token == ")" and stack:
            stack.pop()
        elif stack and stack[-1]:
            positions.add(index)
    return positions


def command_completion(tokens, end):
    # A trailing process-substitution redirection belongs to the parent
    # invocation. Its own commands remain separately inspected as readers.
    while end < len(tokens) and tokens[end] == "(" and end > 0 and tokens[end - 1] in {"<", ">"}:
        depth = 1
        end += 1
        while end < len(tokens) and depth:
            depth += (tokens[end] == "(") - (tokens[end] == ")")
            end += 1
    while end < len(tokens) and tokens[end] == ")":
        end += 1
    return end


def response_context(command, response=None):
    tokens = command_tokens(command)
    for position, end in command_invocations(tokens):
        name = Path(tokens[position]).name
        if name in {"protoc", "aprotoc", "build_license_metadata", "soong_zip"} and (response is None or any(
                token.startswith("@") and os.path.normpath(token[1:]) == os.path.normpath(response)
                for token in tokens[position + 1:end])):
            return name
        if tokens[position] in {".", "source"} and (response is None or any(
                os.path.normpath(token) == os.path.normpath(response) for token in tokens[position + 1:end])):
            return "shell-source"
        if name in {"bash", "sh", "dash"}:
            for index in range(position + 1, len(tokens) - 1):
                if tokens[index].startswith("-") and "c" in tokens[index][1:]:
                    context = response_context(tokens[index + 1], response)
                    if context:
                        return context
    return None


def cpp_invocations(tokens):
    names = {"clang", "clang++", "gcc", "g++", "cc", "c++", "header-abi-dumper", "clang-tidy"}
    for program, end in command_invocations(tokens):
        name = Path(tokens[program]).name
        if name in names or (name == "ccache" and program + 1 < end and Path(tokens[program + 1]).name in names):
            yield program, end


def cpp_frontend(command, response=None):
    tokens = command_tokens(command)
    for program, end in cpp_invocations(tokens):
        if response is None or any(token.startswith("@") and os.path.normpath(token[1:]) == os.path.normpath(response)
                                   for token in tokens[program + 1:end]):
            return True
    for program, end in command_invocations(tokens):
        name = Path(tokens[program]).name
        if name in {"bash", "sh", "dash"}:
            for index in range(program + 1, end - 1):
                if tokens[index].startswith("-") and "c" in tokens[index][1:] and cpp_frontend(tokens[index + 1], response):
                    return True
    return False


def rsp_callers(command, response):
    tokens = command_tokens(command)
    cpp_programs = {program for program, _ in cpp_invocations(tokens)}
    callers = []
    for program, end in command_invocations(tokens):
        if any(token.startswith("@") and os.path.normpath(token[1:]) == os.path.normpath(response)
               for token in tokens[program + 1:end]):
            name = Path(tokens[program]).name
            if program in cpp_programs:
                if name == "ccache":
                    name = Path(tokens[program + 1]).name
                dash = next((index for index in range(program + 1, end) if tokens[index] == "--"), None)
                for index in range(program + 1, end):
                    if tokens[index].startswith("@") and os.path.normpath(tokens[index][1:]) == os.path.normpath(response):
                        after_dash = dash is not None and index > dash
                        callers.append((name + " --" if name in {"header-abi-dumper", "clang-tidy"} and after_dash else name, True))
            else:
                callers.append((Path(tokens[program]).name, False))
        if Path(tokens[program]).name in {"bash", "sh", "dash"}:
            for index in range(program + 1, end - 1):
                if tokens[index].startswith("-") and "c" in tokens[index][1:]:
                    callers.extend(rsp_callers(tokens[index + 1], response))
    return list(dict.fromkeys(callers))


class ActionTemporaries:
    """Literal outputs that become fresh at a specific point of one action."""
    def __init__(self, files=(), directories=(), inherited=None, barriers=()):
        self.files = dict(files)
        self.directories = dict(directories)
        self.position = -1
        self.inherited_files, self.inherited_directories = inherited or (set(), set())
        self.barriers = tuple(barriers)

    def active(self, position):
        return position <= self.position and not any(position <= barrier <= self.position for barrier in self.barriers)

    def __contains__(self, path):
        return (path in self.inherited_files
                or self.active(self.files.get(path, float("inf")))
                or any(beneath(path, root) for root in self.inherited_directories)
                or any(self.active(position) and beneath(path, root)
                       for root, position in self.directories.items()))

    def snapshot(self):
        return (self.inherited_files | {path for path, position in self.files.items() if self.active(position)},
                self.inherited_directories | {path for path, position in self.directories.items()
                                             if self.active(position)})


class Collector:
    def __init__(self, manifest, source_root, out_root, allowed_generated=(), digester=None,
                 selection_roots=None, scan_cache=None):
        self.manifest = manifest
        self.source_root = Path(source_root).resolve()
        self.out_root = Path(out_root).resolve()
        self.entries = {}
        self.virtual_files = {}
        self.errors = set()
        self.outputs = {self.path(item) for item in manifest.get("outputs", [])}
        self.external = {self.path(item) for item in manifest.get("external_inputs", [])}
        self.aux_outputs = {self.path(item) for item in manifest.get("depfiles", []) + manifest.get("rspfiles", [])}
        self.aux_outputs.update(self.path(edge[key]) for edge in manifest.get("edges", [])
                                for key in ("depfile", "rspfile") if edge.get(key))
        self.response_contents = {self.path(edge["rspfile"]): edge["rspfile_content"]
                                  for edge in manifest.get("edges", [])
                                  if edge.get("rspfile") and edge.get("rspfile_content")}
        self.output_dirs = ({self.path(item) for item in manifest.get("output_dirs", [])}
                            | {path for path in self.outputs if path.is_dir()})
        self.external_dirs = ({self.path(item) for item in manifest.get("external_input_dirs", [])}
                              | {path for path in self.external if path.is_dir()})
        self.allowed_generated = {}
        for item in manifest.get("allowed_generated_inputs", []):
            if not isinstance(item, dict) or not item.get("reason"):
                raise CapsuleError("allowed_generated_inputs needs {path, reason} entries")
            self.allowed_generated[self.path(item["path"])] = item["reason"]
        for item in allowed_generated:
            path, separator, reason = item.partition("=")
            if not separator or not reason:
                raise CapsuleError("--allow-generated requires PATH=REASON")
            self.allowed_generated[self.path(path)] = reason
        self.external_roots = set()
        self.used_generated = set()
        self.system_tools = set()
        self.required_interpreters = {}
        self.assembler_scan_cache = set()
        self.proto_scan_cache = set()
        self.command_temporaries = None
        self.cpp_reader = False
        self.cpp_binding = False
        self.generated_include_contract = manifest.get("generated_include_contract")
        self.generated_include_roots = set()
        self.generated_required_headers = set()
        self.generated_processed_roots = set()
        self.compiler_commands = set()
        if self.generated_include_contract is not None:
            contract = self.generated_include_contract
            if contract.get("schema_version") != 1:
                raise CapsuleError("Unsupported generated_include_contract schema")
            self.generated_include_roots = {self.path(value) for value in contract.get("search_roots", [])}
            self.generated_required_headers = {self.path(value) for value in contract.get("required_headers", [])}
            if any(not beneath(path, self.out_root) for path in self.generated_include_roots | self.generated_required_headers):
                raise CapsuleError("Generated compiler include contracts must stay under OUT")
            for path in self.generated_required_headers:
                if not self.produced(path):
                    raise CapsuleError("Generated header contract has no selected producer or receipt import: %s" % path)
            for value in contract.get("owned_dirs", []):
                if self.path(value) not in self.output_dirs | self.external_dirs:
                    raise CapsuleError("Generated include owned directory is not declared for transport: %s" % value)
            for context in contract.get("compiler_contexts", []):
                index = context.get("edge_index")
                edges = manifest.get("edges", [])
                if (not isinstance(index, int) or index < 0 or index >= len(edges)
                        or context.get("reader") != "c_cpp_frontend"
                        or self.path(context.get("primary_output", "")) not in {self.path(path) for path in edges[index].get("outputs", [])}):
                    raise CapsuleError("Invalid compiler_contexts edge binding")
                if any(self.path(root) not in self.generated_include_roots for root in context.get("search_roots", [])):
                    raise CapsuleError("Compiler context contains an unlisted generated include root")
                if not cpp_frontend(edges[index]["command"]):
                    raise CapsuleError("Compiler context is not a supported C/C++ frontend")
                self.compiler_commands.add(edges[index]["command"])
        self.scan_cache = scan_cache if scan_cache is not None else set()
        self.digester = digester or digest
        self.selection_roots = None
        if selection_roots is not None:
            self.selection_roots = {self.path(path) for path in selection_roots}
            self.selection_roots.update(path.resolve() for path in tuple(self.selection_roots))

    def selected(self, path):
        return self.selection_roots is None or any(beneath(path, root) for root in self.selection_roots)

    def path(self, value):
        if isinstance(value, dict):
            value = value["path"]
        path = Path(value)
        if not path.is_absolute():
            path = self.source_root / path
        return Path(os.path.normpath(str(path)))

    def excluded(self, path):
        return any(part in FORBIDDEN_PARTS for part in path.parts)

    def produced(self, path):
        return ((self.command_temporaries is not None and path in self.command_temporaries)
                or path in self.outputs or path in self.external or path in self.aux_outputs
                or any(beneath(path, root) for root in self.output_dirs | self.external_dirs))

    def register(self, path, reason):
        self.entries.setdefault(path, set()).add(reason)

    def symlink_ancestors(self, path, reason):
        # Archive the link and the actual target, never entries below a link.
        current, pending = Path(path.anchor), list(path.parts[1:])
        while pending:
            part = pending.pop(0)
            if part == ".":
                continue
            if part == "..":
                current = current.parent
                continue
            candidate = current / part
            if candidate.is_symlink():
                target = candidate.resolve()
                if self.excluded(target):
                    raise CapsuleError("Excluded symlink target: %s" % candidate)
                if beneath(candidate, self.source_root) and target.exists():
                    self.external_roots.add(target if target.is_dir() else target.parent)
                if self.allowed_path(candidate):
                    self.register(candidate, reason + ":symlink")
                link = Path(os.readlink(candidate))
                if link.is_absolute():
                    current = Path(link.anchor)
                    pending = list(link.parts[1:]) + pending
                else:
                    pending = list(link.parts) + pending
            else:
                current = candidate
        return current

    def allowed_path(self, path):
        return (beneath(path, self.source_root) or beneath(path, self.out_root)
                or any(beneath(path, root) for root in self.external_roots))

    def compiled(self, path):
        if path.suffix.lower() in COMPILED_SUFFIXES or re.search(r"\.so\.\d", path.name):
            return True
        with path.open("rb") as stream:
            start = stream.read(16)
        return (start.startswith(b"\x7fELF") or start.startswith(b"dex\n")
                or start.startswith(b"dey\n") or start.startswith(b"!<arch>\n")
                or start.startswith(b"\xca\xfe\xba\xbe") or start.startswith(b"PK\x03\x04")
                or start.startswith(b"ANDROID!") or start.startswith(b"VNDRBOOT"))

    def binary_header_candidate(self, path):
        if self.compiled(path):
            return True
        with path.open("rb") as stream:
            prefix = stream.read(4096)
        # Extensionless C/C++ headers are text. Binary containers (including
        # jimage, Mach-O and ARM64 Image) contain NULs in their format header.
        return b"\0" in prefix

    def add(self, value, reason, required=True):
        path = self.path(value)
        if not self.selected(path) or self.excluded(path) or self.produced(path):
            return
        if not path.exists() and not path.is_symlink():
            if required:
                self.errors.add("Missing input: %s (%s)" % (path, reason))
            return
        resolved = self.symlink_ancestors(path, reason)
        if path.is_symlink() and not resolved.exists() and not required:
            return
        if self.produced(resolved):
            return
        if not self.allowed_path(resolved):
            if beneath(resolved, Path("/usr")) or beneath(resolved, Path("/bin")):
                self.system_tools.add(str(resolved))
                return
            self.errors.add("Input outside source/OUT and source symlink targets: %s" % resolved)
            return
        if resolved.is_dir():
            self.scan_directory(resolved, reason)
            return
        if not resolved.is_file():
            self.errors.add("Unsupported input type: %s" % resolved)
            return
        if beneath(resolved, self.out_root):
            if self.compiled(resolved):
                self.errors.add("Compiled OUT input has no selected producer: %s" % resolved)
                return
            if resolved not in self.allowed_generated:
                self.errors.add("Generated OUT input needs explicit metadata approval: %s" % resolved)
                return
            self.used_generated.add(resolved)
        self.register(resolved, reason)
        if resolved.suffix in {".py", ".sh"} or reason in {"graph-leaf", "command-file"}:
            with resolved.open("rb") as stream:
                first_line = stream.readline(512)
            if first_line.startswith(b"#!"):
                names = [Path(token).name for token in command_tokens(first_line[2:].decode(errors="replace").strip())]
                if "python2" in names or "python2.7" in names:
                    self.require_python2(resolved)
                elif "python" in names:
                    self.require_python2(resolved, native_default=True)

    def require_python2(self, source_script=None, native_default=False):
        executable = self.source_root / "prebuilts/build-tools/linux-x86/bin/py2-cmd"
        if "python2" not in self.required_interpreters:
            self.required_interpreters["python2"] = {"name": "python2", "aliases": ["python2", "python2.7"],
                                                     "executable": str(executable), "source_scripts": set()}
            self.add(executable, "hermetic-python2-interpreter")
        if source_script:
            self.required_interpreters["python2"]["source_scripts"].add(str(source_script))
        if native_default:
            wrapper = self.source_root / "prebuilts/build-tools/path/linux-x86/python"
            if not wrapper.is_symlink() or wrapper.resolve() != executable:
                raise CapsuleError("Native python wrapper does not bind the frozen Python 2 runtime: %s" % wrapper)
            item = self.required_interpreters["python2"]
            item["aliases"] = ["python", "python2", "python2.7"]
            item["native_default_wrapper"] = str(wrapper)

    def scan_directory(self, directory, reason, headers_only=False):
        if self.produced(directory):
            return
        key = (directory, headers_only)
        if key in self.scan_cache:
            return
        self.scan_cache.add(key)
        self.register(directory, reason)
        # os.walk deliberately does not descend through directory symlinks.
        for root, directories, names in os.walk(directory, followlinks=False):
            root = Path(root)
            if self.produced(root):
                directories[:] = []
                continue
            self.register(root, reason)
            directories[:] = sorted(item for item in directories
                                     if item not in FORBIDDEN_PARTS and not self.produced(root / item))
            for name in list(directories):
                child = root / name
                if child.is_symlink():
                    directories.remove(name)
                    target = self.symlink_ancestors(child, reason)
                    if target.is_dir() and self.allowed_path(target):
                        self.scan_directory(target, reason, headers_only=headers_only)
            for name in sorted(names):
                path = root / name
                if self.excluded(path):
                    continue
                if headers_only and path.suffix not in HEADER_SUFFIXES and path.suffix:
                    continue
                if headers_only and not path.suffix:
                    if path.name.startswith(".") or (path.is_file() and self.binary_header_candidate(path)):
                        continue
                self.add(path, reason, required=False)

    def include_directory(self, value, reason):
        path = self.path(value)
        if self.generated_include_contract is not None and self.cpp_reader and beneath(path, self.out_root):
            if path not in self.generated_include_roots:
                self.errors.add("Compiler generated include root is absent from its contract: %s" % path)
            elif path not in self.generated_processed_roots:
                self.generated_processed_roots.add(path)
                for header in self.generated_required_headers:
                    if beneath(header, path):
                        self.add(header, "compiler-generated-header-contract")
            return
        if not self.selected(path) or not path.is_dir() or self.excluded(path) or self.produced(path):
            return
        resolved = self.symlink_ancestors(path, reason)
        if self.allowed_path(resolved):
            self.scan_directory(resolved, reason, headers_only=True)

    def tool_package(self, value):
        path = self.path(value)
        if not self.selected(path) or not beneath(path, self.source_root):
            return
        relative = path.relative_to(self.source_root).parts
        if not relative or relative[0] != "prebuilts" or not path.exists():
            return
        package = None
        if relative[:4] == ("prebuilts", "clang", "host", "linux-x86") and len(relative) > 5:
            package = self.source_root.joinpath(*relative[:5])
        elif relative[:2] == ("prebuilts", "jdk") and len(relative) > 4:
            package = self.source_root.joinpath(*relative[:4])
        elif relative[:3] == ("prebuilts", "build-tools", "linux-x86"):
            package = self.source_root.joinpath(*relative[:3])
        elif relative[:2] == ("prebuilts", "gcc") and "bin" in relative:
            package = self.source_root.joinpath(*relative[:relative.index("bin")])
        elif relative[:3] == ("prebuilts", "rust", "linux-x86") and len(relative) > 4:
            package = self.source_root.joinpath(*relative[:4])
        elif relative[:3] == ("prebuilts", "clang-tools", "linux-x86"):
            package = self.source_root.joinpath(*relative[:3])
        elif relative[:3] == ("prebuilts", "kernel-build-tools", "linux-x86"):
            package = self.source_root.joinpath(*relative[:3])
        elif relative[:3] == ("prebuilts", "go", "linux-x86"):
            package = self.source_root.joinpath(*relative[:3])
        elif relative[:2] == ("prebuilts", "python") and "bin" in relative:
            package = self.source_root.joinpath(*relative[:relative.index("bin")])
        elif relative[:3] == ("prebuilts", "sdk", "tools") and len(relative) > 3:
            package = self.source_root.joinpath(*relative[:4])
            self.add(self.source_root / "prebuilts/sdk/tools/lib", "sdk-common-runtime", required=False)
        elif relative[:2] == ("prebuilts", "ndk") and "bin" in relative:
            package = self.source_root.joinpath(*relative[:relative.index("bin")])
        if package:
            self.add(package, "runtime-package:" + str(path.relative_to(self.source_root)))

    def python_package(self, path):
        if self.selected(path) and path.suffix == ".py" and path.is_file() and beneath(path, self.source_root):
            self.add(path.parent, "python-siblings:" + str(path.relative_to(self.source_root)))
            if path.resolve().parent != path.parent:
                self.add(path.resolve().parent, "python-target-siblings:" + str(path.relative_to(self.source_root)))

    def environment(self, token):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", token, re.DOTALL)
        if not match:
            return False
        name, value = match.groups()
        if name in {"PWD", "OLDPWD", "TMPDIR", "TMP", "TEMP"}:
            return True  # Working and scratch locations do not carry source inputs.
        if name in {"PATH", "PERL5LIB", "PYTHONPATH", "LD_LIBRARY_PATH", "LIBRARY_PATH", "CLASSPATH",
                    "BISON_PKGDATADIR", "PYTHONHOME"}:
            for item in value.split(":"):
                if not item or "$" in item:
                    continue
                path = self.path(item)
                if not self.selected(path):
                    continue
                if not optional_probe(path, "exists"):
                    continue
                if name == "PATH":
                    # Package the referenced toolchain, never the host /usr tree.
                    self.tool_package(path)
                else:
                    self.add(path, "command-environment:" + name, required=False)
        elif name in {"CC", "CXX", "LD", "AR", "NM", "STRIP", "OBJCOPY", "OBJDUMP", "AS",
                      "HOSTCC", "HOSTCXX", "HOSTLD", "RUSTC", "CLANG"}:
            self.command(value)
        elif name.startswith("CROSS_COMPILE"):
            self.tool_package(self.path(value).parent)
        else:
            path = self.path(value)
            if value and self.selected(path) and (optional_probe(path) or optional_probe(path, "is_symlink")):
                self.add(path, "command-environment:" + name, required=False)
                self.tool_package(path)
                self.python_package(path)
        return True

    def go_test_package(self, value):
        directory = self.path(value)
        if not self.selected(directory) or not beneath(directory, self.source_root):
            return
        self.add(directory, "go-test-runtime-package")
        ancestor = directory
        while beneath(ancestor, self.source_root):
            self.add(ancestor / "testdata", "go-test-ancestor-fixtures", required=False)
            if ancestor == self.source_root or (ancestor / ".git").exists():
                break
            ancestor = ancestor.parent

    def runtime_directory(self, value, reason):
        directory = self.path(value)
        if not self.selected(directory) or beneath(directory, self.out_root):
            return
        if beneath(directory, self.source_root) or any(beneath(directory, root) for root in self.external_roots):
            self.add(directory, reason, required=False)

    def assembler_inputs(self, value, include_paths):
        path = self.path(value)
        if self.produced(path) or not self.selected(path) or not optional_probe(path):
            return
        key = (path.resolve(), tuple(include_paths))
        if key in self.assembler_scan_cache:
            return
        self.assembler_scan_cache.add(key)
        text = path.read_text(errors="replace")
        for directive, name in re.findall(r'^\s*\.(include|incbin)\s+"([^"\n]+)"', text, re.MULTILINE):
            candidates = [self.path(name)]
            if not Path(name).is_absolute():
                candidates.extend(directory / name for directory in include_paths)
            dependency = next((candidate for candidate in candidates
                               if self.produced(candidate) or optional_probe(candidate)), candidates[0])
            self.add(dependency, "assembler-" + directive + ":" + str(path))
            if directive == "include":
                self.assembler_inputs(dependency, include_paths)

    def proto_input(self, value, search_paths):
        path = self.path(value)
        self.add(path, "protobuf-schema")
        if self.produced(path) or not self.selected(path) or not optional_probe(path):
            return
        key = (path.resolve(), tuple(search_paths))
        if key in self.proto_scan_cache:
            return
        self.proto_scan_cache.add(key)
        text = path.read_text(errors="replace")
        # Protobuf accepts adjacent quoted import strings. Tokenize comments and
        # strings together so examples inside either do not become dependencies.
        lexemes = r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_][A-Za-z_0-9]*|[^\s]'
        tokens = [token for token in re.findall(lexemes, text, flags=re.DOTALL)
                  if not token.startswith(("//", "/*"))]
        for index, token in enumerate(tokens):
            if token != "import":
                continue
            index += 1
            if index < len(tokens) and tokens[index] in {"public", "weak"}:
                index += 1
            parts = []
            while index < len(tokens) and tokens[index].startswith(('"', "'")):
                parts.append(ast.literal_eval(tokens[index]))
                index += 1
            if not parts or index >= len(tokens) or tokens[index] != ";":
                continue
            name = "".join(parts)
            candidates = self.proto_candidates(name, search_paths)
            dependency = next((item for item in candidates if self.produced(item) or optional_probe(item)),
                              candidates[0] if candidates else self.path(name))
            self.proto_input(dependency, search_paths)

    def proto_candidates(self, value, search_paths):
        candidates = []
        for virtual, directory in search_paths:
            if not virtual:
                candidates.append(directory / value)
            elif value.startswith(virtual + "/"):
                candidates.append(directory / value[len(virtual) + 1:])
        return candidates

    def semantic_operands(self, tokens, response_literals=()):
        """Handle tools whose path-looking arguments are schemas or metadata."""
        ignored = set()
        for program, end in command_invocations(tokens):
            if self.command_temporaries is not None:
                self.command_temporaries.position = program
            name = Path(tokens[program]).name
            if name in {"protoc", "aprotoc"}:
                search_paths, inputs = [], []
                index = program + 1
                while index < end:
                    token = tokens[index]
                    if token.isdigit() and index + 1 < end and tokens[index + 1] in {"<", ">", ">>", ">|"}:
                        ignored.add(index)
                        index += 1
                        continue
                    if token.startswith("@") and index not in response_literals:
                        index += 1
                        continue
                    ignored.add(index)
                    if token in {"<", ">", ">>", ">|", "<>", "<<", "<<<", "<&", ">&"} and index + 1 < end:
                        index += 1
                        ignored.add(index)
                        if token in {"<", "<>", ">>"}:
                            self.add(tokens[index], "command-redirection-input", required=token != ">>")
                        index += 1
                        continue
                    if token in {"-I", "--proto_path"} and index + 1 < end:
                        index += 1
                        ignored.add(index)
                        mapping = tokens[index]
                    elif token.startswith("--proto_path="):
                        mapping = token.split("=", 1)[1]
                    elif token.startswith("-I") and len(token) > 2:
                        mapping = token[2:]
                    else:
                        if token in {"-o", "--descriptor_set_in", "--descriptor_set_out", "--dependency_out",
                                     "--plugin", "--encode", "--decode"} or (token.startswith("--")
                                                                             and token.endswith("_out")):
                            if index + 1 < end:
                                index += 1
                                ignored.add(index)
                                self.proto_option_input(token, tokens[index])
                        elif token.startswith(("--descriptor_set_in=", "--plugin=")):
                            self.proto_option_input(*token.split("=", 1))
                        elif not token.startswith("-"):
                            if not token:
                                raise CapsuleError("Empty protobuf schema argument")
                            inputs.append(token)
                        index += 1
                        continue
                    for item in mapping.split(":"):
                        virtual, separator, directory = item.partition("=")
                        search_paths.append((virtual.rstrip("/"), self.path(directory)) if separator
                                            else ("", self.path(item)))
                    index += 1
                if not search_paths:
                    search_paths = [("", self.source_root)]
                for value in inputs:
                    direct = self.path(value)
                    candidates = [direct] + self.proto_candidates(value, search_paths)
                    dependency = next((item for item in candidates if self.produced(item) or optional_probe(item)), direct)
                    self.proto_input(dependency, search_paths)
            elif name == "build_license_metadata":
                # The native tool stores these fields as protobuf strings. Only
                # @response files are opened; graph dependencies are collected
                # separately even when the same path is also metadata text.
                index = program + 1
                ignored.update(position for position in response_literals if program < position < end)
                metadata_flags = {"-s", "-t", "-i", "-d", "-m", "-n", "-mt", "-mc", "-k", "-c", "-p", "-o", "-r"}
                while index < end:
                    token = tokens[index]
                    flag, separator, value = token.partition("=")
                    if flag in metadata_flags:
                        ignored.add(index)
                        if not separator and index + 1 < end:
                            index += 1
                            ignored.add(index)
                            value = tokens[index]
                        if flag == "-r":
                            path = self.path(value)
                            if self.selected(path) and not self.produced(path) and optional_probe(path, "is_dir"):
                                resolved = self.symlink_ancestors(path, "license-git-probe-root")
                                if self.allowed_path(resolved) and not beneath(resolved, self.out_root):
                                    self.register(resolved, "license-git-probe-root")
                    elif flag == "-is_container":
                        ignored.add(index)
                    index += 1
            elif name == "xmlnotice":
                for index in range(program + 1, end):
                    flag, separator, _ = tokens[index].partition("=")
                    if flag in {"-strip_prefix", "-product", "-title"}:
                        ignored.add(index)
                        if not separator and index + 1 < end:
                            ignored.add(index + 1)
            elif name in {"java", "javac", "jmod", "jlink", "kotlinc", "kapt", "soong_javac_wrapper"}:
                for index in range(program + 1, end):
                    flag, separator, value = tokens[index].partition("=")
                    if flag in {"-classpath", "--class-path", "-cp"}:
                        ignored.add(index)
                        if not separator and index + 1 < end:
                            ignored.add(index + 1)
                            value = tokens[index + 1]
                        for path in value.split(":"):
                            if path:
                                self.add(path, "java-classpath", required=False)
        return ignored

    def proto_option_input(self, flag, value):
        if flag == "--descriptor_set_in":
            for path in value.split(":"):
                self.add(path, "protobuf-descriptor-input")
        elif flag == "--plugin":
            path = value.partition("=")[2] or value
            self.add(path, "protobuf-plugin")
            self.tool_package(path)

    def expand_semantic_responses(self, tokens, temporary_outputs):
        """Inspect response operands in their caller's exact argument order."""
        def expand(arguments, program, start):
            result = []
            for index, token in enumerate(arguments, start):
                if not token.startswith("@"):
                    result.append((token, False))
                    continue
                path = self.path(token[1:])
                temporary_outputs.position = index
                if path in temporary_outputs:
                    result.append((token, False))
                    continue
                self.add(path, "command-response")
                if path in self.response_contents:
                    content = self.response_contents[path]
                elif not self.produced(path) and optional_probe(path):
                    content = path.read_text()
                else:
                    result.append((token, False))
                    continue
                # Native protoc reads one argument per line. The license tool
                # uses the Soong quoted response parser. Neither re-expands @
                # tokens from inside response content.
                if program in {"protoc", "aprotoc"}:
                    arguments = content.split("\n")
                    if arguments and arguments[-1] == "":
                        arguments.pop()
                else:
                    arguments = shlex.split(content, posix=True)
                result.extend((argument, argument.startswith("@")) for argument in arguments)
            return result

        marked = [(token, False) for token in tokens]
        for program, end in reversed(list(command_invocations(tokens))):
            name = Path(tokens[program]).name
            if name in {"protoc", "aprotoc", "build_license_metadata", "soong_zip"}:
                marked[program + 1:end] = expand(tokens[program + 1:end], name, program + 1)
        return [token for token, _ in marked], {index for index, (_, literal) in enumerate(marked) if literal}

    def literal_path(self, value):
        if not value or value.startswith("-") or any(character in value for character in "$*?[\n"):
            return None
        return self.path(value)

    def tool_output_operands(self, tokens):
        """Return verified truncating file outputs, with completion positions."""
        files, ignored = {}, set()
        for program, end in command_invocations(tokens):
            name = Path(tokens[program]).name
            flags = set()
            attached = set()
            if name in {"dirname", "basename"}:
                ignored.update(range(program + 1, end))
            if name in {"rm", "mkdir", "touch"}:
                reference = set()
                if name == "touch":
                    for index in range(program + 1, end):
                        if tokens[index] in {"-r", "--reference"}:
                            reference.add(index + 1)
                        elif tokens[index].startswith("--reference="):
                            reference.add(index)
                ignored.update(index for index in range(program + 1, end)
                               if not tokens[index].startswith("-") and index not in reference)
            if name == "aidl":
                flags, attached = {"-d", "--dep"}, {"-d"}
            elif name in {"protoc", "aprotoc"}:
                flags = {"--dependency_out", "--descriptor_set_out", "-o"}
            elif name in {"soong_zip", "zip2zip", "clang", "clang++", "llvm-link", "bcc_strip_attr"}:
                flags = {"-o"}
            script = next((index for index in range(program, min(program + 3, end))
                           if Path(tokens[index]).name == "gen-kotlin-build-file.py"
                           and (index == program or name.startswith("python") or name in {"py2-cmd", "py3-cmd"})), None)
            if script is not None:
                flags = {"--out"}
            for index in range(program + 1, end):
                token = tokens[index]
                flag, separator, value = token.partition("=")
                operand_position = index
                if flag in flags:
                    if not separator and index + 1 < end:
                        operand_position = index + 1
                        value = tokens[operand_position]
                    ignored.update({index, operand_position})
                else:
                    prefix = next((prefix for prefix in attached if token.startswith(prefix) and token != prefix), None)
                    if prefix is None:
                        continue
                    value = token[len(prefix):]
                    ignored.add(index)
                path = self.literal_path(value)
                if path is not None:
                    files.setdefault(path, end)
            if name in {"rustc", "clippy-driver"}:
                for index in range(program + 1, end):
                    token = tokens[index]
                    if token == "--emit" and index + 1 < end:
                        value = tokens[index + 1]
                        ignored.update({index, index + 1})
                    elif token.startswith("--emit="):
                        value = token.partition("=")[2]
                        ignored.add(index)
                    else:
                        continue
                    for item in value.split(","):
                        kind, separator, destination = item.partition("=")
                        if separator and kind in {"dep-info", "link", "metadata", "asm", "llvm-ir", "llvm-bc", "obj"}:
                            path = self.literal_path(destination)
                            if path is not None:
                                files.setdefault(path, end)
        return files, ignored

    def action_temporaries(self, tokens, inherited=None):
        """Find fresh writes in the unconditional prefix of one shell action."""
        hazard = next((index for index, token in enumerate(tokens)
                       if token in {"||", "|", "&", "if", "for", "while", "until", "case"}), len(tokens))
        files, _ = self.tool_output_operands(tokens[:hazard])
        substitutions = substitution_positions(tokens)
        completed = {}
        for path, position in files.items():
            if position > 0 and position - 1 in substitutions:
                continue
            position = command_completion(tokens, position)
            if position < len(tokens) and tokens[position] == "&&":
                completed[path] = position
        files = completed
        directories, removed = {}, {}
        for program, end in command_invocations(tokens[:hazard]):
            if program in substitutions:
                continue
            name = Path(tokens[program]).name
            operands = [self.literal_path(token) for token in tokens[program + 1:end] if not token.startswith("-")]
            operands = [path for path in operands if path is not None]
            if name == "rm":
                recursive = any(token == "--recursive" or (token.startswith("-") and not token.startswith("--")
                                                           and "r" in token[1:]) for token in tokens[program + 1:end])
                for path in operands:
                    removed[path] = (end, recursive)
            elif name in {"mkdir", "touch"}:
                for path in operands:
                    for root, (position, recursive) in removed.items():
                        # A successful && chain establishes that the old file
                        # or namespace was removed before its replacement.
                        between = [token for token in tokens[position:program] if token not in {"(", ")"}]
                        if not between or between[0] != "&&" or any(token in {";", "||", "|", "&"} for token in between):
                            continue
                        if name == "touch" and path == root:
                            files.setdefault(path, end)
                        elif name == "mkdir" and recursive and beneath(path, root):
                            directories.setdefault(root, end)
            for index in range(program + 1, end - 1):
                if end < len(tokens) and tokens[end] in {"||", "|", "&"}:
                    continue
                if tokens[index] in {">", ">|", ">>"}:
                    path = self.literal_path(tokens[index + 1])
                    prior = removed.get(path)
                    between = ([token for token in tokens[prior[0]:program] if token not in {"(", ")"}]
                               if prior else [])
                    removed_before = bool(between and between[0] == "&&"
                                          and not any(token in {";", "||", "|", "&"} for token in between))
                    if path is not None and (tokens[index] != ">>" or removed_before):
                        files.setdefault(path, index + 1)
        barriers = [index for index, token in enumerate(tokens) if token in {";", "||"} and index not in substitutions]
        return ActionTemporaries(files, directories, inherited, barriers)

    def command(self, command, context=None, cpp_reader=None):
        tokens = command_tokens(command)
        if context:
            tokens = command_tokens(context) + tokens
        previous = self.command_temporaries
        previous_reader = self.cpp_reader
        previous_binding = self.cpp_binding
        self.cpp_binding = previous_binding if cpp_reader is None else cpp_reader
        temporary_outputs = self.action_temporaries(tokens, previous.snapshot() if previous else None)
        self.command_temporaries = temporary_outputs
        try:
            self.inspect_command(command, tokens, temporary_outputs)
        finally:
            self.command_temporaries = previous
            self.cpp_reader = previous_reader
            self.cpp_binding = previous_binding

    def inspect_command(self, command, tokens, temporary_outputs):
        tokens, response_literals = self.expand_semantic_responses(tokens, temporary_outputs)
        # Expanded response arguments have native caller order; recompute their
        # output positions before inspecting any subsequent readers.
        inherited = (temporary_outputs.inherited_files, temporary_outputs.inherited_directories)
        temporary_outputs = self.action_temporaries(tokens, inherited)
        self.command_temporaries = temporary_outputs
        _, literal_tokens = self.tool_output_operands(tokens)
        literal_tokens.update(self.semantic_operands(tokens, response_literals))
        source_invocations = {position for position, _ in command_invocations(tokens)
                              if tokens[position] in {".", "source"}}
        compiler_positions = set()
        for program, end in cpp_invocations(tokens):
            start = program
            tool = program + 1 if Path(tokens[program]).name == "ccache" else program
            if Path(tokens[tool]).name in {"header-abi-dumper", "clang-tidy"}:
                start = next((index + 1 for index in range(program + 1, end) if tokens[index] == "--"), end)
            compiler_positions.update(range(start, end))
        for position, token in enumerate(tokens):
            if Path(token).name == "ln":
                end = position + 1
                while end < len(tokens) and tokens[end] not in {";", "&&", "||", "|", "(", ")"}:
                    end += 1
                arguments = tokens[position + 1:end]
                if any(item == "--symbolic" or (item.startswith("-") and not item.startswith("--")
                                                and "s" in item[1:]) for item in arguments):
                    literal_tokens.update(range(position + 1, end))
        for position, token in enumerate(tokens):
            if Path(token).name in {"python2", "python2.7"}:
                self.require_python2()
            elif token == "python":
                self.require_python2(native_default=True)
            if Path(token).name == "bpglob":
                for option in range(position + 1, len(tokens) - 1):
                    if tokens[option] == "-p":
                        pattern = tokens[option + 1]
                        parts = Path(pattern).parts
                        fixed = []
                        for part in parts:
                            if any(character in part for character in "*?["):
                                break
                            fixed.append(part)
                        if fixed:
                            self.add(Path(*fixed), "source-glob-runtime-files", required=False)
            if Path(token).name == "gotestrunner":
                for option in range(position + 1, len(tokens)):
                    if tokens[option] == "--":
                        break
                    if tokens[option] == "-p" and option + 1 < len(tokens):
                        self.go_test_package(tokens[option + 1])
                        break
                    if tokens[option].startswith("-p="):
                        self.go_test_package(tokens[option][3:])
                        break
        include_flags = {"-I", "-isystem", "-iquote", "-idirafter", "--sysroot", "-isysroot"}
        runtime_flags = {"-B", "-L", "--gcc-toolchain", "-gcc-toolchain", "-resource-dir", "--resource-dir"}
        file_flags = {"-include", "-imacros"}
        include_paths = []
        assembler_paths = []
        for index, token in enumerate(tokens):
            if index in literal_tokens:
                continue
            if token in include_flags and index + 1 < len(tokens):
                directory = self.path(tokens[index + 1])
                include_paths.append(directory)
                if token == "-I":
                    assembler_paths.append(directory)
                if token in {"--sysroot", "-isysroot"}:
                    include_paths.extend([directory / "usr/include", directory / "include"])
            elif token.startswith(("-I", "-isystem", "-iquote", "-idirafter", "--sysroot=", "-isysroot")):
                match = re.match(r"^(?:-isystem|-iquote|-idirafter|-isysroot|-I|--sysroot=)(.+)$", token)
                if match:
                    directory = self.path(match[1])
                    include_paths.append(directory)
                    if token.startswith("-I"):
                        assembler_paths.append(directory)
                    if token.startswith(("--sysroot=", "-isysroot")):
                        include_paths.extend([directory / "usr/include", directory / "include"])
        index = 0
        while index < len(tokens):
            temporary_outputs.position = index
            self.cpp_reader = self.cpp_binding and index in compiler_positions
            token = tokens[index]
            operand = command_operand(token)
            if operand and not operand.startswith("-") and self.path(operand) in temporary_outputs:
                index += 1
                continue
            if index in literal_tokens:
                index += 1
                continue
            shell_payload = (token.startswith("-") and "c" in token[1:]
                             and not token.startswith("--")
                             and any(Path(item).name in {"bash", "sh", "dash"}
                                     for item in tokens[max(0, index - 4):index]))
            if shell_payload and index + 1 < len(tokens):
                index += 1
                self.command(tokens[index])
            elif self.environment(token):
                pass
            elif index in source_invocations and index + 1 < len(tokens):
                index += 1
                path = self.path(tokens[index])
                self.add(path, "shell-source")
                content = self.response_contents.get(path)
                if content is None and not self.produced(path) and optional_probe(path):
                    content = path.read_text()
                if content is not None:
                    self.command(content)
            elif token == "-C" and index + 1 < len(tokens):
                index += 1
                option = tokens[index]
                if option.startswith("link-args="):
                    self.command(option.partition("=")[2])
                elif option.startswith("linker="):
                    self.command(option.partition("=")[2])
                else:
                    path = self.path(option)
                    if optional_probe(path, "is_dir") and (beneath(path, self.source_root) or self.produced(path)):
                        self.add(path, "command-directory:-C", required=False)
            elif token in runtime_flags and index + 1 < len(tokens):
                index += 1
                self.runtime_directory(tokens[index], "command-runtime:" + token)
            elif re.match(r"^(?:--?gcc-toolchain|--?resource-dir)=", token):
                self.runtime_directory(token.split("=", 1)[1], "command-runtime")
            elif token.startswith(("-B", "-L")) and len(token) > 2:
                self.runtime_directory(token[2:], "command-runtime:" + token[:2])
            elif token in include_flags | file_flags and index + 1 < len(tokens):
                index += 1
                value = tokens[index]
                if token in include_flags:
                    if token in {"--sysroot", "-isysroot"}:
                        self.runtime_directory(value, "command-sysroot")
                    else:
                        self.include_directory(value, "command-include:" + token)
                else:
                    candidates = [self.path(value)]
                    if not Path(value).is_absolute():
                        candidates.extend(directory / value for directory in include_paths)
                    found = next((path for path in candidates if path.is_file() or self.produced(path)), candidates[0])
                    self.add(found, "command-input:" + token)
            elif token.startswith(("-I", "-isystem", "-iquote", "-idirafter", "--sysroot=", "-isysroot")):
                match = re.match(r"^(?:-isystem|-iquote|-idirafter|-isysroot|-I|--sysroot=)(.+)$", token)
                if match:
                    if token.startswith(("--sysroot=", "-isysroot")):
                        self.runtime_directory(match[1], "command-sysroot")
                    else:
                        self.include_directory(match[1], "command-include")
            elif token.startswith("@"):
                # Ninja writes declared rspfiles; their content is inspected separately.
                path = self.path(token[1:])
                if not beneath(path, self.out_root):
                    self.add(path, "command-response")
                    if not self.produced(path) and path.is_file():
                        self.command(path.read_text(), response_context(command, token[1:]))
            elif token not in {"&&", "||", ";", "|", "(", ")", ">", "<"}:
                value = command_operand(token)
                if value and not value.startswith("-"):
                    path = self.path(value)
                    if self.selected(path) and (optional_probe(path) or optional_probe(path, "is_symlink")):
                        self.add(path, "command-file", required=False)
                        if path.suffix in {".s", ".S", ".asm"}:
                            self.assembler_inputs(path, assembler_paths)
                        self.tool_package(path)
                        self.python_package(path)
            index += 1

    def collect(self, slice_dir, include_task=True):
        for value in self.manifest.get("leaf_inputs", []):
            self.add(value, "graph-leaf")
            path = self.path(value)
            if path.suffix in SOURCE_SUFFIXES:
                self.include_directory(path.parent, "source-local-headers")
            self.python_package(path)
            self.tool_package(path)
        commands = [(command, None, command in self.compiler_commands) for command in self.manifest.get("commands", [])]
        for edge in self.manifest.get("edges", []):
            if edge.get("command"):
                commands.append((edge["command"], None, edge["command"] in self.compiler_commands))
            if edge.get("rspfile_content"):
                context = response_context(edge.get("command", ""), edge.get("rspfile"))
                callers = rsp_callers(edge.get("command", ""), edge.get("rspfile", ""))
                if not callers and not context:
                    callers = [(None, False)]
                for reader, cpp_rsp in callers:
                    # These callers expand their arguments inline. Another
                    # reader of the same file still needs its own tool context.
                    if reader in {"protoc", "aprotoc", "build_license_metadata", "soong_zip"}:
                        continue
                    commands.append((edge["rspfile_content"], reader,
                                     cpp_rsp and edge.get("command") in self.compiler_commands))
        for command, context, cpp_reader in dict.fromkeys(commands):
            self.command(command, context, cpp_reader)
        depfiles = list(self.manifest.get("depfiles", []))
        depfiles.extend(edge["depfile"] for edge in self.manifest.get("edges", []) if edge.get("depfile"))
        for value in dict.fromkeys(depfiles):
            path = self.path(value)
            if path.is_file():
                for dependency in make_dependencies(path.read_text(errors="replace")):
                    self.add(dependency, "observed-depfile:" + str(path))
        runner_ninja = self.source_root / "prebuilts/build-tools/linux-x86/bin/ninja"
        self.add(runner_ninja, "runner-ninja")
        self.tool_package(runner_ninja)
        if not include_task:
            if self.errors:
                raise CapsuleError("Shared source input audit failed:\n" + "\n".join(sorted(self.errors)))
            return None
        runtime_dir = Path(self.manifest.get("runtime_dir", ".crux-task/graph"))
        if runtime_dir.is_absolute() or ".." in runtime_dir.parts:
            raise CapsuleError("runtime_dir must be a safe relative path")
        task_path = self.source_root / runtime_dir
        for root, directories, files in os.walk(slice_dir):
            directories[:] = sorted(name for name in directories if name not in FORBIDDEN_PARTS)
            for name in sorted(files):
                path = Path(root) / name
                if path.is_symlink():
                    raise CapsuleError("Slice files must be regular files: %s" % path)
                archive_path = task_path / path.relative_to(slice_dir)
                self.virtual_files[archive_path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        if self.errors:
            raise CapsuleError("Capsule input audit failed:\n" + "\n".join(sorted(self.errors)))
        return task_path

    def file_specs(self):
        files = []
        for path, reasons in sorted(self.entries.items()):
            info = path.lstat()
            item = {"path": str(path), "mode": stat.S_IMODE(info.st_mode), "reasons": sorted(reasons)}
            if stat.S_ISLNK(info.st_mode):
                item.update(type="symlink", target=os.readlink(path), size=0)
            elif stat.S_ISDIR(info.st_mode):
                item.update(type="directory", size=0)
            else:
                item.update(type="file", size=info.st_size, sha256=self.digester(path))
            files.append(item)
        return files

    def metadata(self, manifest_path, task_path):
        files = self.file_specs()
        for path, (data, mode) in sorted(self.virtual_files.items()):
            files.append({"path": str(path), "mode": mode, "type": "file", "size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest(), "reasons": ["task-graph"]})
        graph_inputs = []
        for value in self.manifest.get("graph_inputs", []):
            path = self.path(value)
            if path.is_file():
                graph_inputs.append({"path": str(path), "sha256": self.digester(path), "size": path.stat().st_size})
        return {"schema_version": 1, "source_root": str(self.source_root), "out_root": str(self.out_root),
                "slice_path": str(task_path), "manifest_sha256": self.digester(manifest_path),
                "files": files, "file_count": len(files),
                "input_bytes": sum(item["size"] for item in files),
                "external_inputs": [{"path": str(path)} for path in sorted(self.external)],
                "output_dirs": [str(path) for path in sorted(self.output_dirs)],
                "external_input_dirs": [str(path) for path in sorted(self.external_dirs)],
                "allowed_generated_inputs": [{"path": str(path), "reason": self.allowed_generated[path]}
                                             for path in sorted(self.used_generated)],
                "required_interpreters": [{**item, "source_scripts": sorted(item["source_scripts"])}
                                          for _, item in sorted(self.required_interpreters.items())],
                "graph_inputs": graph_inputs, "system_tools": sorted(self.system_tools)}

    def archive(self, stream, metadata, task_path, omitted=()):
        with tarfile.open(fileobj=stream, mode="w|") as archive:
            for path in sorted(self.entries):
                if str(path) in omitted:
                    continue
                archive.add(path, arcname=str(path).lstrip("/"), recursive=False)
            for path, (data, mode) in sorted(self.virtual_files.items()):
                info = tarfile.TarInfo(str(path).lstrip("/"))
                info.size, info.mode = len(data), mode
                info.uid = info.gid = 0
                archive.addfile(info, io.BytesIO(data))
            data = (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode()
            metadata_destination = (task_path / "bundle.json" if task_path else
                                    self.source_root / ".crux-task/shared-layers" / (metadata["layer_id"] + ".json"))
            info = tarfile.TarInfo(str(metadata_destination).lstrip("/"))
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))


def write_archive(collector, metadata, task_path, output, digester, omitted=()):
    output = Path(output).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not str(output).endswith(".tar.zst"):
        raise CapsuleError("--output must end in .tar.zst")
    if not shutil.which("zstd"):
        raise CapsuleError("zstd is required to create the runner capsule")
    temporary = tempfile.NamedTemporaryFile(prefix=output.name + ".", dir=output.parent, delete=False)
    temporary.close()
    try:
        with open(temporary.name, "wb") as destination:
            compressor = subprocess.Popen(["zstd", "-q", "-T0", "-3", "-c"], stdin=subprocess.PIPE, stdout=destination)
            try:
                collector.archive(compressor.stdin, metadata, task_path, omitted)
            finally:
                compressor.stdin.close()
            if compressor.wait() != 0:
                raise CapsuleError("zstd compression failed")
        os.replace(temporary.name, output)
    finally:
        if os.path.exists(temporary.name):
            os.unlink(temporary.name)
    metadata["archive"] = {"name": output.name, "size": output.stat().st_size, "sha256": digest(output)}
    metadata_path = output.with_name(output.name.removesuffix(".tar.zst") + ".json")
    metadata["digest_cache"] = {"computed": digester.computed, "reused": digester.reused}
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    digester.save()
    return metadata_path, metadata


def identity_spec(spec):
    keys = ["path", "type", "mode"]
    if spec["type"] == "file":
        keys.extend(["size", "sha256"])
    elif spec["type"] == "symlink":
        keys.append("target")
    return {key: spec[key] for key in keys}


def layer_identity(files):
    data = [identity_spec(spec) for spec in sorted(files, key=lambda spec: spec["path"])]
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def shared_inputs(metadata, shared_manifest, digester=digest):
    if not shared_manifest:
        return set()
    layer_path = Path(shared_manifest).resolve()
    layer = json.loads(layer_path.read_text())
    if layer.get("schema_version") != 1 or layer.get("kind") != "shared-source-layer":
        raise CapsuleError("Unsupported shared source layer schema")
    if layer["source_root"] != metadata["source_root"]:
        raise CapsuleError("Shared source layer uses a different source_root")
    if layer_identity(layer["files"]) != layer["layer_id"]:
        raise CapsuleError("Shared source layer file identity mismatch")
    available = {spec["path"]: spec for spec in layer["files"]}
    if len(available) != len(layer["files"]):
        raise CapsuleError("Shared source layer has duplicate file paths")
    omitted = set()
    for spec in metadata["files"]:
        prior = available.get(spec["path"])
        if prior:
            if beneath(Path(spec["path"]), Path(metadata["out_root"])):
                raise CapsuleError("Shared source layers must not contain OUT inputs")
            if identity_spec(spec) != identity_spec(prior):
                raise CapsuleError("Input differs from frozen shared source layer: %s" % spec["path"])
            omitted.add(spec["path"])
    metadata["shared_layers"] = [{"layer_id": layer["layer_id"], "manifest_sha256": digester(layer_path),
                                  "required_files": sorted(omitted)}]
    metadata["task_archive_input_bytes"] = sum(spec["size"] for spec in metadata["files"]
                                              if spec["path"] not in omitted)
    return omitted


def pack(manifest_path, source_root, output, out_root=DEFAULT_OUT, allowed_generated=(), digest_cache=None,
         shared_manifest=None):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1:
        raise CapsuleError("Unsupported slice schema_version")
    if Path(manifest.get("source_root", source_root)) != Path(source_root):
        raise CapsuleError("--source-root differs from manifest source_root")
    digester = DigestCache(digest_cache)
    collector = Collector(manifest, source_root, out_root, allowed_generated, digester)
    task_path = collector.collect(manifest_path.parent)
    metadata = collector.metadata(manifest_path, task_path)
    omitted = shared_inputs(metadata, shared_manifest, digester)
    return write_archive(collector, metadata, task_path, output, digester, omitted)


def manifest_paths(values):
    result = set()
    for value in values:
        path = Path(value).resolve()
        if path.is_file():
            result.add(path)
        else:
            result.update(path.rglob("manifest.json"))
    if not result:
        raise CapsuleError("No slice manifest.json files found")
    return sorted(result)


def build_layer(manifests, source_root, output, out_root=DEFAULT_OUT, shared_roots=None, digest_cache=None,
                metadata_only=False):
    source_root, out_root = Path(source_root).resolve(), Path(out_root).resolve()
    roots = shared_roots or ["prebuilts"]
    roots = [Path(root) if Path(root).is_absolute() else source_root / root for root in roots]
    for root in roots:
        if not beneath(root, source_root) or beneath(root, out_root):
            raise CapsuleError("Shared roots must belong to the frozen source tree: %s" % root)
    digester = DigestCache(digest_cache)
    entries, scan_cache, provenance = {}, set(), []
    aggregate = None
    for path in manifest_paths(manifests):
        manifest = json.loads(path.read_text())
        if manifest.get("schema_version") != 1 or Path(manifest["source_root"]) != source_root:
            raise CapsuleError("Shared source slice schema/source_root mismatch: %s" % path)
        collector = Collector(manifest, source_root, out_root, digester=digester,
                              selection_roots=roots, scan_cache=scan_cache)
        collector.entries = entries
        collector.collect(path.parent, include_task=False)
        provenance.append({"path": str(path), "sha256": digester(path)})
        aggregate = collector
    files = aggregate.file_specs()
    metadata = {"schema_version": 1, "kind": "shared-source-layer", "source_root": str(source_root),
                "out_root": str(out_root), "layer_id": layer_identity(files), "files": files,
                "file_count": len(files), "input_bytes": sum(spec["size"] for spec in files),
                "shared_roots": [str(root) for root in roots], "manifests": provenance}
    if metadata_only:
        metadata["digest_cache"] = {"computed": digester.computed, "reused": digester.reused}
        metadata_path = Path(output).resolve()
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        digester.save()
        return metadata_path, metadata
    return write_archive(aggregate, metadata, None, output, digester)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack_parser = commands.add_parser("pack")
    pack_parser.add_argument("--manifest", required=True)
    pack_parser.add_argument("--source-root", default=DEFAULT_SOURCE)
    pack_parser.add_argument("--out-root", default=DEFAULT_OUT)
    pack_parser.add_argument("--output", required=True)
    pack_parser.add_argument("--allow-generated", action="append", default=[], metavar="PATH=REASON")
    pack_parser.add_argument("--digest-cache", help="local digest cache; source paths and stat identity only")
    pack_parser.add_argument("--shared-manifest", help="frozen shared source layer JSON restored by the worker")
    layer_parser = commands.add_parser("build-layer")
    layer_parser.add_argument("--manifests", nargs="+", required=True, help="slice manifest paths or directories")
    layer_parser.add_argument("--source-root", default=DEFAULT_SOURCE)
    layer_parser.add_argument("--out-root", default=DEFAULT_OUT)
    layer_parser.add_argument("--output", required=True)
    layer_parser.add_argument("--digest-cache")
    layer_parser.add_argument("--shared-root", action="append", help="relative source root; default: prebuilts")
    layer_parser.add_argument("--metadata-only", action="store_true", help="write layer JSON without compression")
    arguments = parser.parse_args()
    try:
        if arguments.command == "pack":
            metadata_path, metadata = pack(arguments.manifest, arguments.source_root, arguments.output,
                                           arguments.out_root, arguments.allow_generated, arguments.digest_cache,
                                           arguments.shared_manifest)
        else:
            metadata_path, metadata = build_layer(arguments.manifests, arguments.source_root, arguments.output,
                                                  arguments.out_root, arguments.shared_root, arguments.digest_cache,
                                                  arguments.metadata_only)
    except (CapsuleError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({"archive": arguments.output, "metadata": str(metadata_path),
                      "file_count": metadata["file_count"], "input_bytes": metadata["input_bytes"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
