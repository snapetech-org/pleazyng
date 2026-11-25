import hashlib
import json
import plistlib
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from verify_release_ci import GateError
import verify_ios_compatibility_checkpoint as checkpoint


class IosCompatibilityCheckpointTests(unittest.TestCase):
    source_sha = "a" * 40

    def test_producer_attempt_is_bound_to_the_run_and_source_and_allows_reused_jobs(self):
        producer = {
            "id": 25,
            "name": "Release CI / Apple native reliability (iOS)",
            "run_id": 42,
            "run_attempt": 1,
            "head_sha": self.source_sha,
            "status": "completed",
            "conclusion": "success",
        }
        with patch.object(checkpoint, "fetch_attempt_history", return_value=[producer]) as fetch:
            attempt = checkpoint.passing_producer_attempt(
                42,
                2,
                "token",
                ["Apple native reliability (iOS)", "Release CI / Apple native reliability (iOS)"],
                expected_workflow_sha=self.source_sha,
            )
        self.assertEqual(attempt, 1)
        fetch.assert_called_once_with(42, 2, "token")

    def test_producer_rejects_ambiguous_failed_or_foreign_execution(self):
        success = {
            "id": 25,
            "name": "Apple native reliability (iOS)",
            "run_id": 42,
            "run_attempt": 2,
            "head_sha": self.source_sha,
            "status": "completed",
            "conclusion": "success",
        }
        variants = [
            [success, {**success, "name": "Release CI / Apple native reliability (iOS)"}],
            [{**success, "conclusion": "failure"}],
            [{**success, "run_id": 43}],
            [{**success, "head_sha": "b" * 40}],
            [{**success, "run_attempt": 3}],
        ]
        for jobs in variants:
            with self.subTest(jobs=jobs), patch.object(checkpoint, "fetch_attempt_history", return_value=jobs), \
                    self.assertRaises(GateError):
                checkpoint.passing_producer_attempt(
                    42,
                    2,
                    "token",
                    ["Apple native reliability (iOS)", "Release CI / Apple native reliability (iOS)"],
                    expected_workflow_sha=self.source_sha,
                )

    def test_supplemental_failed_producer_preserves_its_failure_without_a_checkpoint(self):
        failed = {
            "id": 25,
            "name": "Build and test candidate iOS app",
            "run_id": 42,
            "run_attempt": 2,
            "status": "completed",
            "conclusion": "failure",
        }
        with patch.object(checkpoint, "fetch_attempt_history", return_value=[failed]):
            self.assertIsNone(
                checkpoint.passing_producer_attempt(
                    42,
                    2,
                    "token",
                    ["Build and test candidate iOS app"],
                    allow_nonpassing=True,
                )
            )

    def _checkpoint(self, directory: Path, *, source_sha=None, run_id=42, attempt=2, symlink_target=None):
        app_info = plistlib.dumps({"CFBundleIdentifier": checkpoint.BUNDLE_ID})
        archive_path = directory / "ios-simulator-app.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("Pleazyng.app/Info.plist", app_info)
            archive.writestr("Pleazyng.app/Frameworks/App.framework/App", b"framework")
            if symlink_target is not None:
                symlink = zipfile.ZipInfo("Pleazyng.app/Frameworks/App.framework/redirect")
                symlink.create_system = 3
                symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(symlink, symlink_target)
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        (directory / "ios-simulator-app.json").write_text(
            json.dumps({
                "sha256": digest,
                "product_source_sha": source_sha or self.source_sha,
                "run_id": run_id,
                "run_attempt": attempt,
                "bundle_id": checkpoint.BUNDLE_ID,
            }),
            encoding="utf-8",
        )

    def test_valid_checkpoint_matches_source_run_attempt_bundle_and_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            self._checkpoint(artifact_dir)
            self.assertEqual(
                checkpoint.verify_checkpoint(artifact_dir, self.source_sha, 42, 2),
                "Pleazyng.app",
            )

    def test_checkpoint_rejects_wrong_source_run_attempt_digest_and_unsafe_zip(self):
        for fields in (
            {"source_sha": "b" * 40},
            {"run_id": 43},
            {"attempt": 1},
        ):
            with tempfile.TemporaryDirectory() as directory:
                artifact_dir = Path(directory)
                self._checkpoint(artifact_dir, **fields)
                with self.subTest(fields=fields), self.assertRaises(GateError):
                    checkpoint.verify_checkpoint(artifact_dir, self.source_sha, 42, 2)

        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            self._checkpoint(artifact_dir)
            archive = artifact_dir / "ios-simulator-app.zip"
            archive.write_bytes(archive.read_bytes() + b"tamper")
            with self.assertRaisesRegex(GateError, "SHA-256"):
                checkpoint.verify_checkpoint(artifact_dir, self.source_sha, 42, 2)

        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            self._checkpoint(artifact_dir, symlink_target="../../../../outside")
            with self.assertRaisesRegex(GateError, "symlink"):
                checkpoint.verify_checkpoint(artifact_dir, self.source_sha, 42, 2)


if __name__ == "__main__":
    unittest.main()
