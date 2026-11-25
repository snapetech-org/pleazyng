"""Keep verified GitHub publication independent of downstream delivery."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_all_platforms as release


class CorePublicationTests(unittest.TestCase):
    def setUp(self):
        automatic = patch.object(release, 'dispatch_automatic_play', return_value=None)
        automatic.start()
        self.addCleanup(automatic.stop)
        amazon = patch.object(release, "dispatch_automatic_amazon", return_value=None)
        amazon.start()
        self.addCleanup(amazon.stop)
        aerynos = patch.object(release, 'dispatch_aerynos_delivery', return_value={})
        aerynos.start()
        self.addCleanup(aerynos.stop)
        embedded = patch.object(release, 'embedded_ci_proof',
                                return_value={'mode': 'embedded', 'run_id': 2, 'attempt': 1})
        embedded.start()
        self.addCleanup(embedded.stop)

    def test_pinned_non_frozen_release_uses_current_main_and_exact_ancestry(self):
        commit = "a" * 40
        with patch.object(release, "ensure_remote_main_sha") as remote_main, \
                patch.object(release, "ensure_main_ancestor") as ancestor:
            release.validate_selected_source(commit, frozen=False, frozen_commit=None,
                                             packaging_repair=False)
        remote_main.assert_called_once_with(commit, "starting a pinned-source release")
        ancestor.assert_called_once_with(commit)

    def test_pinned_source_cannot_disagree_with_an_existing_release_tag(self):
        with patch.object(release, "ensure_remote_main_sha") as remote_main, \
                patch.object(release, "ensure_main_ancestor"):
            with self.assertRaisesRegex(release.ReleaseError, "existing immutable version tag"):
                release.validate_selected_source("a" * 40, frozen=False,
                                                 frozen_commit="b" * 40,
                                                 packaging_repair=False)
        remote_main.assert_not_called()

    def test_channel_delivery_uses_immutable_source_after_main_advances(self):
        tag = "v3.0.1"
        title = f"action:publish-snap:pool:public:tag:{tag}:channel:stable"
        fields = {"action": "publish-snap", "runner_pool": "public",
                  "release_tag": tag, "channel": "stable"}
        product_sha = "a" * 40
        run = {"databaseId": 42, "status": "queued", "headSha": "b" * 40}
        with patch.object(release, "ensure_frozen_release_source") as frozen, \
                patch.object(release, "workflow_runs", return_value=[]), \
                patch.object(release, "dispatch", return_value=run) as dispatch:
            result = release.ensure_channel_run(
                "publish-snap.yml", title, fields, "Snap Store", release_sha=product_sha
            )

        frozen.assert_called_once_with(tag, product_sha)
        dispatch.assert_called_once_with(
            release.PLEZY_REPO, "publish-snap.yml", "main", fields, "Snap Store",
            expected_title=title,
        )
        self.assertEqual(result, run)

    def test_channel_delivery_reuses_successful_attempt_from_an_older_publisher_revision(self):
        tag = "v3.0.1"
        title = f"action:publish-snap:pool:public:tag:{tag}:channel:stable"
        run = {"databaseId": 43, "event": "workflow_dispatch", "displayTitle": title,
               "status": "completed", "conclusion": "success", "headSha": "b" * 40}
        with patch.object(release, "ensure_frozen_release_source") as frozen, \
                patch.object(release, "workflow_runs", return_value=[run]), \
                patch.object(release, "dispatch") as dispatch:
            result = release.ensure_channel_run(
                "publish-snap.yml", title,
                {"release_tag": tag}, "Snap Store", release_sha="a" * 40,
            )

        frozen.assert_called_once_with(tag, "a" * 40)
        dispatch.assert_not_called()
        self.assertEqual(result, run)

    def test_only_channel_metadata_is_optional_and_collected_when_present(self):
        workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/build.yml").read_text())
        jobs = workflow["jobs"]
        steps = {step.get("name"): step for name in ("package-windows", "windows-chocolatey")
                 for step in jobs[name]["steps"]}
        for name in ("Build installer and portables", "Attest Windows artifacts", "Upload installer"):
            self.assertFalse(steps[name].get("continue-on-error", False))
        self.assertTrue(steps["Package Chocolatey release"]["continue-on-error"])
        linux = {step.get("name"): step for step in jobs["linux-format"]["steps"]}
        for name in ("Build Flatpak bundle", "Build Snap package"):
            self.assertFalse(linux[name].get("continue-on-error", False))
        release_steps = {step.get("name"): step for step in jobs["create-release"]["steps"]}
        self.assertFalse(release_steps["Require every release platform before publication"].get("continue-on-error", False))
        self.assertTrue(release_steps["Generate WinGet manifest bundle"]["continue-on-error"])
        script = release_steps["Collect release files"]["run"]
        for metadata in (False, True):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                package = root / "artifacts/windows-chocolatey/pleazyng.3.0.1.nupkg"
                manifest = root / "artifacts/pleazyng-winget-manifests-3.0.1.zip"
                if metadata:
                    package.parent.mkdir(parents=True)
                    package.write_bytes(b"metadata")
                    manifest.write_bytes(b"metadata")
                output = root / "output"
                env = {**os.environ, "GITHUB_OUTPUT": str(output), "RELEASE_VERSION": "3.0.1",
                       "INCLUDE_WEBOS_RELEASE": "true", "EXECUTION_PROFILE": "hybrid", "PREPARE_HOMEBREW_CASK": "false"}
                subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], cwd=root, env=env, check=True, capture_output=True)
                files = output.read_text()
                self.assertEqual("windows-chocolatey" in files, metadata)
                self.assertEqual("winget-manifests" in files, metadata)
                self.assertIn("artifacts/windows-installer/pleazyng-windows-installer.exe", files)
                self.assertIn("artifacts/appcast.xml", files)
                self.assertIn("artifacts/webos/*.ipk", files)
                self.assertNotIn("artifacts/webos/pleazyng-lg-webos-submission.zip", files)
                for evidence in ("webos-app-provenance.json", "webos-engine-provenance.json", "webos-compatibility.json", "macos-compilation-recovery.json"):
                    self.assertNotIn(evidence, files)
        webos_steps = {step.get("name"): step for step in jobs['build-webos']['steps']}
        handoff = webos_steps['Prepare LG Seller Lounge handoff bundle for a paid IPK']['run']
        self.assertIn('BILLING_MODE', handoff)
        self.assertIn('this core release contains an off/tester IPK', handoff)
        self.assertIn('--webos-provenance "$OUTPUT_DIR/webos-app-provenance.json"', handoff)
        for evidence in ("webos-app-provenance.json", "webos-engine-provenance.json", "webos-compatibility.json"):
            self.assertIn('--evidence "$OUTPUT_DIR/' + evidence + '"', handoff)


    def test_notification_and_channel_failures_are_separate_results(self):
        for fail in (False, True):
            with self.subTest(exception=fail), patch.object(release, "command", side_effect=release.ReleaseError("notice offline")), \
                 patch.object(release.time, "sleep"), \
                 patch.object(release, "publish_configured_channels", side_effect=release.ReleaseError("store configuration") if fail else None,
                              return_value=([], [], ["store upload refused"])):
                result = release.post_release_actions("v3.0.1", "3.0.1", release_sha="a" * 40,
                    include_webos_release=True, publish_channels=True)
                self.assertEqual(result["notification_failures"], ["notice offline"])
                self.assertTrue(result["channel_failures"])

    def test_discord_dispatch_retries_transient_failure(self):
        attempts = []

        def dispatch(args):
            attempts.append(args)
            if len(attempts) == 1:
                raise release.ReleaseError("workflow dispatch temporarily unavailable")
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(release, "command", side_effect=dispatch), patch.object(release.time, "sleep") as sleep:
            result = release.post_release_actions("v3.0.46", "3.0.46", release_sha="a" * 40,
                include_webos_release=True, publish_channels=False)

        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(result["notification_failures"], [])
        sleep.assert_called_once_with(release.DISCORD_DISPATCH_RETRY_DELAY_SECONDS)

    def test_skipping_manual_notification_preserves_package_and_store_delivery(self):
        with patch.object(release, 'command') as command, \
                patch.object(release, 'publish_configured_channels',
                             return_value=([{'channel': 'COPR'}], [], [])) as channels:
            result = release.post_release_actions('v3.0.31', '3.0.31', release_sha='a' * 40,
                include_webos_release=True, publish_channels=True, notify_discord=False)
        command.assert_not_called()
        channels.assert_called_once()
        self.assertEqual(result['published_channels'], [{'channel': 'COPR'}])
        self.assertFalse(result['discord_notification_requested'])
        self.assertEqual(result['notification_failures'], [])

    def test_unsigned_platform_recovery_publishes_channels_from_immutable_tag_source(self):
        tag_sha = "a" * 40
        infrastructure_sha = "b" * 40
        events = []

        with tempfile.TemporaryDirectory() as scratch, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", [
                "release", "--version", "3.0.31", "--reuse-platform-run", "42",
                "--publish", "--skip-discord-notification", "--scratch", scratch,
            ]))
            stack.enter_context(patch.object(release, "validate_source_metadata"))

            def command(args, **kwargs):
                stdout = infrastructure_sha if args[:3] == ["git", "rev-parse", "HEAD"] else ""
                return subprocess.CompletedProcess(args, 0, stdout, "")

            stack.enter_context(patch.object(release, "command", side_effect=command))
            worktree = Path(scratch) / "plezyng-main"
            stack.enter_context(patch.object(release, "create_plezy_worktree", return_value=worktree))
            stack.enter_context(patch.object(release, "existing_release_tag_commit", return_value=tag_sha))
            stack.enter_context(patch.object(release, "release_ci_source", return_value="c" * 40))
            stack.enter_context(patch.object(release, "ensure_remote_main_sha"))

            def verify_recovery(*args, **kwargs):
                events.append("recovery proof")
                return {"ci_source_sha": "c" * 40, "ci_run_id": 17, "ci_mode": "standalone"}

            stack.enter_context(patch.object(
                release, "verify_platform_recovery", side_effect=verify_recovery,
            ))

            def ensure_build(*args, **kwargs):
                events.append("release build")
                return {"databaseId": 99}

            build = stack.enter_context(patch.object(release, "ensure_release", side_effect=ensure_build))
            stack.enter_context(patch.object(
                release, "validate_release_outputs", side_effect=lambda *a, **kw: events.append("asset validation"),
            ))
            stack.enter_context(patch.object(
                release, "publish_github_release", side_effect=lambda tag: events.append("GitHub release"),
            ))

            def publish_channels(*args, **kwargs):
                events.append("package channels")
                return ([{"channel": "Snap Store"}], [], [])

            channels = stack.enter_context(patch.object(
                release, "publish_configured_channels", side_effect=publish_channels,
            ))
            original_post_release = release.post_release_actions
            post_release_calls = []

            def observe_post_release(*args, **kwargs):
                post_release_calls.append((args, kwargs))
                return original_post_release(*args, **kwargs)

            stack.enter_context(patch.object(
                release, "post_release_actions", side_effect=observe_post_release,
            ))
            stack.enter_context(patch.object(release, "refresh_published_facts", return_value="d" * 40))
            stack.enter_context(patch.object(release, "release_metadata", return_value={"assets": []}))
            stack.enter_context(patch.object(release, "configured_repo_variables", return_value={}))
            stack.enter_context(patch.object(release, "microsoft_store_status", return_value="Not configured"))

            self.assertEqual(release.main(), 0)

            self.assertEqual(events, [
                "recovery proof", "release build", "asset validation", "GitHub release", "package channels",
            ])
            self.assertEqual(build.call_args.args[1], infrastructure_sha)
            self.assertEqual(build.call_args.kwargs["reuse_platform_run_id"], 42)
            self.assertFalse(build.call_args.kwargs["include_apple_store_build"])
            self.assertFalse(build.call_args.kwargs["prepare_homebrew_cask"])
            self.assertEqual(post_release_calls[0][1]["release_sha"], tag_sha)
            channels.assert_called_once_with(
                "v3.0.31", "3.0.31", release_sha=tag_sha,
                include_webos_release=True, scratch_dir=Path(scratch),
            )
            report = json.loads((Path(scratch) / "release-summary.json").read_text())
            self.assertTrue(report["release_published"])
            self.assertEqual(report["published_channels"], [{"channel": "Snap Store"}])
            self.assertEqual(report["publication_scope"], "github-and-configured-channels")

    def test_standard_platform_channel_publication_keeps_signed_recovery_gate(self):
        with patch.object(release, "validate_source_metadata"), \
                patch.object(release, "verify_platform_recovery") as verify:
            for signing_flag in ("--include-apple-store-build", "--prepare-homebrew-cask"):
                with self.subTest(signing_flag=signing_flag), patch.object(sys, "argv", [
                    "release", "--version", "3.0.31", "--reuse-platform-run", "42",
                    "--publish", signing_flag,
                ]):
                    with self.assertRaisesRegex(release.ReleaseError, "approved packaging recovery"):
                        release.main()

            verify.assert_not_called()

    def test_manual_notification_skip_is_explicit_in_release_cli(self):
        with patch.object(sys, 'argv', ['release', '--version', '3.0.31', '--skip-discord-notification']):
            self.assertTrue(release.parse_args().skip_discord_notification)

    def test_published_core_stays_successful_when_followups_fail(self):
        for publish, invalid_core in ((False, False), (True, False), (False, True)):
            with self.subTest(channels=publish, invalid_core=invalid_core), tempfile.TemporaryDirectory() as scratch, contextlib.ExitStack() as stack:
                flags = ["--publish"] if publish else ["--publish-github-only"]
                stack.enter_context(patch.object(sys, "argv", ["release", "--version", "3.0.1", *flags, "--scratch", scratch]))
                def command(args, **kwargs):
                    if args[:3] == ["gh", "workflow", "run"]:
                        progress = json.loads((Path(scratch) / "release-progress.json").read_text())
                        self.assertTrue(progress["release_published"])
                        self.assertEqual(progress["status"], "success")
                        raise release.ReleaseError("notification service unavailable")
                    return subprocess.CompletedProcess(args, 0, "3.0.1" if args[0] == "python3" else "a" * 40, "")
                stack.enter_context(patch.object(release, "command", side_effect=command))
                core = {}
                for name in ("validate_immutable_release_tag", "ensure_remote_main_sha", "ensure_frozen_release_source", "refresh_latest_published_facts", "validate_release_outputs", "publish_github_release"):
                    core[name] = stack.enter_context(patch.object(release, name))
                core["refresh_latest_published_facts"].return_value = "b" * 40
                stack.enter_context(patch.object(release, "gh_json", return_value={"commit": {"sha": "b" * 40}}))
                stack.enter_context(patch.object(release, "create_plezy_worktree", return_value=Path(scratch) / "plezyng-main"))
                stack.enter_context(patch.object(release, "existing_release_tag_commit", return_value="a" * 40))
                stack.enter_context(patch.object(release, "ensure_ci", return_value={"databaseId": 1}))
                build = stack.enter_context(patch.object(release, "ensure_release", return_value={"databaseId": 2}))
                channels = stack.enter_context(patch.object(release, "publish_configured_channels", side_effect=release.ReleaseError("store upload unavailable")))
                if invalid_core:
                    core["validate_release_outputs"].side_effect = release.ReleaseError("required platform missing")
                    with self.assertRaisesRegex(release.ReleaseError, "required platform missing"):
                        release.main()
                    core["publish_github_release"].assert_not_called()
                    channels.assert_not_called()
                    progress = json.loads((Path(scratch) / "release-progress.json").read_text())
                    self.assertEqual(progress["status"], "failed")
                    continue
                self.assertEqual(release.main(), 0)
                self.assertEqual(build.call_count, 2)
                self.assertFalse(build.call_args_list[0].kwargs.get('start_only', False))
                self.assertEqual(build.call_args.kwargs['candidate_ref'], 'v3.0.1-candidate.' + 'a' * 40)
                self.assertNotIn('source_ref', build.call_args.kwargs)
                self.assertFalse(build.call_args.kwargs["include_apple_store_build"])
                self.assertFalse(build.call_args.kwargs["prepare_homebrew_cask"])
                self.assertEqual(channels.call_count, int(publish))
                report = json.loads((Path(scratch) / "release-summary.json").read_text())
                self.assertTrue(report["release_published"])
                self.assertEqual(report['ci_mode'], 'embedded')
                self.assertEqual(report["aerynos_automation"], [{}])
                self.assertTrue(report["notification_failures"])
                self.assertEqual(bool(report["channel_failures"]), publish)
                progress = json.loads((Path(scratch) / "release-progress.json").read_text())
                self.assertEqual(progress["status"], "success")
                self.assertEqual(progress["plezyng_commit"], "a" * 40)
                self.assertEqual(progress["delivery_tooling_commit"], "b" * 40)

    def test_bad_play_configuration_does_not_stop_another_channel(self):
        settings = {"GOOGLE_PLAY_TRACK": "production", "GOOGLE_PLAY_BOOTSTRAP_COMPLETE": "true",
                    "AUR_PACKAGE_NAME": "example"}
        secrets = {"GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "AUR_SSH_KEY"}
        assets = {"pleazyng-android-play.aab", "pleazyng-linux-x64.tar.gz",
                  "pleazyng-linux-arm64.tar.gz", "SHA256SUMS.txt"}
        with patch.object(release, "release_metadata", return_value={"assets": [{"name": name} for name in assets]}), \
             patch.object(release, "configured_repo_names", return_value=secrets), \
             patch.object(release, "configured_repo_variables", return_value=settings), \
             patch.object(release, "ensure_channel_run", return_value={"databaseId": 42}) as dispatch:
            completed, _, failures = release.publish_configured_channels("v3.0.1", "3.0.1", release_sha="a" * 40,
                                                                         include_webos_release=True)
            self.assertTrue(any("Google Play configuration" in error for error in failures))
            self.assertTrue(any(item["channel"] == "AUR" for item in completed))
            self.assertTrue(all(call.args[2].get("channel") != "google-play" for call in dispatch.call_args_list))

    def test_channel_summary_omits_configured_secret_names(self):
        with patch.object(release, "release_metadata", return_value={"assets": [{"name": "SHA256SUMS.txt"}]}), \
             patch.object(release, "configured_repo_names", return_value=set()), \
             patch.object(release, "configured_repo_variables", return_value={}), \
             patch.object(release, "play_publication_settings", return_value=("internal", "draft")), \
             patch.object(release, "ensure_channel_run", return_value={"databaseId": 42, "url": "https://example.invalid/run"}):
            _, skipped, _ = release.publish_configured_channels(
                "v3.0.1", "3.0.1", release_sha="a" * 40, include_webos_release=False
            )

        summary = json.dumps(skipped)
        self.assertIn("Required credentials or variables are not configured.", summary)
        self.assertIn("optional mirror credentials are not configured", summary)
        for secret_name in (
            "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON",
            "AUR_SSH_KEY",
            "DOCKERHUB_TOKEN",
        ):
            self.assertNotIn(secret_name, summary)

    def test_chocolatey_lane_title_includes_the_default_rebuild_input(self):
        tag = "v3.0.34"
        assets = {
            "SHA256SUMS.txt", "pleazyng-windows-installer.exe",
            "pleazyng.3.0.34.nupkg",
        }
        with patch.object(release, "release_metadata", return_value={"assets": [{"name": name} for name in assets]}), \
                patch.object(release, "configured_repo_names", return_value={"CHOCO_API_KEY"}), \
                patch.object(release, "configured_repo_variables", return_value={}), \
                patch.object(release, "ensure_channel_run", return_value={"databaseId": 44}) as dispatch:
            release.publish_configured_channels(
                tag, "3.0.34", release_sha="a" * 40, include_webos_release=False
            )

        chocolatey = next(call for call in dispatch.call_args_list if call.args[3] == "Chocolatey")
        self.assertEqual(
            chocolatey.args[1],
            "action:publish-chocolatey:pool:public:tag:v3.0.34:publish:true:rebuild:false",
        )
        self.assertEqual(chocolatey.args[2]["rebuild_package"], "false")

    def test_tooling_lookup_and_unexpected_followup_failure_preserve_published_core(self):
        for failure in ('lookup', 'followup'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as scratch, contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(sys, 'argv', ['release', '--version', '3.0.30',
                    '--publish-github-only', '--scratch', scratch]))
                stack.enter_context(patch.object(release, 'command', side_effect=lambda args, **kwargs:
                    subprocess.CompletedProcess(args, 0, '3.0.30' if args[0] == 'python3' else 'a' * 40, '')))
                for name in ('ensure_frozen_release_source', 'validate_release_outputs', 'publish_github_release'):
                    stack.enter_context(patch.object(release, name))
                stack.enter_context(patch.object(release, 'create_plezy_worktree', return_value=Path(scratch) / 'product'))
                stack.enter_context(patch.object(release, 'existing_release_tag_commit', return_value='a' * 40))
                stack.enter_context(patch.object(release, 'ensure_ci', return_value={'databaseId': 1}))
                build = stack.enter_context(patch.object(release, 'ensure_release', return_value={'databaseId': 2}))
                stack.enter_context(patch.object(release, 'refresh_latest_published_facts', return_value='b' * 40))
                stack.enter_context(patch.object(release, 'gh_json',
                    side_effect=release.ReleaseError('main temporarily unavailable') if failure == 'lookup' else None,
                    return_value={'commit': {'sha': 'b' * 40}}))
                stack.enter_context(patch.object(release, 'post_release_actions',
                    side_effect=release.ReleaseError('unexpected delivery failure')))
                self.assertEqual(release.main(), 0)
                report = json.loads((Path(scratch) / 'release-summary.json').read_text())
                progress = json.loads((Path(scratch) / 'release-progress.json').read_text())
                self.assertTrue(report['release_published'])
                self.assertEqual(progress['status'], 'success')
                self.assertIn('Post-publication delivery:', report['channel_failures'][0])
                self.assertEqual(build.call_count, 2)


if __name__ == "__main__":
    unittest.main()
