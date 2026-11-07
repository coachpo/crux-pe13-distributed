#!/usr/bin/env python3
"""Preflight, stage, or publish one verified standard-runner dev OTA delivery."""

import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlencode

from dev_ota import DevOtaError, REPOSITORY, parse_tag, require
import dev_ota_manifest
from dev_release_source import verify_source


RELEASE_TARGET = "main"


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


def provenance(source):
    return (f"<!-- dev-ota-source assembly={source.assembly_run} compile={source.compile_run} "
            f"assembly-sha={source.assembly_commit} artifact={source.artifact_id} "
            f"digest={source.artifact_digest} -->")


def phone_provenance(source):
    return f"<!-- dev-ota-phone-manifest sha256={source.phone_manifest} -->"


def release_body(source):
    body = (
        "Crux PE13 development delivery (aosp_crux-userdebug / test-keys).\n\n"
        f"[Verified assembly](https://github.com/{REPOSITORY}/actions/runs/{source.assembly_run}) · "
        f"[Cold compile](https://github.com/{REPOSITORY}/actions/runs/{source.compile_run}).\n\n"
        "Host validation only. This is not the public R6 OTA channel. "
        "Device installation, boot, and version-changing rollback have not been accepted.\n\n")
    if source.phone_manifest:
        body += ("Phone update: the signed `crux-ota-manifest.txt` lets the Crux updater on a "
                 "listed start build install this delivery with data kept.\n"
                 + phone_provenance(source) + "\n\n")
    return body + provenance(source)


def attach_phone(source, start_builds, serial, signature):
    """Add the phone manifest and its verified offline signature to the delivery."""
    text = dev_ota_manifest.from_delivery(source, start_builds=start_builds, serial=serial)
    require(dev_ota_manifest.verify(text, signature),
            "ota_signature does not verify this delivery's phone manifest with keys/crux-ota-a-p256.pub")
    assets = dict(source.assets)
    for name, data in ((dev_ota_manifest.MANIFEST, text), (dev_ota_manifest.SIGNATURE, signature)):
        (source.directory / name).write_bytes(data)
        assets[name] = {"size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest()}
    return dataclasses.replace(source, assets=assets, phone_manifest=hashlib.sha256(text).hexdigest()), text


def find_release(api, source):
    releases = api.api("releases?per_page=100", pages=True)
    matches = [release for release in releases if release.get("tag_name") == source.tag]
    require(len(matches) <= 1, f"multiple releases use {source.tag}")
    require(not any(release.get("tag_name") != source.tag
                    and parse_tag(release.get("tag_name", "")) == parse_tag(source.tag)
                    for release in releases), f"another tag uses the same dev version as {source.tag}")
    return matches[0] if matches else None


def verify_release(api, source, release):
    """Return missing names; no differing or unexpected asset can be reused."""
    tag_ref = f"refs/tags/{source.tag}"
    refs = api.api(f"git/matching-refs/tags/{source.tag}")
    ref = next((ref["object"] for ref in refs if ref["ref"] == tag_ref), None)
    if release is None:
        require(ref is None, f"Git tag exists without a matching delivery release: {source.tag}")
        return set(source.assets)
    require(release.get("tag_name") == source.tag and release.get("prerelease") is True
            and isinstance(release.get("draft"), bool)
            and release.get("target_commitish") == RELEASE_TARGET
            and provenance(source) in (release.get("body") or "").splitlines()
            and (not source.phone_manifest
                 or phone_provenance(source) in (release.get("body") or "").splitlines()),
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
        require(name in source.assets and name not in names,
                f"unexpected or duplicate release asset: {name}")
        verify_asset(name, asset, source.assets[name])
        names.add(name)
    missing = set(source.assets) - names
    require(release["draft"] or not missing,
            f"published delivery is incomplete, not modified: {', '.join(sorted(missing))}")
    return missing


def verify_asset(name, asset, expected):
    require(asset.get("name") == name and asset.get("state") == "uploaded"
            and all(asset.get(key) == value for key, value in expected.items()),
            f"asset conflict for {name}: upload state, size or SHA-256 differs")


def deliver(api, source, operation):
    require(operation in {"preflight", "draft", "publish"}, "unknown release operation")
    release = find_release(api, source)
    missing = verify_release(api, source, release)
    if operation == "preflight":
        state = "available" if release is None else ("draft" if release["draft"] else "published")
        return {"state": state, "missing": sorted(missing), "changed": False}
    if release is not None and not release["draft"]:
        return {"state": "published", "release_id": release["id"], "changed": False}

    if operation == "publish":
        require(release is not None and not missing,
                "publish requires an existing, complete, verified draft; run draft first")
        release_id = release["id"]
        # This is the only transition which can make this release a dev OTA candidate.
        try:
            published = api.api(f"releases/{release_id}", method="PATCH",
                                payload={"draft": False, "prerelease": True, "make_latest": "false"})
            require(published.get("draft") is False and published.get("prerelease") is True
                    and published.get("tag_name") == source.tag and published.get("published_at"),
                    "GitHub did not confirm the expected prerelease")
        except (DevOtaError, OSError, ValueError, subprocess.SubprocessError) as error:
            raise DevOtaError(
                f"publish not confirmed for {source.tag}; inspect its live release state "
                f"before retrying: {error}") from error
        return {"state": "published", "release_id": release_id, "changed": True}

    created = release is None
    if created:
        # The tag is a channel version, not a source pin. Actions' token cannot tag a
        # historical commit with different workflow files; the note binds the actual source.
        release = api.api("releases", method="POST",
                          payload={"tag_name": source.tag, "target_commitish": RELEASE_TARGET,
                                   "name": f"Crux PE13 dev OTA {source.tag}", "body": release_body(source),
                                   "draft": True, "prerelease": True, "make_latest": "false"})
        require(release.get("draft") is True, "GitHub did not create a draft")
        missing = verify_release(api, source, release)
    release_id = release["id"]
    for name in sorted(missing):
        print(f"Uploading draft asset {name}", file=sys.stderr, flush=True)
        asset = api.upload(release_id, source.directory / name)
        verify_asset(name, asset, source.assets[name])
    release = api.api(f"releases/{release_id}")
    require(release.get("draft") is True and not verify_release(api, source, release),
            "draft is no longer private or complete")
    return {"state": "draft", "release_id": release_id, "changed": created or bool(missing)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assembly-run", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--operation", choices=["preflight", "draft", "publish"], default="preflight")
    parser.add_argument("--ota-start-builds", default="",
                        help="phone release: installed builds allowed to update (comma-separated)")
    parser.add_argument("--ota-serial", default="", help="phone release: serial the FITs carry, or any")
    parser.add_argument("--ota-signature", default="",
                        help="phone release: offline P-256 r||s over the phone manifest, 128 hex digits")
    args = parser.parse_args()
    phone = any((args.ota_start_builds, args.ota_serial, args.ota_signature))
    if phone and not (dev_ota_manifest.BUILDS_RE.fullmatch(args.ota_start_builds)
                      and (args.ota_serial == "any" or dev_ota_manifest.SERIAL_RE.fullmatch(args.ota_serial))
                      and re.fullmatch(r"[0-9a-f]{128}", args.ota_signature)):
        parser.error("a phone release needs --ota-start-builds (10-digit builds), --ota-serial and "
                     "--ota-signature (128 hex digits) together")
    if not re.fullmatch(r"[1-9][0-9]*", args.assembly_run):
        parser.error("--assembly-run must be a positive Actions run ID")
    if not re.fullmatch(r"dev-pe13-[0-9]{10}-v[1-9][0-9]*", args.tag):
        parser.error("--tag must be dev-pe13-<10-digit build>-v<positive version> (no leading zero in version)")
    if args.operation != "preflight" and not (
            os.environ.get("GITHUB_WORKFLOW_REF")
            == f"{REPOSITORY}/.github/workflows/release-dev.yml@refs/heads/main"
            and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"):
        parser.error("draft and publish must be dispatched through release-dev.yml on main")
    try:
        with tempfile.TemporaryDirectory(prefix="crux-dev-release-") as work:
            source = verify_source(args.assembly_run, args.tag, Path(work))
            if phone:
                source, text = attach_phone(source, args.ota_start_builds, args.ota_serial,
                                            bytes.fromhex(args.ota_signature))
                print(text.decode(), end="", flush=True)
            print(json.dumps({"tag": source.tag, "assembly_run": source.assembly_run,
                              "compile_run": source.compile_run, "artifact_id": source.artifact_id,
                              "artifact_digest": source.artifact_digest,
                              "phone_manifest_sha256": source.phone_manifest, "assets": source.assets},
                             indent=2), flush=True)
            print(json.dumps({"tag": source.tag, "operation": args.operation,
                              **deliver(Releases(), source, args.operation)}, indent=2))
    except (DevOtaError, OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"dev-release: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
