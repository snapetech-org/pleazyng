#!/usr/bin/env python3
"""Recover the exact v3.0.51 app into a signed Homebrew release asset."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import time

from recover_macos_app import restore_portable_checkpoint, tree_digest


REPOSITORY = "snapetech-org/pleazyng"
RELEASE_TAG = "v3.0.51"
RELEASE_VERSION = "3.0.51"
RELEASE_SOURCE_SHA = "cd7b18d7013edf93cb345738e8deeb4cc809473c"
ORIGINAL_DMG_SHA256 = "2acf7202d9eb3423a476c65a560626b347010b58477c4d6435e6d06742ebe916"
SOURCE_RUN_ID = 45421474803
SOURCE_RUN_ATTEMPT = 2
SOURCE_BUILD_ARTIFACT = "macos-compiled-app-45421474803-2"
RELEASE_ASSEMBLY_RUN_ID = 45533107145
RELEASE_ASSEMBLY_ATTEMPT = 2
RELEASE_ASSEMBLY_SHA = "747c0673fb98410d8ccb24b975ace42a931bc140"
RELEASE_ASSEMBLY_TITLE = (
    "action:release:pool:public:tag:v3.0.51:assembly:source:45421474803:qualification:45526088477"
)
SOURCE_RUN_TITLE = "action:release:pool:public:tag:v3.0.51"
ORIGINAL_DMG = "pleazyng-macos.dmg"
SIGNED_DMG = "pleazyng-macos-notarized.dmg"
SUPPLEMENTAL_CHECKSUMS = "pleazyng-homebrew-SHA256SUMS.txt"
RECOVERY_RECEIPT = "pleazyng-homebrew-recovery.json"
BUNDLE_ID = "org.snapetech.pleazyng"
TEAM_ID = "DM9A59DC85"
MAX_API_ATTEMPTS = 4


def api(path: str) -> dict:
    command = ["gh", "api", f"repos/{REPOSITORY}/{path}"]
    for attempt in range(MAX_API_ATTEMPTS):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            return json.loads(result.stdout)
        if attempt + 1 == MAX_API_ATTEMPTS or not re.search(r"HTTP 5\d\d", result.stderr):
            raise subprocess.CalledProcessError(
                result.returncode, command, output=result.stdout, stderr=result.stderr,
            )
        time.sleep(min(2 ** attempt, 8))
    raise AssertionError("bounded GitHub API retry loop ended unexpectedly")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_digest(path: Path, asset_name: str) -> str:
    pattern = re.compile(rf"^([0-9a-fA-F]{{64}})\s+{re.escape(asset_name)}$")
    matches = [
        match.group(1).lower()
        for line in path.read_text(encoding="utf-8").splitlines()
        if (match := pattern.fullmatch(line.strip()))
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one checksum entry for {asset_name}")
    return matches[0]


def app_version(app: Path) -> tuple[str, str]:
    if app.is_symlink() or not app.is_dir() or app.suffix != ".app":
        raise ValueError("Expected a real Pleazyng.app bundle")
    values = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if values.get("CFBundleIdentifier") != BUNDLE_ID:
        raise ValueError("Released app bundle identifier does not match Pleazyng")
    return str(values.get("CFBundleShortVersionString", "")), str(values.get("CFBundleVersion", ""))


def source_build_job() -> dict:
    jobs: list[dict] = []
    total_count: int | None = None
    page = 1
    while page <= 10:
        inventory = api(
            f"actions/runs/{SOURCE_RUN_ID}/attempts/{SOURCE_RUN_ATTEMPT}/jobs?per_page=30&page={page}"
        )
        jobs.extend(inventory.get("jobs", []))
        total_count = int(inventory.get("total_count", len(jobs)))
        if len(jobs) >= total_count or len(inventory.get("jobs", [])) < 30:
            break
        page += 1
    if total_count is not None and len(jobs) < total_count:
        raise ValueError("The v3.0.51 source build job inventory could not be fully read")
    matches = [job for job in jobs if job.get("name") == "build-macos"]
    if len(matches) != 1:
        raise ValueError("The v3.0.51 native macOS build job is missing or ambiguous")
    job = matches[0]
    if (
        job.get("status") != "completed"
        or job.get("conclusion") != "success"
        or job.get("run_attempt") != SOURCE_RUN_ATTEMPT
        or not any(step.get("name") == "Build macOS" and step.get("conclusion") == "success"
                   for step in job.get("steps", []))
    ):
        raise ValueError("The trusted v3.0.51 native macOS compilation did not succeed")
    return job


def verify_source_run() -> tuple[dict, dict, str]:
    run_info = api(f"actions/runs/{SOURCE_RUN_ID}/attempts/{SOURCE_RUN_ATTEMPT}")
    if (
        run_info.get("head_repository", {}).get("full_name", "").lower() != REPOSITORY
        or run_info.get("head_branch") != "main"
        or run_info.get("event") != "workflow_dispatch"
        or run_info.get("path", "").split("@", 1)[0] != ".github/workflows/build.yml"
        or run_info.get("head_sha") != RELEASE_SOURCE_SHA
        or run_info.get("run_attempt") != SOURCE_RUN_ATTEMPT
        or run_info.get("display_title") != SOURCE_RUN_TITLE
    ):
        raise ValueError("The trusted v3.0.51 source build identity changed")

    job = source_build_job()

    artifacts = api(f"actions/runs/{SOURCE_RUN_ID}/artifacts?per_page=100").get("artifacts", [])
    artifact_matches = [item for item in artifacts if item.get("name") == SOURCE_BUILD_ARTIFACT]
    if (
        len(artifact_matches) != 1
        or artifact_matches[0].get("expired") is not False
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact_matches[0].get("digest", "")))
        or (artifact_matches[0].get("workflow_run") or {}).get("id") != SOURCE_RUN_ID
        or (artifact_matches[0].get("workflow_run") or {}).get("head_sha") != RELEASE_SOURCE_SHA
    ):
        raise ValueError("The trusted v3.0.51 macOS compilation checkpoint is unavailable")
    return run_info, job, artifact_matches[0]["digest"][7:]


def verify_release_assembly() -> dict:
    assembly = api(f"actions/runs/{RELEASE_ASSEMBLY_RUN_ID}/attempts/{RELEASE_ASSEMBLY_ATTEMPT}")
    if (
        assembly.get("head_repository", {}).get("full_name", "").lower() != REPOSITORY
        or assembly.get("head_branch") != "main"
        or assembly.get("event") != "workflow_dispatch"
        or assembly.get("path", "").split("@", 1)[0] != ".github/workflows/build.yml"
        or assembly.get("run_attempt") != RELEASE_ASSEMBLY_ATTEMPT
        or assembly.get("head_sha") != RELEASE_ASSEMBLY_SHA
        or assembly.get("status") != "completed"
        or assembly.get("conclusion") != "success"
        or assembly.get("display_title") != RELEASE_ASSEMBLY_TITLE
    ):
        raise ValueError("The successful v3.0.51 release assembly identity changed")
    return assembly


def prepare(args: argparse.Namespace) -> None:
    if sys.platform != "darwin":
        raise ValueError("Published macOS app recovery requires a native macOS runner")
    if os.environ.get("GITHUB_REPOSITORY", "").lower() != REPOSITORY:
        raise ValueError("Recovery is restricted to the canonical Pleazyng repository")
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Recovery must run from the canonical main branch")
    if args.tag != RELEASE_TAG:
        raise ValueError(f"This focused recovery accepts only {RELEASE_TAG}")
    if (args.output_app.exists() or args.output_app.is_symlink()
            or args.output_receipt.exists() or args.output_receipt.is_symlink()):
        raise ValueError("Refusing to overwrite a recovered app or receipt")

    release = api(f"releases/tags/{RELEASE_TAG}")
    if (
        release.get("tag_name") != RELEASE_TAG
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or not release.get("published_at")
    ):
        raise ValueError("v3.0.51 must remain an exact published stable release")
    asset_by_name = {asset.get("name"): asset for asset in release.get("assets", [])}
    for name in (ORIGINAL_DMG, "SHA256SUMS.txt", "platform-reuse-provenance.json"):
        if name not in asset_by_name:
            raise ValueError(f"Published v3.0.51 release is missing {name}")
    for name in (SIGNED_DMG, SUPPLEMENTAL_CHECKSUMS, RECOVERY_RECEIPT, "snapetech-pleazyng.rb"):
        if name in asset_by_name:
            raise ValueError(f"Refusing to replace an existing release asset: {name}")

    tag_ref = api(f"git/ref/tags/{RELEASE_TAG}")["object"]
    while tag_ref.get("type") == "tag":
        tag_ref = api(f"git/tags/{tag_ref['sha']}")["object"]
    if tag_ref.get("type") != "commit" or tag_ref.get("sha") != RELEASE_SOURCE_SHA:
        raise ValueError("v3.0.51 no longer points at the qualified source commit")
    current_head = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", current_head):
        raise ValueError("The current workflow source commit is invalid")
    comparison = api(f"compare/{RELEASE_SOURCE_SHA}...{current_head}")
    if comparison.get("status") not in {"ahead", "identical"}:
        raise ValueError("The qualified release source is no longer an ancestor of main")

    run_info, job, checkpoint_digest = verify_source_run()
    assembly = verify_release_assembly()
    args.release_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    subprocess.run(
        ["gh", "release", "download", RELEASE_TAG, "--repo", REPOSITORY,
         "--pattern", ORIGINAL_DMG, "--pattern", "SHA256SUMS.txt",
         "--pattern", "platform-reuse-provenance.json", "--dir", str(args.release_dir)],
        check=True,
    )
    core_checksums = args.release_dir / "SHA256SUMS.txt"
    subprocess.run(
        [sys.executable, str(Path(__file__).with_name("verify_release_attestation.py")),
         str(core_checksums), "--repo", REPOSITORY],
        check=True,
    )
    original_dmg = args.release_dir / ORIGINAL_DMG
    if manifest_digest(core_checksums, ORIGINAL_DMG) != ORIGINAL_DMG_SHA256:
        raise ValueError("The attested v3.0.51 DMG checksum differs from the qualified release")
    if sha256(original_dmg) != ORIGINAL_DMG_SHA256:
        raise ValueError("Downloaded v3.0.51 DMG does not match its attested checksum")
    if asset_by_name[ORIGINAL_DMG].get("digest") != f"sha256:{ORIGINAL_DMG_SHA256}":
        raise ValueError("GitHub's immutable v3.0.51 DMG digest differs from SHA256SUMS.txt")

    reuse_path = args.release_dir / "platform-reuse-provenance.json"
    if sha256(reuse_path) != manifest_digest(core_checksums, "platform-reuse-provenance.json"):
        raise ValueError("Platform reuse provenance does not match the attested core checksum manifest")
    reuse = json.loads(reuse_path.read_text(encoding="utf-8"))
    if (
        reuse.get("source_run_id") != SOURCE_RUN_ID
        or reuse.get("source_sha") != RELEASE_SOURCE_SHA
        or reuse.get("version") != RELEASE_VERSION
        or reuse.get("run_attempt") != 3
        or reuse.get("macos_cask_qualified") is not False
        or "build-macos" not in reuse.get("reused_jobs", [])
    ):
        raise ValueError("Release assembly provenance does not identify the expected unsigned macOS build")

    source_file = api(f"contents/pubspec.yaml?ref={RELEASE_SOURCE_SHA}")
    source_text = base64.b64decode(source_file["content"]).decode("utf-8")
    version_match = re.search(r"(?m)^version:\s*([^\s#]+)", source_text)
    if not version_match or version_match.group(1) != "3.0.51+210":
        raise ValueError("The v3.0.51 release source version changed")

    checkpoint_root = args.release_dir.parent / "compiled-checkpoint"
    restored_app, checkpoint_receipt = restore_portable_checkpoint(
        run_info, job, RELEASE_SOURCE_SHA, SOURCE_RUN_ATTEMPT, checkpoint_root,
    )
    if (
        checkpoint_receipt.get("source_sha") != RELEASE_SOURCE_SHA
        or checkpoint_receipt.get("run_id") != SOURCE_RUN_ID
        or checkpoint_receipt.get("run_attempt") != SOURCE_RUN_ATTEMPT
        or checkpoint_receipt.get("archive_sha256") == ""
    ):
        raise ValueError("Trusted macOS checkpoint receipt is incomplete")
    expected_version = (RELEASE_VERSION, "210")
    if app_version(restored_app) != expected_version:
        raise ValueError("Trusted compilation checkpoint contains a different app version")

    args.output_app.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.release_dir.parent, prefix="pleazyng-homebrew-mount-") as temporary:
        mountpoint = Path(temporary) / "mounted"
        mountpoint.mkdir()
        attached = False
        try:
            subprocess.run(
                ["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mountpoint),
                 str(original_dmg)],
                check=True,
            )
            attached = True
            published_app = mountpoint / "Pleazyng.app"
            if app_version(published_app) != expected_version:
                raise ValueError("Published DMG contains a different Pleazyng version")
            original_tree = tree_digest(published_app)
            if original_tree != tree_digest(restored_app):
                raise ValueError("Published DMG app tree differs from its trusted native compilation checkpoint")
            subprocess.run(["ditto", str(published_app), str(args.output_app)], check=True)
        finally:
            if attached:
                subprocess.run(["hdiutil", "detach", str(mountpoint)], check=True)
    if tree_digest(args.output_app) != original_tree:
        raise ValueError("Recovered app copy differs from the verified published app tree")

    prepared = {
        "schema_version": 1,
        "release_tag": RELEASE_TAG,
        "release_version": RELEASE_VERSION,
        "release_source_sha": RELEASE_SOURCE_SHA,
        "source_asset_name": ORIGINAL_DMG,
        "source_asset_sha256": ORIGINAL_DMG_SHA256,
        "core_checksums_sha256": sha256(core_checksums),
        "platform_reuse_provenance_sha256": sha256(reuse_path),
        "release_assembly": {
            "run_id": RELEASE_ASSEMBLY_RUN_ID,
            "run_attempt": RELEASE_ASSEMBLY_ATTEMPT,
            "head_sha": assembly.get("head_sha"),
            "display_title": assembly.get("display_title"),
        },
        "source_build": {
            "run_id": SOURCE_RUN_ID,
            "run_attempt": SOURCE_RUN_ATTEMPT,
            "head_sha": run_info.get("head_sha"),
            "macos_job_id": job.get("id"),
            "compile_checkpoint": {
                "artifact_name": SOURCE_BUILD_ARTIFACT,
                "artifact_sha256": checkpoint_digest,
                "archive_sha256": checkpoint_receipt["archive_sha256"],
                "app_tree_sha256": checkpoint_receipt["app_tree_sha256"],
            },
        },
        "original_app_tree_sha256": original_tree,
    }
    args.output_receipt.parent.mkdir(parents=True, exist_ok=True)
    with args.output_receipt.open("x", encoding="utf-8") as stream:
        json.dump(prepared, stream, indent=2)
        stream.write("\n")
    print(f"Recovered the exact published {RELEASE_VERSION} app and matched its native build checkpoint.")


def finalize(args: argparse.Namespace) -> None:
    prepared = json.loads(args.prepared_receipt.read_text(encoding="utf-8"))
    if (
        prepared.get("schema_version") != 1
        or prepared.get("release_tag") != RELEASE_TAG
        or prepared.get("release_source_sha") != RELEASE_SOURCE_SHA
        or args.notary_status != "Accepted"
        or not args.stapled
        or not args.gatekeeper_assessed
        or os.environ.get("APPLE_TEAM_ID") != TEAM_ID
    ):
        raise ValueError("Recovery preparation, notarization, stapling or Gatekeeper verification is incomplete")
    if (args.output_receipt.exists() or args.output_receipt.is_symlink()
            or args.checksums.exists() or args.checksums.is_symlink()):
        raise ValueError("Refusing to overwrite a Homebrew recovery receipt or checksum manifest")
    digest = sha256(args.asset)
    receipt = {
        **prepared,
        "signing": {
            "bundle_id": BUNDLE_ID,
            "team_id": TEAM_ID,
            "notarization_status": "Accepted",
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
    args.output_receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    with args.checksums.open("x", encoding="utf-8") as stream:
        stream.write(f"{digest}  {SIGNED_DMG}\n")
    print(f"Recorded notarized recovery asset {SIGNED_DMG} ({digest}).")


def verify(args: argparse.Namespace) -> None:
    if args.tag != RELEASE_TAG or args.version != RELEASE_VERSION:
        raise ValueError("Supplemental Homebrew assets are restricted to the exact v3.0.51 release")
    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    if (
        receipt.get("schema_version") != 1
        or receipt.get("release_tag") != RELEASE_TAG
        or receipt.get("release_version") != RELEASE_VERSION
        or receipt.get("release_source_sha") != RELEASE_SOURCE_SHA
        or receipt.get("source_asset_name") != ORIGINAL_DMG
        or receipt.get("source_asset_sha256") != ORIGINAL_DMG_SHA256
        or receipt.get("core_checksums_sha256") != sha256(args.core_checksums)
        or receipt.get("platform_reuse_provenance_sha256") != sha256(args.platform_reuse)
        or receipt.get("original_app_tree_sha256") is None
    ):
        raise ValueError("Recovery receipt does not identify the qualified v3.0.51 source assets")
    if manifest_digest(args.core_checksums, ORIGINAL_DMG) != ORIGINAL_DMG_SHA256:
        raise ValueError("Core release checksum manifest does not identify the original v3.0.51 DMG")
    if sha256(args.platform_reuse) != manifest_digest(args.core_checksums, "platform-reuse-provenance.json"):
        raise ValueError("Platform reuse provenance is not bound to the attested core manifest")
    reuse = json.loads(args.platform_reuse.read_text(encoding="utf-8"))
    if (
        reuse.get("source_run_id") != SOURCE_RUN_ID
        or reuse.get("source_sha") != RELEASE_SOURCE_SHA
        or reuse.get("version") != RELEASE_VERSION
        or reuse.get("run_attempt") != 3
        or reuse.get("macos_cask_qualified") is not False
        or "build-macos" not in reuse.get("reused_jobs", [])
    ):
        raise ValueError("Platform reuse provenance identifies a different source or macOS build")
    assembly = receipt.get("release_assembly", {})
    source_build = receipt.get("source_build", {})
    if (
        assembly.get("run_id") != RELEASE_ASSEMBLY_RUN_ID
        or assembly.get("run_attempt") != RELEASE_ASSEMBLY_ATTEMPT
        or assembly.get("head_sha") != RELEASE_ASSEMBLY_SHA
        or assembly.get("display_title") != RELEASE_ASSEMBLY_TITLE
        or source_build.get("run_id") != SOURCE_RUN_ID
        or source_build.get("run_attempt") != SOURCE_RUN_ATTEMPT
        or source_build.get("head_sha") != RELEASE_SOURCE_SHA
    ):
        raise ValueError("Recovery receipt does not retain the trusted assembly and compilation identities")
    signing = receipt.get("signing", {})
    if (
        signing.get("bundle_id") != BUNDLE_ID
        or signing.get("team_id") != TEAM_ID
        or signing.get("notarization_status") != "Accepted"
        or signing.get("app_ticket_stapled") is not True
        or signing.get("gatekeeper_assessed") is not True
    ):
        raise ValueError("Recovery receipt does not record the required notarization and Gatekeeper checks")
    asset = receipt.get("supplemental_asset", {})
    actual_digest = sha256(args.asset)
    if (
        asset.get("name") != SIGNED_DMG
        or asset.get("sha256") != actual_digest
        or asset.get("size_bytes") != args.asset.stat().st_size
        or manifest_digest(args.homebrew_checksums, SIGNED_DMG) != actual_digest
    ):
        raise ValueError("Signed Homebrew DMG does not match its attested recovery receipt and checksum manifest")
    compile_info = receipt.get("source_build", {}).get("compile_checkpoint", {})
    if (
        compile_info.get("artifact_name") != SOURCE_BUILD_ARTIFACT
        or compile_info.get("app_tree_sha256") != receipt.get("original_app_tree_sha256")
        or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("original_app_tree_sha256", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(compile_info.get("artifact_sha256", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(compile_info.get("archive_sha256", "")))
    ):
        raise ValueError("Recovery receipt omits the trusted native compilation checkpoint identity")
    recovery = receipt.get("recovery_workflow", {})
    if (
        type(recovery.get("run_id")) is not int or recovery.get("run_id", 0) < 1
        or type(recovery.get("run_attempt")) is not int or recovery.get("run_attempt", 0) < 1
        or not re.fullmatch(r"[0-9a-f]{40}", str(recovery.get("workflow_sha", "")))
    ):
        raise ValueError("Recovery receipt does not identify the workflow run that produced the asset")
    print(f"Verified {RELEASE_VERSION} signed Homebrew payload and its source chain.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)

    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--tag", required=True)
    prepare_parser.add_argument("--release-dir", type=Path, required=True)
    prepare_parser.add_argument("--output-app", type=Path, required=True)
    prepare_parser.add_argument("--output-receipt", type=Path, required=True)

    finalize_parser = commands.add_parser("finalize")
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
    verify_parser.add_argument("--homebrew-checksums", type=Path, required=True)
    verify_parser.add_argument("--platform-reuse", type=Path, required=True)
    verify_parser.add_argument("--receipt", type=Path, required=True)
    verify_parser.add_argument("--asset", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.operation == "prepare":
            prepare(args)
        elif args.operation == "finalize":
            finalize(args)
        else:
            verify(args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        parser.exit(1, f"macOS Homebrew recovery failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
