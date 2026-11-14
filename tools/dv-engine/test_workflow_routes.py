"""Preserve native platform routes and isolated, secret-free validation."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def job_block(source: str, job_id: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job_id)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        source,
    )
    if match is None:
        raise AssertionError(f"missing workflow job {job_id}")
    return match.group(1)


class DolbyWorkflowRouteTests(unittest.TestCase):
    def test_trusted_dolby_workflows_are_manual(self):
        paths = {
            "dv-platforms.yml": "codex/dv-platform-*",
            "dv-experimental.yml": "codex/dv-experimental-*",
        }
        for name, branch in paths.items():
            with self.subTest(workflow=name):
                source = (WORKFLOWS / name).read_text()
                self.assertIn("workflow_dispatch:", source)
                self.assertNotRegex(source, r"(?m)^  (push|pull_request|pull_request_target|schedule):")
                self.assertNotIn("pull_request:", source)
                self.assertNotIn("|| 'ubuntu-latest'", source)

    def test_offline_packages_use_native_architecture_and_clean_export_verification(self):
        source = (WORKFLOWS / 'dv-platforms.yml').read_text()
        job = job_block(source, 'offline-tools')
        self.assertIn("'ubuntu-24.04-arm'", job)
        self.assertIn("'ubuntu-24.04'", job)
        self.assertNotIn('qemu', job)
        self.assertNotIn('secrets.', job)
        self.assertIn('--network none --read-only', job)
        self.assertIn('verify_offline_bundle.py', job)

    def test_dolby_jobs_use_compatible_public_hosted_runners(self):
        expected = {
            "dv-platforms.yml": {
                "contracts": ("ubuntu-24.04", "windows-2025", "macos-26"),
                "app-tests": ("ubuntu-24.04",),
                "felbaker-source": ("ubuntu-24.04",),
                "apple-source": ("macos-26",),
            },
            "dv-experimental.yml": {
                "linux-engine": ("ubuntu-24.04",),
            },
        }
        for workflow, jobs in expected.items():
            source = (WORKFLOWS / workflow).read_text()
            for job_id, runners in jobs.items():
                with self.subTest(workflow=workflow, job=job_id):
                    block = job_block(source, job_id)
                    for runner in runners:
                        self.assertIn(runner, block)
                    self.assertNotIn("private-apple-builds", block)
                    self.assertNotIn("ng-shared-main-jit", block)
                    self.assertIn("persist-credentials: false", block)

    def test_platform_specific_jobs_route_to_native_public_runners(self):
        platforms = (WORKFLOWS / "dv-platforms.yml").read_text()
        contracts = job_block(platforms, "contracts")
        self.assertIn("matrix.platform == 'windows-x64'", contracts)
        self.assertIn("'windows-2025'", contracts)
        self.assertIn("platform: [linux-x64, windows-x64, macos-arm64]", contracts)
        self.assertIn("'macos-26'", contracts)

        apple = job_block(platforms, "apple-source")
        self.assertIn("runs-on: macos-26", apple)

    def test_manual_fork_validation_is_public_and_secret_free(self):
        source = (WORKFLOWS / "dv-fork-validation.yml").read_text()
        self.assertIn("workflow_dispatch:", source)
        self.assertNotRegex(source, r"(?m)^  (push|pull_request|pull_request_target|schedule):")
        self.assertIn("runs-on: ubuntu-24.04", source)
        self.assertIn("permissions:\n  contents: read", source)
        self.assertIn("persist-credentials: false", source)
        self.assertNotIn("secrets.", source)
        self.assertNotIn("ubuntu-latest", source)
        self.assertNotIn("ng-shared-main-jit", source)
        self.assertNotIn("gvisor-sandbox", source)


if __name__ == "__main__":
    unittest.main()
