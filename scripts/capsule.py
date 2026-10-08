#!/usr/bin/env python3
"""Package the cold inputs of a Ninja graph slice for a standard Linux runner."""

import argparse
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
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError as error:
        raise CapsuleError("Cannot parse expanded command: %s" % error) from error


class Collector:
    def __init__(self, manifest, source_root, out_root, allowed_generated=(), digester=None):
        self.manifest = manifest
        self.source_root = Path(source_root).resolve()
        self.out_root = Path(out_root).resolve()
        self.entries = {}
        self.virtual_files = {}
        self.errors = set()
        self.outputs = {self.path(item) for item in manifest.get("outputs", [])}
        self.external = {self.path(item) for item in manifest.get("external_inputs", [])}
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
        self.scan_cache = set()
        self.digester = digester or digest

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
        return (path in self.outputs or path in self.external
                or any(beneath(path, root) for root in self.output_dirs | self.external_dirs))

    def register(self, path, reason):
        self.entries.setdefault(path, set()).add(reason)

    def symlink_ancestors(self, path, reason):
        # Archive the link and the actual target, never entries below a link.
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            if current.is_symlink():
                target = current.resolve()
                if self.excluded(target):
                    raise CapsuleError("Excluded symlink target: %s" % current)
                if beneath(current, self.source_root):
                    self.external_roots.add(target if target.is_dir() else target.parent)
                if self.allowed_path(current):
                    self.register(current, reason + ":symlink")
        return path.resolve()

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

    def add(self, value, reason, required=True):
        path = self.path(value)
        if self.excluded(path) or self.produced(path):
            return
        if not path.exists() and not path.is_symlink():
            if required:
                self.errors.add("Missing input: %s (%s)" % (path, reason))
            return
        resolved = self.symlink_ancestors(path, reason)
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
                self.add(path, reason, required=False)

    def include_directory(self, value, reason):
        path = self.path(value)
        if not path.is_dir() or self.excluded(path) or self.produced(path):
            return
        resolved = self.symlink_ancestors(path, reason)
        if self.allowed_path(resolved):
            self.scan_directory(resolved, reason, headers_only=True)

    def tool_package(self, value):
        path = self.path(value)
        if not path.exists() or not beneath(path, self.source_root):
            return
        relative = path.relative_to(self.source_root).parts
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
        if path.suffix == ".py" and path.is_file() and beneath(path, self.source_root):
            self.add(path.parent, "python-siblings:" + str(path.relative_to(self.source_root)))

    def environment(self, token):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", token, re.DOTALL)
        if not match:
            return False
        name, value = match.groups()
        if name in {"PATH", "PERL5LIB", "PYTHONPATH", "LD_LIBRARY_PATH", "LIBRARY_PATH", "CLASSPATH",
                    "BISON_PKGDATADIR", "PYTHONHOME"}:
            for item in value.split(":"):
                if not item or "$" in item:
                    continue
                path = self.path(item)
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
            if value and (optional_probe(path) or optional_probe(path, "is_symlink")):
                self.add(path, "command-environment:" + name, required=False)
                self.tool_package(path)
                self.python_package(path)
        return True

    def command(self, command):
        tokens = command_tokens(command)
        include_flags = {"-I", "-isystem", "-iquote", "-idirafter", "--sysroot", "-isysroot"}
        file_flags = {"-include", "-imacros"}
        index = 0
        while index < len(tokens):
            token = tokens[index]
            shell_payload = (token.startswith("-") and "c" in token[1:]
                             and not token.startswith("--")
                             and any(Path(item).name in {"bash", "sh", "dash"}
                                     for item in tokens[max(0, index - 4):index]))
            if shell_payload and index + 1 < len(tokens):
                index += 1
                self.command(tokens[index])
            elif self.environment(token):
                pass
            elif token == "-C" and index + 1 < len(tokens):
                index += 1
                path = self.path(tokens[index])
                if beneath(path, self.source_root) or self.produced(path):
                    self.add(path, "command-directory:-C", required=False)
            elif token in include_flags | file_flags and index + 1 < len(tokens):
                index += 1
                value = tokens[index]
                if token in include_flags:
                    self.include_directory(value, "command-include:" + token)
                else:
                    self.add(value, "command-input:" + token)
            elif token.startswith(("-I", "-isystem", "-iquote", "-idirafter", "--sysroot=", "-isysroot")):
                match = re.match(r"^(?:-isystem|-iquote|-idirafter|-isysroot|-I|--sysroot=)(.+)$", token)
                if match:
                    self.include_directory(match[1], "command-include")
            elif token.startswith("@"):
                # Ninja writes declared rspfiles; their content is inspected separately.
                path = self.path(token[1:])
                if not beneath(path, self.out_root):
                    self.add(path, "command-response", required=False)
                    if path.is_file():
                        self.command(path.read_text())
            elif token not in {"&&", "||", ";", "|", "(", ")", ">", "<"}:
                value = token.partition("=")[2] if "=" in token else token
                if value and not value.startswith("-"):
                    path = self.path(value)
                    if optional_probe(path) or optional_probe(path, "is_symlink"):
                        self.add(path, "command-file", required=False)
                        self.tool_package(path)
                        self.python_package(path)
            index += 1

    def collect(self, slice_dir):
        for value in self.manifest.get("leaf_inputs", []):
            self.add(value, "graph-leaf")
            path = self.path(value)
            if path.suffix in SOURCE_SUFFIXES:
                self.include_directory(path.parent, "source-local-headers")
            self.python_package(path)
            self.tool_package(path)
        for command in self.manifest.get("commands", []):
            self.command(command)
        for edge in self.manifest.get("edges", []):
            if edge.get("command"):
                self.command(edge["command"])
            if edge.get("rspfile_content"):
                self.command(edge["rspfile_content"])
        depfiles = list(self.manifest.get("depfiles", []))
        depfiles.extend(edge["depfile"] for edge in self.manifest.get("edges", []) if edge.get("depfile"))
        for value in depfiles:
            path = self.path(value)
            if path.is_file():
                for dependency in make_dependencies(path.read_text(errors="replace")):
                    self.add(dependency, "observed-depfile:" + str(path))
        self.add("prebuilts/build-tools/linux-x86/bin/ninja", "runner-ninja")
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

    def metadata(self, manifest_path, task_path):
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
                "graph_inputs": graph_inputs, "system_tools": sorted(self.system_tools)}

    def archive(self, stream, metadata, task_path):
        with tarfile.open(fileobj=stream, mode="w|") as archive:
            for path in sorted(self.entries):
                archive.add(path, arcname=str(path).lstrip("/"), recursive=False)
            for path, (data, mode) in sorted(self.virtual_files.items()):
                info = tarfile.TarInfo(str(path).lstrip("/"))
                info.size, info.mode = len(data), mode
                info.uid = info.gid = 0
                archive.addfile(info, io.BytesIO(data))
            data = (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode()
            info = tarfile.TarInfo(str(task_path / "bundle.json").lstrip("/"))
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))


def pack(manifest_path, source_root, output, out_root=DEFAULT_OUT, allowed_generated=(), digest_cache=None):
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
                collector.archive(compressor.stdin, metadata, task_path)
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
    arguments = parser.parse_args()
    try:
        metadata_path, metadata = pack(arguments.manifest, arguments.source_root, arguments.output,
                                       arguments.out_root, arguments.allow_generated, arguments.digest_cache)
    except (CapsuleError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({"archive": arguments.output, "metadata": str(metadata_path),
                      "file_count": metadata["file_count"], "input_bytes": metadata["input_bytes"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
