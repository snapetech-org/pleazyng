import Foundation
import XCTest

#if os(macOS)
import AppKit

extension PleazyngStoreKitLocalTrialReviewUITests {
  func resizeMacWindowForStoreScreenshot() throws {
    let geometry = try MacWindowResizeSupport.resize(bundleIdentifier: appBundleID)
    attachText(
      "System Events resized the real product window to \(geometry) before XCTest captured it.",
      named: "pleazyng-macos-window-resize"
    )
  }
}

private enum MacWindowResizeSupport {
  static func resize(bundleIdentifier: String) throws -> String {
    let processIdentifier = try processIdentifier(bundleIdentifier: bundleIdentifier)
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    process.arguments = ["-e", script(processIdentifier: processIdentifier)]
    let stdout = Pipe()
    let stderr = Pipe()
    process.standardOutput = stdout
    process.standardError = stderr
    do {
      try process.run()
    } catch {
      throw resizeError(17, "Could not start bounded Mac window resize: \(error)")
    }
    try waitForExit(process)
    let output = pipeText(stdout)
    let errorOutput = pipeText(stderr)
    guard process.terminationStatus == 0, output == "912|570|56|80" else {
      let detail = errorOutput.isEmpty ? output : errorOutput
      throw resizeError(
        19,
        "System Events could not apply the requested 912x570 app window: \(detail)"
      )
    }
    return "912x570 points at 56,80"
  }

  private static func processIdentifier(bundleIdentifier: String) throws -> Int32 {
    let deadline = Date().addingTimeInterval(20)
    while Date() < deadline {
      let matches = NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier)
      let identified = matches.filter { $0.processIdentifier > 0 }
      if identified.count > 1 {
        throw resizeError(16, "More than one running app matches bundle ID \(bundleIdentifier)")
      }
      if let app = identified.first, app.isFinishedLaunching {
        return app.processIdentifier
      }
      Thread.sleep(forTimeInterval: 0.25)
    }
    throw resizeError(16, "The launched product app has no finished process for bundle ID \(bundleIdentifier)")
  }

  private static func waitForExit(_ process: Process) throws {
    let completed = DispatchSemaphore(value: 0)
    DispatchQueue.global(qos: .userInitiated).async {
      process.waitUntilExit()
      completed.signal()
    }
    guard completed.wait(timeout: .now() + 30) == .success else {
      process.terminate()
      throw resizeError(18, "System Events did not resize the Mac window within 30 seconds")
    }
  }

  private static func pipeText(_ pipe: Pipe) -> String {
    String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
      .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
  }

  private static func resizeError(_ code: Int, _ message: String) -> NSError {
    NSError(
      domain: "PleazyngStoreKitReviewCapture",
      code: code,
      userInfo: [NSLocalizedDescriptionKey: message]
    )
  }

  private static func script(processIdentifier: Int32) -> String {
    """
    on run
      set targetPid to \(processIdentifier)
      tell application "System Events"
        set targetProcess to first application process whose unix id is targetPid
        set frontmost of targetProcess to true
        set targetWindow to first window of targetProcess
        set position of targetWindow to {56, 80}
        set size of targetWindow to {912, 570}
        delay 1
        set actualSize to size of targetWindow
        set actualPosition to position of targetWindow
        set widthText to (item 1 of actualSize) as text
        set heightText to (item 2 of actualSize) as text
        set xText to (item 1 of actualPosition) as text
        set yText to (item 2 of actualPosition) as text
        return widthText & "|" & heightText & "|" & xText & "|" & yText
      end tell
    end run
    """
  }
}
#endif
