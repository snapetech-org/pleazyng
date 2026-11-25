#!/usr/bin/env python3
"""Focused checks for selecting and preserving signed Mac App Store inputs."""

from __future__ import annotations

import sys
import hashlib
import json
import plistlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import capture_apple_macos_tvos_storekit as capture
import verify_macos_appstore_capture_input as producer


RUN_ID = "45659740097"
ATTEMPT = "1"
WORKFLOW_ID = "420687200"
WORKFLOW_SHA = "1eb5f204c13717c411ee469293f1bd249873d239"
SOURCE_SHA = "d240ae2c8700a895c8a7b9d8ddcfb3b2204d3fe4"
SOURCE_TAG = f"v3.0.52-candidate.{SOURCE_SHA}"


def producer_responses() -> tuple[dict, dict, dict, dict]:
    run = {
        "id": int(RUN_ID),
        "workflow_id": int(WORKFLOW_ID),
        "path": producer.PRODUCER_WORKFLOW_PATH,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": WORKFLOW_SHA,
        "head_repository": {"full_name": producer.REPOSITORY},
        "status": "completed",
        "conclusion": "success",
    }
    attempt = {**run, "run_attempt": int(ATTEMPT)}
    jobs = {"jobs": [
        {"name": producer.APPSTORE_JOB, "conclusion": "success"},
        {"name": producer.DELIVERY_JOB, "conclusion": "success"},
    ]}
    artifacts = {"artifacts": [{
        "id": 15049005631,
        "name": f"pleazyng-macos-appstore-v3.0.52-{RUN_ID}",
        "size_in_bytes": 313177961,
        "digest": "sha256:09838cd339348c7c2432cc6bd7d3ffc5d9769d8ef962c4401ff297702425d875",
        "expired": False,
        "workflow_run": {"id": int(RUN_ID), "head_sha": WORKFLOW_SHA, "head_branch": "main"},
    }]}
    return run, attempt, jobs, artifacts


class MacAppStoreCaptureInputTests(unittest.TestCase):
    def select(self, *, run=None, attempt=None, jobs=None, artifacts=None) -> dict:
        defaults = producer_responses()
        return producer.select_producer(
            run=run or defaults[0], attempt=attempt or defaults[1], jobs=jobs or defaults[2],
            artifacts=artifacts or defaults[3], run_id=RUN_ID, run_attempt=ATTEMPT,
            workflow_id=WORKFLOW_ID, source_sha=SOURCE_SHA, source_tag=SOURCE_TAG,
            version="3.0.52", build="211",
        )

    def test_selects_exact_successful_package_artifact_from_signed_delivery_run(self) -> None:
        receipt = self.select()
        self.assertEqual(receipt["artifact_id"], "15049005631")
        self.assertEqual(receipt["artifact_digest"], "sha256:09838cd339348c7c2432cc6bd7d3ffc5d9769d8ef962c4401ff297702425d875")
        self.assertEqual(receipt["producer_run_attempt"], ATTEMPT)

    def test_rejects_a_failed_delivery_job_or_unexpected_package_artifact(self) -> None:
        run, attempt, jobs, artifacts = producer_responses()
        jobs["jobs"][1]["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "Both signed package creation and delivery jobs must succeed"):
            self.select(run=run, attempt=attempt, jobs=jobs, artifacts=artifacts)

        run, attempt, jobs, artifacts = producer_responses()
        artifacts["artifacts"][0]["name"] = "pleazyng-macos-appstore-v3.0.51-45659740097"
        with self.assertRaisesRegex(ValueError, "Expected exactly one retained signed package artifact"):
            self.select(run=run, attempt=attempt, jobs=jobs, artifacts=artifacts)

    def test_signed_package_receipt_is_accepted_only_for_exact_mac_attempt_and_product(self) -> None:
        capture_receipt = {
            "schema_version": 1,
            "product": "Pleazyng",
            "capture_input_kind": "verified-macos-app-store-package",
            "source_tag": capture.SOURCE_TAG,
            "source_sha": capture.SOURCE_SHA,
            "product_version": capture.VERSION,
            "product_build": capture.BUILD_NUMBER,
            "platform": "macOS",
            "build_mode": "App Store IAP",
            "build_configuration": "Release Archive",
            "app_bundle_name": "Pleazyng.app",
            "bundle_id": capture.BUNDLE_ID,
            "build_defines": capture.capture_tools.IOS_MAC_STORE_DEFINES,
            "distribution_signing": "Mac App Store Distribution",
            "producer_repository": producer.REPOSITORY,
            "producer_workflow_path": producer.PRODUCER_WORKFLOW_PATH,
            "producer_workflow_ref": (
                f"{producer.REPOSITORY}/{producer.PRODUCER_WORKFLOW_PATH}@refs/heads/main"
            ),
            "producer_workflow_sha": WORKFLOW_SHA,
            "producer_run_id": RUN_ID,
            "producer_run_attempt": ATTEMPT,
            "producer_artifact": {
                "artifact_id": "15049005631",
                "artifact_name": f"pleazyng-macos-appstore-v{capture.VERSION}-{RUN_ID}",
                "artifact_digest": "sha256:" + "a" * 64,
                "artifact_size_bytes": 313177961,
                "producer_run_id": RUN_ID,
                "producer_run_attempt": ATTEMPT,
                "producer_workflow_sha": WORKFLOW_SHA,
            },
            "app_store_receipt": {
                "product": "Pleazyng",
                "channel": "Mac App Store",
                "candidate_state": "app-store-connect-processed",
                "release_tag": f"v{capture.VERSION}",
                "source_commit": capture.SOURCE_SHA,
                "marketing_version": capture.VERSION,
                "build_number": capture.BUILD_NUMBER,
                "bundle_id": capture.BUNDLE_ID,
                "asset_filename": "pleazyng-macos-appstore.pkg",
                "asset_sha256": "b" * 64,
                "platform": "MAC_OS",
                "processing_state": "VALID",
                "github_run_id": int(RUN_ID),
                "github_run_attempt": int(ATTEMPT),
                "workflow_commit": WORKFLOW_SHA,
                "application": {
                    "bundle_identifier": capture.BUNDLE_ID,
                    "version": capture.VERSION,
                    "build_number": capture.BUILD_NUMBER,
                    "executable": "Pleazyng",
                    "architectures": ["arm64", "x86_64"],
                },
                "qualification": {
                    "app_signed_for_distribution": True,
                    "installer_signed_for_app_store": True,
                    "app_store_connect_processed": True,
                    "upload_to_app_store_connect": True,
                },
                "package": {
                    "filename": "pleazyng-macos-appstore.pkg",
                    "sha256": "b" * 64,
                    "signed": True,
                    "size_bytes": 1024,
                },
                "installer_signature": {
                    "certificate_type": "MAC_INSTALLER_DISTRIBUTION",
                    "certificate_sha1": "1A0DABC17F7F2ED2E517B2CCA49A14A88156510F",
                },
                "app_signature": {
                    "team_identifier": "DM9A59DC85",
                    "app_certificate_sha1": "B7D31D8572209681D4C9E655F448708269BAE53B",
                },
                "profile": {"uuid": "profile-uuid", "type": "MAC_APP_STORE"},
            },
            "architectures": ["arm64", "x86_64"],
            "executable": "Pleazyng",
            "app_bundle_sha256": "a" * 64,
            "package_sha256": "b" * 64,
            "producer_package_receipt_sha256": "c" * 64,
            "app_signature": {
                "team_identifier": "DM9A59DC85",
                "app_certificate_sha1": "B7D31D8572209681D4C9E655F448708269BAE53B",
                "sandbox_enabled": True,
                "jit_enabled": True,
            },
            "profile": {"type": "MAC_APP_STORE", "uuid": "profile-uuid"},
        }
        capture.validate_appstore_package_capture_receipt(capture_receipt, "macos", RUN_ID, ATTEMPT)

        capture_receipt["app_store_receipt"]["processing_state"] = "PROCESSING"
        with self.assertRaisesRegex(ValueError, "does not match the selected producer"):
            capture.validate_appstore_package_capture_receipt(capture_receipt, "macos", RUN_ID, ATTEMPT)

    def test_restore_package_builds_receipt_from_exact_downloaded_package(self) -> None:
        selection = self.select()
        package_bytes = b"retained signed App Store package fixture"
        package_sha = hashlib.sha256(package_bytes).hexdigest()
        package_receipt = {
            "schema_version": 1,
            "product": "Pleazyng",
            "channel": "Mac App Store",
            "candidate_state": "app-store-connect-processed",
            "release_tag": "v3.0.52",
            "source_commit": SOURCE_SHA,
            "marketing_version": "3.0.52",
            "build_number": "211",
            "bundle_id": "org.snapetech.pleazyng",
            "platform": "MAC_OS",
            "processing_state": "VALID",
            "github_run_id": int(RUN_ID),
            "github_run_attempt": int(ATTEMPT),
            "workflow_commit": WORKFLOW_SHA,
            "asset_filename": "pleazyng-macos-appstore.pkg",
            "asset_sha256": package_sha,
            "package": {
                "filename": "pleazyng-macos-appstore.pkg",
                "sha256": package_sha,
                "size_bytes": len(package_bytes),
                "signed": True,
            },
            "application": {
                "bundle_identifier": "org.snapetech.pleazyng",
                "version": "3.0.52",
                "build_number": "211",
                "executable": "Pleazyng",
                "architectures": ["arm64", "x86_64"],
            },
            "app_signature": {
                "team_identifier": producer.TEAM_IDENTIFIER,
                "app_certificate_sha1": producer.APP_CERTIFICATE_SHA1,
            },
            "installer_signature": {
                "certificate_sha1": producer.INSTALLER_CERTIFICATE_SHA1,
                "certificate_type": "MAC_INSTALLER_DISTRIBUTION",
            },
            "profile": {"type": "MAC_APP_STORE", "uuid": "profile-uuid"},
            "qualification": {
                "app_signed_for_distribution": True,
                "installer_signed_for_app_store": True,
                "app_store_connect_processed": True,
                "upload_to_app_store_connect": True,
            },
        }
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_dir = root / "artifact"
            artifact_dir.mkdir()
            (artifact_dir / "pleazyng-macos-appstore.pkg").write_bytes(package_bytes)
            (artifact_dir / "pleazyng-macos-appstore.json").write_text(
                json.dumps(package_receipt), encoding="utf-8"
            )
            selection_path = root / "selection.json"
            selection_path.write_text(json.dumps(selection), encoding="utf-8")
            output_dir = root / "restored"
            identity = {
                "bundle_id": "org.snapetech.pleazyng",
                "version": "3.0.52",
                "build_number": "211",
                "executable": "Pleazyng",
            }
            entitlements = {
                "com.apple.security.app-sandbox": True,
                "com.apple.security.cs.allow-jit": True,
                "com.apple.security.device.audio-input": True,
                "com.apple.security.files.user-selected.read-write": True,
                "com.apple.security.network.client": True,
            }

            def fake_command(arguments: list[str], **_: object) -> str:
                if "--expand-full" in arguments:
                    expanded = Path(arguments[-1])
                    (expanded / "Payload" / "Applications" / "Pleazyng.app" / "Contents").mkdir(parents=True)
                    executable = expanded / "Payload" / "Applications" / "Pleazyng.app" / "Contents" / "MacOS" / "Pleazyng"
                    executable.parent.mkdir()
                    executable.write_bytes(b"app executable")
                    (executable.parent.parent / "embedded.provisionprofile").write_bytes(b"profile")
                    return ""
                if "--extract-certificates=" in " ".join(arguments):
                    prefix = Path(next(item.split("=", 1)[1] for item in arguments if item.startswith("--extract-certificates=")))
                    Path(f"{prefix}0").write_bytes(b"certificate")
                    return ""
                if "-archs" in arguments:
                    return "arm64 x86_64"
                if "-dv" in arguments:
                    return "Identifier=org.snapetech.pleazyng\nTeamIdentifier=DM9A59DC85"
                return "Status: signed by a certificate trusted by Mac OS X"

            def fake_plist_command(arguments: list[str], **_: object) -> tuple[dict, bytes]:
                if "security" in arguments[0]:
                    return {"UUID": "profile-uuid", "TeamIdentifier": [producer.TEAM_IDENTIFIER]}, b"profile-plist"
                return entitlements, b"signed-entitlements"

            with (
                patch.object(producer, "run_checked", side_effect=fake_command),
                patch.object(producer, "run_plist_command", side_effect=fake_plist_command),
                patch.object(producer, "_certificate_sha1", return_value=producer.APP_CERTIFICATE_SHA1),
                patch.object(producer.capture_tools, "app_info", return_value=identity),
                patch.object(producer.capture_tools, "sha256_tree", return_value="a" * 64),
            ):
                restored = producer.restore_package(
                    artifact_dir=artifact_dir, selection_path=selection_path, output_dir=output_dir,
                    source_sha=SOURCE_SHA, source_tag=SOURCE_TAG, version="3.0.52", build="211",
                )

            receipt = json.loads(Path(restored["build_receipt"]).read_text(encoding="utf-8"))
            self.assertEqual(receipt["capture_input_kind"], "verified-macos-app-store-package")
            self.assertEqual(receipt["package_sha256"], package_sha)
            self.assertEqual(receipt["producer_artifact"]["artifact_id"], "15049005631")
            self.assertEqual(receipt["app_bundle_sha256"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
