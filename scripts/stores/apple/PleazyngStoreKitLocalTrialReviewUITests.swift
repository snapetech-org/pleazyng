import Foundation
import StoreKit
import StoreKitTest
import XCTest
import CoreGraphics
import ImageIO
import Vision

#if os(macOS)
import AppKit

private func macRectJSON(_ rect: CGRect) -> [Double] {
  [Double(rect.minX), Double(rect.minY), Double(rect.width), Double(rect.height)]
}
#endif

final class PleazyngStoreKitLocalTrialReviewUITests: XCTestCase {
  private let appBundleID = "org.snapetech.pleazyng"
  private let trialProductID = "pleazyng_7_day_trial"
  private let lifetimeProductID = "pleazyng_lifetime_unlock"

  func testCaptureTrialBackedFilesBooksAndAccessSheet() throws {
#if os(macOS)
    // Initialize this XCTest runner's WindowServer connection before using
    // the display API. The runner has no application windows of its own.
    _ = NSApplication.shared
    try assertMacScreenshotGeometryScale()
#endif
    let configurationURL = try XCTUnwrap(
      Bundle(for: PleazyngStoreKitLocalTrialReviewUITests.self).url(
        forResource: "pleazyng-trial-review",
        withExtension: "storekit"
      ),
      "The pinned LOCALTEST StoreKit catalog was not bundled with the XCTest target"
    )
    let session = try SKTestSession(contentsOf: configurationURL)
    session.resetToDefaultState()
    session.storefront = "USA"
    session.locale = Locale(identifier: "en_US")
    session.disableDialogs = true
    session.clearTransactions()
    XCTAssertTrue(session.allTransactions().isEmpty, "The LOCALTEST store must begin without transactions")

    let transactionsBeforeLaunch = session.allTransactions()
    XCTAssertTrue(transactionsBeforeLaunch.isEmpty, "The LOCALTEST store must be empty before the app starts")

    // The test plan associates the exact receipt-verified product bundle
    // with this UI test. The user-visible button below starts the free trial
    // through the actual product app and its native StoreKit integration.
    let app = XCUIApplication(bundleIdentifier: appBundleID)
    app.launch()
    try attachScreenshot(of: app, named: "pleazyng-diagnostic-after-launch-\(platformName())")
    attachText(app.debugDescription, named: "pleazyng-diagnostic-accessibility-after-launch-\(platformName())")

    #if os(macOS)
      try tapMacVisibleText("Start fresh", in: app, attachmentName: "pleazyng-macos-legacy-onboarding")
      let afterOnboarding = try waitForMacVisibleText(
        "Start my 7-day free trial", in: app, attachmentName: "pleazyng-diagnostic-after-onboarding-macos",
        timeout: 90)
      XCTAssertTrue(
        joinedText(afterOnboarding.lines).contains("Read, listen, and watch from your own files"),
        "The actual first-run app access screen did not appear after choosing Start fresh")
    #endif

    var trialOfferShown = false
    var trialOfferLifetimePriceShown = false
    var lifetimeDisplayPrice = ""
    #if os(macOS)
      let offer = try waitForMacVisibleText(
        "Start my 7-day free trial", in: app, attachmentName: "pleazyng-localtest-trial-offer-macos",
        timeout: 60)
      let offerText = joinedText(offer.lines)
      XCTAssertTrue(offerText.contains("Unlock every feature"), "The queried LOCALTEST lifetime product was not visible")
      XCTAssertTrue(offerText.contains("$2.99"), "The queried LOCALTEST lifetime price was not visible")
      XCTAssertTrue(
        offerText.contains("There is no subscription or automatic charge"),
        "The actual product trial disclosure was not visible")
      trialOfferShown = true
      trialOfferLifetimePriceShown = offerText.contains("Unlock every feature") && offerText.contains("$2.99")
      lifetimeDisplayPrice = offer.lines.first(where: { $0.text.contains("$2.99") })?.text ?? "$2.99"
      try tapMacVisibleText(
        "Start my 7-day free trial", in: app, attachmentName: "pleazyng-macos-trial-offer-action")
    #else
      let offerText = try waitForTVScreen(
        ["Start my 7-day free trial", "Unlock every feature", "$2.99", "no subscription or automatic charge"],
        in: app, attachmentName: "pleazyng-localtest-trial-offer-tvos", timeout: 90)
      trialOfferShown = true
      trialOfferLifetimePriceShown = offerText.contains("$2.99")
      lifetimeDisplayPrice = "$2.99"
      // The app's initial focus is the free-trial CTA. The LOCALTEST
      // transaction assertions below verify which control was activated.
      XCUIRemote.shared.press(.select)
    #endif

    #if os(macOS)
      _ = try waitForMacVisibleText(
        "Use files and books", in: app, attachmentName: "pleazyng-diagnostic-after-trial-purchase-macos",
        timeout: 90)
    #else
      _ = try waitForTVScreen(
        ["Use files and books"], in: app,
        attachmentName: "pleazyng-diagnostic-after-trial-purchase-tvos", timeout: 90)
    #endif
    let trialTransactions = session.allTransactions()
    XCTAssertEqual(trialTransactions.count, 1, "The actual product CTA must create only one LOCALTEST trial")
    let original = try XCTUnwrap(trialTransactions.first)
    XCTAssertEqual(original.productIdentifier, trialProductID)
    XCTAssertEqual(original.state, .purchased)
    XCTAssertFalse(trialTransactions.contains { $0.productIdentifier == lifetimeProductID })

    #if os(macOS)
      try tapMacVisibleText("Use files and books", in: app, attachmentName: "pleazyng-macos-use-files-action")
    #else
      // The TV QR sign-in CTA takes initial focus. The next four
      // actions are Jellyfin, Emby, direct Plex, then Files and Books.
      for _ in 0..<4 {
        XCUIRemote.shared.press(.down)
        Thread.sleep(forTimeInterval: 0.25)
      }
      try attachScreenshot(of: app, named: "pleazyng-diagnostic-before-use-files-tvos")
      XCUIRemote.shared.press(.select)
    #endif

    var filesBooksScreenOpened = false
    #if os(macOS)
      let files = try waitForMacVisibleText(
        "Add a source or import a book", in: app, attachmentName: "pleazyng-localtest-files-books-macos",
        timeout: 60)
      let filesText = joinedText(files.lines)
      XCTAssertTrue(filesText.contains("Add source"), "The real local library Add source action is missing")
      XCTAssertTrue(filesText.contains("Import files"), "The real local library Import files action is missing")
      filesBooksScreenOpened = true
    #else
      // Add Source and Import are icon-only buttons on the TV layout.
      _ = try waitForTVScreen(
        ["Files and books", "Add a source or import a book", "Search saved library"],
        in: app, attachmentName: "pleazyng-localtest-files-books-tvos", timeout: 60)
      filesBooksScreenOpened = true
    #endif

    #if os(macOS)
      app.activate()
      app.typeKey(",", modifierFlags: .command)
      _ = try waitForMacVisibleText(
        "Pleazyng lifetime access", in: app, attachmentName: "pleazyng-diagnostic-settings-macos",
        timeout: 60)
      try tapMacVisibleText(
        "Pleazyng lifetime access", in: app, attachmentName: "pleazyng-macos-lifetime-settings-action")
    #else
      try openTVSettings(in: app)
      _ = try waitForTVScreen(
        ["Settings", "Pleazyng lifetime access"], in: app,
        attachmentName: "pleazyng-diagnostic-settings-tvos", timeout: 60)
      // Settings takes focus on its first displayed tile in Store builds.
      XCUIRemote.shared.press(.select)
    #endif

    var activeTrialShown = false
    var accessSheetLifetimePriceShown = false
    #if os(macOS)
      let accessSheet = try waitForMacVisibleText(
        "Your 7-day full-feature trial is active.", in: app,
        attachmentName: "pleazyng-localtest-supporter-macos", timeout: 60)
      let accessText = joinedText(accessSheet.lines)
      XCTAssertTrue(accessText.contains("Restore lifetime purchase"), "The real access sheet did not show its Restore action")
      XCTAssertTrue(accessText.contains("$2.99"), "The app did not show the queried LOCALTEST lifetime price")
      activeTrialShown = true
      accessSheetLifetimePriceShown = accessText.contains("$2.99")
    #else
      let accessText = try waitForTVScreen(
        ["Your 7-day full-feature trial is active", "Restore lifetime purchase", "$2.99"],
        in: app, attachmentName: "pleazyng-localtest-supporter-tvos", timeout: 60)
      activeTrialShown = true
      accessSheetLifetimePriceShown = accessText.contains("$2.99")
    #endif

    let finalTransactions = session.allTransactions()
    XCTAssertEqual(finalTransactions.count, 1, "Opening product screens must not change the local transaction history")
    let finalTrial = try XCTUnwrap(finalTransactions.first)
    XCTAssertEqual(finalTrial.productIdentifier, trialProductID)
    XCTAssertEqual(finalTrial.state, .purchased)
    XCTAssertEqual(finalTrial.originalTransactionIdentifier, original.originalTransactionIdentifier)
    XCTAssertEqual(finalTrial.purchaseDate, original.purchaseDate)
    XCTAssertFalse(finalTransactions.contains { $0.productIdentifier == lifetimeProductID })

    let record: [String: Any] = [
      "environment": "LOCALTEST",
      "qualification":
        "The actual product UI started a native Apple StoreKitTest free-trial transaction using the exact scheme-associated local catalog. This is local test evidence only, not App Store Connect, App Store Sandbox, or live purchase evidence.",
      "storefront": "USA",
      "catalog_queries": [
        "trial_product_id": trialProductID,
        "lifetime_product_id": lifetimeProductID,
        "lifetime_display_price": lifetimeDisplayPrice,
      ],
      "transactions": finalTransactions.map { transaction in
        [
          "product_id": transaction.productIdentifier,
          "state": transaction.state == .purchased ? "purchased" : "other",
          "transaction_id": String(transaction.identifier),
          "original_transaction_id": String(transaction.originalTransactionIdentifier),
          "purchase_date": ISO8601DateFormatter().string(from: transaction.purchaseDate),
          "expiration_date": optionalDate(transaction.expirationDate),
          "cancel_date": optionalDate(transaction.cancelDate),
        ]
      },
      "history": [
        "beforeAppLaunch": [
          "transactionCount": transactionsBeforeLaunch.count
        ],
        "afterProductTrialCTA": [
          "transactionCount": trialTransactions.count,
          "originalTransactionId": String(original.originalTransactionIdentifier),
          "purchaseDate": ISO8601DateFormatter().string(from: original.purchaseDate),
        ],
        "afterProductNavigation": [
          "transactionCount": finalTransactions.count,
          "originalTransactionId": String(finalTrial.originalTransactionIdentifier),
          "purchaseDate": ISO8601DateFormatter().string(from: finalTrial.purchaseDate),
        ],
      ],
      "app_behavior": [
        "trial_offer_shown": trialOfferShown,
        "trial_offer_lifetime_price_shown": trialOfferLifetimePriceShown,
        "trial_started_via_product_ui": true,
        "transaction_created_outside_product_app_ui": false,
        "active_trial_restored_by_app": activeTrialShown,
        "files_and_books_screen_opened": filesBooksScreenOpened,
        "active_trial_sheet_shown": activeTrialShown,
        "lifetime_price_shown": accessSheetLifetimePriceShown,
        "lifetime_purchase_activated": false,
        "lifetime_transaction_created": false,
      ],
      "raw_jws_included": false,
    ]
    let data = try JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
    let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
    attachment.name = "pleazyng-storekit-macos-tvos-localtest-record"
    attachment.lifetime = .keepAlways
    add(attachment)
  }

  private func platformName() -> String {
    #if os(tvOS)
      return "tvos"
    #else
      return "macos"
    #endif
  }

  private func optionalDate(_ value: Date?) -> Any {
    guard let value else { return NSNull() }
    return ISO8601DateFormatter().string(from: value)
  }

  private func attachScreenshot(of app: XCUIApplication, named name: String) throws {
    #if os(macOS)
      let capture = try captureMacWindowScreenshot(of: app)
      attachMacScreenshot(capture, named: name)
    #else
      let screenshot = app.screenshot()
      attachScreenshot(screenshot, named: name)
    #endif
  }

  private func attachScreenshot(_ screenshot: XCUIScreenshot, named name: String) {
    let attachment = XCTAttachment(screenshot: screenshot)
    attachment.name = name
    attachment.lifetime = .keepAlways
    add(attachment)
  }

  private struct ScreenText {
    let text: String
    let boundingBox: CGRect
  }

  private func recognizeScreenText(in image: CGImage) throws -> [ScreenText] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.recognitionLanguages = ["en-US"]
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    return (request.results ?? []).compactMap { observation in
      guard let text = observation.topCandidates(1).first?.string else { return nil }
      return ScreenText(text: text, boundingBox: observation.boundingBox)
    }
  }

  private func joinedText(_ lines: [ScreenText]) -> String {
    lines.map(\.text).joined(separator: " ")
  }

  #if os(tvOS)
    private struct TVScreen {
      let screenshot: XCUIScreenshot
      let image: CGImage
      let lines: [ScreenText]
    }

    private func captureTVScreen(in app: XCUIApplication) throws -> TVScreen {
      let screenshot = app.screenshot()
      guard let source = CGImageSourceCreateWithData(screenshot.pngRepresentation as CFData, nil),
            let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(domain: "PleazyngStoreKitReviewCapture", code: 20,
                      userInfo: [NSLocalizedDescriptionKey: "Could not read the live tvOS screenshot"])
      }
      return TVScreen(screenshot: screenshot, image: image, lines: try recognizeScreenText(in: image))
    }

    // On the fresh default palette the focused rail row has a teal border.
    // Look for long matching border segments immediately above and below
    // the Settings text. An unfocused label or an adjacent icon cannot match.
    private func hasTVRailFocus(_ row: CGRect, in image: CGImage) -> Bool {
      let width = CGFloat(image.width)
      let height = CGFloat(image.height)
      let span = CGRect(
        x: row.minX * width, y: (1 - row.maxY - row.height * 2) * height,
        width: max(row.width, 0.025) * width, height: row.height * 5 * height
      ).integral
      guard CGRect(x: 0, y: 0, width: width, height: height).contains(span),
            let slice = image.cropping(to: span),
            let canvas = CGContext(data: nil, width: slice.width, height: slice.height,
              bitsPerComponent: 8, bytesPerRow: slice.width * 4, space: CGColorSpaceCreateDeviceRGB(),
              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue),
            let pixels = canvas.data?.assumingMemoryBound(to: UInt8.self) else { return false }
      canvas.draw(slice, in: CGRect(x: 0, y: 0, width: span.width, height: span.height))
      func border(in rows: Range<Int>) -> Bool {
        rows.contains { y in
          (1...5).allSatisfy { sample in
            let x = sample * slice.width / 6
            let offset = y * canvas.bytesPerRow + x * 4
            let red = Int(pixels[offset])
            let green = Int(pixels[offset + 1])
            let blue = Int(pixels[offset + 2])
            return green > 150 && green - red > 45 && blue - red > 35
          }
        }
      }
      let third = slice.height / 3
      return border(in: 0..<third) && border(in: (slice.height - third)..<slice.height)
    }

    private func openTVSettings(in app: XCUIApplication) throws {
      // Menu moves focus from the library content to the expanded sidebar.
      XCUIRemote.shared.press(.menu)
      _ = try waitForTVScreen(
        ["Settings"], in: app, attachmentName: "pleazyng-diagnostic-sidebar-tvos", timeout: 15)
      for direction: XCUIRemote.Button in [.down, .up] {
        for _ in 0..<8 {
          let screen = try captureTVScreen(in: app)
          if let settings = screen.lines.first(where: {
            $0.text == "Settings" && $0.boundingBox.midX < 0.30
          }), hasTVRailFocus(settings.boundingBox, in: screen.image) {
            attachScreenshot(screen.screenshot, named: "pleazyng-diagnostic-sidebar-settings-focused-tvos")
            XCUIRemote.shared.press(.select)
            return
          }
          XCUIRemote.shared.press(direction)
          Thread.sleep(forTimeInterval: 0.3)
        }
      }
      try attachScreenshot(of: app, named: "pleazyng-diagnostic-sidebar-settings-unfocused-tvos")
      throw NSError(domain: "PleazyngStoreKitReviewCapture", code: 22,
                    userInfo: [NSLocalizedDescriptionKey: "The Settings row did not receive visible tvOS remote focus"])
    }

    // The tvOS Flutter engine can render controls without exposing them
    // to XCTest. Inspect the actual app screenshot after each remote action.
    private func waitForTVScreen(
      _ markers: [String], in app: XCUIApplication, attachmentName: String, timeout: TimeInterval
    ) throws -> String {
      let deadline = Date().addingTimeInterval(timeout)
      while true {
        let screen = try captureTVScreen(in: app)
        let text = joinedText(screen.lines)
        if markers.allSatisfy({ text.localizedCaseInsensitiveContains($0) }) {
          attachScreenshot(screen.screenshot, named: attachmentName)
          attachText(text, named: "\(attachmentName)-visible-text")
          return text
        }
        if Date() >= deadline {
          attachScreenshot(screen.screenshot, named: "pleazyng-diagnostic-\(attachmentName)-timeout")
          attachText(text, named: "pleazyng-diagnostic-\(attachmentName)-visible-text")
          attachText(app.debugDescription, named: "pleazyng-diagnostic-\(attachmentName)-accessibility")
          throw NSError(domain: "PleazyngStoreKitReviewCapture", code: 21,
                        userInfo: [NSLocalizedDescriptionKey: "Missing visible tvOS controls: \(markers.joined(separator: ", "))"])
        }
        Thread.sleep(forTimeInterval: 0.5)
      }
    }
  #endif

  #if os(macOS)

    private struct MacScreenshotGeometry {
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
          "ocr_coordinate_basis": "The cropped PNG maps one-to-one to the XCTest window frame; Vision bounds are normalized from bottom-left and converted to the window's top-left normalized offset before tapping.",
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
              let string = String(data: data, encoding: .utf8) else {
          return "Failed to encode screenshot geometry for \(name)"
        }
        return string
      }
    }

    private struct MacWindowScreenshot {
      let image: CGImage
      let pngData: Data
      let geometry: MacScreenshotGeometry
    }

    private struct MacScreenshotMapping {
      var basisNames: [String]
      let sourceFrame: CGRect
      let cropRectInPixels: CGRect
      let scaleX: CGFloat
      let scaleY: CGFloat
    }

    private struct MacTextObservation {
      let lines: [ScreenText]
      let screenshot: MacWindowScreenshot
    }

    private func waitForMacVisibleText(
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
        domain: "PleazyngStoreKitReviewCapture", code: 1,
        userInfo: [NSLocalizedDescriptionKey: "Timed out waiting for visible text \(expected.debugDescription); OCR saw: \(joinedText(latest))"])
    }

    private func tapMacVisibleText(
      _ expected: String, in app: XCUIApplication, attachmentName: String, timeout: TimeInterval = 30
    ) throws {
      let window = app.windows.firstMatch
      let button = app.buttons[expected]
      if button.exists && button.isHittable {
        button.tap()
        attachText("Used the real XCTest accessibility button for \(expected)", named: "\(attachmentName)-action")
        return
      }

      let lines = try waitForMacVisibleText(
        expected, in: app, attachmentName: "\(attachmentName)-before-action", timeout: timeout)
      let matchingLines = lines.lines.filter { $0.text.localizedCaseInsensitiveContains(expected) }
      guard matchingLines.count == 1, let match = matchingLines.first else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture", code: 2,
          userInfo: [NSLocalizedDescriptionKey: "Expected one visible OCR hit for \(expected.debugDescription), got \(matchingLines.count)"])
      }
      let offset = try macWindowOffset(forVisionBounds: match.boundingBox, geometry: lines.screenshot.geometry)
      attachText(
        "Used one visible OCR match for \(expected.debugDescription). OCR bounds in the cropped app screenshot: \(NSStringFromRect(match.boundingBox)); normalized window tap offset: (\(offset.dx), \(offset.dy)). Screenshot geometry: \(lines.screenshot.geometry.json(name: attachmentName)).",
        named: "\(attachmentName)-action")
      window.coordinate(withNormalizedOffset: offset).tap()
    }

    private func macWindowOffset(
      forVisionBounds bounds: CGRect, geometry: MacScreenshotGeometry
    ) throws -> CGVector {
      guard bounds.minX >= 0, bounds.minY >= 0, bounds.maxX <= 1, bounds.maxY <= 1,
            bounds.width > 0, bounds.height > 0 else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture", code: 5,
          userInfo: [NSLocalizedDescriptionKey: "Vision returned a text box outside normalized screenshot bounds: \(NSStringFromRect(bounds))"])
      }
      let offset = CGVector(dx: bounds.midX, dy: 1 - bounds.midY)
      guard offset.dx >= 0.02, offset.dx <= 0.98, offset.dy >= 0.02, offset.dy <= 0.98,
            geometry.outputImageSize.width > 0,
            geometry.outputImageSize.height > 0,
            abs(geometry.outputImageSize.width / geometry.outputImageSize.height
                - geometry.windowFrame.width / geometry.windowFrame.height) <= 0.02 else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture", code: 6,
          userInfo: [NSLocalizedDescriptionKey: "The OCR target is not safely inside the cropped app window: \(NSStringFromRect(bounds)); geometry: \(geometry.json(name: "tap"))"])
      }
      return offset
    }

    private func assertMacScreenshotGeometryScale() throws {
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

      let upperScreenText = CGRect(x: 0.1, y: 0.7, width: 0.2, height: 0.2)
      let lowerScreenText = CGRect(x: 0.1, y: 0.1, width: 0.2, height: 0.2)
      let geometry = fixtureGeometry(window)
      let upperOffset = try macWindowOffset(forVisionBounds: upperScreenText, geometry: geometry)
      let lowerOffset = try macWindowOffset(forVisionBounds: lowerScreenText, geometry: geometry)
      XCTAssertEqual(upperOffset.dx, 0.2, accuracy: 0.0001)
      XCTAssertEqual(upperOffset.dy, 0.2, accuracy: 0.0001)
      XCTAssertEqual(lowerOffset.dx, 0.2, accuracy: 0.0001)
      XCTAssertEqual(lowerOffset.dy, 0.8, accuracy: 0.0001)

      XCTAssertThrowsError(try resolveMacScreenshotMapping(
        rawImageSize: CGSize(width: 1024, height: 768),
        screenFrame: screen,
        windowFrame: CGRect(x: 300, y: 100, width: 800, height: 600)))
    }

    private func fixtureGeometry(_ window: CGRect) -> MacScreenshotGeometry {
      MacScreenshotGeometry(
        basisNames: ["window"],
        rawImageSize: CGSize(width: 800, height: 632),
        screenFrame: CGRect(x: 0, y: 0, width: 1024, height: 768),
        applicationFrame: window,
        windowFrame: window,
        selectedSourceFrame: window,
        cropRectInPixels: CGRect(x: 0, y: 0, width: 800, height: 632),
        outputImageSize: CGSize(width: 800, height: 632),
        scaleX: 1,
        scaleY: 1)
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
          domain: "PleazyngStoreKitReviewCapture", code: 7,
          userInfo: [NSLocalizedDescriptionKey: "Could not crop the app window \(NSStringFromRect(windowFrame)) from the main display \(NSStringFromRect(screenFrame)) at \(rawImageSize) pixels"])
      }
      return MacScreenshotMapping(
        basisNames: ["screen"], sourceFrame: screenFrame,
        cropRectInPixels: crop, scaleX: scaleX, scaleY: scaleY)
    }

    private func captureMacWindowScreenshot(of app: XCUIApplication) throws -> MacWindowScreenshot {
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
          domain: "PleazyngStoreKitReviewCapture", code: 10,
          userInfo: [NSLocalizedDescriptionKey: "The observed app window crop fell outside screenshot pixels: \(NSStringFromRect(mapping.cropRectInPixels)) of \(rawImageSize)"])
      }
      let output = NSMutableData()
      guard let destination = CGImageDestinationCreateWithData(output, "public.png" as CFString, 1, nil) else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture", code: 11,
          userInfo: [NSLocalizedDescriptionKey: "Could not create a PNG for the cropped app screenshot"])
      }
      CGImageDestinationAddImage(destination, croppedImage, nil)
      guard CGImageDestinationFinalize(destination) else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture", code: 12,
          userInfo: [NSLocalizedDescriptionKey: "Could not encode the cropped app screenshot as PNG"])
      }
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
      return MacWindowScreenshot(image: croppedImage, pngData: output as Data, geometry: geometry)
    }

    private func attachMacScreenshot(_ capture: MacWindowScreenshot, named name: String) {
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
            line.boundingBox.width, line.boundingBox.height,
          ],
        ]
      }
      guard let data = try? JSONSerialization.data(withJSONObject: records, options: [.sortedKeys]),
            let text = String(data: data, encoding: .utf8) else {
        return "Could not serialize native Vision OCR observations"
      }
      return text
    }
  #endif

  private func attachText(_ value: String, named name: String) {
    let attachment = XCTAttachment(string: value)
    attachment.name = name
    attachment.lifetime = .keepAlways
    add(attachment)
  }
}
