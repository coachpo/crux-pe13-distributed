#!/usr/bin/env python3
"""Download selected artifacts from one Actions run using the shared retry transport."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import re
import sys

import cloud_layers


class ArtifactDownloadError(ValueError):
    pass


def _token(value=None):
    token = value or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ArtifactDownloadError("GH_TOKEN or GITHUB_TOKEN is required to download Actions artifacts")
    return token


def _artifact_directory(root: Path, name: str) -> Path:
    if (not name or name in {".", ".."} or "/" in name or "\\" in name
            or Path(name).is_absolute()):
        raise ArtifactDownloadError("Unsafe artifact name for pattern destination: " + repr(name))
    result = root / name
    if result.resolve().parent != root.resolve():
        raise ArtifactDownloadError("Artifact destination escapes output directory: " + name)
    return result


def download_artifacts(run: str, repository: str, destination: Path, token=None,
                       name: str | None = None, pattern: str | None = None) -> list[dict]:
    if (name is None) == (pattern is None):
        raise ArtifactDownloadError("Specify exactly one of name or pattern")
    if name is not None and not name:
        raise ArtifactDownloadError("Artifact name must be non-empty")
    if pattern is not None and not pattern:
        raise ArtifactDownloadError("Artifact pattern must be non-empty")
    token = _token(token)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)

    artifacts = cloud_layers.artifact_inventory(run, repository, token)
    if name is not None:
        selected = [artifacts[name]] if name in artifacts else []
        if not selected:
            raise ArtifactDownloadError(f"Run {run} has no unexpired artifact named {name!r}")
        placements = [(selected[0], destination)]
    else:
        selected = sorted((artifact for artifact_name, artifact in artifacts.items()
                           if fnmatch.fnmatchcase(artifact_name, pattern)),
                          key=lambda artifact: artifact["name"])
        if not selected:
            raise ArtifactDownloadError(f"Run {run} has no unexpired artifact matching {pattern!r}")
        placements = [(artifact, _artifact_directory(destination, artifact["name"]))
                      for artifact in selected]

    downloaded = []
    for artifact, target in placements:
        target.mkdir(parents=True, exist_ok=True)
        cloud_layers._extract_artifact(artifact, target, token, repository)
        downloaded.append({"name": artifact["name"], "id": artifact["id"],
                           "destination": str(target)})
    return downloaded


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--repository", required=True, help="owner/repository")
    parser.add_argument("--destination", required=True)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--name", help="Download exactly one artifact into destination")
    selector.add_argument("--pattern", help="Download every matching artifact into named subdirectories")
    args = parser.parse_args(argv)
    if not args.run.isdecimal() or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("--run must be numeric and --repository must be owner/repository")
    try:
        downloaded = download_artifacts(args.run, args.repository, Path(args.destination),
                                        name=args.name, pattern=args.pattern)
    except (ArtifactDownloadError, OSError, RuntimeError, ValueError) as error:
        print("cloud_artifacts: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps({"run": args.run, "repository": args.repository,
                      "artifacts": downloaded}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
