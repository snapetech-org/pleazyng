"""Guard the decision to retry all failed jobs in a hosted workflow attempt."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import recover_hosted_capacity as capacity


NOT_ACQUIRED = "The job was not acquired by Runner of type hosted even after multiple attempts"
QUEUE_WARNING = "Due to capacity constraints, jobs targeting macOS arm64 runners may experience longer queue times."
WORKER_NO_SPACE = (
    "Unhandled exception. System.IO.IOException: No space left on device : "
    "'/Users/runner/actions-runner/cached/_diag/Worker_20261124-180538-utc.log'"
)


class HostedCapacityTests(unittest.TestCase):
    repository = "snapetech-org/pleazyng"
    route = f"repos/{repository}/actions/runs/42"

    def setUp(self):
        self.attempt = 3
        self.workflow = {
            "id": 42, "event": "workflow_dispatch", "head_branch": "main",
            "head_repository": {"full_name": self.repository}, "path": ".github/workflows/build.yml",
            "status": "completed", "conclusion": "failure", "run_attempt": self.attempt,
        }
        self.tvos = {
            "id": 101, "name": "Release CI / Apple native reliability (tvOS)",
            "status": "completed", "conclusion": "cancelled", "labels": ["macos-26"],
            "runner_id": 0, "runner_name": "", "steps": [],
        }
        self.macos = {**self.tvos, "id": 102, "name": "Release CI / Apple native reliability (macOS)"}
        self.apple_gate = {
            "id": 105, "name": "Release CI / Apple native reliability", "status": "completed",
            "conclusion": "failure", "labels": ["ubuntu-24.04"], "runner_id": 9,
            "steps": [{"name": "Require all Apple native suites", "conclusion": "failure"}],
        }
        self.jobs = [
            {"jobs": [self.tvos, {**self.tvos, "id": 103, "conclusion": "success", "runner_id": 9}]},
            {"jobs": [self.macos, {**self.macos, "id": 104, "conclusion": "skipped"}]},
        ]
        self.annotations = {
            job["id"]: [[{"message": QUEUE_WARNING}], [{"message": NOT_ACQUIRED}]]
            for job in (self.tvos, self.macos)
        }
        self.mutate = self.enterContext(patch.object(capacity.subprocess, "run", autospec=True))
        self.enterContext(patch.object(capacity, "api", side_effect=self.api))
        self.sleep = self.enterContext(patch.object(capacity.time, "sleep"))

    def api(self, path, *, pages=False):
        if path == self.route and not pages:
            return self.workflow.copy()
        if path == f"{self.route}/attempts/{self.attempt}/jobs?per_page=30" and pages:
            return self.jobs
        for job_id, annotations in self.annotations.items():
            if path == f"repos/{self.repository}/check-runs/{job_id}/annotations?per_page=100" and pages:
                return annotations
        self.fail(f"Unexpected API read: {path} (paginated={pages})")

    def recover(self, *, retry=True):
        return capacity.recover(self.repository, 42, self.attempt, retry)

    def test_retries_both_unacquired_jobs_with_one_failed_jobs_request(self):
        self.recover()
        self.mutate.assert_called_once_with(
            ["gh", "api", "--method", "POST", f"{self.route}/rerun-failed-jobs"], check=True
        )

    def test_retries_the_apple_gate_after_confirmed_suite_allocation_failures(self):
        self.jobs[-1]["jobs"].append(self.apple_gate)
        self.assertIn(self.apple_gate["name"], self.recover())
        self.mutate.assert_called_once_with(
            ["gh", "api", "--method", "POST", f"{self.route}/rerun-failed-jobs"], check=True
        )

    def test_requires_investigation_when_an_apple_suite_actually_runs_and_fails(self):
        self.jobs[-1]["jobs"].extend([self.apple_gate, {
            **self.macos, "id": 106, "name": "Release CI / Apple native reliability (iOS)",
            "conclusion": "failure", "runner_id": 11,
            "steps": [{"name": "Qualify Apple native components", "conclusion": "failure"}],
        }])
        self.assertIn("Requires investigation", self.recover())
        self.mutate.assert_not_called()

    def test_retries_a_hosted_ios_worker_disk_failure_once_with_its_gate(self):
        self.attempt = self.workflow["run_attempt"] = 2
        suite = {
            **self.macos, "id": 106, "name": "Release CI / Apple native reliability (iOS)",
            "conclusion": "failure", "runner_id": 11, "labels": ["xcode-27"],
            "steps": [{"name": "Qualify Apple native components", "status": "in_progress", "conclusion": None}],
        }
        self.jobs[-1]["jobs"].extend([self.apple_gate, suite])
        self.annotations[106] = [[{"annotation_level": "failure", "message": WORKER_NO_SPACE}]]
        self.assertIn(suite["name"], self.recover())
        self.mutate.assert_called_once()

        self.mutate.reset_mock()
        self.annotations[106][0].append({"annotation_level": "failure", "message": "Swift test failed"})
        self.assertIn("No hosted worker disk exhaustion recorded", self.recover())
        self.mutate.assert_not_called()

        self.annotations[106][0].pop()
        self.attempt = self.workflow["run_attempt"] = 3
        self.assertIn("Requires investigation", self.recover())
        self.mutate.assert_not_called()

    def test_aggregate_failure_needs_a_matching_suite_allocation_failure(self):
        package = {**self.tvos, "name": "Build tvOS developer package (arm64)"}
        self.jobs = [{"jobs": [package, self.apple_gate]}]
        self.assertIn("No hosted suite allocation failure recorded", self.recover())
        self.mutate.assert_not_called()

    def test_one_unconfirmed_job_blocks_retry_even_with_a_capacity_warning(self):
        self.annotations[self.macos["id"]] = [[{"message": QUEUE_WARNING}]]
        self.recover()
        self.mutate.assert_not_called()

    def test_one_started_active_or_private_job_blocks_all_retries(self):
        for evidence in (
            {"runner_id": 1}, {"runner_name": "hosted-mac"}, {"steps": [{"name": "Set up job"}]},
            {"status": "in_progress", "conclusion": None}, {"status": "queued", "conclusion": None},
            {"labels": ["self-hosted", "macOS", "arm64"]},
        ):
            with self.subTest(evidence=evidence):
                self.mutate.reset_mock()
                self.jobs[-1]["jobs"][0] = {**self.macos, **evidence}
                self.recover()
                self.mutate.assert_not_called()

    def test_preserves_cancelled_active_and_superseded_workflows(self):
        for status, conclusion, latest in (
            ("completed", "cancelled", self.attempt),
            ("in_progress", None, self.attempt),
            ("completed", "failure", self.attempt + 1),
        ):
            with self.subTest(status=status, conclusion=conclusion, latest=latest):
                self.mutate.reset_mock()
                self.workflow.update(status=status, conclusion=conclusion, run_attempt=latest)
                self.recover()
                self.mutate.assert_not_called()

    def test_attempt_advancing_during_inspection_blocks_retry(self):
        def advancing_api(path, *, pages=False):
            response = self.api(path, pages=pages)
            if "/annotations?" in path:
                self.workflow["run_attempt"] = self.attempt + 1
            return response

        with patch.object(capacity, "api", side_effect=advancing_api):
            self.recover()
        self.mutate.assert_not_called()

    def test_concurrent_retry_winning_the_post_race_is_successful(self):
        def publish_new_attempt(_seconds):
            self.workflow.update(status="queued", conclusion=None, run_attempt=self.attempt + 1)

        self.mutate.side_effect = subprocess.CalledProcessError(1, "gh")
        self.sleep.side_effect = publish_new_attempt
        self.assertEqual(self.recover(), "Run has already advanced")
        self.mutate.assert_called_once()
        self.sleep.assert_called_once()

    def test_rejected_retry_without_state_change_remains_an_error(self):
        self.mutate.side_effect = subprocess.CalledProcessError(1, "gh")
        with self.assertRaises(subprocess.CalledProcessError):
            self.recover()
        self.mutate.assert_called_once()

    def test_historical_attempt_can_be_classified_without_mutation(self):
        self.workflow.update(run_attempt=self.attempt + 1, conclusion="success")
        result = self.recover(retry=False)
        for job in (self.tvos, self.macos):
            self.assertIn(job["name"], result)
        self.mutate.assert_not_called()

    def test_eighth_attempt_stops_without_requesting_another_retry(self):
        self.attempt = self.workflow["run_attempt"] = 8
        with self.assertRaisesRegex(SystemExit, "after 8 attempts"):
            self.recover()
        self.mutate.assert_not_called()

    def test_recent_scan_only_recovers_latest_failed_run_per_title(self):
        def list_runs(path, *, pages=False):
            self.assertTrue(pages)
            if "/workflows/build.yml/" not in path:
                return []
            return [
                {"workflow_runs": [
                    {"id": 51, "display_title": "iOS", "conclusion": None, "run_attempt": 1},
                    {"id": 50, "display_title": "tvOS", "conclusion": "failure", "run_attempt": 2},
                ]},
                {"workflow_runs": [
                    {"id": 49, "display_title": "iOS", "conclusion": "failure", "run_attempt": 1},
                    {"id": 48, "display_title": "tvOS", "conclusion": "failure", "run_attempt": 1},
                    {"id": 47, "display_title": "old run", "conclusion": "failure", "run_attempt": 8},
                ]},
            ]

        with patch.object(capacity, "api", side_effect=list_runs), patch.object(capacity, "recover") as recover:
            capacity.recover_recent(self.repository, retry=True)

        recover.assert_called_once_with(self.repository, 50, 2, True)


if __name__ == "__main__":
    unittest.main()
