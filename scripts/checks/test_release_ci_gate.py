#!/usr/bin/env python3
"""Regression coverage for the exact-commit release CI gate manifest."""

from __future__ import annotations

import base64
import json
import datetime as dt
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_release_ci as release_ci  # noqa: E402
from verify_release_ci import _manifest_gate  # noqa: E402
from verify_tvos_runner_acquisition import RunnerAcquisitionError  # noqa: E402
from release_all_platforms import (  # noqa: E402
    RELEASE_TAG_REUSE_ALLOWLIST,
    ReleaseError,
    await_hosted_retry,
    apple_workflow_route,
    dispatch,
    ensure_candidate_build,
    ensure_ci,
    ensure_release,
    ensure_tag,
    failed_preflight_ci_run,
    select_apple_runner_pool,
    trusted_preflight_ci_run,
    release_ci_source,
    resume_hosted_retry,
    run_has_successful_step,
    wait_for_run,
)


class AppleRunnerRoutingTests(unittest.TestCase):
    def test_hosted_apple_sidecar_cleanup_is_allowlisted_by_both_tag_guards(self) -> None:
        paths = {
            "scripts/clean_appledouble_sidecars.sh",
            "scripts/checks/test_clean_appledouble_sidecars.py",
        }
        self.assertTrue(paths <= RELEASE_TAG_REUSE_ALLOWLIST)
        workflow = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
        case = workflow.split('case "$changed_file" in', 1)[1].split("              *)", 1)[0]
        for path in paths:
            with self.subTest(path=path):
                self.assertIn(path, case)

    def test_macos_flutter_download_deadline_is_allowlisted_by_both_tag_guards(self) -> None:
        paths = {
            "scripts/cache_macos_flutter_sdk.py",
            "scripts/checks/test_cache_macos_flutter_sdk.py",
        }
        self.assertTrue(paths <= RELEASE_TAG_REUSE_ALLOWLIST)
        workflow = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
        case = workflow.split('case "$changed_file" in', 1)[1].split("              *)", 1)[0]
        for path in paths:
            with self.subTest(path=path):
                self.assertIn(path, case)

    def test_tv_mpv_mirror_fix_is_allowlisted_by_both_tag_guards(self) -> None:
        paths = {
            "scripts/tv_mpv/build.py",
            "scripts/checks/test_tv_mpv_build.py",
        }
        self.assertTrue(paths <= RELEASE_TAG_REUSE_ALLOWLIST)
        workflow = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
        case = workflow.split('case "$changed_file" in', 1)[1].split("              *)", 1)[0]
        for path in paths:
            with self.subTest(path=path):
                self.assertIn(path, case)

    def test_signing_recovery_test_is_allowlisted_by_both_tag_guards(self) -> None:
        path = "scripts/checks/test_release_signing_options.py"
        self.assertIn(path, RELEASE_TAG_REUSE_ALLOWLIST)
        workflow = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
        case = workflow.split('case "$changed_file" in', 1)[1].split("              *)", 1)[0]
        self.assertIn(path, case)

    def test_automatic_route_uses_compatible_hosted_apple_first(self) -> None:
        self.assertEqual(select_apple_runner_pool("auto"), "hosted")

    def test_mobile_hosted_macos_private_route_is_split(self) -> None:
        self.assertEqual(apple_workflow_route("hosted", "private"), "private")
        self.assertEqual(apple_workflow_route("private", "private"), "private")
        self.assertEqual(apple_workflow_route("hosted", "hosted"), "hosted")
        self.assertEqual(apple_workflow_route("hosted", "private", "private"), "private")

    @patch("release_all_platforms.wait_for_run")
    @patch("release_all_platforms.workflow_runs")
    @patch("release_all_platforms.command")
    def test_split_dispatch_keeps_mac_private(
        self, _command, workflow_runs, wait_for_run_mock,
    ) -> None:
        run = {
            "databaseId": 17,
            "event": "workflow_dispatch",
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        workflow_runs.return_value = [run]
        wait_for_run_mock.return_value = {"databaseId": 17, "conclusion": "success"}

        dispatch(
            "snapetech-org/pleazyng", "build.yml", "main",
            {"execution_profile": "hybrid", "tvos_runner_pool": "split"},
            "split Apple route",
        )

        wait_for_run_mock.assert_called_once_with(
            "snapetech-org/pleazyng", run, "split Apple route", private_apple=True,
            allow_failure=False,
        )

    @patch("release_all_platforms.wait_for_run")
    @patch("release_all_platforms.workflow_runs")
    @patch("release_all_platforms.command")
    def test_private_ios_dispatch_starts_jit(
        self, _command, workflow_runs, wait_for_run_mock,
    ) -> None:
        run = {
            "databaseId": 20,
            "event": "workflow_dispatch",
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        workflow_runs.return_value = [run]
        wait_for_run_mock.return_value = {"databaseId": 20, "conclusion": "success"}

        dispatch(
            "snapetech-org/pleazyng", "build.yml", "main",
            {"execution_profile": "hybrid", "tvos_runner_pool": "private-ios"},
            "private iOS route",
        )

        wait_for_run_mock.assert_called_once_with(
            "snapetech-org/pleazyng", run, "private iOS route", private_apple=True,
            allow_failure=False,
        )

    @patch("release_all_platforms.configured_repo_variables")
    @patch("release_all_platforms.wait_for_run")
    @patch("release_all_platforms.workflow_runs")
    @patch("release_all_platforms.command")
    def test_captured_dispatch_starts_jit_only_for_private_ci_override(
        self, _command, workflow_runs, wait_for_run_mock, variables,
    ) -> None:
        run = {
            "databaseId": 21,
            "event": "workflow_dispatch",
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        workflow_runs.return_value = [run]
        for pool, signing_unavailable, expected in (
            ("", False, False),
            ("private", False, True),
            ("private", True, False),
        ):
            with self.subTest(pool=pool, signing_unavailable=signing_unavailable):
                variables.return_value = {"APPLE_NATIVE_CI_RUNNER_POOL": pool}
                dispatch(
                    "snapetech-org/pleazyng", "build.yml", "main",
                    {"candidate_ref": "v3.0.51-candidate.fixture", "execution_profile": "hybrid",
                     "tvos_runner_pool": "hosted", "private_apple_signing_unavailable": str(signing_unavailable).lower()},
                    "captured Apple route",
                )
                wait_for_run_mock.assert_called_with(
                    "snapetech-org/pleazyng", run, "captured Apple route",
                    private_apple=expected, allow_failure=False,
                )

    @patch("release_all_platforms.ensure_remote_main_sha")
    @patch("release_all_platforms.dispatch", return_value={
        "databaseId": 18, "status": "completed", "conclusion": "success",
        "jobs": [{"name": "Build Windows (arm64)", "status": "completed", "conclusion": "success"}],
    })
    @patch("release_all_platforms.active_run", return_value=None)
    @patch("release_all_platforms.matching_run", return_value=None)
    def test_candidate_dispatch_keeps_unsigned_apple_jobs_hosted(
        self, _matching_run, _active_run, dispatch_mock, _ensure_main,
    ) -> None:
        ensure_candidate_build("3.0.34", "a" * 40)

        fields = dispatch_mock.call_args.args[3]
        self.assertEqual(fields["tvos_runner_pool"], "hosted")

    @patch("release_all_platforms.run_has_successful_step", return_value=True)
    @patch("release_all_platforms.ensure_remote_main_sha")
    @patch("release_all_platforms.dispatch", return_value={
        "databaseId": 19, "status": "completed", "conclusion": "success",
        "jobs": [{"name": "Build Windows (arm64)", "status": "completed", "conclusion": "success"}],
    })
    @patch("release_all_platforms.active_run", return_value=None)
    @patch("release_all_platforms.matching_run", return_value=None)
    def test_private_apple_route_is_only_selected_for_requested_signing(
        self, _matching_run, _active_run, dispatch_mock, _ensure_main, _signed_steps,
    ) -> None:
        ensure_candidate_build(
            "3.0.34", "b" * 40, tvos_runner_pool="auto", macos_runner_pool="private",
            ios_runner_pool="private", include_apple_store_build=True,
            prepare_homebrew_cask=True,
        )

        fields = dispatch_mock.call_args.args[3]
        self.assertEqual(fields["tvos_runner_pool"], "private")

    @patch("release_all_platforms.time.sleep")
    @patch("release_all_platforms.command")
    @patch("release_all_platforms.gh_json")
    @patch("release_all_platforms.subprocess.Popen")
    @patch("release_all_platforms.matching_run", return_value={"databaseId": 7, "status": "in_progress"})
    def test_candidate_keeps_unassigned_hosted_tvos_queued_after_other_builds_pass(
        self, _matching, popen, gh_json, command, _sleep,
    ):
        watcher = popen.return_value
        watcher.poll.side_effect = [None, None, 0]
        watcher.wait.return_value = 0
        queued = {"id": 42, "name": "Build tvOS developer package (arm64)", "status": "queued",
                  "labels": ["macos-26"], "runner_id": 0, "runner_name": "", "steps": [],
                  "created_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()}
        passed = {"name": "Build Windows (arm64)", "status": "completed", "conclusion": "success"}
        overview = {"id": 7, "status": "in_progress", "event": "workflow_dispatch", "head_branch": "main",
                    "head_repository": {"full_name": "snapetech-org/pleazyng"}, "path": ".github/workflows/build.yml",
                    "display_title": "action:build:pool:public:mode:release:version:3.0.34"}
        def read(*args):
            if args[0] == "run":
                return {"databaseId": 7, "status": "completed", "conclusion": "success",
                        "jobs": [passed, {**queued, "status": "completed", "conclusion": "success"}]}
            if "/jobs?" in args[-1]:
                return {"jobs": [passed, queued], "total_count": 2}
            return overview
        gh_json.side_effect = read

        result = ensure_candidate_build("3.0.34", "a" * 40)

        self.assertEqual(result["conclusion"], "success")
        command.assert_not_called()

    @patch("release_all_platforms.apple_queue.active_run_scoped_apple_runner")
    def test_explicit_route_override_skips_private_capacity_probe(self, private_busy) -> None:
        self.assertEqual(select_apple_runner_pool("hosted"), "hosted")
        private_busy.assert_not_called()


class PreflightCIFastFailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run = {
            "id": 7,
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_repository": {"full_name": "snapetech-org/pleazyng"},
            "path": ".github/workflows/ci.yml@refs/heads/main",
            "display_title": "action:ci:pool:public:apple:hosted",
        }

    def test_only_trusted_main_preflight_ci_is_fast_fail_cancellable(self) -> None:
        self.assertTrue(trusted_preflight_ci_run(self.run, "snapetech-org/pleazyng"))
        for changes in (
            {"event": "pull_request"},
            {"head_branch": "feature/untrusted"},
            {"head_repository": {"full_name": "other/repository"}},
            {"path": ".github/workflows/build.yml@refs/heads/main"},
            {"display_title": "action:release:pool:public:tag:v3.0.0"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(trusted_preflight_ci_run({**self.run, **changes}, "snapetech-org/pleazyng"))

    def test_requires_a_completed_failing_job(self) -> None:
        active = {"name": "Apple native reliability", "status": "in_progress"}
        passed = {"name": "Code Analysis", "status": "completed", "conclusion": "success"}
        failed = {"name": "Unit Tests", "status": "completed", "conclusion": "failure"}
        self.assertFalse(failed_preflight_ci_run(self.run, [active, passed]))
        self.assertTrue(failed_preflight_ci_run(self.run, [active, passed, failed]))
        self.assertFalse(failed_preflight_ci_run(
            {**self.run, "path": ".github/workflows/build.yml"}, [failed]
        ))
        unassigned = {**failed, "labels": ["macos-26"], "steps": [], "runner_id": 0}
        self.assertFalse(failed_preflight_ci_run(self.run, [active, passed, unassigned]))
        self.assertTrue(failed_preflight_ci_run(self.run, [active, unassigned, failed]))

    @patch("release_all_platforms.time.sleep")
    @patch("release_all_platforms.time.monotonic", return_value=0.0)
    @patch("release_all_platforms.command")
    @patch("release_all_platforms.gh_json")
    @patch("release_all_platforms.subprocess.Popen")
    def test_cancels_failed_preflight_while_private_apple_job_is_active(
        self, popen, gh_json, command, _monotonic, _sleep,
    ) -> None:
        apple_controller = Mock()
        apple_controller.poll.return_value = None
        apple_controller.wait.return_value = 1
        watcher = Mock()
        watcher.poll.side_effect = [None, 0]
        watcher.wait.return_value = 1
        popen.side_effect = [apple_controller, watcher]
        jobs = [
            {"name": "Unit Tests", "status": "completed", "conclusion": "failure"},
            {"name": "Apple native reliability", "status": "in_progress", "conclusion": None},
        ]
        completed_cancelled = {"id": 7, "status": "completed", "conclusion": "cancelled"}
        gh_json.side_effect = [
            self.run | {"status": "in_progress"},
            {"jobs": jobs, "total_count": len(jobs)},
            self.run | {"status": "in_progress"},
            {"jobs": jobs, "total_count": len(jobs)},
            completed_cancelled,
            {"databaseId": 7, "status": "completed", "conclusion": "cancelled", "jobs": []},
        ]

        with tempfile.TemporaryDirectory() as evidence:
            with patch("release_all_platforms.APPLE_JIT_EVIDENCE_ROOT", Path(evidence)):
                result = wait_for_run(
                    "snapetech-org/pleazyng",
                    {"databaseId": 7},
                    "Pleazyng CI",
                    private_apple=True,
                    allow_failure=True,
                    cancel_failed_preflight=True,
                )

        self.assertEqual(result["conclusion"], "cancelled")
        command.assert_called_once_with(["gh", "run", "cancel", "7", "--repo", "snapetech-org/pleazyng"])
        apple_controller.wait.assert_called_once()

class HostedRecoveryWatchTests(unittest.TestCase):
    repo = "snapetech-org/pleazyng"

    def setUp(self):
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        self.failed = {"id": 7, "status": "completed", "conclusion": "failure", "run_attempt": 2,
                       "path": ".github/workflows/build.yml", "event": "workflow_dispatch",
                       "head_repository": {"full_name": self.repo}, "head_branch": "main", "updated_at": stamp}
        self.queued = {**self.failed, "status": "queued", "conclusion": None, "run_attempt": 3}
        self.summary = {"databaseId": 7, "status": "completed", "conclusion": "failure",
                        "event": "workflow_dispatch", "headBranch": "main", "attempt": 2, "updatedAt": stamp}
        self.classify = self.enterContext(patch("release_all_platforms.hosted_capacity.classify_attempt",
                                                return_value=(["tvOS"], "")))
        self.recover = self.enterContext(patch("release_all_platforms.hosted_capacity.recover",
                                               return_value="Requested retry for tvOS"))
        self.gh = self.enterContext(patch("release_all_platforms.gh_json"))
        self.sleep = self.enterContext(patch("release_all_platforms.time.sleep"))

    def test_watcher_follows_confirmed_retry_and_reattaches_signing_controller(self):
        final = {**self.summary, "conclusion": "success", "attempt": 3}
        self.gh.side_effect = [self.failed, self.failed, self.queued, final]
        signing = Mock()
        signing.wait.return_value = 0
        first, second = Mock(), Mock()
        first.wait.return_value, second.wait.return_value = 1, 0
        with (tempfile.TemporaryDirectory() as evidence,
              patch("release_all_platforms.APPLE_JIT_EVIDENCE_ROOT", Path(evidence)),
              patch("release_all_platforms.subprocess.Popen",
                    side_effect=[signing, first, signing, second]) as popen):
            result = wait_for_run(self.repo, self.summary, "Signed release", allow_failure=True)
        self.assertEqual(result, final)
        self.assertEqual(popen.call_args_list[0], popen.call_args_list[2])
        self.assertEqual(popen.call_args_list[1], popen.call_args_list[3])
        self.assertEqual(signing.wait.call_count, 2)
        self.classify.assert_called_once_with(self.repo, 7, 2)
        self.recover.assert_not_called()
        self.sleep.assert_called_once()

    def test_resume_between_attempts_follows_same_run(self):
        self.gh.side_effect = [self.failed, self.queued]
        self.assertEqual(resume_hosted_retry(self.summary),
                         {**self.summary, "status": "queued", "conclusion": None, "attempt": 3})
        self.recover.assert_not_called()

    def test_delayed_recovery_workflow_gets_one_guarded_local_request(self):
        self.gh.side_effect = [self.failed, self.failed, self.failed, self.queued]
        with patch("release_all_platforms.time.monotonic", side_effect=[0, 65, 80, 95]):
            self.assertEqual(await_hosted_retry(self.repo, 7, self.failed), self.queued)
        self.recover.assert_called_once_with(self.repo, 7, 2, retry=True)

    def test_watcher_returns_when_an_exact_supplemental_qualification_owns_recovery(self):
        with patch("release_all_platforms.hosted_capacity.has_supplemental_apple_recovery",
                   return_value=True) as supplemental:
            self.assertIsNone(await_hosted_retry(self.repo, 7, self.failed))
        supplemental.assert_called_once_with(self.repo, self.failed)
        self.recover.assert_not_called()
        self.sleep.assert_not_called()

    def test_lost_response_does_not_issue_a_second_local_request(self):
        self.failed["updated_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)).isoformat()
        self.recover.side_effect = subprocess.CalledProcessError(1, "gh")
        self.gh.side_effect = [self.failed, self.failed, self.queued]
        self.assertEqual(await_hosted_retry(self.repo, 7, self.failed), self.queued)
        self.recover.assert_called_once_with(self.repo, 7, 2, retry=True)

    def test_genuine_failure_exhaustion_and_cancellation_return_immediately(self):
        self.classify.return_value = [], "Requires investigation"
        for changes in ({}, {"run_attempt": 8}, {"conclusion": "cancelled"}, {"head_branch": "feature"}):
            with self.subTest(changes=changes):
                self.assertIsNone(await_hosted_retry(self.repo, 7, {**self.failed, **changes}))
        self.classify.assert_called_once()
        self.recover.assert_not_called()
        self.gh.assert_not_called()
        self.sleep.assert_not_called()

    def test_confirmed_failure_has_bounded_wait_when_recovery_never_starts(self):
        self.gh.return_value = self.failed
        with patch("release_all_platforms.time.monotonic", side_effect=[0, 1000]):
            self.assertIsNone(await_hosted_retry(self.repo, 7, self.failed))
        self.sleep.assert_not_called()


class ReleaseCIGateManifestTests(unittest.TestCase):
    def test_reads_release_gate_from_capability_manifest(self) -> None:
        manifest = json.loads(
            (ROOT / "docs/maintainers/capability-manifest.json").read_text(encoding="utf-8")
        )
        release = manifest["release"]
        gate = _manifest_gate()

        self.assertEqual(gate["workflowId"], "ci.yml")
        self.assertEqual(gate["requiredJobs"], release["requiredCiJobs"])

    @patch("verify_release_ci._request_json")
    def test_manifest_read_is_pinned_to_the_requested_commit(self, request_json) -> None:
        manifest = {"release": {"version": "3.0.31"}}
        payload = base64.b64encode(json.dumps(manifest).encode("utf-8")).decode("ascii")
        request_json.return_value = {"encoding": "base64", "content": payload}
        sha = "a" * 40

        self.assertEqual(release_ci._manifest_for_commit(
            "fixture", "snapetech-org/pleazyng", sha,
        ), manifest)
        self.assertIn(
            f"/contents/docs/maintainers/capability-manifest.json?ref={sha}",
            request_json.call_args.args[0],
        )

    def test_standalone_gate_uses_the_manifest_at_the_exact_release_sha(self) -> None:
        required = [f"Apple native reliability ({platform})" for platform in ("iOS", "macOS", "tvOS")]
        manifest = {"release": {"requiredCiJobs": required, "releaseGate": {
            "branch": "main", "event": "workflow_dispatch", "workflowId": "ci.yml",
            "requiredJobs": required,
        }}}
        sha = "a" * 40
        with patch.dict("os.environ", {
            "GH_TOKEN": "fixture", "REPOSITORY": "snapetech-org/pleazyng", "COMMIT_SHA": sha,
        }), patch.object(release_ci, "_manifest_for_commit", return_value=manifest) as read_manifest, \
             patch.object(release_ci, "_latest_run", return_value={
                 "status": "completed", "conclusion": "success",
             }), patch.object(release_ci, "_check_jobs", return_value=[]) as check_jobs:
            self.assertEqual(release_ci.main(), 0)

        read_manifest.assert_called_once_with("fixture", "snapetech-org/pleazyng", sha)
        self.assertEqual(check_jobs.call_args.args[3]["requiredJobs"], required)

    @patch("release_all_platforms.dispatch")
    @patch("release_all_platforms.matching_run", return_value=None)
    def test_release_ci_dispatch_includes_linux_package_smoke(self, _matching_run, dispatch) -> None:
        commit_sha = "a" * 40
        ensure_ci(commit_sha)

        dispatch.assert_called_once_with(
            "snapetech-org/pleazyng",
            "ci.yml",
            "main",
            {
                "action": "ci",
                "runner_pool": "public",
                "execution_profile": "hybrid",
                "build_linux_packages": "true",
                "apple_runner_pool": "hosted",
            },
            "Pleazyng CI",
            expected_title="action:ci:pool:public:apple:hosted",
            expected_sha=commit_sha,
        )

    @patch("release_all_platforms.dispatch")
    @patch("release_all_platforms.matching_run", return_value=None)
    def test_release_ci_dispatch_uses_hybrid_profile_when_requested(
        self, _matching_run, dispatch
    ) -> None:
        commit_sha = "c" * 40
        ensure_ci(commit_sha, execution_profile="hybrid")

        dispatch.assert_called_once_with(
            "snapetech-org/pleazyng",
            "ci.yml",
            "main",
            {
                "action": "ci",
                "runner_pool": "public",
                "execution_profile": "hybrid",
                "build_linux_packages": "true",
                "apple_runner_pool": "hosted",
            },
            "Pleazyng CI",
            expected_title="action:ci:pool:public:apple:hosted",
            expected_sha=commit_sha,
        )

    @patch("release_all_platforms.dispatch")
    @patch("release_all_platforms.matching_run", return_value=None)
    def test_release_ci_dispatch_uses_public_hosted_apple(
        self, _matching_run, dispatch
    ) -> None:
        commit_sha = "e" * 40
        ensure_ci(commit_sha, apple_runner_pool="hosted")

        self.assertEqual(dispatch.call_args.args[3]["apple_runner_pool"], "hosted")

    @patch("release_all_platforms.dispatch")
    @patch("release_all_platforms.matching_run", return_value=None)
    def test_release_ci_always_uses_public_hosted_apple(self, _matching_run, dispatch) -> None:
        ensure_ci("f" * 40, apple_runner_pool="private", ios_runner_pool="private")

        fields = dispatch.call_args.args[3]
        self.assertEqual(fields["runner_pool"], "public")
        self.assertEqual(fields["apple_runner_pool"], "hosted")
        self.assertEqual(dispatch.call_args.kwargs["expected_title"],
                         "action:ci:pool:public:apple:hosted")

    @patch(
        "release_all_platforms.gh_json",
        return_value={
            "jobs": [
                {
                    "name": "Windows native reliability (arm64)",
                    "status": "completed",
                    "conclusion": "skipped",
                }
            ]
        },
    )
    @patch("release_all_platforms.dispatch")
    @patch(
        "release_all_platforms.matching_run",
        return_value={"databaseId": 42, "status": "completed", "conclusion": "success"},
    )
    def test_hybrid_ci_does_not_reuse_private_run_without_arm64(
        self, _matching_run, dispatch, _gh_json
    ) -> None:
        commit_sha = "d" * 40
        ensure_ci(commit_sha, execution_profile="hybrid")

        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[3]["execution_profile"], "hybrid")


class ReleaseCandidateBuildGateTests(unittest.TestCase):
    def test_signed_apple_step_must_succeed_before_reusing_a_candidate(self) -> None:
        run = {
            "jobs": [
                {
                    "name": "build-ios",
                    "steps": [
                        {"name": "Build signed iOS App Store IPA", "status": "completed", "conclusion": "success"},
                        {"name": "Attest signed iOS App Store IPA", "status": "completed", "conclusion": "skipped"},
                    ],
                }
            ]
        }

        self.assertTrue(
            run_has_successful_step("snapetech-org/pleazyng", run, "build-ios", "Build signed iOS App Store IPA")
        )
        self.assertFalse(
            run_has_successful_step("snapetech-org/pleazyng", run, "build-ios", "Attest signed iOS App Store IPA")
        )

    @patch("release_all_platforms.ensure_remote_main_sha")
    @patch("release_all_platforms.dispatch", return_value={
        "databaseId": 7, "status": "completed", "conclusion": "success",
        "jobs": [{"name": "Build Windows (arm64)", "status": "completed", "conclusion": "success"}],
    })
    @patch("release_all_platforms.active_run", return_value=None)
    @patch("release_all_platforms.matching_run", return_value=None)
    def test_dispatches_full_nonpublishing_release_mode_on_exact_main_sha(
        self, _matching_run, _active_run, dispatch, ensure_main
    ) -> None:
        commit_sha = "b" * 40
        ensure_candidate_build("3.0.0", commit_sha)

        ensure_main.assert_called_once_with(commit_sha, "dispatching the 3.0.0 release-mode candidate")
        dispatch.assert_called_once_with(
            "snapetech-org/pleazyng",
            "build.yml",
            "main",
            {
                "action": "build",
                "runner_pool": "public",
                "execution_profile": "hybrid",
                "mode": "release",
                "build_version": "3.0.0",
                "include_webos_build": "true",
                "include_webos_release": "true",
                "include_apple_store_build": "false",
                "private_apple_signing_unavailable": "false",
                "tvos_runner_pool": "hosted",
                "subtitle_gpu_backend": "cpu",
                "prepare_homebrew_cask": "false",
            },
            "Pleazyng 3.0.0 all-platform release-mode candidate",
            expected_title="action:build:pool:public:mode:release:version:3.0.0",
            expected_sha=commit_sha,
            allow_failure=True,
        )

    @patch("release_all_platforms.ensure_remote_main_sha")
    @patch("release_all_platforms.dispatch", return_value={
        "databaseId": 8, "status": "completed", "conclusion": "success",
        "jobs": [
            {"name": "Build Windows (arm64)", "status": "completed", "conclusion": "success"},
            {"name": "build-ios", "steps": [{"name": "Build signed iOS App Store IPA", "status": "completed", "conclusion": "success"}]},
            {"name": "Build tvOS developer package (arm64)", "steps": [{"name": "Build signed tvOS App Store IPA", "status": "completed", "conclusion": "success"}]},
        ],
    })
    @patch("release_all_platforms.active_run", return_value=None)
    @patch("release_all_platforms.matching_run", return_value=None)
    def test_signed_apple_candidate_keeps_public_pool_title(
        self, _matching_run, _active_run, dispatch, _ensure_main
    ) -> None:
        ensure_candidate_build("3.0.0", "e" * 40, include_apple_store_build=True)

        fields = dispatch.call_args.args[3]
        self.assertEqual(fields["include_apple_store_build"], "true")
        self.assertEqual(
            dispatch.call_args.kwargs["expected_title"],
            "action:build:pool:public:mode:release:version:3.0.0",
        )

    @patch("release_all_platforms.run_has_successful_job", return_value=True)
    @patch("release_all_platforms.matching_run")
    def test_reuses_only_a_successful_exact_version_candidate(
        self, matching_run, _arm64_job
    ) -> None:
        successful = {"databaseId": 123, "status": "completed", "conclusion": "success"}
        matching_run.return_value = successful

        self.assertIs(ensure_candidate_build("3.0.0", "b" * 40), successful)
        matching_run.assert_called_once_with(
            "snapetech-org/pleazyng",
            "build.yml",
            "b" * 40,
            ref="main",
            expected_title=(
                "action:build:pool:public:mode:release:version:3.0.0",
                "action:build:pool:private:mode:release:version:3.0.0",
            ),
        )

    @patch("release_all_platforms.verify_unacquired_hosted_tvos_run")
    @patch("release_all_platforms.matching_run")
    def test_failed_candidate_does_not_attempt_a_private_retry(self, matching_run, verify) -> None:
        matching_run.return_value = {"databaseId": 124, "status": "completed", "conclusion": "failure"}

        with self.assertRaisesRegex(ReleaseError, "hosted failures never reroute to private JIT automatically"):
            ensure_candidate_build("3.0.0", "b" * 40)
        verify.assert_not_called()

    @patch("release_all_platforms.verify_unacquired_hosted_tvos_run")
    @patch("release_all_platforms.dispatch")
    @patch("release_all_platforms.matching_run", return_value={
        "databaseId": 124, "status": "completed", "conclusion": "failure",
    })
    def test_failed_hosted_candidate_does_not_retry_privately(
        self, _matching_run, dispatch, verify
    ) -> None:
        with self.assertRaisesRegex(ReleaseError, "hosted failures never reroute to private JIT automatically"):
            ensure_candidate_build("3.0.0", "b" * 40)

        dispatch.assert_not_called()
        verify.assert_not_called()

    @patch("release_all_platforms.verify_unacquired_hosted_tvos_run")
    @patch("release_all_platforms.dispatch")
    @patch("release_all_platforms.matching_run")
    @patch("release_all_platforms.active_run")
    @patch("release_all_platforms.ensure_remote_main_sha")
    @patch("release_all_platforms.wait_for_run", return_value={
        "databaseId": 124, "status": "completed", "conclusion": "failure",
    })
    def test_active_failed_candidate_does_not_retry_privately(
        self, wait, _ensure_main, _active, matching_run, dispatch, verify,
    ) -> None:
        matching_run.return_value = {
            "databaseId": 124, "status": "in_progress", "conclusion": None,
        }

        with self.assertRaisesRegex(ReleaseError, "hosted failures never reroute to private JIT automatically"):
            ensure_candidate_build("3.0.0", "b" * 40)

        wait.assert_called_once_with(
            "snapetech-org/pleazyng", {"databaseId": 124, "status": "in_progress", "conclusion": None},
            "Pleazyng 3.0.0 all-platform release-mode candidate", private_apple=False,
            allow_failure=True,
        )
        dispatch.assert_not_called()
        verify.assert_not_called()

    @patch("release_all_platforms.verify_unacquired_hosted_tvos_run")
    @patch("release_all_platforms.gh_json", return_value={
        "databaseId": 43826939425,
        "status": "completed",
        "conclusion": "failure",
        "jobs": [{
            "name": "build-macos",
            "status": "completed",
            "conclusion": "timed_out",
        }],
    })
    @patch("release_all_platforms.matching_run")
    def test_failed_completed_release_reports_the_actual_failed_job_without_retry(
        self, matching_run, gh_json, verify,
    ) -> None:
        matching_run.return_value = {
            "databaseId": 43826939425,
            "status": "completed",
            "conclusion": "failure",
        }

        with self.assertRaisesRegex(
            ReleaseError,
            r"Release build run 43826939425 concluded failure; failed job\(s\): build-macos \(timed_out\)",
        ):
            ensure_release("v3.0.36", "c" * 40, include_webos_release=True)

        gh_json.assert_called_once_with(
            "run", "view", "43826939425", "--repo", "snapetech-org/pleazyng",
            "--json", "databaseId,status,conclusion,jobs",
        )
        verify.assert_not_called()

    @patch("release_all_platforms.verify_unacquired_hosted_tvos_run")
    @patch("release_all_platforms.matching_run", return_value={
        "databaseId": 43826939426,
        "status": "completed",
        "conclusion": "cancelled",
        "jobs": [{
            "name": "Build tvOS developer package (arm64)",
            "status": "completed",
            "conclusion": "cancelled",
        }],
    })
    def test_cancelled_completed_release_reports_the_cancelled_job(
        self, _matching_run, verify,
    ) -> None:
        with self.assertRaisesRegex(
            ReleaseError,
            r"Release build run 43826939426 concluded cancelled; failed job\(s\): "
            r"Build tvOS developer package \(arm64\) \(cancelled\)",
        ):
            ensure_release("v3.0.36", "c" * 40, include_webos_release=True)

        verify.assert_not_called()

    @patch("release_all_platforms.verify_unacquired_hosted_tvos_run",
           side_effect=RunnerAcquisitionError("tvOS was not proven canceled before hosted runner acquisition"))
    @patch("release_all_platforms.matching_run")
    def test_permitted_retry_still_requires_exact_unassigned_tvos_evidence(
        self, matching_run, verify,
    ) -> None:
        matching_run.return_value = {
            "databaseId": 43826939425,
            "status": "completed",
            "conclusion": "failure",
            "jobs": [{
                "name": "build-macos",
                "status": "completed",
                "conclusion": "timed_out",
            }],
        }

        with self.assertRaisesRegex(
            ReleaseError,
            r"build-macos \(timed_out\).*does not prove a tvOS runner-acquisition cancellation.*no private recovery was started",
        ):
            ensure_release(
                "v3.0.36", "c" * 40, include_webos_release=True,
                allow_private_apple_retry=True,
            )

        verify.assert_called_once_with(
            43826939425, "c" * 40, "3.0.36", action="release",
        )

    @patch("release_all_platforms.ensure_remote_main_sha")
    @patch("release_all_platforms.dispatch", return_value={
        "databaseId": 48, "status": "completed", "conclusion": "success",
    })
    @patch("release_all_platforms.verify_platform_recovery", return_value={"source_run_id": 47})
    @patch("release_all_platforms.verify_unacquired_hosted_tvos_run", return_value={"run_id": 47})
    @patch("release_all_platforms.matching_run")
    def test_private_recovery_requires_policy_and_verified_unassigned_tvos(
        self, matching_run, verify_tvos, verify_recovery, dispatch, _ensure_main,
    ) -> None:
        matching_run.side_effect = [
            {
                "databaseId": 47,
                "status": "completed",
                "conclusion": "cancelled",
                "jobs": [{
                    "name": "Build tvOS developer package (arm64)",
                    "status": "completed",
                    "conclusion": "cancelled",
                }],
            },
            None,
        ]

        result = ensure_release(
            "v3.0.36", "c" * 40, include_webos_release=True,
            allow_private_apple_retry=True,
        )

        self.assertEqual(result["databaseId"], 48)
        verify_tvos.assert_called_once_with(47, "c" * 40, "3.0.36", action="release")
        verify_recovery.assert_called_once_with(
            47, "c" * 40, "3.0.36", rebuild_tvos=True,
            webos_run=47, android_run=47,
        )
        fields = dispatch.call_args.args[3]
        self.assertEqual(fields["tvos_runner_pool"], "private")
        self.assertEqual(fields["reuse_platform_run_id"], "47")
        self.assertEqual(fields["reuse_webos_run_id"], "47")


class ImmutableReleaseTagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="pleazyng-immutable-tag-")
        self.root = Path(self.tempdir.name)
        self.remote = self.root / "remote.git"
        self.checkout = self.root / "checkout"
        self.git_config = {
            "GIT_AUTHOR_NAME": "Release test",
            "GIT_AUTHOR_EMAIL": "release-test@example.invalid",
            "GIT_COMMITTER_NAME": "Release test",
            "GIT_COMMITTER_EMAIL": "release-test@example.invalid",
        }
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.remote), str(self.checkout)], check=True, capture_output=True)
        self.git("checkout", "-b", "main")
        self.checkout.joinpath("lib").mkdir()
        self.checkout.joinpath("lib/main.dart").write_text("void main() {}\n", encoding="utf-8")
        self.git("add", "lib/main.dart")
        self.git("commit", "-m", "initial source")
        self.git("tag", "-a", "v3.0.0", "-m", "Pleazyng 3.0.0")
        self.git("push", "-u", "origin", "main", "--tags")
        self.tag_commit = self.git("rev-parse", "refs/tags/v3.0.0^{commit}")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.checkout,
            env={**os.environ, **getattr(self, "git_config", {})},
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip()

    def commit_file(self, relative_path: str, contents: str) -> str:
        path = self.checkout / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        self.git("add", relative_path)
        self.git("commit", "-m", f"update {relative_path}")
        return self.git("rev-parse", "HEAD")

    def test_reuses_immutable_tag_after_allowlisted_release_changes(self) -> None:
        self.commit_file("CHANGELOG.md", "## [Unreleased]\n\n- Release infrastructure update.\n")
        main_commit = self.commit_file(".github/workflows/build.yml", "name: Pleazyng Release\n")

        self.assertEqual(ensure_tag(self.checkout, "3.0.0", main_commit), "v3.0.0")
        self.assertEqual(self.git("rev-parse", "refs/tags/v3.0.0^{commit}"), self.tag_commit)

    def test_infrastructure_reuses_only_the_validated_tag_ci_source(self) -> None:
        main_commit = self.commit_file("scripts/prepare_native_downloads.py", "# verified downloads\n")
        self.assertEqual(release_ci_source(self.checkout, "v3.0.0", main_commit, self.tag_commit), self.tag_commit)
        self.assertEqual(release_ci_source(self.checkout, "v3.0.1", main_commit, None), main_commit)
        changed = self.commit_file("lib/main.dart", "void main() { print('changed'); }\n")
        with self.assertRaisesRegex(ReleaseError, "non-release source files"):
            release_ci_source(self.checkout, "v3.0.0", changed, self.tag_commit)

    def test_apple_workspace_staging_helper_is_release_infrastructure(self) -> None:
        main_commit = self.commit_file(
            "scripts/prepare_apple_jit_scratch.sh",
            "#!/usr/bin/env bash\n# Stage release-only Apple workspaces.\n",
        )

        self.assertEqual(
            release_ci_source(self.checkout, "v3.0.0", main_commit, self.tag_commit),
            self.tag_commit,
        )

    def test_workflow_ci_uses_the_validated_immutable_tag(self) -> None:
        import yaml
        workflow = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
        steps = workflow["jobs"]["validate-trusted-ref"]["steps"]
        gate = next(step for step in steps if step.get("name") == "Require successful CI for this commit")
        self.assertEqual(gate["env"]["COMMIT_SHA"], "${{ steps.release-tag.outputs.commit }}")
        tag = next(i for i, step in enumerate(steps) if step.get("id") == "release-tag")
        self.assertLess(tag, steps.index(gate))

    def test_standalone_ci_only_exports_verified_commit(self) -> None:
        import yaml
        jobs = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())["jobs"]
        validator = jobs["validate-trusted-ref"]
        gate = next(step for step in validator["steps"] if step.get("id") == "standalone-ci")
        self.assertEqual(validator["outputs"]["qualified_tag_commit"], "${{ steps.standalone-ci.outputs.qualified_tag_commit }}")
        for status in (0, 1):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                verifier = directory / "scripts/verify_release_ci.py"
                verifier.parent.mkdir()
                verifier.write_text(f"raise SystemExit({status})\n")
                output = directory / "outputs"
                output.touch()
                result = subprocess.run(
                    ["bash", "-c", gate["run"]], cwd=directory, capture_output=True,
                    env={**os.environ, "COMMIT_SHA": self.tag_commit, "GITHUB_OUTPUT": str(output)},
                )
                self.assertEqual(result.returncode, status)
                expected = f"qualified_tag_commit={self.tag_commit}\n" if status == 0 else ""
                self.assertEqual(output.read_text(), expected)
        condition = jobs["create-release"]["if"]
        self.assertIn(
            "(inputs.candidate_ref == '' && inputs.reuse_platform_run_id == '' && "
            "needs.freeze-qualified-source.result == 'skipped' && "
            "needs.validate-trusted-ref.outputs.qualified_tag_commit != '' && "
            "needs.validate-trusted-ref.outputs.qualified_tag_commit == needs.validate-trusted-ref.outputs.release_commit)",
            condition,
        )

    def test_rejects_application_changes_without_moving_the_tag(self) -> None:
        self.commit_file("CHANGELOG.md", "## [Unreleased]\n\n- Release infrastructure update.\n")
        main_commit = self.commit_file("lib/main.dart", "void main() { print('changed'); }\n")

        with self.assertRaisesRegex(ReleaseError, "non-release source files: lib/main.dart"):
            ensure_tag(self.checkout, "3.0.0", main_commit)
        self.assertEqual(self.git("rev-parse", "refs/tags/v3.0.0^{commit}"), self.tag_commit)


if __name__ == "__main__":
    unittest.main()
