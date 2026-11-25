import Foundation
import XCTest

#if os(macOS)
import AppKit

extension PleazyngStoreKitLocalTrialReviewUITests {
  func resizeMacWindowForStoreScreenshot(_ app: XCUIApplication) throws {
    let geometry = try MacWindowResizeSupport.resize(app: app)
    attachText(
      "XCTest resized the real product window to \(geometry) before capturing it.",
      named: "pleazyng-macos-window-resize"
    )
  }
}

private enum MacWindowResizeSupport {
  private static let targetSize = CGSize(width: 912, height: 570)

  static func resize(app: XCUIApplication) throws -> String {
    let window = app.windows.firstMatch
    guard window.waitForExistence(timeout: 30), window.isHittable else {
      throw resizeError("The launched product window is not available for XCTest resizing")
    }

    let initialFrame = window.frame
    let screenFrame = CGDisplayBounds(CGMainDisplayID())
    guard screenFrame.width >= targetSize.width, screenFrame.height >= targetSize.height,
          initialFrame.minX >= screenFrame.minX, initialFrame.minY >= screenFrame.minY,
          initialFrame.minX + targetSize.width <= screenFrame.maxX,
          initialFrame.minY + targetSize.height <= screenFrame.maxY else {
      throw resizeError(
        "The 912x570 screenshot window does not fit the visible display at the current origin: " +
          "window=\(NSStringFromRect(initialFrame)), display=\(NSStringFromRect(screenFrame))"
      )
    }

    if abs(initialFrame.width - targetSize.width) > 0.5
      || abs(initialFrame.height - targetSize.height) > 0.5 {
      let lowerRight = window.coordinate(withNormalizedOffset: CGVector(dx: 0.995, dy: 0.995))
      let target = lowerRight.withOffset(CGVector(
        dx: targetSize.width - initialFrame.width,
        dy: targetSize.height - initialFrame.height
      ))
      lowerRight.click(forDuration: 0.15, thenDragTo: target)
    }

    let deadline = Date().addingTimeInterval(10)
    var finalFrame = window.frame
    while Date() < deadline {
      finalFrame = window.frame
      if abs(finalFrame.width - targetSize.width) <= 0.5,
         abs(finalFrame.height - targetSize.height) <= 0.5 {
        return "\(Int(finalFrame.width))x\(Int(finalFrame.height)) points at " +
          "\(Int(finalFrame.minX)),\(Int(finalFrame.minY))"
      }
      Thread.sleep(forTimeInterval: 0.25)
    }
    throw resizeError(
      "XCTest did not resize the native product window to 912x570 points: " +
        NSStringFromRect(finalFrame)
    )
  }

  private static func resizeError(_ message: String) -> NSError {
    NSError(
      domain: "PleazyngStoreKitReviewCapture",
      code: 19,
      userInfo: [NSLocalizedDescriptionKey: message]
    )
  }
}
#endif
