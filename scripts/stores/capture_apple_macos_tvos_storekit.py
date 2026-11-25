#!/usr/bin/env python3
"""Capture real macOS/tvOS app screens using an app-initiated LOCALTEST trial.

The product app is restored from its immutable screenshot-producer artifact.
The temporary capture scheme and XCTest bundle use Apple's local StoreKit
configuration; the production app bundle is neither rebuilt nor rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
SCRIPTS_ROOT = SCRIPT_PATH.parents[1]
sys.path.insert(0, str(SCRIPTS_ROOT))
import capture_apple_store_screenshots as capture_tools


VERSION = capture_tools.VERSION
BUILD_NUMBER = capture_tools.BUILD_NUMBER
SOURCE_SHA = capture_tools.SOURCE_SHA
SOURCE_TAG = capture_tools.SOURCE_TAG
BUNDLE_ID = capture_tools.BUNDLE_ID
TRIAL_PRODUCT_ID = "pleazyng_7_day_trial"
LIFETIME_PRODUCT_ID = "pleazyng_lifetime_unlock"
STOREKIT_CONFIG = Path("scripts/stores/apple/pleazyng-trial-review.storekit")
UI_TEST_SOURCE = Path("scripts/stores/apple/PleazyngStoreKitLocalTrialReviewUITests.swift")
UI_TEST_SUPPORT_SOURCES = (
    Path("scripts/stores/apple/PleazyngStoreKitLocalTrialReviewFlowSupport.swift"),
    Path("scripts/stores/apple/PleazyngStoreKitLocalTrialReviewMacWindowSupport.swift"),
    Path("scripts/stores/apple/PleazyngStoreKitLocalTrialReviewTVSupport.swift"),
    Path("scripts/stores/apple/PleazyngStoreKitLocalTrialReviewMacSupport.swift"),
)
TEST_TARGET = "PleazyngStoreKitLocalTrialReviewUITests"
TEST_BUNDLE_ID = "org.snapetech.pleazyng.localtest.review-tests"
MACOS_TEST_PLAN = "PleazyngStoreKitLocalTrialReview.xctestplan"
SCHEME_PATH_PREFIX = "../../"
PRIOR_STOREKIT_FAILURE = {
    "run_id": "44082331641",
    "host_os_version": "26.6.1",
    "xcode_version": "26.6.0",
    "native_error": "SKInternalErrorDomain Code=3 while applying SKTestSession configuration; buyProduct failed with Unable to Complete Request / StoreKitError.notEntitled",
    "apple_reference_url": "https://developer.apple.com/forums/thread/826971",
    "workaround": "Associate the exact LOCALTEST catalog with the generated Xcode scheme and initiate the free trial through the actual product UI.",
}
SCREENSHOTS = {
    "macos": {
        "pleazyng-localtest-trial-offer-macos": "macos-00-trial-offer-localtest.png",
        "pleazyng-localtest-files-books-macos": "macos-01-files-and-books-localtest.png",
        "pleazyng-localtest-supporter-macos": "macos-02-supporter-localtest.png",
    },
    "tvos": {
        "pleazyng-localtest-trial-offer-tvos": "tvos-00-trial-offer-localtest.png",
        "pleazyng-localtest-files-books-tvos": "tvos-01-files-and-books-localtest.png",
        "pleazyng-localtest-supporter-tvos": "tvos-02-supporter-localtest.png",
    },
}
MACOS_LISTING_OUTPUTS = {
    "pleazyng-localtest-trial-offer-macos": "macos-00-trial-offer-localtest.png",
    "pleazyng-localtest-files-books-macos": "macos-01-files-and-books.png",
    "pleazyng-localtest-supporter-macos": "macos-02-supporter-localtest.png",
}
MACOS_CAPTURE_WINDOW_POINTS = (912, 570)
MACOS_LISTING_OUTPUT_PIXELS = (1280, 800)
PLATFORM = {
    "macos": {
        "xcode_platform": "macOS",
        "deployment_target": "14.0",
        "destination": "platform=macOS",
        "products_directory": "Debug",
        "app_bundle": "Pleazyng.app",
        "app_relative_path": "__TESTROOT__/Debug/Pleazyng.app",
    },
    "tvos": {
        "xcode_platform": "tvOS",
        "deployment_target": "17.0",
        "destination": None,
        "products_directory": "Debug-appletvsimulator",
        "app_bundle": "Runner.app",
        "app_relative_path": "__TESTROOT__/Debug-appletvsimulator/Runner.app",
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_screenshot_receipt(*, attachment_name: str, path: Path) -> dict[str, Any]:
    width, height = capture_tools.png_dimensions(path)
    content = capture_tools.require_visible_screen_content(path)
    return {
        "source_attachment": attachment_name,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "dimensions": [width, height],
        "visible_content_validation": content,
    }


def resample_macos_listing_screenshots(
    output: Path, *, app_hash: str, capture_tools_sha: str,
    window_geometries: dict[str, dict[str, Any]], build_defines: list[str],
) -> list[dict[str, Any]]:
    """Uniformly resample complete native app-window captures to Apple's Mac size."""
    captures: list[dict[str, Any]] = []
    output_width, output_height = MACOS_LISTING_OUTPUT_PIXELS
    for attachment_name, source_name in SCREENSHOTS["macos"].items():
        source = output / source_name
        raw = output / f"{Path(source_name).stem}.raw.png"
        target_name = MACOS_LISTING_OUTPUTS[attachment_name]
        target = output / target_name
        geometry = window_geometries.get(attachment_name)
        if not isinstance(geometry, dict):
            raise ValueError(f"XCTest did not retain native app-window geometry for {attachment_name}")
        window_frame = geometry.get("window_frame_points")
        if (not isinstance(window_frame, list) or len(window_frame) != 4
                or window_frame[2:] != list(MACOS_CAPTURE_WINDOW_POINTS)):
            raise ValueError(f"XCTest screenshot did not come from the requested live 912x570 Mac window: {geometry}")
        source.rename(raw)
        raw_width, raw_height = capture_tools.png_dimensions(raw)
        if raw_width * 5 != raw_height * 8:
            raise ValueError(
                f"The native Mac app-window capture must be a complete 16:10 image before resampling: "
                f"{raw_width}x{raw_height} ({raw})"
            )
        scale_x = output_width / raw_width
        scale_y = output_height / raw_height
        if abs(scale_x - scale_y) > 1e-12:
            raise ValueError(f"The Apple Mac output dimensions would distort the genuine window: {scale_x} vs {scale_y}")
        geometry_pixels = geometry.get("output_window_image_pixels")
        if geometry_pixels != [raw_width, raw_height]:
            raise ValueError(f"Raw app-window screenshot pixels do not match XCTest's recorded geometry: {geometry}")
        result = subprocess.run(
            ["/usr/bin/sips", "-s", "format", "png", "-z", str(output_height), str(output_width),
             str(raw), "--out", str(target)],
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Apple sips could not uniformly resample the complete native window: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        actual_dimensions = capture_tools.png_dimensions(target)
        if actual_dimensions != MACOS_LISTING_OUTPUT_PIXELS:
            raise ValueError(f"Apple sips returned unexpected Mac screenshot dimensions: {actual_dimensions}")
        if target.stat().st_size > 16 * 1024 * 1024:
            raise ValueError(f"Apple Mac screenshot exceeds the App Store Connect 16 MiB image limit: {target}")
        png_encoding = capture_tools.normalize_opaque_rgb_png(target)
        if capture_tools.png_color_type(target) != 2 or png_encoding["alpha_was_opaque"] is False:
            raise ValueError(f"Apple App Store screenshots must be opaque RGB PNGs: {target}")
        raw_content = capture_tools.require_visible_screen_content(raw)
        final_content = capture_tools.require_visible_screen_content(target)
        captures.append({
            "filename": target_name,
            "platform": "macOS",
            "build_configuration": "Release Archive",
            "capture_scene": (
                "Post-onboarding local Files and Books screen. The actual Pleazyng app was navigated through its "
                "local library after the UI initiated the isolated LOCALTEST free trial. No media server or account was configured."
                if attachment_name == "pleazyng-localtest-files-books-macos" else
                "Actual Pleazyng app screen captured during the isolated LOCALTEST trial review flow. "
                "This diagnostic screen is retained to document the observed app UI."
            ),
            "capture_method": [
                "XCTest XCUIScreen screenshot cropped to the actual native Pleazyng app window",
                "/usr/bin/sips resampled the complete 16:10 window uniformly to 1280x800; no crop, padding, overlays, or added material",
            ],
            "source_tag": SOURCE_TAG,
            "source_sha": SOURCE_SHA,
            "capture_tools_sha": capture_tools_sha,
            "bundle_id": BUNDLE_ID,
            "app_version": VERSION,
            "app_build": BUILD_NUMBER,
            "app_bundle_sha256": app_hash,
            "capture_sha256": sha256_file(target),
            "dimensions": list(actual_dimensions),
            "png_color_type": capture_tools.png_color_type(target),
            "png_alpha_channel": False,
            "png_encoding": png_encoding,
            "raw_capture": {
                "filename": raw.name,
                "sha256": sha256_file(raw),
                "size_bytes": raw.stat().st_size,
                "dimensions": [raw_width, raw_height],
                "png_color_type": capture_tools.png_color_type(raw),
                "visible_content_validation": raw_content,
            },
            "native_window_geometry": geometry,
            "resampling": {
                "tool": "/usr/bin/sips",
                "method": "uniform scale of the complete app-window PNG",
                "source_pixels": [raw_width, raw_height],
                "output_pixels": [output_width, output_height],
                "scale_x": scale_x,
                "scale_y": scale_y,
                "cropped": False,
                "padded": False,
                "overlays_added": False,
                "material_added": False,
                "hidden_ui": False,
            },
            "visible_content_validation": final_content,
            "build_defines": list(build_defines),
            "qualification": (
                "Exact retained Pleazyng App Store IAP macOS bundle, shown in the actual native app window after the observed "
                "isolated StoreKit LOCALTEST trial flow. LOCALTEST is not App Store Connect, App Store Sandbox, or a live purchase. "
                "The complete 16:10 app window was uniformly resampled to an Apple-accepted Mac screenshot size."
            ),
        })
    return captures


def listing_capture_manifest(
    captures: list[dict[str, Any]], *, receipt: dict[str, Any], app_hash: str,
    args: argparse.Namespace, build_receipt_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "product": "Pleazyng",
        "product_version": VERSION,
        "product_build": BUILD_NUMBER,
        "bundle_id": BUNDLE_ID,
        "source_tag": SOURCE_TAG,
        "source_sha": SOURCE_SHA,
        "capture_tools_sha": args.capture_tools_sha,
        "platform": "macOS",
        "build_mode": "App Store IAP",
        "build_configuration": "Release Archive",
        "build_defines": list(receipt["build_defines"]),
        "app_bundle_sha256": app_hash,
        "producer_build_receipt_sha256": sha256_file(build_receipt_path),
        "producer_run_id": args.producer_run_id,
        "producer_run_attempt": args.producer_run_attempt,
        "capture_run_id": args.run_id,
        "capture_run_attempt": args.run_attempt,
        "capture_os_version": command(["sw_vers", "-productVersion"], capture=True, timeout=30),
        "capture_architecture": command(["uname", "-m"], capture=True, timeout=30),
        "capture_xcode_version": command(["xcodebuild", "-version"], capture=True, timeout=60),
        "capture_window_points": list(MACOS_CAPTURE_WINDOW_POINTS),
        "captures": captures,
        "qualification": (
            "Authentic app-in-use screenshots from the exact retained macOS App Store IAP app bundle. "
            "The UI was navigated through the isolated StoreKit LOCALTEST free-trial flow; this does not claim "
            "App Store Connect, App Store Sandbox, live purchase, media server, account, or playback verification."
        ),
    }


def command(args: list[str], *, capture: bool = False, timeout: int = 300, cwd: Path | None = None) -> str:
    result = subprocess.run(args, check=True, text=True, capture_output=capture, timeout=timeout, cwd=cwd)
    return result.stdout.strip() if capture else ""


def _best_effort_command(args: list[str]) -> str | None:
    try:
        return command(args, capture=True, timeout=30)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def attachment_name_matches(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    generated_suffix = rf"{re.escape(expected)}_\d+_[0-9A-Fa-f-]{{36}}(?:\.[A-Za-z0-9]+)?"
    return re.fullmatch(generated_suffix, actual) is not None


def unique_attachment(attachments: list[dict[str, Any]], expected: str) -> dict[str, Any]:
    matches = [
        item for item in attachments
        if attachment_name_matches(str(item.get("suggestedHumanReadableName", "")), expected)
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one XCTest attachment {expected!r}; found {len(matches)}")
    return matches[0]


def version_pair(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)(?:\.[0-9]+)?", value)
    if match is None:
        raise ValueError(f"Invalid Apple platform version: {value!r}")
    return int(match.group(1)), int(match.group(2))


def verify_catalog(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    products = data.get("products")
    normalized = [
        {
            "product_id": item.get("productID"),
            "type": item.get("type"),
            "display_price_localtest": item.get("displayPrice"),
        }
        for item in products
    ] if isinstance(products, list) else None
    expected = [
        {"product_id": TRIAL_PRODUCT_ID, "type": "NonConsumable", "display_price_localtest": "0.00"},
        {"product_id": LIFETIME_PRODUCT_ID, "type": "NonConsumable", "display_price_localtest": "2.99"},
    ]
    if normalized != expected:
        raise ValueError(f"LOCALTEST catalog must contain only the reviewed zero-price trial and $2.99 lifetime SKUs: {normalized!r}")
    if data.get("settings", {}).get("_storefront") != "USA":
        raise ValueError("LOCALTEST catalog must use the USA storefront")
    return normalized


def ui_test_project_spec(platform: str) -> dict[str, Any]:
    config = PLATFORM[platform]
    return {
        "name": "PleazyngStoreKitLocalTrialReview",
        "options": {
            "bundleIdPrefix": "org.snapetech.pleazyng.localtest",
            "xcodeVersion": "26.5",
            "schemePathPrefix": SCHEME_PATH_PREFIX,
            "deploymentTarget": {config["xcode_platform"]: config["deployment_target"]},
        },
        "targets": {
            TEST_TARGET: {
                "type": "bundle.ui-testing",
                "platform": config["xcode_platform"],
                "sources": [
                    {"path": UI_TEST_SOURCE.name, "buildPhase": "sources"},
                    *({"path": source.name, "buildPhase": "sources"} for source in UI_TEST_SUPPORT_SOURCES),
                    {"path": STOREKIT_CONFIG.name, "buildPhase": "resources"},
                ],
                "settings": {
                    "base": {
                        "PRODUCT_BUNDLE_IDENTIFIER": TEST_BUNDLE_ID,
                        "GENERATE_INFOPLIST_FILE": "YES",
                        "SWIFT_VERSION": "5.0",
                        "CODE_SIGN_IDENTITY": "-",
                    },
                },
            },
        },
        "schemes": {
            "PleazyngStoreKitLocalTrialReview": {
                "build": {"targets": {TEST_TARGET: ["test"]}},
                "run": {"storeKitConfiguration": STOREKIT_CONFIG.name},
                "test": {
                    "targets": [TEST_TARGET],
                    "captureScreenshotsAutomatically": False,
                    "deleteScreenshotsWhenEachTestSucceeds": True,
                },
            },
        },
    }


def verify_scheme_storekit_configuration(scheme_path: Path, project_root: Path) -> dict[str, str]:
    if scheme_path.is_symlink() or not scheme_path.is_file():
        raise FileNotFoundError("The generated shared XCTest scheme is missing or is a symlink")
    try:
        root = ET.parse(scheme_path).getroot()
    except ET.ParseError as error:
        raise ValueError("The generated XCTest scheme is not valid XML") from error
    reference = root.find("./LaunchAction/StoreKitConfigurationFileReference")
    identifier = reference.get("identifier") if reference is not None else None
    if not isinstance(identifier, str) or not identifier or "\\" in identifier:
        raise ValueError("The generated scheme does not select a relative StoreKit configuration file")
    expected_reference = f"{SCHEME_PATH_PREFIX}{STOREKIT_CONFIG.name}"
    expected = (project_root / STOREKIT_CONFIG.name).resolve(strict=True)
    if identifier != expected_reference or not expected.is_file():
        raise ValueError("The generated scheme selects a different or missing StoreKit configuration")
    return {
        "scheme_path": scheme_path.name,
        "storekit_configuration_reference": identifier,
        "storekit_configuration_path": STOREKIT_CONFIG.name,
        "storekit_configuration_sha256": sha256_file(expected),
    }


def configure_macos_storekit_test_plan(scheme_path: Path, project_root: Path) -> Path:
    scheme = ET.parse(scheme_path)
    action = scheme.find("./TestAction")
    testables = None if action is None else action.find("Testables")
    references = [] if testables is None else testables.findall("./TestableReference/BuildableReference")
    if action is None or testables is None or len(references) != 1:
        raise ValueError("Generated Mac capture scheme must contain one UI test target")
    reference = references[0]
    project = reference.get("ReferencedContainer")
    target_id = reference.get("BlueprintIdentifier")
    if (project != "container:PleazyngStoreKitLocalTrialReview.xcodeproj"
            or reference.get("BlueprintName") != TEST_TARGET
            or not re.fullmatch(r"[0-9A-Fa-f]{24}", target_id or "")):
        raise ValueError("Generated Mac capture plan has an unexpected target reference")
    target = {"containerPath": project, "identifier": target_id, "name": TEST_TARGET}
    plan_path = project_root / MACOS_TEST_PLAN
    write_json(plan_path, {
        "configurations": [{
            "id": "458EFC0F-7DC8-4703-BA86-89BB31859CF4",
            "name": "LOCALTEST",
            "options": {},
        }],
        "defaultOptions": {
            "storeKitConfiguration": STOREKIT_CONFIG.name,
            "targetForVariableExpansion": target,
        },
        "testTargets": [{"target": target}],
        "version": 1,
    })
    action.remove(testables)
    plans = ET.SubElement(action, "TestPlans")
    ET.SubElement(plans, "TestPlanReference", reference=f"container:{MACOS_TEST_PLAN}", default="YES")
    scheme.write(scheme_path, encoding="utf-8", xml_declaration=True)
    return plan_path


def select_canonical_tvos_runtime(inventory: dict[str, Any], sdk_version: tuple[int, int]) -> dict[str, Any]:
    canonical_id = f"com.apple.CoreSimulator.SimRuntime.tvOS-{sdk_version[0]}-{sdk_version[1]}"
    candidates = [
        runtime for runtime in inventory.get("runtimes", [])
        if isinstance(runtime, dict) and runtime.get("identifier") == canonical_id
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"Expected exactly one canonical tvOS runtime {canonical_id}; found {len(candidates)}")
    runtime = candidates[0]
    if runtime.get("isAvailable") is not True:
        raise RuntimeError(f"The canonical tvOS runtime {canonical_id} is not available")
    if version_pair(str(runtime.get("version", ""))) != sdk_version:
        raise RuntimeError(f"The canonical tvOS runtime {canonical_id} does not match the installed SDK")
    return runtime


def create_tvos_simulator(run_id: str, attempt: str, output: Path, *, diagnostic: bool = False) -> dict[str, str]:
    sdk_version_text = command(["xcrun", "--sdk", "appletvsimulator", "--show-sdk-version"], capture=True, timeout=60)
    sdk_version = version_pair(sdk_version_text)
    raw_inventory = command(["xcrun", "simctl", "list", "-j"], capture=True, timeout=60)
    inventory_path = output / "simulator-runtime-inventory.json"
    inventory_path.write_text(raw_inventory + "\n", encoding="utf-8")
    inventory = json.loads(raw_inventory)
    runtime = select_canonical_tvos_runtime(inventory, sdk_version)
    devices = [
        device for device in inventory.get("devices", {}).get(runtime["identifier"], [])
        if isinstance(device, dict)
        and device.get("isAvailable") is True
        and str(device.get("name", "")).startswith("Apple TV")
        and isinstance(device.get("deviceTypeIdentifier"), str)
    ]
    if not devices:
        raise RuntimeError(f"No available Apple TV simulator exists in {runtime['identifier']}")
    template = max(devices, key=lambda item: capture_tools.device_preference(item["name"], "tvos"))
    name = f"Pleazyng-{run_id}-{attempt}-tvos"
    if diagnostic:
        name += "-diagnostic"
    udid = command([
        "xcrun", "simctl", "create", name, template["deviceTypeIdentifier"], runtime["identifier"],
    ], capture=True, timeout=120)
    if re.fullmatch(r"[0-9A-Fa-f-]{36}", udid) is None:
        raise RuntimeError(f"Simulator returned an invalid device identifier: {udid!r}")
    return {
        "udid": udid,
        "name": name,
        "runtime_id": runtime["identifier"],
        "runtime_version": str(runtime["version"]),
        "device_name": str(template["name"]),
        "device_type": template["deviceTypeIdentifier"],
        "sdk_version": sdk_version_text,
        "runtime_inventory_path": inventory_path.name,
        "runtime_inventory_sha256": sha256_file(inventory_path),
    }


def created_tvos_simulator_from_inventory(
    inventory: dict[str, Any], simulator: dict[str, str]
) -> tuple[str, dict[str, Any]]:
    matches = [
        (runtime_id, device)
        for runtime_id, devices in inventory.get("devices", {}).items()
        for device in devices
        if device.get("udid") == simulator["udid"]
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Created tvOS simulator identity appears {len(matches)} times in simctl inventory")
    runtime_id, device = matches[0]
    if (runtime_id != simulator["runtime_id"] or device.get("name") != simulator["name"]
            or device.get("deviceTypeIdentifier") != simulator["device_type"]):
        raise RuntimeError("Created tvOS simulator differs from the exact canonical runtime/device selection")
    return runtime_id, device


def verify_created_tvos_simulator(simulator: dict[str, str]) -> None:
    inventory = json.loads(command(["xcrun", "simctl", "list", "-j"], capture=True, timeout=60))
    created_tvos_simulator_from_inventory(inventory, simulator)


def ensure_tvos_simulator_ready_for_install(simulator: dict[str, str], output: Path) -> dict[str, Any]:
    raw_before = command(["xcrun", "simctl", "list", "-j"], capture=True, timeout=60)
    before_path = output / "simulator-state-before-app-install.json"
    before_path.write_text(raw_before + "\n", encoding="utf-8")
    before_inventory = json.loads(raw_before)
    _, before_device = created_tvos_simulator_from_inventory(before_inventory, simulator)
    initial_state = before_device.get("state")
    if initial_state == "Shutdown":
        command(["xcrun", "simctl", "boot", simulator["udid"]], timeout=180)
        command(["xcrun", "simctl", "bootstatus", simulator["udid"], "-b"], timeout=600)
        boot_action = "booted-after-xcodebuild"
    elif initial_state == "Booted":
        boot_action = "already-booted-after-xcodebuild"
    else:
        raise RuntimeError(f"Created tvOS Simulator is not install-ready after XCTest build: {initial_state!r}")

    launchctl_path = output / "simulator-launchctl-list.txt"
    try:
        launchctl_output = command(
            ["xcrun", "simctl", "spawn", simulator["udid"], "launchctl", "list"],
            capture=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as error:
        write_json(output / "simulator-launchctl-error.json", {
            "returncode": error.returncode,
            "stdout": error.stdout,
            "stderr": error.stderr,
        })
        raise RuntimeError("tvOS Simulator launchctl did not respond before app installation") from error
    launchctl_path.write_text(launchctl_output + "\n", encoding="utf-8")

    raw_after = command(["xcrun", "simctl", "list", "-j"], capture=True, timeout=60)
    after_path = output / "simulator-state-before-app-install-ready.json"
    after_path.write_text(raw_after + "\n", encoding="utf-8")
    after_inventory = json.loads(raw_after)
    _, after_device = created_tvos_simulator_from_inventory(after_inventory, simulator)
    if after_device.get("state") != "Booted":
        raise RuntimeError(
            f"Created tvOS Simulator did not reach Booted state before install: {after_device.get('state')!r}"
        )
    return {
        "simulator_udid": simulator["udid"],
        "state_after_xctest_build": initial_state,
        "readiness_action": boot_action,
        "state_before_install": after_device["state"],
        "launchctl_response_path": launchctl_path.name,
        "launchctl_response_sha256": sha256_file(launchctl_path),
        "before_inventory_path": before_path.name,
        "before_inventory_sha256": sha256_file(before_path),
        "ready_inventory_path": after_path.name,
        "ready_inventory_sha256": sha256_file(after_path),
    }


def xctestrun_test_targets(document: dict[str, Any]) -> list[dict[str, Any]]:
    configurations = document.get("TestConfigurations")
    if isinstance(configurations, list):
        targets: list[dict[str, Any]] = []
        for configuration in configurations:
            if not isinstance(configuration, dict) or not isinstance(configuration.get("TestTargets"), list):
                raise ValueError("XCTest plan contains a malformed test configuration")
            targets.extend(configuration["TestTargets"])
        if any(not isinstance(target, dict) for target in targets):
            raise ValueError("XCTest plan contains a malformed target entry")
        return targets
    return [
        value for key, value in document.items()
        if key not in {"__xctestrun_metadata__", "CodeCoverageBuildableInfos", "TestPlan"}
        and isinstance(value, dict)
        and ("TestBundlePath" in value or "BlueprintName" in value)
    ]


def select_xctestrun_target(document: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("UseDestinationArtifacts") is True:
        raise ValueError("XCTest plan must be a normal TestROOT plan, not a destination-artifact plan")
    matches = [target for target in xctestrun_test_targets(document) if target.get("BlueprintName") == TEST_TARGET]
    if len(matches) != 1:
        raise ValueError(f"Expected one {TEST_TARGET} target in XCTest plan; found {len(matches)}")
    target = matches[0]
    if target.get("IsUITestBundle") is not True or target.get("UseDestinationArtifacts") is True:
        raise ValueError("XCTest plan target is not a UI test bundle using TestROOT dependencies")
    bundle_identifier = target.get("TestBundleIdentifier")
    if bundle_identifier not in (None, TEST_BUNDLE_ID):
        raise ValueError(f"XCTest plan target has unexpected test bundle identifier: {bundle_identifier!r}")
    return target


def safe_testroot_path(value: str, products: Path) -> Path:
    prefix = "__TESTROOT__/"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError("XCTest paths must use the documented __TESTROOT__ macro")
    suffix = value[len(prefix):]
    if "\\" in suffix:
        raise ValueError("XCTest paths must use POSIX separators")
    parts = suffix.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("XCTest path contains an empty or escaping component")
    if any(re.search(r"__[A-Za-z0-9_]+__", part) for part in parts):
        raise ValueError("XCTest path contains an unsupported macro")
    root = products.resolve(strict=True)
    candidate = root.joinpath(*parts)
    cursor = root
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("XCTest path must not traverse symlinks")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("XCTest path resolves outside the build products TestROOT")
    return resolved


def resolve_xctestrun_path(value: str, target: dict[str, Any], products: Path) -> Path:
    if not isinstance(value, str) or "\\" in value:
        raise ValueError("XCTest path must be a POSIX path string")
    if value.startswith("__TESTROOT__/"):
        return safe_testroot_path(value, products)
    host_prefix = "__TESTHOST__/"
    if value.startswith(host_prefix):
        host_reference = target.get("TestHostPath")
        if not isinstance(host_reference, str):
            raise ValueError("XCTest plan is missing TestHostPath for its __TESTHOST__ bundle reference")
        test_host = safe_testroot_path(host_reference, products)
        if not test_host.name.endswith(".app") or not test_host.is_dir():
            raise ValueError("XCTest TestHostPath must resolve to an app bundle under TestROOT")
        suffix = value[len(host_prefix):]
        if not suffix:
            raise ValueError("XCTest test bundle path has no __TESTHOST__-relative suffix")
        parts = suffix.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("XCTest __TESTHOST__ path contains an empty or escaping component")
        if any(re.search(r"__[A-Za-z0-9_]+__", part) for part in parts):
            raise ValueError("XCTest __TESTHOST__ path contains an unsupported macro")
        candidate = test_host.joinpath(*parts)
        cursor = test_host
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("XCTest __TESTHOST__ path must not traverse symlinks")
        resolved = candidate.resolve(strict=True)
        root = products.resolve(strict=True)
        if not resolved.is_relative_to(root) or candidate.is_symlink():
            raise ValueError("XCTest __TESTHOST__ bundle resolves outside TestROOT or through a symlink")
        return resolved
    if "__" in value:
        raise ValueError("XCTest path contains an unsupported or misplaced macro")
    raise ValueError("XCTest path must start with __TESTROOT__ or __TESTHOST__")


def resolve_test_bundle_from_plan(xctestrun: Path, products: Path) -> tuple[Path, dict[str, Any]]:
    document = plistlib.loads(xctestrun.read_bytes())
    target = select_xctestrun_target(document)
    host_reference = target.get("TestHostPath")
    if not isinstance(host_reference, str):
        raise ValueError("XCTest plan target is missing TestHostPath")
    test_host = safe_testroot_path(host_reference, products)
    if not test_host.name.endswith(".app") or not test_host.is_dir():
        raise ValueError("XCTest TestHostPath must resolve to an app bundle under TestROOT")
    bundle_reference = target.get("TestBundlePath")
    if not isinstance(bundle_reference, str):
        raise ValueError("XCTest plan target is missing TestBundlePath")
    reference_suffix = bundle_reference.split("/", 1)[-1]
    if PurePosixPath(reference_suffix).name != f"{TEST_TARGET}.xctest":
        raise ValueError("XCTest plan target path does not identify the exact expected test bundle")
    bundle = resolve_xctestrun_path(bundle_reference, target, products)
    if bundle.name != f"{TEST_TARGET}.xctest" or not bundle.is_dir():
        raise ValueError("Resolved XCTest TestBundlePath is not the exact expected test bundle directory")
    return bundle, target


def verify_test_bundle_identity(test_bundle: Path) -> dict[str, str]:
    info_path = next(
        (candidate for candidate in (test_bundle / "Info.plist", test_bundle / "Contents" / "Info.plist")
         if candidate.is_file()),
        None,
    )
    if info_path is None or info_path.is_symlink():
        raise FileNotFoundError("The resolved XCTest bundle does not contain a regular Info.plist")
    with info_path.open("rb") as source:
        info = plistlib.load(source)
    bundle_id = info.get("CFBundleIdentifier") if isinstance(info, dict) else None
    if bundle_id != TEST_BUNDLE_ID:
        raise ValueError(f"Resolved XCTest bundle has an unexpected identity: {bundle_id!r}")
    return {"bundle_id": str(bundle_id), "info_plist": info_path.relative_to(test_bundle).as_posix()}


def associate_xctestrun_with_retained_app(xctestrun: Path, platform: str) -> dict[str, Any]:
    expected_path = PLATFORM[platform]["app_relative_path"]
    document = plistlib.loads(xctestrun.read_bytes())
    target = select_xctestrun_target(document)
    old_path = target.get("UITargetAppPath")
    if old_path not in (None, expected_path):
        raise ValueError(f"XCTest plan points at a different UI app: {old_path!r}")
    dependencies = target.get("DependentProductPaths", [])
    if not isinstance(dependencies, list) or any(not isinstance(path, str) for path in dependencies):
        raise ValueError("XCTest plan dependencies must be an array of path strings")
    if expected_path not in dependencies:
        dependencies.insert(0, expected_path)
    target["UITargetAppPath"] = expected_path
    target["DependentProductPaths"] = dependencies
    removed_test_provided_app = target.pop("UseUITargetAppProvidedByTests", None)
    xctestrun.write_bytes(plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=False))

    verified = plistlib.loads(xctestrun.read_bytes())
    verified_target = select_xctestrun_target(verified)
    if verified_target.get("UITargetAppPath") != expected_path:
        raise ValueError("Patched XCTest plan does not point at the retained product app")
    if expected_path not in verified_target.get("DependentProductPaths", []):
        raise ValueError("Patched XCTest plan does not include the retained app as a dependent product")
    if "UseUITargetAppProvidedByTests" in verified_target:
        raise ValueError("Patched XCTest plan still expects the test code to provide its app")
    return {
        "target": TEST_TARGET,
        "ui_target_app_path": expected_path,
        "dependent_product_paths": verified_target["DependentProductPaths"],
        "removed_use_ui_target_app_provided_by_tests": removed_test_provided_app,
    }


def validate_localtest_record(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("environment") != "LOCALTEST":
        raise ValueError("StoreKit transaction evidence must be labeled LOCALTEST")
    catalog_queries = record.get("catalog_queries", {})
    if catalog_queries.get("trial_product_id") != TRIAL_PRODUCT_ID:
        raise ValueError("The app's native LOCALTEST trial product query was not recorded")
    if catalog_queries.get("lifetime_product_id") != LIFETIME_PRODUCT_ID:
        raise ValueError("The app's native LOCALTEST lifetime product query was not recorded")
    if "$2.99" not in str(catalog_queries.get("lifetime_display_price", "")):
        raise ValueError("The app did not show the queried LOCALTEST $2.99 lifetime price")
    transactions = record.get("transactions")
    if not isinstance(transactions, list) or len(transactions) != 1:
        raise ValueError("LOCALTEST must contain exactly one transaction")
    transaction = transactions[0]
    if transaction.get("product_id") != TRIAL_PRODUCT_ID or transaction.get("state") != "purchased":
        raise ValueError("LOCALTEST transaction must be the completed free-trial SKU")
    if not transaction.get("transaction_id") or not transaction.get("original_transaction_id"):
        raise ValueError("LOCALTEST trial evidence is missing its transaction identifiers")
    if not transaction.get("purchase_date"):
        raise ValueError("LOCALTEST trial evidence is missing its original purchase date")
    history = record.get("history", {})
    before_launch = history.get("beforeAppLaunch", {})
    after_trial_cta = history.get("afterProductTrialCTA", {})
    if before_launch.get("transactionCount") != 0:
        raise ValueError("LOCALTEST transaction history must be empty before the actual app launches")
    if after_trial_cta.get("transactionCount") != 1:
        raise ValueError("The product trial CTA must create exactly one native LOCALTEST transaction")
    behavior = record.get("app_behavior", {})
    if behavior.get("active_trial_restored_by_app") is not True:
        raise ValueError("The real app did not report the native LOCALTEST trial as active")
    if behavior.get("trial_offer_shown") is not True:
        raise ValueError("The actual product trial offer screen was not captured")
    if behavior.get("trial_started_via_product_ui") is not True:
        raise ValueError("The LOCALTEST trial was not started through the actual product UI")
    if behavior.get("transaction_created_outside_product_app_ui") is not False:
        raise ValueError("The LOCALTEST trial transaction was not created through the product UI")
    if behavior.get("files_and_books_screen_opened") is not True:
        raise ValueError("The real local Files and Books screen did not open")
    if behavior.get("active_trial_sheet_shown") is not True:
        raise ValueError("The actual lifetime-access sheet did not show the active trial")
    if behavior.get("lifetime_purchase_activated") is not False or behavior.get("lifetime_transaction_created") is not False:
        raise ValueError("LOCALTEST evidence shows a lifetime purchase or activation")
    if record.get("raw_jws_included") is not False:
        raise ValueError("The capture record must not include raw StoreKit JWS data")
    return transaction


def run_logged(command_args: list[str], log_path: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("w", encoding="utf-8") as output:
            return subprocess.run(
                command_args, check=False, text=True, stdout=output, stderr=subprocess.STDOUT, timeout=timeout
            )
    except subprocess.TimeoutExpired as error:
        write_json(log_path.with_suffix(f"{log_path.suffix}.timeout.json"), {
            "command": command_args,
            "timeout_seconds": timeout,
            "log": log_path.name,
            "output_excerpt": str(error.stdout or "")[-8000:],
        })
        raise RuntimeError(f"Xcode command timed out after {timeout}s; see {log_path.name}") from error


def export_failure_diagnostics(result_bundle: Path, output: Path, platform: str) -> dict[str, Any]:
    export_dir = output / "failure-diagnostic-attachments"
    export_dir.mkdir(parents=True, exist_ok=True)
    try:
        command(
            ["xcrun", "xcresulttool", "export", "attachments", "--path", str(result_bundle), "--output-path", str(export_dir)],
            timeout=300,
        )
        manifest_path = export_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        attachments = [item for test in manifest for item in test.get("attachments", [])]
        expected = {
            f"pleazyng-diagnostic-after-launch-{platform}": output / f"diagnostic-after-launch-{platform}.png",
            f"pleazyng-diagnostic-accessibility-after-launch-{platform}": (
                output / f"diagnostic-accessibility-after-launch-{platform}.txt"
            ),
        }
        evidence: dict[str, Any] = {
            "manifest": manifest_path.relative_to(output).as_posix(),
            "manifest_sha256": sha256_file(manifest_path),
            "attachments": {},
        }
        for name, destination in expected.items():
            try:
                attachment = unique_attachment(attachments, name)
            except RuntimeError as error:
                evidence["attachments"][name] = {"error": str(error)}
                continue
            exported_name = attachment.get("exportedFileName", "")
            if not exported_name or Path(exported_name).name != exported_name:
                evidence["attachments"][name] = {"error": "invalid exported attachment filename"}
                continue
            source = export_dir / exported_name
            if not source.is_file():
                evidence["attachments"][name] = {"error": "exported attachment file is missing"}
                continue
            shutil.copyfile(source, destination)
            attachment_evidence: dict[str, Any] = {
                "path": destination.relative_to(output).as_posix(),
                "sha256": sha256_file(destination),
                "size_bytes": destination.stat().st_size,
            }
            if destination.suffix == ".png":
                try:
                    attachment_evidence["dimensions"] = list(capture_tools.png_dimensions(destination))
                except (OSError, ValueError) as error:
                    attachment_evidence["image_validation_error"] = str(error)
            evidence["attachments"][name] = attachment_evidence
        return evidence
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        return {
            "error": f"{type(error).__name__}: {error}",
            "export_directory": export_dir.name,
        }


def export_attachments(result_bundle: Path, output: Path, platform: str) -> tuple[dict[str, Path], dict[str, Any]]:
    export_dir = output / "xcresult-attachments"
    export_dir.mkdir(parents=True, exist_ok=True)
    command(["xcrun", "xcresulttool", "export", "attachments", "--path", str(result_bundle), "--output-path", str(export_dir)], timeout=300)
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    attachments = [item for test in manifest for item in test.get("attachments", [])]
    exported: dict[str, Path] = {}
    expected = SCREENSHOTS[platform]
    for name, filename in expected.items():
        attachment = unique_attachment(attachments, name)
        exported_name = attachment.get("exportedFileName", "")
        if not exported_name or Path(exported_name).name != exported_name:
            raise ValueError(f"Invalid exported filename for screenshot {name!r}")
        source = export_dir / exported_name
        if not source.is_file():
            raise FileNotFoundError(f"Missing XCTest screenshot attachment: {source}")
        destination = output / filename
        shutil.copyfile(source, destination)
        exported[name] = destination
        if platform == "macos":
            geometry_attachment = unique_attachment(attachments, f"{name}-capture-geometry")
            geometry_filename = geometry_attachment.get("exportedFileName", "")
            if not geometry_filename or Path(geometry_filename).name != geometry_filename:
                raise ValueError(f"Invalid exported app-window geometry for screenshot {name!r}")
            geometry_source = export_dir / geometry_filename
            if not geometry_source.is_file():
                raise FileNotFoundError(f"Missing XCTest native window geometry: {geometry_source}")
            geometry_path = output / f"{Path(filename).stem}.window-geometry.json"
            shutil.copyfile(geometry_source, geometry_path)
            geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
            if not isinstance(geometry, dict):
                raise ValueError(f"XCTest app-window geometry is not a JSON object: {geometry_path}")
            exported[f"geometry:{name}"] = geometry_path

    record_name = "pleazyng-storekit-macos-tvos-localtest-record"
    record_attachment = unique_attachment(attachments, record_name)
    record_filename = record_attachment.get("exportedFileName", "")
    if not record_filename or Path(record_filename).name != record_filename:
        raise ValueError("Invalid exported LOCALTEST transaction record filename")
    record_path = export_dir / record_filename
    if not record_path.is_file():
        raise FileNotFoundError("XCTest LOCALTEST transaction record attachment was not exported")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    validate_localtest_record(record)
    exported["localtest_record"] = record_path
    return exported, record


def verify_build_identity(platform: str, app: Path, receipt_path: Path, producer_run_id: str, producer_run_attempt: str) -> tuple[dict[str, Any], dict[str, str]]:
    platform_config = PLATFORM[platform]
    app = app.resolve(strict=True)
    if app.name != platform_config["app_bundle"]:
        raise ValueError(f"Unexpected {platform} retained app name {app.name!r}")
    identity = capture_tools.app_info(app)
    receipt_path = receipt_path.resolve(strict=True)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError("Producer build receipt must be a JSON object")
    if receipt.get("capture_input_kind") == "verified-macos-app-store-package":
        validate_appstore_package_capture_receipt(receipt, platform, producer_run_id, producer_run_attempt)
    else:
        capture_tools.validate_build_receipt(
            receipt,
            platform=platform,
            producer_repository=os.environ.get("GITHUB_REPOSITORY", ""),
            producer_run_id=producer_run_id,
            producer_run_attempt=producer_run_attempt,
            capture_tools_sha=str(receipt.get("capture_tools_sha", "")),
        )
    app_hash = capture_tools.sha256_tree(app)
    if receipt.get("app_bundle_sha256") != app_hash:
        raise ValueError("Retained app bundle hash differs from the exact producer receipt")
    if (receipt.get("bundle_id"), receipt.get("product_version"), receipt.get("product_build"), receipt.get("executable")) != (
        identity["bundle_id"], identity["version"], identity["build_number"], identity["executable"]
    ):
        raise ValueError("Retained app identity differs from its producer receipt")
    return receipt, identity


def validate_appstore_package_capture_receipt(
    receipt: dict[str, Any], platform: str, producer_run_id: str, producer_run_attempt: str,
) -> None:
    if platform != "macos":
        raise ValueError("A signed Mac App Store package receipt is valid only for macOS capture")
    producer_artifact = receipt.get("producer_artifact")
    app_store_receipt = receipt.get("app_store_receipt")
    expected = {
        "schema_version": 1,
        "product": "Pleazyng",
        "capture_input_kind": "verified-macos-app-store-package",
        "source_tag": SOURCE_TAG,
        "source_sha": SOURCE_SHA,
        "product_version": VERSION,
        "product_build": BUILD_NUMBER,
        "platform": "macOS",
        "build_mode": "App Store IAP",
        "build_configuration": "Release Archive",
        "app_bundle_name": "Pleazyng.app",
        "bundle_id": BUNDLE_ID,
        "build_defines": capture_tools.IOS_MAC_STORE_DEFINES,
        "distribution_signing": "Mac App Store Distribution",
        "producer_repository": "snapetech-org/pleazyng",
        "producer_workflow_path": ".github/workflows/macos-app-store-signed.yml",
        "producer_workflow_ref": (
            "snapetech-org/pleazyng/.github/workflows/macos-app-store-signed.yml@refs/heads/main"
        ),
        "producer_run_id": producer_run_id,
        "producer_run_attempt": producer_run_attempt,
    }
    mismatches = {key: (receipt.get(key), value) for key, value in expected.items()
                  if receipt.get(key) != value}
    if re.fullmatch(r"[0-9a-f]{40}", str(receipt.get("producer_workflow_sha", ""))) is None:
        mismatches["producer_workflow_sha"] = (receipt.get("producer_workflow_sha"), "40-character Git SHA")
    if not isinstance(producer_artifact, dict):
        mismatches["producer_artifact"] = (producer_artifact, "verified artifact identity")
    else:
        expected_artifact_name = f"pleazyng-macos-appstore-v{VERSION}-{producer_run_id}"
        if (producer_artifact.get("producer_run_id") != producer_run_id
                or producer_artifact.get("producer_run_attempt") != producer_run_attempt
                or producer_artifact.get("producer_workflow_sha") != receipt.get("producer_workflow_sha")
                or producer_artifact.get("artifact_name") != expected_artifact_name
                or re.fullmatch(r"[1-9][0-9]*", str(producer_artifact.get("artifact_id", ""))) is None
                or re.fullmatch(r"sha256:[0-9a-f]{64}", str(producer_artifact.get("artifact_digest", ""))) is None):
            mismatches["producer_artifact"] = (producer_artifact, "verified artifact identity for the exact producer attempt")
    if not isinstance(app_store_receipt, dict):
        mismatches["app_store_receipt"] = (app_store_receipt, "processed signed package receipt")
    else:
        application = app_store_receipt.get("application", {})
        expected_package_receipt = {
            "product": "Pleazyng",
            "channel": "Mac App Store",
            "candidate_state": "app-store-connect-processed",
            "release_tag": f"v{VERSION}",
            "source_commit": SOURCE_SHA,
            "marketing_version": VERSION,
            "build_number": BUILD_NUMBER,
            "bundle_id": BUNDLE_ID,
            "platform": "MAC_OS",
            "processing_state": "VALID",
            "github_run_id": int(producer_run_id),
            "github_run_attempt": int(producer_run_attempt),
            "workflow_commit": receipt.get("producer_workflow_sha"),
            "asset_filename": "pleazyng-macos-appstore.pkg",
            "asset_sha256": receipt.get("package_sha256"),
        }
        mismatches.update({f"app_store_receipt.{key}": (app_store_receipt.get(key), value)
                           for key, value in expected_package_receipt.items()
                           if app_store_receipt.get(key) != value})
        expected_application = {
            "bundle_identifier": BUNDLE_ID,
            "version": VERSION,
            "build_number": BUILD_NUMBER,
            "executable": "Pleazyng",
            "architectures": ["arm64", "x86_64"],
        }
        mismatches.update({f"app_store_receipt.application.{key}": (application.get(key), value)
                           for key, value in expected_application.items()
                           if application.get(key) != value})
        qualifications = app_store_receipt.get("qualification", {})
        if (qualifications.get("app_signed_for_distribution") is not True
                or qualifications.get("installer_signed_for_app_store") is not True
                or qualifications.get("app_store_connect_processed") is not True
                or qualifications.get("upload_to_app_store_connect") is not True):
            mismatches["app_store_receipt.qualification"] = (qualifications, "processed signed Mac App Store package")
        package = app_store_receipt.get("package", {})
        if (package.get("filename") != "pleazyng-macos-appstore.pkg"
                or package.get("sha256") != receipt.get("package_sha256")
                or package.get("signed") is not True
                or not isinstance(package.get("size_bytes"), int)
                or package["size_bytes"] <= 0):
            mismatches["app_store_receipt.package"] = (package, "verified signed installer package")
        installer_signature = app_store_receipt.get("installer_signature", {})
        if (installer_signature.get("certificate_type") != "MAC_INSTALLER_DISTRIBUTION"
                or installer_signature.get("certificate_sha1") != "1A0DABC17F7F2ED2E517B2CCA49A14A88156510F"):
            mismatches["app_store_receipt.installer_signature"] = (installer_signature, "verified Mac Installer Distribution signer")
        original_app_signature = app_store_receipt.get("app_signature", {})
        if (original_app_signature.get("team_identifier") != "DM9A59DC85"
                or original_app_signature.get("app_certificate_sha1") != "B7D31D8572209681D4C9E655F448708269BAE53B"):
            mismatches["app_store_receipt.app_signature"] = (original_app_signature, "verified Mac App Distribution signer")
    signature = receipt.get("app_signature")
    profile = receipt.get("profile")
    if (receipt.get("app_bundle_name") != PLATFORM["macos"]["app_bundle"]
            or receipt.get("executable") != "Pleazyng"
            or receipt.get("architectures") != ["arm64", "x86_64"]
            or re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("app_bundle_sha256", ""))) is None
            or not isinstance(signature, dict)
            or signature.get("team_identifier") != "DM9A59DC85"
            or signature.get("app_certificate_sha1") != "B7D31D8572209681D4C9E655F448708269BAE53B"
            or signature.get("sandbox_enabled") is not True
            or signature.get("jit_enabled") is not True
            or not isinstance(profile, dict)
            or profile.get("type") != "MAC_APP_STORE"
            or profile.get("uuid") != app_store_receipt.get("profile", {}).get("uuid")
            or re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("producer_package_receipt_sha256", ""))) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("package_sha256", ""))) is None):
        mismatches["app_bundle_or_signature"] = ("package capture receipt fields", "exact signed Mac App Store app")
    if mismatches:
        raise ValueError(f"Signed Mac App Store capture receipt does not match the selected producer: {mismatches}")


def stage_app_for_xctestrun(app: Path, products: Path, platform: str, receipt: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    relative = PLATFORM[platform]["app_relative_path"]
    destination = products / PLATFORM[platform]["products_directory"] / PLATFORM[platform]["app_bundle"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Refusing to overwrite an XCTest app staging destination: {destination}")
    if capture_tools.sha256_tree(app) != receipt.get("app_bundle_sha256"):
        raise ValueError("Retained app changed before XCTest staging")
    shutil.copytree(app, destination, symlinks=True, copy_function=shutil.copy2)
    staged_hash = capture_tools.sha256_tree(destination)
    if staged_hash != receipt.get("app_bundle_sha256"):
        raise ValueError("XCTest-staged app is not byte-identical to its retained producer app")
    identity = capture_tools.app_info(destination)
    return destination, {
        "app_path": relative,
        "app_bundle_sha256": staged_hash,
        "app_identity": identity,
    }


def verify_storekit_resource(test_bundle: Path, expected_sha: str) -> Path:
    matches = [
        path for path in test_bundle.rglob(STOREKIT_CONFIG.name)
        if path.is_file() and not path.is_symlink()
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected one StoreKit configuration resource in the built XCTest bundle; found {len(matches)}")
    if sha256_file(matches[0]) != expected_sha:
        raise ValueError("Built XCTest StoreKit resource differs from the pinned local catalog")
    return matches[0]


def install_tvos_app(simulator: dict[str, str], app: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    udid = simulator["udid"]
    command(["xcrun", "simctl", "install", udid, str(app)], timeout=300)
    installed_path = Path(command(
        ["xcrun", "simctl", "get_app_container", udid, BUNDLE_ID, "app"], capture=True, timeout=120
    )).resolve(strict=True)
    installed_hash = capture_tools.sha256_tree(installed_path)
    if installed_hash != receipt.get("app_bundle_sha256"):
        raise ValueError("tvOS Simulator installed an app different from the receipt-verified retained bundle")
    return {
        "bundle_id": BUNDLE_ID,
        "simulator_udid": udid,
        "installed_app_path": str(installed_path),
        "installed_app_bundle_sha256": installed_hash,
        "installed_app_identity": capture_tools.app_info(installed_path),
        "qualification": "Installed the exact producer-receipt-verified retained app into the named tvOS Simulator before SKTestSession setup.",
    }


def diagnostic_command(
    output: Path,
    name: str,
    args: list[str],
    *,
    timeout: int = 60,
) -> dict[str, Any]:
    """Run and retain one bounded simulator diagnostic command verbatim."""
    stdout_path = output / f"{name}.stdout.txt"
    stderr_path = output / f"{name}.stderr.txt"
    try:
        result = subprocess.run(
            args,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        returncode: int | None = result.returncode
        timed_out = False
        error = None
    except subprocess.TimeoutExpired as exc:
        stdout_value = exc.stdout or ""
        stderr_value = exc.stderr or ""
        stdout = stdout_value.decode(errors="replace") if isinstance(stdout_value, bytes) else stdout_value
        stderr = stderr_value.decode(errors="replace") if isinstance(stderr_value, bytes) else stderr_value
        returncode = None
        timed_out = True
        error = f"Timed out after {timeout}s"
    except OSError as exc:
        stdout = ""
        stderr = ""
        returncode = None
        timed_out = False
        error = f"{type(exc).__name__}: {exc}"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return {
        "command": args,
        "returncode": returncode,
        "timed_out": timed_out,
        "error": error,
        "stdout_path": stdout_path.name,
        "stdout_sha256": sha256_file(stdout_path),
        "stdout_size_bytes": stdout_path.stat().st_size,
        "stderr_path": stderr_path.name,
        "stderr_sha256": sha256_file(stderr_path),
        "stderr_size_bytes": stderr_path.stat().st_size,
    }


def simctl_launch_pid(stdout_path: Path) -> int | None:
    text = stdout_path.read_text(encoding="utf-8", errors="replace")
    matches = re.findall(rf"(?m)^\s*{re.escape(BUNDLE_ID)}:\s*([0-9]+)\s*$", text)
    return int(matches[0]) if len(matches) == 1 else None


def tvos_app_service_pid(line: str) -> int | None:
    match = re.fullmatch(
        rf"([0-9]+)\s+-?[0-9]+\s+UIKitApplication:{re.escape(BUNDLE_ID)}(?:\[[^\]]+\])+",
        line.strip(),
    )
    return int(match[1]) if match else None


def _matches_tvos_crash_report(path: Path) -> bool:
    try:
        sample = path.read_text(encoding="utf-8", errors="replace")[:131072]
    except OSError:
        return False
    if BUNDLE_ID in sample:
        return True
    if path.suffix == ".ips":
        try:
            header = json.loads(sample.splitlines()[0])
            payload = json.loads(sample.splitlines()[1])
        except (IndexError, json.JSONDecodeError):
            return False
        return (
            payload.get("procName") == "Runner"
            and header.get("name") == "Runner"
        )
    return re.search(r"(?m)^Process:\s+Runner(?:\s|$)", sample) is not None


def copy_recent_tvos_crash_reports(
    simulator: dict[str, str], output: Path, *, started_at: float
) -> list[dict[str, Any]]:
    home = Path.home()
    device_data = home / "Library" / "Developer" / "CoreSimulator" / "Devices" / simulator["udid"] / "data"
    directories = [
        device_data / "Library" / "Logs" / "CrashReporter",
        device_data / "Library" / "Logs" / "CrashReporter" / "Retired",
        home / "Library" / "Logs" / "DiagnosticReports",
    ]
    destination = output / "tvos-pre-xctest-diagnostic" / "crash-reports"
    destination.mkdir(parents=True, exist_ok=True)
    evidence: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path in seen or not path.is_file() or path.suffix not in {".ips", ".crash"}:
                continue
            seen.add(path)
            try:
                if path.stat().st_mtime < started_at - 5 or not _matches_tvos_crash_report(path):
                    continue
                copied = destination / path.name
                if copied.exists():
                    copied = destination / f"{path.stem}-{len(evidence) + 1}{path.suffix}"
                shutil.copyfile(path, copied)
                evidence.append({
                    "source_path": str(path),
                    "path": copied.relative_to(output).as_posix(),
                    "sha256": sha256_file(copied),
                    "size_bytes": copied.stat().st_size,
                })
            except OSError:
                continue
    return evidence


def diagnose_tvos_retained_app(
    simulator: dict[str, str],
    installed_app_evidence: dict[str, Any],
    app_identity: dict[str, str],
    app_bundle_sha256: str,
    output: Path,
) -> dict[str, Any]:
    """Launch the verified tvOS app outside XCTest and retain bounded runtime evidence."""
    if app_identity.get("bundle_id") != BUNDLE_ID:
        raise ValueError("tvOS diagnostic launch requires the receipt-verified Pleazyng bundle identity")
    if installed_app_evidence.get("installed_app_bundle_sha256") != app_bundle_sha256:
        raise ValueError("tvOS diagnostic launch requires the exact installed producer app hash")
    diagnostic_dir = output / "tvos-pre-xctest-diagnostic"
    diagnostic_dir.mkdir(parents=True, exist_ok=False)
    started_at = time.time()
    udid = simulator["udid"]
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "purpose": "Bounded product-process diagnosis before XCTest; no taps or StoreKit transactions are performed.",
        "storekit_environment": "No SKTestSession is active during this direct launch; its StoreKit response is diagnostic only.",
        "product": {
            "bundle_id": BUNDLE_ID,
            "version": app_identity.get("version"),
            "build": app_identity.get("build_number"),
            "executable": app_identity.get("executable"),
            "app_bundle_sha256": app_bundle_sha256,
            "installed_app_bundle_sha256": installed_app_evidence["installed_app_bundle_sha256"],
        },
        "simulator": simulator,
        "started_at_utc": datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
        "commands": {},
        "crash_reports": [],
    }

    def run(name: str, args: list[str], *, timeout: int = 60) -> dict[str, Any]:
        receipt = diagnostic_command(diagnostic_dir, name, args, timeout=timeout)
        evidence["commands"][name] = receipt
        return receipt

    run("installed_app_inventory", ["xcrun", "simctl", "listapps", udid])
    run("launchctl_before", ["xcrun", "simctl", "spawn", udid, "launchctl", "list"])
    launch = run("simctl_launch", ["xcrun", "simctl", "launch", udid, BUNDLE_ID], timeout=90)
    launch_stdout = diagnostic_dir / launch["stdout_path"]
    pid = simctl_launch_pid(launch_stdout)
    evidence["product_pid"] = pid
    time.sleep(5)

    if pid is not None:
        run(
            "product_launchctl_process_info",
            ["xcrun", "simctl", "spawn", udid, "launchctl", "procinfo", str(pid)],
        )

    launchctl_after = run("launchctl_after", ["xcrun", "simctl", "spawn", udid, "launchctl", "list"])
    launchctl_text = (diagnostic_dir / launchctl_after["stdout_path"]).read_text(encoding="utf-8", errors="replace")
    evidence["launchctl_app_service_lines"] = [
        line for line in launchctl_text.splitlines() if BUNDLE_ID in line
    ]
    process_alive = pid is not None and launchctl_after["returncode"] == 0 and any(
        tvos_app_service_pid(line) == pid for line in evidence["launchctl_app_service_lines"]
    )
    unified_predicate = f'process == "Runner" OR processIdentifier == {pid or 0} OR subsystem CONTAINS[c] "StoreKit"'
    run(
        "unified_log_after_launch",
        ["xcrun", "simctl", "spawn", udid, "log", "show", "--last", "2m", "--style", "compact", "--predicate", unified_predicate],
        timeout=120,
    )

    screenshot = diagnostic_dir / "simulator-screen.png"
    screenshot_receipt = run(
        "simulator_screenshot",
        ["xcrun", "simctl", "io", udid, "screenshot", "--type=png", str(screenshot)],
        timeout=60,
    )
    if screenshot.is_file() and screenshot.stat().st_size:
        image_evidence: dict[str, Any] = {
            "path": screenshot.relative_to(output).as_posix(),
            "sha256": sha256_file(screenshot),
            "size_bytes": screenshot.stat().st_size,
        }
        try:
            image_evidence["dimensions"] = list(capture_tools.png_dimensions(screenshot))
            image_evidence["visible_content_check"] = capture_tools.require_visible_screen_content(screenshot)
        except (OSError, ValueError) as error:
            image_evidence["validation_error"] = f"{type(error).__name__}: {error}"
        evidence["diagnostic_screen"] = image_evidence
    else:
        evidence["diagnostic_screen"] = {
            "error": "Simulator screenshot command did not create a nonempty PNG",
            "command": screenshot_receipt,
        }

    evidence["crash_reports"] = copy_recent_tvos_crash_reports(
        simulator, output, started_at=started_at
    )
    evidence["process_alive_after_launch"] = process_alive
    evidence["simctl_launch_succeeded"] = launch["returncode"] == 0 and pid is not None
    evidence["ready_for_xctest"] = evidence["simctl_launch_succeeded"] and process_alive
    if pid is not None and process_alive:
        terminate = run("simctl_terminate_product", ["xcrun", "simctl", "terminate", udid, BUNDLE_ID])
        if terminate["returncode"] != 0:
            error_text = "\n".join((
                (diagnostic_dir / terminate["stdout_path"]).read_text(encoding="utf-8", errors="replace"),
                (diagnostic_dir / terminate["stderr_path"]).read_text(encoding="utf-8", errors="replace"),
            ))
            if not (terminate["returncode"] == 3 and re.search(r"not running|no running process", error_text, re.IGNORECASE)):
                evidence["ready_for_xctest"] = False
                evidence["termination_error"] = "simctl terminate failed for the preflight product app"
        after_terminate = run(
            "product_process_after_terminate",
            ["xcrun", "simctl", "spawn", udid, "launchctl", "list"],
        )
        after_stdout = diagnostic_dir / after_terminate["stdout_path"]
        evidence["process_alive_after_terminate"] = (
            any(tvos_app_service_pid(line) is not None for line in after_stdout.read_text(encoding="utf-8").splitlines())
        )
        if after_terminate["returncode"] != 0:
            evidence["ready_for_xctest"] = False
            evidence["termination_error"] = "Could not verify the preflight app stopped using launchctl"
        elif evidence["process_alive_after_terminate"]:
            evidence["ready_for_xctest"] = False
            evidence["termination_error"] = "The preflight tvOS product process remained alive after simctl terminate"
    receipt_path = output / "tvos-pre-xctest-diagnostic.json"
    evidence["receipt_path"] = receipt_path.name
    write_json(receipt_path, evidence)
    return evidence


def capture(args: argparse.Namespace) -> None:
    platform = args.platform
    product_root = args.product_root.resolve(strict=True)
    tools_root = args.capture_tools_root.resolve(strict=True)
    capture_tools.require_source(product_root)
    if capture_tools.run(["git", "-C", str(product_root), "rev-parse", "HEAD"], capture=True, timeout=30) != SOURCE_SHA:
        raise ValueError("Product checkout does not match the exact selected candidate source SHA")
    if re.fullmatch(r"[0-9a-f]{40}", args.capture_tools_sha) is None:
        raise ValueError("Capture tools SHA must be an exact lowercase 40-character commit")
    current_tools = capture_tools.run(["git", "-C", str(tools_root), "rev-parse", "HEAD"], capture=True, timeout=30)
    if current_tools != args.capture_tools_sha:
        raise ValueError(f"Capture tools checkout is {current_tools}; expected {args.capture_tools_sha}")
    lineage = capture_tools.source_lineage(product_root, tools_root)
    config_path = tools_root / STOREKIT_CONFIG
    test_source_path = tools_root / UI_TEST_SOURCE
    test_support_paths = {source.name: tools_root / source for source in UI_TEST_SUPPORT_SOURCES}
    missing_support = [str(path) for path in test_support_paths.values() if not path.is_file()]
    if not config_path.is_file() or not test_source_path.is_file() or missing_support:
        raise FileNotFoundError("Capture tools checkout is missing the StoreKit config or platform XCTest source")
    catalog = verify_catalog(config_path)
    receipt, identity = verify_build_identity(
        platform, args.app, args.build_receipt, args.producer_run_id, args.producer_run_attempt
    )
    if receipt.get("capture_input_kind") != "verified-macos-app-store-package":
        receipt_lineage = receipt.get("source_lineage")
        producer_lineage = dict(lineage)
        if "workflow_source_sha" in producer_lineage:
            producer_lineage["workflow_source_sha"] = receipt["capture_tools_sha"]
        if (receipt_lineage != producer_lineage
                and (receipt_lineage is not None or SOURCE_SHA != capture_tools.DEFAULT_SOURCE_SHA)):
            raise ValueError("Retained app build receipt does not match its approved frozen-product source lineage")
    static_dependency_receipt: dict[str, Any] | None = None
    static_dependency_receipt_sha: str | None = None
    if args.dependency_receipt is not None:
        dependency_path = args.dependency_receipt.resolve(strict=True)
        if dependency_path.is_symlink() or not dependency_path.is_file():
            raise ValueError("tvOS static runtime dependency receipt must be a regular file")
        static_dependency_receipt = json.loads(dependency_path.read_text(encoding="utf-8"))
        static_dependency_receipt_sha = sha256_file(dependency_path)
        if (static_dependency_receipt.get("status") != "passed"
                or static_dependency_receipt.get("product_source_sha") != SOURCE_SHA
                or static_dependency_receipt.get("packaging_source_sha") != lineage.get("packaging_source_sha", SOURCE_SHA)
                or static_dependency_receipt.get("workflow_source_sha") != args.capture_tools_sha
                or static_dependency_receipt.get("source_lineage") != lineage
                or static_dependency_receipt.get("app_bundle_sha256") != capture_tools.sha256_tree(args.app.resolve(strict=True))):
            raise ValueError("tvOS dependency receipt does not verify the exact app bundle and source lineage")

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    config_sha = sha256_file(config_path)
    test_source_sha = sha256_file(test_source_path)
    test_support_shas = {name: sha256_file(path) for name, path in test_support_paths.items()}
    build_receipt_path = args.build_receipt.resolve(strict=True)
    app = args.app.resolve(strict=True)
    app_hash = capture_tools.sha256_tree(app)
    producer_consumer = build_receipt_path.parent / "consumer-provenance.json"
    if producer_consumer.is_file():
        shutil.copyfile(producer_consumer, output / "producer-consumer-provenance.json")

    simulator: dict[str, str] | None = None
    simulator_readiness: dict[str, Any] | None = None
    result_bundle: Path | None = None
    failure: str | None = None
    scheme_evidence: dict[str, str] | None = None
    pre_xctest_app_diagnostic: dict[str, Any] | None = None
    macos_frame_preference: dict[str, Any] | None = None
    try:
        simulator_id = None
        if platform == "tvos":
            simulator = create_tvos_simulator(args.run_id, args.run_attempt, output)
            simulator_id = simulator["udid"]
            write_json(output / "simulator.json", simulator)
            verify_created_tvos_simulator(simulator)
            if version_pair(simulator["runtime_version"]) < version_pair("17.0"):
                raise ValueError(f"tvOS StoreKitTest UI target requires runtime 17.0 or newer, found {simulator['runtime_version']}")

        project_root = Path(args.runner_root).resolve()
        project_root.mkdir(parents=True, exist_ok=False)
        (project_root / UI_TEST_SOURCE.name).write_bytes(test_source_path.read_bytes())
        for name, path in test_support_paths.items():
            shutil.copyfile(path, project_root / name)
        (project_root / STOREKIT_CONFIG.name).write_bytes(config_path.read_bytes())
        project_spec_path = project_root / "project.json"
        write_json(project_spec_path, ui_test_project_spec(platform))
        project_spec_sha = sha256_file(project_spec_path)
        shutil.copyfile(project_spec_path, output / "generated-project-spec.json")
        command(["xcodegen", "generate", "--spec", str(project_spec_path), "--project", str(project_root)], timeout=180)

        derived_data = project_root / "derived-data"
        project = project_root / "PleazyngStoreKitLocalTrialReview.xcodeproj"
        scheme_path = project / "xcshareddata" / "xcschemes" / "PleazyngStoreKitLocalTrialReview.xcscheme"
        test_plan_args = []
        if platform == "macos":
            test_plan = configure_macos_storekit_test_plan(scheme_path, project_root)
            shutil.copyfile(test_plan, output / test_plan.name)
            test_plan_args = ["-testPlan", test_plan.stem]
        shutil.copyfile(scheme_path, output / "generated-scheme.xcscheme")
        scheme_evidence = verify_scheme_storekit_configuration(scheme_path, project_root)
        if platform == "macos":
            scheme_evidence.update(test_plan=test_plan.name, test_plan_sha256=sha256_file(test_plan))
        destination = (
            f"platform=tvOS Simulator,id={simulator_id}" if platform == "tvos" else PLATFORM[platform]["destination"]
        )
        build_log = output / "xctest-runner-build.log"
        build_result = run_logged(
            [
                "xcodebuild", "build-for-testing", "-project", str(project),
                "-scheme", "PleazyngStoreKitLocalTrialReview", "-destination", destination,
                *test_plan_args, "-derivedDataPath", str(derived_data), "-parallel-testing-enabled", "NO",
            ],
            build_log,
            timeout=900,
        )
        if build_result.returncode != 0:
            raise RuntimeError(f"XCTest-only {platform} runner build failed ({build_result.returncode}); see {build_log.name}")

        products = derived_data / "Build" / "Products"
        xctestruns = list(products.glob("*.xctestrun"))
        if len(xctestruns) != 1:
            for index, generated_plan in enumerate(sorted(xctestruns), start=1):
                shutil.copyfile(generated_plan, output / f"xctestrun-generated-{index}.xctestrun")
            raise ValueError(f"Expected exactly one XCTest plan, found {len(xctestruns)}")
        xctestrun = xctestruns[0]
        raw_xctestrun_sha = sha256_file(xctestrun)
        shutil.copyfile(xctestrun, output / "xctestrun-before-app-association.xctestrun")
        if platform == "macos":
            metadata = plistlib.loads(xctestrun.read_bytes()).get("__xctestrun_metadata__", {})
            if metadata.get("FormatVersion") != 2:
                raise ValueError("Xcode did not generate a plan-aware Mac StoreKit XCTest manifest")
        test_bundle, _ = resolve_test_bundle_from_plan(xctestrun, products)
        test_bundle_identity = verify_test_bundle_identity(test_bundle)
        storekit_resource = verify_storekit_resource(test_bundle, config_sha)
        test_bundle_sha = capture_tools.sha256_tree(test_bundle)
        storekit_resource_sha = sha256_file(storekit_resource)
        app_staged, staged_evidence = stage_app_for_xctestrun(app, products, platform, receipt)
        before_sha = sha256_file(xctestrun)
        if before_sha != raw_xctestrun_sha:
            raise ValueError("Generated XCTest plan changed before retained-app association")
        association = associate_xctestrun_with_retained_app(xctestrun, platform)
        after_sha = sha256_file(xctestrun)
        shutil.copyfile(xctestrun, output / "xctestrun-after-app-association.xctestrun")
        verified_test_bundle, verified_target = resolve_test_bundle_from_plan(xctestrun, products)
        if verified_test_bundle != test_bundle:
            raise ValueError("App association changed the XCTest plan's resolved test bundle")
        if capture_tools.sha256_tree(verified_test_bundle) != test_bundle_sha:
            raise ValueError("The resolved XCTest bundle changed after app association")
        if sha256_file(verify_storekit_resource(verified_test_bundle, config_sha)) != storekit_resource_sha:
            raise ValueError("The verified StoreKit configuration changed after app association")
        write_json(output / "xctestrun-app-association.json", {
            "platform": platform,
            "blueprint_name": verified_target["BlueprintName"],
            "test_bundle_path": verified_target["TestBundlePath"],
            "test_host_path": verified_target.get("TestHostPath"),
            "resolved_test_bundle": test_bundle.relative_to(products).as_posix(),
            "test_bundle_identity": test_bundle_identity,
            "test_bundle_sha256": test_bundle_sha,
            "storekit_resource_sha256": storekit_resource_sha,
            "xctestrun_sha256_before": before_sha,
            "xctestrun_sha256_after": after_sha,
            **staged_evidence,
            **association,
        })

        installed_app_evidence = None
        if platform == "tvos":
            assert simulator is not None
            # A direct launch starts StoreKit in the default environment. Keep
            # its process and store state off the device used for LOCALTEST.
            diagnostic_output = output / "tvos-diagnostic-simulator"
            diagnostic_output.mkdir()
            diagnostic_simulator = create_tvos_simulator(args.run_id, args.run_attempt, diagnostic_output,
                                                         diagnostic=True)
            if diagnostic_simulator["udid"] == simulator["udid"]:
                raise ValueError("The diagnostic launch must use a separate simulator from LOCALTEST")
            try:
                verify_created_tvos_simulator(diagnostic_simulator)
                ensure_tvos_simulator_ready_for_install(diagnostic_simulator, diagnostic_output)
                diagnostic_install = install_tvos_app(diagnostic_simulator, app_staged, receipt)
                pre_xctest_app_diagnostic = diagnose_tvos_retained_app(
                    diagnostic_simulator, diagnostic_install, identity, app_hash, output,
                )
            finally:
                capture_tools.cleanup_simulator(diagnostic_simulator)
            if not pre_xctest_app_diagnostic["ready_for_xctest"]:
                raise RuntimeError(
                    "The exact retained tvOS app did not stay running after a bounded direct launch; "
                    "see tvos-pre-xctest-diagnostic.json and its process/crash/log evidence"
                )
            simulator_readiness = ensure_tvos_simulator_ready_for_install(simulator, output)
            write_json(output / "simulator-readiness.json", simulator_readiness)
            installed_app_evidence = install_tvos_app(simulator, app_staged, receipt)
            write_json(output / "simulator-installed-app.json", installed_app_evidence)

        if platform == "macos":
            macos_frame_preference = capture_tools.write_macos_main_window_frame_preference(
                width=MACOS_CAPTURE_WINDOW_POINTS[0], height=MACOS_CAPTURE_WINDOW_POINTS[1], x=56, y=80
            )
            write_json(output / "macos-window-frame-preference.json", macos_frame_preference)

        result_bundle = output / f"pleazyng-{platform}-storekit-localtest.xcresult"
        test_log = output / "storekit-ui-test.log"
        test_result = run_logged(
            [
                "xcodebuild", "test-without-building", "-xctestrun", str(xctestrun),
                "-destination", destination, "-derivedDataPath", str(derived_data),
                "-resultBundlePath", str(result_bundle), "-parallel-testing-enabled", "NO",
                "-testLanguage", "en", "-testRegion", "US",
                f"-only-testing:{TEST_TARGET}/{TEST_TARGET}/testCaptureTrialBackedFilesBooksAndAccessSheet",
            ],
            test_log,
            timeout=1800,
        )
        if test_result.returncode != 0:
            if platform == "tvos" and simulator is not None:
                diagnostic_command(
                    output, "tvos-storekit-ui-runtime",
                    ["xcrun", "simctl", "spawn", simulator["udid"], "log", "show",
                     "--last", "5m", "--style", "compact",
                     "--predicate", 'process == "Runner" OR subsystem CONTAINS[c] "StoreKit"'],
                    timeout=60,
                )
            raise RuntimeError(f"Native {platform} StoreKitTest UI flow failed ({test_result.returncode}); see {test_log.name}")
        if not result_bundle.is_dir():
            raise RuntimeError("Xcode reported UI test success but did not create an xcresult bundle")
        exported, localtest = export_attachments(result_bundle, output, platform)

        screenshot_receipts: dict[str, Any] = {}
        for attachment_name, path in ((name, output / filename) for name, filename in SCREENSHOTS[platform].items()):
            screenshot_receipts[path.name] = build_screenshot_receipt(
                attachment_name=attachment_name, path=path
            )
        storekit_provenance = {
            "schema_version": 1,
            "environment": "LOCALTEST",
            "qualification": "The actual product UI created a native free-trial transaction using the exact scheme-associated Apple StoreKitTest catalog. This is LOCALTEST only, not App Store Connect, App Store Sandbox, or a live purchase.",
            "product": {
                "source_tag": SOURCE_TAG,
                "source_sha": SOURCE_SHA,
                "source_lineage": lineage,
                "version": VERSION,
                "build": BUILD_NUMBER,
                "bundle_id": BUNDLE_ID,
                "app_bundle_sha256": app_hash,
                "producer_build_receipt_sha256": sha256_file(build_receipt_path),
                "producer_build_receipt": receipt,
                "producer_consumer_receipt_sha256": (
                    sha256_file(producer_consumer) if producer_consumer.is_file() else None
                ),
                "app_identity": identity,
            },
            "producer": {
                "repository": receipt.get("producer_repository"),
                "workflow_path": receipt.get("producer_workflow_path"),
                "workflow_ref": receipt.get("producer_workflow_ref"),
                "workflow_sha": receipt.get("producer_workflow_sha"),
                "run_id": args.producer_run_id,
                "run_attempt": args.producer_run_attempt,
                "capture_tools_sha": receipt.get("capture_tools_sha"),
                "artifact": receipt.get("producer_artifact"),
                "package_sha256": receipt.get("package_sha256"),
            },
            "capture": {
                "run_id": args.run_id,
                "run_attempt": args.run_attempt,
                "tools_sha": args.capture_tools_sha,
                "platform": platform,
                "xcode_version": command(["xcodebuild", "-version"], capture=True, timeout=60),
                "host_os_version": command(["sw_vers", "-productVersion"], capture=True, timeout=30),
                "host_architecture": command(["uname", "-m"], capture=True, timeout=30),
                "macos_window_frame_preference": macos_frame_preference,
                "simulator": simulator,
                "simulator_readiness_before_app_install": simulator_readiness,
                "pre_xctest_app_diagnostic": pre_xctest_app_diagnostic,
                "pre_xctest_app_diagnostic_receipt_sha256": (
                    sha256_file(output / "tvos-pre-xctest-diagnostic.json")
                    if pre_xctest_app_diagnostic is not None else None
                ),
                "xcodegen_version": command(["xcodegen", "--version"], capture=True, timeout=60),
            },
            "storekit_test": {
                "configuration_sha256": config_sha,
                "scheme_configuration": scheme_evidence,
                "prior_attempt_diagnostic": PRIOR_STOREKIT_FAILURE,
                "test_source_sha256": test_source_sha,
                "test_support_sources_sha256": test_support_shas,
                "test_project_spec_sha256": project_spec_sha,
                "built_test_bundle_sha256": test_bundle_sha,
                "built_test_resource_sha256": sha256_file(storekit_resource),
                "catalog": catalog,
                "transaction_record": localtest,
                "transaction_record_sha256": sha256_file(exported["localtest_record"]),
                "transaction_created_outside_product_app_ui": False,
                "trial_started_via_product_ui": True,
                "trial_entitlement_restored_by_product_app": localtest["app_behavior"]["active_trial_restored_by_app"],
                "lifetime_transaction_created": False,
                "raw_jws_included": False,
            },
            "xctest": {
                "runner_build_log_sha256": sha256_file(build_log),
                "ui_test_log_sha256": sha256_file(test_log),
                "result_bundle": result_bundle.name,
                "result_bundle_sha256": capture_tools.sha256_tree(result_bundle),
                "staged_app": staged_evidence,
                "simulator_installed_app": installed_app_evidence,
            },
            "screenshots": screenshot_receipts,
            "static_tvos_dependency_check": (
                {
                    "receipt_sha256": static_dependency_receipt_sha,
                    "receipt": static_dependency_receipt,
                }
                if static_dependency_receipt is not None else None
            ),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        if platform == "macos":
            window_geometries = {
                name.removeprefix("geometry:"): json.loads(path.read_text(encoding="utf-8"))
                for name, path in exported.items() if name.startswith("geometry:")
            }
            listing_captures = resample_macos_listing_screenshots(
                output, app_hash=app_hash, capture_tools_sha=args.capture_tools_sha,
                window_geometries=window_geometries, build_defines=receipt["build_defines"],
            )
            raw_screenshot_receipts = {}
            for capture_record in listing_captures:
                raw_name = capture_record["raw_capture"]["filename"]
                source_name = Path(raw_name).name.removesuffix(".raw.png") + ".png"
                raw_receipt = dict(screenshot_receipts[source_name])
                raw_receipt["filename"] = raw_name
                raw_receipt["dimensions"] = capture_record["raw_capture"]["dimensions"]
                raw_receipt["sha256"] = capture_record["raw_capture"]["sha256"]
                raw_receipt["size_bytes"] = capture_record["raw_capture"]["size_bytes"]
                raw_screenshot_receipts[raw_name] = raw_receipt
            storekit_provenance["screenshots"] = raw_screenshot_receipts
            write_json(output / "storekit-localtest-provenance.json", storekit_provenance)
            write_json(output / "capture-provenance.json", listing_capture_manifest(
                listing_captures, receipt=receipt, app_hash=app_hash, args=args,
                build_receipt_path=build_receipt_path,
            ))
        else:
            write_json(output / "storekit-localtest-provenance.json", storekit_provenance)
            write_json(output / "capture-provenance.json", storekit_provenance)
        print(f"Captured authentic {platform} LOCALTEST trial-backed app screens in {output}")
    except Exception as error:
        failure = f"{type(error).__name__}: {error}"
        failure_diagnostics = None
        if result_bundle is not None and result_bundle.is_dir():
            failure_diagnostics = export_failure_diagnostics(result_bundle, output, platform)
        write_json(output / "capture-failure.json", {
            "schema_version": 1,
            "environment": "LOCALTEST",
            "platform": platform,
            "product_source_sha": SOURCE_SHA,
            "source_lineage": lineage,
            "static_tvos_dependency_check_sha256": static_dependency_receipt_sha,
            "capture_tools_sha": args.capture_tools_sha,
            "producer_run_id": args.producer_run_id,
            "producer_run_attempt": args.producer_run_attempt,
            "error": failure,
            "scheme_configuration": scheme_evidence,
            "macos_window_frame_preference": macos_frame_preference,
            "simulator_readiness_before_app_install": simulator_readiness,
            "pre_xctest_app_diagnostic": pre_xctest_app_diagnostic,
            "pre_xctest_app_diagnostic_receipt_sha256": (
                sha256_file(output / "tvos-pre-xctest-diagnostic.json")
                if pre_xctest_app_diagnostic is not None else None
            ),
            "observed_prior_failure": PRIOR_STOREKIT_FAILURE,
            "runner_environment": {
                "xcode_version": _best_effort_command(["xcodebuild", "-version"]),
                "host_os_version": _best_effort_command(["sw_vers", "-productVersion"]),
                "host_architecture": _best_effort_command(["uname", "-m"]),
            },
            "result_bundle_present": bool(result_bundle and result_bundle.exists()),
            "failure_diagnostic_attachments": failure_diagnostics,
            "diagnostic_files": sorted(path.name for path in output.iterdir() if path.is_file()),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        raise
    finally:
        if simulator is not None:
            try:
                capture_tools.cleanup_simulator(simulator)
            except Exception as cleanup_error:
                write_json(output / "simulator-cleanup-error.json", {
                    "error": f"{type(cleanup_error).__name__}: {cleanup_error}",
                    "capture_failure": failure,
                })


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    capture_command = commands.add_parser("capture", help="capture one retained Mac or tvOS app using LOCALTEST trial history")
    capture_command.add_argument("--platform", choices=tuple(PLATFORM), required=True)
    capture_command.add_argument("--product-root", required=True, type=Path)
    capture_command.add_argument("--capture-tools-root", required=True, type=Path)
    capture_command.add_argument("--capture-tools-sha", required=True)
    capture_command.add_argument("--app", required=True, type=Path)
    capture_command.add_argument("--build-receipt", required=True, type=Path)
    capture_command.add_argument("--dependency-receipt", type=Path)
    capture_command.add_argument("--producer-run-id", required=True)
    capture_command.add_argument("--producer-run-attempt", required=True)
    capture_command.add_argument("--runner-root", required=True, type=Path)
    capture_command.add_argument("--output-dir", required=True, type=Path)
    capture_command.add_argument("--run-id", required=True)
    capture_command.add_argument("--run-attempt", required=True)
    capture_command.set_defaults(handler=capture)
    return root


if __name__ == "__main__":
    parsed = parser().parse_args()
    parsed.handler(parsed)
