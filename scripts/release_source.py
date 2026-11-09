#!/usr/bin/env python3
"""Verify that a fresh assembly artifact and its ZIP payload come from successful source runs."""
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


API = cloud_layers.API
REPOSITORY = "coachpo/crux-pe13-distributed"
PRODUCT = "aosp_crux-userdebug"
COMPILE_WORKFLOW = ".github/workflows/build-standard.yml"
ASSEMBLY_WORKFLOW = ".github/workflows/assemble-standard.yml"
COMPILE_TITLE_RE = re.compile(r"Fresh PE13 compile ([0-9]{10})")
ZIP_MANIFEST_ARTIFACT = "zip-manifest"
TAG_RE = re.compile(r"crux-pe13-([0-9]{10})-v([1-9][0-9]*)")
DELIVERY_NAMES = ("delivery-record.json", "SHA256SUMS", "boot.img", "recovery.img",
                  "rom-cepheus.itb", "recovery-cepheus.itb", "system.img.zst", "vendor.img")
DELIVERY_ASSET_NAMES = frozenset(DELIVERY_NAMES) - {"delivery-record.json", "SHA256SUMS"}
PAYLOAD_NAMES = ("update-binary", "crux-install.txt", "README.txt", "recovery-cepheus.itb",
                 "rom-cepheus.itb", "system.raw.img.zst", "vendor.raw.img.zst", "payload.json")
MAX_MEMBER_SIZE = 4 * 1024 * 1024 * 1024


class SourceError(RuntimeError):
    """Source-run provenance or artifact verification failed."""


@dataclass(frozen=True)
class VerifiedBuild:
    tag: str
    build: str
    payload: Path
    assembly_commit: str
    assembly_run: str
    compile_run: str
    artifact_id: int
    artifact_digest: str
    manifest_sha256: str


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise SourceError(message)


def _is_sha(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def sha256(path) -> str:
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            result.update(chunk)
    return result.hexdigest()


def parse_tag(tag):
    match = TAG_RE.fullmatch(tag or "")
    return {"build": match.group(1), "version": int(match.group(2))} if match else None


def parse_sums(text: str) -> dict[str, str]:
    sums = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        _require(len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]),
                 "invalid SHA256SUMS line: " + line)
        _require(parts[1] not in sums, "duplicate SHA256SUMS entry: " + parts[1])
        sums[parts[1]] = parts[0]
    _require(bool(sums), "empty SHA256SUMS")
    return sums


def _token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    _require(bool(token), "GH_TOKEN or GITHUB_TOKEN is required for source verification")
    return token


def _json_api(path: str, token: str) -> dict:
    url = API + "/repos/" + REPOSITORY + "/" + path.lstrip("/")
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
        return cloud_layers.artifact_inventory(run_id, REPOSITORY, token)
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
    url = f"{API}/repos/{REPOSITORY}/actions/artifacts/{artifact['id']}/zip"
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "crux-release-source",
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


def _read_members(zip_path: Path, members: set[str], output: Path) -> dict[str, Path]:
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
                         and info.file_size <= MAX_MEMBER_SIZE,
                         "invalid or oversized Actions artifact member: " + name)
                target = output / Path(*posix.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with archive.open(info) as src, target.open("xb") as dst:
                    while True:
                        block = src.read(8 * 1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        _require(size <= MAX_MEMBER_SIZE, "Actions artifact member is oversized: " + name)
                        dst.write(block)
                _require(size == info.file_size, "Actions artifact member size mismatch: " + name)
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


def _lineage(assembly_run: str, token: str):
    """Return the successful assembly, its artifacts, compile run and build number."""
    assembly = _run(assembly_run, ASSEMBLY_WORKFLOW, token)
    inventory = _inventory(assembly_run, token)
    candidates = [(name, match.group(1)) for name in inventory
                  if (match := re.fullmatch(r"cloud-fresh-rom-([1-9][0-9]*)", name))]
    _require(len(candidates) == 1, "assembly run does not identify exactly one fresh compile artifact")
    artifact_name, compile_run = candidates[0]
    _require(assembly.get("display_title") == "Verify fresh PE13 compilation " + compile_run,
             "assembly run title does not match its cold compilation artifact")
    compile = _run(compile_run, COMPILE_WORKFLOW, token)
    match = COMPILE_TITLE_RE.fullmatch(compile.get("display_title") or "")
    _require(match is not None, "compile run title does not name its build number")
    return assembly, inventory, artifact_name, compile_run, compile, match.group(1)


def assembly_build(assembly_run: str) -> str:
    """Return the build number of a successful assembly run's compilation."""
    return _lineage(assembly_run, _token())[-1]


def signing_manifest(assembly_run: str, work: Path) -> tuple[str, Path]:
    """Download the install manifest a successful assembly retained for signing.

    Only the identity fields are checked here; the release verifies the whole payload
    and the signature over these exact bytes before it publishes anything.
    """
    work = Path(work)
    _require(work.is_dir() and not any(work.iterdir()), "signing work directory must be empty")
    token = _token()
    _, inventory, _, _, _, build = _lineage(assembly_run, token)
    zip_path, _ = _download_zip(_artifact(inventory, ZIP_MANIFEST_ARTIFACT), work, token)
    manifest = _read_members(zip_path, {"crux-install.txt"}, work / "manifest")["crux-install.txt"]
    zip_path.unlink()
    _manifest_fields(manifest.read_bytes(), build)
    return build, manifest


def verify_source(assembly_run: str, tag: str, work: Path) -> VerifiedBuild:
    """Download and verify the fresh assembly artifact, its same-run lineage and ZIP payload.

    `work` must be a new existing empty directory. The returned payload directory holds
    the verified crux_zip.py payload of this build, under `zip-payload/`.
    """
    work = Path(work)
    _require(work.is_dir() and not any(work.iterdir()), "source verification work directory must be empty")
    parsed = parse_tag(tag)
    _require(parsed is not None, "invalid release tag")
    token = _token()
    assembly, inventory, artifact_name, compile_run, compile, build = _lineage(assembly_run, token)
    _require(build == parsed["build"], "compile run title does not match the requested build")
    summary = _artifact_json(compile_run, "cloud-fresh-build-summary", "build-summary.json", work, token)
    _validate_summary(summary, compile_run, compile["head_sha"])

    artifact = _artifact(inventory, artifact_name)
    zip_path, zip_digest = _download_zip(artifact, work, token)
    names = ({"delivery/" + name for name in DELIVERY_NAMES} | {"zip-payload/" + name for name in PAYLOAD_NAMES}
             | {"assembly-receipt.json"})
    _read_members(zip_path, names, work)
    zip_path.unlink()
    record = _verify_delivery(parsed["build"], work, summary)
    manifest_sha = _verify_payload(parsed["build"], work / "zip-payload", record)
    return VerifiedBuild(tag, parsed["build"], work / "zip-payload", assembly["head_sha"], assembly_run,
                         compile_run, artifact["id"], zip_digest, manifest_sha)


def _verify_delivery(build: str, work: Path, summary: dict) -> dict:
    delivery_dir = work / "delivery"
    record = json.loads((delivery_dir / "delivery-record.json").read_text())
    sums = parse_sums((delivery_dir / "SHA256SUMS").read_text())
    receipt = json.loads((work / "assembly-receipt.json").read_text())
    _require(isinstance(receipt, dict) and isinstance(record, dict),
             "assembly receipt or delivery record is malformed")
    _require(receipt.get("schema_version") == 1 and receipt.get("status") == "success"
             and receipt.get("build_number") == build and receipt.get("product") == PRODUCT,
             "assembly receipt does not match successful requested build")
    _require(record.get("schema_version") == 1 and record.get("status") == "success"
             and record.get("build_number") == build and record.get("product") == PRODUCT,
             "delivery record does not match successful requested build")
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
             and source_env.get("CCACHE_DISABLE") == "1"
             and str(source_env.get("BUILD_FINGERPRINT", "")).endswith(f"/{build}:userdebug/test-keys"),
             "delivery source selection is not a ccache-free Dev test-keys build of this number")
    assets = record.get("delivery")
    _require(isinstance(assets, dict) and set(assets) == DELIVERY_ASSET_NAMES,
             "delivery record has an incomplete asset set")
    _require(set(sums) == DELIVERY_ASSET_NAMES | {"delivery-record.json"},
             "SHA256SUMS and delivery assets differ")
    _require(sums["delivery-record.json"] == sha256(delivery_dir / "delivery-record.json"),
             "delivery record digest differs from SHA256SUMS")
    receipt_assets = receipt.get("assets")
    _require(isinstance(receipt_assets, dict) and set(receipt_assets) == set(assets),
             "assembly receipt asset inventory mismatch")
    record_producers = record.get("producers")
    _require(isinstance(record_producers, list) and len(record_producers) == 7
             and all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in record_producers),
             "delivery record does not bind exactly seven assembly producers")
    selected = {producer["id"]: producer for producer in summary["producers"]}
    _require(len({item.get("id") for item in record_producers}) == 7
             and all(item == selected.get(item.get("id")) for item in record_producers),
             "delivery producer bindings differ from successful compile summary")
    for name in sorted(DELIVERY_ASSET_NAMES):
        path = delivery_dir / name
        digest = sha256(path)
        _require(sums.get(name) == digest, "asset digest differs from SHA256SUMS: " + name)
        _require(path.stat().st_size == assets[name].get("size") and assets[name].get("sha256") == digest
                 and receipt_assets[name] == digest, "delivery asset differs from its record: " + name)
    fit = record.get("fit_validation", {})
    _require(fit.get("release_ready") is False and fit.get("development_adb") is True,
             "the FITs are not the development ADB profile")
    images = record.get("gates", {}).get("images", {})
    _require(all(images.get(role, {}).get("failures") == [] for role in ("boot", "recovery")),
             "an image gate failed")
    return record


def _manifest_fields(text: bytes, build: str) -> dict:
    fields = dict(line.split("=", 1) for line in text.decode().splitlines() if "=" in line)
    _require(fields.get("schema") == "crux-install/1" and fields.get("kind") == "zip"
             and fields.get("product") == PRODUCT and fields.get("target_build") == build
             and fields.get("serialno") == "any" and fields.get("data_policy") == "discard",
             "the ZIP payload manifest is not this build's identity-free clean-install package")
    return fields


def _verify_payload(build: str, payload: Path, record: dict) -> str:
    text = (payload / "crux-install.txt").read_bytes()
    fields = _manifest_fields(text, build)
    delivery = record["delivery"]
    for role, name in (("recovery", "recovery-cepheus.itb"), ("rom", "rom-cepheus.itb")):
        _require(fields.get(f"fit.{role}.entry") == name
                 and fields.get(f"fit.{role}.sha256") == delivery[name]["sha256"] == sha256(payload / name),
                 f"the ZIP payload {role} FIT is not the verified assembly FIT")
    record_path = payload / "payload.json"
    payload_record = json.loads(record_path.read_text())
    _require(payload_record.get("manifest_sha256") == hashlib.sha256(text).hexdigest()
             and payload_record.get("installer_sha256") == sha256(payload / "update-binary"),
             "payload.json does not describe this payload")
    return hashlib.sha256(text).hexdigest()
