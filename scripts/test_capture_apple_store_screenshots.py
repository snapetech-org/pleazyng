#!/usr/bin/env python3
"""Focused checks for simulator-only iOS Keychain capture signing."""

from __future__ import annotations

import plistlib
import hashlib
import inspect
import json
import subprocess
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import capture_apple_store_screenshots as capture
from capture_apple_store_screenshots import (
    BUNDLE_ID,
    BUILD_NUMBER,
    MACOS_LISTING_OCR_SWIFT,
    MACOS_LIVE_DISPLAY_STATE_SWIFT,
    MACOS_POST_NATIVE_CLICK_SWIFT,
    MACOS_SELECT_WINDOW_CAPTURE_DISPLAY_SWIFT,
    MACOS_WINDOW_BOUNDS_SWIFT,
    SOURCE_SHA,
    SOURCE_TAG,
    VERSION,
    bundle_executable,
    code_executable_inventory,
    derive_nested_bundle_simulator_entitlements,
    derive_ios_simulator_entitlements,
    nested_code_paths,
    runner_signing_identity,
    simulator_launch_pid,
    simulator_reported_tvos_app_stopped,
    simulator_codesign_command,
    require_source,
    verify_tvos_simulator_framework_bundle,
)


TEST_BUNDLE_ID = "org.snapetech.pleazyng.capture-ui-tests"
RUNNER_BUNDLE_ID = f"{TEST_BUNDLE_ID}.xctrunner"
TEAM_PREFIX = "FAKETEAMID."
TEST_GROUP = f"{TEAM_PREFIX}{TEST_BUNDLE_ID}"
TEST_RUNNER_GROUP = f"{TEAM_PREFIX}{RUNNER_BUNDLE_ID}"


class SimulatorSigningTests(unittest.TestCase):
    def make_derived_data(self, temporary: str, simulated: dict, signed: dict) -> tuple[Path, Path]:
        derived_data = Path(temporary)
        simulated_xcent = (
            derived_data / "Build" / "Intermediates.noindex" / "CaptureTests.xctest-Simulated.xcent"
        )
        simulated_xcent.parent.mkdir(parents=True)
        simulated_xcent.write_bytes(plistlib.dumps(simulated))

        runner_app = (
            derived_data / "Build" / "Products" / "Debug-iphonesimulator" / "CaptureTests-Runner.app"
        )
        runner_app.mkdir(parents=True)
        (runner_app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": RUNNER_BUNDLE_ID}))
        return derived_data, simulated_xcent

    def run_identity(self, derived_data: Path, signed: dict, evidence_dir: Path | None = None):
        with patch("capture_apple_store_screenshots.codesign_entitlements", return_value=signed):
            return runner_signing_identity(derived_data, TEST_BUNDLE_ID, evidence_dir)

    def test_derives_prefix_from_observed_group_only_fixture_and_preserves_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            derived_data, source_xcent = self.make_derived_data(
                temporary,
                {"keychain-access-groups": [TEST_GROUP]},
                {},
            )
            evidence_dir = Path(temporary) / "evidence"
            actual_path, entitlements, prefix, runner_bundle_id, signed, signed_path = self.run_identity(
                derived_data, {}, evidence_dir
            )

            self.assertEqual(actual_path.read_bytes(), source_xcent.read_bytes())
            self.assertEqual(actual_path.name, "ui-test-runner-simulated-entitlements.xcent")
            self.assertEqual(signed_path.read_bytes(), plistlib.dumps({}, fmt=plistlib.FMT_XML, sort_keys=True))

        self.assertEqual(entitlements, {"keychain-access-groups": [TEST_GROUP]})
        self.assertEqual(prefix, TEAM_PREFIX)
        self.assertEqual(runner_bundle_id, RUNNER_BUNDLE_ID)
        self.assertEqual(signed, {})


    def test_cross_checks_signed_application_identifier_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            derived_data, _ = self.make_derived_data(
                temporary,
                {"keychain-access-groups": [TEST_GROUP]},
                {},
            )
            signed = {
                "application-identifier": f"{TEAM_PREFIX}{RUNNER_BUNDLE_ID}",
                "keychain-access-groups": [TEST_GROUP],
            }
            _, _, prefix, runner_bundle_id, actual_signed, _ = self.run_identity(derived_data, signed)

        self.assertEqual(prefix, TEAM_PREFIX)
        self.assertEqual(runner_bundle_id, RUNNER_BUNDLE_ID)
        self.assertEqual(actual_signed, signed)

    def test_accepts_xcode_simulated_keychain_group_with_runner_suffix(self) -> None:
        signed = {
            "application-identifier": TEST_RUNNER_GROUP,
            "keychain-access-groups": [TEST_RUNNER_GROUP],
        }
        with tempfile.TemporaryDirectory() as temporary:
            derived_data, _ = self.make_derived_data(
                temporary,
                {"application-identifier": TEST_RUNNER_GROUP, "keychain-access-groups": [TEST_RUNNER_GROUP]},
                {},
            )
            _, entitlements, prefix, runner_bundle_id, actual_signed, _ = self.run_identity(
                derived_data, signed
            )

        self.assertEqual(prefix, TEAM_PREFIX)
        self.assertEqual(runner_bundle_id, RUNNER_BUNDLE_ID)
        self.assertEqual(entitlements["keychain-access-groups"], [TEST_RUNNER_GROUP])
        self.assertEqual(actual_signed, signed)

    def test_rejects_signed_application_identifier_with_a_different_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            derived_data, _ = self.make_derived_data(
                temporary,
                {"keychain-access-groups": [TEST_GROUP]},
                {},
            )
            with self.assertRaisesRegex(RuntimeError, "disagrees with Xcode's simulated prefix"):
                self.run_identity(derived_data, {
                    "application-identifier": f"OTHERTTEAM.org.snapetech.pleazyng.capture-ui-tests.xctrunner",
                })

    def test_rejects_xcode_simulated_groups_for_another_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            derived_data, _ = self.make_derived_data(
                temporary,
                {"keychain-access-groups": [f"{TEAM_PREFIX}unrelated.capture-ui-tests"]},
                {},
            )
            with self.assertRaisesRegex(RuntimeError, "Expected exactly one Xcode-simulated"):
                self.run_identity(derived_data, {})

    def test_rejects_extra_simulated_or_signed_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            derived_data, _ = self.make_derived_data(
                temporary,
                {"keychain-access-groups": [TEST_GROUP, f"{TEAM_PREFIX}unrelated"]},
                {},
            )
            with self.assertRaisesRegex(RuntimeError, "Expected exactly one Xcode-simulated"):
                self.run_identity(derived_data, {})

        with tempfile.TemporaryDirectory() as temporary:
            derived_data, _ = self.make_derived_data(
                temporary,
                {"keychain-access-groups": [TEST_GROUP]},
                {},
            )
            with self.assertRaisesRegex(RuntimeError, "unexpected Keychain access group"):
                self.run_identity(derived_data, {
                    "keychain-access-groups": [TEST_GROUP, f"{TEAM_PREFIX}unrelated"],
                })

    def test_derives_product_group_from_xcode_simulated_group_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "DebugProfile.entitlements"
            source.write_bytes(plistlib.dumps({"keychain-access-groups": []}))

            entitlements, prefix, product_identifier = derive_ios_simulator_entitlements(
                source,
                {"keychain-access-groups": [TEST_GROUP]},
                RUNNER_BUNDLE_ID,
                "org.snapetech.pleazyng",
            )

        self.assertEqual(prefix, TEAM_PREFIX)
        self.assertEqual(product_identifier, f"{TEAM_PREFIX}org.snapetech.pleazyng")
        self.assertEqual(entitlements, {
            "application-identifier": f"{TEAM_PREFIX}org.snapetech.pleazyng",
            "keychain-access-groups": [f"{TEAM_PREFIX}org.snapetech.pleazyng"],
        })

    def test_derives_product_group_from_xcode_runner_suffix_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "DebugProfile.entitlements"
            source.write_bytes(plistlib.dumps({"keychain-access-groups": []}))
            entitlements, prefix, product_identifier = derive_ios_simulator_entitlements(
                source,
                {"application-identifier": TEST_RUNNER_GROUP,
                 "keychain-access-groups": [TEST_RUNNER_GROUP]},
                RUNNER_BUNDLE_ID,
                "org.snapetech.pleazyng",
            )

        self.assertEqual(prefix, TEAM_PREFIX)
        self.assertEqual(product_identifier, f"{TEAM_PREFIX}org.snapetech.pleazyng")
        self.assertEqual(entitlements["keychain-access-groups"], [product_identifier])

    def test_rejects_nonempty_product_group_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "DebugProfile.entitlements"
            source.write_bytes(plistlib.dumps({"keychain-access-groups": ["UNRELATED.group"]}))

            with self.assertRaisesRegex(ValueError, "approved empty Keychain group list"):
                derive_ios_simulator_entitlements(
                    source,
                    {"keychain-access-groups": [TEST_GROUP]},
                    RUNNER_BUNDLE_ID,
                    "org.snapetech.pleazyng",
                )


    def test_nested_extension_keeps_its_source_entitlements_and_own_identity(self) -> None:
        source = {
            "com.apple.security.hardened-process": True,
            "com.apple.security.hardened-process.dyld-ro": True,
        }

        actual = derive_nested_bundle_simulator_entitlements(
            source,
            TEAM_PREFIX,
            "org.snapetech.pleazyng.RommWorker",
        )

        self.assertEqual(actual, {
            **source,
            "application-identifier": f"{TEAM_PREFIX}org.snapetech.pleazyng.RommWorker",
        })

    def test_nested_extension_rejects_inherited_or_sharing_identities(self) -> None:
        for key in (
            "application-identifier",
            "keychain-access-groups",
            "com.apple.developer.team-identifier",
            "com.apple.security.application-groups",
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "identity or sharing grants"):
                derive_nested_bundle_simulator_entitlements(
                    {key: "unexpected"}, TEAM_PREFIX, "org.snapetech.pleazyng.RommWorker"
                )

    def test_nested_codesign_command_never_deep_signs_and_generates_der_for_entitlements(self) -> None:
        no_entitlements = simulator_codesign_command(Path("/tmp/Foo.framework"))
        with_entitlements = simulator_codesign_command(
            Path("/tmp/RommWorker.appex"), Path("/tmp/RommWorker.entitlements.plist")
        )

        self.assertNotIn("--deep", no_entitlements)
        self.assertNotIn("--entitlements", no_entitlements)
        self.assertIn("--generate-entitlement-der", with_entitlements)
        self.assertEqual(with_entitlements[-3:], [
            "--entitlements", "/tmp/RommWorker.entitlements.plist", "/tmp/RommWorker.appex"
        ])
        self.assertNotIn("--deep", with_entitlements)

    def test_nested_code_order_places_contained_code_before_its_container(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Pleazyng.app"
            parent = app / "Frameworks" / "Outer.framework"
            child = parent / "Frameworks" / "Inner.framework"
            extension = app / "PlugIns" / "RommWorker.appex"
            dylib = app / "Frameworks" / "libNative.dylib"
            for path in (child, parent, extension):
                path.mkdir(parents=True, exist_ok=True)
            dylib.parent.mkdir(parents=True, exist_ok=True)
            dylib.touch()

            paths = nested_code_paths(app)
            indices = {path: index for index, (path, _) in enumerate(paths)}

        self.assertLess(indices[child], indices[parent])
        self.assertEqual({kind for _, kind in paths}, {"framework", "appex", "dylib"})

    def test_framework_and_dylib_macho_files_keep_non_executable_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Pleazyng.app"
            app.mkdir()
            (app / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": BUNDLE_ID,
                "CFBundleShortVersionString": VERSION,
                "CFBundleVersion": BUILD_NUMBER,
                "CFBundleExecutable": "Pleazyng",
            }))
            main_executable = app / "Pleazyng"
            main_executable.write_bytes(b"main")
            main_executable.chmod(0o755)

            framework = app / "Frameworks" / "sqlite3.framework"
            framework.mkdir(parents=True)
            (framework / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": "io.flutter.flutter.native-assets.sqlite3",
                "CFBundleExecutable": "sqlite3",
            }))
            framework_binary = framework / "sqlite3"
            framework_binary.write_bytes(b"framework")
            framework_binary.chmod(0o644)

            dylib = app / "Frameworks" / "libnative.dylib"
            dylib.write_bytes(b"dylib")
            dylib.chmod(0o644)

            with patch("capture_apple_store_screenshots.run", return_value="arm64"):
                inventory = code_executable_inventory(app)

        modes = {entry["executable_path"]: entry["executable_mode"] for entry in inventory}
        self.assertEqual(modes["Frameworks/sqlite3.framework/sqlite3"], "0644")
        self.assertEqual(modes["Frameworks/libnative.dylib"], "0644")

    def test_app_extension_executable_must_keep_execute_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            extension = Path(temporary) / "RommWorker.appex"
            extension.mkdir()
            (extension / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": f"{BUNDLE_ID}.RommWorker",
                "CFBundleExecutable": "RommWorker",
            }))
            executable = extension / "RommWorker"
            executable.write_bytes(b"extension")
            executable.chmod(0o644)

            with self.assertRaisesRegex(ValueError, "invalid mode"):
                bundle_executable(extension)


class IpadSimulatorSelectionTests(unittest.TestCase):
    def test_xcode_project_inventory_is_skipped_only_for_ipad_capture(self) -> None:
        self.assertTrue(capture.should_skip_xcode_project_inventory("iOS", "ipad"))
        self.assertFalse(capture.should_skip_xcode_project_inventory("iOS", "iphone"))
        self.assertFalse(capture.should_skip_xcode_project_inventory("tvOS", "ipad"))

    def test_reads_minimum_os_from_the_retained_app_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Pleazyng.app"
            app.mkdir()
            (app / "Info.plist").write_bytes(plistlib.dumps({"MinimumOSVersion": "15.5"}))

            minimum = capture.app_minimum_os_version(app)

        self.assertEqual(minimum, (15, 5))

    def test_tries_installed_runtime_device_pairs_above_the_retained_app_minimum(self) -> None:
        phone_runtime_id = "com.apple.CoreSimulator.SimRuntime.iOS-27-1"
        ipad_runtime_id = "com.apple.CoreSimulator.SimRuntime.iOS-26-5"
        too_old_runtime_id = "com.apple.CoreSimulator.SimRuntime.iOS-14-0"
        inventory = {
            "runtimes": [
                {"identifier": phone_runtime_id, "version": "27.1", "isAvailable": True},
                {"identifier": ipad_runtime_id, "version": "26.5", "isAvailable": True},
                {"identifier": too_old_runtime_id, "version": "14.0", "isAvailable": True},
            ],
            "devices": {phone_runtime_id: [], ipad_runtime_id: [], too_old_runtime_id: []},
        }
        device_types = {"devicetypes": [
            {
                "name": "iPad Pro 13-inch (M5)",
                "identifier": "com.apple.CoreSimulator.SimDeviceType.iPad-Pro-13-inch-M5",
            },
            {
                "name": "iPad Pro 13-inch (M4)",
                "identifier": "com.apple.CoreSimulator.SimDeviceType.iPad-Pro-13-inch-M4",
            },
            {
                "name": "iPad Air 13-inch (M2)",
                "identifier": "com.apple.CoreSimulator.SimDeviceType.iPad-Air-13-inch-M2",
            },
            {
                "name": "iPad Air 11-inch (M3)",
                "identifier": "com.apple.CoreSimulator.SimDeviceType.iPad-Air-11-inch-M3",
                "productFamily": "iPad",
            },
        ]}
        created_udid = "01234567-89AB-CDEF-0123-456789ABCDEF"
        compatible_type = "com.apple.CoreSimulator.SimDeviceType.iPad-Air-13-inch-M2"
        create_calls: list[list[str]] = []

        def mock_run(command: list[str], *, capture: bool = False, timeout: int = 300) -> str:
            if command == ["xcrun", "--sdk", "iphonesimulator", "--show-sdk-version"]:
                return "27.1"
            if command == ["xcrun", "simctl", "list", "-j"]:
                return json.dumps(inventory)
            if command == ["xcrun", "simctl", "list", "devicetypes", "-j"]:
                return json.dumps(device_types)
            if len(command) == 6 and command[:3] == ["xcrun", "simctl", "create"]:
                create_calls.append(command)
                if command[4] == compatible_type and command[5] == ipad_runtime_id:
                    return created_udid
                raise subprocess.CalledProcessError(
                    1, command, stderr="Incompatible device for this runtime"
                )
            raise AssertionError(f"Unexpected simulator command: {command}")

        with patch(
            "capture_apple_store_screenshots.run",
            side_effect=mock_run,
        ):
            device = capture.create_simulator(
                "iOS", "ipad", "45569185714", "1", minimum_os_version=(15, 5)
            )

        self.assertEqual(device["udid"], created_udid)
        self.assertEqual(device["runtime_id"], ipad_runtime_id)
        self.assertEqual(device["runtime_version"], "26.5")
        self.assertEqual(device["device_name"], "iPad Air 13-inch (M2)")
        self.assertEqual(device["device_type"], compatible_type)
        self.assertEqual(len(create_calls), 6)
        self.assertTrue(all(command[5] != too_old_runtime_id for command in create_calls))



class ProductSourcePinTests(unittest.TestCase):
    def make_product_root(self, temporary: str) -> Path:
        root = Path(temporary)
        (root / "pubspec.yaml").write_text(
            f"name: pleazyng\nversion: {VERSION}+{BUILD_NUMBER}\n",
            encoding="utf-8",
        )
        return root

    def test_dynamic_product_source_accepts_exact_candidate_tag_version_and_build(self) -> None:
        source_sha = "a" * 40
        source_tag = f"v3.0.38-candidate.{source_sha}"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pubspec.yaml").write_text(
                "name: pleazyng\nversion: 3.0.38+197\n", encoding="utf-8"
            )
            with patch.object(capture, "SOURCE_SHA", source_sha), \
                    patch.object(capture, "SOURCE_TAG", source_tag), \
                    patch.object(capture, "VERSION", "3.0.38"), \
                    patch.object(capture, "BUILD_NUMBER", "197"), \
                    patch("capture_apple_store_screenshots.run", side_effect=["tag", source_sha, source_sha]):
                capture.require_source(root)

    def test_direct_nonbaseline_lineage_binds_product_packaging_and_workflow_shas(self) -> None:
        source_sha = "a" * 40
        tools_sha = "b" * 40
        with patch.object(capture, "SOURCE_SHA", source_sha), \
                patch.object(capture, "SOURCE_TAG", f"v3.0.38-candidate.{source_sha}"), \
                patch.object(capture, "VERSION", "3.0.38"), \
                patch.object(capture, "BUILD_NUMBER", "197"), \
                patch.dict("os.environ", {"PLEAZYNG_CAPTURE_TOOLS_SHA": tools_sha}):
            lineage = capture.source_lineage(Path("/unused/product"), None)

        self.assertEqual(source_sha, lineage["product_source_sha"])
        self.assertEqual(source_sha, lineage["packaging_source_sha"])
        self.assertEqual(tools_sha, lineage["workflow_source_sha"])
        self.assertIn("built directly", lineage["relationship"])

    def test_requires_exact_annotated_candidate_tag_source_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_product_root(temporary)
            with patch(
                "capture_apple_store_screenshots.run",
                side_effect=["tag", SOURCE_SHA, SOURCE_SHA],
            ) as git_run:
                require_source(root)

        self.assertEqual(git_run.call_count, 3)
        self.assertEqual(git_run.call_args_list[0].args[0][-2:], ["-t", f"refs/tags/{SOURCE_TAG}"])
        self.assertEqual(
            git_run.call_args_list[1].args[0][-1],
            f"refs/tags/{SOURCE_TAG}^{{commit}}",
        )
        self.assertEqual(git_run.call_args_list[2].args[0][-1], "HEAD")

    def test_rejects_lightweight_tag_or_misdirected_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_product_root(temporary)
            with patch("capture_apple_store_screenshots.run", return_value=SOURCE_SHA):
                with self.assertRaisesRegex(ValueError, "annotated tag"):
                    require_source(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_product_root(temporary)
            with patch(
                "capture_apple_store_screenshots.run",
                side_effect=["tag", "f" * 40],
            ):
                with self.assertRaisesRegex(ValueError, "resolve to"):
                    require_source(root)

    def test_rejects_wrong_head_and_pubspec_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_product_root(temporary)
            with patch(
                "capture_apple_store_screenshots.run",
                side_effect=["tag", SOURCE_SHA, "f" * 40],
            ):
                with self.assertRaisesRegex(ValueError, "Expected .* at"):
                    require_source(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_product_root(temporary)
            (root / "pubspec.yaml").write_text("name: pleazyng\nversion: 3.0.36+190\n", encoding="utf-8")
            with patch(
                "capture_apple_store_screenshots.run",
                side_effect=["tag", SOURCE_SHA, SOURCE_SHA],
            ):
                with self.assertRaisesRegex(ValueError, "Expected 3.0.37\\+196"):
                    require_source(root)

    def test_nonbaseline_source_lineage_requires_manifest_pair_parent_and_exact_blobs(self) -> None:
        packaging_sha = "9b1552e8ba7a206a4c05ecc934976358566744f2"
        product_sha = SOURCE_SHA
        tools_sha = "2305765b75d63565f9ad67e36dc9929b23d745a5"
        files = {
            "scripts/verify_apple_macho_dependencies.py": "1" * 40,
            "tvos/scripts/wire_plugins.rb": "2" * 40,
        }
        manifest = {
            "schema_version": 1,
            "repairs": [{
                "product_source_sha": product_sha,
                "packaging_source_sha": packaging_sha,
                "files": files,
            }],
        }
        manifest_bytes = (json.dumps(manifest, separators=(",", ":")) + "\n").encode()
        manifest_blob = hashlib.sha1(b"blob " + str(len(manifest_bytes)).encode() + b"\0" + manifest_bytes).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            product_root = root / "product"
            approval_root = root / "capture-tools"
            product_root.mkdir()
            manifest_path = approval_root / ".github" / "release-packaging-repairs.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_bytes(manifest_bytes)

            def git_output(repository: Path, *arguments: str) -> str:
                if arguments == ("cat-file", "-t", f"refs/tags/v3.0.37-candidate.{product_sha}"):
                    return "tag"
                if arguments == ("rev-parse", f"refs/tags/v3.0.37-candidate.{product_sha}^{{commit}}"):
                    return product_sha
                if arguments == ("rev-parse", "HEAD"):
                    return tools_sha
                if arguments == ("rev-parse", f"{tools_sha}:.github/release-packaging-repairs.json"):
                    return manifest_blob
                if arguments[:1] == ("rev-list",):
                    return f"{packaging_sha} {product_sha}"
                if arguments[:1] == ("diff-tree",):
                    return "\n".join(sorted(files))
                if arguments[:1] == ("rev-parse",) and arguments[1].startswith(f"{packaging_sha}:"):
                    return files[arguments[1].split(":", 1)[1]]
                raise AssertionError((repository, arguments))

            with patch.object(capture, "SOURCE_SHA", packaging_sha), \
                    patch.object(capture, "SOURCE_TAG", f"v3.0.37-candidate.{packaging_sha}"), \
                    patch.dict("os.environ", {
                        "PLEAZYNG_CAPTURE_FROZEN_PRODUCT_SOURCE_SHA": product_sha,
                        "PLEAZYNG_CAPTURE_FROZEN_PRODUCT_SOURCE_TAG": f"v3.0.37-candidate.{product_sha}",
                        "PLEAZYNG_CAPTURE_TOOLS_SHA": tools_sha,
                    }), \
                    patch.object(capture, "_git_output", side_effect=git_output):
                lineage = capture.source_lineage(product_root, approval_root)

        self.assertEqual(packaging_sha, lineage["product_source_sha"])
        self.assertEqual(product_sha, lineage["related_frozen_product_source_sha"])
        self.assertEqual(packaging_sha, lineage["packaging_source_sha"])
        self.assertEqual(tools_sha, lineage["workflow_source_sha"])
        self.assertEqual(manifest_blob, lineage["approval_blob_sha"])
        self.assertEqual(files, lineage["approval_file_blobs"])

    def test_nonbaseline_source_lineage_rejects_a_changed_file_set(self) -> None:
        packaging_sha = "9b1552e8ba7a206a4c05ecc934976358566744f2"
        product_sha = SOURCE_SHA
        tools_sha = "2305765b75d63565f9ad67e36dc9929b23d745a5"
        files = {"tvos/scripts/wire_plugins.rb": "2" * 40}
        manifest_bytes = (json.dumps({"repairs": [{
            "product_source_sha": product_sha,
            "packaging_source_sha": packaging_sha,
            "files": files,
        }]}, separators=(",", ":")) + "\n").encode()
        manifest_blob = hashlib.sha1(b"blob " + str(len(manifest_bytes)).encode() + b"\0" + manifest_bytes).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            product_root = root / "product"
            approval_root = root / "capture-tools"
            product_root.mkdir()
            manifest_path = approval_root / ".github" / "release-packaging-repairs.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_bytes(manifest_bytes)

            def git_output(repository: Path, *arguments: str) -> str:
                if arguments == ("cat-file", "-t", f"refs/tags/v3.0.37-candidate.{product_sha}"):
                    return "tag"
                if arguments == ("rev-parse", f"refs/tags/v3.0.37-candidate.{product_sha}^{{commit}}"):
                    return product_sha
                if arguments == ("rev-parse", "HEAD"):
                    return tools_sha
                if arguments == ("rev-parse", f"{tools_sha}:.github/release-packaging-repairs.json"):
                    return manifest_blob
                if arguments[:1] == ("rev-list",):
                    return f"{packaging_sha} {product_sha}"
                if arguments[:1] == ("diff-tree",):
                    return "tvos/scripts/wire_plugins.rb\nREADME.md"
                raise AssertionError((repository, arguments))

            with patch.object(capture, "SOURCE_SHA", packaging_sha), \
                    patch.object(capture, "SOURCE_TAG", f"v3.0.37-candidate.{packaging_sha}"), \
                    patch.dict("os.environ", {
                        "PLEAZYNG_CAPTURE_FROZEN_PRODUCT_SOURCE_SHA": product_sha,
                        "PLEAZYNG_CAPTURE_FROZEN_PRODUCT_SOURCE_TAG": f"v3.0.37-candidate.{product_sha}",
                        "PLEAZYNG_CAPTURE_TOOLS_SHA": tools_sha,
                    }), \
                    patch.object(capture, "_git_output", side_effect=git_output):
                with self.assertRaisesRegex(ValueError, "changed-file set"):
                    capture.source_lineage(product_root, approval_root)


class TvOSSimulatorCleanupTests(unittest.TestCase):
    def test_accepts_only_the_specific_already_stopped_cleanup_result(self) -> None:
        stderr = (
            "An error was encountered processing the command (domain=NSPOSIXErrorDomain, code=3):\n"
            "Simulator device failed to terminate org.snapetech.pleazyng.\n"
            "found nothing to terminate\n"
        )
        self.assertTrue(simulator_reported_tvos_app_stopped(3, "", stderr))
        self.assertFalse(simulator_reported_tvos_app_stopped(4, "", stderr))
        self.assertFalse(simulator_reported_tvos_app_stopped(3, "", "found nothing to terminate"))
        self.assertFalse(simulator_reported_tvos_app_stopped(
            3, "", "domain=NSPOSIXErrorDomain, code=2: found nothing to terminate"
        ))

    def test_reads_only_the_exact_product_pid_from_simctl_launch_output(self) -> None:
        self.assertEqual(simulator_launch_pid("org.snapetech.pleazyng: 6612\n"), 6612)
        self.assertIsNone(simulator_launch_pid("org.snapetech.pleazyng: not-a-pid\n"))
        self.assertIsNone(simulator_launch_pid("org.snapetech.other: 6612\n"))


class TvOSSimulatorFrameworkClosureTests(unittest.TestCase):
    def make_app(self, root: Path) -> Path:
        app = root / "Runner.app"
        framework = app / "Frameworks" / "AMSMB2.framework" / "AMSMB2"
        framework.parent.mkdir(parents=True)
        framework.write_bytes(bytes.fromhex("feedfacf") + b"framework")
        executable = app / "Runner"
        app.mkdir(exist_ok=True)
        executable.write_bytes(bytes.fromhex("feedfacf") + b"runner")
        executable.chmod(0o755)
        (app / "Runner.debug.dylib").write_bytes(bytes.fromhex("feedfacf") + b"debug")
        (app / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": BUILD_NUMBER,
            "CFBundleExecutable": "Runner",
        }))
        return app

    def run_output(self, command: list[str], *, weak: bool = False, **_kwargs: object) -> str:
        if "-archs" in command:
            return f"Non-fat file: {command[-1]} is architecture: arm64"
        binary = Path(command[-1])
        if binary.name == "Runner.debug.dylib":
            load_command = "LC_LOAD_WEAK_DYLIB" if weak else "LC_LOAD_DYLIB"
            return (
                f"cmd {load_command}\ncmdsize 64\n"
                "name @rpath/AMSMB2.framework/AMSMB2 (offset 24)\n"
                "cmd LC_RPATH\ncmdsize 32\npath @executable_path/Frameworks (offset 12)\n"
            )
        return ""

    def test_accepts_strong_amsmb2_load_resolved_by_app_rpath(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            with patch("capture_apple_store_screenshots.run", side_effect=self.run_output):
                result = verify_tvos_simulator_framework_bundle(app)

        self.assertEqual("passed", result["status"], result["errors"])
        self.assertEqual(1, len(result["loaders"]))
        self.assertEqual("LC_LOAD_DYLIB", result["loaders"][0]["command"])
        self.assertEqual("Frameworks/AMSMB2.framework/AMSMB2", result["loaders"][0]["resolved_path"])

    def test_rejects_missing_framework_even_with_matching_strong_install_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            framework = app / "Frameworks" / "AMSMB2.framework" / "AMSMB2"
            framework.unlink()
            with patch("capture_apple_store_screenshots.run", side_effect=self.run_output):
                result = verify_tvos_simulator_framework_bundle(app)

        self.assertEqual("failed", result["status"])
        self.assertTrue(any("does not contain a regular AMSMB2" in item for item in result["errors"]))

    def test_rejects_weak_amsmb2_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary))
            with patch("capture_apple_store_screenshots.run", side_effect=lambda cmd, **kwargs: self.run_output(cmd, weak=True)):
                result = verify_tvos_simulator_framework_bundle(app)

        self.assertEqual("failed", result["status"])
        self.assertTrue(any("only weakly loaded" in item for item in result["errors"]))


class ListingCaptureMetadataTests(unittest.TestCase):
    def test_localtest_ipad_lane_requires_unchanged_unsigned_app(self) -> None:
        source = inspect.getsource(capture.capture_ios_in_use_with_localtest_trial)

        self.assertIn('simulator_signing.get("status") != "not-applied"', source)
        self.assertIn('installed_app_receipt.get("installed_app_bundle_sha256") != app_digest', source)
        self.assertIn('app_signature_before != {"status": "unsigned"}', source)
        self.assertIn('app_behavior.get("openMediaChoiceLabelVisible") is not True', source)
        self.assertIn('"open_choice_label_visible"', source)
        self.assertIn('"simulator_signing_applied": False', source)
        self.assertNotIn("simulator_signed_app_bundle_sha256", source)

    def make_rgba_png(self, path: Path, *, alpha: int) -> bytes:
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        png = (
            capture.PNG_SIGNATURE
            + capture._png_chunk(b"IHDR", header)
            + capture._png_chunk(b"IDAT", zlib.compress(bytes((0, 12, 34, 56, alpha))))
            + capture._png_chunk(b"IEND", b"")
        )
        path.write_bytes(png)
        return png

    def test_png_color_type_reads_the_ihdr_color_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capture.png"
            header = (
                b"\x89PNG\r\n\x1a\n"
                + (13).to_bytes(4, "big")
                + b"IHDR"
                + (1).to_bytes(4, "big")
                + (1).to_bytes(4, "big")
                + bytes((8, 2, 0, 0, 0))
            )
            path.write_bytes(header)

            self.assertEqual(2, capture.png_color_type(path))

    def test_normalizes_fully_opaque_rgba_without_changing_rgb_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capture.png"
            self.make_rgba_png(path, alpha=255)

            encoding = capture.normalize_opaque_rgb_png(path)
            data = path.read_bytes()

            self.assertEqual(2, capture.png_color_type(path))
            self.assertEqual((1, 1), (
                int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"),
            ))
            self.assertEqual(6, encoding["source_color_type"])
            self.assertTrue(encoding["alpha_was_opaque"])
            self.assertFalse(encoding["rgb_samples_modified"])
            offset = len(capture.PNG_SIGNATURE)
            image_data = b""
            while offset < len(data):
                length = struct.unpack(">I", data[offset:offset + 4])[0]
                chunk_type = data[offset + 4:offset + 8]
                start = offset + 8
                if chunk_type == b"IDAT":
                    image_data += data[start:start + length]
                offset = start + length + 4
                if chunk_type == b"IEND":
                    break
            self.assertEqual(b"\0\x0c\x22\x38", zlib.decompress(image_data))

    def test_rejects_transparent_rgba_without_changing_source_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capture.png"
            source = self.make_rgba_png(path, alpha=254)

            with self.assertRaisesRegex(ValueError, "non-opaque pixels"):
                capture.normalize_opaque_rgb_png(path)

            self.assertEqual(source, path.read_bytes())

    def test_single_ipad_capture_is_not_reported_as_an_iphone_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            capture.write_capture_manifest(output, "iOS and iPadOS", [{
                "build_defines": capture.IOS_MAC_STORE_DEFINES,
                "build_configuration": "Debug Simulator",
                "capture_scene": "Post-onboarding local Files and Books screen.",
                "capture_tools_sha": "a" * 40,
                "device": {"device_name": "iPad Pro 13-inch (M5)"},
            }])
            manifest = json.loads((output / "capture-provenance.json").read_text(encoding="utf-8"))

        self.assertEqual("iOS and iPadOS", manifest["platform"])
        self.assertIn("on iPad.", manifest["qualification"])
        self.assertNotIn("iPhone", manifest["qualification"])

    def test_both_ios_device_families_are_reported_when_both_are_captured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            captures = [{
                "build_defines": capture.IOS_MAC_STORE_DEFINES,
                "build_configuration": "Debug Simulator",
                "capture_scene": "Post-onboarding local Files and Books screen.",
                "capture_tools_sha": "a" * 40,
                "device": {"device_name": device_name},
            } for device_name in ("iPhone 17 Pro Max", "iPad Pro 13-inch (M5)")]
            capture.write_capture_manifest(output, "iOS and iPadOS", captures)
            manifest = json.loads((output / "capture-provenance.json").read_text(encoding="utf-8"))

        self.assertIn("on iPhone and iPad.", manifest["qualification"])

    def test_ipad_producer_option_selects_only_ipad(self) -> None:
        argv = [
            "capture_apple_store_screenshots.py", "capture-ios-in-use",
            "--product-root", "product", "--app", "Pleazyng.app",
            "--build-receipt", "receipt.json", "--simulator-entitlements", "sim.entitlements",
            "--extension-entitlements", "extension.entitlements",
            "--device-category", "ipad", "--localtest-trial", "--output-dir", "output",
        ]
        with patch("sys.argv", argv):
            parsed = capture.parse_args()

        self.assertEqual("ipad", parsed.device_category)
        self.assertTrue(parsed.localtest_trial)

    def test_trial_receipt_adapter_preserves_original_producer_fields(self) -> None:
        original = {
            "source_sha": "a" * 40,
            "source_tag": f"v3.0.51-candidate.{'a' * 40}",
            "product_version": "3.0.51",
            "product_build": 210,
            "app_bundle_sha256": "b" * 64,
            "producer_run_id": "45569185714",
        }
        app_identity = {"bundle_id": "org.snapetech.pleazyng", "version": "3.0.51"}
        receipt_digest = "c" * 64
        adapted = capture.adapt_apple_store_receipt_for_trial_capture(
            original,
            product_app_identity=app_identity,
            receipt_digest=receipt_digest,
            product_root=Path("/product-source"),
        )

        self.assertEqual(original["source_sha"], adapted["product_source_sha"])
        self.assertEqual(original["source_tag"], adapted["product_source_tag"])
        self.assertEqual(original["app_bundle_sha256"], adapted["product_app_bundle_sha256"])
        self.assertEqual(app_identity, adapted["product_app"])
        self.assertEqual(original["producer_run_id"], adapted["producer_run_id"])
        self.assertEqual(receipt_digest, adapted["capture_receipt_adapter"]["original_receipt_sha256"])

    def test_capture_manifest_qualifies_localtest_trial_without_overstating_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            capture.write_capture_manifest(output, "iOS and iPadOS", [{
                "filename": "ipad-01-files-and-books.png",
                "build_defines": capture.IOS_MAC_STORE_DEFINES,
                "build_configuration": "Debug Simulator",
                "capture_scene": "Post-onboarding local Files and Books screen after LOCALTEST.",
                "capture_tools_sha": "a" * 40,
                "device": {"device_name": "iPad Pro 13-inch (M5)"},
                "storekit_localtest": {"environment": "LOCALTEST"},
            }])
            manifest = json.loads((output / "capture-provenance.json").read_text(encoding="utf-8"))

        self.assertIn("isolated Xcode StoreKitTest LOCALTEST transaction", manifest["qualification"])
        self.assertIn("does not qualify App Store Sandbox", manifest["qualification"])


class MacListingCaptureTests(unittest.TestCase):
    def test_mac_listing_capture_uses_native_display_window_and_visible_ocr(self) -> None:
        self.assertIn("CGCompleteDisplayConfiguration(config, .forSession)", MACOS_SELECT_WINDOW_CAPTURE_DISPLAY_SWIFT)
        self.assertIn("1920x1080", MACOS_SELECT_WINDOW_CAPTURE_DISPLAY_SWIFT)
        self.assertIn('"target_native_window_points": [1280, 800]', MACOS_SELECT_WINDOW_CAPTURE_DISPLAY_SWIFT)
        self.assertIn("CGWindowListCopyWindowInfo", MACOS_WINDOW_BOUNDS_SWIFT)
        self.assertIn("VNRecognizeTextRequest()", MACOS_LISTING_OCR_SWIFT)
        self.assertIn("normalized_bounds", MACOS_LISTING_OCR_SWIFT)
        self.assertIn('"recognized_lines": lines', MACOS_LISTING_OCR_SWIFT)
        self.assertIn("CGEvent(mouseEventSource", MACOS_POST_NATIVE_CLICK_SWIFT)
        self.assertNotIn("System Events", MACOS_POST_NATIVE_CLICK_SWIFT)
        self.assertIn("CGDisplayBounds", MACOS_LIVE_DISPLAY_STATE_SWIFT)
        self.assertIn("NSWindow Frame ", capture.write_macos_main_window_frame_preference.__code__.co_consts)
        self.assertIn("use files and books", capture.require_macos_listing_markers.__code__.co_consts)
        self.assertIn("import files", capture.require_macos_listing_markers.__code__.co_consts)
        self.assertIn("/usr/sbin/screencapture", capture.run_macos_listing_window_capture.__code__.co_consts)
        self.assertNotIn("StoreKit", MACOS_LISTING_OCR_SWIFT)
        self.assertNotIn("XCUITest", capture.run_macos_listing_window_capture.__code__.co_consts)

    def test_appkit_saved_frame_request_is_written_and_read_back_before_launch(self) -> None:
        with patch.object(capture, "run", side_effect=[None, "{{0, 0}, {1280, 800}}"]):
            evidence = capture.write_macos_main_window_frame_preference(
                width=1280, height=800, x=0, y=0
            )

        self.assertEqual("{{0, 0}, {1280, 800}}", evidence["saved_frame_string"])
        self.assertTrue(evidence["written_before_app_launch"])

    def test_mac_listing_markers_match_normalized_vision_case_and_spacing(self) -> None:
        capture.require_macos_listing_markers({"recognized_text": "the START\n FRESH action"}, before_navigation=True)
        capture.require_macos_listing_markers(
            {"recognized_text": "IMPORT FILES  Add source"}, before_navigation=False
        )
        capture.require_macos_listing_markers(
            {"recognized_text": "FILESAND BOOKS Search saved library Add a source or import a book"},
            before_navigation=False,
        )

    def test_mac_native_click_point_comes_from_visible_ocr_inside_product_window(self) -> None:
        evidence = {
            "image_pixels": [1024, 768],
            "recognized_regions": [{
                "text": "Use files and books",
                "confidence": 0.98,
                "normalized_bounds": [0.4, 0.48, 0.2, 0.04],
            }],
        }
        point = capture.locate_macos_screen_text(
            evidence,
            "Use files and books",
            window_bounds=[112, 52, 800, 632],
            display_points=[1024, 768],
        )
        self.assertEqual([512.0, 384.0], point["screen_point"])

        with self.assertRaisesRegex(ValueError, "inside the live Pleazyng window"):
            capture.locate_macos_screen_text(
                evidence,
                "Use files and books",
                window_bounds=[700, 500, 300, 200],
                display_points=[1024, 768],
            )

    def test_mac_listing_export_keeps_accepted_dimensions_and_rgb_provenance(self) -> None:
        self.assertIn("normalize_opaque_rgb_png", capture.run_macos_listing_window_capture.__code__.co_names)
        self.assertIn("screenshot_resized_or_padded", capture.run_macos_listing_window_capture.__code__.co_consts)
        self.assertIn(
            ((1280, 800), (1440, 900), (2560, 1600), (2880, 1800)),
            capture.capture_macos_listing_window.__code__.co_consts,
        )


if __name__ == "__main__":
    unittest.main()
