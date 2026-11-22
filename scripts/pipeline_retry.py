#!/usr/bin/env python3
"""Dispatch one same-pin cold retry after typed content-validation evidence."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


class RetryError(ValueError):
    pass


def _read(path):
    return json.loads(Path(path).read_text())


def _identity_matches(evidence, context, origin_run, stage):
    return (evidence.get("schema_version") == 1
            and evidence.get("status") == "failed"
            and evidence.get("failure_class") == "content-validation"
            and evidence.get("stage") == stage
            and str(evidence.get("execution_id")) == str(context.get("execution_id")) == str(origin_run)
            and evidence.get("worker_commit") == context.get("worker_commit")
            and evidence.get("build_number") == context.get("build_number"))


def compile_evidence(receipts_dir, context, origin_run):
    paths = sorted(Path(receipts_dir).glob("**/receipt.json"))
    if not paths:
        raise RetryError("No compilation task receipts are available")
    import cloud_reuse
    records, failures, reused = {}, [], set()
    for path in paths:
        receipt = _read(path)
        task_id = receipt.get("id")
        if (not isinstance(task_id, str) or task_id not in context.get("tasks", {})
                or path.parent.name != "fresh-receipt-" + task_id or task_id in records):
            raise RetryError("Compilation receipt does not match a unique admitted task")
        records[task_id] = receipt
        if receipt.get("status") == "success":
            try:
                cloud_reuse.validate_receipt(context, receipt, task_id)
            except (KeyError, TypeError, ValueError) as error:
                raise RetryError("Compilation receipt has invalid reuse provenance: " + task_id) from error
            continue
        if not _identity_matches(receipt, context, origin_run, "compile"):
            raise RetryError("Compilation failed without an eligible typed content-validation receipt")
        failed_reuse = receipt.get("reused_tasks")
        if (not isinstance(failed_reuse, list)
                or any(not isinstance(item, str) or item not in context.get("tasks", {})
                       for item in failed_reuse)):
            raise RetryError("Typed compile failure does not record its reused dependencies")
        failures.append(receipt)
        reused.update(failed_reuse)
    if not failures or not reused:
        raise RetryError("No typed content failure after actual reused inputs was recorded")
    for task_id in reused:
        dependency_receipt = records.get(task_id)
        if (not isinstance(dependency_receipt, dict) or dependency_receipt.get("status") != "success"
                or dependency_receipt.get("reuse", {}).get("mode") != "reused"):
            raise RetryError("Reused dependency has no successful same-run adoption receipt: " + task_id)
    return {"schema_version": 1, "status": "failed", "failure_class": "content-validation",
            "stage": "compile", "execution_id": str(origin_run),
            "worker_commit": context["worker_commit"], "build_number": context["build_number"],
            "reused_tasks": sorted(reused),
            "error": "; ".join(str(row.get("error", "")) for row in failures)}


def validate_retry(context, evidence, stage, origin_run, repository, ref, execution_sha):
    if not isinstance(context, dict) or context.get("schema_version") != 1:
        raise RetryError("No admitted versioned build context is available")
    policy = context.get("reuse_policy")
    if (not isinstance(policy, dict) or policy.get("force_cold") is not False
            or policy.get("cold_retry_of") not in ("", None)):
        raise RetryError("This execution was already cold or has already used its retry")
    if ref != "refs/heads/main" or not re.fullmatch(r"[a-f0-9]{40}", execution_sha or ""):
        raise RetryError("Cold retries are restricted to the exact main execution commit")
    if context.get("worker_commit") != execution_sha or context.get("repository") != repository:
        raise RetryError("Build context does not match this main-branch execution")
    if not re.fullmatch(r"[0-9]+", str(origin_run or "")):
        raise RetryError("A numeric original compile run is required")
    if str(context.get("execution_id")) != str(origin_run):
        raise RetryError("Retry evidence belongs to another compile execution")
    if not _identity_matches(evidence, context, origin_run, stage):
        raise RetryError("Failure evidence is not a matching typed content-validation failure")
    reused = evidence.get("reused_tasks")
    tasks = context.get("tasks")
    if (not isinstance(tasks, dict) or not isinstance(reused, list) or not reused
            or any(not isinstance(task, str) for task in reused)
            or len(reused) != len(set(reused)) or any(task not in tasks for task in reused)):
        raise RetryError("Failure evidence does not prove actual reuse of admitted tasks")
    if (not re.fullmatch(r"[0-9]+", str(context.get("pack_run", "")))
            or not re.fullmatch(r"[0-9]+", str(context.get("shard_run", "")))
            or not isinstance(context.get("build_number"), str)
            or not context["build_number"].isdecimal()):
        raise RetryError("Admitted retry inputs are incomplete")
    return ["gh", "workflow", "run", "build-standard.yml", "--repo", repository,
            "--ref", "main", "-f", "pack_run=" + context["pack_run"],
            "-f", "shard_run=" + context["shard_run"],
            "-f", "build_number=" + context["build_number"], "-f", "force_cold=true",
            "-f", "cold_retry_of=" + str(origin_run)]


def _run(argv, **kwargs):
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kwargs)


def retry(context, evidence, stage, origin_run, repository, ref, execution_sha,
          run=_run, runs=None, main_sha=None):
    command = validate_retry(context, evidence, stage, origin_run, repository, ref, execution_sha)
    if main_sha is None:
        result = run(["gh", "api", "repos/" + repository + "/commits/main", "--jq", ".sha"])
        main_sha = result.stdout.strip()
    if main_sha != execution_sha:
        raise RetryError("main has moved since the admitted execution; refusing a code-mismatched retry")
    if runs is None:
        result = run(["gh", "run", "list", "--repo", repository, "--workflow", "build-standard.yml",
                      "--branch", "main", "--limit", "1000", "--json", "databaseId,displayTitle,headSha"])
        runs = json.loads(result.stdout)
    marker = "cold retry of " + str(origin_run)
    if any(marker in row.get("displayTitle", "") and row.get("headSha") == execution_sha for row in runs):
        return {"result": "already-dispatched", "origin_run": str(origin_run)}
    run(command)
    return {"result": "dispatched", "origin_run": str(origin_run), "command": command}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("compile", "assemble", "preflight"), required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--receipts", help="Downloaded fresh-receipt-* artifacts for compile failure")
    parser.add_argument("--failure-evidence", help="Typed validation-failure.json from assembly output")
    parser.add_argument("--origin-run", required=True, help="Compile run ID whose exact pins will be retried")
    args = parser.parse_args(argv)
    try:
        context = _read(args.context)
        if args.stage == "compile":
            if not args.receipts:
                raise RetryError("Compile retry requires downloaded per-task receipts")
            evidence = compile_evidence(args.receipts, context, args.origin_run)
        else:
            if not args.failure_evidence:
                raise RetryError("Assembly retry requires typed validation failure evidence")
            evidence = _read(args.failure_evidence)
        result = retry(context, evidence, args.stage, args.origin_run,
                       os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_REF"], os.environ["GITHUB_SHA"])
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        parser.exit(1, "pipeline retry: " + str(error) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
