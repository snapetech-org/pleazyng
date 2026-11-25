#!/usr/bin/env python3
"""Regression checks for retained Apple app-in-use capture routing."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/apple-store-screenshots.yml").read_text(encoding="utf-8")


def job_text(name: str, next_name: str | None) -> str:
    start = WORKFLOW.index(f"  {name}:\n", WORKFLOW.index("jobs:\n"))
    end = WORKFLOW.index(f"  {next_name}:\n", start + 1) if next_name else len(WORKFLOW)
    return WORKFLOW[start:end]


def named_step_condition(job: str, name: str) -> str:
    lines = job.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"- name: {name}":
            continue
        for following in lines[index + 1 :]:
            if following.startswith("      - "):
                break
            match = re.match(r"\s+if: (.*)", following)
            if match is not None:
                return match.group(1)
        raise AssertionError(f"Missing workflow step condition: {name}")
    raise AssertionError(f"Missing workflow step: {name}")


def named_step_text(job: str, name: str) -> str:
    lines = job.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"- name: {name}":
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("      - "):
            end += 1
        return "\n".join(lines[index:end])
    raise AssertionError(f"Missing workflow step: {name}")


class AppleStoreCaptureWorkflowRoutingTests(unittest.TestCase):
    def test_platform_jobs_have_independent_non_cancelling_concurrency_groups(self) -> None:
        self.assertNotIn("\nconcurrency:\n", WORKFLOW.split("jobs:\n", 1)[0])
        next_names = {"ios": "tvos", "tvos": "macos", "macos": "package", "package": None}
        for platform, next_name in next_names.items():
            job = job_text(platform, next_name)
            self.assertRegex(
                job,
                rf"(?m)^    concurrency:\n      group: pleazyng-apple-store-screenshots-{platform}\n      cancel-in-progress: false$",
            )

    def test_build_receipt_follows_the_restored_or_fresh_app_bundle(self) -> None:
        for platform, fresh_path in (
            ("ios", "$GITHUB_WORKSPACE/build/ios/iphonesimulator/Pleazyng.app"),
            ("tvos", "$RUNNER_TEMP/apple-store-tvos-derived/Build/Products/Debug-appletvsimulator/Runner.app"),
            ("macos", "$RUNNER_TEMP/apple-store-macos-candidate/Pleazyng.xcarchive/Products/Applications/Pleazyng.app"),
        ):
            job = job_text(platform, "tvos" if platform == "ios" else "macos" if platform == "tvos" else "package")
            restored_path = f"$RUNNER_TEMP/apple-store-build-{platform}-restored/build-provenance.json"
            fresh_receipt = f"$RUNNER_TEMP/apple-store-build-{platform}/build-provenance.json"

            self.assertIn(f"APPLE_STORE_BUILD_RECEIPT={restored_path}", job, platform)
            self.assertIn(f"APPLE_STORE_BUILD_RECEIPT={fresh_receipt}", job, platform)
            self.assertIn(f"--build-receipt \"$APPLE_STORE_BUILD_RECEIPT\"", job, platform)
            self.assertEqual(job.count('--build-receipt "$APPLE_STORE_BUILD_RECEIPT"'), {"ios": 1, "tvos": 3, "macos": 5}[platform])
            self.assertNotIn(f"--build-receipt \"{restored_path}\"", job, platform)

            restore_name = {
                "ios": "Validate and restore prior iOS build",
                "tvos": "Validate and restore prior tvOS build",
                "macos": "Validate and restore prior macOS build",
            }[platform]
            restore_step = named_step_text(job, restore_name)
            self.assertIn(f"APPLE_STORE_BUILD_RECEIPT={restored_path}", restore_step, platform)
            preserve_name = {
                "ios": "Preserve the compiled iOS Simulator app before capture",
                "tvos": "Preserve the compiled tvOS Simulator app before capture",
                "macos": "Preserve the compiled unsigned Mac app before capture",
            }[platform]
            preserve_step = named_step_text(job, preserve_name)
            self.assertIn(f"APPLE_STORE_APP_PATH={fresh_path}", preserve_step, platform)
            self.assertIn(f"APPLE_STORE_BUILD_RECEIPT={fresh_receipt}", preserve_step, platform)

    def test_ipad_listing_capture_restores_the_exact_ios_artifact_without_building(self) -> None:
        target = "ipad-in-use-capture-only"
        ios = job_text("ios", "tvos")

        self.assertIn(f"- {target}", WORKFLOW)
        self.assertIn(target, ios.split("\n", 2)[1])
        self.assertIn("runs-on: ${{ (inputs.target == 'ipad-in-use-capture-only' || inputs.target == 'ipad-listing-producer-only') && 'xcode-27' || 'macos-26' }}", ios)
        self.assertIn("fetch-depth: ${{ inputs.target == 'ipad-in-use-capture-only' && '0'", ios)
        self.assertIn("inputs.ios_artifact_run_id", named_step_text(ios, "Verify the prior build artifact producer"))
        self.assertIn("inputs.ios_artifact_run_id", named_step_text(ios, "Download prior iOS build"))
        self.assertIn("apple-store-build-ios-${{ inputs.product_version || '3.0.37' }}", named_step_text(ios, "Download prior iOS build"))

        xcode_step = named_step_text(ios, "Select supported Xcode and iOS SDK for retained-app capture")
        self.assertIn(target, named_step_condition(ios, "Select supported Xcode and iOS SDK for retained-app capture"))
        self.assertIn("/Applications/Xcode_27.2.app/Contents/Developer", xcode_step)
        self.assertIn("xcrun --sdk iphonesimulator --show-sdk-version", xcode_step)
        self.assertIn('runtime.get("version") == "27.2"', xcode_step)

        for step_name in (
            "Load pinned Flutter SDK",
            "Setup Flutter",
            "Cache Pub dependencies",
            "Install native build tools",
            "Install CocoaPods",
            "Install locked Flutter dependencies",
            "Resolve pinned iOS Swift packages",
            "Build the Store-mode iOS Simulator app",
            "Preserve the compiled iOS Simulator app before capture",
        ):
            self.assertIn(target, named_step_condition(ios, step_name), step_name)

        capture_step = named_step_text(ios, "Capture post-onboarding iPhone and iPad Files and Books screens")
        self.assertIn(target, named_step_condition(ios, "Capture post-onboarding iPhone and iPad Files and Books screens"))
        self.assertIn("'ipad' || 'both'", capture_step)
        self.assertIn("PLEAZYNG_CAPTURE_IPAD_LOCALTEST_TRIAL", capture_step)
        self.assertIn("inputs.target == 'ipad-in-use-capture-only' && 'true' || 'false'", capture_step)
        self.assertIn("localtest_args+=(--localtest-trial)", capture_step)
        self.assertIn("apple-store-in-use-captures-ios-${{ inputs.product_version || '3.0.37' }}", ios)
        self.assertIn(
            "if: always() && (inputs.target == 'app-in-use-capture-only' || inputs.target == 'ios-in-use-capture-only' || inputs.target == 'ipad-in-use-capture-only' || inputs.target == 'ipad-listing-producer-only')",
            ios,
        )

    def test_tvos_in_use_capture_restores_bundle_and_never_builds_or_preserves_a_new_one(self) -> None:
        tvos = job_text("tvos", "macos")
        self.assertIn("inputs.target == 'tvos-in-use-capture-only'", tvos)
        self.assertIn("inputs.tvos_artifact_run_id", tvos)
        self.assertIn("Validate and restore prior tvOS build", tvos)

        for step_name in (
            "Setup the pinned tvOS Flutter SDK",
            "Resolve tvOS dependencies",
            "Build the Store-mode tvOS Simulator app",
        ):
            condition = named_step_condition(tvos, step_name)
            self.assertIn("inputs.target != 'tvos-in-use-capture-only'", condition, step_name)

        preserve = named_step_condition(tvos, "Preserve the compiled tvOS Simulator app before capture")
        self.assertIn("inputs.target != 'tvos-in-use-capture-only'", preserve)

        build_upload = re.search(
            r"- uses: actions/upload-artifact@[^\n]+\n\s+if: ([^\n]+)\n\s+with:\n\s+name: apple-store-build-tvos-[^\n]+",
            tvos,
        )
        self.assertIsNotNone(build_upload)
        self.assertIn("inputs.target != 'tvos-in-use-capture-only'", build_upload.group(1))

        self.assertIn("Capture tvOS app with native LOCALTEST trial history", tvos)
        self.assertIn("inputs.target == 'tvos-in-use-capture-only'", named_step_condition(
            tvos, "Capture tvOS app with native LOCALTEST trial history"
        ))
        self.assertNotIn("tvos-in-use-capture-only", named_step_condition(
            tvos, "Capture the post-onboarding tvOS Files and Books screen"
        ))

    def test_mac_listing_capture_probes_public_arm_display_before_restoring_the_app(self) -> None:
        macos = job_text("macos", "package")
        self.assertIn(
            "runs-on: ${{ inputs.target == 'macos-in-use-capture-macos26-only' && 'macos-26-intel' || ((inputs.target == 'macos-in-use-capture-only' || inputs.target == 'macos-listing-capture-only') && 'xcode-27' || 'macos-26-intel') }}",
            macos,
        )
        probe = named_step_text(macos, "Probe and select native Mac display mode before downloading the app")
        self.assertIn("inputs.target == 'macos-listing-capture-only'", probe)
        self.assertIn('"enumerated_modes": modes.map(record)', probe)
        self.assertIn('"display_mode_activated": displayModeActivated', probe)
        self.assertIn('"target_window_points": [1280, 800]', probe)
        self.assertIn("swift \"$RUNNER_TEMP/mac-display-capability.swift\"", probe)
        self.assertLess(
            macos.index("Probe and select native Mac display mode before downloading the app"),
            macos.index("Download prior macOS build"),
        )
        self.assertIn("/Applications/Xcode_27.2.app/Contents/Developer", named_step_text(
            macos, "Select supported Xcode for Mac OCR capture"
        ))

    def test_macos26_localtest_capture_uses_retained_app_and_isolated_storekit_flow(self) -> None:
        target = "macos-in-use-capture-macos26-only"
        macos = job_text("macos", "package")
        ios = job_text("ios", "tvos")
        tvos = job_text("tvos", "macos")
        package = job_text("package", None)

        self.assertIn(f"- {target}", WORKFLOW)
        self.assertIn(target, macos.split("\n", 2)[1])
        self.assertNotIn(target, ios)
        self.assertNotIn(target, tvos)
        self.assertNotIn(target, package)
        self.assertIn("&& 'macos-26-intel'", macos)
        self.assertIn("Select public macOS 26 Xcode for retained-app LOCALTEST", macos)
        xcode = named_step_text(macos, "Select public macOS 26 Xcode for retained-app LOCALTEST")
        self.assertIn("/Applications/Xcode_26.6.app/Contents/Developer", xcode)
        self.assertIn("== 'Xcode 26.6'", xcode)
        self.assertIn("echo \"Selected Xcode: $xcode_version\"", xcode)
        self.assertIn("echo \"Selected macOS SDK: $macos_sdk_version\"", xcode)
        self.assertIn("== '26.5'", xcode)

        for step_name in (
            "Verify the prior build artifact producer",
            "Download prior macOS build",
            "Validate and restore prior macOS build",
        ):
            self.assertIn(target, named_step_condition(macos, step_name), step_name)
        restore = named_step_text(macos, "Validate and restore prior macOS build")
        self.assertIn(
            'expected_build_source_tag="v${PLEAZYNG_CAPTURE_PRODUCT_VERSION}-candidate.${PLEAZYNG_CAPTURE_PRODUCT_SOURCE_SHA}"',
            restore,
        )
        self.assertIn('"source_tag": sys.argv[5]', restore)
        self.assertIn('"source_sha": sys.argv[2]', restore)
        self.assertIn(
            'git fetch --no-tags origin "refs/tags/$expected_build_source_tag:refs/tags/$expected_build_source_tag"',
            restore,
        )
        self.assertIn('export PLEAZYNG_CAPTURE_PRODUCT_SOURCE_TAG="$expected_build_source_tag"', restore)
        self.assertIn(
            'echo "PLEAZYNG_CAPTURE_PRODUCT_SOURCE_TAG=$PLEAZYNG_CAPTURE_PRODUCT_SOURCE_TAG" >> "$GITHUB_ENV"',
            restore,
        )
        for step_name in (
            "Build the unsigned Mac App Store candidate",
            "Preserve the compiled unsigned Mac app before capture",
            "Capture the actual macOS app window",
        ):
            self.assertIn(f"inputs.target != '{target}'", named_step_condition(macos, step_name), step_name)

        capture = named_step_text(macos, "Capture Mac app with native LOCALTEST trial history")
        self.assertIn(target, named_step_condition(macos, "Capture Mac app with native LOCALTEST trial history"))
        self.assertIn("--producer-run-attempt", capture)
        self.assertIn("capture_apple_macos_tvos_storekit.py capture", capture)
        self.assertIn(target, named_step_condition(macos, "Install XcodeGen for native Mac UI capture"))
        self.assertIn(target, named_step_condition(macos, "Select public macOS 26 Xcode for retained-app LOCALTEST"))
        self.assertIn(target, macos[macos.index("Verify the prior build artifact producer"):])
        in_use_artifact = re.search(
            r"- uses: actions/upload-artifact@[^\n]+\n\s+if: ([^\n]+)\n\s+with:\n\s+name: apple-store-in-use-captures-macos-",
            macos,
        )
        self.assertIsNotNone(in_use_artifact)
        self.assertIn(target, in_use_artifact.group(1))

    def test_pinned_macos_tvos_producer_builds_only_those_platforms_and_retains_evidence(self) -> None:
        target = "macos-tvos-in-use-producer-only"
        ios = job_text("ios", "tvos")
        tvos = job_text("tvos", "macos")
        macos = job_text("macos", "package")
        package = job_text("package", None)

        self.assertIn(f"- {target}", WORKFLOW)
        self.assertNotIn(target, ios)
        self.assertIn(target, tvos.split("\n", 2)[1])
        self.assertIn(target, macos.split("\n", 2)[1])
        self.assertNotIn(target, package)

        for step_name in (
            "Setup the pinned tvOS Flutter SDK",
            "Build the Store-mode tvOS Simulator app",
            "Verify the prior build artifact producer",
            "Download prior tvOS build",
            "Validate and restore prior tvOS build",
        ):
            self.assertNotIn(target, named_step_condition(tvos, step_name), step_name)
        self.assertIn(target, named_step_condition(tvos, "Install the UI capture project generator"))
        self.assertIn(target, named_step_condition(tvos, "Capture the post-onboarding tvOS Files and Books screen"))
        self.assertNotIn(target, named_step_condition(macos, "Build the unsigned Mac App Store candidate"))
        self.assertIn(target, named_step_condition(macos, "Capture the local Files and Books screen and real Mac lifetime-access sheet"))
        self.assertIn(target, named_step_condition(tvos, "Capture the actual tvOS app screen"))
        self.assertIn(target, named_step_condition(macos, "Capture the actual macOS app window"))
        for step_name in (
            "Verify the prior build artifact producer",
            "Download prior macOS build",
            "Validate and restore prior macOS build",
        ):
            self.assertNotIn(target, named_step_condition(macos, step_name), step_name)

        for platform, job in (("tvos", tvos), ("macos", macos)):
            upload = re.search(
                rf"- uses: actions/upload-artifact@[^\n]+\n\s+if: ([^\n]+)\n\s+with:\n\s+name: apple-store-build-{platform}-[^\n]+",
                job,
            )
            self.assertIsNotNone(upload, platform)
            self.assertIn("always()", upload.group(1), platform)
            self.assertIn(target, named_step_condition(
                job,
                "Capture the post-onboarding tvOS Files and Books screen" if platform == "tvos" else
                "Capture the local Files and Books screen and real Mac lifetime-access sheet",
            ))
            in_use_upload = re.search(
                rf"- uses: actions/upload-artifact@[^\n]+\n\s+if: ([^\n]+)\n\s+with:\n\s+name: apple-store-in-use-captures-{platform}-[^\n]+",
                job,
            )
            self.assertIsNotNone(in_use_upload, platform)
            self.assertIn(target, in_use_upload.group(1), platform)

        self.assertEqual(WORKFLOW.count("EXPECTED_SOURCE_SHA: ${{ inputs.product_source_sha || '28f417ab17a4b6dc4c2e422b27662c62f62be7fb' }}"), 3)
        self.assertEqual(WORKFLOW.count("EXPECTED_SOURCE_TAG: ${{ inputs.product_source_tag || 'v3.0.37-candidate.28f417ab17a4b6dc4c2e422b27662c62f62be7fb' }}"), 3)
        tvos = job_text("tvos", "macos")
        self.assertIn("EXPECTED_SOURCE_SHA: ${{ inputs.product_source_sha", tvos)
        self.assertIn("EXPECTED_SOURCE_TAG: ${{ inputs.product_source_tag", tvos)
        self.assertIn("EXPECTED_VERSION: ${{ inputs.product_version", tvos)
        self.assertIn("EXPECTED_BUILD: ${{ inputs.product_build", tvos)
        self.assertEqual(WORKFLOW.count("EXPECTED_VERSION: ${{ inputs.product_version || '3.0.37' }}"), 3)
        self.assertEqual(WORKFLOW.count("EXPECTED_BUILD: ${{ inputs.product_build || '196' }}"), 3)
        self.assertEqual(WORKFLOW.count("git cat-file -t \"refs/tags/$EXPECTED_SOURCE_TAG\""), 3)
        self.assertEqual(WORKFLOW.count('python3 scripts/pubspec_version.py'), 3)

    def test_tvos_only_storekit_producer_requires_exact_dynamic_source_and_never_runs_mac_or_ios(self) -> None:
        target = "tvos-in-use-producer-only"
        ios = job_text("ios", "tvos")
        tvos = job_text("tvos", "macos")
        macos = job_text("macos", "package")
        package = job_text("package", None)

        self.assertIn(f"- {target}", WORKFLOW)
        self.assertNotIn(target, ios)
        self.assertIn(target, tvos.split("\n", 2)[1])
        self.assertNotRegex(macos, rf"inputs\.target\s*==\s*'{target}'")
        self.assertNotIn(target, package)
        self.assertIn("runs-on: ${{ (inputs.target == 'tvos-in-use-capture-only' || inputs.target == 'tvos-in-use-producer-only') && 'xcode-27' || 'macos-26' }}", tvos)
        self.assertIn("ref: ${{ inputs.product_source_tag", tvos)
        self.assertIn("product_source_tag:", WORKFLOW)
        self.assertIn("product_source_sha:", WORKFLOW)
        self.assertIn("product_version:", WORKFLOW)
        self.assertIn("product_build:", WORKFLOW)
        self.assertIn('[[ -n "$INPUT_PRODUCT_SOURCE_SHA" && -n "$INPUT_PRODUCT_SOURCE_TAG" ]]', tvos)
        self.assertIn('git rev-parse "refs/tags/$EXPECTED_SOURCE_TAG^{commit}"', tvos)
        self.assertIn("test \"$(git rev-parse HEAD)\" = \"$EXPECTED_SOURCE_SHA\"", tvos)
        self.assertIn('[[ "$EXPECTED_SOURCE_TAG" == "v$EXPECTED_VERSION-candidate.$EXPECTED_SOURCE_SHA" ]]', tvos)
        self.assertIn('test "$(python3 scripts/pubspec_version.py)" = "$EXPECTED_VERSION+$EXPECTED_BUILD"', tvos)

        for step_name in (
            "Setup the pinned tvOS Flutter SDK",
            "Resolve tvOS dependencies",
            "Build the Store-mode tvOS Simulator app",
            "Preserve the compiled tvOS Simulator app before capture",
        ):
            condition = named_step_condition(tvos, step_name)
            self.assertNotIn(target, condition, step_name)

        for step_name in (
            "Verify the prior build artifact producer",
            "Download prior tvOS build",
            "Validate and restore prior tvOS build",
        ):
            if step_name in tvos:
                self.assertNotIn(target, named_step_condition(tvos, step_name), step_name)
        self.assertIn(target, named_step_condition(tvos, "Select supported Xcode and tvOS SDK for retained-app capture"))
        self.assertIn(target, named_step_condition(tvos, "Verify the tvOS Simulator AMSMB2 strong dependency"))
        self.assertIn(target, named_step_condition(tvos, "Capture tvOS app with native LOCALTEST trial history"))
        self.assertNotRegex(
            named_step_condition(tvos, "Capture the actual tvOS app screen"),
            rf"inputs\.target\s*==\s*'{target}'",
        )
        self.assertEqual(
            named_step_condition(tvos, "Retain standard tvOS app screenshots"),
            named_step_condition(tvos, "Capture the actual tvOS app screen"),
        )
        capture = named_step_text(tvos, "Capture tvOS app with native LOCALTEST trial history")
        self.assertIn("--dependency-receipt", capture)
        self.assertIn("PLEAZYNG_CAPTURE_PRODUCT_SOURCE_SHA:", WORKFLOW)
        self.assertIn("PLEAZYNG_CAPTURE_FROZEN_PRODUCT_SOURCE_SHA:", WORKFLOW)
        self.assertIn("--source-lineage-root", named_step_text(tvos, "Preserve the compiled tvOS Simulator app before capture"))
        self.assertIn("tvos-runtime-dependency-receipt.json", named_step_text(tvos, "Retain tvOS runtime dependency report"))
        self.assertIn("apple-store-in-use-captures-tvos-${{ inputs.product_version || '3.0.37' }}", tvos)

    def test_apple_capture_lanes_bind_all_platforms_to_the_supplied_candidate_identity(self) -> None:
        for start, end in (("ios", "tvos"), ("tvos", "macos"), ("macos", "package")):
            job = job_text(start, end)
            with self.subTest(job=start):
                self.assertIn("ref: ${{ inputs.product_source_tag || 'v3.0.37-candidate.", job)
                self.assertIn("EXPECTED_SOURCE_SHA: ${{ inputs.product_source_sha ||", job)
                self.assertIn("EXPECTED_SOURCE_TAG: ${{ inputs.product_source_tag ||", job)
                self.assertIn("EXPECTED_VERSION: ${{ inputs.product_version ||", job)
                self.assertIn("EXPECTED_BUILD: ${{ inputs.product_build ||", job)
                self.assertIn('[[ "$EXPECTED_SOURCE_TAG" == "v$EXPECTED_VERSION-candidate.$EXPECTED_SOURCE_SHA" ]]', job)
                self.assertIn('test "$(python3 scripts/pubspec_version.py)" = "$EXPECTED_VERSION+$EXPECTED_BUILD"', job)
                self.assertIn("compare/$EXPECTED_SOURCE_SHA...$GITHUB_SHA", job)

        ios = job_text("ios", "tvos")
        macos = job_text("macos", "package")
        package = job_text("package", None)
        self.assertIn('SOURCE_SHA: ${{ inputs.product_source_sha ||', ios)
        self.assertIn('--source-sha "$PLEAZYNG_CAPTURE_PRODUCT_SOURCE_SHA"', macos)
        self.assertIn('apple-store-screenshots-${{ inputs.product_version ||', package)
        self.assertNotIn("9b1552e8ba7a206a4c05ecc934976358566744f2", WORKFLOW)
        self.assertIn("PLEAZYNG_CAPTURE_PRODUCT_VERSION: ${{ inputs.product_version ||", WORKFLOW)

    def test_macos_diagnostic_target_isolated_from_capture_and_build_lanes(self) -> None:
        self.assertIn("- macos-in-use-diagnostic-only", WORKFLOW)
        ios = job_text("ios", "tvos")
        tvos = job_text("tvos", "macos")
        macos = job_text("macos", "package")
        self.assertNotIn("macos-in-use-diagnostic-only", ios)
        self.assertNotIn("macos-in-use-diagnostic-only", tvos)
        self.assertIn("inputs.target == 'macos-in-use-diagnostic-only'", macos)
        self.assertIn("inputs.macos_artifact_run_id", macos)
        self.assertIn("Validate and restore prior macOS build", macos)

        for step_name in (
            "Load pinned Flutter SDK",
            "Build the unsigned Mac App Store candidate",
        ):
            self.assertIn(
                "inputs.target != 'macos-in-use-diagnostic-only'",
                named_step_condition(macos, step_name),
                step_name,
            )

        diagnosis = named_step_condition(macos, "Diagnose the retained Mac app without rebuilding")
        self.assertEqual(diagnosis, "inputs.target == 'macos-in-use-diagnostic-only'")
        self.assertNotIn(
            "macos-in-use-diagnostic-only",
            named_step_condition(macos, "Capture the local Files and Books screen and real Mac lifetime-access sheet"),
        )
        self.assertNotIn("macos-in-use-diagnostic-only", job_text("package", None))

    def test_platform_in_use_targets_use_native_localtest_trial_harness_without_product_rebuild(self) -> None:
        ios = job_text("ios", "tvos")
        tvos = job_text("tvos", "macos")
        macos = job_text("macos", "package")
        for platform, job, target, step_name, legacy_step, producer_input in (
            (
                "tvos", tvos, "tvos-in-use-capture-only",
                "Capture tvOS app with native LOCALTEST trial history",
                "Capture the post-onboarding tvOS Files and Books screen", "tvos_artifact_run_id",
            ),
            (
                "macos", macos, "macos-in-use-capture-only",
                "Capture Mac app with native LOCALTEST trial history",
                "Capture the local Files and Books screen and real Mac lifetime-access sheet",
                "macos_artifact_run_id",
            ),
        ):
            self.assertIn(target, named_step_condition(job, step_name))
            capture_step = named_step_text(job, step_name)
            self.assertIn(f"--platform {platform}", capture_step)
            self.assertIn("--build-receipt \"$APPLE_STORE_BUILD_RECEIPT\"", capture_step)
            self.assertIn("--capture-tools-sha \"$PLEAZYNG_CAPTURE_TOOLS_SHA\"", capture_step)
            self.assertIn(f"inputs.{producer_input}", job)
            self.assertNotIn(target, named_step_condition(job, legacy_step))
            self.assertIn("Validate and restore prior", job)
            self.assertIn(f"inputs.target != '{target}'", named_step_condition(
                job,
                "Build the Store-mode tvOS Simulator app" if platform == "tvos" else
                "Build the unsigned Mac App Store candidate",
            ))
            self.assertIn(f"inputs.target != '{target}'", named_step_condition(
                job,
                "Setup the pinned tvOS Flutter SDK" if platform == "tvos" else "Load pinned Flutter SDK",
            ))
        self.assertNotIn("tvos-in-use-capture-only", ios)
        self.assertNotIn("macos-in-use-capture-only", ios)


if __name__ == "__main__":
    unittest.main()
