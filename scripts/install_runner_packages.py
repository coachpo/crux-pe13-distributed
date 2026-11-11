#!/usr/bin/env python3
"""Install runner packages using the existing non-Azure Ubuntu mirrors."""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


MIRROR_LIST = Path("/etc/apt/apt-mirrors.txt")
AZURE_HOST = "azure.archive.ubuntu.com"


def filter_azure_mirror_rows(text: str) -> str:
    """Keep every comment and non-Azure row byte-for-byte, in original order."""
    retained = []
    for line in text.splitlines(keepends=True):
        fields = line.partition("#")[0].split()
        if fields and urlsplit(fields[0]).hostname == AZURE_HOST:
            continue
        retained.append(line)

    filtered = "".join(retained)
    if not any(line.partition("#")[0].strip() for line in retained):
        raise ValueError("apt mirror list has no remaining source rows after Azure mirror filtering")
    return filtered


def install_packages(packages, mirror_list: Path = MIRROR_LIST, run=subprocess.run) -> None:
    """Filter the runner's mirror list if needed, then update and install packages."""
    packages = list(packages)
    if not packages or any(not isinstance(package, str) or not package for package in packages):
        raise ValueError("at least one non-empty package name is required")

    mirror_list = Path(mirror_list)
    with mirror_list.open("r", encoding="utf-8", newline="") as source:
        original = source.read()
    filtered = filter_azure_mirror_rows(original)

    temporary = None
    try:
        if filtered != original:
            with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="", prefix="apt-mirrors-", delete=False) as staged:
                staged.write(filtered)
            temporary = Path(staged.name)
            run(["sudo", "install", "-m0644", str(temporary), str(mirror_list)], check=True)

        run(["sudo", "apt-get", "update"], check=True)
        run(["sudo", "apt-get", "install", "-y", *packages], check=True)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", nargs="+", help="apt package names to install")
    args = parser.parse_args(argv)
    try:
        install_packages(args.packages)
    except subprocess.CalledProcessError as error:
        print(f"install_runner_packages: command failed with exit code {error.returncode}: {error.cmd}",
              file=sys.stderr)
        return error.returncode
    except (OSError, ValueError) as error:
        print(f"install_runner_packages: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
