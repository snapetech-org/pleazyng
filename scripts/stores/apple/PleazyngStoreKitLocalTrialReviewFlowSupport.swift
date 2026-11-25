import Foundation
import StoreKit
import StoreKitTest
import XCTest

#if os(macOS)
import AppKit
#endif

private struct TrialOfferEvidence {
  let shown: Bool
  let lifetimePriceShown: Bool
  let lifetimeDisplayPrice: String
}

private struct TrialTransactionEvidence {
  let productID: String
  let state: String
  let transactionID: String
  let originalTransactionID: String
  let purchaseDate: Date
  let expirationDate: Any
  let cancelDate: Any
}

private struct TrialAccessEvidence {
  let activeTrialShown: Bool
  let lifetimePriceShown: Bool
}

private struct LocalTestCaptureEvidence {
  let offer: TrialOfferEvidence
  let trial: TrialTransactionEvidence
  let access: TrialAccessEvidence
  let filesBooksScreenOpened: Bool
  let transactionCountAfterTrial: Int
  let transactions: [[String: Any]]
}

extension PleazyngStoreKitLocalTrialReviewUITests {
  func runLocalTrialCapture() throws {
#if os(macOS)
    _ = NSApplication.shared
    try assertMacScreenshotGeometryScale()
#endif
    let session = try makeLocalTestSession()
    let app = XCUIApplication(bundleIdentifier: appBundleID)
    try launchProductApp(app)
    let offer = try captureTrialOfferAndStart(app, session: session)
    let trial = try completeTrialAndOpenFiles(app, session: session)
    let access = try captureTrialAccessSheet(app)
    let transactions = try verifyFinalTransactions(session, matching: trial)
    let evidence = LocalTestCaptureEvidence(
      offer: offer,
      trial: trial,
      access: access,
      filesBooksScreenOpened: true,
      transactionCountAfterTrial: session.allTransactions().count,
      transactions: transactions
    )
    attachLocalTestRecord(evidence)
  }

  private func makeLocalTestSession() throws -> SKTestSession {
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
    return session
  }

  private func launchProductApp(_ app: XCUIApplication) throws {
    app.launch()
#if os(macOS)
    try resizeMacWindowForStoreScreenshot(app)
    let initialCapture = try captureMacWindowScreenshot(of: app)
    try assertMacListingWindowGeometry(initialCapture)
    attachMacScreenshot(initialCapture, named: "pleazyng-diagnostic-after-launch-macos")
#else
    try attachScreenshot(of: app, named: "pleazyng-diagnostic-after-launch-\(platformName())")
#endif
    attachText(app.debugDescription, named: "pleazyng-diagnostic-accessibility-after-launch-\(platformName())")
  }

  private func captureTrialOfferAndStart(
    _ app: XCUIApplication,
    session: SKTestSession
  ) throws -> TrialOfferEvidence {
#if os(macOS)
    try clickMacVisibleText("Start fresh", in: app, attachmentName: "pleazyng-macos-legacy-onboarding")
    let afterOnboarding = try waitForMacVisibleText(
      "Start my 7-day free trial", in: app,
      attachmentName: "pleazyng-diagnostic-after-onboarding-macos", timeout: 90
    )
    XCTAssertTrue(
      joinedText(afterOnboarding.lines).contains("Read, listen, and watch from your own files"),
      "The actual first-run app access screen did not appear after choosing Start fresh"
    )
    let offer = try waitForMacVisibleText(
      "Start my 7-day free trial", in: app,
      attachmentName: "pleazyng-localtest-trial-offer-macos", timeout: 60
    )
    let offerText = joinedText(offer.lines)
    XCTAssertTrue(offerText.contains("Unlock every feature"), "The queried LOCALTEST lifetime product was not visible")
    XCTAssertTrue(offerText.contains("$2.99"), "The queried LOCALTEST lifetime price was not visible")
    XCTAssertTrue(
      offerText.contains("There is no subscription or automatic charge"),
      "The actual product trial disclosure was not visible"
    )
    guard session.disableDialogs else {
      throw NSError(
        domain: "PleazyngStoreKitReviewCapture",
        code: 14,
        userInfo: [NSLocalizedDescriptionKey: "The Mac StoreKit test session did not apply its local dialog setting"]
      )
    }
    try clickMacVisibleText(
      "Start my 7-day free trial", in: app, attachmentName: "pleazyng-macos-trial-offer-action"
    )
    return TrialOfferEvidence(
      shown: true,
      lifetimePriceShown: offerText.contains("Unlock every feature") && offerText.contains("$2.99"),
      lifetimeDisplayPrice: offer.lines.first(where: { $0.text.contains("$2.99") })?.text ?? "$2.99"
    )
#else
    let offerText = try waitForTVScreen(
      ["Start my 7-day free trial", "Unlock every feature", "$2.99", "no subscription or automatic charge"],
      in: app, attachmentName: "pleazyng-localtest-trial-offer-tvos", timeout: 90
    )
    // The real free-trial CTA starts with focus; transaction evidence verifies it.
    XCUIRemote.shared.press(.select)
    return TrialOfferEvidence(
      shown: true,
      lifetimePriceShown: offerText.contains("$2.99"),
      lifetimeDisplayPrice: "$2.99"
    )
#endif
  }
}

extension PleazyngStoreKitLocalTrialReviewUITests {
  private func completeTrialAndOpenFiles(
    _ app: XCUIApplication,
    session: SKTestSession
  ) throws -> TrialTransactionEvidence {
#if os(macOS)
    _ = try waitForMacVisibleText(
      "Use files and books", in: app,
      attachmentName: "pleazyng-diagnostic-after-trial-purchase-macos", timeout: 90
    )
#else
    _ = try waitForTVScreen(
      ["Use files and books"], in: app,
      attachmentName: "pleazyng-diagnostic-after-trial-purchase-tvos", timeout: 90
    )
#endif
    let trialTransactions = session.allTransactions()
    XCTAssertEqual(trialTransactions.count, 1, "The actual product CTA must create only one LOCALTEST trial")
    let original = try XCTUnwrap(trialTransactions.first)
    XCTAssertEqual(original.productIdentifier, trialProductID)
    XCTAssertEqual(original.state, .purchased)
    XCTAssertFalse(trialTransactions.contains { $0.productIdentifier == lifetimeProductID })

#if os(macOS)
    try clickMacVisibleText("Use files and books", in: app, attachmentName: "pleazyng-macos-use-files-action")
    let files = try waitForMacVisibleText(
      "Add a source or import a book", in: app,
      attachmentName: "pleazyng-localtest-files-books-macos", timeout: 60
    )
    let filesText = joinedText(files.lines)
    XCTAssertTrue(filesText.contains("Add source"), "The real local library Add source action is missing")
    XCTAssertTrue(filesText.contains("Import files"), "The real local library Import files action is missing")
#else
    // The TV QR CTA takes focus before Jellyfin, Emby, Plex and Files and Books.
    for _ in 0..<4 {
      XCUIRemote.shared.press(.down)
      Thread.sleep(forTimeInterval: 0.25)
    }
    try attachScreenshot(of: app, named: "pleazyng-diagnostic-before-use-files-tvos")
    XCUIRemote.shared.press(.select)
    _ = try waitForTVScreen(
      ["Files and books", "Read, listen, and watch from your own files", "Open media"],
      in: app, attachmentName: "pleazyng-diagnostic-files-books-arrival-tvos", timeout: 60
    )
    try captureTVLocalLibrary(in: app)
#endif
    return transactionEvidence(original)
  }

  private func transactionEvidence(_ transaction: SKTestTransaction) -> TrialTransactionEvidence {
    TrialTransactionEvidence(
      productID: transaction.productIdentifier,
      state: transaction.state == .purchased ? "purchased" : "other",
      transactionID: String(transaction.identifier),
      originalTransactionID: String(transaction.originalTransactionIdentifier),
      purchaseDate: transaction.purchaseDate,
      expirationDate: optionalDate(transaction.expirationDate),
      cancelDate: optionalDate(transaction.cancelDate)
    )
  }

  private func captureTrialAccessSheet(_ app: XCUIApplication) throws -> TrialAccessEvidence {
#if os(macOS)
    app.activate()
    app.typeKey(",", modifierFlags: .command)
    _ = try waitForMacVisibleText(
      "Pleazyng lifetime access", in: app,
      attachmentName: "pleazyng-diagnostic-settings-macos", timeout: 60
    )
    try clickMacVisibleText(
      "Pleazyng lifetime access", in: app, attachmentName: "pleazyng-macos-lifetime-settings-action"
    )
    let access = try waitForMacVisibleText(
      "Your 7-day full-feature trial is active.", in: app,
      attachmentName: "pleazyng-localtest-supporter-macos", timeout: 60
    )
    let text = joinedText(access.lines)
    XCTAssertTrue(text.contains("Restore lifetime purchase"), "The real access sheet did not show its Restore action")
    XCTAssertTrue(text.contains("$2.99"), "The app did not show the queried LOCALTEST lifetime price")
    return TrialAccessEvidence(activeTrialShown: true, lifetimePriceShown: text.contains("$2.99"))
#else
    try openTVSettings(in: app)
    _ = try waitForTVScreen(
      ["Settings", "Pleazyng lifetime access"], in: app,
      attachmentName: "pleazyng-diagnostic-settings-tvos", timeout: 60
    )
    // Settings focuses its first tile in Store builds.
    XCUIRemote.shared.press(.select)
    let accessText = try waitForTVScreen(
      ["Your 7-day full-feature trial is active", "Restore lifetime purchase", "$2.99"],
      in: app, attachmentName: "pleazyng-localtest-supporter-tvos", timeout: 60
    )
    return TrialAccessEvidence(activeTrialShown: true, lifetimePriceShown: accessText.contains("$2.99"))
#endif
  }

  private func verifyFinalTransactions(
    _ session: SKTestSession,
    matching original: TrialTransactionEvidence
  ) throws -> [[String: Any]] {
    let transactions = session.allTransactions()
    XCTAssertEqual(transactions.count, 1, "Opening product screens must not change the local transaction history")
    let finalTrial = try XCTUnwrap(transactions.first)
    XCTAssertEqual(finalTrial.productIdentifier, trialProductID)
    XCTAssertEqual(finalTrial.state, .purchased)
    XCTAssertEqual(String(finalTrial.originalTransactionIdentifier), original.originalTransactionID)
    XCTAssertEqual(finalTrial.purchaseDate, original.purchaseDate)
    XCTAssertFalse(transactions.contains { $0.productIdentifier == lifetimeProductID })
    return transactions.map { transaction in
      [
        "product_id": transaction.productIdentifier,
        "state": transaction.state == .purchased ? "purchased" : "other",
        "transaction_id": String(transaction.identifier),
        "original_transaction_id": String(transaction.originalTransactionIdentifier),
        "purchase_date": ISO8601DateFormatter().string(from: transaction.purchaseDate),
        "expiration_date": optionalDate(transaction.expirationDate),
        "cancel_date": optionalDate(transaction.cancelDate)
      ]
    }
  }
}

extension PleazyngStoreKitLocalTrialReviewUITests {
  private func attachLocalTestRecord(_ evidence: LocalTestCaptureEvidence) {
    let record = localTestRecord(evidence)
    guard let data = try? JSONSerialization.data(withJSONObject: record, options: [.sortedKeys]) else {
      XCTFail("Could not encode the LOCALTEST transaction record")
      return
    }
    let attachment = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
    attachment.name = "pleazyng-storekit-macos-tvos-localtest-record"
    attachment.lifetime = .keepAlways
    add(attachment)
  }

  private func localTestRecord(_ evidence: LocalTestCaptureEvidence) -> [String: Any] {
    let record: [String: Any] = [
      "environment": "LOCALTEST",
      "qualification": "The actual product UI started a native Apple StoreKitTest free-trial transaction " +
        "using the exact scheme-associated local catalog. This is local test evidence only, not App Store " +
        "Connect, App Store Sandbox, or live purchase evidence.",
      "storefront": "USA",
      "catalog_queries": [
        "trial_product_id": trialProductID,
        "lifetime_product_id": lifetimeProductID,
        "lifetime_display_price": evidence.offer.lifetimeDisplayPrice
      ],
      "transactions": evidence.transactions,
      "history": [
        "beforeAppLaunch": ["transactionCount": 0],
        "afterProductTrialCTA": [
          "transactionCount": evidence.transactionCountAfterTrial,
          "originalTransactionId": evidence.trial.originalTransactionID,
          "purchaseDate": ISO8601DateFormatter().string(from: evidence.trial.purchaseDate)
        ],
        "afterProductNavigation": [
          "transactionCount": evidence.transactions.count,
          "originalTransactionId": evidence.trial.originalTransactionID,
          "purchaseDate": ISO8601DateFormatter().string(from: evidence.trial.purchaseDate)
        ]
      ],
      "app_behavior": [
        "trial_offer_shown": evidence.offer.shown,
        "trial_offer_lifetime_price_shown": evidence.offer.lifetimePriceShown,
        "trial_started_via_product_ui": true,
        "transaction_created_outside_product_app_ui": false,
        "active_trial_restored_by_app": evidence.access.activeTrialShown,
        "files_and_books_screen_opened": evidence.filesBooksScreenOpened,
        "active_trial_sheet_shown": evidence.access.activeTrialShown,
        "lifetime_price_shown": evidence.access.lifetimePriceShown,
        "lifetime_purchase_activated": false,
        "lifetime_transaction_created": false
      ],
      "raw_jws_included": false
    ]
    return record
  }
}
