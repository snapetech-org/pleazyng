#!/usr/bin/env python3
"""Verify and unpack an immutable Mac App Store package for screenshot capture."""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
SCRIPTS_ROOT = SCRIPT_PATH.parents[1]
sys.path.insert(0, str(SCRIPTS_ROOT))
import capture_apple_store_screenshots as capture_tools


REPOSITORY = "snapetech-org/pleazyng"
PRODUCER_WORKFLOW_PATH = ".github/workflows/macos-app-store-signed.yml"
APPSTORE_JOB = "Build and upload signed Mac App Store package"
DELIVERY_JOB = "Deliver verified Mac App Store package"
TEAM_IDENTIFIER = "DM9A59DC85"
APP_CERTIFICATE_SHA1 = "B7D31D8572209681D4C9E655F448708269BAE53B"
INSTALLER_CERTIFICATE_SHA1 = "1A0DABC17F7F2ED2E517B2CCA49A14A88156510F"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def require_regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    return path.resolve(strict=True)


def select_producer(
    *, run: dict[str, Any], attempt: dict[str, Any], jobs: dict[str, Any], artifacts: dict[str, Any],
    run_id: str, run_attempt: str, workflow_id: str, source_sha: str, source_tag: str,
    version: str, build: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[1-9][0-9]*", run_id) or not re.fullmatch(r"[1-9][0-9]*", run_attempt):
        raise ValueError("Producer run ID and attempt must be positive decimal integers")
    if not re.fullmatch(r"[0-9]+", workflow_id):
        raise ValueError("Signed workflow ID must be a decimal integer")
    expected_run = {
        "id": int(run_id),
        "workflow_id": int(workflow_id),
        "path": PRODUCER_WORKFLOW_PATH,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "status": "completed",
        "conclusion": "success",
    }
    mismatches = {key: (run.get(key), expected) for key, expected in expected_run.items()
                  if run.get(key) != expected}
    expected_attempt = {
        "id": int(run_id),
        "run_attempt": int(run_attempt),
        "head_sha": run.get("head_sha"),
        "status": "completed",
        "conclusion": "success",
    }
    mismatches.update({f"attempt.{key}": (attempt.get(key), expected)
                       for key, expected in expected_attempt.items()
                       if attempt.get(key) != expected})
    if not re.fullmatch(r"[0-9a-f]{40}", str(run.get("head_sha", ""))):
        mismatches["head_sha"] = (run.get("head_sha"), "40-character commit")
    head_repository = run.get("head_repository")
    if not isinstance(head_repository, dict) or head_repository.get("full_name") != REPOSITORY:
        mismatches["head_repository"] = (head_repository, REPOSITORY)
    if mismatches:
        raise ValueError(f"Signed Mac App Store producer run does not match the requested successful main dispatch: {mismatches}")

    job_items = jobs.get("jobs")
    if not isinstance(job_items, list):
        raise ValueError("Producer attempt jobs response must contain a jobs array")
    successful_jobs = {job.get("name") for job in job_items
                       if isinstance(job, dict) and job.get("conclusion") == "success"}
    required_jobs = {APPSTORE_JOB, DELIVERY_JOB}
    if not required_jobs.issubset(successful_jobs):
        raise ValueError(f"Both signed package creation and delivery jobs must succeed: {sorted(successful_jobs)}")

    expected_name = f"pleazyng-macos-appstore-v{version}-{run_id}"
    artifact_items = artifacts.get("artifacts")
    matches = [item for item in artifact_items or []
               if isinstance(item, dict) and item.get("name") == expected_name]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one retained signed package artifact {expected_name!r}; found {len(matches)}")
    artifact = matches[0]
    if artifact.get("expired") is not False or not isinstance(artifact.get("size_in_bytes"), int) or artifact["size_in_bytes"] <= 0:
        raise ValueError("The exact signed package artifact is expired or empty")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(artifact.get("digest", ""))):
        raise ValueError("The exact signed package artifact has no GitHub SHA-256 digest")
    workflow_run = artifact.get("workflow_run")
    if isinstance(workflow_run, dict):
        for key, expected in (("id", int(run_id)), ("head_sha", run["head_sha"]), ("head_branch", "main")):
            if workflow_run.get(key) != expected:
                raise ValueError(f"Artifact workflow_run.{key} does not match the verified producer run")

    if not re.fullmatch(r"[0-9a-f]{40}", source_sha) or source_tag != f"v{version}-candidate.{source_sha}":
        raise ValueError("Product source SHA and annotated candidate tag are inconsistent")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None or re.fullmatch(r"[0-9]+", build) is None:
        raise ValueError("Product version/build is malformed")
    return {
        "schema_version": 1,
        "repository": REPOSITORY,
        "producer_workflow_path": PRODUCER_WORKFLOW_PATH,
        "producer_workflow_id": int(workflow_id),
        "producer_workflow_sha": run["head_sha"],
        "producer_run_id": run_id,
        "producer_run_attempt": run_attempt,
        "producer_event": "workflow_dispatch",
        "producer_branch": "main",
        "producer_conclusion": "success",
        "artifact_id": str(artifact["id"]),
        "artifact_name": expected_name,
        "artifact_digest": artifact["digest"],
        "artifact_size_bytes": artifact["size_in_bytes"],
        "product_source_sha": source_sha,
        "product_source_tag": source_tag,
        "product_version": version,
        "product_build": build,
    }


def run_checked(command: list[str], *, timeout: int = 180) -> str:
    result = subprocess.run(command, check=False, text=True, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{Path(command[0]).name} failed ({result.returncode}): {detail[:3000]}")
    return (result.stdout + result.stderr).strip()


def run_plist_command(command: list[str], *, timeout: int = 120) -> tuple[dict[str, Any], bytes]:
    result = subprocess.run(command, check=False, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise RuntimeError(f"{Path(command[0]).name} failed ({result.returncode}): {detail[:3000]}")
    value = plistlib.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError(f"{Path(command[0]).name} did not return a plist dictionary")
    return value, result.stdout


def _certificate_sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest().upper()


def app_executable_path(app: Path, executable_name: str) -> Path:
    app_root = app.resolve(strict=True)
    macos_dir = (app_root / "Contents" / "MacOS").resolve(strict=True)
    try:
        macos_dir.relative_to(app_root)
    except ValueError as error:
        raise ValueError("The app executable directory escapes the Pleazyng.app bundle") from error
    if (not executable_name or Path(executable_name).name != executable_name
            or "\\" in executable_name):
        raise ValueError("The app Info.plist contains an unsafe CFBundleExecutable value")
    executable = (macos_dir / executable_name).resolve(strict=True)
    try:
        executable.relative_to(macos_dir)
    except ValueError as error:
        raise ValueError("The app executable escapes Contents/MacOS") from error
    if not executable.is_file():
        raise ValueError("The app CFBundleExecutable is not a regular file")
    return executable


def _require_signed_app(app: Path, package_receipt: dict[str, Any], output: Path) -> dict[str, Any]:
    run_checked(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], timeout=180)
    details = run_checked(["/usr/bin/codesign", "-dv", "--verbose=4", str(app)])
    expected_signature = package_receipt.get("app_signature", {})
    if (f"TeamIdentifier={TEAM_IDENTIFIER}" not in details
            or f"Identifier={capture_tools.BUNDLE_ID}" not in details
            or str(expected_signature.get("app_certificate_sha1", "")).upper() != APP_CERTIFICATE_SHA1):
        raise ValueError("The extracted Mac app signature does not match the attested App Store team and certificate")
    cert_prefix = output / "app-signing-certificate-"
    run_checked(["/usr/bin/codesign", "-d", f"--extract-certificates={cert_prefix}", str(app)])
    leaf = Path(f"{cert_prefix}0")
    if not leaf.is_file() or _certificate_sha1(leaf) != APP_CERTIFICATE_SHA1:
        raise ValueError("The extracted Mac app uses a different signing certificate")
    entitlements, entitlements_bytes = run_plist_command(
        ["/usr/bin/codesign", "-d", "--entitlements", ":-", str(app)]
    )
    required_entitlements = {
        "com.apple.security.app-sandbox": True,
        "com.apple.security.cs.allow-jit": True,
        "com.apple.security.device.audio-input": True,
        "com.apple.security.files.user-selected.read-write": True,
        "com.apple.security.network.client": True,
    }
    if any(entitlements.get(key) is not value for key, value in required_entitlements.items()):
        raise ValueError("The extracted app's signed entitlements differ from the reviewed Mac App Store build")
    profile_path = app / "Contents" / "embedded.provisionprofile"
    if profile_path.is_symlink() or not profile_path.is_file():
        raise ValueError("The extracted Mac App Store app is missing its embedded provisioning profile")
    profile, _profile_bytes = run_plist_command(["/usr/bin/security", "cms", "-D", "-i", str(profile_path)])
    expected_profile = package_receipt.get("profile", {})
    profile_teams = profile.get("TeamIdentifier")
    if (profile.get("UUID") != expected_profile.get("uuid")
            or not isinstance(profile_teams, list)
            or TEAM_IDENTIFIER not in profile_teams
            or expected_profile.get("type") != "MAC_APP_STORE"):
        raise ValueError("The extracted app's provisioning profile does not match the attested Mac App Store profile")
    return {
        "team_identifier": TEAM_IDENTIFIER,
        "app_certificate_sha1": APP_CERTIFICATE_SHA1,
        "entitlements_sha256": hashlib.sha256(entitlements_bytes).hexdigest(),
        "sandbox_enabled": True,
        "jit_enabled": True,
        "embedded_profile_uuid": profile["UUID"],
        "profile_team_identifiers": profile["TeamIdentifier"],
    }


def restore_package(
    *, artifact_dir: Path, selection_path: Path, output_dir: Path, source_sha: str, source_tag: str,
    version: str, build: str,
) -> dict[str, Any]:
    selection = load_json(selection_path)
    expected_selection = {
        "repository": REPOSITORY,
        "producer_workflow_path": PRODUCER_WORKFLOW_PATH,
        "producer_event": "workflow_dispatch",
        "producer_branch": "main",
        "producer_conclusion": "success",
        "product_source_sha": source_sha,
        "product_source_tag": source_tag,
        "product_version": version,
        "product_build": build,
    }
    mismatch = {key: (selection.get(key), value) for key, value in expected_selection.items()
                if selection.get(key) != value}
    if mismatch:
        raise ValueError(f"Producer selection receipt does not match the exact product inputs: {mismatch}")
    if (re.fullmatch(r"[0-9]+", str(selection.get("producer_workflow_id", ""))) is None
            or re.fullmatch(r"[0-9a-f]{40}", str(selection.get("producer_workflow_sha", ""))) is None
            or re.fullmatch(r"[1-9][0-9]*", str(selection.get("producer_run_id", ""))) is None
            or re.fullmatch(r"[1-9][0-9]*", str(selection.get("producer_run_attempt", ""))) is None
            or re.fullmatch(r"[1-9][0-9]*", str(selection.get("artifact_id", ""))) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(selection.get("artifact_digest", ""))) is None
            or selection.get("artifact_name") != f"pleazyng-macos-appstore-v{version}-{selection.get('producer_run_id')}"
            or not isinstance(selection.get("artifact_size_bytes"), int)
            or selection["artifact_size_bytes"] <= 0):
        raise ValueError("Producer selection receipt lacks an exact signed workflow/artifact identity")
    package_path = require_regular_file(artifact_dir / "pleazyng-macos-appstore.pkg", "Mac App Store package")
    package_receipt_path = require_regular_file(artifact_dir / "pleazyng-macos-appstore.json", "signed package receipt")
    package_receipt = load_json(package_receipt_path)
    expected_package = {
        "channel": "Mac App Store",
        "candidate_state": "app-store-connect-processed",
        "release_tag": f"v{version}",
        "source_commit": source_sha,
        "marketing_version": version,
        "build_number": build,
        "bundle_id": capture_tools.BUNDLE_ID,
        "platform": "MAC_OS",
        "processing_state": "VALID",
        "github_run_id": int(selection["producer_run_id"]),
        "github_run_attempt": int(selection["producer_run_attempt"]),
        "workflow_commit": selection["producer_workflow_sha"],
        "asset_filename": package_path.name,
    }
    package_mismatch = {key: (package_receipt.get(key), value) for key, value in expected_package.items()
                        if package_receipt.get(key) != value}
    package_info = package_receipt.get("package", {})
    package_sha = capture_tools.sha256_file(package_path)
    if (package_mismatch or package_info.get("filename") != package_path.name
            or package_info.get("sha256") != package_sha
            or package_receipt.get("asset_sha256") != package_sha
            or package_info.get("signed") is not True):
        raise ValueError(f"Signed Mac App Store package receipt does not match its bytes and producer: {package_mismatch}")
    qualifications = package_receipt.get("qualification", {})
    required_true = ("app_signed_for_distribution", "installer_signed_for_app_store", "app_store_connect_processed", "upload_to_app_store_connect")
    if any(qualifications.get(key) is not True for key in required_true):
        raise ValueError("The exact package receipt does not qualify a processed signed App Store upload")
    app_signature = package_receipt.get("app_signature", {})
    installer_signature = package_receipt.get("installer_signature", {})
    if (app_signature.get("team_identifier") != TEAM_IDENTIFIER
            or app_signature.get("app_certificate_sha1", "").upper() != APP_CERTIFICATE_SHA1
            or installer_signature.get("certificate_sha1", "").upper() != INSTALLER_CERTIFICATE_SHA1):
        raise ValueError("The exact package receipt has an unexpected Mac App Distribution signer")
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError(f"Refusing to overwrite Mac App Store capture restore output: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)
    expanded = output_dir / "expanded-package"
    package_signature_output = run_checked(["/usr/sbin/pkgutil", "--check-signature", str(package_path)], timeout=180)
    run_checked(["/usr/sbin/pkgutil", "--expand-full", str(package_path), str(expanded)], timeout=300)
    apps = [path for path in expanded.rglob("Pleazyng.app") if path.is_dir() and not path.is_symlink()]
    if len(apps) != 1:
        raise ValueError(f"The exact installer must contain one Pleazyng.app bundle; found {len(apps)}")
    app = apps[0].resolve(strict=True)
    identity = capture_tools.app_info(app)
    executable = app_executable_path(app, identity["executable"])
    architectures_text = run_checked(["/usr/bin/lipo", "-archs", str(executable)])
    architectures = sorted(set(architectures_text.split()))
    if architectures != sorted(package_receipt.get("application", {}).get("architectures", [])):
        raise ValueError("The extracted app's architectures differ from the verified package receipt")
    signing_evidence = _require_signed_app(app, package_receipt, output_dir)
    app_hash = capture_tools.sha256_tree(app)
    artifact = {
        key: selection[key] for key in (
            "artifact_id", "artifact_name", "artifact_digest", "artifact_size_bytes",
            "producer_run_id", "producer_run_attempt", "producer_workflow_sha",
        )
    }
    receipt = {
        "schema_version": 1,
        "capture_input_kind": "verified-macos-app-store-package",
        "product": "Pleazyng",
        "source_tag": source_tag,
        "source_sha": source_sha,
        "product_version": version,
        "product_build": build,
        "platform": "macOS",
        "build_mode": "App Store IAP",
        "build_configuration": "Release Archive",
        "app_bundle_name": "Pleazyng.app",
        "bundle_id": identity["bundle_id"],
        "executable": identity["executable"],
        "build_defines": capture_tools.IOS_MAC_STORE_DEFINES,
        "distribution_signing": "Mac App Store Distribution",
        "producer_repository": REPOSITORY,
        "producer_workflow_path": PRODUCER_WORKFLOW_PATH,
        "producer_workflow_ref": f"{REPOSITORY}/{PRODUCER_WORKFLOW_PATH}@refs/heads/main",
        "producer_workflow_sha": selection["producer_workflow_sha"],
        "producer_run_id": selection["producer_run_id"],
        "producer_run_attempt": selection["producer_run_attempt"],
        "producer_artifact": artifact,
        "producer_package_receipt_sha256": capture_tools.sha256_file(package_receipt_path),
        "package_sha256": package_sha,
        "package_signature_check": {
            "command": "/usr/sbin/pkgutil --check-signature",
            "succeeded": True,
            "output": package_signature_output,
        },
        "app_store_receipt": package_receipt,
        "app_bundle_sha256": app_hash,
        "architectures": architectures,
        "app_signature": signing_evidence,
        "profile": package_receipt["profile"],
        "source_lineage": {
            "relationship": "The exact annotated product source was compiled and signed by the Mac App Store producer workflow.",
            "product_source_sha": source_sha,
            "product_source_tag": source_tag,
            "producer_workflow_sha": selection["producer_workflow_sha"],
        },
    }
    receipt_path = output_dir / "build-provenance.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"app": str(app), "build_receipt": str(receipt_path), "app_bundle_sha256": app_hash}


def select_command(args: argparse.Namespace) -> None:
    value = select_producer(
        run=load_json(args.run_json), attempt=load_json(args.attempt_json), jobs=load_json(args.jobs_json),
        artifacts=load_json(args.artifacts_json), run_id=args.run_id, run_attempt=args.run_attempt,
        workflow_id=args.workflow_id, source_sha=args.source_sha, source_tag=args.source_tag,
        version=args.version, build=args.build,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def restore_command(args: argparse.Namespace) -> None:
    print(json.dumps(restore_package(
        artifact_dir=args.artifact_dir, selection_path=args.selection, output_dir=args.output_dir,
        source_sha=args.source_sha, source_tag=args.source_tag, version=args.version, build=args.build,
    ), sort_keys=True))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select-producer")
    for name in ("run-json", "attempt-json", "jobs-json", "artifacts-json", "output"):
        select.add_argument(f"--{name}", type=Path, required=True)
    for name in ("run-id", "run-attempt", "workflow-id", "source-sha", "source-tag", "version", "build"):
        select.add_argument(f"--{name}", required=True)
    select.set_defaults(handler=select_command)
    restore = commands.add_parser("restore-package")
    restore.add_argument("--artifact-dir", type=Path, required=True)
    restore.add_argument("--selection", type=Path, required=True)
    restore.add_argument("--output-dir", type=Path, required=True)
    restore.add_argument("--source-sha", required=True)
    restore.add_argument("--source-tag", required=True)
    restore.add_argument("--version", required=True)
    restore.add_argument("--build", required=True)
    restore.set_defaults(handler=restore_command)
    return root


if __name__ == "__main__":
    parsed = parser().parse_args()
    parsed.handler(parsed)
