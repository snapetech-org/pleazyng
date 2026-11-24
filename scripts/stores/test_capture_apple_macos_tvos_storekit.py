#!/usr/bin/env python3
"""Focused guards for retained Mac/tvOS LOCALTEST screenshot capture."""

from __future__ import annotations

import ast
import json
import plistlib
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import capture_apple_macos_tvos_storekit as capture


ROOT = Path(__file__).resolve().parents[2]
UI_TEST = (ROOT / capture.UI_TEST_SOURCE).read_text(encoding="utf-8")
STOREKIT = (ROOT / capture.STOREKIT_CONFIG).read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github/workflows/apple-store-screenshots.yml").read_text(encoding="utf-8")


class StoreKitReviewCaptureTests(unittest.TestCase):
    def test_capture_tools_helper_references_exist(self) -> None:
        tree = ast.parse(Path(capture.__file__).read_text(encoding="utf-8"))
        names = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "capture_tools"
        }
        self.assertTrue(names)
        missing = sorted(name for name in names if not hasattr(capture.capture_tools, name))
        self.assertEqual(missing, [])

    def test_build_identity_uses_the_producer_receipt_schema_version_fields(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = "snapetech-org/pleazyng"
            for platform in ("macos", "tvos"):
                with self.subTest(platform=platform):
                    app = root / capture.PLATFORM[platform]["app_bundle"]
                    executable = "Pleazyng" if platform == "macos" else "Runner"
                    info_path = app / "Contents" / "Info.plist" if platform == "macos" else app / "Info.plist"
                    info_path.parent.mkdir(parents=True)
                    info_path.write_bytes(plistlib.dumps({
                        "CFBundleIdentifier": capture.BUNDLE_ID,
                        "CFBundleShortVersionString": capture.VERSION,
                        "CFBundleVersion": capture.BUILD_NUMBER,
                        "CFBundleExecutable": executable,
                    }))
                    app_hash = capture.capture_tools.sha256_tree(app)
                    receipt = {
                        "schema_version": 1,
                        "product": "Pleazyng",
                        "source_tag": capture.SOURCE_TAG,
                        "source_sha": capture.SOURCE_SHA,
                        "capture_tools_sha": "e6d6c24dbe612c156f583969d05d4fd19b8b3874",
                        "producer_repository": repository,
                        "producer_workflow_ref": f"{repository}/{capture.capture_tools.WORKFLOW_PATH}@refs/heads/main",
                        "producer_run_id": "44062395003",
                        "producer_run_attempt": "1",
                        "product_version": capture.VERSION,
                        "product_build": capture.BUILD_NUMBER,
                        "platform": "macOS" if platform == "macos" else "tvOS Simulator",
                        "build_mode": "App Store IAP",
                        "build_configuration": capture.capture_tools.BUILD_CONFIGURATION[platform],
                        "app_bundle_name": app.name,
                        "bundle_id": capture.BUNDLE_ID,
                        "executable": executable,
                        "architectures": ["arm64", "x86_64"] if platform == "macos" else ["arm64"],
                        "build_host_architecture": "X64",
                        "build_defines": (
                            capture.capture_tools.IOS_MAC_STORE_DEFINES
                            if platform == "macos" else capture.capture_tools.TVOS_STORE_DEFINES
                        ),
                        "app_bundle_sha256": app_hash,
                        "distribution_signing": "disabled",
                        "qualification": "Compiled application bundle only; not a signed or uploadable store product.",
                    }
                    receipt_path = root / f"{platform}-build-provenance.json"
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

                    with patch.dict("os.environ", {"GITHUB_REPOSITORY": repository}):
                        validated_receipt, identity = capture.verify_build_identity(
                            platform, app, receipt_path, "44062395003", "1"
                        )

                    self.assertEqual(validated_receipt, receipt)
                    self.assertEqual(identity["version"], capture.VERSION)
                    self.assertEqual(identity["build_number"], capture.BUILD_NUMBER)
                    self.assertEqual(identity["executable"], executable)

    def test_platform_targets_use_the_native_api_minimums_and_only_test_resources(self) -> None:
        macos = capture.ui_test_project_spec("macos")
        tvos = capture.ui_test_project_spec("tvos")
        for platform, spec, minimum in (("macOS", macos, "14.0"), ("tvOS", tvos, "17.0")):
            target = spec["targets"][capture.TEST_TARGET]
            self.assertEqual(target["platform"], platform)
            self.assertEqual(spec["options"]["deploymentTarget"], {platform: minimum})
            self.assertEqual(target["type"], "bundle.ui-testing")
            self.assertEqual(target["sources"][1]["path"], capture.STOREKIT_CONFIG.name)
            self.assertEqual(spec["schemes"]["PleazyngStoreKitLocalTrialReview"]["test"]["targets"], [capture.TEST_TARGET])
            self.assertEqual(
                spec["schemes"]["PleazyngStoreKitLocalTrialReview"]["run"]["storeKitConfiguration"],
                capture.STOREKIT_CONFIG.name,
            )
        self.assertIn("pleazyng_7_day_trial", STOREKIT)
        self.assertIn("pleazyng_lifetime_unlock", STOREKIT)

    def test_generated_scheme_points_to_the_exact_localtest_catalog(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / capture.STOREKIT_CONFIG.name
            config.write_text(STOREKIT, encoding="utf-8")
            scheme = root / "Capture.xcodeproj" / "xcshareddata" / "xcschemes" / "Capture.xcscheme"
            scheme.parent.mkdir(parents=True)
            scheme.write_text(
                '<Scheme><LaunchAction><StoreKitConfigurationFileReference '
                'identifier="../../pleazyng-trial-review.storekit"/></LaunchAction></Scheme>',
                encoding="utf-8",
            )
            evidence = capture.verify_scheme_storekit_configuration(scheme, root)
            self.assertEqual(evidence["storekit_configuration_path"], config.name)
            self.assertEqual(evidence["storekit_configuration_sha256"], capture.sha256_file(config))

            scheme.write_text(
                '<Scheme><LaunchAction><StoreKitConfigurationFileReference '
                'identifier="../../another.storekit"/></LaunchAction></Scheme>',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "different or missing"):
                capture.verify_scheme_storekit_configuration(scheme, root)

    def test_tvos_simulator_selects_canonical_runtime_not_device_variant(self) -> None:
        canonical_id = "com.apple.CoreSimulator.SimRuntime.tvOS-27-2"
        inventory = {
            "runtimes": [
                {"identifier": canonical_id, "version": "27.2", "isAvailable": True},
                {
                    "identifier": canonical_id + "-AppleTV14-1",
                    "version": "27.2",
                    "isAvailable": True,
                },
            ]
        }
        selected = capture.select_canonical_tvos_runtime(inventory, (27, 2))
        self.assertEqual(selected["identifier"], canonical_id)

        with self.assertRaisesRegex(RuntimeError, "canonical tvOS runtime"):
            capture.select_canonical_tvos_runtime(
                {"runtimes": [inventory["runtimes"][1]]}, (27, 2)
            )

    def test_tvos_runtime_inventory_is_retained_before_canonical_validation(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            raw_inventory = json.dumps({
                "runtimes": [{
                    "identifier": "com.apple.CoreSimulator.SimRuntime.tvOS-27-2-AppleTV14-1",
                    "version": "27.2",
                    "isAvailable": True,
                }]
            })
            with patch.object(capture, "command", side_effect=["27.2", raw_inventory]) as command:
                with self.assertRaisesRegex(RuntimeError, "canonical tvOS runtime"):
                    capture.create_tvos_simulator("123", "1", output)
            retained = output / "simulator-runtime-inventory.json"
            self.assertTrue(retained.is_file())
            self.assertEqual(json.loads(retained.read_text()), json.loads(raw_inventory))
            self.assertEqual(command.call_count, 2)

    def test_tvos_simulator_is_rebooted_after_runner_build_when_it_shutdown(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            simulator = {
                "udid": "01234567-89AB-CDEF-0123-456789ABCDEF",
                "name": "Pleazyng-test-tvos",
                "runtime_id": "com.apple.CoreSimulator.SimRuntime.tvOS-27-2",
                "device_type": "com.apple.CoreSimulator.SimDeviceType.Apple-TV-4K-3rd-generation-4K",
            }

            def inventory(state: str) -> str:
                return json.dumps({
                    "devices": {
                        simulator["runtime_id"]: [{
                            "udid": simulator["udid"],
                            "name": simulator["name"],
                            "deviceTypeIdentifier": simulator["device_type"],
                            "state": state,
                        }]
                    }
                })

            with patch.object(
                capture,
                "command",
                side_effect=[inventory("Shutdown"), "", "", "PID\tStatus\tLabel\n", inventory("Booted")],
            ) as command:
                evidence = capture.ensure_tvos_simulator_ready_for_install(simulator, output)

            invocations = [call.args[0] for call in command.call_args_list]
            self.assertEqual(invocations[1], ["xcrun", "simctl", "boot", simulator["udid"]])
            self.assertEqual(invocations[2], ["xcrun", "simctl", "bootstatus", simulator["udid"], "-b"])
            self.assertIn("launchctl", invocations[3])
            self.assertEqual(evidence["state_after_xctest_build"], "Shutdown")
            self.assertEqual(evidence["state_before_install"], "Booted")
            self.assertEqual(evidence["readiness_action"], "booted-after-xcodebuild")
            self.assertTrue((output / "simulator-state-before-app-install.json").is_file())
            self.assertTrue((output / "simulator-state-before-app-install-ready.json").is_file())
            self.assertTrue((output / "simulator-launchctl-list.txt").is_file())

    def test_tvos_simulator_already_booted_after_runner_build_is_not_reset(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            simulator = {
                "udid": "01234567-89AB-CDEF-0123-456789ABCDEF",
                "name": "Pleazyng-test-tvos",
                "runtime_id": "com.apple.CoreSimulator.SimRuntime.tvOS-27-2",
                "device_type": "com.apple.CoreSimulator.SimDeviceType.Apple-TV-4K-3rd-generation-4K",
            }

            def inventory() -> str:
                return json.dumps({
                    "devices": {
                        simulator["runtime_id"]: [{
                            "udid": simulator["udid"],
                            "name": simulator["name"],
                            "deviceTypeIdentifier": simulator["device_type"],
                            "state": "Booted",
                        }]
                    }
                })

            with patch.object(
                capture, "command", side_effect=[inventory(), "PID\tStatus\tLabel\n", inventory()]
            ) as command:
                evidence = capture.ensure_tvos_simulator_ready_for_install(simulator, output)

            invocations = [call.args[0] for call in command.call_args_list]
            self.assertFalse(any("boot" in invocation[2:] for invocation in invocations))
            self.assertEqual(evidence["readiness_action"], "already-booted-after-xcodebuild")
            self.assertEqual(evidence["state_before_install"], "Booted")

    def test_tvos_direct_launch_diagnostic_binds_pid_logs_and_receipt_to_exact_installed_app(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            simulator = {
                "udid": "01234567-89AB-CDEF-0123-456789ABCDEF",
                "name": "Pleazyng-test-tvos",
                "runtime_id": "com.apple.CoreSimulator.SimRuntime.tvOS-27-2",
                "runtime_version": "27.2",
                "device_type": "com.apple.CoreSimulator.SimDeviceType.Apple-TV-4K-3rd-generation-4K",
            }
            app_hash = "a" * 64
            installed = {"installed_app_bundle_sha256": app_hash}
            identity = {
                "bundle_id": capture.BUNDLE_ID,
                "version": capture.VERSION,
                "build_number": capture.BUILD_NUMBER,
                "executable": "Runner",
            }
            invocations: list[tuple[str, list[str]]] = []

            def diagnostic_command(directory: Path, name: str, args: list[str], *, timeout: int = 60):
                invocations.append((name, args))
                stdout = ""
                returncode = 0
                if name == "simctl_launch":
                    stdout = f"{capture.BUNDLE_ID}: 1234\n"
                elif name == "launchctl_after":
                    stdout = f"1234\t0\tUIKitApplication:{capture.BUNDLE_ID}[a0e8][rb-legacy]\n"
                elif name == "product_process_after_terminate":
                    stdout = f"-\t0\tUIKitApplication:{capture.BUNDLE_ID}[a0e8][rb-legacy]\n"
                stdout_path = directory / f"{name}.stdout.txt"
                stderr_path = directory / f"{name}.stderr.txt"
                stdout_path.write_text(stdout, encoding="utf-8")
                stderr_path.write_text("", encoding="utf-8")
                return {
                    "command": args,
                    "returncode": returncode,
                    "timed_out": False,
                    "error": None,
                    "stdout_path": stdout_path.name,
                    "stdout_sha256": capture.sha256_file(stdout_path),
                    "stdout_size_bytes": stdout_path.stat().st_size,
                    "stderr_path": stderr_path.name,
                    "stderr_sha256": capture.sha256_file(stderr_path),
                    "stderr_size_bytes": 0,
                }

            with (
                patch.object(capture, "diagnostic_command", side_effect=diagnostic_command),
                patch.object(capture, "copy_recent_tvos_crash_reports", return_value=[]),
                patch.object(capture.time, "sleep"),
            ):
                evidence = capture.diagnose_tvos_retained_app(
                    simulator, installed, identity, app_hash, output
                )

            self.assertTrue(evidence["ready_for_xctest"])
            self.assertEqual(evidence["product_pid"], 1234)
            self.assertTrue(evidence["process_alive_after_launch"])
            self.assertFalse(evidence["process_alive_after_terminate"])
            launch_call = next(args for name, args in invocations if name == "simctl_launch")
            self.assertEqual(launch_call, ["xcrun", "simctl", "launch", simulator["udid"], capture.BUNDLE_ID])
            log_call = next(args for name, args in invocations if name == "unified_log_after_launch")
            self.assertIn("processIdentifier == 1234", log_call[-1])
            receipt_path = output / evidence["receipt_path"]
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["product"]["app_bundle_sha256"], app_hash)
            self.assertTrue((output / "tvos-pre-xctest-diagnostic/simctl_launch.stdout.txt").is_file())

    def test_tvos_direct_launch_diagnostic_rejects_wrong_installed_bundle_hash(self) -> None:
        with TemporaryDirectory() as temporary:
            simulator = {"udid": "01234567-89AB-CDEF-0123-456789ABCDEF"}
            installed = {"installed_app_bundle_sha256": "b" * 64}
            identity = {"bundle_id": capture.BUNDLE_ID}
            with self.assertRaisesRegex(ValueError, "exact installed producer app hash"):
                capture.diagnose_tvos_retained_app(
                    simulator, installed, identity, "a" * 64, Path(temporary)
                )

    def test_tvos_direct_launch_pid_parser_requires_unique_exact_bundle_id(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "launch.txt"
            output.write_text(f"{capture.BUNDLE_ID}: 1234\n", encoding="utf-8")
            self.assertEqual(capture.simctl_launch_pid(output), 1234)
            output.write_text(f"org.example.other: 1234\n", encoding="utf-8")
            self.assertIsNone(capture.simctl_launch_pid(output))
            output.write_text(
                f"{capture.BUNDLE_ID}: 1234\n{capture.BUNDLE_ID}: 5678\n", encoding="utf-8"
            )
            self.assertIsNone(capture.simctl_launch_pid(output))

    def test_association_binds_each_target_to_the_exact_retained_product_path(self) -> None:
        for platform in ("macos", "tvos"):
            with self.subTest(platform=platform), TemporaryDirectory() as temporary:
                path = Path(temporary) / "capture.xctestrun"
                target = {
                    "BlueprintName": capture.TEST_TARGET,
                    "IsUITestBundle": True,
                    "TestBundlePath": "__TESTHOST__/PlugIns/Review.xctest",
                    "DependentProductPaths": ["__TESTROOT__/Review-Runner.app"],
                    "UseUITargetAppProvidedByTests": True,
                }
                path.write_bytes(plistlib.dumps({"TestConfigurations": [{"TestTargets": [target]}]}))
                evidence = capture.associate_xctestrun_with_retained_app(path, platform)
                patched = plistlib.loads(path.read_bytes())["TestConfigurations"][0]["TestTargets"][0]
                expected = capture.PLATFORM[platform]["app_relative_path"]
                self.assertEqual(patched["UITargetAppPath"], expected)
                self.assertIn(expected, patched["DependentProductPaths"])
                self.assertNotIn("UseUITargetAppProvidedByTests", patched)
                self.assertEqual(evidence["ui_target_app_path"], expected)

    def test_xctestrun_association_rejects_wrong_or_ambiguous_targets(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.xctestrun"
            path.write_bytes(plistlib.dumps({"OtherUITests": {"BlueprintName": "OtherUITests", "IsUITestBundle": True}}))
            with self.assertRaisesRegex(ValueError, "found 0"):
                capture.associate_xctestrun_with_retained_app(path, "tvos")

            target = {"BlueprintName": capture.TEST_TARGET, "IsUITestBundle": True}
            path.write_bytes(plistlib.dumps({"TestConfigurations": [{"TestTargets": [target, dict(target)]}]}))
            with self.assertRaisesRegex(ValueError, "found 2"):
                capture.associate_xctestrun_with_retained_app(path, "macos")

            target = {"BlueprintName": capture.TEST_TARGET, "IsUITestBundle": True}
            path.write_bytes(plistlib.dumps({"UseDestinationArtifacts": True, capture.TEST_TARGET: target}))
            with self.assertRaisesRegex(ValueError, "TestROOT plan"):
                capture.associate_xctestrun_with_retained_app(path, "macos")

    def test_generated_mac_plan_resolves_exact_nested_host_bundle_and_catalog(self) -> None:
        storekit_resource = ROOT / capture.STOREKIT_CONFIG
        config_sha = capture.sha256_file(storekit_resource)
        with TemporaryDirectory() as temporary:
            products = Path(temporary) / "Build" / "Products"
            host = products / "Debug" / f"{capture.TEST_TARGET}-Runner.app"
            bundle = host / "Contents" / "PlugIns" / f"{capture.TEST_TARGET}.xctest"
            (bundle / "Contents" / "Resources").mkdir(parents=True)
            (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": capture.TEST_BUNDLE_ID,
            }))
            resource = bundle / "Contents" / "Resources" / storekit_resource.name
            resource.write_bytes(storekit_resource.read_bytes())
            (host / "Contents").mkdir(parents=True, exist_ok=True)

            # The hosted Xcode build log proves this exact Mac XCTest nesting.
            # Its macro pair follows Apple's documented TestHostPath contract;
            # this fixture is explicit because the earlier failure preceded
            # preservation of the Mac's generated .xctestrun file.
            target = {
                "BlueprintName": capture.TEST_TARGET,
                "IsUITestBundle": True,
                "TestBundleIdentifier": capture.TEST_BUNDLE_ID,
                "TestBundlePath": f"__TESTHOST__/Contents/PlugIns/{capture.TEST_TARGET}.xctest",
                "TestHostPath": f"__TESTROOT__/Debug/{capture.TEST_TARGET}-Runner.app",
            }
            plan = products / "capture.xctestrun"
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_bytes(plistlib.dumps({capture.TEST_TARGET: target}))

            resolved_bundle, resolved_target = capture.resolve_test_bundle_from_plan(plan, products)
            identity = capture.verify_test_bundle_identity(resolved_bundle)
            resolved_resource = capture.verify_storekit_resource(resolved_bundle, config_sha)

            self.assertEqual(resolved_bundle, bundle)
            self.assertEqual(resolved_target["BlueprintName"], capture.TEST_TARGET)
            self.assertEqual(identity["bundle_id"], capture.TEST_BUNDLE_ID)
            self.assertEqual(resolved_resource, resource)

    def test_retained_tvos_bundle_layout_resolves_from_plan_host_path(self) -> None:
        storekit_resource = ROOT / capture.STOREKIT_CONFIG
        config_sha = capture.sha256_file(storekit_resource)
        with TemporaryDirectory() as temporary:
            products = Path(temporary) / "Build" / "Products"
            host = products / "Debug-appletvsimulator" / f"{capture.TEST_TARGET}-Runner.app"
            testhost_bundle = host / "PlugIns" / f"{capture.TEST_TARGET}.xctest"
            (testhost_bundle / "Info.plist").parent.mkdir(parents=True)
            (testhost_bundle / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": capture.TEST_BUNDLE_ID,
            }))
            resource = testhost_bundle / storekit_resource.name
            resource.write_bytes(storekit_resource.read_bytes())

            # Xcode's actual tvOS build log places the XCTest bundle here and
            # copies the catalog into it. Its exact .xctestrun macro pair was
            # not retained by the earlier failing helper, so this plan object
            # is explicitly a synthesized fixture following Apple's documented
            # TestHostPath/TestBundlePath contract.
            target = {
                "BlueprintName": capture.TEST_TARGET,
                "IsUITestBundle": True,
                "TestBundlePath": f"__TESTHOST__/PlugIns/{capture.TEST_TARGET}.xctest",
                "TestHostPath": f"__TESTROOT__/Debug-appletvsimulator/{capture.TEST_TARGET}-Runner.app",
            }
            plan = products / "tvos-capture.xctestrun"
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_bytes(plistlib.dumps({capture.TEST_TARGET: target}))
            resolved_bundle, resolved_target = capture.resolve_test_bundle_from_plan(plan, products)

            self.assertEqual(resolved_bundle, testhost_bundle)
            self.assertEqual(resolved_target["TestBundlePath"], target["TestBundlePath"])
            self.assertEqual(capture.verify_test_bundle_identity(resolved_bundle)["bundle_id"], capture.TEST_BUNDLE_ID)
            self.assertEqual(capture.verify_storekit_resource(resolved_bundle, config_sha), resource)

    def test_testroot_test_bundle_reference_resolves_as_a_synthetic_macro_case(self) -> None:
        with TemporaryDirectory() as temporary:
            products = Path(temporary) / "Build" / "Products"
            host = products / "Debug" / "Runner.app"
            bundle = products / "Debug" / f"{capture.TEST_TARGET}.xctest"
            bundle.mkdir(parents=True)
            host.mkdir(parents=True)
            (bundle / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": capture.TEST_BUNDLE_ID,
            }))
            target = {
                "BlueprintName": capture.TEST_TARGET,
                "IsUITestBundle": True,
                "TestBundlePath": f"__TESTROOT__/Debug/{capture.TEST_TARGET}.xctest",
                "TestHostPath": "__TESTROOT__/Debug/Runner.app",
            }
            plan = products / "testroot-reference.xctestrun"
            plan.write_bytes(plistlib.dumps({capture.TEST_TARGET: target}))
            resolved_bundle, _ = capture.resolve_test_bundle_from_plan(plan, products)
            self.assertEqual(resolved_bundle, bundle)

    def test_xctestrun_bundle_resolution_rejects_unknown_macros_escapes_and_wrong_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            products = Path(temporary) / "Build" / "Products"
            products.mkdir(parents=True)
            (products / "Debug" / "Runner.app").mkdir(parents=True)
            plan = products / "capture.xctestrun"
            base_target = {
                "BlueprintName": capture.TEST_TARGET,
                "IsUITestBundle": True,
                "TestBundlePath": "__TESTROOT__/Debug/Test.xctest",
                "TestHostPath": "__TESTROOT__/Debug/Runner.app",
            }
            for field, value, error in (
                ("TestBundlePath", "__UNKNOWN__/Runner/PlugIns/" + f"{capture.TEST_TARGET}.xctest", "unsupported"),
                ("TestBundlePath", "__TESTROOT__/Debug/__unknown__/" + f"{capture.TEST_TARGET}.xctest", "unsupported"),
                ("TestBundlePath", "__TESTROOT__/../escape/" + f"{capture.TEST_TARGET}.xctest", "escaping"),
                ("TestHostPath", "__TESTROOT__/Debug/../Runner.app", "escaping"),
            ):
                target = {**base_target, field: value}
                if field == "TestBundlePath" and "Test.xctest" in value:
                    target[field] = f"__TESTROOT__/Debug/{capture.TEST_TARGET}.xctest"
                plan.write_bytes(plistlib.dumps({capture.TEST_TARGET: target}))
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, error):
                    capture.resolve_test_bundle_from_plan(plan, products)

            wrong_identifier = {**base_target, "TestBundleIdentifier": "org.example.wrong"}
            plan.write_bytes(plistlib.dumps({capture.TEST_TARGET: wrong_identifier}))
            with self.assertRaisesRegex(ValueError, "unexpected test bundle identifier"):
                capture.resolve_test_bundle_from_plan(plan, products)

    def test_stage_app_for_xctestrun_copies_the_receipt_verified_bundle_unchanged(self) -> None:
        for platform in ("macos", "tvos"):
            with self.subTest(platform=platform), TemporaryDirectory() as temporary:
                root = Path(temporary)
                app = root / capture.PLATFORM[platform]["app_bundle"]
                app.mkdir()
                executable = "Runner" if platform == "tvos" else "Pleazyng"
                (app / "Info.plist").write_bytes(plistlib.dumps({
                    "CFBundleIdentifier": capture.BUNDLE_ID,
                    "CFBundleShortVersionString": capture.VERSION,
                    "CFBundleVersion": capture.BUILD_NUMBER,
                    "CFBundleExecutable": executable,
                }))
                (app / executable).write_bytes(b"receipt-verified app executable")
                products = root / "Build" / "Products"
                products.mkdir(parents=True)
                expected_hash = capture.capture_tools.sha256_tree(app)
                staged, evidence = capture.stage_app_for_xctestrun(
                    app, products, platform, {"app_bundle_sha256": expected_hash}
                )
                self.assertEqual(capture.capture_tools.sha256_tree(staged), expected_hash)
                self.assertEqual(staged.relative_to(products).as_posix(),
                    f"{capture.PLATFORM[platform]['products_directory']}/{capture.PLATFORM[platform]['app_bundle']}")
                self.assertEqual(evidence["app_path"], capture.PLATFORM[platform]["app_relative_path"])

                with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
                    capture.stage_app_for_xctestrun(app, products, platform, {"app_bundle_sha256": expected_hash})

    def test_localtest_record_requires_only_a_completed_trial_and_actual_app_access(self) -> None:
        record = {
            "environment": "LOCALTEST",
            "catalog_queries": {
                "trial_product_id": capture.TRIAL_PRODUCT_ID,
                "lifetime_product_id": capture.LIFETIME_PRODUCT_ID,
                "lifetime_display_price": "Unlock every feature $2.99",
            },
            "history": {
                "beforeAppLaunch": {"transactionCount": 0},
                "afterProductTrialCTA": {"transactionCount": 1},
            },
            "transactions": [{
                "product_id": capture.TRIAL_PRODUCT_ID,
                "state": "purchased",
                "transaction_id": "11",
                "original_transaction_id": "9",
                "purchase_date": "2026-11-17T12:00:00Z",
            }],
            "app_behavior": {
                "trial_offer_shown": True,
                "trial_started_via_product_ui": True,
                "transaction_created_outside_product_app_ui": False,
                "active_trial_restored_by_app": True,
                "files_and_books_screen_opened": True,
                "active_trial_sheet_shown": True,
                "lifetime_purchase_activated": False,
                "lifetime_transaction_created": False,
            },
            "raw_jws_included": False,
        }
        self.assertEqual(capture.validate_localtest_record(record), record["transactions"][0])

        lifetime = {**record, "transactions": [dict(record["transactions"][0], product_id=capture.LIFETIME_PRODUCT_ID)]}
        with self.assertRaisesRegex(ValueError, "free-trial SKU"):
            capture.validate_localtest_record(lifetime)

        unverified = {**record, "app_behavior": {**record["app_behavior"], "active_trial_restored_by_app": False}}
        with self.assertRaisesRegex(ValueError, "did not report"):
            capture.validate_localtest_record(unverified)

    def test_product_ui_starts_local_trial_and_never_buys_lifetime(self) -> None:
        self.assertIn("import StoreKitTest", UI_TEST)
        self.assertIn("SKTestSession(contentsOf: configurationURL)", UI_TEST)
        self.assertIn("func testCaptureTrialBackedFilesBooksAndAccessSheet() throws {", UI_TEST)
        self.assertNotIn("func testCaptureTrialBackedFilesBooksAndAccessSheet() async", UI_TEST)
        tv_offer = UI_TEST.index('attachmentName: "pleazyng-localtest-trial-offer-tvos"')
        first_remote_select = UI_TEST.index("XCUIRemote.shared.press(.select)")
        transaction_check = UI_TEST.index("let trialTransactions = session.allTransactions()")
        self.assertLess(UI_TEST.index("app.launch()"), tv_offer)
        self.assertLess(tv_offer, first_remote_select)
        self.assertLess(first_remote_select, transaction_check)
        self.assertNotIn("session.buyProduct", UI_TEST)
        self.assertNotIn("buyProduct(identifier: lifetimeProductID", UI_TEST)
        self.assertNotIn("lifetimePrice.tap()", UI_TEST)
        self.assertIn('"transaction_created_outside_product_app_ui": false', UI_TEST)
        self.assertIn('"lifetime_transaction_created": false', UI_TEST)
        self.assertIn("app.screenshot()", UI_TEST)

    def test_launch_diagnostics_are_attached_before_product_ui_assertions(self) -> None:
        launch = UI_TEST.index("app.launch()")
        screenshot = UI_TEST.index('named: "pleazyng-diagnostic-after-launch-\\(platformName())"')
        accessibility = UI_TEST.index('named: "pleazyng-diagnostic-accessibility-after-launch-\\(platformName())"')
        trial_assertion = UI_TEST.index('attachmentName: "pleazyng-localtest-trial-offer-tvos"')
        self.assertLess(launch, screenshot)
        self.assertLess(screenshot, accessibility)
        self.assertLess(accessibility, trial_assertion)
        self.assertIn("attachText(app.debugDescription", UI_TEST)
        self.assertIn(r'named: "pleazyng-diagnostic-\(attachmentName)-timeout"', UI_TEST)
        self.assertIn(r'named: "pleazyng-diagnostic-\(attachmentName)-accessibility"', UI_TEST)

    def test_tvos_checks_rendered_screens_and_navigates_with_the_remote(self) -> None:
        self.assertIn("let screen = try captureTVScreen(in: app)", UI_TEST)
        self.assertIn("markers.allSatisfy", UI_TEST)
        self.assertIn("hasTVRailFocus(settings.boundingBox, in: screen.image)", UI_TEST)
        self.assertIn("XCUIRemote.shared.press(.menu)", UI_TEST)
        self.assertIn('["Files and books", "Search saved library", "import a book"]', UI_TEST)
        self.assertIn("try captureTVLocalLibrary(in: app)", UI_TEST)
        self.assertIn('"pleazyng-diagnostic-files-books-arrival-tvos"', UI_TEST)
        self.assertIn('"pleazyng-diagnostic-search-keyboard-dismissed-tvos"', UI_TEST)
        self.assertIn('["SPACE", "ABC", "search"].allSatisfy', UI_TEST)
        self.assertIn('["Your 7-day full-feature trial is active", "Restore lifetime purchase", "$2.99"]', UI_TEST)
        self.assertNotIn(".hasFocus", UI_TEST)

    def test_macos_fresh_install_uses_visible_onboarding_and_bounded_ocr_fallback(self) -> None:
        self.assertIn('try tapMacVisibleText("Start fresh", in: app', UI_TEST)
        self.assertIn('"Start my 7-day free trial", in: app', UI_TEST)
        self.assertIn('"Use files and books", in: app', UI_TEST)
        self.assertIn('"Pleazyng lifetime access", in: app', UI_TEST)
        self.assertIn('"Your 7-day full-feature trial is active."', UI_TEST)
        self.assertIn("import Vision", UI_TEST)
        self.assertIn("VNRecognizeTextRequest()", UI_TEST)
        self.assertIn("button.exists && button.isHittable", UI_TEST)
        self.assertIn("guard matchingLines.count == 1", UI_TEST)
        self.assertIn("window.coordinate(withNormalizedOffset: offset).tap()", UI_TEST)
        self.assertIn("private func macWindowOffset(", UI_TEST)
        self.assertIn("forVisionBounds bounds: CGRect, geometry: MacScreenshotGeometry", UI_TEST)
        self.assertIn("XCTAssertEqual(upperOffset.dy, 0.2, accuracy: 0.0001)", UI_TEST)
        self.assertIn("XCTAssertEqual(lowerOffset.dy, 0.8, accuracy: 0.0001)", UI_TEST)
        self.assertNotIn("AXEnhancedUserInterface", UI_TEST)
        self.assertNotIn("XCUIVoiceOverService", UI_TEST)

    def test_mac_screenshot_is_app_scoped_cropped_and_geometry_recorded(self) -> None:
        self.assertRegex(
            UI_TEST,
            r"#if os\(macOS\)\s+let capture = try captureMacWindowScreenshot\(of: app\)\s+attachMacScreenshot\(capture, named: name\)\s+#else\s+let screenshot = app\.screenshot\(\)",
        )
        self.assertNotIn("window.screenshot()", UI_TEST)
        self.assertIn("let screenshot = XCUIScreen.main.screenshot()", UI_TEST)
        self.assertIn("_ = NSApplication.shared", UI_TEST)
        self.assertIn("let screenFrame = CGDisplayBounds(CGMainDisplayID())", UI_TEST)
        self.assertIn("let applicationFrame = app.frame", UI_TEST)
        self.assertIn("let windowFrame = window.frame", UI_TEST)
        self.assertIn("image.cropping(to: crop)", UI_TEST)
        self.assertIn('XCTAttachment(data: capture.pngData, uniformTypeIdentifier: "public.png")', UI_TEST)
        for field in (
            '"raw_image_pixels"',
            '"screen_frame_points"',
            '"application_frame_points"',
            '"window_frame_points"',
            '"selected_source_frame_points"',
            '"crop_rect_in_raw_image_pixels"',
            '"output_window_image_pixels"',
        ):
            with self.subTest(field=field):
                self.assertIn(field, UI_TEST)
        self.assertIn("XCTAssertEqual(retinaMapping.scaleX, 2)", UI_TEST)
        self.assertIn("try assertMacScreenshotGeometryScale()", UI_TEST)
        self.assertIn("XCTAssertThrowsError(try resolveMacScreenshotMapping(", UI_TEST)
        self.assertIn("guard matchingLines.count == 1", UI_TEST)
        self.assertIn("macWindowOffset(forVisionBounds: match.boundingBox, geometry: lines.screenshot.geometry)", UI_TEST)
        self.assertIn("window.coordinate(withNormalizedOffset: offset).tap()", UI_TEST)

    def test_mac_geometry_metadata_serializes_nonfinite_frames_as_null(self) -> None:
        self.assertIn("private func macJSONNumber(_ value: CGFloat) -> Any", UI_TEST)
        self.assertIn("number.isFinite ? number as Any : NSNull()", UI_TEST)
        self.assertIn("CGRect(x: CGFloat.infinity, y: CGFloat.nan, width: 800, height: 632)", UI_TEST)
        self.assertIn("JSONSerialization.data(withJSONObject: macRectJSON(unavailableFrame))", UI_TEST)
        self.assertIn("XCTAssertTrue(unavailableFrameValues[0] is NSNull)", UI_TEST)
        self.assertIn("XCTAssertTrue(unavailableFrameValues[1] is NSNull)", UI_TEST)

    def test_mac_ocr_uses_the_supported_cpu_devices(self) -> None:
        self.assertIn("request.recognitionLevel = .fast", UI_TEST)
        self.assertIn("try request.supportedComputeStageDevices", UI_TEST)
        self.assertIn("if case .cpu = device", UI_TEST)
        self.assertIn("request.setComputeDevice(cpu, for: stage)", UI_TEST)

    def test_tvos_screenshot_path_remains_the_existing_xctest_screenshot(self) -> None:
        self.assertIn(
            "#else\n      let screenshot = app.screenshot()\n      attachScreenshot(screenshot, named: name)\n    #endif",
            UI_TEST,
        )
        self.assertIn("#if os(tvOS)\n      return \"tvos\"", UI_TEST)

    def test_xcode_attachment_matcher_accepts_saved_trial_manifest_row(self) -> None:
        # This row is copied from run 44074874321's real xcresult export manifest.
        saved_row = {
            "suggestedHumanReadableName": (
                "pleazyng-trial-offer-localtest_0_667438CC-F976-43C1-B321-A26D0648D5F5.png"
            ),
            "exportedFileName": "E45F8765-DE1B-42C6-AE84-B669C399D1A6.png",
            "uniformTypeIdentifier": None,
            "payloadSize": None,
        }
        self.assertIs(capture.attachment_name_matches(
            saved_row["suggestedHumanReadableName"], "pleazyng-trial-offer-localtest"
        ), True)
        self.assertEqual(
            capture.unique_attachment([saved_row], "pleazyng-trial-offer-localtest"), saved_row
        )
        with self.assertRaisesRegex(RuntimeError, "found 2"):
            capture.unique_attachment([saved_row, saved_row], "pleazyng-trial-offer-localtest")

    def test_failure_diagnostics_export_named_xctest_attachments(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            result_bundle = root / "failed.xcresult"
            result_bundle.mkdir()
            png_name = "pleazyng-diagnostic-after-launch-macos_0_667438CC-F976-43C1-B321-A26D0648D5F5.png"
            text_name = (
                "pleazyng-diagnostic-accessibility-after-launch-macos_0_667438CC-F976-43C1-B321-A26D0648D5F5.txt"
            )

            def export_result_bundle(args: list[str], **_: object) -> str:
                export_dir = Path(args[args.index("--output-path") + 1])
                export_dir.mkdir(parents=True, exist_ok=True)
                header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 640, 480)
                (export_dir / "screenshot.png").write_bytes(header + b"\x00" * (16_001 - len(header)))
                (export_dir / "accessibility.txt").write_text("snapshot fixture\n", encoding="utf-8")
                manifest = [{"testIdentifier": "Test/testCapture()", "attachments": [
                    {"suggestedHumanReadableName": png_name, "exportedFileName": "screenshot.png"},
                    {"suggestedHumanReadableName": text_name, "exportedFileName": "accessibility.txt"},
                ]}]
                (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                return ""

            with patch.object(capture, "command", side_effect=export_result_bundle):
                evidence = capture.export_failure_diagnostics(result_bundle, output, "macos")

            self.assertNotIn("error", evidence)
            exported = evidence["attachments"]
            screenshot = exported["pleazyng-diagnostic-after-launch-macos"]
            accessibility = exported["pleazyng-diagnostic-accessibility-after-launch-macos"]
            self.assertEqual(screenshot["dimensions"], [640, 480])
            self.assertTrue((output / screenshot["path"]).is_file())
            self.assertTrue((output / accessibility["path"]).is_file())

    def test_platform_capture_steps_restore_exact_receipts_without_product_builds(self) -> None:
        self.assertIn("Capture tvOS app with native LOCALTEST trial history", WORKFLOW)
        self.assertIn("Capture Mac app with native LOCALTEST trial history", WORKFLOW)
        self.assertIn("--build-receipt \"$APPLE_STORE_BUILD_RECEIPT\"", WORKFLOW)
        self.assertIn("--producer-run-attempt \"$PRODUCER_RUN_ATTEMPT\"", WORKFLOW)
        self.assertIn("APPLE_STORE_BUILD_RECEIPT=$RUNNER_TEMP/apple-store-build-tvos-restored/build-provenance.json", WORKFLOW)
        self.assertIn("APPLE_STORE_BUILD_RECEIPT=$RUNNER_TEMP/apple-store-build-macos-restored/build-provenance.json", WORKFLOW)
        self.assertNotIn("buyProduct(identifier: lifetimeProductID", UI_TEST)
        self.assertIn(
            "(inputs.target == 'tvos-in-use-capture-only' || inputs.target == 'tvos-in-use-producer-only') && 'xcode-27'",
            WORKFLOW,
        )
        self.assertIn("inputs.target == 'macos-in-use-capture-only' && 'xcode-27'", WORKFLOW)

    def test_retained_capture_jobs_select_the_supported_xcode_and_matching_sdk(self) -> None:
        for platform, capture_label, sdk in (
            ("tvOS", "tvOS", "appletvsimulator"),
            ("macOS", "Mac", "macosx"),
        ):
            with self.subTest(platform=platform):
                step = f"Select supported Xcode and {platform} SDK for retained-app capture"
                self.assertIn(f"- name: {step}", WORKFLOW)
                self.assertIn(
                    f"if: inputs.target == '{platform.lower()}-in-use-capture-only'",
                    WORKFLOW,
                )
                self.assertIn("sudo xcode-select --switch /Applications/Xcode_27.2.app/Contents/Developer", WORKFLOW)
                self.assertIn("[[ \"$(xcodebuild -version | sed -n '1p')\" == 'Xcode 27.2' ]]", WORKFLOW)
                self.assertIn(f'[[ "$(xcrun --sdk {sdk} --show-sdk-version)" == \'27.2\' ]]', WORKFLOW)
                select_index = WORKFLOW.index(f"- name: {step}")
                capture_index = WORKFLOW.index(f"Capture {capture_label} app with native LOCALTEST trial history")
                self.assertLess(select_index, capture_index)


if __name__ == "__main__":
    unittest.main()
