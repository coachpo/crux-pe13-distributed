#!/usr/bin/env python3
"""Describe source-built Android frontend actors for a cold graph consumer."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shlex
import stat
import sys


SCALAR_OUTPUTS = (
    ("build_number", "soong/build_number.txt"),
    ("build_date", "build_date.txt"),
    ("build_fingerprint", "target/product/crux/build_fingerprint.txt"),
    ("build_thumbprint", "target/product/crux/build_thumbprint.txt"),
)
PATH_INTERPOSER_RELATIVE_OUTPUTS = (
    ".path_interposer",
    ".path_interposer_origpath",
    ".path/expr",
    ".path/tr",
)
GO_TOOL_RELATIVE_PATHS = (
    "prebuilts/go/linux-x86/bin/go",
    "prebuilts/go/linux-x86/pkg/tool/linux_amd64/asm",
    "prebuilts/go/linux-x86/pkg/tool/linux_amd64/compile",
    "prebuilts/go/linux-x86/pkg/tool/linux_amd64/link",
)


class FrontendError(ValueError):
    """Invalid frontend inputs or an ambiguous existing producer set."""


def _root(value, name):
    path = Path(value).resolve()
    if not path.is_absolute():
        raise FrontendError(f"{name} must be absolute")
    return path


def _beneath(path, root):
    try:
        Path(path).relative_to(Path(root))
        return True
    except ValueError:
        return False


def _ninja_escape(value):
    return str(value).replace("$", "$$").replace(" ", "$ ").replace(":", "$:")


def _ninja_command(command):
    return command.replace("$", "$$")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _validate_source(source_root):
    required_dirs = (
        "build/blueprint",
        "build/soong",
        "prebuilts/go/linux-x86",
    )
    for relative in required_dirs:
        if not (source_root / relative).is_dir():
            raise FrontendError(f"missing pinned frontend source directory: {source_root / relative}")
    required_files = (
        "build/blueprint/microfactory/microfactory.bash",
        "build/blueprint/bootstrap/bpglob/bpglob.go",
        "build/soong/cmd/path_interposer/main.go",
        "build/soong/ui/build/path.go",
        *GO_TOOL_RELATIVE_PATHS,
    )
    for relative in required_files:
        if not (source_root / relative).is_file():
            raise FrontendError(f"missing pinned frontend source/tool input: {source_root / relative}")


def _existing_set(existing_outputs, destination_out):
    normalized = set()
    for value in existing_outputs or ():
        path = Path(value)
        if not path.is_absolute():
            path = destination_out / path
        normalized.add(Path(_canonical_logical_path(path)))
    return normalized


def _canonical_logical_path(path):
    value = posixpath.normpath(str(path))
    return "/" + value.lstrip("/") if value.startswith("//") else value


def _logical_output(path):
    """Canonicalize an output identity lexically, never following output symlinks."""
    return Path(_canonical_logical_path(path))


def _literal_writer_command(output, data, mode):
    encoded = base64.b64encode(data).decode("ascii")
    program = (
        "import base64,os,pathlib; "
        f"p=pathlib.Path({str(output)!r}); "
        "p.parent.mkdir(parents=True,exist_ok=True); "
        f"p.write_bytes(base64.b64decode({encoded!r})); "
        f"os.chmod(str(p),{mode})"
    )
    return "/usr/bin/python3 -c " + shlex.quote(program)


def _go_bootstrap_command(source_root, destination_out, actor):
    cache = destination_out / ".cloud-frontends" / actor
    if actor == "bpglob":
        binary_relative = "soong/bpglob"
        go_package = "github.com/google/blueprint/bootstrap/bpglob"
        go_name = "bpglob"
        extra_args = ""
    elif actor == "path_interposer":
        binary_relative = ".path_interposer"
        go_package = "android/soong/cmd/path_interposer"
        go_name = ".path_interposer"
        extra_args = "-pkg-path android/soong=" + str(source_root / "build/soong")
    else:
        raise FrontendError("unsupported Go frontend actor: " + actor)
    output = destination_out / binary_relative
    build_dir = cache / "build"
    script = "; ".join((
        "set -euo pipefail",
        "umask 022",
        f"export GOROOT={shlex.quote(str(source_root / 'prebuilts/go/linux-x86'))}",
        f"export GOCACHE={shlex.quote(str(cache / 'gocache'))}",
        f"export GOPATH={shlex.quote(str(cache / 'gopath'))}",
        f"export GOTMPDIR={shlex.quote(str(cache / 'go-tmp'))}",
        f"export TMPDIR={shlex.quote(str(cache / 'tmp'))}",
        "export GOENV=off",
        "export GO111MODULE=off",
        f"export BUILDDIR={shlex.quote(str(build_dir))}",
        f"export BLUEPRINTDIR={shlex.quote(str(source_root / 'build/blueprint'))}",
        f"export SRCDIR={shlex.quote(str(source_root))}",
        f"export EXTRA_ARGS={shlex.quote(extra_args)}",
        'mkdir -p "$BUILDDIR" "$GOCACHE" "$GOPATH" "$GOTMPDIR" "$TMPDIR"',
        f"build_go {shlex.quote(go_name)} {shlex.quote(go_package)}",
        f"mkdir -p {shlex.quote(str(output.parent))}",
        f"cp {shlex.quote(str(build_dir / go_name))} {shlex.quote(str(output))}",
    ))
    command = ("PATH=/usr/bin:/bin BASH_ENV=" + shlex.quote(
        str(source_root / "build/blueprint/microfactory/microfactory.bash"))
        + " /bin/bash -eu -c " + shlex.quote(script))
    return output, cache, command


def _path_metadata_command(destination_out):
    code = "\n".join((
        "import os",
        "from pathlib import Path",
        "os.umask(0o022)",
        f"root=Path({str(destination_out)!r})",
        "root.mkdir(parents=True,exist_ok=True)",
        "directory=root/'.path'",
        "directory.mkdir(mode=0o777,exist_ok=True)",
        "orig=root/'.path_interposer_origpath'",
        "fd=os.open(str(orig),os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o777)",
        "with os.fdopen(fd,'wb') as stream: stream.write(b'/usr/bin:/bin')",
        "os.chmod(str(orig),0o755)",
        "for name in ('expr','tr'):",
        "    link=directory/name",
        "    if link.is_symlink():",
        "        if os.readlink(link)!='../.path_interposer': raise RuntimeError('Declared alias target differs: '+name)",
        "    elif link.exists(): raise RuntimeError('Declared alias path is not a symlink: '+str(link))",
        "    else: os.symlink('../.path_interposer',link)",
    ))
    return "/usr/bin/python3 -c " + shlex.quote("exec(" + repr(code) + ")")


def _validate_paths(source_root, frontend_out, destination_out, graph_dir):
    if not source_root.is_dir():
        raise FrontendError("source_root is not a mounted source tree: " + str(source_root))
    for name, path in (("frontend_out", frontend_out), ("destination_out", destination_out)):
        if path == source_root or source_root in path.parents:
            raise FrontendError(f"{name} must be outside source_root")
    if graph_dir == source_root or source_root in graph_dir.parents:
        raise FrontendError("graph_dir must be outside source_root")


def generate(source_root, frontend_out, destination_out, graph_dir, existing_outputs=None):
    """Create a companion Ninja graph and descriptor for cold source frontends.

    Existing graph output paths are used only to avoid creating duplicate
    producers. Scalar values and modes always come from the fresh hosted frontend
    output tree, never from prior output or hard-coded build identities.
    """
    source_root = _root(source_root, "source_root")
    frontend_out = _root(frontend_out, "frontend_out")
    destination_out = _root(destination_out, "destination_out")
    graph_dir = _root(graph_dir, "graph_dir")
    _validate_paths(source_root, frontend_out, destination_out, graph_dir)
    _validate_source(source_root)

    present = _existing_set(existing_outputs, destination_out)
    path_outputs = [destination_out / relative for relative in PATH_INTERPOSER_RELATIVE_OUTPUTS]
    path_present = [_logical_output(path) in present for path in path_outputs]
    if any(path_present) and not all(path_present):
        missing = [str(path) for path, exists in zip(path_outputs, path_present) if not exists]
        raise FrontendError("partial path_interposer producer group is ambiguous; missing: "
                            + ", ".join(missing))

    scalars = []
    for name, relative in SCALAR_OUTPUTS:
        source = frontend_out / relative
        try:
            details = source.lstat()
            if not stat.S_ISREG(details.st_mode):
                raise FrontendError("frontend scalar is not a regular file: " + str(source))
            data = source.read_bytes()
        except OSError as error:
            raise FrontendError(f"missing frontend scalar {relative} under {frontend_out}: {error}") from error
        output = destination_out / relative
        mode = stat.S_IMODE(details.st_mode)
        writer = _literal_writer_command(output, data, mode)
        exists = _logical_output(output) in present
        scalars.append({"name": name, "source_path": str(source), "output": str(output),
                        "size": len(data), "sha256": _sha256(data),
                        "contents_base64": base64.b64encode(data).decode("ascii"), "mode": mode,
                        "existing_producer": exists, "desired_command": writer})

    graph_dir.mkdir(parents=True, exist_ok=True)
    companion_path = graph_dir / "companion.ninja"
    descriptor_path = graph_dir / "descriptor.json"
    if companion_path.exists() or descriptor_path.exists():
        raise FrontendError("frontend graph outputs already exist; refusing to overwrite")

    source_inputs = [source_root / relative for relative in (
        "build/blueprint", "build/soong", "prebuilts/go/linux-x86")]
    lines = ["ninja_required_version = 1.7",
             "builddir = " + _ninja_escape(destination_out / ".cloud-frontends/ninja"), ""]
    actions = []
    output_dirs = []

    bpglob_output = destination_out / "soong/bpglob"
    bpglob_existing = _logical_output(bpglob_output) in present
    if not bpglob_existing:
        output, cache, command = _go_bootstrap_command(source_root, destination_out, "bpglob")
        rule = "cloud_frontend_bpglob"
        lines.extend(["rule " + rule, "  command = " + _ninja_command(command),
                      "  description = Build pinned Blueprint bpglob from source", "",
                      "build " + _ninja_escape(output) + ": " + rule + " "
                      + " ".join(_ninja_escape(path) for path in source_inputs), ""])
        actions.append({"id": "bpglob", "kind": "source-go-bootstrap", "output": str(output),
                        "source_inputs": [str(path) for path in source_inputs], "command": command,
                        "output_dirs": [str(cache)]})
        output_dirs.append(str(cache))

    if not all(path_present):
        output, cache, command = _go_bootstrap_command(source_root, destination_out, "path_interposer")
        rule = "cloud_frontend_path_interposer"
        lines.extend(["rule " + rule, "  command = " + _ninja_command(command),
                      "  description = Build original Go path_interposer from source", "",
                      "build " + _ninja_escape(output) + ": " + rule + " "
                      + " ".join(_ninja_escape(path) for path in source_inputs), ""])
        actions.append({"id": "path_interposer", "kind": "source-go-bootstrap", "output": str(output),
                        "source_inputs": [str(path) for path in source_inputs], "command": command,
                        "output_dirs": [str(cache)]})
        output_dirs.append(str(cache))

        metadata_outputs = path_outputs[1:]
        metadata_command = _path_metadata_command(destination_out)
        lines.extend(["rule cloud_frontend_path_metadata", "  command = " + _ninja_command(metadata_command),
                      "  description = Recreate admitted PATH aliases and original PATH metadata", "",
                      "build " + " ".join(_ninja_escape(path) for path in metadata_outputs)
                      + ": cloud_frontend_path_metadata | " + _ninja_escape(output), ""])
        actions.append({"id": "path_metadata", "kind": "reviewed-path-metadata",
                        "outputs": [str(path) for path in metadata_outputs],
                        "implicit_inputs": [str(output)], "command": metadata_command,
                        "original_path": "/usr/bin:/bin", "origpath_bytes": 13,
                        "aliases": {"expr": "../.path_interposer", "tr": "../.path_interposer"},
                        "directory_mode": 0o755, "origpath_mode": 0o755})
        output_dirs.append(str(destination_out / ".path"))

    native_scalar_overrides = []
    for scalar in scalars:
        if scalar["existing_producer"]:
            native_scalar_overrides.append({key: scalar[key] for key in (
                "name", "source_path", "output", "size", "sha256", "contents_base64", "mode",
                "desired_command")})
            continue
        rule = "cloud_frontend_scalar_" + scalar["name"]
        lines.extend(["rule " + rule, "  command = " + _ninja_command(scalar["desired_command"]),
                      "  description = Recreate fresh frontend " + scalar["name"], "",
                      "build " + _ninja_escape(scalar["output"]) + ": " + rule, ""])
        actions.append({"id": scalar["name"], "kind": "fresh-frontend-scalar",
                        "output": scalar["output"], "command": scalar["desired_command"],
                        "sha256": scalar["sha256"], "mode": scalar["mode"]})

    descriptor = {
        "schema_version": 1,
        "source_root": str(source_root),
        "frontend_out": str(frontend_out),
        "destination_out": str(destination_out),
        "companion_ninja": str(companion_path),
        "companion_ninja_sha256": _sha256(("\n".join(lines) + "\n").encode()),
        "descriptor_path": str(descriptor_path),
        "source_inputs": [str(path) for path in source_inputs],
        "actions": actions,
        "outputs": [output for action in actions for output in
                    ([action["output"]] if "output" in action else action.get("outputs", []))],
        "output_dirs": sorted(set(output_dirs)),
        "scalars": scalars,
        "native_scalar_overrides": native_scalar_overrides,
        "bpglob": {"output": str(bpglob_output),
                   "producer": "existing" if bpglob_existing else "companion"},
        "path_interposer": {"outputs": [str(path) for path in path_outputs],
                             "producer": "existing" if all(path_present) else "companion",
                             "partial_existing_producer_rejected": True},
        "existing_frontend_outputs": sorted(
            str(path) for path in [bpglob_output, *path_outputs, *(destination_out / rel for _, rel in SCALAR_OUTPUTS)]
            if _logical_output(path) in present),
    }
    companion_path.write_text("\n".join(lines) + "\n")
    descriptor_path.write_text(json.dumps(descriptor, indent=2, sort_keys=True) + "\n")
    return descriptor, companion_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--frontend-out", required=True)
    parser.add_argument("--out-root", required=True)
    parser.add_argument("--graph-dir", required=True)
    parser.add_argument("--existing-outputs", help="JSON array of current Ninja output paths")
    args = parser.parse_args(argv)
    try:
        if args.existing_outputs:
            existing = json.loads(Path(args.existing_outputs).read_text())
            if not isinstance(existing, list) or not all(isinstance(path, str) for path in existing):
                raise FrontendError("--existing-outputs must contain a JSON array of paths")
        else:
            existing = []
        descriptor, companion = generate(args.source_root, args.frontend_out, args.out_root,
                                         args.graph_dir, existing)
    except (FrontendError, OSError, json.JSONDecodeError) as error:
        print("cloud_frontends: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps({"descriptor": descriptor["descriptor_path"],
                      "companion_ninja": str(companion), "action_count": len(descriptor["actions"]),
                      "native_scalar_override_count": len(descriptor["native_scalar_overrides"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
