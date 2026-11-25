#!/usr/bin/env python3
"""Recover successful platform outputs while rebuilding Android and WebOS after CI fixes."""

import argparse
import ast
import base64
from datetime import datetime
from functools import lru_cache
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

import verify_release_ci as release_ci
import verify_release_attestation


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "snapetech-org/pleazyng"
# Application sources, dependency pins, and native toolchain inputs are excluded.
# The signed visualizer catalog is published and verified independently at runtime.
# Gradle heap/worker tuning, Apple scratch setup, and the checksum-preserving TV mirror route are infrastructure-only.
RECOVERY_FILES = frozenset({
    # Exact release-controller recovery changes shipped after the frozen
    # product source; these do not change application or native inputs.
    ".github/release-packaging-repairs.json",
    "scripts/apple_jit_process_tree.py",
    "scripts/checks/test_apple_jit_process_tree.py",
    "scripts/checks/test_verify_frozen_packaging_repair.py",
    "scripts/verify_frozen_packaging_repair.py",
    "tools/webos/tests/test_build_release_candidate_dirs.py",
    "content/visualizer-catalog.json",
    "android/gradle.properties",
    "scripts/tv_mpv/build.py", "scripts/tv_mpv/video.patch",
    "scripts/prepare_android_sdk.py", "scripts/test_prepare_android_sdk.py",
    "tools/webos/NATIVE-PREFLIGHT.md",
    "webos/CMakeLists.txt", "scripts/verify_webos_link_patch.py", "scripts/test_verify_webos_link_patch.py",
    "tools/webos/check_audio_plugin_syntax.sh", "tools/webos/build_release_candidate.sh",
    "scripts/prepare_native_downloads.py", "scripts/test_prepare_native_downloads.py",
    "scripts/launch_queued_apple_jit_jobs.py", "scripts/checks/test_apple_jit_queue.py",
    "scripts/retain_macos_app.py", "scripts/test_retain_macos_app.py",
    ".github/workflows/build.yml", ".github/workflows/ci.yml", "CHANGELOG.md",
    "docs/maintainers/full-release-build.md", "docs/maintainers/apple-jit-runner.md",
    "docs/maintainers/release-channels.md", "docs/maintainers/tizen-tv.md",
    "scripts/launch_apple_jit_runner.sh",
    "scripts/prepare_apple_jit_scratch.sh",
    "scripts/cache_macos_flutter_sdk.py",
    "scripts/checks/test_cache_macos_flutter_sdk.py",
    "scripts/clean_appledouble_sidecars.sh",
    "scripts/cleanup_apple_jit_scratch.sh",
    "scripts/checks/test_clean_appledouble_sidecars.py",
    "scripts/checks/check_build_workflow.py",
    "scripts/checks/test_recover_release_platforms.py",
    "scripts/checks/test_release_ci_gate.py",
    "scripts/checks/test_release_signing_options.py",
    "AGENTS.md",
    "scripts/checks/test_tv_mpv_build.py",
    # The iOS store preflight only moves an existing entitlement failure ahead
    # of the expensive compile; archive/export and shipped bytes are unchanged.
    "scripts/preflight_ios_store_profile.py",
    "scripts/release_all_platforms.py",
    "scripts/prepare_gradle_repositories.py", "scripts/run_android_build.py", "scripts/gradle/plezyng-maven-mirror.init.gradle",
    "scripts/checks/test_prepare_gradle_repositories.py", "scripts/recover_release_platforms.py",
    "scripts/checks/test_recover_release_platforms.py", "scripts/checks/check_build_workflow.py",
    "scripts/checks/test_check_build_workflow.py", "scripts/checks/test_release_ci_gate.py",
    "scripts/checks/test_release_core_publication.py",
    "scripts/verify_tvos_runner_acquisition.py",
    "scripts/checks/test_verify_tvos_runner_acquisition.py",
    "tools/webos/build_ipk.sh", "tools/webos/tests/test_source_ndk_environment.py",
    "scripts/recover_macos_app.py", "scripts/test_recover_macos_app.py",
    "scripts/apple/prepare_macos_app_for_signing.py", "scripts/apple/test_prepare_macos_app_for_signing.py",
    "scripts/checks/test_macos_validation_request.py", "scripts/checks/test_macos_packaging_recovery.py",
})
REBUILT_JOBS = frozenset({"build-android", "Build WebOS TV package (ARMv7)"})
TIZEN_PACKAGE_JOB = "Build Samsung Tizen TV package (ARM) / package"
REQUIRED_JOBS = (
    "Validate release request",
    "Build tvOS developer package (arm64)", "build-ios", "build-macos",
    "Build Linux (x64)", "Build Linux (arm64)",
    "Build Windows (x64)", "Build Windows (arm64)", "package-windows",
    "Build Samsung Tizen TV package (ARM) / validate",
    "Build Samsung Tizen TV package (ARM) / package",
)
ARTIFACTS = {
    "pleazyng-tizen-arm-experimental": "tizen", "ios-ipa": "ios-ipa", "tvos-ipa": "tvos-ipa",
    "macos-dmg": "macos-dmg", "windows-x64-portable": "windows-x64-portable",
    "windows-arm64-build": "windows-arm64-build", "windows-installer": "windows-installer",
    "windows-chocolatey": "windows-chocolatey", "linux-x64": "linux-x64",
    "linux-arm64": "linux-arm64", "linux-x64-appimage": "linux-x64",
}
WINDOWS_REPACKAGED_ARTIFACTS = frozenset({
    "windows-x64-portable", "windows-installer", "windows-chocolatey",
})
WINDOWS_RAW_X64_ARTIFACT = "windows-x64-build"
TVOS_RUNTIME_RECEIPT = "tvos-runtime-dependency-receipt.json"
TVOS_RUNTIME_SYSTEM_PREFIXES = ("/System/Library/", "/System/iOSSupport/", "/usr/lib/")
TVOS_MACHO_MAGICS = frozenset(bytes.fromhex(value) for value in (
    "feedface", "cefaedfe", "feedfacf", "cffaedfe",
    "cafebabe", "bebafeca", "cafebabf", "bfbafeca",
))
TVOS_DYLIB_LOAD_COMMANDS = frozenset({
    "LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB",
    "LC_LOAD_UPWARD_DYLIB", "LC_LAZY_LOAD_DYLIB",
})


class RecoveryError(RuntimeError):
    pass


@lru_cache(maxsize=64)
def immutable_content(repository, revision, path):
    if repository != REPOSITORY or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise RecoveryError("Source content requires an immutable trusted revision")
    response = api(f"repos/{repository}/contents/{path}?ref={revision}")
    if response.get("encoding") != "base64":
        raise RecoveryError("Immutable source content evidence is incomplete")
    return base64.b64decode(response["content"]).decode()


def verify_webos_link_patch(repository, source_sha, head_sha, files):
    from verify_webos_link_patch import CMAKE_PATH, equivalent
    if any(entry.get("filename") == CMAKE_PATH for entry in files):
        if not equivalent(immutable_content(repository, source_sha, CMAKE_PATH), immutable_content(repository, head_sha, CMAKE_PATH)):
            raise RecoveryError("webOS link changes exceed the pinned Flutter/SDK atomic link repairs")


def api(path):
    for attempt in range(3):
        result = subprocess.run(["gh", "api", path], capture_output=True, text=True)
        if not result.returncode:
            return json.loads(result.stdout)
        if attempt == 2 or not re.search(r"\(HTTP 50[234]\)", result.stderr):
            raise RecoveryError(result.stderr.strip())
        time.sleep(attempt + 1)


def collection(path, key):
    from release_job_inventory import inventory, mac_package_evidence
    values = inventory(path, key, api, RecoveryError, page_size=30)
    return mac_package_evidence(values) if key == "jobs" else values


def verified_run_artifact(inventory, name, run_id, source_sha, repository=REPOSITORY):
    """Return unique unexpired artifact evidence tied to the exact workflow run."""
    matches = [item for item in inventory
               if item.get("name") == name and item.get("expired") is False]
    if len(matches) != 1:
        raise RecoveryError(f"Reusable artifact is missing, expired, or ambiguous: {name}")
    artifact = matches[0]
    digest = artifact.get("digest") or ""
    identity = artifact.get("workflow_run") or {}
    if (type(artifact.get("id")) is not int or artifact["id"] < 1
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            or identity.get("id") != int(run_id) or identity.get("head_sha") != source_sha):
        raise RecoveryError(f"Reusable artifact identity or digest is invalid: {name}")
    metadata = api(f"repos/{repository}/actions/artifacts/{artifact['id']}")
    metadata_run = metadata.get("workflow_run") or {}
    if (metadata.get("id") != artifact["id"] or metadata.get("name") != name
            or metadata.get("expired") is not False or metadata.get("digest") != digest
            or metadata_run.get("id") != int(run_id) or metadata_run.get("head_sha") != source_sha):
        raise RecoveryError(f"Reusable artifact metadata changed after source validation: {name}")
    return artifact


def verify_skipped_windows_packaging(source, jobs, repository=REPOSITORY):
    """Recognize only an unassigned source package skip with retained x64 bytes."""
    run_id = source.get("id")
    source_sha = source.get("head_sha")
    attempt = source.get("run_attempt")
    if (type(run_id) is not int or run_id < 1 or type(attempt) is not int
            or not 1 <= attempt <= 100 or not re.fullmatch(r"[0-9a-f]{40}", str(source_sha))):
        raise RecoveryError("Windows package recovery requires an exact source run attempt")
    package_jobs = [job for job in jobs if job.get("name") == "package-windows"]
    if len(package_jobs) != 1:
        raise RecoveryError("Windows package recovery requires one original package-windows job")
    package_job = package_jobs[0]
    if (package_job.get("status") != "completed" or package_job.get("conclusion") != "skipped"
            or package_job.get("runner_id") not in (None, 0)
            or package_job.get("runner_name") not in (None, "")
            or not isinstance(package_job.get("steps"), list) or package_job["steps"]
            or package_job.get("run_id") != run_id or package_job.get("run_attempt") != attempt
            or package_job.get("head_sha") != source_sha):
        raise RecoveryError("Windows packaging may only be recovered after an exact unassigned source skip")
    chocolatey_jobs = [job for job in jobs if job.get("name") == "Package Windows Chocolatey"]
    if len(chocolatey_jobs) != 1:
        raise RecoveryError("Windows package recovery requires one original Chocolatey job")
    chocolatey_job = chocolatey_jobs[0]
    if (chocolatey_job.get("status") != "completed" or chocolatey_job.get("conclusion") != "skipped"
            or chocolatey_job.get("runner_id") not in (None, 0)
            or chocolatey_job.get("runner_name") not in (None, "")
            or not isinstance(chocolatey_job.get("steps"), list) or chocolatey_job["steps"]
            or chocolatey_job.get("run_id") != run_id or chocolatey_job.get("run_attempt") != attempt
            or chocolatey_job.get("head_sha") != source_sha):
        raise RecoveryError("Chocolatey packaging may only be recovered after an exact unassigned source skip")
    x64_jobs = [job for job in jobs if job.get("name") == "Build Windows (x64)"]
    if len(x64_jobs) != 1:
        raise RecoveryError("Windows package recovery requires one original x64 build job")
    x64_job = x64_jobs[0]
    if (x64_job.get("status") != "completed" or x64_job.get("conclusion") != "success"
            or x64_job.get("run_id") != run_id or x64_job.get("run_attempt") != attempt
            or x64_job.get("head_sha") != source_sha):
        raise RecoveryError("Windows x64 package input was not produced by the exact successful source attempt")
    required_steps = ("Build Windows x64", "Verify Windows bundle contents", "Upload x64 build")
    for step_name in required_steps:
        steps = [step for step in x64_job.get("steps", []) if step.get("name") == step_name]
        if len(steps) != 1 or steps[0].get("status") != "completed" or steps[0].get("conclusion") != "success":
            raise RecoveryError(f"Original Windows x64 checkpoint is missing its successful step: {step_name}")
    inventory = collection(f"repos/{repository}/actions/runs/{run_id}/artifacts", "artifacts")
    if any(item.get("name") in WINDOWS_REPACKAGED_ARTIFACTS for item in inventory):
        raise RecoveryError("Skipped Windows packaging has unexpected original package artifacts")
    artifact = verified_run_artifact(inventory, WINDOWS_RAW_X64_ARTIFACT, run_id, source_sha, repository)
    return {
        "source_run_id": run_id,
        "source_sha": source_sha,
        "run_attempt": attempt,
        "source_package_job_id": package_job.get("id"),
        "source_chocolatey_job_id": chocolatey_job.get("id"),
        "source_x64_build_job_id": x64_job.get("id"),
        "artifact_name": WINDOWS_RAW_X64_ARTIFACT,
        "artifact_id": artifact["id"],
        "artifact_digest": artifact["digest"],
        "missing_original_artifacts": sorted(WINDOWS_REPACKAGED_ARTIFACTS),
    }


def trusted_versioned_release_title(run, version):
    """Match a standard release run title emitted by the trusted workflow."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(version)):
        return False
    titles = {
        f"action:release:pool:{pool}:tag:v{version}"
        for pool in ("public", "private")
    }
    # The workflow adds this prefix when private Apple signing was unavailable.
    titles.update(f"apple-signing-unavailable:{title}" for title in tuple(titles))
    return run.get("display_title") in titles


def immutable_ci_source(repository, source_sha, version):
    """Resolve exact app CI only after a complete infrastructure-only comparison."""
    if repository != REPOSITORY or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise RecoveryError("Invalid immutable CI source identity")
    from release_all_platforms import RELEASE_TAG_REUSE_ALLOWLIST
    obj = api(f"repos/{repository}/git/ref/tags/v{version}")["object"]
    for _ in range(8):
        if not re.fullmatch(r"[a-f0-9]{40}", obj.get("sha", "")):
            raise RecoveryError("Invalid immutable CI tag object")
        if obj.get("type") == "commit":
            break
        if obj.get("type") != "tag":
            raise RecoveryError("Invalid immutable CI tag type")
        obj = api(f"repos/{repository}/git/tags/{obj['sha']}")["object"]
    else:
        raise RecoveryError("Immutable CI tag chain is too long")
    tag_sha = obj["sha"]
    if source_sha != tag_sha:
        comparison = api(f"repos/{repository}/compare/{tag_sha}...{source_sha}")
        files = comparison.get("files")
        if (comparison.get("status") not in {"ahead", "identical"}
            or comparison.get("merge_base_commit", {}).get("sha") != tag_sha
            or not isinstance(files, list) or len(files) >= 300
            or any(path not in RELEASE_TAG_REUSE_ALLOWLIST for entry in files
                   for path in (entry.get("filename"), entry.get("previous_filename")) if path)):
            raise RecoveryError("Cannot reuse tag CI: application/native inputs changed or comparison incomplete")
        verify_webos_link_patch(repository, tag_sha, source_sha, files)
    return tag_sha


def embedded_source_ci(repository, source_sha, source_run, *, supplemental_apple=None):
    """Verify qualification inside the original build, even if a platform failed."""
    from verify_embedded_release_ci import PREFIX, verify_jobs
    if repository != REPOSITORY or not str(source_run).isdigit():
        raise RecoveryError("Embedded CI requires the original trusted build identity")
    run = api(f"repos/{repository}/actions/runs/{source_run}")
    attempt = run.get("run_attempt")
    if not isinstance(attempt, int) or attempt < 1:
        raise RecoveryError("Original build omitted its current attempt")
    from verify_embedded_release_ci import latest_attempt_jobs
    from verify_release_ci import GateError
    if attempt > 100:
        raise RecoveryError("Original build attempt history exceeds the supported bound")
    try:
        history = [collection(f"repos/{repository}/actions/runs/{source_run}/attempts/{number}/jobs", "jobs")
                   for number in range(1, attempt + 1)]
        jobs = latest_attempt_jobs(history)
    except GateError as error:
        raise RecoveryError(f"Embedded CI attempt history is invalid: {error}") from error
    if supplemental_apple is None and not any(str(job.get("name", "")).startswith(PREFIX) for job in jobs):
        return None  # Legacy releases qualified in a separate ci.yml run.
    manifest = json.loads(immutable_content(repository, source_sha, "docs/maintainers/capability-manifest.json"))
    options = {}
    if supplemental_apple is not None:
        capture = [job for job in jobs if job.get("name") == "Validate captured source"]
        if (run.get("status") != "completed" or run.get("conclusion") not in {"failure", "cancelled"}
                or len(capture) != 1 or capture[0].get("status") != "completed"
                or capture[0].get("conclusion") != "success"):
            raise RecoveryError("Supplemental qualification requires a completed interrupted captured release")
        options.update(supplemental_apple=supplemental_apple, require_candidate_guards=True)
    proof = verify_jobs(run, jobs, source_sha, int(source_run), attempt,
                        manifest["release"]["releaseGate"]["requiredJobs"], **options)
    freeze = [job for job in jobs if job.get("name") == "Freeze qualified source"]
    if (len(freeze) != 1 or freeze[0].get("status") != "completed"
            or freeze[0].get("conclusion") != ("skipped" if supplemental_apple is not None else "success")):
        raise RecoveryError("Original source was not frozen after successful embedded CI")
    ancestry = api(f"repos/{repository}/compare/{source_sha}...main")
    if ancestry.get("status") not in ("ahead", "identical") or ancestry.get("base_commit", {}).get("sha") != source_sha:
        raise RecoveryError("Embedded CI source is not an ancestor of canonical main")
    return {"ci_mode": "embedded", "ci_run_id": run["id"], "ci_run_url": run["html_url"],
            "ci_source_sha": source_sha, "embedded_ci": proof}


def github_token():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
        token = result.stdout.strip()
    return token


def supplemental_ci_proof(repository, source_sha, version, source_run, qualification_run, *, freeze):
    from verify_embedded_release_ci import resolve_ref, verify_candidate, verify_supplemental_apple_ci

    token = github_token()
    tag = f"v{version}"
    candidate = f"{tag}-candidate.{source_sha}"
    verify_candidate(token, repository, source_sha, tag, candidate,
                     pubspec_text=immutable_content(repository, source_sha, "pubspec.yaml"))
    if not freeze and resolve_ref(tag, token) != source_sha:
        raise RecoveryError("Stable tag must already bind the qualified frozen source")
    supplemental = verify_supplemental_apple_ci(repository, source_sha, int(source_run), qualification_run, token)
    return embedded_source_ci(repository, source_sha, source_run, supplemental_apple=supplemental)


def ci_proof(repository, source_sha, version=None, *, source_run=None):
    token = github_token()
    ci_source = immutable_ci_source(repository, source_sha, version) if version else source_sha
    if source_run is not None:
        if not version:
            raise RecoveryError("Embedded qualification requires an immutable release version")
        embedded = embedded_source_ci(repository, ci_source, source_run)
        if embedded is not None:
            return embedded
    try:
        source_manifest = json.loads(immutable_content(
            repository, ci_source, "docs/maintainers/capability-manifest.json"
        ))
    except (RecoveryError, ValueError) as error:
        raise RecoveryError("Could not read the exact CI source capability manifest") from error
    try:
        gate = release_ci._manifest_gate(source_manifest)
    except release_ci.GateError as error:
        raise RecoveryError(str(error)) from error
    run = release_ci._latest_run(token, repository, ci_source, gate)
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise RecoveryError("The source release commit has no successful exact-SHA CI run.")
    errors = release_ci._check_jobs(token, repository, run, gate)
    if errors:
        raise RecoveryError("Source CI is incomplete: " + "; ".join(errors))
    return {"ci_run_id": run["id"], "ci_run_url": run["html_url"], "ci_source_sha": ci_source}


def verify_webos_transport_patch(repository, source_sha, head_sha, files):
    path = "scripts/prepare_native_downloads.py"
    if not any(entry.get("filename") == path for entry in files):
        return
    fingerprints = []
    for revision in (source_sha, head_sha):
        tree = ast.parse(immutable_content(repository, revision, path))
        tree.body = [node for node in tree.body
                     if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"prepare_webos_python", "prepare_apple_pdfium"})
                     and ast.dump(node) != ast.dump(ast.parse('PRODUCERS.add("espresso3389/pdfium-xcframework")').body[0])]
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                for child in ast.walk(node):
                    if isinstance(child, ast.If):
                        child.body = [statement for statement in child.body if not (isinstance(statement, ast.If)
                                      and ast.dump(statement.test) == ast.dump(ast.parse('args.sqlite_platform in {"ios", "macos"}', mode="eval").body)
                                      and len(statement.body) == 1 and isinstance(statement.body[0], ast.Expr)
                                      and isinstance(statement.body[0].value, ast.Call)
                                      and isinstance(statement.body[0].value.func, ast.Name)
                                      and statement.body[0].value.func.id == "prepare_apple_pdfium")]
        # Printing verified-download diagnostics to stderr cannot change native
        # inputs. Retain every other print argument and expression in the proof.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                node.keywords = [kw for kw in node.keywords if not (kw.arg == "file" and ast.dump(kw.value) == ast.dump(ast.Attribute(value=ast.Name(id="sys", ctx=ast.Load()), attr="stderr", ctx=ast.Load())))]
        fingerprints.append(ast.dump(tree, include_attributes=False))
    if fingerprints[0] != fingerprints[1]:
        raise RecoveryError("Native download pins or non-webOS transport code changed; refusing artifact reuse")


def verify_repaired_run(run_id, head_sha, version, repository, *, allow_pending=False, require_homebrew_cask=False, webos_only=False):
    """Verify prior Linux/Mac repairs or a successful webOS build without changing inputs."""
    if not str(run_id).isdigit():
        raise RecoveryError("Repaired platform source requires an exact run ID")
    run = api(f"repos/{repository}/actions/runs/{run_id}")
    source_sha = run.get("head_sha", "")
    pending = allow_pending and run.get("status") in {"queued", "in_progress", "waiting", "pending"}
    if (run.get("event") != "workflow_dispatch" or run.get("head_branch") != "main"
        or run.get("head_repository", {}).get("full_name") != repository
        or run.get("path", "").split("@")[0] != ".github/workflows/build.yml"
        or not trusted_versioned_release_title(run, version)
        or not re.fullmatch(r"[a-f0-9]{40}", source_sha)
        or (run.get("status") != "completed" and not pending)):
        raise RecoveryError("Repaired platform source must be a trusted main release for the same version")
    comparison = api(f"repos/{repository}/compare/{source_sha}...{head_sha}")
    files = comparison.get("files")
    if (comparison.get("status") not in {"ahead", "identical"}
        or comparison.get("merge_base_commit", {}).get("sha") != source_sha
        or not isinstance(files, list) or len(files) >= 300
        or any(path not in RECOVERY_FILES for entry in files
               for path in (entry.get("filename"), entry.get("previous_filename")) if path)):
        raise RecoveryError("Repaired platform inputs changed or source comparison is incomplete")
    verify_webos_link_patch(repository, source_sha, head_sha, files)
    verify_webos_transport_patch(repository, source_sha, head_sha, files)
    workflows = [immutable_content(repository, revision, ".github/workflows/build.yml") for revision in (source_sha, head_sha)]
    patterns = [r"(?ms)^env:\n.*?(?=^jobs:)"]
    compiled_jobs = ["build-webos"] if webos_only else ["build-macos", "build-linux"]
    if not webos_only:
        for lane in ("finish-notarization", "linux-bundle", "linux-format", "linux-bundle-arm64",
                     "linux-format-arm64", "assemble-linux-arm64"):
            if any(f"\n  {lane}:\n" in workflow for workflow in workflows):
                compiled_jobs.append(lane)
    patterns += [r"(?ms)^  " + job + r":\n.*?(?=^  [a-zA-Z0-9_-]+:|\Z)" for job in compiled_jobs]
    for pattern in patterns:
        blocks = [re.findall(pattern, content) for content in workflows]
        if any(len(block) != 1 for block in blocks):
            raise RecoveryError("Repaired platform compiler and packaging contracts are incomplete")
        if webos_only and "build-webos" in pattern and blocks[0] != blocks[1]:
            blocks[1] = [blocks[1][0].replace("    if: ${{ inputs.reuse_webos_run_id == '' && ", "    if: ${{ ", 1)]
        if blocks[0] != blocks[1]:
            raise RecoveryError("Repaired platform compiler and packaging contracts changed")
    jobs = collection(f"repos/{repository}/actions/runs/{run_id}/jobs", "jobs")
    names = ("Build WebOS TV package (ARMv7)",) if webos_only else ("Build Linux (x64)", "Build Linux (arm64)", "build-macos")
    pending_jobs = []
    for name in ("Validate release request", *names):
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) != 1:
            raise RecoveryError(f"Repaired source job is missing or ambiguous: {name}")
        job = matches[0]
        if pending and name != "Validate release request" and job.get("status") in {"queued", "in_progress", "waiting", "pending", "requested"} and not job.get("conclusion"):
            pending_jobs.append(name)
            continue
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            raise RecoveryError(f"Repaired platform job did not succeed: {name}")
        if name == "build-macos":
            steps = ["Build macOS"] + (["Notarize Application", "Wait for notarization acceptance",
                                         "Verify Gatekeeper acceptance for Homebrew Cask"] if require_homebrew_cask else [])
            if any(not any(step.get("name") == required and step.get("conclusion") == "success" and step.get("status") == "completed" for step in job.get("steps", [])) for required in steps):
                raise RecoveryError("Repaired Mac lacks full compilation or required notarization/Gatekeeper qualification")
    for job in jobs:
        if job.get("name") != "create-release" or job.get("conclusion") == "skipped":
            continue
        if pending and job.get("status") in {"queued", "waiting", "pending"} and not job.get("conclusion"):
            continue
        if webos_only and job.get("status") == "completed" and job.get("conclusion") == "failure" and any(
                step.get("name") == "Create Release" and step.get("conclusion") == "skipped"
                for step in job.get("steps", [])):
            continue  # A failed assembly before the publishing step cannot publish a release.
        raise RecoveryError("Repaired source already attempted publication; inspect side effects")
    macos = next((job for job in jobs if job.get("name") == "build-macos"), {})
    qualified = all(any(step.get("name") == name and step.get("conclusion") == "success" and step.get("status") == "completed" for step in macos.get("steps", []))
                    for name in ("Notarize Application", "Wait for notarization acceptance",
                                 "Verify Gatekeeper acceptance for Homebrew Cask"))
    return {"source_run_id": int(run_id), "source_sha": source_sha, "reused_jobs": list(names),
            "macos_cask_qualified": qualified,
            "platforms_complete": run.get("status") == "completed", "pending_jobs": pending_jobs}


def verify_frozen_tvos_run(run_id, proof, repository=REPOSITORY):
    """Reuse only a signed TV output from an approved sibling repair of S."""
    if repository != REPOSITORY or not str(run_id).isdigit() or int(run_id) < 1:
        raise RecoveryError('Frozen tvOS reuse requires an exact canonical source run')
    run = api(f'repos/{repository}/actions/runs/{run_id}')
    title = re.fullmatch(r'action:release:pool:public:tag:v' + re.escape(proof['version'])
                         + r':repair:([0-9a-f]{40})', run.get('display_title', ''))
    workflow = run.get('head_sha', '')
    if (not title or run.get('id') != int(run_id) or run.get('status') != 'completed'
            or run.get('conclusion') != 'failure' or run.get('event') != 'workflow_dispatch'
            or run.get('head_branch') != 'main' or run.get('path') != '.github/workflows/build.yml'
            or run.get('repository', {}).get('id') != 1433487464
            or run.get('head_repository', {}).get('full_name') != repository
            or not re.fullmatch('[0-9a-f]{40}', workflow)
            or type(run.get('run_attempt')) is not int or run['run_attempt'] < 1):
        raise RecoveryError('TV source must be a completed trusted sibling packaging repair')
    packaging = title.group(1)
    if packaging == proof['packaging_source_sha']:
        raise RecoveryError('TV reuse requires a distinct approved sibling repair')
    prior = verify_source_run(proof['source_run_id'], packaging, proof['version'], repository,
                             rebuild_ios=True, rebuild_tvos=True, packaging_source_sha=packaging,
                             workflow_source_sha=workflow, include_apple_store_build=True)
    if prior.get('source_sha') != proof['product_source_sha'] or not prior.get('platforms_complete'):
        raise RecoveryError('TV sibling repair does not qualify the same frozen product and CI')
    jobs = collection(f'repos/{repository}/actions/runs/{run_id}/attempts/{run["run_attempt"]}/jobs', 'jobs')
    tv_name = 'Build tvOS developer package (arm64)'
    selected = {}
    for name, conclusion in (('Validate release request', 'success'), (tv_name, 'success')):
        matches = [job for job in jobs if job.get('name') == name]
        if (len(matches) != 1 or matches[0].get('status') != 'completed'
                or matches[0].get('conclusion') != conclusion):
            raise RecoveryError('TV sibling source has an unexpected platform outcome: ' + name)
        selected[name] = matches[0]
    _verify_frozen_apple_runner(selected[tv_name], int(run_id), 'tvos')
    ios = [job for job in jobs if job.get('name') == 'build-ios']
    if (len(ios) != 1 or ios[0].get('status') != 'completed'
            or ios[0].get('conclusion') not in ('failure', 'skipped')
            or (ios[0].get('conclusion') == 'skipped' and ios[0].get('steps') != [])):
        raise RecoveryError('TV sibling source has an unexpected iOS outcome')
    selected['build-ios'] = ios[0]
    create = [job for job in jobs if job.get('name') == 'create-release']
    if len(create) != 1 or any(job.get('status') != 'completed' or job.get('conclusion') != 'skipped'
                             or job.get('steps') != [] for job in jobs if job.get('name') not in selected and job != create[0]):
        raise RecoveryError('TV sibling source executed unrelated work or attempted release creation')
    assembly = create[0]
    if ios[0]['conclusion'] == 'failure':
        if assembly.get('status') != 'completed' or assembly.get('conclusion') != 'skipped' or assembly.get('steps') != []:
            raise RecoveryError('TV sibling source attempted release creation after iOS failed')
    else:
        steps = assembly.get('steps', [])
        if (assembly.get('status') != 'completed' or assembly.get('conclusion') != 'failure'
                or len([step for step in steps if step.get('name') == 'Recover verified platform artifacts'
                        and step.get('conclusion') == 'failure']) != 1
                or any(step.get('name') == 'Create Release' and step.get('conclusion') != 'skipped'
                       for step in steps)
                or not any(step.get('name') == 'Create Release' and step.get('conclusion') == 'skipped'
                           for step in steps)):
            raise RecoveryError('TV sibling assembly did not stop before publication')
    for job, required in ((selected['Validate release request'], (
            'Verify approved frozen packaging checkout', 'Verify reusable platform source')),
            (selected[tv_name], ('Build tvOS app', 'Package unsigned tvOS developer IPA',
             'Build signed tvOS App Store IPA', 'Verify tvOS runtime dependencies',
             'Attest unsigned tvOS developer package', 'Attest signed tvOS App Store package', 'Upload tvOS package'))):
        for name in required:
            steps = [step for step in job.get('steps', []) if step.get('name') == name]
            if len(steps) != 1 or steps[0].get('status') != 'completed' or steps[0].get('conclusion') != 'success':
                raise RecoveryError('TV sibling source lacks actual signed package qualification: ' + name)
    artifacts = collection(f'repos/{repository}/actions/runs/{run_id}/artifacts', 'artifacts')
    matches = [item for item in artifacts if item.get('name') == 'tvos-ipa']
    if (len(matches) != 1 or matches[0].get('expired') is not False
            or type(matches[0].get('id')) is not int or matches[0]['id'] < 1
            or not re.fullmatch('sha256:[0-9a-f]{64}', matches[0].get('digest', ''))):
        raise RecoveryError('TV sibling source lacks a unique unexpired digest-bound artifact')
    artifact = matches[0]
    metadata = api(f'repos/{repository}/actions/artifacts/{artifact["id"]}')
    if (metadata.get('id') != artifact['id'] or metadata.get('name') != 'tvos-ipa'
            or metadata.get('digest') != artifact['digest'] or metadata.get('expired') is not False
            or metadata.get('workflow_run', {}).get('id') != int(run_id)
            or metadata.get('workflow_run', {}).get('head_sha') != workflow):
        raise RecoveryError('TV artifact metadata differs from its approved workflow producer')
    return {'source_run_id': int(run_id), 'source_sha': packaging, 'workflow_source_sha': workflow,
            'product_source_sha': proof['product_source_sha'], 'packaging_source_sha': packaging,
            'version': proof['version'], 'runtime_dependency_receipt_required': True,
            'original_source_run_id': proof['source_run_id'],
            'job_id': selected[tv_name]['id'], 'run_attempt': run['run_attempt'],
            'packaging_approval': prior['packaging_approval'], 'artifact_id': artifact['id'],
            'artifact_digest': artifact['digest'], 'platforms_complete': True}


def verify_completed_tvos_run(run_id, proof, repository=REPOSITORY):
    """Reuse a signed tvOS artifact from a frozen packaging assembly retry."""
    if (repository != REPOSITORY or not str(run_id).isdigit() or int(run_id) < 1
            or not re.fullmatch(r'[0-9a-f]{40}', str(proof.get('packaging_source_sha', '')))
            or not re.fullmatch(r'[0-9a-f]{40}', str(proof.get('workflow_source_sha', '')))
            or not re.fullmatch(r'[0-9a-f]{40}', str(proof.get('product_source_sha', '')))):
        raise RecoveryError('Completed tvOS reuse requires exact frozen product, packaging, and workflow sources')
    run = api(f'repos/{repository}/actions/runs/{run_id}')
    version = proof['version']
    title = re.fullmatch(r'action:release:pool:public:tag:v' + re.escape(version)
                         + r':repair:([0-9a-f]{40})', run.get('display_title', ''))
    workflow = run.get('head_sha', '')
    if (not title or title.group(1) != proof['packaging_source_sha']
            or run.get('id') != int(run_id) or run.get('status') != 'completed'
            or run.get('conclusion') != 'failure' or run.get('event') != 'workflow_dispatch'
            or run.get('head_branch') != 'main' or run.get('path') != '.github/workflows/build.yml'
            or run.get('repository', {}).get('id') != 1433487464
            or run.get('head_repository', {}).get('full_name') != repository
            or not re.fullmatch(r'[0-9a-f]{40}', workflow)
            or type(run.get('run_attempt')) is not int or run['run_attempt'] < 1):
        raise RecoveryError('Completed tvOS artifact must come from the exact trusted frozen packaging repair')
    lineage = api(f'repos/{repository}/compare/{workflow}...{proof["workflow_source_sha"]}')
    if (lineage.get('status') not in {'ahead', 'identical'}
            or lineage.get('merge_base_commit', {}).get('sha') != workflow):
        raise RecoveryError('Completed tvOS producer workflow is outside the approved recovery lineage')

    prior = verify_source_run(
        proof['source_run_id'], proof['packaging_source_sha'], version, repository,
        rebuild_tvos=True, packaging_source_sha=proof['packaging_source_sha'],
        workflow_source_sha=workflow, include_apple_store_build=True)
    approval = prior.get('packaging_approval')
    current_approval = proof.get('packaging_approval')
    if (prior.get('source_sha') != proof['product_source_sha'] or not prior.get('platforms_complete')
            or not isinstance(approval, dict) or not isinstance(current_approval, dict)
            or approval.get('approved_files') != current_approval.get('approved_files')
            or approval.get('approval_blob_sha') != current_approval.get('approval_blob_sha')):
        raise RecoveryError('Completed tvOS producer does not share the approved product and packaging proof')

    jobs = collection(f'repos/{repository}/actions/runs/{run_id}/attempts/{run["run_attempt"]}/jobs', 'jobs')
    tv_name = 'Build tvOS developer package (arm64)'
    selected = {}
    for name in ('Validate release request', tv_name, 'create-release'):
        matches = [job for job in jobs if job.get('name') == name]
        if len(matches) != 1:
            raise RecoveryError('Completed tvOS producer job is missing or ambiguous: ' + name)
        selected[name] = matches[0]
    validate = selected['Validate release request']
    tvos = selected[tv_name]
    if (validate.get('status') != 'completed' or validate.get('conclusion') != 'success'
            or tvos.get('status') != 'completed' or tvos.get('conclusion') != 'success'):
        raise RecoveryError('Completed tvOS producer validation or signed build did not succeed')
    _verify_frozen_apple_runner(tvos, int(run_id), 'tvos')

    required_validate_steps = (
        'Verify approved frozen packaging checkout', 'Verify reusable platform source',
        'Validate immutable release tag against trusted main', 'Validate release tag before building',
    )
    for name in required_validate_steps:
        matches = [step for step in validate.get('steps', []) if step.get('name') == name]
        if len(matches) != 1 or matches[0].get('status') != 'completed' or matches[0].get('conclusion') != 'success':
            raise RecoveryError('Completed tvOS producer lacks successful validation: ' + name)
    required_tvos_steps = (
        'Build tvOS app', 'Package unsigned tvOS developer IPA', 'Build signed tvOS App Store IPA',
        'Verify tvOS runtime dependencies',
        'Attest unsigned tvOS developer package', 'Attest signed tvOS App Store package',
        'Retain tvOS signing report', 'Remove tvOS signing archive outputs',
        'Upload tvOS package', 'Clean tvOS source and Xcode scratch',
    )
    for name in required_tvos_steps:
        matches = [step for step in tvos.get('steps', []) if step.get('name') == name]
        if len(matches) != 1 or matches[0].get('status') != 'completed' or matches[0].get('conclusion') != 'success':
            raise RecoveryError('Completed tvOS producer lacks signed artifact qualification: ' + name)

    create = selected['create-release']
    steps = create.get('steps', [])
    if create.get('status') != 'completed' or create.get('conclusion') != 'failure':
        raise RecoveryError('Completed tvOS producer did not stop at the expected pre-publication failure')
    failures = [step.get('name') for step in steps if step.get('conclusion') == 'failure']
    if failures != ['Prepare append-only Apple Store recovery assets']:
        raise RecoveryError('Completed tvOS producer reached an unexpected failure or publication stage')
    for name, conclusion in (
            ('Recover verified platform artifacts', 'success'),
            ('Attest append-only Apple Store recovery assets', 'skipped'),
            ('Verify existing immutable release tag', 'skipped'),
            ('Append signed Apple assets without replacing existing release assets', 'skipped'),
            ('Create Release', 'skipped')):
        matches = [step for step in steps if step.get('name') == name]
        if len(matches) != 1 or matches[0].get('conclusion') != conclusion:
            raise RecoveryError('Completed tvOS producer did not stop before release mutation: ' + name)
    for job in jobs:
        if job in selected.values():
            continue
        if job.get('status') != 'completed' or job.get('conclusion') != 'skipped' or job.get('steps') != []:
            raise RecoveryError('Completed tvOS producer executed unrelated release work')

    artifacts = collection(f'repos/{repository}/actions/runs/{run_id}/artifacts', 'artifacts')
    matches = [item for item in artifacts if item.get('name') == 'tvos-ipa']
    if (len(matches) != 1 or matches[0].get('expired') is not False
            or type(matches[0].get('id')) is not int or matches[0]['id'] < 1
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(matches[0].get('digest', '')))):
        raise RecoveryError('Completed tvOS producer lacks one unexpired digest-bound artifact')
    artifact = matches[0]
    metadata = api(f'repos/{repository}/actions/artifacts/{artifact["id"]}')
    if (metadata.get('id') != artifact['id'] or metadata.get('name') != 'tvos-ipa'
            or metadata.get('digest') != artifact['digest'] or metadata.get('expired') is not False
            or metadata.get('workflow_run', {}).get('id') != int(run_id)
            or metadata.get('workflow_run', {}).get('head_sha') != workflow):
        raise RecoveryError('Completed tvOS artifact metadata differs from its trusted producer')
    return {'source_run_id': int(run_id), 'source_sha': proof['packaging_source_sha'],
            'workflow_source_sha': workflow, 'product_source_sha': proof['product_source_sha'],
            'packaging_source_sha': proof['packaging_source_sha'], 'version': version,
            'original_source_run_id': proof['source_run_id'], 'job_id': tvos['id'],
            'run_attempt': run['run_attempt'], 'packaging_approval': approval,
            'artifact_id': artifact['id'], 'artifact_digest': artifact['digest'],
            'platforms_complete': True, 'runtime_dependency_receipt_required': True}


def verify_frozen_ios_run(run_id, proof, repository=REPOSITORY):
    """Reuse only the signed iOS output of an approved unpublished sibling."""
    if repository != REPOSITORY or not str(run_id).isdigit() or int(run_id) < 1:
        raise RecoveryError('Frozen iOS reuse requires an exact canonical source run')
    run = api(f'repos/{repository}/actions/runs/{run_id}')
    title = re.fullmatch(r'action:release:pool:public:tag:v' + re.escape(proof['version'])
                         + r':repair:([0-9a-f]{40})', run.get('display_title', ''))
    workflow = run.get('head_sha', '')
    if (not title or run.get('id') != int(run_id) or run.get('status') != 'completed'
            or run.get('conclusion') != 'failure' or run.get('event') != 'workflow_dispatch'
            or run.get('head_branch') != 'main' or run.get('path') != '.github/workflows/build.yml'
            or run.get('repository', {}).get('id') != 1433487464
            or run.get('head_repository', {}).get('full_name') != repository
            or not re.fullmatch('[0-9a-f]{40}', workflow)
            or type(run.get('run_attempt')) is not int or run['run_attempt'] < 1):
        raise RecoveryError('iOS source must be a completed trusted sibling packaging repair')
    packaging = title.group(1)
    if packaging == proof['packaging_source_sha']:
        raise RecoveryError('iOS reuse requires a distinct approved sibling repair')
    prior = verify_source_run(proof['source_run_id'], packaging, proof['version'], repository,
                             rebuild_ios=True, rebuild_tvos=True, packaging_source_sha=packaging,
                             workflow_source_sha=workflow, include_apple_store_build=True)
    if prior.get('source_sha') != proof['product_source_sha'] or not prior.get('platforms_complete'):
        raise RecoveryError('iOS sibling repair does not qualify the frozen product and CI')
    jobs = collection(f'repos/{repository}/actions/runs/{run_id}/attempts/{run["run_attempt"]}/jobs', 'jobs')
    selected = {}
    for name in ('Validate release request', 'build-ios'):
        matches = [job for job in jobs if job.get('name') == name]
        if len(matches) != 1 or matches[0].get('status') != 'completed' or matches[0].get('conclusion') != 'success':
            raise RecoveryError('iOS sibling source has an unexpected platform outcome: ' + name)
        selected[name] = matches[0]
    _verify_frozen_apple_runner(selected['build-ios'], int(run_id), 'ios')
    tv = [job for job in jobs if job.get('name') == 'Build tvOS developer package (arm64)']
    if len(tv) != 1 or tv[0].get('conclusion') != 'skipped' or tv[0].get('steps') != []:
        raise RecoveryError('iOS sibling source unexpectedly built tvOS')
    create = [job for job in jobs if job.get('name') == 'create-release']
    if len(create) != 1 or any(job.get('status') != 'completed' or job.get('conclusion') != 'skipped'
                             or job.get('steps') != [] for job in jobs if job.get('name') not in selected
                             and job != create[0]):
        raise RecoveryError('iOS sibling source executed unrelated work')
    steps = create[0].get('steps', [])
    if (create[0].get('status') != 'completed' or create[0].get('conclusion') != 'failure'
            or len([step for step in steps if step.get('name') == 'Recover verified platform artifacts'
                    and step.get('conclusion') == 'failure']) != 1
            or not any(step.get('name') == 'Create Release' and step.get('conclusion') == 'skipped'
                       for step in steps)):
        raise RecoveryError('iOS sibling assembly did not stop before publication')
    for job, required in ((selected['Validate release request'],
                           ('Verify approved frozen packaging checkout', 'Verify reusable platform source')),
                          (selected['build-ios'], ('Build iOS (no codesign)', 'Create unsigned developer IPA',
                           'Attest unsigned developer IPA', 'Build signed iOS App Store IPA',
                           'Attest signed iOS App Store IPA', 'Upload IPA'))):
        for name in required:
            matches = [step for step in job.get('steps', []) if step.get('name') == name]
            if len(matches) != 1 or matches[0].get('status') != 'completed' or matches[0].get('conclusion') != 'success':
                raise RecoveryError('iOS sibling source lacks actual signed package qualification: ' + name)
    artifacts = collection(f'repos/{repository}/actions/runs/{run_id}/artifacts', 'artifacts')
    matches = [item for item in artifacts if item.get('name') == 'ios-ipa']
    if (len(matches) != 1 or matches[0].get('expired') is not False
            or type(matches[0].get('id')) is not int or matches[0]['id'] < 1
            or not re.fullmatch('sha256:[0-9a-f]{64}', matches[0].get('digest', ''))):
        raise RecoveryError('iOS sibling source lacks a unique unexpired digest-bound artifact')
    artifact = matches[0]
    metadata = api(f'repos/{repository}/actions/artifacts/{artifact["id"]}')
    if (metadata.get('id') != artifact['id'] or metadata.get('name') != 'ios-ipa'
            or metadata.get('digest') != artifact['digest'] or metadata.get('expired') is not False
            or metadata.get('workflow_run', {}).get('id') != int(run_id)
            or metadata.get('workflow_run', {}).get('head_sha') != workflow):
        raise RecoveryError('iOS artifact metadata differs from its approved workflow producer')
    return {'source_run_id': int(run_id), 'source_sha': packaging, 'workflow_source_sha': workflow,
            'product_source_sha': proof['product_source_sha'], 'original_source_run_id': proof['source_run_id'],
            'job_id': selected['build-ios']['id'], 'run_attempt': run['run_attempt'],
            'packaging_approval': prior['packaging_approval'], 'artifact_id': artifact['id'],
            'artifact_digest': artifact['digest'], 'platforms_complete': True}


def verify_frozen_apple_run(run_id, proof, repository=REPOSITORY):
    """Reuse both signed Apple packages from one assembly-only repair run."""
    if (repository != REPOSITORY or not str(run_id).isdigit() or int(run_id) < 1
            or not re.fullmatch(r'[0-9a-f]{40}', str(proof.get('packaging_source_sha', '')))
            or not re.fullmatch(r'[0-9a-f]{40}', str(proof.get('workflow_source_sha', '')))
            or not re.fullmatch(r'[0-9a-f]{40}', str(proof.get('product_source_sha', '')))):
        raise RecoveryError('Combined frozen Apple reuse requires an exact canonical source run')
    run = api(f'repos/{repository}/actions/runs/{run_id}')
    title = re.fullmatch(r'action:release:pool:public:tag:v' + re.escape(proof['version'])
                         + r':repair:([0-9a-f]{40})', run.get('display_title', ''))
    workflow = run.get('head_sha', '')
    if (not title or run.get('id') != int(run_id) or run.get('status') != 'completed'
            or run.get('conclusion') != 'failure' or run.get('event') != 'workflow_dispatch'
            or run.get('head_branch') != 'main' or run.get('path') != '.github/workflows/build.yml'
            or run.get('repository', {}).get('id') != 1433487464
            or run.get('head_repository', {}).get('full_name') != repository
            or not re.fullmatch(r'[0-9a-f]{40}', workflow)
            or type(run.get('run_attempt')) is not int or run['run_attempt'] < 1):
        raise RecoveryError('Combined Apple source must be a completed trusted-main repair run')
    packaging = title.group(1)
    if packaging != proof.get('packaging_source_sha'):
        raise RecoveryError('Combined Apple source uses a different approved packaging revision')
    lineage = api(f'repos/{repository}/compare/{workflow}...{proof["workflow_source_sha"]}')
    if (lineage.get('status') not in {'ahead', 'identical'}
            or lineage.get('merge_base_commit', {}).get('sha') != workflow):
        raise RecoveryError('Combined Apple source workflow is not an ancestor of this assembly')
    prior = verify_source_run(
        proof['source_run_id'], packaging, proof['version'], repository,
        rebuild_ios=True, rebuild_tvos=True, packaging_source_sha=packaging,
        workflow_source_sha=workflow, include_apple_store_build=True,
        append_apple_only=True)
    approval = prior.get('packaging_approval')
    if (prior.get('source_sha') != proof.get('product_source_sha')
            or not prior.get('platforms_complete') or not isinstance(approval, dict)
            or approval.get('product_source_sha') != proof.get('product_source_sha')
            or approval.get('packaging_source_sha') != packaging
            or approval.get('workflow_source_sha') != workflow
            or approval.get('approval_source_sha') != workflow
            or not isinstance(approval.get('approved_files'), dict)
            or not approval['approved_files']):
        raise RecoveryError('Combined Apple source does not qualify the frozen product and CI')

    jobs = collection(f'repos/{repository}/actions/runs/{run_id}/attempts/{run["run_attempt"]}/jobs', 'jobs')
    expected = {}
    for name in ('Validate release request', 'build-ios',
                 'Build tvOS developer package (arm64)', 'create-release'):
        matches = [job for job in jobs if job.get('name') == name]
        if len(matches) != 1:
            raise RecoveryError(f'Combined Apple source job is missing or ambiguous: {name}')
        expected[name] = matches[0]

    validate = expected['Validate release request']
    if validate.get('status') != 'completed' or validate.get('conclusion') != 'success':
        raise RecoveryError('Combined Apple source failed release request validation')
    for name in ('Verify approved frozen packaging checkout', 'Verify reusable platform source',
                 'Validate immutable release tag against trusted main',
                 'Validate release tag before building'):
        steps = [step for step in validate.get('steps', []) if step.get('name') == name]
        if len(steps) != 1 or steps[0].get('status') != 'completed' or steps[0].get('conclusion') != 'success':
            raise RecoveryError('Combined Apple source lacks successful validation: ' + name)

    ios = expected['build-ios']
    tvos = expected['Build tvOS developer package (arm64)']
    for job, platform, required in (
            (ios, 'ios', ('Build iOS (no codesign)', 'Create unsigned developer IPA',
                          'Attest unsigned developer IPA', 'Retain unsigned iOS developer checkpoint',
                          'Build signed iOS App Store IPA', 'Attest signed iOS App Store IPA',
                          'Retain iOS signing report', 'Remove iOS signing archive outputs',
                          'Upload IPA', 'Remove short iOS build workspace')),
            (tvos, 'tvos', ('Build tvOS app', 'Package unsigned tvOS developer IPA',
                            'Verify tvOS runtime dependencies',
                            'Build signed tvOS App Store IPA', 'Attest unsigned tvOS developer package',
                            'Attest signed tvOS App Store package', 'Retain tvOS signing report',
                            'Remove tvOS signing archive outputs', 'Upload tvOS package',
                            'Clean tvOS source and Xcode scratch'))):
        if job.get('status') != 'completed' or job.get('conclusion') != 'success':
            raise RecoveryError(f'Combined Apple {platform} signing job did not succeed')
        _verify_frozen_apple_runner(job, int(run_id), platform)
        for name in required:
            steps = [step for step in job.get('steps', []) if step.get('name') == name]
            if len(steps) != 1 or steps[0].get('status') != 'completed' or steps[0].get('conclusion') != 'success':
                raise RecoveryError(f'Combined Apple {platform} source lacks successful build, upload, or cleanup: {name}')

    create = expected['create-release']
    if create.get('status') != 'completed' or create.get('conclusion') != 'failure':
        raise RecoveryError('Combined Apple assembly did not stop at its failed recovery step')
    create_steps = create.get('steps', [])
    failed = [step.get('name') for step in create_steps if step.get('conclusion') == 'failure']
    if failed != ['Recover verified platform artifacts']:
        raise RecoveryError('Combined Apple assembly had an unexpected failure or mutation')
    for name in ('Prepare append-only Apple Store recovery assets',
                 'Attest append-only Apple Store recovery assets',
                 'Verify existing immutable release tag',
                 'Append signed Apple assets without replacing existing release assets',
                 'Create Release'):
        steps = [step for step in create_steps if step.get('name') == name]
        if len(steps) != 1 or steps[0].get('status') != 'completed' or steps[0].get('conclusion') != 'skipped':
            raise RecoveryError('Combined Apple assembly reached a later mutation step: ' + name)
    recovery_steps = [index for index, step in enumerate(create_steps)
                      if step.get('name') == 'Recover verified platform artifacts']
    if len(recovery_steps) != 1:
        raise RecoveryError('Combined Apple assembly has ambiguous recovery steps')
    for step in create_steps[recovery_steps[0] + 1:]:
        if (step.get('conclusion') != 'skipped'
                and not step.get('name', '').startswith('Post ')
                and step.get('name') != 'Complete job'):
            raise RecoveryError('Combined Apple assembly continued after its recovery failure')

    macos_jobs = [job for job in jobs if job.get('name') == 'build-macos']
    if len(macos_jobs) > 1:
        raise RecoveryError('Combined Apple source has duplicate macOS build jobs')
    for job in jobs:
        if job in expected.values():
            continue
        if job.get('name') == 'build-macos' and job.get('status') == 'completed' and job.get('conclusion') == 'success':
            continue  # Do not select the separate hosted DMG for release recovery.
        if job.get('status') != 'completed' or job.get('conclusion') != 'skipped' or job.get('steps') != []:
            raise RecoveryError('Combined Apple source executed an unrelated or failed job')

    artifacts = collection(f'repos/{repository}/actions/runs/{run_id}/artifacts', 'artifacts')
    sources = {}
    for platform, artifact_name, job in (
            ('ios', 'ios-ipa', ios), ('tvos', 'tvos-ipa', tvos)):
        matches = [item for item in artifacts if item.get('name') == artifact_name]
        if (len(matches) != 1 or matches[0].get('expired') is not False
                or type(matches[0].get('id')) is not int or matches[0]['id'] < 1
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(matches[0].get('digest', '')))):
            raise RecoveryError(f'Combined Apple source lacks one unexpired digest-bound {artifact_name} artifact')
        artifact = matches[0]
        metadata = api(f'repos/{repository}/actions/artifacts/{artifact["id"]}')
        if (metadata.get('id') != artifact['id'] or metadata.get('name') != artifact_name
                or metadata.get('digest') != artifact['digest'] or metadata.get('expired') is not False
                or metadata.get('workflow_run', {}).get('id') != int(run_id)
                or metadata.get('workflow_run', {}).get('head_sha') != workflow):
            raise RecoveryError(f'Combined Apple {artifact_name} artifact metadata differs from its producer')
        sources[platform] = {
            'source_run_id': int(run_id), 'source_sha': packaging,
            'workflow_source_sha': workflow, 'product_source_sha': proof['product_source_sha'],
            **({'packaging_source_sha': packaging, 'version': proof['version'],
                'runtime_dependency_receipt_required': True} if platform == 'tvos' else {}),
            'original_source_run_id': proof['source_run_id'], 'job_id': job['id'],
            'run_attempt': run['run_attempt'], 'packaging_approval': prior['packaging_approval'],
            'artifact_id': artifact['id'], 'artifact_digest': artifact['digest'],
            'platforms_complete': True,
        }
    if (sources['ios']['artifact_id'] == sources['tvos']['artifact_id']
            or sources['ios']['artifact_digest'] == sources['tvos']['artifact_digest']):
        raise RecoveryError('Combined Apple source does not bind two distinct IPA artifacts')
    return {'ios_source': sources['ios'], 'tvos_source': sources['tvos']}


def _verify_frozen_apple_runner(job, run_id, platform):
    """Require one of the explicitly supported signed Apple runner routes."""
    expected_labels = ('macos-26-intel', 'xcode-27') if platform == 'ios' else ('macos-26',)
    labels = job.get('labels')
    runner_id = job.get('runner_id')
    runner_name = job.get('runner_name')
    group = job.get('runner_group_name')
    if (not isinstance(labels, list) or type(runner_id) is not int or runner_id < 1
            or not isinstance(runner_name, str) or not runner_name.strip()):
        raise RecoveryError(f'{platform} signed recovery lacks assigned runner metadata (run {run_id})')
    if (group == 'GitHub Actions' and len(labels) == 1 and labels[0] in expected_labels
            and re.fullmatch(r'GitHub Actions [1-9][0-9]*', runner_name)):
        return
    private_task_label = f'gha-{run_id}-build-{platform}'
    private_labels = {
        'self-hosted', 'macOS', 'ARM64', 'private-apple', 'ng-shared-main-pleazyng',
        f'ng-main-run-{run_id}', private_task_label,
    }
    if group == 'private-apple-builds' and private_labels.issubset(set(labels)):
        return
    raise RecoveryError(
        f'{platform} signed recovery did not use its expected public hosted or exact private Apple runner '
        f'(run {run_id})'
    )


def verify_unsigned_ios_run(run_id, head_sha, version, repository=REPOSITORY):
    """Verify an independently uploaded unsigned developer IPA from this release source."""
    if repository != REPOSITORY or not str(run_id).isdigit() or int(run_id) < 1:
        raise RecoveryError("Unsigned iOS reuse requires an exact canonical source run")
    run = api(f"repos/{repository}/actions/runs/{run_id}")
    source_sha = run.get("head_sha", "")
    if (run.get("id") != int(run_id) or run.get("status") != "completed"
            or run.get("event") != "workflow_dispatch" or run.get("head_branch") != "main"
            or run.get("head_repository", {}).get("full_name") != repository
            or run.get("path", "").split("@")[0] != ".github/workflows/build.yml"
            or not trusted_versioned_release_title(run, version)
            or not re.fullmatch(r"[0-9a-f]{40}", source_sha)
            or type(run.get("run_attempt")) is not int or run["run_attempt"] < 1):
        raise RecoveryError("Unsigned iOS artifact must come from a completed trusted release run")
    lineage = api(f"repos/{repository}/compare/{source_sha}...{head_sha}")
    files = lineage.get("files")
    if (lineage.get("status") not in {"ahead", "identical"}
            or lineage.get("merge_base_commit", {}).get("sha") != source_sha
            or not isinstance(files, list) or len(files) >= 300
            or any(path not in RECOVERY_FILES for item in files
                   for path in (item.get("filename"), item.get("previous_filename")) if path)):
        raise RecoveryError("Unsigned iOS artifact is outside the verified release recovery lineage")
    jobs = collection(f"repos/{repository}/actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs", "jobs")
    matches = [job for job in jobs if job.get("name") == "build-ios"]
    required_steps = {
        "Build iOS (no codesign)", "Create unsigned developer IPA",
        "Retain unsigned iOS developer checkpoint", "Attest unsigned developer IPA", "Upload IPA",
    }
    if (len(matches) != 1 or matches[0].get("status") != "completed"
            or matches[0].get("conclusion") != "success"
            or not required_steps.issubset({step.get("name") for step in matches[0].get("steps", [])
                                            if step.get("status") == "completed"
                                            and step.get("conclusion") == "success"})):
        raise RecoveryError("Unsigned iOS source job lacks a complete successful build and upload")
    artifacts = collection(f"repos/{repository}/actions/runs/{run_id}/artifacts", "artifacts")
    found = [item for item in artifacts if item.get("name") == "ios-ipa" and not item.get("expired")]
    if len(found) != 1:
        raise RecoveryError("Unsigned iOS source artifact is missing or ambiguous")
    artifact = found[0]
    workflow_run = artifact.get("workflow_run", {})
    digest = artifact.get("digest") or ""
    if (workflow_run.get("id") != int(run_id) or workflow_run.get("head_sha") != source_sha
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
        raise RecoveryError("Unsigned iOS artifact does not bind to its verified producer run")
    return {"source_run_id": int(run_id), "source_sha": source_sha,
            "workflow_source_sha": source_sha, "version": version,
            "run_attempt": run["run_attempt"], "job_id": matches[0]["id"],
            "artifact_id": artifact["id"], "artifact_digest": digest,
            "platforms_complete": True}


def original_tvos_execution_attempt(repository, run_id, job):
    from verify_embedded_release_ci import same_completed_execution

    observed_attempt = job['run_attempt']
    original_attempt = observed_attempt
    for prior_attempt in range(observed_attempt - 1, 0, -1):
        prior_jobs = collection(f'repos/{repository}/actions/runs/{run_id}/attempts/{prior_attempt}/jobs', 'jobs')
        matches = [prior for prior in prior_jobs if prior.get('name') == job['name']]
        if len(matches) > 1:
            raise RecoveryError('Ambiguous original tvOS execution in prior attempt')
        if matches and same_completed_execution(job, matches[0]):
            original_attempt = prior_attempt
        elif matches:
            break
    return original_attempt


def verify_source_run(run_id, head_sha, version, repository=REPOSITORY, *, allow_pending=False, rebuild_linux=False, rebuild_macos=False, rebuild_tvos=False, rebuild_ios=False, rebuild_tizen=False, repaired_run=None, require_homebrew_cask=False, packaging_source_sha=None, workflow_source_sha=None, include_apple_store_build=False, tvos_run=None, ios_run=None, ios_developer_run=None, built_tvos_run=None, append_apple_only=False, qualification_run=None, qualification_assembly_run=None, current_main_sha=None, freeze=False):
    if qualification_run is not None and any((
            rebuild_linux, rebuild_macos, rebuild_ios, rebuild_tizen, repaired_run,
            require_homebrew_cask, packaging_source_sha, workflow_source_sha, include_apple_store_build,
            tvos_run, ios_run, ios_developer_run, built_tvos_run, append_apple_only)):
        raise RecoveryError("Supplemental qualification requires assembly of the original unsigned release assets except the guarded tvOS acquisition rebuild")
    if qualification_assembly_run is not None and (
            qualification_run is None or rebuild_tvos or rebuild_linux or rebuild_macos or rebuild_ios
            or rebuild_tizen or repaired_run or require_homebrew_cask or packaging_source_sha
            or workflow_source_sha or include_apple_store_build or tvos_run or ios_run
            or ios_developer_run or built_tvos_run or append_apple_only):
        raise RecoveryError('Prior qualified assembly reuse is limited to an unsigned assembly-only continuation')
    if current_main_sha is not None and qualification_assembly_run is None:
        raise RecoveryError('An explicit canonical-main revision is only valid for qualified assembly continuation')
    if ios_run and not tvos_run:
        raise RecoveryError('Signed iOS sibling reuse requires a distinct signed TV sibling')
    if built_tvos_run is not None:
        if (not packaging_source_sha or not workflow_source_sha or not include_apple_store_build
                or rebuild_tvos or rebuild_ios or repaired_run):
            raise RecoveryError('Built tvOS reuse requires frozen signed Apple packaging without another platform rebuild')
        if tvos_run or ios_run or append_apple_only or rebuild_linux or rebuild_macos:
            raise RecoveryError('Built tvOS reuse cannot be mixed with sibling, append-only, or non-Apple recovery modes')
        proof = verify_source_run(
            run_id, head_sha, version, repository, allow_pending=allow_pending,
            rebuild_tvos=True, packaging_source_sha=packaging_source_sha,
            workflow_source_sha=workflow_source_sha, include_apple_store_build=True)
        tvos = verify_completed_tvos_run(built_tvos_run, proof, repository)
        tv_job = 'Build tvOS developer package (arm64)'
        return {**proof, 'tvos_source': tvos, 'rebuild_tvos': False,
                'rebuilt_jobs': sorted(set(proof['rebuilt_jobs']) - {tv_job}),
                'reused_jobs': [*proof['reused_jobs'], tv_job]}
    if append_apple_only and (not packaging_source_sha or not workflow_source_sha
                              or not include_apple_store_build or rebuild_linux or rebuild_macos
                              or repaired_run):
        raise RecoveryError('Append-only Apple recovery requires the exact frozen packaging and signed Apple sources')
    if tvos_run and ios_run:
        if (not packaging_source_sha or not workflow_source_sha or not include_apple_store_build
                or rebuild_ios or rebuild_tvos or repaired_run or rebuild_linux or rebuild_macos):
            raise RecoveryError('Dual signed Apple reuse requires approved assembly-only recovery')
        proof = verify_source_run(run_id, head_sha, version, repository, allow_pending=allow_pending,
                                 rebuild_ios=True, rebuild_tvos=True,
                                 require_homebrew_cask=require_homebrew_cask,
                                 packaging_source_sha=packaging_source_sha, workflow_source_sha=workflow_source_sha,
                                 include_apple_store_build=True, append_apple_only=True)
        if str(tvos_run) == str(ios_run):
            apple = verify_frozen_apple_run(tvos_run, proof, repository)
            tvos, ios = apple['tvos_source'], apple['ios_source']
        else:
            tvos = verify_frozen_tvos_run(tvos_run, proof, repository)
            ios = verify_frozen_ios_run(ios_run, proof, repository)
        return {**proof, 'tvos_source': tvos, 'ios_source': ios,
                'rebuild_tvos': False, 'rebuild_ios': False,
                'rebuilt_jobs': sorted(set(proof['rebuilt_jobs']) -
                                       {'Build tvOS developer package (arm64)', 'build-ios'}),
                'reused_jobs': [*proof['reused_jobs'], 'Build tvOS developer package (arm64)', 'build-ios']}
    if tvos_run:
        if (not packaging_source_sha or not workflow_source_sha or not include_apple_store_build
                or rebuild_tvos or repaired_run or rebuild_linux or rebuild_macos or not rebuild_ios):
            raise RecoveryError('Signed TV reuse is limited to approved iOS-only sibling packaging recovery')
        proof = verify_source_run(run_id, head_sha, version, repository, allow_pending=allow_pending,
                                 rebuild_ios=True, rebuild_tvos=True, require_homebrew_cask=require_homebrew_cask,
                                 packaging_source_sha=packaging_source_sha, workflow_source_sha=workflow_source_sha,
                                 include_apple_store_build=True)
        tvos = verify_frozen_tvos_run(tvos_run, proof, repository)
        return {**proof, 'tvos_source': tvos, 'rebuild_tvos': False,
                'rebuilt_jobs': sorted(set(proof['rebuilt_jobs']) - {'Build tvOS developer package (arm64)'}),
                'reused_jobs': [*proof['reused_jobs'], 'Build tvOS developer package (arm64)']}
    if (rebuild_tvos or rebuild_ios) and repaired_run:
        raise RecoveryError("Apple device rebuilding cannot substitute repaired Linux/Mac outputs")
    if rebuild_tizen and (packaging_source_sha or workflow_source_sha
                          or rebuild_linux or rebuild_macos or rebuild_tvos or rebuild_ios
                          or repaired_run or append_apple_only):
        raise RecoveryError("Tizen packaging recovery cannot be combined with frozen packaging or unrelated rebuild modes")
    if repository != REPOSITORY or not str(run_id).isdigit() or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise RecoveryError("Recovery requires the Pleazyng repository and exact run/commit identities.")
    source = api(f"repos/{repository}/actions/runs/{run_id}")
    source_sha = source.get("head_sha", "")
    pending = (qualification_run is None and allow_pending
               and source.get("status") in {"queued", "in_progress", "waiting", "pending"})
    if ((source.get("status") != "completed" and not pending) or source.get("event") != "workflow_dispatch"
        or source.get("head_branch") != "main"
        or source.get("head_repository", {}).get("full_name") != repository
        or source.get("path", "").split("@")[0] != ".github/workflows/build.yml"
        or not trusted_versioned_release_title(source, version)
        or not re.fullmatch(r"[0-9a-f]{40}", source_sha)):
        raise RecoveryError("Source must be a trusted-main release run for this version, completed before reuse.")
    qualification = None
    qualified_apple_jobs = set()
    if qualification_run is not None:
        if source_sha != head_sha:
            raise RecoveryError("Supplemental qualification cannot assemble a different product revision")
        qualification = supplemental_ci_proof(repository, source_sha, version, run_id, qualification_run,
                                              freeze=freeze)
        qualified_apple_jobs = {"Release CI / Apple native reliability"}
        qualified_apple_jobs.update(f"Release CI / Apple native reliability ({os})" for os in ("iOS", "macOS", "tvOS"))
    comparison = api(f"repos/{repository}/compare/{source_sha}...{head_sha}")
    changed = comparison.get("files")
    if (comparison.get("status") not in {"ahead", "identical"}
        or comparison.get("merge_base_commit", {}).get("sha") != source_sha
        or not isinstance(changed, list) or len(changed) >= 300):
        raise RecoveryError("Cannot prove that the source release is an ancestor with complete file evidence.")
    frozen_repair = None
    if packaging_source_sha or workflow_source_sha:
        if (packaging_source_sha != head_sha or not workflow_source_sha
                or not include_apple_store_build or rebuild_linux or repaired_run):
            raise RecoveryError("Frozen Apple packaging requires approved source identities and full signed Apple builds")
        from verify_frozen_packaging_repair import verify_frozen_packaging_repair
        try:
            frozen_repair = verify_frozen_packaging_repair(source_sha, head_sha, workflow_source_sha, api, repository=repository)
        except (ValueError, RuntimeError) as error:
            raise RecoveryError(f"Frozen packaging approval failed: {error}") from error
    elif include_apple_store_build and not rebuild_tizen:
        raise RecoveryError("Signed Apple recovery requires an explicitly approved frozen packaging source")
    blocked = sorted({path for entry in changed
                      for path in (entry.get("filename"), entry.get("previous_filename"))
                      if path and path not in RECOVERY_FILES})
    if blocked and frozen_repair is None:
        raise RecoveryError("Application or pinned native inputs changed: " + ", ".join(blocked))
    verify_webos_link_patch(repository, source_sha, head_sha, changed)
    verify_webos_transport_patch(repository, source_sha, head_sha, changed)
    if repaired_run and (rebuild_linux or rebuild_macos):
        raise RecoveryError("Do not rebuild Linux/Mac while explicitly reusing their repaired outputs")
    repaired = verify_repaired_run(repaired_run, head_sha, version, repository, allow_pending=allow_pending, require_homebrew_cask=require_homebrew_cask) if repaired_run else None
    jobs = collection(f"repos/{repository}/actions/runs/{run_id}/jobs", "jobs")
    windows_repackaging = None
    package_jobs = [job for job in jobs if job.get("name") == "package-windows"]
    if qualification_run is not None and len(package_jobs) == 1 and package_jobs[0].get("conclusion") == "skipped":
        windows_repackaging = verify_skipped_windows_packaging(source, jobs, repository)
    pending_jobs = []
    ios_developer_source = (verify_unsigned_ios_run(ios_developer_run, head_sha, version, repository)
                            if ios_developer_run else None)
    required_jobs = tuple(name for name in REQUIRED_JOBS
                          if not ((rebuild_linux or repaired) and name.startswith("Build Linux ("))
                          and not ((rebuild_macos or repaired or append_apple_only) and name == "build-macos")
                          and not ((rebuild_tvos or qualification_assembly_run is not None)
                                   and name == "Build tvOS developer package (arm64)")
                          and not ((rebuild_ios or ios_developer_source) and name == "build-ios")
                          and not (windows_repackaging and name == "package-windows")
                          and not (rebuild_tizen and name == TIZEN_PACKAGE_JOB))
    if append_apple_only:
        macos_jobs = [job for job in jobs if job.get('name') == 'build-macos']
        if (len(macos_jobs) != 1 or macos_jobs[0].get('status') != 'completed'
                or macos_jobs[0].get('conclusion') not in {'success', 'failure'}):
            raise RecoveryError('Append-only Apple recovery requires a completed original Mac job')
    rebuilt_jobs = set(REBUILT_JOBS) | ({"Build Linux (x64)", "Build Linux (arm64)"} if rebuild_linux else set()) | ({"build-macos"} if rebuild_macos else set()) | ({"Build tvOS developer package (arm64)"} if rebuild_tvos else set()) | ({"build-ios"} if rebuild_ios else set()) | ({TIZEN_PACKAGE_JOB} if rebuild_tizen else set())
    if qualification_run is not None and (rebuild_tvos or qualification_assembly_run is not None):
        tvos_jobs = [job for job in jobs if job.get("name") == "Build tvOS developer package (arm64)"]
        if len(tvos_jobs) != 1:
            raise RecoveryError("Supplemental tvOS rebuild requires exactly one original tvOS package job")
        tvos_job = tvos_jobs[0]
        labels = tvos_job.get("labels")
        if (source.get("status") != "completed"
                or tvos_job.get("status") != "completed"
                or tvos_job.get("conclusion") != "cancelled"
                or not isinstance(labels, list) or "macos-26" not in labels
                or tvos_job.get("runner_id") not in (None, 0)
                or tvos_job.get("runner_name") not in (None, "")
                or not isinstance(tvos_job.get("steps"), list) or tvos_job["steps"]
                or tvos_job.get("run_id") != int(run_id)
                or tvos_job.get("run_attempt") != source.get("run_attempt")
                or tvos_job.get("head_sha") != source_sha):
            raise RecoveryError(
                "Supplemental tvOS reuse only accepts the original exact-source package cancelled before macos-26 runner acquisition")
    for name in required_jobs:
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) == 1 and pending and matches[0].get("status") in {"queued", "in_progress", "waiting", "pending", "requested"} and not matches[0].get("conclusion"):
            pending_jobs.append(name)
            continue
        if len(matches) != 1 or matches[0].get("status") != "completed" or matches[0].get("conclusion") != "success":
            raise RecoveryError(f"Source platform job is not uniquely successful: {name}")
    if include_apple_store_build:
        for name, rebuilt, step in (
            ('build-ios', rebuild_ios, 'Build signed iOS App Store IPA'),
            ('Build tvOS developer package (arm64)', rebuild_tvos, 'Build signed tvOS App Store IPA'),
        ):
            if rebuilt or name in pending_jobs:
                continue
            job = next(job for job in jobs if job.get('name') == name)
            if name == 'Build tvOS developer package (arm64)':
                required_steps = (step, 'Package unsigned tvOS developer IPA',
                                  'Verify unsigned tvOS runtime dependencies',
                                  'Attest unsigned tvOS checkpoint', 'Upload durable unsigned tvOS checkpoint',
                                  'Attest unsigned tvOS developer package', 'Verify tvOS runtime dependencies')
            else:
                required_steps = (step,)
            for required_step in required_steps:
                matches = [item for item in job.get('steps', []) if item.get('name') == required_step]
                if (len(matches) != 1 or matches[0].get('conclusion') != 'success'
                        or matches[0].get('status') != 'completed'):
                    raise RecoveryError(
                        f'Reusable Apple platform lacks a required signed Store qualification step: {name}/{required_step}')
    original_tvos_attempt = None
    original_tvos_has_store = False
    if not rebuild_tvos and qualification_assembly_run is None:
        tvos_job = next(job for job in jobs if job.get('name') == 'Build tvOS developer package (arm64)')
        if 'Build tvOS developer package (arm64)' not in pending_jobs:
            original_tvos_attempt = tvos_job.get('run_attempt')
            if original_tvos_attempt is not None and (
                    type(original_tvos_attempt) is not int or type(source.get('run_attempt')) is not int
                    or not 1 <= original_tvos_attempt <= source['run_attempt'] <= 100
                    or tvos_job.get('run_id') != int(run_id) or tvos_job.get('head_sha') != source_sha):
                raise RecoveryError('Reusable tvOS job has invalid original attempt evidence')
            if qualification_run is not None and original_tvos_attempt is None:
                raise RecoveryError('Supplemental qualification requires the original tvOS build attempt')
            if original_tvos_attempt is not None and tvos_job.get('started_at'):
                original_tvos_attempt = original_tvos_execution_attempt(repository, run_id, tvos_job)
            original_tvos_has_store = any(
                step.get('name') == 'Build signed tvOS App Store IPA' and step.get('conclusion') == 'success'
                for step in tvos_job.get('steps', []))
            if qualification_run is not None and original_tvos_has_store:
                raise RecoveryError('Supplemental qualification assembles the original unsigned platform release')
            for required_step in (
                    'Package unsigned tvOS developer IPA',
                    'Verify unsigned tvOS runtime dependencies',
                    'Attest unsigned tvOS checkpoint',
                    'Upload durable unsigned tvOS checkpoint',
                    'Verify tvOS runtime dependencies',
                    'Attest unsigned tvOS developer package',
                    'Upload tvOS package'):
                matches = [item for item in tvos_job.get('steps', []) if item.get('name') == required_step]
                if (len(matches) != 1 or matches[0].get('conclusion') != 'success'
                        or matches[0].get('status') != 'completed'):
                    raise RecoveryError(
                        f'Reusable tvOS artifact lacks runtime dependency qualification: {required_step}')
    failures = [job.get("name") for job in jobs
                if job.get("conclusion") in {"failure", "cancelled", "timed_out"}
                and job.get("name") not in rebuilt_jobs | qualified_apple_jobs | set(repaired["reused_jobs"] if repaired else [])
                | ({"build-ios"} if ios_developer_source else set())
                | ({"Build tvOS developer package (arm64)"}
                   if qualification_assembly_run is not None else set())
                and not (append_apple_only and job.get("name") == "build-macos"
                         and job.get("status") == "completed" and job.get("conclusion") == "failure")
                and not (job.get("name") == "create-release" and job.get("conclusion") == "cancelled"
                         and job.get("runner_id") in (None, 0) and job.get("runner_name") in (None, "")
                         and isinstance(job.get("steps"), list) and not job["steps"])]
    if failures:
        raise RecoveryError("Other source jobs failed: " + ", ".join(failures))
    android = [job for job in jobs if job.get("name") == "build-android"]
    android_pending = len(android) == 1 and pending and android[0].get("status") in {"queued", "in_progress", "waiting", "pending", "requested"} and not android[0].get("conclusion")
    if android_pending:
        pending_jobs.append("build-android")
    elif len(android) != 1 or android[0].get("status") != "completed" or android[0].get("conclusion") not in {"failure", "success", "cancelled", "timed_out"}:
        raise RecoveryError("Recovery requires exactly one completed Android build.")
    if any(job.get("name") == "create-release" and job.get("conclusion") != "skipped"
           and not (job.get("status") == "completed" and job.get("conclusion") == "cancelled"
                    and job.get("runner_id") in (None, 0) and job.get("runner_name") in (None, "")
                    and isinstance(job.get("steps"), list) and not job["steps"])
           and not (pending and job.get("status") in {"queued", "waiting", "pending"} and not job.get("conclusion"))
           for job in jobs):
        raise RecoveryError("The source run already attempted release creation; inspect its side effects.")
    qualified_assembly = None
    if qualification_assembly_run is not None:
        qualified_assembly = verify_qualification_assembly_reuse(
            {'source_run_id': int(run_id), 'source_sha': source_sha, 'version': version,
             'qualification_run_id': qualification_run, 'windows_repackaging': windows_repackaging},
            qualification_assembly_run, repository, current_main_sha=current_main_sha)
    macos = next(job for job in jobs if job.get("name") == "build-macos")
    cask_qualified = all(any(step.get("name") == name and step.get("status") == "completed" and step.get("conclusion") == "success" for step in macos.get("steps", []))
                         for name in ("Notarize Application", "Wait for notarization acceptance",
                                      "Verify Gatekeeper acceptance for Homebrew Cask"))
    if require_homebrew_cask and not rebuild_macos and not repaired and macos.get("status") == "completed" and not cask_qualified:
        raise RecoveryError("Reusable Mac build lacks successful notarization and Homebrew Gatekeeper qualification")
    reused_jobs = [*required_jobs, *( ["build-ios"] if ios_developer_source else []),
                   *(qualified_assembly['reused_assembly_jobs'] if qualified_assembly else [])]
    return {"source_run_id": int(run_id), "source_sha": source_sha, "head_sha": head_sha,
            "run_attempt": source.get("run_attempt"),
            "version": version, "reused_jobs": reused_jobs,
            "repackage_windows": windows_repackaging is not None and qualified_assembly is None,
            **({"windows_repackaging": qualified_assembly['windows_repackaging'] if qualified_assembly
                else windows_repackaging} if windows_repackaging else {}),
            **({"qualification_assembly_run_id": qualified_assembly['qualification_assembly_run_id'],
                "qualification_assembly_source": qualified_assembly['qualification_assembly_source'],
                "tvos_source": qualified_assembly['tvos_source']}
               if qualified_assembly else {}),
            "rebuilt_jobs": sorted(rebuilt_jobs), "rebuild_linux": rebuild_linux, "rebuild_macos": rebuild_macos, "rebuild_tvos": rebuild_tvos, "rebuild_ios": rebuild_ios, "rebuild_tizen": rebuild_tizen,
            "macos_cask_qualified": repaired["macos_cask_qualified"] if repaired else cask_qualified,
            "webos_link_compatibility_repaired": any(entry.get("filename") == "webos/CMakeLists.txt" for entry in changed),
            "platforms_complete": source.get("status") == "completed" and (not repaired or repaired["platforms_complete"]),
            "pending_jobs": pending_jobs + [f"repaired:{name}" for name in (repaired["pending_jobs"] if repaired else [])],
            **({"repaired_source": repaired} if repaired else {}),
            **({"ios_developer_source": ios_developer_source} if ios_developer_source else {}),
            **(qualification if qualification is not None else
               ci_proof(repository, source_sha, version, source_run=run_id)),
            **({"qualification_run_id": qualification_run} if qualification is not None else {}),
            **({"original_tvos_attempt": original_tvos_attempt} if original_tvos_attempt is not None else {}),
            **({'schema_version': 2, 'recovery_policy': 'frozen-apple-packaging-v1',
                'product_source_sha': source_sha, 'packaging_source_sha': head_sha,
                'workflow_source_sha': workflow_source_sha, 'packaging_approval': frozen_repair,
                'include_apple_store_build': True} if frozen_repair is not None else {}),
            **({'runtime_dependency_receipt_required': True, 'original_tvos_has_store': original_tvos_has_store}
               if not rebuild_tvos else {}),
            **({'append_apple_only': True,
                'reused_release_assets': ['SHA256SUMS.txt', 'pleazyng-macos.dmg']}
               if append_apple_only else {})}


def verify_macos_packaging_recovery(run_id, macos_run, head_sha, version, repository=REPOSITORY):
    """Replace only failed Mac packaging; require every original platform build."""
    import base64
    from recover_macos_app import build_contract
    if repository != REPOSITORY or not all(str(v).isdigit() for v in (run_id, macos_run)) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise RecoveryError("Mac recovery requires exact trusted run and source identities.")
    run_id = int(run_id)
    macos_run = int(macos_run)
    source = api(f"repos/{repository}/actions/runs/{run_id}")
    native = api(f"repos/{repository}/actions/runs/{macos_run}")
    for run in (source, native):
        if (run.get("status") != "completed" or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != "main" or run.get("head_repository", {}).get("full_name") != repository
            or run.get("path", "").split("@")[0] != ".github/workflows/build.yml"):
            raise RecoveryError("Mac recovery requires completed trusted-main workflow runs.")
    if not trusted_versioned_release_title(source, version) or native.get("conclusion") != "success":
        raise RecoveryError("Mac packaging recovery must be successful and match the original versioned release.")
    source_sha = source["head_sha"]
    for target in dict.fromkeys((head_sha, native["head_sha"])):
        comparison = api(f"repos/{repository}/compare/{source_sha}...{target}")
        files = comparison.get("files")
        if (comparison.get("status") not in {"ahead", "identical"}
            or comparison.get("merge_base_commit", {}).get("sha") != source_sha
            or not isinstance(files, list) or len(files) >= 300
            or any(p not in RECOVERY_FILES for entry in files
                   for p in (entry.get("filename"), entry.get("previous_filename")) if p)):
            raise RecoveryError("Mac recovery changed application or native inputs, or comparison evidence is incomplete.")
        verify_webos_link_patch(repository, source_sha, target, files)
    contracts = []
    for revision in dict.fromkeys((source_sha, native["head_sha"], head_sha)):
        content = api(f"repos/{repository}/contents/.github/workflows/build.yml?ref={revision}")
        contracts.append(build_contract(base64.b64decode(content["content"]).decode()))
    if any(c != contracts[0] for c in contracts):
        raise RecoveryError("Mac compiler configuration differs from the retained compilation.")
    jobs = collection(f"repos/{repository}/actions/runs/{run_id}/jobs", "jobs")
    required = tuple(name for name in REQUIRED_JOBS if name != "build-macos") + ("build-android", "Build WebOS TV package (ARMv7)")
    for name in required:
        matches = [j for j in jobs if j.get("name") == name]
        if len(matches) != 1 or matches[0].get("status") != "completed" or matches[0].get("conclusion") != "success":
            raise RecoveryError(f"Original platform must be uniquely successful: {name}")
    macs = [j for j in jobs if j.get("name") == "build-macos"]
    if len(macs) != 1 or macs[0].get("conclusion") != "failure":
        raise RecoveryError("Expected exactly one failed Mac packaging job.")
    mac = api(f"repos/{repository}/actions/jobs/{macs[0]['id']}")
    if not any(s.get("name") == "Build macOS" and s.get("conclusion") == "success" for s in mac.get("steps", [])):
        raise RecoveryError("The original native application compilation did not succeed.")
    checkpoint_inventory = collection(f'repos/{repository}/actions/runs/{run_id}/artifacts', 'artifacts')
    checkpoint_name = f'macos-compiled-app-{run_id}-{mac.get("run_attempt")}'
    checkpoint_matches = [item for item in checkpoint_inventory
                          if item.get('name') == checkpoint_name and item.get('expired') is False]
    compilation_reused = bool(checkpoint_matches)
    if compilation_reused:
        if (len(checkpoint_matches) != 1
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(checkpoint_matches[0].get('digest', '')))
                or (checkpoint_matches[0].get('workflow_run') or {}).get('head_sha') != source_sha):
            raise RecoveryError('Original Mac compilation checkpoint is ambiguous or has an invalid digest.')
        checkpoint_metadata = api(f'repos/{repository}/actions/artifacts/{checkpoint_matches[0]["id"]}')
        if (checkpoint_metadata.get('id') != checkpoint_matches[0]['id']
                or checkpoint_metadata.get('name') != checkpoint_name
                or checkpoint_metadata.get('expired') is not False
                or checkpoint_metadata.get('digest') != checkpoint_matches[0]['digest']
                or (checkpoint_metadata.get('workflow_run') or {}).get('id') != run_id
                or (checkpoint_metadata.get('workflow_run') or {}).get('head_sha') != source_sha):
            raise RecoveryError('Durable Mac compilation checkpoint metadata changed after selection.')
    elif not any(s.get('name') == 'Create portable unsigned macOS compilation checkpoint'
                 and s.get('conclusion') == 'failure' for s in mac.get('steps', [])):
        raise RecoveryError('Original Mac failure did not occur while creating its app checkpoint.')
    if any(j.get("conclusion") in {"failure", "cancelled", "timed_out"} and j.get("name") != "build-macos" for j in jobs):
        raise RecoveryError("Another original platform failed; do not assemble a partial release.")
    if any(j.get("name") == "create-release" and j.get("conclusion") != "skipped" for j in jobs):
        raise RecoveryError("Original run attempted release creation; inspect side effects.")
    recovered_jobs = collection(f"repos/{repository}/actions/runs/{macos_run}/jobs", "jobs")
    recovered = [j for j in recovered_jobs if j.get("name") == "build-macos" and j.get("conclusion") == "success"]
    if len(recovered) != 1:
        raise RecoveryError("Mac replacement job is missing or ambiguous.")
    recovered_job = api(f"repos/{repository}/actions/jobs/{recovered[0]['id']}")
    from release_job_inventory import mac_package_evidence
    recovered_job = next(job for job in mac_package_evidence(
        [recovered_job, *(j for j in recovered_jobs if j.get("name") != "build-macos")])
        if job.get("name") == "build-macos")
    if compilation_reused:
        required_steps = ("Recover verified native macOS compilation", "Import Code Signing Certificate",
                          "Sign Application", "Verify Signature", "Notarize Application",
                          "Wait for notarization acceptance",
                          "Verify Gatekeeper acceptance for Homebrew Cask", "Sign DMG for Sparkle (EdDSA)",
                          "Attest macOS DMG", "Attest recovered macOS compilation provenance", "Upload macOS DMG")
    else:
        expected_title = f"action:release:pool:public:tag:v{version}:recovery:source:{run_id}:macos"
        if native.get('display_title') != expected_title:
            raise RecoveryError('A fresh Mac rebuild must have the distinct recovery run identity.')
        required_steps = ("Build macOS", "Create portable unsigned macOS compilation checkpoint",
                          "Attest portable macOS compilation checkpoint", "Upload portable macOS compilation checkpoint",
                          "Create DMG", "Sign DMG for Sparkle (EdDSA)", "Write macOS signature metadata",
                          "Attest macOS DMG", "Upload macOS DMG")
    for name in required_steps:
        if not any(s.get("name") == name and s.get("conclusion") == "success" for s in recovered_job.get("steps", [])):
            raise RecoveryError(f"Recovered Mac package lacks successful qualification: {name}")
    if not compilation_reused:
        native_artifacts = collection(f"repos/{repository}/actions/runs/{macos_run}/artifacts", "artifacts")
        dmg_artifacts = [item for item in native_artifacts if item.get('name') == 'macos-dmg'
                         and item.get('expired') is False]
        if (len(dmg_artifacts) != 1
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(dmg_artifacts[0].get('digest', '')))
                or (dmg_artifacts[0].get('workflow_run') or {}).get('id') != macos_run
                or (dmg_artifacts[0].get('workflow_run') or {}).get('head_sha') != native['head_sha']):
            raise RecoveryError('Fresh Mac rebuild lacks its unique digest-bound DMG artifact.')
    return {"profile": "macos-packaging", "source_run_id": int(run_id), "source_sha": source_sha,
            "head_sha": head_sha, "version": version, "reused_jobs": list(required),
            "rebuilt_jobs": [], "recovered_jobs": ["build-macos"], "platforms_complete": True,
            "pending_jobs": [], "excluded_optional_jobs": [],
            "compiled_macos_job": mac["id"] if compilation_reused else recovered_job["id"],
            "macos_source": {"source_run_id": int(macos_run), "source_sha": native["head_sha"],
                             "job_id": recovered_job["id"], "compilation_reused": compilation_reused},
            "android_source": {"source_run_id": int(run_id), "source_sha": source_sha},
            **ci_proof(repository, source_sha, version, source_run=run_id)}


def verify_macos_receipt(receipt, proof):
    if (receipt.get("compiled_source") != proof["source_sha"]
        or receipt.get("compiled_run") != proof["source_run_id"]
        or receipt.get("compiled_job") != proof["compiled_macos_job"]
        or receipt.get("packaging_workflow_source") != proof["macos_source"]["source_sha"]
        or receipt.get("version", "").split("+", 1)[0] != proof["version"]
        or receipt.get("application_inputs_unchanged") is not True
        or receipt.get("compilation_reused") is not True
        or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("recovered_app_tree_sha256", ""))):
        raise RecoveryError("Mac compilation receipt differs from verified GitHub evidence.")


def wait_for_source_run(run_id, head_sha, version, seconds, **options):
    deadline = time.monotonic() + seconds
    while True:
        proof = verify_source_run(run_id, head_sha, version, allow_pending=True, **options)
        if proof["platforms_complete"]:
            return proof
        if time.monotonic() >= deadline:
            raise RecoveryError("Timed out waiting for successful source platform builds; no artifacts reused.")
        print(f"Source run {run_id} is still building: {', '.join(proof['pending_jobs'])}.", flush=True)
        time.sleep(min(60, max(0, deadline - time.monotonic())))


def verify_android_run(run_id, head_sha, version, repository=REPOSITORY, *, allow_pending=False):
    if repository != REPOSITORY or not str(run_id).isdigit() or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise RecoveryError("Android recovery requires an exact Pleazyng run identity.")
    source = api(f"repos/{repository}/actions/runs/{run_id}")
    source_sha = source.get("head_sha", "")
    pending = allow_pending and source.get("status") in {"queued", "in_progress", "waiting", "pending"}
    if ((source.get("status") != "completed" and not pending) or source.get("event") != "workflow_dispatch"
        or source.get("head_branch") != "main"
        or source.get("head_repository", {}).get("full_name") != repository
        or source.get("path", "").split("@")[0] != ".github/workflows/build.yml"
        or not trusted_versioned_release_title(source, version)
        or not re.fullmatch(r"[0-9a-f]{40}", source_sha)):
        raise RecoveryError("Android source must be a completed trusted-main release for this version.")
    comparison = api(f"repos/{repository}/compare/{source_sha}...{head_sha}")
    files = comparison.get("files")
    if (comparison.get("status") not in {"ahead", "identical"}
        or comparison.get("merge_base_commit", {}).get("sha") != source_sha
        or not isinstance(files, list) or len(files) >= 300
        or any(path not in RECOVERY_FILES for entry in files
               for path in (entry.get("filename"), entry.get("previous_filename")) if path)):
        raise RecoveryError("Android product input equivalence could not be proven.")
    verify_webos_transport_patch(repository, source_sha, head_sha, files)
    verify_webos_link_patch(repository, source_sha, head_sha, files)
    jobs = collection(f"repos/{repository}/actions/runs/{run_id}/jobs", "jobs")
    android = [job for job in jobs if job.get("name") == "build-android"]
    android_pending = len(android) == 1 and pending and android[0].get("status") in {"queued", "in_progress", "waiting", "pending", "requested"} and not android[0].get("conclusion")
    if not android_pending and (len(android) != 1 or android[0].get("status") != "completed" or android[0].get("conclusion") != "success"):
        raise RecoveryError("The reused Android job must be uniquely successful.")
    if any(job.get("name") == "create-release" and job.get("conclusion") != "skipped"
           and not (pending and job.get("status") in {"queued", "waiting", "pending"} and not job.get("conclusion")) for job in jobs):
        raise RecoveryError("Android source already attempted publication; inspect its side effects.")
    return {"source_run_id": int(run_id), "source_sha": source_sha, "job_id": android[0]["id"],
            **({"pending": True} if android_pending else {})}


def compose_proof(proof, android_run=None, include_webos_build=True, webos_run=None, ios_developer_run=None):
    if proof.get('qualification_run_id'):
        source_run = proof['source_run_id']
        if str(android_run) != str(source_run) or str(webos_run) != str(source_run) or not include_webos_build:
            raise RecoveryError('Supplemental qualification requires every package from the original release run')
        jobs = collection(f'repos/{REPOSITORY}/actions/runs/{source_run}/jobs', 'jobs')
        aliases = {}
        for key, name in (('android_source', 'build-android'), ('webos_source', 'Build WebOS TV package (ARMv7)')):
            matches = [job for job in jobs if job.get('name') == name]
            if (len(matches) != 1 or matches[0].get('status') != 'completed'
                    or matches[0].get('conclusion') != 'success'):
                raise RecoveryError(f'Original platform must be uniquely successful: {name}')
            aliases[key] = {'source_run_id': source_run, 'source_sha': proof['source_sha'],
                            'job_id': matches[0]['id'], 'platforms_complete': True}
        rebuilt = set(proof.get('rebuilt_jobs', [])) - {'build-android', 'Build WebOS TV package (ARMv7)'}
        expected_rebuilt = {'Build tvOS developer package (arm64)'} if proof.get('rebuild_tvos') else set()
        if rebuilt != expected_rebuilt:
            raise RecoveryError('Supplemental qualification contains an unexpected package rebuild')
        return {**proof, **aliases, 'rebuilt_jobs': sorted(rebuilt), 'excluded_optional_jobs': []}
    if proof.get('recovery_policy') == 'frozen-apple-packaging-v1':
        source_run = proof['source_run_id']
        if str(android_run) != str(source_run) or not include_webos_build:
            raise RecoveryError('Frozen product repair must reuse Android and include webOS')
        jobs = collection(f'repos/{REPOSITORY}/actions/runs/{source_run}/jobs', 'jobs')
        aliases = {}
        for key, name in (('android_source', 'build-android'),):
            matches = [job for job in jobs if job.get('name') == name]
            if (len(matches) != 1 or matches[0].get('status') != 'completed'
                    or matches[0].get('conclusion') != 'success'):
                raise RecoveryError(f'Original reused platform is not uniquely successful: {name}')
            aliases[key] = {'source_run_id': source_run, 'source_sha': proof['source_sha'],
                            'job_id': matches[0]['id'], 'platforms_complete': True}
        rebuilt = set(proof['rebuilt_jobs']) - {'build-android'}
        if str(webos_run) == str(source_run):
            matches = [job for job in jobs if job.get('name') == 'Build WebOS TV package (ARMv7)']
            if (len(matches) != 1 or matches[0].get('status') != 'completed'
                    or matches[0].get('conclusion') != 'success'):
                raise RecoveryError('Original webOS output is not uniquely successful')
            aliases['webos_source'] = {'source_run_id': source_run, 'source_sha': proof['source_sha'],
                                       'job_id': matches[0]['id'], 'platforms_complete': True}
            rebuilt.discard('Build WebOS TV package (ARMv7)')
        elif webos_run is not None:
            raise RecoveryError('Frozen product repair cannot substitute an unrelated webOS run')
        return {**proof, **aliases, 'rebuilt_jobs': sorted(rebuilt), 'excluded_optional_jobs': []}
    if proof.get("profile") == "macos-packaging":
        if webos_run:
            raise RecoveryError("Signing-only recovery cannot substitute another webOS source")
        if not include_webos_build or (android_run and str(android_run) != str(proof["source_run_id"])):
            raise RecoveryError("Mac packaging recovery requires every original platform without substitution or omission.")
        return proof
    if "build-android" in proof.get("pending_jobs", []) and not android_run:
        raise RecoveryError("A pending Android source requires explicit reuse; do not launch a duplicate build")
    rebuilt = set(proof.get("rebuilt_jobs", REBUILT_JOBS))
    if ios_developer_run:
        ios_source = proof.get("ios_developer_source") or verify_unsigned_ios_run(
            ios_developer_run, proof["head_sha"], proof["version"])
        proof = {**proof, "ios_developer_source": ios_source}
        rebuilt.discard("build-ios")
    if android_run:
        proof = {**proof, "android_source": verify_android_run(android_run, proof["head_sha"], proof["version"], allow_pending=not proof.get("platforms_complete", True))}
        rebuilt.remove("build-android")
    if webos_run:
        if not include_webos_build:
            raise RecoveryError("Verified webOS reuse must remain included in the complete release")
        proof = {**proof, "webos_source": verify_repaired_run(webos_run, proof["head_sha"], proof["version"], REPOSITORY, allow_pending=not proof.get("platforms_complete", True), webos_only=True)}
        if not proof["webos_source"]["platforms_complete"]:
            raise RecoveryError("WebOS source must be complete before assembly-only recovery")
        rebuilt.remove("Build WebOS TV package (ARMv7)")
    if not include_webos_build:
        rebuilt.remove("Build WebOS TV package (ARMv7)")
    return {**proof, "rebuilt_jobs": sorted(rebuilt),
            "excluded_optional_jobs": [] if include_webos_build else ["Build WebOS TV package (ARMv7)"]}


def extract_verified(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise RecoveryError("Artifact destination must not be a symlink.")
    with zipfile.ZipFile(archive) as source:
        if sum(member.file_size for member in source.infolist()) > 20 * 1024**3:
            raise RecoveryError("Reusable artifact exceeds the extraction budget.")
        seen = set()
        for member in source.infolist():
            path = PurePosixPath(member.filename)
            if (not path.parts or path.is_absolute() or ".." in path.parts or "\\" in member.filename
                or ":" in member.filename or str(path) in seen
                or stat.S_ISLNK(member.external_attr >> 16)):
                raise RecoveryError("Reusable artifact contains an unsafe member.")
            seen.add(str(path))
            target = destination.joinpath(*path.parts)
            if any(parent.is_symlink() for parent in (target, *target.parents)):
                raise RecoveryError("Artifact extraction must not follow symlinks.")
            if target.exists() and not member.is_dir():
                raise RecoveryError(f"Artifact would overwrite another platform file: {target}")
        source.extractall(destination)


def download_windows_build_artifact(proof, destination, repository=REPOSITORY):
    """Download and safely extract the raw x64 checkpoint for trusted repackaging."""
    evidence = proof.get("windows_repackaging")
    if not proof.get("repackage_windows") or not isinstance(evidence, dict):
        raise RecoveryError("Windows source build download requires validated supplemental repackage proof")
    inventory = collection(
        f"repos/{repository}/actions/runs/{proof['source_run_id']}/artifacts", "artifacts")
    artifact = verified_run_artifact(
        inventory, WINDOWS_RAW_X64_ARTIFACT, proof["source_run_id"], proof["source_sha"], repository)
    if artifact.get("id") != evidence.get("artifact_id") or artifact.get("digest") != evidence.get("artifact_digest"):
        raise RecoveryError("Windows x64 checkpoint artifact changed after source approval")
    with tempfile.TemporaryDirectory(prefix="pleazyng-windows-checkpoint-") as scratch:
        archive = Path(scratch) / "artifact.zip"
        with archive.open("wb") as output:
            result = subprocess.run(["gh", "api", "--allow-escape-sequences",
                                     f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"],
                                    stdout=output, stderr=subprocess.PIPE, text=True)
        with archive.open("rb") as downloaded:
            actual_digest = hashlib.file_digest(downloaded, "sha256").hexdigest()
        if result.returncode or actual_digest != artifact["digest"][7:]:
            raise RecoveryError("Windows x64 checkpoint download failed digest verification")
        extract_verified(archive, destination)
    if not (destination / "pleazyng.exe").is_file():
        raise RecoveryError("Windows x64 checkpoint is missing pleazyng.exe")
    return evidence


def tvos_package_names(source):
    names = ('pleazyng-tvos-unsigned.ipa',)
    if source.get('include_apple_store_build', True):
        return (*names, 'pleazyng-tvos-appstore.ipa')
    return names


def verify_windows_package_attestation(directory, artifact_name, source, repository=REPOSITORY):
    """Verify prior Windows package subjects against their exact producer run."""
    subjects = {
        'windows-x64-portable': ('pleazyng-windows-x64-portable.7z',),
        'windows-installer': ('pleazyng-windows-installer.exe',),
        'windows-chocolatey': (f'pleazyng.{source["version"]}.nupkg',),
    }.get(artifact_name)
    if (repository != REPOSITORY or not subjects
            or not re.fullmatch(r'[0-9a-f]{40}', str(source.get('workflow_source_sha', '')))
            or type(source.get('source_run_id')) is not int
            or type(source.get('run_attempt')) is not int):
        raise RecoveryError(f'Prior Windows package lacks a trusted attestation identity: {artifact_name}')
    digests = {}
    for name in subjects:
        path = directory / name
        if not path.is_file() or path.is_symlink():
            raise RecoveryError(f'Prior Windows artifact is missing its attested package: {name}')
        result = subprocess.run(
            ['gh', 'attestation', 'verify', str(path), '--repo', repository,
             '--signer-workflow', repository + '/.github/workflows/build.yml',
             '--source-digest', source['workflow_source_sha'], '--format', 'json'],
            check=True, capture_output=True, text=True)
        try:
            verification = json.loads(result.stdout)
            certificates = [item.get('verificationResult', {}).get('signature', {}).get('certificate', {})
                            for item in verification]
        except (AttributeError, TypeError, ValueError) as error:
            raise RecoveryError(f'Prior Windows artifact attestation is unreadable: {name}') from error
        expected_uri = f'https://github.com/{repository}/actions/runs/{source["source_run_id"]}'
        if not certificates or any(
                str(cert.get('sourceRepositoryIdentifier')) != '1433487464'
                or cert.get('sourceRepositoryDigest') != source['workflow_source_sha']
                or cert.get('runInvocationURI') not in
                (expected_uri, expected_uri + '/attempts/' + str(source['run_attempt']))
                for cert in certificates):
            raise RecoveryError(f'Prior Windows package attestation does not bind its verified assembly run: {name}')
        with path.open('rb') as package:
            digests[name] = hashlib.file_digest(package, 'sha256').hexdigest()
    return digests


def verify_tvos_package_attestations(directory, source, repository=REPOSITORY):
    digests = {}
    for name in tvos_package_names(source):
        path = directory / name
        if not path.is_file() or path.is_symlink():
            raise RecoveryError(f'Reused TV archive is missing required package: {name}')
        result = subprocess.run(['gh', 'attestation', 'verify', str(path), '--repo', repository,
                                 '--signer-workflow', repository + '/.github/workflows/build.yml',
                                 '--source-digest', source['workflow_source_sha'], '--format', 'json'],
                                check=True, capture_output=True, text=True)
        certificates = [item.get('verificationResult', {}).get('signature', {}).get('certificate', {})
                        for item in json.loads(result.stdout)]
        expected_uri = f'https://github.com/{repository}/actions/runs/{source["source_run_id"]}'
        if not certificates or any(
                str(cert.get('sourceRepositoryIdentifier')) != '1433487464'
                or cert.get('sourceRepositoryDigest') != source['workflow_source_sha']
                or cert.get('runInvocationURI') not in (expected_uri, expected_uri + '/attempts/' + str(source['run_attempt']))
                for cert in certificates):
            raise RecoveryError('Reused TV IPA attestation does not bind the original workflow and run')
        with path.open('rb') as package:
            digests[name] = hashlib.file_digest(package, 'sha256').hexdigest()
    return digests


def _tvos_product_version_build(source, repository):
    product_sha = source.get('product_source_sha')
    if not isinstance(product_sha, str) or not re.fullmatch(r'[0-9a-f]{40}', product_sha):
        raise RecoveryError('tvOS dependency receipt lacks the exact product source SHA')
    content = immutable_content(repository, product_sha, 'pubspec.yaml')
    match = re.search(r'(?m)^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\+([0-9]+)\s*$', content)
    if not match or match.group(1) != source.get('version'):
        raise RecoveryError('tvOS dependency receipt product version/build cannot be bound to frozen source')
    return match.group(1), match.group(2)


def _ipa_runtime_inventory(ipa_path, expected_version, expected_build):
    try:
        with zipfile.ZipFile(ipa_path) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            names = [item.filename for item in files]
            if len(names) != len(set(names)):
                raise RecoveryError(f'tvOS IPA contains duplicate ZIP paths: {ipa_path.name}')
            for name in names:
                member = PurePosixPath(name)
                if (member.is_absolute() or '..' in member.parts or '\\' in name or ':' in name
                        or not member.parts or str(member) != name):
                    raise RecoveryError(f'tvOS IPA contains an unsafe ZIP path: {name}')
            app_info_paths = [PurePosixPath(name) for name in names
                              if PurePosixPath(name).parts[:1] == ('Payload',)
                              and len(PurePosixPath(name).parts) == 3
                              and PurePosixPath(name).parts[1].endswith('.app')
                              and PurePosixPath(name).name == 'Info.plist']
            if len(app_info_paths) != 1:
                raise RecoveryError(f'tvOS IPA must have exactly one app Info.plist: {ipa_path.name}')
            info_path = app_info_paths[0]
            info_item = archive.getinfo(str(info_path))
            if stat.S_ISLNK(info_item.external_attr >> 16):
                raise RecoveryError(f'tvOS IPA app Info.plist is a symlink: {ipa_path.name}')
            try:
                app_info = plistlib.loads(archive.read(info_item))
            except (OSError, plistlib.InvalidFileException, ValueError) as error:
                raise RecoveryError(f'tvOS IPA app Info.plist is invalid: {ipa_path.name}') from error
            app_root = info_path.parent
            if (not isinstance(app_info, dict)
                    or app_info.get('CFBundleIdentifier') != 'org.snapetech.pleazyng'
                    or app_info.get('DTPlatformName') != 'appletvos'
                    or app_info.get('CFBundleShortVersionString') != expected_version
                    or str(app_info.get('CFBundleVersion')) != expected_build):
                raise RecoveryError(f'tvOS IPA app identity differs from frozen product: {ipa_path.name}')

            images = {}
            for item in files:
                path = PurePosixPath(item.filename)
                if app_root not in path.parents or stat.S_ISLNK(item.external_attr >> 16):
                    continue
                with archive.open(item) as stream:
                    first = stream.read(4)
                    if first not in TVOS_MACHO_MAGICS:
                        continue
                    digest = hashlib.sha256(first)
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                images[item.filename] = digest.hexdigest()
            if not images:
                raise RecoveryError(f'tvOS IPA contains no bundled Mach-O images: {ipa_path.name}')
            return images
    except (OSError, zipfile.BadZipFile) as error:
        raise RecoveryError(f'Cannot inspect tvOS IPA from runtime receipt: {ipa_path.name}') from error


def _validate_tvos_runtime_input(record, ipa_path, expected):
    if not isinstance(record, dict):
        raise RecoveryError('tvOS dependency receipt input is not an object')
    identity = {
        'artifact_name': 'tvos-ipa',
        'filename': ipa_path.name,
        'platform': 'tvos',
        'status': 'passed',
        'bundle_id': 'org.snapetech.pleazyng',
        'product_version': expected['product_version'],
        'product_build': expected['product_build'],
        'product_source_sha': expected['product_source_sha'],
        'packaging_source_sha': expected['packaging_source_sha'],
        'workflow_source_sha': expected['workflow_source_sha'],
        'run_id': expected['run_id'],
        'run_attempt': expected['run_attempt'],
    }
    if any(record.get(key) != value for key, value in identity.items()):
        raise RecoveryError(f'tvOS dependency receipt input identity mismatch: {ipa_path.name}')
    errors = record.get('errors')
    if not isinstance(errors, list) or errors:
        raise RecoveryError(f'tvOS dependency receipt reports an input error: {ipa_path.name}')
    with ipa_path.open('rb') as stream:
        actual_ipa_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    if (not isinstance(record.get('ipa_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', record['ipa_sha256'])
            or record['ipa_sha256'] != actual_ipa_sha):
        raise RecoveryError(f'tvOS dependency receipt IPA hash mismatch: {ipa_path.name}')

    actual_images = _ipa_runtime_inventory(ipa_path, expected['product_version'], expected['product_build'])
    image_rows = record.get('macho_images')
    if not isinstance(image_rows, list) or type(record.get('macho_image_count')) is not int:
        raise RecoveryError(f'tvOS dependency receipt has no complete Mach-O inventory: {ipa_path.name}')
    images = {}
    for image in image_rows:
        if not isinstance(image, dict):
            raise RecoveryError(f'tvOS dependency receipt has an invalid Mach-O record: {ipa_path.name}')
        path = image.get('path')
        architectures = image.get('architectures')
        digest = image.get('sha256')
        pure_path = PurePosixPath(path) if isinstance(path, str) else PurePosixPath('.')
        if (not isinstance(path, str) or not pure_path.parts or pure_path.is_absolute()
                or '..' in pure_path.parts or '\\' in path or str(pure_path) != path
                or not path.startswith('Payload/') or path in images
                or not isinstance(architectures, list) or not architectures
                or any(not isinstance(arch, str) or not re.fullmatch(r'[A-Za-z0-9_]+', arch) for arch in architectures)
                or len(architectures) != len(set(architectures))
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise RecoveryError(f'tvOS dependency receipt has an invalid Mach-O record: {ipa_path.name}')
        images[path] = {'architectures': set(architectures), 'sha256': digest}
    if (record['macho_image_count'] != len(images) or set(images) != set(actual_images)
            or any(images[path]['sha256'] != digest for path, digest in actual_images.items())):
        raise RecoveryError(f'tvOS dependency receipt Mach-O inventory differs from IPA bytes: {ipa_path.name}')

    policy = expected['verification_policy']
    dependencies = record.get('dependencies')
    weak_rows = record.get('unresolved_weak_dependencies')
    if not isinstance(dependencies, list) or not isinstance(weak_rows, list):
        raise RecoveryError(f'tvOS dependency receipt has no dependency closure: {ipa_path.name}')
    unresolved_weak = set()
    seen_dependencies = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise RecoveryError(f'tvOS dependency receipt has an invalid load record: {ipa_path.name}')
        architecture = dependency.get('architecture')
        command = dependency.get('command')
        install_name = dependency.get('install_name')
        from_image = dependency.get('from_image')
        resolution = dependency.get('resolution')
        if (not isinstance(architecture, str) or not isinstance(command, str)
                or command not in TVOS_DYLIB_LOAD_COMMANDS
                or not isinstance(install_name, str) or not install_name
                or not isinstance(from_image, str) or from_image not in images
                or architecture not in images[from_image]['architectures']):
            raise RecoveryError(f'tvOS dependency receipt has a foreign or malformed load: {ipa_path.name}')
        key = (from_image, architecture, command, install_name)
        if key in seen_dependencies:
            raise RecoveryError(f'tvOS dependency receipt has duplicate load records: {ipa_path.name}')
        seen_dependencies.add(key)
        weak = command == 'LC_LOAD_WEAK_DYLIB'
        if resolution == 'system':
            if not any(install_name.startswith(prefix) for prefix in policy['system_prefixes']):
                raise RecoveryError(f'tvOS dependency receipt classifies a non-system load as system: {install_name}')
            if dependency.get('resolved_path') is not None or dependency.get('resolved_sha256') is not None:
                raise RecoveryError(f'tvOS system dependency unexpectedly names a bundled image: {install_name}')
            continue
        if resolution == 'bundled':
            target = dependency.get('resolved_path')
            target_image = images.get(target) if isinstance(target, str) else None
            if target_image is None:
                raise RecoveryError(f'tvOS dependency receipt resolves outside its Mach-O inventory: {install_name}')
            if architecture in target_image['architectures']:
                if dependency.get('resolved_sha256') != target_image['sha256']:
                    raise RecoveryError(f'tvOS dependency receipt resolved image hash mismatch: {install_name}')
                continue
            if weak:
                unresolved_weak.add(key)
                continue
            raise RecoveryError(f'tvOS strong dependency lacks a matching architecture: {install_name}')
        if weak and resolution == 'missing':
            if dependency.get('resolved_path') is not None or dependency.get('resolved_sha256') is not None:
                raise RecoveryError(f'tvOS unresolved weak dependency has a false target: {install_name}')
            unresolved_weak.add(key)
            continue
        raise RecoveryError(f'tvOS strong or ambiguous dependency is unresolved: {install_name}')

    listed_weak = set()
    for item in weak_rows:
        if not isinstance(item, dict):
            raise RecoveryError(f'tvOS dependency receipt has an invalid weak-load record: {ipa_path.name}')
        from_image = item.get('from_image')
        architecture = item.get('architecture')
        install_name = item.get('install_name')
        if (not isinstance(from_image, str) or from_image not in images
                or not isinstance(architecture, str)
                or architecture not in images[from_image]['architectures']
                or not isinstance(install_name, str) or not install_name):
            raise RecoveryError(f'tvOS dependency receipt has an invalid weak-load record: {ipa_path.name}')
        key = (from_image, architecture, 'LC_LOAD_WEAK_DYLIB', install_name)
        if key in listed_weak:
            raise RecoveryError(f'tvOS dependency receipt has duplicate weak-load records: {ipa_path.name}')
        listed_weak.add(key)
    if listed_weak != unresolved_weak:
        raise RecoveryError(f'tvOS dependency receipt unresolved weak-load inventory differs: {ipa_path.name}')


def verify_tvos_runtime_dependency_receipt(directory, source, repository=REPOSITORY):
    """Verify the attested dependency receipt against the selected tvOS packages."""
    receipt_path = directory / TVOS_RUNTIME_RECEIPT
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise RecoveryError('Reused tvOS artifact is missing its attested runtime dependency receipt')
    try:
        publisher = verify_release_attestation.verify(receipt_path, repository)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise RecoveryError(f'tvOS runtime dependency receipt attestation failed: {error}') from error
    if publisher != repository:
        raise RecoveryError('tvOS runtime dependency receipt was not attested by the canonical repository')

    try:
        result = subprocess.run(
            ['gh', 'attestation', 'verify', str(receipt_path), '--repo', repository,
             '--signer-workflow', repository + '/.github/workflows/build.yml',
             '--source-digest', source['workflow_source_sha'], '--format', 'json'],
            check=True, capture_output=True, text=True)
        verification = json.loads(result.stdout)
    except (KeyError, TypeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        raise RecoveryError(f'tvOS runtime dependency receipt run attestation failed: {error}') from error
    if not isinstance(verification, list):
        raise RecoveryError('Runtime dependency receipt attestation returned an invalid result')
    certificates = []
    for item in verification:
        if not isinstance(item, dict):
            raise RecoveryError('Runtime dependency receipt attestation returned an invalid result')
        verification_result = item.get('verificationResult')
        signature = verification_result.get('signature') if isinstance(verification_result, dict) else None
        certificate = signature.get('certificate') if isinstance(signature, dict) else None
        if not isinstance(certificate, dict):
            raise RecoveryError('Runtime dependency receipt attestation returned an invalid certificate')
        certificates.append(certificate)
    expected_uri = f'https://github.com/{repository}/actions/runs/{source["source_run_id"]}'
    if not certificates or any(
            str(cert.get('sourceRepositoryIdentifier')) != '1433487464'
            or cert.get('sourceRepositoryDigest') != source['workflow_source_sha']
            or cert.get('runInvocationURI') not in (expected_uri, expected_uri + '/attempts/' + str(source['run_attempt']))
            for cert in certificates):
        raise RecoveryError('Runtime dependency receipt attestation does not bind the approved workflow and run')

    try:
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise RecoveryError('tvOS runtime dependency receipt is unreadable JSON') from error
    product_version, product_build = _tvos_product_version_build(source, repository)
    expected = {
        'repository': repository,
        'product_source_sha': source['product_source_sha'],
        'packaging_source_sha': source['packaging_source_sha'],
        'workflow_source_sha': source['workflow_source_sha'],
        'run_id': str(source['source_run_id']),
        'run_attempt': str(source['run_attempt']),
        'artifact_name': 'tvos-ipa',
        'platform': 'tvos',
        'product_version': product_version,
        'product_build': product_build,
        'verification_policy': {
            'architectures': 'all bundled Mach-O slices',
            'strong_dependencies': 'must resolve to a same-architecture Mach-O inside the app bundle or an allowed system path',
            'weak_dependencies': 'unresolved optional weak loads are recorded and do not fail',
            'system_prefixes': list(TVOS_RUNTIME_SYSTEM_PREFIXES),
        },
    }
    if (not isinstance(receipt, dict) or receipt.get('schema_version') != 1
            or receipt.get('workflow_step') != 'Verify tvOS runtime dependencies'
            or receipt.get('status') != 'passed'
            or receipt.get('actual_checkout_sha') != expected['packaging_source_sha']
            or any(receipt.get(key) != value for key, value in expected.items())):
        raise RecoveryError('tvOS runtime dependency receipt identity, policy, or status mismatch')
    errors = receipt.get('errors')
    if not isinstance(errors, list) or errors:
        raise RecoveryError('tvOS runtime dependency receipt reports verification errors')
    if not isinstance(receipt.get('sdk_root'), str) or not receipt['sdk_root'].startswith('/'):
        raise RecoveryError('tvOS runtime dependency receipt lacks its selected SDK root')
    if (not isinstance(receipt.get('sdk_version'), str)
            or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+', receipt['sdk_version'])):
        raise RecoveryError('tvOS runtime dependency receipt lacks a valid SDK version')
    try:
        created_at = datetime.fromisoformat(receipt.get('created_at_utc', '').replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError):
        created_at = None
    if created_at is None or created_at.utcoffset() is None or created_at.utcoffset().total_seconds() != 0:
        raise RecoveryError('tvOS runtime dependency receipt lacks a UTC creation time')

    inputs = receipt.get('inputs')
    expected_names = set(tvos_package_names(source))
    if not isinstance(inputs, list) or len(inputs) != len(expected_names):
        raise RecoveryError('tvOS runtime dependency receipt must bind every required IPA')
    by_name = {}
    for record in inputs:
        if not isinstance(record, dict):
            raise RecoveryError('tvOS runtime dependency receipt has a missing, duplicate, or unexpected IPA input')
        filename = record.get('filename')
        if not isinstance(filename, str) or filename not in expected_names or filename in by_name:
            raise RecoveryError('tvOS runtime dependency receipt has a missing, duplicate, or unexpected IPA input')
        by_name[filename] = record
    if set(by_name) != expected_names:
        raise RecoveryError('tvOS runtime dependency receipt does not bind all IPA inputs')
    for name in sorted(expected_names):
        ipa_path = directory / name
        if not ipa_path.is_file() or ipa_path.is_symlink():
            raise RecoveryError(f'tvOS runtime dependency receipt input is missing: {name}')
        _validate_tvos_runtime_input(by_name[name], ipa_path, expected)
    with receipt_path.open('rb') as receipt_file:
        return hashlib.file_digest(receipt_file, 'sha256').hexdigest()


def verify_ios_package_attestations(directory, source, repository=REPOSITORY):
    digests = {}
    for name in ('pleazyng-ios-unsigned.ipa', 'pleazyng-ios-appstore.ipa'):
        path = directory / name
        if not path.is_file() or path.is_symlink():
            raise RecoveryError('Reused iOS archive is missing its developer/Store package set')
        result = subprocess.run(['gh', 'attestation', 'verify', str(path), '--repo', repository,
                                 '--signer-workflow', repository + '/.github/workflows/build.yml',
                                 '--source-digest', source['workflow_source_sha'], '--format', 'json'],
                                check=True, capture_output=True, text=True)
        certificates = [item.get('verificationResult', {}).get('signature', {}).get('certificate', {})
                        for item in json.loads(result.stdout)]
        expected_uri = f'https://github.com/{repository}/actions/runs/{source["source_run_id"]}'
        if not certificates or any(
                str(cert.get('sourceRepositoryIdentifier')) != '1433487464'
                or cert.get('sourceRepositoryDigest') != source['workflow_source_sha']
                or cert.get('runInvocationURI') not in (expected_uri, expected_uri + '/attempts/' + str(source['run_attempt']))
                for cert in certificates):
            raise RecoveryError('Reused iOS IPA attestation does not bind the original workflow and run')
        with path.open('rb') as package:
            digests[name] = hashlib.file_digest(package, 'sha256').hexdigest()
    return digests


def verify_unsigned_ios_attestation(directory, source, repository=REPOSITORY):
    """Verify and hash the reusable no-code-sign developer IPA."""
    path = directory / 'pleazyng-ios-unsigned.ipa'
    if not path.is_file() or path.is_symlink():
        raise RecoveryError('Reused iOS developer archive is missing')
    result = subprocess.run(['gh', 'attestation', 'verify', str(path), '--repo', repository,
                             '--signer-workflow', repository + '/.github/workflows/build.yml',
                             '--source-digest', source['workflow_source_sha'], '--format', 'json'],
                            check=True, capture_output=True, text=True)
    certificates = [item.get('verificationResult', {}).get('signature', {}).get('certificate', {})
                    for item in json.loads(result.stdout)]
    expected_uri = f'https://github.com/{repository}/actions/runs/{source["source_run_id"]}'
    if not certificates or any(
            str(cert.get('sourceRepositoryIdentifier')) != '1433487464'
            or cert.get('sourceRepositoryDigest') != source['workflow_source_sha']
            or cert.get('runInvocationURI') not in
            (expected_uri, expected_uri + '/attempts/' + str(source['run_attempt']))
            for cert in certificates):
        raise RecoveryError('Reused iOS IPA attestation does not bind the original workflow and run')
    with path.open('rb') as package:
        return hashlib.file_digest(package, 'sha256').hexdigest()


def preflight_platform_artifacts(proof, repository=REPOSITORY):
    """Reject unusable reused artifacts before starting replacement builds."""
    run_id = proof['source_run_id']
    sources = {run_id: proof}
    for key in ('tvos_source', 'ios_source', 'ios_developer_source', 'android_source',
                'macos_source', 'repaired_source', 'webos_source', 'qualification_assembly_source'):
        source = proof.get(key)
        if source:
            sources[source['source_run_id']] = source
    inventories = {source_run: collection(
        f'repos/{repository}/actions/runs/{source_run}/artifacts', 'artifacts')
        for source_run in sources}
    wanted = {}
    if proof.get('append_apple_only'):
        wanted.update({'ios-ipa': proof.get('ios_source'), 'tvos-ipa': proof.get('tvos_source')})
    else:
        for name in ARTIFACTS:
            if (proof.get('rebuild_linux') and name.startswith('linux-')
                    or proof.get('rebuild_macos') and name == 'macos-dmg'
                    or proof.get('rebuild_tvos') and name == 'tvos-ipa'
                    or proof.get('rebuild_ios') and name == 'ios-ipa'
                    or proof.get('rebuild_tizen') and name == 'pleazyng-tizen-arm-experimental'
                    or proof.get('ios_developer_source') and name == 'ios-ipa'
                    or proof.get('repackage_windows') and name in WINDOWS_REPACKAGED_ARTIFACTS):
                continue
            wanted[name] = proof
        if proof.get('profile') == 'macos-packaging':
            # This profile independently requires every original platform job
            # to succeed; retain the original run's webOS artifact in the
            # preflight inventory just like the assembler does.
            wanted['webos-ipk'] = proof
        if proof.get('android_source'):
            wanted['android-apk'] = proof['android_source']
        if proof.get('webos_source'):
            wanted['webos-ipk'] = proof['webos_source']
        if proof.get('ios_developer_source'):
            wanted['ios-ipa'] = proof['ios_developer_source']
        if proof.get('ios_source'):
            wanted['ios-ipa'] = proof['ios_source']
        if proof.get('tvos_source'):
            wanted['tvos-ipa'] = proof['tvos_source']
        if proof.get('macos_source'):
            wanted['macos-dmg'] = proof['macos_source']
        if proof.get('repaired_source'):
            for name in ('linux-x64', 'linux-arm64', 'linux-x64-appimage', 'macos-dmg'):
                if name in wanted:
                    wanted[name] = proof['repaired_source']
        if proof.get('qualification_assembly_source'):
            for name in WINDOWS_REPACKAGED_ARTIFACTS:
                wanted[name] = proof['qualification_assembly_source']
    selected = []
    for name, source in wanted.items():
        if source is None:
            raise RecoveryError(f'Reusable artifact has no verified source record: {name}')
        source_run = source['source_run_id']
        expected_sha = source.get('workflow_source_sha') or source['source_sha']
        artifact = verified_run_artifact(inventories[source_run], name, source_run, expected_sha, repository)
        if source is proof.get('qualification_assembly_source'):
            expected = (proof.get('windows_repackaging') or {}).get('assembly_artifacts', {}).get(name, {})
            if artifact.get('id') != expected.get('artifact_id') or artifact.get('digest') != expected.get('digest'):
                raise RecoveryError(f'Prior Windows package changed after assembly approval: {name}')
        selected.append({'name': name, 'artifact_id': artifact['id'], 'source_run_id': source_run,
                         'source_sha': source.get('source_sha'), 'digest': artifact['digest']})
    if proof.get('repackage_windows'):
        inventory = inventories[run_id]
        if any(item.get('name') in WINDOWS_REPACKAGED_ARTIFACTS for item in inventory):
            raise RecoveryError('Skipped Windows package artifacts appeared after source approval')
        raw = verified_run_artifact(
            inventory, WINDOWS_RAW_X64_ARTIFACT, run_id, proof['source_sha'], repository)
        evidence = proof.get('windows_repackaging') or {}
        if raw.get('id') != evidence.get('artifact_id') or raw.get('digest') != evidence.get('artifact_digest'):
            raise RecoveryError('Windows x64 checkpoint changed after source approval')
        selected.append({'name': WINDOWS_RAW_X64_ARTIFACT, 'artifact_id': raw['id'],
                         'source_run_id': run_id, 'source_sha': proof['source_sha'],
                         'digest': raw['digest']})
    return selected


def verify_windows_repackaging_assembly(proof, assembly_run_id, repository=REPOSITORY):
    """Bind recovered Windows packaging to this exact trusted assembly attempt."""
    if not proof.get('repackage_windows'):
        return None
    if (repository != REPOSITORY or not str(assembly_run_id).isdigit()
            or not proof.get('qualification_run_id')):
        raise RecoveryError('Windows repackaging requires the exact qualified assembly run')
    assembly = api(f'repos/{repository}/actions/runs/{assembly_run_id}')
    source_run_id = proof['source_run_id']
    suffix = ':rebuild-tvos' if proof.get('rebuild_tvos') else ''
    title = (f'action:release:pool:public:tag:v{proof["version"]}:assembly:source:'
             f'{source_run_id}:qualification:{proof["qualification_run_id"]}{suffix}')
    if assembly.get('display_title') not in {title, 'apple-signing-unavailable:' + title}:
        raise RecoveryError('Windows packaging assembly run title does not bind its source and qualification')
    if (assembly.get('id') != int(assembly_run_id) or assembly.get('event') != 'workflow_dispatch'
            or assembly.get('head_branch') != 'main'
            or (assembly.get('head_repository') or {}).get('full_name') != repository
            or assembly.get('path', '').split('@')[0] != '.github/workflows/build.yml'
            or assembly.get('head_sha') != os.environ.get('GITHUB_SHA')
            or type(assembly.get('run_attempt')) is not int):
        raise RecoveryError('Windows packaging assembly is not the exact trusted main workflow attempt')
    from verify_embedded_release_ci import latest_attempt_jobs

    if not 1 <= assembly['run_attempt'] <= 100:
        raise RecoveryError('Windows packaging assembly attempt count is outside the supported bound')
    expected_jobs = {
        'package-windows': ('Recover verified immutable Windows x64 checkpoint',
                            'Build installer and portables', 'Attest Windows artifacts',
                            'Upload x64 portable', 'Upload installer'),
        'Package Windows Chocolatey': ('Package Chocolatey release', 'Attest Chocolatey package',
                                       'Upload Chocolatey package'),
    }
    expected_names = set(expected_jobs)
    attempts = [collection(
        f'repos/{repository}/actions/runs/{assembly_run_id}/attempts/{attempt}/jobs', 'jobs')
        for attempt in range(1, assembly['run_attempt'] + 1)]
    relevant_attempts = []
    for attempt, page in enumerate(attempts, start=1):
        if not isinstance(page, list):
            raise RecoveryError(f'Windows packaging assembly attempt {attempt} omitted its job inventory')
        relevant = []
        for job in page:
            if not isinstance(job, dict) or not isinstance(job.get('name'), str):
                raise RecoveryError(f'Windows packaging assembly attempt {attempt} contains a malformed job')
            if job['name'] in expected_names:
                relevant.append(job)
        relevant_attempts.append(relevant)
    try:
        # Matrix placeholders can repeat their literal name across unrelated
        # platforms. Merge rerun history only for the two exact Windows jobs
        # whose outputs this provenance record consumes.
        jobs = latest_attempt_jobs(relevant_attempts)
    except release_ci.GateError as error:
        raise RecoveryError(f'Windows packaging assembly attempt history is invalid: {error}') from error
    selected_jobs = {}
    for name, required_steps in expected_jobs.items():
        matches = [job for job in jobs if job.get('name') == name]
        if len(matches) != 1:
            raise RecoveryError(f'Windows repackage job is missing or ambiguous: {name}')
        job = matches[0]
        if (job.get('status') != 'completed' or job.get('conclusion') != 'success'
                or job.get('run_id') != int(assembly_run_id)
                or job.get('head_sha') != assembly.get('head_sha')):
            raise RecoveryError(f'Windows repackage job did not succeed in the exact assembly attempt: {name}')
        for step_name in required_steps:
            steps = [step for step in job.get('steps', []) if step.get('name') == step_name]
            if len(steps) != 1 or steps[0].get('status') != 'completed' or steps[0].get('conclusion') != 'success':
                raise RecoveryError(f'Windows repackage job is missing a successful step: {name}/{step_name}')
        if name == 'package-windows':
            checkout_steps = [step for step in job.get('steps', [])
                              if step.get('name') == 'Verify immutable Windows package source']
            if (len(checkout_steps) != 1 or checkout_steps[0].get('status') != 'completed'
                    or checkout_steps[0].get('conclusion') != 'success'):
                raise RecoveryError('Windows packaging did not verify the immutable product checkout')
        selected_jobs[name] = job
    inventory = collection(f'repos/{repository}/actions/runs/{assembly_run_id}/artifacts', 'artifacts')
    artifacts = {}
    for name in sorted(WINDOWS_REPACKAGED_ARTIFACTS):
        artifact = verified_run_artifact(inventory, name, assembly_run_id, assembly['head_sha'], repository)
        producer = selected_jobs['Package Windows Chocolatey' if name == 'windows-chocolatey' else 'package-windows']
        try:
            created = datetime.fromisoformat(artifact['created_at'].replace('Z', '+00:00'))
            started = datetime.fromisoformat(producer['started_at'].replace('Z', '+00:00'))
            completed = datetime.fromisoformat(producer['completed_at'].replace('Z', '+00:00'))
        except (KeyError, TypeError, ValueError) as error:
            raise RecoveryError(f'Windows packaging artifact lacks job-interval evidence: {name}') from error
        if not started <= created <= completed:
            raise RecoveryError(f'Windows packaging artifact was not created by its verified producer job: {name}')
        artifacts[name] = {'artifact_id': artifact['id'], 'digest': artifact['digest']}
    return {
        **(proof.get('windows_repackaging') or {}),
        'assembly_run_id': int(assembly_run_id),
        'assembly_head_sha': assembly['head_sha'],
        'assembly_run_attempt': assembly['run_attempt'],
        'product_checkout_sha': proof['source_sha'],
        'package_job_id': selected_jobs['package-windows']['id'],
        'chocolatey_job_id': selected_jobs['Package Windows Chocolatey']['id'],
        'assembly_artifacts': artifacts,
    }


def verify_qualification_assembly_reuse(proof, assembly_run_id, repository=REPOSITORY, *, current_main_sha=None):
    """Reuse a failed qualified assembly only when all requested outputs are durable."""
    if (repository != REPOSITORY or not str(assembly_run_id).isdigit()
            or int(assembly_run_id) in {int(proof.get('source_run_id', 0)),
                                        int(proof.get('qualification_run_id', 0))}
            or not proof.get('qualification_run_id') or not proof.get('windows_repackaging')):
        raise RecoveryError('Qualified assembly reuse requires distinct source, qualification, and assembly runs')
    assembly_run_id = int(assembly_run_id)
    assembly = api(f'repos/{repository}/actions/runs/{assembly_run_id}')
    source_run_id = int(proof['source_run_id'])
    title = (f'action:release:pool:public:tag:v{proof["version"]}:assembly:source:'
             f'{source_run_id}:qualification:{proof["qualification_run_id"]}:rebuild-tvos')
    if assembly.get('display_title') not in {title, 'apple-signing-unavailable:' + title}:
        raise RecoveryError('Prior assembly title does not bind the exact source, qualification, and tvOS rebuild')
    assembly_sha = assembly.get('head_sha', '')
    attempt = assembly.get('run_attempt')
    if (assembly.get('id') != assembly_run_id or assembly.get('status') != 'completed'
            or assembly.get('conclusion') != 'failure' or assembly.get('event') != 'workflow_dispatch'
            or assembly.get('head_branch') != 'main'
            or (assembly.get('head_repository') or {}).get('full_name') != repository
            or assembly.get('path', '').split('@')[0] != '.github/workflows/build.yml'
            or type(attempt) is not int or not 1 <= attempt <= 100
            or not re.fullmatch(r'[0-9a-f]{40}', assembly_sha)):
        raise RecoveryError('Prior qualified assembly must be an exact completed trusted-main workflow failure')
    if current_main_sha is not None:
        branch = api(f'repos/{repository}/branches/main')
        observed_main_sha = (branch.get('commit') or {}).get('sha', '')
        if (not re.fullmatch(r'[0-9a-f]{40}', str(current_main_sha))
                or not re.fullmatch(r'[0-9a-f]{40}', str(observed_main_sha))):
            raise RecoveryError('Explicit current revision or canonical main SHA is invalid')
        if observed_main_sha != current_main_sha:
            advance = api(f'repos/{repository}/compare/{current_main_sha}...{observed_main_sha}')
            if (advance.get('status') != 'ahead'
                    or advance.get('merge_base_commit', {}).get('sha') != current_main_sha):
                raise RecoveryError('Canonical main diverged from the explicitly captured revision')
        current_sha = observed_main_sha
    else:
        current_sha = os.environ.get('GITHUB_SHA', '')
        if current_sha and os.environ.get('GITHUB_REF') != 'refs/heads/main':
            raise RecoveryError('Hosted qualified assembly verification must run from canonical main')
    if not current_sha:
        main = api(f'repos/{repository}/branches/main')
        current_sha = (main.get('commit') or {}).get('sha', '')
    if not re.fullmatch(r'[0-9a-f]{40}', current_sha):
        raise RecoveryError('Current canonical main revision is missing its immutable SHA')
    for base_sha, target_sha in ((proof['source_sha'], assembly_sha), (assembly_sha, current_sha)):
        comparison = api(f'repos/{repository}/compare/{base_sha}...{target_sha}')
        if (comparison.get('status') not in {'ahead', 'identical'}
                or comparison.get('merge_base_commit', {}).get('sha') != base_sha):
            raise RecoveryError('Prior assembly and current workflow are not on the exact trusted main lineage')

    from verify_embedded_release_ci import latest_attempt_jobs

    raw_attempts = [collection(
        f'repos/{repository}/actions/runs/{assembly_run_id}/attempts/{number}/jobs', 'jobs')
        for number in range(1, attempt + 1)]
    proof_job_names = {
        'Freeze requalified source', 'Validate release request',
        'Build tvOS developer package (arm64)', 'package-windows',
        'Package Windows Chocolatey', 'create-release',
    }
    unrelated_failures = {
        job.get('name') for page in raw_attempts for job in page
        if job.get('conclusion') in {'failure', 'cancelled', 'timed_out'}
        and job.get('name') != 'create-release'
    }
    if unrelated_failures:
        raise RecoveryError('Prior qualified assembly has failures outside its release validation gate: '
                            + ', '.join(sorted(unrelated_failures)))
    attempts = [[job for job in page if job.get('name') in proof_job_names]
                for page in raw_attempts]
    try:
        jobs = latest_attempt_jobs(attempts)
    except release_ci.GateError as error:
        raise RecoveryError(f'Prior qualified assembly attempt history is invalid: {error}') from error

    def one_job(name, *, conclusion='success'):
        matches = [job for job in jobs if job.get('name') == name]
        if len(matches) != 1:
            raise RecoveryError(f'Prior qualified assembly job is missing or ambiguous: {name}')
        job = matches[0]
        if (job.get('status') != 'completed' or job.get('conclusion') != conclusion
                or job.get('run_id') != assembly_run_id or job.get('run_attempt') != attempt
                or job.get('head_sha') != assembly_sha):
            raise RecoveryError(f'Prior qualified assembly job is not bound to its exact successful attempt: {name}')
        return job

    def successful_steps(job, names, label):
        for name in names:
            matches = [step for step in job.get('steps', []) if step.get('name') == name]
            if (len(matches) != 1 or matches[0].get('status') != 'completed'
                    or matches[0].get('conclusion') != 'success'):
                raise RecoveryError(f'Prior qualified assembly is missing a successful step: {label}/{name}')

    validation = one_job('Validate release request')
    successful_steps(validation, ('Verify reusable platform source',
                                  'Validate immutable release tag against trusted main'),
                     'Validate release request')
    freeze = one_job('Freeze requalified source')
    successful_steps(freeze, ('Verify original platform artifacts and supplemental qualification before creating the stable tag',),
                     'Freeze requalified source')

    tvos_job = one_job('Build tvOS developer package (arm64)')
    if (tvos_job.get('runner_id') in (None, 0) or not tvos_job.get('runner_name')
            or tvos_job.get('runner_group_name') != 'GitHub Actions'
            or not isinstance(tvos_job.get('labels'), list) or 'xcode-27' not in tvos_job['labels']):
        raise RecoveryError('Prior tvOS package was not produced on the public xcode-27 hosted runner')
    successful_steps(tvos_job, (
        'Build tvOS app', 'Package unsigned tvOS developer IPA',
        'Verify unsigned tvOS runtime dependencies', 'Attest unsigned tvOS checkpoint',
        'Upload durable unsigned tvOS checkpoint', 'Verify tvOS runtime dependencies',
        'Attest unsigned tvOS developer package', 'Upload tvOS package'),
        'Build tvOS developer package (arm64)')
    if any(step.get('name') == 'Build signed tvOS App Store IPA'
           and step.get('conclusion') == 'success' for step in tvos_job.get('steps', [])):
        raise RecoveryError('Prior assembly tvOS output is not the required unsigned developer package')

    windows_job = one_job('package-windows')
    if (windows_job.get('runner_id') in (None, 0) or not windows_job.get('runner_name')
            or windows_job.get('runner_group_name') != 'GitHub Actions'
            or not isinstance(windows_job.get('labels'), list) or 'windows-2025' not in windows_job['labels']):
        raise RecoveryError('Prior Windows packages were not produced on the public windows-2025 hosted runner')
    successful_steps(windows_job, (
        'Verify immutable Windows package source', 'Recover verified immutable Windows x64 checkpoint',
        'Build installer and portables', 'Attest Windows artifacts',
        'Upload x64 portable', 'Upload installer'), 'package-windows')
    chocolatey_job = one_job('Package Windows Chocolatey')
    if (chocolatey_job.get('runner_id') in (None, 0) or not chocolatey_job.get('runner_name')
            or chocolatey_job.get('runner_group_name') != 'GitHub Actions'
            or not isinstance(chocolatey_job.get('labels'), list)
            or 'windows-2025' not in chocolatey_job['labels']):
        raise RecoveryError('Prior Chocolatey package was not produced on the public windows-2025 hosted runner')
    successful_steps(chocolatey_job, ('Package Chocolatey release', 'Attest Chocolatey package',
                                      'Upload Chocolatey package'), 'Package Windows Chocolatey')

    failed_jobs = [job.get('name') for job in jobs
                   if job.get('conclusion') in {'failure', 'cancelled', 'timed_out'}]
    if failed_jobs != ['create-release']:
        raise RecoveryError('Prior qualified assembly has failures outside its release validation gate')
    create_release = one_job('create-release', conclusion='failure')
    steps = create_release.get('steps')
    if not isinstance(steps, list):
        raise RecoveryError('Prior failed release job lacks step-level publication evidence')
    recover_steps = [step for step in steps if step.get('name') == 'Recover verified platform artifacts']
    publish_steps = [step for step in steps if step.get('name') in {
        'Append signed Apple assets without replacing existing release assets', 'Create Release'}]
    if (len(recover_steps) != 1 or recover_steps[0].get('status') != 'completed'
            or recover_steps[0].get('conclusion') != 'failure'
            or len(publish_steps) != 2
            or any(step.get('status') != 'completed' or step.get('conclusion') != 'skipped'
                   for step in publish_steps)
            or steps.index(recover_steps[0]) >= min(steps.index(step) for step in publish_steps)):
        raise RecoveryError('Prior release attempt did not stop at artifact validation before publication')

    inventory = collection(f'repos/{repository}/actions/runs/{assembly_run_id}/artifacts', 'artifacts')
    output_names = ('tvos-ipa', *sorted(WINDOWS_REPACKAGED_ARTIFACTS))
    artifacts = {name: verified_run_artifact(inventory, name, assembly_run_id, assembly_sha, repository)
                 for name in output_names}

    def created_inside_job(artifact, producer, name):
        try:
            created = datetime.fromisoformat(artifact['created_at'].replace('Z', '+00:00'))
            started = datetime.fromisoformat(producer['started_at'].replace('Z', '+00:00'))
            completed = datetime.fromisoformat(producer['completed_at'].replace('Z', '+00:00'))
        except (KeyError, TypeError, ValueError) as error:
            raise RecoveryError(f'Prior artifact lacks exact producer interval evidence: {name}') from error
        if not started <= created <= completed:
            raise RecoveryError(f'Prior artifact was not created during its verified producer job: {name}')

    created_inside_job(artifacts['tvos-ipa'], tvos_job, 'tvos-ipa')
    for name in sorted(WINDOWS_REPACKAGED_ARTIFACTS):
        producer = chocolatey_job if name == 'windows-chocolatey' else windows_job
        created_inside_job(artifacts[name], producer, name)

    tvos_source = {
        'source_run_id': assembly_run_id, 'source_sha': proof['source_sha'],
        'workflow_source_sha': assembly_sha, 'run_attempt': tvos_job['run_attempt'],
        'product_source_sha': proof['source_sha'], 'packaging_source_sha': proof['source_sha'],
        'version': proof['version'], 'include_apple_store_build': False,
        'runtime_dependency_receipt_required': True, 'original_tvos_has_store': False,
        'artifact_id': artifacts['tvos-ipa']['id'], 'artifact_digest': artifacts['tvos-ipa']['digest'],
        'job_id': tvos_job['id'], 'runner_id': tvos_job['runner_id'],
        'runner_name': tvos_job['runner_name'], 'runner_labels': tvos_job['labels'],
    }
    assembly_artifacts = {
        name: {'artifact_id': artifacts[name]['id'], 'digest': artifacts[name]['digest']}
        for name in sorted(WINDOWS_REPACKAGED_ARTIFACTS)
    }
    assembly_source = {
        'source_run_id': assembly_run_id, 'source_sha': proof['source_sha'],
        'workflow_source_sha': assembly_sha, 'run_attempt': attempt,
        'version': proof['version'],
        'job_id': windows_job['id'], 'chocolatey_job_id': chocolatey_job['id'],
        'assembly_artifacts': assembly_artifacts,
    }
    windows_repackaging = {
        **proof['windows_repackaging'], 'assembly_run_id': assembly_run_id,
        'assembly_head_sha': assembly_sha, 'assembly_run_attempt': attempt,
        'product_checkout_sha': proof['source_sha'], 'package_job_id': windows_job['id'],
        'chocolatey_job_id': chocolatey_job['id'], 'assembly_artifacts': assembly_artifacts,
    }
    return {'qualification_assembly_run_id': assembly_run_id,
            'qualification_assembly_source': assembly_source,
            'tvos_source': tvos_source, 'windows_repackaging': windows_repackaging,
            'repackage_windows': False,
            'reused_assembly_jobs': ['Build tvOS developer package (arm64)', 'package-windows',
                                     'Package Windows Chocolatey']}


def download_platforms(proof, destination, repository=REPOSITORY):
    run_id = proof["source_run_id"]
    append_apple_only = proof.get('append_apple_only') is True
    if append_apple_only:
        if not proof.get('ios_source') or not proof.get('tvos_source'):
            raise RecoveryError('Append-only Apple recovery requires both signed IPA source records')
        wanted = {'ios-ipa': 'ios-ipa', 'tvos-ipa': 'tvos-ipa'}
        inventories = {}
        for key in ('ios_source', 'tvos_source'):
            source_id = proof[key]['source_run_id']
            if source_id not in inventories:
                inventories[source_id] = collection(
                    f'repos/{repository}/actions/runs/{source_id}/artifacts', 'artifacts')
    else:
        inventory = collection(f"repos/{repository}/actions/runs/{run_id}/artifacts", "artifacts")
        wanted = {name: folder for name, folder in ARTIFACTS.items()
                  if not (proof.get("rebuild_linux") and name.startswith("linux-"))
                  and not (proof.get("rebuild_macos") and name == "macos-dmg")
                  and not (proof.get("rebuild_tvos") and name == "tvos-ipa")
                  and not (proof.get("rebuild_ios") and name == "ios-ipa")
                  and not (proof.get("rebuild_tizen") and name == "pleazyng-tizen-arm-experimental")
                  and not (proof.get("repackage_windows") and name in WINDOWS_REPACKAGED_ARTIFACTS)
                  and not (proof.get("ios_developer_source") and name == "ios-ipa")}
        if any(item.get("name") == "windows-msix" for item in inventory):
            wanted["windows-msix"] = "windows-msix"
        inventories = {run_id: inventory}
        if proof.get('tvos_source'):
            tvos = proof['tvos_source']
            inventories[tvos['source_run_id']] = collection(
                f'repos/{repository}/actions/runs/{tvos["source_run_id"]}/artifacts', 'artifacts')
        if proof.get('ios_source'):
            ios = proof['ios_source']
            inventories[ios['source_run_id']] = collection(
                f'repos/{repository}/actions/runs/{ios["source_run_id"]}/artifacts', 'artifacts')
        if proof.get("ios_developer_source"):
            ios_developer = proof["ios_developer_source"]
            inventories[ios_developer["source_run_id"]] = collection(
                f'repos/{repository}/actions/runs/{ios_developer["source_run_id"]}/artifacts', 'artifacts')
            wanted["ios-ipa"] = "ios-ipa"
        if proof.get('qualification_assembly_source'):
            assembly_source = proof['qualification_assembly_source']
            inventories[assembly_source['source_run_id']] = collection(
                f"repos/{repository}/actions/runs/{assembly_source['source_run_id']}/artifacts", 'artifacts')
        if proof.get("android_source"):
            android = proof["android_source"]
            inventories[android["source_run_id"]] = collection(
                f"repos/{repository}/actions/runs/{android['source_run_id']}/artifacts", "artifacts")
            wanted["android-apk"] = "android-apk"
        if proof.get("macos_source"):
            macos = proof["macos_source"]
            inventories[macos["source_run_id"]] = collection(
                f"repos/{repository}/actions/runs/{macos['source_run_id']}/artifacts", "artifacts")
            wanted["webos-ipk"] = "webos"
        if proof.get("repaired_source"):
            repaired = proof["repaired_source"]
            inventories[repaired["source_run_id"]] = collection(
                f"repos/{repository}/actions/runs/{repaired['source_run_id']}/artifacts", "artifacts")
        if proof.get("webos_source"):
            webos = proof["webos_source"]
            inventories[webos["source_run_id"]] = collection(
                f"repos/{repository}/actions/runs/{webos['source_run_id']}/artifacts", "artifacts")
            wanted["webos-ipk"] = "webos"
    records = []
    for name, folder in wanted.items():
        source = (proof['tvos_source'] if name == 'tvos-ipa' and proof.get('tvos_source') else
                  proof['ios_developer_source'] if name == 'ios-ipa' and proof.get('ios_developer_source') else
                  proof['ios_source'] if name == 'ios-ipa' and proof.get('ios_source') else
                  proof['qualification_assembly_source'] if name in WINDOWS_REPACKAGED_ARTIFACTS
                  and proof.get('qualification_assembly_source') else
                  proof["webos_source"] if name == "webos-ipk" and proof.get("webos_source") else
                  proof["android_source"] if name == "android-apk" else
                  proof["repaired_source"] if proof.get("repaired_source") and (name.startswith("linux-") or name == "macos-dmg") else
                  proof["macos_source"] if name == "macos-dmg" and proof.get("macos_source") else proof)
        artifact_run = source["source_run_id"]
        matches = [item for item in inventories[artifact_run] if item.get("name") == name and not item.get("expired")]
        if len(matches) != 1:
            raise RecoveryError(f"Reusable artifact is missing or ambiguous: {name}")
        item = matches[0]
        identity = item.get("workflow_run", {})
        digest = item.get("digest") or ""
        expected_sha = (source['workflow_source_sha'] if source is not proof and source.get('workflow_source_sha')
                        else source["source_sha"])
        if (identity.get("id") != artifact_run or identity.get("head_sha") != expected_sha
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)):
            raise RecoveryError(f"Artifact lacks matching run, source, or digest evidence: {name}")
        if name in ('tvos-ipa', 'ios-ipa') and proof.get('tvos_source' if name == 'tvos-ipa' else 'ios_source') and (
                item.get('id') != source['artifact_id'] or digest != source['artifact_digest']):
            raise RecoveryError('Apple artifact ID/digest changed after source approval')
        if name == 'ios-ipa' and proof.get('ios_developer_source') and (
                item.get('id') != source['artifact_id'] or digest != source['artifact_digest']):
            raise RecoveryError('Unsigned iOS artifact ID/digest changed after source approval')
        if source is proof.get('qualification_assembly_source'):
            expected = (proof.get('windows_repackaging') or {}).get('assembly_artifacts', {}).get(name, {})
            if item.get('id') != expected.get('artifact_id') or digest != expected.get('digest'):
                raise RecoveryError(f'Prior Windows package artifact changed after source approval: {name}')
        with tempfile.TemporaryDirectory(prefix="pleazyng-release-artifact-") as scratch:
            archive = Path(scratch) / "artifact.zip"
            with archive.open("wb") as output:
                result = subprocess.run(["gh", "api", "--allow-escape-sequences",
                                         f"repos/{repository}/actions/artifacts/{item['id']}/zip"], stdout=output)
            with archive.open("rb") as downloaded:
                actual_digest = hashlib.file_digest(downloaded, "sha256").hexdigest()
            if result.returncode or actual_digest != digest[7:]:
                raise RecoveryError(f"Reusable artifact download/digest verification failed: {name}")
            extract_verified(archive, destination / folder)
            if name == 'tvos-ipa' and (proof.get('tvos_source') or source.get('runtime_dependency_receipt_required')):
                if not source.get('runtime_dependency_receipt_required'):
                    raise RecoveryError('Reused tvOS artifact lacks runtime dependency qualification')
                tvos_source = source
                if source is proof:
                    product_sha = source['source_sha']
                    tvos_source = {
                        **source,
                        'run_attempt': source.get('original_tvos_attempt', source.get('run_attempt')),
                        'product_source_sha': source.get('product_source_sha', product_sha),
                        'packaging_source_sha': source.get('packaging_source_sha', product_sha),
                        'workflow_source_sha': source.get('workflow_source_sha', product_sha),
                        'include_apple_store_build': source.get('original_tvos_has_store',
                                                               source.get('include_apple_store_build', False)),
                    }
                package_hashes = verify_tvos_package_attestations(destination / folder, tvos_source, repository)
                if proof.get('tvos_source'):
                    source['package_sha256'] = package_hashes
                verify_tvos_runtime_dependency_receipt(destination / folder, tvos_source, repository)
            if source is proof.get('qualification_assembly_source'):
                source.setdefault('package_sha256', {}).update(
                    verify_windows_package_attestation(destination / folder, name, source, repository))
            if name == 'ios-ipa' and proof.get('ios_source'):
                source['package_sha256'] = verify_ios_package_attestations(destination / folder, source, repository)
            if name == 'ios-ipa' and proof.get('ios_developer_source'):
                source['package_sha256'] = verify_unsigned_ios_attestation(destination / folder, source, repository)
            if (name == "macos-dmg" and proof.get("macos_source")
                    and proof["macos_source"].get("compilation_reused")):
                verify_macos_receipt(json.loads((destination / folder / "macos-compilation-recovery.json").read_text()), proof)
        records.append({"name": name, "artifact_id": item["id"], "digest": digest,
                        "source_run_id": artifact_run, "source_sha": source["source_sha"],
                        "workflow_source_sha": expected_sha})
        print(f"Recovered verified {name} from run {artifact_run}.", flush=True)
    proof = {**proof, "artifacts": records}
    if proof.get('recovery_policy') == 'frozen-apple-packaging-v1':
        approval = proof.get('packaging_approval')
        expected_approval = {
            'recovery_policy': 'frozen-apple-packaging-v1',
            'product_source_sha': proof.get('product_source_sha'),
            'packaging_source_sha': proof.get('packaging_source_sha'),
            'workflow_source_sha': proof.get('workflow_source_sha'),
            'approval_source_sha': proof.get('workflow_source_sha'),
        }
        if (not isinstance(approval, dict)
                or any(approval.get(key) != value for key, value in expected_approval.items())
                or not isinstance(approval.get('approved_files'), dict)
                or not approval['approved_files']):
            raise RecoveryError('Frozen packaging approval does not bind the product, repair, and workflow revisions')
        actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        if actual != proof['workflow_source_sha']:
            raise RecoveryError('Assembler did not check out the approved workflow revision')
        workflow = os.environ.get('GITHUB_SHA')
        assembly = os.environ.get('GITHUB_RUN_ID', '')
        if workflow != proof['workflow_source_sha'] or not assembly.isdigit():
            raise RecoveryError('Assembler workflow identity is not bound to the frozen repair proof')
        if proof.get('append_apple_only') is True:
            proof.update(assembly_run_id=int(assembly), actual_checkout_sha=actual,
                         assembly_scope='apple-store-append-only')
        else:
            linux = destination / 'linux-x64/pleazyng-linux-x64.tar.gz'
            with linux.open('rb') as stream:
                linux_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            proof.update(assembly_run_id=int(assembly), actual_checkout_sha=actual,
                         linux_archive_sha256=linux_digest,
                         assembly_scope='full-platform-recovery')
    if proof.get('repackage_windows'):
        assembly = os.environ.get('GITHUB_RUN_ID', '')
        actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        if not assembly.isdigit() or actual != os.environ.get('GITHUB_SHA'):
            raise RecoveryError('Windows package proof must be created by the exact trusted assembler checkout')
        proof = {**proof, 'windows_repackaging': verify_windows_repackaging_assembly(proof, assembly, repository)}
        proof['repackaged_jobs'] = ['package-windows', 'Package Windows Chocolatey']
    if proof.get('qualification_assembly_run_id'):
        assembly = os.environ.get('GITHUB_RUN_ID', '')
        workflow = os.environ.get('GITHUB_SHA', '')
        actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        if not assembly.isdigit() or not re.fullmatch(r'[0-9a-f]{40}', workflow) or actual != workflow:
            raise RecoveryError('Qualified continuation proof must use the exact trusted assembler workflow checkout')
        proof.update(assembly_run_id=int(assembly), actual_checkout_sha=actual,
                     assembly_scope='qualified-assembly-continuation')
    (destination / "platform-reuse-provenance.json").write_text(json.dumps(proof, indent=2) + "\n")
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_run")
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument('--packaging-source-sha')
    parser.add_argument('--workflow-source-sha')
    parser.add_argument('--include-apple-store-build', action='store_true')
    parser.add_argument("--download", type=Path)
    parser.add_argument("--download-windows-build", type=Path,
                        help="Download the exact original x64 checkpoint for a verified qualified release assembly")
    parser.add_argument("--macos-run", help="Reuse a qualified signing-only Mac recovery after every original platform succeeds.")
    parser.add_argument("--rebuild-linux", action="store_true", help="Rebuild both Linux architectures instead of reusing their artifacts")
    parser.add_argument("--rebuild-ios", action="store_true", help="Rebuild the iOS developer package instead of reusing its original artifact")
    parser.add_argument("--rebuild-tvos", action="store_true", help="Rebuild tvOS instead of reusing its original artifact")
    parser.add_argument("--rebuild-tizen", action="store_true", help="Rebuild Tizen instead of reusing its original artifact")
    parser.add_argument("--rebuild-macos", action="store_true", help="Build and qualify a fresh Mac package instead of reusing a failed Mac job")
    parser.add_argument("--repaired-run", help="Reuse verified Linux and fully qualified Mac jobs from a prior repair run")
    parser.add_argument('--qualification-run', type=int, help='Supplemental Apple qualification for the exact captured source')
    parser.add_argument('--qualification-assembly-run', type=int,
                        help='Reuse verified tvOS and Windows package outputs from a prior qualified assembly')
    parser.add_argument('--freeze-qualified-release', action='store_true', help='Preflight all original artifacts, then create the verified stable tag')
    parser.add_argument('--tvos-run', help='Reuse signed TV bytes from an independently approved sibling Apple repair')
    parser.add_argument('--ios-run', help='Reuse signed iOS bytes from an independently approved sibling Apple repair')
    parser.add_argument('--ios-developer-run', help='Reuse a verified unsigned developer IPA from a successful recovery run')
    parser.add_argument('--built-tvos-run', type=int,
                        help='Reuse a successful signed tvOS artifact from a verified frozen packaging retry')
    parser.add_argument("--require-homebrew-cask", action="store_true", help="Require the reused Mac notarization and Gatekeeper checks")
    parser.add_argument("--webos-run", help="Reuse a successful verified webOS package from a prior release run")
    parser.add_argument("--android-run", help="Reuse a separately successful Android release build.")
    parser.add_argument("--skip-webos-build", action="store_true", help="Exclude the non-public experimental WebOS candidate.")
    parser.add_argument("--allow-building", action="store_true", help="Preflight only: native jobs may still be running.")
    parser.add_argument("--wait-for-source", type=int, default=0, help="Wait up to this many seconds before recovering completed outputs.")
    args = parser.parse_args()
    if args.qualification_run is not None and (args.qualification_run < 1 or args.macos_run):
        parser.error('Supplemental qualification requires one valid Apple qualification run and no signing-only recovery')
    if args.qualification_assembly_run is not None and (
            args.qualification_assembly_run < 1 or args.qualification_run is None or args.rebuild_tvos):
        parser.error('Prior qualified assembly reuse requires a supplemental qualification and cannot rebuild tvOS')
    if args.freeze_qualified_release and (
            args.qualification_run is None or args.download or args.wait_for_source
            or os.environ.get('GITHUB_REPOSITORY') != REPOSITORY
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'):
        parser.error('Freezing an assembled candidate requires the canonical main qualification preflight')
    if args.macos_run and (args.rebuild_macos or args.rebuild_linux or args.rebuild_tvos or args.rebuild_ios):
        parser.error("Signing-only Mac recovery cannot be combined with fresh platform rebuilding")
    if args.repaired_run and (args.macos_run or args.rebuild_macos or args.rebuild_linux or args.rebuild_tvos or args.rebuild_ios):
        parser.error("Repaired Linux/Mac reuse cannot be combined with rebuilding or signing-only recovery")
    if args.webos_run and (args.macos_run or args.skip_webos_build):
        parser.error("WebOS reuse must remain included and cannot substitute signing-only recovery")
    if args.allow_building and args.download:
        parser.error("--allow-building is only a preflight; downloads require completed successful platform jobs")
    if args.download_windows_build and (
            args.download or args.allow_building or args.wait_for_source or args.freeze_qualified_release
            or args.qualification_run is None or args.qualification_assembly_run is not None
            or str(args.android_run) != args.source_run
            or str(args.webos_run) != args.source_run or args.skip_webos_build):
        parser.error("Windows checkpoint downloads require a completed supplemental qualification assembly")
    if not 0 <= args.wait_for_source <= 10800:
        parser.error("--wait-for-source must be between 0 and 10800 seconds")
    if bool(args.packaging_source_sha) != bool(args.workflow_source_sha):
        parser.error('Frozen packaging requires both packaging and workflow identities')
    if args.packaging_source_sha and (args.macos_run or args.repaired_run):
        parser.error('Frozen packaging cannot substitute unrelated signing or repair runs')
    if args.rebuild_tizen and (
            args.packaging_source_sha or args.macos_run or args.repaired_run or args.rebuild_linux
            or args.rebuild_macos or args.rebuild_tvos or args.rebuild_ios):
        parser.error('Tizen recovery cannot be combined with frozen packaging or unrelated rebuild modes')
    if args.tvos_run and (not args.packaging_source_sha or args.repaired_run or args.macos_run or args.rebuild_tvos):
        parser.error('TV reuse requires approved frozen iOS-only packaging recovery')
    if args.ios_run and (not args.tvos_run or args.rebuild_ios or args.rebuild_tvos):
        parser.error('iOS reuse requires both independently approved signed Apple sibling runs')
    if args.ios_developer_run and (args.packaging_source_sha or args.include_apple_store_build
                                   or args.rebuild_ios or args.ios_run or args.tvos_run or args.macos_run):
        parser.error('Unsigned iOS reuse is limited to ordinary recovery without Apple signing')
    if args.built_tvos_run is not None and (
            args.built_tvos_run < 1 or not args.packaging_source_sha or not args.workflow_source_sha
            or not args.include_apple_store_build or args.rebuild_ios or args.rebuild_tvos
            or args.tvos_run or args.ios_run):
        parser.error('built tvOS reuse requires approved frozen Apple packaging and cannot rebuild or append siblings')
    frozen_options = {'include_apple_store_build': args.include_apple_store_build}
    if args.qualification_run is not None:
        frozen_options.update(qualification_run=args.qualification_run, freeze=args.freeze_qualified_release)
    if args.qualification_assembly_run is not None:
        frozen_options['qualification_assembly_run'] = args.qualification_assembly_run
    if args.packaging_source_sha:
        frozen_options.update(packaging_source_sha=args.packaging_source_sha,
                              workflow_source_sha=args.workflow_source_sha)
        if args.tvos_run:
            frozen_options['tvos_run'] = args.tvos_run
        if args.built_tvos_run is not None:
            frozen_options['built_tvos_run'] = args.built_tvos_run
    if args.ios_run:
        frozen_options['ios_run'] = args.ios_run
    proof = (verify_macos_packaging_recovery(args.source_run, args.macos_run, args.head_sha, args.version)
             if args.macos_run else
             wait_for_source_run(args.source_run, args.head_sha, args.version, args.wait_for_source, rebuild_linux=args.rebuild_linux, rebuild_macos=args.rebuild_macos, rebuild_tvos=args.rebuild_tvos, rebuild_ios=args.rebuild_ios, rebuild_tizen=args.rebuild_tizen, repaired_run=args.repaired_run, require_homebrew_cask=args.require_homebrew_cask, **frozen_options)
             if args.wait_for_source else
             verify_source_run(args.source_run, args.head_sha, args.version, allow_pending=args.allow_building, rebuild_linux=args.rebuild_linux, rebuild_macos=args.rebuild_macos, rebuild_tvos=args.rebuild_tvos, rebuild_ios=args.rebuild_ios, rebuild_tizen=args.rebuild_tizen, repaired_run=args.repaired_run, require_homebrew_cask=args.require_homebrew_cask, ios_developer_run=args.ios_developer_run, **frozen_options))
    proof = compose_proof(proof, args.android_run, not args.skip_webos_build, args.webos_run,
                          args.ios_developer_run)
    if args.download_windows_build:
        if not proof.get('repackage_windows'):
            raise RecoveryError('The exact source run does not qualify for Windows packaging recovery')
        preflight_platform_artifacts(proof)
        evidence = download_windows_build_artifact(proof, args.download_windows_build)
        print(json.dumps({"source_run_id": proof["source_run_id"], "source_sha": proof["source_sha"],
                          "windows_repackaging": evidence}, indent=2))
        return
    if args.freeze_qualified_release:
        from verify_embedded_release_ci import create_stable_tag

        preflight_platform_artifacts(proof)
        create_stable_tag(github_token(), f"v{proof['version']}", proof['source_sha'])
    if args.download:
        proof = download_platforms(proof, args.download)
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"source_sha={proof['source_sha']}\nci_run_id={proof['ci_run_id']}\n")
            output.write(f"repackage_windows={str(proof.get('repackage_windows', False)).lower()}\n")
            if proof.get("webos_source"):
                output.write(f"webos_source_sha={proof['webos_source']['source_sha']}\n")
            if proof.get("android_source"):
                output.write(f"android_source_sha={proof['android_source']['source_sha']}\n")
    print(json.dumps(proof, indent=2))


if __name__ == "__main__":
    main()
