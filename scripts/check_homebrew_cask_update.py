#!/usr/bin/env python3
"""Reject Homebrew Cask downgrades and same-version byte changes."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
CASK_VERSION_PATTERN = re.compile(r'^\s*version\s+"([^"]+)"\s*$', re.MULTILINE)


def version_tuple(value: str) -> tuple[int, int, int]:
    if not VERSION_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid stable Cask version: {value}")
    return tuple(int(part) for part in value.split("."))


def check_update(version: str, current_path: Path, candidate_path: Path) -> None:
    incoming_version = version_tuple(version)
    if candidate_path.is_symlink() or not candidate_path.is_file():
        raise ValueError("Verified Cask candidate must be a regular file")
    if current_path.is_symlink():
        raise ValueError("Existing tap Cask must not be a symlink")
    if not current_path.exists():
        return

    current_text = current_path.read_text(encoding="utf-8")
    versions = CASK_VERSION_PATTERN.findall(current_text)
    if len(versions) != 1:
        raise ValueError("Existing tap Cask has no unique stable version")
    current_version = version_tuple(versions[0])
    if current_version > incoming_version:
        raise ValueError(f"Refusing to roll the tap back from {versions[0]} to {version}")
    if current_version == incoming_version and current_path.read_bytes() != candidate_path.read_bytes():
        raise ValueError(f"Refusing to replace the immutable Cask already published for {version}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="incoming stable MAJOR.MINOR.PATCH version")
    parser.add_argument("--current", type=Path, required=True, help="existing tap Cask path")
    parser.add_argument("--candidate", type=Path, required=True, help="verified Cask candidate path")
    args = parser.parse_args()
    try:
        check_update(args.version, args.current, args.candidate)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Homebrew Cask update rejected: {error}\n")
    print(f"Homebrew Cask update to {args.version} is monotonic and immutable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
