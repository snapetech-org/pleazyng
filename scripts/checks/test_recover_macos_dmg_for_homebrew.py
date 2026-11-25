#!/usr/bin/env python3
"""Exercise bounded GitHub API retries and source-job pagination."""

from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import recover_macos_dmg_for_homebrew as recovery  # noqa: E402


class RecoveryApiTests(unittest.TestCase):
    @patch("recover_macos_dmg_for_homebrew.time.sleep")
    @patch("recover_macos_dmg_for_homebrew.subprocess.run")
    def test_api_retries_transient_server_error(self, run, sleep):
        run.side_effect = [
            subprocess.CompletedProcess([], 1, "", "gh: Server Error (HTTP 502)"),
            subprocess.CompletedProcess([], 0, '{"ready": true}', ""),
        ]
        self.assertEqual(recovery.api("example"), {"ready": True})
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("recover_macos_dmg_for_homebrew.time.sleep")
    @patch("recover_macos_dmg_for_homebrew.subprocess.run")
    def test_api_does_not_retry_client_errors(self, run, sleep):
        run.return_value = subprocess.CompletedProcess([], 1, "", "gh: Not Found (HTTP 404)")
        with self.assertRaises(subprocess.CalledProcessError):
            recovery.api("example")
        run.assert_called_once()
        sleep.assert_not_called()

    @patch("recover_macos_dmg_for_homebrew.api")
    def test_source_build_job_reads_all_pages_of_thirty(self, api):
        api.side_effect = [
            {"total_count": 31, "jobs": [{"name": f"job-{i}"} for i in range(30)]},
            {"total_count": 31, "jobs": [{
                "name": "build-macos",
                "status": "completed",
                "conclusion": "success",
                "run_attempt": recovery.SOURCE_RUN_ATTEMPT,
                "steps": [{"name": "Build macOS", "conclusion": "success"}],
            }]},
        ]
        job = recovery.source_build_job()
        self.assertEqual(job["name"], "build-macos")
        self.assertEqual(api.call_count, 2)
        self.assertIn("per_page=30&page=1", api.call_args_list[0].args[0])
        self.assertIn("per_page=30&page=2", api.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
