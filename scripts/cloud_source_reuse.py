#!/usr/bin/env python3
"""Reuse an unchanged SquashFS source layer from an earlier Actions run."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time


SUCCESS_STATUSES = {"fetched-and-extracted", "fetched-bundle-and-archived",
                    "fetched-git-and-archived"}
MANIFEST_PATH = "config/cloud-source-manifest.json"
PROJECT_CONTRACT_FIELDS = (
    "path", "revision", "archive_revision", "url", "archive_url", "archive_type",
    "expected_tree", "patches",
)


class ReuseError(ValueError):
    pass


class ContractChangedError(ReuseError):
    pass


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_digest(value) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return digest_bytes(data)


def canonical_layout(manifest: dict, layer_projects: set[str]) -> list[dict]:
    layout = manifest.get("layout", [])
    entries = []
    if isinstance(layout, list):
        raw_entries = ((entry.get("type"), entry) for entry in layout)
    elif isinstance(layout, dict):
        raw_entries = (("copy", entry) for entry in layout.get("copyfiles", []))
        raw_entries = list(raw_entries) + [("link", entry) for entry in layout.get("linkfiles", [])]
    else:
        raise ReuseError("Invalid source manifest layout")
    projects = [project["path"] for project in manifest.get("projects", [])]
    for kind, entry in raw_entries:
        source = entry.get("source", entry.get("src"))
        destination = entry.get("destination", entry.get("dest"))
        if not isinstance(source, str) or not isinstance(destination, str):
            raise ReuseError("Invalid source manifest layout entry")
        owners = [path for path in projects if source == path or source.startswith(path + "/")]
        if owners and max(owners, key=len) in layer_projects:
            normalized_kind = {"copyfiles": "copy", "linkfiles": "link"}.get(kind, kind)
            entries.append({"type": normalized_kind, "source": source, "destination": destination})
    return sorted(entries, key=lambda item: (item["source"], item["destination"], item["type"]))


def layer_contract(manifest: dict, layer_id: str) -> dict:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("projects"), list):
        raise ReuseError("Invalid source manifest projects")
    source_root = manifest.get("source_root")
    if not isinstance(source_root, str) or not source_root.startswith("/"):
        raise ReuseError("Invalid source root in source manifest")
    layers = manifest.get("layers")
    if not isinstance(layers, list):
        raise ReuseError("Invalid source manifest layers")
    if any(not isinstance(item, dict) for item in layers):
        raise ReuseError("Invalid source manifest layer entry")
    selected = [item for item in layers if item.get("id") == layer_id]
    if len(selected) != 1:
        raise ReuseError("Layer is absent or duplicated in source manifest: " + layer_id)
    layer_projects = set(selected[0].get("projects", []))
    if any(not isinstance(item, dict) or not isinstance(item.get("path"), str)
           for item in manifest["projects"]):
        raise ReuseError("Invalid source manifest project entry")
    project_by_path = {item["path"]: item for item in manifest["projects"]}
    if len(project_by_path) != len(manifest["projects"]):
        raise ReuseError("Duplicate project path in source manifest")
    if not layer_projects or len(layer_projects) != len(selected[0].get("projects", [])):
        raise ReuseError("Invalid project set for source layer: " + layer_id)
    if not layer_projects.issubset(project_by_path):
        raise ReuseError("Source layer references an unknown project: " + layer_id)
    projects = []
    for path in sorted(layer_projects):
        item = project_by_path[path]
        projects.append({key: item[key] for key in PROJECT_CONTRACT_FIELDS if key in item})
    return {
        "source_root": manifest.get("source_root"),
        "layer_id": layer_id,
        "projects": projects,
        "layout": canonical_layout(manifest, layer_projects),
    }


def layer_contract_sha256(manifest: dict, layer_id: str) -> str:
    return canonical_digest(layer_contract(manifest, layer_id))


def gh_json(args: list[str]) -> dict:
    try:
        result = subprocess.run(["gh", "api", "--method", "GET", *args], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False)
    except OSError as error:
        raise ReuseError("Could not run GitHub CLI: " + str(error)) from error
    if result.returncode:
        raise ReuseError("GitHub API request failed: " + (result.stderr.strip() or "exit %d" % result.returncode))
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ReuseError("GitHub API returned invalid JSON") from error


def fetch_prior_manifest(source_run: str, repository: str) -> tuple[str, bytes]:
    run = gh_json([f"repos/{repository}/actions/runs/{source_run}"])
    head_sha = run.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        raise ReuseError("Source run has no pinned head SHA")
    response = gh_json([f"repos/{repository}/contents/{MANIFEST_PATH}", "-f", "ref=" + head_sha])
    if response.get("encoding") != "base64" or not isinstance(response.get("content"), str):
        raise ReuseError("Could not retrieve the source-run manifest bytes")
    try:
        encoded = "".join(response["content"].split())
        manifest_bytes = base64.b64decode(encoded, validate=True)
        manifest = json.loads(manifest_bytes)
    except (ValueError, json.JSONDecodeError) as error:
        raise ReuseError("Source-run manifest content is invalid") from error
    if not isinstance(manifest, dict):
        raise ReuseError("Source-run manifest is not a JSON object")
    return head_sha, manifest_bytes


def validate_prior_receipt(old_manifest: dict, layer_id: str, old_manifest_sha: str,
                           artifact_dir: Path) -> tuple[bytes, str]:
    receipt_path = artifact_dir / "layer.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ReuseError("Prior source layer receipt is missing or not a regular file")
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    if not isinstance(receipt, dict):
        raise ReuseError("Prior source layer receipt is not a JSON object")
    if receipt.get("layer_id") != layer_id or receipt.get("manifest_sha256") != old_manifest_sha:
        raise ReuseError("Prior layer receipt does not match its source manifest")
    prior_contract = layer_contract(old_manifest, layer_id)
    expected_projects = {project["path"]: project["revision"] for project in prior_contract["projects"]}
    project_receipts = receipt.get("projects")
    if not isinstance(project_receipts, list) or len(project_receipts) != len(expected_projects):
        raise ReuseError("Prior layer receipt has an incomplete project set")
    seen = set()
    for item in project_receipts:
        if not isinstance(item, dict):
            raise ReuseError("Prior layer receipt has an invalid project entry")
        path = item.get("path")
        if (path not in expected_projects or path in seen
                or item.get("revision") != expected_projects[path]
                or item.get("status") not in SUCCESS_STATUSES):
            raise ReuseError("Prior layer receipt has a failed or mismatched project: " + str(path))
        seen.add(path)
    squashfs = receipt.get("squashfs")
    if not isinstance(squashfs, dict) or not isinstance(squashfs.get("parts"), list) or not squashfs["parts"]:
        raise ReuseError("Prior layer receipt has no SquashFS parts")
    parts_dir_name = squashfs.get("parts_directory")
    if not isinstance(parts_dir_name, str) or not parts_dir_name:
        raise ReuseError("Prior layer receipt has an invalid parts directory")
    parts_dir = PurePosixPath(parts_dir_name)
    if parts_dir.is_absolute() or ".." in parts_dir.parts or not parts_dir.parts:
        raise ReuseError("Prior layer receipt has an unsafe parts directory")
    parts_dir_path = artifact_dir.joinpath(*parts_dir.parts)
    if parts_dir_path.is_symlink() or not parts_dir_path.is_dir():
        raise ReuseError("Prior SquashFS parts directory is missing or unsafe")
    total_size = 0
    seen_names = set()
    for part in squashfs["parts"]:
        if not isinstance(part, dict):
            raise ReuseError("Prior layer receipt has an invalid part entry")
        name = part.get("name")
        if (not isinstance(name, str) or PurePosixPath(name).name != name
                or name in seen_names or not isinstance(part.get("size"), int)
                or part["size"] < 0 or not isinstance(part.get("sha256"), str)):
            raise ReuseError("Prior layer receipt has an invalid part entry")
        seen_names.add(name)
        part_path = parts_dir_path / name
        if part_path.is_symlink() or not part_path.is_file():
            raise ReuseError("Prior SquashFS part is missing or not a regular file: " + name)
        if part_path.stat().st_size != part["size"]:
            raise ReuseError("Prior SquashFS part size mismatch: " + name)
        digest = hashlib.sha256()
        with part_path.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != part["sha256"]:
            raise ReuseError("Prior SquashFS part SHA-256 mismatch: " + name)
        total_size += part["size"]
    if total_size != squashfs.get("size"):
        raise ReuseError("Prior SquashFS part sizes do not match the receipt image size")
    return receipt_bytes, digest_bytes(receipt_bytes)


def reuse_layer(manifest_path: Path, layer_id: str, source_run: str, repository: str,
                output: Path) -> dict:
    manifest_bytes = manifest_path.read_bytes()
    current_manifest = json.loads(manifest_bytes)
    current_manifest_sha = digest_bytes(manifest_bytes)
    current_contract = layer_contract(current_manifest, layer_id)
    current_contract_sha = canonical_digest(current_contract)
    if output.exists():
        raise ReuseError("Output directory already exists: " + str(output))
    _, old_manifest_bytes = fetch_prior_manifest(source_run, repository)
    old_manifest_sha = digest_bytes(old_manifest_bytes)
    old_manifest = json.loads(old_manifest_bytes)
    prior_contract_sha = layer_contract_sha256(old_manifest, layer_id)
    if prior_contract_sha != current_contract_sha:
        raise ContractChangedError("Layer contract changed; use a fresh source build for " + layer_id)
    with tempfile.TemporaryDirectory(prefix="cloud-source-reuse-") as temporary:
        artifact_dir = Path(temporary) / "artifact"
        artifact_dir.mkdir()
        command = ["gh", "run", "download", str(source_run), "--repo", repository,
                   "--name", "cloud-" + layer_id, "--dir", str(artifact_dir)]
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False)
        if result.returncode:
            raise ReuseError("Prior source layer artifact unavailable; build it fresh: " +
                             (result.stderr.strip() or "exit %d" % result.returncode))
        receipt_bytes, receipt_sha = validate_prior_receipt(
            old_manifest, layer_id, old_manifest_sha, artifact_dir)
        prior_receipt = json.loads(receipt_bytes)
        current_receipt = {**prior_receipt,
                           "manifest_sha256": current_manifest_sha,
                           "reuse": {"source_run": str(source_run),
                                     "original_manifest_sha256": old_manifest_sha,
                                     "original_receipt_sha256": receipt_sha,
                                     "contract_sha256": current_contract_sha}}
        output.mkdir(parents=True, exist_ok=False)
        try:
            allowed_children = {"layer.json", prior_receipt["squashfs"]["parts_directory"]}
            children = {child.name for child in artifact_dir.iterdir()}
            if not allowed_children.issubset(children) or children - allowed_children - {"reused-layer-original.json"}:
                raise ReuseError("Prior source artifact has unexpected files")
            for child in artifact_dir.iterdir():
                if child.name == "reused-layer-original.json":
                    continue
                target = output / child.name
                if child.name == "layer.json":
                    target.write_bytes(receipt_bytes)
                elif child.is_dir() and not child.is_symlink():
                    shutil.copytree(child, target, symlinks=True)
                elif child.is_file() and not child.is_symlink():
                    shutil.copy2(child, target)
                else:
                    raise ReuseError("Unexpected file type in source layer artifact: " + child.name)
            (output / "reused-layer-original.json").write_bytes(receipt_bytes)
            (output / "layer.json").write_text(json.dumps(current_receipt, indent=2, sort_keys=True) + "\n")
        except Exception:
            shutil.rmtree(output)
            raise
    return current_receipt


def source_job_active(source_run: str, repository: str, layer_id: str) -> bool:
    result = gh_json([f"repos/{repository}/actions/runs/{source_run}/jobs?per_page=100"])
    jobs = result.get("jobs")
    if not isinstance(jobs, list):
        raise ReuseError("Source run job listing is invalid: " + str(source_run))
    expected_name = f"sources ({layer_id})"
    active_statuses = {"queued", "in_progress", "waiting"}
    return any(isinstance(job, dict) and job.get("name") == expected_name
               and job.get("status") in active_statuses for job in jobs)


def reuse_candidates(manifest_path: Path, layer_id: str, source_runs: list[str],
                     repository: str, output: Path, wait_active: bool = False) -> dict:
    if not source_runs:
        raise ReuseError("No source run candidates were provided")
    contract_changed = set()
    last_errors = {}
    while True:
        for source_run in source_runs:
            if source_run in contract_changed:
                continue
            try:
                return reuse_layer(manifest_path, layer_id, source_run, repository, output)
            except ContractChangedError as error:
                contract_changed.add(source_run)
                last_errors[source_run] = str(error)
            except ReuseError as error:
                last_errors[source_run] = str(error)
        if not wait_active:
            raise ReuseError("No candidate source artifact could be reused: " +
                             "; ".join(f"{run}: {reason}" for run, reason in last_errors.items()))
        active_runs = []
        for source_run in source_runs:
            if source_run in contract_changed:
                continue
            if source_job_active(source_run, repository, layer_id):
                active_runs.append(source_run)
        if not active_runs:
            raise ReuseError("No reusable artifact and no matching source job is active: " +
                             "; ".join(f"{run}: {reason}" for run, reason in last_errors.items()))
        print("Waiting 30 seconds for active source layer job(s): " + ", ".join(active_runs), flush=True)
        time.sleep(30)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--layer", required=True)
    runs = parser.add_mutually_exclusive_group(required=True)
    runs.add_argument("--source-run")
    runs.add_argument("--source-runs", help="Comma-separated source run IDs, tried in order")
    parser.add_argument("--wait-active", action="store_true",
                        help="Wait and retry while a matching source layer job is active")
    parser.add_argument("--repository", required=True, help="owner/repository")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    source_runs = ([args.source_run] if args.source_run else args.source_runs.split(","))
    if (not source_runs or any(not run.isdecimal() for run in source_runs)
            or args.repository.count("/") != 1
            or any(not part for part in args.repository.split("/"))):
        parser.error("source run IDs must be numeric and --repository must be owner/repository")
    try:
        receipt = reuse_candidates(Path(args.manifest), args.layer, source_runs,
                                   args.repository, Path(args.output), args.wait_active)
    except (ReuseError, OSError, json.JSONDecodeError) as error:
        print("cloud_source_reuse: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps({"layer": receipt["layer_id"], "reused_from": receipt["reuse"]["source_run"],
                      "manifest_sha256": receipt["manifest_sha256"],
                      "contract_sha256": receipt["reuse"]["contract_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
