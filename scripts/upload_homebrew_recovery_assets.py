#!/usr/bin/env python3
"""Append only exact, digest-matching v3.0.51 Homebrew recovery assets."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


REPOSITORY = "snapetech-org/pleazyng"
RELEASE_TAG = "v3.0.51"
ASSET_NAMES = (
    "pleazyng-macos-notarized.dmg",
    "pleazyng-homebrew-SHA256SUMS.txt",
    "pleazyng-homebrew-recovery.json",
    "snapetech-pleazyng.rb",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload(tag: str, repository: str, directory: Path) -> list[str]:
    if tag != RELEASE_TAG or repository.lower() != REPOSITORY:
        raise ValueError("Homebrew recovery uploads are restricted to the canonical v3.0.51 release")

    local: dict[str, Path] = {}
    for name in ASSET_NAMES:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Required recovery asset is missing or unsafe: {name}")
        local[name] = path

    response = subprocess.check_output(
        ["gh", "release", "view", tag, "--repo", REPOSITORY, "--json", "assets"],
        text=True,
    )
    remote_items = json.loads(response).get("assets", [])
    remote: dict[str, dict] = {}
    for asset in remote_items:
        name = asset.get("name")
        if name in ASSET_NAMES:
            if name in remote:
                raise ValueError(f"Release contains duplicate recovery asset name: {name}")
            remote[name] = asset

    missing: list[str] = []
    for name, path in local.items():
        digest = f"sha256:{sha256(path)}"
        existing = remote.get(name)
        if existing is None:
            missing.append(name)
        elif existing.get("digest") != digest:
            raise ValueError(f"Existing release asset differs from the preserved recovery bundle: {name}")

    if missing:
        subprocess.run(
            ["gh", "release", "upload", tag, "--repo", REPOSITORY,
             *(str(local[name]) for name in missing)],
            check=True,
        )
        print(f"Appended {len(missing)} missing, digest-verified Homebrew recovery asset(s).")
    else:
        print("All Homebrew recovery release assets already match the preserved bundle.")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", default=REPOSITORY)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        upload(args.tag, args.repo, args.directory)
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        parser.exit(1, f"Homebrew recovery asset upload failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
