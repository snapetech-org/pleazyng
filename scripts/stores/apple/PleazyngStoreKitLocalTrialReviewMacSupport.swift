import Foundation
import CoreGraphics
import ImageIO
import Vision
import XCTest

#if os(macOS)
import AppKit
import CoreML

private func macJSONNumber(_ value: CGFloat) -> Any {
  let number = Double(value)
  return number.isFinite ? number as Any : NSNull()
}

private func macRectJSON(_ rect: CGRect) -> [Any] {
  [macJSONNumber(rect.minX), macJSONNumber(rect.minY), macJSONNumber(rect.width), macJSONNumber(rect.height)]
}
extension PleazyngStoreKitLocalTrialReviewUITests {

    struct MacScreenshotGeometry {
      let basisNames: [String]
      let rawImageSize: CGSize
      let screenFrame: CGRect
      let applicationFrame: CGRect
      let windowFrame: CGRect
      let selectedSourceFrame: CGRect
      let cropRectInPixels: CGRect
      let outputImageSize: CGSize
      let scaleX: CGFloat
      let scaleY: CGFloat

      func json(name: String) -> String {
        let value: [String: Any] = [
          "schema_version": 1,
          "capture_api": "XCUIScreen.main.screenshot()",
          "image_basis": basisNames,
          "raw_image_pixels": [rawImageSize.width, rawImageSize.height],
          "screen_frame_points": macRectJSON(screenFrame),
          "application_frame_points": macRectJSON(applicationFrame),
          "window_frame_points": macRectJSON(windowFrame),
          "selected_source_frame_points": macRectJSON(selectedSourceFrame),
          "crop_rect_in_raw_image_pixels": macRectJSON(cropRectInPixels),
          "output_window_image_pixels": [outputImageSize.width, outputImageSize.height],
          "pixels_per_point": [scaleX, scaleY],
          "ocr_coordinate_basis":
            "The cropped PNG maps one-to-one to the XCTest window frame; " +
            "Vision bounds are normalized from bottom-left and converted to " +
            "the window's top-left normalized offset before clicking."
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
              let string = String(data: data, encoding: .utf8) else {
          return "Failed to encode screenshot geometry for \(name)"
        }
        return string
      }
    }

    struct MacWindowScreenshot {
      let image: CGImage
      let pngData: Data
      let geometry: MacScreenshotGeometry
    }

    struct MacScreenshotMapping {
      var basisNames: [String]
      let sourceFrame: CGRect
      let cropRectInPixels: CGRect
      let scaleX: CGFloat
      let scaleY: CGFloat
    }

    struct MacTextObservation {
      let lines: [ScreenText]
      let screenshot: MacWindowScreenshot
    }

}

extension PleazyngStoreKitLocalTrialReviewUITests {
    func waitForMacVisibleText(
      _ expected: String, in app: XCUIApplication, attachmentName: String, timeout: TimeInterval
    ) throws -> MacTextObservation {
      let window = app.windows.firstMatch
      guard window.waitForExistence(timeout: min(timeout, 30)) else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture", code: 4,
          userInfo: [NSLocalizedDescriptionKey: "The real app window did not appear before the OCR deadline"])
      }
      let deadline = Date().addingTimeInterval(timeout)
      var latest: [ScreenText] = []
      var latestCapture: MacWindowScreenshot?
      repeat {
        let capture = try captureMacWindowScreenshot(of: app)
        latestCapture = capture
        latest = try recognizeScreenText(in: capture.image)
        if joinedText(latest).localizedCaseInsensitiveContains(expected) {
          attachMacScreenshot(capture, named: attachmentName)
          attachText(app.debugDescription, named: "\(attachmentName)-accessibility")
          attachText(macScreenTextJSON(latest), named: "\(attachmentName)-vision-ocr")
          return MacTextObservation(lines: latest, screenshot: capture)
        }
        Thread.sleep(forTimeInterval: 0.5)
      } while Date() < deadline

      if let latestCapture {
        attachMacScreenshot(latestCapture, named: "\(attachmentName)-timeout")
        attachText(macScreenTextJSON(latest), named: "\(attachmentName)-timeout-vision-ocr")
        attachText(app.debugDescription, named: "\(attachmentName)-timeout-accessibility")
      }
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture",
          code: 1,
          userInfo: [NSLocalizedDescriptionKey:
            "Timed out waiting for visible text \(expected.debugDescription); OCR saw: \(joinedText(latest))"]
        )
    }

    func clickMacVisibleText(
      _ expected: String, in app: XCUIApplication, attachmentName: String, timeout: TimeInterval = 30
    ) throws {
      let window = app.windows.firstMatch
      let button = app.buttons[expected]
      if button.exists && button.isHittable {
        button.click()
        attachText("Used the real XCTest accessibility button for \(expected)", named: "\(attachmentName)-action")
        return
      }

      let lines = try waitForMacVisibleText(
        expected, in: app, attachmentName: "\(attachmentName)-before-action", timeout: timeout)
      let matchingLines = lines.lines.filter { $0.text.localizedCaseInsensitiveContains(expected) }
      guard matchingLines.count == 1, let match = matchingLines.first else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture",
          code: 2,
          userInfo: [NSLocalizedDescriptionKey:
            "Expected one visible OCR hit for \(expected.debugDescription), got \(matchingLines.count)"]
        )
      }
      let offset = try macWindowOffset(forVisionBounds: match.boundingBox, geometry: lines.screenshot.geometry)
      attachText(
        "Used one visible OCR match for \(expected.debugDescription). " +
          "OCR bounds in the cropped app screenshot: \(NSStringFromRect(match.boundingBox)); " +
          "normalized window click offset: (\(offset.dx), \(offset.dy)). " +
          "Screenshot geometry: \(lines.screenshot.geometry.json(name: attachmentName)).",
        named: "\(attachmentName)-action"
      )
      window.coordinate(withNormalizedOffset: offset).click()
    }

    private func macWindowOffset(
      forVisionBounds bounds: CGRect, geometry: MacScreenshotGeometry
    ) throws -> CGVector {
      guard bounds.minX >= 0, bounds.minY >= 0, bounds.maxX <= 1, bounds.maxY <= 1,
            bounds.width > 0, bounds.height > 0 else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture",
          code: 5,
          userInfo: [NSLocalizedDescriptionKey:
            "Vision returned a text box outside normalized screenshot bounds: \(NSStringFromRect(bounds))"]
        )
      }
      let offset = CGVector(dx: bounds.midX, dy: 1 - bounds.midY)
      guard offset.dx >= 0.02, offset.dx <= 0.98, offset.dy >= 0.02, offset.dy <= 0.98,
            geometry.outputImageSize.width > 0,
            geometry.outputImageSize.height > 0,
            abs(geometry.outputImageSize.width / geometry.outputImageSize.height
                - geometry.windowFrame.width / geometry.windowFrame.height) <= 0.02 else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture",
          code: 6,
          userInfo: [NSLocalizedDescriptionKey:
            "The OCR target is not safely inside the cropped app window: \(NSStringFromRect(bounds)); " +
              "geometry: \(geometry.json(name: "click"))"]
        )
      }
      return offset
    }

    func assertMacScreenshotGeometryScale() throws {
      let screen = CGRect(x: 0, y: 0, width: 1024, height: 768)
      let window = CGRect(x: 112, y: 51, width: 800, height: 632)
      let fullScreenMapping = try resolveMacScreenshotMapping(
        rawImageSize: CGSize(width: 1024, height: 768), screenFrame: screen, windowFrame: window)
      XCTAssertEqual(fullScreenMapping.cropRectInPixels, window)

      let retinaMapping = try resolveMacScreenshotMapping(
        rawImageSize: CGSize(width: 2048, height: 1536), screenFrame: screen, windowFrame: window)
      XCTAssertEqual(retinaMapping.cropRectInPixels, CGRect(x: 224, y: 102, width: 1600, height: 1264))
      XCTAssertEqual(retinaMapping.scaleX, 2)
      XCTAssertEqual(retinaMapping.scaleY, 2)

      let listingWindow = CGRect(x: 56, y: 80, width: 912, height: 570)
      let listingMapping = try resolveMacScreenshotMapping(
        rawImageSize: CGSize(width: 1024, height: 768), screenFrame: screen, windowFrame: listingWindow)
      XCTAssertEqual(listingMapping.cropRectInPixels, listingWindow)
      XCTAssertEqual(listingWindow.width * 5, listingWindow.height * 8)
      let listingRetinaMapping = try resolveMacScreenshotMapping(
        rawImageSize: CGSize(width: 2048, height: 1536), screenFrame: screen, windowFrame: listingWindow)
      XCTAssertEqual(listingRetinaMapping.cropRectInPixels, CGRect(x: 112, y: 160, width: 1824, height: 1140))
      XCTAssertEqual(listingRetinaMapping.scaleX, 2)
      XCTAssertEqual(listingRetinaMapping.scaleY, 2)

      let upperScreenText = CGRect(x: 0.1, y: 0.7, width: 0.2, height: 0.2)
      let lowerScreenText = CGRect(x: 0.1, y: 0.1, width: 0.2, height: 0.2)
      let geometry = fixtureGeometry(window)
      let upperOffset = try macWindowOffset(forVisionBounds: upperScreenText, geometry: geometry)
      let lowerOffset = try macWindowOffset(forVisionBounds: lowerScreenText, geometry: geometry)
      XCTAssertEqual(upperOffset.dx, 0.2, accuracy: 0.0001)
      XCTAssertEqual(upperOffset.dy, 0.2, accuracy: 0.0001)
      XCTAssertEqual(lowerOffset.dx, 0.2, accuracy: 0.0001)
      XCTAssertEqual(lowerOffset.dy, 0.8, accuracy: 0.0001)

      let unavailableFrame = CGRect(x: CGFloat.infinity, y: CGFloat.nan, width: 800, height: 632)
      let unavailableFrameJSON = try JSONSerialization.data(withJSONObject: macRectJSON(unavailableFrame))
      let unavailableFrameValues = try XCTUnwrap(
        JSONSerialization.jsonObject(with: unavailableFrameJSON) as? [Any])
      XCTAssertTrue(unavailableFrameValues[0] is NSNull)
      XCTAssertTrue(unavailableFrameValues[1] is NSNull)
      XCTAssertEqual(unavailableFrameValues[2] as? Double, 800)
      XCTAssertEqual(unavailableFrameValues[3] as? Double, 632)

      XCTAssertThrowsError(try resolveMacScreenshotMapping(
        rawImageSize: CGSize(width: 1024, height: 768),
        screenFrame: screen,
        windowFrame: CGRect(x: 300, y: 100, width: 800, height: 600)))
    }

    private func fixtureGeometry(_ window: CGRect) -> MacScreenshotGeometry {
      MacScreenshotGeometry(
        basisNames: ["window"],
        rawImageSize: window.size,
        screenFrame: CGRect(x: 0, y: 0, width: 1024, height: 768),
        applicationFrame: window,
        windowFrame: window,
        selectedSourceFrame: window,
        cropRectInPixels: CGRect(origin: .zero, size: window.size),
        outputImageSize: window.size,
        scaleX: 1,
        scaleY: 1)
    }

    func assertMacListingWindowGeometry(_ capture: MacWindowScreenshot) throws {
      let frame = capture.geometry.windowFrame
      let expectedWidth: CGFloat = 912
      let expectedHeight: CGFloat = 570
      let raw = capture.geometry.outputImageSize
      let scaleX = raw.width / frame.width
      let scaleY = raw.height / frame.height
      guard abs(frame.width - expectedWidth) <= 0.5,
            abs(frame.height - expectedHeight) <= 0.5,
            abs(frame.width * 5 - frame.height * 8) <= 0.01,
            raw.width * 5 == raw.height * 8,
            scaleX.isFinite, scaleY.isFinite, scaleX > 0,
            abs(scaleX - scaleY) <= 0.02 else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture", code: 15,
          userInfo: [NSLocalizedDescriptionKey:
            "The complete captured Mac app window is not the requested native 912x570 16:10 window: " +
            capture.geometry.json(name: "macos-native-listing-window")])
      }
    }

    private func resolveMacScreenshotMapping(
      rawImageSize: CGSize, screenFrame: CGRect, windowFrame: CGRect
    ) throws -> MacScreenshotMapping {
      let scaleX = rawImageSize.width / screenFrame.width
      let scaleY = rawImageSize.height / screenFrame.height
      let crop = CGRect(
        x: (windowFrame.minX - screenFrame.minX) * scaleX,
        y: (windowFrame.minY - screenFrame.minY) * scaleY,
        width: windowFrame.width * scaleX,
        height: windowFrame.height * scaleY).integral
      guard scaleX.isFinite, scaleY.isFinite, scaleX > 0, scaleY > 0,
            abs(scaleX - scaleY) / max(scaleX, scaleY) <= 0.02,
            crop.width > 0, crop.height > 0,
            CGRect(origin: .zero, size: rawImageSize).contains(crop) else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture",
          code: 7,
          userInfo: [NSLocalizedDescriptionKey:
            "Could not crop the app window \(NSStringFromRect(windowFrame)) from the main display " +
              "\(NSStringFromRect(screenFrame)) at \(rawImageSize) pixels"]
        )
      }
      return MacScreenshotMapping(
        basisNames: ["screen"], sourceFrame: screenFrame,
        cropRectInPixels: crop, scaleX: scaleX, scaleY: scaleY)
    }

    func captureMacWindowScreenshot(of app: XCUIApplication) throws -> MacWindowScreenshot {
      let window = app.windows.firstMatch
      guard window.waitForExistence(timeout: 30) else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture", code: 8,
          userInfo: [NSLocalizedDescriptionKey: "The app window was not available for a supported XCTest screenshot"])
      }
      // The element screenshot path aborts inside XCTest's display lookup on
      // Xcode 27. Capture the known main display and attach only the app crop.
      app.activate()
      let screenshot = XCUIScreen.main.screenshot()
      guard let source = CGImageSourceCreateWithData(screenshot.pngRepresentation as CFData, nil),
            let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture", code: 9,
          userInfo: [NSLocalizedDescriptionKey: "XCUIScreen.main.screenshot() did not provide a decodable PNG"])
      }
      let rawImageSize = CGSize(width: image.width, height: image.height)
      let screenFrame = CGDisplayBounds(CGMainDisplayID())
      let applicationFrame = app.frame
      let windowFrame = window.frame
      let mapping = try resolveMacScreenshotMapping(
        rawImageSize: rawImageSize,
        screenFrame: screenFrame,
        windowFrame: windowFrame)
      let imageBounds = CGRect(origin: .zero, size: rawImageSize)
      let crop = mapping.cropRectInPixels.intersection(imageBounds)
      guard crop == mapping.cropRectInPixels,
            let croppedImage = image.cropping(to: crop) else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture",
          code: 10,
          userInfo: [NSLocalizedDescriptionKey:
            "The observed app window crop fell outside screenshot pixels: " +
              "\(NSStringFromRect(mapping.cropRectInPixels)) of \(rawImageSize)"]
        )
      }
      let output = try encodeMacScreenshot(croppedImage)
      let geometry = MacScreenshotGeometry(
        basisNames: mapping.basisNames,
        rawImageSize: rawImageSize,
        screenFrame: screenFrame,
        applicationFrame: applicationFrame,
        windowFrame: windowFrame,
        selectedSourceFrame: mapping.sourceFrame,
        cropRectInPixels: crop,
        outputImageSize: CGSize(width: croppedImage.width, height: croppedImage.height),
        scaleX: mapping.scaleX,
        scaleY: mapping.scaleY)
      return MacWindowScreenshot(image: croppedImage, pngData: output, geometry: geometry)
    }

    private func encodeMacScreenshot(_ image: CGImage) throws -> Data {
      let output = NSMutableData()
      guard let destination = CGImageDestinationCreateWithData(output, "public.png" as CFString, 1, nil) else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture",
          code: 11,
          userInfo: [NSLocalizedDescriptionKey: "Could not create a PNG for the cropped app screenshot"]
        )
      }
      CGImageDestinationAddImage(destination, image, nil)
      guard CGImageDestinationFinalize(destination) else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture",
          code: 12,
          userInfo: [NSLocalizedDescriptionKey: "Could not encode the cropped app screenshot as PNG"]
        )
      }
      return output as Data
    }

    func attachMacScreenshot(_ capture: MacWindowScreenshot, named name: String) {
      let attachment = XCTAttachment(data: capture.pngData, uniformTypeIdentifier: "public.png")
      attachment.name = name
      attachment.lifetime = .keepAlways
      add(attachment)
      attachText(capture.geometry.json(name: name), named: "\(name)-capture-geometry")
    }

    private func macScreenTextJSON(_ lines: [ScreenText]) -> String {
      let records: [[String: Any]] = lines.map { line in
        [
          "text": line.text,
          "normalized_bounds": [
            line.boundingBox.minX, line.boundingBox.minY,
            line.boundingBox.width, line.boundingBox.height
          ]
        ]
      }
      guard let data = try? JSONSerialization.data(withJSONObject: records, options: [.sortedKeys]),
            let text = String(data: data, encoding: .utf8) else {
        return "Could not serialize native Vision OCR observations"
      }
      return text
    }
}
#endif
