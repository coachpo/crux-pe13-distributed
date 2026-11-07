#!/usr/bin/env python3
"""Verify that an unpublished fresh-ROM artifact is bound to successful source runs."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import urllib.request
import zipfile

import cloud_layers
import dev_ota


API = cloud_layers.API
COMPILE_WORKFLOW = ".github/workflows/build-standard.yml"
ASSEMBLY_WORKFLOW = ".github/workflows/assemble-standard.yml"
EXPECTED_FILES = frozenset(dev_ota.DELIVERY_NAMES)
MAX_ASSET_SIZE = 2 * 1024 * 1024 * 1024


class SourceError(dev_ota.DevOtaError):
    """Source-run provenance or artifact verification failed."""


@dataclass(frozen=True)
class VerifiedDelivery:
    tag: str
    directory: Path
    assembly_commit: str
    assembly_run: str
    compile_run: str
    artifact_id: int
    artifact_digest: str
    assets: dict[str, dict]
    phone_manifest: str | None = None


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise SourceError(message)


def _is_sha(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    _require(bool(token), "GH_TOKEN or GITHUB_TOKEN is required for source verification")
    return token


def _json_api(path: str, token: str) -> dict:
    url = API + "/repos/" + dev_ota.REPOSITORY + "/" + path.lstrip("/")
    try:
        payload, _ = cloud_layers._request(url, token)
        value = json.loads(payload)
    except Exception as error:
        raise SourceError("GitHub source metadata request failed: " + str(error)) from error
    _require(isinstance(value, dict), "GitHub source metadata response is malformed")
    return value


def _run(run_id: str, workflow_path: str, token: str) -> dict:
    _require(isinstance(run_id, str) and run_id.isdecimal(), "invalid Actions run ID")
    record = _json_api("actions/runs/" + run_id, token)
    _require(str(record.get("id")) == run_id, "Actions run ID mismatch")
    _require(record.get("status") == "completed" and record.get("conclusion") == "success",
             "Actions run did not complete successfully: " + run_id)
    _require(record.get("event") == "workflow_dispatch", "Actions run was not manually dispatched: " + run_id)
    _require(record.get("head_branch") == "main", "Actions run did not use main: " + run_id)
    _require(record.get("path") == workflow_path, "Actions run used the wrong workflow: " + run_id)
    _require(isinstance(record.get("head_sha"), str)
             and re.fullmatch(r"[0-9a-f]{40}", record["head_sha"]) is not None,
             "Actions run has an invalid target commit: " + run_id)
    return record


def _inventory(run_id: str, token: str) -> dict:
    try:
        return cloud_layers.artifact_inventory(run_id, dev_ota.REPOSITORY, token)
    except Exception as error:
        raise SourceError("Actions artifact inventory failed: " + str(error)) from error


def _artifact(inventory: dict, name: str) -> dict:
    artifact = inventory.get(name)
    _require(isinstance(artifact, dict), "required Actions artifact is missing: " + name)
    _require(not artifact.get("expired"), "Actions artifact has expired: " + name)
    _require(isinstance(artifact.get("id"), int), "Actions artifact has an invalid ID: " + name)
    digest = artifact.get("digest")
    _require(isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest),
             "Actions artifact has no trusted SHA-256 digest: " + name)
    return artifact


def _download_zip(artifact: dict, destination: Path, token: str) -> tuple[Path, str]:
    url = f"{API}/repos/{dev_ota.REPOSITORY}/actions/artifacts/{artifact['id']}/zip"
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "crux-dev-release-source",
    })
    path = destination / (str(artifact["id"]) + ".zip")
    digest = hashlib.sha256()
    try:
        with cloud_layers._open_retry(request) as response, path.open("xb") as output:
            while True:
                block = response.read(8 * 1024 * 1024)
                if not block:
                    break
                output.write(block)
                digest.update(block)
    except Exception as error:
        path.unlink(missing_ok=True)
        raise SourceError("Actions artifact download failed: " + str(error)) from error
    actual = "sha256:" + digest.hexdigest()
    _require(actual == artifact["digest"], "downloaded Actions ZIP digest differs from GitHub metadata")
    expected_size = artifact.get("size_in_bytes")
    _require(type(expected_size) is int and path.stat().st_size == expected_size,
             "downloaded Actions ZIP size differs from GitHub metadata")
    return path, actual


def _read_members(zip_path: Path, members: set[str], output: Path,
                  max_size: int = MAX_ASSET_SIZE) -> dict[str, Path]:
    found = {}
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                name = info.filename
                posix = PurePosixPath(name)
                _require(not posix.is_absolute() and ".." not in posix.parts,
                         "unsafe Actions artifact member path")
                if name not in members:
                    continue
                _require(name not in found, "duplicate Actions artifact member: " + name)
                mode = info.external_attr >> 16
                _require(not info.is_dir() and (mode & 0o170000) != 0o120000
                         and info.file_size <= max_size,
                         "invalid or oversized Actions artifact member: " + name)
                target = output / Path(*posix.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest_size = 0
                with archive.open(info) as src, target.open("xb") as dst:
                    while True:
                        block = src.read(8 * 1024 * 1024)
                        if not block:
                            break
                        digest_size += len(block)
                        _require(digest_size <= max_size, "Actions artifact member exceeds 2 GiB: " + name)
                        dst.write(block)
                _require(digest_size == info.file_size, "Actions artifact member size mismatch: " + name)
                found[name] = target
    except (zipfile.BadZipFile, OSError) as error:
        raise SourceError("invalid Actions artifact ZIP: " + str(error)) from error
    missing = members - found.keys()
    _require(not missing, "Actions artifact is missing required files: " + ", ".join(sorted(missing)))
    return found


def _artifact_json(run_id: str, name: str, filename: str, work: Path, token: str) -> dict:
    artifact = _artifact(_inventory(run_id, token), name)
    zip_path, _ = _download_zip(artifact, work, token)
    extracted = _read_members(zip_path, {filename}, work / ("json-" + str(artifact["id"])))
    try:
        return json.loads(extracted[filename].read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SourceError("malformed " + filename + " in " + name) from error


def _validate_summary(summary: dict, compile_run: str, worker: str) -> None:
    _require(isinstance(summary, dict) and summary.get("schema_version") == 1
             and summary.get("status") == "success",
             "compile summary is not a successful build")
    _require(summary.get("execution_id") == compile_run, "compile summary execution ID mismatch")
    _require(summary.get("worker_commit") == worker,
             "compile summary worker does not match the compile run commit")
    fresh = summary.get("fresh_build")
    _require(isinstance(fresh, dict) and fresh.get("execution_id") == compile_run,
             "compile summary fresh-build binding mismatch")
    _require(isinstance(fresh.get("pack_run"), str) and fresh["pack_run"].isdecimal()
             and isinstance(fresh.get("shard_run"), str) and fresh["shard_run"].isdecimal()
             and _is_sha(fresh.get("plan_sha256"))
             and _is_sha(fresh.get("source_manifest_sha256")),
             "compile summary has invalid source/plan bindings")
    producers = summary.get("producers")
    _require(isinstance(producers, list) and producers and summary.get("task_count") == len(producers),
             "compile summary does not bind every compiled task to a producer")
    ids = set()
    waves = set()
    for producer in producers:
        _require(isinstance(producer, dict), "invalid compile producer binding")
        ident = producer.get("id")
        match = re.fullmatch(r"w([0-9]{3})-s[0-9]{3}", ident) if isinstance(ident, str) else None
        _require(match is not None, "invalid compile producer task ID")
        _require(ident not in ids, "duplicate compile producer task ID")
        ids.add(ident)
        waves.add(int(match.group(1)))
        _require(producer.get("run_id") == compile_run
                 and producer.get("artifact") == "shard-" + ident
                 and producer.get("expected_worker_commit") == worker
                 and _is_sha(producer.get("expected_manifest_sha256")),
                 "compile producer provenance mismatch: " + ident)
    _require(waves == set(range(8)), "compile summary does not cover all eight waves")


def verify_source(assembly_run: str, tag: str, work: Path) -> VerifiedDelivery:
    """Download and verify the fresh assembly artifact and its same-run lineage.

    `work` must be a new existing empty directory. The returned directory holds only
    the eight verified delivery files, under `delivery/`.
    """
    work = Path(work)
    _require(work.is_dir() and not any(work.iterdir()), "source verification work directory must be empty")
    parsed = dev_ota.parse_tag(tag)
    _require(parsed is not None, "invalid development delivery tag")
    token = _token()
    assembly = _run(assembly_run, ASSEMBLY_WORKFLOW, token)
    inventory = _inventory(assembly_run, token)
    candidates = [(name, match.group(1)) for name in inventory
                  if (match := re.fullmatch(r"cloud-fresh-rom-([1-9][0-9]*)", name))]
    _require(len(candidates) == 1, "assembly run does not identify exactly one fresh compile artifact")
    artifact_name, compile_run = candidates[0]
    _require(assembly.get("display_title") == "Verify fresh PE13 compilation " + compile_run,
             "assembly run title does not match its cold compilation artifact")
    compile = _run(compile_run, COMPILE_WORKFLOW, token)
    _require(compile.get("display_title") == "Fresh PE13 compile " + parsed["build"],
             "compile run title does not match the requested build")
    summary = _artifact_json(compile_run, "cloud-fresh-build-summary", "build-summary.json", work, token)
    _validate_summary(summary, compile_run, compile["head_sha"])

    artifact = _artifact(inventory, artifact_name)
    zip_path, zip_digest = _download_zip(artifact, work, token)
    names = {"delivery/" + name for name in EXPECTED_FILES}
    _read_members(zip_path, names | {"assembly-receipt.json"}, work)
    verified_assets = _verify_delivery(tag, parsed["build"], work, summary)
    return VerifiedDelivery(tag, work / "delivery", assembly["head_sha"], assembly_run,
                            compile_run, artifact["id"], zip_digest, verified_assets)


def _verify_delivery(tag: str, build: str, work: Path, summary: dict) -> dict[str, dict]:
    delivery_dir = work / "delivery"
    record_path = delivery_dir / "delivery-record.json"
    record = json.loads(record_path.read_text())
    sums = dev_ota.parse_sums((delivery_dir / "SHA256SUMS").read_text())
    receipt = json.loads((work / "assembly-receipt.json").read_text())
    _require(isinstance(receipt, dict) and isinstance(record, dict),
             "assembly receipt or delivery record is malformed")
    _require(receipt.get("schema_version") == 1 and receipt.get("status") == "success"
             and receipt.get("build_number") == build
             and receipt.get("product") == dev_ota.PRODUCT,
             "assembly receipt does not match successful requested build")
    source_selection = record.get("source_selection")
    fresh = summary["fresh_build"]
    _require(isinstance(source_selection, dict)
             and source_selection.get("shard_run") == fresh["shard_run"]
             and source_selection.get("source_manifest_sha256") == fresh["source_manifest_sha256"]
             and source_selection.get("build_number") == build
             and source_selection.get("target_product") == "aosp_crux"
             and source_selection.get("target_build_variant") == "userdebug",
             "delivery source selection differs from the successful compile summary")
    source_env = source_selection.get("env", {})
    _require(isinstance(source_env, dict) and source_env.get("USE_CCACHE") == "0"
             and source_env.get("CCACHE_DISABLE") == "1",
             "delivery source selection does not confirm ccache is disabled")
    assets = record.get("delivery")
    _require(isinstance(assets, dict) and set(assets) == dev_ota.DELIVERY_ASSET_NAMES,
             "delivery record has an incomplete asset set")
    _require(all(isinstance(value, dict) and type(value.get("size")) is int
                 and value["size"] > 0 and _is_sha(value.get("sha256"))
                 for value in assets.values()), "delivery record has invalid asset metadata")
    receipt_assets = receipt.get("assets")
    _require(isinstance(receipt_assets, dict), "assembly receipt has no asset inventory")
    _require(set(receipt_assets) == set(assets), "assembly receipt asset inventory mismatch")
    record_producers = record.get("producers")
    _require(isinstance(record_producers, list) and len(record_producers) == 7,
             "delivery record does not bind exactly seven assembly producers")
    _require(all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in record_producers),
             "delivery record has malformed producer bindings")
    selected = {producer["id"]: producer for producer in summary["producers"]}
    _require(len({item.get("id") for item in record_producers}) == 7
             and all(item == selected.get(item.get("id")) for item in record_producers),
             "delivery producer bindings differ from successful compile summary")
    verified = {}
    for name in sorted(EXPECTED_FILES):
        path = delivery_dir / name
        size = path.stat().st_size
        _require(0 < size < MAX_ASSET_SIZE, "invalid release asset size: " + name)
        digest = dev_ota.sha256(path)
        if name != "SHA256SUMS":
            _require(sums.get(name) == digest, "asset digest differs from SHA256SUMS: " + name)
        if name in assets:
            _require(size == assets[name]["size"], "delivery asset size mismatch: " + name)
            _require(receipt_assets[name] == digest, "assembly receipt digest mismatch: " + name)
        verified[name] = {"size": size, "digest": "sha256:" + digest}
    release = {"tag": tag, "build": build,
               "assets": [{"name": name, **meta} for name, meta in verified.items()]}
    dev_ota.validate_record(release, record, sums)
    return verified
