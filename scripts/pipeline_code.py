#!/usr/bin/env python3
"""Reject stage inputs produced by another version of the pipeline code.

This is an execution-code check, not source or artifact admission. Those checks
remain in each stage. Source-layer reuse is intentionally outside this check:
its byte contracts can remain valid across pipeline revisions.
"""
import argparse
import json
import os
import re
import subprocess


class PipelineCodeError(RuntimeError):
    pass


def api(endpoint):
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def verify(repository, execution_sha, runs):
    if not re.fullmatch(r"[0-9a-f]{40}", execution_sha or ""):
        raise PipelineCodeError("A full execution commit SHA is required")
    # Optional preparation run inputs are empty when the current run creates
    # those artifacts. At least the required preceding stage must be supplied.
    selected = list(dict.fromkeys(run for run in runs if run))
    if not selected or any(not re.fullmatch(r"[0-9]+", run) for run in selected):
        raise PipelineCodeError("At least one numeric upstream run ID is required")
    for run in selected:
        record = api(f"repos/{repository}/actions/runs/{run}")
        if record.get("head_sha") != execution_sha:
            raise PipelineCodeError(
                f"Upstream run {run} used {record.get('head_sha')}, not execution {execution_sha}. "
                "Start a full freeze on this pipeline revision; do not mix stage code versions.")
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, help="upstream run; optional inputs may be empty")
    args = parser.parse_args(argv)
    try:
        runs = verify(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"], args.run)
    except (KeyError, PipelineCodeError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        parser.exit(1, f"pipeline code: {error}\n")
    print(json.dumps({"execution_sha": os.environ["GITHUB_SHA"], "upstream_runs": runs}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
