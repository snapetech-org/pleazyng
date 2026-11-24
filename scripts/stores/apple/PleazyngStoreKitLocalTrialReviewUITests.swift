import Foundation
import StoreKit
import StoreKitTest
import XCTest
#if os(macOS)
import CoreGraphics
import ImageIO
import Vision

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
      let trialButton = app.buttons["Start my 7-day free trial"]
      guard trialButton.waitForExistence(timeout: 90) else {
        try attachScreenshot(of: app, named: "pleazyng-diagnostic-trial-offer-timeout-tvos")
        attachText(app.debugDescription, named: "pleazyng-diagnostic-trial-offer-timeout-accessibility-tvos")
        XCTFail("The actual product trial offer did not load")
        return
      }
      XCTAssertTrue(trialButton.isEnabled, "The actual product trial offer is unavailable")
      let lifetimeOffer = app.buttons.matching(
        NSPredicate(format: "label CONTAINS %@ AND label CONTAINS %@", "Unlock every feature", "$2.99")
      ).firstMatch
      XCTAssertTrue(lifetimeOffer.waitForExistence(timeout: 60), "The scheme-associated LOCALTEST catalog did not load")
      XCTAssertTrue(
        app.staticTexts.containing(
          NSPredicate(format: "label CONTAINS[c] %@", "There is no subscription or automatic charge")
        )
        .firstMatch.exists,
        "The actual product trial disclosure was not visible"
      )
      trialOfferShown = trialButton.exists
      trialOfferLifetimePriceShown = lifetimeOffer.exists
      lifetimeDisplayPrice = lifetimeOffer.label
      try attachScreenshot(of: app, named: "pleazyng-localtest-trial-offer-\(platformName())")
      select(trialButton, in: app)
    #endif

    #if os(macOS)
      _ = try waitForMacVisibleText(
        "Use files and books", in: app, attachmentName: "pleazyng-diagnostic-after-trial-purchase-macos",
        timeout: 90)
    #else
      let useFiles = app.buttons["Use files and books"]
      XCTAssertTrue(useFiles.waitForExistence(timeout: 90), "The app did not unlock after its LOCALTEST trial purchase")
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
      select(useFiles, in: app)
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
      let addSource = app.buttons["Add source"]
      XCTAssertTrue(addSource.waitForExistence(timeout: 60), "The product did not open its local Files and Books screen")
      XCTAssertTrue(app.buttons["Import files"].exists, "The local library import action was missing")
      XCTAssertTrue(
        app.staticTexts.containing(NSPredicate(format: "label CONTAINS[c] %@", "Add a source or import a book"))
          .firstMatch.exists,
        "Expected the actual empty local library, without seeded media or a server"
      )
      filesBooksScreenOpened = addSource.exists
      try attachScreenshot(of: app, named: "pleazyng-localtest-files-books-\(platformName())")
    #endif

    openSettings(in: app)
    #if os(macOS)
      _ = try waitForMacVisibleText(
        "Pleazyng lifetime access", in: app, attachmentName: "pleazyng-diagnostic-settings-macos",
        timeout: 60)
      try tapMacVisibleText(
        "Pleazyng lifetime access", in: app, attachmentName: "pleazyng-macos-lifetime-settings-action")
    #endif
    #if os(tvOS)
      let lifetimeTile = app.descendants(matching: .any).matching(
        NSPredicate(format: "label CONTAINS %@", "Pleazyng lifetime access")
      ).firstMatch
      XCTAssertTrue(lifetimeTile.waitForExistence(timeout: 60), "The actual product Settings row was not available")
      select(lifetimeTile, in: app)
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
      let activeTrial = app.staticTexts["Your 7-day full-feature trial is active."]
      let restoreButton = app.buttons["Restore lifetime purchase"]
      let lifetimePrice = app.buttons.matching(
        NSPredicate(format: "label CONTAINS %@ AND label CONTAINS %@", "Unlock every feature", "$2.99")
      ).firstMatch
      XCTAssertTrue(
        activeTrial.waitForExistence(timeout: 60), "The actual app did not restore the native trial entitlement")
      XCTAssertTrue(restoreButton.waitForExistence(timeout: 60), "The real access sheet did not show its Restore action")
      XCTAssertTrue(
        lifetimePrice.waitForExistence(timeout: 60), "The app did not show the queried LOCALTEST lifetime price")
      activeTrialShown = activeTrial.exists
      accessSheetLifetimePriceShown = lifetimePrice.exists
      try attachScreenshot(of: app, named: "pleazyng-localtest-supporter-\(platformName())")
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

  private func select(_ element: XCUIElement, in app: XCUIApplication) {
    #if os(tvOS)
      let deadline = Date().addingTimeInterval(45)
      while !element.hasFocus && Date() < deadline {
        XCUIRemote.shared.press(.down)
        Thread.sleep(forTimeInterval: 0.2)
      }
      XCTAssertTrue(element.hasFocus, "The requested actual tvOS control did not receive remote focus")
      XCUIRemote.shared.press(.select)
    #else
      XCTAssertTrue(element.isHittable, "The requested actual Mac control is not hittable")
      element.tap()
    #endif
  }

  private func openSettings(in app: XCUIApplication) {
    #if os(tvOS)
      let homeTab = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", "Home")).firstMatch
      XCTAssertTrue(homeTab.waitForExistence(timeout: 30), "The actual Home navigation action was not available")
      select(homeTab, in: app)
      let profilesMenu = app.buttons["Profiles"]
      XCTAssertTrue(profilesMenu.waitForExistence(timeout: 30), "The actual Profiles menu was not available")
      select(profilesMenu, in: app)
      let settingsItem = app.buttons["Settings"]
      XCTAssertTrue(settingsItem.waitForExistence(timeout: 30), "The actual Settings action was not available")
      select(settingsItem, in: app)
    #else
      app.activate()
      app.typeKey(",", modifierFlags: .command)
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

  #if os(macOS)
    private struct MacScreenText {
      let text: String
      let boundingBox: CGRect
    }

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
          "capture_api": "XCUIApplication.screenshot()",
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
      let lines: [MacScreenText]
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
      var latest: [MacScreenText] = []
      var latestCapture: MacWindowScreenshot?
      repeat {
        let capture = try captureMacWindowScreenshot(of: app)
        latestCapture = capture
        latest = try recognizeMacScreenText(in: capture.image)
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
        rawImageSize: CGSize(width: 1024, height: 768),
        screenFrame: screen,
        applicationFrame: screen,
        windowFrame: window)
      XCTAssertEqual(fullScreenMapping.cropRectInPixels, window)
      XCTAssertTrue(fullScreenMapping.basisNames.contains("screen"))

      let windowOnlyMapping = try resolveMacScreenshotMapping(
        rawImageSize: CGSize(width: 1600, height: 1264),
        screenFrame: screen,
        applicationFrame: window,
        windowFrame: window)
      XCTAssertEqual(windowOnlyMapping.cropRectInPixels, CGRect(x: 0, y: 0, width: 1600, height: 1264))
      XCTAssertTrue(windowOnlyMapping.basisNames.contains("window"))

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
        applicationFrame: screen,
        windowFrame: CGRect(x: 100, y: 100, width: 800, height: 600)))
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
      rawImageSize: CGSize,
      screenFrame: CGRect,
      applicationFrame: CGRect,
      windowFrame: CGRect
    ) throws -> MacScreenshotMapping {
      let rawBounds = CGRect(origin: .zero, size: rawImageSize)
      let candidates: [(String, CGRect)] = [
        ("screen", screenFrame), ("application", applicationFrame), ("window", windowFrame),
      ]
      var mappings: [MacScreenshotMapping] = []
      for (name, sourceFrame) in candidates {
        guard sourceFrame.width > 0, sourceFrame.height > 0,
              windowFrame.minX >= sourceFrame.minX - 1,
              windowFrame.minY >= sourceFrame.minY - 1,
              windowFrame.maxX <= sourceFrame.maxX + 1,
              windowFrame.maxY <= sourceFrame.maxY + 1 else { continue }
        let scaleX = rawImageSize.width / sourceFrame.width
        let scaleY = rawImageSize.height / sourceFrame.height
        guard scaleX > 0, scaleY > 0,
              abs(scaleX - scaleY) / max(scaleX, scaleY) <= 0.02 else { continue }
        // XCTest frames use the same top-left screen coordinate basis as the
        // screenshot PNG's pixel rows; CGImage cropping uses pixel coordinates.
        let crop = CGRect(
          x: (windowFrame.minX - sourceFrame.minX) * scaleX,
          y: (windowFrame.minY - sourceFrame.minY) * scaleY,
          width: windowFrame.width * scaleX,
          height: windowFrame.height * scaleY).integral
        guard rawBounds.contains(crop),
              crop.width > 0, crop.height > 0 else { continue }
        if let existing = mappings.firstIndex(where: { abs($0.cropRectInPixels.minX - crop.minX) < 0.5
          && abs($0.cropRectInPixels.minY - crop.minY) < 0.5
          && abs($0.cropRectInPixels.width - crop.width) < 0.5
          && abs($0.cropRectInPixels.height - crop.height) < 0.5 }) {
          mappings[existing].basisNames.append(name)
        } else {
          mappings.append(MacScreenshotMapping(
            basisNames: [name], sourceFrame: sourceFrame,
            cropRectInPixels: crop, scaleX: scaleX, scaleY: scaleY))
        }
      }
      guard mappings.count == 1, let mapping = mappings.first else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture", code: 7,
          userInfo: [NSLocalizedDescriptionKey: "Could not identify one unambiguous app-window crop from screenshot pixels \(rawImageSize), screen \(NSStringFromRect(screenFrame)), app \(NSStringFromRect(applicationFrame)), window \(NSStringFromRect(windowFrame)); valid mappings: \(mappings.count)"])
      }
      return mapping
    }

    private func captureMacWindowScreenshot(of app: XCUIApplication) throws -> MacWindowScreenshot {
      let window = app.windows.firstMatch
      guard window.waitForExistence(timeout: 30) else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture", code: 8,
          userInfo: [NSLocalizedDescriptionKey: "The app window was not available for a supported XCTest screenshot"])
      }
      let screenshot = app.screenshot()
      guard let source = CGImageSourceCreateWithData(screenshot.pngRepresentation as CFData, nil),
            let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(
          domain: "PleazyngStoreKitReviewCapture", code: 9,
          userInfo: [NSLocalizedDescriptionKey: "XCUIApplication.screenshot() did not provide a decodable PNG"])
      }
      let rawImageSize = CGSize(width: image.width, height: image.height)
      let screenFrame = CGDisplayBounds(CGMainDisplayID())
      let applicationFrame = app.frame
      let windowFrame = window.frame
      let mapping = try resolveMacScreenshotMapping(
        rawImageSize: rawImageSize,
        screenFrame: screenFrame,
        applicationFrame: applicationFrame,
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

    private func recognizeMacScreenText(in image: CGImage) throws -> [MacScreenText] {
      let request = VNRecognizeTextRequest()
      request.recognitionLevel = .accurate
      request.usesLanguageCorrection = false
      request.recognitionLanguages = ["en-US"]
      try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
      return (request.results ?? []).compactMap { observation in
        guard let text = observation.topCandidates(1).first?.string else { return nil }
        return MacScreenText(text: text, boundingBox: observation.boundingBox)
      }
    }

    private func joinedText(_ lines: [MacScreenText]) -> String {
      lines.map(\.text).joined(separator: " ")
    }

    private func macScreenTextJSON(_ lines: [MacScreenText]) -> String {
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
