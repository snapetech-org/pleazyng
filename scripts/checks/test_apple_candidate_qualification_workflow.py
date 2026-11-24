"""Guard the frozen Apple candidate qualification producer wiring."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/apple-candidate-qualification.yml"
ACTION = ROOT / ".github/actions/apple-native-reliability/action.yml"


class AppleCandidateQualificationWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.action = ACTION.read_text(encoding="utf-8")

    def test_workflow_is_main_only_dispatch_with_exact_recovery_identity(self):
        self.assertIn(
            "run-name: apple-candidate-qualification:${{ inputs.source_run_id }}:${{ inputs.product_source_sha }}",
            self.workflow,
        )
        self.assertRegex(self.workflow, r"(?m)^on:\n  workflow_dispatch:\n")
        self.assertNotRegex(self.workflow, r"(?m)^  (?:push|pull_request|schedule|workflow_call):")
        self.assertIn("source_run_id:\n", self.workflow)
        self.assertIn("product_source_sha:\n", self.workflow)
        self.assertIn('os.environ["REF_NAME"] == "refs/heads/main"', self.workflow)
        self.assertIn("github.workflow_ref", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)
        self.assertIn("group: apple-candidate-qualification-${{ inputs.source_run_id }}", self.workflow)

    def test_native_matrix_is_public_arm64_and_runs_every_platform_suite(self):
        self.assertIn("name: Apple native reliability (${{ matrix.platform }})", self.workflow)
        self.assertIn("runs-on: xcode-27", self.workflow)
        for platform in ("iOS", "macOS", "tvOS"):
            self.assertRegex(self.workflow, rf"(?m)^          - platform: {platform}$")
        self.assertIn("- name: Require native Apple Silicon host", self.workflow)
        self.assertIn('test "$RUNNER_ENVIRONMENT" = github-hosted', self.workflow)
        self.assertIn('test "$(uname -m)" = arm64', self.workflow)
        self.assertIn("hosted-xcode-version: '27.1'", self.workflow)
        self.assertIn("require-duo-sdk: ${{ matrix.platform == 'iOS'", self.workflow)
        self.assertIn('"Launch the same iOS app on an older phone runtime"', self.workflow)
        self.assertNotIn("build-only: 'true'", self.workflow)
        self.assertNotIn("motion-test-only: 'true'", self.workflow)

    def test_source_preflight_precedes_tool_setup_and_checks_canonical_candidate(self):
        preflight = self.workflow.find("- name: Verify frozen candidate source")
        native = self.workflow.find("- name: Qualify Apple native components")
        self.assertGreaterEqual(preflight, 0)
        self.assertGreater(native, preflight)
        for required in (
            "Original release run ID must be numeric",
            'source_run.get("head_branch") == "main"',
            'source_run.get("head_sha") == product_sha',
            ".github/workflows/build.yml",
            'source_run["repository"].get("id") == repository_id',
            'source_run["head_repository"].get("id") == repository_id',
            'source_run.get("status") in {"queued", "in_progress", "waiting", "pending"}',
            'job.get("conclusion") in {"failure", "cancelled", "timed_out"}',
            "per_page=30&page={page}",
            "candidate_ref = api(f\"git/ref/tags/{candidate_tag}\")",
            'ref_object.get("type") == "tag"',
            'target.get("sha") == product_sha',
            "compare/{product_sha}...main",
            "Stable release tag already exists",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.workflow, msg=f"missing preflight check: {required}")

    def test_action_candidate_mode_limits_overlays_and_preserves_normal_checkout(self):
        for name in (
            "candidate-source-sha",
            "candidate-test-harness-sha",
            "candidate-dv-build-tool-sha",
        ):
            self.assertRegex(
                self.action,
                rf"(?ms)^  {re.escape(name)}:\n.*?^    default: ''$",
            )
        self.assertIn('git worktree add --detach "$SUITE_ROOT" "$GITHUB_SHA"', self.action)
        self.assertIn("ios/RunnerTests/AVFoundationMotionPlaybackTests.swift", self.action)
        self.assertIn("tools/dv-engine/build_apple_runtime.py", self.action)
        self.assertIn("Candidate source contains an unapproved overlay", self.action)
        self.assertIn("inputs.platform == 'macOS' || inputs.build-only == 'true'", self.action)
        self.assertIn("xcodebuild build-for-testing", self.action)
        self.assertIn("xcodebuild test", self.action)
        self.assertIn("Launch the same iOS app on an older phone runtime", self.action)

    def test_receipt_records_source_test_tool_and_workflow_identities(self):
        self.assertIn('"purpose": "supplemental native Apple qualification', self.workflow)
        for field in (
            '"product_source_sha"',
            '"staged_product_source_sha"',
            '"candidate_tag_object_sha"',
            '"source_run_id"',
            '"source_run_attempt"',
            '"test_harness_source_sha"',
            '"test_harness_in_product"',
            '"test_harness_overlay_applied"',
            '"test_harness_blob_sha"',
            '"dv_build_tool_source_sha"',
            '"dv_build_tool_in_product"',
            '"dv_build_tool_overlay_applied"',
            '"dv_build_tool_blob_sha"',
            '"workflow_source_sha"',
            '"qualification_run_id"',
            '"qualification_job"',
            '"native_test_outcome"',
            '"test_mode"',
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.workflow)
        self.assertIn("actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9", self.workflow)
        self.assertIn("apple-candidate-qualification-${{ matrix.slug }}-${{ github.run_id }}", self.workflow)


if __name__ == "__main__":
    unittest.main()
