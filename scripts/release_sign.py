#!/usr/bin/env python3
"""Sign one successful assembly's ZIP install manifest with the CI install key.

release.yml runs this only in its install-signing environment job, which uses no
third-party action and uploads nothing. The private key comes from
CRUX_INSTALL_SIGNING_KEY into a private temporary file that is deleted after
signing. The output is the 128-hex-digit P-256 signature of the crux-install.txt
that the assembly retained as its zip-manifest artifact, checked against
keys/crux-b-install-2026-11-07.pub.pem.
"""

import argparse
import contextlib
import io
import os
from pathlib import Path
import re
import sys
import tempfile
import time

import crux_zip
import release_source
from release_source import SourceError

PUBLIC_KEY = Path(__file__).resolve().parents[1] / "keys/crux-b-install-2026-11-07.pub.pem"
KEY_ENV = "CRUX_INSTALL_SIGNING_KEY"


def wait_until_completed(run_id, timeout, interval=20):
    """The assembly dispatches its release as its last job; wait for that run to end."""
    deadline = time.monotonic() + timeout
    while release_source._json_api("actions/runs/" + run_id, release_source._token()).get("status") != "completed":
        if time.monotonic() > deadline:
            raise SourceError(f"assembly run {run_id} did not finish within {timeout} seconds")
        time.sleep(interval)


def sign(manifest, key_text):
    if not key_text.strip():
        raise SourceError(f"{KEY_ENV} is empty; store the install key in the install-signing environment")
    with tempfile.TemporaryDirectory(prefix="crux-sign-") as directory:
        key = Path(directory) / "key.pem"
        with os.fdopen(os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            handle.write(key_text if key_text.endswith("\n") else key_text + "\n")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = crux_zip.main(["sign", "--payload", str(manifest.parent), "--key", str(key)])
    signature = output.getvalue().strip()
    if status != 0 or not re.fullmatch(r"[0-9a-f]{128}", signature):
        raise SourceError("the install manifest could not be signed")
    crux_zip.verify(manifest, bytes.fromhex(signature), PUBLIC_KEY)
    return signature


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--assembly-run", required=True)
    parser.add_argument("--wait-seconds", type=int, default=1200)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[1-9][0-9]*", args.assembly_run):
        parser.error("--assembly-run must be a positive Actions run ID")
    try:
        wait_until_completed(args.assembly_run, args.wait_seconds)
        with tempfile.TemporaryDirectory(prefix="crux-manifest-") as work:
            build, manifest = release_source.signing_manifest(args.assembly_run, Path(work))
            signature = sign(manifest, os.environ.get(KEY_ENV, ""))
    except (SourceError, crux_zip.ZipError, OSError, ValueError) as error:
        parser.exit(1, f"release-sign: {error}\n")
    if args.github_output:
        with args.github_output.open("a") as output:
            print("signature=" + signature, file=output)
    print(f"Signed the install manifest of build {build}: {signature}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
