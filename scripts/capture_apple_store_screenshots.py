#!/usr/bin/env python3
"""Build evidence and package authentic Apple App Store screenshots for the selected product source."""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
import plistlib
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any


DEFAULT_SOURCE_TAG = "v3.0.37-candidate.28f417ab17a4b6dc4c2e422b27662c62f62be7fb"
DEFAULT_SOURCE_SHA = "28f417ab17a4b6dc4c2e422b27662c62f62be7fb"
DEFAULT_VERSION = "3.0.37"
DEFAULT_BUILD_NUMBER = "196"
SOURCE_TAG = os.environ.get("PLEAZYNG_CAPTURE_PRODUCT_SOURCE_TAG", DEFAULT_SOURCE_TAG)
SOURCE_SHA = os.environ.get("PLEAZYNG_CAPTURE_PRODUCT_SOURCE_SHA", DEFAULT_SOURCE_SHA)
WORKFLOW_PATH = ".github/workflows/apple-store-screenshots.yml"
VERSION = os.environ.get("PLEAZYNG_CAPTURE_PRODUCT_VERSION", DEFAULT_VERSION)
BUILD_NUMBER = os.environ.get("PLEAZYNG_CAPTURE_PRODUCT_BUILD", DEFAULT_BUILD_NUMBER)
BUNDLE_ID = "org.snapetech.pleazyng"
COMMON_STORE_DEFINES = ["SUPPORTER_MODE=iap"]
IOS_MAC_STORE_DEFINES = [
    *COMMON_STORE_DEFINES,
    "ENABLE_UPDATE_CHECK=false",
    f"GIT_COMMIT={SOURCE_SHA}",
    "PLEAZYNG_DISCORD_APPLICATION_ID=",
]
TVOS_STORE_DEFINES = ["TVOS_BUILD=true", *COMMON_STORE_DEFINES]
BUILD_CONFIGURATION = {
    "ios": "Debug Simulator",
    "tvos": "Debug Simulator",
    "macos": "Release Archive",
}
LEGACY_BUILD_CONFIGURATION_EVIDENCE = {
    ("8a11288cbdf4b67a26f888b54771eabcf1be1762", "ios"): {
        "schema_version": 1,
        "build_configuration": "Debug Simulator",
        "evidence_source": "verified producer workflow build command",
        "producer_tools_sha": "8a11288cbdf4b67a26f888b54771eabcf1be1762",
        "workflow_path": WORKFLOW_PATH,
        "workflow_blob_sha1": "18a951057abeb83bd9dab60d7d1e3e5e3b4f6967",
        "workflow_sha256": "2fdd1039bdd3a2107cc223ff5bbff9adcb63185a487ef73f5b153a6ee2b54fb3",
        "build_step": "Build the Store-mode iOS Simulator app",
        "build_command": [
            "flutter build ios --debug --simulator --no-codesign --no-pub",
            "--dart-define=SUPPORTER_MODE=iap",
            "--dart-define=ENABLE_UPDATE_CHECK=false",
            "--dart-define=GIT_COMMIT=28f417ab17a4b6dc4c2e422b27662c62f62be7fb",
            "--dart-define=PLEAZYNG_DISCORD_APPLICATION_ID=",
        ],
        "app_output_path": "build/ios/iphonesimulator/Pleazyng.app",
    },
    ("496bc05789aca8a405f12611587e00ef92682451", "tvos"): {
        "schema_version": 1,
        "build_configuration": "Debug Simulator",
        "evidence_source": "verified producer workflow build command",
        "producer_tools_sha": "496bc05789aca8a405f12611587e00ef92682451",
        "workflow_path": WORKFLOW_PATH,
        "workflow_blob_sha1": "0fb60039b116a558e65917c7359c04735ccb3be9",
        "workflow_sha256": "bd84105246ac0714ebbe28b6555f0ddcb782cf37782da894134be5c7f18dde74",
        "build_step": "Build the Store-mode tvOS Simulator app",
        "build_command": [
            "xcodebuild build",
            "-workspace tvos/Runner.xcworkspace",
            "-scheme Runner",
            "-configuration Debug",
            "-sdk appletvsimulator",
            "-destination generic/platform=tvOS Simulator",
            "-derivedDataPath $RUNNER_TEMP/apple-store-tvos-derived",
            "-clonedSourcePackagesDirPath $RUNNER_TEMP/apple-store-tvos-source-packages",
            "ARCHS=arm64",
            "CODE_SIGNING_ALLOWED=NO",
            "COMPILER_INDEX_STORE_ENABLE=NO",
        ],
        "app_output_path": "apple-store-tvos-derived/Build/Products/Debug-appletvsimulator/Runner.app",
    },
}
SIMULATOR_SETTLE_SECONDS = 25
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

APP_IN_USE_UI_TEST = r'''import XCTest

final class PleazyngAppInUseCaptureTests: XCTestCase {
    func testCaptureFilesAndBooksScreen() {
        let app = XCUIApplication(bundleIdentifier: "org.snapetech.pleazyng")
        app.launch()

        let useFiles = app.buttons["Use files and books"]
        XCTAssertTrue(useFiles.waitForExistence(timeout: 60), "The real first-launch Files and Books action was not available")

        #if os(tvOS)
        for _ in 0..<12 {
            if useFiles.hasFocus { break }
            XCUIRemote.shared.press(.down)
            Thread.sleep(forTimeInterval: 0.25)
        }
        XCTAssertTrue(useFiles.hasFocus, "The real Files and Books action did not receive Apple TV focus")
        XCUIRemote.shared.press(.select)
        #else
        useFiles.tap()
        #endif

        XCTAssertTrue(app.buttons["Add source"].waitForExistence(timeout: 60), "The local Files and Books screen did not open")
        XCTAssertTrue(app.buttons["Import files"].exists, "The local Files and Books import action is missing")
        XCTAssertTrue(
            app.staticTexts.containing(NSPredicate(format: "label CONTAINS[c] %@", "Add a source or import a book")).firstMatch.exists,
            "The empty local library state is missing"
        )

        #if os(iOS)
        let keychainSuccess = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS[c] %@", "Connections stay private to this profile and encrypted on this device")
        ).firstMatch
        let keychainFailure = app.descendants(matching: .any).matching(
            NSPredicate(format: "label CONTAINS[c] %@", "Couldn't unlock the saved streaming settings")
        ).firstMatch
        let initialDeadline = Date().addingTimeInterval(30)
        while !keychainSuccess.exists && !keychainFailure.exists && Date() < initialDeadline {
            Thread.sleep(forTimeInterval: 0.25)
        }
        XCTAssertTrue(keychainSuccess.exists || keychainFailure.exists, "Fresh keychain state did not settle")
        XCTAssertFalse(keychainFailure.exists, "Fresh protected settings write/read failed on iOS")
        XCTAssertTrue(keychainSuccess.exists, "The fresh profile's protected settings did not load")

        app.terminate()
        app.launch()
        let librariesTab = app.buttons["Libraries"]
        XCTAssertTrue(librariesTab.waitForExistence(timeout: 60), "The Libraries tab did not return after relaunch")
        librariesTab.tap()
        let relaunchDeadline = Date().addingTimeInterval(30)
        while !keychainSuccess.exists && !keychainFailure.exists && Date() < relaunchDeadline {
            Thread.sleep(forTimeInterval: 0.25)
        }
        XCTAssertTrue(keychainSuccess.exists || keychainFailure.exists, "Persisted keychain state did not settle after relaunch")
        XCTAssertFalse(keychainFailure.exists, "Persisted protected settings failed to decrypt after relaunch")
        XCTAssertTrue(keychainSuccess.exists, "Persisted protected settings were not readable after relaunch")
        #endif

        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = "pleazyng-app-in-use-screen"
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
'''




MACOS_SELECT_WINDOW_CAPTURE_DISPLAY_SWIFT = r'''import AppKit
import CoreGraphics
import Foundation

_ = NSApplication.shared
let displayID = CGMainDisplayID()
let modes = (CGDisplayCopyAllDisplayModes(displayID, nil) as? [CGDisplayMode]) ?? []
let selected = modes.first(where: {
  $0.width == 1920 && $0.height == 1080 && $0.pixelWidth == 1920 && $0.pixelHeight == 1080
    && $0.isUsableForDesktopGUI()
})
guard let selected else {
  fputs("No usable native 1920x1080 display mode is available for a 1280x800 Mac app window.\n", stderr)
  exit(3)
}
let switchResult = CGDisplaySetDisplayMode(displayID, selected, nil)
guard switchResult == .success,
      let active = CGDisplayCopyDisplayMode(displayID),
      active.width == selected.width, active.height == selected.height,
      active.pixelWidth == selected.pixelWidth, active.pixelHeight == selected.pixelHeight else {
  fputs("The hosted Mac could not activate and verify native 1920x1080 mode.\n", stderr)
  exit(4)
}
let bounds = CGDisplayBounds(displayID)
let result: [String: Any] = [
  "display_id": Int(displayID),
  "selected_mode_points": [Double(selected.width), Double(selected.height)],
  "selected_mode_pixels": [selected.pixelWidth, selected.pixelHeight],
  "active_mode_points": [Double(active.width), Double(active.height)],
  "active_mode_pixels": [active.pixelWidth, active.pixelHeight],
  "display_bounds_points": [Double(bounds.width), Double(bounds.height)],
  "usable_for_desktop_gui": active.isUsableForDesktopGUI(),
  "target_native_window_points": [1280, 800],
  "window_center_points": [320, 140],
  "capture_api": "/usr/sbin/screencapture -x -o -l <app-window-id>",
  "image_resized_or_padded": false,
]
let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys, .prettyPrinted])
print(String(decoding: data, as: UTF8.self))
'''


MACOS_LIVE_DISPLAY_STATE_SWIFT = r'''import CoreGraphics
import Foundation

let displayID = CGMainDisplayID()
let bounds = CGDisplayBounds(displayID)
let mode = CGDisplayCopyDisplayMode(displayID)
let result: [String: Any] = [
  "display_id": Int(displayID),
  "display_bounds_points": [Double(bounds.width), Double(bounds.height)],
  "current_mode_points": mode.map { [Double($0.width), Double($0.height)] } ?? [],
  "current_mode_pixels": mode.map { [$0.pixelWidth, $0.pixelHeight] } ?? [],
  "reported_pixel_width": CGDisplayPixelsWide(displayID),
  "reported_pixel_height": CGDisplayPixelsHigh(displayID),
]
let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys, .prettyPrinted])
print(String(decoding: data, as: UTF8.self))
'''


MACOS_WINDOW_BOUNDS_SWIFT = r'''import CoreGraphics
import Foundation

guard CommandLine.arguments.count == 3,
      let targetPID = Int32(CommandLine.arguments[1]),
      let targetWindowID = Int(CommandLine.arguments[2]) else { exit(2) }
let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
guard let window = windows.first(where: {
  ($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == targetPID
    && ($0[kCGWindowNumber as String] as? NSNumber)?.intValue == targetWindowID
    && (($0[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1) > 0.9
}) else { exit(3) }
let bounds = window[kCGWindowBounds as String] as? [String: CGFloat] ?? [:]
let result: [String: Any] = [
  "window_id": targetWindowID,
  "owner_pid": targetPID,
  "on_screen": window[kCGWindowIsOnscreen as String] as? Bool ?? false,
  "bounds_points": [
    Double(bounds["X"] ?? 0), Double(bounds["Y"] ?? 0),
    Double(bounds["Width"] ?? 0), Double(bounds["Height"] ?? 0),
  ],
  "title": window[kCGWindowName as String] as? String ?? "",
]
let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys, .prettyPrinted])
print(String(decoding: data, as: UTF8.self))
'''


MACOS_LISTING_OCR_SWIFT = r'''import CoreGraphics
import Foundation
import ImageIO
import Vision

guard CommandLine.arguments.count == 2,
      let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: CommandLine.arguments[1]) as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
  fputs("Could not decode screenshot PNG for native Vision OCR.\n", stderr)
  exit(2)
}
let request = VNRecognizeTextRequest()
request.usesLanguageCorrection = false
request.recognitionLanguages = ["en-US"]
request.recognitionLevel = .fast
for (stage, devices) in try request.supportedComputeStageDevices {
  guard let cpu = devices.first(where: { if case .cpu = $0 { return true }; return false }) else {
    fputs("Vision OCR has no CPU for required stage.\n", stderr)
    exit(4)
  }
  request.setComputeDevice(cpu, for: stage)
}
try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
let observations = request.results ?? []
let recognized = observations.compactMap { observation -> [String: Any]? in
  guard let candidate = observation.topCandidates(1).first else { return nil }
  let box = observation.boundingBox
  return [
    "text": candidate.string,
    "confidence": Double(candidate.confidence),
    "normalized_bounds": [Double(box.minX), Double(box.minY), Double(box.width), Double(box.height)],
  ]
}
let lines = recognized.compactMap { $0["text"] as? String }
let result: [String: Any] = [
  "image_pixels": [image.width, image.height],
  "recognized_lines": lines,
  "recognized_regions": recognized,
]
let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys, .prettyPrinted])
print(String(decoding: data, as: UTF8.self))
'''


MACOS_POST_NATIVE_CLICK_SWIFT = r'''import CoreGraphics
import Foundation

guard CommandLine.arguments.count == 3,
      let x = Double(CommandLine.arguments[1]),
      let y = Double(CommandLine.arguments[2]),
      x.isFinite, y.isFinite, x >= 0, y >= 0 else { exit(2) }
let point = CGPoint(x: x, y: y)
guard let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left),
      let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left) else {
  fputs("Could not create native mouse events.\n", stderr)
  exit(3)
}
down.post(tap: .cghidEventTap)
Thread.sleep(forTimeInterval: 0.08)
up.post(tap: .cghidEventTap)
let result: [String: Any] = ["posted": true, "screen_point": [x, y], "input_method": "CoreGraphics CGEvent left mouse click"]
let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys, .prettyPrinted])
print(String(decoding: data, as: UTF8.self))
'''

MACOS_MAIN_WINDOW_AUTOSAVE_NAME = "org.snapetech.pleazyng.MainWindow"


def run(command: list[str], *, capture: bool = False, timeout: int = 300) -> str:
    result = subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )
    return result.stdout.strip() if capture else ""


def capture_ios_keychain_diagnostics(
    simulator_udid: str,
    output: Path,
    filename: str,
    app_executable: str,
) -> dict[str, Any]:
    """Keep only app/keychain daemon diagnostics from the fresh iOS capture simulator."""
    if re.fullmatch(r"[A-Za-z0-9._-]+", app_executable) is None:
        raise ValueError("The product's Info.plist executable name is invalid for Keychain diagnostics")
    log_path = output / f"{Path(filename).stem}.simulator-keychain.log"
    predicate = (
        f'(process == "{app_executable}" AND (eventMessage CONTAINS[c] "keychain" '
        'OR eventMessage CONTAINS[c] "secure storage" OR eventMessage CONTAINS[c] "errSec" '
        'OR eventMessage CONTAINS[c] "34018" OR eventMessage CONTAINS[c] "security")) '
        'OR (process == "securityd" AND (eventMessage CONTAINS[c] "org.snapetech.pleazyng" '
        'OR eventMessage CONTAINS[c] "34018" OR eventMessage CONTAINS[c] "errSec" '
        'OR eventMessage CONTAINS[c] "keychain" OR eventMessage CONTAINS[c] "entitlement"))'
    )
    command = [
        "xcrun", "simctl", "spawn", simulator_udid,
        "log", "show", "--last", "15m", "--style", "compact", "--predicate", predicate,
    ]
    returncode = None
    with log_path.open("w", encoding="utf-8") as log_file:
        try:
            result = subprocess.run(
                command,
                check=False,
                text=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            returncode = result.returncode
            if returncode == 0 and log_path.stat().st_size == 0:
                log_file.write("No matching Pleazyng/keychain daemon events were retained by the simulator log.")
        except subprocess.TimeoutExpired:
            log_file.write("Simulator keychain diagnostic log collection timed out after 120 seconds.\n")
        except OSError as error:
            log_file.write(f"Simulator keychain diagnostic log collection failed: {error.__class__.__name__}.\n")
    return {
        "filename": log_path.name,
        "sha256": sha256_file(log_path),
        "size_bytes": log_path.stat().st_size,
        "exit_code": returncode,
        "filter": predicate,
        "app_executable": app_executable,
        "scope": "Fresh capture simulator only; the actual CFBundleExecutable's Keychain-related messages and matching securityd records.",
    }


def require_source(product_root: Path) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", SOURCE_SHA) is None:
        raise ValueError("Expected product source SHA must be a full lowercase 40-character commit")
    if (re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", VERSION) is None
            or re.fullmatch(r"[0-9]+", BUILD_NUMBER) is None):
        raise ValueError("Expected product version and build must be numeric version fields")
    expected_tag = f"v{VERSION}-candidate.{SOURCE_SHA}"
    if SOURCE_TAG != expected_tag:
        raise ValueError(f"Expected exact source tag {expected_tag}, found {SOURCE_TAG!r}")
    tag_type = run(
        ["git", "-C", str(product_root), "cat-file", "-t", f"refs/tags/{SOURCE_TAG}"],
        capture=True,
        timeout=20,
    )
    if tag_type != "tag":
        raise ValueError(f"Expected {SOURCE_TAG} to be an annotated tag, found {tag_type!r}")
    tagged_commit = run(
        ["git", "-C", str(product_root), "rev-parse", f"refs/tags/{SOURCE_TAG}^{{commit}}"],
        capture=True,
        timeout=20,
    )
    if tagged_commit != SOURCE_SHA:
        raise ValueError(f"Expected {SOURCE_TAG} to resolve to {SOURCE_SHA}, found {tagged_commit}")
    head = run(["git", "-C", str(product_root), "rev-parse", "HEAD"], capture=True, timeout=20)
    if head != SOURCE_SHA:
        raise ValueError(f"Expected {SOURCE_TAG} at {SOURCE_SHA}, found {head}")
    version_line = next(
        (line for line in (product_root / "pubspec.yaml").read_text(encoding="utf-8").splitlines()
         if line.startswith("version:")),
        "",
    )
    match = re.fullmatch(r"""version:\s*["']?([0-9.]+)\+([0-9]+)["']?\s*""", version_line)
    if match is None or match.groups() != (VERSION, BUILD_NUMBER):
        raise ValueError(f"Expected {VERSION}+{BUILD_NUMBER} in pubspec.yaml, found {version_line!r}")


def _git_output(repository: Path, *arguments: str) -> str:
    return run(["git", "-C", str(repository), *arguments], capture=True, timeout=30)


def source_lineage(product_root: Path, approval_root: Path | None) -> dict[str, Any]:
    """Verify the explicit packaging-repair relationship for a nonbaseline build."""
    tools_sha = require_capture_tools_sha()
    frozen_sha = os.environ.get("PLEAZYNG_CAPTURE_FROZEN_PRODUCT_SOURCE_SHA", "")
    frozen_tag = os.environ.get("PLEAZYNG_CAPTURE_FROZEN_PRODUCT_SOURCE_TAG", "")
    if not frozen_sha and not frozen_tag:
        if (SOURCE_SHA == DEFAULT_SOURCE_SHA and VERSION == DEFAULT_VERSION
                and BUILD_NUMBER == DEFAULT_BUILD_NUMBER):
            # Preserve the historic receipt shape so retained 3.0.37 bundles remain reusable.
            return {
                "relationship": "qualified product source built directly",
                "product_source_sha": SOURCE_SHA,
                "product_source_tag": SOURCE_TAG,
            }
        return {
            "relationship": "exact candidate product source built directly; product and packaging sources are identical",
            "product_source_sha": SOURCE_SHA,
            "product_source_tag": SOURCE_TAG,
            "packaging_source_sha": SOURCE_SHA,
            "workflow_source_sha": tools_sha,
            "proof_method": "Verified the exact annotated candidate tag, checked-out commit, and product version/build fields.",
        }

    if (approval_root is None or re.fullmatch(r"[0-9a-f]{40}", frozen_sha) is None
            or frozen_tag != f"v{VERSION}-candidate.{frozen_sha}"):
        raise ValueError("A nonbaseline capture requires exact frozen-product and approved packaging-source proof inputs")
    frozen_tag_type = _git_output(product_root, "cat-file", "-t", f"refs/tags/{frozen_tag}")
    if frozen_tag_type != "tag":
        raise ValueError(f"Expected frozen product tag {frozen_tag} to be annotated, found {frozen_tag_type!r}")
    frozen_tag_commit = _git_output(product_root, "rev-parse", f"refs/tags/{frozen_tag}^{{commit}}")
    if frozen_tag_commit != frozen_sha:
        raise ValueError(f"Frozen product tag {frozen_tag} does not resolve to {frozen_sha}")
    if _git_output(approval_root, "rev-parse", "HEAD") != tools_sha:
        raise ValueError("Source approval manifest checkout differs from the capture-tools receipt SHA")
    approval_path = ".github/release-packaging-repairs.json"
    manifest_path = approval_root / approval_path
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Approved frozen-packaging source manifest is missing or unsafe")
    manifest_bytes = manifest_path.read_bytes()
    approval_blob = hashlib.sha1(
        b"blob " + str(len(manifest_bytes)).encode("ascii") + b"\0" + manifest_bytes
    ).hexdigest()
    recorded_approval_blob = _git_output(approval_root, "rev-parse", f"{tools_sha}:{approval_path}")
    if approval_blob != recorded_approval_blob:
        raise ValueError("Frozen-packaging approval manifest bytes differ from the capture-tools Git tree")
    manifest = json.loads(manifest_bytes)
    repairs = manifest.get("repairs") if isinstance(manifest, dict) else None
    matches = [
        entry for entry in repairs or []
        if isinstance(entry, dict)
        and entry.get("product_source_sha") == frozen_sha
        and entry.get("packaging_source_sha") == SOURCE_SHA
    ]
    if len(matches) != 1:
        raise ValueError("Approval manifest does not contain one exact frozen-product/packaging-source pair")
    approved_files = matches[0].get("files")
    if not isinstance(approved_files, dict) or not approved_files:
        raise ValueError("Approved packaging-source file/blob inventory is missing")
    for path, blob in approved_files.items():
        normalized = PurePosixPath(path) if isinstance(path, str) else PurePosixPath("..")
        if (not isinstance(path, str) or not path or normalized.is_absolute()
                or ".." in normalized.parts or normalized.as_posix() != path
                or re.fullmatch(r"[0-9a-f]{40}", str(blob)) is None):
            raise ValueError("Approved packaging-source inventory contains an unsafe path or invalid blob")
    parents = _git_output(product_root, "rev-list", "--parents", "-n", "1", SOURCE_SHA).split()
    if parents != [SOURCE_SHA, frozen_sha]:
        raise ValueError("Packaging source is not the reviewed single-commit child of the frozen product source")
    changed_paths = _git_output(product_root, "diff-tree", "--no-commit-id", "--name-only", "-r", SOURCE_SHA).splitlines()
    if sorted(changed_paths) != sorted(approved_files):
        raise ValueError("Packaging source changed-file set differs from the approved manifest")
    for path, expected_blob in approved_files.items():
        if (not isinstance(path, str) or not isinstance(expected_blob, str)
                or _git_output(product_root, "rev-parse", f"{SOURCE_SHA}:{path}") != expected_blob):
            raise ValueError(f"Packaging source does not match approved file blob: {path}")
    return {
        "relationship": "approved frozen-product packaging repair; simulator app built from packaging source",
        "product_source_sha": SOURCE_SHA,
        "product_source_tag": SOURCE_TAG,
        "related_frozen_product_source_sha": frozen_sha,
        "related_frozen_product_source_tag": frozen_tag,
        "packaging_source_sha": SOURCE_SHA,
        "workflow_source_sha": tools_sha,
        "approval_path": approval_path,
        "approval_blob_sha": approval_blob,
        "approval_file_blobs": dict(sorted(approved_files.items())),
        "proof_method": "Verified the exact GitHub packaging-approval manifest pair, sole-parent relationship, changed-file set, and every approved product-source blob.",
    }


def require_capture_tools_sha() -> str:
    value = os.environ.get("PLEAZYNG_CAPTURE_TOOLS_SHA", "")
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("PLEAZYNG_CAPTURE_TOOLS_SHA must identify the capture-tools checkout commit")
    return value


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


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as source:
        header = source.read(26)
    if len(header) != 26 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise ValueError(f"Capture is not a valid PNG: {path}")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if width < 640 or height < 480:
        raise ValueError(f"Capture surface is unexpectedly small: {width}x{height}")
    if path.stat().st_size < 16_000:
        raise ValueError(f"Capture is unexpectedly small to be a complete app screenshot: {path}")
    return width, height


def png_color_type(path: Path) -> int:
    with path.open("rb") as source:
        header = source.read(26)
    if len(header) != 26 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise ValueError(f"Capture is not a valid PNG: {path}")
    return header[25]


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", checksum)


def normalize_opaque_rgb_png(path: Path) -> dict[str, Any]:
    """Strip an all-opaque PNG alpha channel without changing screenshot RGB samples."""
    data = path.read_bytes()
    if data[:8] != PNG_SIGNATURE:
        raise ValueError(f"Capture is not a valid PNG: {path}")
    chunks: list[tuple[bytes, bytes]] = []
    offset = len(PNG_SIGNATURE)
    while offset < len(data):
        if len(data) - offset < 12:
            raise ValueError(f"PNG has a truncated chunk header: {path}")
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type = data[offset + 4:offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        checksum_end = payload_end + 4
        if checksum_end > len(data):
            raise ValueError(f"PNG has a truncated {chunk_type!r} chunk: {path}")
        payload = data[payload_start:payload_end]
        expected_checksum = struct.unpack(">I", data[payload_end:checksum_end])[0]
        actual_checksum = binascii.crc32(chunk_type + payload) & 0xFFFFFFFF
        if actual_checksum != expected_checksum:
            raise ValueError(f"PNG chunk {chunk_type!r} has an invalid checksum: {path}")
        chunks.append((chunk_type, payload))
        offset = checksum_end
        if chunk_type == b"IEND":
            break
    if offset != len(data) or not chunks or chunks[0][0] != b"IHDR":
        raise ValueError(f"PNG chunk ordering or trailing data is invalid: {path}")
    header = chunks[0][1]
    if len(header) != 13:
        raise ValueError(f"PNG IHDR chunk has an invalid length: {path}")
    width, height = struct.unpack(">II", header[:8])
    bit_depth, color_type, _compression, _filter, interlace = header[8:13]
    if color_type == 2:
        return {
            "source_color_type": 2,
            "output_color_type": 2,
            "alpha_was_opaque": None,
            "rgb_samples_modified": False,
        }
    if color_type != 6 or bit_depth != 8 or interlace != 0:
        raise ValueError(
            f"Apple listing PNG must be RGB or opaque 8-bit RGBA; received color type {color_type}, "
            f"bit depth {bit_depth}, interlace {interlace}: {path}"
        )
    compressed = b"".join(payload for kind, payload in chunks if kind == b"IDAT")
    if not compressed:
        raise ValueError(f"PNG has no image data: {path}")
    filtered_data = zlib.decompress(compressed)
    bytes_per_pixel = 4
    row_bytes = width * bytes_per_pixel
    expected_length = height * (row_bytes + 1)
    if len(filtered_data) != expected_length:
        raise ValueError(f"PNG scanline data has an unexpected size: {path}")

    def paeth(left: int, above: int, upper_left: int) -> int:
        estimate = left + above - upper_left
        left_distance = abs(estimate - left)
        above_distance = abs(estimate - above)
        upper_left_distance = abs(estimate - upper_left)
        if left_distance <= above_distance and left_distance <= upper_left_distance:
            return left
        if above_distance <= upper_left_distance:
            return above
        return upper_left

    rgb_scanlines = bytearray()
    previous = bytearray(row_bytes)
    source_offset = 0
    for _row in range(height):
        filter_type = filtered_data[source_offset]
        source_offset += 1
        filtered = filtered_data[source_offset:source_offset + row_bytes]
        source_offset += row_bytes
        reconstructed = bytearray(row_bytes)
        for index, value in enumerate(filtered):
            left = reconstructed[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                predictor = paeth(left, above, upper_left)
            else:
                raise ValueError(f"PNG uses an unsupported scanline filter {filter_type}: {path}")
            reconstructed[index] = (value + predictor) & 0xFF
        for index in range(0, row_bytes, bytes_per_pixel):
            if reconstructed[index + 3] != 255:
                raise ValueError(f"Screenshot contains non-opaque pixels; refusing to flatten its UI: {path}")
            rgb_scanlines.extend(reconstructed[index:index + 3])
        previous = reconstructed

    rgb_rows = bytearray()
    row_rgb_bytes = width * 3
    for row in range(height):
        start = row * row_rgb_bytes
        rgb_rows.append(0)
        rgb_rows.extend(rgb_scanlines[start:start + row_rgb_bytes])

    updated_header = bytearray(header)
    updated_header[9] = 2
    output_data = bytearray(PNG_SIGNATURE)
    inserted_image_data = False
    for chunk_type, payload in chunks:
        if chunk_type == b"IHDR":
            payload = bytes(updated_header)
        elif chunk_type == b"IDAT":
            if not inserted_image_data:
                output_data.extend(_png_chunk(b"IDAT", zlib.compress(bytes(rgb_rows), level=9)))
                inserted_image_data = True
            continue
        elif chunk_type == b"tRNS":
            continue
        output_data.extend(_png_chunk(chunk_type, payload))
    if not inserted_image_data:
        raise ValueError(f"PNG image data was not preserved during RGB conversion: {path}")
    path.write_bytes(output_data)
    if png_color_type(path) != 2:
        raise ValueError(f"Could not normalize screenshot to an opaque RGB PNG: {path}")
    return {
        "source_color_type": 6,
        "output_color_type": 2,
        "alpha_was_opaque": True,
        "rgb_samples_modified": False,
    }


def require_visible_screen_content(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    if data[:8] != PNG_SIGNATURE:
        raise ValueError(f"Capture is not a PNG image: {path}")
    position = 8
    width = height = bit_depth = color_type = interlace = None
    image_data = bytearray()
    palette: bytes | None = None
    while position + 12 <= len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        kind = data[position + 4:position + 8]
        start = position + 8
        end = start + length
        if end + 4 > len(data):
            raise ValueError(f"Capture PNG has a truncated chunk: {path}")
        chunk = data[start:end]
        if kind == b"IHDR":
            if len(chunk) != 13:
                raise ValueError(f"Capture PNG has a malformed header: {path}")
            width = int.from_bytes(chunk[0:4], "big")
            height = int.from_bytes(chunk[4:8], "big")
            bit_depth, color_type, _, _, interlace = chunk[8:13]
        elif kind == b"PLTE":
            palette = chunk
        elif kind == b"IDAT":
            image_data.extend(chunk)
        elif kind == b"IEND":
            break
        position = end + 4
    if (not width or not height or bit_depth != 8 or interlace != 0
            or color_type not in (0, 2, 3, 4, 6)):
        raise ValueError(f"Capture PNG format is unsupported for content validation: {path}")
    if color_type == 3 and (palette is None or len(palette) % 3 != 0):
        raise ValueError(f"Capture PNG palette is missing or malformed: {path}")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    bytes_per_pixel = channels
    row_bytes = width * channels
    decoded = zlib.decompress(image_data)
    expected_bytes = height * (row_bytes + 1)
    if len(decoded) != expected_bytes:
        raise ValueError(f"Capture PNG pixel data has an unexpected length: {path}")

    region_x0, region_x1 = width // 10, width - width // 10
    region_y0, region_y1 = height // 10, height - height // 10
    region_area = (region_x1 - region_x0) * (region_y1 - region_y0)
    visible_pixels = 0
    sampled_colors: set[tuple[int, int, int]] = set()
    previous = bytearray(row_bytes)
    cursor = 0
    for y in range(height):
        filter_type = decoded[cursor]
        filtered = decoded[cursor + 1:cursor + 1 + row_bytes]
        cursor += row_bytes + 1
        row = bytearray(row_bytes)
        for index, value in enumerate(filtered):
            left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                estimate = left + above - upper_left
                distance_left = abs(estimate - left)
                distance_above = abs(estimate - above)
                distance_upper_left = abs(estimate - upper_left)
                predictor = (
                    left if distance_left <= distance_above and distance_left <= distance_upper_left
                    else above if distance_above <= distance_upper_left
                    else upper_left
                )
            else:
                raise ValueError(f"Capture PNG has an unsupported scanline filter: {filter_type}")
            row[index] = (value + predictor) & 0xFF
        if region_y0 <= y < region_y1:
            for x in range(region_x0, region_x1):
                offset = x * channels
                if color_type == 0:
                    gray = row[offset]
                    rgb = (gray, gray, gray)
                elif color_type == 2:
                    rgb = (row[offset], row[offset + 1], row[offset + 2])
                elif color_type == 3:
                    palette_offset = row[offset] * 3
                    if palette_offset + 2 >= len(palette or b""):
                        raise ValueError(f"Capture PNG references an invalid palette index: {path}")
                    rgb = tuple((palette or b"")[palette_offset:palette_offset + 3])
                elif color_type == 4:
                    gray = row[offset]
                    rgb = (gray, gray, gray)
                else:
                    rgb = (row[offset], row[offset + 1], row[offset + 2])
                alpha = row[offset + channels - 1] if color_type in (4, 6) else 255
                if alpha > 16 and max(rgb) >= 48:
                    visible_pixels += 1
                if alpha > 16 and x % 6 == 0 and y % 6 == 0:
                    sampled_colors.add(rgb)
        previous = row
    metrics = {
        "central_region": [region_x0, region_y0, region_x1, region_y1],
        "central_region_pixels": region_area,
        "visible_pixel_count": visible_pixels,
        "sampled_color_count": len(sampled_colors),
    }
    if visible_pixels < max(200, region_area // 1000) or len(sampled_colors) < 6:
        raise ValueError(f"Capture looks blank or has no usable app content: {path} ({metrics})")
    return metrics


def app_info(app: Path) -> dict[str, str]:
    if app.is_symlink() or not app.is_dir():
        raise FileNotFoundError(f"Application bundle is missing: {app}")
    plist_path = next(
        (candidate for candidate in (app / "Info.plist", app / "Contents" / "Info.plist") if candidate.is_file()),
        app / "Info.plist",
    )
    with plist_path.open("rb") as source:
        info = plistlib.load(source)
    actual = {
        "bundle_id": info.get("CFBundleIdentifier"),
        "version": info.get("CFBundleShortVersionString"),
        "build_number": info.get("CFBundleVersion"),
        "executable": info.get("CFBundleExecutable"),
    }
    if actual != {
        "bundle_id": BUNDLE_ID,
        "version": VERSION,
        "build_number": BUILD_NUMBER,
        "executable": actual["executable"],
    } or not isinstance(actual["executable"], str) or not actual["executable"]:
        raise ValueError(f"Application identity does not match the v{VERSION} Store build: {actual}")
    return {key: str(value) for key, value in actual.items()}


def verify_tvos_simulator_framework_bundle(app: Path) -> dict[str, Any]:
    """Verify the known strong AMSMB2 load resolves in the preserved simulator app."""
    app = app.resolve(strict=True)
    identity = app_info(app)
    executable = app / identity["executable"]
    framework = app / "Frameworks" / "AMSMB2.framework" / "AMSMB2"
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "platform": "tvOS Simulator",
        "product_source_sha": SOURCE_SHA,
        "product_source_tag": SOURCE_TAG,
        "app_bundle_sha256": sha256_tree(app),
        "bundle_id": identity["bundle_id"],
        "app_executable": executable.relative_to(app).as_posix(),
        "required_install_name": "@rpath/AMSMB2.framework/AMSMB2",
        "framework_path": framework.relative_to(app).as_posix(),
        "loaders": [],
        "errors": [],
    }
    if not executable.is_file() or not executable.stat().st_mode & 0o111:
        evidence["errors"].append("The tvOS Simulator app executable is missing or not executable")
    if not framework.is_file() or framework.is_symlink():
        evidence["errors"].append("The app does not contain a regular AMSMB2 framework executable")
    else:
        try:
            framework.resolve(strict=True).relative_to(app)
        except ValueError:
            evidence["errors"].append("The AMSMB2 framework resolves outside the app bundle")
        evidence["framework_sha256"] = sha256_file(framework)

    macho_magic = {
        bytes.fromhex(value) for value in (
            "feedface", "cefaedfe", "feedfacf", "cffaedfe",
            "cafebabe", "bebafeca", "cafebabf", "bfbafeca",
        )
    }
    binaries: list[Path] = []
    if executable.is_file():
        binaries.append(executable)
    for path in app.rglob("*"):
        if path.is_file() and not path.is_symlink() and path != executable:
            try:
                with path.open("rb") as source:
                    if source.read(4) in macho_magic:
                        binaries.append(path)
            except OSError:
                continue
    binaries = sorted(set(binaries))
    if framework.is_file() and framework not in binaries:
        binaries.append(framework)

    def parse_architectures(value: str) -> tuple[str, ...]:
        match = re.search(r"\bis architecture:\s*([A-Za-z0-9_]+)\s*$", value)
        if match:
            return (match.group(1),)
        match = re.search(r"\bare:\s*(.+)$", value, re.MULTILINE)
        result = tuple(match.group(1).split()) if match else tuple(value.split())
        if not result or any(re.fullmatch(r"[A-Za-z0-9_]+", item) is None for item in result):
            raise ValueError(f"Cannot parse lipo architectures: {value!r}")
        return tuple(dict.fromkeys(result))

    def load_commands(binary: Path, architecture: str) -> tuple[list[str], list[str]]:
        output = run(["xcrun", "otool", "-arch", architecture, "-l", str(binary)], capture=True, timeout=60)
        loads: list[str] = []
        rpaths: list[str] = []
        current = ""
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("cmd "):
                current = stripped[4:]
            elif current in {"LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB", "LC_LOAD_UPWARD_DYLIB", "LC_LAZY_LOAD_DYLIB"} and stripped.startswith("name "):
                loads.append(f"{current}\t{stripped[5:].split(' (offset ', 1)[0]}")
                current = ""
            elif current == "LC_RPATH" and stripped.startswith("path "):
                rpaths.append(stripped[5:].split(" (offset ", 1)[0])
                current = ""
        return loads, rpaths

    required = evidence["required_install_name"]
    try:
        root_architectures = parse_architectures(
            run(["/usr/bin/lipo", "-archs", str(executable)], capture=True, timeout=60)
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        root_architectures = ()
    framework_architectures: tuple[str, ...] = ()
    if framework.is_file() and not framework.is_symlink():
        try:
            framework_architectures = parse_architectures(
                run(["/usr/bin/lipo", "-archs", str(framework)], capture=True, timeout=60)
            )
        except (OSError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            pass
    resolved_loads = 0
    for binary in binaries:
        try:
            arch_output = run(["/usr/bin/lipo", "-archs", str(binary)], capture=True, timeout=60)
            architectures = parse_architectures(arch_output)
        except (OSError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as error:
            evidence["errors"].append(f"Cannot inspect bundled Mach-O {binary.relative_to(app)}: {error}")
            continue
        if binary == framework and "arm64" not in architectures:
            evidence["errors"].append("The embedded AMSMB2 framework does not contain arm64")
        for architecture in architectures:
            try:
                loads, rpaths = load_commands(binary, architecture)
            except (OSError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                evidence["errors"].append(f"Cannot read load commands for {binary.relative_to(app)} ({architecture}): {error}")
                continue
            for entry in loads:
                command_name, install_name = entry.split("\t", 1)
                if install_name != required:
                    continue
                loader = {
                    "image": binary.relative_to(app).as_posix(),
                    "architecture": architecture,
                    "command": command_name,
                    "install_name": install_name,
                    "rpaths": rpaths,
                    "resolved_path": None,
                }
                evidence["loaders"].append(loader)
                if command_name == "LC_LOAD_WEAK_DYLIB":
                    evidence["errors"].append(f"AMSMB2 is only weakly loaded by {loader['image']} ({architecture})")
                    continue
                if architecture not in framework_architectures:
                    evidence["errors"].append(
                        f"Embedded AMSMB2 framework lacks {architecture} for {loader['image']}"
                    )
                    continue
                inherited_rpaths: list[str] = []
                if binary != executable and architecture in root_architectures:
                    try:
                        _, inherited_rpaths = load_commands(executable, architecture)
                    except (OSError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                        evidence["errors"].append(f"Cannot read app executable runpaths ({architecture}): {error}")
                        continue
                candidates: list[Path] = []
                for runpath in dict.fromkeys((*inherited_rpaths, *rpaths)):
                    if runpath == "@loader_path" or runpath.startswith("@loader_path/"):
                        prefix = binary.parent / runpath[len("@loader_path"):].lstrip("/")
                    elif runpath == "@executable_path" or runpath.startswith("@executable_path/"):
                        prefix = executable.parent / runpath[len("@executable_path"):].lstrip("/")
                    elif runpath.startswith("/"):
                        prefix = Path(runpath)
                    else:
                        continue
                    candidates.append(prefix / "AMSMB2.framework" / "AMSMB2")
                for candidate in candidates:
                    try:
                        if candidate.resolve(strict=True) == framework.resolve(strict=True):
                            loader["resolved_path"] = framework.relative_to(app).as_posix()
                            resolved_loads += 1
                            break
                    except OSError:
                        continue
                if loader["resolved_path"] is None:
                    evidence["errors"].append(
                        f"Strong AMSMB2 load from {loader['image']} ({architecture}) does not resolve into the app bundle"
                    )
    if not evidence["loaders"]:
        evidence["errors"].append("No strong AMSMB2.framework/AMSMB2 load was found in bundled tvOS Mach-O images")
    if resolved_loads != len(evidence["loaders"]):
        evidence["errors"].append("Not every AMSMB2 load resolved to the same-architecture embedded framework")
    evidence["status"] = "passed" if not evidence["errors"] else "failed"
    evidence["qualification"] = "Static tvOS Simulator Mach-O check of the known strong AMSMB2 dyld load; not runtime qualification."
    return evidence


def preserve_build(args: argparse.Namespace) -> None:
    product_root = args.product_root.resolve(strict=True)
    require_source(product_root)
    lineage = source_lineage(
        product_root,
        args.source_lineage_root.resolve(strict=True) if args.source_lineage_root else None,
    )
    tool_source_sha = require_capture_tools_sha()
    app = args.app.resolve(strict=True)
    identity = app_info(app)
    executable = app / "Contents" / "MacOS" / identity["executable"] if args.platform == "macos" else app / identity["executable"]
    if not executable.is_file() or not executable.stat().st_mode & 0o111:
        raise ValueError(f"Application executable is missing or not executable: {executable}")
    architecture_output = run(["/usr/bin/lipo", "-archs", str(executable)], capture=True, timeout=60)
    architectures = sorted(set(architecture_output.split()))
    if not architectures:
        raise ValueError(f"No executable architectures found in {executable}")
    if args.platform in ("ios", "tvos") and "arm64" not in architectures:
        raise ValueError(f"Simulator build is missing arm64: {architectures}")
    if args.platform == "macos" and not {"arm64", "x86_64"}.issubset(architectures):
        raise ValueError(f"Mac App Store candidate is missing universal architectures: {architectures}")

    build_defines = {
        "ios": IOS_MAC_STORE_DEFINES,
        "tvos": TVOS_STORE_DEFINES,
        "macos": IOS_MAC_STORE_DEFINES,
    }[args.platform]
    platform_name = {"ios": "iOS Simulator", "tvos": "tvOS Simulator", "macos": "macOS"}[args.platform]
    producer_repository = os.environ.get("GITHUB_REPOSITORY", "")
    producer_workflow_ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    producer_run_id = os.environ.get("GITHUB_RUN_ID", "")
    producer_run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    expected_workflow_ref = f"{producer_repository}/{WORKFLOW_PATH}@refs/heads/main"
    if (producer_repository != "snapetech-org/pleazyng"
            or producer_workflow_ref != expected_workflow_ref
            or re.fullmatch(r"[0-9]+", producer_run_id) is None
            or re.fullmatch(r"[0-9]+", producer_run_attempt) is None):
        raise ValueError("Build artifacts must be produced by a main-branch Apple screenshot workflow run")
    receipt = {
        "schema_version": 1,
        "product": "Pleazyng",
        "source_tag": SOURCE_TAG,
        "source_sha": SOURCE_SHA,
        "source_lineage": lineage,
        "capture_tools_sha": tool_source_sha,
        "producer_repository": producer_repository,
        "producer_workflow_ref": producer_workflow_ref,
        "producer_run_id": producer_run_id,
        "producer_run_attempt": producer_run_attempt,
        "product_version": VERSION,
        "product_build": BUILD_NUMBER,
        "platform": platform_name,
        "build_mode": "App Store IAP",
        "build_configuration": BUILD_CONFIGURATION[args.platform],
        "app_bundle_name": app.name,
        "bundle_id": identity["bundle_id"],
        "executable": identity["executable"],
        "architectures": architectures,
        "build_host_architecture": os.environ.get("RUNNER_ARCH", "unknown"),
        "build_defines": build_defines,
        "app_bundle_sha256": sha256_tree(app),
        "distribution_signing": "disabled",
        "qualification": "Compiled application bundle only; not a signed or uploadable store product.",
    }
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    receipt_path = output / "build-provenance.json"
    archive_path = output / f"pleazyng-apple-store-build-{args.platform}.tar.gz"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    with tarfile.open(archive_path, mode="w:gz", compresslevel=1) as archive:
        archive.add(app, arcname=app.name)
        archive.add(receipt_path, arcname=receipt_path.name)
    print(f"Preserved {platform_name} app bundle and receipt in {output}")


def verify_tvos_simulator_dependencies(args: argparse.Namespace) -> None:
    product_root = args.product_root.resolve(strict=True)
    approval_root = args.source_lineage_root.resolve(strict=True)
    app = args.app.resolve(strict=True)
    build_receipt_path = args.build_receipt.resolve(strict=True)
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "status": "failed",
        "platform": "tvOS Simulator",
        "product_source_sha": SOURCE_SHA,
        "product_source_tag": SOURCE_TAG,
        "producer_run_id": args.producer_run_id,
        "producer_run_attempt": args.producer_run_attempt,
        "capture_tools_sha": os.environ.get("PLEAZYNG_CAPTURE_TOOLS_SHA", ""),
        "errors": [],
    }
    try:
        require_source(product_root)
        lineage = source_lineage(product_root, approval_root)
        tools_sha = require_capture_tools_sha()
        producer_repository = os.environ.get("GITHUB_REPOSITORY", "")
        receipt = json.loads(build_receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError("tvOS simulator build receipt must be a JSON object")
        validate_build_receipt(
            receipt,
            platform="tvos",
            producer_repository=producer_repository,
            producer_run_id=args.producer_run_id,
            producer_run_attempt=args.producer_run_attempt,
            capture_tools_sha=tools_sha,
        )
        if receipt.get("source_lineage") != lineage:
            raise ValueError("tvOS simulator build receipt does not match approved frozen-product source lineage")
        if receipt.get("app_bundle_sha256") != sha256_tree(app):
            raise ValueError("tvOS simulator app differs from the preserved app receipt")
        evidence.update(verify_tvos_simulator_framework_bundle(app))
        evidence["packaging_source_sha"] = lineage.get("packaging_source_sha", SOURCE_SHA)
        evidence["workflow_source_sha"] = tools_sha
        evidence["build_receipt_sha256"] = sha256_file(build_receipt_path)
        evidence["source_lineage"] = lineage
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        evidence["errors"].append(f"{type(error).__name__}: {error}")
    evidence["status"] = "passed" if not evidence["errors"] else "failed"
    if args.output.is_symlink() or args.output.exists():
        raise ValueError(f"Refusing to replace existing tvOS simulator dependency receipt: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if evidence["status"] != "passed":
        raise ValueError(f"tvOS Simulator AMSMB2 closure verification failed; see {args.output}")


def validate_build_receipt(
    receipt: dict[str, Any],
    *,
    platform: str,
    producer_repository: str,
    producer_run_id: str,
    producer_run_attempt: str,
    capture_tools_sha: str,
) -> str:
    platform_name = {"ios": "iOS Simulator", "tvos": "tvOS Simulator", "macos": "macOS"}[platform]
    expected_architectures = {"arm64", "x86_64"} if platform == "macos" else {"arm64"}
    expected_defines = {
        "ios": IOS_MAC_STORE_DEFINES,
        "tvos": TVOS_STORE_DEFINES,
        "macos": IOS_MAC_STORE_DEFINES,
    }[platform]
    expected_app_name = {"ios": "Pleazyng.app", "tvos": "Runner.app", "macos": "Pleazyng.app"}[platform]
    expected = {
        "schema_version": 1,
        "product": "Pleazyng",
        "source_tag": SOURCE_TAG,
        "source_sha": SOURCE_SHA,
        "capture_tools_sha": capture_tools_sha,
        "producer_repository": producer_repository,
        "producer_workflow_ref": f"{producer_repository}/{WORKFLOW_PATH}@refs/heads/main",
        "producer_run_id": producer_run_id,
        "producer_run_attempt": producer_run_attempt,
        "product_version": VERSION,
        "product_build": BUILD_NUMBER,
        "platform": platform_name,
        "build_mode": "App Store IAP",
        "app_bundle_name": expected_app_name,
        "bundle_id": BUNDLE_ID,
        "build_defines": expected_defines,
        "distribution_signing": "disabled",
    }
    mismatched = {key: (receipt.get(key), value) for key, value in expected.items() if receipt.get(key) != value}
    configuration_evidence = build_configuration_evidence(receipt, platform)
    if (configuration_evidence is None
            or configuration_evidence.get("build_configuration") != BUILD_CONFIGURATION[platform]):
        mismatched["build_configuration"] = (
            receipt.get("build_configuration"), BUILD_CONFIGURATION[platform]
        )
    if mismatched:
        raise ValueError(f"Build receipt does not match the expected producer and product: {mismatched}")
    if SOURCE_SHA != DEFAULT_SOURCE_SHA and not isinstance(receipt.get("source_lineage"), dict):
        raise ValueError("A nonbaseline product build receipt must include its approved source lineage")
    architectures = receipt.get("architectures")
    if (not isinstance(architectures, list) or not all(isinstance(item, str) for item in architectures)
            or architectures != sorted(set(architectures)) or not expected_architectures.issubset(architectures)):
        raise ValueError(f"Build receipt has unexpected architectures for {platform_name}: {architectures!r}")
    if re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("app_bundle_sha256", ""))) is None:
        raise ValueError("Build receipt has an invalid app bundle SHA-256")
    return expected_app_name


def build_configuration_evidence(receipt: dict[str, Any], platform: str) -> dict[str, Any] | None:
    configuration = receipt.get("build_configuration")
    if isinstance(configuration, str) and configuration:
        evidence = {
            "schema_version": 1,
            "build_configuration": configuration,
            "evidence_source": "producer build-provenance.json",
            "producer_tools_sha": receipt.get("capture_tools_sha"),
        }
    else:
        legacy_evidence = LEGACY_BUILD_CONFIGURATION_EVIDENCE.get(
            (str(receipt.get("capture_tools_sha", "")), platform)
        )
        if legacy_evidence is None:
            return None
        evidence = dict(legacy_evidence)
    evidence.update({
        "producer_repository": receipt.get("producer_repository"),
        "producer_workflow_ref": receipt.get("producer_workflow_ref"),
        "producer_run_id": receipt.get("producer_run_id"),
        "producer_run_attempt": receipt.get("producer_run_attempt"),
        "product_source_sha": receipt.get("source_sha"),
    })
    return evidence


def restore_build(args: argparse.Namespace) -> None:
    product_root = args.product_root.resolve(strict=True)
    require_source(product_root)
    expected_tools_sha = args.expected_capture_tools_sha
    if re.fullmatch(r"[0-9a-f]{40}", expected_tools_sha) is None:
        raise ValueError("Expected capture-tools SHA must be a full lowercase Git commit")
    producer_repository = os.environ.get("GITHUB_REPOSITORY", "")
    if producer_repository != "snapetech-org/pleazyng":
        raise ValueError("Build artifacts may only be restored within snapetech-org/pleazyng")
    receipt_path = args.artifact_dir / "build-provenance.json"
    archive_path = args.artifact_dir / f"pleazyng-apple-store-build-{args.platform}.tar.gz"
    if receipt_path.is_symlink() or not receipt_path.is_file() or archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError("Downloaded build artifact must contain a regular receipt and app archive")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError("Downloaded build provenance is not a JSON object")
    app_name = validate_build_receipt(
        receipt,
        platform=args.platform,
        producer_repository=producer_repository,
        producer_run_id=args.producer_run_id,
        producer_run_attempt=args.producer_run_attempt,
        capture_tools_sha=expected_tools_sha,
    )
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        seen: set[str] = set()
        for member in members:
            member_path = PurePosixPath(member.name)
            if (member_path.is_absolute() or not member_path.parts
                    or any(part in ("", ".", "..") for part in member_path.parts)):
                raise ValueError(f"Build archive contains an unsafe path: {member.name!r}")
            normalized_name = member_path.as_posix()
            if normalized_name in seen:
                raise ValueError(f"Build archive contains a duplicate path: {normalized_name}")
            seen.add(normalized_name)
            if normalized_name != "build-provenance.json" and member_path.parts[0] != app_name:
                raise ValueError(f"Build archive contains an unexpected path: {normalized_name}")
            if normalized_name == "build-provenance.json" and not member.isfile():
                raise ValueError("Build archive provenance entry is not a regular file")
            if not (member.isdir() or member.isfile() or member.issym()):
                raise ValueError(f"Build archive contains an unsupported entry type: {normalized_name}")
            if member.issym():
                link = PurePosixPath(member.linkname)
                if link.is_absolute():
                    raise ValueError(f"Build archive contains an absolute symlink: {normalized_name}")
                resolved_link = posixpath.normpath(posixpath.join(member_path.parent.as_posix(), member.linkname))
                if resolved_link != app_name and not resolved_link.startswith(f"{app_name}/"):
                    raise ValueError(f"Build archive symlink escapes the app bundle: {normalized_name}")

        if "build-provenance.json" not in seen or app_name not in seen:
            raise ValueError("Build archive is missing the app bundle or its provenance receipt")
        archive_receipt_member = next(member for member in members if member.name == "build-provenance.json")
        archive_receipt_file = archive.extractfile(archive_receipt_member)
        if archive_receipt_file is None:
            raise ValueError("Build archive provenance receipt is unreadable")
        with archive_receipt_file:
            archive_receipt = json.load(archive_receipt_file)
        if archive_receipt != receipt:
            raise ValueError("Build archive receipt differs from the downloaded provenance receipt")

        for member in members:
            member_path = PurePosixPath(member.name)
            destination = output.joinpath(*member_path.parts)
            parent = destination.parent
            if any(part.is_symlink() for part in (parent, *parent.parents) if part != output.parent):
                raise ValueError(f"Build archive attempts to write through a symlink: {member.name}")
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                destination.chmod(member.mode & 0o777)
            elif member.issym():
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(member.linkname, destination)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"Build archive could not read: {member.name}")
                with source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
                destination.chmod(member.mode & 0o777)

    app = output / app_name
    identity = app_info(app)
    if (identity["bundle_id"] != receipt["bundle_id"] or identity["executable"] != receipt["executable"]
            or identity["version"] != VERSION or identity["build_number"] != BUILD_NUMBER):
        raise ValueError("Restored app identity differs from its build receipt")
    if sha256_tree(app) != receipt["app_bundle_sha256"]:
        raise ValueError("Restored app bundle hash differs from its build receipt")
    executable = app / "Contents" / "MacOS" / identity["executable"] if args.platform == "macos" else app / identity["executable"]
    if not executable.is_file() or not executable.stat().st_mode & 0o111:
        raise ValueError(f"Restored application executable is missing or not executable: {executable}")
    architectures = sorted(set(run(["/usr/bin/lipo", "-archs", str(executable)], capture=True, timeout=60).split()))
    if architectures != receipt["architectures"]:
        raise ValueError(f"Restored app architectures differ from its receipt: {architectures}")
    print(f"Restored validated {receipt['platform']} app at {app}")


def version_pair(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", value)
    if match is None:
        raise ValueError(f"Unexpected Apple SDK/runtime version: {value!r}")
    return int(match.group(1)), int(match.group(2))


def number_tokens(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))


def device_preference(name: str, category: str) -> tuple[Any, ...]:
    lowered = name.lower()
    generation = number_tokens(name)
    if category == "iphone":
        size = 5 if "pro max" in lowered else 4 if "plus" in lowered else 3 if "pro" in lowered else 2 if "air" in lowered else 1
        return (size, generation, name)
    if category == "ipad":
        size = 5 if "13-inch" in lowered or "13 inch" in lowered else 4 if "12.9-inch" in lowered or "12.9 inch" in lowered else 3 if "pro" in lowered else 2 if "air" in lowered else 1
        return (size, generation, name)
    size = 2 if "4k" in lowered else 1
    return (size, generation, name)


def app_minimum_os_version(app: Path) -> tuple[int, int]:
    try:
        with (app / "Info.plist").open("rb") as source:
            info = plistlib.load(source)
    except (OSError, plistlib.InvalidFileException) as error:
        raise RuntimeError(f"Could not read the retained app's minimum OS version: {app}") from error
    minimum = info.get("MinimumOSVersion") if isinstance(info, dict) else None
    if not isinstance(minimum, str):
        raise RuntimeError("The retained iOS app bundle does not declare a MinimumOSVersion")
    return version_pair(minimum)


def create_simulator(
    platform: str,
    category: str,
    run_id: str,
    attempt: str,
    *,
    minimum_os_version: tuple[int, int] | None = None,
) -> dict[str, str]:
    sdk = "iphonesimulator" if platform == "iOS" else "appletvsimulator"
    sdk_version = version_pair(run(["xcrun", "--sdk", sdk, "--show-sdk-version"], capture=True, timeout=60))
    inventory = json.loads(run(["xcrun", "simctl", "list", "-j"], capture=True, timeout=60))
    available_runtimes = [
        runtime for runtime in inventory.get("runtimes", [])
        if runtime.get("isAvailable") is True
        and f".{platform}-" in runtime.get("identifier", "")
    ]
    base_name = f"Pleazyng-{run_id}-{attempt}-{category}"

    def available_devices(runtime: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            device for device in inventory.get("devices", {}).get(runtime["identifier"], [])
            if device.get("isAvailable") is True
            and (device.get("name", "").startswith("iPhone") if category == "iphone"
                 else device.get("name", "").startswith("iPad") if category == "ipad"
                 else device.get("name", "").startswith("Apple TV"))
            and isinstance(device.get("deviceTypeIdentifier"), str)
        ]

    if category == "ipad" and platform == "iOS":
        minimum = minimum_os_version or (0, 0)
        runtimes = sorted(
            (runtime for runtime in available_runtimes if version_pair(runtime.get("version", "")) >= minimum),
            key=lambda runtime: (version_pair(runtime["version"]), runtime["identifier"]),
            reverse=True,
        )
        if not runtimes:
            raise RuntimeError(
                f"No available iOS Simulator runtime meets app MinimumOSVersion "
                f"{minimum[0]}.{minimum[1]}; available runtimes: "
                f"{[(item.get('identifier'), item.get('version')) for item in available_runtimes]}"
            )

        def is_thirteen_inch(name: str) -> bool:
            lowered = name.lower()
            return "13-inch" in lowered or "13 inch" in lowered

        device_type_inventory = json.loads(
            run(["xcrun", "simctl", "list", "devicetypes", "-j"], capture=True, timeout=60)
        )
        registered_ipads = [
            item for item in device_type_inventory.get("devicetypes", [])
            if item.get("productFamily") in (None, "iPad")
            and isinstance(item.get("name"), str)
            and item["name"].startswith("iPad")
            and is_thirteen_inch(item["name"])
            and isinstance(item.get("identifier"), str)
        ]
        registered_ipads.sort(
            key=lambda item: device_preference(item["name"], category),
            reverse=True,
        )
        if not registered_ipads:
            available_names = sorted(
                item.get("name", "") for item in device_type_inventory.get("devicetypes", [])
                if item.get("productFamily") in (None, "iPad")
                and isinstance(item.get("name"), str)
                and item["name"].startswith("iPad")
            )
            raise RuntimeError(
                f"No registered 13-inch iPad device type is available; registered iPad device types: {available_names}"
            )

        failures: list[str] = []
        for runtime in runtimes:
            devices = [device for device in available_devices(runtime) if is_thirteen_inch(device["name"])]
            if devices:
                selected = max(devices, key=lambda item: device_preference(item["name"], category))
                templates = [{
                    "name": selected["name"],
                    "device_type": selected["deviceTypeIdentifier"],
                }]
            else:
                templates = [
                    {"name": item["name"], "device_type": item["identifier"]}
                    for item in registered_ipads
                ]
            runtime_suffix = re.sub(r"[^A-Za-z0-9-]+", "-", runtime["identifier"].rsplit(".", 1)[-1])
            for template_index, template in enumerate(templates):
                suffix = f"{runtime_suffix}-{template_index + 1}"
                name = f"{base_name}-{suffix}"
                try:
                    udid = run([
                        "xcrun", "simctl", "create", name,
                        template["device_type"], runtime["identifier"],
                    ], capture=True, timeout=120)
                except subprocess.CalledProcessError as error:
                    detail = (error.stderr or error.stdout or str(error)).strip().splitlines()
                    failures.append(
                        f"{runtime['version']} {template['name']} ({template['device_type']}): "
                        f"{detail[-1] if detail else error}"
                    )
                    continue
                if re.fullmatch(r"[0-9A-Fa-f-]{36}", udid) is None:
                    raise RuntimeError(f"Simulator returned an invalid device identifier: {udid!r}")
                return {
                    "udid": udid,
                    "name": name,
                    "runtime_id": runtime["identifier"],
                    "runtime_version": runtime["version"],
                    "device_name": template["name"],
                    "device_type": template["device_type"],
                    "sdk_version": run(["xcrun", "--sdk", sdk, "--show-sdk-version"], capture=True, timeout=60),
                }
        raise RuntimeError(
            f"Could not create a 13-inch iPad simulator meeting MinimumOSVersion {minimum[0]}.{minimum[1]}. "
            f"Tried installed iOS runtimes {[(item['identifier'], item['version']) for item in runtimes]}; "
            + "; ".join(failures)
        )

    runtimes = [runtime for runtime in available_runtimes if version_pair(runtime.get("version", "")) == sdk_version]
    if not runtimes:
        raise RuntimeError(f"No {platform} Simulator runtime matches SDK {sdk_version[0]}.{sdk_version[1]}")
    runtime = sorted(runtimes, key=lambda item: item["identifier"])[-1]
    devices = available_devices(runtime)
    if not devices:
        raise RuntimeError(f"No available {category} device exists in runtime {runtime['identifier']}")
    selected = max(devices, key=lambda item: device_preference(item["name"], category))
    name = base_name
    udid = run([
        "xcrun", "simctl", "create", name,
        selected["deviceTypeIdentifier"], runtime["identifier"],
    ], capture=True, timeout=120)
    if re.fullmatch(r"[0-9A-Fa-f-]{36}", udid) is None:
        raise RuntimeError(f"Simulator returned an invalid device identifier: {udid!r}")
    return {
        "udid": udid,
        "name": name,
        "runtime_id": runtime["identifier"],
        "runtime_version": runtime["version"],
        "device_name": selected["name"],
        "device_type": selected["deviceTypeIdentifier"],
        "sdk_version": run(["xcrun", "--sdk", sdk, "--show-sdk-version"], capture=True, timeout=60),
    }


def cleanup_simulator(device: dict[str, str]) -> None:
    inventory = json.loads(run(["xcrun", "simctl", "list", "-j"], capture=True, timeout=60))
    matches = [
        (runtime_id, item)
        for runtime_id, devices in inventory.get("devices", {}).items()
        for item in devices
        if item.get("udid") == device["udid"]
    ]
    if not matches:
        return
    runtime_id, item = matches[0]
    if (runtime_id != device["runtime_id"] or item.get("name") != device["name"]
            or item.get("deviceTypeIdentifier") != device["device_type"]):
        raise RuntimeError("Created simulator identity changed; refusing to remove it")
    if item.get("state") != "Shutdown":
        run(["xcrun", "simctl", "shutdown", device["udid"]], timeout=120)
    run(["xcrun", "simctl", "delete", device["udid"]], timeout=120)


def runner_signing_identity(
    derived_data: Path,
    target_bundle_id: str,
    evidence_dir: Path | None = None,
) -> tuple[Path, dict[str, Any], str, str, dict[str, Any], Path | None]:
    """Derive the simulator prefix from Xcode's observed simulated test entitlements.

    Xcode 26 emits the test target's simulated entitlement file with the derived
    Keychain group but without an ``application-identifier``. Read the actual
    signed UI-test runner too and cross-check its application identifier when
    one is present instead of inventing a missing value in Xcode's plist.
    """
    runner_apps = [path for path in derived_data.rglob("*-Runner.app") if path.is_dir()]
    if len(runner_apps) != 1:
        raise RuntimeError(f"Expected one signed UI-test runner app; found {len(runner_apps)}")
    runner_app = runner_apps[0]
    try:
        with (runner_app / "Info.plist").open("rb") as source:
            runner_info = plistlib.load(source)
    except (OSError, plistlib.InvalidFileException) as error:
        raise RuntimeError("The signed UI-test runner has no readable bundle identity") from error
    runner_bundle_id = runner_info.get("CFBundleIdentifier") if isinstance(runner_info, dict) else None
    accepted_runner_ids = {target_bundle_id, f"{target_bundle_id}.xctrunner"}
    if runner_bundle_id not in accepted_runner_ids:
        raise RuntimeError(f"The signed UI-test runner has an unexpected bundle identifier: {runner_bundle_id!r}")

    signed_entitlements = codesign_entitlements(runner_app)
    candidates: list[tuple[Path, dict[str, Any], str]] = []
    expected_group_suffix = f".{target_bundle_id}"
    for path in derived_data.rglob("*.xcent"):
        if not path.name.endswith("-Simulated.xcent"):
            continue
        try:
            with path.open("rb") as source:
                entitlements = plistlib.load(source)
        except (OSError, plistlib.InvalidFileException):
            continue
        if not isinstance(entitlements, dict):
            continue
        groups = entitlements.get("keychain-access-groups")
        if (not isinstance(groups, list) or len(groups) != 1
                or not isinstance(groups[0], str) or not groups[0].endswith(expected_group_suffix)):
            continue
        prefix = groups[0][:-len(target_bundle_id)]
        if not prefix.endswith(".") or re.fullmatch(r"[A-Za-z0-9]{10}\.", prefix) is None:
            continue
        application_identifier = entitlements.get("application-identifier")
        if application_identifier is not None:
            valid_ids = {
                f"{prefix}{target_bundle_id}",
                f"{prefix}{target_bundle_id}.xctrunner",
            }
            if application_identifier not in valid_ids:
                raise RuntimeError("The simulated UI-test entitlement identity does not match its derived Keychain prefix")
        candidates.append((path, entitlements, prefix))

    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected exactly one Xcode-simulated UI-test entitlement file for {target_bundle_id}; found {len(candidates)}"
        )
    path, entitlements, prefix = candidates[0]
    signed_application_identifier = signed_entitlements.get("application-identifier")
    if signed_application_identifier is not None:
        valid_signed_ids = {
            f"{prefix}{target_bundle_id}",
            f"{prefix}{target_bundle_id}.xctrunner",
        }
        if signed_application_identifier not in valid_signed_ids:
            raise RuntimeError("The signed UI-test runner application identifier disagrees with Xcode's simulated prefix")
    signed_groups = signed_entitlements.get("keychain-access-groups")
    if signed_groups is not None:
        allowed_groups = {f"{prefix}{target_bundle_id}", f"{prefix}{runner_bundle_id}"}
        if (not isinstance(signed_groups, list) or not all(isinstance(group, str) for group in signed_groups)
                or len(signed_groups) > 1 or not set(signed_groups).issubset(allowed_groups)):
            raise RuntimeError("The signed UI-test runner exposes an unexpected Keychain access group")

    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        preserved_xcent = evidence_dir / "ui-test-runner-simulated-entitlements.xcent"
        shutil.copyfile(path, preserved_xcent)
        signed_entitlements_path = evidence_dir / "ui-test-runner-code-signature-entitlements.plist"
        signed_entitlements_path.write_bytes(plistlib.dumps(signed_entitlements, fmt=plistlib.FMT_XML, sort_keys=True))
        path = preserved_xcent
    else:
        signed_entitlements_path = None
    return path, entitlements, prefix, runner_bundle_id, signed_entitlements, signed_entitlements_path


def derive_ios_simulator_entitlements(
    base_entitlements_path: Path,
    runner_entitlements: dict[str, Any],
    runner_bundle_id: str,
    product_bundle_id: str,
) -> tuple[dict[str, Any], str, str]:
    runner_identifier = runner_entitlements.get("application-identifier")
    runner_groups = runner_entitlements.get("keychain-access-groups")
    group_bundle_id = runner_bundle_id.removesuffix(".xctrunner")
    if (not isinstance(runner_groups, list) or len(runner_groups) != 1
            or not isinstance(runner_groups[0], str) or not runner_groups[0].endswith(f".{group_bundle_id}")):
        raise ValueError("The Xcode-simulated UI-test Keychain group does not match its owned test bundle")
    prefix = runner_groups[0][:-len(group_bundle_id)]
    if re.fullmatch(r"[A-Za-z0-9]{10}\.", prefix) is None:
        raise ValueError("The Xcode-simulated UI-test Keychain group has an invalid derived prefix")
    if runner_identifier is not None:
        valid_ids = {f"{prefix}{group_bundle_id}", f"{prefix}{runner_bundle_id}"}
        if runner_identifier not in valid_ids:
            raise ValueError("The UI-test runner application identifier disagrees with its Keychain group prefix")

    with base_entitlements_path.open("rb") as source:
        base = plistlib.load(source)
    if (not isinstance(base, dict) or set(base) != {"keychain-access-groups"}
            or base.get("keychain-access-groups") != []):
        raise ValueError("The iOS simulator signing source must contain only the approved empty Keychain group list")
    product_identifier = f"{prefix}{product_bundle_id}"
    result = dict(base)
    result["application-identifier"] = product_identifier
    result["keychain-access-groups"] = [product_identifier]
    return result, prefix, product_identifier


def derive_nested_bundle_simulator_entitlements(
    source_entitlements: dict[str, Any],
    prefix: str,
    bundle_id: str,
) -> dict[str, Any]:
    if re.fullmatch(r"[A-Za-z0-9]{10}\.", prefix) is None:
        raise ValueError("The nested Simulator bundle has an invalid Xcode-derived prefix")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", bundle_id):
        raise ValueError("The nested Simulator bundle has an invalid verified bundle identifier")
    identity_keys = {
        "application-identifier",
        "keychain-access-groups",
        "com.apple.developer.team-identifier",
        "com.apple.security.application-groups",
    }
    if identity_keys.intersection(source_entitlements):
        raise ValueError("The nested entitlement source already contains identity or sharing grants")
    result = dict(source_entitlements)
    result["application-identifier"] = f"{prefix}{bundle_id}"
    return result


def nested_code_paths(app: Path) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    for path in app.rglob("*"):
        if path.is_dir() and path.suffix in (".app", ".appex", ".framework", ".xpc"):
            candidates.append((path, path.suffix.lstrip(".")))
        elif path.is_file() and path.suffix == ".dylib":
            candidates.append((path, "dylib"))
    return sorted(
        candidates,
        key=lambda candidate: (len(candidate[0].relative_to(app).parts), candidate[0].as_posix()),
        reverse=True,
    )


def simulator_codesign_command(code_path: Path, entitlements_path: Path | None = None) -> list[str]:
    command = [
        "/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none",
    ]
    if entitlements_path is not None:
        command.extend(["--generate-entitlement-der", "--entitlements", str(entitlements_path)])
    command.append(str(code_path))
    return command


def bundle_executable(bundle: Path) -> tuple[str, str, Path]:
    info_path = next(
        (candidate for candidate in (bundle / "Info.plist", bundle / "Contents" / "Info.plist")
         if candidate.is_file()),
        bundle / "Info.plist",
    )
    with info_path.open("rb") as source:
        info = plistlib.load(source)
    bundle_id = info.get("CFBundleIdentifier")
    executable = info.get("CFBundleExecutable")
    if (not isinstance(bundle_id, str) or not isinstance(executable, str) or not executable
            or Path(executable).name != executable):
        raise ValueError(f"Nested code bundle has no safe executable identity: {bundle}")
    executable_path = bundle / executable
    needs_execute_mode = bundle.suffix != ".framework"
    if (not executable_path.is_file()
            or (needs_execute_mode and not executable_path.stat().st_mode & 0o111)):
        raise ValueError(f"Nested code executable is missing or has an invalid mode: {executable_path}")
    return bundle_id, executable, executable_path


def code_executable_inventory(app: Path) -> list[dict[str, Any]]:
    entries: list[tuple[Path, str, str, Path]] = []
    main = app_info(app)
    main_executable = app / main["executable"]
    if not main_executable.is_file() or not main_executable.stat().st_mode & 0o111:
        raise ValueError(f"Product executable is missing or not executable: {main_executable}")
    entries.append((app, "app", main["bundle_id"], main_executable))
    for path, kind in nested_code_paths(app):
        if kind == "dylib":
            entries.append((path, kind, path.stem, path))
        else:
            bundle_id, _, executable = bundle_executable(path)
            entries.append((path, kind, bundle_id, executable))

    inventory = []
    for code_path, kind, identifier, executable in entries:
        architectures = sorted(set(run(["/usr/bin/lipo", "-archs", str(executable)], capture=True).split()))
        if not architectures:
            raise ValueError(f"No Mach-O architecture was found in {executable}")
        inventory.append({
            "code_path": code_path.relative_to(app).as_posix() or ".",
            "code_kind": kind,
            "bundle_or_file_identifier": identifier,
            "executable_path": executable.relative_to(app).as_posix(),
            "executable_mode": f"{stat.S_IMODE(executable.stat().st_mode):04o}",
            "executable_size_bytes": executable.stat().st_size,
            "architectures": architectures,
        })
    return inventory


def codesign_details(code_path: Path) -> str:
    result = subprocess.run(
        ["/usr/bin/codesign", "--display", "--verbose=5", str(code_path)],
        check=False,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not inspect the Simulator code signature for {code_path}: {result.stderr.strip()}")
    return (result.stdout + result.stderr).strip()


def capture_ios_simulator_launch_diagnostics(
    simulator_udid: str,
    output: Path,
    filename: str,
    app_executable: str,
) -> dict[str, Any]:
    if re.fullmatch(r"[A-Za-z0-9._-]+", app_executable) is None:
        raise ValueError("The product's Info.plist executable name is invalid for Simulator diagnostics")
    log_path = output / f"{Path(filename).stem}.simulator-launch.log"
    services = (
        "SpringBoard", "launchd", "launchd_sim", "runningboardd", "amfid", "installd",
        "launchservicesd", "lsd", "dyld", app_executable, "PleazyngInUseCaptureTests-Runner",
    )
    service_predicate = " OR ".join(f'process == "{name}"' for name in dict.fromkeys(services))
    relevant_text = (
        'eventMessage CONTAINS[c] "org.snapetech.pleazyng" '
        'OR eventMessage CONTAINS[c] "RommWorker" '
        'OR eventMessage CONTAINS[c] "FAKETEAMID" '
        'OR eventMessage CONTAINS[c] "spawn" '
        'OR eventMessage CONTAINS[c] "deny" '
        'OR eventMessage CONTAINS[c] "signature" '
        'OR eventMessage CONTAINS[c] "entitlement" '
        'OR eventMessage CONTAINS[c] "dyld"'
    )
    predicate = f"(({service_predicate}) AND ({relevant_text})) OR process == \"{app_executable}\""
    command = [
        "xcrun", "simctl", "spawn", simulator_udid, "log", "show",
        "--last", "15m", "--style", "compact", "--predicate", predicate,
    ]
    with log_path.open("w", encoding="utf-8") as log_file:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
    if result.returncode == 0 and log_path.stat().st_size == 0:
        log_path.write_text("No matching Simulator launch, AMFI, dyld, or SpringBoard events were retained.\n", encoding="utf-8")
    return {
        "filename": log_path.name,
        "sha256": sha256_file(log_path),
        "size_bytes": log_path.stat().st_size,
        "exit_code": result.returncode,
        "app_executable": app_executable,
        "filter": predicate,
        "scope": "Fresh capture Simulator launch, SpringBoard, launchd, RunningBoard, AMFI, dyld, installer, and product executable records.",
    }


def simulator_reported_tvos_app_stopped(returncode: int, stdout: str, stderr: str) -> bool:
    details = f"{stdout}\n{stderr}"
    return (
        returncode == 3
        and "NSPOSIXErrorDomain" in details
        and re.search(r"\bcode\s*=\s*3\b", details) is not None
        and "found nothing to terminate" in details.lower()
    )


def simulator_launch_pid(stdout: str) -> int | None:
    match = re.search(rf"{re.escape(BUNDLE_ID)}:\s*([0-9]+)\b", stdout)
    return int(match.group(1)) if match is not None else None


def capture_tvos_simulator_launch_diagnostics(
    simulator_udid: str,
    output: Path,
    filename: str,
    app_executable: str,
    app_pid: int | None,
) -> dict[str, Any]:
    if re.fullmatch(r"[A-Za-z0-9._-]+", app_executable) is None:
        raise ValueError("The product's Info.plist executable name is invalid for tvOS diagnostics")
    stem = Path(filename).stem
    state: dict[str, Any] = {
        "simulator_udid": simulator_udid,
        "app_executable": app_executable,
        "preflight_process_id": app_pid,
        "scope": "The disposable tvOS capture Simulator only; no StoreKit action or product rebuild was performed.",
    }
    process_path = output / f"{stem}.simulator-process-state.txt"
    process_command = [
        "xcrun", "simctl", "spawn", simulator_udid, "/bin/ps",
        "-A", "-o", "pid=,ppid=,state=,etime=,command=",
    ]
    try:
        process_result = subprocess.run(
            process_command, check=False, text=True, capture_output=True, timeout=30,
        )
        app_rows = [
            line for line in process_result.stdout.splitlines()
            if app_executable in line or (app_pid is not None and re.match(rf"\s*{app_pid}\b", line))
        ]
        process_path.write_text(
            "command: " + " ".join(process_command) + "\n"
            + f"exit_code: {process_result.returncode}\n"
            + "matching_app_processes:\n" + "\n".join(app_rows) + "\n"
            + "stderr:\n" + process_result.stderr,
            encoding="utf-8",
        )
        state["process_state"] = {
            "path": process_path.name,
            "exit_code": process_result.returncode,
            "matching_rows": app_rows,
            "sha256": sha256_file(process_path),
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        process_path.write_text(f"Simulator process-state probe failed: {error}\n", encoding="utf-8")
        state["process_state"] = {"path": process_path.name, "error": str(error)}

    services = (
        "SpringBoard", "launchd", "launchd_sim", "runningboardd", "amfid", "installd",
        "launchservicesd", "lsd", "dyld", "ReportCrash", app_executable,
    )
    service_predicate = " OR ".join(f'process == "{name}"' for name in dict.fromkeys(services))
    relevant_text = (
        f'eventMessage CONTAINS[c] "{BUNDLE_ID}" '
        'OR eventMessage CONTAINS[c] "crash" '
        'OR eventMessage CONTAINS[c] "termination" '
        'OR eventMessage CONTAINS[c] "dyld" '
        'OR eventMessage CONTAINS[c] "launch" '
        'OR eventMessage CONTAINS[c] "deny" '
        'OR eventMessage CONTAINS[c] "signature" '
        'OR eventMessage CONTAINS[c] "entitlement"'
    )
    predicate = f"(({service_predicate}) AND ({relevant_text})) OR process == \"{app_executable}\""
    if app_pid is not None:
        predicate += f" OR processIdentifier == {app_pid}"
    log_command = [
        "xcrun", "simctl", "spawn", simulator_udid, "log", "show",
        "--last", "15m", "--style", "compact", "--predicate", predicate,
    ]
    log_path = output / f"{stem}.simulator-launch-crash.log"
    try:
        log_result = subprocess.run(
            log_command, check=False, text=True, capture_output=True, timeout=120,
        )
        log_path.write_text(log_result.stdout + log_result.stderr, encoding="utf-8")
        if log_result.returncode == 0 and not log_result.stdout.strip():
            log_path.write_text(
                "No matching Simulator launch, crash reporter, dyld, or SpringBoard events were retained.\n",
                encoding="utf-8",
            )
        state["unified_log"] = {
            "path": log_path.name,
            "exit_code": log_result.returncode,
            "sha256": sha256_file(log_path),
            "filter": predicate,
            "preflight_process_id": app_pid,
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        log_path.write_text(f"Simulator unified log collection failed: {error}\n", encoding="utf-8")
        state["unified_log"] = {"path": log_path.name, "error": str(error)}

    summary_path = output / f"{stem}.simulator-launch-diagnostics.json"
    summary_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    state["summary_path"] = summary_path.name
    return state


def codesign_entitlements(app: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["/usr/bin/codesign", "-d", "--entitlements", ":-", str(app)],
        check=True,
        text=False,
        capture_output=True,
        timeout=60,
    )
    entitlements = plistlib.loads(result.stdout) if result.stdout.strip() else {}
    if not isinstance(entitlements, dict):
        raise RuntimeError("The signed application entitlements are not a plist dictionary")
    return entitlements


def sign_ios_app_for_simulator(
    app: Path,
    output: Path,
    filename: str,
    simulator_udid: str,
    original_app_digest: str,
    build_receipt: dict[str, Any],
    receipt_digest: str,
    base_entitlements_path: Path,
    extension_entitlements_source_path: Path,
    runner_xcent: Path,
    runner_entitlements: dict[str, Any],
    runner_bundle_id: str,
    runner_signed_entitlements: dict[str, Any],
    runner_signed_entitlements_path: Path,
    signing_state: dict[str, Any] | None,
) -> dict[str, Any]:
    derived, prefix, product_identifier = derive_ios_simulator_entitlements(
        base_entitlements_path, runner_entitlements, runner_bundle_id, BUNDLE_ID
    )
    entitlements_path = output / f"{Path(filename).stem}.simulator-entitlements.plist"
    entitlements_path.write_bytes(plistlib.dumps(derived, fmt=plistlib.FMT_XML, sort_keys=True))
    if signing_state is None:
        observed_digest = sha256_tree(app)
        if observed_digest != original_app_digest:
            raise ValueError("The restored iOS app changed before simulator-only signing")
        before_inventory = code_executable_inventory(app)
        nested = nested_code_paths(app)
        extensions = [path for path, kind in nested if kind == "appex"]
        if len(extensions) != 1:
            raise RuntimeError(f"Expected one product app extension to sign independently; found {len(extensions)}")
        extension = extensions[0]
        extension_bundle_id, extension_executable, _ = bundle_executable(extension)
        if extension_bundle_id != f"{BUNDLE_ID}.RommWorker":
            raise RuntimeError(f"The embedded product extension has an unexpected identity: {extension_bundle_id}")
        with extension_entitlements_source_path.open("rb") as source:
            extension_source_entitlements = plistlib.load(source)
        if not isinstance(extension_source_entitlements, dict):
            raise ValueError("The product extension's source entitlements are not a plist dictionary")
        extension_entitlements = derive_nested_bundle_simulator_entitlements(
            extension_source_entitlements, prefix, extension_bundle_id
        )
        extension_entitlements_path = output / f"{Path(filename).stem}.simulator-extension-entitlements.plist"
        extension_entitlements_path.write_bytes(
            plistlib.dumps(extension_entitlements, fmt=plistlib.FMT_XML, sort_keys=True)
        )

        signing_actions: list[dict[str, Any]] = []
        for code_path, kind in nested:
            action_entitlements = extension_entitlements_path if code_path == extension else None
            command = simulator_codesign_command(code_path, action_entitlements)
            run(command, timeout=600)
            signing_actions.append({
                "code_path": code_path.relative_to(app).as_posix(),
                "kind": kind,
                "bundle_id": extension_bundle_id if code_path == extension else (
                    bundle_executable(code_path)[0] if kind in ("app", "appex", "framework", "xpc")
                    else code_path.stem
                ),
                "entitlements_file": action_entitlements.name if action_entitlements else None,
                "command": [*command[:-1], "<bundle-path>"],
            })
        main_command = simulator_codesign_command(app, entitlements_path)
        run(main_command, timeout=600)
        signing_actions.append({
            "code_path": ".",
            "kind": "app",
            "bundle_id": BUNDLE_ID,
            "entitlements_file": entitlements_path.name,
            "command": [*main_command[:-1], "<app-bundle>"],
        })

        run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], timeout=300)
        for code_path, _ in nested:
            run(["/usr/bin/codesign", "--verify", "--strict", str(code_path)], timeout=180)
        signed_entitlements = codesign_entitlements(app)
        if (signed_entitlements.get("application-identifier") != product_identifier
                or signed_entitlements.get("keychain-access-groups") != [product_identifier]
                or set(signed_entitlements) != {"application-identifier", "keychain-access-groups"}):
            raise RuntimeError("Ad-hoc Simulator app signature does not contain only its derived application and Keychain identities")
        if (app / "embedded.mobileprovision").exists() or any(app.rglob("embedded.mobileprovision")):
            raise RuntimeError("The Simulator-only app unexpectedly contains a provisioning profile")
        if codesign_entitlements(extension) != extension_entitlements:
            raise RuntimeError("The Simulator extension signature does not match its own source entitlements and bundle identity")

        signature_inventory: list[dict[str, Any]] = []
        for code_path, kind in [(app, "app"), *nested]:
            actual_entitlements = codesign_entitlements(code_path)
            if code_path == app:
                expected_entitlements = derived
            elif code_path == extension:
                expected_entitlements = extension_entitlements
            else:
                expected_entitlements = {}
            if actual_entitlements != expected_entitlements:
                raise RuntimeError(f"Unexpected Simulator signing entitlements on {code_path.relative_to(app) or Path('.')}")
            signature_inventory.append({
                "code_path": code_path.relative_to(app).as_posix() or ".",
                "kind": kind,
                "entitlements": actual_entitlements,
                "codesign_display": codesign_details(code_path),
            })
        after_inventory = code_executable_inventory(app)
        before_by_path = {item["executable_path"]: item for item in before_inventory}
        after_by_path = {item["executable_path"]: item for item in after_inventory}
        if before_by_path.keys() != after_by_path.keys():
            raise RuntimeError("Simulator signing changed the product executable inventory")
        for executable_path, before in before_by_path.items():
            after = after_by_path[executable_path]
            if (before["architectures"] != after["architectures"]
                    or before["executable_mode"] != after["executable_mode"]):
                raise RuntimeError(f"Simulator signing changed executable mode or architectures: {executable_path}")

        inventory_path = output / f"{Path(filename).stem}.simulator-signing-inventory.json"
        inventory_record = {
            "schema_version": 1,
            "product_source_sha": SOURCE_SHA,
            "product_bundle_id": BUNDLE_ID,
            "producer_app_bundle_sha256_before_signing": observed_digest,
            "signed_app_bundle_sha256": sha256_tree(app),
            "signing_order": ["nested code deepest first", "main app last"],
            "signing_actions": signing_actions,
            "before_executables": before_inventory,
            "after_executables": after_inventory,
            "signed_code": signature_inventory,
            "nested_code_entitlements_policy": "Frameworks and dylibs have no entitlements; the RommWorker extension retains only its own source entitlement payload and derived application identifier, with no Keychain or app-group sharing grant.",
            "main_app_entitlements_policy": "The main app has only its Xcode-derived application identifier and matching default Keychain group, with DER entitlements; no profile or real signing identity.",
        }
        inventory_path.write_text(json.dumps(inventory_record, indent=2) + "\n", encoding="utf-8")
        derived_digest = sha256_tree(app)
        signing_state = {
            "derived_application_identifier": product_identifier,
            "derived_keychain_access_group": product_identifier,
            "derived_app_bundle_sha256": derived_digest,
            "entitlements_sha256": sha256_file(entitlements_path),
            "entitlements_source_sha256": sha256_file(base_entitlements_path),
            "runner_xcent_sha256": sha256_file(runner_xcent),
            "runner_xcent_filename": runner_xcent.name,
            "runner_signed_entitlements_sha256": sha256_file(runner_signed_entitlements_path),
            "runner_application_identifier": runner_signed_entitlements.get("application-identifier"),
            "runner_keychain_access_groups": runner_entitlements["keychain-access-groups"],
            "runner_signed_keychain_access_groups": runner_signed_entitlements.get("keychain-access-groups"),
            "runner_simulated_keychain_group_source": "Xcode-generated -Simulated.xcent; application-identifier may be absent",
            "runner_bundle_id": runner_bundle_id,
            "extension_bundle_id": extension_bundle_id,
            "extension_executable": extension_executable,
            "extension_source_entitlements_sha256": sha256_file(extension_entitlements_source_path),
            "extension_simulator_entitlements_sha256": sha256_file(extension_entitlements_path),
            "signing_inventory_filename": inventory_path.name,
            "signing_inventory_sha256": sha256_file(inventory_path),
            "signing_mode": "ad-hoc Simulator-only (-); nested code signed inside-out, frameworks/dylibs without entitlements, source extension scope retained, main app signed last with DER; producer artifact remains unchanged",
            "signature_verified": True,
        }
    else:
        if signing_state.get("derived_application_identifier") != product_identifier:
            raise RuntimeError("The second iOS simulator derived a different signing prefix")
        if sha256_tree(app) != signing_state.get("derived_app_bundle_sha256"):
            raise ValueError("The simulator-signed app bundle changed between iPhone and iPad captures")
        run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], timeout=300)
        signed_entitlements = codesign_entitlements(app)
        if (signed_entitlements.get("application-identifier") != product_identifier
                or signed_entitlements.get("keychain-access-groups") != [product_identifier]
                or set(signed_entitlements) != {"application-identifier", "keychain-access-groups"}):
            raise RuntimeError("The reused simulator signature no longer has the expected product Keychain group")
        if (app / "embedded.mobileprovision").exists():
            raise RuntimeError("The simulator-only app unexpectedly contains a provisioning profile")

    signature_entitlements_path = output / f"{Path(filename).stem}.simulator-signature-entitlements.plist"
    signature_entitlements_path.write_bytes(
        plistlib.dumps(signed_entitlements, fmt=plistlib.FMT_XML, sort_keys=True)
    )
    signing_state["derived_signature_entitlements_sha256"] = sha256_file(signature_entitlements_path)
    signing_state["derived_signature_entitlements_filename"] = signature_entitlements_path.name
    signing_state["source_keychain_access_groups"] = []
    signing_state["derived_signature_keychain_access_groups"] = signed_entitlements["keychain-access-groups"]
    signing_state["entitlement_expansion"] = (
        "Empty project entitlement group list expanded only for this ad-hoc Simulator signature to the "
        "derived product application identifier and matching default Keychain group."
    )

    provenance = {
        "schema_version": 1,
        "purpose": "diagnostic iOS Simulator signing for actual fresh-profile Keychain persistence capture",
        "product_source_sha": SOURCE_SHA,
        "product_source_tag": SOURCE_TAG,
        "product_version": VERSION,
        "product_build": BUILD_NUMBER,
        "bundle_id": BUNDLE_ID,
        "producer_run_id": build_receipt.get("producer_run_id"),
        "producer_run_attempt": build_receipt.get("producer_run_attempt"),
        "producer_capture_tools_sha": build_receipt.get("capture_tools_sha"),
        "producer_build_receipt_sha256": receipt_digest,
        "producer_app_bundle_sha256": original_app_digest,
        "producer_distribution_signing": build_receipt.get("distribution_signing"),
        "producer_architectures": build_receipt.get("architectures"),
        "simulator_udid": simulator_udid,
        "derived_signature": signing_state,
        "product_entitlements_file": "ios/Runner/DebugProfile.entitlements",
        "product_entitlements_file_sha256": signing_state["entitlements_source_sha256"],
        "test_runner_entitlements_sha256": signing_state["runner_xcent_sha256"],
        "derived_signature_entitlements_sha256": signing_state["derived_signature_entitlements_sha256"],
        "derived_app_bundle_sha256": signing_state["derived_app_bundle_sha256"],
        "qualification": "Simulator-only diagnostic signature; not an App Store signed or uploadable product artifact.",
    }
    signing_provenance = output / f"{Path(filename).stem}.simulator-signing-provenance.json"
    signing_provenance.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    signing_state["provenance_filename"] = signing_provenance.name
    return signing_state


def run_app_in_use_ui_test(
    platform: str,
    simulator_udid: str,
    output: Path,
    filename: str,
    *,
    app: Path,
    app_digest: str,
    build_receipt: dict[str, Any] | None,
    build_receipt_digest: str | None,
    simulator_entitlements: Path | None,
    extension_entitlements_source_path: Path | None,
    signing_state: dict[str, Any] | None,
) -> tuple[Path, dict[str, Any], dict[str, Any] | None]:
    xcode_platform = "tvOS" if platform == "tvOS" else "iOS"
    deployment_target = "15.0" if platform == "tvOS" else "15.5"
    project_spec_data = {
        "name": "PleazyngInUseCapture",
        "options": {
            "bundleIdPrefix": "org.snapetech.pleazyng.capture",
            "xcodeVersion": "26.5",
            "deploymentTarget": {xcode_platform: deployment_target},
        },
        "targets": {
            "PleazyngInUseCaptureTests": {
                "type": "bundle.ui-testing",
                "platform": xcode_platform,
                "sources": ["CaptureTests.swift"],
                "settings": {
                    "base": {
                        "PRODUCT_BUNDLE_IDENTIFIER": "org.snapetech.pleazyng.capture-ui-tests",
                        "GENERATE_INFOPLIST_FILE": "YES",
                        "SWIFT_VERSION": "5.0",
                        "CODE_SIGN_IDENTITY": "-",
                    },
                },
            },
        },
        "schemes": {
            "PleazyngInUseCapture": {
                "build": {"targets": {"PleazyngInUseCaptureTests": ["test"]}},
                "test": {
                    "targets": ["PleazyngInUseCaptureTests"],
                    "captureScreenshotsAutomatically": False,
                    "deleteScreenshotsWhenEachTestSucceeds": True,
                },
            },
        },
    }
    with tempfile.TemporaryDirectory(prefix="pleazyng-in-use-ui-test-") as temporary:
        project_root = Path(temporary)
        project_spec = project_root / "project.json"
        project_spec.write_text(json.dumps(project_spec_data, indent=2) + "\n", encoding="utf-8")
        test_source = project_root / "CaptureTests.swift"
        test_source.write_text(APP_IN_USE_UI_TEST, encoding="utf-8")
        runner_entitlements_path = project_root / "CaptureTests.entitlements"
        runner_entitlements_path.write_bytes(plistlib.dumps({
            "keychain-access-groups": ["$(AppIdentifierPrefix)$(PRODUCT_BUNDLE_IDENTIFIER)"],
        }, fmt=plistlib.FMT_XML, sort_keys=True))
        project_spec_data["targets"]["PleazyngInUseCaptureTests"]["settings"]["base"]["CODE_SIGN_ENTITLEMENTS"] = "CaptureTests.entitlements"
        project_spec.write_text(json.dumps(project_spec_data, indent=2) + "\n", encoding="utf-8")
        run([
            "xcodegen", "generate", "--spec", str(project_spec), "--project", str(project_root),
        ], timeout=180)
        project = project_root / "PleazyngInUseCapture.xcodeproj"
        project_inventory = json.loads(run([
            "xcodebuild", "-list", "-json", "-project", str(project),
        ], capture=True, timeout=180))
        targets = project_inventory.get("project", {}).get("targets", [])
        if targets != ["PleazyngInUseCaptureTests"]:
            raise RuntimeError(f"The UI capture project contains an unexpected target set: {targets}")

        derived_data = project_root / "derived-data"
        result_bundle = project_root / "capture.xcresult"
        log_path = output / f"{Path(filename).stem}.ui-test.log"
        build_log_path = output / f"{Path(filename).stem}.ui-runner-build.log"
        build_command = [
            "xcodebuild", "build-for-testing",
            "-project", str(project),
            "-scheme", "PleazyngInUseCapture",
            "-destination", f"platform={xcode_platform} Simulator,id={simulator_udid}",
            "-derivedDataPath", str(derived_data),
            "-parallel-testing-enabled", "NO",
        ]
        with build_log_path.open("w", encoding="utf-8") as log_file:
            result = subprocess.run(
                build_command,
                check=False,
                text=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=900,
            )
        if result.returncode != 0:
            tail = build_log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
            raise RuntimeError(
                f"The test-only UI runner build failed with exit code {result.returncode}; "
                f"runner build log is retained at {build_log_path}:\n" + "\n".join(tail)
            )

        runner_xcent = None
        runner_entitlements: dict[str, Any] = {}
        derived_prefix = None
        runner_bundle_id = None
        runner_signed_entitlements: dict[str, Any] = {}
        runner_signed_entitlements_path: Path | None = None
        if platform == "iOS":
            (
                runner_xcent,
                runner_entitlements,
                derived_prefix,
                runner_bundle_id,
                runner_signed_entitlements,
                runner_signed_entitlements_path,
            ) = runner_signing_identity(
                derived_data,
                "org.snapetech.pleazyng.capture-ui-tests",
                output,
            )
            if (simulator_entitlements is None or extension_entitlements_source_path is None
                    or build_receipt is None or build_receipt_digest is None
                    or runner_signed_entitlements_path is None):
                raise ValueError("iOS Keychain capture requires both product entitlement sources and the producer receipt")
            signing_state = sign_ios_app_for_simulator(
                app,
                output,
                filename,
                simulator_udid,
                app_digest,
                build_receipt,
                build_receipt_digest,
                simulator_entitlements,
                extension_entitlements_source_path,
                runner_xcent,
                runner_entitlements,
                runner_bundle_id,
                runner_signed_entitlements,
                runner_signed_entitlements_path,
                signing_state,
            )
        run(["xcrun", "simctl", "install", simulator_udid, str(app)], timeout=180)

        app_executable = app_info(app)["executable"]
        preflight_log_path = output / f"{Path(filename).stem}.simulator-launch-preflight.log"
        preflight_command = ["xcrun", "simctl", "launch", simulator_udid, BUNDLE_ID]
        preflight = subprocess.run(
            preflight_command,
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
        preflight_log_path.write_text(
            "command: " + " ".join(preflight_command) + "\n"
            + f"exit_code: {preflight.returncode}\n"
            + "stdout:\n" + preflight.stdout
            + "stderr:\n" + preflight.stderr,
            encoding="utf-8",
        )
        preflight_pid = simulator_launch_pid(preflight.stdout)
        if preflight.returncode != 0:
            if platform == "iOS":
                launch_diagnostics = capture_ios_simulator_launch_diagnostics(
                    simulator_udid, output, filename, app_executable
                )
            elif platform == "tvOS":
                launch_diagnostics = capture_tvos_simulator_launch_diagnostics(
                    simulator_udid, output, filename, app_executable, preflight_pid
                )
            else:
                launch_diagnostics = None
            raise RuntimeError(
                f"The ad-hoc Simulator product failed direct launch with exit code {preflight.returncode}; "
                f"launch output is retained at {preflight_log_path}; diagnostics={launch_diagnostics}"
            )
        tvos_termination_evidence = None
        launch_diagnostics = None
        result = None
        try:
            if platform == "tvOS":
                termination_command = ["xcrun", "simctl", "terminate", simulator_udid, BUNDLE_ID]
                termination = subprocess.run(
                    termination_command,
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                termination_log_path = output / f"{Path(filename).stem}.simulator-pre-test-terminate.log"
                termination_log_path.write_text(
                    "command: " + " ".join(termination_command) + "\n"
                    + f"exit_code: {termination.returncode}\n"
                    + "stdout:\n" + termination.stdout
                    + "stderr:\n" + termination.stderr,
                    encoding="utf-8",
                )
                stopped_before_test = simulator_reported_tvos_app_stopped(
                    termination.returncode, termination.stdout, termination.stderr,
                )
                tvos_termination_evidence = {
                    "exit_code": termination.returncode,
                    "log_path": termination_log_path.name,
                    "already_stopped_exact_result": stopped_before_test,
                    "action": "continue to XCTest relaunch" if stopped_before_test else "not tolerated",
                }
                if termination.returncode != 0 and not stopped_before_test:
                    raise subprocess.CalledProcessError(
                        termination.returncode,
                        termination_command,
                        output=termination.stdout,
                        stderr=termination.stderr,
                    )
            else:
                run(["xcrun", "simctl", "terminate", simulator_udid, BUNDLE_ID], timeout=60)

            xctestruns = list((derived_data / "Build" / "Products").glob("*.xctestrun"))
            if len(xctestruns) != 1:
                raise RuntimeError(f"Expected one built UI test plan; found {len(xctestruns)}")
            command = [
                "xcodebuild", "test-without-building",
                "-xctestrun", str(xctestruns[0]),
                "-destination", f"platform={xcode_platform} Simulator,id={simulator_udid}",
                "-derivedDataPath", str(derived_data),
                "-resultBundlePath", str(result_bundle),
                "-parallel-testing-enabled", "NO",
                "-testLanguage", "en",
                "-testRegion", "US",
                "-only-testing:PleazyngInUseCaptureTests/PleazyngAppInUseCaptureTests/testCaptureFilesAndBooksScreen",
            ]
            with log_path.open("w", encoding="utf-8") as log_file:
                result = subprocess.run(
                    command,
                    check=False,
                    text=True,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    timeout=1500,
                )
        finally:
            if platform == "tvOS":
                try:
                    launch_diagnostics = capture_tvos_simulator_launch_diagnostics(
                        simulator_udid, output, filename, app_executable, preflight_pid,
                    )
                except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError,
                        subprocess.TimeoutExpired) as error:
                    launch_diagnostics = {"error": f"tvOS diagnostic collection failed: {error}"}
                    (output / f"{Path(filename).stem}.simulator-launch-diagnostics.json").write_text(
                        json.dumps(launch_diagnostics, indent=2) + "\n", encoding="utf-8",
                    )
            elif result is not None:
                launch_diagnostics = capture_ios_simulator_launch_diagnostics(
                    simulator_udid, output, filename, app_executable
                ) if platform == "iOS" else None
        if result.returncode != 0:
            failed_result_bundle = output / f"{Path(filename).stem}.failed.xcresult"
            if result_bundle.is_dir():
                shutil.copytree(result_bundle, failed_result_bundle)
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
            raise RuntimeError(
                f"The app-in-use UI test failed with exit code {result.returncode}; "
                f"runner log is retained at {log_path}; failure result bundle is retained at "
                f"{failed_result_bundle if failed_result_bundle.exists() else 'unavailable'}; "
                f"launch diagnostics={launch_diagnostics}; tvOS cleanup={tvos_termination_evidence}:\n"
                + "\n".join(tail)
            )

        export_dir = project_root / "exported-attachments"
        run([
            "xcrun", "xcresulttool", "export", "attachments",
            "--path", str(result_bundle),
            "--output-path", str(export_dir),
        ], timeout=180)
        manifest_path = export_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        matches = [
            attachment
            for test in manifest
            for attachment in test.get("attachments", [])
            if attachment.get("suggestedHumanReadableName", "").startswith("pleazyng-app-in-use-screen")
        ]
        if len(matches) != 1:
            raise RuntimeError(f"Expected one named real app screen attachment from XCTest; found {len(matches)}")
        exported_name = matches[0].get("exportedFileName", "")
        if not exported_name or Path(exported_name).name != exported_name:
            raise ValueError("The XCTest screenshot attachment has an invalid exported filename")
        exported_screenshot = export_dir / exported_name
        if not exported_screenshot.is_file():
            raise FileNotFoundError(f"XCTest did not export its named screenshot attachment: {exported_screenshot}")
        screenshot = output / filename
        shutil.copyfile(exported_screenshot, screenshot)
        runner_metadata = {
            "project_target": "PleazyngInUseCaptureTests",
            "project_targets": targets,
            "project_sha256": sha256_file(project_spec),
            "test_source_sha256": sha256_file(test_source),
            "test_runner_entitlements_sha256": sha256_file(runner_xcent) if runner_xcent is not None else None,
            "test_runner_application_identifier": runner_signed_entitlements.get("application-identifier"),
            "test_runner_code_signature_entitlements_sha256": (
                sha256_file(runner_signed_entitlements_path) if runner_signed_entitlements_path is not None else None
            ),
            "simulator_application_identifier_prefix": derived_prefix,
            "test_identifier": matches[0].get("suggestedHumanReadableName"),
            "test_target_bundle_id": "org.snapetech.pleazyng.capture-ui-tests",
            "product_app_bundle_id": BUNDLE_ID,
            "simulator_udid": simulator_udid,
            "xcode_version": run(["xcodebuild", "-version"], capture=True, timeout=60),
            "screenshot_attachment": exported_name,
            "direct_product_launch_exit_code": preflight.returncode,
            "direct_product_launch_log": preflight_log_path.name,
            "direct_product_launch_process_id": preflight_pid,
            "simulator_launch_diagnostics": launch_diagnostics,
            "tvos_pre_test_termination": tvos_termination_evidence,
            "product_target_compiled": False,
            "ios_keychain_roundtrip_verified": platform == "iOS",
            "ios_keychain_roundtrip_scope": (
                "Fresh-profile protected-state write/read, then app termination/relaunch and persisted decrypt/read; "
                "error-card absence is asserted."
                if platform == "iOS" else None
            ),
        }
        return screenshot, runner_metadata, signing_state


def run_macos_swift_json(source: str, arguments: list[str], *, timeout: int = 180) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="pleazyng-macos-native-capture-") as temporary:
        swift_file = Path(temporary) / "capture.swift"
        swift_file.write_text(source, encoding="utf-8")
        environment = dict(os.environ)
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "APPLE_API_PRIVATE_KEY"):
            environment.pop(key, None)
        result = subprocess.run(
            ["swift", str(swift_file), *arguments],
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=environment,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"Native Mac capture helper failed with exit code {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    try:
        record = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Native Mac capture helper returned invalid JSON: {result.stdout[:1000]}") from error
    if not isinstance(record, dict):
        raise RuntimeError("Native Mac capture helper returned a non-object result")
    return record


def resize_macos_app_window(pid: int, *, width: int, height: int, x: int, y: int) -> dict[str, int]:
    script = "\n".join((
        "on run",
        f"  set targetPid to {pid}",
        "  tell application \"System Events\"",
        "    set targetProcess to first application process whose unix id is targetPid",
        "    set frontmost of targetProcess to true",
        "    set targetWindow to first window of targetProcess",
        f"    set size of targetWindow to {{{width}, {height}}}",
        f"    set position of targetWindow to {{{x}, {y}}}",
        "    delay 1",
        "    set actualSize to size of targetWindow",
        "    set actualPosition to position of targetWindow",
        "    return ((item 1 of actualSize) as text) & \"|\" & ((item 2 of actualSize) as text) & \"|\" & ((item 1 of actualPosition) as text) & \"|\" & ((item 2 of actualPosition) as text)",
        "  end tell",
        "end run",
    ))
    result = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"System Events could not resize the real Pleazyng window: {(result.stderr or result.stdout).strip()}")
    fields = result.stdout.strip().split("|")
    if len(fields) != 4 or any(re.fullmatch(r"-?[0-9]+", value) is None for value in fields):
        raise RuntimeError(f"System Events returned unexpected Pleazyng window geometry: {result.stdout!r}")
    actual = dict(zip(("width", "height", "x", "y"), map(int, fields), strict=True))
    requested = {"width": width, "height": height, "x": x, "y": y}
    if actual != requested:
        raise RuntimeError(f"System Events did not apply the requested app window geometry: requested={requested}, actual={actual}")
    return actual


def macos_screen_ocr(path: Path) -> dict[str, Any]:
    evidence = run_macos_swift_json(MACOS_LISTING_OCR_SWIFT, [str(path)], timeout=120)
    expected_size = list(png_dimensions(path))
    if evidence.get("image_pixels") != expected_size:
        raise RuntimeError(f"Vision OCR decoded a different screenshot size than the PNG header: {evidence}")
    lines = evidence.get("recognized_lines")
    if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
        raise RuntimeError("Vision OCR returned malformed text evidence")
    evidence["screenshot_sha256"] = sha256_file(path)
    evidence["png_dimensions"] = expected_size
    evidence["recognized_text"] = " ".join(lines)
    return evidence


def require_macos_listing_markers(evidence: dict[str, Any], *, before_navigation: bool) -> None:
    text = str(evidence.get("recognized_text", ""))
    if before_navigation:
        if "Start fresh" not in text and "Use files and books" not in text:
            raise ValueError(f"The fresh app screen did not visibly offer a local setup path; Vision saw: {text}")
        return
    has_import = "Import files" in text
    has_empty_state = "Add a source or import a book" in text or "Add source" in text
    if not has_import or not has_empty_state:
        raise ValueError(f"The real empty Files and Books screen was not visible; Vision saw: {text}")


def write_macos_main_window_frame_preference(*, width: int, height: int, x: int, y: int) -> dict[str, Any]:
    key = f"NSWindow Frame {MACOS_MAIN_WINDOW_AUTOSAVE_NAME}"
    frame = f"{{{{{x}, {y}}}, {{{width}, {height}}}}}"
    run(["/usr/bin/defaults", "write", BUNDLE_ID, key, "-string", frame], timeout=30)
    observed = run(["/usr/bin/defaults", "read", BUNDLE_ID, key], capture=True, timeout=30)
    if observed != frame:
        raise RuntimeError(f"AppKit window frame preference did not retain its requested value: {observed!r}")
    return {
        "preference_domain": BUNDLE_ID,
        "preference_key": key,
        "saved_frame_string": observed,
        "requested_window_bounds_points": [x, y, width, height],
        "applied_before_app_launch": True,
    }


def require_live_desktop_capture(path: Path, display_state: dict[str, Any]) -> tuple[int, int]:
    dimensions = png_dimensions(path)
    bounds = display_state.get("display_bounds_points")
    if (not isinstance(bounds, list) or len(bounds) != 2
            or any(not isinstance(value, (int, float)) or value <= 0 for value in bounds)):
        raise ValueError(f"WindowServer did not report usable live desktop bounds: {display_state}")
    scale_x = dimensions[0] / float(bounds[0])
    scale_y = dimensions[1] / float(bounds[1])
    if abs(scale_x - scale_y) > 0.02:
        raise ValueError(
            f"The genuine desktop screenshot {dimensions} does not match live WindowServer bounds {bounds} "
            f"at one consistent pixel scale ({scale_x:g}x, {scale_y:g})"
        )
    return dimensions


def capture_macos_display_stage(
    screenshot: Path,
    evidence_path: Path,
    display_state: dict[str, Any],
    *,
    accepted_text: tuple[str, ...],
    timeout: int = 60,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        run(["/usr/sbin/screencapture", "-x", str(screenshot)], timeout=60)
        require_live_desktop_capture(screenshot, display_state)
        evidence = macos_screen_ocr(screenshot)
        evidence["live_display_state"] = display_state
        evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        text = str(evidence.get("recognized_text", ""))
        if any(marker in text for marker in accepted_text):
            return evidence
        if time.monotonic() >= deadline:
            raise ValueError(
                f"Timed out waiting for visible screen text {accepted_text}; Vision saw: {text}. "
                f"The genuine screen capture remains at {screenshot}."
            )
        time.sleep(2)


def click_macos_visible_text(
    app: Path,
    pid: int,
    evidence: dict[str, Any],
    target_text: str,
    *,
    display_state: dict[str, Any],
) -> dict[str, Any]:
    current_pid, window_id = _launched_app_pid_and_window(app)
    if current_pid != pid:
        raise RuntimeError("The Pleazyng process changed while navigating the Mac listing screen")
    geometry = run_macos_swift_json(MACOS_WINDOW_BOUNDS_SWIFT, [str(pid), str(window_id)])
    bounds = geometry.get("bounds_points")
    if geometry.get("on_screen") is not True or not isinstance(bounds, list) or len(bounds) != 4:
        raise ValueError(f"The live Pleazyng window is unavailable for native input: {geometry}")
    target = locate_macos_screen_text(
        evidence,
        target_text,
        window_bounds=bounds,
        display_points=display_state["display_bounds_points"],
    )
    native_event = post_macos_native_click(target["screen_point"])
    return {
        "target_region": target,
        "window_geometry": geometry,
        "native_event": native_event,
    }


def locate_macos_screen_text(
    evidence: dict[str, Any],
    target_text: str,
    *,
    window_bounds: list[float],
    display_points: list[float],
) -> dict[str, Any]:
    """Map a visibly OCR-recognized label to a native screen point inside its app window."""
    image_pixels = evidence.get("image_pixels")
    regions = evidence.get("recognized_regions")
    if (not isinstance(image_pixels, list) or len(image_pixels) != 2
            or any(not isinstance(value, int) or value <= 0 for value in image_pixels)
            or not isinstance(window_bounds, list) or len(window_bounds) != 4
            or not isinstance(display_points, list) or len(display_points) != 2
            or any(not isinstance(value, (int, float)) or value <= 0 for value in display_points)
            or not isinstance(regions, list)):
        raise ValueError("Vision text click needs screenshot pixels, app-window bounds, display points, and OCR regions")
    matches: list[dict[str, Any]] = []
    for region in regions:
        if not isinstance(region, dict):
            continue
        text = region.get("text")
        normalized_bounds = region.get("normalized_bounds")
        if (not isinstance(text, str) or target_text.casefold() not in text.casefold()
                or not isinstance(normalized_bounds, list) or len(normalized_bounds) != 4
                or any(not isinstance(value, (int, float)) for value in normalized_bounds)):
            continue
        left, bottom, width, height = map(float, normalized_bounds)
        if width <= 0 or height <= 0 or left < 0 or bottom < 0 or left + width > 1 or bottom + height > 1:
            continue
        image_width, image_height = image_pixels
        point_x = (left + width / 2) * image_width * float(display_points[0]) / image_width
        point_y = (1 - (bottom + height / 2)) * image_height * float(display_points[1]) / image_height
        window_x, window_y, window_width, window_height = map(float, window_bounds)
        if not (window_x <= point_x < window_x + window_width and window_y <= point_y < window_y + window_height):
            continue
        matches.append({
            "text": text,
            "confidence": region.get("confidence"),
            "normalized_bounds": [left, bottom, width, height],
            "screen_point": [round(point_x, 2), round(point_y, 2)],
            "window_bounds_points": [window_x, window_y, window_width, window_height],
        })
    if not matches:
        raise ValueError(f"Vision found no {target_text!r} text region inside the live Pleazyng window")
    if len(matches) > 1:
        matches.sort(key=lambda candidate: float(candidate.get("confidence") or 0), reverse=True)
        if (matches[0].get("confidence") is not None and matches[1].get("confidence") is not None
                and float(matches[0]["confidence"]) == float(matches[1]["confidence"])):
            raise ValueError(f"Vision found multiple equally confident {target_text!r} regions in the app window")
    return matches[0]


def post_macos_native_click(point: list[float]) -> dict[str, Any]:
    if len(point) != 2 or any(not isinstance(value, (int, float)) or value < 0 for value in point):
        raise ValueError(f"Native Mac click needs a nonnegative screen coordinate pair: {point}")
    result = run_macos_swift_json(MACOS_POST_NATIVE_CLICK_SWIFT, [str(point[0]), str(point[1])])
    if result.get("posted") is not True or result.get("screen_point") != point:
        raise RuntimeError(f"CoreGraphics did not confirm the requested native click: {result}")
    return result


def run_macos_listing_window_capture(app: Path, output: Path) -> tuple[Path, dict[str, Any]]:
    display_mode = run_macos_swift_json(MACOS_SELECT_WINDOW_CAPTURE_DISPLAY_SWIFT, [])
    display_mode_path = output / "macos-01-files-and-books.display-mode.json"
    display_mode_path.write_text(json.dumps(display_mode, indent=2) + "\n", encoding="utf-8")
    mode_points = display_mode.get("active_mode_points")
    mode_pixels = display_mode.get("active_mode_pixels")
    if mode_points != [1920.0, 1080.0] or mode_pixels != [1920, 1080]:
        raise ValueError(f"The hosted ARM Mac no longer exposes the probed 1920x1080 mode: {display_mode}")

    window_preference = write_macos_main_window_frame_preference(
        width=1280, height=800, x=0, y=0
    )
    (output / "macos-01-files-and-books.window-frame-preference.json").write_text(
        json.dumps(window_preference, indent=2) + "\n", encoding="utf-8"
    )

    environment = dict(os.environ)
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "APPLE_API_PRIVATE_KEY"):
        environment.pop(key, None)
    open_result = subprocess.run(
        ["open", "-n", "-a", str(app)],
        check=False,
        text=True,
        capture_output=True,
        timeout=120,
        env=environment,
    )
    if open_result.returncode != 0:
        raise RuntimeError(f"Could not launch the retained product app: {(open_result.stderr or open_result.stdout).strip()}")
    app_pid, initial_window_id = _launched_app_pid_and_window(app)
    run(["osascript", "-e", f'tell application id "{BUNDLE_ID}" to activate'], timeout=60)
    live_display_state = run_macos_swift_json(MACOS_LIVE_DISPLAY_STATE_SWIFT, [])
    (output / "macos-01-files-and-books.live-display-state.json").write_text(
        json.dumps(live_display_state, indent=2) + "\n", encoding="utf-8"
    )

    first_path = output / "macos-01-files-and-books.initial-desktop.png"
    first_ocr_path = output / "macos-01-files-and-books.initial-ocr.json"
    first_ocr = capture_macos_display_stage(
        first_path,
        first_ocr_path,
        live_display_state,
        accepted_text=("Start fresh", "Use files and books"),
    )
    require_macos_listing_markers(first_ocr, before_navigation=True)
    click_records: list[dict[str, Any]] = []
    first_text = str(first_ocr.get("recognized_text", ""))
    if "Start fresh" in first_text:
        click_records.append(click_macos_visible_text(
            app, app_pid, first_ocr, "Start fresh", display_state=live_display_state
        ))
        choice_path = output / "macos-02-files-and-books-choice.png"
        choice_ocr_path = output / "macos-02-files-and-books-choice-ocr.json"
        choice_ocr = capture_macos_display_stage(
            choice_path,
            choice_ocr_path,
            live_display_state,
            accepted_text=("Use files and books",),
        )
    else:
        choice_path = first_path
        choice_ocr_path = first_ocr_path
        choice_ocr = first_ocr

    click_records.append(click_macos_visible_text(
        app, app_pid, choice_ocr, "Use files and books", display_state=live_display_state
    ))
    (output / "macos-01-files-and-books.native-clicks.json").write_text(
        json.dumps(click_records, indent=2) + "\n", encoding="utf-8"
    )
    after_path = output / "macos-03-files-and-books-empty-library.png"
    after_ocr = capture_macos_display_stage(
        after_path,
        output / "macos-03-files-and-books-empty-library-ocr.json",
        live_display_state,
        accepted_text=("Add a source or import a book", "Add source"),
        timeout=45,
    )
    require_macos_listing_markers(after_ocr, before_navigation=False)

    final_pid, window_id = _launched_app_pid_and_window(app)
    if final_pid != app_pid:
        raise RuntimeError("The Pleazyng process changed while navigating to Files and Books")
    geometry_before_capture = run_macos_swift_json(
        MACOS_WINDOW_BOUNDS_SWIFT, [str(final_pid), str(window_id)]
    )
    before_bounds = geometry_before_capture.get("bounds_points")
    if (geometry_before_capture.get("on_screen") is not True or not isinstance(before_bounds, list)
            or len(before_bounds) != 4):
        raise ValueError(f"The real Pleazyng window is unavailable before store capture: {geometry_before_capture}")
    resize: dict[str, Any] = {
        "frame_preference": window_preference,
        "window_geometry_before_capture": geometry_before_capture,
        "accessibility_resize_used": False,
    }
    if before_bounds[2:] != [1280.0, 800.0]:
        fallback_resize = resize_macos_app_window(
            app_pid,
            width=1280,
            height=800,
            x=int(round(before_bounds[0])),
            y=int(round(before_bounds[1])),
        )
        resize["window_level_resize_fallback"] = fallback_resize
        resize["accessibility_resize_used"] = True
    resize["requested_bounds_points"] = [0, 0, 1280, 800]
    resize["window_server_clamping"] = "verified against live CoreGraphics bounds after AppKit restored the saved window frame"

    geometry = run_macos_swift_json(MACOS_WINDOW_BOUNDS_SWIFT, [str(final_pid), str(window_id)])
    bounds = geometry.get("bounds_points")
    if (geometry.get("window_id") != int(window_id) or geometry.get("on_screen") is not True
            or not isinstance(bounds, list) or len(bounds) != 4
            or bounds[2:] != [1280.0, 800.0]):
        raise ValueError(f"The actual product window did not retain the requested native 1280x800 geometry: {geometry}")
    desktop_points = live_display_state.get("display_bounds_points")
    fully_visible_on_desktop = (
        isinstance(desktop_points, list) and len(desktop_points) == 2
        and bounds[0] >= 0 and bounds[1] >= 0
        and bounds[0] + bounds[2] <= desktop_points[0]
        and bounds[1] + bounds[3] <= desktop_points[1]
    )

    screenshot = output / "macos-01-files-and-books.png"
    run(["/usr/sbin/screencapture", "-x", "-o", "-l", str(window_id), str(screenshot)], timeout=120)
    width, height = png_dimensions(screenshot)
    scale_x = width / bounds[2]
    scale_y = height / bounds[3]
    expected_size = (round(1280 * scale_x), round(800 * scale_y))
    if (width, height) != expected_size or abs(scale_x - scale_y) > 0.02:
        raise ValueError(
            f"The native app-window screenshot is {width}x{height}; expected the real 1280x800-point window "
            f"at {scale_x:g}x{scale_y:g} display scale ({expected_size[0]}x{expected_size[1]} pixels). "
            f"The unmodified window capture remains at {screenshot}."
        )
    if (width, height) not in ((1280, 800), (2560, 1600)):
        raise ValueError(f"Mac App Store screenshot size is not accepted: {width}x{height}: {screenshot}")
    final_ocr = macos_screen_ocr(screenshot)
    require_macos_listing_markers(final_ocr, before_navigation=False)
    png_encoding = normalize_opaque_rgb_png(screenshot)
    if png_color_type(screenshot) != 2:
        raise ValueError(f"Mac App Store screenshots must be opaque RGB PNGs: {screenshot}")

    for filename, evidence in (
        ("macos-01-files-and-books.final-window-ocr.json", final_ocr),
        ("macos-01-files-and-books.window.json", {"resize_request_and_result": resize, **geometry}),
    ):
        (output / filename).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    metadata = {
        "product_app_bundle_id": BUNDLE_ID,
        "product_app_path": str(app),
        "launch_process_id": app_pid,
        "initial_window_id": int(initial_window_id),
        "captured_window_id": int(window_id),
        "xcode_version": run(["xcodebuild", "-version"], capture=True, timeout=60),
        "display_mode": display_mode,
        "live_display_state": live_display_state,
        "window_resize": resize,
        "window_geometry": geometry,
        "window_fully_visible_on_host_desktop": fully_visible_on_desktop,
        "window_frame_preference_file": "macos-01-files-and-books.window-frame-preference.json",
        "initial_screen_file": first_path.name,
        "initial_screen_ocr_file": first_ocr_path.name,
        "files_books_choice_screen_file": choice_path.name,
        "files_books_choice_ocr_file": choice_ocr_path.name,
        "post_navigation_screen_file": after_path.name,
        "post_navigation_ocr_file": "macos-03-files-and-books-empty-library-ocr.json",
        "native_click_file": "macos-01-files-and-books.native-clicks.json",
        "navigation_input_method": "CoreGraphics CGEvent clicks centered on Vision OCR bounds of visible Start fresh and Use files and books text",
        "final_window_ocr_file": "macos-01-files-and-books.final-window-ocr.json",
        "screenshot_dimensions": [width, height],
        "screenshot_png_encoding": png_encoding,
        "screenshot_capture_sha256": sha256_file(screenshot),
        "screenshot_resized_or_padded": False,
        "screenshot_crop_scope": "The complete native Pleazyng app window selected by its live CoreGraphics window ID; no crop or resampling was applied.",
        "purchase_actions_performed": False,
    }
    return screenshot, metadata


def capture_simulator(
    app: Path,
    output: Path,
    *,
    platform: str,
    category: str,
    filename: str,
    run_id: str,
    attempt: str,
    app_digest: str,
    build_defines: list[str],
    tool_source_sha: str,
    app_in_use: bool = False,
    build_receipt: dict[str, Any] | None = None,
    build_receipt_digest: str | None = None,
    simulator_entitlements: Path | None = None,
    extension_entitlements_source_path: Path | None = None,
    signing_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    minimum_os_version = app_minimum_os_version(app) if platform == "iOS" and category == "ipad" else None
    device = create_simulator(
        platform,
        category,
        run_id,
        attempt,
        minimum_os_version=minimum_os_version,
    )
    simulator_runtime_diagnostics = None
    try:
        run(["xcrun", "simctl", "boot", device["udid"]], timeout=180)
        run(["xcrun", "simctl", "bootstatus", device["udid"], "-b"], timeout=300)
        simulator_appearance = "platform default"
        if platform == "iOS" and category != "ipad":
            run(["xcrun", "simctl", "ui", device["udid"], "appearance", "light"], timeout=60)
            simulator_appearance = "light"
        screenshot = output / filename
        ui_test_metadata = None
        if app_in_use:
            screenshot, ui_test_metadata, signing_state = run_app_in_use_ui_test(
                platform,
                device["udid"],
                output,
                filename,
                app=app,
                app_digest=app_digest,
                build_receipt=build_receipt,
                build_receipt_digest=build_receipt_digest,
                simulator_entitlements=simulator_entitlements,
                extension_entitlements_source_path=extension_entitlements_source_path,
                signing_state=signing_state,
            )
            if platform == "iOS":
                simulator_runtime_diagnostics = capture_ios_keychain_diagnostics(
                    device["udid"], output, filename, app_info(app)["executable"]
                )
        else:
            run(["xcrun", "simctl", "install", device["udid"], str(app)], timeout=180)
            run(["xcrun", "simctl", "launch", device["udid"], BUNDLE_ID], timeout=120)
            time.sleep(SIMULATOR_SETTLE_SECONDS)
            run(["xcrun", "simctl", "io", device["udid"], "screenshot", "--type=png", str(screenshot)], timeout=120)
        width, height = png_dimensions(screenshot)
        png_encoding = normalize_opaque_rgb_png(screenshot) if app_in_use else {
            "source_color_type": png_color_type(screenshot),
            "output_color_type": png_color_type(screenshot),
            "alpha_was_opaque": None,
            "rgb_samples_modified": False,
        }
        color_type = png_color_type(screenshot)
        if app_in_use and (color_type != 2 or png_encoding["alpha_was_opaque"] is False):
            raise ValueError(
                f"Apple listing screenshots must be opaque RGB PNGs; received PNG color type {color_type}: {screenshot}"
            )
        if app_in_use and platform == "iOS" and category == "ipad" and (width, height) not in (
                (2064, 2752), (2048, 2732)):
            raise ValueError(
                f"The iPad listing screenshot must use an accepted 13-inch size; received {width}x{height}: {screenshot}"
            )
        screen_content_check = require_visible_screen_content(screenshot) if app_in_use else None
        capture = {
            "filename": filename,
            "platform": platform,
            "build_configuration": BUILD_CONFIGURATION["ios" if platform == "iOS" else "tvos"],
            "simulator_appearance": simulator_appearance,
            "capture_scene": (
                "Post-onboarding local Files and Books screen. A real local profile was created in the fresh Simulator; "
                "no media server, account, or media was configured."
                if app_in_use else
                "Fresh install; first-launch onboarding; no media server or account configured."
            ),
            "capture_method": (
                ["XCTest UI automation; XCUIScreen.main.screenshot() exported from the test result bundle"]
                if app_in_use else
                ["xcrun", "simctl", "io", device["udid"], "screenshot", "--type=png"]
            ),
            "source_tag": SOURCE_TAG,
            "source_sha": SOURCE_SHA,
            "capture_tools_sha": tool_source_sha,
            "bundle_id": BUNDLE_ID,
            "app_version": VERSION,
            "app_build": BUILD_NUMBER,
            "app_bundle_sha256": app_digest,
            "capture_sha256": sha256_file(screenshot),
            "dimensions": [width, height],
            "png_color_type": color_type,
            "png_alpha_channel": color_type in (4, 6),
            "png_encoding": png_encoding,
            "device": device,
            "build_defines": build_defines,
            "qualification": (
                "Authentic post-onboarding local Files and Books interface from the actual product app bundle on Apple Simulator. "
                "It does not claim server connection, media playback, StoreKit transactions, or physical-device qualification."
                if app_in_use else
                "Actual app UI on an Apple Simulator; does not qualify physical-device behavior, StoreKit transactions, playback, or content."
            ),
        }
        if ui_test_metadata is not None:
            capture["ui_test_runner"] = ui_test_metadata
        if signing_state is not None:
            capture["simulator_signing_provenance"] = signing_state["provenance_filename"]
            capture["simulator_signing"] = dict(signing_state)
        if simulator_runtime_diagnostics is not None:
            capture["simulator_runtime_diagnostics"] = simulator_runtime_diagnostics
        if screen_content_check is not None:
            capture["screen_content_check"] = screen_content_check
        if build_receipt is not None:
            capture["build_receipt"] = build_receipt
            receipt_platform = {"iOS": "ios", "tvOS": "tvos"}[platform]
            capture["build_configuration_evidence"] = build_configuration_evidence(
                build_receipt, receipt_platform
            )
        return capture
    finally:
        try:
            if app_in_use and platform == "iOS" and simulator_runtime_diagnostics is None:
                capture_ios_keychain_diagnostics(
                    device["udid"], output, filename, app_info(app)["executable"]
                )
        finally:
            cleanup_simulator(device)


def write_capture_manifest(output: Path, platform: str, captures: list[dict[str, Any]]) -> None:
    defines = captures[0]["build_defines"]
    if any(capture["build_defines"] != defines for capture in captures):
        raise ValueError(f"Captures in {platform} do not share build defines")
    in_use = any(
        capture["capture_scene"].startswith("Post-onboarding local Files and Books screen")
        or capture["capture_scene"].startswith("Real lifetime-access sheet")
        for capture in captures
    )
    capture_scope = {"macOS": "Mac", "tvOS": "tvOS"}.get(platform, platform)
    if platform == "iOS and iPadOS":
        device_names = {
            str(capture.get("device", {}).get("device_name", "")).lower()
            for capture in captures
        }
        scopes = []
        if any(name.startswith("iphone") for name in device_names):
            scopes.append("iPhone")
        if any(name.startswith("ipad") for name in device_names):
            scopes.append("iPad")
        capture_scope = " and ".join(scopes) if scopes else "iOS and iPadOS"
    qualification = (
        f"Authentic app-in-use screenshots from the actual v{VERSION} product app bundle(s) on {capture_scope}. "
        "They show the local Files and Books library after real onboarding and do not claim server connection, media "
        "playback, StoreKit transactions, or physical-device qualification."
        if in_use else
        "Authentic application screenshots for App Store listing review. The images show first-launch onboarding only; "
        "they do not claim media playback, billing, StoreKit, or device qualification."
    )
    manifest = {
        "schema_version": 1,
        "product": "Pleazyng",
        "source_tag": SOURCE_TAG,
        "source_sha": SOURCE_SHA,
        "capture_tools_sha": captures[0]["capture_tools_sha"],
        "product_version": VERSION,
        "product_build": BUILD_NUMBER,
        "platform": platform,
        "build_mode": "App Store IAP",
        "build_configuration": captures[0].get(
            "build_configuration",
            BUILD_CONFIGURATION["tvos" if platform == "tvOS" else "ios" if platform == "iOS and iPadOS" else "macos"],
        ),
        "build_defines": defines,
        "captures": captures,
        "qualification": qualification,
    }
    (output / "capture-provenance.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def capture_ios(args: argparse.Namespace) -> None:
    product_root = args.product_root.resolve(strict=True)
    require_source(product_root)
    tool_source_sha = require_capture_tools_sha()
    app = args.app.resolve(strict=True)
    identity = app_info(app)
    if identity["bundle_id"] != BUNDLE_ID:
        raise ValueError("Unexpected iOS Simulator application identity")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    app_digest = sha256_tree(app)
    captures = []
    captures.append(capture_simulator(
        app, output, platform="iOS", category="iphone", filename="iphone-01-onboarding.png",
        run_id=args.run_id, attempt=args.run_attempt, app_digest=app_digest,
        build_defines=IOS_MAC_STORE_DEFINES, tool_source_sha=tool_source_sha,
    ))
    captures.append(capture_simulator(
        app, output, platform="iOS", category="ipad", filename="ipad-01-onboarding.png",
        run_id=args.run_id, attempt=args.run_attempt, app_digest=app_digest,
        build_defines=IOS_MAC_STORE_DEFINES, tool_source_sha=tool_source_sha,
    ))
    write_capture_manifest(output, "iOS and iPadOS", captures)
    print(f"Captured authentic iPhone and iPad app screens in {output}")


def capture_tvos(args: argparse.Namespace) -> None:
    product_root = args.product_root.resolve(strict=True)
    require_source(product_root)
    tool_source_sha = require_capture_tools_sha()
    app = args.app.resolve(strict=True)
    identity = app_info(app)
    if identity["bundle_id"] != BUNDLE_ID:
        raise ValueError("Unexpected tvOS Simulator application identity")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    capture = capture_simulator(
        app, output, platform="tvOS", category="tvos", filename="tvos-01-onboarding.png",
        run_id=args.run_id, attempt=args.run_attempt, app_digest=sha256_tree(app),
        build_defines=TVOS_STORE_DEFINES, tool_source_sha=tool_source_sha,
    )
    write_capture_manifest(output, "tvOS", [capture])
    print(f"Captured authentic tvOS app screen in {output}")


def capture_ios_in_use(args: argparse.Namespace) -> None:
    product_root = args.product_root.resolve(strict=True)
    require_source(product_root)
    tool_source_sha = require_capture_tools_sha()
    app = args.app.resolve(strict=True)
    identity = app_info(app)
    if identity["bundle_id"] != BUNDLE_ID:
        raise ValueError("Unexpected iOS Simulator application identity")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    app_digest = sha256_tree(app)
    build_receipt_path = args.build_receipt.resolve(strict=True)
    build_receipt = json.loads(build_receipt_path.read_text(encoding="utf-8"))
    if not isinstance(build_receipt, dict):
        raise ValueError("The iOS producer build receipt is not a JSON object")
    validate_build_receipt(
        build_receipt,
        platform="ios",
        producer_repository=os.environ.get("GITHUB_REPOSITORY", ""),
        producer_run_id=str(build_receipt.get("producer_run_id", "")),
        producer_run_attempt=str(build_receipt.get("producer_run_attempt", "")),
        capture_tools_sha=str(build_receipt.get("capture_tools_sha", "")),
    )
    if build_receipt.get("app_bundle_sha256") != app_digest:
        raise ValueError("The iOS app bundle differs from its restored build receipt")
    simulator_entitlements = args.simulator_entitlements.resolve(strict=True)
    if simulator_entitlements.is_symlink() or not simulator_entitlements.is_file():
        raise ValueError("The approved iOS simulator entitlement source must be a regular file")
    extension_entitlements_source_path = args.extension_entitlements.resolve(strict=True)
    if extension_entitlements_source_path.is_symlink() or not extension_entitlements_source_path.is_file():
        raise ValueError("The product extension entitlement source must be a regular file")
    try:
        extension_entitlements_source_path.relative_to(product_root)
    except ValueError as error:
        raise ValueError("The product extension entitlement source must remain under the exact source checkout") from error
    receipt_digest = sha256_file(build_receipt_path)
    shutil.copyfile(build_receipt_path, output / "producer-build-provenance.json")
    captures = []
    signing_state = None
    selected_category = getattr(args, "device_category", "both")
    categories = (
        ("iphone", "iphone-01-files-and-books.png"),
        ("ipad", "ipad-01-files-and-books.png"),
    )
    for category, filename in categories:
        if selected_category not in ("both", category):
            continue
        capture = capture_simulator(
            app, output, platform="iOS", category=category, filename=filename,
            run_id=args.run_id, attempt=args.run_attempt, app_digest=app_digest,
            build_defines=IOS_MAC_STORE_DEFINES, tool_source_sha=tool_source_sha, app_in_use=True,
            build_receipt=build_receipt,
            build_receipt_digest=receipt_digest,
            simulator_entitlements=simulator_entitlements,
            extension_entitlements_source_path=extension_entitlements_source_path,
            signing_state=signing_state,
        )
        signing_state = capture.get("simulator_signing")
        captures.append(capture)
    write_capture_manifest(output, "iOS and iPadOS", captures)
    selected_scope = {"both": "iPhone and iPad", "iphone": "iPhone", "ipad": "iPad"}[selected_category]
    print(f"Captured post-onboarding {selected_scope} Files and Books screen(s) in {output}")


def capture_tvos_in_use(args: argparse.Namespace) -> None:
    product_root = args.product_root.resolve(strict=True)
    require_source(product_root)
    tool_source_sha = require_capture_tools_sha()
    app = args.app.resolve(strict=True)
    identity = app_info(app)
    if identity["bundle_id"] != BUNDLE_ID:
        raise ValueError("Unexpected tvOS Simulator application identity")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    app_digest = sha256_tree(app)
    build_receipt = json.loads(args.build_receipt.resolve(strict=True).read_text(encoding="utf-8"))
    validate_build_receipt(
        build_receipt,
        platform="tvos",
        producer_repository=os.environ.get("GITHUB_REPOSITORY", ""),
        producer_run_id=str(build_receipt.get("producer_run_id", "")),
        producer_run_attempt=str(build_receipt.get("producer_run_attempt", "")),
        capture_tools_sha=str(build_receipt.get("capture_tools_sha", "")),
    )
    if build_receipt.get("app_bundle_sha256") != app_digest:
        raise ValueError("The tvOS app bundle differs from its restored build receipt")
    capture = capture_simulator(
        app, output, platform="tvOS", category="tvos", filename="tvos-01-files-and-books.png",
        run_id=args.run_id, attempt=args.run_attempt, app_digest=app_digest,
        build_defines=TVOS_STORE_DEFINES, tool_source_sha=tool_source_sha, app_in_use=True,
        build_receipt=build_receipt,
    )
    write_capture_manifest(output, "tvOS", [capture])
    print(f"Captured the post-onboarding tvOS Files and Books screen in {output}")


SWIFT_WINDOW_ID = r'''
import AppKit
import CoreGraphics
import Foundation

guard let bundle = ProcessInfo.processInfo.environment["PLEAZYNG_CAPTURE_BUNDLE_ID"] else { exit(2) }
guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundle).first else { exit(3) }
let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
let windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
let matches = windows.filter {
  ($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == app.processIdentifier
    && ($0[kCGWindowLayer as String] as? NSNumber)?.intValue == 0
    && (($0[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1) > 0.9
}
guard let window = matches.first,
      let number = window[kCGWindowNumber as String] as? NSNumber else { exit(4) }
print(number.intValue)
'''


SWIFT_PAYWALL_PID_AND_WINDOW = r'''
import AppKit
import CoreGraphics
import Foundation

guard let appPath = ProcessInfo.processInfo.environment["PLEAZYNG_CAPTURE_APP_PATH"] else { exit(2) }
let expectedPath = URL(fileURLWithPath: appPath).standardizedFileURL.resolvingSymlinksInPath().path
guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: "org.snapetech.pleazyng")
  .first(where: { $0.bundleURL?.standardizedFileURL.resolvingSymlinksInPath().path == expectedPath }) else { exit(3) }
let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
let windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
let matches = windows.filter {
  ($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == app.processIdentifier
    && ($0[kCGWindowLayer as String] as? NSNumber)?.intValue == 0
    && (($0[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1) > 0.9
}
guard let window = matches.first,
      let number = window[kCGWindowNumber as String] as? NSNumber else { exit(4) }
print("\(app.processIdentifier)\t\(number.intValue)")
'''


def _paywall_applescript(pid: int, action: str, label: str | None = None) -> str:
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError("Expected the launched app's positive process ID")
    if action == "settings":
        return f'''on run
  set targetPid to {pid}
  tell application "System Events"
    set targetProcess to first application process whose unix id is targetPid
    set frontmost of targetProcess to true
    keystroke "," using {{command down}}
  end tell
end run'''
    if action not in ("click", "wait") or label is None:
        raise ValueError("Expected an accessible button action and label")
    escaped_label = label.replace("\\", "\\\\").replace('"', '\\"')
    on_success = (
        f'''        click targetButton
        set foundButton to true
        exit repeat'''
        if action == "click"
        else "        set foundButton to true\n        exit repeat"
    )
    return f'''on run
  set targetPid to {pid}
  set targetLabel to "{escaped_label}"
  tell application "System Events"
    set targetProcess to first application process whose unix id is targetPid
    set frontmost of targetProcess to true
    set foundButton to false
    repeat 60 times
      try
        set targetWindow to first window of targetProcess
        set targetButton to first button of targetWindow whose name is targetLabel
{on_success}
      on error errorMessage number errorNumber
        if errorNumber is -25211 or errorNumber is -1743 then error errorMessage number errorNumber
        if errorNumber is not -1719 and errorNumber is not -1728 then error errorMessage number errorNumber
      end try
      delay 0.5
    end repeat
    if not foundButton then error "Accessible button not found: " & targetLabel number -1728
  end tell
end run'''


def _run_paywall_applescript(pid: int, action: str, label: str | None = None) -> None:
    script = _paywall_applescript(pid, action, label)
    result = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"macOS accessibility automation failed for {action} {label or ''}: {detail}")


def _launched_app_pid_and_window(app: Path) -> tuple[int, str]:
    with tempfile.TemporaryDirectory(prefix="pleazyng-paywall-window-") as temporary:
        temporary_path = Path(temporary)
        swift_source = temporary_path / "paywall_window.swift"
        helper = temporary_path / "paywall_window"
        swift_source.write_text(SWIFT_PAYWALL_PID_AND_WINDOW, encoding="utf-8")
        run([
            "swiftc", "-framework", "AppKit", "-framework", "CoreGraphics",
            str(swift_source), "-o", str(helper),
        ], timeout=180)
        for _ in range(30):
            result = subprocess.run(
                [str(helper)],
                check=False,
                text=True,
                capture_output=True,
                env={**os.environ, "PLEAZYNG_CAPTURE_APP_PATH": str(app)},
                timeout=15,
            )
            match = re.fullmatch(r"([0-9]+)\t([0-9]+)\s*", result.stdout)
            if result.returncode == 0 and match is not None:
                return int(match.group(1)), match.group(2)
            if result.returncode not in (3, 4):
                raise RuntimeError(f"Could not bind the launched app window to its process: {result.stderr.strip()}")
            time.sleep(1)
    raise RuntimeError("The launched Mac app did not expose an on-screen window within 30 seconds")


def capture_macos_paywall(args: argparse.Namespace) -> None:
    product_root = args.product_root.resolve(strict=True)
    require_source(product_root)
    tool_source_sha = require_capture_tools_sha()
    app = args.app.resolve(strict=True)
    identity = app_info(app)
    build_receipt_path = args.build_receipt.resolve(strict=True)
    build_receipt = json.loads(build_receipt_path.read_text(encoding="utf-8"))
    app_digest = sha256_tree(app)
    if (build_receipt.get("source_sha") != SOURCE_SHA
            or build_receipt.get("product_version") != VERSION
            or build_receipt.get("product_build") != BUILD_NUMBER
            or build_receipt.get("platform") != "macOS"
            or build_receipt.get("build_configuration") != BUILD_CONFIGURATION["macos"]
            or build_receipt.get("build_mode") != "App Store IAP"
            or build_receipt.get("app_bundle_sha256") != app_digest
            or build_receipt.get("bundle_id") != identity["bundle_id"]
            or build_receipt.get("distribution_signing") != "disabled"
            or not re.fullmatch(r"[0-9]+", str(build_receipt.get("producer_run_id", "")))
            or not re.fullmatch(r"[0-9]+", str(build_receipt.get("producer_run_attempt", "")))):
        raise ValueError(f"The restored Mac app does not match its unsigned v{VERSION} App Store build receipt")

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    run(["open", "-n", "-a", str(app)], timeout=120)
    app_pid, window_id = _launched_app_pid_and_window(app)
    run(["osascript", "-e", f'tell application id "{BUNDLE_ID}" to activate'], timeout=60)
    _run_paywall_applescript(app_pid, "click", "Use files and books")
    time.sleep(3)
    _run_paywall_applescript(app_pid, "settings")
    _run_paywall_applescript(app_pid, "click", "Pleazyng lifetime access")
    _run_paywall_applescript(app_pid, "wait", "Restore lifetime purchase")
    time.sleep(SIMULATOR_SETTLE_SECONDS)

    final_pid, window_id = _launched_app_pid_and_window(app)
    if final_pid != app_pid:
        raise RuntimeError("The captured app process changed during paywall navigation")
    screenshot = output / "macos-iap-review-paywall.png"
    run(["/usr/sbin/screencapture", "-x", "-l", window_id, str(screenshot)], timeout=120)
    png_encoding = normalize_opaque_rgb_png(screenshot)
    width, height = png_dimensions(screenshot)
    capture = {
        "filename": screenshot.name,
        "platform": "macOS",
        "build_configuration": build_receipt["build_configuration"],
        "capture_scene": "Real lifetime-access sheet opened through Settings on a fresh local-files session. No server/account was configured, and Buy or Restore was not activated. StoreKit price/error state is the live app response.",
        "capture_method": ["System Events accessibility buttons and Cmd+,; /usr/sbin/screencapture -x -l <app-window-id>"],
        "capture_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "capture_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "source_tag": SOURCE_TAG,
        "source_sha": SOURCE_SHA,
        "capture_tools_sha": tool_source_sha,
        "build_receipt": build_receipt,
        "launch_process_id": app_pid,
        "captured_window_id": window_id,
        "bundle_id": identity["bundle_id"],
        "app_version": identity["version"],
        "app_build": identity["build_number"],
        "app_bundle_sha256": app_digest,
        "capture_sha256": sha256_file(screenshot),
        "dimensions": [width, height],
        "device": {
            "kind": "GitHub-hosted macOS desktop",
            "model": run(["sysctl", "-n", "hw.model"], capture=True, timeout=30),
            "architecture": run(["uname", "-m"], capture=True, timeout=30),
            "os_version": run(["sw_vers", "-productVersion"], capture=True, timeout=30),
        },
        "build_defines": IOS_MAC_STORE_DEFINES,
        "qualification": "Authentic UI from the unsigned App Store app bundle. A visible price reflects the live StoreKit response; this capture does not qualify a purchase, StoreKit sandbox, signing, or App Store processing.",
    }
    (output / "capture-provenance.json").write_text(json.dumps(capture, indent=2) + "\n", encoding="utf-8")
    print(f"Captured authentic lifetime-access sheet from the restored Mac app in {output}")


def capture_macos_in_use(args: argparse.Namespace) -> None:
    product_root = args.product_root.resolve(strict=True)
    require_source(product_root)
    tool_source_sha = require_capture_tools_sha()
    app = args.app.resolve(strict=True)
    identity = app_info(app)
    receipt_path = args.build_receipt.resolve(strict=True)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    app_digest = sha256_tree(app)
    if (receipt.get("source_sha") != SOURCE_SHA
            or receipt.get("product_version") != VERSION
            or receipt.get("product_build") != BUILD_NUMBER
            or receipt.get("platform") != "macOS"
            or receipt.get("build_configuration") != BUILD_CONFIGURATION["macos"]
            or receipt.get("build_mode") != "App Store IAP"
            or receipt.get("app_bundle_sha256") != app_digest
            or receipt.get("bundle_id") != identity["bundle_id"]
            or receipt.get("distribution_signing") != "disabled"
            or not re.fullmatch(r"[0-9]+", str(receipt.get("producer_run_id", "")))
            or not re.fullmatch(r"[0-9]+", str(receipt.get("producer_run_attempt", "")))):
        raise ValueError(f"The restored Mac app does not match its unsigned v{VERSION} App Store build receipt")
    configuration_evidence = build_configuration_evidence(receipt, "macos")
    if configuration_evidence is None:
        raise ValueError("The restored Mac app has no verifiable build-configuration evidence")

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    run(["open", "-n", "-a", str(app)], timeout=120)
    app_pid, _ = _launched_app_pid_and_window(app)
    run(["osascript", "-e", f'tell application id "{BUNDLE_ID}" to activate'], timeout=60)
    _run_paywall_applescript(app_pid, "click", "Use files and books")
    _run_paywall_applescript(app_pid, "wait", "Import files")
    time.sleep(3)

    device = {
        "kind": "GitHub-hosted macOS desktop",
        "model": run(["sysctl", "-n", "hw.model"], capture=True, timeout=30),
        "architecture": run(["uname", "-m"], capture=True, timeout=30),
        "os_version": run(["sw_vers", "-productVersion"], capture=True, timeout=30),
    }
    captures = []
    scenes = [
        (
            "macos-01-files-and-books.png",
            "Post-onboarding local Files and Books screen. A real local profile was created; no media server, account, or media was configured.",
        ),
    ]
    for filename, scene in scenes:
        current_pid, window_id = _launched_app_pid_and_window(app)
        if current_pid != app_pid:
            raise RuntimeError("The captured app process changed during local Files and Books navigation")
        screenshot = output / filename
        run(["/usr/sbin/screencapture", "-x", "-l", window_id, str(screenshot)], timeout=120)
        width, height = png_dimensions(screenshot)
        screen_content_check = require_visible_screen_content(screenshot)
        captures.append({
            "filename": filename,
            "platform": "macOS",
            "build_configuration": receipt["build_configuration"],
            "capture_scene": scene,
            "capture_method": ["System Events Accessibility; /usr/sbin/screencapture -x -l <app-window-id>"],
            "capture_run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "capture_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            "source_tag": SOURCE_TAG,
            "source_sha": SOURCE_SHA,
            "capture_tools_sha": tool_source_sha,
            "build_receipt": receipt,
            "build_configuration_evidence": configuration_evidence,
            "launch_process_id": app_pid,
            "captured_window_id": window_id,
            "bundle_id": identity["bundle_id"],
            "app_version": identity["version"],
            "app_build": identity["build_number"],
            "app_bundle_sha256": app_digest,
            "capture_sha256": sha256_file(screenshot),
            "dimensions": [width, height],
            "screen_content_check": screen_content_check,
            "device": device,
            "build_defines": IOS_MAC_STORE_DEFINES,
            "qualification": "Authentic post-onboarding local Files and Books interface from the actual App Store app bundle; no content or server is represented.",
        })

    _run_paywall_applescript(app_pid, "settings")
    _run_paywall_applescript(app_pid, "click", "Pleazyng lifetime access")
    _run_paywall_applescript(app_pid, "wait", "Restore lifetime purchase")
    time.sleep(SIMULATOR_SETTLE_SECONDS)
    filename = "macos-02-iap-review-paywall.png"
    current_pid, window_id = _launched_app_pid_and_window(app)
    if current_pid != app_pid:
        raise RuntimeError("The captured app process changed during paywall navigation")
    screenshot = output / filename
    run(["/usr/sbin/screencapture", "-x", "-l", window_id, str(screenshot)], timeout=120)
    width, height = png_dimensions(screenshot)
    screen_content_check = require_visible_screen_content(screenshot)
    captures.append({
        "filename": filename,
        "platform": "macOS",
        "build_configuration": receipt["build_configuration"],
        "capture_scene": "Real lifetime-access sheet opened through Settings after a local Files and Books session. No server/account was configured, and Buy or Restore was not activated. StoreKit price/error state is the live app response.",
        "capture_method": ["System Events accessibility buttons and Cmd+,; /usr/sbin/screencapture -x -l <app-window-id>"],
        "capture_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "capture_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "source_tag": SOURCE_TAG,
        "source_sha": SOURCE_SHA,
        "capture_tools_sha": tool_source_sha,
        "build_receipt": receipt,
        "build_configuration_evidence": configuration_evidence,
        "launch_process_id": app_pid,
        "captured_window_id": window_id,
        "bundle_id": identity["bundle_id"],
        "app_version": identity["version"],
        "app_build": identity["build_number"],
        "app_bundle_sha256": app_digest,
        "capture_sha256": sha256_file(screenshot),
        "dimensions": [width, height],
        "screen_content_check": screen_content_check,
        "device": device,
        "build_defines": IOS_MAC_STORE_DEFINES,
        "qualification": "Authentic UI from the unsigned App Store app bundle. A visible price/error reflects the live StoreKit response; no purchase or restore action was activated.",
    })
    write_capture_manifest(output, "macOS", captures)
    print(f"Captured authentic Mac Files and Books and lifetime-access screens in {output}")


def capture_macos_listing_window(args: argparse.Namespace) -> None:
    product_root = args.product_root.resolve(strict=True)
    require_source(product_root)
    tool_source_sha = require_capture_tools_sha()
    app = args.app.resolve(strict=True)
    identity = app_info(app)
    receipt_path = args.build_receipt.resolve(strict=True)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    app_digest = sha256_tree(app)
    if (receipt.get("source_sha") != SOURCE_SHA
            or receipt.get("product_version") != VERSION
            or receipt.get("product_build") != BUILD_NUMBER
            or receipt.get("platform") != "macOS"
            or receipt.get("build_configuration") != BUILD_CONFIGURATION["macos"]
            or receipt.get("build_mode") != "App Store IAP"
            or receipt.get("app_bundle_sha256") != app_digest
            or receipt.get("bundle_id") != identity["bundle_id"]
            or receipt.get("distribution_signing") != "disabled"
            or not re.fullmatch(r"[0-9]+", str(receipt.get("producer_run_id", "")))
            or not re.fullmatch(r"[0-9]+", str(receipt.get("producer_run_attempt", "")))):
        raise ValueError(f"The restored Mac app does not match its unsigned v{VERSION} App Store build receipt")
    configuration_evidence = build_configuration_evidence(receipt, "macos")
    if configuration_evidence is None:
        raise ValueError("The restored Mac app has no verifiable build-configuration evidence")

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    unexpected = [path.name for path in output.iterdir() if path.name != "macos-display-capability.json"]
    if unexpected:
        raise FileExistsError(f"Refusing to overwrite unexpected Mac capture output files: {unexpected}")
    screenshot, native_capture = run_macos_listing_window_capture(app, output)
    width, height = png_dimensions(screenshot)
    if (width, height) not in ((1280, 800), (1440, 900), (2560, 1600), (2880, 1800)):
        raise ValueError(f"Mac App Store screenshot has unsupported dimensions {width}x{height}: {screenshot}")
    color_type = png_color_type(screenshot)
    if color_type != 2:
        raise ValueError(f"Mac App Store screenshots must be opaque RGB PNGs; received color type {color_type}: {screenshot}")
    capture = {
        "filename": screenshot.name,
        "platform": "macOS",
        "build_configuration": receipt["build_configuration"],
        "capture_scene": (
            "Post-onboarding local Files and Books screen. A real local profile was created; "
            "no media server, account, or media was configured."
        ),
        "capture_method": [
            "Vision OCR located the visible Start fresh and Use files and books actions; CoreGraphics CGEvent clicks navigated the real app; "
            "the AppKit saved-frame preference requested a real 1280x800-point window before launch and CoreGraphics verified its actual bounds; "
            "the complete native app window was captured by window ID with "
            "/usr/sbin/screencapture -x -o -l <app-window-id>"
        ],
        "capture_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "capture_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "source_tag": SOURCE_TAG,
        "source_sha": SOURCE_SHA,
        "capture_tools_sha": tool_source_sha,
        "build_receipt": receipt,
        "build_configuration_evidence": configuration_evidence,
        "bundle_id": identity["bundle_id"],
        "app_version": identity["version"],
        "app_build": identity["build_number"],
        "app_bundle_sha256": app_digest,
        "capture_sha256": sha256_file(screenshot),
        "dimensions": [width, height],
        "png_color_type": color_type,
        "png_alpha_channel": False,
        "png_encoding": native_capture["screenshot_png_encoding"],
        "screen_content_check": require_visible_screen_content(screenshot),
        "device": {
            "kind": "GitHub-hosted macOS desktop",
            "model": run(["sysctl", "-n", "hw.model"], capture=True, timeout=30),
            "architecture": run(["uname", "-m"], capture=True, timeout=30),
            "os_version": run(["sw_vers", "-productVersion"], capture=True, timeout=30),
        },
        "build_defines": IOS_MAC_STORE_DEFINES,
        "native_window_capture": native_capture,
        "qualification": (
            "Authentic local Files and Books interface from the exact unsigned App Store app bundle, captured from its native app window. "
            "No content, server connection, playback, StoreKit transaction, or purchase is represented."
        ),
    }
    write_capture_manifest(output, "macOS", [capture])
    print(f"Captured authentic Mac Files and Books app window in {output}")


MACOS_DIAGNOSTIC_WINDOWS_SWIFT = r'''
import CoreGraphics
import Foundation

guard CommandLine.arguments.count == 2, let targetPID = Int32(CommandLine.arguments[1]) else { exit(2) }
let options: CGWindowListOption = [.optionAll]
let windows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
let matches = windows.filter {
  ($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == targetPID
}
let rows = matches.map { window -> [String: Any] in
  let bounds = window[kCGWindowBounds as String] as? [String: CGFloat] ?? [:]
  return [
    "window_id": window[kCGWindowNumber as String] ?? 0,
    "owner_pid": window[kCGWindowOwnerPID as String] ?? 0,
    "owner_name": window[kCGWindowOwnerName as String] ?? "",
    "title": window[kCGWindowName as String] ?? "",
    "layer": window[kCGWindowLayer as String] ?? 0,
    "alpha": window[kCGWindowAlpha as String] ?? 0,
    "bounds": bounds,
    "on_screen": window[kCGWindowIsOnscreen as String] ?? false,
  ]
}
let display = CGDisplayBounds(CGMainDisplayID())
let result: [String: Any] = [
  "target_pid": targetPID,
  "display_bounds": ["x": display.origin.x, "y": display.origin.y, "width": display.width, "height": display.height],
  "windows": rows,
]
let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
print(String(decoding: data, as: UTF8.self))
'''


MACOS_DIAGNOSTIC_ACCESSIBILITY_SWIFT = r'''
import ApplicationServices
import Foundation

guard CommandLine.arguments.count == 2, let targetPID = Int32(CommandLine.arguments[1]) else { exit(2) }
let app = AXUIElementCreateApplication(targetPID)
let timeoutStatus = AXUIElementSetMessagingTimeout(app, 2.0)
var remaining = 300

func read(_ element: AXUIElement, _ attribute: CFString) -> (Any?, Int32) {
  var value: CFTypeRef?
  let status = AXUIElementCopyAttributeValue(element, attribute, &value)
  return (value, status.rawValue)
}

func label(_ element: AXUIElement, _ attribute: CFString) -> [String: Any] {
  let (value, status) = read(element, attribute)
  if status != AXError.success.rawValue { return ["status": status] }
  if let text = value as? String { return ["status": status, "value": text] }
  return ["status": status]
}

func node(_ element: AXUIElement, _ depth: Int) -> [String: Any] {
  remaining -= 1
  var result: [String: Any] = [
    "role": label(element, kAXRoleAttribute as CFString),
    "title": label(element, kAXTitleAttribute as CFString),
    "description": label(element, kAXDescriptionAttribute as CFString),
    "help": label(element, kAXHelpAttribute as CFString),
  ]
  guard depth < 6, remaining > 0 else {
    result["children_truncated"] = true
    return result
  }
  let (rawChildren, status) = read(element, kAXChildrenAttribute as CFString)
  result["children_status"] = status
  if status == AXError.success.rawValue, let children = rawChildren as? [AXUIElement] {
    result["children"] = children.prefix(100).map { node($0, depth + 1) }
  }
  return result
}

let output: [String: Any] = [
  "target_pid": targetPID,
  "accessibility_trusted": AXIsProcessTrusted(),
  "messaging_timeout_status": timeoutStatus.rawValue,
  "tree": node(app, 0),
]
let data = try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys])
print(String(decoding: data, as: UTF8.self))
'''


MACOS_DIAGNOSTIC_METAL_SWIFT = r'''
import Foundation
import Metal

var output: [String: Any] = ["default_device_available": false]
if let device = MTLCreateSystemDefaultDevice() {
  output["default_device_available"] = true
  output["device_name"] = device.name
  output["low_power"] = device.isLowPower
  output["headless"] = device.isHeadless
  output["registry_id"] = String(device.registryID, radix: 16)
}
let data = try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys])
print(String(decoding: data, as: UTF8.self))
'''


def _compile_macos_diagnostic_helper(
    output: Path,
    name: str,
    source_text: str,
    frameworks: tuple[str, ...] = (),
) -> tuple[Path | None, dict[str, Any]]:
    source = output / f"{name}.swift"
    executable = output / name
    source.write_text(source_text, encoding="utf-8")
    command = ["/usr/bin/swiftc"]
    for framework in frameworks:
        command.extend(("-framework", framework))
    command.extend((str(source), "-o", str(executable)))
    try:
        result = subprocess.run(command, check=False, text=True, capture_output=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as error:
        details = str(error)
        (output / f"{name}.compile.log").write_text(details + "\n", encoding="utf-8")
        return None, {"command": command, "error": details}
    compile_log = (result.stdout or "") + (result.stderr or "")
    (output / f"{name}.compile.log").write_text(compile_log, encoding="utf-8")
    state = {"command": command, "exit_code": result.returncode, "log_path": f"{name}.compile.log"}
    if result.returncode != 0 or not executable.is_file():
        state["error"] = "Swift diagnostic helper did not compile"
        return None, state
    return executable, state


def _run_macos_diagnostic_probe(executable: Path | None, output: Path, name: str, *arguments: str) -> dict[str, Any]:
    if executable is None:
        return {"error": "diagnostic helper was unavailable"}
    try:
        result = subprocess.run(
            [str(executable), *arguments], check=False, text=True, capture_output=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        details = str(error)
        (output / f"{name}.stdout.txt").write_text("", encoding="utf-8")
        (output / f"{name}.stderr.txt").write_text(details + "\n", encoding="utf-8")
        return {"error": details}
    stdout_path = output / f"{name}.stdout.txt"
    stderr_path = output / f"{name}.stderr.txt"
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    state: dict[str, Any] = {
        "exit_code": result.returncode,
        "stdout_path": stdout_path.name,
        "stderr_path": stderr_path.name,
    }
    if result.returncode == 0:
        try:
            state["result"] = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            state["parse_error"] = str(error)
    else:
        state["error"] = f"probe exited with status {result.returncode}"
    return state


def diagnose_macos_in_use(args: argparse.Namespace) -> None:
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    diagnostics: dict[str, Any] = {
        "schema_version": 1,
        "purpose": "bounded diagnosis of retained macOS app launch, rendering, Metal availability, and Accessibility state",
        "source_tag": SOURCE_TAG,
        "source_sha": SOURCE_SHA,
        "product_version": VERSION,
        "product_build": BUILD_NUMBER,
        "capture_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "capture_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "capture_tools_sha": os.environ.get("PLEAZYNG_CAPTURE_TOOLS_SHA", ""),
        "errors": [],
        "timeline": [],
        "windowserver_processes": [],
    }
    process: subprocess.Popen[bytes] | None = None
    stdout_file = None
    stderr_file = None
    app: Path | None = None
    executable: Path | None = None
    windows_helper: Path | None = None
    accessibility_helper: Path | None = None
    metal_helper: Path | None = None
    app_pid: int | None = None
    window_state: dict[str, Any] | None = None
    screenshot = output / "macos-diagnostic-screen.png"
    stdout_path = output / "app.stdout.log"
    stderr_path = output / "app.stderr.log"

    try:
        product_root = args.product_root.resolve(strict=True)
        require_source(product_root)
        app = args.app.resolve(strict=True)
        identity = app_info(app)
        executable = app / "Contents" / "MacOS" / identity["executable"]
        if not executable.is_file() or not executable.stat().st_mode & 0o111:
            raise ValueError(f"CFBundleExecutable is missing or not executable: {executable}")
        receipt_path = args.build_receipt.resolve(strict=True)
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes)
        app_digest = sha256_tree(app)
        if (receipt.get("source_sha") != SOURCE_SHA
                or receipt.get("product_version") != VERSION
                or receipt.get("product_build") != BUILD_NUMBER
                or receipt.get("platform") != "macOS"
                or receipt.get("build_configuration") != BUILD_CONFIGURATION["macos"]
                or receipt.get("build_mode") != "App Store IAP"
                or receipt.get("app_bundle_sha256") != app_digest
                or receipt.get("bundle_id") != identity["bundle_id"]
                or receipt.get("distribution_signing") != "disabled"):
            raise ValueError(f"The retained Mac app differs from its unsigned v{VERSION} App Store build receipt")
        configuration_evidence = build_configuration_evidence(receipt, "macos")
        if configuration_evidence is None:
            raise ValueError("The retained Mac app has no verifiable build-configuration evidence")
        diagnostics.update({
            "bundle_id": identity["bundle_id"],
            "app_version": identity["version"],
            "app_build": identity["build_number"],
            "app_bundle_sha256": app_digest,
            "build_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "producer_run_id": receipt.get("producer_run_id"),
            "producer_run_attempt": receipt.get("producer_run_attempt"),
            "build_configuration_evidence": configuration_evidence,
            "cf_bundle_executable": identity["executable"],
            "executable_path": str(executable),
            "executable_sha256": sha256_file(executable),
            "launch_mode": "direct launch of the retained CFBundleExecutable to capture stdout/stderr; product bundle unchanged",
            "device": {
                "kind": "GitHub-hosted macOS desktop",
                "model": run(["sysctl", "-n", "hw.model"], capture=True, timeout=30),
                "architecture": run(["uname", "-m"], capture=True, timeout=30),
                "os_version": run(["sw_vers", "-productVersion"], capture=True, timeout=30),
            },
        })

        windows_helper, diagnostics["windows_helper_compile"] = _compile_macos_diagnostic_helper(
            output, "window_probe", MACOS_DIAGNOSTIC_WINDOWS_SWIFT, ("CoreGraphics",)
        )
        accessibility_helper, diagnostics["accessibility_helper_compile"] = _compile_macos_diagnostic_helper(
            output, "accessibility_probe", MACOS_DIAGNOSTIC_ACCESSIBILITY_SWIFT, ("ApplicationServices",)
        )
        metal_helper, diagnostics["metal_helper_compile"] = _compile_macos_diagnostic_helper(
            output, "metal_probe", MACOS_DIAGNOSTIC_METAL_SWIFT, ("Metal",)
        )
        diagnostics["metal"] = _run_macos_diagnostic_probe(metal_helper, output, "metal_probe")

        app_env = dict(os.environ)
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "APPLE_API_PRIVATE_KEY"):
            app_env.pop(key, None)
        stdout_file = stdout_path.open("wb")
        stderr_file = stderr_path.open("wb")
        try:
            process = subprocess.Popen(
                [str(executable)],
                cwd=str(executable.parent),
                env=app_env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            app_pid = process.pid
            diagnostics["launch_pid"] = app_pid
        except OSError as error:
            diagnostics["errors"].append(f"CFBundleExecutable launch failed: {error}")

        if app_pid is not None:
            start = time.monotonic()
            for elapsed_target in (1, 5, 12, 22):
                delay = elapsed_target - (time.monotonic() - start)
                if delay > 0:
                    time.sleep(delay)
                poll = process.poll() if process is not None else None
                ps = subprocess.run(
                    ["/bin/ps", "-p", str(app_pid), "-o", "pid=,ppid=,state=,etime=,%cpu=,%mem=,command="],
                    check=False, text=True, capture_output=True, timeout=10,
                )
                windows_state = _run_macos_diagnostic_probe(
                    windows_helper, output, f"windows_t{elapsed_target}", str(app_pid),
                )
                diagnostics["timeline"].append({
                    "elapsed_seconds": round(time.monotonic() - start, 2),
                    "process_exit_code": poll,
                    "process_snapshot": ps.stdout.strip(),
                    "process_snapshot_error": ps.stderr.strip(),
                    "windows": windows_state,
                })
                if windows_state.get("result"):
                    window_state = windows_state["result"]
                if elapsed_target == 5:
                    diagnostics["accessibility"] = _run_macos_diagnostic_probe(
                        accessibility_helper, output, "accessibility_probe", str(app_pid),
                    )
        else:
            diagnostics["accessibility"] = {"not_run": "the retained CFBundleExecutable did not start"}

        windowserver = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,state=,etime=,command="],
            check=False, text=True, capture_output=True, timeout=10,
        )
        diagnostics["windowserver_processes"] = [
            line.strip() for line in windowserver.stdout.splitlines()
            if "WindowServer" in line
        ]
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        diagnostics["errors"].append(f"{error.__class__.__name__}: {error}")
    finally:
        if app_pid is not None and windows_helper is not None:
            try:
                final_windows = _run_macos_diagnostic_probe(
                    windows_helper, output, "windows_final", str(app_pid),
                )
                diagnostics["final_windows"] = final_windows
                if final_windows.get("result"):
                    window_state = final_windows["result"]
            except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError,
                    subprocess.TimeoutExpired) as error:
                diagnostics["errors"].append(f"Final app window probe failed: {error}")

        try:
            screenshot_command = ["/usr/sbin/screencapture", "-x"]
            app_windows = (window_state or {}).get("windows", [])
            app_window = next((
                window for window in app_windows
                if window.get("on_screen") is True
                and isinstance(window.get("window_id"), int)
                and window.get("layer") == 0
                and window.get("alpha", 0) > 0.9
            ), None)
            if app_window is not None:
                screenshot_command.extend(("-l", str(app_window["window_id"])))
                diagnostics["screenshot_scope"] = "retained app process window"
            else:
                diagnostics["screenshot_scope"] = "full hosted macOS display; no matching app window was reported"
            screenshot_command.append(str(screenshot))
            screenshot_result = subprocess.run(
                screenshot_command, check=False, text=True, capture_output=True, timeout=30,
            )
            diagnostics["screenshot_command"] = screenshot_command
            diagnostics["screenshot_exit_code"] = screenshot_result.returncode
            diagnostics["screenshot_stderr"] = screenshot_result.stderr.strip()
            if screenshot.is_file():
                diagnostics["screenshot_path"] = screenshot.name
                diagnostics["screenshot_sha256"] = sha256_file(screenshot)
                try:
                    diagnostics["screenshot_dimensions"] = list(png_dimensions(screenshot))
                    diagnostics["screen_content_check"] = require_visible_screen_content(screenshot)
                    diagnostics["screen_content_status"] = "visible"
                except (OSError, ValueError) as error:
                    diagnostics["screen_content_status"] = "invalid_or_blank"
                    diagnostics["screen_content_error"] = str(error)
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            diagnostics["errors"].append(f"Screenshot evidence collection failed: {error.__class__.__name__}: {error}")

        if app_pid is not None:
            log_path = output / "owned-process-unified-log.txt"
            try:
                log_result = subprocess.run(
                    ["/usr/bin/log", "show", "--last", "2m", "--style", "compact",
                     "--predicate", f"processIdentifier == {app_pid}"],
                    check=False, text=True, capture_output=True, timeout=30,
                )
                log_path.write_text(log_result.stdout + log_result.stderr, encoding="utf-8")
                diagnostics["owned_process_unified_log"] = {
                    "path": log_path.name,
                    "exit_code": log_result.returncode,
                    "bytes": log_path.stat().st_size,
                }
            except (OSError, subprocess.TimeoutExpired) as error:
                log_path.write_text(f"Owned-process unified log query failed: {error}\n", encoding="utf-8")
                diagnostics["owned_process_unified_log"] = {"path": log_path.name, "error": str(error)}

        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as error:
                if process.poll() is None:
                    diagnostics["errors"].append(f"App graceful termination failed: {error}")
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired) as kill_error:
                        diagnostics["errors"].append(f"App termination fallback failed: {kill_error}")
            diagnostics["process_exit_code_after_cleanup"] = process.poll()
        try:
            if stdout_file is not None:
                stdout_file.close()
            if stderr_file is not None:
                stderr_file.close()
            if stdout_path.is_file():
                diagnostics["stdout_sha256"] = sha256_file(stdout_path)
                diagnostics["stdout_bytes"] = stdout_path.stat().st_size
            if stderr_path.is_file():
                diagnostics["stderr_sha256"] = sha256_file(stderr_path)
                diagnostics["stderr_bytes"] = stderr_path.stat().st_size
        except OSError as error:
            diagnostics["errors"].append(f"Could not finalize executable output logs: {error}")
        finally:
            summary = output / "diagnosis.json"
            summary.write_text(json.dumps(diagnostics, indent=2) + "\n", encoding="utf-8")

    print(f"Retained bounded macOS app launch/render/Accessibility diagnostics in {output}")


def capture_macos(args: argparse.Namespace) -> None:
    product_root = args.product_root.resolve(strict=True)
    require_source(product_root)
    tool_source_sha = require_capture_tools_sha()
    app = args.app.resolve(strict=True)
    identity = app_info(app)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    run(["open", "-n", "-a", str(app)], timeout=120)
    run(["osascript", "-e", f'tell application id "{BUNDLE_ID}" to activate'], timeout=60)
    window_id = ""
    time.sleep(SIMULATOR_SETTLE_SECONDS)
    with tempfile.TemporaryDirectory(prefix="pleazyng-window-capture-") as temporary:
        temporary_path = Path(temporary)
        swift_source = temporary_path / "window_id.swift"
        helper = temporary_path / "window_id"
        swift_source.write_text(SWIFT_WINDOW_ID, encoding="utf-8")
        run([
            "swiftc", "-framework", "AppKit", "-framework", "CoreGraphics",
            str(swift_source), "-o", str(helper),
        ], timeout=180)
        for _ in range(24):
            result = subprocess.run(
                [str(helper)],
                check=False,
                text=True,
                capture_output=True,
                env={**os.environ, "PLEAZYNG_CAPTURE_BUNDLE_ID": BUNDLE_ID},
                timeout=15,
            )
            if result.returncode == 0 and re.fullmatch(r"\d+", result.stdout.strip()):
                window_id = result.stdout.strip()
                break
            time.sleep(2)
    if not window_id:
        raise RuntimeError("The launched Mac app did not expose an on-screen window to capture")
    screenshot = output / "macos-01-onboarding.png"
    run(["/usr/sbin/screencapture", "-x", "-l", window_id, str(screenshot)], timeout=120)
    width, height = png_dimensions(screenshot)
    capture = {
        "filename": screenshot.name,
        "platform": "macOS",
        "build_configuration": BUILD_CONFIGURATION["macos"],
        "capture_scene": "Fresh launch; first-launch onboarding; no media server or account configured.",
        "capture_method": ["/usr/sbin/screencapture", "-x", "-l", window_id],
        "source_tag": SOURCE_TAG,
        "source_sha": SOURCE_SHA,
        "capture_tools_sha": tool_source_sha,
        "bundle_id": identity["bundle_id"],
        "app_version": identity["version"],
        "app_build": identity["build_number"],
        "app_bundle_sha256": sha256_tree(app),
        "capture_sha256": sha256_file(screenshot),
        "dimensions": [width, height],
        "device": {
            "kind": "GitHub-hosted macOS desktop",
            "model": run(["sysctl", "-n", "hw.model"], capture=True, timeout=30),
            "architecture": run(["uname", "-m"], capture=True, timeout=30),
            "os_version": run(["sw_vers", "-productVersion"], capture=True, timeout=30),
        },
        "build_defines": IOS_MAC_STORE_DEFINES,
        "qualification": "Actual app UI captured from the unsigned Mac App Store candidate on hosted macOS; does not qualify sandbox runtime, StoreKit transactions, playback, or content.",
    }
    write_capture_manifest(output, "macOS", [capture])
    print(f"Captured authentic macOS app screen in {output}")


def load_platform_capture(
    directory: Path,
    expected_platform: str,
    expected_defines: list[str],
    legacy_tool_source_sha: str | None,
) -> tuple[dict[str, Any], list[tuple[Path, dict[str, Any]]]]:
    manifest_path = directory / "capture-provenance.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_tool_sha = manifest.get("capture_tools_sha") or legacy_tool_source_sha
    if re.fullmatch(r"[0-9a-f]{40}", manifest_tool_sha or "") is None:
        raise ValueError(f"Capture tools source SHA is missing or invalid for {expected_platform}")
    if (manifest.get("schema_version") != 1
            or manifest.get("source_tag") != SOURCE_TAG
            or manifest.get("source_sha") != SOURCE_SHA
            or manifest.get("product_version") != VERSION
            or manifest.get("product_build") != BUILD_NUMBER
            or manifest.get("platform") != expected_platform
            or manifest.get("build_mode") != "App Store IAP"
            or manifest.get("build_defines") != expected_defines):
        raise ValueError(f"Capture receipt does not describe the expected {expected_platform} build")
    checked = []
    for capture in manifest.get("captures", []):
        filename = capture.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".png"):
            raise ValueError("Capture receipt contains an invalid filename")
        image = directory / filename
        dimensions = list(png_dimensions(image))
        capture_tool_sha = capture.get("capture_tools_sha") or manifest_tool_sha
        if capture_tool_sha != manifest_tool_sha:
            raise ValueError(f"Capture tools source SHA differs within {expected_platform}: {image}")
        capture["capture_tools_sha"] = capture_tool_sha
        expected_capture_platform = {
            "iOS and iPadOS": "iOS",
            "tvOS": "tvOS",
            "macOS": "macOS",
        }[expected_platform]
        if (capture.get("platform") != expected_capture_platform
                or capture.get("bundle_id") != BUNDLE_ID
                or capture.get("app_version") != VERSION
                or capture.get("app_build") != BUILD_NUMBER
                or not isinstance(capture.get("device"), dict)
                or not re.fullmatch(r"[0-9a-f]{64}", capture.get("app_bundle_sha256", ""))):
            raise ValueError(f"Capture provenance does not describe a v{VERSION} {expected_capture_platform} app: {image}")
        if (capture.get("source_sha") != SOURCE_SHA or capture.get("source_tag") != SOURCE_TAG
                or capture.get("capture_sha256") != sha256_file(image)
                or capture.get("dimensions") != dimensions
                or capture.get("build_defines") != expected_defines
                or capture.get("qualification", "") == ""):
            raise ValueError(f"Capture verification failed: {image}")
        checked.append((image, capture))
    if not checked:
        raise ValueError(f"No screenshots were recorded for {expected_platform}")
    return manifest, checked


def package_captures(args: argparse.Namespace) -> None:
    source_dirs = {
        "iOS and iPadOS": args.ios_dir.resolve(strict=True),
        "tvOS": args.tvos_dir.resolve(strict=True),
        "macOS": args.macos_dir.resolve(strict=True),
    }
    expected_defines = {
        "iOS and iPadOS": IOS_MAC_STORE_DEFINES,
        "tvOS": TVOS_STORE_DEFINES,
        "macOS": IOS_MAC_STORE_DEFINES,
    }
    images: list[tuple[Path, dict[str, Any]]] = []
    legacy_tool_source_sha = args.legacy_tool_source_sha
    if legacy_tool_source_sha and re.fullmatch(r"[0-9a-f]{40}", legacy_tool_source_sha) is None:
        raise ValueError("Legacy capture tools source SHA must be a full lowercase Git commit")
    for platform, directory in source_dirs.items():
        manifest, captures = load_platform_capture(
            directory, platform, expected_defines[platform], legacy_tool_source_sha,
        )
        if len(captures) != (2 if platform == "iOS and iPadOS" else 1):
            raise ValueError(f"Unexpected screenshot count for {platform}: {len(captures)}")
        images.extend(captures)

    names = {capture["filename"] for _, capture in images}
    required = {"iphone-01-onboarding.png", "ipad-01-onboarding.png", "tvos-01-onboarding.png", "macos-01-onboarding.png"}
    if names != required:
        raise ValueError(f"Expected the four platform screenshots, found {sorted(names)}")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    final_captures: list[dict[str, Any]] = []
    for image, capture in images:
        name = capture["filename"]
        destination = output / name
        shutil.copyfile(image, destination)
        final_captures.append(capture)
    final_manifest = {
        "schema_version": 1,
        "product": "Pleazyng",
        "source_tag": SOURCE_TAG,
        "source_sha": SOURCE_SHA,
        "product_version": VERSION,
        "product_build": BUILD_NUMBER,
        "build_mode": "App Store IAP",
        "build_configuration_by_platform": BUILD_CONFIGURATION,
        "packaging_tool_source_sha": require_capture_tools_sha(),
        "capture_tool_source_sha_by_platform": {
            "ios": next(item["capture_tools_sha"] for item in final_captures if item["filename"].startswith("iphone-")),
            "tvos": next(item["capture_tools_sha"] for item in final_captures if item["filename"].startswith("tvos-")),
            "macos": next(item["capture_tools_sha"] for item in final_captures if item["filename"].startswith("macos-")),
        },
        "build_defines_by_platform": {
            "ios": IOS_MAC_STORE_DEFINES,
            "tvos": TVOS_STORE_DEFINES,
            "macos": IOS_MAC_STORE_DEFINES,
        },
        "captures": final_captures,
        "qualification": f"Four authentic app UI captures from the v{VERSION} source on iPhone, iPad, tvOS Simulator, and hosted macOS. They show first-launch onboarding only; no media is included and playback, StoreKit, physical-device behavior, App Store processing, and sandbox runtime are not qualified.",
    }
    manifest_path = output / "capture-provenance.json"
    manifest_path.write_text(json.dumps(final_manifest, indent=2) + "\n", encoding="utf-8")
    (output / "README.txt").write_text(
        f"Pleazyng App Store screenshots for v{VERSION}\n\n"
        f"Source commit: {SOURCE_SHA}\n"
        "These are unaltered screenshots of the running app. iPhone, iPad, and tvOS "
        "captures come from Apple Simulators; macOS comes from the hosted Mac app. "
        "Every image shows first-launch onboarding without a server or account. "
        "The set does not demonstrate playback, StoreKit, device qualification, "
        "sandbox runtime, App Store processing, or media content.\n"
        "iOS/tvOS screenshots use Debug Simulator builds with App Store IAP feature defines; macOS uses an unsigned Release Archive.\n"
        "See capture-provenance.json for product/tool source revisions, build configuration, simulator, device, dimension, and hash evidence.\n",
        encoding="utf-8",
    )
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for path in sorted(output.iterdir()):
            target.write(path, path.name)
    print(f"Packaged verified screenshots: {archive}")


def package_app_in_use_captures(args: argparse.Namespace) -> None:
    source_dirs = {
        "iOS and iPadOS": args.ios_dir.resolve(strict=True),
        "tvOS": args.tvos_dir.resolve(strict=True),
        "macOS": args.macos_dir.resolve(strict=True),
    }
    expected_defines = {
        "iOS and iPadOS": IOS_MAC_STORE_DEFINES,
        "tvOS": TVOS_STORE_DEFINES,
        "macOS": IOS_MAC_STORE_DEFINES,
    }
    expected_counts = {"iOS and iPadOS": 2, "tvOS": 1, "macOS": 2}
    images: list[tuple[Path, dict[str, Any]]] = []
    for platform, directory in source_dirs.items():
        _, captures = load_platform_capture(directory, platform, expected_defines[platform], None)
        if len(captures) != expected_counts[platform]:
            raise ValueError(f"Unexpected screenshot count for {platform}: {len(captures)}")
        for image, capture in captures:
            content_check = require_visible_screen_content(image)
            if capture.get("screen_content_check") is None:
                capture["screen_content_check"] = content_check
            scene = capture.get("capture_scene", "")
            if not (scene.startswith("Post-onboarding local Files and Books screen")
                    or scene.startswith("Real lifetime-access sheet")):
                raise ValueError(f"Refusing to package a login/onboarding-only image: {image}")
            receipt = capture.get("build_receipt")
            if not isinstance(receipt, dict):
                raise ValueError(f"Screenshot is missing the validated product build receipt: {image}")
            platform_key = {"iOS and iPadOS": "ios", "tvOS": "tvos", "macOS": "macos"}[platform]
            validate_build_receipt(
                receipt,
                platform=platform_key,
                producer_repository=str(receipt.get("producer_repository", "")),
                producer_run_id=str(receipt.get("producer_run_id", "")),
                producer_run_attempt=str(receipt.get("producer_run_attempt", "")),
                capture_tools_sha=str(receipt.get("capture_tools_sha", "")),
            )
            if receipt.get("app_bundle_sha256") != capture.get("app_bundle_sha256"):
                raise ValueError(f"Screenshot product hash differs from its validated build receipt: {image}")
            expected_configuration_evidence = build_configuration_evidence(receipt, platform_key)
            if (expected_configuration_evidence is None
                    or capture.get("build_configuration_evidence") != expected_configuration_evidence):
                raise ValueError(
                    f"Screenshot is missing the verified producer build-configuration evidence: {image}"
                )
            if platform in ("iOS and iPadOS", "tvOS"):
                ui_runner = capture.get("ui_test_runner")
                if (not isinstance(ui_runner, dict)
                        or ui_runner.get("project_targets") != ["PleazyngInUseCaptureTests"]
                        or ui_runner.get("product_target_compiled") is not False
                        or re.fullmatch(r"[0-9a-f]{64}", str(ui_runner.get("test_source_sha256", ""))) is None):
                    raise ValueError(f"Screenshot lacks the isolated UI-test-only runner receipt: {image}")
        images.extend(captures)

    names = {capture["filename"] for _, capture in images}
    required = {
        "iphone-01-files-and-books.png",
        "ipad-01-files-and-books.png",
        "tvos-01-files-and-books.png",
        "macos-01-files-and-books.png",
        "macos-02-iap-review-paywall.png",
    }
    if names != required:
        raise ValueError(f"Expected the five post-onboarding review screenshots, found {sorted(names)}")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    captures = []
    for image, capture in images:
        shutil.copyfile(image, output / capture["filename"])
        captures.append(capture)
    manifest = {
        "schema_version": 1,
        "product": "Pleazyng",
        "source_tag": SOURCE_TAG,
        "source_sha": SOURCE_SHA,
        "product_version": VERSION,
        "product_build": BUILD_NUMBER,
        "build_mode": "App Store IAP",
        "build_configuration_by_platform": BUILD_CONFIGURATION,
        "build_configuration_evidence_by_platform": {
            "ios": next(item["build_configuration_evidence"] for item in captures if item["filename"].startswith("iphone-")),
            "tvos": next(item["build_configuration_evidence"] for item in captures if item["filename"].startswith("tvos-")),
            "macos": next(item["build_configuration_evidence"] for item in captures if item["filename"].startswith("macos-")),
        },
        "packaging_tool_source_sha": require_capture_tools_sha(),
        "capture_tools_sha_by_platform": {
            "ios": next(item["capture_tools_sha"] for item in captures if item["filename"].startswith("iphone-")),
            "tvos": next(item["capture_tools_sha"] for item in captures if item["filename"].startswith("tvos-")),
            "macos": next(item["capture_tools_sha"] for item in captures if item["filename"].startswith("macos-")),
        },
        "captures": captures,
        "qualification": (
            f"Authentic post-onboarding local Files and Books screens from the v{VERSION} product app bundles on iPhone, iPad, "
            "tvOS Simulator, and hosted macOS. The Mac IAP sheet reflects the live StoreKit response and no purchase or "
            "restore action was activated. These captures do not qualify playback, StoreKit transactions, physical-device "
            "behavior, signing, or App Store processing."
        ),
    }
    (output / "capture-provenance.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "README.txt").write_text(
        f"Pleazyng app-in-use review screenshots for v{VERSION}\n\n"
        f"Source commit: {SOURCE_SHA}\n"
        "The screenshots show the real Files and Books screen after onboarding on iPhone, iPad, tvOS, and macOS. "
        "The macOS set also includes the real lifetime-access sheet, opened without activating Buy or Restore. "
        "No server, account, or media was seeded. StoreKit displays its live response.\n"
        "See capture-provenance.json for product source, bundle hashes, runner/device details, dimensions, and image hashes.\n",
        encoding="utf-8",
    )
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for path in sorted(output.iterdir()):
            target.write(path, path.name)
    print(f"Packaged verified app-in-use review screenshots: {archive}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preserve = subparsers.add_parser("preserve-build")
    preserve.add_argument("--product-root", required=True, type=Path)
    preserve.add_argument("--app", required=True, type=Path)
    preserve.add_argument("--platform", required=True, choices=("ios", "tvos", "macos"))
    preserve.add_argument("--source-lineage-root", type=Path)
    preserve.add_argument("--output-dir", required=True, type=Path)
    preserve.set_defaults(handler=preserve_build)
    tvos_dependency = subparsers.add_parser("verify-tvos-simulator-framework")
    tvos_dependency.add_argument("--product-root", required=True, type=Path)
    tvos_dependency.add_argument("--source-lineage-root", required=True, type=Path)
    tvos_dependency.add_argument("--app", required=True, type=Path)
    tvos_dependency.add_argument("--build-receipt", required=True, type=Path)
    tvos_dependency.add_argument("--producer-run-id", required=True)
    tvos_dependency.add_argument("--producer-run-attempt", required=True)
    tvos_dependency.add_argument("--output", required=True, type=Path)
    tvos_dependency.set_defaults(handler=verify_tvos_simulator_dependencies)
    restore = subparsers.add_parser("restore-build")
    restore.add_argument("--product-root", required=True, type=Path)
    restore.add_argument("--artifact-dir", required=True, type=Path)
    restore.add_argument("--platform", required=True, choices=("ios", "tvos", "macos"))
    restore.add_argument("--producer-run-id", required=True)
    restore.add_argument("--producer-run-attempt", required=True)
    restore.add_argument("--expected-capture-tools-sha", required=True)
    restore.add_argument("--output-dir", required=True, type=Path)
    restore.set_defaults(handler=restore_build)
    for name, handler in (
        ("capture-ios", capture_ios),
        ("capture-tvos", capture_tvos),
        ("capture-ios-in-use", capture_ios_in_use),
        ("capture-tvos-in-use", capture_tvos_in_use),
        ("capture-macos", capture_macos),
    ):
        command = subparsers.add_parser(name)
        command.add_argument("--product-root", required=True, type=Path)
        command.add_argument("--app", required=True, type=Path)
        command.add_argument("--output-dir", required=True, type=Path)
        if name != "capture-macos":
            command.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", str(os.getpid())))
            command.add_argument("--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
        if name in ("capture-ios-in-use", "capture-tvos-in-use"):
            command.add_argument("--build-receipt", required=True, type=Path)
        if name == "capture-ios-in-use":
            command.add_argument("--simulator-entitlements", required=True, type=Path)
            command.add_argument("--extension-entitlements", required=True, type=Path)
            command.add_argument("--device-category", choices=("both", "iphone", "ipad"), default="both")
        command.set_defaults(handler=handler)
    paywall = subparsers.add_parser("capture-macos-paywall")
    paywall.add_argument("--product-root", required=True, type=Path)
    paywall.add_argument("--app", required=True, type=Path)
    paywall.add_argument("--build-receipt", required=True, type=Path)
    paywall.add_argument("--output-dir", required=True, type=Path)
    paywall.set_defaults(handler=capture_macos_paywall)
    macos_in_use = subparsers.add_parser("capture-macos-in-use")
    macos_in_use.add_argument("--product-root", required=True, type=Path)
    macos_in_use.add_argument("--app", required=True, type=Path)
    macos_in_use.add_argument("--build-receipt", required=True, type=Path)
    macos_in_use.add_argument("--output-dir", required=True, type=Path)
    macos_in_use.set_defaults(handler=capture_macos_in_use)
    macos_listing_window = subparsers.add_parser("capture-macos-listing-window")
    macos_listing_window.add_argument("--product-root", required=True, type=Path)
    macos_listing_window.add_argument("--app", required=True, type=Path)
    macos_listing_window.add_argument("--build-receipt", required=True, type=Path)
    macos_listing_window.add_argument("--output-dir", required=True, type=Path)
    macos_listing_window.set_defaults(handler=capture_macos_listing_window)
    macos_diagnostic = subparsers.add_parser("diagnose-macos-in-use")
    macos_diagnostic.add_argument("--product-root", required=True, type=Path)
    macos_diagnostic.add_argument("--app", required=True, type=Path)
    macos_diagnostic.add_argument("--build-receipt", required=True, type=Path)
    macos_diagnostic.add_argument("--output-dir", required=True, type=Path)
    macos_diagnostic.set_defaults(handler=diagnose_macos_in_use)
    package = subparsers.add_parser("package")
    package.add_argument("--ios-dir", required=True, type=Path)
    package.add_argument("--tvos-dir", required=True, type=Path)
    package.add_argument("--macos-dir", required=True, type=Path)
    package.add_argument("--output-dir", required=True, type=Path)
    package.add_argument("--legacy-tool-source-sha", help="Capture-tools commit from a prior run when its receipts lack that field")
    package.set_defaults(handler=package_captures)
    app_in_use_package = subparsers.add_parser("package-app-in-use")
    app_in_use_package.add_argument("--ios-dir", required=True, type=Path)
    app_in_use_package.add_argument("--tvos-dir", required=True, type=Path)
    app_in_use_package.add_argument("--macos-dir", required=True, type=Path)
    app_in_use_package.add_argument("--output-dir", required=True, type=Path)
    app_in_use_package.set_defaults(handler=package_app_in_use_captures)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        args.handler(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"Apple Store screenshot capture failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
