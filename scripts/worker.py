#!/usr/bin/env python3
"""Run one cold Ninja slice and exchange verified output archives between runners."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import resource
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time


def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def absolute(path, source_root):
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(source_root) / candidate
    # Do not resolve symlinks: a link itself is part of the transport contract.
    return Path(os.path.abspath(candidate))


def describe(path):
    path = Path(path)
    details = path.lstat()
    result = {"path": str(path), "mode": stat.S_IMODE(details.st_mode)}
    if stat.S_ISLNK(details.st_mode):
        result.update(type="symlink", target=os.readlink(path), size=details.st_size)
    elif stat.S_ISREG(details.st_mode):
        result.update(type="file", size=details.st_size, sha256=digest(path))
    elif stat.S_ISDIR(details.st_mode):
        result.update(type="directory", size=details.st_size)
    else:
        raise ValueError(f"unsupported output type: {path}")
    return result


def verify_spec(spec):
    path = Path(spec["path"])
    try:
        actual = describe(path)
    except FileNotFoundError:
        raise ValueError(f"missing input: {path}") from None
    keys = ["type", "mode"]
    if spec["type"] == "file":
        keys.extend(["size", "sha256"])
    elif spec["type"] == "symlink":
        keys.append("target")
    for key in keys:
        if actual[key] != spec[key]:
            raise ValueError(f"input integrity mismatch ({key}): {path}")
    return actual


def read_json(path):
    return json.loads(Path(path).read_text())


def require_file(path):
    if not Path(path).exists():
        raise ValueError(f"missing input: {path}")


def verify_bundle(bundle_path, manifest_path):
    bundle = read_json(bundle_path)
    if bundle.get("schema_version") != 1:
        raise ValueError("unsupported capsule schema")
    if digest(manifest_path) != bundle["manifest_sha256"]:
        raise ValueError("Ninja manifest integrity mismatch")
    for spec in bundle["files"]:
        verify_spec(spec)
    return bundle


def prepare_python2_aliases(bundle, source_root):
    """Expose the frozen hermetic Python 2 executable to source shebangs."""
    executable = Path(source_root) / "prebuilts/build-tools/linux-x86/bin/py2-cmd"
    spec = next((item for item in bundle["files"] if item["path"] == str(executable)), None)
    if spec is None:
        return []
    if spec["type"] != "file" or not spec["mode"] & 0o111:
        raise ValueError(f"frozen Python 2 runtime is not executable: {executable}")
    # verify_bundle has already checked this exact frozen executable. The
    # aliases only adapt the source shebang names to that declared runtime.
    directory = Path(source_root) / ".crux-task/host-bin"
    directory.mkdir(parents=True, exist_ok=True)
    aliases = []
    for name in ("python2", "python2.7"):
        alias = directory / name
        if alias.exists() or alias.is_symlink():
            if not alias.is_symlink() or os.readlink(alias) != str(executable):
                raise ValueError(f"conflicting Python 2 runtime alias: {alias}")
        else:
            alias.symlink_to(executable)
        aliases.append({"name": name, "path": str(alias), "target": str(executable), "target_sha256": spec["sha256"]})
    return aliases


def archive_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"invalid archive path: {name}")
    return "/" + str(path)


def merge_dependencies(directories):
    """Validate the producer receipt before accepting any compiled output."""
    accepted = {}
    receipts = []
    for directory in directories:
        directory = Path(directory)
        receipt_path = directory / "receipt.json"
        receipt = read_json(receipt_path)
        if receipt.get("schema_version") != 1 or receipt.get("status") != "success":
            raise ValueError(f"dependency did not complete successfully: {directory}")
        archive = directory / "outputs.tar.zst"
        if digest(archive) != receipt["archive_sha256"]:
            raise ValueError(f"dependency archive integrity mismatch: {directory}")
        specs = {item["path"]: item for item in receipt["outputs"]}
        if len(specs) != len(receipt["outputs"]):
            raise ValueError(f"duplicate dependency output: {directory}")
        for path, spec in specs.items():
            if path in accepted and accepted[path] != spec:
                raise ValueError(f"conflicting dependency output: {path}")
        process = subprocess.Popen(["zstd", "-dc", str(archive)], stdout=subprocess.PIPE)
        seen = set()
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as stream:
                for member in stream:
                    path = archive_path(member.name)
                    if path in seen or path not in specs:
                        raise ValueError(f"unrecorded or duplicate archive member: {path}")
                    seen.add(path)
                    spec = specs[path]
                    kind = "file" if member.isfile() else "symlink" if member.issym() else "directory" if member.isdir() else None
                    if kind != spec["type"] or stat.S_IMODE(member.mode) != spec["mode"]:
                        raise ValueError(f"dependency member metadata mismatch: {path}")
                    if kind == "file" and member.size != spec["size"]:
                        raise ValueError(f"dependency member size mismatch: {path}")
                    if kind == "symlink" and member.linkname != spec["target"]:
                        raise ValueError(f"dependency member link mismatch: {path}")
                    destination = Path(path)
                    if destination.exists() or destination.is_symlink():
                        verify_spec(spec)
                        # Still consume and check the archived bytes rather than
                        # trusting an identical pre-existing destination alone.
                    elif kind == "directory":
                        destination.mkdir(parents=True, exist_ok=True)
                        destination.chmod(spec["mode"])
                    elif kind == "symlink":
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.symlink_to(spec["target"])
                    if kind == "file":
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        content = stream.extractfile(member)
                        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
                            temp_path = Path(temporary.name)
                            try:
                                shutil.copyfileobj(content, temporary, 1024 * 1024)
                            except BaseException:
                                temp_path.unlink(missing_ok=True)
                                raise
                        try:
                            if digest(temp_path) != spec["sha256"]:
                                raise ValueError(f"dependency member integrity mismatch: {path}")
                            temp_path.chmod(spec["mode"])
                            if not destination.exists():
                                temp_path.replace(destination)
                        finally:
                            temp_path.unlink(missing_ok=True)
                    verify_spec(spec)
            if seen != set(specs):
                raise ValueError(f"dependency archive missing outputs: {sorted(set(specs) - seen)}")
            if process.wait() != 0:
                raise ValueError(f"dependency decompression failed: {archive}")
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
            process.wait()
        accepted.update(specs)
        receipts.append({"id": receipt["id"], "manifest_sha256": receipt["manifest_sha256"],
                         "receipt_sha256": digest(receipt_path), "archive_sha256": receipt["archive_sha256"]})
    return accepted, receipts


def output_specs(paths, source_root):
    selected = set()
    for value in paths:
        path = absolute(value, source_root)
        if not path.exists() and not path.is_symlink():
            raise ValueError(f"Ninja did not produce declared output: {path}")
        selected.add(path)
        if path.is_dir() and not path.is_symlink():
            for current, directories, files in os.walk(path, followlinks=False):
                selected.update(Path(current) / name for name in directories + files)
    return [describe(path) for path in sorted(selected)]


def pack_outputs(specs, archive):
    # Explicit members preserve links and prevent directory traversal from
    # adding outputs not covered by the receipt.
    with open(archive, "wb") as destination:
        process = subprocess.Popen(["zstd", "-T0", "-6", "-q", "-c"], stdin=subprocess.PIPE,
                                   stdout=destination)
        try:
            with tarfile.open(fileobj=process.stdin, mode="w|") as stream:
                for spec in specs:
                    stream.add(spec["path"], arcname=spec["path"].lstrip("/"), recursive=False)
            process.stdin.close()
            if process.wait() != 0:
                raise ValueError("output archive compression failed")
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
            process.wait()


def resources(source_root):
    result = {"cpu_count": os.cpu_count(), "disk": dict(zip(("total", "used", "free"), shutil.disk_usage(source_root)))}
    for path, name in [("/proc/meminfo", "meminfo"), ("/etc/os-release", "os_release")]:
        if Path(path).exists():
            result[name] = Path(path).read_text()
    return result


def retain_abi_diagnostics(source_root, build_environment, output_dir):
    if "DIST_DIR" not in build_environment:
        return []
    directory = absolute(build_environment["DIST_DIR"] + "/abidiffs", source_root)
    if not directory.is_dir() or directory.is_symlink():
        return []
    retained = []
    for current, directories, files in os.walk(directory, followlinks=False):
        directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
        for name in files:
            path = Path(current) / name
            # Only regular report files inside the actual diagnostic directory
            # are attachments; links must not expand collection into source/OUT.
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(directory)
            destination = Path(output_dir) / "abidiffs" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            retained.append({"source": str(path), "file": str(Path("abidiffs") / relative), "size": path.stat().st_size})
    return retained


def run_shard(manifest_path, bundle_path, shard_id, output_dir, dependency_dirs=(), ninja=None, jobs=4):
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    receipt = {"schema_version": 1, "id": shard_id, "status": "failed", "outputs": [],
               "started_at": datetime.now(timezone.utc).isoformat()}
    for variable, field in [("GITHUB_RUN_ID", "run_id"), ("GITHUB_RUN_ATTEMPT", "run_attempt"),
                            ("GITHUB_REPOSITORY", "repository"), ("GITHUB_SHA", "worker_commit")]:
        if variable in os.environ:
            receipt[field] = os.environ[variable]
    started = time.monotonic()
    archive = output_dir / "outputs.tar.zst"
    archive.unlink(missing_ok=True)
    with open(output_dir / "build.log", "w", buffering=1) as log:
        try:
            manifest_path = Path(manifest_path).resolve()
            manifest = read_json(manifest_path)
            if manifest.get("schema_version") != 1:
                raise ValueError("unsupported Ninja manifest schema")
            source_root = Path(manifest["source_root"])
            receipt.update(source_root=str(source_root), manifest_sha256=digest(manifest_path),
                           targets=manifest["targets"], edge_count=manifest["edge_count"])
            receipt["resources_before"] = resources(source_root)
            bundle = verify_bundle(bundle_path, manifest_path)
            if Path(bundle["source_root"]) != source_root:
                raise ValueError("capsule and Ninja source roots differ")
            for path in manifest["outputs"]:
                resolved = absolute(path, source_root)
                if resolved.exists() or resolved.is_symlink():
                    raise ValueError(f"cold task contains a pre-existing output: {resolved}")
            for path in manifest["external_inputs"]:
                resolved = absolute(path, source_root)
                if resolved.exists() or resolved.is_symlink():
                    raise ValueError(f"cold capsule contains a producer input: {resolved}")
            accepted, receipts = merge_dependencies(dependency_dirs)
            receipt["dependencies"] = receipts
            for path in manifest["external_inputs"]:
                resolved = str(absolute(path, source_root))
                if resolved not in accepted:
                    raise ValueError(f"external input has no producer receipt: {resolved}")
                verify_spec(accepted[resolved])
            for path in manifest["leaf_inputs"]:
                require_file(absolute(path, source_root))
            executable = Path(ninja) if ninja else source_root / "prebuilts/build-tools/linux-x86/bin/ninja"
            require_file(executable)
            build_environment = manifest.get("build_environment", {})
            if not isinstance(build_environment, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                                              for key, value in build_environment.items()):
                raise ValueError("build_environment must contain string keys and values")
            out_root = Path(bundle["out_root"])
            if not out_root.is_absolute():
                raise ValueError("capsule out_root must be an absolute path")
            for variable, expected in (("OUT_DIR", out_root), ("ANDROID_BUILD_TOP", source_root)):
                if variable in build_environment and (not build_environment[variable] or
                                                      absolute(build_environment[variable], source_root) != expected):
                    raise ValueError(f"{variable} differs from the verified capsule build context")
            build_environment = {**build_environment, "OUT_DIR": str(out_root), "ANDROID_BUILD_TOP": str(source_root),
                                 "NINJA_STATUS": "[%f/%t %e sec] ", "CCACHE_DISABLE": "1"}
            build_environment.setdefault("DIST_DIR", str(out_root / "dist"))
            receipt["out_root"] = str(out_root)
            runtime_aliases = prepare_python2_aliases(bundle, source_root)
            if runtime_aliases:
                alias_dir = str(source_root / ".crux-task/host-bin")
                build_environment["PATH"] = alias_dir + os.pathsep + build_environment.get("PATH", os.environ.get("PATH", os.defpath))
            receipt["runtime_aliases"] = runtime_aliases
            command = [str(executable), "-f", str(manifest_path.parent / manifest["ninja"]),
                       "-j", str(jobs), *manifest["targets"]]
            receipt["command"] = command
            receipt["build_environment"] = build_environment
            (output_dir / "command.json").write_text(json.dumps({"cwd": str(source_root), "argv": command,
                                                                 "environment": build_environment,
                                                                 "runtime_aliases": runtime_aliases}, indent=2) + "\n")
            print(json.dumps({"cwd": str(source_root), "argv": command}), file=log)
            result = subprocess.run(command, cwd=source_root, stdout=log, stderr=subprocess.STDOUT,
                                    env={**os.environ, **build_environment})
            receipt["returncode"] = result.returncode
            if result.returncode:
                raise ValueError(f"Ninja exited with status {result.returncode}")
            for path in manifest["outputs"]:
                resolved = absolute(path, source_root)
                if not resolved.exists() and not resolved.is_symlink():
                    raise ValueError(f"Ninja did not produce declared output: {resolved}")
            exported = manifest.get("export_outputs", manifest["outputs"])
            if not set(exported).issubset(set(manifest["outputs"])):
                raise ValueError("export_outputs contains an output outside this Ninja slice")
            receipt["verified_output_count"] = len(manifest["outputs"])
            receipt["outputs"] = output_specs(exported, source_root)
            pack_outputs(receipt["outputs"], archive)
            receipt["archive_sha256"] = digest(archive)
            receipt["archive_bytes"] = archive.stat().st_size
            receipt["status"] = "success"
        except Exception as error:
            receipt["error"] = str(error)
            print(f"ERROR: {error}", file=log)
            archive.unlink(missing_ok=True)
            try:
                if "build_environment" in receipt:
                    reports = retain_abi_diagnostics(receipt["source_root"], receipt["build_environment"], output_dir)
                    if reports:
                        receipt["abi_diagnostics"] = reports
                        print(f"Retained {len(reports)} ABI diagnostic attachments", file=log)
            except Exception as retention_error:
                receipt["diagnostic_retention_error"] = str(retention_error)
                print(f"ABI diagnostic retention failed: {retention_error}", file=log)
        finally:
            receipt["elapsed_seconds"] = time.monotonic() - started
            receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
            usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            receipt["child_resources"] = {"max_rss_kib": usage.ru_maxrss, "user_seconds": usage.ru_utime, "system_seconds": usage.ru_stime}
            if "source_root" in receipt and Path(receipt["source_root"]).exists():
                receipt["resources_after"] = resources(receipt["source_root"])
            (output_dir / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--dependency-dir", action="append", default=[])
    parser.add_argument("--ninja")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    receipt = run_shard(args.manifest, args.bundle, args.id, args.output_dir,
                        args.dependency_dir, args.ninja, args.jobs)
    print(json.dumps({key: receipt.get(key) for key in ("id", "status", "elapsed_seconds", "error")}))
    return 0 if receipt["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
