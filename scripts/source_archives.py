#!/usr/bin/env python3
"""Restore frozen public source archives and check the selected capsule bytes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


class ArchiveError(ValueError):
    pass


def beneath(path, root):
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def validate_spec(spec, source_root):
    path = Path(spec["path"])
    if not path.is_absolute() or ".." in path.parts or not beneath(path, source_root):
        raise ArchiveError("Input is outside the source root: " + str(path))
    if spec["type"] not in {"file", "directory", "symlink"}:
        raise ArchiveError("Unsupported input type: " + str(path))
    if spec["mode"] != stat.S_IMODE(spec["mode"]):
        raise ArchiveError("Input mode must contain permission bits only: " + str(path))
    if spec["type"] == "file" and (spec["size"] < 0 or len(spec["sha256"]) != 64):
        raise ArchiveError("Invalid input file identity: " + str(path))
    return path


def checked_parent(path, source_root):
    """Never let an archive member write through an existing directory symlink."""
    parts = path.parent.relative_to(source_root).parts
    current = source_root
    for part in (None, *parts):
        if part is not None:
            current /= part
        if current.is_symlink():
            raise ArchiveError("Symlink in archive destination parents: " + str(current))
        if current.exists() and not current.is_dir():
            raise ArchiveError("Non-directory archive destination parent: " + str(current))
        current.mkdir(parents=True, exist_ok=True)


def verify_spec(spec):
    path = Path(spec["path"])
    try:
        details = path.lstat()
    except FileNotFoundError:
        raise ArchiveError("Missing remote input: " + str(path)) from None
    actual = ("file" if stat.S_ISREG(details.st_mode) else
              "directory" if stat.S_ISDIR(details.st_mode) else
              "symlink" if stat.S_ISLNK(details.st_mode) else "unsupported")
    if actual != spec["type"] or stat.S_IMODE(details.st_mode) != spec["mode"]:
        raise ArchiveError("Remote input type/mode mismatch: " + str(path))
    if actual == "file" and (details.st_size != spec["size"] or digest(path) != spec["sha256"]):
        raise ArchiveError("Remote input bytes differ from the frozen capsule: " + str(path))
    if actual == "symlink" and os.readlink(path) != spec["target"]:
        raise ArchiveError("Remote input link differs from the frozen capsule: " + str(path))


def archive_relative(member_name, descriptor):
    path = PurePosixPath(member_name)
    if path.is_absolute() or ".." in path.parts:
        raise ArchiveError("Unsafe archive member: " + member_name)
    stripped = path.parts[descriptor.get("strip_components", 0):]
    if not stripped:
        return None
    subdir = PurePosixPath(descriptor.get("archive_subdir", ".")).parts
    if stripped[:len(subdir)] != subdir:
        return None
    return PurePosixPath(*stripped[len(subdir):])


class DownloadReader:
    def __init__(self, stream):
        self.stream = stream
        self.sha256 = hashlib.sha256()
        self.size = 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.sha256.update(data)
        self.size += len(data)
        return data


def extract_archive(descriptor, specs, source_root, timeout):
    """Stream only recorded inputs to their original absolute build paths."""
    install = Path(descriptor["install_path"])
    wanted = {str(Path(spec["path"]).relative_to(install)): spec for spec in specs}
    # Git archives do not carry the selected subtree root or empty directories.
    # Their exact local modes are supplied by the frozen capsule manifest.
    for spec in sorted(specs, key=lambda item: len(Path(item["path"]).parts)):
        if spec["type"] != "directory":
            continue
        path = Path(spec["path"])
        checked_parent(path, source_root)
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ArchiveError("Unexpected destination type: " + str(path))
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(spec["mode"])
    seen = set()
    with urllib.request.urlopen(descriptor["url"], timeout=timeout) as response:
        reader = DownloadReader(response)
        with tarfile.open(fileobj=reader, mode="r|gz") as archive:
            for member in archive:
                relative = archive_relative(member.name, descriptor)
                if relative is None or str(relative) not in wanted:
                    continue
                spec = wanted[str(relative)]
                kind = ("file" if member.isfile() else "directory" if member.isdir() else
                        "symlink" if member.issym() else "unsupported")
                if kind != spec["type"]:
                    raise ArchiveError("Archive member type mismatch: " + member.name)
                if str(relative) in seen:
                    raise ArchiveError("Duplicate selected archive member: " + member.name)
                seen.add(str(relative))
                path = Path(spec["path"])
                checked_parent(path, source_root)
                if kind == "directory":
                    path.chmod(spec["mode"])
                elif kind == "symlink":
                    if member.linkname != spec["target"]:
                        raise ArchiveError("Archive member link mismatch: " + member.name)
                    if path.is_symlink():
                        path.unlink()
                    elif path.exists():
                        raise ArchiveError("Unexpected existing symlink destination: " + str(path))
                    path.symlink_to(member.linkname)
                    if stat.S_IMODE(path.lstat().st_mode) != spec["mode"]:
                        raise ArchiveError("Host does not support the recorded symlink mode: " + str(path))
                else:
                    if member.size != spec["size"]:
                        raise ArchiveError("Archive member size mismatch: " + member.name)
                    if path.is_symlink() or (path.exists() and not path.is_file()):
                        raise ArchiveError("Unexpected destination type: " + str(path))
                    incoming = archive.extractfile(member)
                    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".remote-source-", delete=False) as target:
                        temporary = Path(target.name)
                        try:
                            shutil.copyfileobj(incoming, target, 1024 * 1024)
                            target.flush()
                            if digest(temporary) != spec["sha256"]:
                                raise ArchiveError("Archive member bytes differ from capsule: " + member.name)
                            temporary.chmod(spec["mode"])
                            os.replace(temporary, path)
                        finally:
                            temporary.unlink(missing_ok=True)
        for block in iter(lambda: reader.read(1024 * 1024), b""):
            pass
        result = {"archive_sha256": reader.sha256.hexdigest(), "download_bytes": reader.size}
    if descriptor.get("archive_sha256") and result["archive_sha256"] != descriptor["archive_sha256"]:
        raise ArchiveError("Archive digest mismatch: " + descriptor["url"])
    missing = sorted(key for key, spec in wanted.items() if spec["type"] != "directory" and key not in seen)
    if missing:
        raise ArchiveError("Selected inputs absent from archive: " + ", ".join(missing))
    for spec in specs:
        # Regular files were checked before their atomic replacement. Verify
        # directory/link identities here; the worker checks the full bundle
        # again before starting any compile action.
        if spec["type"] != "file":
            verify_spec(spec)
    return result


def select_archives(profile, bundles):
    if profile.get("schema_version") != 1:
        raise ArchiveError("Unsupported remote source profile schema")
    source_root = Path(profile["source_root"])
    if not source_root.is_absolute() or ".." in source_root.parts:
        raise ArchiveError("Source root must be an absolute path")
    inputs = {}
    for bundle in bundles:
        if bundle.get("schema_version") != 1 or Path(bundle["source_root"]) != source_root:
            raise ArchiveError("Capsule and remote source profile roots differ")
        for spec in bundle["files"]:
            # Capsules also record generated graph/out paths. Only source inputs
            # can be provided by these frozen repository archives.
            if not beneath(Path(spec["path"]), source_root):
                continue
            path = validate_spec(spec, source_root)
            identity = {key: spec[key] for key in ("path", "type", "mode", "size", "sha256", "target") if key in spec}
            if str(path) in inputs and inputs[str(path)] != identity:
                raise ArchiveError("Conflicting selected source identities: " + str(path))
            inputs[str(path)] = identity
    selected = []
    assigned = set()
    for descriptor in profile["archives"]:
        install = Path(descriptor["install_path"])
        parsed = urllib.parse.urlsplit(descriptor["url"])
        revision = descriptor["revision"]
        if (not install.is_absolute() or ".." in install.parts or not beneath(install, source_root)
                or parsed.scheme != "https" or parsed.username or parsed.password
                or len(revision) != 40 or any(ch not in "0123456789abcdef" for ch in revision)
                or revision not in descriptor["url"]):
            raise ArchiveError("Invalid frozen archive descriptor: " + str(descriptor))
        strip = descriptor.get("strip_components", 0)
        subdir = PurePosixPath(descriptor.get("archive_subdir", "."))
        if not isinstance(strip, int) or strip < 0 or subdir.is_absolute() or ".." in subdir.parts:
            raise ArchiveError("Invalid archive path mapping: " + descriptor["url"])
        specs = [spec for path, spec in inputs.items() if beneath(path, install)]
        overlap = assigned & {spec["path"] for spec in specs}
        if overlap:
            raise ArchiveError("Overlapping archive destinations: " + ", ".join(sorted(overlap)))
        if specs:
            assigned.update(spec["path"] for spec in specs)
            selected.append((descriptor, specs))
    return source_root, selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--bundle", action="append", required=True,
                        help="Frozen capsule/shared-layer JSON; may be repeated")
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--list", action="store_true", help="Show only the archives this capsule needs")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=60, help="Per network-read timeout in seconds")
    args = parser.parse_args()
    if args.attempts < 1 or args.timeout <= 0:
        parser.error("--attempts and --timeout must be positive")
    profile = json.loads(Path(args.profile).read_text())
    bundles = [json.loads(Path(path).read_text()) for path in args.bundle]
    root, selected = select_archives(profile, bundles)
    receipt = {"schema_version": 1, "source_root": str(root), "status": "planned" if args.list else "success",
               "profile_sha256": digest(args.profile), "bundle_sha256": {path: digest(path) for path in args.bundle},
               "archives": []}
    for descriptor, specs in selected:
        record = {"id": descriptor["id"], "url": descriptor["url"], "revision": descriptor["revision"],
                  "install_path": descriptor["install_path"], "verified_input_count": len(specs),
                  "selected_input_bytes": sum(spec.get("size", 0) for spec in specs)}
        if not args.list:
            print("Restore frozen source: " + descriptor["id"], flush=True)
            for attempt in range(1, args.attempts + 1):
                try:
                    record.update(extract_archive(descriptor, specs, root, args.timeout))
                    record["attempts"] = attempt
                    break
                except (urllib.error.URLError, TimeoutError, tarfile.ReadError, EOFError) as error:
                    if attempt == args.attempts:
                        raise
                    print("Retry source download " + descriptor["id"] + ": " + str(error), flush=True)
                    time.sleep(2 * attempt)
            print("Verified %d frozen inputs: %s" % (len(specs), descriptor["id"]), flush=True)
        receipt["archives"].append(record)
    receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
    destination = Path(args.receipt)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": receipt["status"], "archive_count": len(selected),
                      "selected_input_bytes": sum(item["selected_input_bytes"] for item in receipt["archives"]),
                      "receipt": str(destination)}, sort_keys=True))


if __name__ == "__main__":
    main()
