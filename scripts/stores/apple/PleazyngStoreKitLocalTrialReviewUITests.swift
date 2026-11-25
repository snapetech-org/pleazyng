import Foundation
import StoreKit
import StoreKitTest
import XCTest
import CoreGraphics
import ImageIO
import Vision

final class PleazyngStoreKitLocalTrialReviewUITests: XCTestCase {
  let appBundleID = "org.snapetech.pleazyng"
  let trialProductID = "pleazyng_7_day_trial"
  let lifetimeProductID = "pleazyng_lifetime_unlock"

  func testCaptureTrialBackedFilesBooksAndAccessSheet() throws {
    try runLocalTrialCapture()
  }

  func platformName() -> String {
    #if os(tvOS)
      return "tvos"
    #else
      return "macos"
    #endif
  }

  func optionalDate(_ value: Date?) -> Any {
    guard let value else { return NSNull() }
    return ISO8601DateFormatter().string(from: value)
  }

  func attachScreenshot(of app: XCUIApplication, named name: String) throws {
    #if os(macOS)
      let capture = try captureMacWindowScreenshot(of: app)
      attachMacScreenshot(capture, named: name)
    #else
      let screenshot = app.screenshot()
      attachScreenshot(screenshot, named: name)
    #endif
  }

  func attachScreenshot(_ screenshot: XCUIScreenshot, named name: String) {
    let attachment = XCTAttachment(screenshot: screenshot)
    attachment.name = name
    attachment.lifetime = .keepAlways
    add(attachment)
  }

  struct ScreenText {
    let text: String
    let boundingBox: CGRect
  }

  func recognizeScreenText(in image: CGImage) throws -> [ScreenText] {
    let request = VNRecognizeTextRequest()
    request.usesLanguageCorrection = false
    request.recognitionLanguages = ["en-US"]
    #if os(macOS)
      // Hosted Macs are virtualized. Vision's default text detector failed
      // to compile there. These screenshots contain large UI text, so use
      // the fast detector and its supported CPU device for every stage.
      request.recognitionLevel = .fast
      for (stage, devices) in try request.supportedComputeStageDevices {
        let cpu = devices.first { device in
          if case .cpu = device { return true }
          return false
        }
        guard let cpu else {
          throw NSError(
            domain: "PleazyngStoreKitReviewCapture",
            code: 13,
            userInfo: [NSLocalizedDescriptionKey:
              "Vision text recognition has no CPU for a required stage: \(stage)"]
          )
        }
        request.setComputeDevice(cpu, for: stage)
      }
    #else
      request.recognitionLevel = .accurate
    #endif
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    return (request.results ?? []).compactMap { observation in
      guard let text = observation.topCandidates(1).first?.string else { return nil }
      return ScreenText(text: text, boundingBox: observation.boundingBox)
    }
  }

  func joinedText(_ lines: [ScreenText]) -> String {
    lines.map(\.text).joined(separator: " ")
  }

  func attachText(_ value: String, named name: String) {
    let attachment = XCTAttachment(string: value)
    attachment.name = name
    attachment.lifetime = .keepAlways
    add(attachment)
  }
}
