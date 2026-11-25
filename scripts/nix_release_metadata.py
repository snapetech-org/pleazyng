#!/usr/bin/env python3
"""Advance Nix metadata using an authenticated, complete public release."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from prepare_linux_channel_recipes import ASSETS, release_hashes
from pubspec_version import parse_pubspec_version
from release_coverage import missing_assets
from verify_release_attestation import require_repository_identity
from verify_frozen_packaging_repair import verify_receipt

REPOSITORY = "snapetech-org/pleazyng"
METADATA_PATH = "linux/packaging/nix/release.json"
ACTIVATION_VERSION = (3, 0, 33)
FROZEN_REPAIR_RECEIPT_FIELDS = frozenset({
    "product_source_sha", "packaging_source_sha", "workflow_source_sha",
    "packaging_approval", "recovery_policy", "schema_version",
})


def identity(metadata: dict) -> dict:
    return {"release": {key: metadata.get(key) for key in
                        ("id", "tag_name", "draft", "prerelease", "published_at")},
            "assets": sorted((asset.get("name"), asset.get("id"), asset.get("size"), asset.get("digest"))
                             for asset in metadata.get("assets", []))}


def version(tag: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Expected a stable release tag")
    return tuple(map(int, tag[1:].split(".")))


def validate_release(metadata: dict) -> dict[str, dict]:
    version(metadata.get("tag_name", ""))
    if (metadata.get("draft") is not False or metadata.get("prerelease") is not False
            or not metadata.get("published_at")):
        raise ValueError("Nix metadata requires a published stable release")
    assets = {}
    for asset in metadata.get("assets", []):
        name = asset.get("name")
        if not isinstance(name, str) or asset.get("size", 0) <= 0:
            continue
        if name in assets:
            raise ValueError("Duplicate release asset")
        assets[name] = asset
    missing = missing_assets(set(assets))
    if missing or "SHA256SUMS.txt" not in assets:
        raise ValueError("Nix metadata requires complete release coverage and final checksums")
    return assets


def proposed_metadata(current: dict, tag: str, manifest: str) -> dict:
    incoming = version(tag)
    if incoming < ACTIVATION_VERSION:
        raise ValueError("Retired release is below the Nix activation floor")
    if current.get("repository") != REPOSITORY or current.get("version") != current.get("tag", "")[1:]:
        raise ValueError("Existing Nix metadata identity is invalid")
    hashes = release_hashes(manifest)
    proposed = {"version": tag[1:], "tag": tag, "repository": REPOSITORY,
                "sha256": {arch: hashes[name] for arch, name in ASSETS.items()}}
    existing = version(current["tag"])
    if incoming < existing:
        raise ValueError("Nix metadata cannot downgrade")
    if incoming == existing and current != proposed:
        raise ValueError("Published Nix metadata changed for the same version")
    return proposed


def manifest_invocations(proofs: list, digest: str) -> set[tuple[str, int, int]]:
    """Inspect only statements already cryptographically verified by gh."""
    matches = set()
    for proof in proofs:
        statement = proof.get("verificationResult", {}).get("statement", {})
        if not any(subject.get("name") == "SHA256SUMS.txt"
                   and subject.get("digest", {}).get("sha256") == digest
                   for subject in statement.get("subject", [])):
            continue
        predicate = statement.get("predicate", {})
        definition = predicate.get("buildDefinition", {})
        workflow = definition.get("externalParameters", {}).get("workflow", {})
        if (workflow.get("repository") != f"https://github.com/{REPOSITORY}"
                or workflow.get("path") != ".github/workflows/build.yml"):
            continue
        invocation = predicate.get("runDetails", {}).get("metadata", {}).get("invocationId", "")
        match = re.fullmatch(rf"https://github.com/{REPOSITORY}/actions/runs/([1-9][0-9]*)/attempts/([1-9][0-9]*)", invocation)
        if not match:
            continue
        run_id, attempt = map(int, match.groups())
        for dependency in definition.get("resolvedDependencies", []):
            commit = dependency.get("digest", {}).get("gitCommit", "")
            if (isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit)
                    and dependency.get("uri", "").startswith(f"git+https://github.com/{REPOSITORY}@")):
                matches.add((commit, run_id, attempt))
    return matches


def manifest_run(proofs: list, digest: str, commit: str) -> tuple[int, int]:
    matches = {(run_id, attempt) for sha, run_id, attempt in manifest_invocations(proofs, digest) if sha == commit}
    if len(matches) != 1:
        raise ValueError("Final manifest has no unique exact-source assembly attestation")
    return matches.pop()


def verify_ordinary_recovery(receipt: dict, product_commit: str, tag: str, github) -> str:
    """Authenticate the narrow ordinary Linux-reuse receipt profile."""
    import recover_release_platforms

    if (not isinstance(receipt, dict)
            or any(key in receipt for key in (
                "schema_version", "product_source_sha", "packaging_source_sha",
                "workflow_source_sha", "packaging_approval", "recovery_policy",
                "assembly_run_id", "actual_checkout_sha"))):
        raise ValueError("Unsupported ordinary recovery receipt shape")
    source_run_id = receipt.get("source_run_id")
    head_sha = receipt.get("head_sha")
    if (type(source_run_id) is not int or source_run_id < 1
            or receipt.get("source_sha") != product_commit
            or receipt.get("version") != tag[1:]
            or not isinstance(head_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None
            or receipt.get("platforms_complete") is not True):
        raise ValueError("Ordinary recovery receipt differs from the published source or version")

    # This published recovery reused both Linux builds and rebuilt only Mac.
    # Keep the accepted profile narrow so a receipt cannot omit a Linux job.
    expected_rebuilds = {
        "rebuild_linux": False, "rebuild_macos": True,
        "rebuild_tvos": False, "rebuild_ios": False,
    }
    if any(receipt.get(key) is not value for key, value in expected_rebuilds.items()):
        raise ValueError("Ordinary recovery receipt has unsupported platform reuse flags")
    if (receipt.get("rebuilt_jobs") != ["build-macos"]
            or receipt.get("append_apple_only") is True):
        raise ValueError("Ordinary recovery receipt has an unsupported rebuilt-job set")

    proof = recover_release_platforms.verify_source_run(
        source_run_id, head_sha, tag[1:], REPOSITORY, rebuild_macos=True,
    )
    expected_proof = {
        "source_run_id": source_run_id,
        "source_sha": product_commit,
        "head_sha": head_sha,
        "version": tag[1:],
        "rebuild_linux": False,
        "rebuild_macos": True,
        "rebuild_tvos": False,
        "rebuild_ios": False,
        "reused_jobs": receipt.get("reused_jobs"),
        "platforms_complete": True,
        "pending_jobs": receipt.get("pending_jobs"),
        "macos_cask_qualified": receipt.get("macos_cask_qualified"),
        "webos_link_compatibility_repaired": receipt.get("webos_link_compatibility_repaired"),
    }
    if any(proof.get(key) != expected for key, expected in expected_proof.items()):
        raise ValueError("Ordinary recovery receipt differs from independently verified source-run proof")
    if (not isinstance(proof.get("reused_jobs"), list)
            or proof["reused_jobs"].count("Build Linux (x64)") != 1
            or proof["reused_jobs"].count("Build Linux (arm64)") != 1):
        raise ValueError("Verified source run did not reuse both exact Linux platform jobs")

    # The CI receipt is itself returned by verify_source_run after checking the
    # original source run. Compare its complete mode-specific proof, not just
    # the receipt's self-reported CI run ID.
    for key in ("ci_mode", "ci_run_id", "ci_run_url", "ci_source_sha", "embedded_ci"):
        if key not in proof or receipt.get(key) != proof[key]:
            raise ValueError("Ordinary recovery CI proof differs from the verified source run")

    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("Ordinary recovery receipt has no bound source artifacts")
    inventory = github(f"actions/runs/{source_run_id}/artifacts?per_page=100")
    items = inventory.get("artifacts") if isinstance(inventory, dict) else None
    total_count = inventory.get("total_count") if isinstance(inventory, dict) else None
    if (not isinstance(items, list) or type(total_count) is not int
            or total_count != len(items)):
        raise ValueError("Cannot read a complete source-run artifact inventory")
    seen = set()
    for record in artifacts:
        if not isinstance(record, dict):
            raise ValueError("Ordinary recovery artifact record is invalid")
        name = record.get("name")
        artifact_id = record.get("artifact_id")
        digest = record.get("digest")
        if (not isinstance(name, str) or not name or name in seen
                or type(artifact_id) is not int or artifact_id < 1
                or not isinstance(digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                or record.get("source_run_id") != source_run_id
                or record.get("source_sha") != product_commit
                or ("workflow_source_sha" in record
                    and record["workflow_source_sha"] != product_commit)):
            raise ValueError("Ordinary recovery artifact is not bound to the exact source run")
        seen.add(name)
        matches = [item for item in items if item.get("name") == name]
        if len(matches) != 1:
            raise ValueError("Source-run artifact is missing or ambiguous: " + name)
        item = matches[0]
        workflow_run = item.get("workflow_run", {})
        if (item.get("id") != artifact_id or item.get("digest") != digest
                or item.get("expired") is not False
                or workflow_run.get("id") != source_run_id
                or workflow_run.get("head_sha") != product_commit):
            raise ValueError("Recovery artifact ID, digest, or source identity changed: " + name)
    if not {"linux-x64", "linux-arm64"}.issubset(seen):
        raise ValueError("Ordinary recovery receipt does not bind both Linux source artifacts")
    return head_sha


def verify_macos_packaging_recovery(receipt: dict, product_commit: str, tag: str) -> str:
    """Reauthenticate a receipt that replaces only Mac packaging."""
    import recover_release_platforms

    if (not isinstance(receipt, dict) or receipt.get("profile") != "macos-packaging"
            or receipt.get("source_sha") != product_commit
            or receipt.get("version") != tag[1:]
            or receipt.get("platforms_complete") is not True
            or receipt.get("pending_jobs") != []):
        raise ValueError("Mac packaging receipt differs from the published source or version")
    source_run = receipt.get("source_run_id")
    macos_source = receipt.get("macos_source")
    head_sha = receipt.get("head_sha")
    if (type(source_run) is not int or source_run < 1
            or not isinstance(macos_source, dict)
            or type(macos_source.get("source_run_id")) is not int
            or macos_source["source_run_id"] < 1
            or not isinstance(head_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None):
        raise ValueError("Mac packaging receipt has invalid run or workflow identities")

    proof = recover_release_platforms.verify_macos_packaging_recovery(
        source_run, macos_source["source_run_id"], head_sha, tag[1:], REPOSITORY,
    )
    if any(receipt.get(key) != value for key, value in proof.items()):
        raise ValueError("Mac packaging receipt differs from independently verified run evidence")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("Mac packaging receipt has no bound source artifacts")
    verified_artifacts = recover_release_platforms.preflight_platform_artifacts(proof, REPOSITORY)
    expected_by_name = {record["name"]: record for record in verified_artifacts}
    actual_by_name = {record.get("name"): record for record in artifacts
                      if isinstance(record, dict)}
    if (len(artifacts) != len(verified_artifacts)
            or len(actual_by_name) != len(artifacts)
            or set(actual_by_name) != set(expected_by_name)):
        raise ValueError("Mac packaging artifact inventory differs from live GitHub evidence")
    for name, expected in expected_by_name.items():
        record = actual_by_name[name]
        workflow_sha = (proof["macos_source"]["source_sha"]
                        if expected["name"] == "macos-dmg" else expected["source_sha"])
        if (not isinstance(record, dict)
                or set(record) != set(expected) | {"workflow_source_sha"}
                or any(record.get(key) != value for key, value in expected.items())
                or record.get("workflow_source_sha") != workflow_sha):
            raise ValueError("Mac packaging artifact IDs or digests differ from live GitHub evidence")
    return head_sha


def verify_continuation_package_hashes(receipt: dict, manifest: str, tag: str) -> None:
    """Bind package hashes recorded by an assembly continuation to the public manifest."""
    expected = {
        "tvos_source": {"pleazyng-tvos-unsigned.ipa"},
        "qualification_assembly_source": {
            "pleazyng-windows-x64-portable.7z", "pleazyng-windows-installer.exe",
            f"pleazyng.{tag[1:]}.nupkg",
        },
    }
    manifest_hashes = {}
    for line in manifest.splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2:
            continue
        digest, name = fields
        name = name.strip()
        if name.startswith("*"):
            name = name[1:]
        if name not in set().union(*expected.values()):
            continue
        if re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None or name in manifest_hashes:
            raise ValueError("Continuation package checksum manifest has an invalid or duplicate entry")
        manifest_hashes[name] = digest.lower()
    required = set().union(*expected.values())
    if set(manifest_hashes) != required:
        raise ValueError("Continuation package checksums are missing from the public manifest")
    for key, names in expected.items():
        source = receipt.get(key)
        hashes = source.get("package_sha256") if isinstance(source, dict) else None
        if not isinstance(hashes, dict) or set(hashes) != names:
            raise ValueError("Continuation receipt has an incomplete verified package hash set: " + key)
        if any(hashes[name] != manifest_hashes[name] for name in names):
            raise ValueError("Continuation package hashes differ from the public checksum manifest: " + key)


def verify_supplemental_recovery(receipt: dict, product_commit: str, tag: str, *,
                                 manifest: str | None = None, github=None) -> str:
    """Bind every reused package to the same candidate and exact Apple qualification."""
    import recover_release_platforms as recovery

    source_run = receipt.get("source_run_id")
    qualification = receipt.get("qualification_run_id")
    if (type(source_run) is not int or source_run < 1
            or type(qualification) is not int or qualification < 1 or qualification == source_run
            or receipt.get("head_sha") != product_commit or receipt.get("source_sha") != product_commit
            or receipt.get("version") != tag[1:] or receipt.get("platforms_complete") is not True):
        raise ValueError("Invalid supplemental recovery source, version, or qualification")
    continuation = "qualification_assembly_run_id" in receipt
    continuation_fields = {"assembly_run_id", "actual_checkout_sha", "assembly_scope"}
    qualification_assembly = receipt.get("qualification_assembly_run_id")
    current_main_sha = None
    if continuation:
        assembly_run = receipt.get("assembly_run_id")
        actual_checkout = receipt.get("actual_checkout_sha")
        if (type(qualification_assembly) is not int or qualification_assembly < 1
                or qualification_assembly in {source_run, qualification}
                or type(assembly_run) is not int or assembly_run < 1
                or assembly_run in {source_run, qualification, qualification_assembly}
                or receipt.get("assembly_scope") != "qualified-assembly-continuation"
                or not isinstance(actual_checkout, str)
                or re.fullmatch(r"[0-9a-f]{40}", actual_checkout) is None
                or manifest is None):
            raise ValueError("Qualified continuation receipt has invalid assembly, workflow, or checksum identities")
        branch = (github("branches/main") if github else
                  recovery.api(f"repos/{REPOSITORY}/branches/main"))
        current_main_sha = (branch.get("commit") or {}).get("sha") if isinstance(branch, dict) else None
        if not isinstance(current_main_sha, str) or re.fullmatch(r"[0-9a-f]{40}", current_main_sha) is None:
            raise ValueError("Cannot resolve canonical main for qualified assembly continuation")
    elif continuation_fields.intersection(receipt):
        raise ValueError("Supplemental receipt contains unbound continuation assembly metadata")

    source_options = {"qualification_run": qualification}
    if continuation:
        source_options.update(qualification_assembly_run=qualification_assembly,
                              current_main_sha=current_main_sha)
    proof = recovery.verify_source_run(source_run, product_commit, tag[1:], REPOSITORY,
                                       **source_options)
    proof = recovery.compose_proof(proof, source_run, True, source_run)
    accepted_receipt_fields = set(proof) | {"artifacts"}
    if continuation:
        accepted_receipt_fields |= continuation_fields
    receipt_proof = dict(receipt)
    for key in ("tvos_source", "qualification_assembly_source"):
        source = receipt_proof.get(key)
        if isinstance(source, dict) and "package_sha256" in source:
            receipt_proof[key] = {name: value for name, value in source.items()
                                  if name != "package_sha256"}
    if (any(receipt_proof.get(key) != value for key, value in proof.items())
            or set(receipt) != accepted_receipt_fields):
        raise ValueError("Supplemental recovery receipt differs from the independently verified candidate")
    if continuation:
        verify_continuation_package_hashes(receipt, manifest, tag)
    artifacts = recovery.preflight_platform_artifacts(proof, REPOSITORY)
    expected = []
    for item in artifacts:
        name = item["name"]
        source = (proof.get("tvos_source") if name == "tvos-ipa" else
                  proof.get("qualification_assembly_source")
                  if name in recovery.WINDOWS_REPACKAGED_ARTIFACTS else None)
        expected.append(dict(item, workflow_source_sha=(
            source.get("workflow_source_sha") if source else product_commit)))
    if receipt["artifacts"] != expected:
        raise ValueError("Supplemental recovery artifact IDs or digests differ from GitHub evidence")
    title = f"action:release:pool:public:tag:{tag}:assembly:source:{source_run}:qualification:{qualification}"
    if continuation:
        title += f":assembly:{qualification_assembly}"
    return title


def recovery_source(directory: str, assets: dict, manifest: str, product_commit: str,
                    tag: str, github) -> tuple[str | None, dict | None]:
    """Read an attestation-bound recovery receipt, if this release has one."""
    asset = assets.get("platform-reuse-provenance.json")
    if asset is None:
        return product_commit, None
    if asset.get("size", 0) > 1024 * 1024:
        raise ValueError("Unexpected recovery receipt size")
    subprocess.run(["gh", "release", "download", tag, "--repo", REPOSITORY,
                    "--pattern", "platform-reuse-provenance.json", "--dir", directory], check=True)
    path = Path(directory) / "platform-reuse-provenance.json"
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) != asset["size"] or asset.get("digest") != f"sha256:{digest}":
        raise ValueError("Recovery receipt differs from public release asset")
    matches = [line.split()[0] for line in manifest.splitlines()
               if len(line.split()) == 2 and line.split()[1].lstrip("*") == path.name]
    if matches != [digest]:
        raise ValueError("Final checksums do not uniquely bind the recovery receipt")
    receipt = json.loads(raw)
    if not isinstance(receipt, dict):
        raise ValueError("Recovery receipt must be a JSON object")
    if "qualification_run_id" in receipt:
        return None, receipt
    if receipt.get("profile") == "macos-packaging":
        workflow_commit = verify_macos_packaging_recovery(receipt, product_commit, tag)
        return workflow_commit, receipt
    if FROZEN_REPAIR_RECEIPT_FIELDS.intersection(receipt):
        workflow_commit = receipt.get("workflow_source_sha")
        if (receipt.get("product_source_sha") != product_commit
                or not isinstance(workflow_commit, str)
                or re.fullmatch(r"[0-9a-f]{40}", workflow_commit) is None):
            raise ValueError("Recovery receipt has a different immutable product or workflow source")
        return workflow_commit, receipt
    workflow_commit = verify_ordinary_recovery(receipt, product_commit, tag, github)
    return workflow_commit, receipt


def verify_supplemental_assembly_identity(receipt: dict, assembly_run: int,
                                         workflow_commit: str) -> None:
    """Bind a continued supplemental receipt to its final manifest assembly."""
    if "qualification_assembly_run_id" not in receipt:
        return
    if (receipt.get("assembly_scope") != "qualified-assembly-continuation"
            or receipt.get("assembly_run_id") != assembly_run
            or receipt.get("actual_checkout_sha") != workflow_commit):
        raise ValueError("Qualified continuation receipt does not bind the attested final assembly")


def prepare(root: Path, metadata: dict, commit: str, github) -> tuple[str | None, dict]:
    """Verify all external inputs before returning bytes for a maintained file."""
    assets = validate_release(metadata)
    tag = metadata["tag_name"]
    if version(tag) < ACTIVATION_VERSION:
        return None, {"tag": tag, "state": "below-activation-floor"}
    require_repository_identity(REPOSITORY)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid immutable source")
    comparison = github(f"compare/{commit}...main")
    if comparison.get("status") not in {"identical", "ahead"}:
        raise ValueError("Release source is not on canonical main")
    source = github(f"contents/pubspec.yaml?ref={commit}")
    if source.get("encoding") != "base64" or source.get("size", 0) > 1024 * 1024:
        raise ValueError("Cannot verify immutable application version")
    source_bytes = base64.b64decode("".join(source.get("content", "").split()), validate=True)
    if len(source_bytes) != source.get("size"):
        raise ValueError("Immutable application source size differs")
    pubspec = source_bytes.decode("utf-8")
    marketing, build = parse_pubspec_version(pubspec)
    if "v" + marketing.split("+", 1)[0] != tag:
        raise ValueError("Release tag differs from immutable application version")
    if assets["SHA256SUMS.txt"]["size"] > 1024 * 1024:
        raise ValueError("Unexpected checksum manifest size")
    with tempfile.TemporaryDirectory(prefix="pleazyng-nix-metadata-") as directory:
        subprocess.run(["gh", "release", "download", tag, "--repo", REPOSITORY,
                        "--pattern", "SHA256SUMS.txt", "--dir", directory], check=True)
        manifest = Path(directory) / "SHA256SUMS.txt"
        raw = manifest.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != assets[manifest.name]["size"] or assets[manifest.name].get("digest") != f"sha256:{digest}":
            raise ValueError("Final manifest differs from public release asset")
        workflow_commit, recovery = recovery_source(
            directory, assets, raw.decode("utf-8"), commit, tag, github,
        )
        source_selector = (["--source-digest", workflow_commit] if workflow_commit is not None
                           else ["--source-ref", "refs/heads/main"])
        result = subprocess.run(["gh", "attestation", "verify", str(manifest), "--repo", REPOSITORY,
                                 *source_selector, "--signer-workflow",
                                 f"{REPOSITORY}/.github/workflows/build.yml", "--format", "json"],
                                check=True, capture_output=True, text=True)
        verified = json.loads(result.stdout)
        supplemental = workflow_commit is None
        if supplemental:
            title = verify_supplemental_recovery(
                recovery, commit, tag, manifest=raw.decode("utf-8"), github=github,
            )
            matches = []
            for sha, run_id, attempt in manifest_invocations(verified, digest):
                run = github(f"actions/runs/{run_id}/attempts/{attempt}")
                if (run.get("display_title") == title and run.get("event") == "workflow_dispatch"
                        and run.get("head_branch") == "main" and run.get("head_sha") == sha
                        and run.get("head_repository", {}).get("full_name") == REPOSITORY):
                    matches.append((sha, run_id, attempt, run))
            if len(matches) != 1:
                raise ValueError("Final manifest has no unique exact supplemental assembly attestation")
            workflow_commit, run_id, attempt, assembly = matches[0]
        else:
            run_id, attempt = manifest_run(verified, digest, workflow_commit)
            assembly = github(f"actions/runs/{run_id}/attempts/{attempt}")
        if (assembly.get("status") != "completed" or assembly.get("conclusion") != "success"
                or assembly.get("head_sha") != workflow_commit or assembly.get("path") != ".github/workflows/build.yml"
                or assembly.get("run_attempt") != attempt
                or assembly.get("repository", {}).get("full_name") != REPOSITORY):
            raise ValueError("Manifest assembly did not successfully build the immutable release source")
        if recovery is not None and not supplemental:
            if not FROZEN_REPAIR_RECEIPT_FIELDS.intersection(recovery):
                if (workflow_commit != recovery.get("head_sha")
                        or assembly.get("head_branch") != "main"
                        or assembly.get("head_repository", {}).get("full_name") != REPOSITORY):
                    raise ValueError("Manifest attestation does not bind the ordinary recovery workflow source")
            else:
                prefix = f"repos/{REPOSITORY}/"
                def api(path: str) -> dict:
                    if path == f"repos/{REPOSITORY}":
                        return json.loads(subprocess.check_output(["gh", "api", path], text=True))
                    if not path.startswith(prefix):
                        raise ValueError("Recovery verifier requested a foreign repository")
                    return github(path[len(prefix):])
                approval = verify_receipt(
                    recovery, product_source_sha=commit,
                    packaging_source_sha=recovery.get("packaging_source_sha"),
                    workflow_source_sha=workflow_commit,
                    source_run_id=recovery.get("source_run_id"), assembly_run_id=run_id,
                    api=api,
                )
                if (recovery.get("packaging_approval") != approval
                        or recovery.get("source_sha") != commit
                        or recovery.get("actual_checkout_sha") != recovery.get("packaging_source_sha")):
                    raise ValueError("Recovery receipt differs from its independently approved repair")
        if supplemental and recovery is not None:
            verify_supplemental_assembly_identity(recovery, run_id, workflow_commit)
        hashes = release_hashes(raw.decode("utf-8"))
        for name, expected in hashes.items():
            if assets[name].get("digest") != f"sha256:{expected}":
                raise ValueError("Linux release asset differs from authenticated manifest")
        current = json.loads((root / METADATA_PATH).read_text())
        proposed = proposed_metadata(current, tag, raw.decode("utf-8"))
    # Publication may have advanced during network verification. Reconcile again
    # from fresh main rather than writing facts from a superseded observation.
    if identity(github("releases/latest")) != identity(metadata):
        raise ValueError("Public latest release changed during verification")
    return json.dumps(proposed, indent=2) + "\n", {
        "tag": tag, "source_commit": commit, "manifest_sha256": digest,
        "application_build": build,
        "assembly_run": run_id, "assembly_attempt": attempt,
        "assembly_workflow_source": workflow_commit,
        "sha256": proposed["sha256"], "state": "authenticated-metadata",
        "native_qualification": "separate", "upstream_acceptance": "separate",
    }
