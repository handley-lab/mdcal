#!/usr/bin/env python3
"""Fail if `pyproject.toml`'s version equals the one on origin/master."""

import subprocess
import sys
from pathlib import Path

import tomli


def pyproject_version(content: str) -> str:
    return tomli.loads(content)["project"]["version"]


def main() -> int:
    local = pyproject_version(Path("pyproject.toml").read_text())
    subprocess.run(
        ["git", "fetch", "origin", "master"], check=True, capture_output=True
    )
    show = subprocess.run(
        ["git", "show", "origin/master:pyproject.toml"],
        check=True,
        capture_output=True,
        text=True,
    )
    master = pyproject_version(show.stdout)
    if local == master:
        print(
            f"version {local} matches origin/master — bump pyproject.toml before merging",
            file=sys.stderr,
        )
        return 1
    print(f"version {local} (origin/master at {master})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
