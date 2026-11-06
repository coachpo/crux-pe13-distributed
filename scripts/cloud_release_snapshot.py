"""Fetch only the two pinned private source snapshots from GitHub Releases."""

import hashlib
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request


ARCHIVE_TYPE = "github-release-snapshot"
TOKEN_ENV = "CRUX_SOURCE_SNAPSHOTS_TOKEN"
API = "https://api.github.com"
PROJECT_REPOSITORIES = {
    "external/faceunlock": "coachpo/crux-pe13-faceunlock-snapshot",
    "vendor/gms": "coachpo/crux-pe13-vendor-gms-snapshot",
}
LOCK_FIELDS = {"repository", "repository_id", "release_id", "tag", "asset_id", "asset_name",
               "size_bytes", "sha256"}


class SnapshotError(ValueError):
    pass


def validate(project):
    lock = project.get("release_snapshot")
    repository = PROJECT_REPOSITORIES.get(project.get("path"))
    revision, tree = project.get("revision"), project.get("expected_tree")
    if (repository is None or not isinstance(lock, dict) or set(lock) != LOCK_FIELDS
            or lock.get("repository") != repository
            or project.get("url") != f"https://github.com/{repository}"):
        raise SnapshotError("Invalid private snapshot project or repository")
    if any(not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{40}", pin)
           for pin in (revision, tree)):
        raise SnapshotError("Private snapshot requires exact source commit and Git tree")
    asset_name = project["path"].replace("/", "-") + f"-{revision}.tar.gz"
    if lock.get("tag") != "snapshot-" + revision or lock.get("asset_name") != asset_name:
        raise SnapshotError("Private snapshot tag or asset name differs from the source pin")
    for key in ("repository_id", "release_id", "asset_id", "size_bytes"):
        if type(lock.get(key)) is not int or lock[key] <= 0:
            raise SnapshotError(f"Invalid private snapshot {key}")
    sha = lock.get("sha256")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise SnapshotError("Private snapshot requires an exact asset SHA-256")
    if (project.get("patches") or "archive_revision" in project or "strip_components" in project
            or project.get("archive_url", asset_url(lock)) != asset_url(lock)):
        raise SnapshotError("Private snapshot does not accept archive or patch overrides")
    return lock


def asset_url(lock):
    return f"{API}/repos/{lock['repository']}/releases/assets/{lock['asset_id']}"


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or not target.hostname or target.username is not None:
            raise SnapshotError("Private snapshot redirect requires HTTPS without credentials")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _request(url, accept, token):
    request = urllib.request.Request(url, headers={
        "Accept": accept, "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "crux-pe13-source-snapshots",
    })
    # urllib does not forward unredirected headers, including across HTTPS
    # to the signed Release asset host. Neither token nor redirect URL is logged.
    request.add_unredirected_header("Authorization", "Bearer " + token)
    return request


def _metadata(opener, url, token, timeout):
    with opener.open(_request(url, "application/vnd.github+json", token), timeout=timeout) as response:
        try:
            data = json.load(response)
        except ValueError:
            raise SnapshotError("Private snapshot API did not return JSON metadata") from None
    if not isinstance(data, dict):
        raise SnapshotError("Private snapshot API metadata is not an object")
    return data


def verify_archive(path, lock):
    if path.stat().st_size != lock["size_bytes"]:
        raise SnapshotError("Private snapshot asset byte count differs")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != lock["sha256"]:
        raise SnapshotError("Private snapshot asset SHA-256 differs")


def download(project, destination, token, timeout):
    lock = validate(project)
    if not token:
        raise SnapshotError(f"Missing {TOKEN_ENV} for private snapshot download")
    opener = urllib.request.build_opener(HTTPSRedirect())
    repo_url = f"{API}/repos/{lock['repository']}"
    try:
        repo = _metadata(opener, repo_url, token, timeout)
        if (repo.get("id") != lock["repository_id"] or repo.get("full_name") != lock["repository"]
                or repo.get("private") is not True):
            raise SnapshotError("Private snapshot repository identity or visibility differs")
        release = _metadata(opener, f"{repo_url}/releases/tags/{lock['tag']}", token, timeout)
        if (release.get("id") != lock["release_id"] or release.get("tag_name") != lock["tag"]
                or release.get("draft") is not False):
            raise SnapshotError("Private snapshot Release identity differs")
        assets = release.get("assets")
        matches = [asset for asset in assets if isinstance(asset, dict) and
                   asset.get("id") == lock["asset_id"]] if isinstance(assets, list) else []
        if len(matches) != 1:
            raise SnapshotError("Private snapshot asset is absent or ambiguous in the Release")
        asset = matches[0]
        if (asset.get("name") != lock["asset_name"] or asset.get("state") != "uploaded"
                or asset.get("size") != lock["size_bytes"] or asset.get("digest") != "sha256:" + lock["sha256"]):
            raise SnapshotError("Private snapshot Release asset metadata differs")
        request = _request(asset_url(lock), "application/octet-stream", token)
        size = 0
        with opener.open(request, timeout=timeout) as response, destination.open("xb") as output:
            for block in iter(lambda: response.read(1024 * 1024), b""):
                size += len(block)
                if size > lock["size_bytes"]:
                    raise SnapshotError("Private snapshot download exceeds its pinned byte count")
                output.write(block)
        verify_archive(destination, lock)
    except urllib.error.HTTPError as error:
        if error.fp is not None:
            error.close()
        raise SnapshotError(f"Private snapshot API returned HTTP {error.code}") from None
    except SnapshotError:
        raise
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise SnapshotError(f"Private snapshot transfer failed ({type(error).__name__})") from None
    return lock
