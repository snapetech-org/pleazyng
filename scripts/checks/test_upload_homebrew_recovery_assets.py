#!/usr/bin/env python3
"""Verify retry-safe appends for the v3.0.51 supplemental Homebrew assets."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from upload_homebrew_recovery_assets import ASSET_NAMES, upload  # noqa: E402


class UploadHomebrewRecoveryAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.digests = {}
        for name in ASSET_NAMES:
            data = f"exact {name}\n".encode()
            (self.directory / name).write_bytes(data)
            self.digests[name] = "sha256:" + hashlib.sha256(data).hexdigest()

    def remote(self, names):
        return json.dumps({"assets": [
            {"name": name, "digest": self.digests[name]} for name in names
        ]})

    @patch("upload_homebrew_recovery_assets.subprocess.run")
    @patch("upload_homebrew_recovery_assets.subprocess.check_output")
    def test_uploads_all_missing_names(self, check_output, run):
        check_output.return_value = self.remote([])
        missing = upload("v3.0.51", "snapetech-org/pleazyng", self.directory)
        self.assertEqual(missing, list(ASSET_NAMES))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-4:], [str(self.directory / name) for name in ASSET_NAMES])

    @patch("upload_homebrew_recovery_assets.subprocess.run")
    @patch("upload_homebrew_recovery_assets.subprocess.check_output")
    def test_matching_existing_assets_are_kept_and_only_missing_are_uploaded(self, check_output, run):
        check_output.return_value = self.remote([ASSET_NAMES[0], ASSET_NAMES[2]])
        missing = upload("v3.0.51", "snapetech-org/pleazyng", self.directory)
        self.assertEqual(missing, [ASSET_NAMES[1], ASSET_NAMES[3]])
        self.assertEqual(run.call_args.args[0][-2:], [
            str(self.directory / ASSET_NAMES[1]), str(self.directory / ASSET_NAMES[3]),
        ])

    @patch("upload_homebrew_recovery_assets.subprocess.run")
    @patch("upload_homebrew_recovery_assets.subprocess.check_output")
    def test_mismatched_existing_asset_fails_without_upload(self, check_output, run):
        metadata = json.loads(self.remote([ASSET_NAMES[0]]))
        metadata["assets"][0]["digest"] = "sha256:" + "0" * 64
        check_output.return_value = json.dumps(metadata)
        with self.assertRaisesRegex(ValueError, "preserved recovery bundle"):
            upload("v3.0.51", "snapetech-org/pleazyng", self.directory)
        run.assert_not_called()

    @patch("upload_homebrew_recovery_assets.subprocess.run")
    @patch("upload_homebrew_recovery_assets.subprocess.check_output")
    def test_complete_matching_assets_are_not_reuploaded(self, check_output, run):
        check_output.return_value = self.remote(ASSET_NAMES)
        self.assertEqual(upload("v3.0.51", "snapetech-org/pleazyng", self.directory), [])
        run.assert_not_called()

    @patch("upload_homebrew_recovery_assets.subprocess.check_output")
    def test_upload_is_pinned_to_exact_release_and_repository(self, check_output):
        check_output.return_value = self.remote([])
        with self.assertRaisesRegex(ValueError, "restricted"):
            upload("v3.0.52", "snapetech-org/pleazyng", self.directory)
        with self.assertRaisesRegex(ValueError, "restricted"):
            upload("v3.0.51", "attacker/example", self.directory)
        check_output.assert_not_called()


if __name__ == "__main__":
    unittest.main()
