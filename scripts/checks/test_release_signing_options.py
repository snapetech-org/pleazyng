#!/usr/bin/env python3
"""Independent signing qualification must not enable store publication."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_all_platforms as release


class SigningOptionsTests(unittest.TestCase):
    def test_android_store_payloads_leave_updates_to_the_store(self):
        import yaml
        workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/build.yml").read_text())
        shared = workflow["env"]["RELEASE_DART_DEFINES"]
        steps = workflow["jobs"]["android-variant"]["steps"]
        commands = {step.get("name"): step for step in steps}
        channel_options = {
            "Build APKs": ("developer", "true"),
            "Build and verify Automotive audio APK": ("automotive", "true"),
            "Build signed Android App Bundle": ("play", "false"),
            "Build signed Amazon Appstore APK": ("firetv", "false"),
            "Build signed Automotive audio App Bundle": ("automotive", "false"),
        }
        for step, (variant, enabled) in channel_options.items():
            with self.subTest(build=step):
                entry = commands[step]
                self.assertIn(f"matrix.variant == '{variant}'", entry.get("if", ""))
                command = entry.get("run", "").replace("${{ env.RELEASE_DART_DEFINES }}", shared)
                flags = [arg for arg in command.split() if arg.startswith("--dart-define=ENABLE_UPDATE_CHECK=")]
                self.assertEqual(flags, [f"--dart-define=ENABLE_UPDATE_CHECK={enabled}"])

    def test_cli_defaults_and_github_only_override(self):
        for flags, expected in (([], False), (["--include-apple-store-build", "--prepare-homebrew-cask", "--publish-github-only"], True)):
            with self.subTest(flags=flags), patch.object(sys, "argv", ["release", "--version", "3.0.1", *flags]):
                args = release.parse_args()
                self.assertEqual(args.include_apple_store_build, expected)
                self.assertEqual(args.prepare_homebrew_cask, expected)
                self.assertEqual(args.macos_runner_pool, "auto")
                self.assertFalse(args.publish)

    def test_cli_allows_supplemental_qualification_with_tvos_rebuild(self):
        argv = ["release", "--version", "3.0.52", "--source-commit", "a" * 40,
                "--reuse-platform-run", "42", "--reuse-repaired-run", "qualification:84",
                "--rebuild-tvos", "--include-webos-release", "--publish"]
        with patch.object(sys, "argv", argv), patch.object(
            release, "command", side_effect=SystemExit
        ) as command, self.assertRaises(SystemExit):
            release.main()
        command.assert_called_once_with(["gh", "auth", "status", "--hostname", "github.com"],
                                        capture=True, quiet=True)

    def test_dispatch_and_reuse_require_every_requested_step(self):
        mac = [("build-macos", "Notarize Application"),
               ("build-macos", "Wait for notarization acceptance"),
               ("build-macos", "Verify Gatekeeper acceptance for Homebrew Cask")]
        cask = [("create-release", "Generate Homebrew Cask candidate"),
                ("create-release", "Attest Homebrew Cask candidate")]
        for candidate in (True, False):
            required = mac + ([] if candidate else cask)
            for missing in [None, *required]:
                with self.subTest(candidate=candidate, missing=missing), contextlib.ExitStack() as stack:
                    existing = {"databaseId": 42, "status": "completed", "conclusion": "success"}
                    dispatched = {"databaseId": 43, "status": "completed", "conclusion": "success"}
                    stack.enter_context(patch.object(release, "matching_run", return_value=existing))
                    stack.enter_context(patch.object(release, "run_has_successful_job", return_value=True))
                    step = stack.enter_context(patch.object(
                        release,
                        "run_has_successful_step",
                        side_effect=lambda repo, run, job, name: run is not existing or (job, name) != missing,
                    ))
                    stack.enter_context(patch.object(release, "active_run", return_value=None))
                    stack.enter_context(patch.object(release, "ensure_remote_main_sha"))
                    dispatch = stack.enter_context(patch.object(release, "dispatch", return_value=dispatched))
                    options = dict(include_apple_store_build=True, prepare_homebrew_cask=True)
                    if candidate:
                        result = release.ensure_candidate_build("3.0.1", "a" * 40, **options)
                    else:
                        result = release.ensure_release("v3.0.1", "a" * 40, include_webos_release=True, **options)
                    if missing is None:
                        self.assertIs(result, existing)
                        dispatch.assert_not_called()
                        self.assertEqual({tuple(call.args[2:]) for call in step.call_args_list}, set(required))
                    else:
                        fields = dispatch.call_args.args[3]
                        self.assertEqual(fields["include_apple_store_build"], "true")
                        self.assertEqual(fields["prepare_homebrew_cask"], "true")

    def test_recovery_requires_existing_homebrew_qualification(self):
        # Recovery may reuse an already qualified cask, but must request that
        # proof. Do not accidentally pass because an unrelated gh call fails.
        with patch.object(release, "verify_platform_recovery", side_effect=release.ReleaseError("missing cask proof")) as verify, \
                patch.object(release, "matching_run") as matching, \
                patch.object(release, "dispatch") as dispatch:
            with self.assertRaisesRegex(release.ReleaseError, "missing cask proof"):
                release.ensure_release("v3.0.1", "a" * 40, include_webos_release=True,
                                       reuse_platform_run_id=42, prepare_homebrew_cask=True)
            verify.assert_called_once_with(42, "a" * 40, "3.0.1", webos_run=42,
                                           android_run=42,
                                           require_homebrew_cask=True)
            matching.assert_not_called()
            dispatch.assert_not_called()

    def test_unsigned_apple_core_assets_suffice_when_store_signing_is_blocked(self):
        # Store credentials affect the optional Store channel, not completeness
        # of the unsigned iOS/tvOS assets in the core release.
        core_assets = {
            "SHA256SUMS.txt", "appcast.xml", "pleazyng-android-arm64-v8a.tar.gz",
            "pleazyng-android-armeabi-v7a.tar.gz", "pleazyng-android-x86_64.tar.gz",
            "pleazyng-ios-unsigned.ipa", "pleazyng-tvos-unsigned.ipa", "pleazyng-macos.dmg",
            "pleazyng-windows-x64-portable.7z", "pleazyng-windows-arm64-portable.zip",
            "pleazyng-windows-installer.exe", "pleazyng-linux-x64.tar.gz", "pleazyng-linux-x64.deb",
            "pleazyng-linux-x64.rpm", "pleazyng-linux-x64.pkg.tar.zst", "pleazyng-linux-x64.flatpak",
            "pleazyng-linux-x64.snap", "pleazyng-linux-arm64.tar.gz", "pleazyng-linux-arm64.deb",
            "pleazyng-linux-arm64.rpm", "pleazyng-linux-arm64.pkg.tar.zst", "pleazyng-linux-arm64.flatpak",
            "pleazyng-linux-arm64.snap", "pleazyng-linux-x86_64.AppImage",
            "pleazyng-tizen-arm-tester-handoff.zip", "pleazyng-amazon-firetv.apk",
            "pleazyng-amazon-firetv-provenance.json", "webos-SHA256SUMS.txt", "pleazyng-webos.ipk",
        }
        with patch.object(release, "release_metadata", return_value={"assets": [{"name": name} for name in core_assets]}), \
             patch.object(release, "configured_repo_names", return_value=set()), \
             patch.object(release, "configured_repo_variables", return_value={}):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                release.validate_release_outputs("v3.0.1", "3.0.1", include_webos_release=True,
                    include_apple_store_build=True, prepare_homebrew_cask=False)
            self.assertIn("Apple Store delivery remains blocked by signing readiness", output.getvalue())

    def test_homebrew_candidate_remains_required_when_requested(self):
        with patch.object(release, "release_metadata", return_value={"assets": []}), \
             patch.object(release, "missing_assets", return_value=[]), \
             patch.object(release, "configured_repo_names", return_value=set()), \
             patch.object(release, "configured_repo_variables", return_value={}):
            with self.assertRaises(release.ReleaseError) as raised:
                release.validate_release_outputs("v3.0.1", "3.0.1", include_webos_release=True,
                    include_apple_store_build=False, prepare_homebrew_cask=True)
            self.assertIn("snapetech-pleazyng.rb", str(raised.exception))

    def test_github_only_frozen_release_propagates_flags_without_store_uploads(self):
        with tempfile.TemporaryDirectory() as scratch, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", ["release", "--version", "3.0.1", "--publish-github-only",
                "--include-apple-store-build", "--prepare-homebrew-cask", "--scratch", scratch]))
            def command(args, **kwargs):
                return subprocess.CompletedProcess(args, 0, "3.0.1" if args[0] == "python3" else "a" * 40, "")
            stack.enter_context(patch.object(release, "command", side_effect=command))
            stack.enter_context(patch.object(release, "create_plezy_worktree", return_value=Path(scratch) / "plezyng-main"))
            stack.enter_context(patch.object(release, "existing_release_tag_commit", return_value="a" * 40))
            stack.enter_context(patch.object(release, "validate_immutable_release_tag"))
            stack.enter_context(patch.object(release, "ensure_remote_main_sha"))
            stack.enter_context(patch.object(release, "ensure_frozen_release_source"))
            stack.enter_context(patch.object(release, "private_apple_host_reachable", return_value=True))
            stack.enter_context(patch.object(release, "embedded_ci_proof", return_value={"mode": "embedded", "run_id": 2, "attempt": 1}))
            stack.enter_context(patch.object(release, "gh_json", return_value={"commit": {"sha": "b" * 40}}))
            stack.enter_context(patch.object(release, "refresh_latest_published_facts", return_value="b" * 40))
            stack.enter_context(patch.object(release, "dispatch_automatic_play", return_value=None))
            stack.enter_context(patch.object(release, "dispatch_automatic_amazon", return_value=None))
            stack.enter_context(patch.object(release, "dispatch_aerynos_delivery", return_value={}))
            stack.enter_context(patch.object(release, "ensure_ci", return_value={"databaseId": 1}))
            build = stack.enter_context(patch.object(release, "ensure_release", return_value={"databaseId": 2}))
            validate = stack.enter_context(patch.object(release, "validate_release_outputs"))
            publish = stack.enter_context(patch.object(release, "publish_github_release"))
            stores = stack.enter_context(patch.object(release, "publish_configured_channels"))
            secrets = stack.enter_context(patch.object(release, "configured_repo_names"))
            self.assertEqual(release.main(), 0)
            for mock in (build, validate):
                self.assertTrue(mock.call_args.kwargs["include_apple_store_build"])
                self.assertTrue(mock.call_args.kwargs["prepare_homebrew_cask"])
            publish.assert_called_once_with("v3.0.1")
            stores.assert_not_called()
            secrets.assert_not_called()
            for filename in ("release-progress.json", "release-summary.json"):
                report = json.loads((Path(scratch) / filename).read_text())
                self.assertTrue(report["include_apple_store_build"])
                self.assertTrue(report["prepare_homebrew_cask"])
                self.assertEqual(report["publication_scope"], "github-only")


if __name__ == "__main__":
    unittest.main()
