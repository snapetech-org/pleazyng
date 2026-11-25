#!/usr/bin/env python3
"""Verify a preserved v3.0.51 recovery bundle and append missing release assets."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from recover_macos_dmg_for_homebrew import api
from upload_homebrew_recovery_assets import ASSET_NAMES, RELEASE_TAG, REPOSITORY, upload


WORKFLOW_PATH = ".github/workflows/recover-homebrew-cask-v3051.yml"
RUN_TITLE = "action:homebrew:recover:tag:v3.0.51"
RECOVERY_ASSET = "pleazyng-macos-notarized.dmg"
CHECKSUMS = "pleazyng-homebrew-SHA256SUMS.txt"
RECEIPT = "pleazyng-homebrew-recovery.json"
CASK = "snapetech-pleazyng.rb"


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def restore_and_upload(source_run_id: int, source_attempt: int,
                       asset_dir: Path, release_dir: Path) -> None:
    if (os.environ.get("GITHUB_REPOSITORY", "").lower() != REPOSITORY
            or os.environ.get("GITHUB_REF") != "refs/heads/main"):
        raise ValueError("Recovery upload resume is restricted to the canonical main workflow")
    if source_run_id < 1 or source_attempt < 1:
        raise ValueError("Source recovery run ID and attempt must be positive integers")
    if asset_dir.exists() or asset_dir.is_symlink() or release_dir.exists() or release_dir.is_symlink():
        raise ValueError("Refusing to overwrite an existing recovery restore directory")

    source = api(f"actions/runs/{source_run_id}/attempts/{source_attempt}")
    source_sha = source.get("head_sha", "")
    if (
        source.get("head_repository", {}).get("full_name", "").lower() != REPOSITORY
        or source.get("head_branch") != "main"
        or source.get("event") != "workflow_dispatch"
        or source.get("path", "").split("@", 1)[0] != WORKFLOW_PATH
        or source.get("run_attempt") != source_attempt
        or source.get("display_title") != RUN_TITLE
        or not re.fullmatch(r"[0-9a-f]{40}", source_sha)
        or source.get("status") != "completed"
    ):
        raise ValueError("Source run is not a completed canonical v3.0.51 recovery attempt")

    current_sha = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", current_sha):
        raise ValueError("Current resume workflow source commit is invalid")
    comparison = api(f"compare/{source_sha}...{current_sha}")
    if comparison.get("status") not in {"ahead", "identical"}:
        raise ValueError("Source recovery workflow commit is not an ancestor of current main")

    artifact_name = f"pleazyng-homebrew-recovery-assets-{source_run_id}-{source_attempt}"
    artifacts = api(f"actions/runs/{source_run_id}/artifacts?per_page=30&page=1").get("artifacts", [])
    matches = [item for item in artifacts if item.get("name") == artifact_name]
    if (
        len(matches) != 1
        or matches[0].get("expired") is not False
        or (matches[0].get("workflow_run") or {}).get("id") != source_run_id
        or (matches[0].get("workflow_run") or {}).get("head_sha") != source_sha
    ):
        raise ValueError("The exact recovery-attempt asset bundle is missing or expired")

    asset_dir.mkdir(mode=0o700, parents=True)
    release_dir.mkdir(mode=0o700, parents=True)
    run([
        "gh", "run", "download", str(source_run_id), "--repo", REPOSITORY,
        "--name", artifact_name, "--dir", str(asset_dir),
    ])
    actual_names = {path.name for path in asset_dir.iterdir()}
    if actual_names != set(ASSET_NAMES) or any(path.is_symlink() or not path.is_file()
                                               for path in asset_dir.iterdir()):
        raise ValueError("Preserved recovery bundle does not contain exactly the four expected files")

    run([
        "gh", "release", "download", RELEASE_TAG, "--repo", REPOSITORY,
        "--pattern", "SHA256SUMS.txt", "--pattern", "platform-reuse-provenance.json",
        "--dir", str(release_dir),
    ])
    root = Path(__file__).resolve().parents[1]
    run([
        sys.executable, str(root / "scripts/verify_release_attestation.py"),
        str(release_dir / "SHA256SUMS.txt"), "--repo", REPOSITORY,
    ])
    run([
        sys.executable, str(root / "scripts/recover_macos_dmg_for_homebrew.py"), "verify",
        "--tag", RELEASE_TAG, "--version", "3.0.51",
        "--core-checksums", str(release_dir / "SHA256SUMS.txt"),
        "--homebrew-checksums", str(asset_dir / CHECKSUMS),
        "--platform-reuse", str(release_dir / "platform-reuse-provenance.json"),
        "--receipt", str(asset_dir / RECEIPT), "--asset", str(asset_dir / RECOVERY_ASSET),
    ])

    expected_cask = asset_dir / "expected-cask.rb"
    run([
        sys.executable, str(root / "scripts/generate_homebrew_cask.py"),
        "--version", "3.0.51", "--checksums", str(asset_dir / CHECKSUMS),
        "--asset-name", RECOVERY_ASSET, "--output", str(expected_cask),
    ])
    if (asset_dir / CASK).read_bytes() != expected_cask.read_bytes():
        raise ValueError("Preserved Cask candidate does not match the signed DMG and checksum manifest")
    expected_cask.unlink()

    invocation = f"https://github.com/{REPOSITORY}/actions/runs/{source_run_id}/attempts/{source_attempt}"
    for name in ASSET_NAMES:
        result = subprocess.run(
            [
                "gh", "attestation", "verify", str(asset_dir / name), "--repo", REPOSITORY,
                "--signer-workflow", f"{REPOSITORY}/{WORKFLOW_PATH}",
                "--signer-digest", source_sha, "--source-ref", "refs/heads/main",
                "--source-digest", source_sha, "--format", "json",
            ],
            check=True, capture_output=True, text=True,
        )
        attestations = json.loads(result.stdout)
        if not any(
            item.get("verificationResult", {}).get("signature", {}).get("certificate", {}).get(
                "runInvocationURI"
            ) == invocation
            for item in attestations
        ):
            raise ValueError(f"Asset attestation is not from the specified recovery attempt: {name}")

    upload(RELEASE_TAG, REPOSITORY, asset_dir)
    print("Preserved recovery bundle verified; only missing matching release assets were appended.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", type=int, required=True)
    parser.add_argument("--source-attempt", type=int, required=True)
    parser.add_argument("--asset-directory", type=Path, required=True)
    parser.add_argument("--release-directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        restore_and_upload(args.source_run_id, args.source_attempt,
                           args.asset_directory, args.release_directory)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        parser.exit(1, f"Homebrew recovery upload resume failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
