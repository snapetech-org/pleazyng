#!/usr/bin/env python3
"""Capture Pleazyng's real iOS trial UI with Apple's local StoreKit Test environment.

The product is built from an exact, main-ancestry candidate source SHA. StoreKit
Test configuration and XCTest code are test-only resources; they are never
copied into or compiled as part of the product app.
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
import tarfile
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capture_apple_store_screenshots as capture_tools


VERSION = capture_tools.VERSION
BUILD_NUMBER = capture_tools.BUILD_NUMBER
SOURCE_TAG = capture_tools.SOURCE_TAG
BUNDLE_ID = "org.snapetech.pleazyng"
TRIAL_PRODUCT_ID = "pleazyng_7_day_trial"
LIFETIME_PRODUCT_ID = "pleazyng_lifetime_unlock"
RESTORE_SYNC_CLOCK_TOLERANCE_MS = 2000
CAPTURE_DISPLAY_TARGETS = {
    "default": None,
    "iphone-medium": {
        "device_name": "iPhone 18 Pro",
        "expected_screenshot_dimensions": [1206, 2622],
    },
    "ipad-13": {
        "device_name": "iPad Pro 13-inch (M5)",
        "expected_screenshot_dimensions": [2064, 2752],
    },
}
CAPTURE_DISPLAY_XCODE_VERSION = "27.2"
RECEIPT_NAME = "product-build-provenance.json"
STOREKIT_RELATIVE_PATH = Path("scripts/stores/apple/pleazyng-trial-review.storekit")
UITEST_RELATIVE_PATH = Path("scripts/stores/apple/PleazyngTrialReviewUITests.swift")
SCREENSHOTS = {
    "pleazyng-trial-offer-localtest": "trial-offer-localtest.png",
    "pleazyng-trial-active-files-books-localtest": "trial-active-files-books-localtest.png",
    "pleazyng-trial-active-supporter-localtest": "trial-active-supporter-localtest.png",
    "pleazyng-open-media-form-localtest": "open-media-form-localtest.png",
}


def build_artifact_name() -> str:
    return f"pleazyng-apple-trial-review-product-v{VERSION}.tar.gz"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Expected an application bundle directory: {root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        if path.is_symlink():
            digest.update(b"L")
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"F")
            digest.update(bytes.fromhex(sha256_file(path)))
        elif path.is_dir():
            digest.update(b"D")
        else:
            raise ValueError(f"Unexpected filesystem object in application bundle: {path}")
    return digest.hexdigest()


def command(args: list[str], *, capture: bool = False, timeout: int = 300, cwd: Path | None = None) -> str:
    result = subprocess.run(args, check=True, text=True, capture_output=capture, timeout=timeout, cwd=cwd)
    if not capture:
        return ""
    return result.stdout.strip()


def _command_stream_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def run_simulator_command(
    args: list[str],
    output_dir: Path,
    name: str,
    *,
    timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a Simulator command and retain its exact output, including timeout fragments."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = output_dir / f"{name}.stdout.txt"
    stderr_path = output_dir / f"{name}.stderr.txt"
    receipt_path = output_dir / f"{name}.command.json"
    started_at = datetime.now(timezone.utc)
    start = time.monotonic()
    try:
        result = subprocess.run(args, check=False, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        stdout = _command_stream_text(error.stdout or error.output)
        stderr = _command_stream_text(error.stderr)
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        write_json(receipt_path, {
            "command": args,
            "timeout_seconds": timeout,
            "timed_out": True,
            "started_at_utc": started_at.isoformat(),
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "stdout_file": stdout_path.name,
            "stderr_file": stderr_path.name,
        })
        raise
    stdout_path.write_text(result.stdout or "", encoding="utf-8")
    stderr_path.write_text(result.stderr or "", encoding="utf-8")
    write_json(receipt_path, {
        "command": args,
        "timeout_seconds": timeout,
        "returncode": result.returncode,
        "timed_out": False,
        "started_at_utc": started_at.isoformat(),
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "stdout_file": stdout_path.name,
        "stderr_file": stderr_path.name,
    })
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            args,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, check=check, text=True, capture_output=True, timeout=120)


def require_exact_source_sha(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("The product source SHA must be an exact lowercase 40-character Git commit")
    return value


def verify_source(product_root: Path, expected_sha: str) -> dict[str, str]:
    product_root = product_root.resolve(strict=True)
    require_exact_source_sha(expected_sha)
    actual_sha = git(product_root, "rev-parse", "HEAD").stdout.strip()
    if actual_sha != expected_sha:
        raise ValueError(f"Product checkout is {actual_sha}; expected the exact source SHA {expected_sha}")
    if SOURCE_TAG != f"v{VERSION}-candidate.{expected_sha}":
        raise ValueError("The product source tag must be the exact versioned candidate tag for the source SHA")
    tag_type = git(product_root, "cat-file", "-t", f"refs/tags/{SOURCE_TAG}", check=False)
    if tag_type.returncode != 0 or tag_type.stdout.strip() != "tag":
        raise ValueError(f"The exact product candidate tag is missing or is not annotated: {SOURCE_TAG}")
    tag_commit = git(product_root, "rev-parse", f"refs/tags/{SOURCE_TAG}^{{commit}}", check=False)
    if tag_commit.returncode != 0 or tag_commit.stdout.strip() != expected_sha:
        raise ValueError("The exact product candidate tag does not resolve to the selected source SHA")
    main_ref = git(product_root, "rev-parse", "--verify", "refs/remotes/origin/main", check=False)
    if main_ref.returncode != 0:
        raise ValueError("The product checkout must contain origin/main for the ancestry guard")
    ancestry = git(product_root, "merge-base", "--is-ancestor", expected_sha, "refs/remotes/origin/main", check=False)
    if ancestry.returncode != 0:
        raise ValueError("The product source SHA is not an ancestor of origin/main")

    version_line = next(
        (line.strip() for line in (product_root / "pubspec.yaml").read_text(encoding="utf-8").splitlines()
         if line.strip().startswith("version:")),
        "",
    )
    match = re.fullmatch(r"version:\s*[\"']?([0-9.]+)\+([0-9]+)[\"']?", version_line)
    if match is None or match.groups() != (VERSION, BUILD_NUMBER):
        raise ValueError(f"Expected source version {VERSION}+{BUILD_NUMBER}; found {version_line!r}")

    store_source = (product_root / "lib/services/supporter/supporter_store.dart").read_text(encoding="utf-8")
    if not re.search(r"const String appStoreTrialProductId\s*=\s*'pleazyng_7_day_trial'\s*;", store_source):
        raise ValueError("The exact product source does not declare the approved Apple trial product ID")
    if not re.search(r"defaultValue:\s*'pleazyng_lifetime_unlock'", store_source):
        raise ValueError("The exact product source does not declare the approved lifetime product ID")
    return {
        "product_source_sha": actual_sha,
        "product_source_tag": SOURCE_TAG,
        "product_version": VERSION,
        "product_build": BUILD_NUMBER,
    }


def bundle_info(app: Path) -> dict[str, Any]:
    plist_path = app / "Info.plist"
    with plist_path.open("rb") as source:
        info = plistlib.load(source)
    expected = {
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": BUILD_NUMBER,
    }
    for key, value in expected.items():
        if info.get(key) != value:
            raise ValueError(f"Built app has unexpected {key}: {info.get(key)!r}; expected {value!r}")
    executable_name = info.get("CFBundleExecutable")
    if not isinstance(executable_name, str) or not executable_name:
        raise ValueError("Built app does not declare a CFBundleExecutable")
    executable = app / executable_name
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("Built app's declared executable is missing or not executable")
    architectures = command(["/usr/bin/lipo", "-archs", str(executable)], capture=True, timeout=60)
    if "arm64" not in architectures.split():
        raise ValueError(f"Expected an arm64 Simulator app executable; found {architectures!r}")
    return {
        "bundle_id": BUNDLE_ID,
        "version": VERSION,
        "build": BUILD_NUMBER,
        "executable": executable_name,
        "architectures": architectures.split(),
    }


def storekit_catalog(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    products = data.get("products")
    if not isinstance(products, list):
        raise ValueError("StoreKit configuration must contain a products array")
    normalized = [
        {
            "product_id": item.get("productID"),
            "type": item.get("type"),
            "display_price_localtest": item.get("displayPrice"),
        }
        for item in products
    ]
    expected = [
        {"product_id": TRIAL_PRODUCT_ID, "type": "NonConsumable", "display_price_localtest": "0.00"},
        {"product_id": LIFETIME_PRODUCT_ID, "type": "NonConsumable", "display_price_localtest": "2.99"},
    ]
    if normalized != expected:
        raise ValueError(f"LOCALTEST StoreKit catalog must exactly match the two reviewed SKUs: {normalized!r}")
    if data.get("settings", {}).get("_storefront") != "USA":
        raise ValueError("LOCALTEST StoreKit catalog must use the USA storefront")
    return normalized


def verify_storekit_test_runtime(
    sdk_version: str,
    runtime_inventory: dict[str, Any],
    minimum_ios_version: str,
) -> dict[str, str]:
    """Require an installed iOS runtime that includes the CLI StoreKitTest fix."""
    def version_pair(value: str) -> tuple[int, int]:
        match = re.fullmatch(r"([0-9]+)\.([0-9]+)(?:\.[0-9]+)?", value)
        if match is None:
            raise ValueError(f"Invalid iOS Simulator version: {value!r}")
        return int(match.group(1)), int(match.group(2))

    minimum_pair = version_pair(minimum_ios_version)
    sdk_pair = version_pair(sdk_version)
    if sdk_pair < minimum_pair:
        raise ValueError(
            f"StoreKitTest CLI capture requires iOS Simulator {minimum_ios_version} or newer; "
            f"the selected SDK is {sdk_version}"
        )
    runtimes = [
        item for item in runtime_inventory.get("runtimes", [])
        if item.get("isAvailable") is True
        and isinstance(item.get("identifier"), str)
        and ".iOS-" in item["identifier"]
        and version_pair(str(item.get("version", ""))) == sdk_pair
    ]
    if not runtimes:
        raise ValueError(f"No installed iOS Simulator runtime matches SDK {sdk_version}")
    canonical_runtime_id = f"com.apple.CoreSimulator.SimRuntime.iOS-{sdk_pair[0]}-{sdk_pair[1]}"
    canonical_runtimes = [item for item in runtimes if item["identifier"] == canonical_runtime_id]
    if len(canonical_runtimes) != 1:
        raise ValueError(
            f"Expected exactly one canonical iOS Simulator runtime {canonical_runtime_id}; "
            f"found {len(canonical_runtimes)}"
        )
    selected = canonical_runtimes[0]
    return {
        "sdk_version": sdk_version,
        "runtime_id": selected["identifier"],
        "runtime_version": str(selected["version"]),
    }


def verify_capture_display_target(
    display_target: str,
    sdk_version: str,
    runtime_inventory: dict[str, Any],
    device_type_inventory: dict[str, Any],
) -> dict[str, Any] | None:
    """Require exact public iOS 27.2 simulator models for required screenshots."""
    if display_target not in CAPTURE_DISPLAY_TARGETS:
        raise ValueError(f"Unsupported StoreKit capture display target: {display_target!r}")
    spec = CAPTURE_DISPLAY_TARGETS[display_target]
    if spec is None:
        return None
    if sdk_version != CAPTURE_DISPLAY_XCODE_VERSION:
        raise ValueError(
            f"{display_target} capture requires the iOS {CAPTURE_DISPLAY_XCODE_VERSION} SDK; found {sdk_version}"
        )
    runtime = verify_storekit_test_runtime(
        sdk_version,
        runtime_inventory,
        CAPTURE_DISPLAY_XCODE_VERSION,
    )
    device_types = device_type_inventory.get("devicetypes")
    if not isinstance(device_types, list):
        raise ValueError("simctl did not return a device-types inventory")
    devices = [
        device for device in device_types
        if device.get("name") == spec["device_name"]
        and isinstance(device.get("identifier"), str)
    ]
    if len(devices) != 1:
        raise ValueError(
            f"Expected exactly one available {spec['device_name']} in iOS {CAPTURE_DISPLAY_XCODE_VERSION}; "
            f"found {len(devices)}"
        )
    return {
        **runtime,
        "display_target": display_target,
        "device_name": spec["device_name"],
        "device_type": devices[0]["identifier"],
        "expected_screenshot_dimensions": spec["expected_screenshot_dimensions"],
    }


def create_storekit_simulator(
    display_target: str,
    run_id: str,
    attempt: str,
    diagnostic_dir: Path | None = None,
) -> dict[str, Any]:
    """Create the exact required screenshot simulator, or the default producer device."""
    if display_target not in CAPTURE_DISPLAY_TARGETS:
        raise ValueError(f"Unsupported StoreKit capture display target: {display_target!r}")
    spec = CAPTURE_DISPLAY_TARGETS[display_target]
    if spec is None:
        device = capture_tools.create_simulator("iOS", "iphone", run_id, attempt)
        return {**device, "display_target": display_target}

    sdk_version = command(["xcrun", "--sdk", "iphonesimulator", "--show-sdk-version"], capture=True, timeout=60)
    runtimes = json.loads(command(["xcrun", "simctl", "list", "-j", "runtimes"], capture=True, timeout=60))
    device_types = json.loads(command(["xcrun", "simctl", "list", "-j", "devicetypes"], capture=True, timeout=60))
    selected = verify_capture_display_target(display_target, sdk_version, runtimes, device_types)
    assert selected is not None
    name = f"Pleazyng-{run_id}-{attempt}-{display_target}"
    create_command = ["xcrun", "simctl", "create", name, selected["device_type"], selected["runtime_id"]]
    try:
        udid = command(
            create_command,
            capture=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as error:
        def bounded_output(value: str | bytes | None) -> str:
            if value is None:
                return ""
            decoded = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
            return decoded[-4000:]

        if diagnostic_dir is not None:
            write_json(diagnostic_dir / "simulator-create-failure.json", {
                "command": create_command,
                "exit_code": error.returncode,
                "stdout_tail": bounded_output(error.stdout or error.output),
                "stderr_tail": bounded_output(error.stderr),
            })
        raise
    if re.fullmatch(r"[0-9A-Fa-f-]{36}", udid) is None:
        raise RuntimeError(f"Simulator returned an invalid device identifier: {udid!r}")
    return {
        "udid": udid,
        "name": name,
        "runtime_id": selected["runtime_id"],
        "runtime_version": selected["runtime_version"],
        "device_name": selected["device_name"],
        "device_type": selected["device_type"],
        "sdk_version": selected["sdk_version"],
        "display_target": display_target,
        "expected_screenshot_dimensions": selected["expected_screenshot_dimensions"],
    }


def verify_capture_screenshot_dimensions(display_target: str, dimensions: tuple[int, int]) -> None:
    """Fail if an image does not occupy the target's exact portrait pixel slot."""
    if display_target not in CAPTURE_DISPLAY_TARGETS:
        raise ValueError(f"Unsupported StoreKit capture display target: {display_target!r}")
    spec = CAPTURE_DISPLAY_TARGETS[display_target]
    if spec is not None and list(dimensions) != spec["expected_screenshot_dimensions"]:
        expected = tuple(spec["expected_screenshot_dimensions"])
        raise ValueError(
            f"{display_target} screenshot dimensions are {dimensions}; expected {expected} for {spec['device_name']}"
        )


def verify_runtime(args: argparse.Namespace) -> None:
    sdk_version = command(["xcrun", "--sdk", "iphonesimulator", "--show-sdk-version"], capture=True, timeout=60)
    runtimes = json.loads(command(["xcrun", "simctl", "list", "-j", "runtimes"], capture=True, timeout=60))
    device_types = (
        json.loads(command(["xcrun", "simctl", "list", "-j", "devicetypes"], capture=True, timeout=60))
        if args.display_target != "default" else {}
    )
    if args.inventory_output is not None:
        write_json(args.inventory_output, {
            "schema_version": 1,
            "display_target": args.display_target,
            "sdk_version": sdk_version,
            "simctl_list_runtimes_json": runtimes,
            "simctl_list_devicetypes_json": device_types,
            "qualification": "Raw `simctl list -j runtimes` and `simctl list -j devicetypes` JSON captured before display-target selection.",
        })
    selected = verify_storekit_test_runtime(sdk_version, runtimes, args.minimum_ios_version)
    display = verify_capture_display_target(args.display_target, sdk_version, runtimes, device_types)
    if display is not None:
        selected["capture_display_target"] = display
    print(json.dumps(selected, sort_keys=True))


def storekit_ui_test_project_spec() -> dict[str, Any]:
    """Build an XcodeGen spec with the StoreKit catalog in the XCTest bundle."""
    return {
        "name": "PleazyngTrialReview",
        "options": {
            "bundleIdPrefix": "org.snapetech.pleazyng.localtest",
            "xcodeVersion": "26.5",
            "deploymentTarget": {"iOS": "15.5"},
        },
        "targets": {
            "PleazyngTrialReviewUITests": {
                "type": "bundle.ui-testing",
                "platform": "iOS",
                "sources": [
                    {"path": "PleazyngTrialReviewUITests.swift", "buildPhase": "sources"},
                    {"path": STOREKIT_RELATIVE_PATH.name, "buildPhase": "resources"},
                ],
                "settings": {
                    "base": {
                        "PRODUCT_BUNDLE_IDENTIFIER": "org.snapetech.pleazyng.localtest.trial-review-tests",
                        "GENERATE_INFOPLIST_FILE": "YES",
                        "SWIFT_VERSION": "5.0",
                        "CODE_SIGN_STYLE": "Automatic",
                    },
                },
            },
        },
        "schemes": {
            "PleazyngTrialReview": {
                "build": {"targets": {"PleazyngTrialReviewUITests": ["test"]}},
                "test": {
                    "targets": ["PleazyngTrialReviewUITests"],
                    "captureScreenshotsAutomatically": False,
                    "deleteScreenshotsWhenEachTestSucceeds": True,
                },
            },
        },
    }


def verify_built_storekit_test_resource(built_products: Path, expected_sha256: str) -> Path:
    """Require the exact StoreKit catalog inside one built XCTest bundle."""
    built_products = built_products.resolve(strict=True)
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("Expected StoreKit configuration SHA-256 must be 64 lowercase hexadecimal characters")
    candidates = [
        path for path in built_products.rglob(STOREKIT_RELATIVE_PATH.name)
        if path.parent.suffix == ".xctest" and path.is_file() and not path.is_symlink()
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly one regular StoreKit configuration in a built XCTest bundle; found {len(candidates)}"
        )
    resource = candidates[0]
    if sha256_file(resource) != expected_sha256:
        raise ValueError("Built XCTest bundle StoreKit configuration does not match the pinned source catalog")
    return resource


def _xctestrun_test_targets(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Return test-target dictionaries from either supported plist layout."""
    configurations = document.get("TestConfigurations")
    if isinstance(configurations, list):
        targets: list[dict[str, Any]] = []
        for configuration in configurations:
            if not isinstance(configuration, dict):
                raise ValueError("XCTest plan contains a malformed test configuration")
            configured_targets = configuration.get("TestTargets")
            if not isinstance(configured_targets, list):
                raise ValueError("XCTest plan configuration does not contain a TestTargets array")
            if any(not isinstance(target, dict) for target in configured_targets):
                raise ValueError("XCTest plan contains a malformed test target")
            targets.extend(configured_targets)
        return targets

    targets = []
    for key, value in document.items():
        if key in {"__xctestrun_metadata__", "CodeCoverageBuildableInfos", "TestPlan"}:
            continue
        if isinstance(value, dict) and ("TestBundlePath" in value or "BlueprintName" in value):
            targets.append(value)
    return targets


def associate_xctestrun_with_app(xctestrun: Path, app_relative_path: str) -> dict[str, Any]:
    """Associate a UI test with its staged app using xcodebuild's documented fields."""
    xctestrun = xctestrun.resolve(strict=True)
    if app_relative_path != "__TESTROOT__/Debug-iphonesimulator/Pleazyng.app":
        raise ValueError("The UI target path must identify the retained Pleazyng app under TestROOT")
    document = plistlib.loads(xctestrun.read_bytes())
    if not isinstance(document, dict):
        raise ValueError("XCTest plan root must be a property-list dictionary")
    if document.get("UseDestinationArtifacts") is True:
        raise ValueError("The retained app association expects xcodebuild to install TestROOT artifacts")
    matches = [
        target for target in _xctestrun_test_targets(document)
        if target.get("BlueprintName") == "PleazyngTrialReviewUITests"
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one Pleazyng trial UI test target in xctestrun; found {len(matches)}")
    target = matches[0]
    if target.get("IsUITestBundle") is not True:
        raise ValueError("The selected XCTest plan target is not marked as a UI test bundle")
    if target.get("UseDestinationArtifacts") is True:
        raise ValueError("The retained app association expects xcodebuild to install TestROOT artifacts")
    previous_app_path = target.get("UITargetAppPath")
    if previous_app_path not in (None, app_relative_path):
        raise ValueError(f"XCTest plan already points at a different UI target app: {previous_app_path!r}")
    dependencies = target.get("DependentProductPaths", [])
    if not isinstance(dependencies, list) or any(not isinstance(path, str) for path in dependencies):
        raise ValueError("XCTest plan DependentProductPaths must be an array of path strings")
    if app_relative_path not in dependencies:
        dependencies.insert(0, app_relative_path)
    target["UITargetAppPath"] = app_relative_path
    target["DependentProductPaths"] = dependencies
    removed_provided_by_tests = target.pop("UseUITargetAppProvidedByTests", None)

    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{xctestrun.name}.", suffix=".tmp", dir=xctestrun.parent, delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=False))
    os.replace(temporary_path, xctestrun)

    verified = plistlib.loads(xctestrun.read_bytes())
    verified_targets = [
        item for item in _xctestrun_test_targets(verified)
        if item.get("BlueprintName") == "PleazyngTrialReviewUITests"
    ]
    if len(verified_targets) != 1:
        raise ValueError("Patched XCTest plan no longer contains exactly one expected UI test target")
    verified_target = verified_targets[0]
    if verified_target.get("UITargetAppPath") != app_relative_path:
        raise ValueError("Patched XCTest plan did not retain the UI target app path")
    if app_relative_path not in verified_target.get("DependentProductPaths", []):
        raise ValueError("Patched XCTest plan did not add the app to DependentProductPaths")
    if "UseUITargetAppProvidedByTests" in verified_target:
        raise ValueError("Patched XCTest plan still marks its target app as provided by test code")
    return {
        "blueprint_name": "PleazyngTrialReviewUITests",
        "ui_target_app_path": app_relative_path,
        "dependent_product_paths": verified_target["DependentProductPaths"],
        "removed_use_ui_target_app_provided_by_tests": removed_provided_by_tests,
    }


def codesign_identity(app: Path) -> dict[str, str]:
    """Verify a signed app, or explicitly record a wholly unsigned Simulator bundle."""
    verification = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        check=False,
        text=True,
        capture_output=True,
        timeout=300,
    )
    verification_output = verification.stdout + verification.stderr
    if verification.returncode != 0:
        unsigned_diagnostic = "code object is not signed at all" in verification_output.lower()
        has_bundle_signature = (app / "_CodeSignature").exists()
        if unsigned_diagnostic and not has_bundle_signature:
            return {"status": "unsigned"}
        raise RuntimeError(
            f"The retained Simulator app has an invalid code signature: {verification_output.strip()}"
        )
    result = subprocess.run(
        ["/usr/bin/codesign", "-dv", "--verbose=4", str(app)],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
    )
    details = result.stdout + result.stderr
    stable_fields = ("Identifier", "CDHash", "Signature", "TeamIdentifier")
    identity: dict[str, str] = {}
    for line in details.splitlines():
        for field in stable_fields:
            prefix = f"{field}="
            if line.startswith(prefix):
                identity[field] = line[len(prefix):]
    missing = [field for field in stable_fields if not identity.get(field)]
    if missing:
        raise ValueError(f"Unable to read stable code-signing identity fields: {missing!r}")
    return identity


def stage_verified_app_for_xctestrun(
    *,
    xctestrun: Path,
    built_products: Path,
    app: Path,
    expected_identity: dict[str, Any],
    expected_sha256: str,
    evidence_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    """Stage an unchanged retained app under TestROOT and associate the UI test."""
    xctestrun = xctestrun.resolve(strict=True)
    built_products = built_products.resolve(strict=True)
    app = app.resolve(strict=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    if xctestrun.parent != built_products:
        raise ValueError("The xctestrun file must be directly under its built-products TestROOT")
    if app.name != "Pleazyng.app":
        raise ValueError(f"Unexpected retained app bundle name: {app.name!r}")

    source_hash = sha256_tree(app)
    if source_hash != expected_sha256:
        raise ValueError("Retained app changed after its producer receipt was verified")
    source_identity = bundle_info(app)
    if source_identity != expected_identity:
        raise ValueError("Retained app identity changed after its producer receipt was verified")
    source_signature = codesign_identity(app)

    app_relative_path = "__TESTROOT__/Debug-iphonesimulator/Pleazyng.app"
    destination_parent = built_products / "Debug-iphonesimulator"
    if destination_parent.is_symlink():
        raise ValueError("Refusing a symlinked XCTest app-staging directory")
    destination_parent.mkdir(parents=True, exist_ok=True)
    destination = destination_parent / "Pleazyng.app"
    if destination.exists() or destination.is_symlink():
        raise ValueError("Refusing to overwrite an existing XCTest app-staging destination")

    shutil.copytree(app, destination, symlinks=True, copy_function=shutil.copy2)
    staged_hash = sha256_tree(destination)
    if staged_hash != source_hash:
        raise ValueError("Staged UI target app is not byte-identical to the retained product app")
    staged_identity = bundle_info(destination)
    if staged_identity != source_identity:
        raise ValueError("Staged UI target app identity differs from the retained product app")
    staged_signature = codesign_identity(destination)
    if staged_signature != source_signature:
        raise ValueError("Staged UI target app code signature differs from the retained product app")

    before_plan_sha = sha256_file(xctestrun)
    before_plan_copy = evidence_dir / "pleazyng-trial-review-before-association.xctestrun"
    shutil.copyfile(xctestrun, before_plan_copy)
    association = associate_xctestrun_with_app(xctestrun, app_relative_path)
    after_plan_sha = sha256_file(xctestrun)
    after_plan_copy = evidence_dir / "pleazyng-trial-review-after-association.xctestrun"
    shutil.copyfile(xctestrun, after_plan_copy)
    evidence = {
        "schema_version": 1,
        "qualification": "The UI target path and XCTest dependency metadata refer to an unchanged copy of the receipt-verified retained Simulator app.",
        "product_app_bundle_sha256_before": source_hash,
        "product_app_bundle_sha256_staged": staged_hash,
        "product_app_identity_before": source_identity,
        "product_app_identity_staged": staged_identity,
        "product_app_codesign_identity_before": source_signature,
        "product_app_codesign_identity_staged": staged_signature,
        "ui_target_app_bundle_id": source_identity["bundle_id"],
        "staged_app_path": str(destination),
        "xctestrun_path": str(xctestrun),
        "xctestrun_sha256_before": before_plan_sha,
        "xctestrun_sha256_after": after_plan_sha,
        "xctestrun_before_copy": before_plan_copy.name,
        "xctestrun_after_copy": after_plan_copy.name,
        **association,
    }
    write_json(evidence_dir / "xctestrun-app-association.json", evidence)
    return destination, evidence


def install_verified_app_on_simulator(
    *,
    device_id: str,
    app: Path,
    expected_identity: dict[str, Any],
    expected_sha256: str,
    diagnostic_dir: Path,
) -> dict[str, Any]:
    """Install the exact staged simulator app before StoreKitTest configures the app."""
    app = app.resolve(strict=True)
    if app.name != "Pleazyng.app":
        raise ValueError(f"Unexpected Simulator app bundle name: {app.name!r}")
    if sha256_tree(app) != expected_sha256:
        raise ValueError("Staged Simulator app changed before installation")
    if bundle_info(app) != expected_identity:
        raise ValueError("Staged Simulator app identity changed before installation")

    run_simulator_command(
        ["xcrun", "simctl", "install", device_id, str(app)],
        diagnostic_dir,
        "simctl-install-retained-app",
        timeout=300,
    )
    service_diagnostics = capture_simulator_service_diagnostics(device_id, diagnostic_dir, "after-app-install")
    launchctl_status = service_diagnostics.get("launchctl", {})
    listapps_status = service_diagnostics.get("listapps", {})
    if not isinstance(launchctl_status, dict) or launchctl_status.get("returncode") != 0:
        raise RuntimeError("The simulator launchd service did not respond after app installation")
    if not isinstance(listapps_status, dict) or listapps_status.get("returncode") != 0:
        raise RuntimeError("The simulator could not return its installed-app inventory after app installation")
    if listapps_status.get("expected_bundle_id_text_present") is not True:
        raise RuntimeError("The installed-app inventory does not contain the retained Pleazyng bundle ID")
    try:
        installed_path_value = run_simulator_command(
            ["xcrun", "simctl", "get_app_container", device_id, BUNDLE_ID, "app"],
            diagnostic_dir,
            "simctl-get-app-container",
            timeout=120,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        capture_simulator_service_diagnostics(device_id, diagnostic_dir, "get-app-container-failed")
        raise
    installed_app = Path(installed_path_value).resolve(strict=True)
    if installed_app == app or installed_app.name != "Pleazyng.app" or not installed_app.is_dir():
        raise ValueError("simctl did not return the installed Pleazyng app container")
    installed_hash = sha256_tree(installed_app)
    if installed_hash != expected_sha256:
        raise ValueError("Simulator-installed app is not byte-identical to the verified staged app")
    installed_identity = bundle_info(installed_app)
    if installed_identity != expected_identity:
        raise ValueError("Simulator-installed app identity differs from the receipt-verified product app")
    return {
        "schema_version": 1,
        "simulator_udid": device_id,
        "bundle_id": BUNDLE_ID,
        "installed_app_path": str(installed_app),
        "installed_app_bundle_sha256": installed_hash,
        "installed_app_identity": installed_identity,
        "qualification": "simctl installed the verified staged app before SKTestSession setup; app container hash and bundle identity match the staged app.",
    }


def capture_simulator_storekit_logs(device_id: str, output: Path) -> None:
    """Retain bounded storekitd diagnostics from the just-finished simulator test."""
    log_path = output / "simulator-storekitd.log"
    command_args = [
        "xcrun", "simctl", "spawn", device_id, "log", "show",
        "--style", "compact", "--last", "15m", "--info",
        "--predicate", 'process == "storekitd"',
    ]
    try:
        with log_path.open("w", encoding="utf-8") as log_file:
            result = subprocess.run(
                command_args,
                check=False,
                text=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=180,
            )
    except subprocess.TimeoutExpired:
        write_json(output / "simulator-storekitd-log-error.json", {"error": "simulator storekitd log query timed out"})
    else:
        if result.returncode != 0:
            write_json(
                output / "simulator-storekitd-log-error.json",
                {"error": "simulator storekitd log query failed", "exit_code": result.returncode},
            )


def verify_storekit_restore_sync(log_path: Path, localtest_record: dict[str, Any]) -> dict[str, Any]:
    """Correlate Restore tap timing with the app's completed StoreKit sync."""
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    behavior = localtest_record.get("appBehavior")
    timing = behavior.get("restoreActionTiming") if isinstance(behavior, dict) else None
    if not isinstance(behavior, dict) or behavior.get("restoreActionTapCompleted") is not True:
        raise ValueError("XCTest did not complete the actual Restore control tap")
    if not isinstance(timing, dict) or timing.get("windowEndsBeforeFreshRelaunch") is not True:
        raise ValueError("XCTest did not record a Restore observation window before fresh relaunch")
    start_ms = timing.get("windowStartedAtEpochMilliseconds")
    tap_returned_ms = timing.get("tapReturnedAtEpochMilliseconds")
    end_ms = timing.get("windowCompletedAtEpochMilliseconds")
    before_relaunch_ms = timing.get("preRelaunchAtEpochMilliseconds")
    offset_seconds = timing.get("timezoneOffsetSeconds")
    if any(type(value) is not int for value in (start_ms, tap_returned_ms, end_ms, before_relaunch_ms, offset_seconds)):
        raise ValueError("XCTest Restore observation window has invalid clock values")
    if (
        not start_ms <= tap_returned_ms <= end_ms == before_relaunch_ms
        or abs(offset_seconds) >= 24 * 60 * 60
    ):
        raise ValueError("XCTest Restore observation window is invalid")

    purchase_phrase = f"b:{BUNDLE_ID} p:{TRIAL_PRODUCT_ID} s:1"
    purchase_index = next(
        (
            index for index, line in enumerate(lines)
            if "Purchase completed successfully. Transactions:" in line and purchase_phrase in line
        ),
        None,
    )
    if purchase_index is None:
        raise ValueError("StoreKit daemon log has no successful LOCALTEST trial purchase for the app")

    sync_start_indexes = [
        index for index in range(purchase_index + 1, len(lines))
        if 'Starting transactions sync with reason "reset(' in lines[index]
        and f"for {BUNDLE_ID}" in lines[index]
    ]
    if not sync_start_indexes:
        raise ValueError("StoreKit daemon log has no app-account sync after the LOCALTEST trial purchase")

    def process_id(index: int) -> str | None:
        match = re.search(r"storekitd\[(\d+):", lines[index])
        return match.group(1) if match else None

    def log_timestamp(index: int) -> str | None:
        match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)", lines[index])
        return match.group(1) if match else None

    def log_epoch_milliseconds(index: int) -> int:
        value = log_timestamp(index)
        if value is None:
            raise ValueError(f"StoreKit daemon log line {index + 1} has no parseable timestamp")
        try:
            parsed = datetime.fromisoformat(value).replace(tzinfo=timezone(timedelta(seconds=offset_seconds)))
        except ValueError as error:
            raise ValueError(f"StoreKit daemon log line {index + 1} has an invalid timestamp") from error
        return int(parsed.timestamp() * 1000)

    purchase_ms = log_epoch_milliseconds(purchase_index)
    tolerated_start = start_ms - RESTORE_SYNC_CLOCK_TOLERANCE_MS
    tolerated_end = end_ms + RESTORE_SYNC_CLOCK_TOLERANCE_MS
    if purchase_ms >= start_ms:
        raise ValueError("The successful trial purchase did not precede the recorded Restore tap window")

    completed_syncs: list[tuple[int, int, int, int]] = []
    saw_completed_sync = False
    saw_process_identity = False
    for sync_start_index in sync_start_indexes:
        sync_pid = process_id(sync_start_index)
        if sync_pid is None:
            continue
        saw_process_identity = True
        sync_finished_index = next(
            (
                index for index in range(sync_start_index + 1, len(lines))
                if "Finished syncing transactions" in lines[index]
                and process_id(index) == sync_pid
            ),
            None,
        )
        if sync_finished_index is None:
            continue
        saw_completed_sync = True
        sync_start_ms = log_epoch_milliseconds(sync_start_index)
        sync_finished_ms = log_epoch_milliseconds(sync_finished_index)
        if sync_finished_ms < sync_start_ms:
            continue
        if tolerated_start <= sync_start_ms <= tolerated_end and tolerated_start <= sync_finished_ms <= tolerated_end:
            completed_syncs.append((sync_start_index, sync_finished_index, sync_start_ms, sync_finished_ms))

    if not saw_process_identity:
        raise ValueError("StoreKit daemon sync-start log line has no process identity")
    if not saw_completed_sync:
        raise ValueError("StoreKit daemon log has no completed transaction sync after the Restore tap")
    if not completed_syncs:
        raise ValueError("StoreKit daemon sync did not finish within the recorded Restore tap window")
    sync_start_index, sync_finished_index, sync_start_ms, sync_finished_ms = completed_syncs[0]
    if purchase_ms >= sync_start_ms:
        raise ValueError("StoreKit daemon account sync did not follow the successful trial purchase")

    return {
        "schema_version": 1,
        "environment": "LOCALTEST",
        "bundle_id": BUNDLE_ID,
        "trial_product_id": TRIAL_PRODUCT_ID,
        "successful_trial_purchase_log_line": purchase_index + 1,
        "successful_trial_purchase_log_timestamp": log_timestamp(purchase_index),
        "post_purchase_app_store_sync_log_line": sync_start_index + 1,
        "post_purchase_app_store_sync_log_timestamp": log_timestamp(sync_start_index),
        "completed_sync_log_line": sync_finished_index + 1,
        "completed_sync_log_timestamp": log_timestamp(sync_finished_index),
        "restore_window_started_at_epoch_milliseconds": start_ms,
        "restore_tap_returned_at_epoch_milliseconds": tap_returned_ms,
        "restore_window_completed_at_epoch_milliseconds": end_ms,
        "pre_relaunch_at_epoch_milliseconds": before_relaunch_ms,
        "restore_window_timezone_offset_seconds": offset_seconds,
        "clock_tolerance_milliseconds": RESTORE_SYNC_CLOCK_TOLERANCE_MS,
        "post_purchase_app_store_sync_observed": True,
        "completed_sync_inside_restore_window": True,
    }


def _attachment_name_matches(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    generated_suffix = rf"{re.escape(expected)}_\d+_[0-9A-Fa-f-]{{36}}(?:\.[A-Za-z0-9]+)?"
    return re.fullmatch(generated_suffix, actual) is not None


def retain_consumer_provenance(source: Path, output: Path) -> Path:
    """Copy consumer provenance into the evidence directory unless already there."""
    source = source.resolve(strict=True)
    if source.name != "consumer-provenance.json":
        raise ValueError("Retained build consumer provenance has an unexpected filename")
    destination = (output / source.name).resolve()
    if source != destination:
        shutil.copyfile(source, destination)
    return destination


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def simulator_device_record(device_id: str, inventory: dict[str, Any]) -> dict[str, Any]:
    devices = inventory.get("devices")
    if not isinstance(devices, dict):
        raise ValueError("simctl device inventory does not contain a devices map")
    matches = [
        {**device, "runtime_id": runtime_id}
        for runtime_id, runtime_devices in devices.items()
        if isinstance(runtime_devices, list)
        for device in runtime_devices
        if isinstance(device, dict)
        and isinstance(device.get("udid"), str)
        and device["udid"].casefold() == device_id.casefold()
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one simulator record for {device_id}; found {len(matches)}")
    return matches[0]


def read_simulator_device_record(device_id: str, output_dir: Path, name: str) -> dict[str, Any]:
    result = run_simulator_command(
        ["xcrun", "simctl", "list", "devices", "-j"],
        output_dir,
        name,
        timeout=60,
    )
    try:
        inventory = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"simctl device inventory is not valid JSON ({name})") from error
    if not isinstance(inventory, dict):
        raise ValueError(f"simctl device inventory has an unexpected top-level value ({name})")
    record = simulator_device_record(device_id, inventory)
    write_json(output_dir / f"{name}.json", {
        "simulator_udid": device_id,
        "device": record,
        "raw_inventory_stdout": f"{name}.stdout.txt",
    })
    return record


def capture_simulator_service_diagnostics(
    device_id: str,
    output_dir: Path,
    phase: str,
    *,
    include_devices: bool = True,
    include_launchctl: bool = True,
) -> dict[str, Any]:
    """Keep the exact device state, launchd response, and installed-app inventory."""
    outcomes: dict[str, Any] = {"simulator_udid": device_id, "phase": phase}
    commands = []
    if include_devices:
        commands.append(("devices", ["xcrun", "simctl", "list", "devices", "-j"], 60))
    if include_launchctl:
        commands.append(("launchctl", ["xcrun", "simctl", "spawn", device_id, "launchctl", "list"], 60))
    commands.append(("listapps", ["xcrun", "simctl", "listapps", device_id], 90))
    for label, command_args, timeout in commands:
        name = f"simulator-{phase}-{label}"
        try:
            result = run_simulator_command(command_args, output_dir, name, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            outcomes[label] = {"error": f"{type(error).__name__}: {error}"}
            continue
        outcome = {
            "returncode": result.returncode,
            "stdout_file": f"{name}.stdout.txt",
            "stderr_file": f"{name}.stderr.txt",
        }
        if label == "devices" and result.returncode == 0:
            try:
                inventory = json.loads(result.stdout)
                outcome["device"] = simulator_device_record(device_id, inventory)
            except (json.JSONDecodeError, ValueError, TypeError) as error:
                outcome["inventory_error"] = f"{type(error).__name__}: {error}"
        elif label == "listapps" and result.returncode == 0:
            outcome["expected_bundle_id_text_present"] = BUNDLE_ID in result.stdout
        outcomes[label] = outcome
    write_json(output_dir / f"simulator-{phase}-diagnostics.json", outcomes)
    return outcomes


def ensure_simulator_ready_after_xctest_build(
    device: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    """Restore and verify the exact Simulator after Xcode's runner build may stop it."""
    device_id = device["udid"]
    before = read_simulator_device_record(device_id, output_dir, "simulator-after-xctest-build-before")
    if (
        before.get("runtime_id") != device.get("runtime_id")
        or before.get("deviceTypeIdentifier") != device.get("device_type")
    ):
        capture_simulator_service_diagnostics(device_id, output_dir, "post-build-identity-mismatch")
        raise RuntimeError("The post-build Simulator record does not match the exact selected runtime and device type")
    initial_state = before.get("state")
    booted_here = initial_state == "Shutdown"
    if initial_state == "Shutdown":
        run_simulator_command(
            ["xcrun", "simctl", "boot", device_id],
            output_dir,
            "simulator-after-xctest-build-boot",
            timeout=180,
        )
    elif initial_state not in ("Booted", "Booting"):
        capture_simulator_service_diagnostics(device_id, output_dir, "unexpected-post-build-state")
        raise RuntimeError(
            f"Expected the selected Simulator to be Booted or Shutdown after XCTest build; found {initial_state!r}"
        )

    bootstatus = run_simulator_command(
        ["xcrun", "simctl", "bootstatus", device_id, "-b"],
        output_dir,
        "simulator-after-xctest-build-bootstatus",
        timeout=600,
    )
    receipt = {
        "schema_version": 1,
        "simulator_udid": device_id,
        "runtime_id": device.get("runtime_id"),
        "device_type": device.get("device_type"),
        "device_state_before_recovery": initial_state,
        "booted_after_xctest_runner_build": booted_here,
        "device_state_after_bootstatus": "Booted",
        "bootstatus_returncode": bootstatus.returncode,
        "post_build_inventory": "omitted; the exact-UDID bootstatus result is authoritative",
        "launchctl_preflight": "omitted; exact-device app installation and XCTest are authoritative",
        "qualification": (
            "The exact selected Simulator completed simctl bootstatus -b after xcodebuild build-for-testing. "
            "The following exact-UDID install and XCTest actions determine whether Simulator services are ready."
        ),
    }
    write_json(output_dir / "simulator-readiness-after-xctest-build.json", receipt)
    return receipt


def run_logged_command(args: list[str], log_path: Path, *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run Xcode with durable combined output and a timeout receipt."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("w", encoding="utf-8") as log_file:
            return subprocess.run(
                args,
                check=False,
                text=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired as error:
        evidence: dict[str, Any] = {
            "command": args,
            "timeout_seconds": timeout,
            "combined_stdout_stderr_log": log_path.name,
        }
        for stream_name, stream in (
            ("stdout", getattr(error, "stdout", None) or getattr(error, "output", None)),
            ("stderr", getattr(error, "stderr", None)),
        ):
            if stream is None:
                continue
            stream_bytes = stream if isinstance(stream, bytes) else str(stream).encode("utf-8")
            stream_path = log_path.with_name(f"{log_path.name}.{stream_name}.timeout.txt")
            stream_path.write_bytes(stream_bytes)
            evidence[f"captured_{stream_name}_log"] = stream_path.name
        write_json(log_path.with_name(f"{log_path.name}.timeout.json"), evidence)
        raise RuntimeError(
            f"Xcode command timed out after {timeout}s; combined stdout/stderr retained in {log_path.name}"
        ) from error


def failure_diagnostic_files(output: Path) -> list[str]:
    return sorted(
        path.name for path in output.iterdir()
        if path.is_file() and (
            ".timeout." in path.name
            or path.name.startswith(("simulator-", "simctl-"))
            or path.name in {"xctest-runner-build.log", "storekit-ui-test.log", "simulator-storekitd.log"}
        )
    )


def record_build(args: argparse.Namespace) -> None:
    root = args.product_root.resolve(strict=True)
    source = verify_source(root, args.product_source_sha)
    app = args.app.resolve(strict=True)
    identity = bundle_info(app)
    command(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], timeout=300)
    if (app / "embedded.mobileprovision").exists():
        raise ValueError("An iOS Simulator app unexpectedly contains a provisioning profile")
    signing_result = subprocess.run(
        ["/usr/bin/codesign", "-dv", "--verbose=4", str(app)],
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
    )
    signing = (signing_result.stdout + signing_result.stderr).strip()
    storekit = args.capture_tools_root.resolve(strict=True) / STOREKIT_RELATIVE_PATH
    ui_test = args.capture_tools_root.resolve(strict=True) / UITEST_RELATIVE_PATH
    catalog = storekit_catalog(storekit)
    tools_sha = args.capture_tools_sha
    if re.fullmatch(r"[0-9a-f]{40}", tools_sha) is None:
        raise ValueError("Capture tool source SHA must be an exact lowercase 40-character Git commit")
    tools_root = args.capture_tools_root.resolve(strict=True)
    actual_tools_sha = git(tools_root, "rev-parse", "HEAD").stdout.strip()
    if actual_tools_sha != tools_sha:
        raise ValueError(f"Capture tools checkout is {actual_tools_sha}; expected exact SHA {tools_sha}")

    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    archive = out / build_artifact_name()
    with tarfile.open(archive, "w:gz", compresslevel=6) as bundle:
        bundle.add(app, arcname="Pleazyng.app", recursive=True)
    receipt = {
        "schema_version": 1,
        "purpose": f"Retained v{VERSION} iOS Simulator product build for LOCALTEST StoreKitTest review capture.",
        **source,
        "product_bundle_id": BUNDLE_ID,
        "product_app_bundle_sha256": sha256_tree(app),
        "product_app_archive": archive.name,
        "product_app_archive_sha256": sha256_file(archive),
        "product_app": identity,
        "product_build_configuration": "Debug iOS Simulator, Xcode-managed Simulator signing",
        "product_signing_details": signing,
        "product_source_checkout_path": str(root),
        "producer_run_id": args.producer_run_id,
        "producer_run_attempt": args.producer_run_attempt,
        "capture_tools_sha": tools_sha,
        "capture_tools_checkout_sha": actual_tools_sha,
        "storekit_configuration_sha256": sha256_file(storekit),
        "ui_test_source_sha256": sha256_file(ui_test),
        "local_test_catalog": catalog,
        "storekit_configuration_in_product_app": False,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "qualification": "Local Simulator build only; not a signed App Store product and not purchase qualification.",
    }
    receipt_path = out / RECEIPT_NAME
    write_json(receipt_path, receipt)
    print(json.dumps({"receipt": str(receipt_path), "app_archive": str(archive), "app_sha256": receipt["product_app_bundle_sha256"]}))


def _safe_extract_bundle(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if not members:
            raise ValueError("Retained app archive is empty")
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "Pleazyng.app":
                raise ValueError(f"Refusing unsafe app archive path: {member.name!r}")
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError(f"Refusing unexpected app archive member type: {member.name!r}")
            target = root.joinpath(*name.parts)
            if member.issym():
                link = PurePosixPath(member.linkname)
                if link.is_absolute():
                    raise ValueError(f"Refusing absolute app bundle symlink: {member.name!r}")
                resolved = (target.parent / Path(*link.parts)).resolve()
                if resolved != root and root not in resolved.parents:
                    raise ValueError(f"Refusing escaping app bundle symlink: {member.name!r}")
        for member in members:
            target = root.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(member.mode & 0o777)
            elif member.issym():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = bundle.extractfile(member)
                if stream is None:
                    raise ValueError(f"Unable to read retained app file: {member.name!r}")
                with stream, target.open("wb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(member.mode & 0o777)
    return destination / "Pleazyng.app"


def restore_build(args: argparse.Namespace) -> None:
    artifact_dir = args.artifact_dir.resolve(strict=True)
    receipt_path = artifact_dir / RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("product_source_sha") != args.product_source_sha:
        raise ValueError("Retained product app belongs to a different exact source SHA")
    if receipt.get("producer_run_id") != args.producer_run_id:
        raise ValueError("Retained app receipt belongs to a different producer workflow run")
    if receipt.get("producer_run_attempt") != args.producer_run_attempt:
        raise ValueError("Retained app receipt belongs to a different producer workflow attempt")
    if receipt.get("capture_tools_sha") != args.producer_tools_sha:
        raise ValueError("Retained app receipt capture-tools SHA does not match its producer workflow revision")
    if (receipt.get("product_version"), str(receipt.get("product_build"))) != (VERSION, BUILD_NUMBER):
        raise ValueError(f"Retained app receipt is not for the expected {VERSION}+{BUILD_NUMBER} product")
    if receipt.get("product_source_tag") != SOURCE_TAG:
        raise ValueError("Retained app receipt does not match the exact selected product candidate tag")
    archive_name = receipt.get("product_app_archive")
    if archive_name != build_artifact_name() or Path(archive_name).name != archive_name:
        raise ValueError("Retained product app receipt names an unexpected archive")
    archive = artifact_dir / archive_name
    if sha256_file(archive) != receipt.get("product_app_archive_sha256"):
        raise ValueError("Retained product app archive SHA-256 does not match its producer receipt")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    app = _safe_extract_bundle(archive, output / "restore")
    identity = bundle_info(app)
    if sha256_tree(app) != receipt.get("product_app_bundle_sha256"):
        raise ValueError("Extracted app bundle SHA-256 does not match its producer receipt")
    command(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], timeout=300)
    consumer = {
        "schema_version": 1,
        "consumer_product_source_sha": args.product_source_sha,
        "consumer_product_source_tag": receipt.get("product_source_tag"),
        "consumer_product_version": receipt.get("product_version"),
        "consumer_product_build": receipt.get("product_build"),
        "producer_run_id": args.producer_run_id,
        "producer_run_attempt": args.producer_run_attempt,
        "producer_receipt_sha256": sha256_file(receipt_path),
        "producer_archive_sha256": sha256_file(archive),
        "restored_app_bundle_sha256": sha256_tree(app),
        "restored_app_identity": identity,
        "producer_capture_tools_sha": receipt.get("capture_tools_sha"),
        "qualification": "Exact retained Simulator product app verified before install; no rebuild or signing transformation.",
    }
    write_json(output / "consumer-provenance.json", consumer)
    print(json.dumps({"app": str(app), "receipt": str(receipt_path)}))


def _export_attachments(result_bundle: Path, output: Path) -> dict[str, Path]:
    export_dir = output / "xcresult-attachments"
    export_dir.mkdir(parents=True, exist_ok=True)
    command(["xcrun", "xcresulttool", "export", "attachments", "--path", str(result_bundle), "--output-path", str(export_dir)], timeout=240)
    manifest_path = export_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    all_attachments = [attachment for test in manifest for attachment in test.get("attachments", [])]
    exported: dict[str, Path] = {}
    for attachment_name, filename in SCREENSHOTS.items():
        matches = [
            item for item in all_attachments
            if _attachment_name_matches(str(item.get("suggestedHumanReadableName", "")), attachment_name)
        ]
        if len(matches) != 1:
            raise RuntimeError(f"Expected exactly one authentic screenshot attachment named {attachment_name!r}; found {len(matches)}")
        exported_name = matches[0].get("exportedFileName", "")
        if not exported_name or Path(exported_name).name != exported_name:
            raise ValueError(f"Invalid exported attachment filename for {attachment_name!r}")
        screenshot = export_dir / exported_name
        if not screenshot.is_file():
            raise FileNotFoundError(f"XCTest screenshot attachment is missing: {screenshot}")
        destination = output / filename
        shutil.copyfile(screenshot, destination)
        exported[attachment_name] = destination

    record_attachment_name = "pleazyng-trial-storekit-localtest-record"
    record_matches = [
        item for item in all_attachments
        if _attachment_name_matches(str(item.get("suggestedHumanReadableName", "")), record_attachment_name)
    ]
    if len(record_matches) != 1:
        raise RuntimeError(f"Expected one StoreKitTest LOCALTEST transaction record; found {len(record_matches)}")
    record_name = record_matches[0].get("exportedFileName", "")
    if not record_name or Path(record_name).name != record_name:
        raise ValueError("Invalid exported LOCALTEST record attachment filename")
    record_path = export_dir / record_name
    if not record_path.is_file():
        raise FileNotFoundError("XCTest transaction record attachment was not exported")
    exported["localtest_record"] = record_path
    return exported


def capture(args: argparse.Namespace) -> None:
    product_root = args.product_root.resolve(strict=True)
    source = verify_source(product_root, args.product_source_sha)
    tools_root = args.capture_tools_root.resolve(strict=True)
    tools_sha = args.capture_tools_sha
    if re.fullmatch(r"[0-9a-f]{40}", tools_sha) is None:
        raise ValueError("Capture tool source SHA must be an exact lowercase 40-character Git commit")
    current_tools_sha = git(tools_root, "rev-parse", "HEAD").stdout.strip()
    if current_tools_sha != tools_sha:
        raise ValueError(f"Capture tools checkout is {current_tools_sha}; expected exact SHA {tools_sha}")
    storekit_path = tools_root / STOREKIT_RELATIVE_PATH
    ui_test_path = tools_root / UITEST_RELATIVE_PATH
    catalog = storekit_catalog(storekit_path)
    app = args.app.resolve(strict=True)
    identity = bundle_info(app)
    app_hash = sha256_tree(app)
    receipt_path = args.build_receipt.resolve(strict=True)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("product_source_sha") != args.product_source_sha:
        raise ValueError("Built app receipt source SHA does not match the selected product source")
    if receipt.get("product_source_tag") != source["product_source_tag"]:
        raise ValueError("Built app receipt candidate tag does not match the selected product source")
    if (receipt.get("product_version"), str(receipt.get("product_build"))) != (
        source["product_version"], source["product_build"]
    ):
        raise ValueError("Built app receipt version/build does not match the selected product source")
    if receipt.get("product_app_bundle_sha256") != app_hash:
        raise ValueError("Installed app bundle does not match the retained product build receipt")
    if receipt.get("product_app") != identity:
        raise ValueError("Built app identity differs from its producer receipt")

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    copied_receipt = output / "product-build-provenance.json"
    if receipt_path != copied_receipt:
        shutil.copyfile(receipt_path, copied_receipt)
    consumer_path = args.consumer_provenance
    if consumer_path is None and (output / "consumer-provenance.json").is_file():
        consumer_path = output / "consumer-provenance.json"
    if consumer_path is not None:
        consumer_path = retain_consumer_provenance(consumer_path, output)
    device: dict[str, str] | None = None
    failure: str | None = None
    result_bundle: Path | None = None
    simulator_signing: dict[str, Any] | None = None
    installed_app_receipt: dict[str, Any] | None = None
    try:
        xcode_version = command(["xcodebuild", "-version"], capture=True, timeout=60)
        write_json(output / "xcode-version-preflight.json", {
            "command": ["xcodebuild", "-version"],
            "output": xcode_version,
            "developer_directory": command(["xcode-select", "-p"], capture=True, timeout=30),
            "qualification": "Captured before simulator creation and app installation.",
        })
        device = create_storekit_simulator(
            args.display_target,
            args.run_id,
            args.run_attempt,
            diagnostic_dir=output,
        )
        write_json(output / "simulator.json", device)
        project_root = output / "storekit-ui-runner"
        project_root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ui_test_path, project_root / "PleazyngTrialReviewUITests.swift")
        shutil.copyfile(storekit_path, project_root / "pleazyng-trial-review.storekit")
        project_spec_data = storekit_ui_test_project_spec()
        project_spec = project_root / "project.json"
        write_json(project_spec, project_spec_data)
        command(["xcodegen", "generate", "--spec", str(project_spec), "--project", str(project_root)], timeout=180)
        project = project_root / "PleazyngTrialReview.xcodeproj"

        derived_data = project_root / "derived-data"
        build_log = output / "xctest-runner-build.log"
        build_command = [
            "xcodebuild", "build-for-testing",
            "-project", str(project),
            "-scheme", "PleazyngTrialReview",
            "-destination", f"platform=iOS Simulator,id={device['udid']}",
            "-derivedDataPath", str(derived_data),
            "-parallel-testing-enabled", "NO",
        ]
        build = run_logged_command(build_command, build_log, timeout=900)
        if build.returncode != 0:
            raise RuntimeError(f"XCTest-only runner build failed ({build.returncode}); see {build_log}")

        built_products = derived_data / "Build" / "Products"
        verify_built_storekit_test_resource(built_products, sha256_file(storekit_path))

        xctestruns = list((derived_data / "Build" / "Products").glob("*.xctestrun"))
        if len(xctestruns) != 1:
            raise ValueError(f"Expected exactly one XCTest plan, found {len(xctestruns)}")
        staged_app, _ = stage_verified_app_for_xctestrun(
            xctestrun=xctestruns[0],
            built_products=built_products,
            app=app,
            expected_identity=identity,
            expected_sha256=receipt["product_app_bundle_sha256"],
            evidence_dir=output,
        )
        if staged_app != built_products / "Debug-iphonesimulator" / "Pleazyng.app":
            raise ValueError("The staged UI target app path differs from the xctestrun TestROOT path")
        stage_evidence = json.loads((output / "xctestrun-app-association.json").read_text(encoding="utf-8"))
        if (stage_evidence.get("product_app_bundle_sha256_before") != receipt["product_app_bundle_sha256"]
                or stage_evidence.get("product_app_bundle_sha256_staged") != receipt["product_app_bundle_sha256"]
                or stage_evidence.get("product_app_codesign_identity_before") != {"status": "unsigned"}
                or stage_evidence.get("product_app_codesign_identity_staged") != {"status": "unsigned"}):
            raise ValueError("The XCTest app staging copy differs from the unchanged unsigned producer app")
        stage_evidence.update({
            "product_app_source_unchanged": True,
            "qualification": (
                "The producer app remains byte-identical and unsigned. The XCTest-staged simulator copy is also "
                "byte-identical and unsigned; no additional code signature was applied."
            ),
        })
        write_json(output / "xctestrun-app-association.json", stage_evidence)
        ensure_simulator_ready_after_xctest_build(device, output)
        installed_app_receipt = install_verified_app_on_simulator(
            device_id=device["udid"],
            app=staged_app,
            expected_identity=identity,
            expected_sha256=receipt["product_app_bundle_sha256"],
            diagnostic_dir=output,
        )
        installed_app_receipt["producer_app_bundle_sha256"] = receipt["product_app_bundle_sha256"]
        installed_app_receipt["simulator_signing_applied"] = False
        installed_app_receipt["qualification"] = (
            "simctl installed the byte-identical unsigned XCTest-staged copy of the receipt-verified app; its app "
            "container hash and bundle identity match that staged copy. The producer app remains unchanged."
        )
        write_json(output / "simulator-installed-app.json", installed_app_receipt)
        result_bundle = output / "pleazyng-trial-review.xcresult"
        test_command = [
            "xcodebuild", "test-without-building",
            "-xctestrun", str(xctestruns[0]),
            "-destination", f"platform=iOS Simulator,id={device['udid']}",
            "-derivedDataPath", str(derived_data),
            "-resultBundlePath", str(result_bundle),
            "-parallel-testing-enabled", "NO",
            "-testLanguage", "en",
            "-testRegion", "US",
            "-only-testing:PleazyngTrialReviewUITests/PleazyngTrialReviewUITests/testCaptureRealTrialOfferAndActiveAccess",
        ]
        test_log = output / "storekit-ui-test.log"
        try:
            result = run_logged_command(test_command, test_log, timeout=1800)
        finally:
            capture_simulator_storekit_logs(device["udid"], output)
        if result_bundle.is_dir():
            try:
                exported = _export_attachments(result_bundle, output)
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                write_json(output / "attachment-export-error.json", {"error": str(error)})
                exported = {}
        else:
            exported = {}
        if result.returncode != 0:
            raise RuntimeError(f"Native StoreKitTest UI flow failed ({result.returncode}); see {test_log}")
        if set(exported) != {*SCREENSHOTS, "localtest_record"}:
            raise RuntimeError("The successful UI test did not export every required genuine screenshot and LOCALTEST record")

        localtest = json.loads(exported["localtest_record"].read_text(encoding="utf-8"))
        if localtest.get("environment") != "LOCALTEST":
            raise ValueError("XCTest transaction evidence is not labeled LOCALTEST")
        restore_sync_evidence = verify_storekit_restore_sync(
            output / "simulator-storekitd.log",
            localtest,
        )
        write_json(output / "storekit-restore-sync.json", restore_sync_evidence)
        queries = localtest.get("productQueries", {})
        if queries.get("trialProductVisibleInApp") is not True or queries.get("lifetimeProductVisibleInApp") is not True:
            raise ValueError("The app did not show both actual LOCALTEST product query results")
        if "$2.99" not in queries.get("lifetimeDisplayPrice", ""):
            raise ValueError("The app's actual LOCALTEST lifetime price label did not show $2.99")
        transactions = localtest.get("allLocalTestTransactions")
        if not isinstance(transactions, list) or len(transactions) != 1:
            raise ValueError("LOCALTEST must contain exactly the free trial transaction and no lifetime transaction")
        transaction = transactions[0]
        if transaction.get("productID") != TRIAL_PRODUCT_ID or transaction.get("state") != "purchased":
            raise ValueError("LOCALTEST transaction is not a completed free-trial product")
        if localtest.get("appBehavior", {}).get("lifetimeTransactionCreated") is not False:
            raise ValueError("UI test did not explicitly prove that no lifetime transaction was created")
        if localtest.get("appBehavior", {}).get("restoreActionTapCompleted") is not True:
            raise ValueError("UI test did not complete the tap on the real Restore control")
        if localtest.get("appBehavior", {}).get("activeTrialRestoredAfterFreshRelaunch") is not True:
            raise ValueError("The app did not restore verified trial history after a fresh process launch")

        screenshot_receipts: dict[str, Any] = {}
        for attachment_name, path in ((name, output / filename) for name, filename in SCREENSHOTS.items()):
            dimensions = capture_tools.png_dimensions(path)
            verify_capture_screenshot_dimensions(args.display_target, dimensions)
            pixels = capture_tools.require_visible_screen_content(path)
            screenshot_receipts[path.name] = {
                "source_attachment": attachment_name,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "dimensions": list(dimensions),
                "visible_content_validation": pixels,
            }
        receipt = {
            "schema_version": 1,
            "environment": "LOCALTEST",
            "qualification": "Apple StoreKitTest local StoreKit configuration and Simulator UI only; not App Store Connect or App Store Sandbox purchase evidence.",
            **source,
            "product_bundle_id": BUNDLE_ID,
            "product_app_bundle_sha256": app_hash,
            "product_build_receipt_sha256": sha256_file(args.build_receipt.resolve(strict=True)),
            "product_build_provenance": receipt,
            "build_consumer_provenance_sha256": (
                sha256_file(consumer_path) if consumer_path is not None else None
            ),
            "capture_tools_sha": tools_sha,
            "capture_tools_checkout_sha": current_tools_sha,
            "storekit_configuration_sha256": sha256_file(storekit_path),
            "ui_test_source_sha256": sha256_file(ui_test_path),
            "local_test_catalog": catalog,
            "storekit_restore_sync": restore_sync_evidence,
            "capture_display_target": args.display_target,
            "simulator": device,
            "xcode_version": xcode_version,
            "xcode_test_result_bundle": result_bundle.name,
            "test_runner_build_log_sha256": sha256_file(build_log),
            "ui_test_log_sha256": sha256_file(test_log),
            "localtest_transaction_record": localtest,
            "simulator_app_signing": {
                "status": "not-applied",
                "qualification": (
                    "No extra Simulator signature was applied. The producer app, XCTest-staged copy, and installed "
                    "app container remain byte-identical and unsigned."
                ),
            },
            "simulator_installed_app": installed_app_receipt,
            "producer_app_bundle_sha256_unchanged": sha256_tree(app) == receipt["product_app_bundle_sha256"],
            "screenshots": screenshot_receipts,
            "lifetime_purchase_button_activated": False,
            "lifetime_transaction_created": False,
            "raw_jws_included": False,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_json(output / "capture-provenance.json", receipt)
    except Exception as error:
        failure = f"{type(error).__name__}: {error}"
        write_json(output / "capture-failure.json", {
            "environment": "LOCALTEST",
            "product_source_sha": args.product_source_sha,
            "capture_tools_sha": tools_sha,
            "error": failure,
            "result_bundle_present": bool(result_bundle and result_bundle.exists()),
            "diagnostic_files": failure_diagnostic_files(output),
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        raise
    finally:
        if device is not None:
            try:
                capture_tools.cleanup_simulator(device)
            except (OSError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                write_json(output / "simulator-cleanup-warning.json", {"error": str(error)})
    if failure:
        raise RuntimeError(failure)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify-source")
    verify.add_argument("--product-root", required=True, type=Path)
    verify.add_argument("--product-source-sha", required=True)
    verify.set_defaults(handler=lambda args: print(json.dumps(verify_source(args.product_root, args.product_source_sha))))

    record = commands.add_parser("record-build")
    record.add_argument("--product-root", required=True, type=Path)
    record.add_argument("--product-source-sha", required=True)
    record.add_argument("--capture-tools-root", required=True, type=Path)
    record.add_argument("--capture-tools-sha", required=True)
    record.add_argument("--producer-run-id", required=True)
    record.add_argument("--producer-run-attempt", required=True)
    record.add_argument("--app", required=True, type=Path)
    record.add_argument("--output-dir", required=True, type=Path)
    record.set_defaults(handler=record_build)

    restore = commands.add_parser("restore-build")
    restore.add_argument("--artifact-dir", required=True, type=Path)
    restore.add_argument("--product-source-sha", required=True)
    restore.add_argument("--producer-run-id", required=True)
    restore.add_argument("--producer-run-attempt", required=True)
    restore.add_argument("--producer-tools-sha", required=True)
    restore.add_argument("--output-dir", required=True, type=Path)
    restore.set_defaults(handler=restore_build)

    runtime = commands.add_parser("verify-runtime", help="verify an installed Simulator runtime for StoreKitTest")
    runtime.add_argument("--minimum-ios-version", required=True)
    runtime.add_argument("--display-target", choices=tuple(CAPTURE_DISPLAY_TARGETS), default="default")
    runtime.add_argument("--inventory-output", type=Path)
    runtime.set_defaults(handler=verify_runtime)

    capture_command = commands.add_parser("capture")
    capture_command.add_argument("--product-root", required=True, type=Path)
    capture_command.add_argument("--product-source-sha", required=True)
    capture_command.add_argument("--capture-tools-root", required=True, type=Path)
    capture_command.add_argument("--capture-tools-sha", required=True)
    capture_command.add_argument("--app", required=True, type=Path)
    capture_command.add_argument("--build-receipt", required=True, type=Path)
    capture_command.add_argument("--consumer-provenance", type=Path)
    capture_command.add_argument("--output-dir", required=True, type=Path)
    capture_command.add_argument("--run-id", required=True)
    capture_command.add_argument("--run-attempt", required=True)
    capture_command.add_argument("--display-target", choices=tuple(CAPTURE_DISPLAY_TARGETS), default="default")
    capture_command.set_defaults(handler=capture)
    return root


if __name__ == "__main__":
    parsed = parser().parse_args()
    parsed.handler(parsed)
