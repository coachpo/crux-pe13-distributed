#!/usr/bin/env python3
"""Materialize a current cold capsule only after its shard cache misses.

The audit descriptor is re-derived from the actual packed bytes, not trusted as
a substitute for the existing cold packer's archive and generated-input gates.
Explicit expired inputs fail closed; this entry does not select another run.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import cloud_artifacts
import cloud_pack
import cloud_reuse
import pipeline_code

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config/cloud-source-manifest.json"


class DeferredPackError(ValueError):
    pass


def _paths(context, output, staging):
    source, out = Path(context["source_root"]), Path(context["out_root"])
    if not source.is_absolute() or not out.is_absolute():
        raise DeferredPackError("Source and OUT roots must be absolute")
    roots = [source.resolve(), out.resolve(), output.resolve(), staging.resolve()]
    for index, left in enumerate(roots):
        for right in roots[index + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise DeferredPackError("Source, OUT, output and staging paths must be separate")
    if os.path.lexists(source) or os.path.lexists(out):
        raise DeferredPackError("Deferred packing requires a cold source/OUT view")
    for path in (output, staging):
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise DeferredPackError("Deferred output/staging directory is not empty: " + str(path))


def pack(context: dict, shard_id: str, output: Path, staging: Path,
         manifest_path: Path = MANIFEST, environment=None) -> dict:
    environment = dict(os.environ if environment is None else environment)
    output, staging, manifest_path = Path(output).resolve(), Path(staging).resolve(), Path(manifest_path).resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", shard_id):
        raise DeferredPackError("Invalid shard ID")
    task = context["tasks"][shard_id]
    expected = task["reuse_descriptor"]
    if not isinstance(expected, dict) or not expected.get("input_identity"):
        raise DeferredPackError("Current task has no audited input descriptor")
    if context["worker_commit"] != environment.get("GITHUB_SHA"):
        raise DeferredPackError("Context pipeline code differs from this execution")
    if context["repository"] != environment.get("GITHUB_REPOSITORY"):
        raise DeferredPackError("Context repository differs from this execution")
    if cloud_pack.sha256(manifest_path) != context["source_manifest_sha256"]:
        raise DeferredPackError("Source manifest differs from the admitted current context")
    if json.loads(manifest_path.read_text()).get("source_root") != context["source_root"]:
        raise DeferredPackError("Source mount root differs from the admitted current context")
    _paths(context, output, staging)
    pipeline_code.verify(context["repository"], context["worker_commit"],
                         [str(context[key]) for key in ("graph_run", "index_run", "shard_run")])
    output.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    receipt = {"schema_version": 1, "status": "failed", "shard_id": shard_id,
               "execution_id": context["execution_id"], "shard_run": context["shard_run"]}
    try:
        shard_input, shard_tree = staging / "shard-input", staging / "shard-tree"
        shard_tree.mkdir()
        cloud_artifacts.download_artifacts(
            str(context["shard_run"]), context["repository"], shard_input,
            token=environment.get("GH_TOKEN") or environment.get("GITHUB_TOKEN"),
            name="cloud-shards-" + context["build_number"])
        subprocess.run(["tar", "--zstd", "-xf", str(shard_input / "shards.tar.zst"),
                        "-C", str(shard_tree)], check=True, env=environment)
        subprocess.run(["tar", "--zstd", "-xf", str(shard_input / "derived-graph.tar.zst"),
                        "-C", "/"], check=True, env=environment)
        graph_index = staging / "index-pruned.sqlite"
        subprocess.run(["zstd", "-d", str(shard_input / "index-pruned.sqlite.zst"),
                        "-o", str(graph_index)], check=True, env=environment)
        subprocess.run([sys.executable, str(ROOT / "scripts/cloud_layers.py"),
                        "--manifest", str(manifest_path), "--source-run", str(context["source_run"]),
                        "--repository", context["repository"], "--staging", str(staging / "source-layers"),
                        "--evidence", str(output / "source")], check=True, env=environment)
        shards = shard_tree / "shards"
        code, packed = cloud_pack.pack_shard(
            shards, shard_id, shard_input / "slice-inventory.json",
            shard_input / "shard-receipt.json", manifest_path, graph_index, output)
        receipt.update(packed)
        if code or packed.get("status") != "success":
            raise DeferredPackError(packed.get("error", "Current cold capsule packing failed"))
        manifest = json.loads((shards / shard_id / "manifest.json").read_text())
        metadata = json.loads((output / (shard_id + ".json")).read_text())
        descriptor = cloud_reuse.input_descriptor(manifest, metadata, cloud_reuse.execution_digest())
        if descriptor.get("input_identity") != expected["input_identity"] or descriptor != expected:
            raise DeferredPackError("Packed capsule inputs differ from the current audit descriptor")
        receipt.update({"status": "success", "input_identity": descriptor["input_identity"],
                        "reuse_descriptor": descriptor})
    except Exception as error:
        receipt.update({"status": "failed", "error": str(error)})
    (output / "pack-receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("pack", help="materialize one current cache-miss capsule")
    command.add_argument("--context", type=Path, required=True)
    command.add_argument("--id", required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--staging", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        context = json.loads(args.context.read_text())
        receipt = pack(context, args.id, args.output, args.staging)
    except (DeferredPackError, OSError, KeyError, ValueError, subprocess.CalledProcessError) as error:
        print("cloud_deferred: " + str(error), file=sys.stderr)
        return 1
    if receipt["status"] != "success":
        print("cloud_deferred: " + receipt["error"], file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
