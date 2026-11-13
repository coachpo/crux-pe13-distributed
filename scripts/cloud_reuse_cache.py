#!/usr/bin/env python3
"""Find and verify a prior completed native shard with the same content basis."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import urllib.parse
import urllib.error

import cloud_layers
import cloud_reuse
import release_source


REPOSITORY = "coachpo/crux-pe13-distributed"
BUILD_WORKFLOW = ".github/workflows/build-standard.yml"


class CacheIntegrityError(ValueError):
    """A candidate claimed reusable content but failed its provenance checks."""


class _CandidateUnavailable(Exception):
    """An artifact disappeared after inventory and can no longer be used."""


def _run_inventory(repository: str, token: str) -> list[dict]:
    """Read every matching run page; do not limit the search to a guessed history window."""
    records, page, total = [], 1, None
    while total is None or len(records) < total:
        query = urllib.parse.urlencode({"branch": "main", "event": "workflow_dispatch",
                                       "per_page": 100, "page": page})
        url = f"{cloud_layers.API}/repos/{repository}/actions/runs?{query}"
        payload, _ = cloud_layers._request(url, token)
        try:
            response = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as error:
            raise CacheIntegrityError("GitHub Actions run inventory is malformed") from error
        if not isinstance(response, dict):
            raise CacheIntegrityError("GitHub Actions run inventory is malformed")
        items, reported = response.get("workflow_runs"), response.get("total_count")
        if not isinstance(items, list) or type(reported) is not int or reported < 0:
            raise CacheIntegrityError("GitHub Actions run inventory is malformed")
        if any(not isinstance(item, dict) for item in items):
            raise CacheIntegrityError("GitHub Actions run inventory contains an invalid record")
        records.extend(items)
        total = reported
        page += 1
        if not items and len(records) < total:
            raise CacheIntegrityError("GitHub Actions run inventory ended before total_count")
    ids = [str(record.get("id", "")) for record in records]
    if len(ids) != len(set(ids)):
        raise CacheIntegrityError("GitHub Actions run inventory contains duplicate run IDs")
    return records


def _eligible_run(record: dict) -> bool:
    return (record.get("status") == "completed" and record.get("conclusion") == "success"
            and record.get("event") == "workflow_dispatch" and record.get("head_branch") == "main"
            and record.get("path") == BUILD_WORKFLOW)


def _trusted_artifact(inventory: dict, name: str) -> dict | None:
    artifact = inventory.get(name)
    if artifact is None:
        return None
    if not isinstance(artifact, dict):
        raise CacheIntegrityError("Candidate Actions artifact metadata is malformed: " + name)
    if artifact.get("expired"):
        return None
    try:
        release_source._artifact(inventory, name)
    except release_source.SourceError as error:
        raise CacheIntegrityError("Candidate Actions artifact metadata is invalid: " + name) from error
    if type(artifact.get("size_in_bytes")) is not int or artifact["size_in_bytes"] < 0:
        raise CacheIntegrityError("Candidate Actions artifact has an invalid size: " + name)
    return artifact


def _download(artifact: dict, directory: Path, token: str) -> Path:
    directory.mkdir(parents=True, exist_ok=False)
    try:
        path, _ = release_source._download_zip(artifact, directory, token)
        return path
    except release_source.SourceError as error:
        cause = error
        while cause is not None:
            if isinstance(cause, urllib.error.HTTPError) and cause.code in (404, 410):
                raise _CandidateUnavailable from error
            cause = cause.__cause__
        message = str(error)
        if "Actions artifact download failed:" in message:
            if "(404)" in message or "(410)" in message:
                raise _CandidateUnavailable from error
            raise
        raise CacheIntegrityError("Candidate Actions artifact failed digest/size verification") from error


def _read_json_member(artifact: dict, directory: Path, token: str, filename: str) -> dict:
    archive = _download(artifact, directory / "download", token)
    try:
        extracted = release_source._read_members(archive, {filename}, directory / "content")
    except release_source.SourceError as error:
        raise CacheIntegrityError("Candidate artifact JSON member failed verification: " + filename) from error
    try:
        value = json.loads(extracted[filename].read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise CacheIntegrityError("Candidate artifact contains malformed " + filename) from error
    if not isinstance(value, dict):
        raise CacheIntegrityError("Candidate artifact member is not a JSON object: " + filename)
    return value


def _output_files(artifact: dict, directory: Path, token: str) -> tuple[dict, Path]:
    """Extract a shard artifact without following links or accepting duplicate paths."""
    archive_path = _download(artifact, directory / "download", token)
    extracted_root = directory / "files"
    extracted_root.mkdir(parents=True, exist_ok=False)
    seen: set[str] = set()
    try:
        import zipfile
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                name = member.filename
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise CacheIntegrityError("Candidate shard artifact has an unsafe member path")
                mode = member.external_attr >> 16
                kind = mode & 0o170000
                if member.is_dir() or kind == 0o120000 or (kind not in (0, 0o100000)):
                    raise CacheIntegrityError("Candidate shard artifact contains a non-regular member")
                if name in seen:
                    raise CacheIntegrityError("Candidate shard artifact has a duplicate member")
                seen.add(name)
                target = extracted_root.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as output:
                    while True:
                        block = source.read(8 * 1024 * 1024)
                        if not block:
                            break
                        output.write(block)
    except CacheIntegrityError:
        raise
    except Exception as error:
        raise CacheIntegrityError("Candidate shard artifact ZIP is invalid") from error

    receipts = list(extracted_root.rglob("receipt.json"))
    outputs = list(extracted_root.rglob("outputs.tar.zst"))
    if len(receipts) != 1 or len(outputs) != 1:
        raise CacheIntegrityError("Candidate shard artifact lacks unique receipt/output archive")
    try:
        receipt = json.loads(receipts[0].read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise CacheIntegrityError("Candidate shard receipt is malformed") from error
    if not isinstance(receipt, dict):
        raise CacheIntegrityError("Candidate shard receipt is not an object")
    return receipt, outputs[0]


def _valid_summary(summary: dict, run_id: str, worker_commit: str) -> bool:
    fresh = summary.get("fresh_build")
    if (summary.get("schema_version") != 1 or summary.get("status") != "success"
            or str(summary.get("execution_id")) != run_id
            or summary.get("worker_commit") != worker_commit
            or not isinstance(fresh, dict) or str(fresh.get("execution_id")) != run_id
            or not re.fullmatch(r"[1-9][0-9]*", str(fresh.get("pack_run", "")))
            or not re.fullmatch(r"[1-9][0-9]*", str(fresh.get("shard_run", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(fresh.get("plan_sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(fresh.get("source_manifest_sha256", "")))):
        return False
    producers = summary.get("producers")
    return (isinstance(producers, list) and bool(producers)
            and summary.get("task_count") == len(producers))


def _matching_entries(summary: dict, key: str, basis: dict, execution_digest: str) -> list[str]:
    reuse, identities = summary.get("reuse"), summary.get("output_identity")
    if not isinstance(reuse, dict) or not isinstance(identities, dict):
        return []  # Pre-reuse or expired-format summary.
    matches = []
    for task_id, envelope in reuse.items():
        if not isinstance(envelope, dict) or envelope.get("key") != key:
            continue
        if (envelope.get("schema_version") != 1 or envelope.get("mode") not in {"built", "reused"}
                or envelope.get("basis") != basis
                or envelope["basis"].get("execution_digest") != execution_digest
                or cloud_reuse.content_key(envelope["basis"]) != key
                or task_id not in identities):
            raise CacheIntegrityError("Matching reuse summary envelope is inconsistent")
        matches.append(task_id)
    return sorted(matches)


def find(context: dict, task_id: str, basis: dict, staging: Path, token: str):
    """Return a verified prior shard carrier, or ``None`` if no retained match exists.

    Network/API failures and malformed matching records propagate as errors; they are
    never silently converted into cache misses.
    """
    if not isinstance(context, dict) or context.get("repository") != REPOSITORY:
        raise ValueError("Native shard cache is restricted to the maintained repository")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("Invalid current task ID")
    if not isinstance(basis, dict) or not token:
        raise ValueError("A content basis and GitHub token are required")
    policy = context.get("reuse_policy")
    execution_digest = policy.get("execution_digest") if isinstance(policy, dict) else None
    if (not isinstance(execution_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", execution_digest)
            or basis.get("execution_digest") != execution_digest):
        raise ValueError("Current content basis does not bind the reuse execution policy")
    key = cloud_reuse.content_key(basis)
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)

    for run in _run_inventory(REPOSITORY, token):
        if not _eligible_run(run):
            continue
        run_id = str(run.get("id", ""))
        worker_commit = run.get("head_sha")
        if (not re.fullmatch(r"[1-9][0-9]*", run_id) or not isinstance(worker_commit, str)
                or not re.fullmatch(r"[0-9a-f]{40}", worker_commit)):
            continue
        if run_id == str(context.get("execution_id")):
            continue

        inventory = cloud_layers.artifact_inventory(run_id, REPOSITORY, token)
        summary_artifact = _trusted_artifact(inventory, "cloud-fresh-build-summary")
        if summary_artifact is None:
            continue
        run_dir = staging / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        try:
            summary = _read_json_member(summary_artifact, run_dir / "summary", token, "build-summary.json")
        except _CandidateUnavailable:
            continue
        if not _valid_summary(summary, run_id, worker_commit):
            raise CacheIntegrityError("Candidate build summary is not bound to its successful run")
        matching = _matching_entries(summary, key, basis, execution_digest)
        for candidate_task in matching:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", candidate_task):
                raise CacheIntegrityError("Matching summary has an invalid task ID")
            producer_rows = [item for item in summary["producers"]
                             if isinstance(item, dict) and item.get("id") == candidate_task]
            if len(producer_rows) != 1:
                raise CacheIntegrityError("Matching summary has no unique task producer binding")
            producer = producer_rows[0]
            if (str(producer.get("run_id")) != run_id
                    or producer.get("artifact") != "shard-" + candidate_task
                    or producer.get("expected_worker_commit") != worker_commit):
                raise CacheIntegrityError("Matching summary producer is not bound to its run")

            cache_context_artifact = _trusted_artifact(inventory, "cloud-build-context")
            if cache_context_artifact is None:
                continue
            try:
                cache_context = _read_json_member(cache_context_artifact, run_dir / "context", token,
                                                  "context.json")
            except _CandidateUnavailable:
                continue
            cached_policy = cache_context.get("reuse_policy")
            if (cache_context.get("execution_id") != run_id
                    or cache_context.get("worker_commit") != worker_commit
                    or cache_context.get("repository") != REPOSITORY
                    or not isinstance(cached_policy, dict)
                    or cached_policy.get("execution_digest") != execution_digest
                    or candidate_task not in cache_context.get("tasks", {})):
                raise CacheIntegrityError("Candidate build context is not bound to its run/content policy")
            task = cache_context["tasks"][candidate_task]
            try:
                expected_fresh = {key_name: cache_context[key_name] for key_name in
                                  ("execution_id", "pack_run", "shard_run", "plan_sha256",
                                   "source_manifest_sha256")}
            except KeyError as error:
                raise CacheIntegrityError("Candidate build context lacks source bindings") from error
            if (producer.get("expected_manifest_sha256") != task.get("manifest_sha256")
                    or summary.get("fresh_build") != expected_fresh):
                raise CacheIntegrityError("Candidate summary and build context identities differ")
            try:
                cloud_reuse.validate_summary(cache_context, summary)
            except ValueError as error:
                raise CacheIntegrityError('Candidate summary has invalid dependency content lineage') from error

            shard_artifact_name = "shard-" + candidate_task
            shard_artifact = _trusted_artifact(inventory, shard_artifact_name)
            if shard_artifact is None:
                continue
            try:
                receipt, output_archive = _output_files(
                    shard_artifact, run_dir / ("shard-" + candidate_task), token)
            except _CandidateUnavailable:
                continue
            envelope = summary["reuse"][candidate_task]
            if receipt.get("reuse") != envelope or receipt.get("outputs") != summary["output_identity"][candidate_task]:
                raise CacheIntegrityError("Candidate shard receipt differs from its successful summary")
            if (receipt.get("status") != "success" or receipt.get("id") != candidate_task
                    or str(receipt.get("run_id")) != run_id or receipt.get("worker_commit") != worker_commit
                    or receipt.get("manifest_sha256") != task.get("manifest_sha256")):
                raise CacheIntegrityError("Candidate shard receipt is not bound to its compile run")
            try:
                cloud_reuse.validate_receipt(cache_context, receipt, candidate_task)
                verified_archive = Path(cloud_reuse.verify_native_output(receipt, output_archive.parent))
            except Exception as error:
                raise CacheIntegrityError("Candidate native output failed reuse verification") from error
            if verified_archive != output_archive:
                raise CacheIntegrityError("Native output verifier returned a different archive path")

            origin = {"repository": REPOSITORY, "run_id": run_id, "worker_commit": worker_commit,
                      "task_id": candidate_task, "artifact_id": shard_artifact["id"],
                      "artifact_digest": shard_artifact["digest"],
                      "receipt_sha256": cloud_reuse.digest(receipt)}
            return origin, receipt, output_archive
    return None
