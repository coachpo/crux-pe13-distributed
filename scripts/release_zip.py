#!/usr/bin/env python3
"""Preflight, stage, or publish the signed TWRP ZIP of one verified standard-runner build.

The release carries crux-pe13-<build>.zip, its .sha256, README.txt and the pinned
RAM-only install TWRP (config/install-twrp.json) with its .sha256. The ZIP is
made here from the assembly's verified payload and the signature of its
manifest (made by release.yml's install-signing job, or offline), checked
against keys/crux-b-install-2026-11-07.pub.pem. Without --tag, the release
takes this assembly's existing tag or the next free version of its build;
the release operation stages the draft and publishes it in one run.
"""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request
from urllib.parse import urlencode

import crux_zip
from release_source import REPOSITORY, SourceError, assembly_build, parse_tag, sha256, verify_source


RELEASE_TARGET = "main"
ROOT = Path(__file__).resolve().parents[1]
PUBLIC_KEY = ROOT / "keys/crux-b-install-2026-11-07.pub.pem"
TWRP = ROOT / "config/install-twrp.json"


class ReleaseError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


class Releases:
    def api(self, endpoint, *, pages=False, method="GET", payload=None):
        argv = ["gh", "api", "--hostname", "github.com", "--method", method,
                f"repos/{REPOSITORY}/{endpoint}"]
        if pages:
            argv += ["--paginate", "--slurp"]
        if payload is not None:
            argv += ["--input", "-"]
        result = subprocess.run(argv, input=json.dumps(payload) if payload is not None else None,
                                capture_output=True, text=True, timeout=120)
        require(result.returncode == 0, f"GitHub {method} {endpoint}: {result.stderr.strip()}")
        value = json.loads(result.stdout)
        return [item for page in value for item in page] if pages else value

    def upload(self, release_id, path):
        # Stream the file with Content-Length. Do not retry a possibly committed POST.
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        require(token, "GH_TOKEN is required for release asset upload")
        query = urlencode({"name": path.name})
        url = f"https://uploads.github.com/repos/{REPOSITORY}/releases/{release_id}/assets?{query}"
        result = subprocess.run(
            ["curl", "--disable", "--silent", "--show-error", "--fail-with-body",
             "--connect-timeout", "30", "--request", "POST", "--upload-file", str(path),
             "--header", "@-", "--header", "Content-Type: application/octet-stream",
             "--header", "Accept: application/vnd.github+json", url],
            input=f"Authorization: Bearer {token}\n", capture_output=True, text=True, timeout=1800)
        require(result.returncode == 0,
                f"upload failed for {path.name}: {result.stderr.strip()} {result.stdout[-1000:]}")
        return json.loads(result.stdout)


def fetch_twrp(directory, pin=None):
    """Download the pinned install TWRP from its tool release and check its bytes."""
    pin = pin or json.loads(TWRP.read_text())
    require(isinstance(pin.get("asset_id"), int),
            f"the install TWRP release {pin.get('release_tag')} has no pinned asset yet")
    path = Path(directory) / pin["name"]
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/octet-stream", "User-Agent": "crux-release-zip"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{pin['asset_id']}", headers=headers)
    with urllib.request.urlopen(request, timeout=300) as response, path.open("xb") as output:
        while True:
            block = response.read(1 << 22)
            if not block:
                break
            output.write(block)
    require(path.stat().st_size == pin["bytes"] and sha256(path) == pin["sha256"],
            "the downloaded install TWRP differs from config/install-twrp.json")
    return path


def stage_assets(source, signature, directory, twrp_fetch=fetch_twrp):
    """Make the ZIP and gather every release asset; return their names, sizes and digests."""
    directory = Path(directory)
    with contextlib.redirect_stdout(io.StringIO()):
        status = crux_zip.main(["finalize", "--payload", str(source.payload), "--signature", signature,
                                "--public-key", str(PUBLIC_KEY), "--out", str(directory / "zip")])
    require(status == 0, "the ZIP could not be made from the verified payload and signature")
    staged = directory / "assets"
    staged.mkdir()
    for name in (f"crux-pe13-{source.build}.zip", f"crux-pe13-{source.build}.zip.sha256", "README.txt"):
        (directory / "zip" / name).rename(staged / name)
    twrp = twrp_fetch(staged)
    (staged / f"{twrp.name}.sha256").write_text(f"{sha256(twrp)}  {twrp.name}\n")
    return staged, {path.name: {"size": path.stat().st_size, "digest": "sha256:" + sha256(path)}
                    for path in sorted(staged.iterdir())}


def provenance(source):
    return (f"<!-- crux-zip-source assembly={source.assembly_run} compile={source.compile_run} "
            f"assembly-sha={source.assembly_commit} artifact={source.artifact_id} "
            f"digest={source.artifact_digest} manifest={source.manifest_sha256} -->")


def release_body(source):
    return (
        "Crux PE13 flashable ZIP (aosp_crux-userdebug / test-keys), built by GitHub Actions.\n\n"
        f"[Verified assembly](https://github.com/{REPOSITORY}/actions/runs/{source.assembly_run}) · "
        f"[Cold compile](https://github.com/{REPOSITORY}/actions/runs/{source.compile_run}).\n\n"
        "Install it with the RAM-only install TWRP attached here, following README.txt. "
        "It is a clean install: PE user data and metadata are erased. Host validation only; "
        "device acceptance is recorded separately.\n\n" + provenance(source))


def next_tag(api, build, assembly_run):
    """Return the tag of this assembly's release, or the next free version of its build."""
    marker = f"<!-- crux-zip-source assembly={assembly_run} "
    versions = set()
    for release in api.api("releases?per_page=100", pages=True):
        parsed = parse_tag(release.get("tag_name", ""))
        if parsed is None or parsed["build"] != build:
            continue
        if any(line.startswith(marker) for line in (release.get("body") or "").splitlines()):
            return release["tag_name"]
        versions.add(parsed["version"])
    for ref in api.api(f"git/matching-refs/tags/crux-pe13-{build}-v"):
        parsed = parse_tag(ref.get("ref", "").removeprefix("refs/tags/"))
        if parsed is not None and parsed["build"] == build:
            versions.add(parsed["version"])
    return f"crux-pe13-{build}-v{max(versions, default=0) + 1}"


def find_release(api, source):
    releases = api.api("releases?per_page=100", pages=True)
    matches = [release for release in releases if release.get("tag_name") == source.tag]
    require(len(matches) <= 1, f"multiple releases use {source.tag}")
    require(not any(release.get("tag_name") != source.tag
                    and parse_tag(release.get("tag_name", "")) == parse_tag(source.tag)
                    for release in releases), f"another tag uses the same version as {source.tag}")
    return matches[0] if matches else None


def verify_release(api, source, assets, release):
    """Return missing names; no differing or unexpected asset can be reused."""
    tag_ref = f"refs/tags/{source.tag}"
    refs = api.api(f"git/matching-refs/tags/{source.tag}")
    ref = next((ref["object"] for ref in refs if ref["ref"] == tag_ref), None)
    if release is None:
        require(ref is None, f"Git tag exists without a matching release: {source.tag}")
        return set(assets)
    require(release.get("tag_name") == source.tag and release.get("prerelease") is True
            and isinstance(release.get("draft"), bool)
            and release.get("target_commitish") == RELEASE_TARGET
            and provenance(source) in (release.get("body") or "").splitlines(),
            f"release conflict for {source.tag}: state or source identity differs")
    if release["draft"]:
        require(ref is None, f"Git tag already exists for draft: {source.tag}")
    else:
        require(release.get("published_at") and ref is not None and ref.get("type") == "commit"
                and re.fullmatch(r"[0-9a-f]{40}", ref.get("sha", "")),
                f"published release has no commit tag: {source.tag}")
    names = set()
    for asset in api.api(f"releases/{release['id']}/assets?per_page=100", pages=True):
        name = asset.get("name")
        require(name in assets and name not in names, f"unexpected or duplicate release asset: {name}")
        verify_asset(name, asset, assets[name])
        names.add(name)
    missing = set(assets) - names
    require(release["draft"] or not missing,
            f"published release is incomplete, not modified: {', '.join(sorted(missing))}")
    return missing


def verify_asset(name, asset, expected):
    require(asset.get("name") == name and asset.get("state") == "uploaded"
            and all(asset.get(key) == value for key, value in expected.items()),
            f"asset conflict for {name}: upload state, size or SHA-256 differs")


def deliver(api, source, staged, assets, operation):
    require(operation in {"preflight", "draft", "publish"}, "unknown release operation")
    release = find_release(api, source)
    missing = verify_release(api, source, assets, release)
    if operation == "preflight":
        state = "available" if release is None else ("draft" if release["draft"] else "published")
        return {"state": state, "missing": sorted(missing), "changed": False}
    if release is not None and not release["draft"]:
        return {"state": "published", "release_id": release["id"], "changed": False}

    if operation == "publish":
        require(release is not None and not missing,
                "publish requires an existing, complete, verified draft; run draft first")
        release_id = release["id"]
        try:
            published = api.api(f"releases/{release_id}", method="PATCH",
                                payload={"draft": False, "prerelease": True, "make_latest": "false"})
            require(published.get("draft") is False and published.get("prerelease") is True
                    and published.get("tag_name") == source.tag and published.get("published_at"),
                    "GitHub did not confirm the expected prerelease")
        except (ReleaseError, OSError, ValueError, subprocess.SubprocessError) as error:
            raise ReleaseError(
                f"publish not confirmed for {source.tag}; inspect its live release state "
                f"before retrying: {error}") from error
        return {"state": "published", "release_id": release_id, "changed": True}

    created = release is None
    if created:
        # The tag names the release; the body binds the actual source runs.
        release = api.api("releases", method="POST",
                          payload={"tag_name": source.tag, "target_commitish": RELEASE_TARGET,
                                   "name": f"Crux PE13 {source.tag}", "body": release_body(source),
                                   "draft": True, "prerelease": True, "make_latest": "false"})
        require(release.get("draft") is True, "GitHub did not create a draft")
        missing = verify_release(api, source, assets, release)
    release_id = release["id"]
    for name in sorted(missing):
        print(f"Uploading draft asset {name}", file=sys.stderr, flush=True)
        asset = api.upload(release_id, Path(staged) / name)
        verify_asset(name, asset, assets[name])
    release = api.api(f"releases/{release_id}")
    require(release.get("draft") is True and not verify_release(api, source, assets, release),
            "draft is no longer private or complete")
    return {"state": "draft", "release_id": release_id, "changed": created or bool(missing)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--assembly-run", required=True)
    parser.add_argument("--tag", default="",
                        help="crux-pe13-<build>-v<n>; empty selects this assembly's release or the next free version")
    parser.add_argument("--operation", choices=["preflight", "draft", "publish", "release"], default="preflight")
    parser.add_argument("--signature", required=True,
                        help="P-256 r||s over the payload's crux-install.txt, 128 hex digits")
    args = parser.parse_args()
    if not re.fullmatch(r"[1-9][0-9]*", args.assembly_run):
        parser.error("--assembly-run must be a positive Actions run ID")
    if args.tag and parse_tag(args.tag) is None:
        parser.error("--tag must be crux-pe13-<10-digit build>-v<positive version> (no leading zero)")
    if not re.fullmatch(r"[0-9a-f]{128}", args.signature):
        parser.error("--signature must be 128 lowercase hex digits")
    if args.operation != "preflight" and not (
            os.environ.get("GITHUB_WORKFLOW_REF")
            == f"{REPOSITORY}/.github/workflows/release.yml@refs/heads/main"
            and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"):
        parser.error("draft, publish and release must be dispatched through release.yml on main")
    try:
        api = Releases()
        tag = args.tag or next_tag(api, assembly_build(args.assembly_run), args.assembly_run)
        with tempfile.TemporaryDirectory(prefix="crux-release-") as work:
            work = Path(work)
            (work / "source").mkdir()
            source = verify_source(args.assembly_run, tag, work / "source")
            staged, assets = stage_assets(source, args.signature, work)
            print(json.dumps({"tag": source.tag, "assembly_run": source.assembly_run,
                              "compile_run": source.compile_run, "artifact_id": source.artifact_id,
                              "artifact_digest": source.artifact_digest,
                              "manifest_sha256": source.manifest_sha256, "assets": assets}, indent=2), flush=True)
            # One signature is staged once, so the draft and its publication carry identical ZIP bytes.
            for operation in ("draft", "publish") if args.operation == "release" else (args.operation,):
                print(json.dumps({"tag": source.tag, "operation": operation,
                                  **deliver(api, source, staged, assets, operation)}, indent=2), flush=True)
    except (ReleaseError, SourceError, OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"release-zip: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
