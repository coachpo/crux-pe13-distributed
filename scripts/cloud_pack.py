#!/usr/bin/env python3
"""Package and verify one cold-input slice using the maintained capsule collector."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time


CAPSULE_PATH = Path(__file__).resolve().with_name("capsule.py")
spec = importlib.util.spec_from_file_location("crux_capsule", CAPSULE_PATH)
capsule = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capsule)


class PackError(ValueError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise PackError("Unsafe shard-relative path: " + value)
    return path


def validate_inputs(shards: Path, shard_id: str, inventory_path: Path,
                    receipt_path: Path, source_manifest_path: Path,
                    graph_index: Path) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", shard_id):
        raise PackError("Invalid shard ID: " + shard_id)
    manifest_bytes = source_manifest_path.read_bytes()
    source_manifest = json.loads(manifest_bytes)
    if not isinstance(source_manifest, dict) or not isinstance(source_manifest.get("source_root"), str):
        raise PackError("Source manifest has no source_root")
    source_root_value = source_manifest["source_root"]
    if not Path(source_root_value).is_absolute():
        raise PackError("Source manifest source_root must be absolute")
    manifest_sha = _digest_bytes(manifest_bytes)

    receipt = json.loads(receipt_path.read_text())
    if not isinstance(receipt, dict) or receipt.get("status") != "success":
        raise PackError("Shard receipt is not successful")
    if receipt.get("manifest_sha256") != manifest_sha:
        raise PackError("Shard receipt uses a different source manifest")
    source_root = receipt.get("source_root")
    out_root = receipt.get("out_root")
    if source_root != source_root_value or not isinstance(out_root, str) or not Path(out_root).is_absolute():
        raise PackError("Shard receipt source_root/OUT_DIR differs from the current source view")
    pruned = receipt.get("pruned_database")
    if (not isinstance(pruned, dict) or not isinstance(pruned.get("bytes"), int)
            or not isinstance(pruned.get("sha256"), str)):
        raise PackError("Shard receipt has no pruned_database byte/SHA identity")
    graph_index = graph_index.resolve()
    if not graph_index.is_file():
        raise PackError("Missing restored pruned graph index: " + str(graph_index))
    graph_bytes = graph_index.stat().st_size
    graph_sha = sha256(graph_index)
    if graph_bytes != pruned["bytes"] or graph_sha != pruned["sha256"]:
        raise PackError("Restored pruned graph index differs from the shard receipt")

    inventory = json.loads(inventory_path.read_text())
    if not isinstance(inventory, dict) or inventory.get("schema_version") != 1:
        raise PackError("Unsupported slice inventory schema")
    plan_sha = inventory.get("plan_sha256")
    if not isinstance(plan_sha, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", plan_sha):
        raise PackError("Slice inventory has an invalid plan_sha256")
    entries = inventory.get("shards")
    if not isinstance(entries, dict) or shard_id not in entries:
        raise PackError("Selected shard is missing from slice inventory: " + shard_id)
    selected = entries[shard_id]
    if not isinstance(selected, dict):
        raise PackError("Slice inventory shard entry is invalid: " + shard_id)
    shard_dir = (shards / shard_id).resolve()
    if not shard_dir.is_dir() or not shard_dir.is_relative_to(shards.resolve()):
        raise PackError("Shard directory is missing or outside --shards: " + str(shard_dir))
    slice_manifest = shard_dir / "manifest.json"
    manifest_digest = selected.get("manifest_sha256")
    if not isinstance(manifest_digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", manifest_digest):
        raise PackError("Slice inventory has an invalid manifest SHA for " + shard_id)
    if slice_manifest.is_symlink() or not slice_manifest.is_file():
        raise PackError("Shard manifest is missing or unsafe: " + str(slice_manifest))
    if sha256(slice_manifest) != manifest_digest:
        raise PackError("Shard manifest differs from the slice inventory: " + shard_id)
    shard_manifest = json.loads(slice_manifest.read_text())
    if shard_manifest.get("source_root") != source_root:
        raise PackError("Shard manifest source_root differs from the shard receipt")
    ninja_files = selected.get("ninja_files")
    if not isinstance(ninja_files, dict) or not ninja_files:
        raise PackError("Slice inventory has no Ninja file hashes for " + shard_id)
    listed_ninja = set()
    for name, expected_sha in ninja_files.items():
        if not isinstance(name, str) or not isinstance(expected_sha, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha):
            raise PackError("Slice inventory contains an invalid Ninja file entry")
        relative = _safe_relative(name)
        path = shard_dir.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file():
            raise PackError("Shard Ninja file is missing or unsafe: " + name)
        if sha256(path) != expected_sha:
            raise PackError("Shard Ninja file differs from the slice inventory: " + name)
        listed_ninja.add(relative.as_posix())
    actual_ninja = {path.relative_to(shard_dir).as_posix() for path in shard_dir.rglob("*.ninja")
                    if path.is_file() and not path.is_symlink()}
    if actual_ninja != listed_ninja:
        raise PackError("Slice inventory Ninja file set differs from the selected shard")
    return {"source_root": source_root, "out_root": out_root, "manifest_sha256": manifest_sha,
            "graph_index_bytes": graph_bytes, "graph_index_sha256": graph_sha,
            "plan_sha256": plan_sha, "shard_dir": shard_dir,
            "slice_manifest": slice_manifest, "slice_manifest_sha256": manifest_digest,
            "shard_manifest": shard_manifest}


def _archive_name(value: str) -> str:
    name = value
    while name.startswith("./"):
        name = name[2:]
    if name.startswith("/"):
        raise PackError("Archive member is absolute: " + value)
    normalized = posixpath.normpath(name)
    if normalized in ("", ".") or normalized == ".." or normalized.startswith("../"):
        raise PackError("Unsafe archive member path: " + value)
    return normalized


def verify_tar_stream(stream, metadata: dict) -> dict:
    files = metadata.get("files")
    if not isinstance(files, list) or metadata.get("file_count") != len(files):
        raise PackError("Capsule metadata file_count differs from its files list")
    expected = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise PackError("Capsule metadata has an invalid file record")
        name = _archive_name(item["path"].lstrip("/"))
        if name in expected:
            raise PackError("Capsule metadata has duplicate paths: " + name)
        if item.get("type") not in {"file", "directory", "symlink"}:
            raise PackError("Unsupported capsule metadata file type: " + str(item.get("type")))
        expected[name] = item
    slice_path = metadata.get("slice_path")
    if not isinstance(slice_path, str) or not Path(slice_path).is_absolute():
        raise PackError("Capsule metadata has an invalid slice_path")
    bundle_name = _archive_name(str(Path(slice_path) / "bundle.json").lstrip("/"))
    if bundle_name in expected:
        raise PackError("Capsule file list conflicts with its bundle metadata member")
    seen, verified_files = set(), {}
    bundle_bytes = None
    hardlinks = 0
    kinds = {"file": 0, "directory": 0, "symlink": 0, "hardlink": 0}
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            name = _archive_name(member.name)
            if name in seen:
                raise PackError("Duplicate capsule archive member: " + name)
            seen.add(name)
            if name == bundle_name:
                if not member.isfile() or member.mode & 0o777 != 0o644:
                    raise PackError("bundle.json has an invalid archive type or mode")
                source = archive.extractfile(member)
                bundle_bytes = source.read() if source else b""
                if len(bundle_bytes) != member.size:
                    raise PackError("bundle.json size differs from its tar header")
                continue
            item = expected.get(name)
            if item is None:
                raise PackError("Unexpected capsule archive member: " + name)
            if stat.S_IMODE(member.mode) != item.get("mode"):
                raise PackError("Capsule archive mode differs from metadata: " + name)
            kind = item["type"]
            if kind == "directory":
                if not member.isdir() or member.size != 0 or item.get("size") != 0:
                    raise PackError("Capsule directory type/size mismatch: " + name)
                kinds["directory"] += 1
                continue
            if kind == "symlink":
                if (not member.issym() or member.size != 0 or member.linkname != item.get("target")
                        or item.get("size") != 0):
                    raise PackError("Capsule symlink target/type mismatch: " + name)
                kinds["symlink"] += 1
                continue
            if member.islnk():
                if member.size != 0:
                    raise PackError("Capsule hardlink has an unexpected payload: " + name)
                target_name = _archive_name(member.linkname)
                target = verified_files.get(target_name)
                if target is None:
                    raise PackError("Capsule hardlink does not refer to a prior verified member: " + name)
                actual = {"size": target["size"], "sha256": target["sha256"], "mode": target["mode"]}
                if (actual["size"] != item.get("size") or actual["sha256"] != item.get("sha256")
                        or actual["mode"] != item.get("mode")):
                    raise PackError("Capsule hardlink differs from its file metadata: " + name)
                verified_files[name] = actual
                hardlinks += 1
                kinds["hardlink"] += 1
                continue
            if not member.isfile() or member.size != item.get("size"):
                raise PackError("Capsule regular file type/size mismatch: " + name)
            source = archive.extractfile(member)
            if source is None:
                raise PackError("Capsule regular file has no data stream: " + name)
            digest = hashlib.sha256()
            size = 0
            for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                size += len(block)
                digest.update(block)
            if size != item["size"] or digest.hexdigest() != item.get("sha256"):
                raise PackError("Capsule regular file content differs from metadata: " + name)
            verified_files[name] = {"size": size, "sha256": digest.hexdigest(),
                                    "mode": stat.S_IMODE(member.mode)}
            kinds["file"] += 1
    if bundle_bytes is None:
        raise PackError("Capsule archive is missing bundle.json")
    if seen != set(expected) | {bundle_name}:
        missing = sorted((set(expected) | {bundle_name}) - seen)
        raise PackError("Capsule archive is missing members: " + ", ".join(missing[:10]))
    expected_bytes = sum(item.get("size", 0) for item in files)
    if metadata.get("input_bytes") != expected_bytes:
        raise PackError("Capsule metadata input_bytes differs from its files list")
    try:
        bundle = json.loads(bundle_bytes)
    except json.JSONDecodeError as error:
        raise PackError("Capsule bundle.json is invalid JSON") from error
    outside = {key: value for key, value in metadata.items() if key not in {"archive", "digest_cache"}}
    if bundle != outside:
        raise PackError("Embedded bundle.json differs from the external capsule metadata")
    return {"verified_file_count": len(expected), "member_count": len(seen),
            "input_bytes": expected_bytes, "hardlink_count": hardlinks,
            "type_counts": kinds, "bundle_path": bundle_name}


def verify_archive(archive_path: Path, metadata: dict) -> dict:
    if not shutil.which("zstd"):
        raise PackError("zstd is required to verify the runner capsule")
    stderr = tempfile.TemporaryFile()
    process = subprocess.Popen(["zstd", "-q", "-d", "-c", str(archive_path)],
                               stdout=subprocess.PIPE, stderr=stderr)
    try:
        if process.stdout is None:
            raise PackError("zstd decompressor did not provide a stream")
        result = verify_tar_stream(process.stdout, metadata)
        while process.stdout.read(8 * 1024 * 1024):
            pass
        process.stdout.close()
        status = process.wait()
        if status:
            stderr.seek(0)
            raise PackError("zstd verification failed: " + stderr.read().decode(errors="replace")[-2000:])
        return result
    except Exception:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        if process.stdout and not process.stdout.closed:
            process.stdout.close()
        stderr.close()


def _declared_paths(values, source_root: Path) -> set[str]:
    result = set()
    for value in values or []:
        if isinstance(value, dict):
            value = value.get("path")
        if not isinstance(value, str):
            raise PackError("Slice manifest contains an invalid declared path")
        path = Path(value)
        if not path.is_absolute():
            path = source_root / path
        result.add(os.path.abspath(path))
    return result


def pack_shard(shards: Path, shard_id: str, inventory_path: Path,
               shard_receipt_path: Path, source_manifest_path: Path,
               graph_index: Path, output: Path) -> tuple[int, dict]:
    output.mkdir(parents=True, exist_ok=True)
    receipt_path = output / "pack-receipt.json"
    if receipt_path.exists():
        raise PackError("Pack receipt already exists; refusing to overwrite")
    receipt = {"schema_version": 1, "status": "running", "shard_id": shard_id,
               "output": str(output.resolve())}
    start = time.monotonic()
    archive_path = output / (shard_id + ".tar.zst")
    metadata_path = output / (shard_id + ".json")
    try:
        identity = validate_inputs(shards, shard_id, inventory_path, shard_receipt_path,
                                   source_manifest_path, graph_index)
        source_root = Path(identity["source_root"]).resolve()
        out_root = Path(identity["out_root"]).resolve()
        resolved_output = output.resolve()
        if (resolved_output == source_root or source_root in resolved_output.parents
                or resolved_output == out_root or out_root in resolved_output.parents):
            raise PackError("Pack output directory must be outside source_root and OUT_DIR")
        slice_manifest = identity["slice_manifest"]
        if archive_path.exists() or metadata_path.exists():
            raise PackError("Shard archive/metadata already exists; refusing to overwrite")
        receipt.update({"source_manifest": str(source_manifest_path.resolve()),
                        "source_manifest_sha256": identity["manifest_sha256"],
                        "slice_manifest": str(slice_manifest),
                        "slice_manifest_sha256": identity["slice_manifest_sha256"],
                        "plan_sha256": identity["plan_sha256"],
                        "graph_index": {"bytes": identity["graph_index_bytes"],
                                        "sha256": identity["graph_index_sha256"]}})
        _, metadata = capsule.pack(
            slice_manifest, source_root, archive_path, out_root,
            allowed_generated=(), digest_cache=None, shared_manifest=None,
            graph_index=graph_index.resolve())
        if metadata.get("source_root") != str(source_root) or metadata.get("out_root") != str(out_root):
            raise PackError("Capsule metadata source/out roots differ from the shard receipt")
        if metadata.get("manifest_sha256") != identity["slice_manifest_sha256"]:
            raise PackError("Capsule metadata manifest SHA differs from the inventory")
        allowed = metadata.get("allowed_generated_inputs")
        if allowed != []:
            raise PackError("Cold-input capsule contains allowed_generated_inputs")
        files = metadata.get("files")
        if not isinstance(files, list) or metadata.get("file_count") != len(files):
            raise PackError("Capsule metadata has inconsistent file count")
        declared = _declared_paths(identity["shard_manifest"].get("outputs", []), source_root)
        declared.update(_declared_paths(identity["shard_manifest"].get("external_inputs", []), source_root))
        embedded = {os.path.abspath(item["path"]) for item in files if isinstance(item, dict) and isinstance(item.get("path"), str)}
        overlap = sorted(declared & embedded)
        if overlap:
            raise PackError("Capsule embeds declared outputs/external inputs: " + ", ".join(overlap[:10]))
        sidecar = json.loads(metadata_path.read_text())
        if sidecar != metadata:
            raise PackError("Capsule sidecar metadata differs from the packer result")
        archive = metadata.get("archive")
        archive_sha = sha256(archive_path)
        if (not isinstance(archive, dict) or archive.get("name") != archive_path.name
                or archive.get("size") != archive_path.stat().st_size
                or archive.get("sha256") != archive_sha):
            raise PackError("Capsule archive identity differs from its metadata")
        verification = verify_archive(archive_path, metadata)
        receipt.update({"status": "success", "archive": archive_path.name,
                        "archive_bytes": archive_path.stat().st_size,
                        "archive_sha256": archive_sha,
                        "file_count": metadata["file_count"],
                        "input_bytes": metadata["input_bytes"],
                        "verification": verification,
                        "elapsed_seconds": round(time.monotonic() - start, 3)})
        return 0, receipt
    except Exception as error:
        receipt.update({"status": "failed", "error": str(error),
                        "elapsed_seconds": round(time.monotonic() - start, 3)})
        if archive_path.is_file():
            receipt["partial_archive"] = {"name": archive_path.name,
                                          "bytes": archive_path.stat().st_size,
                                          "sha256": sha256(archive_path)}
        return 1, receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", required=True)
    parser.add_argument("--shard", required=True)
    parser.add_argument("--slice-inventory", required=True)
    parser.add_argument("--shard-receipt", required=True)
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--graph-index", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output).resolve()
    try:
        code, receipt = pack_shard(Path(args.shards), args.shard, Path(args.slice_inventory),
                                   Path(args.shard_receipt), Path(args.source_manifest),
                                   Path(args.graph_index), output)
        (output / "pack-receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    except (PackError, OSError, json.JSONDecodeError, capsule.CapsuleError, ValueError) as error:
        print("cloud_pack: " + str(error), file=sys.stderr)
        return 1
    if code:
        print("cloud_pack: " + receipt.get("error", "packing failed"), file=sys.stderr)
        return code
    print(json.dumps({"status": "success", "shard_id": args.shard,
                      "archive": receipt["archive"], "archive_bytes": receipt["archive_bytes"],
                      "file_count": receipt["file_count"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
