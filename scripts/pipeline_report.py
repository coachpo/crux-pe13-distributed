#!/usr/bin/env python3
"""Report a failed stage of the cloud build chain in an issue assigned to the owner.

GitHub notifies only the person who triggered a run. The chain's later runs are
dispatched by github-actions[bot], so their failures reach the owner through one
open issue per build number, which each later failure of that build comments on.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote

BOT = "github-actions[bot]"
BUILD = re.compile(r"(?<![0-9])([0-9]{10})(?![0-9])")
RUN = re.compile(r"(?<![0-9])([0-9]{11,})(?![0-9])")


def api(endpoint, *, method="GET", payload=None):
    argv = ["gh", "api", "--method", method, endpoint]
    if payload is not None:
        argv += ["--input", "-"]
    result = subprocess.run(argv, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"GitHub {method} {endpoint}: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def build_of(repository, title, hops=3):
    """Find the build number in a run title, following the upstream run IDs it names."""
    for _ in range(hops):
        match = BUILD.search(title)
        if match:
            return match.group(1)
        upstream = RUN.search(title)
        if not upstream:
            return None
        title = api(f"repos/{repository}/actions/runs/{upstream.group(1)}").get("display_title", "")
    return None


def report(repository, run_id, stage, build=None, detail=""):
    run = api(f"repos/{repository}/actions/runs/{run_id}")
    build = build or build_of(repository, run.get("display_title", ""))
    title = f"PE13 cloud build {build} failed" if build else f"PE13 {stage} failed"
    line = (f"- **{stage}** failed: [{run.get('display_title')}]({run.get('html_url')}), "
            f"attempt {run.get('run_attempt')}, commit `{str(run.get('head_sha'))[:12]}`")
    if detail:
        line += f". {detail}"
    issues = api(f"repos/{repository}/issues?state=open&creator={quote(BOT)}&per_page=100")
    existing = next((issue for issue in issues if issue.get("title") == title and "pull_request" not in issue), None)
    if existing:
        api(f"repos/{repository}/issues/{existing['number']}/comments", method="POST", payload={"body": line})
        return existing["html_url"]
    created = api(f"repos/{repository}/issues", method="POST", payload={
        "title": title, "assignees": [repository.split("/")[0]],
        "body": ("The automatic cloud build chain stopped. Fix the cause, then re-run the failed jobs "
                 "or dispatch the stage again from the fixed commit; close this issue once the build "
                 "is released.\n\n" + line)})
    return created["html_url"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", required=True, help="chain stage that failed, for example compile")
    parser.add_argument("--build", default="", help="build number when the run title does not name it")
    parser.add_argument("--detail", default="")
    args = parser.parse_args(argv)
    try:
        url = report(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"], args.stage,
                     args.build or None, args.detail)
    except (KeyError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"pipeline-report: {error}\n")
    print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
