#!/usr/bin/env python3
"""Check that a local PE13 source tree holds the code the Actions build pins.

    check_local_source.py --tree PE13_ROOT [--manifest config/cloud-source-manifest.json]

Run it in the build VM before a local (B) build. For every project of the
cloud source manifest it reads the checkout's HEAD, following symlinked
project directories such as the kernel, and its tracked working-tree changes.
Every revision must equal the manifest's and no tracked file may be modified;
untracked files (nested checkouts, for example) are not code of the project.
Prints a JSON report and exits 1 on any difference.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

MANIFEST = Path(__file__).resolve().parents[1] / "config/cloud-source-manifest.json"


def git(checkout, *argv):
    done = subprocess.run(["git", "-C", str(checkout), *argv], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def inspect(tree, project):
    checkout = (tree / project["path"]).resolve()
    if not (checkout / ".git").exists():
        return {"path": project["path"], "problem": "missing checkout"}
    head = git(checkout, "rev-parse", "HEAD")
    changes = git(checkout, "status", "--porcelain", "--untracked-files=no")
    if head != project["revision"]:
        return {"path": project["path"], "problem": "revision differs", "expected": project["revision"],
                "actual": head}
    if changes is None or changes:
        return {"path": project["path"], "problem": "tracked changes", "changes": (changes or "").splitlines()[:20]}
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args(argv)
    projects = json.loads(args.manifest.read_text())["projects"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        problems = [row for row in pool.map(lambda p: inspect(args.tree, p), projects) if row]
    report = {"manifest": str(args.manifest.resolve()), "tree": str(args.tree.resolve()),
              "projects": len(projects), "problems": problems}
    print(json.dumps(report, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
