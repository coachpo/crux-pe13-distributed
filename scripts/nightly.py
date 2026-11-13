#!/usr/bin/env python3
"""Pin changed sources for a new CI build and start its cloud build chain.

nightly.yml runs this once a night; manual dispatch uses --force. A separate
--force-cold switch disables matching incremental cache reuse. It does nothing
while a chain run is queued or running, or (unless forced) when no tracked fork
maintenance branch or snapshot Release moved and main changed no
build path since the last full freeze. Otherwise it takes the next CI build number
of the Helsinki day (YYYYMMDD50 to YYYYMMDD99; local B builds use 01 to 49), pins
the moved sources, regenerates the device marker patch and its patched tree on the
device head the way the freeze checks them, runs the source configuration tests,
commits and pushes main as github-actions[bot] and dispatches a full freeze, which
starts the rest of the chain.
"""
import argparse
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote
from zoneinfo import ZoneInfo

import cloud_release_snapshot
import cloud_source

ROOT = Path(__file__).resolve().parents[1]
DEVICE = "device/xiaomi/crux"
# Explicit maintenance branches; changing a repository's default branch must not
# silently move this Android 13 build to a different platform generation.
TRACKED = {
    DEVICE: "thirteen-plus",
    "vendor/xiaomi/crux": "thirteen-plus",
    "kernel/xiaomi/crux-pe-cepheus": "thirteen",
    "bootable/recovery": "crux-pe13-functional",
    "frameworks/base": "crux-pe13-baseline",
    "art": "crux-pe13-baseline",
    "hardware/qcom-caf/sm8150/display": "crux-pe13-baseline",
    "system/core": "crux-pe13-baseline",
    "packages/modules/adb": "crux-pe13-baseline",
    "vendor/aosp": "crux-pe13-baseline",
}
CHAIN = tuple(".github/workflows/" + name for name in (
    "freeze-standard.yml", "prepare-standard.yml", "build-standard.yml", "assemble-standard.yml", "release.yml"))
ACTIVE = ("queued", "in_progress", "waiting", "requested", "pending")
# Changes elsewhere (docs, tests, guides) do not start a build.
BUILD_PATHS = ("scripts", "config", "installer", "keys", ".github",
               "references/pe13-boot-contracts")
CI_NUMBERS = range(50, 100)
BUILD_RE = re.compile(r"(?<![0-9])(20[0-9]{8})(?![0-9])")
FREEZE_TITLE = re.compile(r"PE13 standard freeze full ([0-9]{10})")
MARKER_FILE = "crux-delivery-verification.txt"
MARKER_RULE = ("", "ifeq ($(TARGET_BUILD_VARIANT),userdebug)", "PRODUCT_COPY_FILES += \\",
               f"    device/xiaomi/crux/{MARKER_FILE}:$(TARGET_COPY_OUT_SYSTEM)/etc/{MARKER_FILE}", "endif")
BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")


class NightlyError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise NightlyError(message)


def api(endpoint, token=None):
    environment = dict(os.environ, **({"GH_TOKEN": token} if token else {}))
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, timeout=120, env=environment)
    require(result.returncode == 0, f"GitHub GET {endpoint}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def chain_in_flight(repository):
    for status in ACTIVE:
        for run in api(f"repos/{repository}/actions/runs?status={status}&per_page=100")["workflow_runs"]:
            if run.get("path") in CHAIN:
                return f"{run['path']} run {run['id']} is {status}"
    return None


def fork_head(project):
    repository = project["url"].removeprefix("https://github.com/")
    branch = TRACKED[project["path"]]
    return api(f"repos/{repository}/commits/{quote(branch, safe='')}")["sha"]


def snapshot_update(project, token):
    """Return the new lock fields when the snapshot repository records another commit."""
    repository = project["release_snapshot"]["repository"]
    meta = api(f"repos/{repository}", token)
    content = api(f"repos/{repository}/contents/snapshot.json?ref={quote(meta['default_branch'], safe='')}", token)
    record = json.loads(base64.b64decode(content["content"]))
    if record.get("commit") == project["revision"]:
        return None
    require(record.get("project") == project["path"], f"{repository} snapshot.json describes another project")
    release = api(f"repos/{repository}/releases/tags/snapshot-{record['commit']}", token)
    assets = [asset for asset in release.get("assets", []) if asset.get("name") == record.get("name")]
    require(len(assets) == 1 and release.get("draft") is False
            and assets[0].get("size") == record.get("size_bytes")
            and assets[0].get("digest") == "sha256:" + str(record.get("sha256")),
            f"{repository} Release snapshot-{record['commit']} differs from its snapshot.json")
    fields = {"revision": record["commit"], "expected_tree": record.get("git_tree"), "release_snapshot": {
        "repository": repository, "repository_id": meta["id"], "release_id": release["id"],
        "tag": release["tag_name"], "asset_id": assets[0]["id"], "asset_name": assets[0]["name"],
        "size_bytes": assets[0]["size"], "sha256": record["sha256"]}}
    cloud_release_snapshot.validate({**project, **fields})
    return fields


def full_freezes(repository):
    runs = api(f"repos/{repository}/actions/workflows/freeze-standard.yml/runs?branch=main&per_page=100")
    return [run for run in runs["workflow_runs"] if FREEZE_TITLE.fullmatch(run.get("display_title") or "")]


def build_paths_changed(baseline, root=ROOT):
    """Whether main changed a build path since the commit of the last full freeze."""
    if not baseline:
        return True
    result = subprocess.run(["git", "diff", "--name-only", baseline, "HEAD", "--", *BUILD_PATHS],
                            cwd=root, capture_output=True, text=True)
    return result.returncode != 0 or bool(result.stdout.strip())


def next_build(day, used):
    for number in CI_NUMBERS:
        candidate = f"{day}{number:02d}"
        if candidate not in used:
            return candidate
    raise NightlyError(f"every CI build number of {day} is used")


def used_builds(repository, freezes, root=ROOT):
    titles = [run.get("display_title") or "" for run in freezes]
    titles += [release.get("tag_name") or "" for release in api(f"repos/{repository}/releases?per_page=100")]
    titles += [path.name for path in (root / "config/source-patches").iterdir()]
    return {match for title in titles for match in BUILD_RE.findall(title)}


def marker_patch(makefile, build):
    """The device marker: install crux-delivery-verification.txt from aosp_crux.mk's end."""
    require(makefile.endswith("\n") and MARKER_FILE not in makefile,
            "aosp_crux.mk does not end with a newline or already installs the delivery marker")
    lines = makefile.splitlines()
    added = "".join("+" + line + "\n" for line in MARKER_RULE)
    return (f"diff --git a/aosp_crux.mk b/aosp_crux.mk\n--- a/aosp_crux.mk\n+++ b/aosp_crux.mk\n"
            f"@@ -{len(lines)} +{len(lines)},{len(MARKER_RULE) + 1} @@\n {lines[-1]}\n{added}"
            f"diff --git a/{MARKER_FILE} b/{MARKER_FILE}\nnew file mode 100644\n--- /dev/null\n"
            f"+++ b/{MARKER_FILE}\n@@ -0,0 +1 @@\n+crux-pe13-dev-{build}\n")


def device_marker(project, build, patches):
    """Write the marker patch for the device head; return its manifest entry and patched tree.

    The tree is computed as the freeze checks it: the same archive, git apply and tree id.
    """
    fetched = {key: project[key] for key in ("path", "url", "revision")}
    fetched["archive_url"] = cloud_source.resolve_archive_url(fetched)
    patch = patches / f"device-crux-dev-marker-{build}.patch"
    with tempfile.TemporaryDirectory(prefix="nightly-device-") as directory:
        tree = Path(directory) / "tree"
        tree.mkdir()
        cloud_source._extract_archive(fetched, tree, 300)
        patch.write_text(marker_patch((tree / "aosp_crux.mk").read_text(), build))
        entry = {"path": f"config/source-patches/{patch.name}",
                 "sha256": hashlib.sha256(patch.read_bytes()).hexdigest()}
        cloud_source._apply_project_patches({"path": project["path"], "patches": [{**entry, "path": str(patch)}]},
                                            tree)
        return entry, cloud_source._git_tree_id(tree)


def pin_sources(root, changes, build, marker=device_marker):
    """Apply source changes and the new device marker to the manifest and its overrides."""
    manifest_path = root / "config/cloud-source-manifest.json"
    overrides_path = root / "config/cloud-source-overrides.json"
    manifest = json.loads(manifest_path.read_text())
    overrides = json.loads(overrides_path.read_text())
    projects = {project["path"]: project for project in manifest["projects"]}
    device = projects[DEVICE]
    superseded = {patch["path"] for patch in device.get("patches", [])
                  if Path(patch["path"]).name.startswith("device-crux-dev-marker-")}
    for path, fields in changes.items():
        projects[path].update(fields)
    entry, tree = marker({**device}, build, root / "config/source-patches")
    projects[DEVICE].update({"patches": [entry], "expected_tree": tree})
    # The freeze reads the manifest; each override field mirrors its manifest value.
    for path, fields in overrides["projects"].items():
        for key in fields:
            fields[key] = projects[path][key]
    for relative in superseded - {entry["path"]}:
        (root / relative).unlink()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    overrides_path.write_text(json.dumps(overrides, indent=2) + "\n")
    cloud_source._validate_manifest(json.loads(manifest_path.read_text()))


def commit_and_push(root, build, notes):
    subprocess.run([sys.executable, "-m", "unittest", "tests.test_cloud_source_config"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A", "--", "config"], cwd=root, check=True)
    message = f"Pin sources for PE13 build {build}\n\n" + "\n".join(notes) + "\n"
    subprocess.run(["git", "-c", f"user.name={BOT[0]}", "-c", f"user.email={BOT[1]}", "commit", "--quiet",
                    "-m", message], cwd=root, check=True)
    subprocess.run(["git", "push", "--quiet", "origin", "HEAD:main"], cwd=root, check=True)


def run(repository, snapshot_token, today, dispatch=True, root=ROOT, force=False, force_cold=False):
    active = chain_in_flight(repository)
    if active:
        return {"result": "skipped", "reason": active}
    manifest = json.loads((root / "config/cloud-source-manifest.json").read_text())
    projects = {project["path"]: project for project in manifest["projects"]}
    changes, notes = {}, []
    for path in TRACKED:
        head = fork_head(projects[path])
        if head != projects[path]["revision"]:
            changes[path] = {"revision": head}
            notes.append(f"- {path}: {projects[path]['revision'][:12]} -> {head[:12]}")
    for path in cloud_release_snapshot.PROJECT_REPOSITORIES:
        fields = snapshot_update(projects[path], snapshot_token)
        if fields:
            changes[path] = fields
            notes.append(f"- {path}: snapshot {projects[path]['revision'][:12]} -> {fields['revision'][:12]}")
    freezes = full_freezes(repository)
    if not force and not changes and not build_paths_changed(freezes[0]["head_sha"] if freezes else None, root):
        return {"result": "no-change"}
    build = next_build(today, used_builds(repository, freezes, root))
    pin_sources(root, changes, build)
    notes = notes or ["- Manual full build requested." if force else
                      "- No tracked source moved; main changed a build path since the last full freeze."]
    notes.append(f"- {DEVICE}: delivery marker crux-pe13-dev-{build}.")
    reuse = next((str(run["id"]) for run in freezes if run.get("conclusion") == "success"), "")
    if dispatch:
        commit_and_push(root, build, notes)
        subprocess.run(["gh", "workflow", "run", "freeze-standard.yml", "--repo", repository, "--ref", "main",
                        "-f", "mode=full", "-f", f"build_number={build}", "-f", f"reuse_sources_from={reuse}",
                        "-f", f"force_cold={'true' if force_cold else 'false'}"],
                       check=True)
    return {"result": "started" if dispatch else "pinned", "build": build, "reuse_sources_from": reuse,
            "force_cold": force_cold,
            "changes": notes}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--force", action="store_true",
                        help="build even without source changes; an active chain still prevents duplicates")
    parser.add_argument("--force-cold", action="store_true",
                        help="disable matching incremental cache reuse for this build")
    parser.add_argument("--no-dispatch", action="store_true",
                        help="pin the sources in this checkout without committing, pushing or dispatching")
    args = parser.parse_args(argv)
    today = datetime.now(ZoneInfo("Europe/Helsinki")).strftime("%Y%m%d")
    try:
        result = run(os.environ["GITHUB_REPOSITORY"], os.environ.get(cloud_release_snapshot.TOKEN_ENV),
                     today, dispatch=not args.no_dispatch, force=args.force, force_cold=args.force_cold)
    except (KeyError, NightlyError, cloud_source.SourceError, cloud_release_snapshot.SnapshotError,
            OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"nightly: {error}\n")
    if args.github_output:
        with args.github_output.open("a") as output:
            print(f"result={result['result']}", file=output)
            print(f"build={result.get('build', '')}", file=output)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
