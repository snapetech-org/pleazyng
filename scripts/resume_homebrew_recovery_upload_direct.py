#!/usr/bin/env python3
"""Resume a partial direct-stable Homebrew release upload from its saved bundle."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from recover_macos_dmg_for_homebrew import api
from recover_macos_dmg_for_homebrew_direct import (
    ASSET_NAMES,
    CASK,
    RECOVERY_TITLE_PREFIX,
    REPOSITORY,
    RECOVERY_WORKFLOW_PATH,
    SIGNED_DMG,
    SUPPLEMENTAL_CHECKSUMS,
    upload,
)


def command(argv: list[str]) -> None:
    subprocess.run(argv, check=True)


def verify_attestation(path: Path, *, source_sha: str, invocation: str) -> None:
    result = subprocess.run(
        [
            "gh", "attestation", "verify", str(path), "--repo", REPOSITORY,
            "--signer-workflow", f"{REPOSITORY}/{RECOVERY_WORKFLOW_PATH}",
            "--signer-digest", source_sha, "--source-ref", "refs/heads/main",
            "--source-digest", source_sha, "--format", "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    attestations = json.loads(result.stdout)
    if not any(
        item.get("verificationResult", {}).get("signature", {}).get("certificate", {}).get(
            "runInvocationURI"
        ) == invocation
        for item in attestations
    ):
        raise ValueError(f"Asset attestation is not from the specified recovery attempt: {path.name}")


def restore_and_upload(
    source_run_id: int,
    source_attempt: int,
    asset_dir: Path,
    release_dir: Path,
) -> None:
    if (
        os.environ.get("GITHUB_REPOSITORY", "").lower() != REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
    ):
        raise ValueError("Direct recovery upload resume is restricted to canonical main")
    if source_run_id < 1 or source_attempt < 1:
        raise ValueError("Recovery run ID and attempt must be positive integers")
    if any(path.exists() or path.is_symlink() for path in (asset_dir, release_dir)):
        raise ValueError("Refusing to overwrite recovery restore directories")

    source = api(f"actions/runs/{source_run_id}/attempts/{source_attempt}")
    source_sha = source.get("head_sha", "")
    title = source.get("display_title", "")
    tag_match = re.fullmatch(re.escape(RECOVERY_TITLE_PREFIX) + r"(v[0-9]+\.[0-9]+\.[0-9]+)", title)
    if (
        source.get("head_repository", {}).get("full_name", "").lower() != REPOSITORY
        or source.get("head_branch") != "main"
        or source.get("event") != "workflow_dispatch"
        or source.get("path", "").split("@", 1)[0] != RECOVERY_WORKFLOW_PATH
        or source.get("run_attempt") != source_attempt
        or source.get("status") != "completed"
        or not tag_match
        or not re.fullmatch(r"[0-9a-f]{40}", source_sha)
    ):
        raise ValueError("Source run is not a completed canonical direct-stable recovery attempt")
    current_sha = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", current_sha):
        raise ValueError("Current resume workflow source commit is invalid")
    if api(f"compare/{source_sha}...{current_sha}").get("status") not in {"ahead", "identical"}:
        raise ValueError("Recovery workflow source is not an ancestor of current main")

    artifact_name = f"pleazyng-homebrew-recovery-assets-{source_run_id}-{source_attempt}"
    artifacts = api(f"actions/runs/{source_run_id}/artifacts?per_page=100").get("artifacts", [])
    matches = [item for item in artifacts if item.get("name") == artifact_name]
    if (
        len(matches) != 1
        or matches[0].get("expired") is not False
        or (matches[0].get("workflow_run") or {}).get("id") != source_run_id
        or (matches[0].get("workflow_run") or {}).get("head_sha") != source_sha
    ):
        raise ValueError("Exact preserved direct-recovery bundle is missing, expired, or ambiguous")

    asset_dir.mkdir(mode=0o700, parents=True)
    release_dir.mkdir(mode=0o700, parents=True)
    command([
        "gh", "run", "download", str(source_run_id), "--repo", REPOSITORY,
        "--name", artifact_name, "--dir", str(asset_dir),
    ])
    files = list(asset_dir.iterdir())
    if (
        {path.name for path in files} != set(ASSET_NAMES)
        or any(path.is_symlink() or not path.is_file() for path in files)
    ):
        raise ValueError("Preserved recovery bundle does not contain exactly four safe assets")

    tag = tag_match.group(1)
    receipt = json.loads((asset_dir / "pleazyng-homebrew-recovery.json").read_text(encoding="utf-8"))
    core_release = receipt.get("core_release", {})
    assembly_run_id = core_release.get("assembly_run_id")
    assembly_attempt = core_release.get("assembly_run_attempt")
    is_assembly = assembly_run_id is not None
    if (
        receipt.get("recovery_kind") != ("platform-assembly" if is_assembly else "direct-source")
        or receipt.get("release_tag") != tag
        or receipt.get("recovery_workflow", {}).get("run_id") != source_run_id
        or receipt.get("recovery_workflow", {}).get("run_attempt") != source_attempt
        or receipt.get("recovery_workflow", {}).get("workflow_sha") != source_sha
        or (assembly_run_id is None) != (assembly_attempt is None)
        or (is_assembly and
            (type(assembly_run_id) is not int or assembly_run_id < 1
             or type(assembly_attempt) is not int or assembly_attempt < 1))
    ):
        raise ValueError("Saved recovery receipt does not identify this workflow attempt")

    patterns = ["--pattern", "SHA256SUMS.txt"]
    if is_assembly:
        patterns.extend(["--pattern", "platform-reuse-provenance.json"])
    command([
        "gh", "release", "download", tag, "--repo", REPOSITORY,
        *patterns, "--dir", str(release_dir),
    ])
    core_checksums = release_dir / "SHA256SUMS.txt"
    command([
        sys.executable,
        str(Path(__file__).with_name("verify_release_attestation.py")),
        str(core_checksums), "--repo", REPOSITORY,
    ])

    verify_args = [
        sys.executable, str(Path(__file__).with_name("recover_macos_dmg_for_homebrew_direct.py")),
        "verify", "--tag", tag, "--version", tag[1:], "--core-checksums", str(core_checksums),
        "--homebrew-checksums", str(asset_dir / SUPPLEMENTAL_CHECKSUMS),
        "--receipt", str(asset_dir / "pleazyng-homebrew-recovery.json"),
        "--asset", str(asset_dir / SIGNED_DMG),
    ]
    if is_assembly:
        verify_args.extend(["--platform-reuse", str(release_dir / "platform-reuse-provenance.json")])
    command(verify_args)
    expected_cask = asset_dir / "expected-cask.rb"
    command([
        sys.executable, str(Path(__file__).with_name("generate_homebrew_cask.py")),
        "--version", tag[1:], "--checksums", str(asset_dir / SUPPLEMENTAL_CHECKSUMS),
        "--asset-name", SIGNED_DMG, "--output", str(expected_cask),
    ])
    if (asset_dir / CASK).read_bytes() != expected_cask.read_bytes():
        raise ValueError("Preserved Cask candidate differs from the notarized DMG checksum")
    expected_cask.unlink()

    invocation = f"https://github.com/{REPOSITORY}/actions/runs/{source_run_id}/attempts/{source_attempt}"
    for name in ASSET_NAMES:
        verify_attestation(asset_dir / name, source_sha=source_sha, invocation=invocation)
    upload(tag, REPOSITORY, asset_dir)
    print("Preserved direct-recovery bundle verified; only missing matching assets were appended.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", type=int, required=True)
    parser.add_argument("--source-attempt", type=int, required=True)
    parser.add_argument("--asset-directory", type=Path, required=True)
    parser.add_argument("--release-directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        restore_and_upload(args.source_run_id, args.source_attempt, args.asset_directory, args.release_directory)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        parser.exit(1, f"Direct Homebrew recovery upload resume failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
