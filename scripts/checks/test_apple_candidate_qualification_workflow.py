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
            "run-name: apple-candidate-qualification:${{ inputs.source_run_id }}:${{ inputs.product_source_sha }}"
            "${{ inputs.retained_run_id != '' && format(':resume:{0}:{1}', inputs.retained_run_id, inputs.retry_platform) || '' }}",
            self.workflow,
        )
        self.assertRegex(self.workflow, r"(?m)^on:\n  workflow_dispatch:\n")
        self.assertNotRegex(self.workflow, r"(?m)^  (?:push|pull_request|schedule|workflow_call):")
        self.assertIn("source_run_id:\n", self.workflow)
        self.assertIn("product_source_sha:\n", self.workflow)
        self.assertIn("retained_run_id:\n", self.workflow)
        self.assertIn("retry_platform:\n", self.workflow)
        for platform in ("all", "ios", "macos", "tvos"):
            self.assertRegex(self.workflow, rf"(?m)^          - {platform}$")
        self.assertIn('os.environ["REF_NAME"] == "refs/heads/main"', self.workflow)
        self.assertIn("github.workflow_ref", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)
        self.assertIn("group: apple-candidate-qualification-${{ inputs.source_run_id }}", self.workflow)

    def test_native_matrix_is_public_arm64_and_runs_every_platform_suite(self):
        self.assertIn(
            "name: ${{ matrix.platform == 'iOS' && 'Build and test candidate iOS app' || format('",
            self.workflow,
        )
        self.assertIn("format('Apple native reliability ({0})', matrix.platform)", self.workflow)
        self.assertIn("runs-on: xcode-27", self.workflow)
        self.assertIn("inputs.retained_run_id == '' || inputs.retry_platform == 'all'", self.workflow)
        self.assertIn("inputs.retry_platform == 'ios' &&", self.workflow)
        self.assertIn("inputs.retry_platform == 'macos' &&", self.workflow)
        self.assertIn('{\"platform\":\"iOS\",\"slug\":\"ios\"}', self.workflow)
        self.assertIn('{\"platform\":\"macOS\",\"slug\":\"macos\"}', self.workflow)
        self.assertIn('{\"platform\":\"tvOS\",\"slug\":\"tvos\"}', self.workflow)
        self.assertIn('retained_run_id == "" and retry_platform == "all"', self.workflow)
        self.assertIn('retained_run_id != "" and retry_platform != "all"', self.workflow)
        self.assertIn("- name: Require native Apple Silicon host", self.workflow)
        self.assertIn('test "$RUNNER_ENVIRONMENT" = github-hosted', self.workflow)
        self.assertIn('test "$(uname -m)" = arm64', self.workflow)
        self.assertIn("hosted-xcode-version: '27.1'", self.workflow)
        self.assertIn("require-duo-sdk: ${{ matrix.platform == 'iOS'", self.workflow)
        self.assertIn(
            "defer-older-iphone-smoke: ${{ matrix.platform == 'iOS' && 'true' || 'false' }}",
            self.workflow,
        )
        self.assertIn(
            '"Apple native reliability (iOS): Launch the same iOS app on an older phone runtime"',
            self.workflow,
        )
        self.assertNotIn("build-only: 'true'", self.workflow)
        self.assertNotIn("motion-test-only: 'true'", self.workflow)

    def test_legacy_iphone_smoke_is_a_separate_retained_artifact_consumer(self):
        consumer = self.workflow.split("  apple-native-ios-compatibility:\n", 1)[1]
        self.assertIn("name: Apple native reliability (iOS)", consumer)
        self.assertIn("needs: apple-native-reliability", consumer)
        self.assertIn("if: ${{ !cancelled() &&", consumer)
        self.assertNotIn("needs.apple-native-reliability.result", consumer)
        self.assertIn("inputs.retained_run_id == '' || inputs.retry_platform == 'ios'", consumer)
        self.assertIn("runs-on: macos-26", consumer)
        expected_steps = (
            "Verify the iOS checkpoint producer",
            "Download candidate iOS app artifact",
            "Verify frozen candidate source",
            "Verify retained Apple siblings",
            "Require native Apple Silicon host",
            "Extract candidate iOS app",
            "Qualify Apple native components",
        )
        positions = [consumer.index(f"- name: {step}") for step in expected_steps]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("name: ios-compatibility-app-${{ github.run_id }}", consumer)
        self.assertIn("ios-simulator-app.zip", consumer)
        self.assertIn("ios-simulator-app.json", consumer)
        self.assertIn("for chunk in iter(lambda: archive_file.read(1024 * 1024), b\"\")", consumer)
        self.assertIn('receipt.get("product_source_sha") == product_sha', consumer)
        self.assertIn('receipt["run_id"] == run_id', consumer)
        self.assertIn('producer_attempt == expected_producer_attempt', consumer)
        self.assertIn('steps.ios-producer.outputs.attempt !=', consumer)
        self.assertIn('receipt.get("bundle_id") == "org.snapetech.pleazyng"', consumer)
        self.assertIn('verify_retained_apple_siblings(', consumer)
        self.assertIn('test "$RUNNER_ENVIRONMENT" = github-hosted', consumer)
        self.assertIn('test "$(uname -m)" = arm64', consumer)
        self.assertIn('runtime.get("version") == "26.5"', consumer)
        self.assertIn("--require-installed-runtime", consumer)

    def test_runner_scratch_is_configured_after_the_job_starts(self):
        job_setup = self.workflow.split('\njobs:\n', 1)[1].split('\n    steps:\n', 1)[0]
        self.assertNotIn('${{ runner.', job_setup)
        self.assertIn('PUB_CACHE=$RUNNER_TEMP/apple-native-pub-$PLATFORM', self.workflow)

    def test_source_preflight_precedes_tool_setup_and_checks_canonical_candidate(self):
        preflight = self.workflow.find("- name: Verify frozen candidate source")
        retained = self.workflow.find("- name: Verify retained Apple siblings")
        host = self.workflow.find("- name: Require native Apple Silicon host")
        native = self.workflow.find("- name: Qualify Apple native components")
        self.assertGreaterEqual(preflight, 0)
        self.assertGreater(retained, preflight)
        self.assertGreater(host, retained)
        self.assertGreater(native, preflight)
        self.assertIn("from verify_embedded_release_ci import verify_retained_apple_siblings", self.workflow)
        self.assertIn('os.environ["RETAINED_RUN_ID"]', self.workflow)
        self.assertIn('os.environ["RETRY_PLATFORM"]', self.workflow)
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
        self.assertIn('"Build and test candidate iOS app"', self.workflow)
        self.assertIn(
            '"Apple native reliability (iOS): Launch the same iOS app on an older phone runtime"',
            self.workflow,
        )
        self.assertIn("actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9", self.workflow)
        self.assertIn("apple-candidate-qualification-${{ matrix.slug }}-${{ github.run_id }}", self.workflow)


if __name__ == "__main__":
    unittest.main()
