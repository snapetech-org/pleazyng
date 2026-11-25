#!/usr/bin/env python3
"""Recover a stable direct-release macOS app into an attested Cask asset."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from recover_macos_dmg_for_homebrew import (
    api,
    app_version,
    manifest_digest,
    sha256,
)
from recover_macos_app import restore_portable_checkpoint, tree_digest


REPOSITORY = "snapetech-org/pleazyng"
ORIGINAL_DMG = "pleazyng-macos.dmg"
SIGNED_DMG = "pleazyng-macos-notarized.dmg"
SUPPLEMENTAL_CHECKSUMS = "pleazyng-homebrew-SHA256SUMS.txt"
RECOVERY_RECEIPT = "pleazyng-homebrew-recovery.json"
CASK = "snapetech-pleazyng.rb"
ASSET_NAMES = (SIGNED_DMG, SUPPLEMENTAL_CHECKSUMS, RECOVERY_RECEIPT, CASK)
BUNDLE_ID = "org.snapetech.pleazyng"
TEAM_ID = "DM9A59DC85"
CHECKPOINT_PREFIX = "macos-compiled-app-"
RELEASE_TITLE_PREFIX = "action:release:pool:public:tag:"
RECOVERY_TITLE_PREFIX = "action:homebrew:recover:tag:"
WORKFLOW_PATH = ".github/workflows/build.yml"
RECOVERY_WORKFLOW_PATH = ".github/workflows/recover-homebrew-cask.yml"
MAX_JOB_PAGES = 10


def verify_recovery_attestation(path: Path, recovery: dict) -> None:
    run_id = recovery.get("run_id")
    attempt = recovery.get("run_attempt")
    workflow_sha = recovery.get("workflow_sha", "")
    if (
        type(run_id) is not int or run_id < 1
        or type(attempt) is not int or attempt < 1
        or not re.fullmatch(r"[0-9a-f]{40}", str(workflow_sha))
    ):
        raise ValueError("Recovery receipt does not identify an exact attested workflow attempt")
    result = subprocess.run(
        [
            "gh", "attestation", "verify", str(path), "--repo", REPOSITORY,
            "--signer-workflow", f"{REPOSITORY}/{RECOVERY_WORKFLOW_PATH}",
            "--signer-digest", workflow_sha, "--source-ref", "refs/heads/main",
            "--source-digest", workflow_sha, "--format", "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    invocation = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}"
    attestations = json.loads(result.stdout)
    if not any(
        item.get("verificationResult", {}).get("signature", {}).get("certificate", {}).get(
            "runInvocationURI"
        ) == invocation
        for item in attestations
    ):
        raise ValueError(f"Recovery asset attestation is not from its recorded run: {path.name}")


def stable_version(tag: str) -> str:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Homebrew recovery requires an exact stable vMAJOR.MINOR.PATCH tag")
    return tag[1:]


def tag_commit(tag: str) -> str:
    value = api(f"git/ref/tags/{tag}")["object"]
    for _ in range(8):
        if value.get("type") == "commit":
            commit = value.get("sha", "")
            if re.fullmatch(r"[0-9a-f]{40}", commit):
                return commit
            break
        if value.get("type") != "tag" or not re.fullmatch(r"[0-9a-f]{40}", str(value.get("sha", ""))):
            break
        value = api(f"git/tags/{value['sha']}")["object"]
    raise ValueError("Stable release tag does not resolve to a commit")


def attempt_jobs(run_id: int, attempt: int) -> list[dict]:
    jobs: list[dict] = []
    total_count: int | None = None
    for page in range(1, MAX_JOB_PAGES + 1):
        inventory = api(
            f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=30&page={page}"
        )
        page_jobs = inventory.get("jobs", [])
        jobs.extend(page_jobs)
        total_count = int(inventory.get("total_count", len(jobs)))
        if len(jobs) >= total_count or len(page_jobs) < 30:
            break
    if total_count is not None and len(jobs) < total_count:
        raise ValueError("Direct release job inventory could not be fully read")
    return jobs


def require_successful_job(jobs: list[dict], name: str, attempt: int, required_step: str) -> dict:
    matches = [job for job in jobs if job.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"Direct release job {name!r} is missing or ambiguous")
    job = matches[0]
    if (
        job.get("status") != "completed"
        or job.get("conclusion") != "success"
        or job.get("run_attempt") != attempt
        or not any(
            step.get("name") == required_step and step.get("conclusion") == "success"
            for step in job.get("steps", [])
        )
    ):
        raise ValueError(f"Direct release job {name!r} did not complete successfully")
    return job


def verify_build_attestation(path: Path, run: dict) -> None:
    run_id = run.get("id")
    attempt = run.get("run_attempt")
    source_sha = run.get("head_sha", "")
    if (type(run_id) is not int or run_id < 1 or type(attempt) is not int or attempt < 1
            or not re.fullmatch(r"[0-9a-f]{40}", source_sha)):
        raise ValueError("Core release assembly identity is incomplete")
    result = subprocess.run(
        [
            "gh", "attestation", "verify", str(path), "--repo", REPOSITORY,
            "--signer-workflow", f"{REPOSITORY}/{WORKFLOW_PATH}",
            "--signer-digest", source_sha, "--source-ref", "refs/heads/main",
            "--source-digest", source_sha, "--format", "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    invocation = f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}"
    attestations = json.loads(result.stdout)
    if not any(
        item.get("verificationResult", {}).get("signature", {}).get("certificate", {}).get(
            "runInvocationURI"
        ) == invocation
        for item in attestations
    ):
        raise ValueError(f"Core release attestation is not from the selected release assembly: {path.name}")


def verify_direct_source(
    tag: str,
    source_run_id: int,
    source_attempt: int,
    *,
    assembly_run_id: int | None = None,
    assembly_attempt: int | None = None,
    allow_recovery_assets: bool = False,
) -> dict:
    version = stable_version(tag)
    if source_run_id < 1 or source_attempt < 1:
        raise ValueError("Source release run ID and attempt must be positive integers")
    if (assembly_run_id is None) != (assembly_attempt is None):
        raise ValueError("Assembly run ID and attempt must be supplied together")
    if assembly_run_id is not None and (assembly_run_id < 1 or assembly_attempt < 1):
        raise ValueError("Assembly run ID and attempt must be positive integers")
    if assembly_run_id == source_run_id and assembly_attempt == source_attempt:
        raise ValueError("Assembly recovery must identify a separate release-assembly run")
    source_sha = tag_commit(tag)
    run = api(f"actions/runs/{source_run_id}/attempts/{source_attempt}")
    expected_title = RELEASE_TITLE_PREFIX + tag
    if (
        run.get("head_repository", {}).get("full_name", "").lower() != REPOSITORY
        or run.get("head_branch") != "main"
        or run.get("event") != "workflow_dispatch"
        or run.get("path", "").split("@", 1)[0] != WORKFLOW_PATH
        or run.get("head_sha") != source_sha
        or run.get("run_attempt") != source_attempt
        or run.get("display_title") != expected_title
        or run.get("status") != "completed"
        or (assembly_run_id is None and run.get("conclusion") != "success")
        or (assembly_run_id is not None and run.get("conclusion") not in {
            "success", "failure", "cancelled", "timed_out",
        })
    ):
        raise ValueError("Source run is not a completed exact-version public release build")

    source_inventory = attempt_jobs(source_run_id, source_attempt)
    macos_job = require_successful_job(source_inventory, "build-macos", source_attempt, "Build macOS")
    if macos_job.get("head_sha") != source_sha:
        raise ValueError("Successful Mac build does not match the release tag source")
    if assembly_run_id is None:
        release_job = require_successful_job(source_inventory, "create-release", source_attempt, "Create Release")
        if release_job.get("head_sha") != source_sha:
            raise ValueError("Direct release job does not match the release tag source")
        core_run = run
        platform_reuse_asset_expected = False
    else:
        core_run = api(f"actions/runs/{assembly_run_id}/attempts/{assembly_attempt}")
        expected_assembly_title = f"{RELEASE_TITLE_PREFIX}{tag}:assembly:source:{source_run_id}"
        title_pattern = re.escape(expected_assembly_title) + (
            r"(?::(?:qualification:[1-9][0-9]*(?::(?:rebuild-tvos|assembly:[1-9][0-9]*))?|macos:[1-9][0-9]*))?"
        )
        if (
            core_run.get("head_repository", {}).get("full_name", "").lower() != REPOSITORY
            or core_run.get("head_branch") != "main"
            or core_run.get("event") != "workflow_dispatch"
            or core_run.get("path", "").split("@", 1)[0] != WORKFLOW_PATH
            or core_run.get("run_attempt") != assembly_attempt
            or core_run.get("status") != "completed"
            or core_run.get("conclusion") != "success"
            or not re.fullmatch(title_pattern, core_run.get("display_title", ""))
        ):
            raise ValueError("Assembly run is not the successful public release assembly for this source run")
        assembly_inventory = attempt_jobs(assembly_run_id, assembly_attempt)
        release_job = require_successful_job(assembly_inventory, "create-release", assembly_attempt, "Create Release")
        if release_job.get("head_sha") != core_run.get("head_sha"):
            raise ValueError("Release assembly job does not match its workflow source")
        platform_reuse_asset_expected = True

    current_sha = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", current_sha):
        raise ValueError("Current workflow source commit is invalid")
    comparison = api(f"compare/{source_sha}...{current_sha}")
    if comparison.get("status") not in {"ahead", "identical"}:
        raise ValueError("Release source is not an ancestor of the current main workflow")

    version_file = api(f"contents/pubspec.yaml?ref={source_sha}")
    source_text = base64.b64decode(version_file["content"]).decode("utf-8")
    version_match = re.search(r"(?m)^version:\s*([0-9]+\.[0-9]+\.[0-9]+\+[0-9]+)\s*$", source_text)
    if not version_match or version_match.group(1).split("+", 1)[0] != version:
        raise ValueError("Release source pubspec version does not match the stable tag")
    app_build = version_match.group(1).split("+", 1)[1]

    release = api(f"releases/tags/{tag}")
    if (
        release.get("tag_name") != tag
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or not release.get("published_at")
    ):
        raise ValueError("Homebrew recovery requires an exact published stable release")
    asset_map: dict[str, dict] = {}
    for asset in release.get("assets", []):
        name = asset.get("name")
        if name in asset_map:
            raise ValueError(f"Published release contains duplicate asset name: {name}")
        asset_map[name] = asset
    for name in (ORIGINAL_DMG, "SHA256SUMS.txt"):
        if name not in asset_map:
            raise ValueError(f"Published release is missing {name}")
    if not allow_recovery_assets and any(name in asset_map for name in ASSET_NAMES):
        raise ValueError("Refusing to replace or mix with existing Homebrew recovery assets")
    has_platform_reuse = "platform-reuse-provenance.json" in asset_map
    if has_platform_reuse != platform_reuse_asset_expected:
        if has_platform_reuse:
            raise ValueError("Platform-reuse release requires its explicit assembly run identity")
        raise ValueError("Assembly recovery requires published platform-reuse provenance")

    checkpoint_name = f"{CHECKPOINT_PREFIX}{source_run_id}-{source_attempt}"
    artifacts = api(f"actions/runs/{source_run_id}/artifacts?per_page=100").get("artifacts", [])
    matches = [item for item in artifacts if item.get("name") == checkpoint_name]
    if (
        len(matches) != 1
        or matches[0].get("expired") is not False
        or (matches[0].get("workflow_run") or {}).get("id") != source_run_id
        or (matches[0].get("workflow_run") or {}).get("head_sha") != source_sha
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(matches[0].get("digest", "")))
    ):
        raise ValueError("Exact trusted Mac compilation checkpoint is missing, expired, or ambiguous")

    return {
        "version": version,
        "app_build": app_build,
        "source_sha": source_sha,
        "run": run,
        "macos_job": macos_job,
        "release_job": release_job,
        "core_run": core_run,
        "assembly_run_id": assembly_run_id,
        "assembly_attempt": assembly_attempt,
        "release": release,
        "assets": asset_map,
        "checkpoint_name": checkpoint_name,
        "checkpoint_artifact": matches[0],
    }


def verify_core_download(
    release: dict,
    asset_map: dict,
    core_run: dict,
    directory: Path,
    *,
    include_platform_reuse: bool,
) -> tuple[Path, Path, Path | None]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    patterns = ["--pattern", ORIGINAL_DMG, "--pattern", "SHA256SUMS.txt"]
    if include_platform_reuse:
        patterns.extend(["--pattern", "platform-reuse-provenance.json"])
    subprocess.run(
        ["gh", "release", "download", release["tag_name"], "--repo", REPOSITORY,
         *patterns, "--dir", str(directory)],
        check=True,
    )
    checksums = directory / "SHA256SUMS.txt"
    dmg = directory / ORIGINAL_DMG
    verify_build_attestation(checksums, core_run)
    expected = manifest_digest(checksums, ORIGINAL_DMG)
    remote_digest = str(asset_map[ORIGINAL_DMG].get("digest", ""))
    if (
        not re.fullmatch(r"sha256:[0-9a-f]{64}", remote_digest)
        or remote_digest[7:] != expected
        or sha256(dmg) != expected
    ):
        raise ValueError("Published core DMG does not match its attested checksum and immutable release digest")
    manifest_remote = str(asset_map["SHA256SUMS.txt"].get("digest", ""))
    if manifest_remote and manifest_remote != f"sha256:{sha256(checksums)}":
        raise ValueError("Published core checksum manifest differs from the immutable release asset")
    platform_reuse = directory / "platform-reuse-provenance.json" if include_platform_reuse else None
    if platform_reuse is not None:
        expected = manifest_digest(checksums, "platform-reuse-provenance.json")
        remote = str(asset_map["platform-reuse-provenance.json"].get("digest", ""))
        if (
            sha256(platform_reuse) != expected
            or remote != f"sha256:{expected}"
        ):
            raise ValueError("Published platform-reuse proof differs from its attested manifest and asset digest")
        # The signed checksum manifest binds the exact platform-reuse proof digest.
    return checksums, dmg, platform_reuse


def verify_platform_reuse(path: Path, facts: dict) -> None:
    proof = json.loads(path.read_text(encoding="utf-8"))
    source_run = facts["run"]
    assembly = facts["core_run"]
    qualification = re.search(
        r":qualification:([1-9][0-9]*)(?::assembly:([1-9][0-9]*)|:rebuild-tvos)?$",
        assembly.get("display_title", ""),
    )
    expected_head = facts["source_sha"] if qualification else assembly.get("head_sha")
    if (
        proof.get("source_run_id") != source_run.get("id")
        or proof.get("source_sha") != facts["source_sha"]
        or proof.get("version") != facts["version"]
        or proof.get("macos_cask_qualified") is not False
        or "build-macos" not in proof.get("reused_jobs", [])
        or proof.get("head_sha") != expected_head
        or (qualification and proof.get("qualification_run_id") != int(qualification[1]))
    ):
        raise ValueError("Attested platform-reuse proof does not bind the exact Mac source and assembly")
    if qualification and qualification[2] and (
        proof.get("qualification_assembly_run_id") != int(qualification[2])
        or proof.get("assembly_run_id") != assembly.get("id")
        or proof.get("actual_checkout_sha") != assembly.get("head_sha")
    ):
        raise ValueError("Platform-reuse proof identifies a different assembly continuation")
    current = api(f"actions/runs/{source_run['id']}")
    current_attempt = current.get("run_attempt")
    if (
        type(current_attempt) is not int
        or type(proof.get("run_attempt")) is not int
        or proof.get("run_attempt") != current_attempt
        or current.get("head_sha") != facts["source_sha"]
        or type(source_run.get("run_attempt")) is not int
        or source_run["run_attempt"] > current_attempt
    ):
        raise ValueError("Platform-reuse proof identifies a different source-run attempt history")


def prepare(args: argparse.Namespace) -> None:
    if sys.platform != "darwin":
        raise ValueError("Published macOS recovery requires a native macOS runner")
    if (
        os.environ.get("GITHUB_REPOSITORY", "").lower() != REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
    ):
        raise ValueError("Stable Homebrew recovery is restricted to the canonical main workflow")
    for path in (args.output_app, args.output_receipt, args.release_dir, args.checkpoint_dir):
        if path.exists() or path.is_symlink():
            raise ValueError(f"Refusing to overwrite recovery output: {path.name}")

    facts = verify_direct_source(
        args.tag, args.source_run_id, args.source_attempt,
        assembly_run_id=args.assembly_run_id,
        assembly_attempt=args.assembly_attempt,
    )
    checksums, dmg, platform_reuse = verify_core_download(
        facts["release"], facts["assets"], facts["core_run"], args.release_dir,
        include_platform_reuse=facts["assembly_run_id"] is not None,
    )
    if platform_reuse is not None:
        verify_platform_reuse(platform_reuse, facts)
    restored_app, checkpoint = restore_portable_checkpoint(
        facts["run"], facts["macos_job"], facts["source_sha"],
        args.source_attempt, args.checkpoint_dir,
    )
    checkpoint_artifact = facts["checkpoint_artifact"]
    if (
        checkpoint.get("source_sha") != facts["source_sha"]
        or checkpoint.get("run_id") != args.source_run_id
        or checkpoint.get("run_attempt") != args.source_attempt
        or checkpoint.get("signed") is not False
        or checkpoint.get("notarized") is not False
        or checkpoint.get("archive_sha256") == ""
    ):
        raise ValueError("Mac checkpoint receipt does not identify the exact unsigned source compilation")
    if app_version(restored_app) != (facts["version"], facts["app_build"]):
        raise ValueError("Mac checkpoint app version differs from the tagged product source")

    args.output_app.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.release_dir.parent, prefix="pleazyng-homebrew-mount-") as temporary:
        mountpoint = Path(temporary) / "mounted"
        mountpoint.mkdir()
        attached = False
        try:
            subprocess.run(
                ["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mountpoint), str(dmg)],
                check=True,
            )
            attached = True
            published_app = mountpoint / "Pleazyng.app"
            if app_version(published_app) != (facts["version"], facts["app_build"]):
                raise ValueError("Published core DMG contains a different Pleazyng version")
            published_tree = tree_digest(published_app)
            if published_tree != tree_digest(restored_app):
                raise ValueError("Published core DMG app differs from the trusted native compilation checkpoint")
            subprocess.run(["ditto", str(published_app), str(args.output_app)], check=True)
        finally:
            if attached:
                subprocess.run(["hdiutil", "detach", str(mountpoint)], check=True)
    if tree_digest(args.output_app) != published_tree:
        raise ValueError("Recovered signing input differs from the app inside the published core DMG")

    recovery_job = facts["macos_job"]
    creation_job = facts["release_job"]
    run = facts["run"]
    recovery_kind = "platform-assembly" if platform_reuse is not None else "direct-source"
    receipt = {
        "schema_version": 2,
        "recovery_kind": recovery_kind,
        "release_tag": args.tag,
        "release_version": facts["version"],
        "release_source_sha": facts["source_sha"],
        "source_asset_name": ORIGINAL_DMG,
        "source_asset_sha256": manifest_digest(checksums, ORIGINAL_DMG),
        "core_checksums_sha256": sha256(checksums),
        "core_release": {
            "release_id": facts["release"]["id"],
            "published_at": facts["release"]["published_at"],
            "source_run_id": args.source_run_id,
            "source_run_attempt": args.source_attempt,
            "source_run_sha": run["head_sha"],
            "source_run_title": run["display_title"],
            "release_run_id": facts["core_run"]["id"],
            "release_run_attempt": facts["core_run"]["run_attempt"],
            "release_run_sha": facts["core_run"]["head_sha"],
            "release_run_title": facts["core_run"]["display_title"],
            "assembly_run_id": facts["assembly_run_id"],
            "assembly_run_attempt": facts["assembly_attempt"],
            "release_job_id": creation_job["id"],
        },
        "source_build": {
            "run_id": args.source_run_id,
            "run_attempt": args.source_attempt,
            "head_sha": facts["source_sha"],
            "macos_job_id": recovery_job["id"],
            "compile_checkpoint": {
                "artifact_name": facts["checkpoint_name"],
                "artifact_sha256": checkpoint_artifact["digest"][7:],
                "archive_sha256": checkpoint["archive_sha256"],
                "app_tree_sha256": checkpoint["app_tree_sha256"],
            },
        },
        "platform_reuse_sha256": sha256(platform_reuse) if platform_reuse is not None else None,
        "original_app_tree_sha256": published_tree,
    }
    args.output_receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with args.output_receipt.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as environment:
        environment.write(f"COMPILE_SOURCE_SHA={facts['source_sha']}\n")
        environment.write(f"RELEASE_VERSION={facts['version']}\n")
    print(f"Recovered the exact published {facts['version']} app from source run {args.source_run_id}.")


def resolve(args: argparse.Namespace) -> None:
    facts = verify_direct_source(
        args.tag, args.source_run_id, args.source_attempt,
        assembly_run_id=args.assembly_run_id,
        assembly_attempt=args.assembly_attempt,
    )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"source_sha={facts['source_sha']}\n")
        output.write(f"version={facts['version']}\n")
    print(f"Resolved stable release {args.tag} to source {facts['source_sha']}.")


def finalize(args: argparse.Namespace) -> None:
    prepared = json.loads(args.prepared_receipt.read_text(encoding="utf-8"))
    if (
        prepared.get("schema_version") != 2
        or prepared.get("recovery_kind") not in {"direct-source", "platform-assembly"}
        or stable_version(prepared.get("release_tag", "")) != args.version
        or prepared.get("release_version") != args.version
        or prepared.get("source_asset_name") != ORIGINAL_DMG
        or not re.fullmatch(r"[0-9a-f]{40}", str(prepared.get("release_source_sha", "")))
    ):
        raise ValueError("Prepared receipt does not identify a supported stable release")
    if args.notary_status != "Accepted" or not args.stapled or not args.gatekeeper_assessed:
        raise ValueError("Homebrew recovery requires accepted notarization, stapling, and Gatekeeper assessment")
    if not args.asset.is_file() or args.asset.is_symlink():
        raise ValueError("Notarized Homebrew DMG must be a regular file")
    digest = sha256(args.asset)
    receipt = {
        **prepared,
        "signing": {
            "bundle_id": BUNDLE_ID,
            "team_id": TEAM_ID,
            "notarization_status": args.notary_status,
            "app_ticket_stapled": True,
            "gatekeeper_assessed": True,
        },
        "recovery_workflow": {
            "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "workflow_sha": os.environ["GITHUB_SHA"],
        },
        "supplemental_asset": {
            "name": SIGNED_DMG,
            "sha256": digest,
            "size_bytes": args.asset.stat().st_size,
        },
    }
    with args.output_receipt.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    with args.checksums.open("x", encoding="utf-8") as stream:
        stream.write(f"{digest}  {SIGNED_DMG}\n")
    print(f"Recorded notarized recovery asset {SIGNED_DMG} ({digest}).")


def verify(args: argparse.Namespace) -> None:
    version = stable_version(args.tag)
    if args.version != version:
        raise ValueError("Tag and release version do not match")
    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    core_release = receipt.get("core_release", {})
    source_run_id = core_release.get("source_run_id")
    source_run_attempt = core_release.get("source_run_attempt")
    assembly_run_id = core_release.get("assembly_run_id")
    assembly_attempt = core_release.get("assembly_run_attempt")
    if (type(source_run_id) is not int or source_run_id < 1
            or type(source_run_attempt) is not int or source_run_attempt < 1
            or (assembly_run_id is None) != (assembly_attempt is None)
            or (assembly_run_id is not None and
                (type(assembly_run_id) is not int or assembly_run_id < 1
                 or type(assembly_attempt) is not int or assembly_attempt < 1))):
        raise ValueError("Recovery receipt omits exact source or assembly run identities")
    is_assembly = assembly_run_id is not None
    expected_kind = "platform-assembly" if is_assembly else "direct-source"
    if receipt.get("recovery_kind") != expected_kind:
        raise ValueError("Recovery receipt kind does not match its source and assembly identities")
    if is_assembly != (args.platform_reuse is not None):
        raise ValueError("Platform-reuse proof must be supplied exactly for an assembled release")
    if (
        receipt.get("schema_version") != 2
        or receipt.get("release_tag") != args.tag
        or receipt.get("release_version") != version
        or receipt.get("source_asset_name") != ORIGINAL_DMG
        or receipt.get("core_checksums_sha256") != sha256(args.core_checksums)
    ):
        raise ValueError("Recovery receipt does not identify the release and core checksum manifest")
    if manifest_digest(args.core_checksums, ORIGINAL_DMG) != receipt.get("source_asset_sha256"):
        raise ValueError("Core release checksum manifest differs from the recovery receipt")
    facts = verify_direct_source(
        args.tag,
        source_run_id,
        source_run_attempt,
        assembly_run_id=assembly_run_id,
        assembly_attempt=assembly_attempt,
        allow_recovery_assets=True,
    )
    verify_build_attestation(args.core_checksums, facts["core_run"])
    if facts["assets"]["SHA256SUMS.txt"].get("digest") != f"sha256:{sha256(args.core_checksums)}":
        raise ValueError("Published core checksum asset differs from the attested manifest")
    if (
        facts["source_sha"] != receipt.get("release_source_sha")
        or facts["run"].get("display_title") != core_release.get("source_run_title")
        or facts["run"].get("head_sha") != core_release.get("source_run_sha")
        or facts["macos_job"].get("id") != receipt.get("source_build", {}).get("macos_job_id")
    ):
        raise ValueError("Recovery receipt does not retain the exact successful Mac source job")
    expected_core_run_id = assembly_run_id if is_assembly else source_run_id
    expected_core_run_attempt = assembly_attempt if is_assembly else source_run_attempt
    if (facts["core_run"].get("id") != expected_core_run_id
            or facts["core_run"].get("run_attempt") != expected_core_run_attempt
            or facts["core_run"].get("id") != core_release.get("release_run_id")
            or facts["core_run"].get("run_attempt") != core_release.get("release_run_attempt")
            or facts["core_run"].get("head_sha") != core_release.get("release_run_sha")
            or facts["core_run"].get("display_title") != core_release.get("release_run_title")
            or facts["release_job"].get("id") != core_release.get("release_job_id")):
        raise ValueError("Recovery receipt does not retain the known successful release job and provenance")
    if (core_release.get("release_id") != facts["release"].get("id")
            or core_release.get("published_at") != facts["release"].get("published_at")):
        raise ValueError("Recovery receipt identifies a different published release record")
    checkpoint = facts["checkpoint_artifact"]
    source_build = receipt.get("source_build", {})
    compile = source_build.get("compile_checkpoint", {})
    if (
        source_build.get("run_id") != facts["run"]["id"]
        or source_build.get("run_attempt") != facts["run"]["run_attempt"]
        or source_build.get("head_sha") != facts["source_sha"]
        or compile.get("artifact_name") != facts["checkpoint_name"]
        or compile.get("artifact_sha256") != checkpoint.get("digest", "").removeprefix("sha256:")
        or compile.get("app_tree_sha256") != receipt.get("original_app_tree_sha256")
        or not re.fullmatch(r"[0-9a-f]{64}", str(compile.get("archive_sha256", "")))
    ):
        raise ValueError("Receipt does not identify the exact successful native Mac checkpoint")
    if facts["assets"][ORIGINAL_DMG].get("digest") != f"sha256:{receipt['source_asset_sha256']}":
        raise ValueError("Published core DMG digest differs from the receipt")
    if is_assembly:
        proof = args.platform_reuse
        proof_digest = sha256(proof)
        if (
            proof_digest != receipt.get("platform_reuse_sha256")
            or manifest_digest(args.core_checksums, "platform-reuse-provenance.json") != proof_digest
            or facts["assets"].get("platform-reuse-provenance.json", {}).get("digest") != f"sha256:{proof_digest}"
        ):
            raise ValueError("Platform-reuse provenance differs from the signed release and recovery receipt")
        verify_platform_reuse(proof, facts)
    elif receipt.get("platform_reuse_sha256") is not None:
        raise ValueError("Direct-source recovery receipt unexpectedly contains assembly provenance")

    signing = receipt.get("signing", {})
    if (
        signing.get("bundle_id") != BUNDLE_ID
        or signing.get("team_id") != TEAM_ID
        or signing.get("notarization_status") != "Accepted"
        or signing.get("app_ticket_stapled") is not True
        or signing.get("gatekeeper_assessed") is not True
    ):
        raise ValueError("Receipt omits the required signature, notarization, stapling, or Gatekeeper evidence")
    asset = receipt.get("supplemental_asset", {})
    actual_digest = sha256(args.asset)
    if (
        asset.get("name") != SIGNED_DMG
        or asset.get("sha256") != actual_digest
        or asset.get("size_bytes") != args.asset.stat().st_size
        or manifest_digest(args.homebrew_checksums, SIGNED_DMG) != actual_digest
    ):
        raise ValueError("Notarized DMG does not match the recovery receipt and checksum manifest")
    recovery = receipt.get("recovery_workflow", {})
    if (
        type(recovery.get("run_id")) is not int or recovery.get("run_id", 0) < 1
        or type(recovery.get("run_attempt")) is not int or recovery.get("run_attempt", 0) < 1
        or not re.fullmatch(r"[0-9a-f]{40}", str(recovery.get("workflow_sha", "")))
    ):
        raise ValueError("Receipt does not identify the workflow attempt that produced the notarized asset")
    verify_recovery_attestation(args.receipt, recovery)
    verify_recovery_attestation(args.asset, recovery)
    print(f"Verified {expected_kind} {version} Homebrew source and notarized payload.")


def upload(tag: str, repository: str, directory: Path) -> list[str]:
    stable_version(tag)
    if repository.lower() != REPOSITORY:
        raise ValueError("Homebrew recovery upload is restricted to the canonical Pleazyng repository")
    local: dict[str, Path] = {}
    for name in ASSET_NAMES:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Required recovery asset is missing or unsafe: {name}")
        local[name] = path
    response = subprocess.check_output(
        ["gh", "release", "view", tag, "--repo", REPOSITORY, "--json", "assets"], text=True,
    )
    remote_items = json.loads(response).get("assets", [])
    remote: dict[str, dict] = {}
    for asset in remote_items:
        name = asset.get("name")
        if name in ASSET_NAMES:
            if name in remote:
                raise ValueError(f"Release contains duplicate Homebrew recovery asset name: {name}")
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
    commands = parser.add_subparsers(dest="operation", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--tag", required=True)
    prepare_parser.add_argument("--source-run-id", type=int, required=True)
    prepare_parser.add_argument("--source-attempt", type=int, required=True)
    prepare_parser.add_argument("--assembly-run-id", type=int)
    prepare_parser.add_argument("--assembly-attempt", type=int)
    prepare_parser.add_argument("--release-dir", type=Path, required=True)
    prepare_parser.add_argument("--checkpoint-dir", type=Path, required=True)
    prepare_parser.add_argument("--output-app", type=Path, required=True)
    prepare_parser.add_argument("--output-receipt", type=Path, required=True)
    resolve_parser = commands.add_parser("resolve")
    resolve_parser.add_argument("--tag", required=True)
    resolve_parser.add_argument("--source-run-id", type=int, required=True)
    resolve_parser.add_argument("--source-attempt", type=int, required=True)
    resolve_parser.add_argument("--assembly-run-id", type=int)
    resolve_parser.add_argument("--assembly-attempt", type=int)
    finalize_parser = commands.add_parser("finalize")
    finalize_parser.add_argument("--version", required=True)
    finalize_parser.add_argument("--prepared-receipt", type=Path, required=True)
    finalize_parser.add_argument("--asset", type=Path, required=True)
    finalize_parser.add_argument("--output-receipt", type=Path, required=True)
    finalize_parser.add_argument("--checksums", type=Path, required=True)
    finalize_parser.add_argument("--notary-status", required=True)
    finalize_parser.add_argument("--stapled", action="store_true")
    finalize_parser.add_argument("--gatekeeper-assessed", action="store_true")
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--tag", required=True)
    verify_parser.add_argument("--version", required=True)
    verify_parser.add_argument("--core-checksums", type=Path, required=True)
    verify_parser.add_argument("--platform-reuse", type=Path)
    verify_parser.add_argument("--homebrew-checksums", type=Path, required=True)
    verify_parser.add_argument("--receipt", type=Path, required=True)
    verify_parser.add_argument("--asset", type=Path, required=True)
    upload_parser = commands.add_parser("upload")
    upload_parser.add_argument("--tag", required=True)
    upload_parser.add_argument("--repo", default=REPOSITORY)
    upload_parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "prepare":
            prepare(args)
        elif args.operation == "resolve":
            resolve(args)
        elif args.operation == "finalize":
            finalize(args)
        elif args.operation == "verify":
            verify(args)
        else:
            upload(args.tag, args.repo, args.directory)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        parser.exit(1, f"Stable Homebrew recovery failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
