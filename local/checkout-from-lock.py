#!/usr/bin/env python3
"""Create the Git checkout of every locked project that a local (B) build reads.

    checkout-from-lock.py [--tree PE13_ROOT] [--kernel KERNEL_CHECKOUT]
                          [--snapshot PATH=BUNDLE_OR_REPO]... [--jobs N] [--only PATH]...

Run it in the build VM as the build user. Defaults are the locations
build-pe13-local.sh reads: ~/crux-pe13-offline-2026-09-25/pe13 and
~/crux-kernel-cepheus. For every project of config/cloud-source-manifest.json it
makes a shallow Git checkout with HEAD at the locked revision (`git fetch --depth 1
URL REVISION`), checks the locked Git tree where the lock names one for the commit
itself (a patched project locks its tree after the patches, which the B build applies
in its overlay), and then adds
the lock's layout links and copies. The kernel project is checked out on its own and
linked into the tree, as the B build expects. Nested projects are fetched after the
project that contains them.

The two private snapshot projects (vendor/gms, external/faceunlock) are not fetched
from their URLs: pass a Git bundle or repository holding the locked commit with
--snapshot (for example from the PixelExperience GitLab bundles on Archive.org).
Their commit and tree must equal the lock.

It can run again: a project already at its revision with no tracked change is kept.
It leaves a project with tracked changes, or a path that is a regular file or a
link, untouched; in an existing non-Git directory Git refuses to overwrite files it
does not track. Prints a JSON report and exits 1 if anything is missing or differs; then run
check_local_source.py --tree PE13_ROOT.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

MANIFEST = Path(__file__).resolve().parents[1] / "config/cloud-source-manifest.json"
KERNEL_PATH = "kernel/xiaomi/crux-pe-cepheus"
SNAPSHOT = "github-release-snapshot"
ATTEMPTS = 3


class CheckoutError(Exception):
    pass


def git(directory, *argv, check=True):
    done = subprocess.run(["git", "-C", str(directory), *argv], capture_output=True, text=True)
    if check and done.returncode:
        raise CheckoutError(f"git {argv[0]} failed in {directory}: {done.stderr.strip()[-400:]}")
    return done.stdout.strip() if done.returncode == 0 else None


def relative(path):
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise CheckoutError(f"unsafe lock path: {path}")
    return pure


def waves(projects):
    """Group projects so that each comes after every project whose path contains it."""
    paths = sorted(p["path"] for p in projects)
    depth = {}
    for path in paths:
        depth[path] = sum(1 for other in paths if other != path and path.startswith(other + "/"))
    groups = {}
    for project in projects:
        groups.setdefault(depth[project["path"]], []).append(project)
    return [groups[level] for level in sorted(groups)]


def fetch(destination, source, revision, shallow):
    for attempt in range(ATTEMPTS):
        argv = ["fetch", "--quiet", "--no-tags"] + (["--depth", "1", source, revision] if shallow
                                                    else [source, "+refs/*:refs/crux-source/*"])
        done = subprocess.run(["git", "-C", str(destination), *argv], capture_output=True, text=True)
        if done.returncode == 0:
            return
        error = done.stderr.strip()[-400:]
    raise CheckoutError(f"fetch of {revision} from {source} failed: {error}")


def checkout(project, destination, snapshot_source):
    revision = project["revision"]
    if (destination / ".git").exists():
        head = git(destination, "rev-parse", "HEAD", check=False)
        changes = git(destination, "status", "--porcelain", "--untracked-files=no", check=False)
        if changes:
            raise CheckoutError("tracked changes; left untouched")
        if head == revision:
            return "present"
    elif destination.is_symlink() or destination.is_file():
        raise CheckoutError("exists and is not a directory; left untouched")
    else:
        destination.mkdir(parents=True, exist_ok=True)
        git(destination, "init", "--quiet")
    if project.get("archive_type") == SNAPSHOT:
        if snapshot_source is None:
            raise CheckoutError("private snapshot: pass --snapshot with a bundle or repository holding "
                                f"commit {revision}")
        fetch(destination, snapshot_source, revision, shallow=False)
    else:
        fetch(destination, project["url"], revision, shallow=True)
    git(destination, "checkout", "--quiet", "--detach", revision)
    for ref in (git(destination, "for-each-ref", "--format=%(refname)", "refs/crux-source") or "").split():
        git(destination, "update-ref", "-d", ref)
    if git(destination, "rev-parse", "HEAD") != revision:
        raise CheckoutError("HEAD differs from the locked revision after checkout")
    return "fetched"


def check_tree(project, destination):
    # A project with patches locks the tree after them (the device marker patch), which the
    # B build applies in its overlay; its checkout stays at the locked commit.
    expected = project.get("expected_tree")
    if expected and not project.get("patches") and git(destination, "rev-parse", "HEAD^{tree}") != expected:
        raise CheckoutError(f"Git tree differs from the locked {expected}")


def link_kernel(tree, kernel):
    link = tree / KERNEL_PATH
    link.parent.mkdir(parents=True, exist_ok=True)
    target = os.path.relpath(kernel, link.parent)
    if link.is_symlink():
        if link.resolve() == kernel.resolve():
            return
        link.unlink()
    elif link.exists():
        raise CheckoutError(f"{KERNEL_PATH} exists and is not the kernel link; left untouched")
    link.symlink_to(target)


def apply_layout(tree, layout, selected, all_paths):
    problems = []
    for entry in layout:
        source, destination, kind = entry["source"], entry["destination"], entry["type"]
        owners = [path for path in all_paths if source == path or source.startswith(path + "/")]
        if not owners or max(owners, key=len) not in selected:
            continue
        source_path, target = tree / relative(source), tree / relative(destination)
        try:
            if target.exists() and not target.is_symlink() and kind == "link":
                raise CheckoutError("exists and is not a link; left untouched")
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind == "link":
                wanted = os.path.relpath(source_path, target.parent)
                if target.is_symlink():
                    if os.readlink(target) == wanted:
                        continue
                    target.unlink()
                target.symlink_to(wanted)  # may dangle, as in repo layouts
            elif kind == "copy":
                if source_path.is_symlink() or not source_path.is_file():
                    raise CheckoutError("copy source is not a regular file")
                if target.is_symlink():
                    target.unlink()
                shutil.copyfile(source_path, target)
                target.chmod(source_path.stat().st_mode & 0o7777)
            else:
                raise CheckoutError(f"unknown layout type {kind}")
        except (CheckoutError, OSError) as error:
            problems.append({"path": destination, "problem": f"layout {kind}: {error}"})
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--tree", type=Path, default=Path.home() / "crux-pe13-offline-2026-09-25/pe13")
    parser.add_argument("--kernel", type=Path, default=Path.home() / "crux-kernel-cepheus")
    parser.add_argument("--snapshot", action="append", default=[], metavar="PATH=SOURCE",
                        help="Git bundle or repository holding a snapshot project's locked commit")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--only", action="append", default=[], metavar="PATH",
                        help="limit to these project paths (for a retry or a trial)")
    args = parser.parse_args(argv)
    lock = json.loads(args.manifest.read_text())
    projects = lock["projects"]
    all_paths = [p["path"] for p in projects]
    for path in all_paths:
        relative(path)
    snapshot_paths = {p["path"] for p in projects if p.get("archive_type") == SNAPSHOT}
    sources = {}
    for item in args.snapshot:
        path, sep, source = item.partition("=")
        if not sep or path not in snapshot_paths or not Path(source).exists():
            parser.error(f"--snapshot needs one of {sorted(snapshot_paths)}=<existing bundle or repository>")
        sources[path] = str(Path(source).resolve())
    unknown = set(args.only) - set(all_paths)
    if unknown:
        parser.error(f"--only names paths outside the lock: {sorted(unknown)}")
    selected = [p for p in projects if not args.only or p["path"] in args.only]
    tree, kernel = args.tree.resolve(), args.kernel.resolve()
    tree.mkdir(parents=True, exist_ok=True)
    report = {"tree": str(tree), "kernel": str(kernel), "projects": len(selected),
              "present": 0, "fetched": 0, "problems": []}

    def one(project):
        path = project["path"]
        destination = kernel if path == KERNEL_PATH else tree / relative(path)
        try:
            state = checkout(project, destination, sources.get(path))
            check_tree(project, destination)
            if path == KERNEL_PATH:
                link_kernel(tree, kernel)
            return state, None
        except CheckoutError as error:
            return None, {"path": path, "problem": str(error)}

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for wave in waves(selected):
            for state, problem in pool.map(one, wave):
                if problem:
                    report["problems"].append(problem)
                else:
                    report[state] += 1
    report["problems"] += apply_layout(tree, lock.get("layout", []), {p["path"] for p in selected}, all_paths)
    print(json.dumps(report, indent=2))
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
